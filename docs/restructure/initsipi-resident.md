# INIT and SIPI in the resident (boot-interpreter plan B2)

Status: built, November 22, 2026, on `boot-interp` cdff642c (ac111723 before the branch's rebase onto main; with
`natemu_abi::power_on`, `--no-mailbox`, Multiboot, the program driver's machine sections). The interpreter is merged and
runs a SIPI's start: the stub climbs to 64-bit code ([the interpreter's transitions](interp-transitions.md)); the entry
regime of the cases below still stops at the interpreter's entry on purpose (`NATEMU_INTERPRETER=off`). The spec is
natemu's protocol in `tools/models/initsipi_model.py`; plan sections 6 and 9c.

## The pending word

`Events::nmi` becomes `Events::pending`: the same word at the same offset, `Events` unchanged in size, `State` not
grown (nothing needs the word the plan allows).

| Bits | Name | Set by | Cleared by |
| --- | --- | --- | --- |
| 0 | N, an NMI pending (today's value 1) | senders, the host's NMI input | the target taking it; an INIT |
| 1 | I, an INIT pending | senders | the target taking it |
| 2 | W, in wait-for-SIPI | the target taking INIT; the host before an AP first runs | the target's start |
| 3 | S, a SIPI latched | senders | the target's start; an INIT |
| 15:8 | the latched vector | with S | with S |

Every update is one atomic read-modify-write (`fetch_update`); the rules are pure functions in `natemu_abi::lapic`
(unit-tested against the model's rules, and that every reachable word keeps J1 and J2: S only with I or W, never I and
W, never W on the BSP):

- send INIT: with W, clear S, N and the vector; else set I and clear them. Send SIPI: with I or W and no S, set S and
  the vector; else nothing. The post (`posts` + 1, doorbell) only when the word changed. Send NMI: `fetch_or` N (was a
  store of 1, which would now clear I, W, S), posted as today; the host's NMI post likewise.
- take INIT: an AP clears I and sets W; the BSP (IA32_APIC_BASE.BSP) clears I and S. Start: with S, clear S, W and the
  vector, by compare-and-swap (an INIT may clear S meanwhile). Take NMI: clear N only while I is clear (was a store of 0).

`events::next` loads the word once: zero goes straight to the interrupt part (one `ldar` and one branch, today's
`nmi_blocked` load no longer first); nonzero: I gives `Next::Init` before NMI, shadows and IF (INIT is not masked by
them), else N with NMIs unblocked gives `Next::Nmi`. So `events::none` stays one load, and `sys_commit`'s, `miss_fast`'s
and `sys_fast`'s tests are unchanged in source (phase 2 shows the disassembly). `events::held` is unchanged (an I
reads as pending: a conservative exit of an inline STI).

## Sending (`lapic.rs` `send`)

INIT (assert) and start-up replace `ap_start`'s stop: the targets from the existing shorthands and destination match
(self, all, all-but-self, physical, logical), those with the APIC hardware-enabled (as for NMI), each given its update.
A shorthand or destination selecting no CPU is a no-op (one vCPU, an absent APIC ID). INIT level de-assert stays
without effect; SMI and the reserved modes still stop. INIT to self or all includes the sender, as QEMU, KVM and the
model do (the SDM calls those shorthands invalid for INIT).

## Taking INIT: `startup::init` (new `execution/startup.rs`, cold, out of line)

1. The take (above).
2. `power_on::init(&mut s.cpu)`: Table 12-1's INIT column (CD/NW, x87, SSE, XCR0, MSRs other than EFER and the FS/GS
   bases kept).
3. The resident's own: `nmi_blocked`, `sti_shadow`, `mid_string`, `shadow_entry`, `plain_step` 0, pending traps dropped
   from `debug`, `mailbox` 0, `Events::halted` 0.
4. The local APIC (SDM 13.4.7.3): the software-disabled word published first, then the record reset to `RESET` (keeping
   the measured frequency and the reinject counters), the IRR and TMR words cleared, the timer off; IA32_APIC_BASE, the
   ID and x2APIC mode kept. A fixed send that read SW_ENABLED before the publish may still set an IRR bit (model A2): it
   is not delivered until software enables the APIC; nothing asserts IRR = 0.
5. `tlb(s, true)`: every shadow, every L1 entry and every link into a block that is no longer current go (the global
   flush a paging-control change makes).
6. An AP waits for its SIPI (below); the BSP is left at the reset vector.

Where it is taken:

- **The boundary prologue** (`find_from`): `events::take` on `Next::Init` calls it and sets `debug::INTERPRET`, so the
  loop's existing trap test enters the interpreter on its next pass (`interpret::boundary`): no new test on the hot
  path. `trap_due` (cold) leaves a pending trap-class #DB to a pending INIT (Table 7-2: class 3 before 4).
- **Translated code** and **a host request:** the sender's doorbell stops the block at a boundary, or is pending at the
  next block's first word; either reaches the prologue.
- **HLT:** `park`'s look is `next`, so I wakes it (its recheck after `end` too); the HLT returns to the prologue. A SIPI
  changes nothing there (S needs I or W), so it neither posts nor wakes.
- **The mailbox wait:** looks at I after reading `posts`, before publishing `halted`, and again after reading `end`; on
  I it leaves (`start()` then runs `init`).
- **The interpreter loop:** `events::interpreted(s) -> Option<Undelivered>` (its contract in its doc), which
  `interpret::events` calls at each interpreted boundary, after the debug state's look (an INIT pending there comes
  first even with TF or a breakpoint armed, Table 7-2): it takes INIT, then looks at the new state's boundary again, and
  returns an NMI, an interrupt with IF = 1 or an ExtINT, left pending, for the interpreter's typed stop (`None`: go on).
  Interpreted HLT waits in `park` (INIT wakes it, an NMI ends it for the typed stop, a SIPI does not).

## The wait for a SIPI (`startup::wait`, a `wfi` loop like `wait_mailbox`)

Each pass: `doorbells`, `io_checkpoint`, `take_invalidations` (it keeps serving peers' shadow requests); `seen` =
`posts`; if S: `halted` = 0 first (the host never sees an idle vCPU that is starting), then the start CAS (on failure the
pass repeats); else `halted` = 1 | seen << 8; if `end` is set, S once more, then `halt(stop::WAIT_FOR_SIPI)`; `wfi`. The
start: `power_on::sipi(&mut s.cpu, vector)` (CS VV00H, base VV000H, IP 0), the record written, and the interpreter is
entered. A held NMI stays in N: the first event after the start. `start()` enters the wait when the host published W.

## The end rule and the report

`execution::idle` is unchanged: the wait publishes `halted` state 1 with every post seen, as the mailbox wait does. A
vCPU ended there stops with the new `stop::WAIT_FOR_SIPI` ("wait_for_sipi"), which the program driver's `finished`
counts as the vCPU's own end and its report names; the Linux report's per-vCPU entry gains `waiting` (`sipi`, `halted`,
`mailbox`, from the event record at the end), so a parked AP is never reported halted.

## Start-up

`execution::wait_for_sipi(memory, v)` (host) publishes W before any vCPU runs, so a SIPI sent before an AP's first entry
is latched. Linux's `--no-mailbox` APs and Multiboot's (the INIT state, from the power-on branch) use it. The program
driver gains a `cpu` item `wait-for-sipi` (the power-on state, the APIC at `RESET`, W published, no code; what
`exit_prog.asm`'s contract asks for) and a test option `--entry-ends`: a stop at the interpreter's entry (today's
`unsupported_mode` in an interpretable state) is that vCPU's own end, so every AP's SIPI state is reported and the
others run on.

## Cases

- abi unit tests: the update rules; the reachable words.
- `tests/system/run_initsipi.py`, cases `initsipi` and `initsipi-1` (blocks of one instruction): one description per
  scenario: the BSP's 64-bit program (x2APIC ICR writes, ops, awaits with a TSC bound, an idle HLT); the APs in the
  scenario's context (the wait item, translated code or HLT, the mailbox, a host request); at each vector a stub of
  first-version instructions shaped as the model's (it climbs to 64-bit, signals on a shared counter, idles). The judge
  (`tests/system/initsipi_expected.py`) projects both sides (per CPU: how it ended and the vectors it started at; or the
  typed stop) and requires the observation in the projection of `outcomes(name)`; while the interpreter is the stub, of
  the outcomes of the scenario with each interpreted program cut to one instruction and the awaits dropped (each AP is
  done at its entry). The SIPI state is also checked field by field. Scenarios: `linux`, `kvm-unit-tests` (all-but-self
  back to back), `edk2` and `edk2-back-to-back` (rounds), `init-sipi-init`, `sipi-then-init`, `sipi-before-init-taken`,
  `sipi-during-host-request`, `init-in-mailbox`, `init-in-translated-hlt`, `nmi-in-wfs`, `nmi-held`,
  `nmi-init-in-wfs`, `park-with-init`, `never-started`, `bsp-init`, `fixed-in-wfs`, and two added to the model:
  `sipi-sipi` (two vectors: the second ignored) and `one-cpu` (INIT and SIPI to all-but-self at one vCPU). Not run (they
  need boot programs that start in interpreted code, which the runner's descriptions cannot state beside a translated
  BSP, or a translated NMI or interrupt delivery, which the driver's logging IDT ends): `hlt-init-not-sipi`,
  `nmi-in-interpreted-hlt`, `init-in-interpreted-code`, `nmi-around-init`, `fixed-around-init`; interpreted HLT woken by
  an INIT and by an NMI is `tests/interp` case `natemu-machine`'s (`r-hlt-init`, `p32-hlt-nmi`), and so is
  `hlt-init-not-sipi` on two CPUs (`smp-hlt-sipi-init`: a machine description; CPU 1 halts in real mode with IF = 0,
  the BSP in P32 sends it a SIPI, which leaves the HLT as it is, then an INIT, which ends it and is taken before the
  next instruction, then a SIPI, which starts it at vector 0AH; each wait bounded and its success recorded; shown to
  fail with a SIPI that wakes the HLT and with an INIT that does not).
  Case `initsipi-interpreter` (the interpreter regime): every scenario but `bsp-init`, the interpreter running each AP's
  stub from its SIPI to 64-bit code (a start counts at its arrival); `fixed-in-wfs`'s own start program (the climb, the
  APIC enabled, STI, an idle HLT) is the stub with a jump the BSP patches at its 64-bit code.
  The judge reads each CPU's pending word as its own run ended (the driver's `startup.pending`): an entry stop with an
  NMI pending is the model's NMI stop there, one with an INIT pending is open (the interpreter's first boundary would
  take it).
- `x3-cases`: `ipi-init`, `ipi-startup`, `ipi-init-xapic` (to APIC ID 1 at one vCPU) now reach their end with no effect
  (they stopped `unsupported_system`); `ipi-startup-self` (discarded), `ipi-init-self` (the BSP's reset state at the
  interpreter's entry) are new.
- `exit-ap-natemu` (tests/boot, `exit_prog.asm` with the wait item): the pass at 1, 2 and 4 vCPUs (every AP climbs in the
  interpreter and arrives once, counted from 64-bit code).
- `boot-startup` (tests/boot, `run_boot_smoke.py --only startup`, B2's Debian slice): Debian's 6.12.107 kernel and
  installer initrd through `boot-linux` at 2 and 4 vCPUs, with the mailbox and with `--no-mailbox`, a busybox script on
  serial. Every CPU is online at boot. Without the mailbox each AP goes offline (`play_dead`: a translated HLT with
  IF = 0, natemu's profile having no MWAIT) and online again (an INIT that ends that HLT, the SIPIs, the trampoline in the interpreter); with it Linux
  refuses offlining (a version-0 mailbox cannot start a CPU twice). Each AP start takes Linux's warm-reset path, with
  the mailbox too: CMOS 0FH := 0AH and the vector at 467H, restored once the AP is up (counted by the pr_debug `Setting
  warm reset code and vector.`, turned on with `dyndbg`; port 71H's writes: 4 with the mailbox at 2 and 4 vCPUs, 6 and
  10 without, two per extra setup and restore). Both are benign here: natemu's CMOS keeps 0FH as a plain byte, as
  QEMU's does, and 467H is guest RAM. Shutdown (`poweroff -f`): Linux 6.12 parks no CPU with INIT; its
  `native_stop_other_cpus` sends the reboot vector and each AP stays in `stop_this_cpu`'s HLT (IF = 0), reported
  `halted`; the run ends at S5. No typed stop. With `NATEMU_INTERPRETER=off` both `--no-mailbox` boots fail (vCPU 1
  stops at the interpreter's entry) and the mailbox boots pass.

## Files

`crates/abi`: `execution.rs` (`Events::pending`, its bits, `stop::WAIT_FOR_SIPI`), `lapic.rs` (rules, tests).
`crates/runtime`: `execution/lapic.rs` (send, the APIC's INIT reset), `execution/events.rs` (`next`, `take`, the
waits, the hook), `execution/startup.rs` (new), `execution.rs` (`start`, `trap_due`). `crates/host`: `execution.rs` (NMI post, `wait_for_sipi`, `waiting`), `linux.rs`. `crates/driver`:
`program.rs`, `linux.rs`. `tests/system` (runner, judge, its unit tests, `family.toml`, `run_x3.py`), `tests/boot`,
`tools/models/initsipi_model.py` (the two scenarios).

## Risks for a running 64-bit OS

- `next` runs at every boundary and in `sys_commit`: still one load of the word; the common path loses one load. Checked
  by the disassembly (below, an experiment) and the hackbench cases at e21b0209 ([benchmarks](../benchmarks.md); this
  item's ids are the commits before the branch's rebase onto main, tag `boot-interp-prerebase`). B2 had no gate run of
  its own: its author ran the CPU start-up cases (`initsipi`, `initsipi-1`, `x3-cases`, `exit-ap-natemu`); after the
  interpreter's merge the system, smp, interp and unit families ran at e21b0209 (M2 Max), and the full gate on the final
  tip ([the plan](m2-boot-interpreter-plan.md), section 12).
- NMI post and take become read-modify-writes: the NMI path only.
- INIT and SIPI code runs only for a guest that sends them; Debian with the mailbox never does (today they stop the
  VM). No new test on the hot path; the mailbox wait gains two loads per pass (APs before their wakeup only).
- `State` and `Events` keep their layout; the end rule is unchanged, and wait-for-SIPI vCPUs exist only on the new
  boot paths and in the new cases.

## The hot path, before and after (resident release build, `llvm-objdump`; ac111723 against this change)

Experiment (not a framework case; M2 Max; ac111723 is the commit before the rebase onto main, tag
`boot-interp-prerebase`).
`events::none` is not a function of its own: it is a call of the out-of-line `events::next` and a test of its `None`
(-1). Its call sites, `miss_fast`'s own test and the two of `sys_commit` (before the commit and after the transaction),
which is inlined into both `miss_fast` and `sys_fast`, are, like the rest of both functions, the same instructions before
and after (the diff differs only in the symbols' hash suffixes):

    mov   x0, x19
    bl    events::next
    cmn   x0, #0x1            ; None?
    b.ne  <declined>          ; (b.eq in miss_fast's first site)

`events::held` is the same instructions too. `events::next`'s common path (no event, RFLAGS.IF = 0, NMIs not blocked)
was, after the frame set-up, `ldr x8, [x0, #0x4f8]` (`nmi_blocked`), `cbz`, the event record's address from
`State::vcpu` (`ldr`, `lsl`, `sub`, `mov`, two `movk`, `add`, `sub`), `ldar x20` (the NMI word), `ldrb` (IF),
`tbnz`, `cbnz`, `mov x0, #-1`; it is now the address (`ldr`, `lsl`, `sub`, `mov`, two `movk`, `add`), `ldar x10`
(the pending word), `cbz x10` (zero: no NMI, no INIT), `mov w20, wzr`, `b`, `ldrb` (IF), `tbnz`, `tbz`, `mov x0,
#-1`: 22 instructions in both counting the frame, one load fewer. A nonzero word is tested for INIT (`tbnz w10, #1`:
`Next::Init`) and then NMI (`tbnz w10, #0`, then `nmi_blocked`), so an NMI is recognised exactly when it was before:
its bit set and NMIs not blocked. The NMI bit is cleared only where an NMI is taken (`take`, only after `next` saw it
unblocked) and by an INIT (which discards pending events); while NMIs are blocked it stays set and nothing wakes a HLT
for it, and the boundary after the IRET that unblocks them takes it, as before.
