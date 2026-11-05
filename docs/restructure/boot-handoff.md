# Boot integration handoff

Status: November 2, 2026, end of the boot-features phase (chunk R2 of the
[boot-features plan](boot-features-plan.md), "the plan" below; its §4 checklist says what is met). Emulator code at
e6b2bf5 (the fix of the blocking interrupt defect found at R2, §4) plus the fixes of the independent phase-end review,
uncommitted on e6b2bf5 (plan, Built (R2 review fixes)); the
compiler is fexc **series48-lean** (November 3, perf plan "Measured (chunk 2)": series47-seg with the compile server sending
only what the host reads; objects, extent and args_read byte-identical; series47-seg is series46-br66's patches with the owner's segment-prefix decisions in the shim
and `fexc.cpp`: CS/DS/ES/SS prefixes ignored, the last FS/GS prefix applies, FS/GS with no memory operand ignored; plan §5
and Built (segment prefixes, November 3); series46-br66 is series45-r2fix plus patch 0029 and its shim change: 66 on near
direct branches ignored as on Intel; series45-r2fix is series44-r2, byte-identical to the hand-built series43-x1, plus the
review's shim change: segment prefixes on near direct branches ignored; §6), built by `tools/fexc/build.sh`. Since perf
chunk 4 (uncommitted on a24c785) the host reuses a translation at every RIP of the same half and page offset by its
relocation records (`crates/host/src/relocation.rs`; perf plan "Measured (chunk 4)"): installer N = 1 cache off 27.7 s.
Since perf chunk 5 (uncommitted on d89bc4c) the resident keeps four dispatcher L1 tables, one per code context, and a CPL
change switches tables instead of clearing L1 (`crates/runtime/src/cache.rs` `select_l1`; perf plan "Measured (chunk 5)"):
installer N = 1 cache off 17.4 s, N = 4 15.6 s. Since perf chunk 7 (uncommitted on 0da552a) the host's per-request work
is leaner: the watchdog samples an entry-time word instead of being woken by two channel sends per exit, the block RIP set
is kept only for the program driver, guest bytes are copied a word at a time, and the memory cache is looked up in one pass
under a universal hash (perf plan "Measured (chunk 7)"): installer N = 1 cache off 15.9 s, N = 4 13.6 s. Since perf
chunk 8 (uncommitted on 1c4d8b9) fewer block links are undone at TLB events: a block placed from global pages is global at
once, a link within one page and frame (`cache::local`) stays across CR3 loads, global flushes and INVLPGs, and INVLPG (and
a #PF) reaches only its 4 KiB page and blocks from a 2 MiB leaf around it (perf plan "Measured (chunk 8)"): installer N = 1
cache off 13.6 s, N = 4 12.4 s (15.6 and 13.4 s before, in the same rounds). Since perf chunk 9 (uncommitted on 9ba60d5)
a resident entry copies less: the state passes by reference to `dispatch`, `enter` and `step`, records are copied 32 bytes a
step, the entry frame is built in place in `Shared::initial` (its unwritten rest stays zero; the host checks it at every
resident stop) and the linker searches the block index once (perf plan "Measured (chunk 9)"): installer N = 1 cache off
10.8 s, N = 4 11.0 s (13.1 and 12.5 s before, in the same rounds). Since perf chunk 10 (uncommitted on 851cbd2) the resident
places most blocks the host would only relocate itself: a miss whose frame, page offset and context a live block of this
vCPU holds at another RIP copies that block and rewrites its RIP-dependent words by the host relocator's rules
(`natemu_abi::execution::relocate_code`, a hint table by frame in the lookup area, `execution::reused`), refusing to the host
whatever it cannot prove; `--check-reuse` (and the suites' `--code-shape`) asks the host for every copy too and stops
`reuse_mismatch` on any difference (perf plan "Measured (chunk 10)"): installer N = 1 cache off 8.6 s, N = 4 9.4 s (10.7
and 11.0 s before, in the same rounds), 854 k of 1.16 M host requests avoided at N = 1.
Since perf chunk 15 (uncommitted on e2f8aba, fexc series54-inline-tsc) a CPL-0 translation runs the one-byte CLI and STI
and the two-byte RDTSC inline instead of a resident round trip each: IF lives in its guest-state byte (banked at every
exit), an inline STI reads the resident's event-pending byte and, with an event pending, exits to its own RIP so the
resident's STI (shadow, delivery) runs; STI is never first or last in its block; RDTSC computes the resident's TSC from
the virtual count, which EL0 may now read (CNTKCTL_EL1.EL0VCTEN only; perf plan "Measured (chunk 15)").
Since perf chunk 17 (uncommitted on 114c9eb, fexc series55-swapgs) a CPL-0 translation also runs the plain SWAPGS inline: it
exchanges the guest-state page's GS base with IA32_KERNEL_GS_BASE, which the resident writes there at every CPL-0 entry (0
at CPL 3) and banks back, with the GS base, after CPL-0 code; Linux's system call then takes two resident trips (SYSCALL,
SYSRET) instead of two trips with six system instructions (perf plan "Measured (chunk 17)").
Performance batch 1 ended at 4ed8e3a (November 3): the whole standing suite green at both RAM sizes (§5 "Full suite at the end of performance batch 1"), the §4 comparison rerun with the stub: installer N = 1 and 4 cache off 8.85 and 9.39 s, warm 6.89 and 5.80 s, against QEMU `thread=multi` 6.10 and 4.81 s.
Perf chunk 6 (uncommitted on ea11225) measured the whole-cache flushes and left the cache as it is: only 123 k requests per
installer boot (N = 1) refill blocks placed before a flush, and a record arena and index sized for the code area were no
faster (+261 MiB); the performance log gains four request-line fields, and up to 870 k requests at N = 1 could reuse a block
of the same physical page inside the resident (perf plan "Measured (chunk 6)").
The integrity audit of November 3 (uncommitted on 1c4d8b9) tightened the ASM runner (unsupported only for a documented
refusal), made SSE4a EXTRQ/INSERTQ #UD and F2/F3 on SMSW ignored, added `boot-linux --code-shape`, listed the reserved forms
refused (§4), and replaced the QEMU comparison by a like-for-like one (§4 boots, §5 "Rerun with the integrity-audit fixes").
This page is enough to start the boot integration: how to build and run a boot, what a boot takes as input, what is
known to cost or differ, and the standing suite. The plan's "Built (...)" paragraphs hold the detail and the evidence;
the [contract inventory](contracts.md) maps each contract to its check. No performance tuning was done beyond the
owner's K1 and P3 decisions.

## 1. Boot commands

**Build and sign** (from the repository root; Rust 1.97.0, offline, locked):

```sh
cargo +1.97.0 build --offline --locked --release -p natemu --bin natemu      # the stub alone: no `testing` feature
codesign --force --sign - --entitlements crates/host/entitlements.plist target/release/natemu
cargo +1.97.0 build --offline --locked --release -p natemu-runtime --target aarch64-unknown-none
tools/fexc/build.sh ../natemu-raw/fexc-$(date +%Y%m%d)-01                  # a fresh directory: it must not exist (about 30 s)
```

series48-lean already exists, so `build.sh` refuses it: build into a new directory and pass its `obj/fexc` (§6). Build the stub with `-p natemu` alone: a whole-workspace build unifies the driver's `testing` feature (raw record and
code-cache writes, the hook's reply/object/compile-server calls) into it (plan, Built (L0), review round). The runners
build and sign `natemu-host` themselves.

**Boot stub** (`crates/host/src/main.rs`):

```sh
R=target/aarch64-unknown-none/release/natemu-runtime
F=../natemu-raw/fexc-native-20261026-01/series61-chunk25-r1/obj/fexc
K=../natemu-raw/kernel-census-20261028-01/dl/linux               # Debian 6.12.107 bzImage
I=../natemu-raw/kernel-census-20261028-01/dl/initrd.gz           # Debian installer initrd
target/release/natemu "$R" "$F" "$K" --cmdline 'console=ttyS0 earlyprintk=serial mitigations=off nokaslr' --vcpus 4
target/release/natemu "$R" "$F" "$K" --initrd "$I" --cmdline 'console=ttyS0 earlyprintk=serial mitigations=off nokaslr'
```

The initrd boot hung after "Run /init" at 16b0576 (device interrupts through a dangling pointer); fixed after R2 (§4,
"Blocking defect found at R2 (resolved)").

`natemu RUNTIME FEXC BZIMAGE [--initrd FILE] [--cmdline TEXT] [--ram BYTES] [--vcpus N] [--serial FILE]`: COM1's output
to standard output (or `--serial FILE`) as it is sent; the stop of the vCPU that ended the run on standard error; exit 0
when the guest powered off (ACPI S5), 2 at any other stop (reset, a typed stop, a watchdog, `Cancelled`), 1 on a host
error (a failed serial write included). SIGINT or SIGTERM cancels the run (the library's canceller below; `Cancelled`,
exit 2); a second one exits 130 at once. It opts into the persistent compile cache from the environment (§2).

**Library entry** (`crates/host/src/linux.rs`, package `natemu`): `natemu::linux::Boot` (every manifest field below,
defaults from `Boot::new(runtime, fexc, kernel)`), `Boot::prepare()` (guest RAM, loader, ACPI tables, each vCPU's state,
LAPIC and mailbox, COM1, I/O APIC and 8259 pair, PIT/RTC/PM) returning `Prepared`, `Prepared::run()` (creates the VM,
one thread per vCPU, the first vCPU whose run ends ends the run, the others are cancelled; joined and destroyed before
it returns; once only; at its end, before the VM is destroyed, the chipset drops the VM's doorbells and the PIT/RTC/PM
threads stop) returning `Outcome` (each vCPU's `Stop` and compile count, `ended_first`). `Prepared::canceller()` gives a
`Canceller` (`Clone + Send + Sync`) whose `cancel()` stops the run from any thread: each vCPU stops at its next host exit
(running ones are forced out) and `run` returns with `Cancelled` stops; before `run`, it ends at once. `Prepared` also
gives the loader's placement, the devices, the count of serial bytes sent (`serial_bytes`) and a failed serial write
(`serial_error`). Guest RAM by value (`Prepared::guest`, `Backing::ram`) is `unsafe`: a `GuestRam` is a `Copy` view of
the backing's mapping and must not outlive it; `vm::Machine::create` takes the backing's `Arc<Memory>` and keeps it
mapped while the VM exists. `Boot::run()` is both. The compile cache is off unless the caller sets
`compile_cache::Config::from_env()?`. No hook, fixture or tamper code is in the library (plan, Built (L0)).

**Test driver boot** (`crates/driver/src/drivers/linux.rs`): `natemu-host RUNTIME boot-linux FEXC BZIMAGE` with the
stub's options plus `--rsdp`, `--code-cache`, `--watchdog-ms` (default 60,000), `--max-block`, `--stop-at`,
`--load-only`, `--dump`, `--save`, `--strict-unassigned`, `--lost-ticks`, `--clock-trace`, `--check-reuse` (perf chunk 4,
debug: every translation reused at another RIP is also compiled there and must be byte-equal, else a host error; the
program driver's `--code-shape` turns the same check on), `--code-shape` (debug, off by default: admission's code-shape
check on every object, as the suites run it; here without `--check-reuse`, which it can be combined with; the library's
`Boot::code_shape`, not in the stub; one installer boot passed it, §5 "Rerun with the integrity-audit fixes"); prints one JSON report (the
placement, the BSP's stop, state and stats, each vCPU's stop and counters). Same `Boot` underneath. SIGINT or SIGTERM
cancels its run as the stub's does (the report is still printed; a second one exits 130; both use
`Canceller::on_signals`).

**Performance log** (perf plan §2, "Measured (chunk 1)"; off by default): `NATEMU_PERF_LOG=FILE` on `natemu-host
boot-linux` or the `natemu` stub turns on the library's timing log (`crates/host/src/perf.rs`, `natemu::perf::enable`
for an embedder): FILE gets one `mark NAME NS` line per process phase (both entries: `main`, `run`, `disk_open`,
`server_startN`, `first_entryN`, `cancel`, `disk_written`, `destroyed`; `boot-linux` only: `prepared`, `report`, `exit`) and one `vcpu {JSON}` line per
vCPU at its end (host time by loop part and exit kind, the translation-request parts, thread CPU, its fexc server's CPU
from `wait4`, the resident's buckets, K1 statistics, counters); FILE.vN one line per translation request of vCPU N (CR3,
RIP, guest-physical page, result, answer source, bytes read and their hash, nanoseconds per part), at most 1 GiB per
file (later lines counted as `dropped`); failed writes are counted, the first reported on standard error, and both
appear in each vCPU's line (`"log"`) and as a warning at its end. A resident built with `perf` but run with the log off
draws a warning after the run. Since perf chunk 5 the K1 statistics carry `l1_switches` (code-context changes) and `l1_clears` (L1 tables cleared for a context that had none), and the resident's `l1` bucket times the switches; since perf chunk 8 `reclaims` (the link records found full: how often, and the local links into blocks not current then undone); since perf chunk 9 the resident's `enter_copy` bucket holds the copies inside `enter` (25 buckets); since perf chunk 10 `reuse` (copies made in the resident, refusals by kind, checks; also per vCPU in the `boot-linux` report with its `requests`) and the resident's `reuse` bucket (26 buckets). Since perf chunk 7 the vCPU line's parts carry `relocate` (an answer kept for another RIP relocated) and `admit` is admission alone; the request line keeps its nine parts. Since perf chunk 6 the request line ends with four more fields: the resident's flush generation when it asked, the code context (`Cpu::code_context`, hex), 1 if the block could be an in-VM reuse template (not a step, no address or halving cut, its answer read neither to its bytes' end nor beyond the block's pages, not `unsupported_form`) else 0, and the fetch's second guest-physical page (`ffffffffffffffff`: none); `natemu-raw/perf6-20261103-02/refill6.py` classifies refills and reuse candidates from them. Since the compile-ahead gate the request line ends with five more: the request's start (ns), 1 for a step request else 0, the admitted block's guest bytes (0: none), 1 if its last instruction is a call (e8 or ff /2) else 0, and its exit candidates `target:F` (the link targets and a call's return site; F = 1 when the target lies on a page the request fetched), comma-separated, or `-`; computed only with the log on; `natemu-raw/ahead-20261104-01/ahead.py` reads them. Times are
`CLOCK_UPTIME_RAW` nanoseconds (= `mach_absolute_time` = the guest's CNTVCT). The resident's buckets need the runtime
built with its `perf` feature, into its own target directory so the default build stays as it is:
`CARGO_TARGET_DIR=target/perf cargo +1.97.0 build --offline --locked --release -p natemu-runtime --target
aarch64-unknown-none --features perf` (without it they read zero, `"magic":false`); a runner that builds its own
resident gets it with `CARGO_TARGET_DIR=target/perf` and `CARGO_TARGET_AARCH64_UNKNOWN_NONE_RUSTFLAGS` set to
`.cargo/config.toml`'s flags plus `--cfg feature="perf"` (not `RUSTFLAGS`: it replaces the linker script flag). The default runtime's `.text` is
byte-identical with and without this code (only panic-location line numbers in `.rodata` move). The harness and
analysis (`boot.py`, `analyze.py`, `aggregate.py`, `campaign.sh`) are in `natemu-raw/perf1-20261103-01/`.

## 2. Manifest inputs

| Input | Field / option | Default and limits |
| --- | --- | --- |
| Kernel | `Boot::kernel`, BZIMAGE | a bzImage of the 64-bit boot protocol (2.12+), entered at `startup_64` (+0x200) in long mode, LA57 = 0, flat CS 0x10/DS 0x18, IF = 0, RSI = boot_params; checked against the independent `tools/models/bootproto.py` (`run_l1.py`); loader errors are typed codes (`BAD_LAYOUT`, `KERNEL_DOES_NOT_FIT`, `INITRD_DOES_NOT_FIT`, ...; `crates/host/src/boot.rs`) |
| Initrd | `initrd`, `--initrd` | none; placed by the loader, `ramdisk_image/size` and the ext fields set |
| Command line | `cmdline`, `--cmdline` | empty; the user's choice (the evidence boots use `console=ttyS0 earlyprintk=serial mitigations=off nokaslr`, owner decision October 28: mitigations off) |
| Guest RAM | `ram`, `--ram` | 2 GiB; a 2 MiB multiple above 16 MiB (the map's RAM start) up to 3 GiB (`execution::MAX_GUEST_RAM_BYTES`); no RAM above 4 GiB |
| vCPUs | `vcpus`, `--vcpus` | 1; 1-4 (`cache::MAX_VCPUS`); vCPU 0 is the BSP, the others wait on the ACPI wakeup mailbox (X4) |
| Memory map | built from `ram` (`boot::Layout::e820`) | e820: reserved page 0, RAM to 0xA0000, the legacy hole (unassigned: all-ones reads), loader page tables at 1 MiB (reserved to 0x200000), reserved 0x200000-0x600000 (the normal map's hole), ACPI NVS 0x600000-0x1000000 (tables, mailbox, AP boot scratch), RAM 16 MiB to R, reserved R to 4 GiB (the device range: I/O APIC, LAPIC) |
| Firmware tables | `rsdp` (default 0x600000) | ACPI 6.5 RSDP, XSDT, FADT, FACS, DSDT, MADT with the version-0 wakeup mailbox for N processors (`crates/host/src/boot/acpi.rs`; no `_CST`/P_BLK, HPET, MCFG or EFI); `acpi_rsdp_addr` in boot_params. MTRRs as the virtual firmware leaves them: all RAM WB, the hole and [R, 4 GiB) UC (plan, Built (M2 addendum 1)) |
| CPU profile | built in (`crates/abi/src/cpu_profile.rs` from `tools/models/cpu_profile.json`) | the accepted Intel Haswell-class profile (INV-03-01 CLM-03-01-16/-17): SSE to SSE4.2, AVX/AVX2/FMA/F16C, XSAVE (XCR0 7) with XSAVEOPT and XSAVEC (owner, November 3), MOVBE, AES-NI/PCLMULQDQ, SHA, ADX, CLFLUSHOPT/CLWB (owner, November 3), BMI1/2, RDRAND, x2APIC; no PCID, 1 GiB pages, LA57, SMAP, TSC-deadline, PMU, MPX/CET/AVX-512 |
| Compiler | `fexc`, FEXC | the path of a fexc binary; one `fexc --serve` child per vCPU; each answer is validated by admission |
| Compile cache | `compile_cache` (`Config::from_env`) | `NATEMU_COMPILE_CACHE=DIR|off` (default `~/Library/Caches/natemu/fexc`), `NATEMU_COMPILE_CACHE_MB` (default 512; not a whole number: an error, not the default); keyed by the fexc binary's SHA-256 and the exact request, so a rebuilt identical fexc reuses it; past 3/4 of the cap other builds' packs, then user, then kernel packs are evicted (`crates/host/src/compile_cache.rs`) |
| Code cache | `code_bytes`, `--code-cache` | 128 MiB less its head per vCPU (`CODE_BYTES_MAX`) |
| Serial | `serial`, `--serial` | the stub: standard output; the library: none (the UART keeps the bytes only without a sink; `Prepared::serial_bytes` counts them either way; a failed sink write is kept in `Prepared::serial_error` and the sink dropped) |
| Other | `strict_unassigned`, `lost_ticks`, `watchdog`, `max_block`, `stop_at` | off; PIT reinject, RTC and LAPIC discard (QEMU's defaults, `clock::LostTicks::DEFAULT`); 60 s; fexc's own; none |

## 3. What runs

x86-64 guest code runs as FEX-compiled ARM64 blocks at EL0 inside one Hypervisor.framework VM, under a resident runtime
at EL1 per vCPU that owns translation (four-level walks, A/D, shadows per x86 CPL class), the code cache, faults and
exception delivery, interrupts, and system instructions; the host compiles (fexc), decodes system instructions once per
translation, and runs the devices. Pieces: compiler `tools/fexc/` (shim, front end; the patch series 0001-0033 lives in the FEX fork's `natemu` branch, the pinned
submodule `third_party/FEX`); admission `crates/host/src/admission.rs`; host `crates/host/src/` (`vm.rs`, `memory.rs`,
`owners.rs`, `execution.rs`, `system.rs`, devices `uart.rs`, `ioapic.rs`, `pic.rs`, `pit.rs`, `rtc.rs`, `pm.rs`,
`clock.rs`, loader `boot.rs`, `boot/acpi.rs`); resident `crates/runtime/src/` (`execution.rs` and `execution/`:
delivery, events, segments, syscalls, lapic, mmio, ports, sse, x87, xsave, watch, helpers; `cache.rs`, `tables.rs`, `gates.S`); shared
layouts `crates/abi/src/`. Everything the boot path needs is built and checked: delivery of every exception and
interrupt through the IDT (X1a-X1c, X2), LAPIC timer and IPIs (X3), mailbox AP start (X4), descriptors, SYSCALL/SYSRET,
SWAPGS, MSRs, CPUID (S1-S3), TF and breakpoints (S4), SSE/AVX/x87/XSAVE (I5-I10), REP and LOCK forms (I2, I3), memory
types (M2), devices (M3, L1-L4b), translations kept across CR3 and INVLPG (K1), system instructions in the resident
(K2), in-process compiles and the compile cache (P3). The plan's §1 inventory maps every boot requirement to a
composite or a named check (plan §4, "R1 coverage").

## 4. Known limits and costs

**Address-space changes (K1).** A MOV to CR3 (any value) no longer drops code: blocks are filed by guest-physical
pages and revalidated by a fetch walk when no longer current; global blocks survive a CR3 load. It still resets the
vCPU's data shadows (the data TLB: no PCID is advertised and there is one shadow set per CPL class), so each CR3 load
costs its reset plus a shadow miss per page touched afterwards. Measured over the Debian boot to the root-fs panic: 18,557
CR3 loads and 38 global flushes in 81 ms (about 4.4 us each), 70,227 INVLPGs in 24 ms, about 456 k shadow-miss retries
(plan, Built (K1)). With PTI (`mitigations=off` not given) every kernel entry and exit is a CR3 load, so a
syscall-heavy guest pays a shadow reset each way; the evidence boots run with mitigations off (owner decision, October
28). CR0.PG/WP, CR4.PGE/PAE/SMEP and EFER.NXE changes and MTRR writes are global flushes (every block revalidated); the
only full code flushes left are a full pool and a host write to guest RAM (load time only).

**Uncached RAM** (M2 addendum 2, owner decision November 2): UC/UC- RAM is strongly ordered: each access runs its
instruction alone in the step block between two `dmb ish`, about 4.3-4.8 us per access against 7-11 ns on WB
(`run_m2.py cost`). Linux's `cache_disable` window costs about 533 such instructions at boot. WB, WT, WP and WC RAM run
natively. Device pages complete each MOV-family access once through the host (M3), about one host round trip each.

**x87 at reduced precision** (owner direction; plan, Built (I6) deviations 1-12, Built (I6b) deviations, Built (I5c)
deviations): every ST(i) is a double (53-bit significand and the double's exponent range whatever FCW.PC), so inexact
results, their comparisons and integer conversions, PE/OE/UE and C1's round-up bit may differ from an 80-bit x87;
FLD m80/FRSTOR round, FSTP m80/FNSAVE/FXSAVE widen exactly; FIP/FDP/FOP read 0; transcendentals are double libm results
computed by the resident. MMX aliasing of the x87 registers is not exact (plan, Built (I9) deviations 1-2).

**Declared deviations still in force:** the guest-state page `0xffff_4000_0000_1000` (one 4 KiB page per vCPU,
non-canonical: a guest data access there reaches the vCPU's own DF, FS/GS bases, MXCSR, x87, MMX and YMM upper halves
instead of #GP/#SS; needs no LA57 and no LAM; plan, Built (I1, G1 minimum), owner decision October 28); LOCK BTS/BTR/BTC
as a byte atomic (plan, Built (I3)); no #AC for a misaligned BT unit past the canonical top (Built (X2)); FXSAVE/XSAVE
image bytes 10-15 of each ST slot zero (Built (I5b)); the recorded choices where the SDM leaves room (implicit
supervisor accesses never hit data breakpoints, Built (S4); Reserved-NOP cells and BND on near branches execute,
Built (X1b, X1c); the TSC is CNTVCT scaled to 1 GHz, Built (S1), L5). Aligned 16-byte vector moves are one LDP/STP, so
no tearing is declared any more (Built (I5a)). The I/O APIC (R2 review): a fixed, lowest-priority or ExtINT message
with vector 0-15 is dropped at the chipset without the target LAPIC's receive-illegal-vector ESR bit (SDM Vol.3 11.5.3
sets it; only the target's resident keeps its ESR); SMI and INIT delivery modes deliver nothing (no SMM in the profile;
INIT has no model, the AP start is the mailbox), nor do the reserved modes 3 and 6 (as QEMU). Linux programs none of
these.

**Typed refusals** (a stop naming the reason, never a silent wrong result; `crates/abi/src/execution.rs` stop kinds):
`unsupported_atomic` for a LOCKed access or an unlocked CMPXCHG8B crossing 16 bytes (split locks; owner decision);
`unsupported_mode` for CPL 1/2, compatibility-mode code (far transfers or SYSRET/SYSEXIT into 32-bit code), 32-bit and
paging-off modes (owner decision 3), page tables outside guest RAM; `unsupported_system` for call gates, an ICR INIT/start-up IPI or
any delivery mode but fixed, lowest priority and NMI (the mailbox is the AP start; `execution/lapic.rs`), a LAPIC base moved onto RAM, DR7 R/W = 10 without
CR4.DE, more data breakpoints than the host offers; `unsupported_mmio` for RMW, atomic, pair, page-crossing, string and
vector forms on a device page; `unsupported_ferr` for a pending x87 exception with CR0.NE = 0; `unsupported_port` for
an SLP_TYP other than S5; `fetch_not_ram` for code on a non-RAM page; `unsupported_form` for forms fexc refuses and the
SDM gives no exception for: VEX gathers, MASKMOVQ, MASKMOVDQU, VMASKMOV/VPMASKMOV (owner decision October 30 (2): 10
gather sites in `libmvec.so.1`, 0 masked stores in the census), VCVTPS2PH m, scalar VEX.L = 1 forms, RCL/RCR m, BT* m, r
with 0x67, 16-bit BSWAP/SHLD/SHRD, ENTER with nesting, 66 on PUSHF/ENTER/LEAVE/RET/indirect branches/CMPXCHG8B, FBLD/FBSTP and
undocumented x87 aliases (`tools/fexc/shim/fexc_slice1.inc`; segment prefixes on near direct branches, reserved by the
SDM, are ignored as processors do since series45-r2fix: plan §5, owner rule, and its list to verify on x86 hardware; 66 on
near direct branches is ignored as the SDM defines for 64-bit mode since series46-br66: plan, Built (66 on near direct
branches); no segment-prefix form is refused since series47-seg: CS/DS/ES/SS ignored, so the default segment decides #SS/#GP,
the last FS/GS prefix applies, FS/GS with no memory operand ignored, owner decisions of November 3 from the reference AMD
machine, plan §5); and forms the SDM leaves reserved (Vol.2 2.1.1: "use of repeat prefixes and/or undefined opcodes
with other Intel 64 or IA-32 instructions is reserved", and it gives 66 next to a mandatory F2/F3 prefix no meaning), refused by
policy and not checked on hardware: REPNE (F2) on MOVS/STOS/LODS (the REP page defines only F3 there; hardware repeats as with
REP), REP or REPNE on a form that defines neither (not a string, mandatory-prefix, BND, PAUSE or `rep ret` form; the
suites reach BT*, INC/DEC and CMPXCHG8B/16B), and 66 with F2/F3 on an SSE form (`66 f3 0f 2c`, CVTTSS2SI), 66 on CMPXCHG16B as on CMPXCHG8B; `no_code` `spill_area` for a block fexc can
only compile with a spill area at one instruction (admission verifies no spill-slot code; a longer block is translated
again with half its instructions first, as for `code_too_large`; fexc spilled on no corpus and no boot). The ASM runner
(`tools/fexc/upstream_tests/run_asm.py`, `REFUSED`) counts a test unsupported only at a form of this list; an `unsupported_form`
stop anywhere else, or any other no-code stop, fails it (integrity audit, November 3). Since that audit, F2/F3 on SMSW are ignored
(reserved by 2.1.1; owner rule: the reference AMD machine, a user-mode probe where Linux enables UMIP: every F2/F3 and 66
register and memory form took plain SMSW's UMIP #GP, none #UD; `natemu-raw/audit-fix-20261103-01/probe/`) and AMD's SSE4a
EXTRQ/INSERTQ (66/F2 `0f 78/79`, not advertised: CPUID.80000001H:ECX[6] = 0) are #UD (the host's system decode,
`crates/host/src/system.rs`), as MOVNTSS/MOVNTSD already were. CR0.NE = 0 stays the named stop `unsupported_ferr`
and INCSSP without CET is #UD per the SDM, not QEMU's behaviour (owner decisions, November 2, phase end). Not built:
RAM above 3 GiB, more than 4 vCPUs (4-bit masks per page), 1 GiB pages, PCID, LA57, TSC-deadline, CPU offlining
(mailbox version 1).

**Deferred past this phase** (owner decisions, November 2, phase end; plan §5): faster first-time code (parallel
compiling, translating ahead of use, translations reusable across load addresses) is the next chunk, measured first;
an AES-based random-number generator inside the VM, seeded from the host (RDRAND/RDSEED refills are host exits today,
K2); raising and testing guest RAM beyond 3 GiB and vCPUs beyond 4.

**Translation latency.** Every distinct block is a compile on first use (about 0.04 ms in process, P3); the compile
cache keeps answers across runs (a boot without initrd stores about 36 MiB, the initrd boot to the installer fills the
512 MiB cap). User code at randomized addresses is compiled again per process (the RIP is in every key): the initrd
boot makes about 1.14 M compiles, 140 k with `norandmaps`. In the R2 boots Linux takes the TSC rate from the profile
("tsc: Detected 1000.000 MHz", no PIT calibration line) and never marks the TSC unstable; where `quick_pit_calibrate`
runs, first-time blocks inside its 24 us windows make it fail (plan, Built (P3)).

**Other measured costs** (not tuned): `ret` goes through the dispatcher (about 15 words); a peer storing to data on the
page a vCPU runs from makes that vCPU step each instruction; a PTE stored with no pause starves the walker's
compare-exchange (contained by the watchdog); a system instruction is a resident round trip of about 0.4 us (K2); a
stop is a GIC doorbell taking effect at the next block boundary (p50 5-8 us, a host-scheduling tail to 5-8 ms); a
compile stall delays cancellation up to the 30 s request deadline. **Resident stack** (`check_resident_stack.py` on
the final runtime, R2): the deepest gate path 19,168 of the 32,768-byte exception stack (SP_EL1; 13,600 bytes margin),
23,312 with one nested entry to its halt (9,456 margin), the boot path 12,832 of 32,768 (SP_EL0); the dispatcher template
uses no SP and no literal; `ok`. An overflow is contained to its vCPU but overwrites its boot/block stack before it
halts; new resident paths must be re-checked.

**Boots at R2** (Debian 6.12.107 bzImage and installer initrd, 2 GiB, `console=ttyS0 earlyprintk=serial
mitigations=off nokaslr`; release builds, series44-r2; one VM at a time. Wall time is from process start to the end-point
line appearing in the serial file (polled every 50 ms), the same for both emulators, so natemu's post-panic watchdog is
not included; guest time is the kernel's printk timestamp at the panic, or at "Run /init". End points: "VFS: Unable to
mount root fs" without initrd, the installer's "Select a language" dialog with it. Warm = the default compile cache after
a warm-up boot, off = `NATEMU_COMPILE_CACHE=off`. Raw records `natemu-raw/r2-20261102-01/boot/boots.jsonl`, serial logs
beside it; the natemu rows to "Select a language" were rerun on the build with the review fixes (e6b2bf5 plus them; a
warm-up boot first), `natemu-raw/r2-final-20261102-01/boot/boots.jsonl`. R2's rows labelled "with the fix" for
`boot-linux` and the cache-off stub came from the scratch rebind experiment, not the committed fix; these replace them,
and agree with them within 0.6 s. The `natemu` stub cache-warm rows were remeasured on series47-seg at 335c070, the stub
built alone with `-p natemu` and signed, the same method, after a warm-up boot at N = 1 without and with the initrd, one boot per cell
(`natemu-raw/final-suite-20261103-01/boot/boots.jsonl`); the perf chunk 2 rows are three interleaved boots per cell at
e9214c8, old and new compiler each with its own warm cache after a warm-up boot, the log off (`natemu-raw/perf2-20261103-01/`,
perf plan "Measured (chunk 2)"); the perf chunk 4 rows likewise, before (a24c785) and after (the uncommitted reuse across
load addresses) each with its own warm cache (`natemu-raw/perf4-20261103-01/`, perf plan "Measured (chunk 4)"); the
perf chunk 5 row is three interleaved boots per cell, cache off, before (d89bc4c) and after (the per-context L1 tables)
(`natemu-raw/perf5-20261103-01/step3/`, perf plan "Measured (chunk 5)"); the perf chunk 7 row likewise, before (0da552a) and
after (the host cleanups) (`natemu-raw/perf7-20261103-01/runs/`, perf plan "Measured (chunk 7)"); the perf chunk 8 row
likewise at N = 1 and one boot each at N = 4, log off, before (1c4d8b9) and after (links kept across TLB events)
(`natemu-raw/perf8-20261103-01/runs2/`, perf plan
"Measured (chunk 8)"); the perf chunk 9 row likewise, before (9ba60d5) and after (cheaper resident entry)
(`natemu-raw/perf9-20261103-01/runs/`, perf plan "Measured (chunk 9)"); the other rows are R2's.)

| To the root-fs panic, wall s (guest s) | N = 1 | N = 2 | N = 3 | N = 4 |
| --- | --- | --- | --- | --- |
| `natemu` stub, cache warm (series47-seg, 335c070) | 2.40 (1.26) | 2.19 (1.43) | 2.26 (1.54) | 2.36 (1.61) |
| `natemu-host boot-linux`, cache warm | 2.46 (1.19) | 2.04 (1.34) | 2.19 (1.47) | 2.26 (1.55) |
| `natemu` stub, cache off | 4.15 (2.83) | 4.68 (3.70) | 5.33 (4.32) | 5.70 (4.65) |
| `natemu-host boot-linux`, cache off | 4.51 (3.12) | 5.24 (4.13) | 5.58 (4.48) | 6.06 (5.03) |
| `natemu` stub, cache warm, **batch 1 end** (4ed8e3a, series48-lean; like-for-like harness below, mean of 3, `nokaslr`) | **1.69** (0.82) | **1.59** (1.10) | **1.51** (1.01) | **1.57** (1.06) |
| `natemu` stub, cache off, **batch 1 end** (same) | **2.69** (1.68) | **2.80** (2.13) | **3.05** (2.40) | **3.31** (2.62) |

| To "Select a language" (initrd), wall s ("Run /init" guest s) | N = 1 | N = 2 | N = 3 | N = 4 |
| --- | --- | --- | --- | --- |
| `natemu` stub, cache warm (series47-seg, 335c070) | 68.2 (5.24) | 53.4 (5.26) | | 50.2 (5.51) |
| `natemu-host boot-linux`, cache warm | 67.3 (5.15) | | | 49.5 (5.39) |
| `natemu` stub, cache off | 67.8 (7.12) | | | 53.1 (9.47) |
| `natemu-host boot-linux`, cache off | 68.0 (7.22) | | | 52.5 (8.95) |
| `natemu-host boot-linux`, cache warm, series47-seg (perf chunk 2, mean of 3) | 68.0 (5.25) | | | 49.8 (5.53) |
| `natemu-host boot-linux`, cache warm, **series48-lean** (perf chunk 2, mean of 3) | **52.2** (4.40) | | | **37.6** (4.52) |
| `natemu-host boot-linux`, cache off, series47-seg (perf chunk 2, mean of 3) | 67.9 (7.09) | | | 53.6 (9.60) |
| `natemu-host boot-linux`, cache off, **series48-lean** (perf chunk 2, mean of 3) | **49.3** (5.13) | | | **37.6** (6.32) |
| `natemu-host boot-linux`, cache warm, series48-lean, **reuse across addresses** (perf chunk 4, mean of 3) | **26.2** (2.95) | | | **18.8** (3.08) |
| `natemu-host boot-linux`, cache off, series48-lean, **reuse across addresses** (perf chunk 4, mean of 3) | **27.7** (4.00) | | | **22.8** (5.43) |
| `natemu-host boot-linux`, cache off, series48-lean, **per-context L1 tables** (perf chunk 5, mean of 3; d89bc4c in the same rounds: 27.7 (4.04), 22.6 (5.38)) | **17.4** (3.57) | | | **15.6** (5.05) |
| `natemu-host boot-linux`, cache off, series48-lean, **host per-request cleanups** (perf chunk 7, mean of 3; 0da552a in the same rounds: 17.8 (3.62), 15.7 (5.00)) | **15.9** (3.29) | | | **13.6** (4.51) |
| `natemu-host boot-linux`, cache off, series48-lean, **links kept across TLB events** (perf chunk 8, N = 1 mean of 3, N = 4 one boot; 1c4d8b9 in the same rounds: 15.6 (3.22), 13.4 (4.45)) | **13.6** (3.10) | | | **12.4** (4.29) |
| `natemu-host boot-linux`, cache off, series48-lean, **cheaper resident entry** (perf chunk 9, N = 1 mean of 3, N = 4 one boot; 9ba60d5 in the same rounds: 13.1 (3.02), 12.5 (4.42)) | **10.8** (2.66) | | | **11.0** (4.16) |
| `natemu-host boot-linux`, cache off, series48-lean, **reuse inside the resident** (perf chunk 10, N = 1 mean of 3, N = 4 one boot; 851cbd2 in the same rounds: 10.7 (2.64), 11.0 (4.14)) | **8.6** (2.42) | | | **9.4** (3.98) |
| `natemu` stub, cache warm, series48-lean, **batch 1 end** (4ed8e3a, like-for-like harness, mean of 3) | **6.89** (1.50) | | | **5.80** (1.78) |
| `natemu` stub, cache off, series48-lean, **batch 1 end** (4ed8e3a, same) | **8.85** (2.51) | | | **9.39** (3.78) |
| `natemu` stub and `boot-linux` at 16b0576, warm or off | never (hangs after "Run /init", see below) | never | | never |

The QEMU rows these tables held until November 3 (R2's harness: `-machine pc` with its default devices and HPET,
`-cpu Haswell,-pcid,-hle,-rtm,-invpcid`, and `ps -M` thread polling on the QEMU side only) were replaced by a like-for-like
comparison (integrity audit, November 3; `natemu-raw/qcmp-20261103-01/`: `boot.py`, `campaign.py`, `aggregate.py`), and
the tables below are that comparison rerun at the end of performance batch 1 (November 3, **HEAD 4ed8e3a**, clean tree,
series48-lean, Rust 1.97.0; `natemu-raw/batch1-final-20261103-01/timing/`: `boot.py`, `campaign.py`, `aggregate.py`, `runs/boots.jsonl`,
serial logs, `table.md`; the audit's `cpu/` flag lists are in `natemu-raw/audit-fix-20261103-01/`). One harness for both
emulators: wall from process start to the end-point line, serial polled every 50 ms, nothing else polled but a check every
0.5 s for a foreign VM process (none seen in 149 boots); Debian 6.12.107, 2 GiB, `console=ttyS0 earlyprintk=serial
mitigations=off` with and without `nokaslr`; three interleaved rounds, one VM at a time. natemu is now the `natemu` stub
(`-p natemu --bin natemu` alone, signed per §1; the audit's table used `boot-linux`; the stub reads no watchdog option, its
default is 60 s), warm = its own compile cache per KASLR setting after a warm-up boot, off = `NATEMU_COMPILE_CACHE=off`.
QEMU 11.1.2: `-nodefaults -machine pc,hpet=off,i8042=off,vmport=off` (COM1 only; the board's PIC, PIT, RTC, I/O APIC and
PIIX4 PM block stay; its fixed PIIX3 IDE and floppy controllers have no drives), `-cpu
Haswell,-pcid,-hle,-rtm,-invpcid,-tsc-deadline,-erms,-mca,-mce` (`+sha-ni` added on November 3 with the profile's SHA,
in `qcmp-20261103-01/boot.py` and `bench-20261103-01/scripts/bench.py`; QEMU 11.1.2's TCG implements it, 7.2's does not; `+adx,+clflushopt,+clwb`
follow the November 4 profile, in both harness files since November 4, and `+smep,+umip` too (plan, Built (SMEP, UMIP); Haswell already has `smep`; patch `umip-20261104-01/harness/`)), whose `/proc/cpuinfo` flags equal natemu's but three: QEMU
lacks `xsavec` (natemu's since November 4; QEMU 11.1.2's TCG implements no XSAVEC, so the control cannot match natemu in this one flag and its Linux
uses XSAVEOPT and the standard format; before, QEMU kept `xsaveopt`,
without which TCG's boot hangs after "SRBDS", and natemu had none), `nonstop_tsc` and `tsc_known_freq` (TCG offers no
invariant TSC and no leaf 15H, so Linux calibrates against the PIT there); `-accel tcg,thread=multi|single`. Cells: mean
(min-max; guest s at the panic or at "Run /init"). The aggregator fails on any boot that did not reach its end point; all
149 boots reached theirs (the audit's QEMU `thread=single` installer segfault did not recur).

| To the root-fs panic: wall s mean (min-max; guest s) | N = 1 | N = 2 | N = 3 | N = 4 |
| --- | --- | --- | --- | --- |
| `natemu` stub, cache warm, `nokaslr` | 1.69 (1.67-1.75; 0.82) | 1.59 (1.38-2.01; 1.10) | 1.51 (1.50-1.51; 1.01) | 1.57 (1.50-1.62; 1.06) |
| `natemu` stub, cache off, `nokaslr` | 2.69 (2.65-2.73; 1.68) | 2.80 (2.78-2.82; 2.13) | 3.05 (3.00-3.11; 2.40) | 3.31 (3.28-3.35; 2.62) |
| QEMU TCG `thread=multi`, `nokaslr` | 1.93 (1.93-1.94; 1.24) | 1.69 (1.67-1.71; 0.96) | 1.62 (1.58-1.68; 0.93) | 1.66 (1.65-1.67; 0.95) |
| QEMU TCG `thread=single`, `nokaslr` | 1.93 (1.91-1.95; 1.23) |  |  | 1.85 (1.78-1.91; 1.13) |
| `natemu` stub, cache warm, KASLR | 2.72 (2.67-2.76; 1.67) | 3.02 (2.93-3.11; 2.33) | 3.40 (3.35-3.49; 2.66) | 3.30 (2.49-3.78; 2.58) |
| `natemu` stub, cache off, KASLR | 2.75 (2.71-2.79; 1.69) | 2.76 (2.71-2.79; 2.11) | 3.06 (3.01-3.08; 2.38) | 3.27 (3.17-3.37; 2.60) |
| QEMU TCG `thread=multi`, KASLR | 1.91 (1.87-1.94; 1.23) | 1.67 (1.66-1.69; 0.97) | 1.61 (1.58-1.66; 0.93) | 1.66 (1.64-1.67; 0.94) |
| QEMU TCG `thread=single`, KASLR | 1.92 (1.89-1.94; 1.22) |  |  | 1.83 (1.78-1.89; 1.14) |

| To "Select a language" (initrd): wall s mean (min-max; guest s) | N = 1 | N = 2 | N = 3 | N = 4 |
| --- | --- | --- | --- | --- |
| `natemu` stub, cache warm, `nokaslr` | 6.89 (6.82-6.94; 1.50) |  |  | 5.80 (5.63-6.09; 1.78) |
| `natemu` stub, cache off, `nokaslr` | 8.85 (8.76-8.94; 2.51) |  |  | 9.39 (9.32-9.48; 3.78) |
| QEMU TCG `thread=multi`, `nokaslr` | 6.10 (6.07-6.13; 2.14) | 4.96 (4.87-5.01; 1.73) | 4.78 (4.75-4.81; 1.65) | 4.81 (4.79-4.83; 1.68) |
| QEMU TCG `thread=single`, `nokaslr` | 6.12 (6.09-6.17; 2.13) |  |  | 6.26 (6.20-6.29; 2.15) |
| `natemu` stub, cache warm, KASLR | 8.95 (8.24-9.44; 2.55) |  |  | 9.92 (8.58-10.65; 4.26) |
| `natemu` stub, cache off, KASLR | 8.96 (8.89-9.05; 2.53) |  |  | 9.44 (9.27-9.61; 3.91) |
| QEMU TCG `thread=multi`, KASLR | 6.21 (6.19-6.22; 2.17) | 5.03 (4.99-5.08; 1.76) | 4.87 (4.80-4.96; 1.70) | 4.83 (4.81-4.85; 1.73) |
| QEMU TCG `thread=single`, KASLR | 6.14 (6.07-6.20; 2.15) |  |  | 6.25 (6.20-6.28; 2.15) |

Without initrd and with `nokaslr`, the stub with a warm cache is 0.88-0.95x QEMU `thread=multi` (N = 1-4: faster) and 1.4-2.0x
with the cache off; with KASLR the warm cache still gains little for the kernel (the decompressor's relocations change its
bytes every boot, so the persistent cache misses: §6a; warm N = 4 ranges 2.49-3.78 s) and is 1.4-2.1x QEMU, as is cache off
(1.4-2.0x). To the installer the stub is 1.13x (N = 1) and 1.21x (N = 4) multi-threaded QEMU with a warm cache and `nokaslr`
(0.93x single-threaded QEMU at N = 4), 1.45x and 1.95x with the cache off, 1.44-2.05x with KASLR. QEMU's own KASLR and
`nokaslr` rows agree within 0.1 s. Against the audit's table (1c4d8b9, `boot-linux`: installer N = 1 and 4 cache off 16.04 and
13.67 s, warm 13.88 and 10.13 s; kernel only warm 2.25 and 2.01 s, off 3.27 and 3.76 s), the end of batch 1 is 8.85 and 9.39 s,
6.89 and 5.80 s, 1.69 and 1.57 s, 2.69 and 3.31 s (the audit's build predates perf chunks 8-10; the rows now use the stub, not `boot-linux`).

**Blocking defect found at R2 (resolved): device interrupts after `Boot::prepare` went through a dangling pointer.**
`irq::install` keeps `Lapics::memory: *const Memory` pointing at `backing.memory` (`crates/host/src/irq.rs:31-50`),
but `Boot::prepare` (`crates/host/src/linux.rs`) calls it on its local `backing` and then moves `backing` into the
returned `Prepared`, so every interrupt a device raises later (`Lapics::memory()` in posting and ringing) reads the
`Memory` through a stale stack address: undefined behaviour in the host. Seen as: no user-space console output (the
8250's THRE interrupt, IRQ 4, is never taken: 6 IIR reads against 14 before the split), so the installer's init blocks
in its first tty write and never prints; `rdinit=/bin/busybox -- echo r2hello` prints nothing on natemu at 16b0576 at
N = 1 and 2, prints on 959e430 (before the host split L0, 3c12567) and on QEMU. The kernel's own console is polled, so
the boots to the root-fs panic still reach it. The test driver's `run-program` keeps its backing in place, so the
suite's interrupt tests (`r1-serial`, `run_l4a.py`, `run_x3.py`) cannot see it, and L0's checks booted only to a
`poweroff -f` init. Experimental fix (scratch build only, never committed;
`natemu-raw/r2-20261102-01/diag/rebind-experiment.diff`): `Prepared::run` re-points the pointer at `self.backing.memory`
before the VM starts; with it the echo prints and both boot paths reach the installer dialog (table above). **Fix** (after R2, committed as e6b2bf5): `Backing::memory` is an `Arc<Memory>` and the
chipset's `Lapics` holds a clone of it (no raw pointer, no `unsafe impl Send`; `Memory` is `Send` as a mapping handle),
so the chipset keeps the mapping alive itself; the vCPU path still borrows `&Memory` through `Area` (unchanged). That
alone did not make the public API sound (R2 review): `GuestRam` is `Copy` with safe writes, so a copy taken from
`Prepared::guest()` or the public `Backing::ram` could outlive the mapping, and `vm::Machine::create(&Memory)` kept no
borrow. Since the review those are `unsafe` (`Prepared::guest`, `Backing::ram`), `Backing::memory` is no longer a
public field (a reassignment would strand `ram`), and the machine owns an `Arc<Memory>` clone, so safe code reaches the
mapping only while something keeps it mapped. No other
pointer or reference captured during `prepare` outlives the move: devices, the UART hook, PIT/RTC/PM and `Devices` hold
`Arc`s, `GuestRam` and the watchdog's `halted` word address the mmap (which does not move), the doorbell and compile
cache are set up in `run`. Checks, red at b3f6841 and green with the fix: `tools/run_boot_smoke.py` (§5; the busybox
echo marker on serial at N = 1 and 4: 0/2 before, 2/2 after) and the unit test
`linux::tests::interrupts_reach_the_backing_after_prepare_moves_it` (prepare, move into a `Box`, overwrite the old
stack, COM1's THRE interrupt must post vector 0x41 into the moved backing's vCPU 0: it panicked on the stale `Memory`
before). Evidence `natemu-raw/r2-fix-20261102-01/` (`red-on-head/`, the reruns, `boot/boots.jsonl`).

## 5. Standing suite

Every VM runner takes `--fexc FEXC --out DIR` under `./tools/with-env`, builds and signs the release `natemu-host` and
runtime itself (`run_boot_smoke.py` the `natemu` stub), and judges against pinned QEMU (`tools/reference/ref.py` pins the container image; Homebrew
`qemu-system-x86_64` for SMP programs) and the independent models in `tools/models/`. Guest RAM for every
`run-program` runner is `NATEMU_RAM` (hex, default 2 GiB; `NATEMU_PLACE` moves the driver's page placement); `run_m1.py`,
`run_m2.py firmware` set their own sizes. `run_l1.py`, `run_l2.py` and `run_l2_iasl.py` run no VM: `--out DIR` only
(`run_l1_smoke.py` runs the decompressor in-tree). Contract-by-contract
mapping: [contracts.md](contracts.md), "General-path check per contract".

**Small-RAM suite** (owner decision 7, November 2): every command below also runs at 4 MiB (`NATEMU_RAM=0x400000`).
The three runners that could not at R2 now can: `run_x4.py`'s mailbox, slots and results moved to 0x300000-0x320fff
and its alias page to 0x3f0000 (the mailbox's address is the program's choice; Linux's real one is in ACPI NVS),
`run_l3.py`'s RAM buffer to 0x3d0000 with "RAM's last page" following `NATEMU_RAM`, and `run_m1.py`'s `xmc-high`
cases place at R - 1 MiB of `NATEMU_RAM` (the highest mask word and summary bits for that RAM, 0x7ff00000 at 2 GiB as
before); relocation keeps what each tests, so none needed a larger small-RAM size. At 4 MiB: `run_x4.py` 33/33,
28/28, 6/6, `run_l3.py` 0 failures, `run_m1.py` 20/20 (`natemu-raw/r2-final-20261102-01/small-4m/`).

**Results at R2** (series44-r2, both RAM sizes, every command below plus `run_r1.py r1b`; raw summaries
`natemu-raw/r2-20261102-01/suite-{4m,2g}/`): at 2 GiB everything green but the 7 known i8b QEMU two-NaN departures at
both block sizes. **At 4 MiB** (`NATEMU_RAM=0x400000`) the same for every runner that can run there; three cannot, by
construction, because their programs use fixed guest-physical addresses at or above 4 MiB and the driver refuses a
page outside guest RAM: `run_x4.py` (identity-mapped pages at 0x500000), `run_l3.py` (at 0x400000) and `run_m1.py`
(its code-near-the-top cases at 0x7ff00000; it sets its own sizes for the rest); since owner decision 7 they are
relocated and run there (small-RAM suite, above). `run_m2.py` failed at 4 MiB at R2 (its model raised `KeyError` on an
MTRR overlap the SDM leaves undefined: there the firmware's MTRRs are default UC with a WB range over [0, 4 MiB), which
meets the `types` and `qemu` programs' WC and WP ranges); since the fix its model judges an undefined overlap as
natemu's recorded choice, UC, and `run_m2.py` is 112/112 at both sizes. `cargo test` 147 + 1 ignored at R2 (148 + 1
with the fix's unit test), Python unit tests 317.

**Rerun with the fix** (`natemu-raw/r2-fix-20261102-01/`, 2 GiB unless named): `cargo test` 148 + 1 ignored,
`check_resident_stack.py` `ok` (same depths), Python 317, boot smoke 2/2, `contain` 0 failures (423 fresh-process
controls), `delivery` 0 failures, `run_x3.py` (9/9, 4/4, 24/24, 6/6, 10/10, 54/54), `run_x4.py` (33/33, 28/28, 6/6),
`run_l3.py` 0 failures, `run_l4a.py` (13/13, 4/4, 2/2, 2/2, 1/1), `run_l4b.py` (5/5, 2/2, 3/3, 11/11, 7/7, 1/1),
`run_m3.py` 0 failures, `run_m2.py` 112/112 at 2 GiB and at 4 MiB, `run_r1.py r1a` 7/7 and `r1b` 12/12 (negative
controls flagged), and the initrd boots to "Select a language" at N = 1 and 4 (§4 table).

**Rerun with the integrity-audit fixes** (`natemu-raw/audit-fix-20261103-01/suite/`, 2 GiB, 1c4d8b9 plus the uncommitted
change, series48-lean; no fexc change): `cargo test` 159 + 1 ignored, also with `FEXC` set; Python 317 run, 0 skipped (with
`FEXC` set, a release `natemu-host` and the loader dump; without them `test_fexc.py` and `test_slice1.py` skip, and
`test_acpi_model.py`'s 18 tests count as one skipped module: 299 + 1 skipped in a copy with no `../natemu-raw`); ASM
1,876 pass / 0 fail / 48 deviations / 346 unsupported at both block sizes (selftest ok, with a new case: an
`unsupported_form` outside `REFUSED` fails; rerun with the same counts after the independent review made the scalar
VEX.L = 1, 66-with-F2/F3 and masked-move tests match the shim's sets byte for byte). Under the new rule the old 347 held 9 failures: the 8 SSE4a EXTRQ/INSERTQ tests
(`system_instruction` stops, now #UD as not advertised) and `Secondary/07_XX_04` (`rep smsw`, now ignored F2/F3; its
expectation is Linux's UMIP emulation, 80050033H with a 32-bit destination's upper half kept, so the runner checks it
against the SDM with the driver's CR0, `SDM_EXPECTED`: pass). The 346 by reason: refused forms of §4 201 (VEX gathers 97,
REPNE on MOVS/STOS/LODS 29, 16-bit SHLD/SHRD 20, FBLD/FBSTP and x87 aliases 14, masked moves 11, VCVTPS2PH m 10, REP/REPNE on
BT*/INC/CMPXCHG8B/16B 9, 66 on PUSHF/ENTER/LEAVE/CMPXCHG8B/16B 7, RCL/RCR m 2, `66 f3 0f 2c` 1, ENTER with nesting 1), #UD
for an instruction the profile does not advertise 83, split locks (`unsupported_atomic`) 45, FEX's Linux descriptor tables 7,
FEX-only expectations 5 (AMD's 66 rel16 2, FXAM of +0, VSTMXCSR, FYL2XP1 out of range), 80-bit MMX/x87 aliasing 2, `int 0x80`
delivered 1, a region beyond guest RAM 1, SYSCALL with EFER.SCE = 0 1. i10 faults 296 at both block sizes; `run_x1.py` 6/6
and 196/196. One installer boot with `boot-linux --code-shape` (N = 1, cache off) reached "Select a language" in 15.5 s:
1,160,065 translation requests, every object admitted with the code-shape check and none refused (a refusal is a host error
that ends the run); admission took 112 against 85 ms without the option on a kernel-only boot, so the check ran
(`natemu-raw/audit-fix-20261103-01/code-shape/`). The rest of the block was not rerun: the change reaches only the host's
system decode (66/F2 `0f 78/79`, F2/F3 SMSW), the ASM runner and the boot driver's option.

**Full suite at the end of performance batch 1** (HEAD 4ed8e3a, clean tree, series48-lean (SHA-256 `8c8bd659...fda4f592`), Rust 1.97.0, the
blocks of this section exactly, with `FEXC` set for `cargo test` and the Python tests; `natemu-raw/batch1-final-20261103-01/`:
`suite/2g/` and `suite/4m/` each hold `results.txt` (exit code and seconds per command, with a rerun's beside it), `logs/`
and the commands; `check/` the installer boots; `timing/` the §4 comparison; per-case outputs deleted as each case ended; one
VM runner at a time, none of `natemu-host`, the stub or QEMU seen alongside): 79 commands at 2 GiB and 69 at 4 MiB
(`NATEMU_RAM=0x400000`; `run_boot_smoke.py` sets its own RAM and runs at the default only). **Everything passed but the 7 known
i8b departures** (`i8-forms-001`, `-045`, `i8-random-067`, `-077`, `-097`, `-111`, `-126`, at both block sizes and both RAM sizes;
the runner exits 1, rerun once each: the same 7). No timing failure, so no other rerun. The same counts as the table of "Full
suite on the final build" above, but for: `cargo test` 163 + 1 ignored (also with `FEXC` set: the same), `check_resident_stack.py`
`ok` (16,656 / 20,832 / 8,912 bytes), Python 317 (`OK`, 0 skipped, `FEXC` set), `run_slice2_probes.py` 85, ASM 1,876 / 0 fail /
48 deviations / 346 unsupported (the audit's counts) at both RAM sizes, I7 faults 6,313, `run_x1.py` 6/6 and 196/196, `run_boot_smoke.py` 4/4,
`run_r1.py r1a` 7/7 and `r1b` 12/12 (negative controls flagged), `contain` 423 controls 0 failures, `run_l1.py` 12/12 + 41/41, `run_l2.py`
and `run_l2_iasl.py` N = 2..4 pass. Installer boots with `boot-linux --check-reuse --code-shape` (cache off, `nokaslr`, to "Select a
language"): N = 1 852,326 resident copies each also answered by the host and equal, 0 mismatches (refused: 18 word count,
1,008 second frame), 31.1 s; N = 4 vCPU 0 149,013 copies and checks, 0 mismatches, every vCPU ended `Cancelled`, 25.2 s.

**Rerun with reuse inside the resident** (perf plan "Measured (chunk 10)"; `natemu-raw/perf10-20261103-01/final/`, 2 GiB,
851cbd2 plus the uncommitted change, series48-lean, every runner with the reuse check on): `cargo test` 162 + 1 ignored, also
with `FEXC` set; `check_resident_stack.py` `ok` (16,656 / 20,832 / 8,912 bytes); fixtures 64, seed 44, faults 7,912, each at
both block sizes; `contain` (423 controls), `smp`, `sharedpt`, `coherence`, `delivery` 0 failures; `run_k1.py` 280 runs (four
reuse programs new); `run_k2.py`; `run_s4.py`; `run_x1.py` 6/6 and 196/196; `run_boot_smoke.py` 4/4; `run_r1.py r1a` 7/7,
`r1b` 12/12; installer boots with `--check-reuse` at N = 1 and 4: 0 mismatches; every boot reached the dialog.

**Rerun with the cheaper resident entry** (perf plan "Measured (chunk 9)"; `natemu-raw/perf9-20261103-01/suite/`, 2 GiB,
9ba60d5 plus the uncommitted change, series48-lean): `cargo test` 160 + 1 ignored, also with `FEXC` set;
`check_resident_stack.py` `ok` (16,368 / 20,512 / 8,672 bytes); fixtures 64, seed 44, faults 7,912, `run_i5c.py` 255,
`run_i6b.py` corpus 112 and cases 129, each at both block sizes; `delivery`, `events`, `system`, `contain` (423 controls),
`smp` 0 failures; `run_k1.py` 248 runs; `run_k2.py`; `run_s4.py` (QEMU's known `s4-d-stack` aborts as departures);
`run_x1.py` 6/6 and 196/196; `run_x3.py` all green; `run_i5a_faults.py` 454; `run_boot_smoke.py` 4/4; `run_r1.py r1a`
7/7, `r1b` 12/12; all 31 installer boots (29 measured, 2 diagnostic) reached the dialog. The first pass ran out of disk at `system`; `suite9.sh`
reran from there with each case's output deleted as it ended. The rest of the block was not rerun.

**Rerun with the chunk-6 log fields, window check and capacity cases** (perf plan "Measured (chunk 6)";
`natemu-raw/perf6-20261103-02/suite/`, 2 GiB, ea11225 plus the uncommitted change, series48-lean): `cargo test` 159 + 1 ignored,
also with `FEXC` set; `check_resident_stack.py` `ok`; Python 317; fixtures 64 and faults 7,912 at both block sizes;
`run_slice2_probes.py` 85 (two new cases fill the block index and the metadata arena at the largest code cache and still match;
negative controls red); `run_k1.py` 200 cases; `run_k2.py`; `contain` 0 failures (423 controls); `run_boot_smoke.py` 4/4;
`run_r1.py r1a` 7/7, `r1b` 12/12. The rest of the block was not rerun.

**Rerun with the host per-request cleanups** (perf plan "Measured (chunk 7)"; `natemu-raw/perf7-20261103-01/suite/`, 2 GiB,
0da552a plus the uncommitted change, series48-lean): `cargo test` 159 + 1 ignored, also with `FEXC` set; `run_boot_smoke.py`
4/4; `contain` 0 failures (423 controls); faults 7,912; `run_slice2_probes.py` 74; `run_x3.py`, `run_x4.py`, `run_l4a.py`,
`run_l4b.py` all green (the counts above; `run_l4a.py halt` now also runs the halt program under a 20 ms watchdog, 3/3, red
when the watchdog kicks halted vCPUs); all 26 installer boots reached the dialog. The rest of the block was not rerun.

**Rerun with per-context L1 tables** (perf plan "Measured (chunk 5)"; `natemu-raw/perf5-20261103-01/suite/` and `final/`, 2 GiB,
d89bc4c plus the uncommitted change, series48-lean): every command of the block below at both block sizes, green but the 7
known i8b departures; `cargo test` 159 + 1 ignored, also with `FEXC` set; `check_resident_stack.py` `ok` (same depths);
Python 317; `run_k1.py` gains `contexts` (one block called through the dispatcher under six code contexts, more than the
four L1 tables, then an INVLPG, a code write and a CR3 reload made under one context reaching the block another context's
table holds; its negative controls, no table switch, an evicted table reused uncleared, and invalidation or the CR3-load scan
limited to the dispatcher's table, fail as they must).

**Rerun with reuse across load addresses** (perf plan "Measured (chunk 4)"; `natemu-raw/perf4-20261103-01/suite/`, 2 GiB,
a24c785 plus the uncommitted change, series48-lean; the program driver's `--code-shape` now also compiles every answer
reused at another RIP and requires it equal): `cargo test` 157 + 1 ignored, also with `FEXC` set; the same runners and
counts as the series48-lean rerun below (fixtures 64, seed 44, I8 136, I8b 329 + the same 7 QEMU departures, I9 120, I9-big
5, I10 88, REX 37, 66-on-branches 12, I7 69, at both block sizes; faults 7,912 and 6,313 at both; ASM 1,875 / 0 / 48 / 347;
`run_x1.py` 6/6 and 196/196; i10 faults 296; `run_i5c.py` 255; `run_boot_smoke.py` 4/4; `run_r1.py` 7/7 and 12/12); one
installer boot with `--check-reuse` (919,666 reused answers compiled again, all equal); all 51 boots reached the dialog.

**Rerun on series48-lean** (`natemu-raw/perf2-20261103-01/suite/` and `checks/`, 2 GiB, e9214c8 plus the uncommitted
change; perf plan "Measured (chunk 2)"; its objects equal series47-seg's on a boot's 1.14 M recorded requests): `cargo test` 152 + 1 ignored, also with `FEXC` set; fexc
`test_slice1.py` 35, `test_fexc.py` 10, `check_sites.py --slice1` 64 programs (779 sites, 0 errors), `verify_precision.py`
PASS, `test_serve.py` (both modes and against series47-seg's CLI); `run_slice1_vm.py` at both block sizes: fixtures 64, seed 44, I8 136, I8b 329 + the 7 known
QEMU two-NaN departures (the same 7 programs), I9 120, I9-big 5, I10 88, REX 37, 66-on-branches 12, I7 69; faults 7,912 and
the I7 corpus's 6,313 at both; ASM 1,875 / 0 fail / 48 / 347; `run_x1.py` 6/6 and 196/196; i10 faults 296 at both;
`run_i5c.py` 255 at both; `run_boot_smoke.py` 4/4; `run_r1.py r1a` 7/7, `r1b` 12/12 (negative controls flagged); all 52
measurement boots reached the dialog. Not rerun (no fexc change can reach them beyond the objects shown identical): the
rest of the suite. The rest of the suite below ran on series47-seg.

**Rerun on series47-seg** (`natemu-raw/seg-check-20261103-01/green/`, 2 GiB, HEAD 4a56356 plus the uncommitted change;
plan, Built (segment prefixes, November 3)): `cargo test` 151 + 1 ignored (also with `FEXC` set), `check_resident_stack.py`
`ok`, Python 317, fexc `test_slice1.py` 35 and `check_sites.py --slice1` 64; fixtures 64, REX 37, 66-on-branches 12, I10 88
and the new I7 corpus 69 (also at CPL 0) at both block sizes; faults 7,912 and the I7 corpus's page mutations 6,313 at both;
probes 83; `contain` 0 failures; `run_x1.py` 6/6 and 196/196; i10 faults 296 at both; `run_i5b.py` 445/445 (255 `segment-*`);
ASM 1,875 / 0 fail / 48 / 347; `run_boot_smoke.py` 4/4; `run_r1.py r1a` 7/7, `r1b` 12/12; the installer to "Select a
language" at N = 2 in 57.2 s. The rest of the suite below ran on series46-br66 or earlier.

**Rerun on series46-br66** (`natemu-raw/br66-check-20261102-01/rerun/`; plan, Built (66 on near direct branches)): the
66 corpus 12/12 at both block sizes (series45-r2fix: 12 unsupported), `cargo test` 151 + 1 ignored, the REX corpus 37/37 and I10 corpus
88/88 at both, `run_x1.py` 6/6 and 196/196, i10 faults 296/296 at both, faults 7,912 at both, probes 83/83, ASM 1,875 / 0
fail / 48 / 347, `run_boot_smoke.py` 4/4. The rest of the suite below ran on series45-r2fix or series44-r2.

**Rerun on series45-r2fix** (`natemu-raw/r2-final-20261102-01/rerun2/`, 2 GiB): `cargo test` 151 + 1 ignored, the REX
corpus 37/37 and faults 7,912 at both block sizes, `contain`, `run_x1.py` 6/6 and 188/188, i10 faults 296/296 at both,
ASM 1,875 / 0 fail / 48 / 347, `run_boot_smoke.py` 4/4, `run_r1.py r1a` 7/7 and `r1b` 12/12. The rest of the suite below
ran on series44-r2, which differs only in the branch-prefix rule.

**Rerun after the review fixes** (`natemu-raw/r2-final-20261102-01/`, every command below at 2 GiB, both block sizes;
plan, Built (R2 review fixes)): green but the 7 known i8b departures at both block sizes; `cargo test` 151 + 1 ignored
(3 new tests; with `FEXC` set too), `check_resident_stack.py` `ok` (same depths), Python 317, ASM 1,875 / 0 fail / 48 /
347, the REX corpus 25/25 at both block sizes, `run_x4.py` 33/33, 28/28, 6/6, `run_l3.py` 0 failures, `run_m1.py` 20/20,
`run_r1.py r1a` 7/7 and `r1b` 12/12, `run_boot_smoke.py` 3/3 (with `cancel`); at 4 MiB the three relocated runners
(above); the installer boots (§4 table).

**Full suite on the final build** (series47-seg at HEAD 335c070, clean tree; `natemu-raw/final-suite-20261103-01/`: `2g/` and
`4m/` each hold `results.txt` (exit code and seconds per command) and `logs/`, `extra/` the reruns, `boot/` the boots; per-case outputs
deleted): every command of the block below, one VM runner at a time with no other `natemu-host` running, once at 2 GiB
(78 commands) and once at 4 MiB (`NATEMU_RAM=0x400000`, the 69 VM commands; `run_boot_smoke.py`, which sets its own RAM, runs at
the default only). Everything passed but the 7 known i8b departures (they flag `i8-forms-001`, `-045` and
`i8-random-067`, `-077`, `-097`, `-111`, `-126`; the runner exits 1; rerun alone once at both block sizes: the same 7, also `cargo test` with `FEXC` set: 151 + 1 ignored).

| Runner | 2 GiB | 4 MiB |
| --- | --- | --- |
| `cargo test` (also with `FEXC` set), `check_resident_stack.py`, Python | 151 + 1 ignored in both, `ok`, 317 | not run |
| `run_slice1_vm.py` fixtures / seed / REX / 66-on-branches / I7 / I8 / I9 / I9-big / I10 (default and `--max-block 1`) | 64 / 44 / 37 / 12 / 69 / 136 / 120 / 5 / 88, 0 fail | same |
| `run_slice1_vm.py` I8b | 329 match, 7 known departures | same |
| `run_slice1_faults.py` (also on the I7 corpus), `run_slice2_*` | 7,912 (I7: 6,313); flow 64, loops 846 (x3 block sizes), probes 83; 0 fail | same |
| `run_asm.py` | 1,875 pass / 0 fail / 48 deviations / 347 unsupported | same |
| `run_slice3_vm.py` (all 12 modes) | 0 failures; `contain` 423 controls | same |
| `run_i6_faults.py`, `run_i8_faults.py`, `run_i9_faults.py`, `run_i10_faults.py`, `run_i5b.py`, `run_i5c.py` | 275, 851, 586, 296, 445, 255 as expected | same |
| `run_i6b.py` corpus / cases | 112 / 129 | same |
| `run_m3.py`, `run_k1.py`, `run_k2.py`, `run_s4.py` | 0 failures | same |
| `run_x1.py`, `run_x3.py`, `run_x4.py` | 6/6 + 196/196; 9/9, 4/4, 24/24, 6/6, 10/10, 54/54; 33/33, 28/28, 6/6 | same |
| `run_l1.py` / `run_l2.py` / `run_l2_iasl.py` | 12/12 + 41/41; N = 2..4 pass; N = 2..4 pass | no VM |
| `run_l3.py`, `run_m1.py`, `run_m2.py` | 0 failures; 20/20; 112/112 | same |
| `run_l4a.py`, `run_l4b.py` | 13/13, 4/4, 2/2, 2/2, 1/1; 5/5, 2/2, 3/3, 11/11, 7/7, 1/1 | same |
| `run_r1.py r1a` / `r1b` | 7/7 / 12/12, negative controls flagged | same |
| `run_boot_smoke.py` | 4/4 | not run |

| Runner | Checks (counts at R2) |
| --- | --- |
| `tools/run_slice1_vm.py` | 64 fixtures and seeded corpora (seed 4242: 44 programs) against pinned QEMU; `--max-block 1` too; also runs the I8/I9/I10 QEMU 7.2 corpora (136, 329 + 7 known QEMU two-NaN departures, 120, 5, 88) and the R2 review's REX-chain corpus (`gen_rex.py`, 25: the census's CRYPTOGAMS sites byte for byte, random programs with 1-3 REX bytes before each REX-encoded instruction); the 66-on-branches corpus (`gen_br66.py`, 12: expected from pinned QEMU on twins with 3E for 66, since QEMU decodes rel16) |
| `tools/run_slice1_faults.py` | 7,912 precise-fault cases (paging cells, access forms, probes) against QEMU prefix runs, each delivered through the driver's logging IDT and rebuilt by `delivery_check.py`; since M2 every paging cell also sets memory-type bits in its page's walk (a rotation of PAT indices 0-7 and upper-level PWT/PCD) |
| `tools/run_slice2_vm.py`, `run_slice2_faults.py`, `run_slice2_probes.py` | control flow (64 × 3 block sizes), faults inside loops (846 × 3), code-cache/SMC probes (83) |
| `tools/fexc/upstream_tests/run_asm.py` | FEX's ASM suite: 2,270 tests, 1,875 pass, 0 fail, 48 x87 precision deviations (reduced precision), 347 unsupported, each with its reason (forms not advertised by the profile, typed refusals, FEX's known failures); since the integrity audit 1,876 / 0 / 48 / 346, unsupported only for a §4 refused form (`REFUSED`), a #UD the profile gives, a split lock or a listed FEX-only expectation; any other no-code stop fails |
| `tools/run_slice3_vm.py MODE` | `corpora`, `smp`, `limits`, `kicks`, `system`, `coherence`, `sharedpt`, `litmus`, `contain`, `privilege` (U1), `delivery` (X1a; X1c: the Table 7-4/7-5 matrix, faults during delivery, triple fault), `events` (X2: interrupt eligibility, shadows, POPF, HLT, NMI, #AC; QEMU x2APIC programs, natemu-only cases, random-time stress) |
| `tools/run_i6_faults.py` | I6 x87: gating (#NM), precise faults with the x87 state unchanged, refusals, FXAM classes, FIST indefinite, MXCSR isolation, Linux's FPU probes (SDM rules; `--max-block 1` too); the x87 QEMU corpus is `tools/reference/gen_i6.py` + `ref.py` + `run_slice1_vm.py` |
| `tools/run_k2.py` | K2 resident system path: cost per CLI/STI/POPF/SWAPGS/RDTSC/CPUID/WRMSR/FLD+FSTP m80 loop iteration (decode exits must not grow with N), RDRAND beyond the pool, cross-modifying code at a system instruction (the x86 protocol: after the writer's flag and a CPUID the new instruction runs, every rewrite checked; deterministic) |
| `tools/run_x3.py` | X3 local APIC: register read-backs (xAPIC MMIO, x2APIC), priority/TPR/EOI with nested self IPIs, CR8, the x2APIC #GP rules and x2APIC IPIs at 2-4 vCPUs vs QEMU and the SDM model; xAPIC IPIs at 2/4 vCPUs, one-shot/periodic/masked/HLT timers (exact model from TSC and CNTFRQ), random IPI + timer stress at 2/4 vCPUs, HLT across peers (the driver's end rule), driver fault and typed-stop cases; `--only cost` for APIC access costs |
| `tools/run_k1.py` | K1 code cache across CR3/INVLPG/control changes, each CPU with its own tables: remap + INVLPG or CR3 reload, CR3 ping-pong with different bytes at one address, a write to an idle block, linked direct jumps, an instruction crossing into a remapped page and across a 2 MiB boundary, vs QEMU at 1-4 vCPUs (block sizes default and 1); natemu-only by the TLB rules: no-INVLPG either-or, global pages kept across CR3 (asserted), a revalidation walk killing its own block, #PF then remap; translation requests must not grow with CR3 rounds (`--base-host/--base-runtime` reports another build beside) (boot-features plan, Built (K1)); perf chunk 8's links kept across TLB events: global at placement, INVLPG of one page or of a 2 MiB page, local links (frame check, crossing pages, code writes by self and a peer through a kept link) (perf plan "Measured (chunk 8)") |
| `tools/run_m1.py` | M1 guest RAM: tables and data in RAM's top MiB at 4 MiB, 2 GiB and 3 GiB, the device page at R, placement across 0xA0000, cross- and self-modifying code at 0x7ff00000; any runner runs at another size or placement with `NATEMU_RAM`/`NATEMU_PLACE` |
| `tools/run_m3.py` | M3 device pages: MOV-family corpus vs QEMU (on RAM) and `tools/models/mmio_model.py`, fault priority, refused forms, exactly-once under kicks at 1-4 vCPUs, a tampered reply, cross-modifying code at a device access |
| `tools/run_l1.py`, `tools/run_l1_smoke.py` | L1 boot-protocol loader: Debian and synthetic bzImages judged by `tools/models/bootproto.py`, every header and loader refusal with the model's codes (no VM); the smoke test (Debian decompressor to its jump into vmlinux vs QEMU) runs in-tree at 2 GiB since M1 (boot-features plan, Built (L1), Built (M1)) |
| `tools/run_l2.py`, `tools/run_l2_iasl.py` | L2 ACPI tables at N = 1..4 from load-only dumps: the independently written checker `tools/models/acpi_model.py` (structure, AML, CPUID/LAPIC/e820/device-set agreement; controls in `test_acpi_model.py`), and ACPICA `iasl` (Homebrew `acpica`): `-d` clean except the version-0 wakeup structure, the DSDT recompiled from its disassembly and the RSDP from iasl's template byte-equal (no VM; boot-features plan, Built (L2)) |
| `tools/run_l3.py` | L3 port I/O, UART and the unassigned rule (programs `tools/bench/smp/ports.py`): `qemu` UART widths, Linux's 8250 probe and earlyprintk sequences, unassigned ports, REP INS/OUTS with faults midway, CPL 3 IOPL/TSS-bitmap configurations vs pinned QEMU (COM1 = `-serial file`); `stress` events between string elements; `cases` strict switch, PCI probes, wrap, refusals; `pages` unassigned pages (hole, above RAM, above 4 GiB, IOAPIC page) on the all-ones shadow, writes, refusals, strict, SMP, the xAPIC page (boot-features plan, Built (L3)) |
| `tools/run_l4a.py` | L4a I/O APIC, 8259 pair and routing (programs `tools/bench/smp/irqs.py`): `qemu` register read-backs, IRQ4 edge and level (Remote IRR, TMR, EOI broadcast, masked -> unmasked, EOI register), the 8259 (Linux's probe and init, OCW3, ELCR, ExtINT via LINT0, poll, auto-EOI, rotation), the TMR across triggers, destinations at 2-4 CPUs, all vs QEMU and the model; `xapic` destinations in xAPIC mode, LINT0 fixed and the disabled-APIC pass-through; `stress` asynchronous edge and level lines at 4 and 2 CPUs; `halt` the busy-guard veto and its negative control, and the halted vCPU under a 20 ms watchdog (never forced out); `lint1` the NMI input |
| `tools/run_l4b.py` | L4b PIT/61H, RTC, ACPI PM block, S5, reset register (programs `tools/bench/smp/timers.py`): `qemu` PIT (latches, access modes, 61H, Linux's `pit_calibrate_tsc` and `quick_pit_calibrate`, IRQ0 rate, PIT-disable sequence), RTC (formats, SET, rollover, UIP, periodic/update/alarm IRQ 8), PM (PM1, PM timer, SCI) and S5 vs QEMU and the exact model, PIT modes 1/4/5 and the RTC's period change (`l4b-more`); `halt` the PIT as a vetoing source; `stress` IRQ0 + IRQ8 broadcast with HLT at 1/2/4 CPUs; `cases` typed stops, refusals and the review's `frozen` probe; `modes` PIT modes 6/7 and the lost-tick policies (reinject/discard per source); `clocks` PM/PIT/LAPIC/RTC against the TSC over 10 s |
| `tools/run_i8_faults.py`, `tools/reference/gen_i8.py` | I8 AVX/AVX2/FMA/F16C: the QEMU 7.2 corpus (`gen_i8.py`, VEX.128 and VEX.256 forms, every rounding mode, FTZ/DAZ; run with `run_slice1_vm.py`), and the fault matrix by the SDM rules against the run cut before the instruction: #PF on 32-byte crossings with no partial update, alignment #GP (16/32), non-canonical #GP/#SS, #UD/#NM gating (OSXSAVE, XCR0, TS; EM/OSFXSR/BMI not gated; legacy prefixes before VEX; XSETBV re-keying), refusals, device and all-ones pages, the XSAVE round trip with live YMM state, DE/MXCSR hand cases (boot-features plan, Built (I8)) |
| `tools/run_i6b.py`, `tools/reference/gen_i6b.py` | I6b PCMPxSTRx and the x87 transcendental, remainder, scale and extract forms (resident `svc`): `corpus` against QEMU 7.2 (every imm8 in every form, legacy/VEX, REX.W/VEX.W lengths where QEMU agrees; x87 results QEMU computes in 80 bits within 2 ulps), `cases` by the SDM (C2 at and around 2^63, FPREM/FPREM1 quotient bits and partial remainders, stack underflow/overflow responses, IE/ZE/OE/UE/PE, PCMPxSTRx #PF/#GP/#SS with no partial update, no alignment fault, 64-bit lengths against an SDM model) (boot-features plan, Built (I6b)) |
| `tools/run_x4.py` | X4 AP start through the ACPI wakeup mailbox: `units` OS scripts at 2-4 vCPUs (each AP in turn, in reverse, an absent or the BSP's APIC ID and a reserved command ignored, a second wakeup for a woken AP ignored) judged by the independently written `tools/models/mailbox_model.py` (outcome and trace; the vector read before the acknowledgement is a code-inspected invariant, no test sees it) plus the ACPI wake state, block sizes default and 1, wake latency reported; `adversarial` the X4 review's cases (command before APIC ID, a Wakeup before any vCPU runs, the mailbox through an alias page, stores into the page's OS area, two Wakeups without waiting); `trampoline` Linux 6.12's `trampoline_start64` 64-bit branch and the `secondary_startup_64` prefix to the lock release, transcribed, run by every AP at 2-4 vCPUs vs pinned QEMU (its stub starts the APs at the same vector) (boot-features plan, Built (X4)) |
| `tools/run_i9_faults.py`, `tools/reference/gen_i9.py` | I9 MOVBE, MMX, AES-NI/PCLMULQDQ (legacy and VEX.128), PDEP/PEXT, FFREEP: the QEMU 7.2 corpus (`gen_i9.py`; run with `run_slice1_vm.py`), and the fault matrix by the SDM rules against the run cut before the instruction, the x87/MMX state included (#PF/#GP/#SS, m32 PUNPCKL*, alignment of the legacy m128 forms, MMX gating #UD/#NM, #UD for MOVBE register forms and ADX, #AC, MASKMOVQ refused; since November 3 the SHA forms' faults, alignment, gating and 66/F2/F3 #UD) (boot-features plan, Built (I9), Built (SHA)) |
| `tools/reference/gen_adx.py`, `tools/reference/gen_xsave.py` | ADX and XSAVEOPT/XSAVEC (November 4): 56 programs each, expected from the AMD machine (`ref_native.py --require-flag adx` / `xsavec`); ADX's model programs also replayed through the SDM (`gen_adx.py --check`), XSAVE images compared byte for byte; run with `run_slice1_vm.py` (boot-features plan, Built (ADX), Built (XSAVEOPT, XSAVEC)) |
| `tools/reference/gen_sha.py`, `tools/reference/ref_native.py` | SHA (November 3): 60 programs, all seven instructions, every SHA1RNDS4 imm8, register and memory forms with REX, 0x67 and ignored prefixes; the reference is the AMD machine running each program's ELF natively (`ref_native.py`; pinned QEMU 7.2 has no SHA), checked against the SDM pseudocode (`gen_sha.py --check`); run with `run_slice1_vm.py` (boot-features plan, Built (SHA)) |
| `tools/run_i10_faults.py`, `tools/reference/gen_i10.py` | I10 0x67 address size, XLAT, #UD forms, CET hint space: the QEMU 7.2 corpus (`gen_i10.py`: EA forms with garbage upper halves and 4 GiB wrap, FS-based, EIP-relative, moffs32, XLAT, addr32 call/jmp, strings with ESI/EDI/ECX, LOOP/JECXZ; run with `run_slice1_vm.py`), and the case matrix by the SDM rules against the run cut before the instruction (#PF at the truncated address, no #GP for garbage upper halves, FS.base + EA non-canonical #GP, EIP wrap, host-decoded forms with 0x67, #AC, zero-extended ESI/EDI/ECX at a midway REP fault, LOOP/JECXZ on ECX, BT* m, r refused, every #UD form, #UD before #NM, the 0f 1e NOPs) (boot-features plan, Built (I10)) |
| `tools/run_i5b.py` | I5b FXSAVE/FXRSTOR, XSAVE/XRSTOR, XGETBV/XSETBV (since November 4 XSAVEOPT, XSAVEC and the compacted XRSTOR: their faults by the SDM, natemu only for XSAVEC): `images` (FXSAVE/XSAVE/XRSTOR images and XCR0/CPUID.0DH sequences byte by byte against QEMU's Haswell model, block sizes default and 1, declared differences compared on natemu's side), `faults` (alignment, #PF at page boundaries with no partial update, #NM/#UD gating, XSETBV/XGETBV and XRSTOR header #GP, refusals, #AC never; SDM rules, QEMU where it can run the case) (boot-features plan, Built (I5b)) |
| `tools/run_s4.py` | S4 debug: `programs` (tools/bench/smp/debug.py: TF through mixed instructions, REP strings, CALL/RET, CPUID; POPF/IRETQ/SYSRET/SYSCALL with TF; MOV SS; INT3/INT n/INT1 under TF; UD2/#GP/#PF under TF; instruction breakpoints mid-block, at a loop head, at a kept system instruction, inside an instruction, after MOV SS, at a REP, by CALL RAX, armed over translated code; DR6/DR7 read-back, DR4/DR5, DR7.GD, sticky DR6; data breakpoints for every R/W x LEN at range edges, RMW/LOCK/XCHG/CMPXCHG, BT units, x87/SSE/AVX, string and stack forms, IRETQ/POPF, an exception frame, MOV SS, a page-crossing store; I/O breakpoints on IN/OUT/INS/OUTS) judged by the SDM's expected event logs and against pinned QEMU, block sizes default and 1; `cases` HLT under TF, device-page accesses, per-vCPU registers, R/W 10 without CR4.DE refused (boot-features plan, Built (S4)) |
| `tools/run_m2.py` | M2 memory types: `qemu` CPL 0 programs on their own tables with Linux's PAT and PAT indices 0-7 in PTEs, PWT/PCD in the PML4E/PDPTE/PDE and CR3, 2 MiB leaves with each index, the SDM's MTRR update sequence with UC/WC/WT/WP variable ranges and MTRRs disabled (loads, stores, LOCK, XCHG, MOVNTI, CLFLUSH, strings, code run from each page) vs pinned QEMU at 1/2 CPUs, block sizes default and 1; `types` the resident's data shadows by type (`stats.k1.memory_types`) against the SDM's Table 14-7 and MTRR precedences (reset and Linux PAT, upper-level and CR3 bits, large leaves, ranges and overlaps, fixed ranges with FE set and clear, MTRRs disabled); `devices` a device page with each PAT index under UC/WC/WB MTRRs and MTRRs disabled: one device access each, never shadowed; `firmware` (addendum 1) the firmware's MTRRs read back at 4 MiB, 1, 2 and 3 GiB of RAM equal `cpu_profile.py`'s `mtrr_state(R)`: all RAM WB, the hole and [R, 4 GiB) UC; addendum 2 (UC RAM strongly ordered): `qemu` also `uc` (every access form on UC pages, straddles both ways, a UC stack, code from UC), `faults` (#PF/U-S/R-W/WP/reserved/#AC/data breakpoints on UC pages equal to WB and to the SDM), `litmus` (store buffering on UC pages at 2-4 vCPUs never shows every load old; the WB control at 2 does; at 2 one location UC and one WB never both old), `types` also a PAT write turning a WB-shadowed page UC (after a CR3 reload or INVLPG it takes the UC path), `cost` (reported) (boot-features plan, Built (M2), Built (M2 addendum 1, 2)) |
| `tools/run_i5c.py` | I5c unmasked floating-point exceptions, judged by the SDM (QEMU 7.2 raises no #XM and delivers #MF only at FWAIT without leaving operands unaltered: it cannot judge them): 47 SSE/AVX/FMA/F16C forms each masked, with each raised flag unmasked (#XM at the instruction, destination and RFLAGS unchanged, MXCSR by the pre/post-computation precedence) and with the others unmasked; exact tiny results, FTZ under an unmasked UE, DAZ, DPPS steps, YMM, #UD without OSXMMEXCPT, LDMXCSR/VLDMXCSR/FXRSTOR re-keying; x87: #MF at 18 kinds of waiting instruction (MMX, the resident's FLDCW/FLDENV/FRSTOR/m80 and helper forms included), none at FNSTSW/FNSTCW/FNCLEX/FNINIT/FNSTENV/FNSAVE/FXSAVE, aborted IE/DE/ZE, stack faults, FST/FIST stores, PE/OE stores, CR0.NE = 0 (`unsupported_ferr`) (boot-features plan, Built (I5c)) |
| `tools/run_x1.py` | X1b/X1c (programs `tools/bench/smp/entry.py`): `qemu` `x1-entry@1/2/4` vs pinned QEMU and delivery_model: frames on RSP0 and IST1..7 (CPL 3 -> 0 and 0 -> 0), IRETQ to CPL 3, normal (SWAPGS) and paranoid (RDGSBASE, LSL of a CPU/node descriptor, WRGSBASE) entries of NMI/#DF/#DB/0x12, #DF from CPL 3 and 0, NMI blocking until IRETQ; `x1-sreg@1` PUSH/POP FS/GS and the PREFETCHW/CLDEMOTE hints vs QEMU and the SDM; `x1-rnop@1` the Reserved-NOP cells vs QEMU; `cases` refused-but-defined forms now delivered (SDM rules): #UD encodings (64-bit-invalid opcodes, Table A-2/A-3/A-6 blanks, register operands of memory-only forms, GETSEC, RSM, VMX group 9, unprefixed 0f 38/0f 3a), direct branches (LOOPcc/JrCXZ and 0x67 included) to a non-canonical target (#GP(0) when taken), PUSH/POP FS/GS faults, the split-lock and fetch-from-non-RAM typed stops, CLTS, SMSW, LMSW, SYSENTER, SYSEXIT (boot-features plan, Built (X1b, X1c)) |
| `tools/run_r1.py r1a` | R1a single-vCPU composites (one program each, feature chains already built): `r1-boot` (64-bit entry into a kernel-head sequence: 4-level tables with a kernel map, CR4/CR3, LGDT/LRETQ to __KERNEL_CS, EFER SCE and NX, CR0_STATE, early IDT fixing up UD2, back to the identity map), `r1-demand` (#PF handler mapping on demand from zeroed pools: read, write, fetch, REP STOSQ faulting twice), `r1-int3` (Linux's `int3_selftest`), `r1-poke` (`__text_poke` through a poking address under a second CR3, `sync_core`, the `text_poke_bp` INT3 protocol, a static-call NOP5 -> JMP), `r1-syscall` (SYSCALL/SYSRET with SWAPGS, a PTI CR3 switch each way and VERW), `r1-timer` (LAPIC one-shot waking `sti; hlt`), `r1-serial` (8250 THRE transmit driven by IRQ4 through the I/O APIC); each vs pinned QEMU at block sizes default and 1 and by the SDM's expected state; a negative control each (boot-features plan, Built (R1a)) |
| `tools/run_r1.py r1b` | R1b multi-vCPU composites at N = 2, 3, 4 (block sizes default and 1): `r1b-mpstart` (APs started through the ACPI MP-wakeup mailbox, each running a kernel-head-shaped sequence: own GDT/IDT, GS base, stack, arrival check-in; natemu wakes via the mailbox, QEMU's stub starts them at the same vector), `r1b-poke` (the `text_poke_bp` INT3 protocol across CPUs while peers execute the site, the sync IPI's IRETQ as `sync_core`, each peer's #BP handler emulating the new instruction), `r1b-nmi` (NMI by x2APIC IPI on an IST stack, the paranoid handler reading its CPU number by LSL of a per-CPU descriptor with RDGSBASE/WRGSBASE/SWAPGS), `r1b-ipiload` (all-to-all fixed-vector IPIs under memory-heavy loops: per-pair counts exact, EOI leaving the ISR clear, the loop checksums); vs pinned QEMU (x2APIC) with peers by exact counts, per-CPU end state and SDM invariants, plus a negative control each (boot-features plan, Built (R1b)) |
| `tools/run_boot_smoke.py` | `cancel` and `cancel-group`: the bzImage at 4 vCPUs, SIGINT 1.5 s in to the stub, then to its whole process group (a terminal's Ctrl-C): exit 2, `Cancelled`, within 5 s; the library boot path (the `natemu` stub, built alone): the Debian bzImage and installer initrd with `rdinit=/bin/busybox -- sh -c "echo MARKER; poweroff -f"` at N = 1 and 4 (`--vcpus`); pass: the marker on its own serial line (busybox's console write goes through the 8250's THRE interrupt, IRQ 4 via the I/O APIC) and exit 0 (S5). Needs `natemu-raw/kernel-census-20261028-01/dl/{linux,initrd.gz}`; compile cache off unless `NATEMU_COMPILE_CACHE` is set; about 25 s. Guards R2's blocker (§4), which `run-program` runners cannot see |
| `tools/check_resident_stack.py` | resident stack bound and gate/ERET binary shape of the built runtime |
| `tools/models/` | independent ordering, coherence, PAE and modes models (used by the runners) |

Full suite (about 25 minutes of runner time per pass on the M4 at R2, waits for other VM users excluded; the reduced
`kicks`/`sharedpt`/`litmus` settings are the phase's):

```sh
# From the repository root; zsh or bash. Wait until no other natemu-host runs (timing-sensitive cases: kicks, l4b,
# x3 stress). A timing failure may be rerun alone once, never more: the results record the first run's result and the
# rerun's beside it; a failed rerun is a failure.
F=../natemu-raw/fexc-native-20261026-01/series61-chunk25-r1/obj/fexc  # tools/fexc/build.sh rebuilds it from the fork pin
SEED=../natemu-raw/s1d1-seed4242
FLOW=../natemu-raw/s2d/corpus
I8=../natemu-raw/i8-corpus-20261030-01      # gen_i8.py programs and their QEMU 7.2 results
I8B=../natemu-raw/i8-corpus-20261030-02     # I8 review corpus: 7 known QEMU two-NaN departures (plan, Built (I8) review)
I6B=../natemu-raw/i6b-corpus-20261030-01    # gen_i6b.py: PCMPxSTRx every imm8, x87 helper forms
I9=../natemu-raw/i9-corpus-20261030-01      # gen_i9.py: MOVBE, MMX, AES/PCLMULQDQ, PDEP/PEXT, FFREEP
SHA=../natemu-raw/sha-corpus-20261103-01    # gen_sha.py: SHA1*/SHA256*, expected from the AMD machine (ref_native.py)
ADX=../natemu-raw/adx-corpus-20261104-01    # gen_adx.py: ADCX/ADOX, expected from the AMD machine (pinned QEMU 7.2's ADX is broken)
XS=../natemu-raw/xsave-corpus-20261104-01   # gen_xsave.py: XSAVEC/XSAVEOPT/compacted XRSTOR images, expected from the AMD machine
BIG=../natemu-raw/i9-big-20261030-01        # PDEP/PEXT/PCMPISTRI runs over a block's 32 KiB
I10=../natemu-raw/i10-corpus-20261030-01    # gen_i10.py: 0x67 forms, XLAT, strings/LOOP with ECX
REX=../natemu-raw/rex-corpus-20261102-02    # gen_rex.py: REX chains (the census's last sites), segment-prefixed branches
BR66=../natemu-raw/br66-corpus-20261102-01  # gen_br66.py: 66 on near direct branches (expected: QEMU on the 3E twins)
I7=../natemu-raw/i7-corpus-20261103-01      # gen_i7.py: FS/GS overrides, and the November 3 segment-prefix rules (2 edge programs)
O=../natemu-raw/suite-$(date +%Y%m%d-%H%M%S)
s3() { tools/with-env python3 tools/run_slice3_vm.py --fexc "$F" "$@"; }
cargo +1.97.0 build --offline --locked
cargo +1.97.0 test --offline --locked
cargo +1.97.0 build --offline --locked --release -p natemu-runtime --target aarch64-unknown-none
python3 tools/check_resident_stack.py
FEXC="$F" python3 -B -m unittest discover -s tools -p 'test_*.py'   # record "Ran N" with its skip count: test_fexc.py and test_slice1.py skip without a fexc, test_acpi_model.py without target/release/natemu-host and ../natemu-raw (or L2_DUMP)
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/fix"
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/fix-1" --max-block 1
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/seed" "$SEED/programs.json" "$SEED/expected.json"
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/seed-1" --max-block 1 "$SEED/programs.json" "$SEED/expected.json"
tools/with-env python3 tools/run_slice1_faults.py --fexc "$F" --out "$O/faults"
tools/with-env python3 tools/run_slice1_faults.py --fexc "$F" --out "$O/faults-1" --max-block 1
tools/with-env python3 tools/run_slice2_vm.py --fexc "$F" --out "$O/flow" --programs "$FLOW/programs.json" --expected "$FLOW/expected.json"
tools/with-env python3 tools/run_slice2_faults.py --fexc "$F" --out "$O/loopfaults"
tools/with-env python3 tools/run_slice2_probes.py --fexc "$F" --out "$O/probes"
tools/with-env python3 tools/fexc/upstream_tests/run_asm.py --fexc "$F" --out "$O/asm" --jobs 4
s3 --out "$O/corpora" corpora --parallel 4
s3 --out "$O/smp" smp --parallel 2
s3 --out "$O/limits" limits
s3 --out "$O/kicks" kicks --kicks 1000 --cpus 1 2 4
s3 --out "$O/system" system
s3 --out "$O/coherence" coherence --reps 1 --parallel 2
s3 --out "$O/sharedpt" sharedpt --reps 1 --parallel 2
s3 --out "$O/litmus" litmus --k 5000 --runs 1 --parallel 2
s3 --out "$O/contain" contain
s3 --out "$O/privilege" privilege
s3 --out "$O/delivery" delivery                               # X1a, X1c: nested faults, the #DF matrix, shutdown, gates (delivery_model)
s3 --out "$O/events" events --kicks 2000                       # X2: eligibility, shadows, POPF, HLT, NMI, #AC, stress (delivery_model)
tools/with-env python3 tools/run_i6_faults.py --fexc "$F" --out "$O/i6-faults"                  # I6: x87 gating, faults, refusals
tools/with-env python3 tools/run_i6_faults.py --fexc "$F" --out "$O/i6-faults-1" --max-block 1
tools/with-env python3 tools/run_m3.py --fexc "$F" --out "$O/m3"   # M3: device pages (QEMU via Docker, mmio_model, kicks)
python3 tools/run_l1.py --out "$O/l1"                        # L1: loader vs bootproto.py, refusals (load-only, no VM)
python3 tools/run_l2.py --out "$O/l2"                        # L2: ACPI tables vs the independent checker, N = 1..4 (load-only, no VM)
python3 tools/run_l2_iasl.py --out "$O/l2-iasl"               # L2: the same tables through iasl -d, DSDT and RSDP recompiled
tools/with-env python3 tools/run_k2.py --fexc "$F" --out "$O/k2"   # K2: system-instruction cost, RDRAND refill, XMC at system instructions
tools/with-env python3 tools/run_x3.py --fexc "$F" --out "$O/x3"   # X3: local APIC, IPIs, timers vs QEMU and the SDM model; stress; HLT across peers
tools/with-env python3 tools/run_x4.py --fexc "$F" --out "$O/x4"   # X4: mailbox AP start vs the mailbox model; the review's adversarial cases; the trampoline + secondary_startup_64 prefix vs QEMU at 2-4 vCPUs
tools/with-env python3 tools/run_k1.py --fexc "$F" --out "$O/k1"   # K1: code cache across CR3/INVLPG/control changes vs QEMU at 1-4 vCPUs; TLB rules; counts
tools/with-env python3 tools/run_m1.py --fexc "$F" --out "$O/m1"   # M1: RAM top at 4 MiB/2 GiB/3 GiB, the holes, code near the RAM top
tools/with-env python3 tools/run_l3.py --fexc "$F" --out "$O/l3"   # L3: port I/O, UART, IOPL/bitmap vs QEMU; unassigned ports and pages (all-ones shadow), strict switch
tools/with-env python3 tools/run_l4a.py --fexc "$F" --out "$O/l4a"   # L4a: I/O APIC, 8259, routing vs QEMU; async lines, the halted veto, LINT0/LINT1
tools/with-env python3 tools/run_l4b.py --fexc "$F" --out "$O/l4b"   # L4b: PIT, RTC, PM block, S5/reset vs QEMU; halted veto, stress, typed stops, clock agreement
tools/with-env python3 tools/run_i5b.py --fexc "$F" --out "$O/i5b"   # I5b: FXSAVE/XSAVE images vs QEMU (Haswell model), the fault matrix by the SDM rules
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/i8" "$I8/programs.json" "$I8/expected.json"
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/i8-1" --max-block 1 "$I8/programs.json" "$I8/expected.json"
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/i8b" "$I8B/programs.json" "$I8B/expected.json"          # 7 known QEMU two-NaN departures (plan, Built (I8) review)
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/i8b-1" --max-block 1 "$I8B/programs.json" "$I8B/expected.json"
tools/with-env python3 tools/run_i8_faults.py --fexc "$F" --out "$O/i8-faults"               # I8: AVX faults, gating, refusals, XSAVE round trip, DE
tools/with-env python3 tools/run_i8_faults.py --fexc "$F" --out "$O/i8-faults-1" --max-block 1
tools/with-env python3 tools/run_i6b.py corpus --fexc "$F" --out "$O/i6b" "$I6B/programs.json" "$I6B/expected.json"
tools/with-env python3 tools/run_i6b.py corpus --fexc "$F" --out "$O/i6b-1" --max-block 1 "$I6B/programs.json" "$I6B/expected.json"
tools/with-env python3 tools/run_i6b.py cases --fexc "$F" --out "$O/i6b-cases"                 # I6b: C2, FPREM bits/partial, stack faults, flags, PCMPxSTRx faults, 64-bit lengths
tools/with-env python3 tools/run_i6b.py cases --fexc "$F" --out "$O/i6b-cases-1" --max-block 1
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/i9" "$I9/programs.json" "$I9/expected.json"
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/i9-1" --max-block 1 "$I9/programs.json" "$I9/expected.json"
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/i9-big" "$BIG/programs.json" "$BIG/expected.json"
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/i9-big-1" --max-block 1 "$BIG/programs.json" "$BIG/expected.json"
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/sha" "$SHA/programs.json" "$SHA/expected.json"
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/sha-1" --max-block 1 "$SHA/programs.json" "$SHA/expected.json"
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/adx" "$ADX/programs.json" "$ADX/expected.json"
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/adx-1" --max-block 1 "$ADX/programs.json" "$ADX/expected.json"
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/xsave" "$XS/programs.json" "$XS/expected.json"
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/xsave-1" --max-block 1 "$XS/programs.json" "$XS/expected.json"
tools/with-env python3 tools/run_i9_faults.py --fexc "$F" --out "$O/i9-faults"                 # I9: faults with the x87/MMX state, gating, #UD, #AC, refusals
tools/with-env python3 tools/run_i9_faults.py --fexc "$F" --out "$O/i9-faults-1" --max-block 1
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/i10" "$I10/programs.json" "$I10/expected.json"
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/i10-1" --max-block 1 "$I10/programs.json" "$I10/expected.json"
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/rex" "$REX/programs.json" "$REX/expected.json"
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/rex-1" --max-block 1 "$REX/programs.json" "$REX/expected.json"
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/br66" "$BR66/programs.json" "$BR66/expected.json"
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/br66-1" --max-block 1 "$BR66/programs.json" "$BR66/expected.json"
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/i7" "$I7/programs.json" "$I7/expected.json"
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/i7-1" --max-block 1 "$I7/programs.json" "$I7/expected.json"
tools/with-env python3 tools/run_slice1_faults.py --fexc "$F" --out "$O/i7-faults" --no-probes "$I7/programs.json"   # #PF at base + EA
tools/with-env python3 tools/run_slice1_faults.py --fexc "$F" --out "$O/i7-faults-1" --no-probes --max-block 1 "$I7/programs.json"
tools/with-env python3 tools/run_i10_faults.py --fexc "$F" --out "$O/i10-faults"               # I10: 0x67 faults, #UD forms, CET NOPs
tools/with-env python3 tools/run_i10_faults.py --fexc "$F" --out "$O/i10-faults-1" --max-block 1
tools/with-env python3 tools/run_s4.py --fexc "$F" --out "$O/s4"   # S4: TF single-step, INT1, instruction breakpoints, DR6/DR7 vs the SDM's logs and QEMU
tools/with-env python3 tools/run_m2.py --fexc "$F" --out "$O/m2"   # M2: memory types: PAT/PWT/PCD/MTRR programs vs QEMU, type counts and device pages by the SDM
tools/with-env python3 tools/run_i5c.py --fexc "$F" --out "$O/i5c"   # I5c: #XM/#UD and #MF by the SDM rules (masked, each flag unmasked, precedence, waiting instructions)
tools/with-env python3 tools/run_i5c.py --fexc "$F" --out "$O/i5c-1" --max-block 1
tools/with-env python3 tools/run_x1.py --fexc "$F" --out "$O/x1"   # X1b/X1c: CPL 3 frames, IST1..7, paranoid entries, NMI blocking vs QEMU and delivery_model; refused-but-defined forms delivered
tools/with-env python3 tools/run_r1.py r1a --fexc "$F" --out "$O/r1a"   # R1a: single-vCPU composites (boot head, on-demand #PF, int3_selftest, text_poke, syscall/PTI/VERW, timer HLT wake, serial IRQ4) vs QEMU at both block sizes; negative controls
tools/with-env python3 tools/run_boot_smoke.py --fexc "$F" --out "$O/boot-smoke"   # library boot path: Debian initrd, busybox echo on serial (IRQ 4) at N = 1, 4
tools/with-env python3 tools/run_r1.py r1b --fexc "$F" --out "$O/r1b"   # R1b: multi-vCPU composites (mailbox AP start, cross-CPU text_poke_bp, NMI by IPI on an IST with LSL, IPIs under load) vs QEMU at N = 2..4, both block sizes; negative controls
```

## 6. Compiler reproducibility

**series61-chunk25-r1** (November 5; perf plan "Measured (chunk 25)", after its review): the fork's `natemu` branch at
ab62c7e06 (an inline STI may start its block; STMXCSR/VSTMXCSR store `FexcMxcsr`) and this tree's shim; `obj/fexc` SHA-256
`c906217071ee7258d832d10a90706c2e0497f02e1637e77c7f044cf50bb43906`, reproduced by `tools/fexc/build.sh` from the pin. Before it,
**series58-channel-r1** (November 4; perf plan "Measured (chunk 21)", after its review): series57-fork's patches and shim;
`fexc.cpp`'s server also answers through a shared mapping (`--serve --channel FD`, with a layout magic), which the host now
requires; every reply byte-identical to series57-fork's over the pipe on recorded installer and `build` streams; `obj/fexc`
SHA-256 `8bd3b7eaff3ef4c2cb101ffb34b55d2e83aa7f6747e22a186b65e493aca83e59` (reproduced by a second `build.sh`). Before it,
series58-channel (`da3370217403f5c314a75263f78aee075b9b387e7ddf8cd2ddf9e470b687315c`, before the review; measured). Before it,
series57-fork (built from the fork, the same bytes as series56-setup-r1). Before it:

**series56-setup-r1** (November 4; perf plan "Measured (chunk 20)", after its review): series55-swapgs's patches and shim;
`fexc.cpp`'s server parses, checks and configures once per distinct argument list less the `--rip` value and writes each
reply with one `writev` (every reply byte-identical to series55-swapgs's on recorded installer and `build` streams);
`obj/fexc` SHA-256 `a81a2f241b6bf368cd7b6e8b87c5892efacd9747127f6f8dea97ae936add1fd8`. Before it, series56-setup (`d3222fda2d06f0f4771b51c8ab24c33c61a4db456ed136b972cc6f76fc7db064`,
the change before the review's fixes, reproduced by a second `build.sh`; measured). Before it:

**series55-swapgs** (November 4; perf plan "Measured (chunk 17)"): `patches/0001-0033` (new 0033: SWAPGS inline, exchanging
`gs_cached` with IA32_KERNEL_GS_BASE at FEX's `L1Pointer`) on the pinned FEX commit with the shim's `Cut` rule for the plain
SWAPGS under `--cpl0`; `obj/fexc` SHA-256 `4a50bda9ac5231db9faa2ce7f5e901da840666a435215f7b8aa47654baa8ecae`. Built by
`tools/fexc/build.sh`. Before it:

**series54-inline-tsc** (November 4; perf plan "Measured (chunk 15)"): `patches/0001-0032` (new 0031: CLI and STI inline,
the STI's exit to its own RIP on the event-pending byte, the next decoded instruction passed to the allowlist's `Cut`; new
0032: RDTSC inline, floor(V x 125 / 3) + the TSC offset) on the pinned FEX commit with the shim's `--cpl0` and `--tsc`
arguments (FEXC_ARG_CPL0, FEXC_ARG_TSC) and their `Cut` rules; `obj/fexc` SHA-256
`81fa0932da3a89e3fffd14889c48a42803d9ee7c635651fe25f5c3a6c8eab134`. Its predecessor in this work, not kept as a reference:
series53-inline (0031 only). Built as `build.sh` does (a worktree of the submodule). Before it:

**series52-adox66** (November 4; boot-features plan, Built (ADX), Built (XSAVEOPT, XSAVEC)): `patches/0001-0030` (new 0030: ADOX
with a 66 prefix decoded as ADOX, 66 ignored, as on the reference AMD machine) on the pinned FEX commit with the shim admitting
ADCX/ADOX and naming XSAVEOPT/CLWB/CLFLUSHOPT/XSAVEC as the resident's in its cut reasons; `obj/fexc` SHA-256
`bef4081c9444d030413448159e0c02d197d2bbf50c6f53b543a0f5479c7e9fe8`. Its predecessors in this work, not kept as references:
series51-xsave (`9600ebd1...1892`, no patch 0030, 66 F3 cut) and series50-adx (`8a25ab6c...196e`). Built as `build.sh` does (a worktree of the submodule). Before it:

**series49-sha** (SHA advertised, owner decision November 3; boot-features plan, Built (SHA)): `patches/0001-0029`
(unchanged) on the pinned FEX commit with the shim admitting SHA1*/SHA256* (`Sse`) and its #UD cut removed; `obj/fexc`
SHA-256 `fe1631cbea73342f32a3a3689593c6bf5f9263577e0691e68f3b76d9208e1f61`. Built as `build.sh` does but from
`git archive` trees of the submodule and its three externals (no worktree in the repository's git directory); the same
procedure on HEAD's shim gives series48-lean's `8c8bd659...a91a` exactly. Before it:

**series48-lean** (perf plan "Measured (chunk 2)", November 3): `tools/fexc/build.sh
../natemu-raw/fexc-native-20261026-01/series48-lean` applies `patches/0001-0029` to the pinned FEX commit with the working
tree's shim (as series47-seg) and `fexc.cpp`, whose `--serve` now sends only what the host reads (no JSON report for an
object, `guest.end` for no code; `--serve --report` for `test_serve.py`); `obj/fexc` SHA-256
`8c8bd6594a5cbe557d8928c483f2f6c3887f9c68b8797d84e83e1912fda4f592` (a fresh `build.sh` into a scratch directory gave the
same); its compile-cache entries are its own. Its objects, extents and args_read equal series47-seg's on an installer boot's
1,142,279 recorded requests. Before it:

**series47-seg** (the owner's segment-prefix decisions, November 3): `tools/fexc/build.sh
../natemu-raw/fexc-native-20261026-01/series47-seg` (21 s wall) applies `patches/0001-0029` to the pinned FEX commit with the
working tree's shim and `fexc.cpp` (no patch changed: FEX's 64-bit decoder already ignores CS/DS/ES/SS and keeps the last
FS/GS prefix); `obj/fexc` SHA-256 `c4de5da07fabda7595a27ce4833961e297f34e4753e80cce3454a32aeb69f91a`; its compile-cache
entries are its own. Before it:

**series46-br66** (66 on near direct branches): `tools/fexc/build.sh ../natemu-raw/fexc-native-20261026-01/series46-br66`
(20 s wall) applies `patches/0001-0029` to the pinned FEX commit with the working tree's shim (66 admitted on near direct
branches); `obj/fexc` SHA-256 `973c06fe5ace771a2ab8d099d4ba588f140ff57e6f0d250bfe5df237f823724c`; its compile-cache entries
are its own. Before it:

**series45-r2fix** (after the R2 review): `tools/fexc/build.sh ../natemu-raw/fexc-native-20261026-01/series45-r2fix` (20 s
wall), the same patches and pinned FEX commit with the working tree's shim (the near-direct-branch segment-prefix rule);
`obj/fexc` SHA-256 `354cab22f15e79f0e1b83ee59ffa4b4f40d9099b61ec6ae93209a8b1f1ba56f3`; its compile-cache entries are its own
(a new binary hash). Before it:

`tools/fexc/build.sh ../natemu-raw/fexc-native-20261026-01/series44-r2` (R2, 21 s wall) applies `patches/0001-0028` to
the pinned FEX commit `48d71752` and builds with the shim and `fexc.cpp` at 9730c4d. Its `obj/fexc` is byte-identical
to series43-x1's, which was built by hand with cmake from the same patches and shim (SHA-256
`6c282011...a7c293d` both); the source trees are identical too. Only `libfexcore.a` differs (archive member
timestamps), which does not reach the linked binary. Series43-x1 and series44-r2 are therefore the same compiler, and
the compile cache's entries (keyed by the binary's hash) serve both.

## 6a. Known issues (carried to the next phase)

The phase's finish rule (owner decision 8, plan §5): no known blocking defect remains; everything else known is here.
Severity: **medium** = a real cost or gap an integrator will meet, **low** = a limit with a typed stop, a declared
deviation, or a contained corner.

- The next phase is performance (owner decision 1); the correctness pass comes after it.
- **low**, guest (not natemu): an installer boot can miss "Select a language" because the Debian installer's own two
  `main-menu` copies (serial console and tty1) race on `/var/lib/dpkg/status` while `udpkg` renames it; the serial copy
  reads NULL and faults: "segfault at 8 ... error 4 in main-menu[3335,...]". Timing-dependent: 0/50 boots at N = 1 and
  0/25 at N = 4 with the default resident, 4/50 with the perf log on; widening the rename gap makes QEMU fail the same way
  (6/6, control 0/6). A miss with exactly that fault counts as the installer's race, not a natemu failure
  (`natemu-raw/segv-20261105-01/`, November 5).
- **medium**, performance: installer to "Select a language" 15.9 s at N = 1 and 13.6 s at N = 4, cache off (perf chunk 7),
  against like-for-like QEMU's 6.3 s and 4.9 s (`thread=multi`, §4; R2's harness gave QEMU 7.3 s and 5.5 s); at R2 it was 67 s, mostly first-time compiles of user code at randomized addresses,
  which chunks 2 and 4 removed; the resident's own work now leads (perf plan "Measured (chunk 5)"); of the host's, full
admission of relocated hits is about 0.8 s at N = 1 (perf plan "Measured (chunk 7)", admission split).
- **medium**, performance: each vCPU's in-memory compile cache is its own (`execution::Compiler::cache`; only the disk
  cache is shared); since perf chunk 4 it keeps two generations of 262,144 answers or 128 MiB each (at most 256 MiB per
  vCPU; the installer keeps about 127 k, 80 MB per vCPU), relocating answers to other RIPs of the same half and page offset (`relocation.rs`); the disk cache
  evicts oldest-written first (`compile_cache.rs`).
- **medium**, performance: the resident's metadata arena (64 MiB, about 255 k blocks) fills on the installer: 4 whole-cache
  flushes per boot at N = 1, one per vCPU at N = 4, each followed by about 255 k blocks requested and placed again (about
  1.0 M of the boot's 1.16 M requests at N = 1, an estimated 3.5-4.5 s of 17.4 s); the code area is a third full then (perf
  plan "Measured (chunk 5)", whole-cache flushes).
- **medium**, performance: with KASLR the kernel's address is chosen once per boot and does not move within it, so the
  in-memory and in-VM caches work as with `nokaslr`; only the persistent cache misses for the kernel across boots (the
  decompressor's relocations change its bytes): the warm-cache speed-up is lost for kernel code, and the misses' lookups
  make a warm KASLR boot slower than cache off at N >= 2 (kernel only, N = 4: 4.28 against 3.78 s; §4 like-for-like table).
- **medium**, performance: uncached (UC/UC-) RAM costs about 4.3-4.8 us per access (§4, M2 addendum 2); device pages a
  host round trip per access (M3).
- **medium**, API: the library's public surface is wide (about 190 public items (functions, types, constants) across `execution`, `memory`, `vm`,
  `owners`, `irq`, `devices`, ...; `crates/host/src/lib.rs`): the test driver's needs, not a curated integration API; the
  raw-memory paths are `unsafe` or behind `testing` since the review, but the rest is not reviewed as a stable interface.
- **low**, correctness pass (after the performance phase): large-scale differential fuzzing and real workloads. The
  user-mode items of plan §5's "To verify on real x86 hardware" were run on the reference AMD machine on November 2-3 and
  agree (segment prefixes and BND on branches, Reserved-NOP cells, `0f 0d` register #UD, INCSSP, prefixed EMMS/SHA
  cells, REX before a legacy prefix; plan §5, "Hardware check"); still to run, with a kernel-mode harness: lowest-priority
  arbitration, undefined MTRR overlaps.
- **low**, boot: Linux's `quick_pit_calibrate` fails when first-time blocks fall inside its 24 us windows (§4); the evidence
  boots take the TSC rate from the profile and never need it.
- **low**, refusals: `spill_area` (a block fexc can only compile with a spill area at one instruction; never seen); the other typed
  refusals of §4.
- **low**, devices: I/O APIC vectors 0-15 dropped without the target's ESR bit, SMI/INIT modes deliver nothing (§4
  deviations; `irq.rs`, ponytail).
- **low**, lifecycle: one VM per process is what is tested (`vm.rs`: the all-ones page leaks per VM; one process GIC lock
  around every GIC call); a watchdog cancel racing a guest exit stays pending on that vCPU (harmless while a vCPU serves
  one run, `execution.rs`); cancellation waits for a compile in progress, up to the 30 s request deadline (§4).
- **low**, costs not tuned (§4): `ret` through the dispatcher, a peer's stores to the page a vCPU runs from making it
  step, a PTE stored with no pause starving the walker (contained by the watchdog), a stop's 5-8 ms scheduling tail.
- **low**, tooling: the census classifier is a Python port of the shim and drifts when the shim changes (X1c's rules
  arrived only at the R2 review); admission's per-access linear scans are quadratic in a block's sites
  (`admission/structure.rs`, ponytail).
- **low**, correctness pass (deferred by the integrity audit, November 3): models written from memory with no source snapshot
  in the repository: `tools/models/segment_model.py`, `syscall_model.py`, `delivery_model.py` and `acpi_model.py`'s
  `qemu_flags` (line 34, QEMU's FADT flags); each rule needs its SDM (or source) page, and each `QEMU_DIFFERS` entry
  (`run_slice3_vm.py`, `run_s4.py`) the SDM page that makes natemu right.
- **low**, correctness pass (same audit): the 7 i8b programs with QEMU two-NaN departures are flagged whole, so what they
  compute after their first two-NaN operation is not judged; to do: NaN-free twins against QEMU, or judging the two-NaN
  results by SDM Vol.1 Table 4-7; MIN/MAX under DAZ is untested.
- **deferred by the owner** (plan §5): RAM above 3 GiB, more than 4 vCPUs, an in-VM AES RNG (RDRAND/RDSEED refills exit
  to the host), 1 GiB pages, PCID, LA57, TSC-deadline, CPU offlining; x87 at reduced precision (owner direction).

## 7. Open items before or during integration

- *Resolved after the review:* the census (plan §4; `natemu-raw/kernel-census-r2-20261102-01/`): every refused form is
  implemented or a typed refusal; the R2 review's findings (plan, Built (R2 review fixes)).
- *Resolved after R2:* the dangling interrupt-controller pointer in the library boot entry (§4, "Blocking defect found
  at R2 (resolved)"): fixed, `run_boot_smoke.py` in the standing suite, the installer boots rerun.
- P1's outcome and P2's options A/B: owner decision 6 (November 2, plan §5).
- Everything else known: §6a (owner decision 8).
- The independent review of the whole path (the plan's R2 row) is the orchestrator's.
