#!/bin/sh
# Run inside the pinned Linux ARM64 image on a fresh source/dependency snapshot.
set -eu
test "$#" -eq 2
natemu_fex_source=$1
natemu_fex_build=$2
natemu_adapter_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
git -C "$natemu_fex_source" apply "$natemu_adapter_dir/root.patch"
python3 "$natemu_adapter_dir/pressure_adapt.py" "$natemu_fex_source"
python3 "$natemu_adapter_dir/store_adapt.py" "$natemu_fex_source"
python3 "$natemu_adapter_dir/fex_export.py" "$natemu_fex_source"
cmake -S "$natemu_fex_source" -B "$natemu_fex_build" -G Ninja \
    -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
    -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF -DBUILD_FEXCONFIG=OFF \
    -DBUILD_THUNKS=OFF -DENABLE_LTO=OFF -DENABLE_VIXL_DISASSEMBLER=ON \
    -DENABLE_VIXL_SIMULATOR=OFF -DENABLE_CCACHE=OFF
cmake --build "$natemu_fex_build" --target CodeSizeValidation --parallel 2
