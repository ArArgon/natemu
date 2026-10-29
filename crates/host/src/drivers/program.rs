//! Test driver: run x86-64 program descriptions in the VM, one per vCPU.
//!
//! Fixture setup (owned here, not by the emulator): user-mode 4-level page
//! tables mapping exactly the pages of the code and the memory regions, the
//! bytes, and the initial architectural state. A vCPU's program ends where the
//! body holding its start RIP ends: that RIP is its stop address. Prints the final
//! state as JSON. An optional block size limit makes every program run as several blocks;
//! `--code-cache BYTES` sets each code cache's capacity and `--watchdog-ms MS`
//! the watchdog (default 10 s). After the run each vCPU's cache link points are
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
//! makes its load and store sites claim to be guard stubs (a data abort there: `site_mismatch`). Containment:
//! `--ignore-kicks` leaves the vCPU's doorbells disabled and its watchdog off (it never stops or drains while in linked
//! code, a peer that never answers, so a peer waiting for its shadow reset is contained by that peer's watchdog);
//! `--kill-compiler N` kills its `fexc --serve` before the request after its first N
//! (the run fails with the server's exit), `--stall-compiler N` stops it there (SIGSTOP: alive, never answering; the
//! request's deadline, `--compile-deadline-ms`, default 30 s, kills it and fails the run). Each run ends with a `teardown:` line on stderr, written after the VM is
//! destroyed, success or failure: vCPUs joined and destroyed, the VM's destroy status, and whether any child process
//! (compile server) is left, alive or unreaped.
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
//! reaching its end, a guest fault, code it cannot compile or a split lock (`unsupported_atomic`: a precise stop before the
//! instruction, like a guest fault) cancels the others (another resident halt, the watchdog).
//!
//! Description (text, one item per line). State items: rip ADDR | rflags VALUE | gpr V0 .. V15
//!   | cpl 0|3 (default 3) | wp 0|1 (CR0.WP, default 1) | smep 0|1 (CR4.SMEP, default 0) | nxe 0|1 (EFER.NXE, default 1)
//!   | fsbase ADDR | gsbase ADDR (FS.base, GS.base, default 0, canonical; no x86 instruction changes them yet)
//!   | sse 0|1 (CR4.OSFXSR and OSXMMEXCPT, default 0) | xmm N LO HI (XMMn's quadwords) | mxcsr VALUE (default 0x1f80; every
//!     exception masked: unmasked SIMD exceptions are not built) (I5a; the report lists `xmm` as 32 quadwords and `mxcsr`)
//!   | switch ADDR 0|3 (repeatable: the one-byte instruction at ADDR, a `nop`, runs as a change of the CPL to the value,
//!     the resident's general CPL change; no x86 instruction changes the CPL yet. Blocks end before it.)
//! CR0 is PE, ET, PG and WP as set, CR4 PAE and SMEP as set, EFER LME, LMA and NXE as set; CPL0 code may change them
//! with system instructions (the resident's transactions).
//! Address-space items: code HEX (a body at the section's rip) | body ADDR HEX | memory ADDR HEX
//!   | identity   (every page maps to the guest-physical page of its own address, below 4 MiB; no `large`)
//!   | region ADDR SIZE   (zero-filled writable pages covering the byte range; eager: guest RAM bounds the total)
//!   | large ADDR   (the 2 MiB-aligned range holding ADDR is one present 2 MiB leaf (PDE with PS), writable if any page
//!     of the space in it is, not executable unless one is code; at most one per run: guest RAM holds one such frame)
//!   | unmap ADDR | readonly ADDR   (mutations: clear P or R/W in the leaf entry of ADDR's page after setup)
//!   | entry ADDR LEVEL set|clear BITS   (mutation: set or clear BITS in the paging-structure entry of ADDR's walk at
//!     LEVEL, 4 = PML4E .. 1 = PTE, after setup; mutations apply in order, and the levels above must be present).
//! Every entry is present and user; leaves of code pages are read-only and executable, the others writable and
//! (with NXE) not executable; the space's tables follow the NXE of the first vCPU using it.
//! The resident's walker refuses what it does not model (a refusal is a halt, never a wrong result): any PWT, PCD or PAT
//! bit (entry bits 3, 4; bit 7 of a PTE; bit 12 of a 2 MiB leaf) makes the page non-write-back (`shadow status=1
//! detail=7`), although under the default PAT some such pages are write-back on x86; PS in a PML4E/PDPTE is reserved
//! (no 1 GiB pages); the A bit of a non-leaf entry is set but never compared.
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
    xmm: [[u64; 2]; 16],
    mxcsr: u64,
    space: usize,
    /// `switch` items: (RIP, CPL).
    switches: Vec<(u64, u64)>,
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
    let new = |space| Section { rip: 0, rflags: 0, gpr: [0; 16], cpl: 3, wp: true, smep: false, nxe: true, fs_base: 0, gs_base: 0, sse: false, xmm: [[0; 2]; 16], mxcsr: exec::MXCSR_RESET, space, switches: vec![] };
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
            ("mxcsr", [v]) if number(v)? & !exec::MXCSR_MASK == 0 && number(v)? & exec::MXCSR_MASKS == exec::MXCSR_MASKS => {
                state(&mut cpu, line)?.mxcsr = number(v)?
            }
            ("cpl", ["0"]) => state(&mut cpu, line)?.cpl = 0,
            ("cpl", ["3"]) => state(&mut cpu, line)?.cpl = 3,
            ("switch", [a, c @ ("0" | "3")]) => state(&mut cpu, line)?.switches.push((number(a)?, c.parse().unwrap())),
            ("wp" | "smep" | "nxe" | "sse", [v @ ("0" | "1")]) => {
                let c = state(&mut cpu, line)?;
                *match words[0] {
                    "wp" => &mut c.wp,
                    "smep" => &mut c.smep,
                    "sse" => &mut c.sse,
                    _ => &mut c.nxe,
                } = *v == "1";
            }
            ("code", [h]) => space.bodies.push((None, hex(h)?)),
            ("body", [a, h]) => space.bodies.push((Some(number(a)?), hex(h)?)),
            ("memory", [a, h]) => space.memory.push((number(a)?, hex(h)?)),
            ("region", [a, n]) if number(n)? > 0 => space.regions.push((number(a)?, number(n)?)),
            ("large", [a]) => space.large.push(number(a)? & !(LARGE - 1)),
            ("identity", []) => space.identity = true,
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
        while self.reserved.contains(&self.next) {
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
    /// Linear page -> (physical page, leaf entry address).
    pages: BTreeMap<u64, (u64, u64)>,
}
impl Tables {
    fn new(b: &mut Builder) -> Result<Self> {
        Ok(Self { ram: b.ram, cr3: b.page()?, pages: BTreeMap::new() })
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
            assert!(self.ram.write(self.physical(va + i as u64), b));
        }
    }
    fn copy_out(&self, va: u64, length: usize) -> Vec<u8> {
        (0..length as u64).map(|i| self.ram.read::<u8>(self.physical(va + i)).unwrap()).collect()
    }
}
/// Builds `space` (for a vCPU starting at `rip` when its bodies sit at the rip): code pages are read-only and
/// executable, region pages writable and not executable (any region covering a code page leaves it executable;
/// without `nxe` nothing is marked not executable).
/// With `identity`, every page maps to the guest-physical page of its own address (the space's guest code can then
/// build page tables of its own: its physical addresses are its linear ones); no `large` pages.
fn build(b: &mut Builder, space: &Space, rip: u64, nxe: bool) -> Result<Tables> {
    let no_execute = if nxe { NO_EXECUTE } else { 0 };
    if space.identity && !space.large.is_empty() {
        return Err("identity space with a large page".into());
    }
    let mut flags = BTreeMap::new();
    let code_pages: Vec<u64> = bodies(space, rip).flat_map(|(at, code)| page_span(at, code.len())).collect();
    for &page in &code_pages {
        flags.insert(page, 0);
    }
    let ranges = space.memory.iter().map(|(a, d)| (*a, d.len() as u64)).chain(space.regions.iter().copied());
    for (address, length) in ranges {
        if length > exec::GUEST_RAM_BYTES as u64 {
            return Err("guest RAM exhausted".into());
        }
        for page in page_span(address, length as usize) {
            flags.insert(page, WRITABLE | if code_pages.contains(&page) { 0 } else { no_execute });
        }
    }
    let mut t = Tables::new(b)?;
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
/// never mapped or fetched: reaching it ends the run (`Stop::Logged`, before any fetch) and the report reads the frame
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
                     [--code-shape] [--code-cache BYTES] [--watchdog-ms MS] [--tamper-vcpu V] [--forge-exit] [--tamper offset|word|record|system] [--tamper-after word|record] \
                     [--forge-stop] [--forge-entry HEX[,HEX]...] [--kick-vcpu V]... [--kicks M] [--kick-every-us P] [--kick-jitter-us J] [--tamper next-page|site|site-kind|armed|dispatch|target] [--ignore-kicks] [--kill-compiler N] [--stall-compiler N] [--compile-deadline-ms MS]";

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

/// What one vCPU's thread returns.
struct Ran {
    stop: Stop,
    compiles: u64,
    slowest: std::time::Duration,
    blocks: u64,
    /// The distinct RIPs of the blocks the host translated for it.
    rips: std::collections::HashSet<u64>,
    tampered: bool,
}
/// A vCPU's own end (it parks; peers run on). Any other stop cancels the peers.
fn finished(stop: &Stop) -> bool {
    matches!(
        stop,
        Stop::Reached | Stop::Logged | Stop::FetchFault(_) | Stop::NoCode(_) | Stop::Resident { stop: exec::stop::SHUTDOWN | exec::stop::UNSUPPORTED_ATOMIC, .. }
    )
}

pub(crate) fn run(args: &[String]) -> Result<()> {
    let [_, resident, _, fexc, description, _work, options @ ..] = args else {
        return Err(USAGE.into());
    };
    let (mut max_instructions, mut code_bytes, mut watchdog_ms, mut forge, mut target) = (None, exec::cache::CODE_BYTES, 10_000, false, 0);
    let (mut tamper, mut tamper_after): (Option<fn(Area, &mut State) -> bool>, Option<Corrupt>) = (None, None);
    let (mut forge_stop, mut forged, mut kicked, mut kicks, mut every_us, mut jitter_us) = (false, None, Vec::new(), 10_000, 0, 0);
    let (mut ignore_kicks, mut signal_at, mut deadline_ms) = (false, None, execution::COMPILE_DEADLINE.as_millis() as u64);
    let mut code_shape = false;
    let mut options = options.iter();
    while let Some(option) = options.next() {
        let mut value = || -> Result<usize> { options.next().and_then(|v| v.parse().ok()).ok_or(USAGE.into()) };
        match option.as_str() {
            "--code-shape" => code_shape = true,
            "--code-cache" => code_bytes = value()?,
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
            "--tamper" => {
                tamper = Some(match options.next().map(String::as_str) {
                    Some("offset") => tamper_offset,
                    Some("word") => tamper_word,
                    Some("record") => tamper_record,
                    Some("system") => tamper_system,
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
    let backing = execution::prepare(resident, code_bytes, n)?;
    require_vcpus(n)?;
    let mut builder = Builder { ram: execution::guest_ram(&backing.memory), next: 0x1000, top: exec::GUEST_RAM_BYTES as u64, reserved: Default::default() };
    // Identity-mapped pages first: the allocator never hands them out (the logging pages of identity spaces too).
    for c in d.cpus.iter().filter(|c| d.spaces[c.space].identity) {
        for page in identity_pages(&d.spaces[c.space], c.rip) {
            if page >= exec::GUEST_RAM_BYTES as u64 || page == 0 {
                return Err(format!("identity page {page:#x} outside guest RAM (or page 0)"));
            }
            if (LOG_IDENTITY..LOG_IDENTITY + 0x7000).contains(&page) {
                return Err(format!("identity page {page:#x} is the driver's logging IDT"));
            }
            builder.reserved.insert(page);
        }
        builder.reserved.extend(LOG_PAGES.map(|o| LOG_IDENTITY + o));
    }
    // A space is built for the first vCPU using it (its rip places `code` bodies).
    let mut spaces: Vec<Option<Tables>> = d.spaces.iter().map(|_| None).collect();
    let mut ends = Vec::new();
    for c in &d.cpus {
        if spaces[c.space].is_none() {
            spaces[c.space] = Some(build(&mut builder, &d.spaces[c.space], c.rip, c.nxe)?);
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
    execution::guest_ram_written(&backing.memory);
    for (v, c) in d.cpus.iter().enumerate() {
        // 4-level paging; user mode, NX and WP on unless the section says otherwise.
        let bit = |on: bool, value: u64| if on { value } else { 0 };
        execution::set_cpu(
            backing.vcpu(v),
            Cpu {
                gpr: c.gpr,
                rip: c.rip,
                rflags: c.rflags | 0x202,
                cr0: exec::CR0_PE | exec::CR0_ET | exec::CR0_PG | bit(c.wp, exec::CR0_WP),
                cr3: spaces[c.space].as_ref().unwrap().cr3,
                cr4: exec::CR4_PAE | bit(c.smep, exec::CR4_SMEP) | bit(c.sse, exec::CR4_OSFXSR | exec::CR4_OSXMMEXCPT),
                efer: exec::EFER_LME | exec::EFER_LMA | bit(c.nxe, exec::EFER_NXE),
                cpl: c.cpl,
                fs_base: c.fs_base,
                gs_base: c.gs_base,
                xmm: c.xmm,
                mxcsr: c.mxcsr,
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
    }

    let machine = Machine::create(&backing.memory)?;
    let stops = execution::Stops::new(&backing, !kicked.is_empty());
    // vCPU v passed the start barrier: every vCPU exists. A kicker waits for it: an SPI rung while vCPUs are being
    // created would also find no vCPU to take it. Its end (`Stops::end`, under its kick mutex) stops its kicker.
    let started: Vec<AtomicBool> = (0..n).map(|_| AtomicBool::new(false)).collect();
    let watchdog = std::time::Duration::from_millis(watchdog_ms);
    let run_vcpu = |v: usize, cpu: &Vcpu, cancelled: &AtomicBool| -> Result<(Stop, Compiler, execution::Requests)> {
        let mut compiler = Compiler::new(Path::new(fexc), cpu, max_instructions)?;
        compiler.deadline = std::time::Duration::from_millis(deadline_ms);
        compiler.shape = code_shape;
        let handlers = log_base(d.spaces[d.cpus[v].space].identity) + LOG_HANDLERS;
        let mut requests = execution::Requests { switches: d.cpus[v].switches.iter().copied().collect(), handlers: Some(handlers..handlers + 0x1000), ..Default::default() };
        if v == target {
            compiler.tamper = forge.then_some(forge_exit as fn(&mut [u8]));
            compiler.signal_at = signal_at;
            requests.tamper = if forged_entry.is_some() { Some(tamper_forge_entry) } else { tamper };
        }
        // A peer that never answers: alive (no watchdog of its own), never stopping.
        let watchdog = if v == target && ignore_kicks { std::time::Duration::from_secs(86_400) } else { watchdog };
        let stop = execution::run(cpu, backing.vcpu(v), &mut compiler, Some(stop_at[v]), watchdog, &mut requests, &stops, cancelled)?;
        Ok((stop, compiler, requests))
    };
    let body = |v: usize, cpu: &Vcpu, cancelled: &AtomicBool| -> Result<Ran> {
        started[v].store(true, SeqCst);
        let ran = run_vcpu(v, cpu, cancelled);
        stops.end(v);
        let (stop, compiler, requests) = ran?;
        if !finished(&stop) {
            owners::cancel_owners(cancelled);
        }
        Ok(Ran { stop, compiles: compiler.compiles, slowest: compiler.slowest, blocks: requests.blocks, rips: requests.rips, tampered: requests.tampered })
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
    // One kicker per kicked vCPU, joined before the VM goes (a protect needs the VM).
    let (kicked, supervision) = std::thread::scope(|scope| {
        let kickers: Vec<_> = kicked
            .iter()
            .map(|&v| {
                let (stops, started, machine, backing) = (&stops, &started, &machine, &backing);
                scope.spawn(move || -> Result<()> {
                    let mut rng = 0x2545_f491_4f6c_dd1d_u64 ^ (v as u64 + 1) << 32;
                    while !started[v].load(SeqCst) && !stops.ended(v) {
                        std::thread::sleep(std::time::Duration::from_micros(20));
                    }
                    for _ in 0..kicks {
                        let area = backing.vcpu(v);
                        let taken = stops.taken(area);
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
        let supervision = owners::supervise(n, &prepare, &body);
        for v in 0..n {
            stops.end(v); // a vCPU that never started
        }
        let kicked: Result<()> = kickers.into_iter().map(|k| k.join().unwrap_or(Err("kicker panicked".into()))).collect();
        // Every vCPU and kicker has ended: a doorbell still pending is a kick no stop followed.
        let kicked = kicked.and_then(|()| (0..n).try_for_each(|v| stops.pending_at_end(&machine, v)));
        (kicked, supervision)
    });
    // Every vCPU is destroyed (or never created): the VM goes next, then the host reads the backing.
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
        out.push(json + &kick_report(backing.vcpu(v), &stops) + "}");
    }
    if d.sections {
        println!("{{\"cpus\":[{}]}}", out.join(","));
    } else {
        println!("{}", out[0]);
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
    if matches!(ran.stop, Stop::Logged) {
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
        Stop::Logged => ("guest_fault", "delivered".into(), s.fault),
        Stop::Resident { stop: exec::stop::SHUTDOWN, .. } => ("shutdown", "triple fault".into(), s.fault),
        Stop::Resident { stop, status, detail } => ("resident", format!("{} status={status} detail={detail}", exec::stop::name(*stop)), [0; 4]),
        Stop::Watchdog { pc, el0, asked } => {
            let place = if *el0 { "guest" } else { "resident" };
            let asked: Vec<u32> = (0..8).filter(|p| asked & 1 << p != 0).collect();
            ("watchdog", format!("{place} pc={pc:#x} shadows_asked={asked:?}"), [0; 4])
        }
        Stop::Cancelled => ("cancelled", String::new(), [0; 4]),
    };
    let mut rips: Vec<u64> = ran.rips.iter().copied().collect();
    rips.sort_unstable();
    let mut out = format!(
        "{{\"vcpu\":{},\"tso\":{},\"stop\":\"{kind}\",\"reason\":\"{reason}\",\"gpr\":[{}],\"rip\":\"{:#x}\",\"rflags\":\"{:#x}\",\"xmm\":[{}],\"mxcsr\":\"{:#x}\",\"memory\":[",
        area.vcpu,
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
    out += "],\"pages\":[";
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
        "],\"stats\":{{\"blocks\":{},\"l2_hits\":{},\"requests\":{},\"distinct\":{},\"host_requests\":{},\"flushes\":{},\"compiles\":{},\"compile_max_us\":{},\"steps\":{},\"shootdowns\":{},\"stops\":{},\"systems\":{},\"invalidations\":{},\"retries\":{},\"upgrades\":{},\"evictions\":{},\"shadow_resets\":{},\"splits\":{},\"linker\":{},\"links\":{},\"unlinks\":{},\"stale_links\":{},\"full_links\":{},\"link_points\":{},\"linked\":{},\"dispatch_exits\":{}}},\"fault\":[{}],\"native\":[{}],\"frame_intact\":{},\"tampered\":{},\"links_check\":\"{}\",\"block_rips\":[{}]{}}}",
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
    let k = stops.report(area.vcpu);
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

/// The FEX frame holds nothing outside its guest-state fields (guest code cannot have written private memory there).
fn frame_intact(area: Area) -> bool {
    let frame: [u8; exec::frame::BYTES] = area.read_record(exec::FRAME_OFFSET);
    let admitted = |i: usize| exec::frame::GUEST_STATE.iter().any(|&(at, len)| (at as usize..(at + len) as usize).contains(&i));
    frame.iter().enumerate().all(|(i, &b)| b == 0 || admitted(i))
}

#[cfg(test)]
mod tests {
    use super::*;
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
        let area = Area { memory: &memory, vcpu: 0, vcpus: 1, epoch: 0, base: 0 };
        let mut s = State::new(0, Cpu { gpr: [0; 16], rip: 0x1000, rflags: 2, cr0: 0, cr3: 0, cr4: 0, efer: 0, cpl: 3, fs_base: 0, gs_base: 0, cr2: 0, cr8: 0, dr: exec::DR_RESET, seg: exec::flat_segments(3), ldtr: exec::LDTR_RESET, tr: exec::TR_RESET, gdtr: exec::TABLE_RESET, idtr: exec::TABLE_RESET, mov_ss: 0, xmm: [[0; 2]; 16], mxcsr: exec::MXCSR_RESET });
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
