#!/bin/sh
# run-repro.sh DIR [PART]: run DIR/repro/run.sh (a minimised repro appended to an image as its own cpio by repro.sh).
cd "$1/repro" && exec sh ./run.sh
