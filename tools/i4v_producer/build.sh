#!/bin/sh
set -eu
source=$1
build=$2
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python3 "$here/../i3_producer/fex_export.py" "$source"
python3 "$here/adapt.py" "$source"
cmake -S "$source" -B "$build" -G Ninja \
  -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF -DBUILD_FEXCONFIG=OFF \
  -DBUILD_THUNKS=OFF -DENABLE_LTO=OFF -DENABLE_VIXL_DISASSEMBLER=ON \
  -DENABLE_VIXL_SIMULATOR=OFF -DENABLE_CCACHE=OFF
cmake --build "$build" --target CodeSizeValidation --parallel 2
