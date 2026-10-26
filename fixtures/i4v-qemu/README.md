# Supporting QEMU scalar comparison

`tools/run_i4v_qemu.py` prepares an independently snapshotted six-case comparison:
MOV RAX,[RBX], the retained five-instruction PUSH/MOV sequence, aligned DWORD
XCHG plus result store, and LFENCE/SFENCE/MFENCE scalar preservation. It binds
literal guest bytes, the low guest RIP, initial registers and memory, and compares
QEMU results both with an independent small Python model and with selected actual
resident reports from passed retained bundles. The report and manifest checksums
and selected native job are retained with each case.

This is supporting outcome evidence, not a privileged paging, fault retry,
concurrent atomicity, fence ordering, or generated-code publication comparison.
The QEMU process runs one thread under Linux-user TCG. Existing native suites own
those other claims. No FEX implementation or runtime helper supplies expected
instruction outcomes in this comparator.

The ELF has one fixed mapping at 0x100000, exact instruction bytes at 0x102000,
and entry at 0x103000. NASM emits a flat image; the harness writes an ELF64 header
without needing a linker or libc. Source fallthrough enters a capture sequence
that saves all 16 GPRs using MOV before switching to the harness stack. PUSHFQ
then captures flags; LEA records the actual capture label as the final guest RIP.
No flag-changing instruction precedes flag capture. A direct JMP enters the
fixture, so unused noncanonical guest RSP values never become harness stacks.

All 16 GPRs and 8192 mapped bytes are checked against the independent model. The
resident overlap is explicitly narrower where its report is narrower: load has
16 canonical GPRs and 256 memory bytes; stack has 16 canonical GPRs and 8192 bytes;
ordering has the eight actually represented guest GPR roots and 8192 bytes. The
remaining load memory and ordering R8–R15 are QEMU-only canaries. The latter do
not become claims about resident state. Load/stack canonical RFLAGS are checked;
ordering reports instead establish preservation of their actual native NZCV.

All six QEMU outputs must have exact full RFLAGS 0x202 for this fixed Linux-user
profile. No output bits are masked; the selected instructions have no undefined
flags. The setup's POPFQ and Linux-user privilege state produce that value, and
the instruction sequence must preserve it. This does not claim arbitrary IF/IOPL
restoration through CPL3 POPFQ. Load/stack resident canonical RFLAGS must also be
0x202. Ordering resident reports provide only native NZCV preservation, so their
canonical RFLAGS are not included in the cross-emulator equivalence claim.

## Producing pin

QEMU is Debian's statically linked ARM64 Linux-user binary, package
`qemu-user-static 1:7.2+dfsg-7+deb12u18+b3`, built from source
`qemu 1:7.2+dfsg-7+deb12u18`. The package, extracted binary, original source,
Debian patch/build configuration archive, signed source descriptor, copyright,
and package metadata are retained in each comparison. Exact SHA-256 pins live
in the runner and are checked before any execution.

- [Binary package](https://deb.debian.org/debian/pool/main/q/qemu/qemu-user-static_7.2+dfsg-7+deb12u18+b3_arm64.deb)
- [Source descriptor](https://deb.debian.org/debian/pool/main/q/qemu/qemu_7.2+dfsg-7+deb12u18.dsc)
- [Original source](https://deb.debian.org/debian/pool/main/q/qemu/qemu_7.2+dfsg.orig.tar.xz)
- [Debian build configuration and patches](https://deb.debian.org/debian/pool/main/q/qemu/qemu_7.2+dfsg-7+deb12u18.debian.tar.xz)

The existing pinned ARM64 toolchain image is
`sha256:27475cb39ecb4500d02b7c6e73c9d598e8d9f0736f0dc217d2d985e5cbe01aac`.
It provides NASM 2.16.03 and Python; their executed binary hashes and version
outputs are recorded. QEMU uses explicit `-cpu qemu64` and an instruction log.
The container has no network, a read-only root, 512 MiB RAM, two CPUs and 128 PIDs.
The inherited PostgreSQL data volume is replaced by a bounded 1 MiB tmpfs;
removal also requests anonymous-volume cleanup. Only the new comparison directory
is a writable host mount. The exact created container ID
is recorded and removed in final cleanup; cleanup failure prevents acceptance.

Preparation performs no native/compiler/container work. `--run DIRECTORY` is a
separate explicit execution step, allowed only after the shared native slot is
assigned. Each fresh reproduction prepares a new directory and runs the retained
source. `--audit DIRECTORY` recomputes both independent and resident projections.
