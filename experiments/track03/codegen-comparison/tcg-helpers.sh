#!/bin/sh
set -eu
cd /work
cp -a /input/upstream upstream
cp -a /input/build build
mkdir outputs
python3 source/tcg-helpers.py --inside
