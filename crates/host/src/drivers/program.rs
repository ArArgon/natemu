//! Test driver: run one x86-64 program description in the VM.
//!
//! Fixture setup (owned here, not by the emulator): user-mode 4-level page
//! tables mapping exactly the pages of the code and the memory regions, the
//! bytes, and the initial architectural state. The program ends where its
//! bytes end: that RIP is the stop address. Prints the final state as JSON.
//! An optional block size limit makes every program run as several blocks;
//! `--code-cache BYTES` sets the code cache's capacity and `--watchdog-ms MS`
//! the watchdog (default 10 s). After the run the cache's link points are
//! self-checked ([`execution::check_links`]). Run-time defence checks, which corrupt what admission or the resident
//! would never produce: `--forge-header` makes every block's entry prologue store its entry instead of its header (the
//! resident's linker must halt); `--tamper KIND` corrupts the cache when the next block will not fit and the resident is
//! about to flush (KIND `offset`: a link point outside the code, so the reply is refused; `word`: a linked point's branch;
//! `literal`: the target literal of a linked point; `record`: a link record), and the unlink must halt (`bad_link`);
//! `--tamper-after KIND` (not `offset`) corrupts after the run, and the host's self-check must refuse (`links_check`).
//!
//! Description (text, one item per line):
//!   rip ADDR | rflags VALUE | gpr V0 .. V15 | code HEX | memory ADDR HEX
//!   | region ADDR SIZE   (zero-filled writable pages covering the byte range; eager: guest RAM bounds the total)
//!   | unmap ADDR | readonly ADDR   (mutations: clear P or R/W in the leaf entry of ADDR's page after setup)
use super::super::*;
use crate::execution::{self, Compiler, Stop};
use natemu_abi::execution::{self as exec, Cpu, State, cache};
use natemu_abi::guest_memory::GuestRam;
use natemu_abi::paging::Fault;
use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::path::Path;

struct Program {
    rip: u64,
    rflags: u64,
    gpr: [u64; 16],
    code: Vec<u8>,
    memory: Vec<(u64, Vec<u8>)>,
    /// Zero-filled writable ranges (address, length).
    regions: Vec<(u64, u64)>,
    /// Leaf-entry bits cleared after setup, per page.
    clear: Vec<(u64, u64)>,
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
fn parse(text: &str) -> Result<Program> {
    let mut p = Program { rip: 0, rflags: 0, gpr: [0; 16], code: vec![], memory: vec![], regions: vec![], clear: vec![] };
    for line in text.lines().filter(|l| !l.trim().is_empty()) {
        let words: Vec<_> = line.split_whitespace().collect();
        match (words[0], &words[1..]) {
            ("rip", [v]) => p.rip = number(v)?,
            ("rflags", [v]) => p.rflags = number(v)?,
            ("gpr", values) if values.len() == 16 => {
                for (r, v) in p.gpr.iter_mut().zip(values) {
                    *r = number(v)?;
                }
            }
            ("code", [h]) => p.code = hex(h)?,
            ("memory", [a, h]) => p.memory.push((number(a)?, hex(h)?)),
            ("region", [a, n]) if number(n)? > 0 => p.regions.push((number(a)?, number(n)?)),
            ("unmap", [a]) => p.clear.push((number(a)? & !4095, PRESENT)),
            ("readonly", [a]) => p.clear.push((number(a)? & !4095, WRITABLE)),
            _ => return Err(format!("bad description line: {line}")),
        }
    }
    if p.code.is_empty() {
        return Err("no code".into());
    }
    Ok(p)
}

const PRESENT: u64 = 1;
const WRITABLE: u64 = 2;
const USER: u64 = 4;
const NO_EXECUTE: u64 = 1 << 63;
const ADDRESS: u64 = natemu_abi::paging::PA_MASK;

/// Guest-physical page allocation and 4-level user mappings.
struct Builder {
    ram: GuestRam,
    next: u64,
    cr3: u64,
    /// Linear page -> (physical page, leaf entry address).
    pages: BTreeMap<u64, (u64, u64)>,
}
impl Builder {
    fn new(ram: GuestRam) -> Result<Self> {
        let mut b = Self { ram, next: 0x1000, cr3: 0, pages: BTreeMap::new() };
        b.cr3 = b.page()?;
        Ok(b)
    }
    fn page(&mut self) -> Result<u64> {
        let gpa = self.next;
        if gpa + 4096 > exec::GUEST_RAM_BYTES as u64 {
            return Err("guest RAM exhausted".into());
        }
        self.next += 4096;
        Ok(gpa)
    }
    fn map(&mut self, va: u64, flags: u64) -> Result<()> {
        let mut table = self.cr3;
        for level in (2..=4).rev() {
            let at = table + ((va >> (12 + 9 * (level - 1))) & 511) * 8;
            let entry = self.ram.read::<u64>(at).unwrap();
            table = if entry & PRESENT != 0 {
                entry & ADDRESS
            } else {
                let next = self.page()?;
                assert!(self.ram.write(at, next | PRESENT | WRITABLE | USER));
                next
            };
        }
        let leaf = table + ((va >> 12) & 511) * 8;
        let gpa = self.page()?;
        assert!(self.ram.write(leaf, gpa | PRESENT | USER | flags));
        self.pages.insert(va, (gpa, leaf));
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
fn page_span(start: u64, length: usize) -> impl Iterator<Item = u64> {
    (start & !4095..start + length as u64).step_by(4096)
}

const USAGE: &str = "usage: natemu-host RUNTIME.elf run-program FEXC PROGRAM.txt WORKDIR [MAX_BLOCK_INSTRUCTIONS] \
                     [--code-cache BYTES] [--watchdog-ms MS] [--forge-header] [--tamper offset|word|literal|record] [--tamper-after word|literal|record]";

/// `adr x0, #-4` (the block's header) at the entry becomes `adr x0, #0` (the entry itself).
fn forge_header(object: &mut [u8]) {
    let h: natemu_abi::code_object::Header = unsafe { std::ptr::read_unaligned(object.as_ptr().cast()) };
    let at = (h.code_offset + exec::ENTRY_OFFSET) as usize;
    assert_eq!(object[at..at + 4], 0x10ff_ffe0u32.to_le_bytes());
    object[at..at + 4].copy_from_slice(&0x1000_0000u32.to_le_bytes());
}

/// The first link record and its source block, if the vCPU has linked anything.
fn first_link(memory: &Memory) -> Option<(cache::Block, cache::LinkRecord)> {
    let control: cache::Control = memory.read_record(cache::LOOKUP_OFFSET + cache::CONTROL);
    (control.links > 0).then(|| {
        let r = execution::link_record(memory, 0);
        (execution::placed_blocks(memory)[r.source as usize], r)
    })
}
/// The resident is about to flush: the staged block does not fit (as `cache::place` computes) and links exist.
fn flushing_next(memory: &Memory) -> bool {
    let control: cache::Control = memory.read_record(cache::LOOKUP_OFFSET + cache::CONTROL);
    let h: natemu_abi::code_object::Header = memory.read_record(exec::STAGING_OFFSET);
    control.links > 0 && control.code_used.next_multiple_of(64) + u64::from(h.code_size) > control.code_bytes
}
#[derive(Clone, Copy)]
enum Corrupt {
    Word,
    Literal,
    Record,
}
/// Corrupts the first linked point, its literal or its record; false when nothing is linked yet.
fn corrupt(memory: &Memory, kind: Corrupt) -> bool {
    let Some((b, r)) = first_link(memory) else { return false };
    let at = cache::CODE_OFFSET + b.code as usize + r.offset as usize;
    match kind {
        Corrupt::Word => memory.copy(at, &0x1400_0001u32.to_le_bytes()), // b .+4
        Corrupt::Literal => {
            let literal = at.wrapping_add((((r.original << 8) as i32 >> 13) as i64 * 4) as usize);
            let value = u64::from_le_bytes(memory.read::<8>(literal)) ^ 1 << 20;
            memory.copy(literal, &value.to_le_bytes());
        }
        Corrupt::Record => {
            let record = cache::LinkRecord { original: r.original.wrapping_add(1 << 5), ..r };
            memory.write_record(cache::LOOKUP_OFFSET + cache::LINKS, record); // the literal one word further
        }
    }
    true
}
fn tamper_offset(memory: &Memory, state: &mut State) -> bool {
    let h: natemu_abi::code_object::Header = memory.read_record(exec::STAGING_OFFSET);
    let Some(p) = state.link_points.iter_mut().find(|p| p.offset != 0) else { return false };
    p.offset = h.code_size; // a whole exit no longer fits
    true
}
fn tamper_word(memory: &Memory, _: &mut State) -> bool {
    flushing_next(memory) && corrupt(memory, Corrupt::Word)
}
fn tamper_literal(memory: &Memory, _: &mut State) -> bool {
    flushing_next(memory) && corrupt(memory, Corrupt::Literal)
}
fn tamper_record(memory: &Memory, _: &mut State) -> bool {
    flushing_next(memory) && corrupt(memory, Corrupt::Record)
}

pub(crate) fn run(args: &[String]) -> Result<()> {
    let [_, resident, _, fexc, description, _work, options @ ..] = args else {
        return Err(USAGE.into());
    };
    let (mut max_instructions, mut code_bytes, mut watchdog_ms, mut forge) = (None, exec::cache::CODE_BYTES, 10_000, false);
    let (mut tamper, mut tamper_after): (Option<fn(&Memory, &mut State) -> bool>, Option<Corrupt>) = (None, None);
    let mut options = options.iter();
    while let Some(option) = options.next() {
        let mut value = || -> Result<usize> { options.next().and_then(|v| v.parse().ok()).ok_or(USAGE.into()) };
        match option.as_str() {
            "--code-cache" => code_bytes = value()?,
            "--watchdog-ms" => watchdog_ms = value()? as u64,
            "--forge-header" => forge = true,
            "--tamper" => {
                tamper = Some(match options.next().map(String::as_str) {
                    Some("offset") => tamper_offset,
                    Some("word") => tamper_word,
                    Some("literal") => tamper_literal,
                    Some("record") => tamper_record,
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
    let p = parse(&fs::read_to_string(description).map_err(|e| e.to_string())?)?;
    // Code pages are read-only and executable; region pages writable and not executable.
    let mut flags = BTreeMap::new();
    let code_pages: Vec<u64> = page_span(p.rip, p.code.len()).collect();
    for &page in &code_pages {
        flags.insert(page, 0);
    }
    let ranges = p.memory.iter().map(|(a, d)| (*a, d.len() as u64)).chain(p.regions.iter().copied());
    for (address, length) in ranges {
        if length > exec::GUEST_RAM_BYTES as u64 {
            return Err("guest RAM exhausted".into());
        }
        for page in page_span(address, length as usize) {
            // Any region covering a code page leaves it executable.
            flags.insert(page, WRITABLE | if code_pages.contains(&page) { 0 } else { NO_EXECUTE });
        }
    }
    let memory = execution::prepare(resident, code_bytes)?;
    let mut guest = Builder::new(execution::guest_ram(&memory))?;
    for (&page, &f) in &flags {
        guest.map(page, f)?;
    }
    guest.copy_in(p.rip, &p.code);
    for (address, data) in &p.memory {
        guest.copy_in(*address, data);
    }
    for &(page, bits) in &p.clear {
        let &(_, leaf) = guest.pages.get(&page).ok_or(format!("{page:#x} is not mapped"))?;
        let entry = guest.ram.read::<u64>(leaf).unwrap();
        assert!(guest.ram.write(leaf, entry & !bits));
    }
    execution::guest_ram_written(&memory);
    // User mode (as the QEMU reference), 4-level paging, NX and WP on.
    execution::set_cpu(
        &memory,
        Cpu {
            gpr: p.gpr,
            rip: p.rip,
            rflags: p.rflags | 0x202,
            cr0: exec::CR0_PE | exec::CR0_PG | exec::CR0_WP,
            cr3: guest.cr3,
            cr4: exec::CR4_PAE,
            efer: exec::EFER_LME | exec::EFER_LMA | exec::EFER_NXE,
            cpl: 3,
        },
    );

    let vm = Vm::new(memory)?;
    let mut compiler = Compiler::new(Path::new(fexc), &vm.cpu, max_instructions)?;
    if forge {
        compiler.tamper = Some(forge_header);
    }
    vm.cpu.enter_resident(0, natemu_abi::shadow::TCR_BOTH)?;
    let mut requests = execution::Requests { tamper, ..Default::default() };
    let watchdog = std::time::Duration::from_millis(watchdog_ms);
    let stop = execution::run(&vm, &mut compiler, Some(p.rip + p.code.len() as u64), watchdog, &mut requests)?;
    let s: State = vm.memory.read_record(exec::STATE_OFFSET);
    let mut tampered = requests.tampered;
    if let Some(kind) = tamper_after {
        tampered = corrupt(&vm.memory, kind);
    }
    // Under a tamper option the self-check's verdict is reported, not fatal.
    let (link_points, linked, links_check) = match execution::check_links(&vm.memory) {
        Ok((points, linked)) => (points, linked, "ok".to_string()),
        Err(e) if tamper.is_some() || tamper_after.is_some() => (0, 0, e),
        Err(e) => return Err(e),
    };

    // A guest exception (resident: at an access site or stub; host: instruction fetch) with its record.
    let (kind, reason, fault) = match &stop {
        Stop::Reached => ("reached", String::new(), [0; 4]),
        Stop::FetchFault(Fault::Page { address, error }) => ("guest_fault", "fetch".into(), [14, *address, *error, s.cpu.rip]),
        Stop::FetchFault(Fault::Noncanonical) => ("guest_fault", "fetch".into(), [13, 0, 0, s.cpu.rip]),
        Stop::FetchFault(f) => ("fetch_fault", format!("{f:?}"), [0; 4]),
        Stop::NoCode(reason) => ("no_code", reason.clone(), [0; 4]),
        Stop::Resident { stop: exec::stop::GUEST_FAULT, .. } => ("guest_fault", "resident".into(), s.fault),
        Stop::Resident { stop, status, detail } => ("resident", format!("{} status={status} detail={detail}", exec::stop::name(*stop)), [0; 4]),
        Stop::Watchdog => ("watchdog", String::new(), [0; 4]),
    };
    let list = |v: &[u64]| v.iter().map(|x| format!("\"{x:#x}\"")).collect::<Vec<_>>().join(",");
    let mut out = format!(
        "{{\"stop\":\"{kind}\",\"reason\":\"{reason}\",\"gpr\":[{}],\"rip\":\"{:#x}\",\"rflags\":\"{:#x}\",\"memory\":[",
        list(&s.cpu.gpr),
        s.cpu.rip,
        s.cpu.rflags
    );
    for (i, (address, data)) in p.memory.iter().enumerate() {
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
        "],\"stats\":{{\"blocks\":{},\"fast\":{},\"l2_hits\":{},\"requests\":{},\"distinct\":{},\"host_requests\":{},\"flushes\":{},\"compiles\":{},\"steps\":{},\"invalidations\":{},\"retries\":{},\"upgrades\":{},\"evictions\":{},\"splits\":{},\"linker\":{},\"links\":{},\"unlinks\":{},\"stale_links\":{},\"full_links\":{},\"link_points\":{},\"linked\":{}}},\"fault\":[{}],\"native\":[{}],\"frame_intact\":{},\"tampered\":{},\"links_check\":\"{}\"}}",
        s.blocks,
        s.fast,
        s.l2_hits,
        s.requests,
        requests.rips.len(),
        requests.blocks,
        s.generation,
        compiler.compiles,
        s.steps,
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
        frame_intact(&vm.memory),
        tampered,
        links_check.replace('"', "'")
    );
    println!("{out}");
    Ok(())
}

/// The FEX frame holds nothing but its admitted fields (guest code cannot have written
/// private memory there), and its native pointers (exit gate, division helpers) still point into the resident.
fn frame_intact(memory: &Memory) -> bool {
    let frame: [u8; exec::frame::BYTES] = memory.read_record(exec::FRAME_OFFSET);
    let admitted = |i: usize| exec::frame::FIELDS.iter().any(|&(at, len)| (at as usize..(at + len) as usize).contains(&i));
    let text = |offset: usize| PRIVATE + offset as u64;
    let in_text = |at: u32| {
        let pointer = u64::from_le_bytes(frame[at as usize..][..8].try_into().unwrap());
        (text(natemu_abi::resident::TEXT_OFFSET)..text(natemu_abi::resident::TEXT_END)).contains(&pointer)
    };
    frame.iter().enumerate().all(|(i, &b)| b == 0 || admitted(i)) && exec::frame::POINTERS.into_iter().all(in_text)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn description_parses_and_rejects_malformed_lines() {
        let p = parse("rip 0x100000\nrflags 0x1\ngpr 0 1 2 3 4 5 6 7 8 9 a b c d e f\ncode 90c3\nmemory 0x200000 0102\nunmap 0x200010\nreadonly 0x201000\n").unwrap();
        assert_eq!((p.rip, p.rflags, p.gpr[15], p.code, p.memory), (0x100000, 1, 15, vec![0x90, 0xc3], vec![(0x200000, vec![1, 2])]));
        assert_eq!(parse("region 1000 2000\ncode 90").unwrap().regions, vec![(0x1000, 0x2000)]);
        assert_eq!(p.clear, vec![(0x200000, PRESENT), (0x201000, WRITABLE)]);
        for bad in ["rip\ncode 90", "code 9", "gpr 1 2\ncode 90", "", "frob 1\ncode 90", "region 1 0\ncode 90"] {
            assert!(parse(bad).is_err(), "{bad:?}");
        }
    }
}
