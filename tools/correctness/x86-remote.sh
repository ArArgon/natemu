#!/bin/bash
# x86-remote.sh WORKDIR STAGE...: the x86 selftest and litmus lane's stages on the reference AMD machine (no root).
# WORKDIR needs remote.sh's `sysroot` and `play` stages first (Debian trixie gcc 14 + libc; the play rootfs in pb/).
#   selftests  tools/testing/selftests/x86 of Debian linux 6.12.107-1 (src/linux_6.12.107.orig.tar.xz, checked
#              against the .dsc; no Debian patch touches tools/testing), the 64-bit tests built -static with Debian
#              gcc into WORKDIR/x86st/ (plus ldt_gdt_64 and ptrace_syscall_64, which upstream builds only when -m32 works).
#   herd       herdtools7 7.58 (src/herdtools7-7.58.tar.gz) with Debian trixie's OCaml 5.3 / dune / menhir / zarith
#              unpacked in WORKDIR/oc (ocenv.sh), installed into WORKDIR/herd.
#   image S..  WORKDIR/out/S.cpio.zst: the play rootfs + the correctness /init + /ct/run-S.sh + the payload
#              (x86st: WORKDIR/x86st; litmus: WORKDIR/lit, the compiled litmus7 harnesses).
set -euo pipefail
W=$(realpath "$1"); shift; here=$(dirname "$(realpath "$0")"); S=$W/sysroot
CC="$S/usr/bin/x86_64-linux-gnu-gcc-14 --sysroot=$S"
export PATH=$S/usr/bin:$PATH LD_LIBRARY_PATH=$W/nativelibs

selftests() {
    cd "$W"; rm -rf kst x86st; mkdir kst x86st
    tar xJf src/linux_6.12.107.orig.tar.xz -C kst --strip-components=1 --wildcards '*/tools/testing/selftests/x86/*' \
        '*/tools/testing/selftests/lib.mk' '*/tools/testing/selftests/kselftest.h' '*/tools/testing/selftests/kselftest_harness.h' \
        '*/tools/include/*' '*/tools/scripts/*' '*/scripts/subarch.include' '*/arch/x86/include/asm/desc_defs.h'
    cd kst/tools/testing/selftests/x86
    # KHDR_INCLUDES: the uapi headers are Debian's linux-libc-dev 6.12 in the sysroot (the same kernel series).
    make -k CC="$CC" EXTRA_CFLAGS=-static KHDR_INCLUDES=-DKHDR_SYSROOT OUTPUT="$W/x86st" all_64 > "$W/log/x86st-make.log" 2>&1 || true
    for t in ldt_gdt ptrace_syscall; do
        $CC -m64 -o "$W/x86st/${t}_64" -O2 -g -std=gnu99 -pthread -Wall -no-pie -static $t.c helpers.h -lrt -ldl >> "$W/log/x86st-make.log" 2>&1 || echo "build failed: $t"
    done
    # test_vsyscall finds the vDSO with dlopen, which a static program cannot: also a dynamic build (Debian libc is in the image).
    $CC -m64 -o "$W/x86st/test_vsyscall_dyn_64" -O2 -g -std=gnu99 -pthread -Wall -no-pie test_vsyscall.c helpers.h -lrt -ldl >> "$W/log/x86st-make.log" 2>&1
    cd "$W"; rm -rf kst; ls x86st | tee out/x86st-list.txt | wc -l
    (cd x86st && sha256sum *) > out/x86st.sha256
}

ocaml() {   # Debian trixie OCaml packages (remote.sh's snapshot apt config; a 503 from snapshot: the same .deb from
            # deb.debian.org, checked against the snapshot index's SHA256), unpacked in WORKDIR/oc, run in place.
    export APT_CONFIG=$W/apt/apt.conf; mkdir -p "$W/ocdebs"; cd "$W/ocdebs"
    for p in ocaml ocaml-base ocaml-interp libstdlib-ocaml libstdlib-ocaml-dev ocaml-dune menhir libmenhir-ocaml-dev \
             libzarith-ocaml-dev libzarith-ocaml libgmp-dev ocaml-findlib libfindlib-ocaml libfindlib-ocaml-dev; do
        f=$(apt-cache show "$p" | sed -n 's/^Filename: //p' | head -1); h=$(apt-cache show "$p" | sed -n 's/^SHA256: //p' | head -1)
        [ -f "${f##*/}" ] || apt-get download "$p" || curl -sSfLO "https://deb.debian.org/debian/$f"
        echo "$h  ${f##*/}" | sha256sum -c --quiet
    done
    cd "$W"; rm -rf oc; mkdir oc hostbin hostlib 2>/dev/null || true; for d in ocdebs/*.deb; do dpkg -x "$d" oc; done
    # ocamlopt calls x86_64-linux-gnu-gcc (Debian's name) to link: the host's cc; -lgmp: the host's libgmp.so.10.
    ln -sf /usr/bin/cc hostbin/x86_64-linux-gnu-gcc; ln -sf /usr/lib/x86_64-linux-gnu/libgmp.so.10 hostlib/libgmp.so
    L=$W/oc/usr/lib/x86_64-linux-gnu/ocaml/5.3.0
    printf 'destdir="%s"\npath="%s"\nldconf="ignore"\n' "$L" "$L:$L/METAS" > oc/findlib.conf
    cat > ocenv.sh <<EOF
L=$L
export PATH=$W/herd/bin:$W/hostbin:$W/oc/usr/bin:\$PATH OCAMLLIB=\$L OCAMLFIND_CONF=$W/oc/findlib.conf OCAMLPATH=\$L CAML_LD_LIBRARY_PATH=\$L/stublibs LIBRARY_PATH=$W/hostlib
EOF
    ls ocdebs > out/ocaml-debs.txt
}

herd() {
    . "$W/ocenv.sh"; cd "$W"; rm -rf herd-src herd; tar xzf src/herdtools7-7.58.tar.gz; mv herdtools7-7.58 herd-src
    (cd herd-src && make -j4 PREFIX="$W/herd" all && make PREFIX="$W/herd" install) > "$W/log/herd.log" 2>&1
    ls "$W/herd/bin"
}

litmus() {   # the corpus (litmus-gen.py), herd7's x86-TSO states per test (the oracle), litmus7 harnesses -> WORKDIR/lit
    # (dynamic, against Debian libc 2.41, which the image carries: 1,319 static programs would be 1.2 GB of guest RAM)
    . "$W/ocenv.sh"; cd "$W"; rm -rf diy corpus oracle litc lit; mkdir diy oracle litc lit
    (cd diy && diy7 -arch X86_64 -type uint64_t -num false -mode critical -size 6 -nprocs 4 \
        -safe Rmw,PodWR,PodWW,PodRR,PodRW,Fre,Rfe,Wse,MFencedWR -o .)
    python3 -I "$here/litmus-gen.py" corpus src/herd-git/catalogue src/ltx86/tests/non-mixed-size diy
    for d in corpus/*/; do
        f=$(basename "$d")
        herd7 "$d"*.litmus > "oracle/$f.txt" 2> "oracle/$f.err" || true   # default models: x86tso.cat (X86), x86tso-mixed.cat (X86_64)
        mkdir "litc/$f"; litmus7 -mach x86_64 -smt 1 -affinity incr1 -a 4 -s 100000 -r 100 -gcc x86_64-linux-gnu-gcc-14 \
            -ccopts "--sysroot=$S -O2" -o "litc/$f" "$d"*.litmus > "log/litmus7-$f.log" 2>&1
        make -C "litc/$f" -j4 > "log/litmus-make-$f.log" 2>&1 || echo "make errors: $f"
        for e in litc/"$f"/*.exe; do cp "$e" "lit/$f.$(basename "$e" .exe)"; done
    done
    cp corpus/threads.txt lit/; ls lit | wc -l; cat oracle/*.err | sort | uniq -c | head
    tar czf out/litmus-corpus.tgz corpus oracle; rm -rf litc
}

image() {
    for s in "$@"; do
        R=$W/img-$s; rm -rf "$R"; cp -a "$W/pb/rootfs" "$R"; mkdir -p "$R/ct"
        cp "$here/init" "$R/init"; cp "$here/run-$s.sh" "$here/run-repro.sh" "$R/ct/"; chmod 755 "$R/init" "$R"/ct/run-*.sh
        case $s in
            x86st) mkdir "$R/ct/x86st"; cp "$W"/x86st/*_64 "$R/ct/x86st/";;
            litmus) mkdir "$R/ct/lit"; cp "$W"/lit/* "$R/ct/lit/";;
        esac
        python3 "$here/play/mkcpio.py" "$R" "$W/out/$s.cpio"
        zstd -q -19 -T0 -f --rm "$W/out/$s.cpio" -o "$W/out/$s.cpio.zst"; rm -rf "$R"
        ls -la "$W/out/$s.cpio.zst"
    done
}

native() {   # native SUITE [TAG] [ARGS]: the guest runner natively as this user (D = WORKDIR: x86st/, lit/ as in /ct)
    s=$1; o=$W/out/native-$s${2:+-$2}.txt; export TMPDIR=$W/run/tmp; rm -rf "$W/run"; mkdir -p "$TMPDIR"
    { echo "CT-BEGIN suite=$s part=0/1 cpus=$(nproc) kernel=$(uname -r) native"
      t0=$(date +%s.%N); sh "$here/run-$s.sh" "$W" "${3:-0/1}" || true
      echo "CT-END suite=$s guest_s=$(echo "$t0 $(date +%s.%N)" | awk '{print $2-$1}')"; } > "$o" 2>&1
    rm -rf "$W/run"; grep -c CT-RESULT "$o"
}

st=$1; shift; "$st" "$@"
