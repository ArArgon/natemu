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
//! would never produce, on vCPU `--tamper-vcpu V` (default 0): `--forge-header` makes every block's entry prologue store
//! its entry instead of its header (the resident's linker must halt); `--tamper KIND` corrupts the cache when the next
//! block will not fit and the resident is about to flush (KIND `offset`: a link point outside the code, so the reply is
//! refused; `word`: a linked point's branch; `literal`: the target literal of a linked point; `record`: a link record),
//! and the unlink must halt (`bad_link`); `--tamper-after KIND` (not `offset`) corrupts after the run, and the host's
//! self-check must refuse (`links_check`); `--forge-stop` enters the stop gate of its first stop with a forged ELR
//! (the resident must halt: `bad_stop`); `--tamper system` makes the first system reply's decode malformed (the resident
//! must refuse it: `bad_reply`).
//!
//! Precise stops: `--kick-vcpu V` (repeatable) runs a host kicker thread per listed vCPU that kicks it `--kicks M`
//! times (default 10,000), each after the previous kick's stop was taken (or the vCPU ended) and `--kick-every-us P`
//! (default 0) more; kicks while the vCPU is in the host or the resident are part of the stream. Each vCPU's output
//! has its `kicks` accounting (issued, of which found it in the host, merged, stops taken, still pending at its end),
//! the native PCs of its stops with counts (`stops_at_entry`: every one is a stop store of a block it placed) and the
//! latencies from each protect's return to the stop's exit.
//!
//! vCPUs run concurrently over shared guest RAM, each until its own stop. A vCPU that stops for any other reason than
//! reaching its end, a guest fault, code it cannot compile or a split lock (`unsupported_atomic`: a precise stop before the
//! instruction, like a guest fault) cancels the others (another resident halt, the watchdog).
//!
//! Description (text, one item per line). State items: rip ADDR | rflags VALUE | gpr V0 .. V15
//!   | cpl 0|3 (default 3) | wp 0|1 (CR0.WP, default 1) | smep 0|1 (CR4.SMEP, default 0) | nxe 0|1 (EFER.NXE, default 1).
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
    space: usize,
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
    let new = |space| Section { rip: 0, rflags: 0, gpr: [0; 16], cpl: 3, wp: true, smep: false, nxe: true, space };
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
            ("cpl", ["0"]) => state(&mut cpu, line)?.cpl = 0,
            ("cpl", ["3"]) => state(&mut cpu, line)?.cpl = 3,
            ("wp" | "smep" | "nxe", [v @ ("0" | "1")]) => {
                let c = state(&mut cpu, line)?;
                *match words[0] {
                    "wp" => &mut c.wp,
                    "smep" => &mut c.smep,
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
        let leaf = self.entry(Some(b), va, 1)?;
        let gpa = if identity { va } else { b.page()? };
        assert!(self.ram.write(leaf, gpa | PRESENT | USER | flags));
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

const USAGE: &str = "usage: natemu-host RUNTIME.elf run-program FEXC PROGRAM.txt WORKDIR [MAX_BLOCK_INSTRUCTIONS] \
                     [--code-cache BYTES] [--watchdog-ms MS] [--tamper-vcpu V] [--forge-header] [--tamper offset|word|literal|record|system] [--tamper-after word|literal|record] \
                     [--forge-stop] [--kick-vcpu V]... [--kicks M] [--kick-every-us P]";

/// `adr x0, #-4` (the block's header) at the entry becomes `adr x0, #0` (the entry itself).
fn forge_header(object: &mut [u8]) {
    let h: natemu_abi::code_object::Header = unsafe { std::ptr::read_unaligned(object.as_ptr().cast()) };
    let at = (h.code_offset + exec::ENTRY_OFFSET) as usize;
    assert_eq!(object[at..at + 4], 0x10ff_ffe0u32.to_le_bytes());
    object[at..at + 4].copy_from_slice(&0x1000_0000u32.to_le_bytes());
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
    Literal,
    Record,
}
/// Corrupts the first linked point, its literal or its record; false when nothing is linked yet.
fn corrupt(area: Area, kind: Corrupt) -> bool {
    let Some((b, r)) = first_link(area) else { return false };
    let at = cache::CODE_OFFSET + b.code as usize + r.offset as usize;
    match kind {
        Corrupt::Word => area.copy(at, &0x1400_0001u32.to_le_bytes()), // b .+4
        Corrupt::Literal => {
            let literal = at.wrapping_add((((r.original << 8) as i32 >> 13) as i64 * 4) as usize);
            let value = u64::from_le_bytes(area.read::<8>(literal)) ^ 1 << 20;
            area.copy(literal, &value.to_le_bytes());
        }
        Corrupt::Record => {
            let record = cache::LinkRecord { original: r.original.wrapping_add(1 << 5), ..r };
            area.write_record(cache::LOOKUP_OFFSET + cache::LINKS, record); // the literal one word further
        }
    }
    true
}
fn tamper_offset(area: Area, state: &mut State) -> bool {
    let h: natemu_abi::code_object::Header = area.read_record(exec::STAGING_OFFSET);
    let Some(p) = state.link_points.iter_mut().find(|p| p.offset != 0) else { return false };
    p.offset = h.code_size; // a whole exit no longer fits
    true
}
/// A system reply whose decode the resident must refuse (`bad_reply`): an instruction length of 16.
fn tamper_system(_: Area, state: &mut State) -> bool {
    let reply = state.system.kind != 0;
    if reply {
        state.system.length = 16;
    }
    reply
}
fn tamper_word(area: Area, _: &mut State) -> bool {
    flushing_next(area) && corrupt(area, Corrupt::Word)
}
fn tamper_literal(area: Area, _: &mut State) -> bool {
    flushing_next(area) && corrupt(area, Corrupt::Literal)
}
fn tamper_record(area: Area, _: &mut State) -> bool {
    flushing_next(area) && corrupt(area, Corrupt::Record)
}

/// What one vCPU's thread returns.
struct Ran {
    stop: Stop,
    compiles: u64,
    blocks: u64,
    distinct: usize,
    tampered: bool,
}
/// A vCPU's own end (it parks; peers run on). Any other stop cancels the peers.
fn finished(stop: &Stop) -> bool {
    matches!(
        stop,
        Stop::Reached | Stop::FetchFault(_) | Stop::NoCode(_) | Stop::Resident { stop: exec::stop::GUEST_FAULT | exec::stop::UNSUPPORTED_ATOMIC, .. }
    )
}

pub(crate) fn run(args: &[String]) -> Result<()> {
    let [_, resident, _, fexc, description, _work, options @ ..] = args else {
        return Err(USAGE.into());
    };
    let (mut max_instructions, mut code_bytes, mut watchdog_ms, mut forge, mut target) = (None, exec::cache::CODE_BYTES, 10_000, false, 0);
    let (mut tamper, mut tamper_after): (Option<fn(Area, &mut State) -> bool>, Option<Corrupt>) = (None, None);
    let (mut forge_stop, mut kicked, mut kicks, mut every_us) = (false, Vec::new(), 10_000, 0);
    let mut options = options.iter();
    while let Some(option) = options.next() {
        let mut value = || -> Result<usize> { options.next().and_then(|v| v.parse().ok()).ok_or(USAGE.into()) };
        match option.as_str() {
            "--code-cache" => code_bytes = value()?,
            "--watchdog-ms" => watchdog_ms = value()? as u64,
            "--tamper-vcpu" => target = value()?,
            "--forge-header" => forge = true,
            "--forge-stop" => forge_stop = true,
            "--kick-vcpu" => kicked.push(value()?),
            "--kicks" => kicks = value()?,
            "--kick-every-us" => every_us = value()? as u64,
            "--tamper" => {
                tamper = Some(match options.next().map(String::as_str) {
                    Some("offset") => tamper_offset,
                    Some("word") => tamper_word,
                    Some("literal") => tamper_literal,
                    Some("record") => tamper_record,
                    Some("system") => tamper_system,
                    _ => return Err(USAGE.into()),
                })
            }
            "--tamper-after" => {
                tamper_after = Some(match options.next().map(String::as_str) {
                    Some("word") => Corrupt::Word,
                    Some("literal") => Corrupt::Literal,
                    Some("record") => Corrupt::Record,
                    _ => return Err(USAGE.into()),
                })
            }
            n if max_instructions.is_none() => max_instructions = Some(n.parse().map_err(|_| "bad block size limit")?),
            _ => return Err(USAGE.into()),
        }
    }
    let d = parse(&fs::read_to_string(description).map_err(|e| e.to_string())?)?;
    let n = d.cpus.len();
    if kicked.iter().any(|&v| v >= n) {
        return Err(format!("--kick-vcpu: {n} vCPUs"));
    }
    let backing = execution::prepare(resident, code_bytes, n)?;
    require_vcpus(n)?;
    let mut builder = Builder { ram: execution::guest_ram(&backing.memory), next: 0x1000, top: exec::GUEST_RAM_BYTES as u64, reserved: Default::default() };
    // Identity-mapped pages first: the allocator never hands them out.
    for c in d.cpus.iter().filter(|c| d.spaces[c.space].identity) {
        for page in identity_pages(&d.spaces[c.space], c.rip) {
            if page >= exec::GUEST_RAM_BYTES as u64 || page == 0 {
                return Err(format!("identity page {page:#x} outside guest RAM (or page 0)"));
            }
            builder.reserved.insert(page);
        }
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
                cr4: exec::CR4_PAE | bit(c.smep, exec::CR4_SMEP),
                efer: exec::EFER_LME | exec::EFER_LMA | bit(c.nxe, exec::EFER_NXE),
                cpl: c.cpl,
            },
        );
    }

    let machine = Machine::create(&backing.memory)?;
    let stops = execution::Stops::new(&backing, &machine, !kicked.is_empty())?;
    let ended: Vec<AtomicBool> = (0..n).map(|_| AtomicBool::new(false)).collect();
    let watchdog = std::time::Duration::from_millis(watchdog_ms);
    let body = |v: usize, cpu: &Vcpu, cancelled: &AtomicBool| -> Result<Ran> {
        let mut compiler = Compiler::new(Path::new(fexc), cpu, max_instructions)?;
        let mut requests = execution::Requests::default();
        if v == target {
            compiler.tamper = forge.then_some(forge_header as fn(&mut [u8]));
            requests.tamper = tamper;
            requests.forge_stop = forge_stop;
        }
        let stop = execution::run(cpu, backing.vcpu(v), &mut compiler, Some(stop_at[v]), watchdog, &mut requests, &stops, cancelled);
        ended[v].store(true, SeqCst);
        let stop = stop?;
        if !finished(&stop) {
            owners::cancel_owners(cancelled);
        }
        Ok(Ran { stop, compiles: compiler.compiles, blocks: requests.blocks, distinct: requests.rips.len(), tampered: requests.tampered })
    };
    let prepare = |v: usize| -> Result<Vcpu> {
        let cpu = Vcpu::create()?;
        execution::enter(&cpu, backing.vcpu(v))?;
        Ok(cpu)
    };
    // One kicker per kicked vCPU, joined before the VM goes (a protect needs the VM).
    let supervision = std::thread::scope(|scope| {
        let kickers: Vec<_> = kicked
            .iter()
            .map(|&v| {
                let (stops, ended) = (&stops, &ended);
                scope.spawn(move || -> Result<()> {
                    for _ in 0..kicks {
                        if ended[v].load(SeqCst) {
                            break;
                        }
                        let taken = stops.taken(v);
                        stops.kick(v)?;
                        // Spin briefly, then sleep: a spinning kicker would take a core from the vCPUs.
                        for spins in 0.. {
                            if stops.taken(v) != taken || ended[v].load(SeqCst) {
                                break;
                            }
                            if spins < 1000 {
                                std::hint::spin_loop();
                            } else {
                                std::thread::sleep(std::time::Duration::from_micros(20));
                            }
                        }
                        std::thread::sleep(std::time::Duration::from_micros(every_us));
                    }
                    Ok(())
                })
            })
            .collect();
        let supervision = owners::supervise(n, std::time::Duration::MAX, &prepare, &body, &mut || None, &|_| {});
        for e in &ended {
            e.store(true, SeqCst); // a vCPU that never started
        }
        let kicked: Result<()> = kickers.into_iter().map(|k| k.join().unwrap_or(Err("kicker panicked".into()))).collect();
        kicked.and(supervision)
    })?;
    // Every vCPU is destroyed: the VM goes next, then the host reads the backing.
    hv(machine.destroy(), "destroy VM")?;
    if let Some(e) = supervision.error {
        return Err(e);
    }
    let mut out = Vec::new();
    for (v, ran) in supervision.outputs.into_iter().enumerate() {
        let ran = ran.ok_or("vCPU without a result")?;
        let tables = spaces[d.cpus[v].space].as_ref().unwrap();
        let tampering = v == target && (tamper.is_some() || tamper_after.is_some());
        let tampering = tampering || (v == target && forge_stop);
        let mut json = report(backing.vcpu(v), &d.spaces[d.cpus[v].space], tables, ran, tampering, tamper_after.filter(|_| v == target))?;
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

/// One vCPU's final state, its space's memory and A/D bits, and its counters, as JSON. With `tamper_after`, corrupts
/// its cache first; with `tampering` the link self-check's verdict is reported, not fatal.
fn report(area: Area, space: &Space, guest: &Tables, ran: Ran, tampering: bool, tamper_after: Option<Corrupt>) -> Result<String> {
    let s: State = area.read_record(exec::STATE_OFFSET);
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
        Stop::Resident { stop: exec::stop::GUEST_FAULT, .. } => ("guest_fault", "resident".into(), s.fault),
        Stop::Resident { stop, status, detail } => ("resident", format!("{} status={status} detail={detail}", exec::stop::name(*stop)), [0; 4]),
        Stop::Watchdog => ("watchdog", String::new(), [0; 4]),
        Stop::Cancelled => ("cancelled", String::new(), [0; 4]),
    };
    let list = |v: &[u64]| v.iter().map(|x| format!("\"{x:#x}\"")).collect::<Vec<_>>().join(",");
    let mut out = format!(
        "{{\"vcpu\":{},\"tso\":{},\"stop\":\"{kind}\",\"reason\":\"{reason}\",\"gpr\":[{}],\"rip\":\"{:#x}\",\"rflags\":\"{:#x}\",\"memory\":[",
        area.vcpu,
        shared.actlr & 2 != 0,
        list(&s.cpu.gpr),
        s.cpu.rip,
        s.cpu.rflags
    );
    for (i, (address, data)) in space.memory.iter().enumerate() {
        let bytes: String = guest.copy_out(*address, data.len()).iter().map(|b| format!("{b:02x}")).collect();
        let _ = write!(out, "{}{{\"addr\":\"{address:#x}\",\"data\":\"{bytes}\"}}", if i > 0 { "," } else { "" });
    }
    out += "],\"pages\":[";
    for (i, (va, &(_, leaf))) in guest.pages.iter().enumerate() {
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
        "],\"stats\":{{\"blocks\":{},\"fast\":{},\"l2_hits\":{},\"requests\":{},\"distinct\":{},\"host_requests\":{},\"flushes\":{},\"compiles\":{},\"steps\":{},\"shootdowns\":{},\"stops\":{},\"systems\":{},\"invalidations\":{},\"retries\":{},\"upgrades\":{},\"evictions\":{},\"splits\":{},\"linker\":{},\"links\":{},\"unlinks\":{},\"stale_links\":{},\"full_links\":{},\"link_points\":{},\"linked\":{}}},\"fault\":[{}],\"native\":[{}],\"frame_intact\":{},\"tampered\":{},\"links_check\":\"{}\"}}",
        s.blocks,
        s.fast,
        s.l2_hits,
        s.requests,
        ran.distinct,
        ran.blocks,
        s.generation,
        ran.compiles,
        s.steps,
        s.shootdowns,
        s.stops,
        s.systems,
        s.invalidations,
        s.retries,
        s.upgrades,
        s.evictions,
        s.splits,
        s.linker,
        s.links,
        s.unlinks,
        s.stale_links,
        s.full_links,
        link_points,
        linked,
        list(&fault),
        list(&s.native),
        frame_intact(area),
        tampered,
        links_check.replace('"', "'")
    );
    Ok(out)
}

/// `,"kicks":{..}` for vCPU `area.vcpu`: its kick accounting, its stops' PCs and their latency distribution.
fn kick_report(area: Area, stops: &execution::Stops) -> String {
    let (k, pending) = stops.report(area.vcpu);
    let entries: std::collections::HashSet<u64> = execution::placed_blocks(area)
        .iter()
        .map(|b| b.code_va() + u64::from(exec::STOP_STORE))
        .chain([exec::SLOT_VA + u64::from(exec::STOP_STORE)])
        .collect();
    let at_entry = k.pcs.keys().all(|pc| entries.contains(pc));
    let at_step = k.pcs.get(&(exec::SLOT_VA + u64::from(exec::STOP_STORE))).copied().unwrap_or(0);
    let pcs: Vec<String> = k.pcs.iter().map(|(pc, n)| format!("[\"{pc:#x}\",{n}]")).collect();
    let quantiles = |host: bool| {
        let mut ns: Vec<u64> = k.latency.iter().filter(|l| l.1 == host).map(|l| l.0).collect();
        ns.sort_unstable();
        let q = |f: f64| ns.get(((ns.len() as f64 - 1.0) * f).round() as usize).copied().unwrap_or(0);
        format!("{{\"n\":{},\"min\":{},\"p50\":{},\"p90\":{},\"p99\":{},\"p999\":{},\"max\":{}}}", ns.len(), q(0.0), q(0.5), q(0.9), q(0.99), q(0.999), q(1.0))
    };
    format!(
        ",\"kicks\":{{\"issued\":{},\"in_host\":{},\"merged\":{},\"stops\":{},\"pending\":{pending},\"stop_pcs\":[{}],\"stops_at_entry\":{at_entry},\"stops_at_step\":{at_step},\"latency_ns\":{},\"latency_in_host_ns\":{}}}",
        k.issued,
        k.in_host,
        k.merged,
        k.stops,
        pcs.join(","),
        quantiles(false),
        quantiles(true)
    )
}

/// The FEX frame holds nothing but its admitted fields (guest code cannot have written
/// private memory there), and its native pointers (exit gate, division helpers) still point into the resident.
fn frame_intact(area: Area) -> bool {
    let frame: [u8; exec::frame::BYTES] = area.read_record(exec::FRAME_OFFSET);
    // x28 is the page + FRAME_BIAS.
    let bias = exec::FRAME_BIAS as usize;
    let admitted = |i: usize| exec::frame::FIELDS.iter().any(|&(at, len)| (bias + at as usize..bias + (at + len) as usize).contains(&i));
    let text = |offset: usize| PRIVATE + offset as u64;
    let in_text = |at: u32| {
        let pointer = u64::from_le_bytes(frame[bias + at as usize..][..8].try_into().unwrap());
        (text(natemu_abi::resident::TEXT_OFFSET)..text(natemu_abi::resident::TEXT_END)).contains(&pointer)
    };
    frame.iter().enumerate().all(|(i, &b)| b == 0 || admitted(i)) && exec::frame::POINTERS.into_iter().all(in_text)
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
        assert!(parse("identity\ncode 90").unwrap().spaces[0].identity && !parse("code 90").unwrap().spaces[0].identity);
        for bad in ["rip\ncode 90", "code 9", "gpr 1 2\ncode 90", "", "frob 1\ncode 90", "region 1 0\ncode 90", "cpl 1\ncode 90", "wp 2\ncode 90",
                    "entry 0x1000 5 set 1\ncode 90", "entry 0x1000 1 flip 1\ncode 90", "body 0x1000 90\nsmep 1\ncpu\nrip 0x1000"] {
            assert!(parse(bad).is_err(), "{bad:?}");
        }
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
