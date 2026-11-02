//! Descriptor tables, segment registers and far transfers in 64-bit mode (plan S2b; SDM Vol.2 LGDT/LIDT, SGDT, SIDT,
//! LLDT, SLDT, LTR, STR, MOV (segment registers), JMP, CALL, RET, IRET; Vol.3 3.4 "Segment Selectors/Descriptors", 5
//! "Protection", 8.2.3 and 8.7 (64-bit TSS)), executed as system transactions: every check raises its exception before
//! any architectural change. Descriptor-table and TSS-descriptor reads and writes are implicit supervisor accesses
//! through the guest walker (never a shadow: the walk is made at CPL 0 whatever the CPL, so a #PF has U/S = 0 and
//! CR0.WP decides writes to read-only pages); operand and stack accesses are explicit, at the CPL. The SDM's accessed-bit
//! write on a segment load and LTR's busy-bit write are real guest writes: a clear A bit in a read-only GDT page faults,
//! as on hardware. Flat 64-bit only (plan D1, owner decision 3): bases and limits of CS/SS/DS/ES are not used, FS/GS
//! loads set the bank's bases; a transfer to a compatibility-mode code segment (CS.L = 0, or L = D = 1) stops
//! `unsupported_mode` with that CS in the record and RIP at the instruction, nothing else changed; a call gate stops
//! `unsupported_system`. `tools/models/segment_model.py` (and `delivery_model.py`'s IRETQ) is the oracle; the check order
//! follows it where the SDM lists several conditions without a priority.
use super::*;

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

/// Executes `i` (LGDT ..= IRET, PUSH_SREG, POP_SREG). `Ok(None)`: committed, RIP moves past it; `Ok(Some(rip))`: a far transfer committed to
/// `rip`. On `Err` the vCPU's state is unchanged (guest memory may hold an accessed bit a failed load set, as the SDM
/// allows).
pub(super) fn execute(s: &mut State, i: System) -> Result<Option<u64>, Exception> {
    let c = s.cpu;
    let selector_operand = |s: &mut State| -> Result<u16, Exception> {
        if i.memory == 0 {
            return Ok(c.gpr[i.reg as usize] as u16);
        }
        let mut b = [0u8; 2];
        linear(s, address(&c, &i), &mut b, false, false, noncanonical(&i))?;
        Ok(u16::from_le_bytes(b))
    };
    match i.kind {
        system::LGDT..=system::LIDT => {
            let mut b = [0u8; 10];
            linear(s, address(&c, &i), &mut b, false, false, noncanonical(&i))?;
            let table = Table { base: u64::from_le_bytes(b[2..].try_into().unwrap()), limit: u64::from(u16::from_le_bytes([b[0], b[1]])) };
            // The base must be canonical (AMD APM Vol.3 LGDT/LIDT; KVM checks it; the Intel SDM lists no such #GP).
            if !paging::canonical(table.base) {
                return Err(GP0);
            }
            *if i.kind == system::LGDT { &mut s.cpu.gdtr } else { &mut s.cpu.idtr } = table;
        }
        system::SGDT..=system::SIDT => {
            let t = if i.kind == system::SGDT { c.gdtr } else { c.idtr };
            let mut b = [0u8; 10];
            b[..2].copy_from_slice(&(t.limit as u16).to_le_bytes());
            b[2..].copy_from_slice(&t.base.to_le_bytes());
            linear(s, address(&c, &i), &mut b, true, false, noncanonical(&i))?;
        }
        system::SLDT..=system::MOV_FROM_SREG => {
            let v = match i.kind {
                system::SLDT => c.ldtr.selector,
                system::STR => c.tr.selector,
                _ => c.seg[i.cr as usize].selector,
            };
            if i.memory == 1 {
                linear(s, address(&c, &i), &mut v.to_le_bytes(), true, false, noncanonical(&i))?;
            } else {
                let r = &mut s.cpu.gpr[i.reg as usize];
                *r = if i.size == 2 { *r & !0xffff | u64::from(v) } else { u64::from(v) };
            }
        }
        system::LLDT => {
            let selector = selector_operand(s)?;
            s.cpu.ldtr = if selector & 0xfffc == 0 { Segment { selector, ..Segment::NULL } } else { system_segment(s, selector, 2)?.1 };
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
            s.cpu.tr = Segment { attributes: tr.attributes | 2, ..tr };
        }
        system::MOV_TO_SREG => {
            let selector = selector_operand(s)?;
            load_data(s, i.cr as usize, selector)?;
            s.cpu.mov_ss = u64::from(i.cr as usize == SREG_SS);
        }
        // X1c: PUSH FS/GS: the selector zero-extended to the operand size (SDM Vol.2 PUSH), at RSP - size (#SS(0) when
        // not canonical). POP FS/GS: the popped selector loaded as MOV Sreg does (#GP/#NP checks, FS/GS base from the
        // descriptor), then RSP + size; a fault leaves RSP unchanged.
        system::PUSH_SREG => {
            let n = i.size as usize;
            let rsp = c.gpr[4].wrapping_sub(u64::from(i.size));
            let mut b = u64::from(c.seg[i.cr as usize].selector).to_le_bytes();
            linear(s, rsp, &mut b[..n], true, false, SS0)?;
            s.cpu.gpr[4] = rsp;
        }
        system::POP_SREG => {
            let n = i.size as usize;
            let mut b = [0u8; 8];
            linear(s, c.gpr[4], &mut b[..n], false, false, SS0)?;
            load_data(s, i.cr as usize, u16::from_le_bytes([b[0], b[1]]))?;
            s.cpu.gpr[4] = c.gpr[4].wrapping_add(u64::from(i.size));
        }
        system::JMP_FAR..=system::CALL_FAR => return far_transfer(s, i).map(Some),
        system::RET_FAR..=system::IRET => return far_return(s, i).map(Some),
        _ => halt(s, stop::BAD_REPLY),
    }
    Ok(None)
}

/// The linear address of `i`'s memory operand: base + index * scale + displacement (I10: truncated to 32 bits with 0x67,
/// RIP-relative included), plus the FS/GS base of an override.
pub(super) fn address(c: &Cpu, i: &System) -> u64 {
    let base = match i.base {
        system::RIP => c.rip.wrapping_add(u64::from(i.length)),
        system::NO_REG => 0,
        r => c.gpr[r as usize],
    };
    let index = if i.index == system::NO_REG { 0 } else { c.gpr[i.index as usize] << i.scale };
    let segment = match i.segment as usize {
        SREG_FS => c.fs_base,
        SREG_GS => c.gs_base,
        _ => 0,
    };
    let ea = base.wrapping_add(index).wrapping_add(i.disp as u64);
    let ea = if i.prefixes & system::ADDRESS32 != 0 { ea as u32 as u64 } else { ea };
    ea.wrapping_add(segment)
}
/// A non-canonical memory operand: #SS(0) for an SS-class reference (an SS override, or an RSP or RBP base with no
/// override), else #GP(0) (a CS/DS/ES override even on an RSP/RBP base; FS/GS, I7): Intel SDM Vol. 3A 4.5.1.
pub(super) fn noncanonical(i: &System) -> Exception {
    match i.segment as usize {
        SREG_SS => SS0,
        0 if matches!(i.base, 4 | 5) => SS0,
        _ => GP0,
    }
}

/// Copies between `buf` and linear `va` for the instruction at RIP: an explicit access at the CPL, or an implicit
/// supervisor one (`supervisor`: descriptor tables, TSS). Both pages of a crossing range are walked (A/D set as the CPU
/// does) before any byte moves, so a #PF (CR2 the first faulting byte's page start, or `va`) leaves memory unchanged;
/// a non-canonical range raises `noncanonical`. A write into a page holding translations invalidates them first
/// ([`FetchWrites`]: no block of this vCPU runs during a transaction). Guest RAM of any memory type is accessed (M2: UC RAM
/// in program order, [`pages`]); device pages stop `unsupported_mmio` (status `system`, M3).
pub(super) fn linear(s: &mut State, va: u64, buf: &mut [u8], write: bool, supervisor: bool, noncanonical: Exception) -> Result<(), Exception> {
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
        let last = va.wrapping_add(n - 1);
        if va & (align - 1) != 0 && paging::canonical(va) && paging::canonical(last) && last >= va {
            return Err([17, 0, 0]);
        }
    }
    let gpa = pages(s, va, n, write, supervisor, noncanonical)?;
    // S4: an explicit access meets data breakpoints; an implicit supervisor one (descriptor tables, TSS, IDT) never does.
    if !supervisor {
        super::watch::access(s, va, n, write);
    }
    let first = (4096 - (va & 4095)).min(n) as usize;
    // Only guest RAM is accessed, and nothing is written unless every byte is RAM (M3: a device page stops first).
    if !super::mmio::ram(gpa[0]) || (first < n as usize && !super::mmio::ram(gpa[1])) {
        super::mmio::system_access(s)
    }
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
    Ok(())
}
/// The guest-physical addresses of `[va, va + n)` (its page, and the next one when it crosses), walked for [`linear`]
/// (which accesses only guest RAM) and for CLFLUSH (M3: a device page is uncached, nothing to flush; the walk's faults
/// are all it has). M2 (owner decision, November 2): when either page is UC RAM, or a UC access was made since the last
/// boundary, every earlier access is made globally visible before the caller's access (a barrier here), and the next
/// boundary makes a UC one so before any later one (`uncached_end`).
pub(super) fn pages(s: &mut State, va: u64, n: u64, write: bool, supervisor: bool, noncanonical: Exception) -> Result<[u64; 2], Exception> {
    let last = va.wrapping_add(n - 1);
    if !paging::canonical(va) || !paging::canonical(last) || last < va {
        return Err(noncanonical);
    }
    let Some(mut control) = s.cpu.control() else { halt(s, stop::UNSUPPORTED_MODE) };
    if supervisor {
        control.cpl = 0;
    }
    let access = if write { Access::Write } else { Access::Read };
    let crosses = (last ^ va) >> 12 != 0;
    let mut gpa = [0u64; 2];
    for (k, at) in [va, last & !4095].into_iter().enumerate().take(1 + usize::from(crosses)) {
        gpa[k] = loop {
            match paging::walk(&mut GuestTables::observed(guest_ram(), CodeWrites(FetchWrites(s))), control, at, access) {
                Ok(t) => {
                    let c = cache::control();
                    // A UC access made since the last boundary (an instruction run alone, or the resident's own) is
                    // globally visible before this one, whatever this one's type.
                    let owed = c.uncached_count != 0 || c.uncached_fence != 0;
                    let uncached = super::mmio::ram(t.gpa) && cpu::memory_type(msrs(), t.gpa, t.pat) == cpu::memory_type::UC;
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
    Ok(gpa)
}
/// Updates the guest byte at linear `va` atomically, as an implicit supervisor write (walked for write first: a
/// read-only page is #PF(3) at `va`): `update` maps the current value to the new one, or `None` to leave it (then
/// `Ok(Err(current))`). The code-write protocol applies as for [`linear`]. Used for the accessed and busy bits, which
/// hardware sets by a locked read-modify-write.
fn atomic_byte(s: &mut State, va: u64, update: impl FnMut(u8) -> Option<u8>) -> Result<Result<u8, u8>, Exception> {
    let gpa = pages(s, va, 1, true, true, GP0)?[0];
    let Some(p) = guest_ram().range(gpa, 1) else { super::mmio::system_access(s) };
    if cache::translated(gpa) {
        FetchWrites(s).code_write(gpa);
    }
    // SAFETY: `p` is a guest RAM byte in the resident's alias (Normal memory); peers and guest code access it too, so
    // only atomic accesses are made here.
    let byte = unsafe { &*(p as *const core::sync::atomic::AtomicU8) };
    let done = byte.fetch_update(core::sync::atomic::Ordering::SeqCst, core::sync::atomic::Ordering::SeqCst, update);
    cache::rechecked(gpa);
    Ok(done)
}

/// The descriptor `selector` names (8 bytes, or 16 for a `wide` system descriptor) in the GDT, or for TI = 1 the LDT,
/// and its linear address: #GP(selector) when it does not lie within the table's limit, when TI = 1 with a null LDTR,
/// or when its address is not canonical.
pub(super) fn descriptor(s: &mut State, selector: u16, wide: bool) -> Result<(u64, u128), Exception> {
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
    linear(s, va, &mut raw[..if wide { 16 } else { 8 }], false, true, gp(selector))?;
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
    (raw >> 44 & 1 != 0, raw >> 40 & 15, raw >> 45 & 3, raw >> 47 & 1 != 0)
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
    Ok((va, Segment { base, ..Segment::loaded(selector, raw as u64) }))
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
        Segment { selector, ..Segment::NULL }
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
            if !code_or_data || (code && kind & 2 == 0) || (!conforming && (rpl > dpl || cpl > dpl)) {
                return Err(gp(selector));
            }
            if !present {
                return Err(np(selector));
            }
        }
        accessed(s, va, raw)?;
        Segment::loaded(selector, raw | 1 << 40)
    };
    match sreg {
        SREG_FS => s.cpu.fs_base = loaded.base,
        SREG_GS => s.cpu.gs_base = loaded.base,
        _ => {}
    }
    s.cpu.seg[sreg] = Segment { base: 0, ..loaded };
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
        halt(s, stop::UNSUPPORTED_SYSTEM)
    }
    let conforming = kind & 4 != 0;
    if !code_or_data
        || kind & 8 == 0
        || if conforming { dpl > cpl } else { u64::from(selector & 3) > cpl || dpl != cpl }
        || raw >> 53 & 3 == 3
    {
        return Err(gp(selector));
    }
    if !present {
        return Err(np(selector));
    }
    Ok((va, raw))
}
/// Loads CS with `selector` (RPL as given) from `raw`, refusing a compatibility-mode segment: the record then holds
/// that CS (the stop's cause) with RIP still at the instruction.
fn load_cs(s: &mut State, selector: u16, raw: u64) {
    s.cpu.seg[SREG_CS] = Segment { base: 0, ..Segment::loaded(selector, raw | 1 << 40) };
    if s.cpu.control().is_none() {
        halt(s, stop::UNSUPPORTED_MODE)
    }
}
/// Halts `unsupported_mode` before any effect when `raw` is not a 64-bit code segment (L = 1, D = 0).
fn refuse_compatibility(s: &State, selector: u16, raw: u64) {
    if raw >> 53 & 3 != 1 {
        let mut refused = *s;
        refused.cpu.seg[SREG_CS] = Segment { base: 0, ..Segment::loaded(selector, raw) };
        halt(&refused, stop::UNSUPPORTED_MODE)
    }
}

/// JMP/CALL far through an m16:`size` pointer to a code segment at the same CPL (SDM Vol.2 JMP, CALL): the target checks
/// ([`far_target`]), then for CALL #SS(0) when the return address (CS and RIP, `size` bytes each) would not be canonical,
/// #GP(0) for a non-canonical offset, the accessed bit, CALL's pushes; CS = selector with RPL = CPL.
fn far_transfer(s: &mut State, i: System) -> Result<u64, Exception> {
    let c = s.cpu;
    let w = i.size as usize;
    let mut pointer = [0u8; 10];
    linear(s, address(&c, &i), &mut pointer[..w + 2], false, false, noncanonical(&i))?;
    let (mut offset, mut word) = ([0u8; 8], [0u8; 2]);
    offset[..w].copy_from_slice(&pointer[..w]);
    word.copy_from_slice(&pointer[w..w + 2]);
    let (rip, selector) = (u64::from_le_bytes(offset), u16::from_le_bytes(word));
    let (va, raw) = far_target(s, selector)?;
    refuse_compatibility(s, selector, raw);
    let call = i.kind == system::CALL_FAR;
    let rsp = c.gpr[4].wrapping_sub(if call { 2 * w as u64 } else { 0 });
    if call && (!paging::canonical(rsp) || !paging::canonical(c.gpr[4].wrapping_sub(1))) {
        return Err(SS0);
    }
    if !paging::canonical(rip) {
        return Err(GP0);
    }
    accessed(s, va, raw)?;
    if call {
        let mut frame = [0u8; 16];
        frame[..w].copy_from_slice(&c.rip.wrapping_add(u64::from(i.length)).to_le_bytes()[..w]);
        frame[w..2 * w].copy_from_slice(&u64::from(c.seg[SREG_CS].selector).to_le_bytes()[..w]);
        linear(s, rsp, &mut frame[..2 * w], true, false, SS0)?;
        s.cpu.gpr[4] = rsp;
    }
    load_cs(s, selector & !3 | c.cpl as u16, raw);
    Ok(rip)
}

/// RET far (`size`-byte pops, imm16 `disp` released) and IRET (SDM Vol.2 RET, IRET, IA-32e mode): IRET #GP(0) with
/// RFLAGS.NT; the pops (#SS(0) when not canonical); the return CS: #GP(0) null, #GP(selector) outside the table, not
/// code, RPL < CPL, conforming with DPL > RPL, nonconforming with DPL != RPL, #NP(selector) not present; a
/// compatibility-mode CS is refused; then, for IRET and for a return to an outer level, SS:RSP from the stack: a null SS
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
        linear(s, at, &mut b[..n * w as usize], false, false, SS0)?;
        let mut words = [0u64; 5];
        for (k, word) in words.iter_mut().enumerate().take(n) {
            let mut q = [0u8; 8];
            q[..w as usize].copy_from_slice(&b[k * w as usize..(k + 1) * w as usize]);
            *word = u64::from_le_bytes(q);
        }
        Ok(words)
    };
    let words = pops(s, c.gpr[4], if iret { 5 } else { 2 })?;
    let (rip, selector) = (words[0], words[1] as u16);
    if selector & 0xfffc == 0 {
        return Err(GP0);
    }
    let (va, raw) = descriptor(s, selector, false)?;
    let raw = raw as u64;
    let (code_or_data, kind, dpl, present) = fields(raw);
    let (cpl, rpl) = (c.cpl, u64::from(selector & 3));
    if !code_or_data || kind & 8 == 0 || rpl < cpl || if kind & 4 != 0 { dpl > rpl } else { dpl != rpl } {
        return Err(gp(selector));
    }
    if !present {
        return Err(np(selector));
    }
    refuse_compatibility(s, selector, raw);
    if iret && !paging::canonical(rip) {
        return Err(GP0);
    }
    let outer = rpl > cpl;
    let mut stack = None;
    if iret || outer {
        let [rsp, ss_selector, ..] = if iret { [words[3], words[4], 0, 0, 0] } else { pops(s, c.gpr[4].wrapping_add(2 * w + release), 2)? };
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
            if u64::from(ss_selector & 3) != rpl || !code_or_data || kind & 8 != 0 || kind & 2 == 0 || dpl != rpl {
                return Err(gp(ss_selector));
            }
            if !present {
                return Err(ss(ss_selector));
            }
            Some((ss_va, ss_raw))
        };
        stack = Some((rsp, ss_selector, ss_segment));
    }
    if !iret && !paging::canonical(rip) {
        return Err(GP0);
    }
    // CPL 1 and 2 are not built ([`Cpu::control`]): refused with nothing changed.
    if matches!(rpl, 1 | 2) {
        halt(s, stop::UNSUPPORTED_MODE)
    }
    let mut rflags = c.rflags;
    if iret {
        let mask = IRET_ALWAYS | if cpl <= (c.rflags & RFLAGS_IOPL) >> 12 { RFLAGS_IF } else { 0 } | if cpl == 0 { IRET_CPL0 } else { 0 };
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
                Some((_, ss_raw)) => Segment { base: 0, ..Segment::loaded(ss_selector, ss_raw | 1 << 40) },
                None => Segment { selector: ss_selector, ..Segment::NULL },
            };
            s.cpu.gpr[4] = rsp.wrapping_add(if iret { 0 } else { release });
        }
        None => s.cpu.gpr[4] = c.gpr[4].wrapping_add(2 * w + release),
    }
    if outer {
        for sreg in [SREG_ES, SREG_DS, SREG_FS, SREG_GS] {
            let a = s.cpu.seg[sreg].attributes;
            let (kind, dpl) = (u64::from(a & 15), u64::from(a >> 5 & 3));
            if a & ATTR_P == 0 || ((kind & 8 == 0 || kind & 4 == 0) && dpl < rpl) {
                s.cpu.seg[sreg] = Segment::NULL;
            }
        }
        set_cpl(s, rpl);
    }
    Ok(rip)
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
        linear(s, address(&c, &i), &mut b, false, false, noncanonical(&i))?;
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
        let (code, conforming) = (code_or_data && kind & 8 != 0, code_or_data && kind & 0xc == 0xc);
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
        *r = if i.size == 2 { *r & !0xffff | v & 0xffff } else { v & if i.size == 4 { 0xffff_ffff } else { u64::MAX } };
    }
    Ok(())
}
