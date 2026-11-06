#!/bin/bash
# native.sh WORKDIR SUITE [TAG]: run one suite natively on the build host (the reference AMD machine, as its normal user)
# with the same runner the guest uses; CT-* lines to WORKDIR/out/native-SUITE[-TAG].txt. One suite at a time, idle machine.
set -euo pipefail
W=$(realpath "$1"); s=$2; here=$(dirname "$(realpath "$0")"); o=$W/out/native-$s${3:+-$3}.txt
export TMPDIR=$W/run/tmp; rm -rf "$W/run"; mkdir -p "$TMPDIR"
case $s in
    torture) D=$W/torture;;
    ltp) D=$W/out; export LTPROOT=$W/ltp-inst/opt/ltp;;
    libm) D=$W/out;;
    openssl) D=$W/out; export OB=$W/ob;;
esac
{ echo "CT-BEGIN suite=$s part=0/1 cpus=$(nproc) kernel=$(uname -r) native"
  t0=$(date +%s.%N); bash "$here/run-$s.sh" "$D" 0/1 || true
  echo "CT-END suite=$s guest_s=$(echo "$t0 $(date +%s.%N)" | awk '{print $2-$1}')"; } > "$o" 2>&1
rm -rf "$W/run"; grep -c CT-RESULT "$o"
