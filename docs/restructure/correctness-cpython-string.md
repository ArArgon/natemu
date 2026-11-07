# Correctness pass: CPython (build and regression suite) and glibc's string tests in a Linux guest

Status: **execution and targeted CPython diagnosis complete; explicit x87 and coverage limits remain** (November 6, 2026).
Latest FD follow-up (November 7 UTC): **14/14 post-fix checks pass on each of N=1, N=4 and QEMU**—42 passes,
zero failures/errors/skips, including the previously unrerun `test_script_as_dev_fd`. Historical skips below are
original-run outcomes, not the current post-fix verdict. Evidence: `natemu-raw/correctness-fd-afterfix-20261107-01/`.

Evidence, rerun steps and raw logs: `natemu-raw/correct-cpy-20261106-01/`
(README there). Scripts: `tools/correctness/` (this lane's new files `cpy-remote.sh`, `cpy-build.sh`, `run-cpy.sh`,
`run-cpyb.sh`, `gstr.py`, `run-gstr.sh`, `cpy-check.py`; lane 1's harness reused unchanged: `boot.py`, `vm.sh`,
`idle-boot.sh`, `init`, `remote.sh` (`sysroot`, `play`), `compare.py`; the stress lane's `stress-campaign.sh`).

natemu: the `natemu` stub and release runtime built from 4527322 in a clean worktree (`build-stub.sh`: stub
`572ce252...2400`... see README), fexc **series66-gather** (boot-handoff's `F=` line), compile cache off, 2 GiB, Debian
6.12.107 kernel, `console=ttyS0 mitigations=off nokaslr`. Reference: the AMD machine (`ubuntu@192.168.12.11`, Ryzen 7
8845HS KVM guest, 4 vCPUs, 3.9 GB, Ubuntu kernel 7.0.0-42), as its normal user. QEMU: 11.1.2 TCG `thread=multi`, lane
1's like-for-like CPU model.

## Retained execution and scope

All paths below are relative to `natemu-raw/correct-cpy-20261106-01/` unless identified as repository paths.
`q1.txt` has 60 boots, all present in `runs/runs.jsonl` with `reached=true`, process exit 0 and no recorded contention;
`runs/campaign.log` ends with `15:16:06 CAMPAIGN-END`. None of these boots was capped or stopped before its closing
record. This includes CPython's 12 parts at natemu N=1, natemu N=4 and QEMU N=4; builds at natemu N=1/N=4 and QEMU
N=4; six string-test parts at each natemu CPU count and one complete QEMU run; and eight diagnostic reruns.
No campaign restart is needed to obtain these results. The raw README's unfinished wording predates this analysis.

The source is Debian's patched CPython 3.13.5-2+deb13u5, built with Debian gcc 14, and glibc 2.41-12+deb13u4.
`native/cpy-tests.txt` lists all 473 selected modules and their parts; its `smoke` tags select the post-build tests.
Only CPython's `subprocess` resource is enabled (`-u subprocess`); network, URL fetch, GUI, audio, curses, large-file,
extra-large-file, CPU-intensive, decimal, tzdata and walltime resources are not enabled. Tests use the native build
tree, with `LC_ALL=C.UTF-8`, `TZ=UTC`, `PYTHONHASHSEED=0`; the guest drops to `nobody`, while the AMD reference runs
as its ordinary user. Source files used for the analysis are retained inside `cpyt.cpio.zst`, under
`home/ubuntu/cp/py/Lib/test/`; they were inspected directly, without substituting a newer CPython version.

**A completed boot is not a passing suite.** `boot.py` records reaching `CT-END`; `run-cpy.sh` prints the regrtest
exit code and cleans up, so its shell exit does not propagate that code. `cpy-check.py` is a diagnostic comparison,
not a pass/fail gate. The tables below count `CT-RESULT`, regrtest's closing module verdicts and `CT-CASE`, rather
than treating a zero boot status as success. Assertions checked that each 12-part run has exactly the 473 listed
modules, all 12 end markers, and case totals equal pass + fail + error + skip.

## CPython regression results

| Run | Modules: passed / failed / skipped / resource denied | Cases | Pass | Fail | Error | Skip |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| AMD reference, first and repeated runs | 443 / 1 / 20 / 9 | 44,458 | 42,686 | 0 | 1 | 1,771 |
| natemu N=1 | 441 / 3 / 20 / 9 | 44,433 | 42,562 | 6 | 6 | 1,859 |
| natemu N=4 | 441 / 3 / 20 / 9 | 44,433 | 42,565 | 6 | 6 | 1,856 |
| QEMU N=4 | 441 / 3 / 20 / 9 | 44,433 | 42,566 | 5 | 6 | 1,856 |

Evidence: `native/native-cpy-all{,-r2}.txt` and `runs/cpyt{0..11}-{natemu1,natemu4,qemu4}.serial`.
The native/guest difference of 25 cases is confined to `test_locale` (64 versus 41) and `test_c_locale_coercion`
(8 versus 6). These tests select classes/cases using the available locales; the retained image omits the native
machine's locale set. This is a coverage difference, not 25 successful guest executions.

### Every failed or errored case

| Cases | Observations and disposition |
| --- | --- |
| `test_asyncio.test_subprocess`: `test_devstdin_input` in `SubprocessFastWatcherTests`, `SubprocessPidfdWatcherTests`, `SubprocessSafeWatcherTests`, `SubprocessThreadedWatcherTests` | All four fail on natemu N=1/N=4 and QEMU4; pass natively. The retained test opens `/dev/stdin` in a subprocess and expects three bytes. The image has no `/dev/stdin` link; `/init` mounts devtmpfs but creates neither that link nor `/dev/fd`. The observed assertion is empty output versus `b'3'`. Source-backed image setup defect; the targeted follow-up below reproduces the failures and passes all four after correction on all three guest configurations. |
| `test_zipfile.test_core.PyZipFileTests.test_write_filtered_python_package`, `.test_write_with_optimization`; `ZipInfoTests.test_from_file`, `.test_from_file_bytes`, `.test_from_file_fileno`, `.test_from_file_pathlike` | The same six errors on all three guest runs, all passing natively. Tracebacks report `ValueError: ZIP does not support timestamps before 1980`. Repository `tools/play/mkcpio.py` stores every initial file with mtime 0. The diagnostic reruns also error in `PyZipFileTests.test_write_python_package`, making seven errors in that module. The follow-up below reproduces all seven and passes them after environment correction on all three guest configurations; the original failures remain in the matrix. |
| `test_os.TimerfdTests.test_timerfd_TFD_TIMER_ABSTIME` | Fails in natemu N=1/N=4 and QEMU4, passing natively. The test sets an absolute deadline one second beyond a prior clock sample, then requires the remaining time to equal one second to three decimal places, without subtracting the time spent before `timerfd_gettime`. Observed deficits: natemu1 1.626455 ms, natemu4 1.918955 ms, QEMU4 0.700222 ms. This assertion measures a sub-millisecond execution budget, before checking delivery, and cannot alone diagnose an incorrect timer. Remains a failed upstream test, not a pass. |
| `test_os.TimerfdTests.test_timerfd_ns_TFD_TIMER_ABSTIME` | Fails only on natemu in the original matrix: remaining-time deficit 1,725,875 ns at N=1 and 1,150,583 ns at N=4, against a strict 1,000,000 ns bound. Same source-level budget issue. Passes in all three N=4 diagnostic reruns, but fails again in the N=1 rerun. The fresh timing-bracket follow-up below directly observes budget-exceeding deficits with consistent remaining time and expiration counts. The original assertion is still recorded as failed; it was not weakened or relabeled. |
| `test_os.TimerfdTests.test_timerfd_initval` | Additional failure in `runs/re1-natemu4.serial`, absent from the original matrix and subsequent N=4 reruns: remaining time 0.249428791 versus 0.25 to three decimal places. Another immediate remaining-time assertion, with a 0.571209 ms deficit. The follow-up checks this relative-timer API and observes consistent remaining time; it does not retroactively measure this old sample. |
| Native-only `test_socket.ThreadedVSOCKSocketStreamTest.testStream` | Errors in both native runs: server `accept` times out and client `connect` reports `ENODEV`. The guest skips VSOCK support, including this case, on both emulators. This reference-environment failure supplies no positive guest coverage. |

The rerun image `cpyt-re.img`, whose selection is `ov-re/cpy-tests.txt`, selects the six diagnostic modules.
`runs/re{1,2,3}-natemu4.serial` retain three runs; `re1-natemu1.serial` one; `re{1,2}-qemu4.serial` two.
Natemu4 reruns 2 and 3 have the same failure/error set as QEMU4: four stdin failures, seven ZIP errors, and the
non-nanosecond absolute-timer failure. Repetition narrows the variable timer symptom; it does not convert it into
a verified timer implementation. The N=1 and N=4 post-build smoke suites below are separate selections and do not
cover every failing module.

### Skips and missing coverage

The 29 whole-module exclusions are identical across native and guests:

- Platform/build exclusions (20): the two asyncio Windows modules, `test_android`, `test_apple`, `test_devpoll`,
  `test_kqueue`, `test_free_threading` (GIL build), `test_launcher`, `test_msvcrt`, `test_startfile`, `test_winapi`,
  `test_winconsoleio`, `test_winreg`, `test_wmi`, and the six missing-Tk modules `test_idle`, `test_tcl`,
  `test_tkinter`, `test_ttk`, `test_ttk_textonly`, `test_turtle`.
- Disabled resources (9): `test_curses`, `test_pyrepl`, `test_peg_generator`, `test_smtpnet`, `test_socketserver`,
  `test_urllib2net`, `test_urllibnet`, `test_winsound`, `test_zipfile64`.

At case level, natemu4 and QEMU4 have the same skipped cases. Of the 1,856 guest skips, 1,764 also skip natively;
they remain unexercised baseline cases, not correctness passes. Common skips include resource, platform, build
configuration and test-fixture restrictions. The largest shared counts include `test_pathlib` (517), `test_capi`
(70), `test_selectors` (44), `test_subprocess` (42) and `test_pickle` (40). The retained `CT-CASE ... skip` lines
identify every case; a common skip is evidence of equal exclusion, not independent validation of the missing path.

All 92 additional guest skips are accounted for here (91 were native passes; one is the native VSOCK error above):

| Group | Additional guest skips | Source/recorded condition |
| --- | ---: | --- |
| `test_socket` | 76 | SCTP send/receive classes 42; CAN 8; kernel crypto 7; QIPCRTR 6; basic VSOCK 4 plus threaded VSOCK 1; ISO-TP 4; J1939 2; hostname/socket-name checks 2. The guest image/kernel lacks those protocol/service facilities; same exclusions on QEMU. |
| Locale/encoding | 12 | `test_calendar` 6; `test_builtin` 1; `test_format` 1; `test_io` 2; `test_readline` 2. Locale availability and default-encoding preconditions differ in the minimal image. |
| `test_cmd_line_script` | 1 | `test_script_as_dev_fd`: `/dev/fd` is absent in the image setup. |
| `test_os` | 1 | `ChownFileTests.test_chown_gid`: recorded reason is fewer than two groups. |
| `test_uuid` | 2 | `test_ip_getnode`, with and without extension module: no eligible interface/tool result in the guest environment. |

Seven native skips instead pass on both guest emulators: the two `LocaleCodecTest` surrogate-pass cases;
`test_ctypes.test_dlerror.TestNullDlsym.test_null_dlsym`; `test_ctypes.test_find.FindLibraryLinux.test_find_on_libpath`;
`test_shutil.TestArchives.test_unzip_zipfile`; `test_site.StartupImportTests.test_startup_imports`;
`test_sys.SysModuleTest.test_ioencoding_nonascii`. These differences concern the available locale, libraries, tools
and startup environment; they do not cancel other skips.

Natemu N=1 has three further skips, all recorded in `runs/cpyt4-natemu1.serial` and repeated in `re1-natemu1.serial`:
`CPUCountTests.test_process_cpu_count_affinity` needs at least two CPUs; `FileTests.test_closerange` could not
allocate two consecutive FDs; `TestInvalidFD.test_closerange` could not acquire an invalid FD range. The affinity
exclusion follows directly from N=1. The FD cases aborted setup, so their assertions did not run; N=4 passes do not
replace N=1 coverage. The focused follow-up below now passes both exact cases at N=1 and reproduces their setup
skips with a deliberately occupied descriptor.

## CPython builds

Configure and `make` finish with rc=0 in `native/native-cpyb.txt`, `runs/cpyb-natemu4.serial`,
`runs/cpyb2-natemu1.serial`, and `runs/cpyb2-qemu4.serial`. Each manifest has the same 474 paths. Against native,
471 hashes agree, including all recorded objects, libraries and programs. The three differing paths are:

- `./Makefile`;
- `./build/lib.linux-x86_64-3.13/_sysconfigdata__linux_x86_64-linux-gnu.py`;
- that module's `__pycache__/_sysconfigdata__linux_x86_64-linux-gnu.cpython-313.pyc`.

`scratch/Makefile.{native,guest}` and `scratch/sysconfigdata.{native,guest}` preserve the text. After removing
serial-line CR characters, the Makefile differences are exactly CXX (`c++` versus `gcc`), INSTALL and MKDIR_P
(`/usr/bin` versus the sysroot path), and PYTHON_FOR_REGEN (`python3` versus `python3.13`). The sysconfig differences
are CXX/LDCXXSHARED and the corresponding install/mkdir variables. These record different available build tools,
not different compiled C output. Natemu/QEMU guest hashes agree on 473 paths, including both text files; only the
`.pyc` differs. Its bytes were not retained separately from the manifest, so a timestamp-header explanation is
plausible but unverified. Do not report a completely byte-identical build.

Each guest-built interpreter's smoke run reports **5,936 cases: 5,780 pass, 156 skip, zero fail/error**. Those skips
are equal across natemu1/natemu4/QEMU4. This confirms the selected smoke behavior of the built interpreter, not
all 473 modules against the guest build (the full matrix uses the native-built interpreter).

## glibc string and wide-character tests

`native/gstr-cmds.txt` has 155 executable commands captured from native `make check`, replayed unchanged with the
build-tree loader and libraries. `native/native-gstr{,-r2}.txt` both report 140 pass and 15 unsupported. QEMU4 matches.
Natemu N=1 and N=4 each report **139 pass, 15 unsupported and one failure**, with no capped tests.

All 15 unsupported commands are `string/tst-*-rtm`: memchr, memcmp, memmove, memrchr, memset, strcasecmp, strchr,
strcmp, strcpy, strlen, strncasecmp, strncmp, strrchr, wcscmp and wcsncmp. These RTM tests return 77 on the AMD
reference as well as both emulators; no transactional-memory coverage is claimed.

The native make summaries contain 164 entries, so replaying 155 commands is not the whole upstream suite.
The nine not replayed are `check-installed-headers-c`, `check-obsolete-constructs`, `check-wrapper-headers` in each
of string/wcsmbs; `string/tst-svc-cmp` (all seven native PASS); and `string/tst-strerror`, `string/tst-strsignal`
(native UNSUPPORTED). `gstr.py` selects executable test programs from wrapper logs, so these summary-only entries
were not captured for guest execution. The later pinned-recipe audit below classifies all nine and identifies a
genuine missing input/output contract in the old `tst-svc` replay; the historical rc-zero counts above remain counts
of recorded outcomes, not proof that its sorting corpus was exercised.

The failure is `wcsmbs/tst-wcstod-round` (`runs/gstr0-natemu{1,4}.serial`), whose printed diagnostics report missing
inexact flags for long-double/`_Float64x` conversions. The runner preserves only the first 60 output lines of a
failing test, so those lines do not enumerate every internal assertion. Native and QEMU pass that test.

`repro/wcstold/wcstold.c` is a retained minimal diagnostic; `repro/wcstold.img` runs it with the glibc build tree.
For `1.00000005960464477550`, natemu prints `3fff8000008000000000 inexact=0` from both strtold and wcstold; QEMU prints
`3fff8000008000000001 inexact=1`. Natemu also reports `(long double)1 + 2^-63 == 1`, while QEMU reports false;
the double conversion agrees. The native replay of the original glibc test passes, but no native output of this
minimal diagnostic is retained here.

Repository `crates/abi/src/x87.rs` explicitly converts 80-bit images to double, and the existing
[userspace report](correctness-userspace.md#findings) records the owner's temporary acceptance of reduced x87
precision. That source and this diagnostic support a reduced-precision explanation. They do **not** independently
establish that every missing inexact flag is entailed by the accepted limitation, nor constitute acceptance of this
new failure. Keep this case as a known observed incompatibility pending a focused instruction/flag analysis or the
precise-x87 work; do not silently mark it passed.

## Reproduction and remaining work

For the existing comparison, pass each comma-separated set of 12 `runs/cpytN-EMU.serial` files to
`python3 tools/correctness/cpy-check.py REF RUN`; use `hashes REF RUN` for the build manifests. `native/cpy-tests.txt`,
`native/gstr-cmds.txt`, the images, queue, build script, manifests and serial files preserve the inputs and outcomes.
The checker was corrected during this analysis to split `CT-CASE` at its final whitespace: subtest identifiers
contain spaces. `python3 -B tools/correctness/test_cpy_check.py` verifies distinct spaced identifiers and statuses.
For the skip-set accounting above only process-address text (`object at 0x...`) in two urllib subtest names was
normalized; it is not a semantic case difference. Totals come directly from the unchanged result records.

1. The lane scripts, parser regression and completed reports are preserved under
   `natemu-raw/correctness-followup-20261107-01/source/`; the clean emulator worktree does not itself contain those
   originally untracked files. The original raw README remains a partial recipe; this report supplies the closeout.
2. The asyncio/ZIP image diagnosis and focused reruns are complete below. A future full-lane rerun should use the
   corrected image environment; this follow-up does not replace the old full-matrix results or require another
   full run just to re-establish the eleven diagnosed cases.
3. The bounded timer and FD diagnostics are complete below. Timer deficits are accounted for by elapsed execution
   in the measured samples; original upstream wall-time assertions remain failed. Exact original FD ownership and
   general host-to-guest clock calibration were not measured, and are not prerequisites to the scoped conclusions.
4. Separate the wcstod result-precision mismatch from exception-flag correctness with an instruction-level repro;
   retain a native run of that minimal diagnostic before closing attribution. Exact x87 remains outside the current
   reduced-precision behavior; this report does not authorize or implement a precision redesign.

The original matrix above remains unchanged. The follow-up below adds bounded fresh guest execution; it does not
replace excluded cases or supply a new native-reference run.

## Targeted follow-up

New evidence is under `natemu-raw/correctness-followup-20261107-01/cpydiag/`, outside the original run. Its
`manifest.json` binds the overlay and image hashes; `overlay/ct/repro/diagnostic.py` and `run.sh` retain the method.
It uses the original CPython image, emulator worktree and series66-gather compiler, with a 300-second boot cap.
The preparer did not run the VM; the coordinating executor did. The selected upstream test methods and assertions
are unchanged, and their source hashes are printed before and after the environment corrections.

All three planned boots completed, exit 0 and no recorded contention: `runs/cpydiag-{natemu1,natemu4,qemu4}.serial`
and `runs/runs.jsonl`. Each has exactly four phase results (before=1, fix=0, after=0, measure=0), a successful end
record, 18 distinct one-shot samples and two periodic samples. Recomputed input, script and image hashes match
`manifest.json`; the three upstream test-source hashes match before/after and across all boots.

| Check | natemu N=1 | natemu N=4 | QEMU N=4 |
| --- | ---: | ---: | ---: |
| Guest seconds | 20.02 | 19.97 | 27.09 |
| Eleven cases before correction: failures / errors / skips | 4 / 7 / 0 | 4 / 7 / 0 | 4 / 7 / 0 |
| Same eleven after correction: pass / fail / error / skip | 11 / 0 / 0 / 0 | 11 / 0 / 0 / 0 | 11 / 0 / 0 / 0 |
| Exact closerange cases, fresh-process passes | 2 | 2 | 2 |
| Same closerange cases, expected skips with fd4 held | 2 | 2 | 2 |
| One-shot values inside exact measured bounds | 18 / 18 | 18 / 18 | 18 / 18 |
| Periodic expiration counts equal both measured bounds | 2 / 2 | 2 / 2 | 2 / 2 |

Creating the device links after devtmpfs mounting and giving Lib files ZIP-valid mtimes closes the eleven selected
image failures in each configuration, with original test assertions unchanged. Every one-shot expiration read
returned one, completed after the earliest deadline and left the timer disarmed. Both periodic timers returned six
expirations. The timer harness's extra 1 µs allowance was unnecessary: all 54 remaining values lie inside the exact
brackets. Those brackets use clock reads immediately before and after gettime, with the settime interval also
bounding a relative timer's deadline.

The timer samples exercise absolute float, absolute nanosecond and relative float APIs, three repetitions each,
against realtime and monotonic clocks. Ranges of the deficit from the original requested offset, across the six
samples per API, are:

| API | natemu N=1, µs | natemu N=4, µs | QEMU N=4, µs |
| --- | ---: | ---: | ---: |
| Absolute float | 59.042–2,027.876 | 34.311–1,973.250 | 100.907–671.095 |
| Absolute nanosecond | 14.125–1,533.167 | 35.750–1,772.208 | 68.000–401.002 |
| Relative float | 8.667–121.708 | 9.500–79.708 | 49.000–57.000 |

The first realtime absolute-float samples exceed the original approximately 0.5 ms budget in all three configurations;
settime alone takes 1.715167 ms, 1.667125 ms and 0.566004 ms respectively. The first realtime absolute-nanosecond
samples exceed the 1 ms budget on both natemu runs; settime consumes 1.507000 ms and 1.745750 ms. In every case the
returned remaining value fits its measured clock bracket. Thus execution elapsed before gettime accounts for the
budget-exceeding deficit in these fresh samples, with no observed remaining-time or expiration-count inconsistency.
The first samples are slower than later samples; no separate compilation-cost attribution was measured.

These diagnostics do not retroactively measure the old samples, rerun or loosen the original timer assertions,
calibrate guest time against an independent host clock, or establish every timer interleaving. Late read completion
ranges from 0.083–5.739 ms at natemu N=1, 0.128–6.694 ms at N=4 and 1.247–11.977 ms at QEMU4; this includes scheduling
and is not equated to hardware interrupt-delivery latency. No further timer run is needed to establish the bounded
remaining-time explanation; broader timer performance or clock claims would require their own measurement.

The FD control proves that an occupied fd4 with fd3 free is sufficient to produce both original setup skips;
it does not identify what held that descriptor in the old regrtest process. The fresh N=1 pass closes the selected
closerange behavior gap under the new process setup, while the old skip remains part of the original matrix.

The read-only attempt to inspect the pinned glibc build rules on the AMD reference failed with `No route to host`.
The initial audit left the six header/source checks' exact recipes and `tst-svc-cmp` unclassified. The subsequent
pinned-recipe audit below resolves that classification; `tst-strerror` and `tst-strsignal` remain native UNSUPPORTED.

### Pinned recipe and capture audit (November 7 UTC)

The exact Debian glibc **2.41-12+deb13u4** string/wcsmbs Makefiles, Rules, supporting scripts and test data were
recovered from Debian Sources, with URLs and hashes in `natemu-raw/correctness-blocked-20261107-01/svc-cmp/`.
The nine native summary entries absent from the 155-command list now have source-backed classifications:

- The six header entries are native-passing build/source checks: compiler `-fsyntax-only` checks and Python checks
  over source headers/wrappers. They are outside the executable replay and require the original source/build paths.
  They were not blocked by VM stalls and were not run under natemu.
- The two translation/container tests require translated message catalogs and their test binaries, which are absent
  from the retained guest image. Their native result is UNSUPPORTED. The recipe can skip for missing msgfmt, but
  the exact native skip reason was not retained; it must not be inferred from that conditional alone.
- `tst-svc-cmp` is an output assertion: `cmp tst-svc.expect tst-svc.out`. The associated `tst-svc` binary reads strings
  from the upstream `.input`, sorts using `strverscmp`, and writes stdout. The old replay instead supplied
  `/dev/null` and discarded stdout on rc0. Its successful process exit therefore did **not** exercise the supplied
  sorting corpus or establish the expected-output assertion.

The pinned directory inventories contain only `string/tst-svc.input`/`.expect` and no `.input`/`.expect` fixtures in
wcsmbs. The Makefiles contain no other executable-output comparison target. This targeted audit found no second
lost input/output assertion in these selected recipes; it is not a claim about every possible upstream suite.

The corrected contract was then executed in three separately reviewed boots: natemu N=1/N=4 on the **original
4527322 build and series66 compiler**, plus QEMU N=4. The captured argv, environment, cwd and retained executable/
loader/libc are unchanged; only the missing pinned stdin and stdout comparison are restored. Both `tst-svc` and
`tst-svc-cmp` return zero on all three, with the same exact 199-byte output (SHA-256 `2cb061c2…5e0f176`), matching
the expected file. Guest times are 0.13/0.17/0.08 seconds; all boots finish without contention or caps. The checker
requires all eight distinct input/binary/output hashes, binds them to the preparation manifest and independently
extracted base-image files, checks queue identities and CT-END, and rejects missing hash evidence. Full stdout,
empty stderr and `analysis.json` are retained under `svc-cmp/`.

This closes the omitted sorting-input/output assertion without rewriting the historical 155-command outcome
counts. The six native-only build/source checks and two native-unsupported translation tests remain explicit scope
limits, not guest passes or missing post-stall jobs.

### FD/image checks after correction (November 7 UTC)

At the owner's request, the remaining `/dev/fd` verification gap was closed on the original 4527322/series66 build
at N=1/N=4 and on QEMU N=4. Each boot first records the unchanged `test_script_as_dev_fd` skipping with
`requires /dev/fd platform`; then applies the existing device-link/mtime correction. Fresh interpreter processes
run the eleven asyncio/ZIP cases, `CmdLineTest.test_script_as_dev_fd`, and the two original closerange cases.
All **14 pass per configuration**, with no post-fix skips, failures or errors. Guest times: 4.40/4.39/8.40 seconds;
all three boots complete without caps or recorded contention. Four upstream test-source hashes remain unchanged.
The before-fix skip is a negative control, not an unresolved post-fix outcome. The correction is applied inside
these rerun guests; the historical base image and its original result matrix remain preserved.

`natemu-raw/correctness-fd-afterfix-20261107-01/` retains the exact selections, fixture correction, pinned inputs,
full test-name output, queue, manifests and checked results. This closes the previously stated dev_fd rerun gap;
it does not add the separate stress-ng FD/filesystem family to the campaign.

Independent findings review by `/root/rerun_review` checked all three configurations' retained results, unchanged
source hashes and exact timer brackets, and accepted these bounded attributions with the limitations above.
