#!/bin/bash
# Build the guest_rates x86_64 initramfs.
#
# REUSES probes/qemu_baseline/ read-only: its Alpine 3.24.1 minirootfs tarball,
# its netboot kernel, and its 7-Zip overlay. Nothing there is written.
# Adds: our own /init (guest_init.sh), the Alpine gcc 15.2 toolchain
# (fetch_toolchain.sh), and /src -- a real C project (SQLite amalgamation) with
# a Makefile, for the compile workload.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="$HERE/../qemu_baseline"
WORK="$HERE/work"
ALPINE_VER=3.24.1
A=x86_64
RD="$WORK/rootfs-$A"

mkdir -p "$WORK"

[ -f "$BASE/work/alpine-minirootfs-$ALPINE_VER-$A.tar.gz" ] || {
    echo "missing qemu_baseline minirootfs; run probes/qemu_baseline/build.sh first" >&2; exit 1; }
[ -d "$WORK/tc-$A" ] || { echo "run ./fetch_toolchain.sh first" >&2; exit 1; }

rm -rf "$RD"; mkdir -p "$RD"
tar -xzf "$BASE/work/alpine-minirootfs-$ALPINE_VER-$A.tar.gz" -C "$RD" 2>/dev/null || true

# --- 7-Zip, from qemu_baseline's overlay (same 26.01 build doc 15 measured)
( cd "$BASE/work/overlay-$A" && tar -cf - . ) | ( cd "$RD" && tar -xpf - )

# --- gcc toolchain, trimmed of the parts a plain -O1 C build never touches
( cd "$WORK/tc-$A" && tar -cf - . ) | ( cd "$RD" && tar -xpf - )
chmod -R u+w "$RD"
rm -f  "$RD/usr/bin/lto-dump" "$RD/usr/bin/dwp" "$RD/usr/bin/gcov" \
       "$RD/usr/bin/gcov-dump" "$RD/usr/bin/gcov-tool"
rm -f  "$RD"/usr/libexec/gcc/*/*/lto1
rm -rf "$RD/usr/share"

# --- /src : SQLite amalgamation + a Makefile. Real code, one big TU + one
#     medium TU, which is what a compile job actually looks like.
SQ="$WORK/sqlite-amalgamation-3500400"
[ -d "$SQ" ] || { echo "missing $SQ; see fetch_src.sh" >&2; exit 1; }
mkdir -p "$RD/src"
cp "$SQ/sqlite3.c" "$SQ/sqlite3.h" "$SQ/shell.c" "$SQ/sqlite3ext.h" "$RD/src/"
cat > "$RD/src/Makefile" <<'MK'
CC     = /usr/bin/gcc
CFLAGS = -O1 -w -DSQLITE_OMIT_LOAD_EXTENSION=1 -DSQLITE_THREADSAFE=0 \
         -DSQLITE_DEFAULT_MEMSTATUS=0 -DSQLITE_MAX_EXPR_DEPTH=0
OBJS   = sqlite3.o shell.o
all: sqlite3bin
sqlite3bin: $(OBJS)
	$(CC) $(CFLAGS) -o $@ $(OBJS)
%.o: %.c
	$(CC) $(CFLAGS) -c -o $@ $<
clean:
	rm -f $(OBJS) sqlite3bin
MK

# --- our init
cp "$HERE/guest_init.sh" "$RD/init"; chmod 755 "$RD/init"
mkdir -p "$RD/proc" "$RD/sys" "$RD/dev" "$RD/sys/kernel/tracing"

OUT="$WORK/initramfs-$A.cpio"
( cd "$RD" && tar --format newc --uid 0 --gid 0 --uname root --gname root -cf "$OUT" . )
echo "$(basename "$OUT")  $(stat -f%z "$OUT") bytes  sha256=$(shasum -a 256 "$OUT" | cut -d' ' -f1)"
echo "kernel  $(shasum -a 256 "$BASE/work/nb-$A/boot/vmlinuz-virt")"
echo OK
