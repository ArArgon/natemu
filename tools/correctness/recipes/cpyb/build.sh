#!/bin/bash
# build.sh: the cpyb campaign image (the CPython build in the guest: the pristine source, run-cpyb.sh). The recipe is ../cpy/build.sh, shared with
# the other CPython suite; this folder only names the suite (testkit/inputs.py:campaign_image('cpyb')).
SUITE=cpyb
. /recipe/correctness/recipes/cpy/build.sh
