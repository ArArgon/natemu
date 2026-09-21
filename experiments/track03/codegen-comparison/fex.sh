#!/bin/sh
set -eu
cd /work
mkdir upstream build outputs empty-home
# Root supplies a retained, pinned gitlink overlay; this stage never fetches dependencies.
test -s source/dependencies.json
test -s source/dependency-overlay.tar
tar --extract --gzip --file source/upstream.tar.gz --directory upstream --strip-components=1 --no-same-owner
tar --extract --file source/dependency-overlay.tar --directory upstream --no-same-owner
python3 source/fex-export.py upstream
python3 source/ordering-profile.py fex upstream "${NATEMU_HARDWARE_TSO:-0}"
find upstream -type f -print0 | sort -z | xargs -0 sha256sum > source-before-build.sha256
cmake -S upstream -B build -G Ninja -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
    -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF -DBUILD_FEXCONFIG=OFF \
    -DBUILD_THUNKS=OFF -DENABLE_LTO=OFF -DENABLE_VIXL_DISASSEMBLER=ON \
    -DENABLE_VIXL_SIMULATOR=OFF -DENABLE_CCACHE=OFF
cmake --build build --target CodeSizeValidation --parallel 2
python3 upstream/Scripts/InstructionCountParser.py source/fixtures.json fixtures.instcountci
python3 source/fex-export.py --check-input fixtures.instcountci source/fixtures.json
status=0
env -i PATH=/usr/bin:/bin HOME=/work/empty-home NATEMU_EXPORT=/work/outputs \
    timeout 30 build/Bin/CodeSizeValidation fixtures.instcountci outputs/normalized-counts.json \
    > outputs/fex.log 2>&1 || status=$?
printf '%s\n' "$status" > outputs/exit-code.txt
# Count mismatches are retained separately; only a completed, validated export can pass.
python3 source/fex-export.py --check-output outputs source/fixtures.json
