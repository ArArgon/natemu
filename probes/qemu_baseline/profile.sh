#!/bin/bash
# Host-side profile of where QEMU TCG time goes, plus QEMU's own `info jit`
# statistics and TCG-plugin guest-instruction counts.
#
# Uses macOS `sample`, which needs no sudo and no SIP changes. TCG-generated
# code lives in an anonymous mmap with no symbols, so it shows up as an
# unsymbolicated frame -- that is precisely the "time in translated code"
# bucket. Everything symbolicated inside qemu-system-x86_64 is runtime:
# helpers, softmmu slow paths, translation, device model, main loop.
#
# The short workloads are REPEATED (bench:w,w,w,...) so that the sampling
# window lands entirely inside steady-state execution of that workload and
# not inside boot.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$HERE/work"
OUT="$HERE/results/profile"
mkdir -p "$OUT"
QEMU=/opt/homebrew/bin/qemu-system-x86_64

run_x86() { # append-task hold [extra...] -- exec so that $! is the QEMU pid
    exec "$QEMU" -machine q35 -accel tcg,thread=single -cpu max -m 3072 -smp 1 \
        -kernel "$WORK/nb-x86_64/boot/vmlinuz-virt" \
        -initrd "$WORK/initramfs-x86_64.cpio" \
        -append "console=ttyS0 natemu.task=$1 natemu.hold=${2:-0} panic=-1 rdinit=/init loglevel=3" \
        -display none -serial stdio -no-reboot -nodefaults -monitor none "${@:3}"
}

# repeat counts chosen so each workload runs >= ~20 s of steady state
declare -a WL=(
  "alu4:alu4,alu4,alu4,alu4,alu4,alu4,alu4,alu4,alu4,alu4,alu4,alu4,alu4,alu4,alu4,alu4,alu4,alu4,alu4,alu4,alu4,alu4,alu4,alu4,alu4"
  "ls4:ls4,ls4,ls4,ls4,ls4,ls4,ls4,ls4,ls4,ls4"
  "ls4tlb:ls4tlb"
  "stream:stream,stream,stream,stream,stream,stream,stream,stream"
  "ptr256:ptr256,ptr256"
  "nqueens:nqueens,nqueens,nqueens,nqueens,nqueens,nqueens,nqueens,nqueens,nqueens,nqueens,nqueens,nqueens,nqueens,nqueens,nqueens"
  "syscall:syscall,syscall,syscall,syscall,syscall,syscall,syscall,syscall,syscall,syscall,syscall,syscall,syscall,syscall,syscall,syscall,syscall,syscall"
)

for entry in "${WL[@]}"; do
    w="${entry%%:*}"; list="${entry#*:}"
    echo "== sampling $w"
    run_x86 "bench:$list" 0 > "$OUT/run.$w.log" 2>&1 &
    PID=$!
    sleep 6            # past boot (2.2 s) and into steady state
    if kill -0 $PID 2>/dev/null; then
        sample $PID 12 1 -f "$OUT/sample.$w.txt" >/dev/null 2>&1 \
            || echo "  sample FAILED for $w"
    else
        echo "  workload finished before sampling window ($w)"
    fi
    kill $PID 2>/dev/null; wait $PID 2>/dev/null
done

# boot phase (translation-heavy): sample from t=0
echo "== sampling boot"
run_x86 none 60 > "$OUT/run.boot.log" 2>&1 &
PID=$!
sample $PID 2 1 -f "$OUT/sample.boot.txt" >/dev/null 2>&1 || echo "  sample FAILED boot"
kill $PID 2>/dev/null; wait $PID 2>/dev/null

# ---------------------------------------------------------------- 2. info jit
jit_run() { # label task
    local label="$1" task="$2"
    local sock="/tmp/natemu-mon-$$.sock"
    rm -f "$sock"
    "$QEMU" -machine q35 -accel tcg,thread=single -cpu max -m 3072 -smp 1 \
        -kernel "$WORK/nb-x86_64/boot/vmlinuz-virt" \
        -initrd "$WORK/initramfs-x86_64.cpio" \
        -append "console=ttyS0 natemu.task=$task natemu.hold=90 panic=-1 rdinit=/init loglevel=3" \
        -display none -serial stdio -no-reboot -nodefaults \
        -monitor "unix:$sock,server,nowait" > "$OUT/jitrun.$label.log" 2>&1 &
    local PID=$!
    for _ in $(seq 1 900); do
        grep -q NATEMU_TASK_END "$OUT/jitrun.$label.log" 2>/dev/null && break
        kill -0 $PID 2>/dev/null || break
        sleep 1
    done
    python3 - "$sock" "$OUT/infojit.$label.txt" <<'PY'
import socket, sys, time
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(20)
    s.connect(sys.argv[1]); time.sleep(0.5)
    buf = b""
    for cmd in (b"info jit\n", b"info status\n"):
        s.sendall(cmd); time.sleep(2.0)
        try:
            while True:
                s.settimeout(1.5)
                d = s.recv(65536)
                if not d: break
                buf += d
        except socket.timeout:
            pass
    open(sys.argv[2], "wb").write(buf)
    s.sendall(b"quit\n")
except Exception as e:
    open(sys.argv[2], "w").write("MONITOR FAILED: %r\n" % (e,))
PY
    kill $PID 2>/dev/null; wait $PID 2>/dev/null; rm -f "$sock"
}

jit_run boot none
jit_run bench bench

# ---------------------------------------------------------------- 3. plugin
if [ -f "$HERE/bin/plugin_count.so" ]; then
  for w in alu4 ls4 ls4tlb ptr256 stream matmul nqueens syscall; do
    echo "== plugin count $w"
    "$QEMU" -machine q35 -accel tcg,thread=single -cpu max -m 3072 -smp 1 \
        -plugin "$HERE/bin/plugin_count.so" -d plugin -D "$OUT/plugin.$w.txt" \
        -kernel "$WORK/nb-x86_64/boot/vmlinuz-virt" \
        -initrd "$WORK/initramfs-x86_64.cpio" \
        -append "console=ttyS0 natemu.task=bench:$w panic=-1 rdinit=/init loglevel=3" \
        -display none -serial stdio -no-reboot -nodefaults -monitor none \
        > "$OUT/pluginrun.$w.log" 2>&1
  done
  # baseline: what does boot alone cost, so it can be subtracted
  "$QEMU" -machine q35 -accel tcg,thread=single -cpu max -m 3072 -smp 1 \
      -plugin "$HERE/bin/plugin_count.so" -d plugin -D "$OUT/plugin.boot.txt" \
      -kernel "$WORK/nb-x86_64/boot/vmlinuz-virt" \
      -initrd "$WORK/initramfs-x86_64.cpio" \
      -append "console=ttyS0 natemu.task=none panic=-1 rdinit=/init loglevel=3" \
      -display none -serial stdio -no-reboot -nodefaults -monitor none \
      > "$OUT/pluginrun.boot.log" 2>&1
fi
echo "PROFILE DONE"
