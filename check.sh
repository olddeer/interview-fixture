#!/usr/bin/env bash

DIFF_OUTPUT=$(diff submitted.txt expected.txt)
 
 if [ $? -eq 0 ]; then
     echo "good"
     exit 0
 else
     echo "$DIFF_OUTPUT"
     echo "bad"
     exit 1
 fi
