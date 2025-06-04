#!/usr/bin/env python


import BaseHTTPServer
import CGIHTTPServer
import hashlib
import json
import optparse
import os
import Queue
import random
import signal
import SocketServer
import string
import threading
import time
import urllib


block_size = 100
countdown_window = block_size * 4


class AccessGuard:
    def __init__(self):
        self.countdown = random.randint(countdown_window, countdown_window + 100)
        self.lock = threading.RLock()

    def is_blocked(self):
        with self.lock:
            if self.countdown == 0:
                return True
            self.countdown -= 1
            return False

    def reset(self):
        with self.lock:
            self.countdown = random.randint(countdown_window, countdown_window + 100)


class StateModel():
    def __init__(self):
        self.lock = threading.RLock()
        self.max = 1000
        self.count = 0
        self.done = False
        self.drained_a = False
        self.drained_b = False
        self.results_blocks = []
        self.source_a_queue = Queue.Queue()
        self.source_b_queue = Queue.Queue()

    def set_max(self, x):
        self.max = x

    def get_source_a_next(self):
        with self.lock:
            if self.source_a_queue.empty():
                self.feed_queues()
            if self.source_a_queue.empty() and self.done:
                self.drained_a = True
                return (None, None)
            return self.source_a_queue.get()

    def get_source_b_next(self):
        with self.lock:
            if self.source_b_queue.empty():
                self.feed_queues()
            if self.source_b_queue.empty() and self.done:
                self.drained_b = True
                return (None, None)
            return self.source_b_queue.get()

    def generate_block(self, low, high):
        data = []
        for i in range(low, high):
            data.append((i, self.generate_kind()))
        random.shuffle(data)
        return data

    def feed_queues(self):
        with self.lock:
            if self.done:
                return
            low = self.count
            high = max(low + random.randint(block_size, block_size * 2), self.max)
            if high >= self.max:
                self.done = True
            self.count = high
            block = self.generate_block(low, high)
            for (i, kind) in block:
                self.queue_datum(i, kind)
            self.results_blocks.append(block)

    def get_results_blocks(self):
        with self.lock:
            if self.results_blocks:
                rb = self.results_blocks
                self.results_blocks = []
                return rb
            else:
                return []

    def is_done(self):
        with self.lock:
            return self.done

    def generate_kind(self):
        n = random.randint(0, 100)  # a percentage
        if n < 5:
            return KIND.ORPHAN_A
        elif n < 10:
            return KIND.ORPHAN_B
        elif n < 13:
            return KIND.DEFECT_A
        elif n < 16:
            return KIND.DEFECT_B
        else:
            return KIND.JOINED

    def queue_datum(self, i, kind):
        if kind == KIND.ORPHAN_A:
            with self.lock:
                self.source_a_queue.put((i, True))
        elif kind == KIND.ORPHAN_B:
            with self.lock:
                self.source_b_queue.put((i, True))
        elif kind == KIND.DEFECT_A:
            with self.lock:
                self.source_a_queue.put((i, False))
        elif kind == KIND.DEFECT_B:
            with self.lock:
                self.source_b_queue.put((i, False))
        else:
            with self.lock:
                self.source_a_queue.put((i, True))
                self.source_b_queue.put((i, True))

    def is_complete(self):
        with self.lock:
            return self.done and self.drained_a and self.drained_b and not self.results_blocks


class ChallengeFixture(CGIHTTPServer.CGIHTTPRequestHandler):

    guard_source_a = AccessGuard()
    guard_source_b = AccessGuard()
    guard_sink_a = AccessGuard()
    state_model = StateModel()
    outs = Queue.Queue()

    def do_GET(self):
        collapsed_path = CGIHTTPServer._url_collapse_path(urllib.unquote(self.path))
        if collapsed_path.endswith("/") and collapsed_path != "/":
            collapsed_path = collapsed_path[:-1]
        if collapsed_path == "/source/a":
            return self.do_source_a()
        elif collapsed_path == "/source/b":
            return self.do_source_b()
        else:
            self.send_error(404, "No such endpoint (%s)" % collapsed_path)
            return

    def do_POST(self):
        collapsed_path = CGIHTTPServer._url_collapse_path(urllib.unquote(self.path))
        if collapsed_path.endswith("/") and collapsed_path != "/":
            collapsed_path = collapsed_path[:-1]
        if collapsed_path == "/sink/a":
            return self.do_sink_a()
        else:
            self.send_error(404, "No such endpoint (%s)" % collapsed_path)
            return

    def do_source_a(self):
        if self.guard_source_a.is_blocked():
            self.send_error(406, "you gotta read or write somewhere else first")
            return
        self.guard_source_b.reset()
        self.guard_sink_a.reset()
        self.send_response(200, "success")
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        datum = self.state_model.get_source_a_next()
        if datum is None:
            self.send_error(406, "nothing else at the moment")
            return
        self.wfile.write(generate_json(datum))

    def do_source_b(self):
        if self.guard_source_b.is_blocked():
            self.send_error(406, "you gotta read or write somewhere else first")
            return
        self.guard_source_a.reset()
        self.guard_sink_a.reset()
        self.send_response(200, "success")
        self.send_header("Content-Type", "application/xml")
        self.end_headers()
        datum = self.state_model.get_source_b_next()
        if datum is None:
            self.send_error(406, "nothing else at the moment")
            return
        self.wfile.write(generate_xml(datum))

    def do_sink_a(self):
        if self.guard_sink_a.is_blocked():
            self.send_error(406, "you gotta read somewhere else first")
            return
        self.guard_source_a.reset()
        self.guard_source_b.reset()
        try:
            bytes = int(self.headers['Content-Length'])
            data = json.loads(self.rfile.read(bytes))
            if set(["id", "kind"]) != set(data.keys()):
                raise ValueError
            self.outs.put((data['id'], data['kind']))
            self.send_response(200, "success")
            resp = '{"status": "ok"}'
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(resp))
            self.end_headers()
            self.wfile.write(resp)
        except Exception, e:
            print "EXEC: " + str(e)
            resp = '{"status": "fail"}'
            self.send_response(200, "bad result")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(resp))
            self.end_headers()
            self.wfile.write(resp)


def results_dumper(model, results_file):
    def kind_op(k):
        if k == KIND.ORPHAN_A or k == KIND.ORPHAN_B:
            return "orphaned"
        elif k == KIND.DEFECT_A or k == KIND.DEFECT_B:
            return "defect"
        else:
            return "joined"

    with open(results_file, "w") as f:
        while True:
            blocks = model.get_results_blocks()
            if not blocks and model.is_done():
                return
            for block in blocks:
                for (i, kind) in block:
                    op = kind_op(kind)
                    if op != "defect":
                        print >> f, "%s %s" % (op, hash_key(i))
            time.sleep(0.3)


def write_out(outs, out_file):
    with open(out_file, "w") as f:
        while True:
            if not outs.empty():
                (i, kind) = outs.get()
                print >> f, "%s %s" % (kind, i)
                f.flush()
            else:
                time.sleep(1)


def is_sentinel(x):
    """x can never be None"""
    (id, kind) = x
    return id is None and kind is None


def shepherd(model):
    while True:
        if model.is_complete():
            print "Terminating in six seconds"
            time.sleep(6)  # yeah, ok, this is really hacky, but it's late at night
            os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(1)


def hash_key(i):
    return hashlib.md5(str(i)*13).hexdigest()


def random_string(n):
    return ''.join(random.SystemRandom().choice(string.ascii_uppercase + string.digits) for _ in range(n))


def generate_json(datum):
    if is_sentinel(datum):
        return '{"status": "done"}'
    (i, valid) = datum
    if valid:
        return '{"status": "ok", "id": "%s"}' % hash_key(i)
    else:
        return '{"status": "ok", "id": [%s [}' % random_string(random.randint(10, 100))


def generate_xml(datum):
    if is_sentinel(datum):
        return '<?xml version="1.0" encoding="UTF-8"?><msg><done/></msg>'
    (i, valid) = datum
    if valid:
        return '<?xml version="1.0" encoding="UTF-8"?><msg><id value="%s"/></msg>' % hash_key(i)
    else:
        return '<?xml version="1.0" encoding="UTF-8"?><msg><%s</foo></msg>' % random_string(random.randint(10, 100))


class KIND:
    ORPHAN_A = "ORPHAN_A"
    ORPHAN_B = "ORPHAN_B"
    DEFECT_A = "DEFECT_A"
    DEFECT_B = "DEFECT_B"
    JOINED = "JOINED"


class ThreadingSimpleServer(SocketServer.ThreadingMixIn,
                            BaseHTTPServer.HTTPServer):
    pass


def main():
    parser = optparse.OptionParser()
    parser.add_option('-f', '--file', dest='results_file', default='expected.txt',
                      help='write master data to FILE', metavar='FILE')
    parser.add_option('-o', '--outfile', dest='output_file', default='submitted.txt',
                      help='write submitted results to FILE', metavar='FILE')
    parser.add_option('-n', '--num', dest='num', default=1000,
                      help='send NUM messages', metavar='NUM', )
    parser.add_option('--host', dest='host', default='',
                      help='host to connect to HOST', metavar='HOST')
    parser.add_option('-p', '--port', dest='port', default=7299,
                      help='listen on PORT', metavar='PORT')
    (opts, args) = parser.parse_args()

    ChallengeFixture.state_model.set_max(int(opts.num))
    threading.Thread(target=write_out, args=(ChallengeFixture.outs, opts.output_file)).start()
    threading.Thread(target=results_dumper, args=(ChallengeFixture.state_model, opts.results_file)).start()
    threading.Thread(target=shepherd, args=(ChallengeFixture.state_model, )).start()
    server_address = ('', int(opts.port))
    httpd = ThreadingSimpleServer(server_address, ChallengeFixture)
    httpd.serve_forever()


if __name__ == '__main__':
    main()
