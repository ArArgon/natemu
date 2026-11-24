//! M2 Part 2: the interpreter's own resident operations (boot-interpreter plan sections 5 and 6;
//! docs/restructure/interp-transitions.md): far JMP and far RET in protected and compatibility mode, MOV Sreg in protected
//! mode, MOV to CR0/CR2/CR3/CR4 with the mode transitions, and HLT. The architectural rules are pure functions in
//! `natemu_abi::interp::system` (host unit tests); here are the memory accesses, the commit and the resident's
//! consequences. Every check is made before anything changes, so a typed stop leaves the state at the instruction.
//!
//! The descriptor read and the accessed-bit write are implicit supervisor accesses through the interpreter's own memory
//! ([`Mem`]: the identity with paging off, the 4-level walk at CPL 0 in compatibility mode, guest RAM, unassigned memory
//! and device pages as every interpreted access, plan section 4), not through `segments::descriptor` and
//! `segments::accessed`: their memory goes through `segments::pages`, whose walk controls (`controls`) exist only in the
//! states translated code runs. `segments::far_target` is not called either: it applies the 64-bit rules (L = D = 1 is
//! #GP whatever LMA; a task gate or TSS is #GP) and halts on a call gate at CPL 0. Shared with the 64-bit functions,
//! which stay as they are for their callers: the descriptor's layout (`Segment::loaded`) and the CS load with its
//! accessed bit (`segments::load_cs`).
//!
//! What a change leaves for translated code after the hand-off is kept exact as the 64-bit transaction keeps it: a change
//! of CR0.PG (IA-32e activation and deactivation, LMA with it), CR0.WP or a CR4 paging bit flushes the TLBs globally
//! (`tlb(s, true)`), a CR3 load flushes them as a CR3 load does (`tlb(s, false)`); the interpreter itself uses no shadow
//! and no translated block, and its records are keyed by mode, CPL and CS.D and checked by their bytes.
use super::*;
use natemu_abi::interp::system::{self as rules, Check};

/// A rule's result as the typed stop it is.
fn checked(c: Check) -> Stop {
    match c {
        Check::Fault { vector, error } => Stop::exception(u64::from(vector), u64::from(error), 0),
        Check::Gate => Stop::unsupported(detail::GATE),
        Check::Outer => Stop::unsupported(detail::OUTER),
        Check::Paging => Stop::new(stop::INTERP_MODE, detail::PAGING, [0; 3]),
    }
}

/// The interpreter's memory for one resident operation's accesses.
fn memory(s: &mut State, paging: Option<paging::Control>) -> Mem<'_> {
    Mem {
        s,
        paging,
        pages: [[0; 2]; 4],
        aliases: [0; 4],
        data: false,
        count: 0,
        stopped: [0; 2],
        checks: [(0, 0, false); 4],
        checked: 0,
        generation: 0,
    }
}

/// The 8-byte descriptor `selector` (not null) names and its linear address: the table checks ([`rules::entry`]), then the
/// read. With paging off the address wraps at 4 GiB as every linear address does; a table entry above 4 GiB in
/// compatibility mode (a GDT loaded high in 64-bit mode; none of the surveyed stubs) is a typed stop.
fn descriptor(m: &mut Mem, selector: u16) -> Result<(u32, u64), Stop> {
    let at = rules::entry(&m.s.cpu, selector).map_err(checked)?;
    if m.paging.is_some() && at.wrapping_add(7) > u64::from(u32::MAX) {
        return Err(Stop::unsupported(detail::TABLE));
    }
    let at = at as u32;
    m.check(at, 8, false).map_err(|e| exited(e, m.stopped))?;
    let low = m.read(at, 4);
    let high = m.read(at.wrapping_add(4), 4);
    Ok((at, u64::from(high) << 32 | u64::from(low)))
}

/// Sets the accessed bit of the descriptor at `at` when `raw` has it clear (SDM Vol.3 3.4.5.1), as the CPU does: an
/// atomic OR of its access byte (a peer's change to the byte is kept), an implicit supervisor write (a #PF on a read-only
/// GDT page under CR0.WP, CR2 that byte: segment_model B8's choice). The last check of an operation: nothing else has
/// changed when it faults. Unassigned memory ignores it.
fn accessed(m: &mut Mem, at: u32, raw: u64) -> Result<(), Stop> {
    if raw >> 40 & 1 != 0 {
        return Ok(());
    }
    let byte = at.wrapping_add(5);
    m.check(byte, 1, true).map_err(|e| exited(e, m.stopped))?;
    let mut was = m.read(byte, 1);
    while was & 1 == 0 {
        match m.compare_exchange(byte, 1, was, was | 1) {
            now if now == was => break,
            now => was = now,
        }
    }
    Ok(())
}

/// The new CS of a far transfer to `selector` and its descriptor (read, checked by `check`, its accessed bit set in
/// memory), or the typed stop: #GP(0) for a null selector first.
fn target(
    s: &mut State,
    paging: Option<paging::Control>,
    selector: u16,
    check: impl FnOnce(&Cpu, u64) -> Result<Segment, Check>,
) -> Result<(Segment, u64), Stop> {
    if rules::null(selector) {
        return Err(GP0);
    }
    let mut m = memory(s, paging);
    let (at, raw) = descriptor(&mut m, selector)?;
    let cs = check(&m.s.cpu, raw).map_err(checked)?;
    accessed(&mut m, at, raw)?;
    Ok((cs, raw))
}

/// Far JMP (ptr16:16, ptr16:32, m16:16, m16:32; the core read the pointer) in protected or compatibility mode
/// ([`rules::jump`]): CS := the target with RPL = CPL, EIP := `offset`. Into 64-bit code with IA-32e mode active, the
/// state is one the translator runs: [`run`] hands the vCPU over at its next pass.
pub(super) fn far_jump(
    s: &mut State,
    paging: Option<paging::Control>,
    selector: u16,
    offset: u32,
) -> Result<(), Stop> {
    let (cs, raw) = target(s, paging, selector, |c, raw| rules::jump(c, selector, raw, offset))?;
    land(s, cs.selector, raw, offset);
    Ok(())
}

/// Far RET (16- or 32-bit, with or without imm16; the core read and checked both stack slots) to the same privilege
/// level ([`rules::ret`]): CS := the popped selector, EIP := `offset`, then ESP (or SP, by SS.B) releases `release`
/// bytes.
pub(super) fn far_return(
    s: &mut State,
    paging: Option<paging::Control>,
    selector: u16,
    offset: u32,
    release: u32,
) -> Result<(), Stop> {
    let (cs, raw) = target(s, paging, selector, |c, raw| rules::ret(c, selector, raw, offset))?;
    let ctx = exec::Context::of(&s.cpu);
    exec::release(&mut s.cpu, ctx, release);
    land(s, cs.selector, raw, offset);
    Ok(())
}

/// A far transfer's commit: CS `selector` from `raw` (`load_cs`: as the rules' segment, accessed), EIP `offset`; the
/// instruction completed (RF clear).
fn land(s: &mut State, selector: u16, raw: u64, offset: u32) {
    segments::load_cs(s, selector, raw);
    s.cpu.rip = u64::from(offset);
    s.cpu.rflags &= !RFLAGS_RF;
}

/// MOV `sreg` (ES, SS, DS, FS, GS: the core refused CS) := `selector` in protected or compatibility mode
/// ([`rules::null_load`], [`rules::load`]): FS and GS take the descriptor's base (a null selector clears it). No MOV SS
/// shadow: the interpreter delivers no event.
pub(super) fn load_segment(
    s: &mut State,
    paging: Option<paging::Control>,
    i: &Insn,
    sreg: u8,
    selector: u16,
) -> Result<(), Stop> {
    let sreg = usize::from(sreg).min(SREG_GS);
    let segment = if rules::null(selector) {
        rules::null_load(sreg, selector).map_err(checked)?
    } else {
        let mut m = memory(s, paging);
        let (at, raw) = descriptor(&mut m, selector)?;
        let segment = rules::load(&m.s.cpu, sreg, selector, raw).map_err(checked)?;
        accessed(&mut m, at, raw)?;
        segment
    };
    s.cpu.seg[sreg] = segment;
    complete(s, i);
    Ok(())
}

/// MOV CRn := `value` (CR0, CR2, CR3, CR4; the other numbers are #UD, CR8 being 64-bit mode's) at CPL 0 outside 64-bit
/// mode, with the transitions of [`rules::cr0`] (PE set and cleared, IA-32e activation and deactivation; paging without
/// LME is a typed stop) and the checks of [`rules::cr4`]; then the TLB consequences (module doc). A CR3 load also
/// starts the address space's state as the 64-bit transaction's does (the x87 integer kept, the stack-trap count).
pub(super) fn mov_to_cr(s: &mut State, i: &Insn, cr: u8, value: u32) -> Result<(), Stop> {
    let c = s.cpu;
    match cr {
        0 => {
            let (cr0, efer) = rules::cr0(&c, value).map_err(checked)?;
            (s.cpu.cr0, s.cpu.efer) = (cr0, efer);
            if (cr0 ^ c.cr0) & (CR0_PG | CR0_WP) != 0 || efer != c.efer {
                tlb(s, true);
            }
        }
        2 => s.cpu.cr2 = u64::from(value),
        3 => {
            s.cpu.cr3 = u64::from(value);
            x87::address_space(c.cr3 & !(1 << 63), s.cpu.cr3);
            s.cpu.stack_traps = 0;
            tlb(s, false); // a load of the same value too
        }
        4 => {
            s.cpu.cr4 = rules::cr4(&c, value).map_err(checked)?;
            if (s.cpu.cr4 ^ c.cr4) & CR4_PAGING != 0 {
                tlb(s, true);
            }
        }
        _ => return Err(UD),
    }
    complete(s, i);
    Ok(())
}

/// HLT (CPL 0: the interpreter's only CPL in protected mode): it completes (EIP past it), then waits in `events::park`
/// until an event can be taken: an INIT wakes it and the next boundary takes it ([`events`]); an NMI or, with RFLAGS.IF,
/// an interrupt wakes it for that boundary's typed stop; a SIPI changes nothing in a vCPU neither INIT-pending nor
/// waiting for one, so it neither wakes nor posts (initsipi_model). Every other vCPU halted ends the run there, as for a
/// translated HLT.
pub(super) fn hlt(s: &mut State, i: &Insn) {
    complete(s, i);
    (s.cpu.mov_ss, s.sti_shadow) = (0, 0); // no shadow holds the wake's event (the interpreter sets none)
    events::park(s);
}
