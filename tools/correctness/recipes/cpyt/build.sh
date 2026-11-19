#!/bin/bash
# build.sh: the cpyt campaign image (the CPython regression tests: the native build tree, run-cpy.sh per part). The recipe is ../cpy/build.sh, shared with
# the other CPython suite; this folder only names the suite (testkit/inputs.py:campaign_image('cpyt')).
SUITE=cpyt
. /recipe/correctness/recipes/cpy/build.sh
