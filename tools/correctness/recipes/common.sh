#!/bin/bash
# common.sh: sourced by tools/correctness/recipes/SUITE/build.sh. testkit/inputs.py:campaign_image runs build.sh in the
# amd64 Debian builder container (artifacts.BENCH_BUILDER, through Rosetta) as root, with /recipe holding the tools/
# files the recipe reads (its uses.txt, this file, the pins in common/ and the suite's folder; read-only) and /work
# the output: SUITE.cpio.zst, SUITE-list.txt, downloads.txt (URL SHA256 FILE of every download) and log/.
#
# The build is tools/correctness/remote.sh's stages, unchanged, run as the user `ubuntu` with W=/home/ubuntu/ct (the
# reference machine's layout: the paths the campaigns bake in, such as OpenSSL's OB=/home/ubuntu/ct/ob, stay the same).
# remote.sh finds tools/play beside itself, so the tools are staged in /home/ubuntu/ct-tools with play/ inside.
#
# Pins. Everything comes from snapshot.debian.org at SNAP (tools/play/rootfs.sh's date), apt checking the archive's
# signature against the builder image's keyring, and every downloaded file is checked against a pin file here:
#   common/tools-debs.sha256    what the container lacks (python3, curl, zstd, patch, xz, kmod, perl), installed as root
#   common/sysroot-debs.sha256  remote.sh's sysroot (Debian gcc 14 and libc 2.41)
#   common/play-debs.sha256     the play rootfs (tools/play/rootfs.sh's package set) plus remote.sh's zstd
#   common/kernel-deb.sha256    the 6.12.107 kernel package (tools/play/build.sh's DEB_SHA; its modules and config)
#   SUITE/*.sha256              the suite's sources and extra image packages
# Without a pin file (re-pinning) the build writes what it downloaded to /work/repin/<pin file>, goes on downloading
# and stops before building anything: check the files and copy them into tools/correctness/recipes.
#
# Fixes that existed only as overlays on the reference images, folded in: /init (tools/correctness/init) links
# /dev/stdin, /dev/stdout, /dev/stderr and /dev/fd; every cpio entry's mtime is SOURCE_DATE_EPOCH, not 0 (1970, which
# ZIP and other date checks reject); overlay() measures with GNU stat -c %s (overlay.sh's stat -f %z is macOS's).
set -euo pipefail
SNAP=20260915T000000Z
export SOURCE_DATE_EPOCH=1789430400                 # = SNAP (2026-09-15 00:00 UTC)
export DEBIAN_FRONTEND=noninteractive LC_ALL=C TZ=UTC
MIRROR=http://snapshot.debian.org/archive/debian/$SNAP
TOOLS='python3 curl ca-certificates zstd patch xz-utils kmod perl'
RC=/recipe/correctness/recipes
H=/home/ubuntu; W=$H/ct; T=$H/ct-tools; REPIN=$H/repin
K=linux-image-6.12.107+deb13-amd64_6.12.107-1_amd64.deb
KDEB_SHA=$(awk -v f="$K" '$2 == f {print $1}' "$RC/common/kernel-deb.sha256")
[ ${#KDEB_SHA} = 64 ] || { echo "no pin for $K in common/kernel-deb.sha256" >&2; exit 1; }
export KDEB_SHA                                      # tools/play/rootfs.sh checks the kernel package with it

say() { echo "$(date -u +%T) $*" >&2; }

# pins LISTING PIN: LISTING ("SHA256  FILE" lines) must be PIN (tools/correctness/recipes/PIN) line for line, in any
# order. Without PIN: re-pinning (the listing goes to $REPIN/PIN).
pins() {
    local got=$1 pin=$RC/$2
    if [ ! -f "$pin" ]; then
        mkdir -p "$(dirname "$REPIN/$2")"; sort -k2 "$got" > "$REPIN/$2"; say "no pins $2: wrote repin/$2"; return 0
    fi
    diff <(sort -k2 "$pin") <(sort -k2 "$got") >&2 || { echo "downloads differ from $2 (< pinned, > downloaded)" >&2; exit 1; }
}

# debsums DIR: "SHA256  FILE" of DIR/*.deb.
debsums() { (cd "$1" && sha256sum -- *.deb); }

# uris FILE...: "URL SHA256 FILE" per downloaded FILE (a .deb, or a source file with `source PKG=VER` first), the URL
# from apt's index ($APT_CONFIG's snapshot), the hash of the file itself (which its pin checked).
uris() {   # (apt prints nothing for a file already in its folder: it runs in an empty one)
    local map specs=() f e; e=$(mktemp -d)
    if [ "$1" = source ]; then map=$(cd "$e" && apt-get source --print-uris -qq "$2"); shift 2
    else for f in "$@"; do specs+=("$(dpkg-deb -f "$f" Package)=$(dpkg-deb -f "$f" Version)"); done
         map=$(cd "$e" && apt-get download --print-uris -qq "${specs[@]}"); fi
    rmdir "$e"
    for f in "$@"; do
        u=$(awk -v f="${f##*/}" '$2 == f {gsub("\047", "", $1); print $1}' <<< "$map")
        [ -n "$u" ] || { echo "no URL in apt's index for ${f##*/}" >&2; exit 1; }
        echo "$u $(sha256sum < "$f" | cut -d' ' -f1) ${f##*/}"
    done
}

# --- as root ------------------------------------------------------------------------------------------------------
# root_phase SCRIPT SUITE: the tools from the snapshot, the user ubuntu and its staged tools, SCRIPT `user` as ubuntu,
# then the outputs (and any new pins) to /work.
root_phase() {
    local script=$1 suite=$2 rc=0
    [ "$(id -u)" = 0 ] || { echo "root_phase runs as the container's root" >&2; exit 1; }
    rm -f /etc/apt/sources.list.d/* /etc/apt/sources.list
    echo "deb [check-valid-until=no] $MIRROR/ trixie main" > /etc/apt/sources.list
    apt-get -o Acquire::Retries=5 update -qq
    rm -f /var/cache/apt/archives/*.deb
    apt-get -o Acquire::Retries=5 -y -qq -d install --no-install-recommends $TOOLS >/dev/null
    useradd -m -u 1000 -s /bin/bash ubuntu
    mkdir -p "$REPIN" "$W/out" "$W/log" "$W/kr/usr/share/keyrings" "$W/pb/kr/usr/share/keyrings"
    debsums /var/cache/apt/archives > "$H/tools.lst"; pins "$H/tools.lst" common/tools-debs.sha256
    uris /var/cache/apt/archives/*.deb > "$W/out/downloads.txt"
    apt-get -y -qq install --no-install-recommends $TOOLS >/dev/null
    # The apt keyring of the private apts remote.sh and rootfs.sh run (they would download debian-archive-keyring with
    # an apt that has no configuration yet): the builder image's, the anchor the pinned image carries.
    for d in "$W/kr" "$W/pb/kr"; do cp /usr/share/keyrings/debian-archive-keyring.pgp "$d/usr/share/keyrings/"; done
    cp -r /recipe/correctness "$T"; cp -r /recipe/play "$T/play"
    mkcpio_epoch > "$T/play/mkcpio.py"
    chown -R ubuntu: "$H"
    runuser -u ubuntu -- bash "$script" user || rc=$?
    cp -r "$W/log" /work/log 2>/dev/null || true
    case $rc in
        0) [ -z "$(ls -A "$REPIN")" ] || { echo "new pins, yet the build went on" >&2; exit 1; };;
        3) cp -r "$REPIN" /work/repin; echo "new pins in /work/repin: build stopped" >&2; exit 1;;
        *) echo "the build as ubuntu failed (exit $rc)" >&2; exit "$rc";;   # a failure, even while re-pinning
    esac
    cp "$W/out/$suite.cpio.zst" "$W/out/$suite-list.txt" "$W/out/downloads.txt" /work/
}

# mkcpio_epoch: a mkcpio.py that runs tools/play/mkcpio.py, then sets each entry's mtime to SOURCE_DATE_EPOCH.
mkcpio_epoch() {
    cat <<'EOP'
#!/usr/bin/env python3
"""mkcpio.py ROOTDIR OUT.cpio: tools/play/mkcpio.py (/recipe/play/mkcpio.py), then every entry's mtime (newc field 6,
mkcpio writes 0) set to SOURCE_DATE_EPOCH. Written by tools/correctness/recipes/common.sh."""
import mmap, os, runpy, sys
sys.argv[0] = '/recipe/play/mkcpio.py'; runpy.run_path(sys.argv[0], run_name='__main__')
mtime = b'%08X' % int(os.environ['SOURCE_DATE_EPOCH'])
with open(sys.argv[2], 'r+b') as f, mmap.mmap(f.fileno(), 0) as m:
    p = 0
    while True:
        assert m[p:p + 6] == b'070701', f'not a newc header at {p}'
        size, namesize = int(m[p + 54:p + 62], 16), int(m[p + 94:p + 102], 16)
        if m[p + 110:p + 109 + namesize] == b'TRAILER!!!': break
        m[p + 46:p + 54] = mtime
        p += 110 + namesize; p += -p % 4; p += size; p += -p % 4
EOP
}

# --- as ubuntu ----------------------------------------------------------------------------------------------------
stage() { say "remote.sh $*"; bash "$T/remote.sh" "$W" "$@"; }
user_env() { export APT_CONFIG=$W/apt/apt.conf; }  # remote.sh's private apt (written by its sysroot stage)

# sysroot: remote.sh sysroot, its archives checked against common/sysroot-debs.sha256.
sysroot() {
    stage sysroot > "$W/log/sysroot.log" 2>&1 || { tail -20 "$W/log/sysroot.log" >&2; exit 1; }
    sed 's| \./| |' "$W/out/sysroot-debs.sha256" > "$H/sysroot.lst"; pins "$H/sysroot.lst" common/sysroot-debs.sha256
    uris "$W"/debs/*.deb >> "$W/out/downloads.txt"
}

# sources PKG=VER PIN: Debian's source package (apt-get source -d checks it against the signed Sources) into $W/src.
sources() {
    say "apt-get source $1"
    mkdir -p "$W/src"; cd "$W/src"; rm -rf "$H/src-new"; mkdir "$H/src-new"
    (cd "$H/src-new" && apt-get source -d -qq "$1")
    (cd "$H/src-new" && sha256sum -- *) > "$H/src.lst"; pins "$H/src.lst" "$2"
    (cd "$H/src-new" && uris source "$1" *) >> "$W/out/downloads.txt"
    mv "$H"/src-new/* "$W/src/"; rmdir "$H/src-new"; cd "$W"
}

# play: remote.sh play (tools/play/rootfs.sh, the kernel package checked against KDEB_SHA, plus zstd), its archives
# checked against common/play-debs.sha256.
play() {
    stage play > "$W/log/play-stage.log" 2>&1 || { tail -20 "$W/log/play-stage.log" "$W/log/play.log" >&2; exit 1; }
    debsums "$W/pb/apt/cache/archives" > "$H/play.lst"; pins "$H/play.lst" common/play-debs.sha256
    uris "$W"/pb/apt/cache/archives/*.deb >> "$W/out/downloads.txt"
    echo "$MIRROR/pool/main/l/linux-signed-amd64/$K $KDEB_SHA $K" >> "$W/out/downloads.txt"
}

# extra_debs PIN PKG...: packages the suite's image stage adds from play's apt (downloaded now, into its cache, where
# remote.sh's image stage finds them), checked against PIN.
extra_debs() {
    local pin=$1; shift
    say "image packages: $*"
    APT_CONFIG=$W/pb/apt/apt.conf apt-get -y -qq -d install --no-install-recommends "$@" >/dev/null
    debsums "$W/pb/apt/cache/archives" | sort > "$H/all.lst"
    sort "$H/play.lst" | comm -13 - "$H/all.lst" > "$H/extra.lst"; pins "$H/extra.lst" "$pin"
    (cd "$W/pb/apt/cache/archives" && APT_CONFIG=$W/pb/apt/apt.conf uris $(awk '{print $2}' "$H/extra.lst")) >> "$W/out/downloads.txt"
}

# stop_if_repin: after the downloads, before building: re-pinning ends here.
stop_if_repin() { [ -z "$(ls -A "$REPIN")" ] || { say "re-pinning: not building"; exit 3; }; }

# overlay BASE.cpio.zst OUT FILE...: overlay.sh on Linux (GNU stat): OUT = BASE, then a zstd cpio putting each FILE in /ct.
overlay() {
    local base=$1 out=$2 t; shift 2; t=$(mktemp -d)
    mkdir -p "$t/r/ct"; cp "$@" "$t/r/ct/"; chmod -R a+rX "$t/r"; chmod 755 "$t"/r/ct/*.sh 2>/dev/null || true
    python3 "$T/play/mkcpio.py" "$t/r" "$t/r.cpio"; zstd -q -19 "$t/r.cpio" -o "$t/r.cpio.zst"
    local pad=$(( (4 - $(stat -c %s "$base") % 4) % 4 ))
    { cat "$base"; head -c $pad /dev/zero; cat "$t/r.cpio.zst"; } > "$out"; rm -rf "$t"
}
