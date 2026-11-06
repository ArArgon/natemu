# Correctness pass: user-space test suites in a Linux guest

Status: **done** for the four suites (November 5, 2026). Evidence, scripts' run order and raw logs:
`natemu-raw/correct-us-20261105-01/` (README there). Reusable scripts: `tools/correctness/`.

Established test suites built on the reference AMD machine (`ubuntu@192.168.12.11`, Ryzen 7 8845HS KVM guest, Ubuntu
26.04, kernel 7.0.0-42, 4 vCPUs, no root) with Debian trixie's own gcc 14.2.0-19 against Debian's libc 2.41-12+deb13u4
(user-level apt from snapshot.debian.org `20260915T000000Z`, the play image's pin), packed into initramfs images derived
from the play image (`tools/play`; same rootfs bytes, plus zstd, the kernel's `/boot/config-*` and one suite), booted
on the Debian 6.12.107 kernel with `console=ttyS0 mitigations=off nokaslr ct=SUITE part=K/N`. Results are one
`CT-RESULT` line per test on ttyS0 between `CT-BEGIN` and `CT-END`; the same runner script runs natively.

natemu: the `natemu` stub and release runtime built from 38c8c55 (tree `d2a67e10`, = 5f51dc0 after the authorship
rewrite) in a clean worktree, fexc `series65-p2`, compile cache off, 2 GiB. QEMU: 11.1.2 TCG `thread=multi` with the
like-for-like CPU model of `bench-20261105-03` (`Haswell,-pcid,...,+sha-ni,+adx,+clflushopt,+clwb,+smep,+umip`), 2 GiB.

## Suites and versions

| Suite | Version / source | Build | Tests |
| --- | --- | --- | --- |
| gcc C torture | `gcc.c-torture/execute` + `ieee/` of Debian `gcc-14_14.2.0.orig.tar.gz` (Debian gcc-14 14.2.0-19) | each test at `-O0` and `-O2`, `-static`, flags as gcc's DejaGnu harness gives them on x86_64-linux (`-w` or dg-options, dg-additional-options; ieee: `-fno-inline` + the `.x` file's flags) | 1,724 tests x 2 = 3,448 programs; all compile |
| glibc math | Debian glibc 2.41-12+deb13u4 source (orig + Debian's patch series), built in place | `make check subdirs=math` natively with a logging test-wrapper; the guest runs the identical recorded commands against the same build tree (same absolute paths in the image) | 1,859 test programs (all of `math/`) |
| LTP | upstream `ltp-full-20250530` (not packaged by Debian; the release current at trixie's release), sha256 `27586ba7...d4f2` | Debian gcc, dynamic against Debian libc (in the image), `--without-modules --without-numa --without-tirpc` | runtest files syscalls, math, sched, nptl, pty, mm, cve, crypto, fs, fcntl-locktests, capability, watchqueue: 1,446 entries run, 256 excluded |
| OpenSSL | Debian openssl 3.5.7-1~deb13u2 source (orig sha256 `a8c0d28a...98e8` + Debian's patch series) | `Configure linux-x86_64 no-shared no-module no-dso -static`, Debian gcc; the guest image carries the build tree at the same path (minus objects; only the 40 programs the chosen recipes run, stripped) and Debian's perl for OpenSSL's own harness (`test/run_tests.pl`, `make test`'s environment) | 41 recipes (`tools/correctness/openssl-tests.txt`: the internal bn/chacha/curve448/ec/modes/poly1305/siphash/sm3/sm4/mdc2/ml_kem/rsa tests, cmac, des, hmac, rand, rc2, rc4, rc5, bn, dh, dsa, ec, ecdsa, mp_rsa, rsa, rsaoaep, rsapss, rsax931, sha, kdf, mac, aesgcm, evp, evp_extra, evp_kdf, evp_xof, ml_dsa, slh_dsa, bio_enc, pkey_check) x 6 `OPENSSL_ia32cap` settings (`openssl-masks.txt`): default; `0:0` (generic C); `~0x1000000000000000:0` (AES-NI + PCLMULQDQ, no AVX, none of leaf 7); `...:0x20000000` (+ SHA-NI); `...:0x80108` (BMI1, BMI2, ADX: MULX/ADCX/ADOX bignum); `~0x0:0x20` (AVX2 only of leaf 7) |

The masks were checked natively with `openssl info -cpusettings` (e.g. `~0x1000000000000000:0x80108` gives
`0xeff83203078bffff:0x0000000000080108:0:0:0`: AVX off, AES-NI and PCLMULQDQ kept, leaf 7 = BMI1/BMI2/ADX only; `0:0`
gives `0x400:0:...`); OpenSSL ANDs `~` masks with CPUID and takes plain values as they are, so the guest gets the same
paths where natemu advertises the features (AES-NI, PCLMULQDQ, SHA-NI, BMI1/2, ADX, AVX2 all are).

OpenSSL exclusions: TLS/QUIC/socket recipes, fuzz corpora (`99-test_fuzz_*`), `test_speed` and `test_ecstress` (timed or
long), everything needing a FIPS module or shared libraries.

## Exclusions

- Torture: `builtins/` (its harness links per-test helper objects that override libc functions; DejaGnu-specific), none
  of `execute/` (no dg-skip-if or effective-target applies to x86_64-linux at -O0/-O2). The five tests that `#include`
  gcc.dg sources get those files. One left out of the LTP build: `listmount04` (needs `struct mnt_id_req.spare`, newer
  than trixie's linux-libc-dev 6.12).
- LTP (`natemu-raw/correct-us-20261105-01/native/ltp-excluded.txt`, with reasons): 172 entries whose source sets
  `.needs_device/.mount_device/.all_filesystems/.format_device/.needs_rofs` (loop devices: none in an initramfs machine);
  10 that use `tst_timer_test.h` (judge sleep latency against fixed thresholds: clock_nanosleep02, epoll_wait02/04,
  epoll_pwait03, nanosleep01, poll02, prctl09, pselect01, select02, futex_wait05); time-boxed work (growfiles gf01-30,
  rwtest01-04, iogen01, fs_racer, mmapstress09/10, mtest06*, thp01, min_free_kbytes, KSM ksm01-07 and variants);
  memory-pressure/OOM tests (oom01-05, mtest01/01w, mallocstress01); real-time measurement (time-schedule01,
  starvation, sched_football, cfs_bandwidth01); devices or tools absent (read_all_dev, quota_remount_test01, isofs,
  squashfs01, fs_fill, swapping01). net.* and other runtest files needing networking, disks, modules or KVM are not run.
  Settings: `LTP_TIMEOUT_MUL=4`, `LTP_RUNTIME_MUL=0.2` (runtime-bounded tests run a fifth as long), outer kill 900 s.
- glibc math: none. Eight tests that reach libmvec's AVX2 exp/log/pow/powf are skipped in the natemu runs only (they stop
  the machine, finding 2): `test-{double-libmvec-{exp,log,pow},double-vlen4-avx2-{exp,log,pow},float-libmvec-powf,float-vlen8-avx2-pow}-avx2`-style
  names, listed in `natemu-raw/correct-us-20261105-01/libm-skip.txt`. Since the fix (November 6) all eight pass.

## Summary

Four suites, 6,999 tests in all (3,448 torture programs, 1,859 glibc math programs, 1,446 LTP entries, 246 OpenSSL
recipe runs over six CPU paths), each at natemu N=1 and N=4. One natemu defect: **an AVX2 VEX gather in user space stops
the whole machine** (finding 2). Every other difference from the reference is the owner's known deviation (reduced
x87 precision: 8 torture runs, 332 libm tests), an environment difference (AVX-512 absent in natemu's profile, one
CPU at N=1), or output that depends on load addresses. LTP is identical to QEMU on the same kernel and image at N=4
(and to N=4 at N=1, but one test that needs two CPUs); OpenSSL's known-answer tests pass on every path at N=1 and N=4.

## Totals

| Suite | Native (AMD) | natemu N=1 | natemu N=4 | QEMU N=4 |
| --- | --- | --- | --- | --- |
| torture (3,448) | 3,448 exit 0 | 10 differ | 10 differ (the same) | 2 differ (return-addr only) |
| glibc math (1,859) | 1,859 PASS | 1,465 PASS; 394 differ: 54 AVX-512 variants UNSUPPORTED (environment: natemu's profile has no AVX-512, the AMD machine has it), 8 skipped (finding 2), 332 FAIL, all reduced x87 precision (finding 1: 320 long double / `_Float64x` / narrowing tests, 12 others that pass long doubles or use x87 internally; `basic-test` and `test-nan-payload` checked, finding 4) | 0 differ from N=1 | 1,804 PASS, 54 AVX-512 UNSUPPORTED, 1 FAIL (`test-fenv`, a QEMU defect: natemu and native pass it); QEMU passes every long double test and the AVX2 gather tests |
| LTP (1,446) | 895 pass, 514 TCONF, 25 TBROK, 8 TFAIL, 4 other (non-root on Ubuntu 7.0: not like-for-like) | identical to N=4 except `getcwd04` (TCONF: `.min_cpus = 2`) | 1,207 pass, 205 TCONF, 21 TBROK, 10 TWARN+TCONF, 1 TFAIL, 2 other; **0 differ from QEMU** but 2 that QEMU fails (`getrusage04`, `pselect01_64`: timing, QEMU too slow) | 1,205 pass, 205 TCONF, 21 TBROK, 10 TWARN+TCONF, 3 TFAIL, 2 other |
| OpenSSL (41 x 6 = 246) | 246 pass | 246 pass, 0 differ | 246 pass, 0 differ (subtest counts equal too) | not run |

Times: torture natemu N=1 239 s, N=4 170 s; QEMU N=4 112 s (three parts); native 28 s. LTP native 578 s, natemu N=4
1,046 s (four parts), N=1 1,403 s, QEMU N=4 1,264 s. libm native 30 s, natemu N=1 about 180 s, N=4 160 s, QEMU N=4 37 s. OpenSSL native 280 s, natemu N=4 1,011 s, N=1 1,014 s (single-threaded tests).

The native LTP run is a weak reference (different kernel, no root: most root-only tests TCONF natively); QEMU on the
same kernel and image is the like-for-like reference for LTP, native for the CPU-only suites (torture, libm).

LTP results that are not pass or TCONF are the same on natemu and QEMU and come from the machine, not the CPU:
old-style tests that need a block device (`tst_device`: lchown03, rename11, linkat02, mknod07, ...: TBROK "Failed to
acquire device"; the source markers of the exclusion rule only catch new-style tests), `ioctl_loop*` and others that
look for `/lib/modules` (TWARN + TCONF: the image has no modules), `keyctl05` (modprobe), `accept02` and
`cve-2020-25705` (multicast / veth), `epoll_ctl04` (kernel 6.12 returns ELOOP where this LTP expects EINVAL),
`nice05` (TBROK "Runtime not set!" under `LTP_RUNTIME_MUL`), `prot_hsymlinks` (aborts on both), `listmount04` (not
built). A first natemu N=4 pass with 0700 per-test directories (tests that drop to `nobody` got EACCES; `runs/ltp-tmp700/`)
was rerun with world-accessible directories, as runltp's.

## Findings

1. **Known deviation: reduced x87 precision** (owner, November 5: accepted for now; a slow precise x87 mode is
   backlogged). Torture `ieee/inf-2`, `ieee/inf-3`, `ieee/pr36332`, `pr39228`, each at -O0 and -O2, abort under natemu
   at N=1 and N=4; pass natively and on QEMU. Cause: an x87 register is a double, so `FLD m80` of LDBL_MAX
   (`7ffe ffffffffffffffff`) rounds to +inf; GCC's `isinfl(x)` (`|x| > LDBL_MAX`), `x != inf` and `x == inf` then
   misjudge. Repro `natemu-raw/correct-us-20261105-01/repro/x87/` (`x87max.c`): native prints `fld+fstp_m80(LDBL_MAX)
   7ffeffffffffffffffff`, `LDBL_MAX==inf 0 isinfl(1.01L*LDBL_MAX) 1`; natemu prints `7fff8000000000000000`,
   `LDBL_MAX==inf 1 isinfl(...) 0`. glibc's long double and double-from-long-double narrowing tests
   (`test-double-ldouble-*`, `test-ldouble-*`) are expected to fail for the same reason (first run: `test-double-ldouble-sub`
   FAIL with inexact/overflow flags and results of the double range); they are classified with this deviation. In the
   natemu N=1 run (and identically at N=4), the 332 FAILs are: 320 `test-ldouble-*`, `test-float64x-*` and
   `test-{float,double,float32x,float64}-{ldouble,float64x}-*` tests; the nexttoward tests (`bug-nexttoward`,
   `test-{float,double}-nexttoward`: a long double argument); `bug-nextafter` (nextafterl overflow/underflow);
   `test-misc` (`2^16384: mantissa incorrect`, values beyond the double range); `test-canonical-ldbl-96` and
   `tst-ldbl-nonnormal-printf` (80-bit pseudo-NaN/pseudo-infinity/unnormal encodings, which an FLD m80 into a double
   cannot keep); `test-{double,float32x,float64}-tgamma` (10 ulp against 9 allowed: glibc's x86_64 `__gamma_product`
   multiplies in long double, i.e. x87); `test-nan-payload` and `basic-test` (finding 4).
2. **natemu defect: a user-space AVX2 VEX gather stops the whole machine.** glibc's `test-double-libmvec-exp-avx2`
   (libmvec `_ZGVdN4v_exp_avx2`, which uses `vgatherdpd`/`vpgatherdd`) ended the natemu run with
   `natemu: NoCode("unsupported_form")` on the stub's standard error (exit 2) — the VEX gathers are refused forms
   (boot-handoff §5 ASM list: 97), yet CPUID advertises AVX2, and the refusal is a machine stop, not a guest fault. Any
   user program that uses AVX2 gathers kills the VM. In this libm build the gather users are the AVX2 variants of exp,
   log, pow and powf (objdump of `libmvec.so`; libm.so and libc.so have none). Minimal repro
   `repro/gather/gather.c` (one `vpgatherdd` at CPL 3): native and QEMU print `17 16 15 14 13 12 11 10`; natemu prints
   `before gather`, then the machine stops with `NoCode("unsupported_form")` (exit 2, `runs/gather-natemu1.*`). The
   second libm run (four libmvec tests skipped) stopped the same way at `test-double-vlen4-avx2-exp` (the vlen tests
   call the same functions), so the skip list is all eight tests that reach the AVX2 exp/log/pow/powf variants
   (`libm-skip.txt`). Expected behaviour per the SDM: the gather executes (AVX2 is advertised). If natemu keeps the
   refusal, the least it owes is a guest-visible #UD with AVX2 not advertised; a host stop is not an x86 behaviour.
   **Fixed** (November 6, below: "Finding 2 fixed").
3. **Not wrong execution: `return-addr` output.** It prints stack addresses (`printf("%p ...")`); natively two runs
   give different output and with ASLR off (`setarch -R`) the same; exit 0 everywhere. Load-address dependent.
4. **Resolved into finding 1: `basic-test` "long double isinf (inf) == 1" and `test-nan-payload`.** Repro
   `repro/x87inf/x87inf.c` (`runs/x87inf-natemu1.serial`): every x87 infinity stores correctly (`7fff8000000000000000`,
   FLD m32/m64/m80 of inf, FDIV 1/0, the conversion), but GCC expands `isinf` of a long double inline as
   `|x| > LDBL_MAX`, and LDBL_MAX loads as +inf (finding 1), so it answers 0 although the stored bits are right.
   An 80-bit quiet NaN with payload 0x123 stores as `7fffc000000000000000` (native `7fffc000000000000123`): the payload
   sits in the low 11 significand bits, which a double does not hold; `test-nan-payload` fails only its long double
   and `_Float64x` payload checks. Both: known deviation, reduced x87 precision.

## Next steps

1. Finding 2: done (below). The advertised forms still refused (a machine stop) are listed there.
2. Finding 1 needs nothing until the precise x87 mode (backlog) exists; then rerun torture and libm: the 8 torture runs
   and 332 libm tests are the check.
3. LTP coverage: the old-style device tests (TBROK "Failed to acquire device") could join the exclusion rule
   (`tst_device` in the source); adding a loop device (a virtio disk, integration phase) would open 172 + those tests.
4. Not run: QEMU for OpenSSL (natemu equals native on every recipe and path), the torture `builtins/` directory.

## Finding 2 fixed: the AVX2 gathers run in the resident (November 6, 2026)

Branch `gather-fix` (from `correctness` 5a0d7ba), compiler **series66-gather** (FEX `natemu` 8d812cadd unchanged, this tree's
shim; `obj/fexc` SHA-256 `dd60f985...c5ade47d`). Evidence: `natemu-raw/gather-20261105-01/` (scripts there).

**Cause.** The shim's AVX table (`fexc_slice1.inc` `Avx`) refused VEX.66.0F38 90..93 (`unsupported_form`: "FEX's gather is an
element loop (one site per element)"). A refusal at a block's first instruction is `Code::None`, which ends the vCPU's run
(host `execution.rs` `answered`): unless the host maps the bytes to an SDM exception (`system::undefined`, `branch`), every
refused form is a machine stop. FEX's own translation was not usable: its gather is a loop of element loads with no access
site per element and no partial mask, so a fault could neither be delivered at its element nor leave the SDM's partial state,
and admission has no site model for VSIB's per-element addresses.

**Design.** The gather is now a system instruction run by the resident, element by element (`system::GATHER`; shim: a system
cut, AVX-gated first, the encoding #UDs kept as fexc's `cut_ud`: no SIB, a register operand, two of destination, mask and
index the same; host `system.rs`: the VSIB operand, VEX.R/X/B/vvvv/L/W; resident `execution/gather.rs`). Each element in
index order: if its mask MSB is set, base + sign-extended index << scale + displacement (mod 2^64, 2^32 with 0x67) + the FS/GS
base is read as one explicit load at the CPL (`segments::copy`, `linear` without its #AC check: page walks with A bits, #PF
with CR2 its first faulting byte, #GP(0)/#SS(0) when non-canonical, data breakpoints, UC RAM; a device page stops
`unsupported_mmio` as any vector form there); then its mask element is cleared. Completed: the whole mask 0, the destination
past the gathered elements 0 to bit 255. The cost is one resident transaction per gather (about 0.3 us).

**Why it is exact.** It is the SDM's operation (Vol.2 VPGATHERDD/QD, VPGATHERDQ/QQ, VGATHERDPS/QPS, VGATHERDPD/QPD) with the
Type 12 exceptions (Vol.2 Table 2-27: the encoding #UDs, then #UD without CR4.OSXSAVE or XCR0[2:1] = 11b, then #NM with
CR0.TS, checked by the host's gated cut and again in the transaction; #PF, #GP(0)/#SS(0) per element; never #AC). Where the
SDM leaves room the reference AMD machine decides (`probe/gprobe.c`, user mode, every form at k = 0..n-1): at a fault on
element k the elements below k are done (destination loaded where masked in, mask 0) and nothing else of either register
changes, not the mask elements from k on (the pseudocode's MSB extension is not visible to software: only the MSB is read on
the restart) nor the parts no element covers; faults are delivered lowest element first and no element above the faulting
one is gathered. Returning to the instruction restarts it from that state. A data breakpoint met by an element completed
before the fault is delivered in lieu of the fault, the instruction partly done, RIP at it and RF = 1 in the frame (SDM: "If
any traps or interrupts are pending from already gathered elements, they will be delivered in lieu of the exception"), as a
boundary between two REP iterations; the restart then meets the fault. The elements are separate loads in order under the
VM's TSO, and the gather's loads are ordered with the loads around it.

**Tests.** `run_k2.py gather` (new, 88 cases, block sizes default and 1, judged by `gather_model`, the SDM's operation with the
AMD machine's partial state): every form at VEX.128 and VEX.256 with scales 1/2/4/8, five mask patterns, negative indices,
displacements, high registers (VEX.R/X/vvvv), five base registers and none, garbage in every unused input bit, 0x67 with a
32-bit wrap, FS (323 gathers per program, at CPL 3 and CPL 0); per form a fault on each element, a masked-off element on the
fault page, a page-crossing element, two faulting elements, each with CR2, error, RIP and the partial state at the #PF and the
restart's result (the done elements' indices pointed elsewhere: not read again); CPL 3 #PF error 4 and 5, #GP(0), #SS(0)
through RSP, a masked-off non-canonical element; #UD for the three overlaps, no SIB, a register operand, LOCK, 66 before VEX,
CR4.OSXSAVE clear, XCR0 = 3, #NM with CR0.TS; a data breakpoint on an element (#DB after it), on a masked-off one (none), on
element 0 with element 2 faulting (the #DB in lieu, RF = 1, then the #PF), TF (and after a #PF), an instruction breakpoint at
a faulting gather; no #AC with CR0.AM and AC; UC; a device page (stop) and a masked-off element there (completes); TSO
across two vCPUs. Negative controls (`nc/summary.txt`, `mutate.py`: one replacement each, built, judged, restored by
SHA-256), each red: masked-off elements not cleared before a fault (42 cases), the destination's upper part kept (24), the
partial state dropped at a fault (42), no trap in lieu (`inlieu`), indices zero-extended (24), #AC checked (`ac`), elements
in reverse order (42), bit 0 as the mask (32), 0x67 ignored (`forms`), the FS base ignored (`forms`), the host dropping VEX.X
or VEX.R (`forms`). Host test `system::tests::gathers`; `test_slice1.py` (the eight forms are system cuts). FEX's ASM suite:
its 97 gather tests (`VEX/vgather_*`, `vpgather_*`, `FEX_bugs/vgather_xmm4`) leave the runner's REFUSED list and all pass.
User mode (`probe/gprobe`, a static program over every form, scale, mask kind, fault position, crossing, 0x67, no base, the
#UD encodings; SIGSEGV handler records si_addr and the YMM state from the signal frame, then maps the page and returns):
933 lines, natemu N = 1 (Linux guest, `runs/gprobe-natemu1.serial`), QEMU 11.1.2 TCG and the AMD machine natively
byte-identical. The repro prints `17 16 15 14 13 12 11 10`. glibc math rerun at N = 1 without the skip list
(`runs/libm-natemu1.serial`): 1,859 results; the eight libmvec AVX2 tests pass; against the earlier run with them skipped
only those eight differ (386 differences from native: the 54 AVX-512 and 332 x87 ones of finding 1).

Checks: `python3 tools/run_suite.py --fexc series66-gather` (now with `k2-gather`; `suite/`): 115 of 118; `i8-faults` at both
block sizes still expected the gather refused (its case removed, `suite-r1/`: both and `k2-gather` pass, 850 cases and 88)
and `pytests` fails only `test_check_setup` (the worktree's path layout; 327 run). Its `asm` step finds no tests in a
worktree (no `third_party/FEX` checkout); run with series66-gather's FEX tree: 2,270 tests, 1,995 pass, 48 precision
deviations, 227 unsupported, no failure (chunk 26: 1,898 / 48 / 324). `cargo +1.97.0 test --offline --locked` with and
without `FEXC`: host 119 + 2 ignored, abi 67. Measured (`bench/`, `bench.sh`, the `natemu` stub, cache off,
N = 1, base `correctness` with series65-p2 against this branch with series66-gather, interleaved, three rounds; median
(min-max), seconds, cold run; every run verified, none contended): `avx` (x264) 8.90 (8.85-8.92) against 8.87
(8.87-8.92), warm 8.64 against 8.66; `zstd` 3.07 (3.03-3.10) against 3.07 (3.04-3.08), warm 2.92 and 2.92. No change: neither
runs a gather (the change adds no code to any other path).

**Advertised forms still refused** (a machine stop, `unsupported_form`; not built in this change): MASKMOVQ (SSE), MASKMOVDQU
(SSE2), VMASKMOVDQU, VMASKMOVPS/PD (AVX), VPMASKMOVD/Q (AVX2); VCVTPS2PH with a memory destination (F16C); the scalar VEX LIG
forms with VEX.L = 1 (AVX, FMA: hardware ignores L); INSERTPS/VINSERTPS m32 whose zero mask clears the inserted element
(SSE4.1, AVX); and of the base instruction set RCL/RCR m, BT* m, r with 0x67, 16-bit BSWAP/SHLD/SHRD, ENTER with a nesting
level, 66 on PUSHF/ENTER/LEAVE/RET/indirect branches/CMPXCHG8B/16B, FBLD/FBSTP, the undocumented x87, SAL /6 and TEST /1
aliases, and the prefix combinations the SDM reserves (REPNE on MOVS/STOS/LODS, REP/REPNE on non-string forms, 66 with
F2/F3 on an SSE form). The masked moves fit the gather's resident path (element loads and stores with per-element fault
suppression) but have implementation-dependent parts (A/D bits of masked-off elements, faults and breakpoints with an
all-zero mask, the non-temporal hint) that need the AMD machine first. Separately, the x86-selftest lane's `mov_ss_trap_64`
hang (ICEBP after MOV SS with a data breakpoint on its operand, at CPL 3 under Linux: a resident watchdog, not a refused
form) is another mechanism: ICEBP alone and after MOV SS deliver #DB at CPL 3, and the same sequence at CPL 0 in the program
driver gives the hardware's two #DBs (the pending data breakpoint at the handler's first instruction, then INT1's;
`icebp/probe.py`); not changed here.
