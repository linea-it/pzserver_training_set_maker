#!/bin/bash

echo "Running test script..."
echo `pwd`
SCRIPT_PATH=$(readlink -f "$0")
echo absolute path: "$SCRIPT_PATH"
echo scriptname: $(basename "$SCRIPT_PATH")
echo dirname: $(dirname "$SCRIPT_PATH")