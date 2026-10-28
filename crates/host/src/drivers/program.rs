//! Test driver: run one x86-64 program description in the VM.
//!
//! Fixture setup (owned here, not by the emulator): user-mode 4-level page
//! tables mapping exactly the pages of the code and the memory regions, the
//! bytes, and the initial architectural state. The program ends where its
//! bytes end: that RIP is the stop address. Prints the final state as JSON.
//! An optional block size limit makes every program run as several blocks;
//! `--code-cache BYTES` sets the code cache's capacity and `--watchdog-ms MS`
//! the watchdog (default 10 s). After the run the cache's link points are
//! self-checked ([`execution::check_links`]). `--forge-header` (a run-time
//! defence check) makes every block's entry prologue store its entry instead of its
//! header, which admission refuses: the resident's linker must halt.
//!
//! Description (text, one item per line):
//!   rip ADDR | rflags VALUE | gpr V0 .. V15 | code HEX | memory ADDR HEX
//!   | unmap ADDR | readonly ADDR   (mutations: clear P or R/W in the leaf entry of ADDR's page after setup)
use super::super::*;
use crate::execution::{self, Compiler, Stop};
use natemu_abi::execution::{self as exec, Cpu, State};
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
    let mut p = Program { rip: 0, rflags: 0, gpr: [0; 16], code: vec![], memory: vec![], clear: vec![] };
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
                     [--code-cache BYTES] [--watchdog-ms MS] [--forge-header]";

/// `adr x0, #-4` (the block's header) at the entry becomes `adr x0, #0` (the entry itself).
fn forge_header(object: &mut [u8]) {
    let h: natemu_abi::code_object::Header = unsafe { std::ptr::read_unaligned(object.as_ptr().cast()) };
    let at = (h.code_offset + exec::ENTRY_OFFSET) as usize;
    assert_eq!(object[at..at + 4], 0x10ff_ffe0u32.to_le_bytes());
    object[at..at + 4].copy_from_slice(&0x1000_0000u32.to_le_bytes());
}

pub(crate) fn run(args: &[String]) -> Result<()> {
    let [_, resident, _, fexc, description, _work, options @ ..] = args else {
        return Err(USAGE.into());
    };
    let (mut max_instructions, mut code_bytes, mut watchdog_ms, mut forge) = (None, exec::cache::CODE_BYTES, 10_000, false);
    let mut options = options.iter();
    while let Some(option) = options.next() {
        let mut value = || -> Result<usize> { options.next().and_then(|v| v.parse().ok()).ok_or(USAGE.into()) };
        match option.as_str() {
            "--code-cache" => code_bytes = value()?,
            "--watchdog-ms" => watchdog_ms = value()? as u64,
            "--forge-header" => forge = true,
            n if max_instructions.is_none() => max_instructions = Some(n.parse().map_err(|_| "bad block size limit")?),
            _ => return Err(USAGE.into()),
        }
    }
    let p = parse(&fs::read_to_string(description).map_err(|e| e.to_string())?)?;
    // Code pages are read-only and executable; region pages writable and not executable.
    let mut flags = BTreeMap::new();
    for page in page_span(p.rip, p.code.len()) {
        flags.insert(page, 0);
    }
    for (address, data) in &p.memory {
        for page in page_span(*address, data.len()) {
            let code = flags.get(&page) == Some(&0);
            flags.insert(page, WRITABLE | if code { 0 } else { NO_EXECUTE });
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
    let mut requests = execution::Requests::default();
    let watchdog = std::time::Duration::from_millis(watchdog_ms);
    let stop = execution::run(&vm, &mut compiler, Some(p.rip + p.code.len() as u64), watchdog, &mut requests)?;
    let s: State = vm.memory.read_record(exec::STATE_OFFSET);
    let (link_points, linked) = execution::check_links(&vm.memory)?;

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
        "],\"stats\":{{\"blocks\":{},\"fast\":{},\"l2_hits\":{},\"requests\":{},\"distinct\":{},\"host_requests\":{},\"flushes\":{},\"compiles\":{},\"retries\":{},\"upgrades\":{},\"evictions\":{},\"splits\":{},\"linker\":{},\"links\":{},\"unlinks\":{},\"stale_links\":{},\"full_links\":{},\"link_points\":{},\"linked\":{}}},\"fault\":[{}],\"native\":[{}],\"frame_intact\":{}}}",
        s.blocks,
        s.fast,
        s.l2_hits,
        s.requests,
        requests.rips.len(),
        requests.blocks,
        s.generation,
        compiler.compiles,
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
        frame_intact(&vm.memory)
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
        assert_eq!(p.clear, vec![(0x200000, PRESENT), (0x201000, WRITABLE)]);
        for bad in ["rip\ncode 90", "code 9", "gpr 1 2\ncode 90", "", "frob 1\ncode 90"] {
            assert!(parse(bad).is_err(), "{bad:?}");
        }
    }
}
