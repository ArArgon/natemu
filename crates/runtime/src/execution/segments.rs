//! Descriptor tables, segment registers and far transfers in 64-bit mode (plan S2b; SDM Vol.2 LGDT/LIDT, SGDT, SIDT,
//! LLDT, SLDT, LTR, STR, MOV (segment registers), JMP, CALL, RET, IRET; Vol.3 3.4 "Segment Selectors/Descriptors", 5
//! "Protection", 8.2.3 and 8.7 (64-bit TSS)), executed as system transactions: every check raises its exception before
//! any architectural change. Descriptor-table and TSS-descriptor reads and writes are implicit supervisor accesses
//! through the guest walker (never a shadow: the walk is made at CPL 0 whatever the CPL, so a #PF has U/S = 0 and
//! CR0.WP decides writes to read-only pages); operand and stack accesses are explicit, at the CPL. The SDM's
//! accessed-bit write on a segment load and LTR's busy-bit write are real guest writes: a clear A bit in a read-only
//! GDT page faults, as on hardware. A far transfer reads operands and stack slots in the source mode.
//! The destination descriptor selects the target limit or canonicality check. In user compatibility code,
//! `Cpu::compat_refused` checks for a supported flat CS/SS/DS/ES before fetching the first instruction.
//! CPL-0 compatibility code retains the boot interpreter route. Call gates stop at CPL 0 and raise
//! #GP(selector) at CPL 3. See `tools/models/segment_model.py` for transaction order and the IRET rules.
use super::*;
pub(super) mod span;

/// An exception a transaction raises: vector, error code, CR2.
pub(super) type Exception = [u64; 3];
const GP0: Exception = [13, 0, 0];
const SS0: Exception = [12, 0, 0];
fn gp(selector: u16) -> Exception {
    [13, u64::from(selector & 0xfffc), 0]
}
fn np(selector: u16) -> Exception {
    [11, u64::from(selector & 0xfffc), 0]
}
fn ss(selector: u16) -> Exception {
    [12, u64::from(selector & 0xfffc), 0]
}
const RFLAGS_NT: u64 = 1 << 14;
const RFLAGS_IF: u64 = 1 << 9;
const RFLAGS_IOPL: u64 = 3 << 12;
const RFLAGS_VM: u64 = 1 << 17;
/// RFLAGS bits IRET always restores (SDM Vol.2 IRET): CF PF AF ZF SF TF DF OF NT RF AC ID; IF when CPL <= IOPL; IOPL,
/// VIF and VIP at CPL 0.
const IRET_ALWAYS: u64 = 0x25_4dd5;
const IRET_CPL0: u64 = RFLAGS_IOPL | 3 << 19;

/// Executes `i` (LGDT ..= IRET, PUSH_SREG, POP_SREG, LOAD_FAR). `Ok(None)`: committed, RIP moves past it; `Ok(Some(rip))`: a far transfer committed to
/// `rip`. On `Err` the vCPU's state is unchanged (guest memory may hold an accessed bit a failed load set, as the SDM
/// allows).
pub(super) fn execute(s: &mut State, i: System) -> Result<Option<u64>, Exception> {
    let c = s.cpu;
    let selector_operand = |s: &mut State| -> Result<u16, Exception> {
        if i.memory == 0 {
            return Ok(c.gpr[i.reg as usize] as u16);
        }
        let mut b = [0u8; 2];
        linear(s, address(&c, &i, 2, false)?, &mut b, false, false, operand_range(&i))?;
        Ok(u16::from_le_bytes(b))
    };
    match i.kind {
        // The 10-byte image in 64-bit mode (`size` 8), else 6 bytes; a 16-bit LGDT/LIDT keeps 24 bits of the base.
        system::LGDT..=system::LIDT => {
            let mut b = [0u8; 10];
            let n = if i.size == 8 { 10 } else { 6 };
            linear(s, address(&c, &i, n as u64, false)?, &mut b[..n], false, false, operand_range(&i))?;
            let base = u64::from_le_bytes(b[2..].try_into().unwrap());
            let table = Table {
                base: if i.size == 2 { base & 0xff_ffff } else { base },
                limit: u64::from(u16::from_le_bytes([b[0], b[1]])),
            };
            // The base must be canonical (AMD APM Vol.3 LGDT/LIDT; KVM checks it; the Intel SDM lists no such #GP).
            if !paging::canonical(table.base) {
                return Err(GP0);
            }
            *if i.kind == system::LGDT {
                &mut s.cpu.gdtr
            } else {
                &mut s.cpu.idtr
            } = table;
        }
        system::SGDT..=system::SIDT => {
            let t = if i.kind == system::SGDT {
                c.gdtr
            } else {
                c.idtr
            };
            let mut b = [0u8; 10];
            b[..2].copy_from_slice(&(t.limit as u16).to_le_bytes());
            b[2..].copy_from_slice(&t.base.to_le_bytes());
            let n = if i.size == 8 { 10 } else { 6 };
            linear(s, address(&c, &i, n as u64, true)?, &mut b[..n], true, false, operand_range(&i))?;
        }
        system::SLDT..=system::MOV_FROM_SREG => {
            let v = match i.kind {
                system::SLDT => c.ldtr.selector,
                system::STR => c.tr.selector,
                _ => c.seg[i.cr as usize].selector,
            };
            if i.memory == 1 {
                linear(
                    s,
                    address(&c, &i, 2, true)?,
                    &mut v.to_le_bytes(),
                    true,
                    false,
                    operand_range(&i),
                )?;
            } else {
                let r = &mut s.cpu.gpr[i.reg as usize];
                *r = if i.size == 2 {
                    *r & !0xffff | u64::from(v)
                } else {
                    u64::from(v)
                };
            }
        }
        system::LLDT => {
            let selector = selector_operand(s)?;
            s.cpu.ldtr = if selector & 0xfffc == 0 {
                Segment {
                    selector,
                    ..Segment::NULL
                }
            } else {
                system_segment(s, selector, 2)?.1
            };
        }
        system::LTR => {
            let selector = selector_operand(s)?;
            if selector & 0xfffc == 0 {
                return Err(GP0);
            }
            let (va, tr) = system_segment(s, selector, 9)?;
            // Busy (type 1001B -> 1011B) by a compare-exchange on the access byte, as hardware's locked read-modify-write:
            // of two vCPUs loading one TSS descriptor, the second sees it busy and takes #GP(selector).
            if atomic_byte(s, va + 5, |b| (b & 15 == 9).then_some(b | 2))?.is_err() {
                return Err(gp(selector));
            }
            s.cpu.tr = Segment {
                attributes: tr.attributes | 2,
                ..tr
            };
        }
        system::MOV_TO_SREG => {
            let selector = selector_operand(s)?;
            load_data(s, i.cr as usize, selector)?;
            s.cpu.mov_ss = u64::from(i.cr as usize == SREG_SS);
        }
        // X1c: PUSH FS/GS: the selector zero-extended to the operand size (SDM Vol.2 PUSH; for a 32-bit push the SDM
        // also allows a 16-bit store, which recent Intel processors make: natemu zero-extends, as QEMU), at RSP - size
        // (#SS(0) when not canonical). POP FS/GS: the popped selector loaded as MOV Sreg does (#GP/#NP checks, FS/GS base
        // from the descriptor), then RSP + size; a fault leaves RSP unchanged. M2 task 3b: from 32-bit code also ES, CS,
        // SS and DS, with ESP wrapping at 4 GiB; POP SS sets the MOV SS shadow (SDM Vol.2 POP).
        system::PUSH_SREG => {
            let n = i.size as usize;
            let rsp = wrap(&c, c.gpr[4].wrapping_sub(u64::from(i.size)));
            let mut b = u64::from(c.seg[i.cr as usize].selector).to_le_bytes();
            linear(s, rsp, &mut b[..n], true, false, SS0)?;
            s.cpu.gpr[4] = rsp;
        }
        system::POP_SREG => {
            let n = i.size as usize;
            let mut b = [0u8; 8];
            linear(s, c.gpr[4], &mut b[..n], false, false, SS0)?;
            load_data(s, i.cr as usize, u16::from_le_bytes([b[0], b[1]]))?;
            s.cpu.gpr[4] = wrap(&c, c.gpr[4].wrapping_add(u64::from(i.size)));
            s.cpu.mov_ss = u64::from(i.cr as usize == SREG_SS);
        }
        // M2 task 3b: LDS, LES, LSS, LFS, LGS (32- and 16-bit code; SDM Vol.2 LDS/LES/LFS/LGS/LSS): the far pointer m16:16 or
        // m16:32, its selector loaded as MOV Sreg does, then the offset into `reg` (16 bits merged, 32 zero-extended).
        system::LOAD_FAR => {
            let w = i.size as usize;
            let mut pointer = [0u8; 6];
            aligned_operand(
                s,
                address(&c, &i, (w + 2) as u64, false)?,
                &mut pointer[..w + 2],
                w,
                if w == 2 { 2 } else { 4 },
                false,
                operand_range(&i),
            )?;
            load_data(s, i.cr as usize, u16::from_le_bytes([pointer[w], pointer[w + 1]]))?;
            let mut offset = [0u8; 8];
            offset[..w].copy_from_slice(&pointer[..w]);
            let (v, r) = (u64::from_le_bytes(offset), &mut s.cpu.gpr[i.reg as usize]);
            *r = if w == 2 { *r & !0xffff | v } else { v };
        }
        system::JMP_FAR..=system::CALL_FAR => return far_transfer(s, i).map(Some),
        system::RET_FAR..=system::IRET => return far_return(s, i).map(Some),
        _ => halt(s, stop::BAD_REPLY),
    }
    Ok(None)
}

/// The effective offset at the instruction's address size, before adding a segment base.
pub(super) fn offset(c: &Cpu, i: &System) -> u64 {
    let base = match i.base {
        system::RIP => c.rip.wrapping_add(u64::from(i.length)),
        system::NO_REG => 0,
        r => c.gpr[r as usize],
    };
    let index = if i.index == system::NO_REG {
        0
    } else {
        c.gpr[i.index as usize] << i.scale
    };
    let ea = base.wrapping_add(index).wrapping_add(i.disp as u64);
    match i.prefixes & (system::ADDRESS32 | system::ADDRESS16) {
        system::ADDRESS32 => ea as u32 as u64,
        system::ADDRESS16 => ea as u16 as u64,
        _ => ea,
    }
}
/// Check the architectural operand (including bytes a split lowering may access separately)
/// in its original segment before alignment checks, page walks or any architectural effects.
pub(super) fn address(c: &Cpu, i: &System, bytes: u64, write: bool) -> Result<u64, Exception> {
    operand(c, i.segment, offset(c, i), bytes, write)
}
pub(super) fn operand(c: &Cpu, sreg: u32, ea: u64, bytes: u64, write: bool) -> Result<u64, Exception> {
    let reg = sreg as usize;
    let seg = &c.seg[reg];
    if c.code_mode() == Some(CodeMode::Bits64) {
        return Ok(if matches!(reg, SREG_FS | SREG_GS) { ea.wrapping_add(seg.base) } else { ea });
    }
    usable(c, sreg)?;
    if matches!(reg, SREG_FS | SREG_GS) {
        if !seg.allows(ea, bytes, write, true) {
            return Err(GP0);
        }
    } else if ea > u64::from(u32::MAX) || bytes - 1 > u64::from(u32::MAX) - ea {
        return Err(if reg == SREG_SS { SS0 } else { GP0 });
    }
    Ok(ea.wrapping_add(seg.base) as u32 as u64)
}
/// M2 task 3b: #GP(0) for an access through a null segment in 32-bit code (SDM Vol.3 3.4.4; translated code's is the
/// compiler's cut, [`Cpu::null_segment`]).
pub(super) fn usable(c: &Cpu, sreg: u32) -> Result<(), Exception> {
    if c.null_segment(sreg as usize) {
        Err(GP0)
    } else {
        Ok(())
    }
}
/// M2 task 3b: a stack pointer or instruction pointer value at the code's width: cut to 32 bits in 32-bit code (ESP
/// and EIP wrap at 4 GiB; its stack is always a 32-bit one, `Cpu::compat_refused`), else unchanged.
pub(super) fn wrap(c: &Cpu, v: u64) -> u64 {
    if c.code_mode() == Some(CodeMode::Bits64) {
        v
    } else {
        v as u32 as u64
    }
}
/// A flat compatibility segment rejects an operand that crosses 4 GiB. A based FS/GS operand
/// reaching this layer already passed its offset/limit check, so its linear byte addresses wrap.
#[derive(Clone, Copy)]
pub(super) enum OperandRange {
    Flat(Exception),
    Based,
}

impl From<Exception> for OperandRange {
    fn from(fault: Exception) -> Self {
        Self::Flat(fault)
    }
}

impl OperandRange {
    pub(super) fn fault(self) -> Exception {
        match self { Self::Flat(fault) => fault, Self::Based => GP0 }
    }

    fn line(self, c: &Cpu, va: u64) -> u64 {
        if matches!(self, Self::Based) { wrap(c, va) } else { va }
    }

    pub(super) fn outside(self, c: &Cpu, va: u64, n: u64) -> bool {
        if matches!(self, Self::Based) && c.code_mode() == Some(CodeMode::Compat32) {
            return false;
        }
        outside(c, va, n)
    }
}

pub(super) fn outside(c: &Cpu, va: u64, n: u64) -> bool {
    let last = va.wrapping_add(n - 1);
    !paging::canonical(va) || !paging::canonical(last) || last < va
        || (last >> 32 != 0 && c.code_mode() != Some(CodeMode::Bits64))
}

pub(super) fn operand_range(i: &System) -> OperandRange {
    match i.segment as usize {
        SREG_FS | SREG_GS => OperandRange::Based,
        SREG_SS => OperandRange::Flat(SS0),
        _ => OperandRange::Flat(GP0),
    }
}

/// Copies between `buf` and linear `va` for the instruction at RIP: an explicit access at the CPL, or an implicit
/// supervisor one (`supervisor`: descriptor tables, TSS). Both pages of a crossing range are walked (A/D set as the CPU
/// does) before any byte moves, so a #PF (CR2 the first faulting byte's page start, or `va`) leaves memory unchanged;
/// a non-canonical range raises `noncanonical`. A write into a page holding translations invalidates them first
/// ([`FetchWrites`]: no block of this vCPU runs during a transaction). Guest RAM of any memory type is accessed (M2: UC RAM
/// in program order, [`pages`]); device pages stop `unsupported_mmio` (status `system`, M3).
pub(super) fn linear(
    s: &mut State,
    va: u64,
    buf: &mut [u8],
    write: bool,
    supervisor: bool,
    range: impl Into<OperandRange>,
) -> Result<(), Exception> {
    let range = range.into();
    let n = buf.len() as u64;
    // X2: an explicit access at CPL 3 under alignment checking (SDM Vol.3 7.15, Table 7-7): #AC(0) after the canonical
    // check, before any walk, when misaligned for its data type: a word 2, a doubleword or a 48-bit far pointer or
    // descriptor-table register image (6 or 10 bytes) 4, a quadword (and each quadword of a frame or pop: a multiple of 8
    // bytes) 8; an 80-bit far pointer (m16:64) is taken as 4, as the table's far pointers. Implicit supervisor accesses
    // are never checked.
    if !supervisor && super::alignment_check(&s.cpu) != 0 {
        let align = match n {
            6 | 10 => 4,
            n if n % 8 == 0 => 8,
            4 => 4,
            2 => 2,
            _ => 1,
        };
        if va & (align - 1) != 0 && !range.outside(&s.cpu, va, n) {
            return Err([17, 0, 0]);
        }
    }
    copy(s, va, buf, write, supervisor, range)
}
/// [`linear`] without its alignment check: for the explicit accesses that never raise #AC (the gathers' elements, SDM
/// Vol.2 2.5 exception class 12).
pub(super) fn copy(
    s: &mut State,
    va: u64,
    buf: &mut [u8],
    write: bool,
    supervisor: bool,
    range: impl Into<OperandRange>,
) -> Result<(), Exception> {
    let range = range.into();
    let n = buf.len() as u64;
    let gpa = pages(s, va, n, write, supervisor, range)?;
    // S4: an explicit access meets data breakpoints; an implicit supervisor one (descriptor tables, TSS, IDT) never does.
    if !supervisor {
        super::watch::access(s, va, n, write);
    }
    let first = (4096 - (va & 4095)).min(n) as usize;
    // Only guest RAM is accessed, and nothing is written unless every byte is RAM (M3: a device page stops first; M2 Part 2:
    // the firmware image is read, a store to it dropped, out of line).
    if !super::mmio::ram(gpa[0]) || (first < n as usize && !super::mmio::ram(gpa[1])) {
        copy_rom(s, gpa[0], gpa[1], buf, write);
        return Ok(());
    }
    copy_ram(s, gpa, first, buf, write);
    Ok(())
}

/// Both pages have been walked at the required permissions and are held against split locks.
pub(super) fn copy_ram(s: &mut State, gpa: [u64; 2], first: usize, buf: &mut [u8], write: bool) {
    let (low, high) = buf.split_at_mut(first);
    for (gpa, chunk) in [(gpa[0], low), (gpa[1], high)] {
        if chunk.is_empty() {
            continue;
        }
        let done = if write {
            if cache::translated(gpa) {
                FetchWrites(s).code_write(gpa);
            }
            let done = guest_ram().copy_in(gpa, chunk);
            cache::rechecked(gpa);
            done
        } else {
            guest_ram().copy_out(gpa, chunk)
        };
        if !done {
            halt(s, stop::UNSUPPORTED_SYSTEM)
        }
    }
}
/// [`copy`] when a page of the access is not RAM: with every such page in the firmware image (M2 Part 2,
/// docs/restructure/firmware-mode.md), the access is made, the image read and a store to it dropped (x86's ROM; a descriptor
/// table, an SGDT, a delivery's frame there); else a device page's stop, nothing written.
#[cold]
#[inline(never)]
fn copy_rom(s: &mut State, low: u64, high: u64, buf: &mut [u8], write: bool) {
    let (gpa, first) = ([low, high], (4096 - (low & 4095) as usize).min(buf.len()));
    let pages = 1 + usize::from(first < buf.len());
    if gpa[..pages].iter().any(|&g| !super::mmio::ram(g) && !super::mmio::rom(g)) {
        super::mmio::system_access(s)
    }
    copy_mixed(s, gpa, first, buf, write)
}

/// Copy resolved RAM or ROM spans. For a generated read, the MMIO page classifier has also
/// approved any unassigned span for the board's all-ones response. A write may only name RAM/ROM.
#[cold]
pub(super) fn copy_mixed(s: &mut State, gpa: [u64; 2], first: usize, buf: &mut [u8], write: bool) {
    let (low, high) = buf.split_at_mut(first);
    for (gpa, chunk) in [(gpa[0], low), (gpa[1], high)] {
        if chunk.is_empty() || write && super::mmio::rom(gpa) {
            continue;
        }
        let done = if super::mmio::rom(gpa) {
            super::mmio::roms().copy_out(gpa, chunk)
        } else if !super::mmio::ram(gpa) && !write {
            chunk.fill(0xff);
            true
        } else if write {
            if cache::translated(gpa) {
                FetchWrites(s).code_write(gpa);
            }
            let done = guest_ram().copy_in(gpa, chunk);
            cache::rechecked(gpa);
            done
        } else {
            guest_ram().copy_out(gpa, chunk)
        };
        if !done {
            halt(s, stop::UNSUPPORTED_SYSTEM)
        }
    }
}
/// The guest-physical addresses of `[va, va + n)` (its page, and the next one when it crosses), walked for [`linear`]
/// (which accesses only guest RAM) and for CLFLUSH (M3: a device page is uncached, nothing to flush; the walk's faults
/// are all it has). M2 (owner decision, November 2): when either page is UC RAM, or a UC access was made since the last
/// boundary, every earlier access is made globally visible before the caller's access (a barrier here), and the next
/// boundary makes a UC one so before any later one (`uncached_end`).
pub(super) fn pages(
    s: &mut State,
    va: u64,
    n: u64,
    write: bool,
    supervisor: bool,
    range: impl Into<OperandRange>,
) -> Result<[u64; 2], Exception> {
    let range = range.into();
    let va = range.line(&s.cpu, va);
    let last = va.wrapping_add(n - 1);
    // Implicit accesses (descriptor tables, the TSS) are 64-bit ones whatever the code's mode.
    if !paging::canonical(va) || !paging::canonical(last) || last < va || (!supervisor && range.outside(&s.cpu, va, n)) {
        return Err(range.fault());
    }
    let mut control = controls(s);
    if supervisor {
        control.cpl = 0;
    }
    let access = if write { Access::Write } else { Access::Read };
    let crosses = (last ^ va) >> 12 != 0;
    let mut gpa = [0u64; 2];
    for (k, at) in [va, range.line(&s.cpu, last & !4095)]
        .into_iter()
        .enumerate()
        .take(1 + usize::from(crosses))
    {
        gpa[k] = loop {
            match paging::walk(
                &mut GuestTables::observed(guest_ram(), CodeWrites(FetchWrites(s))),
                control,
                at,
                access,
                cache::control().speculation_barriers != 0,
            ) {
                Ok(t) => {
                    let c = cache::control();
                    // A UC access made since the last boundary (an instruction run alone, or the resident's own) is
                    // globally visible before this one, whatever this one's type.
                    let owed = c.uncached_count != 0 || c.uncached_fence != 0;
                    let uncached = super::mmio::ram(t.gpa)
                        && cpu::memory_type(msrs(), t.gpa, t.pat) == cpu::memory_type::UC;
                    if uncached {
                        (c.uncached_fence, c.uncached_resident) = (1, c.uncached_resident + 1);
                    }
                    if owed || uncached {
                        // SAFETY: a barrier; no other effect.
                        unsafe { asm!("dmb ish") };
                    }
                    break t.gpa;
                }
                Err(Fault::Page { address, error }) => return Err([14, error, address]),
                Err(Fault::Contended) => {}
                Err(_) => halt(s, stop::UNSUPPORTED_MODE),
            }
        };
    }
    super::split_wait(s, &gpa[..1 + usize::from(crosses)]);
    Ok(gpa)
}
/// Updates the guest byte at linear `va` atomically, as an implicit supervisor write (walked for write first: a
/// read-only page is #PF(3) at `va`): `update` maps the current value to the new one, or `None` to leave it (then
/// `Ok(Err(current))`). The code-write protocol applies as for [`linear`]. Used for the accessed and busy bits, which
/// hardware sets by a locked read-modify-write.
fn atomic_byte(
    s: &mut State,
    va: u64,
    update: impl FnMut(u8) -> Option<u8>,
) -> Result<Result<u8, u8>, Exception> {
    let gpa = pages(s, va, 1, true, true, GP0)?[0];
    let Some(p) = guest_ram().range(gpa, 1) else {
        return Ok(rom_byte(s, gpa, update));
    };
    if cache::translated(gpa) {
        FetchWrites(s).code_write(gpa);
    }
    // SAFETY: `p` is a guest RAM byte in the resident's alias (Normal memory); peers and guest code access it too, so
    // only atomic accesses are made here.
    let byte = unsafe { &*(p as *const core::sync::atomic::AtomicU8) };
    let done = byte.fetch_update(
        core::sync::atomic::Ordering::SeqCst,
        core::sync::atomic::Ordering::SeqCst,
        update,
    );
    cache::rechecked(gpa);
    Ok(done)
}

/// [`atomic_byte`] on a byte outside RAM: in the firmware image (M2 Part 2), `update` meets the byte and nothing is written
/// (x86 drops the locked write to ROM: an accessed or busy bit there stays clear); else a device page's stop.
#[cold]
#[inline(never)]
fn rom_byte(s: &State, gpa: u64, mut update: impl FnMut(u8) -> Option<u8>) -> Result<u8, u8> {
    let mut byte = [0u8];
    if !super::mmio::rom(gpa) || !super::mmio::roms().copy_out(gpa, &mut byte) {
        super::mmio::system_access(s)
    }
    match update(byte[0]) {
        Some(_) => Ok(byte[0]),
        None => Err(byte[0]),
    }
}

/// The descriptor `selector` names (8 bytes, or 16 for a `wide` system descriptor) in the GDT, or for TI = 1 the LDT,
/// and its linear address: #GP(selector) when it does not lie within the table's limit, when TI = 1 with a null LDTR,
/// or when its address is not canonical.
pub(super) fn descriptor(
    s: &mut State,
    selector: u16,
    wide: bool,
) -> Result<(u64, u128), Exception> {
    let (base, limit) = if selector & 4 != 0 {
        let l = s.cpu.ldtr;
        if l.attributes & ATTR_P == 0 {
            return Err(gp(selector));
        }
        (l.base, u64::from(l.limit))
    } else {
        (s.cpu.gdtr.base, s.cpu.gdtr.limit)
    };
    let index = u64::from(selector & !7);
    if index + if wide { 15 } else { 7 } > limit {
        return Err(gp(selector));
    }
    let va = base.wrapping_add(index);
    let mut raw = [0u8; 16];
    linear(
        s,
        va,
        &mut raw[..if wide { 16 } else { 8 }],
        false,
        true,
        gp(selector),
    )?;
    Ok((va, u128::from_le_bytes(raw)))
}
/// Sets the accessed bit of the descriptor at `va` when `raw` has it clear (SDM Vol.3 3.4.5).
pub(super) fn accessed(s: &mut State, va: u64, raw: u64) -> Result<(), Exception> {
    if raw >> 40 & 1 == 0 {
        let _ = atomic_byte(s, va + 5, |b| Some(b | 1))?; // an atomic OR: a peer's change to the byte is kept
    }
    Ok(())
}
/// Descriptor fields: (S, type, DPL, P).
pub(super) fn fields(raw: u64) -> (bool, u64, u64, bool) {
    (
        raw >> 44 & 1 != 0,
        raw >> 40 & 15,
        raw >> 45 & 3,
        raw >> 47 & 1 != 0,
    )
}

/// The 16-byte LDT (`kind` 2) or available 64-bit TSS (`kind` 9) descriptor of non-null `selector` for LLDT/LTR, and its
/// address: #GP(selector) for TI = 1, outside the GDT, another type, a nonzero type field in the upper half (SDM Vol.3
/// Figure 8-4) or a non-canonical base; #NP(selector) when not present.
fn system_segment(s: &mut State, selector: u16, kind: u64) -> Result<(u64, Segment), Exception> {
    if selector & 4 != 0 {
        return Err(gp(selector));
    }
    let (va, raw) = descriptor(s, selector, true)?;
    let (system, kind_, _, present) = fields(raw as u64);
    if system || kind_ != kind {
        return Err(gp(selector));
    }
    if !present {
        return Err(np(selector));
    }
    let base = Segment::loaded(selector, raw as u64).base | u64::from((raw >> 64) as u32) << 32;
    if (raw >> 104) & 0x1f != 0 || !paging::canonical(base) {
        return Err(gp(selector));
    }
    Ok((
        va,
        Segment {
            base,
            ..Segment::loaded(selector, raw as u64)
        },
    ))
}

/// MOV/POP to ES, SS, DS, FS or GS in 64-bit mode (SDM Vol.2 MOV, "64-Bit Mode Exceptions"). SS: a null selector is
/// allowed at CPL < 3 with RPL = CPL; otherwise RPL = DPL = CPL and a writable data segment (#GP(selector)), present
/// (#SS(selector)). DS/ES/FS/GS: null is always allowed; otherwise data or readable code, with max(CPL, RPL) <= DPL
/// unless conforming code (#GP(selector)), present (#NP(selector)). FS/GS take the descriptor's 32-bit base (a null
/// load clears it, as Intel processors do).
pub(super) fn load_data(s: &mut State, sreg: usize, selector: u16) -> Result<(), Exception> {
    let (cpl, rpl) = (s.cpu.cpl, u64::from(selector & 3));
    let loaded = if selector & 0xfffc == 0 {
        if sreg == SREG_SS && (cpl == 3 || rpl != cpl) {
            return Err(GP0);
        }
        Segment {
            selector,
            ..Segment::NULL
        }
    } else {
        let (va, raw) = descriptor(s, selector, false)?;
        let raw = raw as u64;
        let (code_or_data, kind, dpl, present) = fields(raw);
        let code = kind & 8 != 0;
        if sreg == SREG_SS {
            if rpl != cpl || !code_or_data || code || kind & 2 == 0 || dpl != cpl {
                return Err(gp(selector));
            }
            if !present {
                return Err(ss(selector));
            }
        } else {
            let conforming = code && kind & 4 != 0;
            if !code_or_data || (code && kind & 2 == 0) || (!conforming && (rpl > dpl || cpl > dpl))
            {
                return Err(gp(selector));
            }
            if !present {
                return Err(np(selector));
            }
        }
        accessed(s, va, raw)?;
        Segment::loaded(selector, raw | 1 << 40)
    };
    s.cpu.seg[sreg] = loaded;
    Ok(())
}

/// The code segment `selector` of a JMP/CALL far (SDM Vol.2 JMP, CALL, protected mode, "conforming / nonconforming code
/// segment"): #GP(0) when null; #GP(selector) outside the table, for a data segment or a system descriptor other than a
/// call gate (a TSS or task gate: no task switch in IA-32e mode), conforming code with DPL > CPL, nonconforming code
/// with RPL > CPL or DPL != CPL, and L = D = 1; #NP(selector) when not present. A 64-bit call gate stops
/// `unsupported_system`. Returns its address and descriptor.
fn far_target(s: &mut State, selector: u16) -> Result<(u64, u64), Exception> {
    if selector & 0xfffc == 0 {
        return Err(GP0);
    }
    let (va, raw) = descriptor(s, selector, false)?;
    let raw = raw as u64;
    let (code_or_data, kind, dpl, present) = fields(raw);
    let cpl = s.cpu.cpl;
    if !code_or_data && kind == 0xc {
        if cpl == 3 {
            return Err(gp(selector)); // the deviation above: call gates are not built
        }
        halt(s, stop::UNSUPPORTED_SYSTEM)
    }
    let conforming = kind & 4 != 0;
    if !code_or_data
        || kind & 8 == 0
        || if conforming {
            dpl > cpl
        } else {
            u64::from(selector & 3) > cpl || dpl != cpl
        }
        || raw >> 53 & 3 == 3
    {
        return Err(gp(selector));
    }
    if !present {
        return Err(np(selector));
    }
    Ok((va, raw))
}
/// Commit the checked descriptor. The boot-time interpreter uses the same hidden CS representation.
pub(super) fn load_cs(s: &mut State, selector: u16, raw: u64) {
    s.cpu.seg[SREG_CS] = Segment::loaded(selector, raw | 1 << 40);
}
/// User compatibility code is checked by `Cpu::compat_refused` after the transfer commits.
/// Supervisor compatibility code stays on the boot-time interpreter's existing route.
#[inline(never)]
fn target_mode(s: &State, selector: u16, raw: u64, cpl: u64) -> Result<(), Exception> {
    if raw >> 53 & 3 == 1 || (cpl == 3 && raw >> 53 & 1 == 0) {
        return Ok(());
    }
    if cpl == 3 {
        return Err(gp(selector));
    }
    super::interpret::kernel_compatibility(s, selector, raw, cpl)
}

/// JMP/CALL far through an m16:`size` pointer to a code segment at the same CPL (SDM Vol.2 JMP, CALL): the target checks
/// ([`far_target`]), then for CALL #SS(0) when the return address (CS and RIP, `size` bytes each) would not be canonical,
/// #GP(0) for a non-canonical offset, the accessed bit, CALL's pushes; CS = selector with RPL = CPL.
fn far_transfer(s: &mut State, i: System) -> Result<u64, Exception> {
    let c = s.cpu;
    let w = i.size as usize;
    let (mut rip, selector) = if i.memory == 0 {
        // M2 task 3b: the immediate pointer of 32- and 16-bit code (`9a`, `ea`).
        (i.disp as u64, i.cr as u16)
    } else {
        let mut pointer = [0u8; 10];
        aligned_operand(
            s,
            address(&c, &i, (w + 2) as u64, false)?,
            &mut pointer[..w + 2],
            w,
            if w == 2 { 2 } else { 4 },
            false,
            operand_range(&i),
        )?;
        let (mut offset, mut word) = ([0u8; 8], [0u8; 2]);
        offset[..w].copy_from_slice(&pointer[..w]);
        word.copy_from_slice(&pointer[w..w + 2]);
        (u64::from_le_bytes(offset), u16::from_le_bytes(word))
    };
    let (va, raw) = far_target(s, selector)?;
    target_mode(s, selector, raw, c.cpl)?;
    let call = i.kind == system::CALL_FAR;
    let rsp = wrap(&c, c.gpr[4].wrapping_sub(if call { 2 * w as u64 } else { 0 }));
    if call && outside(&c, rsp, 2 * w as u64) {
        return Err(SS0);
    }
    if c.cpl == 3 && raw >> 53 & 1 == 0 {
        // SDM Vol. 2A: same-level CALL masks the offset before the limit check. JMP checks the full pointer.
        if call {
            rip = u64::from(rip as u32);
        }
        if rip > u64::from(Segment::loaded(selector, raw).limit) {
            return Err(GP0);
        }
    }
    if !paging::canonical(rip) {
        return Err(GP0);
    }
    accessed(s, va, raw)?;
    if call {
        let mut frame = [0u8; 16];
        frame[..w].copy_from_slice(&wrap(&c, c.rip.wrapping_add(u64::from(i.length))).to_le_bytes()[..w]);
        frame[w..2 * w].copy_from_slice(&u64::from(c.seg[SREG_CS].selector).to_le_bytes()[..w]);
        aligned_operand(s, rsp, &mut frame[..2 * w], w, w as u64, true, SS0)?;
        s.cpu.gpr[4] = rsp;
    }
    load_cs(s, selector & !3 | c.cpl as u16, raw);
    Ok(rip)
}

/// RET far (`size`-byte pops, imm16 `disp` released) and IRET (SDM Vol.2 RET, IRET, IA-32e mode): IRET #GP(0) with
/// RFLAGS.NT; the pops (#SS(0) when not canonical); the return CS: #GP(0) null, #GP(selector) outside the table, not
/// code, RPL < CPL, conforming with DPL > RPL, nonconforming with DPL != RPL, #NP(selector) not present; a
/// user compatibility target may follow either return. For IRET and for an outer return, SS:RSP from the stack: a null SS
/// only for a new CPL < 3 with RPL = the new CPL (SDM IRET/RET 64-bit mode #GP(0)), else #GP(selector) unless RPL = DPL =
/// the new CPL and a writable data segment, #SS(selector) not present; #GP(0) for a non-canonical RIP, checked before SS
/// for IRET (delivery_model's order) and after it for RET (its pseudocode; segment_model B4). A return to CPL 1 or 2 is
/// refused (`unsupported_mode`) before any effect. IRET's RFLAGS follow the CPL and IOPL masks. A return to an outer
/// level nulls DS/ES/FS/GS that are null or hold data or nonconforming code with DPL below the new CPL (segment_model B5)
/// and changes the CPL ([`set_cpl`]). IRET releases NMI blocking before this runs, even when it faults (`system`); a TF
/// it loads takes effect after the next instruction (S4); RF is loaded from the frame.
fn far_return(s: &mut State, i: System) -> Result<u64, Exception> {
    let c = s.cpu;
    let (w, iret, release) = (u64::from(i.size), i.kind == system::IRET, i.disp as u64);
    if iret && c.rflags & RFLAGS_NT != 0 {
        return Err(GP0);
    }
    let pops = |s: &mut State, at: u64, n: usize| -> Result<[u64; 5], Exception> {
        let mut b = [0u8; 40];
        aligned_operand(s, at, &mut b[..n * w as usize], w as usize, w, false, SS0)?;
        let mut words = [0u64; 5];
        for (k, word) in words.iter_mut().enumerate().take(n) {
            let mut q = [0u8; 8];
            q[..w as usize].copy_from_slice(&b[k * w as usize..(k + 1) * w as usize]);
            *word = u64::from_le_bytes(q);
        }
        Ok(words)
    };
    // M2 task 3b: IRET from 32-bit code pops EIP, CS and EFLAGS, and SS:ESP only for a return to an outer level (SDM
    // Vol.2 IRET, IA-32e mode: RETURN-TO-SAME-PRIVILEGE-LEVEL outside 64-bit mode); from 64-bit code all five.
    let bits64 = c.code_mode() == Some(CodeMode::Bits64);
    let frame = if !iret { 2 } else if bits64 { 5 } else { 3 };
    let words = pops(s, wrap(&c, c.gpr[4]), frame)?;
    let (rip, selector) = (words[0], words[1] as u16);
    if selector & 0xfffc == 0 {
        return Err(GP0);
    }
    let (va, raw) = descriptor(s, selector, false)?;
    let raw = raw as u64;
    let (code_or_data, kind, dpl, present) = fields(raw);
    let (cpl, rpl) = (c.cpl, u64::from(selector & 3));
    if !code_or_data
        || kind & 8 == 0
        || (!iret && rpl == 3 && raw >> 53 & 3 == 3)
        || rpl < cpl
        || if kind & 4 != 0 { dpl > rpl } else { dpl != rpl }
    {
        return Err(gp(selector));
    }
    if !present {
        return Err(np(selector));
    }
    target_mode(s, selector, raw, rpl)?;
    let compat = rpl == 3 && raw >> 53 & 1 == 0;
    let bad_rip = if compat && !iret {
        rip > u64::from(Segment::loaded(selector, raw).limit)
    } else if compat {
        rip > u64::from(u32::MAX)
    } else {
        !paging::canonical(rip)
    };
    if iret && bad_rip {
        return Err(GP0);
    }
    let outer = rpl > cpl;
    let mut stack = None;
    if frame == 5 || outer {
        let [rsp, ss_selector, ..] = if frame == 5 {
            [words[3], words[4], 0, 0, 0]
        } else {
            pops(s, wrap(&c, c.gpr[4].wrapping_add(frame as u64 * w + release)), 2)?
        };
        let ss_selector = ss_selector as u16;
        let ss_segment = if ss_selector & 0xfffc == 0 {
            if rpl == 3 || u64::from(ss_selector & 3) != rpl {
                return Err(GP0);
            }
            None
        } else {
            let (ss_va, ss_raw) = descriptor(s, ss_selector, false)?;
            let ss_raw = ss_raw as u64;
            let (code_or_data, kind, dpl, present) = fields(ss_raw);
            if u64::from(ss_selector & 3) != rpl
                || !code_or_data
                || kind & 8 != 0
                || kind & 2 == 0
                || dpl != rpl
            {
                return Err(gp(ss_selector));
            }
            if !present {
                return Err(ss(ss_selector));
            }
            Some((ss_va, ss_raw))
        };
        stack = Some((rsp, ss_selector, ss_segment));
    }
    if !iret && bad_rip {
        return Err(GP0);
    }
    // CPL 1 and 2 are not built ([`Cpu::paging`]): refused with nothing changed.
    if matches!(rpl, 1 | 2) {
        halt(s, stop::UNSUPPORTED_MODE)
    }
    let mut rflags = c.rflags;
    if iret {
        let mask = (IRET_ALWAYS
            | if cpl <= (c.rflags & RFLAGS_IOPL) >> 12 {
                RFLAGS_IF
            } else {
                0
            }
            | if cpl == 0 { IRET_CPL0 } else { 0 })
            & if w == 2 { 0xffff } else { u64::MAX };
        rflags = (c.rflags & !mask | words[2] & mask) & !RFLAGS_VM | 2;
        // RF comes from the frame (X1a) and lasts until the next instruction completes (`execution::completed`); it
        // suppresses an instruction breakpoint there (S4).
    }
    accessed(s, va, raw)?;
    if let Some((_, _, Some((ss_va, ss_raw)))) = stack {
        accessed(s, ss_va, ss_raw)?;
    }
    load_cs(s, selector, raw);
    s.cpu.rflags = rflags;
    match stack {
        Some((rsp, ss_selector, segment)) => {
            s.cpu.seg[SREG_SS] = match segment {
                Some((_, ss_raw)) => Segment::loaded(ss_selector, ss_raw | 1 << 40),
                None => Segment {
                    selector: ss_selector,
                    ..Segment::NULL
                },
            };
            let rsp = rsp.wrapping_add(if iret { 0 } else { release });
            s.cpu.gpr[4] = if compat { rsp as u32 as u64 } else { rsp };
        }
        None => s.cpu.gpr[4] = wrap(&c, c.gpr[4].wrapping_add(frame as u64 * w + release)),
    }
    if outer {
        for sreg in [SREG_ES, SREG_DS, SREG_FS, SREG_GS] {
            let a = s.cpu.seg[sreg].attributes;
            let (kind, dpl) = (u64::from(a & 15), u64::from(a >> 5 & 3));
            if a & ATTR_P == 0 || ((kind & 8 == 0 || kind & 4 == 0) && dpl < rpl) {
                // The FS/GS bases stay (segment_model B5).
                (s.cpu.seg[sreg].selector, s.cpu.seg[sreg].attributes) = (0, 0);
            }
        }
        set_cpl(s, rpl);
    }
    Ok(rip)
}

/// Far pointers and stack frames have alignment independent of the aggregate byte count (SDM Vol.3A Table 7-7).
pub(super) fn aligned_operand(
    s: &mut State,
    at: u64,
    bytes: &mut [u8],
    slot: usize,
    alignment: u64,
    write: bool,
    range: impl Into<OperandRange>,
) -> Result<(), Exception> {
    ordered_operand(s, at, bytes, slot, alignment, write, range.into(), |s, _, _, _, _| mmio::system_access(s))
}

/// One compatibility string element. Preserve the translated path's ROM and unassigned reads
/// and its typed refusal of device strings after both pages have been walked.
pub(super) fn string_element(
    s: &mut State, at: u64, bytes: &mut [u8], write: bool, range: OperandRange,
) -> Result<(), Exception> {
    ordered_operand(s, at, bytes, bytes.len(), bytes.len() as u64, write, range, |s, gpa, first, bytes, write| {
        mmio::string_operand(s, at, gpa, first, bytes, write)
    })
}

fn ordered_operand(
    s: &mut State, at: u64, bytes: &mut [u8], slot: usize, alignment: u64, write: bool, range: OperandRange,
    nonram: impl FnOnce(&mut State, [u64; 2], usize, &mut [u8], bool),
) -> Result<(), Exception> {
    if range.outside(&s.cpu, at, bytes.len() as u64) {
        return Err(range.fault());
    }
    if s.cpu.alignment_check() && at & (alignment - 1) != 0 {
        return Err([17, 0, 0]);
    }
    // Preflight the entire pointer/frame, then transfer its operand-sized slots (a pointer's last selector is2bytes).
    // Naturally aligned slots cannot tear; the frame as a whole need not be atomic. Pointer alignment and offset
    // width are distinct: m16:64 aligns4 but its offset is an8byte load.
    let gpa = pages(s, at, bytes.len() as u64, write, false, range)?;
    let head = bytes.len().min((4096 - (at & 4095)) as usize);
    if !mmio::ram(gpa[0]) || (head < bytes.len() && !mmio::ram(gpa[1])) {
        nonram(s, gpa, head, bytes, write);
        watch::access(s, at, bytes.len() as u64, write);
        return Ok(());
    }
    if write {
        for (pa, count) in [(gpa[0], head), (gpa[1], bytes.len() - head)] {
            if count != 0 && cache::translated(pa) {
                FetchWrites(s).code_write(pa);
            }
        }
        unsafe { asm!("dmb ish") };
    }
    for (k, word) in bytes.chunks_mut(slot).enumerate() {
        let offset = slot * k;
        let pa = if offset < head {
            gpa[0] + offset as u64
        } else {
            gpa[1] + (offset - head) as u64
        };
        if offset < head && offset + word.len() > head {
            // Only an unaligned slot can straddle a page; its pieces may tear, but permissions are settled.
            let (lo, hi) = word.split_at_mut(head - offset);
            for (p, part) in [(pa, lo), (gpa[1], hi)] {
                let ok = if write {
                    guest_ram().copy_in(p, part)
                } else {
                    guest_ram().copy_out(p, part)
                };
                if !ok {
                    halt(s, stop::SHADOW);
                }
            }
        } else {
            let width = word.len();
            let Some(p) = guest_ram().range(pa, width as u64) else {
                halt(s, stop::SHADOW)
            };
            let mut value = [0u8; 8];
            // SAFETY: the slot lies in fully preflighted RAM pages held against split-lock exclusion.
            unsafe {
                if write {
                    value[..width].copy_from_slice(word);
                    rotate::store(p, width as u64, u64::from_le_bytes(value));
                } else {
                    word.copy_from_slice(&rotate::load(p, width as u64).to_le_bytes()[..width]);
                }
            }
        }
    }
    unsafe {
        if write {
            asm!("dsb ish")
        } else {
            asm!("dmb ish")
        }
    };
    if write {
        cache::rechecked(gpa[0]);
        if head < bytes.len() {
            cache::rechecked(gpa[1]);
        }
    }
    watch::access(s, at, bytes.len() as u64, write);
    Ok(())
}

/// LAR, LSL, VERR, VERW (S3; SDM Vol.2 LAR, LSL, VERR/VERW, IA-32e mode): the selector from a register or an m16 operand
/// (an explicit access at the CPL), then the descriptor it names checked without faulting on it: ZF = 0, the destination
/// unchanged, for a null selector, one outside the GDT/LDT limit (or TI = 1 with a null LDTR), a type the instruction does
/// not accept, or (but for conforming code) DPL < CPL or DPL < RPL; else ZF = 1 and LAR/LSL write the destination. Only
/// ZF changes among the flags. A #PF reading the descriptor (an implicit supervisor access) is raised. Accepted types
/// (64-bit mode, SDM Vol.3 "System Descriptor Types"): every code and data segment; LAR also LDT (2), 64-bit TSS (9, B) and
/// 64-bit call gate (C); LSL LDT and 64-bit TSS; VERR readable segments (data, code with R), VERW writable data. The
/// descriptor is read as 8 bytes whatever its type (its limit, type and access rights are there). LAR gives the
/// descriptor's dword 1 masked with 00F0FF00H (bits 19:16, the limit's, read as 0); LSL the limit scaled by G. A 16-bit
/// destination keeps bits 63:16; 32- and 64-bit ones are written zero-extended.
pub(super) fn inspect(s: &mut State, i: System) -> Result<(), Exception> {
    let c = s.cpu;
    let selector = if i.memory == 0 {
        c.gpr[i.reg as usize] as u16
    } else {
        let mut b = [0u8; 2];
        linear(s, address(&c, &i, 2, false)?, &mut b, false, false, operand_range(&i))?;
        u16::from_le_bytes(b)
    };
    let found = if selector & 0xfffc == 0 {
        None
    } else {
        match descriptor(s, selector, false) {
            Ok((_, raw)) => Some(raw as u64),
            Err([13, ..]) => None, // outside the table (or not canonical): no fault, ZF = 0
            Err(e) => return Err(e),
        }
    };
    let result = found.and_then(|raw| {
        let (code_or_data, kind, dpl, _) = fields(raw);
        let (code, conforming) = (
            code_or_data && kind & 8 != 0,
            code_or_data && kind & 0xc == 0xc,
        );
        let accepted = match i.kind {
            system::LAR => code_or_data || matches!(kind, 2 | 9 | 0xb | 0xc),
            system::LSL => code_or_data || matches!(kind, 2 | 9 | 0xb),
            system::VERR => code_or_data && (!code || kind & 2 != 0),
            _ => code_or_data && !code && kind & 2 != 0,
        };
        let privileged = !conforming && (c.cpl > dpl || u64::from(selector & 3) > dpl);
        (accepted && !privileged).then(|| match i.kind {
            system::LAR => raw >> 32 & 0x00f0_ff00,
            _ => u64::from(Segment::loaded(selector, raw).limit),
        })
    });
    const ZF: u64 = 1 << 6;
    s.cpu.rflags = c.rflags & !ZF | if result.is_some() { ZF } else { 0 };
    if let (Some(v), system::LAR | system::LSL) = (result, i.kind) {
        let r = &mut s.cpu.gpr[i.cr as usize];
        *r = if i.size == 2 {
            *r & !0xffff | v & 0xffff
        } else {
            v & if i.size == 4 { 0xffff_ffff } else { u64::MAX }
        };
    }
    Ok(())
}
