# Correctness pass: kernel x86 selftests and x86-TSO litmus tests in a Linux guest

**Update November 7 UTC:** the ICEBP stall is fixed and independently reviewed. The new regression passes 20/20;
the original full `mov_ss_trap_64` passes at N=1/N=4 on the preserved no-stop snapshot plus this fix.
See [the fix and validation](#icebp-fix-and-validation-november-7-utc). Original campaign results below remain historical.

Status: **done** (November 6, 2026). Evidence, rerun steps and raw logs: `natemu-raw/correct-x86-20261106-01/`
(README there). Scripts: `tools/correctness/` (this lane's new files `x86-remote.sh`, `run-x86st.sh`, `run-litmus.sh`,
`run-x86con.sh`, `litmus-gen.py`, `litmus-check.py`, `x86-overlay.sh`, `x86-campaign.sh`; lane 1's harness reused unchanged: `boot.py`, `vm.sh`,
`idle-boot.sh`, `campaign.sh`, `compare.py`, `init`; see [correctness-userspace.md](correctness-userspace.md)).

natemu: the `natemu` stub and release runtime built from 178ffd8 in a clean worktree (`build-stub.sh`; runtime SHA-256
`bb4e8736...b2b2`, the same as lane 1's), fexc `series65-p2`, compile cache off, 2 GiB, Debian 6.12.107 kernel,
`console=ttyS0 mitigations=off nokaslr`. QEMU: 11.1.2 TCG `thread=multi`, lane 1's like-for-like CPU model
(`Haswell,-pcid,...,+sha-ni,+adx,+clflushopt,+clwb,+smep,+umip`). Reference: the AMD machine (`ubuntu@192.168.12.11`,
Ryzen 7 8845HS KVM guest, Ubuntu kernel 7.0.0-42, 4 vCPUs), as its normal user (user-mode only). Images: the play
rootfs (rebuilt bit-identical, SHA-256 `c3fb9721...7004`) + the correctness `/init` + one suite.

## Suite 1: kernel x86 selftests

Source: `tools/testing/selftests/x86` of Debian `linux_6.12.107-1` (the guest kernel's source package; `.orig.tar.xz`
SHA-256 `efc76d3c...6919` checked against the `.dsc`, whose hash is in trixie's signed `Sources` index; none of Debian's
78 patches touches `tools/testing`). Built on the AMD machine with Debian trixie gcc 14.2.0-19 against Debian libc 2.41,
`make all_64 EXTRA_CFLAGS=-static` (uapi headers: Debian linux-libc-dev 6.12).

Programs (25): the upstream 64-bit set (22: amx, check_initial_reg_state, corrupt_xstate_header, fsgsbase,
fsgsbase_restore, ioperm, iopl, lam, mov_ss_trap, nx_stack, sigaltstack, sigreturn, sigtrap_loop, single_step_syscall,
syscall_arg_fault, syscall_nt, syscall_numbering, sysret_rip, sysret_ss_attrs, test_mremap_vdso, test_shadow_stack,
test_vsyscall), plus `ldt_gdt_64` and `ptrace_syscall_64` (upstream builds their 64-bit variants only where `-m32` works;
built the same way by hand), plus `test_vsyscall_dyn_64` (dynamic: the static build cannot `dlopen` the vDSO, so its
vDSO comparisons are skipped).

Excluded: every `_32` program (24: natemu has no compatibility mode, owner decision 3), and `srso.c` (not in the
Makefile's lists).

Runner `run-x86st.sh`: each program in an empty directory, 300 s kill timeout, its output kept (`CT-LOG` lines) and a
`CT-RESULT` line with the exit code and the counts of `[OK]`/`[FAIL]`/`[SKIP]` lines. The guest runs as root; natively
as the normal user. Since a test can stop the whole machine, the natemu runs are **one test per boot** (`part=K/25`;
queue `q5.txt`), at N=1 and N=4; QEMU ran the suite once at N=4 (`x86st2.img`, mov_ss_trap skipped there and run
alone as `runs/movss-qemu1`).

Environment notes (native, AMD VM as a user): `iopl_64` and `ioperm_64` need root (natively they only check that the
calls fail: 1 and 3 `[OK]`; as root in the guests 15 and 19); `amx_64` (no AMX) and `lam_64` (no LAM) exit 4 (skip) on
every platform; `test_shadow_stack_64` runs natively (the AMD VM has user shadow stacks) and fails its uretprobe
subtest there (user; uprobes); natemu's and QEMU's profiles have no shadow stacks (exit 1, one `[SKIP]`, as expected);
`syscall_arg_fault_64` SYSENTER subtests: `[SKIP] Illegal instruction` on AMD (SYSENTER is #UD in long mode on AMD),
executed on the Intel-profiled guests.

### Results (one test per boot, N=1 and N=4; native; QEMU N=4)

| | natemu N=1 | natemu N=4 | QEMU N=4 | native (AMD, user) |
| --- | --- | --- | --- | --- |
| finish with native's result up to environment | 20 | 20 | 24 | 25 run: 22 exit 0, amx/lam exit 4 (feature skips), test_shadow_stack exit 1 (uretprobe, as a user) |
| stop the whole machine | 5 | 5 | 0 | - |
| wrong result | 0 | 0 | 1 (mov_ss_trap, QEMU's) | - |

The 20 (N=1 and N=4 alike): amx and lam (exit 4, no such feature, everywhere), check_initial_reg_state,
corrupt_xstate_header, fsgsbase (110 `[OK]`), fsgsbase_restore, ioperm and iopl (19 and 15 `[OK]` as root; natively, as a
user, only the refusals), ldt_gdt (63 `[OK]`; at N=1 one `[SKIP]`, "Cannot set affinity to CPU 1"), nx_stack,
ptrace_syscall (10), sigaltstack, sigtrap_loop, syscall_nt (9), syscall_numbering (701), sysret_rip (52), test_mremap_vdso,
test_shadow_stack (exit 1 "Could not enable Shadow stack": no shadow stacks in the profile, as QEMU), test_vsyscall and
its dynamic build (13 `[OK]`). Environment differences, all checked in the logs: the Debian kernel has no vsyscall page
(`vsyscall=none` default: 5 `# SKIP vsyscall_map_x` in natemu and QEMU; Ubuntu's kernel maps it), at N=1 the getcpu
check on CPU 1 skips, sigaltstack's "insufficient size" case is not run where the signal frame is small (no AVX-512 in
natemu's and QEMU's profiles; the AMD VM has it), and output hashes differ by printed addresses only. N=1 and N=4 are
identical but for the CPU-count skips. natemu N=4 equals QEMU N=4 on all 20 (counts and skips; vsyscall's 5 skips at N=4 as QEMU). QEMU also finishes the
four compatibility-mode tests (sigreturn 19 `[OK]`, single_step_syscall 5, syscall_arg_fault 5 with SYSENTER executed on
its Intel profile where AMD gives #UD and skips, sysret_ss_attrs 1); `mov_ss_trap_64` under QEMU (`runs/movss-qemu1`)
takes a second, early SIGTRAP after each MOV SS and dies at `MOV SS; SYSCALL` (SIGSEGV): QEMU does not hold the data
breakpoint across MOV SS, a QEMU defect, so native is the reference there.

Machine stops: `mov_ss_trap_64` (finding 1: a natemu defect) and four tests entering compatibility mode (finding 2: the
documented refusal).

## Suite 2: x86-TSO litmus tests

Tools: herdtools7 **7.58** (tag archive SHA-256 `1b727984...066ad`; the catalogue, export-ignored from the archive,
from the same tag, commit `1ca343e1`), built on the AMD machine with Debian trixie's OCaml 5.3.0 / dune 3.17.2 / menhir
20240715 / zarith 1.14 (unpacked, run in place). Oracle: herd7's final states per test under its default models,
`x86tso-mixed.cat` (X86_64 tests) and `x86tso.cat` (X86): a state outside them observed by a run is a defect.

Corpus (`litmus-gen.py`), 1,318 tests, all accepted by herd7 and litmus7:

| Family | Tests | Source |
| --- | --- | --- |
| cat86, cat64 | 23, 28 | herdtools7 7.58 `catalogue/x86/tests`, `catalogue/x86_64/tests` (SB, MP, LB, R, S, 2+2W, WRC, RWC, ... with MFENCE and po variants) |
| ltx2, ltx3, ltx4, ltxco | 21, 100, 490, 33 | `litmus-tests/litmus-tests-x86` (a6dadce, diy-generated by Flur and Sewell): BASIC 2/3/4-thread cycles (IRIW and friends) and coherence |
| rmw | 123 | diy7 7.58 critical cycles with Rmw (XCHG) edges, `-size 6 -nprocs 4` |
| lk-add, lk-inc, lk-xor, lk-xchg, lk-cmpxchg | 100 each | every cat64/ltx2/ltx3 test with an MFENCE, each MFENCE replaced by a locked RMW on the thread's own location: LOCK ADD $0, LOCK INC, LOCK XOR $0, XCHG, LOCK CMPXCHG (succeeding) |

Not covered: string instructions and LOCK XADD (herd7 7.58's X86_64 parses neither); unaligned, page-crossing and
mixed-size accesses (not attempted).

Harnesses: litmus7 `-mach x86_64 -smt 1 -affinity incr1 -a 4 -s 100000 -r 100`, Debian gcc, dynamic against Debian libc
(in the image). Runner `run-litmus.sh` (part `K/N[,a=A][,r=R]`): tests with more threads than A are skipped (N=2: the
237 tests of at most two threads). Checker `litmus-check.py`.

### Results

-r 100 -s 100000 everywhere (10 M iterations per test and instance; -a 4 runs two instances of a 2-thread test at once).
natemu N=4 took 1,825 s for all 1,318 tests (two boots), N=2 224 s for 237; native 2,284 s and 257 s.

| Run | Tests | FORBIDDEN (x86-TSO) states observed | allowed states never observed | exists-tests with an allowed condition never observed |
| --- | --- | --- | --- | --- |
| native, -a 4 | 1,318 | **0** | 178 of 13,403 (119 tests) | 81 of 340 |
| natemu N=4 | 1,318 | **0** | 322 of 13,403 (244 tests) | 198 of 340 |
| native, -a 2 | 237 | **0** | 21 of 786 | 0 of 52 |
| natemu N=2 | 237 | **0** | 54 of 786 (38 tests) | 10 of 52 |
| QEMU N=4 (four parts, 5,320 s) | 1,318 | **0** | 448 of 13,403 (372 tests) | 340 of 340 |
| natemu N=4, -r 1000, the 198 | 198 | **0** | 96 of 2,729 | 89 of 198 |
| natemu N=2, -r 1000, the 10 | 10 | **0** | 2 of 41 | 0 of 10 |

No run shows an outcome x86-TSO forbids: no defect in plain loads and stores, MFENCE, XCHG and LOCK ADD/INC/XOR/CMPXCHG,
in 2- to 4-thread tests (IRIW-like, WRC, RWC, Z6, coherence) at N=4 and N=2. The native results check the oracle and the
checker (normalised states; 0 forbidden on hardware).

Weaker evidence: natemu shows fewer of the allowed relaxed outcomes than the AMD machine. N=4: 119 conditions natemu
never showed but native did (57 of them ltx4 4-thread tests, 45 lk-* variants), 2 the other way; N=2: natemu never
showed the store-buffering outcome in 10 M iterations (`cat64.SB`, `cat86.SB`, `cat64.R`, the `+rfi` variants, three
lk/rmw R tests; native at -a 2 showed every allowed condition), though at N=4 it did (`cat64.SB` 4,385 times in
20 M). Allowed-but-unobserved is not a defect (x86-TSO allows, never requires, a relaxation; a stronger machine is
correct); it limits what the run can show (a test whose relaxed outcome never appears cannot catch a fence that fails).
Rerun at N=2 with -r 1000 (100 M iterations; `runs/lit10x-natemu2`): all 10 conditions appear, but rarely (`cat64.SB`
4 times, `cat86.SB` once, `cat64.R` once, against about 2,500 per 10 M natively), still 0 forbidden; and `cat64.SB`'s
fully interleaved outcome (both loads see the other store) never appears at N=2 in 100 M, so the two vCPUs of a
2-CPU natemu rarely run the test bodies at the same time (cause not established; the AMD machine shows it at -a 2). At N=4,
-r 1000 of the 198 tests whose condition natemu never showed at -r 100 (`runs/lit10x-natemu4-p*`): 109 now show it;
89 still never do (82 of them ltx4 4-thread tests), and only 34 of those 89 did natively at -r 100. QEMU TCG never shows
any relaxed condition (340 of 340 never observed: it runs these tests as if sequentially consistent), so for the
weaker evidence natemu sits between QEMU and the AMD machine.


## Findings

1. **Original natemu defect, now repaired: ICEBP right after MOV SS with a data breakpoint on the MOV SS operand stopped the machine.**
   `mov_ss_trap_64` hangs natemu at `[RUN] MOV SS; ICEBP` (the vCPU spins; cancelled at the cap, N=1, twice). Minimal
   repro `natemu-raw/correct-x86-20261106-01/repro/icebp/icebp.c` (`icebp STEP`): step 1 (ICEBP alone) and step 2
   (`mov ss; icebp`) give SIGTRAP after the 0xF1 byte under natemu as natively; step 3 (DR0 = read/write watchpoint on
   the MOV SS operand, set by a ptrace child, then `mov ss; icebp`) stops natemu with
   `Watchdog { pc: 140738562803956, el0: false, asked: 0 }` (`runs/icebp-natemu1.*`); natively it gives one SIGTRAP at
   RIP after the 0xF1 byte. The steps before it in mov_ss_trap (MOV SS; INT3 / INT 3 / prefixed INT3 / INT 4 with the
   same watchpoint) match native. SDM-defined: MOV SS holds the data breakpoint's #DB until after the next instruction;
   ICEBP is a trap-class #DB; a CPL-3 program must not stop the machine. Passed to the gather-fix lane (same class: a
   legal instruction becoming a host stop). Rerun: `repro/icebp.img` (`ct=repro`), then `runs/x86st-n1-09`.
   QEMU differs from native here too (it delivers the watchpoint #DB right after MOV SS, a second SIGTRAP, and the
   test dies at `MOV SS; SYSCALL`): a QEMU defect, not a reference.
2. **Known limit (owner decision 3), not a defect: four 64-bit selftests enter compatibility mode and stop the machine
   with `UNSUPPORTED_MODE`** (the documented typed refusal for compatibility-mode code, boot-handoff §4). Located with
   the output straight to the console (`run-x86con.sh`, `runs/x86con-n1-*`): `sigreturn_64` at its first sigreturn
   into a 32-bit CS (`[RUN] Valid sigreturn: 32-bit CS (23), 32-bit SS`, after the 64-bit CS case passed);
   `single_step_syscall_64` at `[RUN] Set TF and check SYSENTER` and `syscall_arg_fault_64` at `[RUN] SYSENTER with
   invalid state` (SYSENTER in 64-bit mode on the Intel-profiled guest enters Linux's compat entry, which returns to the
   32-bit user CS; on the AMD reference SYSENTER is #UD in long mode and the subtests skip); `sysret_ss_attrs_64` at
   `[RUN] Syscalls followed by SS validation` (`call32_from_64`, a far return to a 32-bit CS, `thunks.S`). Every
   subtest before the stop matched native. These belong with the excluded 32-bit programs; the record only notes that
   CPL-3 code reaching compatibility mode ends the whole machine rather than the process, by design.

## Original campaign summary

Two suites. Kernel x86 selftests (Debian 6.12.107 source, 25 64-bit programs, one per boot at N=1 and N=4): 20 equal
native up to environment and equal QEMU; one natemu defect, **ICEBP after MOV SS with a data breakpoint pending stops the
machine** (finding 1, minimal repro); four tests stop at their first entry into 32-bit code, the documented
compatibility-mode refusal (finding 2). x86-TSO litmus (1,318 tests: herdtools7 catalogue, litmus-tests-x86, diy7 XCHG
cycles, five locked-RMW-as-fence families): **no forbidden outcome** in natemu at N=4 or N=2, nor natively or on QEMU;
natemu shows fewer of the allowed relaxed outcomes than the AMD machine (weaker evidence, not a defect).

## Next steps

1. Finding 1 is repaired by the November 7 ICEBP fix below. Preserve its regression when integrating the separate
   no-stop worktree; its final dirty corrections and series69 compiler remain necessary for that branch's results.
2. Not covered by this ordering corpus: string instructions and LOCK XADD (herd7 7.58's X86_64 has neither: a hand-written harness), unaligned,
   page-crossing and mixed-size accesses (not attempted; herdtools7 has a mixed-size x86 model, `x86tso-mixed.cat`), and
   accesses to pages natemu's resident emulates. The 89
   allowed conditions natemu never showed could be retried with litmus7's other modes (`-mem indirect`, `-st`, a
   different `-affinity`) if stronger evidence on fences is wanted.

## Retained-result audit and branch status (November 6, 2026)

Independent rereading of `runs/runs.jsonl` finds 19 unfinished boots out of 72. They are repeated observations of two
causes, not 19 distinct defects: four cap cancellations at the MOV SS/ICEBP test (the initial whole-suite attempt,
the isolated movss repro, and the N=1/N=4 part-9 runs), one resident watchdog in the minimal ICEBP repro, and fourteen
compatibility-mode stops (initial suite attempts, individual tests and console-output localization). These caps do
not become passes because the runner terminates cleanly. The minimal repro isolates the trigger to MOV SS followed
by ICEBP with a watchpoint on the MOV SS operand; either instruction sequence without that watchpoint works.

The watchdog PC `0x8000400ac8f4` maps to `execution::watch::program + 0x1c` in the retained runtime (SHA-256
`bb4e8736901389111632da5552ee36384a4455529beee79a0ae0575606bad2b2`, identical to the userspace lane's retained binary).
This is a sampled PC during debug-register programming, **not proof that this instruction itself loops**. The pending
debug-trap/delivery interaction under Linux remains the unresolved defect; the earlier program-driver probe passing
at CPL 0 does not establish the CPL-3/Linux path. No successful rerun of this trigger is retained in the no-stop lane.

The separate, unmerged `no-stop` worktree changes finding 2's containment, not compatibility-mode support. Its final
`natemu-raw/nostop-20261106-01/runs/f-x86st-n{1,4}-{13,15,16,20}.serial` logs all reach CT-END: `sigreturn_64` and
`sysret_ss_attrs_64` exit 139 (the process receives SIGSEGV on the unavailable transition), while
`single_step_syscall_64` and `syscall_arg_fault_64` exit 0 with five OKs each (their signal handlers recover). Those
first two are contained test failures under the recorded deviation, not successful compatibility execution. The
original results above belong to `178ffd8`; they must not be relabeled with the branch's outcome before integration
and a current-build check. The other exclusions remain: 24 32-bit programs, absent AMX/LAM/CET, root/user differences,
the N=1 CPU-affinity skips, and the Debian vsyscall/signal-frame differences. QEMU's early watchpoint delivery in
`mov_ss_trap` and the native shadow-stack uretprobe failure are reference/environment failures, not natemu passes.

Rerunning `litmus-check.py` over all seven retained result groups reproduces the table: zero failed-to-run tests,
zero unknown-oracle tests and zero forbidden states. The initial N=2 run explicitly skips 1,081 tests with more than
two threads; they ran in the full N=4 campaign. The 100-million-iteration follow-ups are selected subsets (198 tests
at N=4, 10 at N=2), not replacement full campaigns. The 89 allowed conditions still absent after the N=4 follow-up
are coverage limits, not failures or timeouts; nothing in these retained observations diagnoses their scheduling
cause. String/LOCK-XADD, unaligned, crossing, mixed-size and resident-emulated accesses remain outside this corpus.
Separate no-stop XADD, split-lock and mixed-overlap probes pass; those functional checks do not establish systematic
ordering coverage across these placements and paths. Keep litmus7 and add focused tests with reviewed x86 outcomes;
herd's mixed-size model helps define those outcomes but is not itself a complete executable suite.

### ICEBP: original hypothesis and discriminating check (superseded by validation below)

The initial hypothesis was **a second #DB overwriting the first #DB's IST frame before its handler starts**.
The resident source establishes the following path: `segments::transaction` sets `mov_ss` after reading its watched
operand; `commit`/`stepped` move that access's hit into `s.debug`'s pending trap bits; `trap_due` retains them during
the MOV SS shadow. `system` clears the shadow before executing ICEBP, whose `transaction` arm calls
`deliver(Event::Debug { length: 1, ... })`. `delivery::delivery` preserves the old pending trap bits. On returning to
`find_from`, `trap_due` therefore delivers another #DB **at the first handler's entry, before any handler instruction**.
`delivery::attempt` rereads the TSS IST pointer for each delivery. If the gate has nonzero IST, both 40-byte frames
occupy the same addresses: the second replaces the user's return RIP/CS/RSP with the handler entry, kernel CS and
that same frame's address. A handler IRET can then return to its own entry repeatedly.

This fits Linux's entry structure: the [v6.12 IDT table](https://github.com/torvalds/linux/blob/v6.12/arch/x86/kernel/idt.c)
assigns #DB an IST stack; the exact [v6.12.107 entry stub](https://github.com/gregkh/linux/blob/v6.12.107/arch/x86/entry/entry_64.S)
(`idtentry_mce_db`) switches a user entry off IST only after several instructions. Its
[debug handlers](https://github.com/gregkh/linux/blob/v6.12.107/arch/x86/kernel/traps.c)
(`exc_debug_kernel`, `debug_read_reset_dr6`) write debug registers, consistent with sampling `watch::program`, which
natemu calls on guest MOV-to-DR instructions. The earlier `gather-20261105-01/icebp/probe.py` installs a #DB gate with
**IST = 0**: its nested frames use successively lower RSPs, so it cannot expose this overwrite. Likewise MOV SS; INT3
first enters a different vector, unlike two immediate deliveries through the same #DB IST gate.

At that audit this was a likely explanation, **not yet a confirmed root cause**: the running Debian
guest's actual gate/IST and the two frame writes have not been captured. The next bounded check is the existing
minimal step-3 repro with a diagnostic stop immediately before a second vector-1 frame write when
`old RIP == gate target`, `old RSP == new frame base` and `IST != 0`. Capture both deliveries' source RIP/CPL/RSP,
pending debug bits, DR6/DR7, gate IST, TSS-selected stack pointer and five frame slots. A matching pair proves the
overwrite path in the failing guest; absence redirects diagnosis toward watchpoint/restart handling. An independent
program-driver control should differ only by #DB gate IST=0 versus IST=1 with a fixed TSS IST pointer. No diagnostic
instrumentation or execution was performed here. The correct architectural treatment of coincident deferred #DB and
ICEBP still needs a reference check before choosing a fix; the retained user signal handler uses `siglongjmp`, so its
single observed SIGTRAP alone does not establish the CPU's complete delivery sequence.

### ICEBP fix and validation (November 7 UTC)

Evidence: `natemu-raw/icebp-fix-20261107-01/`. A fresh unpatched Linux repro stalls at step 3. The bounded driver
reproduces two deliveries with IST=0 and an overwritten return frame with IST=1. A temporary Linux diagnostic stops
immediately before the second write: current RIP equals the #DB entry, current RSP equals the new frame base,
and the existing IST3 frame still contains the valid user return state. Diagnostic instrumentation was removed.

The fix clears the old pending trap before ICEBP delivery. Clearing before delivery preserves newly generated
frame-store watchpoints; INT3 and other interrupt behavior stay unchanged. It is integrated in `correctness`, with
`tools/run_icebp.py`. Independent code review accepted the change and root replicated all 20 cases.

| Check | Result |
| --- | --- |
| New frame/count regression, IST=0/1 and default/one-instruction blocks | Before: 4/20 fail; after: 20/20 pass; independent replay: 20/20 pass |
| Existing S4 debug controls, six programs at both block sizes | 12/12 pass |
| Minimal Linux repro, all three steps, current source N=1/N=4 | All six executions pass |
| Same minimal repro on copied final no-stop source plus fix, N=1/N=4 | All six executions pass |
| Original full `mov_ss_trap_64`, copied no-stop source plus fix, N=1/N=4 | Both pass, rc=0, fail=0, skip=0, including final `[OK] I aten't dead` |

Current `correctness` alone passes ICEBP in the full test but stops later at its existing SYSENTER mode boundary.
The no-stop snapshot includes all twelve dirty tracked files and series69; its original worktree was untouched.
Two empty full-test selections during harness setup are excluded, as are the superseded initial driver setup results.
No final successful run was capped or contended. Resident stack audit and the 53 delivery-model tests pass.

The hardware reference was unreachable. Exact DR6 merging of a deferred watchpoint into ICEBP remains unasserted:
Intel specifies that INT1 itself sets no DR6 bits, but the reviewed wording does not settle coincident deferred
causes. This fix establishes removal of the stale second delivery, not complete debug-register conformance.

## Subtests hidden behind earlier stops (November 7 UTC)

The owner requested execution of cases skipped after machine stalls or previous failures. Fresh source inspection
found **22 later subtests without execution evidence**: seven MOV-SS cases after ICEBP and fifteen sigreturn calls
after the first compatibility transition. The no-stop reruns had contained the latter failure but still terminated
the process before its later assertions. `single_step_syscall` and `syscall_arg_fault` already completed their five
OKs on no-stop. `sysret_ss_attrs` has no independent tail: its sole assertion requires a compatibility-mode access
on every iteration.

Evidence is retained in `natemu-raw/correctness-blocked-20261107-01/x86tail-v2/`. The build is the retained final
unmerged no-stop state (`129784c` plus twelve dirty files), with copied signed binaries and explicit series69 compiler;
the parent manifest, source archive/patch and hashes identify it separately from the older baseline.

The original Debian source archive was unavailable locally. Sources were recovered from upstream **v6.12.107**, with
URLs and hashes retained; no byte-for-byte comparison against that original archive is claimed. The original build
record states Debian's patches do not touch tools/testing. Selection gates change only which main-body cases execute;
original helpers, handlers, assertions and instruction sequences remain. A named MOV-SS/NOP sequence lives in one
`noinline,noclone` helper to prevent GCC from duplicating its assembly label. The rejected first wrapper and its seven
compile failures remain in `x86tail/`; those were setup failures, not executed MOV-SS tests.

Each missing subtest ran in its own natemu boot at N=1 and N=4. All **44 boots** reached CT-END, without caps,
recorded contention or compilation failures. The same two guest-built executables have identical SHA-256s at
N=1, N=4 and QEMU N=4 (`6cf459eb…e6da6b` MOV-SS; `7d31e18b…90c092` sigreturn). The independently reviewed checker
verifies exact selections/RUN lines, source and binary hashes, result counts and the recorded DR0/DR1/DR7 watchpoint
setup; it reports zero evidence-integrity errors.

| Selected subtests | N=1 | N=4 |
| --- | --- | --- |
| MOV SS followed by CLI, page fault, INT 1, SYSCALL, breakpointed NOP, SYSENTER, INT 0x80 | 7 pass | 7 pass |
| sigreturn: 64-bit CS/16-bit SS; eight invalid-descriptor cases; non-strict SS restoration | 10 pass | 10 pass |
| sigreturn: valid 16-bit CS/32-bit SS; valid 32/16-bit CS with 16-bit SS; two corrupted-final-SS cases requiring 32-bit execution | 5 exit 139 | 5 exit 139 |

The ten SIGSEGV outcomes are **failed tests, not skips or passes**. Their selected paths require valid 16/32-bit
execution and are consistent with no-stop's documented containment of unsupported compatibility transitions; no
new runtime fault trace was captured. The important recovered coverage is that the other ten sigreturn calls do
not disappear behind that deviation. Passing assertions do not establish identical fault behavior: `bad32-code`
and `bad16-code` report #GP(0x20)/#GP(0x34) in natemu, versus QEMU's #GP(0x30). The upstream bad-IRET helper accepts
any trapped signal. The differing selectors are consistent with no-stop rejecting the compatibility CS before
validating SS (`execution/segments.rs`'s `refuse_compatibility` check, used by `far_return`), rather than full architectural agreement.
The MOV-SS cases are upstream survival checks; their passing result does not
establish every detail of debug-trap ordering. ICEBP itself was already executed and failed in the older evidence;
this continuation bypasses it to expose its tails and does not resolve or freshly retest its defect.

QEMU runs all 28 isolates, including two already-reached controls and four conditional GDT cases: **23 pass, one
fails** (MOV SS/SYSCALL, SIGSEGV, matching its earlier watchpoint-delivery problem), **four are unavailable** because
the kernel refuses the required GDT descriptors. Zero RUN lines are explicitly classified as unavailable, even if
the child returns zero. The first reference pilot also observed an extra RF bit (`0x246` versus `0x10246`) in
`corrupt32-ss16`; the corrected-wrapper reference passes that case with the identical sigreturn binary. This is an
intermittent reference observation, not evidence of a natemu defect. The original native full-run results remain
the hardware reference; no new remote native run was possible during this continuation.
