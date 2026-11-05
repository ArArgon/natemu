//! Test driver: run x86-64 program descriptions in the VM, one per vCPU.
//!
//! Fixture setup (owned here, not by the emulator): user-mode 4-level page
//! tables mapping exactly the pages of the code and the memory regions, the
//! bytes, and the initial architectural state. A vCPU's program ends where the
//! body holding its start RIP ends: that RIP is its stop address. Prints the final
//! state as JSON. An optional block size limit makes every program run as several blocks;
//! `--code-cache BYTES` sets each code cache's capacity and `--watchdog-ms MS`
//! the watchdog (default 10 s). M1: `--ram BYTES` is the VM's guest RAM (a 2 MiB multiple to 3 GiB; default 2 GiB, or
//! `NATEMU_RAM`) and `--place GPA` the guest-physical page the driver places tables and pages from, upwards (default
//! 0x1000, or `NATEMU_PLACE`); the environment defaults let a suite's runner run unchanged at another size or placement. After the run each vCPU's cache link points are
//! self-checked ([`execution::check_links`]). Run-time defence checks, which corrupt what admission or the resident
//! would never produce, on vCPU `--tamper-vcpu V` (default 0): `--forge-exit` makes every literal exit move another RIP than its link
//! point's target into x0, the last word of its move altered (the resident refuses the reply: `bad_reply`); `--tamper KIND` corrupts the cache when the
//! next block will not fit and the resident is about to flush (KIND `offset`: a link point outside the code, so the reply
//! is refused; `word`: a linked point's branch; `record`: a link record),
//! and the unlink must halt (`bad_link`); `--tamper-after KIND` (not `offset`) corrupts after the run, and the host's
//! self-check must refuse (`links_check`); `--forge-stop` writes the stop word (`exec::STOP_WORD`) over the first
//! block's entry, a stop nobody armed (the resident must halt: `bad_stop`); `--forge-entry W[,W..]` writes those A64
//! words there instead (generated code runs at EL0, so a system instruction must halt `trapped` and an access to the
//! private window `private_access`); `--tamper target` changes a link point's target alone (its move unchanged: the reply
//! is refused, `bad_reply`); `--tamper armed` records an armed stop at a
//! word that does not hold the stop word (the next resident entry must halt: `bad_stop`); `--tamper dispatch` names the
//! first block's entry as its computed exit (the reply is refused: `bad_reply`); `--tamper system` makes the first system reply's decode malformed (the resident
//! must refuse it: `bad_reply`); `--tamper next-page` answers a block request with [`exec::REPLY_NEXT_PAGE`] twice (the
//! second after the next page was walked: `bad_reply`); `--tamper site` moves the native offsets of the first block with
//! a load or store site off the instruction grid (its first abort is at no site: `unknown_abort`), `--tamper site-kind`
//! makes its load and store sites claim to be guard stubs (a data abort there: `site_mismatch`); `--tamper mmio` makes the first
//! completed device access's reply status unknown (`bad_reply`, nothing committed). Containment:
//! `--ignore-kicks` leaves the vCPU's doorbells disabled and its watchdog off (it never stops or drains while in linked
//! code, a peer that never answers, so a peer waiting for its shadow reset is contained by that peer's watchdog);
//! `--kill-compiler N` kills its `fexc --serve` before the request after its first N
//! (the run fails with the server's exit), `--stall-compiler N` stops it there (SIGSTOP: alive, never answering; the
//! request's deadline, `--compile-deadline-ms`, default 30 s, kills it and fails the run). Each run ends with a `teardown:` line on stderr, written after the VM is
//! destroyed, success or failure: vCPUs joined and destroyed, the VM's destroy status, and whether any child process
//! (compile server) is left, alive or unreaped.
//!
//! L3: the VM has COM1, a 16550A UART at ports 3F8H-3FFH (`uart::Uart`), and no other port device. An unassigned port
//! reads all-ones at its width and ignores writes; an unassigned page is shadowed read-only onto the all-ones page at its
//! first read (every read form then runs natively) and ignores MOV stores; `--strict-unassigned` makes such accesses typed
//! stops (`unsupported_port`; `unsupported_mmio` status `unassigned`). `--device-at GPA` (repeatable) registers a test
//! device (`test_devices::TestDevice`) at that page, outside guest RAM, as an `mmio` item does without mapping it. Once any port
//! access, unassigned-page access or UART output happened, the output gains `ports` (per port touched: reads, writes,
//! unassigned), `unassigned_pages` (per page: reads = shadow fills, writes) and `uart` (the bytes transmitted, hex).
//!
//! L4a: the VM has the I/O APIC at 0xFEC00000 and the 8259 pair with its ELCR (`irq`), the UART's IRQ4 wired into both.
//! `--irq-source PORT` registers the asynchronous test source (`test_devices::TestSource`) on PORT..PORT+7: a 32-bit OUT at PORT
//! asks a host thread to pulse (bit 8 clear) or assert (bit 8 set) line bits 7:0 after bits 31:16 microseconds, an OUT at
//! PORT+4 deasserts a line at once; a request vetoes ending halted vCPUs until it fired (`--irq-source-no-veto`: not, the
//! negative control of that rule).
//!
//! L4b/L5: the PIT, the RTC and the ACPI PM block (`clock::install`); `--lost-ticks pit=reinject|discard,rtc=..,lapic=..`
//! sets the lost-tick policies of the periodic sources (default `clock::LostTicks::DEFAULT`: PIT reinject, RTC and LAPIC
//! discard).
//!
//! X2: with `--kick-events E[,E..]` (vectors 16..255 or `nmi`) each kick first posts one of them, chosen at random, so
//! events arrive at random times. A halted vCPU (HLT) can be woken only by an event: once every kicker has ended and
//! every vCPU is halted or ended, the driver asks each halted one to end (`stop` `resident`, reason `halted`).
//!
//! Precise stops: `--kick-vcpu V` (repeatable) runs a host kicker thread per listed vCPU that rings its doorbell
//! `--kicks M` times (default 10,000), each after the previous kick was acknowledged and a stop taken (or the vCPU ended) and
//! `--kick-every-us P` (default 0) more, plus a random 0..`--kick-jitter-us J` (default 0); kicks while the vCPU is in the host or the resident are part of the stream.
//! Each vCPU's output has its `kicks` accounting: host kicks issued (of which found it in the host), its doorbell still
//! pending at its end, the resident's doorbells (IRQs, of which SGIs from peers, and those merged into an armed stop),
//! its stops by place (at a block entry, in the dispatcher or L1, at an armed exit, at a resident entry), SGIs it sent,
//! the mean acknowledge (IAR to EOI) and SGI send costs, and the latencies from each ring to the resident's stop.
//!
//! vCPUs run concurrently over shared guest RAM, each until its own stop. A vCPU that stops for any other reason than
//! reaching its end, a guest fault, code it cannot compile, a split lock or a refused device access (`unsupported_atomic`,
//! `unsupported_mmio`: precise stops before the instruction, like a guest fault) cancels the others (another resident halt, the watchdog).
//!
//! Description (text, one item per line). State items: rip ADDR | rflags VALUE | gpr V0 .. V15
//!   | cpl 0|3 (default 3) | wp 0|1 (CR0.WP, default 1) | smep 0|1 (CR4.SMEP, default 0) | nxe 0|1 (EFER.NXE, default 1)
//!   | fsbase ADDR | gsbase ADDR (FS.base, GS.base, default 0, canonical; no x86 instruction changes them yet)
//!   | sse 0|1 (CR4.OSFXSR and OSXMMEXCPT, default 0) | xmm N LO HI (XMMn's quadwords) | mxcsr VALUE (default 0x1f80; I5c:
//!     any masks) (I5a; the report lists `xmm` as 32 quadwords and `mxcsr`) | ne 0|1 (I5c: CR0.NE, x87 errors as #MF, as
//!     Linux sets it; default 0)
//!   | xcr0 1|3|7 (I5b: CR4.OSXSAVE set and XCR0 this value, as Linux leaves user mode; default OSXSAVE clear, XCR0 1)
//!   | switch ADDR 0|3 (repeatable: the one-byte instruction at ADDR, a `nop`, runs as a change of the CPL to the value,
//!     the resident's general CPL change; no x86 instruction changes the CPL yet. Blocks end before it.)
//!   | if 0|1 (RFLAGS.IF at the start, default 1; RFLAGS is the `rflags` value with bit 1 and this)
//!   | mailbox GPA (X4: the vCPU starts as an AP waiting on the ACPI wakeup mailbox at guest-physical GPA, page-aligned;
//!     its state is the firmware's wake state (`boot::ap_wake`: CPL 0, IF = 0, EFER.NXE, no IDT, GPRs 0) over the
//!     section's paging and segments, RIP replaced by the mailbox's wakeup vector when a Wakeup command
//!     for its APIC ID (its vCPU number) arrives; the report's `stats.mailbox` is [GPA still waited on (0: woken),
//!     sleeps while waiting, the guest TSC at the acknowledgement or 0, us spent marking the page])
//!   | svr VALUE (X3: the local APIC's spurious-interrupt vector register at the start, default its reset value 0xff,
//!     software disabled; e.g. 0x1ff lets `post` items reach the core without guest APIC set-up)
//!   | post ADDR VECTOR | nmi ADDR (X2 event sources, repeatable: the one-byte instruction at ADDR, a `nop`, makes the host
//!     post the external interrupt VECTOR (16..255) or an NMI into the vCPU's event record and ring its doorbell; the
//!     vCPU takes it when eligible. Blocks end before it.)
//! CR0 is PE, ET, PG and WP as set, CR4 PAE and SMEP as set, EFER LME, LMA and NXE as set; CPL0 code may change them
//! with system instructions (the resident's transactions).
//! Address-space items: code HEX (a body at the section's rip) | body ADDR HEX | memory ADDR HEX
//!   | identity   (every page maps to the guest-physical page of its own address, inside guest RAM, outside the legacy hole; no `large`)
//!   | region ADDR SIZE   (zero-filled writable pages covering the byte range; eager: guest RAM bounds the total)
//!   | large ADDR   (the 2 MiB-aligned range holding ADDR is one present 2 MiB leaf (PDE with PS), writable if any page
//!     of the space in it is, not executable unless one is code; at most one per run: guest RAM holds one such frame)
//!   | unmap ADDR | readonly ADDR   (mutations: clear P or R/W in the leaf entry of ADDR's page after setup)
//!   | mmio ADDR GPA [unassigned]   (M3: ADDR's page maps the guest-physical page GPA, above guest RAM or in the legacy hole, writable, not
//!     executable, where the driver registers a test device (`test_devices::TestDevice`, one per distinct GPA; none with
//!     `unassigned`: the unassigned rule then answers, L3); `memory` items on that page preload
//!     its bytes and are reported from it; the mutations below apply to it. The output gains `devices` (each test
//!     device's per-vCPU reads and writes, order errors and access log) and per vCPU `mmio` (device accesses completed,
//!     the last status: `done` or the refusal). Not in identity spaces.)
//!   | entry ADDR LEVEL set|clear BITS   (mutation: set or clear BITS in the paging-structure entry of ADDR's walk at
//!     LEVEL, 4 = PML4E .. 1 = PTE, after setup; mutations apply in order, and the levels above must be present).
//! Every entry is present and user; leaves of code pages are read-only and executable, the others writable and
//! (with NXE) not executable; the space's tables follow the NXE of the first vCPU using it.
//! The resident's walker refuses what it does not model (a refusal is a halt, never a wrong result): PS in a PML4E/PDPTE is
//! reserved (no 1 GiB pages); the A bit of a non-leaf entry is set but never compared. PWT, PCD and PAT bits (entry bits
//! 3, 4; bit 7 of a PTE; bit 12 of a 2 MiB leaf) select the page's memory type (M2); UC RAM's accesses run their instruction alone
//! (strong ordering), and the output's `memory_types` counts data shadows by type, `uncached` the UC accesses.
//! Without `cpu` lines the description is one program on one vCPU (output: one state object). Each `cpu` line starts
//! the next vCPU's section (vCPUs 0.., output `{"cpus": [...]}`); address-space items before the first `cpu` line
//! form the shared address space, which a section uses unless it has address-space items of its own (then it gets
//! its own tables and guest-physical pages, disjoint from every other space's).
use super::super::*;
use crate::execution::{self, Area, Compiler, Stop};
use crate::owners;
use natemu_abi::execution::{self as exec, Cpu, State, cache};
use natemu_abi::guest_memory::GuestRam;
use natemu_abi::paging::Fault;
use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering::SeqCst};
use std::sync::{Arc, Mutex};
use super::test_devices::TestDevice;
use crate::devices::Devices;

/// One address space's contents.
#[derive(Default, Debug, PartialEq)]
struct Space {
    /// Code bytes at an address (`None`: at the section's rip).
    bodies: Vec<(Option<u64>, Vec<u8>)>,
    memory: Vec<(u64, Vec<u8>)>,
    /// Zero-filled writable ranges (address, length).
    regions: Vec<(u64, u64)>,
    /// 2 MiB-aligned linear ranges mapped by one PDE leaf.
    large: Vec<u64>,
    /// Entry mutations after setup, in order.
    edits: Vec<Edit>,
    /// Pages map to the guest-physical page of the same address.
    identity: bool,
    /// M3: (linear page, device page, a test device there) of `mmio` items.
    mmio: Vec<(u64, u64, bool)>,
}
/// Set, then clear, bits of the entry at `level` (`None`: the leaf) of `va`'s walk.
#[derive(Debug, PartialEq)]
struct Edit {
    va: u64,
    level: Option<u32>,
    set: u64,
    clear: u64,
}
/// One vCPU's initial state and the address space it runs in (an index into `Description::spaces`).
#[derive(Debug, PartialEq)]
struct Section {
    rip: u64,
    rflags: u64,
    gpr: [u64; 16],
    cpl: u64,
    wp: bool,
    smep: bool,
    nxe: bool,
    fs_base: u64,
    gs_base: u64,
    /// I5a: CR4.OSFXSR and OSXMMEXCPT (SSE enabled, as Linux sets them), the XMM registers and MXCSR.
    sse: bool,
    /// S3: CR4.FSGSBASE (RDFSBASE..WRGSBASE enabled, as Linux sets it).
    fsgsbase: bool,
    /// I5c: CR0.NE.
    ne: bool,
    xmm: [[u64; 2]; 16],
    mxcsr: u64,
    /// I5b: XCR0 with CR4.OSXSAVE, or 0 (OSXSAVE clear, XCR0 at reset).
    xcr0: u64,
    space: usize,
    /// `switch` items: (RIP, CPL).
    switches: Vec<(u64, u64)>,
    /// `post`/`nmi` items (X2 event sources): (RIP, vector or `execution::NMI`).
    posts: Vec<(u64, u64)>,
    /// RFLAGS.IF at the start (`if 0|1`, default 1).
    interrupts: bool,
    /// X3: the local APIC's SVR at the start (`svr VALUE`, default the reset value 0xff: software disabled).
    svr: u64,
    /// X4: the guest-physical ACPI mailbox the vCPU waits on before it runs (`mailbox GPA`), else 0.
    mailbox: u64,
}
#[derive(Debug, PartialEq)]
struct Description {
    /// Space 0 is the shared one.
    spaces: Vec<Space>,
    cpus: Vec<Section>,
    /// Written with `cpu` lines.
    sections: bool,
}

fn number(text: &str) -> Result<u64> {
    let digits = text.strip_prefix("0x").unwrap_or(text);
    u64::from_str_radix(digits, 16).map_err(|_| format!("bad number {text}"))
}
fn hex(text: &str) -> Result<Vec<u8>> {
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(text.get(i..i + 2).ok_or("odd hex")?, 16).map_err(|_| "bad hex".into()))
        .collect()
}
fn parse(text: &str) -> Result<Description> {
    let mut chunks = vec![vec![]];
    for line in text.lines().filter(|l| !l.trim().is_empty()) {
        match line.split_whitespace().collect::<Vec<_>>()[..] {
            ["cpu"] => chunks.push(vec![]),
            _ => chunks.last_mut().unwrap().push(line),
        }
    }
    let new = |space| Section { rip: 0, rflags: 0, gpr: [0; 16], cpl: 3, wp: true, smep: false, nxe: true, fs_base: 0, gs_base: 0, sse: false, fsgsbase: false, ne: false, xmm: [[0; 2]; 16], mxcsr: exec::MXCSR_RESET, xcr0: 0, space, switches: vec![], posts: vec![], interrupts: true, svr: natemu_abi::lapic::SVR_RESET, mailbox: 0 };
    let mut d = Description { spaces: vec![Space::default()], cpus: vec![], sections: chunks.len() > 1 };
    if d.sections {
        items(&chunks[0], &mut d.spaces[0], None)?;
        if d.spaces[0].bodies.iter().any(|(at, _)| at.is_none()) {
            return Err("code in the shared space: write it as body ADDR HEX".into());
        }
        for chunk in &chunks[1..] {
            let (mut own, mut cpu) = (Space::default(), new(0));
            items(chunk, &mut own, Some(&mut cpu))?;
            if own != Space::default() {
                cpu.space = d.spaces.len();
                d.spaces.push(own);
            }
            d.cpus.push(cpu);
        }
    } else {
        let mut cpu = new(0);
        items(&chunks[0], &mut d.spaces[0], Some(&mut cpu))?;
        d.cpus.push(cpu);
    }
    for c in &d.cpus {
        if bodies(&d.spaces[c.space], c.rip).all(|(_, code)| code.is_empty()) {
            return Err("no code".into());
        }
    }
    Ok(d)
}
fn state<'a>(cpu: &'a mut Option<&mut Section>, line: &str) -> Result<&'a mut Section> {
    cpu.as_deref_mut().ok_or(format!("state line outside a cpu section: {line}"))
}
/// Reads address-space items into `space` and state items into `cpu` (refused without one).
fn items(lines: &[&str], space: &mut Space, mut cpu: Option<&mut Section>) -> Result<()> {
    for line in lines {
        let words: Vec<_> = line.split_whitespace().collect();
        match (words[0], &words[1..]) {
            ("rip", [v]) => state(&mut cpu, line)?.rip = number(v)?,
            ("rflags", [v]) => state(&mut cpu, line)?.rflags = number(v)?,
            ("gpr", values) if values.len() == 16 => {
                for (r, v) in state(&mut cpu, line)?.gpr.iter_mut().zip(values) {
                    *r = number(v)?;
                }
            }
            ("fsbase" | "gsbase", [v]) => {
                let (c, base) = (state(&mut cpu, line)?, number(v)?);
                if !natemu_abi::paging::canonical(base) {
                    return Err(format!("non-canonical segment base: {line}"));
                }
                *if words[0] == "fsbase" { &mut c.fs_base } else { &mut c.gs_base } = base;
            }
            ("xmm", [n, lo, hi]) if number(n)? < 16 => state(&mut cpu, line)?.xmm[number(n)? as usize] = [number(lo)?, number(hi)?],
            ("mxcsr", [v]) if number(v)? & !exec::MXCSR_MASK == 0 => {
                state(&mut cpu, line)?.mxcsr = number(v)?
            }
            ("xcr0", [v]) if natemu_abi::cpu_profile::XCR0_VALID.contains(&number(v)?) => state(&mut cpu, line)?.xcr0 = number(v)?,
            ("cpl", ["0"]) => state(&mut cpu, line)?.cpl = 0,
            ("cpl", ["3"]) => state(&mut cpu, line)?.cpl = 3,
            ("switch", [a, c @ ("0" | "3")]) => state(&mut cpu, line)?.switches.push((number(a)?, c.parse().unwrap())),
            ("post", [a, v]) if (16..256).contains(&number(v)?) => state(&mut cpu, line)?.posts.push((number(a)?, number(v)?)),
            ("nmi", [a]) => state(&mut cpu, line)?.posts.push((number(a)?, execution::NMI)),
            ("if", [v @ ("0" | "1")]) => state(&mut cpu, line)?.interrupts = *v == "1",
            ("svr", [v]) if number(v)? & !natemu_abi::lapic::SVR_WRITABLE == 0 => state(&mut cpu, line)?.svr = number(v)?,
            ("mailbox", [g]) if number(g)? % 4096 == 0 && number(g)? != 0 => state(&mut cpu, line)?.mailbox = number(g)?,
            ("wp" | "smep" | "nxe" | "sse" | "fsgsbase" | "ne", [v @ ("0" | "1")]) => {
                let c = state(&mut cpu, line)?;
                *match words[0] {
                    "wp" => &mut c.wp,
                    "smep" => &mut c.smep,
                    "sse" => &mut c.sse,
                    "fsgsbase" => &mut c.fsgsbase,
                    "ne" => &mut c.ne,
                    _ => &mut c.nxe,
                } = *v == "1";
            }
            ("code", [h]) => space.bodies.push((None, hex(h)?)),
            ("body", [a, h]) => space.bodies.push((Some(number(a)?), hex(h)?)),
            ("memory", [a, h]) => space.memory.push((number(a)?, hex(h)?)),
            ("region", [a, n]) if number(n)? > 0 => space.regions.push((number(a)?, number(n)?)),
            ("large", [a]) => space.large.push(number(a)? & !(LARGE - 1)),
            ("identity", []) => space.identity = true,
            ("mmio", [a, g, rest @ ..]) if number(g)? % 4096 == 0 && matches!(rest, [] | ["unassigned"]) => {
                space.mmio.push((number(a)? & !4095, number(g)?, rest.is_empty()))
            }
            ("unmap", [a]) => space.edits.push(Edit { va: number(a)?, level: None, set: 0, clear: PRESENT }),
            ("readonly", [a]) => space.edits.push(Edit { va: number(a)?, level: None, set: 0, clear: WRITABLE }),
            ("entry", [a, l @ ("1" | "2" | "3" | "4"), op @ ("set" | "clear"), bits]) => {
                let (va, bits, level) = (number(a)?, number(bits)?, Some(l.parse().unwrap()));
                let set = if *op == "set" { bits } else { 0 };
                space.edits.push(Edit { va, level, set, clear: if *op == "clear" { bits } else { 0 } });
            }
            _ => return Err(format!("bad description line: {line}")),
        }
    }
    Ok(())
}
/// The bodies of `space` with their addresses, for a vCPU starting at `rip`.
fn bodies(space: &Space, rip: u64) -> impl Iterator<Item = (u64, &[u8])> {
    space.bodies.iter().map(move |(at, code)| (at.unwrap_or(rip), code.as_slice()))
}
/// A vCPU's stop address: the end of the body holding its start.
fn stop_at(space: &Space, rip: u64) -> Result<u64> {
    bodies(space, rip)
        .find(|&(at, code)| (at..at + code.len() as u64).contains(&rip))
        .map(|(at, code)| at + code.len() as u64)
        .ok_or_else(|| format!("rip {rip:#x} is in no body"))
}

const PRESENT: u64 = 1;
const WRITABLE: u64 = 2;
const USER: u64 = 4;
const LARGE_PAGE: u64 = 1 << 7;
const NO_EXECUTE: u64 = 1 << 63;
const LARGE: u64 = 0x20_0000;
const ADDRESS: u64 = natemu_abi::paging::PA_MASK;

/// Guest-physical page allocation (shared by every address space): 4 KiB pages upwards from `next`, 2 MiB frames
/// downwards from `top`, never one of the `reserved` pages (identity-mapped pages).
struct Builder {
    ram: GuestRam,
    next: u64,
    top: u64,
    reserved: std::collections::BTreeSet<u64>,
}
impl Builder {
    fn page(&mut self) -> Result<u64> {
        while self.reserved.contains(&self.next) || exec::LEGACY_HOLE.contains(&self.next) {
            self.next += 4096;
        }
        let gpa = self.next;
        if gpa + 4096 > self.top {
            return Err("guest RAM exhausted".into());
        }
        self.next += 4096;
        Ok(gpa)
    }
    fn large(&mut self) -> Result<u64> {
        let gpa = self.top.saturating_sub(LARGE) & !(LARGE - 1);
        if gpa == 0 || gpa < self.next {
            return Err("guest RAM exhausted (2 MiB frame)".into());
        }
        self.top = gpa;
        Ok(gpa)
    }
}
/// One address space: 4-level user mappings.
struct Tables {
    ram: GuestRam,
    cr3: u64,
    /// M3: test devices by guest-physical page (`mmio` items): `copy_in`/`copy_out` reach their bytes.
    devices: BTreeMap<u64, Arc<Mutex<TestDevice>>>,
    /// Linear page -> (physical page, leaf entry address).
    pages: BTreeMap<u64, (u64, u64)>,
}
impl Tables {
    fn new(b: &mut Builder) -> Result<Self> {
        Ok(Self { ram: b.ram, cr3: b.page()?, pages: BTreeMap::new(), devices: BTreeMap::new() })
    }
    /// The address of the entry at `level` of `va`'s walk, creating missing tables above it with a builder (without
    /// one, a non-present entry above it is an error: mutations never create tables).
    fn entry(&mut self, mut b: Option<&mut Builder>, va: u64, level: u32) -> Result<u64> {
        let mut table = self.cr3;
        for l in (level + 1..=4).rev() {
            let at = table + ((va >> (12 + 9 * (l - 1))) & 511) * 8;
            let entry = self.ram.read::<u64>(at).unwrap();
            table = if entry & PRESENT == 0 {
                let next = b.as_deref_mut().ok_or(format!("{va:#x}: no level-{level} entry (level {l} is not present)"))?.page()?;
                assert!(self.ram.write(at, next | PRESENT | WRITABLE | USER));
                next
            } else if l == 2 && entry & LARGE_PAGE != 0 {
                return Err(format!("{va:#x} is in a 2 MiB page: no level-{level} entry"));
            } else {
                entry & ADDRESS
            };
        }
        Ok(table + ((va >> (12 + 9 * (level - 1))) & 511) * 8)
    }
    /// Maps `va` to a new page, or (`identity`) to its own address.
    fn map(&mut self, b: &mut Builder, va: u64, flags: u64, identity: bool) -> Result<()> {
        let gpa = if identity { va } else { b.page()? };
        self.map_at(b, va, gpa, USER | flags)
    }
    /// Maps `va` to guest-physical page `gpa` with the leaf's `flags` (USER among them or not).
    fn map_at(&mut self, b: &mut Builder, va: u64, gpa: u64, flags: u64) -> Result<()> {
        let leaf = self.entry(Some(b), va, 1)?;
        assert!(self.ram.write(leaf, gpa | PRESENT | flags));
        self.pages.insert(va, (gpa, leaf));
        Ok(())
    }
    /// Maps the 2 MiB range at `va` with one PDE leaf; its `pages` (those the space uses) are listed.
    fn map_large(&mut self, b: &mut Builder, va: u64, flags: u64, pages: impl Iterator<Item = u64>) -> Result<()> {
        let pde = self.entry(Some(b), va, 2)?;
        let gpa = b.large()?;
        assert!(self.ram.write(pde, gpa | PRESENT | USER | LARGE_PAGE | flags));
        for page in pages {
            self.pages.insert(page, (gpa + (page - va), pde));
        }
        Ok(())
    }
    /// Physical address of mapped linear `va`.
    fn physical(&self, va: u64) -> u64 {
        self.pages[&(va & !4095)].0 | (va & 4095)
    }
    fn copy_in(&self, va: u64, bytes: &[u8]) {
        for (i, &b) in bytes.iter().enumerate() {
            let gpa = self.physical(va + i as u64);
            match self.devices.get(&(gpa & !4095)) {
                Some(d) => d.lock().unwrap().bytes[(gpa & 4095) as usize] = b,
                None => assert!(self.ram.write(gpa, b)),
            }
        }
    }
    fn copy_out(&self, va: u64, length: usize) -> Vec<u8> {
        let byte = |gpa: u64| match self.devices.get(&(gpa & !4095)) {
            Some(d) => d.lock().unwrap().bytes[(gpa & 4095) as usize],
            None => self.ram.read::<u8>(gpa).unwrap(),
        };
        (0..length as u64).map(|i| byte(self.physical(va + i))).collect()
    }
}
/// Builds `space` (for a vCPU starting at `rip` when its bodies sit at the rip): code pages are read-only and
/// executable, region pages writable and not executable (any region covering a code page leaves it executable;
/// without `nxe` nothing is marked not executable).
/// With `identity`, every page maps to the guest-physical page of its own address (the space's guest code can then
/// build page tables of its own: its physical addresses are its linear ones); no `large` pages.
fn build(b: &mut Builder, space: &Space, rip: u64, nxe: bool, devices: &BTreeMap<u64, Arc<Mutex<TestDevice>>>) -> Result<Tables> {
    let no_execute = if nxe { NO_EXECUTE } else { 0 };
    if space.identity && !space.large.is_empty() {
        return Err("identity space with a large page".into());
    }
    if space.identity && !space.mmio.is_empty() {
        return Err("identity space with an mmio page".into());
    }
    let mut flags = BTreeMap::new();
    let code_pages: Vec<u64> = bodies(space, rip).flat_map(|(at, code)| page_span(at, code.len())).collect();
    for &page in &code_pages {
        flags.insert(page, 0);
    }
    let ranges = space.memory.iter().map(|(a, d)| (*a, d.len() as u64)).chain(space.regions.iter().copied());
    for (address, length) in ranges {
        if length > b.top {
            return Err("guest RAM exhausted".into());
        }
        for page in page_span(address, length as usize) {
            flags.insert(page, WRITABLE | if code_pages.contains(&page) { 0 } else { no_execute });
        }
    }
    let mut t = Tables::new(b)?;
    t.devices = devices.clone();
    for &(va, gpa, _) in &space.mmio {
        flags.remove(&va);
        t.map_at(b, va, gpa, USER | WRITABLE | no_execute)?;
    }
    for &region in &space.large {
        let inside: Vec<(u64, u64)> = flags.range(region..region + LARGE).map(|(&p, &f)| (p, f)).collect();
        let writable = inside.iter().fold(0, |w, &(_, f)| w | f & WRITABLE);
        let code = inside.iter().any(|(p, _)| code_pages.contains(p));
        t.map_large(b, region, writable | if code { 0 } else { no_execute }, inside.iter().map(|&(p, _)| p))?;
    }
    for (&page, &f) in &flags {
        if !space.large.contains(&(page & !(LARGE - 1))) {
            t.map(b, page, f, space.identity)?;
        }
    }
    for (at, code) in bodies(space, rip) {
        t.copy_in(at, code);
    }
    for (address, data) in &space.memory {
        t.copy_in(*address, data);
    }
    for e in &space.edits {
        let at = match e.level {
            None => t.pages.get(&(e.va & !4095)).ok_or(format!("{:#x} is not mapped", e.va))?.1,
            Some(level) => t.entry(None, e.va, level)?,
        };
        let entry = t.ram.read::<u64>(at).unwrap();
        assert!(t.ram.write(at, (entry | e.set) & !e.clear));
    }
    Ok(t)
}
/// The pages an identity space maps (as [`build`]: its code, memory and regions).
fn identity_pages(space: &Space, rip: u64) -> Vec<u64> {
    let code = bodies(space, rip).map(|(at, code)| (at, code.len()));
    let data = space.memory.iter().map(|(a, d)| (*a, d.len())).chain(space.regions.iter().map(|&(a, n)| (a, n as usize)));
    code.chain(data).flat_map(|(at, n)| page_span(at, n)).collect()
}
fn page_span(start: u64, length: usize) -> impl Iterator<Item = u64> {
    (start & !4095..start + length as u64).step_by(4096)
}

/// The driver's logging IDT (X1a; boot-features plan, X1a "Old corpora"). Every space gets, at `log_base`, a GDT
/// (QEMU stub's layout: 0x08 code64 DPL 0, 0x10 data DPL 0, 0x18 data DPL 3, 0x20 code64 DPL 3, 0x28 code32 DPL 0, the
/// TSS of vCPU v at 0x30 + 16v; accessed bits preset), the TSSs (RSP0 and IST1 = vCPU v's own stack page), and an IDT of
/// 256 present 64-bit interrupt gates (CS 0x08, DPL 3 so INT n logs at any CPL, IST 1) whose handler for vector n is
/// `log_base + 0x2000 + 16n`. Each vCPU starts with GDTR, IDTR and TR (hidden part, busy) naming them. The handler page is
/// never mapped or fetched: reaching it ends the run (`Stop::Hooked(LOGGED)`, before any fetch) and the report reads the frame
/// from the vCPU's stack, as a handler that logs and halts would. A program that loads its own GDT, IDT, TSS or tables
/// owns delivery from then on. Pages: GDT and TSSs at +0, IDT at +0x1000, stacks at +0x3000 + 0x1000v (vCPUs 0..3), all
/// writable, supervisor-only (CPL 3 code cannot change them), not executable; at `LOG_BASE` (its own PML4 entry) in driver-built spaces, identity-mapped at
/// `LOG_IDENTITY` in identity spaces (inside the ranges `tools/bench/smp/programs.py` `sys_tables` maps, so programs
/// that load those tables keep them).
const LOG_BASE: u64 = 0xffff_ff00_0000_0000;
const LOG_IDENTITY: u64 = 0x3e_8000;
const LOG_PAGES: [u64; 6] = [0, 0x1000, 0x3000, 0x4000, 0x5000, 0x6000];
const LOG_HANDLERS: u64 = 0x2000;
const LOG_TSS: u64 = 0x800;
fn log_base(identity: bool) -> u64 {
    if identity { LOG_IDENTITY } else { LOG_BASE }
}
/// The GDT/TSS page and the IDT page at `base`.
fn log_images(base: u64) -> [Vec<u8>; 2] {
    let mut gdt: Vec<u64> = vec![0, 0x00af_9b00_0000_ffff, 0x00cf_9300_0000_ffff, 0x00cf_f300_0000_ffff, 0x00af_fb00_0000_ffff, 0x00cf_9b00_0000_ffff];
    let mut page = vec![0u8; 4096];
    for v in 0..cache::MAX_VCPUS as u64 {
        let tss = base + LOG_TSS + 0x80 * v;
        gdt.push(0x67 | (tss & 0xff_ffff) << 16 | 0x8b << 40 | (tss >> 24 & 0xff) << 56);
        gdt.push(tss >> 32);
        let stack = (base + 0x4000 + 0x1000 * v).to_le_bytes();
        let at = (LOG_TSS + 0x80 * v) as usize;
        page[at + 4..at + 12].copy_from_slice(&stack); // RSP0
        page[at + 0x24..at + 0x2c].copy_from_slice(&stack); // IST1
    }
    for (k, d) in gdt.iter().enumerate() {
        page[8 * k..8 * k + 8].copy_from_slice(&d.to_le_bytes());
    }
    let mut idt = vec![0u8; 4096];
    for n in 0..256u64 {
        let offset = base + LOG_HANDLERS + 16 * n;
        let lo = offset & 0xffff | 0x08 << 16 | 1 << 32 | 0xe << 40 | 3 << 45 | 1 << 47 | (offset >> 16 & 0xffff) << 48;
        idt[16 * n as usize..16 * n as usize + 8].copy_from_slice(&lo.to_le_bytes());
        idt[16 * n as usize + 8..16 * n as usize + 16].copy_from_slice(&(offset >> 32).to_le_bytes());
    }
    [page, idt]
}

const USAGE: &str = "usage: natemu-host RUNTIME.elf run-program FEXC PROGRAM.txt WORKDIR [MAX_BLOCK_INSTRUCTIONS] \
                     [--code-shape] [--check-reuse] [--code-cache BYTES] [--ram BYTES] [--place GPA] [--watchdog-ms MS] [--tamper-vcpu V] [--forge-exit] [--tamper offset|word|record|system] [--tamper-after word|record] \
                     [--forge-stop] [--forge-entry HEX[,HEX]...] [--kick-vcpu V]... [--kicks M] [--kick-every-us P] [--kick-jitter-us J] [--kick-events E,..] [--tamper next-page|site|site-kind|armed|dispatch|target] [--ignore-kicks] [--kill-compiler N] [--stall-compiler N] [--compile-deadline-ms MS] [--strict-unassigned] [--device-at GPA]... [--irq-source PORT] [--irq-source-no-veto]";

/// Every literal exit's RIP move puts another RIP in x0: bit 0 of the immediate of its last word (the one before its
/// `svc #LINK`) flips. A one-word move would change the link point's first word itself, which the host's self-check
/// refuses after the run; the RIPs of test programs need two words.
fn forge_exit(object: &mut [u8]) {
    let h: natemu_abi::code_object::Header = unsafe { std::ptr::read_unaligned(object.as_ptr().cast()) };
    let link = (0xd400_0001u32 | u32::from(exec::svc::LINK) << 5).to_le_bytes();
    let code = h.code_offset as usize;
    for at in (code + exec::ENTRY_OFFSET as usize + 4..code + h.code_region_end as usize).step_by(4) {
        if object[at..at + 4] == link {
            object[at - 4] ^= 1 << 5;
        }
    }
}

/// The first link record and its source block, if the vCPU has linked anything.
fn first_link(area: Area) -> Option<(cache::Block, cache::LinkRecord)> {
    let control: cache::Control = area.read_record(cache::LOOKUP_OFFSET + cache::CONTROL);
    (control.links > 0).then(|| {
        let r = execution::link_record(area, 0);
        (execution::placed_blocks(area)[r.source as usize], r)
    })
}
/// The resident is about to flush: the staged block does not fit (as `cache::place` computes) and links exist.
fn flushing_next(area: Area) -> bool {
    let control: cache::Control = area.read_record(cache::LOOKUP_OFFSET + cache::CONTROL);
    let h: natemu_abi::code_object::Header = area.read_record(exec::STAGING_OFFSET);
    control.links > 0 && control.code_used.next_multiple_of(64) + u64::from(h.code_size) > control.code_bytes
}
#[derive(Clone, Copy)]
enum Corrupt {
    Word,
    Record,
}
/// Corrupts the first linked point or its record; false when nothing is linked yet.
fn corrupt(area: Area, kind: Corrupt) -> bool {
    let Some((b, r)) = first_link(area) else { return false };
    let at = cache::CODE_OFFSET + b.code as usize + r.offset as usize;
    match kind {
        Corrupt::Word => area.copy(at, &0x1400_0001u32.to_le_bytes()), // b .+4
        Corrupt::Record => {
            let record = cache::LinkRecord { original: r.original ^ 1 << 5, ..r };
            area.write_record(cache::LOOKUP_OFFSET + cache::LINKS, record); // another immediate
        }
    }
    true
}
fn tamper_offset(area: Area, state: &mut State) -> bool {
    if !admitted(state) {
        return false;
    }
    let h: natemu_abi::code_object::Header = area.read_record(exec::STAGING_OFFSET);
    let Some(p) = state.link_points.iter_mut().find(|p| p.offset != 0) else { return false };
    p.offset = h.code_size; // a whole exit no longer fits
    true
}
/// M3: a completed device access's reply with a status the resident does not know: it must refuse it (`bad_reply`) and
/// commit nothing.
fn tamper_mmio(area: Area, state: &mut State) -> bool {
    let mut m: exec::Mmio = area.read_record(exec::MMIO_OFFSET);
    let done = state.reply == exec::REPLY_DEVICE && m.status == exec::mmio::DONE;
    if done {
        m.status = 0x7f;
        area.write_record(exec::MMIO_OFFSET, m);
    }
    done
}
/// A system reply whose decode the resident must refuse (`bad_reply`): an instruction length of 16.
fn tamper_system(_: Area, state: &mut State) -> bool {
    let reply = state.reply == exec::REPLY_SYSTEM;
    if reply {
        state.system.length = 16;
    }
    reply
}
/// A block reply turned into a next-page reply, and the request that follows (its next page walked or its walk failed)
/// answered with one again: a second next-page reply, which the resident must refuse (`bad_reply`).
fn tamper_next_page(_: Area, state: &mut State) -> bool {
    if state.reply != exec::REPLY_ADMITTED {
        return false;
    }
    state.reply = exec::REPLY_NEXT_PAGE;
    state.fetch[1] != u64::MAX || state.fetch_fault[0] != exec::fetch::NONE
}
fn admitted(state: &State) -> bool {
    state.reply == exec::REPLY_ADMITTED
}
/// Rewrites every site of the staged block with `f`, once it has a load or store site (the block's first access to a
/// page it has no shadow of aborts there).
fn tamper_sites(area: Area, state: &State, f: fn(&mut natemu_abi::code_object::Site)) -> bool {
    use natemu_abi::code_object::{Header, Site, kind};
    // Staging holds a block only with a block reply.
    if !admitted(state) {
        return false;
    }
    let h: Header = area.read_record(exec::STAGING_OFFSET);
    let at = |i: usize| exec::STAGING_OFFSET + h.sites_offset as usize + i * size_of::<Site>();
    let sites: Vec<Site> = (0..h.sites_count as usize).map(|i| area.read_record(at(i))).collect();
    if !sites.iter().any(|s| matches!(s.kind, kind::LOAD | kind::STORE)) {
        return false;
    }
    for (i, mut site) in sites.into_iter().enumerate() {
        f(&mut site);
        area.write_record(at(i), site);
    }
    true
}
/// No site at any native PC of the block (offsets moved off the instruction grid): its first abort is `unknown_abort`.
fn tamper_site(area: Area, state: &mut State) -> bool {
    tamper_sites(area, state, |s| s.native_offset += 2)
}
/// Every load and store site claims to be a guard stub (a BRK): its first data abort is `site_mismatch`.
fn tamper_site_kind(area: Area, state: &mut State) -> bool {
    use natemu_abi::code_object::kind;
    tamper_sites(area, state, |s| {
        if matches!(s.kind, kind::LOAD | kind::STORE) {
            s.kind = kind::GUARD;
        }
    })
}
/// The words `--forge-entry` (or `--forge-stop`) puts over the first staged block's entry.
static FORGED_ENTRY: std::sync::OnceLock<Vec<u32>> = std::sync::OnceLock::new();
/// [`FORGED_ENTRY`] over the staged block's entry (admission never saw them; the block keeps its length). The stop word
/// traps with no stop armed (`bad_stop`); words the hardware refuses at EL0, or that touch the private window, show
/// that EL0 isolation is the hardware's (`trapped`, `private_access`).
fn tamper_forge_entry(area: Area, state: &mut State) -> bool {
    let words = FORGED_ENTRY.get().map_or(&[][..], Vec::as_slice);
    let h: natemu_abi::code_object::Header = area.read_record(exec::STAGING_OFFSET);
    if !admitted(state) || exec::ENTRY_OFFSET as usize + 4 * words.len() > h.code_size as usize {
        return false;
    }
    let bytes: Vec<u8> = words.iter().flat_map(|w| w.to_le_bytes()).collect();
    area.copy(exec::STAGING_OFFSET + (h.code_offset + exec::ENTRY_OFFSET) as usize, &bytes);
    true
}
/// A link point's target changed alone (its move still builds the old RIP): the reply is refused (`bad_reply`).
fn tamper_target(_: Area, state: &mut State) -> bool {
    if !admitted(state) {
        return false;
    }
    let Some(p) = state.link_points.iter_mut().find(|p| p.offset != 0) else { return false };
    p.target ^= 1 << 4;
    true
}
/// An armed stop recorded at the code area's first block word (the first block placed, the staged one at the latest),
/// which holds no stop word: the next resident entry from generated code must refuse to restore it (`bad_stop`).
fn tamper_armed(area: Area, state: &mut State) -> bool {
    let at = cache::LOOKUP_OFFSET + cache::CONTROL;
    let mut c: cache::Control = area.read_record(at);
    if !admitted(state) {
        return false;
    }
    (c.armed, c.armed_words[0]) = (1, [cache::HEAD_BYTES as u32 + exec::ENTRY_OFFSET, 0]);
    area.write_record(at, c);
    true
}
/// The staged block's entry named as its computed exit (no `b .` there): the reply is refused (`bad_reply`).
fn tamper_dispatch(_: Area, state: &mut State) -> bool {
    let reply = admitted(state);
    if reply {
        state.dispatch_exit = u64::from(exec::ENTRY_OFFSET);
    }
    reply
}
fn tamper_word(area: Area, state: &mut State) -> bool {
    admitted(state) && flushing_next(area) && corrupt(area, Corrupt::Word)
}
fn tamper_record(area: Area, state: &mut State) -> bool {
    admitted(state) && flushing_next(area) && corrupt(area, Corrupt::Record)
}

/// Signals sent to a vCPU's compile server (`--kill-compiler`, `--stall-compiler`); macOS numbers.
const SIGKILL: i32 = 9;
const SIGSTOP: i32 = 17;

/// A reply tamper: called with the vCPU stopped, the reply (`State::reply`) and its block or decode in place but not yet
/// seen by the resident, until it returns true (it corrupted something, possibly the reply kind).
type Tamper = fn(Area, &mut State) -> bool;
/// [`Stop::Hooked`]'s code for the logging IDT.
const LOGGED: u32 = 0;
/// The driver's [`execution::Hook`] for one vCPU: its program's hooks and, on the tampered vCPU, the run-time defence
/// checks.
struct ProgramHook {
    /// RIPs whose one-byte instruction is executed as a CPL change to the value ([`exec::system::SET_CPL`]).
    switches: BTreeMap<u64, u64>,
    /// X2 event sources: RIPs whose one-byte instruction (a `nop`) posts an event into the vCPU's record
    /// ([`execution::post`]: a vector, or [`execution::NMI`]) and rings its doorbell, then completes as
    /// [`exec::system::EVENT_HOOK`].
    posts: BTreeMap<u64, u64>,
    /// The logging IDT's handler range: reaching it ends the run ([`LOGGED`]) before anything there is fetched.
    handlers: std::ops::Range<u64>,
    doorbell: Doorbell,
    /// `--forge-exit`: rewrites each admitted object before it is staged ([`forge_exit`]).
    forge_exit: bool,
    /// `--kill-compiler`/`--stall-compiler`: once `.0` requests have reached the server, it gets signal `.1` before the next
    /// (SIGKILL: the server dies mid-run; SIGSTOP: it stays alive and never answers). The run must fail with the cause.
    signal_at: Option<(u64, i32)>,
    /// The translation reply tamper and the device reply tamper (M3); each disarms once it returns true.
    tamper: Option<Tamper>,
    tamper_device: Option<Tamper>,
    tampered: bool,
}
impl execution::Hook for ProgramHook {
    fn at(&mut self, area: Area, rip: u64) -> Result<Option<execution::At>> {
        if self.handlers.contains(&rip) {
            return Ok(Some(execution::At::Stop(LOGGED)));
        }
        if let Some(&cpl) = self.switches.get(&rip) {
            return Ok(Some(execution::At::System(exec::System { kind: exec::system::SET_CPL, length: 1, cr: cpl as u32, ..exec::System::NONE })));
        }
        if let Some(&event) = self.posts.get(&rip) {
            let _ = execution::post(area.memory(), area.vcpu(), event, false);
            self.doorbell.ring(area.vcpu())?;
            return Ok(Some(execution::At::System(exec::System { kind: exec::system::EVENT_HOOK, length: 1, ..exec::System::NONE })));
        }
        Ok(None)
    }
    fn boundary(&self, rip: u64) -> Option<u64> {
        use std::ops::Bound::{Excluded, Unbounded};
        let after = |m: &BTreeMap<u64, u64>| m.range((Excluded(rip), Unbounded)).next().map(|(&r, _)| r);
        after(&self.switches).into_iter().chain(after(&self.posts)).min()
    }
    fn fences(&self) -> Vec<std::ops::Range<u64>> {
        let points = self.switches.keys().chain(self.posts.keys()).map(|&r| r..r + 1);
        std::iter::once(self.handlers.clone()).chain(points).collect()
    }
    fn admitted(&mut self, object: &mut [u8]) {
        if self.forge_exit {
            forge_exit(object);
        }
    }
    fn before_compile(&mut self, compiles: u64, server: u32) {
        unsafe extern "C" {
            fn kill(pid: i32, signal: i32) -> i32;
        }
        if let Some((_, signal)) = self.signal_at.filter(|s| s.0 == compiles) {
            unsafe { kill(server as i32, signal) };
        }
    }
    fn reply(&mut self, kind: execution::ReplyKind, area: Area, state: &mut State) {
        let tamper = match kind {
            execution::ReplyKind::Translation => &mut self.tamper,
            execution::ReplyKind::Device => &mut self.tamper_device,
        };
        if tamper.is_some_and(|t| t(area, state)) {
            (*tamper, self.tampered) = (None, true);
        }
    }
}

/// What one vCPU's thread returns.
struct Ran {
    stop: Stop,
    compiles: u64,
    slowest: std::time::Duration,
    blocks: u64,
    /// The distinct RIPs of the blocks the host translated for it.
    rips: std::collections::HashSet<u64>,
    tampered: bool,
    /// K2: the host's system decodes by kind, and entropy refills.
    decodes: std::collections::BTreeMap<u32, u64>,
    entropy: u64,
}
/// L4b: a power stop's name (`pm::POWEROFF`, `pm::RESET`).
pub(crate) fn power_name(p: u8) -> &'static str {
    if p == crate::pm::RESET { "reset" } else { "poweroff" }
}
/// A vCPU's own end (it parks; peers run on). Any other stop cancels the peers.
fn finished(stop: &Stop) -> bool {
    matches!(
        stop,
        Stop::Reached
            | Stop::Hooked(_)
            | Stop::FetchFault(_)
            | Stop::NoCode(_)
            | Stop::Resident { stop: exec::stop::SHUTDOWN | exec::stop::UNSUPPORTED_ATOMIC | exec::stop::HALTED | exec::stop::UNSUPPORTED_MMIO | exec::stop::UNSUPPORTED_PORT, .. }
    )
}

pub(crate) fn run(args: &[String]) -> Result<()> {
    let [_, resident, _, fexc, description, _work, options @ ..] = args else {
        return Err(USAGE.into());
    };
    let (mut max_instructions, mut code_bytes, mut watchdog_ms, mut forge, mut target) = (None, exec::cache::CODE_BYTES, 10_000, false, 0);
    let (mut tamper, mut tamper_after): (Option<Tamper>, Option<Corrupt>) = (None, None);
    let (mut forge_stop, mut forged, mut kicked, mut kicks, mut every_us, mut jitter_us) = (false, None, Vec::new(), 10_000, 0, 0);
    let mut kick_events: Vec<u64> = Vec::new();
    let (mut ignore_kicks, mut signal_at, mut deadline_ms) = (false, None, execution::COMPILE_DEADLINE.as_millis() as u64);
    let (mut code_shape, mut device_tamper, mut strict, mut check_reuse) = (false, false, false, false);
    let mut device_at: Vec<u64> = Vec::new();
    // L4a: the asynchronous test source's ports, and whether its requests veto ending halted vCPUs.
    let (mut irq_source, mut irq_veto) = (None, true);
    // L5: the lost-tick policies (`clock::LostTicks`).
    let mut lost = crate::clock::LostTicks::DEFAULT;
    // M1: the VM's guest RAM and where the driver starts placing pages; the environment's defaults let a whole suite run
    // at another size or placement unchanged.
    let env = |name: &str, default: u64| std::env::var(name).map_or(Ok(default), |v| number(&v));
    let (mut ram_bytes, mut place) = (env("NATEMU_RAM", 2 << 30)?, env("NATEMU_PLACE", 0x1000)?);
    let mut options = options.iter();
    while let Some(option) = options.next() {
        let mut value = || -> Result<usize> { options.next().and_then(|v| v.parse().ok()).ok_or(USAGE.into()) };
        match option.as_str() {
            "--code-shape" => code_shape = true,
            "--check-reuse" => check_reuse = true,
            "--strict-unassigned" => strict = true,
            "--device-at" => device_at.push(number(options.next().ok_or(USAGE)?)?),
            "--irq-source" => irq_source = Some(number(options.next().ok_or(USAGE)?)?),
            "--irq-source-no-veto" => irq_veto = false,
            "--lost-ticks" => lost = crate::clock::LostTicks::parse(options.next().ok_or(USAGE)?)?,
            "--code-cache" => code_bytes = value()?,
            "--ram" => ram_bytes = number(options.next().ok_or(USAGE)?)?,
            "--place" => place = number(options.next().ok_or(USAGE)?)?,
            "--watchdog-ms" => watchdog_ms = value()? as u64,
            "--tamper-vcpu" => target = value()?,
            "--forge-exit" => forge = true,
            "--forge-stop" => forge_stop = true,
            "--forge-entry" => {
                let words = options.next().ok_or(USAGE)?.split(',').map(|w| u32::from_str_radix(w.trim_start_matches("0x"), 16));
                forged = Some(words.collect::<std::result::Result<Vec<u32>, _>>().map_err(|_| "--forge-entry: hex words")?);
            }
            "--ignore-kicks" => ignore_kicks = true,
            "--compile-deadline-ms" => deadline_ms = value()? as u64,
            "--kill-compiler" => signal_at = Some((value()? as u64, SIGKILL)),
            "--stall-compiler" => signal_at = Some((value()? as u64, SIGSTOP)),
            "--kick-vcpu" => kicked.push(value()?),
            "--kicks" => kicks = value()?,
            "--kick-every-us" => every_us = value()? as u64,
            "--kick-jitter-us" => jitter_us = value()? as u64,
            "--kick-events" => {
                for e in options.next().ok_or(USAGE)?.split(',') {
                    kick_events.push(match e {
                        "nmi" => execution::NMI,
                        v => number(v).ok().filter(|v| (16..256).contains(v)).ok_or(USAGE)?,
                    });
                }
            }
            "--tamper" => {
                tamper = Some(match options.next().map(String::as_str) {
                    Some("offset") => tamper_offset,
                    Some("word") => tamper_word,
                    Some("record") => tamper_record,
                    Some("system") => tamper_system,
                    Some("mmio") => {
                        device_tamper = true;
                        tamper_mmio
                    }
                    Some("next-page") => tamper_next_page,
                    Some("site") => tamper_site,
                    Some("site-kind") => tamper_site_kind,
                    Some("armed") => tamper_armed,
                    Some("dispatch") => tamper_dispatch,
                    Some("target") => tamper_target,
                    _ => return Err(USAGE.into()),
                })
            }
            "--tamper-after" => {
                tamper_after = Some(match options.next().map(String::as_str) {
                    Some("word") => Corrupt::Word,
                    Some("record") => Corrupt::Record,
                    _ => return Err(USAGE.into()),
                })
            }
            n if max_instructions.is_none() => max_instructions = Some(n.parse().map_err(|_| "bad block size limit")?),
            _ => return Err(USAGE.into()),
        }
    }
    let forged_entry = if forge_stop { Some(vec![exec::STOP_WORD]) } else { forged };
    if let Some(words) = &forged_entry {
        FORGED_ENTRY.set(words.clone()).map_err(|_| "forged entry set twice")?;
    }
    let d = parse(&fs::read_to_string(description).map_err(|e| e.to_string())?)?;
    let n = d.cpus.len();
    if kicked.iter().any(|&v| v >= n) {
        return Err(format!("--kick-vcpu: {n} vCPUs"));
    }
    if place % 4096 != 0 || place == 0 || place >= ram_bytes {
        return Err(format!("--place {place:#x}: a nonzero page inside guest RAM"));
    }
    if let Some(&(_, gpa, _)) = d.spaces.iter().flat_map(|s| &s.mmio).find(|m| m.1 < ram_bytes && !exec::LEGACY_HOLE.contains(&m.1)) {
        return Err(format!("mmio page {gpa:#x} is guest RAM"));
    }
    let backing = execution::prepare(resident, code_bytes, n, ram_bytes as usize)?;
    require_vcpus(n)?;
    // SAFETY: the builder and the tables it makes are dropped before `backing` (this function owns both).
    let mut builder = Builder { ram: unsafe { backing.ram() }, next: place, top: ram_bytes, reserved: Default::default() };
    // Identity-mapped pages first: the allocator never hands them out (the logging pages of identity spaces too).
    for c in d.cpus.iter().filter(|c| d.spaces[c.space].identity) {
        for page in identity_pages(&d.spaces[c.space], c.rip) {
            if page >= ram_bytes || page == 0 || exec::LEGACY_HOLE.contains(&page) {
                return Err(format!("identity page {page:#x} outside guest RAM (or page 0, or the legacy hole)"));
            }
            if (LOG_IDENTITY..LOG_IDENTITY + 0x7000).contains(&page) {
                return Err(format!("identity page {page:#x} is the driver's logging IDT"));
            }
            builder.reserved.insert(page);
        }
        builder.reserved.extend(LOG_PAGES.map(|o| LOG_IDENTITY + o));
    }
    // M3: one test device per distinct `mmio` page, registered for every vCPU.
    let mut test_devices: BTreeMap<u64, Arc<Mutex<TestDevice>>> = BTreeMap::new();
    let mut registry = Devices::new(ram_bytes);
    let items = d.spaces.iter().flat_map(|s| s.mmio.iter().filter(|m| m.2)).map(|m| m.1);
    for gpa in items.chain(device_at.iter().copied()) {
        if !test_devices.contains_key(&gpa) {
            let device = Arc::new(Mutex::new(TestDevice::new()));
            registry.register(gpa, 4096, device.clone())?;
            test_devices.insert(gpa, device);
        }
    }
    // L3: COM1, a 16550A at 3F8H (output reported as `uart`); the unassigned rule, or with `--strict-unassigned` stops.
    let uart = Arc::new(Mutex::new(crate::uart::Uart::new()));
    registry.register_ports(crate::boot::acpi::platform::COM1_PORT.into(), 8, uart.clone())?;
    // L4a: the I/O APIC, the 8259 pair and their wires; the UART's IRQ4 into them.
    let irqs = crate::irq::install(&mut registry, backing.memory(), n, &uart)?;
    // L4b: the PIT and port 61H, the RTC, the ACPI PM block and reset register.
    let board = crate::clock::install(&mut registry, &irqs, lost)?;
    if let Some(port) = irq_source {
        let mut source = super::test_devices::TestSource::new(&irqs);
        source.veto = irq_veto;
        registry.register_ports(port as u32, 8, Arc::new(Mutex::new(source)))?;
    }
    registry.strict = strict;
    let registry = Arc::new(registry);
    // A space is built for the first vCPU using it (its rip places `code` bodies).
    let mut spaces: Vec<Option<Tables>> = d.spaces.iter().map(|_| None).collect();
    let mut ends = Vec::new();
    for c in &d.cpus {
        if spaces[c.space].is_none() {
            spaces[c.space] = Some(build(&mut builder, &d.spaces[c.space], c.rip, c.nxe, &test_devices)?);
        }
        ends.push(stop_at(&d.spaces[c.space], c.rip)?);
    }
    let stop_at = ends;
    // The logging IDT's pages, after every space (the spaces' own pages keep their places): one set for the driver-built
    // spaces, the identity set for identity spaces.
    let mut log_gpa = None;
    for (k, t) in spaces.iter_mut().enumerate() {
        let Some(t) = t else { continue };
        let identity = d.spaces[k].identity;
        let base = log_base(identity);
        let gpas = match (identity, log_gpa) {
            (true, _) => LOG_PAGES.map(|o| base + o),
            (false, Some(g)) => g,
            (false, None) => *log_gpa.insert([(); 6].map(|_| builder.page()).into_iter().collect::<Result<Vec<_>>>()?.try_into().unwrap()),
        };
        let nx = if d.cpus.iter().find(|c| c.space == k).is_some_and(|c| c.nxe) { NO_EXECUTE } else { 0 };
        for (o, gpa) in LOG_PAGES.iter().zip(gpas) {
            t.map_at(&mut builder, base + o, gpa, WRITABLE | nx)?;
        }
        let [gdt, idt] = log_images(base);
        t.copy_in(base, &gdt);
        t.copy_in(base + 0x1000, &idt);
    }
    execution::guest_ram_written(backing.memory());
    for (v, c) in d.cpus.iter().enumerate() {
        // 4-level paging; user mode, NX and WP on unless the section says otherwise.
        let bit = |on: bool, value: u64| if on { value } else { 0 };
        execution::set_cpu(
            backing.vcpu(v),
            Cpu {
                gpr: c.gpr,
                rip: c.rip,
                rflags: c.rflags | 2 | if c.interrupts { 0x200 } else { 0 },
                cr0: exec::CR0_PE | exec::CR0_ET | exec::CR0_PG | bit(c.wp, exec::CR0_WP) | bit(c.ne, exec::CR0_NE),
                cr3: spaces[c.space].as_ref().unwrap().cr3,
                cr4: exec::CR4_PAE | bit(c.smep, exec::CR4_SMEP) | bit(c.sse, exec::CR4_OSFXSR | exec::CR4_OSXMMEXCPT) | bit(c.fsgsbase, 1 << 16) | bit(c.xcr0 != 0, exec::CR4_OSXSAVE),
                efer: exec::EFER_LME | exec::EFER_LMA | bit(c.nxe, exec::EFER_NXE),
                cpl: c.cpl,
                fs_base: c.fs_base,
                gs_base: c.gs_base,
                xmm: c.xmm,
                mxcsr: c.mxcsr,
                fcw: exec::FCW_INIT,
                xcr0: if c.xcr0 != 0 { c.xcr0 } else { natemu_abi::cpu_profile::XCR0_RESET },
                cr2: 0,
                cr8: 0,
                dr: exec::DR_RESET,
                seg: exec::flat_segments(c.cpl),
                ldtr: exec::LDTR_RESET,
                tr: exec::Segment { selector: 0x30 + 16 * v as u16, attributes: 0x8b, limit: 0x67, base: log_base(d.spaces[c.space].identity) + LOG_TSS + 0x80 * v as u64 },
                gdtr: exec::Table { base: log_base(d.spaces[c.space].identity), limit: 0x6f },
                idtr: exec::Table { base: log_base(d.spaces[c.space].identity) + 0x1000, limit: 0xfff },
                mov_ss: 0,
            },
        );
        execution::set_lapic(backing.vcpu(v), natemu_abi::lapic::Lapic { svr: c.svr, reinject: u64::from(lost.lapic), ..natemu_abi::lapic::RESET });
        let mut state: State = backing.vcpu(v).read_record(exec::STATE_OFFSET);
        state.mailbox = c.mailbox;
        if c.mailbox != 0 {
            // X4: the firmware's wake state (`boot::ap_wake`, as `boot-linux`'s APs), over the section's paging and segments.
            state.cpu = crate::boot::ap_wake(state.cpu);
            state.cpu.rip = c.rip;
        }
        backing.vcpu(v).write_record(exec::STATE_OFFSET, state);
    }

    let machine = Machine::create(backing.memory().clone())?;
    irqs.attach(machine.doorbell());
    let stops = execution::Stops::new(&backing);
    // vCPU v passed the start barrier: every vCPU exists. A kicker waits for it: an SPI rung while vCPUs are being
    // created would also find no vCPU to take it. Its end (`Stops::end`, under its kick mutex) stops its kicker.
    let started: Vec<AtomicBool> = (0..n).map(|_| AtomicBool::new(false)).collect();
    let watchdog = std::time::Duration::from_millis(watchdog_ms);
    let run_vcpu = |v: usize, cpu: &Vcpu, cancelled: &AtomicBool| -> Result<(Stop, Compiler, execution::Requests, ProgramHook)> {
        let mut compiler = Compiler::new(Path::new(fexc), cpu, max_instructions)?;
        compiler.deadline = std::time::Duration::from_millis(deadline_ms);
        compiler.shape = code_shape;
        // The suites' code-shape option also checks every answer reused at another RIP (perf chunk 4).
        compiler.check_reuse = code_shape || check_reuse;
        let handlers = log_base(d.spaces[d.cpus[v].space].identity) + LOG_HANDLERS;
        let mut requests = execution::Requests { devices: Some(registry.clone()), rips: Some(Default::default()), ..Default::default() };
        let mut hook = ProgramHook {
            switches: d.cpus[v].switches.iter().copied().collect(),
            posts: d.cpus[v].posts.iter().copied().collect(),
            handlers: handlers..handlers + 0x1000,
            doorbell: machine.doorbell(),
            forge_exit: false,
            signal_at: None,
            tamper: None,
            tamper_device: None,
            tampered: false,
        };
        if v == target {
            (hook.forge_exit, hook.signal_at) = (forge, signal_at);
            hook.tamper = if forged_entry.is_some() { Some(tamper_forge_entry) } else { tamper };
            // M3: the device reply tamper runs on device replies only.
            if device_tamper {
                (hook.tamper, hook.tamper_device) = (None, tamper);
            }
        }
        // A peer that never answers: alive (no watchdog of its own), never stopping.
        let watchdog = if v == target && ignore_kicks { std::time::Duration::from_secs(86_400) } else { watchdog };
        let stop = execution::run(cpu, backing.vcpu(v), &mut compiler, Some(stop_at[v]), watchdog, &mut requests, &stops, cancelled, &mut hook)?;
        Ok((stop, compiler, requests, hook))
    };
    let body = |v: usize, cpu: &Vcpu, cancelled: &AtomicBool| -> Result<Ran> {
        started[v].store(true, SeqCst);
        let ran = run_vcpu(v, cpu, cancelled);
        stops.end(v);
        let (stop, compiler, requests, hook) = ran?;
        if !finished(&stop) {
            owners::cancel_owners(cancelled);
        }
        Ok(Ran { stop, compiles: compiler.compiles, slowest: compiler.slowest, blocks: requests.blocks, rips: requests.rips.unwrap_or_default(), tampered: hook.tampered, decodes: requests.decodes, entropy: requests.entropy })
    };
    // HVF's GIC calls never race vCPU create/destroy (`vm.rs`, one lock).
    let prepare = |v: usize| -> Result<Vcpu> {
        let cpu = Vcpu::create()?;
        let answers = !(ignore_kicks && v == target);
        execution::enter(&cpu, backing.vcpu(v), answers)?;
        machine.route(v)?;
        if !answers {
            machine.silence(v)?;
        }
        Ok(cpu)
    };
    // One kicker per kicked vCPU, joined before the VM goes (a protect needs the VM). With `--kick-events`, each kick
    // first posts an event (X2), chosen at random from the list.
    let kickers_left = std::sync::atomic::AtomicUsize::new(kicked.len());
    let all_ended = AtomicBool::new(false);
    let (kicked, supervision) = std::thread::scope(|scope| {
        let kickers: Vec<_> = kicked
            .iter()
            .map(|&v| {
                let (stops, started, machine, backing, kick_events, kickers_left) = (&stops, &started, &machine, &backing, &kick_events, &kickers_left);
                scope.spawn(move || -> Result<()> {
                    struct Done<'a>(&'a std::sync::atomic::AtomicUsize);
                    impl Drop for Done<'_> {
                        fn drop(&mut self) {
                            self.0.fetch_sub(1, SeqCst);
                        }
                    }
                    let _done = Done(kickers_left);
                    let mut rng = 0x2545_f491_4f6c_dd1d_u64 ^ (v as u64 + 1) << 32;
                    while !started[v].load(SeqCst) && !stops.ended(v) {
                        std::thread::sleep(std::time::Duration::from_micros(20));
                    }
                    for _ in 0..kicks {
                        let area = backing.vcpu(v);
                        let taken = stops.taken(area);
                        if !kick_events.is_empty() && !stops.ended(v) {
                            let _ = execution::post(backing.memory(), v, kick_events[(rng >> 33) as usize % kick_events.len()], false);
                        }
                        if !stops.kick(machine, v)? {
                            break;
                        }
                        // Spin briefly, then sleep: a spinning kicker would take a core from the vCPUs.
                        for spins in 0.. {
                            // Acknowledged (a doorbell rung again before that would merge into this one in the GIC) and a
                            // stop taken since (peers' doorbells stop the vCPU too); or the vCPU ended.
                            let moved = stops.taken(area) != taken;
                            let Some(ringing) = stops.ringing(machine, v)? else { break };
                            if moved && !ringing {
                                stops.followed(area);
                                break;
                            }
                            if spins < 1000 {
                                std::hint::spin_loop();
                            } else {
                                std::thread::sleep(std::time::Duration::from_micros(20));
                            }
                        }
                        // A random extra 0..J us (xorshift seeded by the vCPU), so kicks land anywhere.
                        rng ^= rng << 13;
                        rng ^= rng >> 7;
                        rng ^= rng << 17;
                        std::thread::sleep(std::time::Duration::from_micros(every_us + rng % (jitter_us + 1)));
                    }
                    Ok(())
                })
            })
            .collect();
        // X2: a halted vCPU can only be woken by an event; in this driver every source is a kicker, a vCPU's own
        // `post`/`nmi` items (which a halted vCPU cannot reach), a running vCPU's IPI or a vCPU's own LAPIC timer (X3).
        // X3: once every kicker has ended, the park state of every vCPU not ended is read twice: when both reads are
        // equal and idle (halted, its timer unable to wake it, every post to it seen), no vCPU can run again. (A vCPU
        // that runs again must leave its park, which needs a post it has not seen, made by a vCPU running then, which
        // left its park the same way; the first to leave did so between the reads, which changes its post count.) Each
        // is then asked to end its run (`halted`). One read alone could take a vCPU just woken by a peer as idle.
        // The argument covers only sources that are vCPUs or that veto the decision while they can still fire: every
        // asynchronous host source (the kickers here; L4a's PIT, RTC, UART and device SPIs) must post through
        // `execution::post`, so the posts count sees it, and must keep the decision off while it may still post, as
        // `kickers_left` does.
        let (stops, backing, machine, kickers_left, all_ended, irqs) = (&stops, &backing, &machine, &kickers_left, &all_ended, &irqs);
        let idle = scope.spawn(move || -> Result<()> {
            let mut asked = vec![false; n];
            while !all_ended.load(SeqCst) {
                std::thread::sleep(std::time::Duration::from_millis(1));
                let read = || (0..n).map(|v| (!stops.ended(v)).then(|| execution::park_state(backing.memory(), v))).collect::<Vec<_>>();
                let (first, second) = (read(), read());
                let halted: Vec<usize> = (0..n).filter(|&v| first[v].is_some()).collect();
                // The vetoes are read after the two reads (L4a): a source that may still fire (a kicker, or an
                // asynchronous interrupt source's `Irqs::busy` guard) vetoes. A guard is taken by a vCPU's device access,
                // before that vCPU can halt, or held continuously by a host source while armed; so if every vCPU was idle
                // across both reads, any guard taken before then is visible here, and none can be taken after (no vCPU
                // runs). Checked before the reads instead, a guard taken between the check and the reads by a vCPU that
                // then halted went unseen and its vCPU was ended (`run_l4a.py halt` with a 30 ms delay forced there: red
                // 5/5; this order: green 5/5).
                let vetoed = kickers_left.load(SeqCst) != 0 || irqs.live() != 0;
                if !vetoed && !halted.is_empty() && halted.iter().all(|&v| first[v] == second[v] && first[v].is_some_and(execution::idle)) {
                    for &v in halted.iter().filter(|&&v| !std::mem::replace(&mut asked[v], true)) {
                        execution::end_when_halted(backing.memory(), v);
                        stops.kick(machine, v)?;
                    }
                }
            }
            Ok(())
        });
        let supervision = owners::supervise(n, &prepare, &body, &AtomicBool::new(false));
        all_ended.store(true, SeqCst);
        let idle = idle.join().unwrap_or(Err("idle watcher panicked".into()));
        for v in 0..n {
            stops.end(v); // a vCPU that never started
        }
        let kicked: Result<()> = kickers.into_iter().map(|k| k.join().unwrap_or(Err("kicker panicked".into()))).collect();
        let kicked = kicked.and(idle);
        // Every vCPU and kicker has ended: a doorbell still pending is a kick no stop followed.
        let kicked = kicked.and_then(|()| (0..n).try_for_each(|v| stops.pending_at_end(&machine, v)));
        (kicked, supervision)
    });
    // Every vCPU is destroyed (or never created): the VM goes next (no device rings it after this), then the host reads
    // the backing.
    irqs.detach();
    board.stop();
    let destroyed = machine.destroy();
    eprintln!("{}", teardown(n, supervision.as_ref().ok(), destroyed));
    let supervision = supervision?;
    hv(destroyed, "destroy VM")?;
    if let Some(e) = supervision.error {
        return Err(e);
    }
    kicked?;
    let mut out = Vec::new();
    for (v, ran) in supervision.outputs.into_iter().enumerate() {
        let ran = ran.ok_or("vCPU without a result")?;
        let tables = spaces[d.cpus[v].space].as_ref().unwrap();
        let tampering = v == target && (tamper.is_some() || tamper_after.is_some());
        let tampering = tampering || (v == target && forged_entry.is_some());
        let log = log_base(d.spaces[d.cpus[v].space].identity);
        let mut json = report(backing.vcpu(v), &d.spaces[d.cpus[v].space], tables, log, ran, tampering, tamper_after.filter(|_| v == target))?;
        json.pop();
        let m: exec::Mmio = backing.vcpu(v).read_record(exec::MMIO_OFFSET);
        // With test devices, or once the vCPU made a device access (X3: its xAPIC page) or had one refused.
        let mmio = if test_devices.is_empty() && m.completed == 0 && m.status == 0 {
            String::new()
        } else {
            // The address only where the host answered for it (a refusal before any request names none).
            let address = if matches!(m.status, exec::mmio::UNASSIGNED | exec::mmio::DEVICE) { format!(",\"address\":\"{:#x}\"", m.gpa) } else { String::new() };
            format!(",\"mmio\":{{\"completed\":{},\"status\":\"{}\"{address}}}", m.completed, exec::mmio::name(m.status))
        };
        out.push(json + &kick_report(backing.vcpu(v), &stops) + &mmio + "}");
    }
    let devices = test_devices.iter().map(|(gpa, d)| {
        let d = d.lock().unwrap();
        let log: Vec<String> = d.log.iter().map(|e| format!("[{},{},{},{},\"{:#x}\"]", e[0], e[1], e[2], e[3], e[4])).collect();
        format!("{{\"gpa\":\"{gpa:#x}\",\"reads\":{:?},\"writes\":{:?},\"order_errors\":{},\"log\":[{}]}}", &d.reads[..n], &d.writes[..n], d.order_errors, log.join(","))
    });
    let mut devices = if test_devices.is_empty() { String::new() } else { format!(",\"devices\":[{}]", devices.collect::<Vec<_>>().join(",")) };
    // L3: port and unassigned-page counts and the UART's output, once there are any.
    let counts = registry.report();
    let output = std::mem::take(&mut uart.lock().unwrap_or_else(|e| e.into_inner()).output);
    if counts != "\"ports\":[],\"unassigned_pages\":[]" || !output.is_empty() {
        let hex: String = output.iter().map(|b| format!("{b:02x}")).collect();
        let _ = write!(devices, ",{counts},\"uart\":\"{hex}\"");
    }
    if d.sections {
        println!("{{\"cpus\":[{}]{devices}}}", out.join(","));
    } else {
        let mut one = out.swap_remove(0);
        one.pop();
        println!("{one}{devices}}}");
    }
    Ok(())
}

/// The `teardown:` line, written after the joins (every vCPU thread has returned) and the VM's destroy: of `n` vCPUs,
/// how many joined with a result and how many were destroyed with status 0 (`None`: [`owners::supervise`] failed in
/// setup, before the start barrier, so no vCPU entered and none reports), the VM's destroy status, and the child
/// processes left (`waitpid(-1, WNOHANG)` failing with ECHILD: none, alive or unreaped). A kicker's failure does not
/// change it (it is returned after the line).
fn teardown(n: usize, s: Option<&owners::Supervision<Ran>>, vm: i32) -> String {
    unsafe extern "C" {
        fn waitpid(pid: i32, status: *mut i32, options: i32) -> i32;
    }
    const WNOHANG: i32 = 1;
    let mut status = 0;
    let children = match unsafe { waitpid(-1, &mut status, WNOHANG) } {
        -1 if std::io::Error::last_os_error().raw_os_error() == Some(10) => "none".to_string(), // ECHILD
        -1 => format!("unknown ({})", std::io::Error::last_os_error()),
        0 => "alive".into(),
        pid => format!("unreaped (pid {pid})"),
    };
    let (joined, destroyed) = s.map_or((0, 0), |s| (s.outputs.len(), s.destroyed.iter().filter(|d| d.1 == 0).count()));
    format!("teardown: vcpus {n} joined {joined} destroyed {destroyed} vm_destroy {vm} children {children}")
}

/// One vCPU's final state, its space's memory and A/D bits, and its counters, as JSON. With `tamper_after`, corrupts
/// its cache first; with `tampering` the link self-check's verdict is reported, not fatal.
/// `n` bytes at linear `va` through the 4-level tables at `cr3` (the vCPU's own, which a program may have loaded), read
/// only: no A/D update; `None` when a byte is not mapped.
fn linear_bytes(ram: GuestRam, cr3: u64, va: u64, n: usize) -> Option<Vec<u8>> {
    (va..va + n as u64)
        .map(|a| {
            let mut table = cr3 & ADDRESS;
            for level in (1..=4u32).rev() {
                let entry = ram.read::<u64>(table + ((a >> (12 + 9 * (level - 1))) & 511) * 8)?;
                if entry & PRESENT == 0 {
                    return None;
                }
                if level == 2 && entry & LARGE_PAGE != 0 {
                    return ram.read::<u8>(entry & ADDRESS & !(LARGE - 1) | a & (LARGE - 1));
                }
                table = entry & ADDRESS;
            }
            ram.read::<u8>(table | a & 4095)
        })
        .collect()
}
/// A run that reached the logging IDT (`log`: its base) reports the state its frame saved, as the faulting instruction
/// found it (GPRs with RSP from the frame, RIP and RFLAGS from the frame, RF left out), `stop` `guest_fault` with the
/// record (vector, CR2 for #PF, error code, RIP), and `delivered`: the vector, the raw frame (RIP, CS, RFLAGS, RSP, SS,
/// error code or 0), whether it has an error code, the handler's RSP and CPL, and CR2.
fn report(area: Area, space: &Space, guest: &Tables, log: u64, ran: Ran, tampering: bool, tamper_after: Option<Corrupt>) -> Result<String> {
    let mut s: State = area.read_record(exec::STATE_OFFSET);
    let list = |v: &[u64]| v.iter().map(|x| format!("\"{x:#x}\"")).collect::<Vec<_>>().join(",");
    let mut delivered = String::new();
    if matches!(ran.stop, Stop::Hooked(LOGGED)) {
        let vector = (s.cpu.rip - log - LOG_HANDLERS) / 16;
        // From the event, not the vector: INT n to an error-code vector pushes none.
        if s.delivered[0] != vector {
            return Err(format!("logged vector {vector}, but the last delivery was vector {}", s.delivered[0]));
        }
        let error = s.delivered[1] != u64::MAX;
        let n = if error { 6 } else { 5 };
        let at = s.cpu.gpr[4];
        let frame = linear_bytes(guest.ram, s.cpu.cr3, at, 8 * n).ok_or(format!("logged vector {vector}: its frame at {at:#x} is not mapped"))?;
        let mut w: Vec<u64> = frame.chunks(8).map(|c| u64::from_le_bytes(c.try_into().unwrap())).collect();
        if !error {
            w.insert(0, 0);
        }
        let [e, rip, cs, rflags, rsp, ss] = w[..] else { unreachable!() };
        s.fault = [vector, if vector == 14 { s.cpu.cr2 } else { 0 }, e, rip];
        delivered = format!(",\"delivered\":{{\"vector\":{vector},\"frame\":[{}],\"error_code\":{error},\"handler_rsp\":\"{at:#x}\",\"cpl\":{},\"cr2\":\"{:#x}\"}}", list(&[rip, cs, rflags, rsp, ss, e]), s.cpu.cpl, s.cpu.cr2);
        (s.cpu.gpr[4], s.cpu.rip, s.cpu.rflags) = (rsp, rip, rflags & !(1 << 16));
    }
    let shared: Shared = area.read_record(SHARED_OFFSET);
    let mut tampered = ran.tampered;
    if let Some(kind) = tamper_after {
        tampered = corrupt(area, kind);
    }
    let (link_points, linked, links_check) = match execution::check_links(area) {
        Ok((points, linked)) => (points, linked, "ok".to_string()),
        Err(e) if tampering => (0, 0, e),
        Err(e) => return Err(e),
    };

    // A guest exception (resident: at an access site or stub; host: instruction fetch) with its record.
    let (kind, reason, fault) = match &ran.stop {
        Stop::Reached => ("reached", String::new(), [0; 4]),
        Stop::FetchFault(Fault::Page { address, error }) => ("guest_fault", "fetch".into(), [14, *address, *error, s.cpu.rip]),
        Stop::FetchFault(Fault::Noncanonical) => ("guest_fault", "fetch".into(), [13, 0, 0, s.cpu.rip]),
        Stop::FetchFault(f) => ("fetch_fault", format!("{f:?}"), [0; 4]),
        Stop::NoCode(reason) => ("no_code", reason.clone(), [0; 4]),
        Stop::Hooked(_) => ("guest_fault", "delivered".into(), s.fault),
        Stop::Resident { stop: exec::stop::SHUTDOWN, .. } => ("shutdown", "triple fault".into(), s.fault),
        Stop::Resident { stop, status, detail } => ("resident", format!("{} status={status} detail={detail}", exec::stop::name(*stop)), [0; 4]),
        Stop::Watchdog { pc, el0, asked } => {
            let place = if *el0 { "guest" } else { "resident" };
            let asked: Vec<u32> = (0..8).filter(|p| asked & 1 << p != 0).collect();
            // M1: the walk retries (lost A/D races) so far, to tell a retry storm from a stall at one instruction.
            ("watchdog", format!("{place} pc={pc:#x} shadows_asked={asked:?} retries={}", s.retries), [0; 4])
        }
        Stop::Cancelled => ("cancelled", String::new(), [0; 4]),
        Stop::Power(p) => (power_name(*p), String::new(), [0; 4]),
    };
    let mut rips: Vec<u64> = ran.rips.iter().copied().collect();
    rips.sort_unstable();
    // K2: decodes by kind ("0x7": n). A suite can collect them: with NATEMU_EXIT_LOG set, one line per vCPU is appended.
    let decodes = ran.decodes.iter().map(|(k, n)| format!(r#""{k:#x}":{n}"#)).collect::<Vec<_>>().join(",");
    // S4's counters live in the cache's control record (not copied with the state).
    let control: natemu_abi::execution::cache::Control = area.read_record(natemu_abi::execution::cache::LOOKUP_OFFSET + natemu_abi::execution::cache::CONTROL);
    if let Some(path) = std::env::var_os("NATEMU_EXIT_LOG") {
        use std::io::Write as _;
        let line = format!(r#"{{"systems":{},"entropy":{},"decodes":{{{decodes}}}}}"#, s.systems, ran.entropy) + "\n";
        let _ = std::fs::OpenOptions::new().create(true).append(true).open(path).and_then(|mut f| f.write_all(line.as_bytes()));
    }
    let mut out = format!(
        "{{\"vcpu\":{},\"tso\":{},\"stop\":\"{kind}\",\"reason\":\"{reason}\",\"gpr\":[{}],\"rip\":\"{:#x}\",\"rflags\":\"{:#x}\",\"xmm\":[{}],\"mxcsr\":\"{:#x}\",\"memory\":[",
        area.vcpu(),
        shared.actlr & 2 != 0,
        list(&s.cpu.gpr),
        s.cpu.rip,
        s.cpu.rflags,
        list(s.cpu.xmm.as_flattened()),
        s.cpu.mxcsr
    );
    for (i, (address, data)) in space.memory.iter().enumerate() {
        let bytes: String = guest.copy_out(*address, data.len()).iter().map(|b| format!("{b:02x}")).collect();
        let _ = write!(out, "{}{{\"addr\":\"{address:#x}\",\"data\":\"{bytes}\"}}", if i > 0 { "," } else { "" });
    }
    // I6: the x87 state (FCW from the bank; the rest from the guest-state page: TOP, C0..C3, FEX's IE byte, the exception
    // word, the abridged tag byte, ST's physical registers mm[0..8] as doubles).
    let frame: [u8; exec::frame::BYTES] = area.read_record(exec::FRAME_OFFSET);
    let byte = |at: u32| u64::from(frame[at as usize]);
    let word = |at: u32| u64::from(u32::from_le_bytes(frame[at as usize..at as usize + 4].try_into().unwrap()));
    let mm: Vec<u64> = (0..8).map(|r| u64::from_le_bytes(frame[(exec::frame::X87_MM + 16 * r) as usize..][..8].try_into().unwrap())).collect();
    let _ = write!(
        out,
        "],\"x87\":{{\"fcw\":\"{:#x}\",\"top\":{},\"c\":[{},{},{},{}],\"ie\":{},\"pe\":{},\"sf\":{},\"exceptions\":\"{:#x}\",\"ftw\":\"{:#x}\",\"mm\":[{}]}}",
        s.cpu.fcw,
        byte(exec::frame::X87_TOP),
        byte(exec::frame::X87_C0),
        byte(exec::frame::X87_C1),
        byte(exec::frame::X87_C2),
        byte(exec::frame::X87_C3),
        byte(exec::frame::X87_IE),
        byte(exec::frame::X87_PE),
        byte(exec::frame::X87_SF),
        word(exec::frame::X87_EXCEPTIONS),
        byte(exec::frame::X87_FTW),
        list(&mm)
    );
    // I8: YMM0..15's upper halves (the guest-state page's avx_high, two quadwords per register).
    let ymmh: Vec<u64> = (0..32).map(|q| u64::from_le_bytes(frame[(exec::frame::AVX_HIGH + 8 * q) as usize..][..8].try_into().unwrap())).collect();
    let _ = write!(out, ",\"ymmh\":[{}]", list(&ymmh));
    // Perf chunk 17: GS.base (the bank's) and IA32_KERNEL_GS_BASE (the MSR record's), which an inline SWAPGS exchanges.
    let msrs: natemu_abi::cpu::Msrs = area.read_record(exec::MSRS_OFFSET);
    let _ = write!(out, ",\"gs_base\":\"{:#x}\",\"kernel_gs_base\":\"{:#x}\"", s.cpu.gs_base, msrs[natemu_abi::cpu::msr(natemu_abi::cpu::MSR_KERNEL_GS_BASE).unwrap()]);
    let l = execution::lapic(area);
    let _ = write!(out, ",\"lapic\":{{\"cntfrq\":{},\"svr\":\"{:#x}\",\"tpr\":\"{:#x}\",\"isr\":[{}],\"esr\":\"{:#x}\"}}", l.frequency, l.svr, s.cpu.cr8, list(&l.isr), l.esr);
    out += ",\"pages\":[";
    for (i, (va, &(_, leaf))) in guest.pages.iter().filter(|(va, _)| !(log..log + 0x7000).contains(*va)).enumerate() {
        let entry = guest.ram.read::<u64>(leaf).unwrap();
        let _ = write!(
            out,
            "{}{{\"va\":\"{va:#x}\",\"accessed\":{},\"dirty\":{}}}",
            if i > 0 { "," } else { "" },
            entry & natemu_abi::paging::ACCESSED != 0,
            entry & natemu_abi::paging::DIRTY != 0
        );
    }
    let _ = write!(
        out,
        "],\"stats\":{{\"blocks\":{},\"l2_hits\":{},\"requests\":{},\"distinct\":{},\"host_requests\":{},\"flushes\":{},\"compiles\":{},\"compile_max_us\":{},\"steps\":{},\"shootdowns\":{},\"stops\":{},\"systems\":{},\"invalidations\":{},\"retries\":{},\"upgrades\":{},\"evictions\":{},\"shadow_resets\":{},\"splits\":{},\"linker\":{},\"links\":{},\"unlinks\":{},\"stale_links\":{},\"full_links\":{},\"link_points\":{},\"linked\":{},\"dispatch_exits\":{},\"interrupts\":{},\"nmis\":{},\"halts\":{},\"shadow_steps\":{},\"debug_steps\":{},\"debug_traps\":{},\"code_breaks\":{},\"watch_hits\":{},\"watchpoints\":{},\"mailbox\":[{},{},{},{}],\"decode_exits\":{},\"decodes\":{{{}}},\"entropy_exits\":{},\"k1\":{}}},\"fault\":[{}],\"native\":[{}],\"frame_intact\":{},\"tampered\":{},\"links_check\":\"{}\",\"block_rips\":[{}]{}}}",
        s.blocks,
        s.l2_hits,
        s.requests,
        ran.rips.len(),
        ran.blocks,
        s.generation,
        ran.compiles,
        ran.slowest.as_micros(),
        s.steps,
        s.shootdowns,
        s.stops,
        s.systems,
        s.invalidations,
        s.retries,
        s.upgrades,
        s.evictions,
        execution::shadow_resets(area),
        s.splits,
        s.linker,
        s.links,
        s.unlinks,
        s.stale_links,
        s.full_links,
        link_points,
        linked,
        execution::placed_blocks(area).iter().filter(|b| b.dispatch_exit != 0).count(),
        s.interrupts,
        s.nmis,
        s.halts,
        s.shadow_steps,
        control.debug_steps,
        control.debug_traps,
        control.code_breaks,
        control.watch_hits,
        control.watchpoints,
        s.mailbox,
        s.mailbox_wakes,
        // As the guest's TSC (ns since the VM's epoch, the MSR's offset), 0 if never acknowledged.
        if s.mailbox_acked == 0 { 0 } else { execution::ticks_ns(s.mailbox_acked) - execution::ticks_ns(area.epoch()) },
        execution::ticks_ns(s.mailbox_mark_ticks) / 1000,
        ran.decodes.values().sum::<u64>(),
        decodes,
        ran.entropy,
        execution::k1_stats(area),
        list(&fault),
        list(&s.native),
        frame_intact(area),
        tampered,
        links_check.replace('"', "'"),
        list(&rips),
        delivered
    );
    Ok(out)
}

/// `,"kicks":{..}` for vCPU `area.vcpu`: its host kicks, the resident's doorbells and stops, and the latency
/// distribution from each ring to the stop that followed.
fn kick_report(area: Area, stops: &execution::Stops) -> String {
    let k = stops.report(area.vcpu());
    let s: State = area.read_record(exec::STATE_OFFSET);
    let c: cache::Control = area.read_record(cache::LOOKUP_OFFSET + cache::CONTROL);
    let quantiles = |host: bool| {
        let mut ns: Vec<u64> = k.latency.iter().filter(|l| l.1 == host).map(|l| l.0).collect();
        ns.sort_unstable();
        let q = |f: f64| ns.get(((ns.len() as f64 - 1.0) * f).round() as usize).copied().unwrap_or(0);
        format!("{{\"n\":{},\"min\":{},\"p50\":{},\"p90\":{},\"p99\":{},\"p999\":{},\"max\":{}}}", ns.len(), q(0.0), q(0.5), q(0.9), q(0.99), q(0.999), q(1.0))
    };
    let mean = |ticks: u64, n: u64| if n == 0 { 0 } else { execution::ticks_ns(ticks) / n };
    format!(
        ",\"kicks\":{{\"issued\":{},\"in_host\":{},\"pending\":{},\"irqs\":{},\"irq_sgis\":{},\"merged\":{},\"arms\":{},\"stops\":{},\"stops_entry\":{},\"stops_dispatch\":{},\"stops_l1\":{},\"stops_exit\":{},\"stops_resident\":{},\"irqs_step\":{},\"rearms\":{},\"armed_at_end\":{},\"sgis_sent\":{},\"ack_ns_mean\":{},\"sgi_ns_mean\":{},\"ring_ns_mean\":{},\"latency_ns\":{},\"latency_in_host_ns\":{}}}",
        k.issued,
        k.in_host,
        k.pending,
        c.irqs,
        c.irq_sgis,
        s.merged,
        s.arms,
        s.stops,
        s.stops_entry,
        s.stops_dispatch,
        s.stops_l1,
        s.stops_exit,
        s.stops_resident,
        s.irqs_step,
        c.rearms,
        c.armed,
        c.sgis,
        mean(c.irq_ticks, c.irqs),
        mean(c.sgi_ticks, c.sgis),
        if k.issued == 0 { 0 } else { k.ring_ns / k.issued },
        quantiles(false),
        quantiles(true)
    )
}

/// The FEX frame holds nothing outside its guest-state fields (guest code cannot have written private memory there; I8:
/// YMM_Hi128 is one of them; perf chunk 17: IA32_KERNEL_GS_BASE's field, which the resident writes at every entry and only a
/// CPL-0 block's SWAPGS may access; perf chunk 26: CR2's, likewise written at every entry, loaded only by a CPL-0 MOV r64, CR2).
fn frame_intact(area: Area) -> bool {
    let frame: [u8; exec::frame::BYTES] = area.read_record(exec::FRAME_OFFSET);
    let field = |at: u32| at as usize..at as usize + 8;
    let admitted = |i: usize| field(exec::frame::KERNEL_GS_BASE).contains(&i) || field(exec::frame::CR2).contains(&i) || exec::frame::GUEST_STATE.iter().any(|&(at, len)| (at as usize..(at + len) as usize).contains(&i));
    frame.iter().enumerate().all(|(i, &b)| b == 0 || admitted(i))
}

#[cfg(test)]
mod tests {
    use super::*;
    use natemu::memory::Memory;
    #[test]
    fn description_parses_and_rejects_malformed_lines() {
        let d = parse("rip 0x100000\nrflags 0x1\ngpr 0 1 2 3 4 5 6 7 8 9 a b c d e f\ncode 90c3\nmemory 0x200000 0102\nunmap 0x200010\nreadonly 0x201000\ncpl 0\nwp 0\nsmep 1\nnxe 0\nlarge 0x3fffff\nentry 0x200000 2 set 0x10000000000\nentry 0x200000 4 clear 4\n").unwrap();
        let (c, p) = (&d.cpus[0], &d.spaces[0]);
        assert_eq!((d.cpus.len(), d.sections, c.rip, c.rflags, c.gpr[15]), (1, false, 0x100000, 1, 15));
        assert_eq!((&p.bodies, &p.memory), (&vec![(None, vec![0x90, 0xc3])], &vec![(0x200000, vec![1, 2])]));
        assert_eq!(stop_at(p, c.rip), Ok(0x100002));
        assert_eq!(parse("region 1000 2000\ncode 90").unwrap().spaces[0].regions, vec![(0x1000, 0x2000)]);
        assert_eq!((c.cpl, c.wp, c.smep, c.nxe, &p.large), (0, false, true, false, &vec![0x200000]));
        let edit = |va, level, set, clear| Edit { va, level, set, clear };
        let edits = [edit(0x200010, None, 0, PRESENT), edit(0x201000, None, 0, WRITABLE), edit(0x200000, Some(2), 1 << 40, 0), edit(0x200000, Some(4), 0, 4)];
        assert_eq!(p.edits, edits);
        assert_eq!((parse("code 90").unwrap().cpus[0].cpl, parse("code 90").unwrap().cpus[0].nxe), (3, true));
        let c = &parse("code 90\nfsbase 0xffff800000001000\ngsbase 0x10").unwrap().cpus[0];
        assert_eq!((c.fs_base, c.gs_base), (0xffff_8000_0000_1000, 0x10));
        assert!(parse("code 90\ngsbase 0x800000000000").is_err());
        assert!(parse("identity\ncode 90").unwrap().spaces[0].identity && !parse("code 90").unwrap().spaces[0].identity);
        assert_eq!(parse("code 9090\nswitch 0x1001 3\nswitch 1000 0").unwrap().cpus[0].switches, vec![(0x1001, 3), (0x1000, 0)]);
        for bad in ["switch 0x1000 1\ncode 90", "body 0x1000 90\nswitch 0x1000 0\ncpu\nrip 0x1000", "rip\ncode 90", "code 9", "gpr 1 2\ncode 90", "", "frob 1\ncode 90", "region 1 0\ncode 90", "cpl 1\ncode 90", "wp 2\ncode 90",
                    "entry 0x1000 5 set 1\ncode 90", "entry 0x1000 1 flip 1\ncode 90", "body 0x1000 90\nsmep 1\ncpu\nrip 0x1000"] {
            assert!(parse(bad).is_err(), "{bad:?}");
        }
    }
    #[test]
    fn next_page_tamper_answers_a_block_request_twice() {
        let memory = Memory::with_size(4096).unwrap();
        let area = Area::over(&memory);
        let mut s = State::new(0, Cpu { gpr: [0; 16], rip: 0x1000, rflags: 2, cr0: 0, cr3: 0, cr4: 0, efer: 0, cpl: 3, fs_base: 0, gs_base: 0, cr2: 0, cr8: 0, dr: exec::DR_RESET, seg: exec::flat_segments(3), ldtr: exec::LDTR_RESET, tr: exec::TR_RESET, gdtr: exec::TABLE_RESET, idtr: exec::TABLE_RESET, mov_ss: 0, xmm: [[0; 2]; 16], mxcsr: exec::MXCSR_RESET, fcw: exec::FCW_INIT, xcr0: 1 });
        s.fetch = [0x5000, u64::MAX];
        s.reply = exec::REPLY_SYSTEM;
        assert!(!tamper_next_page(area, &mut s) && s.reply == exec::REPLY_SYSTEM);
        s.reply = exec::REPLY_ADMITTED;
        assert!(!tamper_next_page(area, &mut s) && s.reply == exec::REPLY_NEXT_PAGE); // first: not yet refused
        (s.reply, s.fetch[1]) = (exec::REPLY_ADMITTED, 0x6000);
        assert!(tamper_next_page(area, &mut s) && s.reply == exec::REPLY_NEXT_PAGE); // second, after the walk
    }
    #[test]
    fn cpu_sections_share_the_preamble_space_or_own_one() {
        let d = parse("body 0x1000 90c3\nmemory 0x5000 01\ncpu\nrip 0x1001\ncpu\nrip 0x2000\ncode 90\ncpu\nrip 0x1000\n").unwrap();
        assert!(d.sections);
        assert_eq!(d.cpus.iter().map(|c| (c.rip, c.space)).collect::<Vec<_>>(), [(0x1001, 0), (0x2000, 1), (0x1000, 0)]);
        assert_eq!(d.spaces[1].bodies, vec![(None, vec![0x90])]);
        assert_eq!((stop_at(&d.spaces[0], 0x1001), stop_at(&d.spaces[1], 0x2000)), (Ok(0x1002), Ok(0x2001)));
        assert!(stop_at(&d.spaces[0], 0x3000).is_err());
        // State before the first section, a section without code, code at a rip in the shared space.
        for bad in ["rip 1\nbody 0x1000 90\ncpu\nrip 0x1000", "body 0x1000 90\ncpu\nrip 0x1000\ncpu\nmemory 0x5000 01", "code 90\ncpu\nrip 0x1000"] {
            assert!(parse(bad).is_err(), "{bad:?}");
        }
    }
}
