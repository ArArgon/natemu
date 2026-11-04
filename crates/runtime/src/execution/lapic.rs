//! X3, the local APIC in the resident (SDM Vol.3 ch. 11; the rules and the presented APIC are `natemu_abi::lapic`'s).
//!
//! **State.** Each vCPU's APIC is its own record ([`Lapic`], private to its resident), the bank's `Cpu::cr8` (the TPR,
//! bits 7:0; CR8 reads and writes TPR[7:4], 11.8.6) and its shared [`Events`]: `vectors` is the IRR (senders set bits
//! with an atomic OR, then ring the target's doorbell), `nmi` the NMI pending bit, `apic` the word senders match
//! destinations against ([`apic::published`], written only here). IA32_APIC_BASE is the MSR record's slot.
//!
//! **Delivery** (11.8): the highest IRR vector whose class is above the PPR's (`max(TPR, ISRV)` by class) is taken at the
//! next boundary where RFLAGS.IF and no shadow allow it ([`super::events`]): its IRR bit moves to the ISR and the
//! vector is delivered through the IDT. EOI clears the highest ISR bit; every transaction and device access returns to
//! `find`, whose loop evaluates again, so an EOI or TPR write that unmasks an interrupt takes it at the next boundary.
//!
//! **Access.** xAPIC: the 4 KiB page at the base, answered here from `mmio::local` with no host exit (32-bit accesses
//! at 16-byte-aligned offsets only, 11.4.1; any other access is refused as the device's, `unsupported_mmio`); a base
//! moved onto guest RAM stops `unsupported_system` (the resident cannot overlay RAM; Linux never moves it). x2APIC:
//! MSRs 0x800-0x8ff as resident transactions with 11.12's rules (#GP for an index not implemented, a write to a
//! read-only or a read of a write-only register, a nonzero write to EOI or ESR and any reserved bit set, 11.12.1.3).
//! IA32_APIC_BASE moves through the 11.12.5 states: x2APIC -> xAPIC and disabled -> x2APIC are #GP; entering the
//! disabled state resets the APIC.
//!
//! **IPIs** (11.6): an ICR write sends at once (delivery status always idle): fixed and lowest-priority set the target's
//! IRR bit, NMI its NMI bit, by atomic OR, then ring it (an SGI to a peer; this vCPU looks at its own events at its next
//! boundary). INIT and start-up stop `unsupported_system` (`ap_start`: real mode does not exist; X4 starts APs through the ACPI
//! mailbox), as do SMI and the reserved modes, before any effect.
//!
//! **Timer** (11.5.4): counts the crystal clock (`clock::CRYSTAL_HZ`) through the divider from V = CNTVCT, exactly
//! (`clock`); the next expiry's V is programmed into the Arm virtual timer (CNTV, PPI 27 through the in-kernel GIC), whose
//! interrupt only rings the bell: the IRQ paths turn it off ([`timer_irq`]) and the next resident boundary ([`poll`])
//! takes every expiry due (an unmasked LVT sets the vector's IRR bit; periodic expiries that pass while one is pending
//! merge, as in the IRR) and arms the next. The current count is computed from V when read.
//!
//! **L4a:** the TMR is the shared `Events::tmr` (the sender sets or clears the bit with the IRR bit); an EOI of a vector
//! whose TMR bit is set is broadcast to the host's I/O APIC (`REQUEST_IRQ`, one host exit only then). LINT0 is the 8259's
//! output (`Events::lint0`): with LVT LINT0 ExtINT and unmasked (or the APIC hardware disabled) it is taken before the
//! IRR when RFLAGS.IF allows, its vector from the PIC's INTA cycle (`REQUEST_IRQ`), never entering the IRR or ISR.
//!
//! LINT1 in NMI mode is published (`lapic::LINT1_NMI`): the host's NMI input posts the NMI to the APICs taking it.
//!
//! **Not built:** LINT0/LINT1 in fixed mode (no board source drives them so), thermal
//! and performance-counter sources (none in the profile), INIT/start-up (real mode; X4 uses the mailbox), TSC-deadline
//! (not advertised).
use super::*;
use super::segments::Exception;
use core::sync::atomic::{AtomicU64, Ordering::SeqCst};
use natemu_abi::clock;
use natemu_abi::execution::cache::{EVENTS, Events, GLOBAL_VA};
use natemu_abi::execution::{MMIO_VA, Mmio, REPLY_IRQ, REQUEST_IRQ};
use natemu_abi::lapic::{self as apic, Lapic, icr};

const EN: u64 = 1 << 11;
const EXTD: u64 = 1 << 10;
const BASE: u64 = 0xff_ffff_f000;
/// The Arm virtual timer's interrupt (a PPI, the same INTID on every vCPU).
pub(super) const TIMER_PPI: u64 = 27;

fn lapic() -> &'static mut Lapic {
    // SAFETY: the record is mapped EL1 read/write in this vCPU's window; only its resident uses it once it runs, and the
    // resident is single-threaded. A caller may hold one across a call that takes another (e.g. `timer` across `raise`),
    // but never uses the first after the second is taken: every access is sequential.
    unsafe { &mut *(LAPIC_VA as *mut Lapic) }
}
/// vCPU `v`'s event record word at `field` (an offset in [`Events`]).
pub(super) fn event_word(v: u64, field: usize) -> &'static AtomicU64 {
    let at = GLOBAL_VA + (EVENTS + v as usize * size_of::<Events>() + field) as u64;
    // SAFETY: the global area is mapped EL1 read/write in every vCPU's window; `at` is an aligned word of vCPU v's record
    // (v < MAX_VCPUS: callers pass this vCPU's number, checked at start, or a peer below `State::vcpus`). Every user of
    // the record (residents and host) accesses it atomically.
    unsafe { AtomicU64::from_ptr(at as *mut u64) }
}
fn irr_word(v: u64, w: usize) -> &'static AtomicU64 {
    event_word(v, core::mem::offset_of!(Events, vectors) + 8 * w)
}
fn tmr_word(v: u64, w: usize) -> &'static AtomicU64 {
    event_word(v, core::mem::offset_of!(Events, tmr) + 8 * w)
}
/// Accepts edge-triggered `vector` into vCPU `v`'s IRR (11.8.4): its TMR bit cleared, then the IRR bit set (the host's
/// `post` order). Every resident source is edge-triggered (LVT timer and error, IPIs: the trigger of a fixed IPI is
/// ignored, 11.6.1), so a vector last accepted level-triggered must not keep TMR = 1 (its EOI would broadcast).
fn accept_edge(v: u64, vector: u64) {
    let (w, bit) = (vector as usize / 64, 1 << (vector % 64));
    tmr_word(v, w).fetch_and(!bit, SeqCst);
    irr_word(v, w).fetch_or(bit, SeqCst);
}
fn word_of(v: u64) -> u64 {
    event_word(v, core::mem::offset_of!(Events, apic)).load(SeqCst)
}
fn base_slot() -> usize {
    cpu::msr(cpu::MSR_APIC_BASE).unwrap()
}
fn base() -> u64 {
    msr_get(base_slot())
}
fn publish(s: &State) {
    event_word(s.vcpu, core::mem::offset_of!(Events, apic)).store(apic::published(base(), lapic(), s.vcpu), SeqCst);
}
/// V without a barrier (see [`poll`]).
fn count() -> u64 {
    let v: u64;
    // SAFETY: reads the virtual count.
    unsafe { asm!("mrs {}, cntvct_el0", out(reg) v, options(nomem, nostack)) };
    v
}
fn now() -> u64 {
    let v: u64;
    // SAFETY: reads the virtual count.
    unsafe { asm!("isb", "mrs {}, cntvct_el0", out(reg) v) };
    v
}

/// At start: V's rate (the guest's CNTFRQ_EL0, measured, never assumed) and the virtual timer off.
pub(super) fn init() {
    let f: u64;
    unsafe { asm!("mrs {}, cntfrq_el0", out(reg) f) };
    lapic().frequency = f.max(1);
    cntv(None);
}
/// Programs the Arm virtual timer to fire at V = `at`, or turns it off.
fn cntv(at: Option<u64>) {
    // SAFETY: EL1 access to the vCPU's own virtual timer (CNTV_CVAL_EL0, CNTV_CTL_EL0: ENABLE, IMASK clear).
    unsafe {
        match at {
            Some(v) => asm!("msr cntv_cval_el0, {v}", "mov {t}, #1", "msr cntv_ctl_el0, {t}", "isb", v = in(reg) v, t = out(reg) _),
            None => asm!("msr cntv_ctl_el0, xzr", "isb"),
        }
    }
}
/// The virtual timer's IRQ, before its end of interrupt: with the expiry due, the timer goes off (it is level-triggered:
/// left on, it would interrupt again at once) and the next boundary ([`poll`]) takes the expiry and arms the next one;
/// an IRQ that comes before the expiry (a halted vCPU's early wake, [`wait`], or one pending while the timer was
/// re-armed for a later expiry) leaves the timer armed at the expiry, or nothing would wake a halted vCPU then.
/// Whether the expiry was due.
pub(super) fn timer_irq() -> bool {
    let d = lapic().deadline;
    let early = d != u64::MAX && now() < d;
    cntv(early.then_some(d));
    !early
}

/// The interrupt the APIC delivers now (11.8.4), if any: hardware and software enabled, the highest IRR vector above PPR.
pub(super) fn deliverable(s: &State) -> Option<u64> {
    let irr = [0, 1, 2, 3].map(|w| irr_word(s.vcpu, w).load(SeqCst));
    if irr == [0; 4] || word_of(s.vcpu) & apic::SW_ENABLED == 0 {
        return None;
    }
    apic::deliverable(&irr, s.cpu.cr8, &lapic().isr)
}
/// The core takes `v` ([`deliverable`]): its IRR bit moves to the ISR.
pub(super) fn accept(s: &State, v: u64) {
    irr_word(s.vcpu, v as usize / 64).fetch_and(!(1 << (v % 64)), SeqCst);
    lapic().isr[v as usize / 64] |= 1 << (v % 64);
}
/// EOI: the highest in-service vector ends; a level-triggered one (its TMR bit set, L4a) is broadcast to the I/O APIC
/// (one host exit: QEMU's `apic_eoi`; no directed EOI, SVR bit 12 is reserved).
fn eoi(s: &mut State) {
    let l = lapic();
    if let Some(v) = apic::highest(&l.isr) {
        l.isr[v as usize / 64] &= !(1 << (v % 64));
        if tmr_word(s.vcpu, v as usize / 64).load(SeqCst) >> (v % 64) & 1 != 0 {
            controller(s, true, v);
        }
    }
}
/// L4a: a request to the host's interrupt controllers ([`REQUEST_IRQ`]): the EOI broadcast of `value` (`eoi`), or the
/// INTA cycle of an ExtINT, whose vector is returned. The state goes to the record and back in place (as `refill`). The
/// exchange record is put back afterwards: an xAPIC EOI is itself a device access in progress (`mmio::device`), and the
/// record keeps describing the last device access for the run report.
#[inline(never)]
fn controller(s: &mut State, eoi: bool, value: u64) -> u64 {
    let exchange = MMIO_VA as *mut Mmio;
    let bytes = size_of::<State>();
    unsafe {
        let saved = read_record(exchange);
        write_record(exchange, &Mmio { write: u64::from(eoi), value, completed: saved.completed, ..Mmio::default() });
        (s.request, s.reply) = (REQUEST_IRQ, NONE);
        copy_record(record().cast(), (&raw const *s).cast(), bytes);
        let p = crate::perf::enter(crate::perf::HOST);
        asm!("dsb sy", "hvc #{imm}", imm = const TRANSLATE_HVC);
        crate::perf::leave(p);
        copy_record((&raw mut *s).cast(), record().cast(), bytes);
        s.request = NONE;
        let m = read_record(exchange);
        if s.reply != REPLY_IRQ || m.status != natemu_abi::execution::mmio::DONE {
            halt(s, stop::BAD_REPLY)
        }
        write_record(exchange, &saved);
        m.value & 0xff
    }
}
/// L4a: an ExtINT is pending at LINT0 (the 8259's output asserted) and this APIC passes it to the core: hardware
/// disabled (the PIC wired through, QEMU's `apic_accept_pic_intr`), or LVT LINT0 unmasked with delivery mode ExtINT.
/// It ignores the priority rules (QEMU's `apic_get_interrupt` takes it before the IRR) and needs RFLAGS.IF.
pub(super) fn extint(s: &State) -> bool {
    event_word(s.vcpu, core::mem::offset_of!(Events, lint0)).load(SeqCst) != 0 && word_of(s.vcpu) & apic::EXTINT != 0
}
/// L4a: takes the ExtINT: the PIC's vector (its INTA cycle, which may give the spurious IRQ 7 when a peer took it first).
pub(super) fn take_extint(s: &mut State) -> u64 {
    controller(s, false, 0)
}
/// A local interrupt (LVT, self IPI) with `vector`: into this vCPU's IRR, or a receive-illegal-vector error (11.5.3);
/// a software-disabled APIC accepts none (as from a peer, [`send`]).
fn raise(s: &mut State, vector: u64) {
    if vector < 16 {
        error(s, apic::RECEIVE_ILLEGAL)
    } else if lapic().svr & apic::SVR_ENABLE != 0 {
        accept_edge(s.vcpu, vector);
    }
}
/// An APIC error: recorded for the next ESR write, and the LVT error interrupt raised unless masked.
fn error(s: &mut State, bits: u64) {
    let l = lapic();
    l.errors |= bits;
    let e = l.lvt[apic::ERROR];
    let v = e & 0xff;
    if e & apic::MASKED == 0 {
        if v < 16 {
            l.errors |= apic::RECEIVE_ILLEGAL;
        } else {
            accept_edge(s.vcpu, v);
        }
    }
}

/// The timer's divided ticks since its count was loaded.
fn ticks(l: &Lapic) -> u64 {
    (clock::crystal(now(), l.frequency) - l.start) / apic::divisor(l.dcr)
}
fn current_count(l: &Lapic) -> u64 {
    if l.spent != 0 { 0 } else { apic::current_count(l.initial, l.lvt[apic::TIMER] & apic::PERIODIC != 0, ticks(l)) }
}
/// Takes the timer's expiries due by now and arms the virtual timer for the next one (after any timer register change,
/// and from [`poll`]).
fn timer(s: &mut State) {
    let l = lapic();
    let periodic = l.lvt[apic::TIMER] & apic::PERIODIC != 0;
    let n = if l.spent != 0 { 0 } else { apic::expiries(l.initial, periodic, ticks(l)) };
    if n > l.fired {
        // L5: expiries that passed while one was due merge into one (the IRR's rule); so does one whose vector is still
        // pending. Under reinject each is delivered later, one per acceptance ([`reinject`]).
        let merged = n - l.fired - 1;
        (l.fired, l.spent) = (n, u64::from(!periodic));
        if l.lvt[apic::TIMER] & apic::MASKED == 0 {
            let v = l.lvt[apic::TIMER] & 0xff;
            let missed = merged + u64::from(v >= 16 && irr_pending(s.vcpu, v));
            raise(s, v);
            let l = lapic();
            l.missed += missed;
            if l.reinject != 0 && periodic {
                l.backlog += missed;
            }
        }
    }
    let l = lapic();
    let next = apic::next_expiry(l.initial, periodic, l.fired)
        .filter(|_| l.spent == 0)
        .and_then(|t| t.checked_mul(apic::divisor(l.dcr)))
        .and_then(|t| t.checked_add(l.start))
        .map(|edges| clock::count_at_crystal(edges, l.frequency));
    l.deadline = next.unwrap_or(u64::MAX);
    cntv(next);
}
/// At every resident boundary (`find`, a halted vCPU's wake): the timer's expiries that are due, and under reinject the
/// next owed one once the last was accepted.
pub(super) fn poll(s: &mut State) {
    let d = lapic().deadline;
    // Perf chunk 12: the count read without `now`'s barrier (at every boundary): it may be read early and miss an expiry
    // just due. None is lost: `cntv` (ending in an `isb`) leaves the virtual timer armed at the deadline, and its IRQ's
    // `timer_irq` reads the count with the barrier, re-arming it if early; once due, the IRQ's exception entry is a
    // context synchronization, so this read at any boundary after it sees the deadline passed.
    if d != u64::MAX && count() >= d {
        timer(s)
    }
    if lapic().backlog != 0 {
        reinject(s)
    }
}
/// L5 reinject: one owed expiry, raised when the vector's IRR bit is clear (the previous one accepted), as KVM's PIT
/// injects its next pending tick on the acknowledge of the last; a masked or periodic-off timer owes nothing.
fn reinject(s: &mut State) {
    let l = lapic();
    let t = l.lvt[apic::TIMER];
    let v = t & 0xff;
    if t & apic::MASKED != 0 || t & apic::PERIODIC == 0 || v < 16 {
        l.backlog = 0;
    } else if !irr_pending(s.vcpu, v) {
        (l.backlog, l.reinjected) = (l.backlog - 1, l.reinjected + 1);
        raise(s, v);
    }
}
fn irr_pending(v: u64, vector: u64) -> bool {
    irr_word(v, vector as usize / 64).load(SeqCst) >> (vector % 64) & 1 != 0
}
/// L5: one wait of a halted vCPU for an interrupt (`park`'s `wfi`), woken on time for its timer's next expiry. macOS
/// lets a blocked thread's timer fire up to half its interval late (measured: a `wfi` on CNTV for 0.2, 2 and 10 ms woke
/// 0.1, 1 and 5 ms late; host `nanosleep` the same), so CNTV is armed at half the time left and the wait repeats; once
/// at most [`SPIN_US`] are left the vCPU spins on CNTVCT until the expiry or a pending interrupt (ISR_EL1.I). The timer
/// is left armed at the expiry itself, as `timer` set it. The early wakes are not stops ([`timer_irq`] says the expiry
/// was not due; `park` does not count them). Measured cost: the vCPU's core is busy about 2 % at a 250 Hz periodic
/// timer and 5.4 % at 1 kHz (the review's `hltcpu` probe); lateness 4-7 us typically, with a tail of 90-388 us when the
/// final short wait or the spin is preempted.
pub(super) fn wait(s: &State) {
    let (d, f, n) = (lapic().deadline, lapic().frequency, now());
    // With nothing its timer can deliver (masked, IF = 0, a class not above the PPR) the timer cannot end the halt:
    // a plain wait, no early wakes, no spin.
    if d == u64::MAX || d <= n || !can_wake(s) {
        // SAFETY: waits for an interrupt (masked at EL1: it only wakes the PE); no memory effect.
        return unsafe { asm!("dsb sy", "wfi") };
    }
    if d - n <= clock::scale(SPIN_US, f, 1_000_000) {
        while now() < d {
            let isr: u64;
            // SAFETY: reads the pending-interrupt status.
            unsafe { asm!("mrs {}, isr_el1", out(reg) isr) };
            if isr & 1 << 7 != 0 {
                return;
            }
            core::hint::spin_loop();
        }
        return;
    }
    cntv(Some(n + (d - n) / 2));
    unsafe { asm!("dsb sy", "wfi") };
    cntv(Some(d));
}
/// The longest a halted vCPU spins for its timer instead of waiting ([`wait`]).
const SPIN_US: u64 = 25;
/// The timer will interrupt this vCPU by itself: armed, LVT unmasked, and the vector its expiry raises (its own, or the
/// LVT error vector for an illegal one) deliverable then (RFLAGS.IF, its class above the PPR's).
pub(super) fn can_wake(s: &State) -> bool {
    let l = lapic();
    let t = l.lvt[apic::TIMER];
    // The vector the expiry raises: the timer's, or for an illegal one (0..15) the LVT error entry's (`raise`, `error`).
    let e = l.lvt[apic::ERROR];
    let vector = match t & 0xff {
        _ if t & apic::MASKED != 0 => None,
        v if v >= 16 => Some(v),
        _ if e & apic::MASKED == 0 && e & 0xff >= 16 => Some(e & 0xff),
        _ => None,
    };
    l.deadline != u64::MAX
        && s.cpu.rflags & 1 << 9 != 0
        && vector.is_some_and(|v| v & 0xf0 > apic::ppr(s.cpu.cr8, &l.isr) & 0xf0)
}

/// Whether `gpa` lies in this vCPU's APIC page (IA32_APIC_BASE's address, whatever the APIC's mode): a resident device
/// page, never shadowed onto the all-ones page (L3 amendment), so enabling the APIC needs no shadow change.
pub(super) fn claims(gpa: u64) -> bool {
    gpa & BASE == base() & BASE
}
/// The xAPIC page: `gpa`'s access of `width` bytes (a store's `data`, else a load). `None`: not this vCPU's APIC page;
/// `Some(Err)`: an access the APIC does not implement (11.4.1: registers are 32 bits wide at 16-byte-aligned offsets).
/// With the APIC hardware disabled or in x2APIC mode the page is no APIC's: it answers as an unassigned page does (L3:
/// a read all-ones at its width, a write ignored), any width.
pub(super) fn mmio(s: &mut State, gpa: u64, width: u64, data: Option<u64>) -> Option<Result<u64, ()>> {
    let b = base();
    if !claims(gpa) {
        return None;
    }
    if b & (EN | EXTD) != EN {
        return Some(Ok(if data.is_some() { 0 } else if width == 8 { u64::MAX } else { (1 << (8 * width)) - 1 }));
    }
    let offset = gpa & 0xfff;
    if width != 4 || offset & 0xf != 0 {
        return Some(Err(()));
    }
    let reg = offset >> 4;
    Some(Ok(match data {
        Some(v) => {
            write_xapic(s, reg, v);
            0
        }
        None => read(s, reg, false).unwrap_or_else(|| {
            error(s, apic::ILLEGAL_REGISTER);
            0
        }),
    }))
}
/// A register's value (register numbers are xAPIC offset / 16 = x2APIC MSR - 0x800), `None` for an index the APIC does
/// not implement or a write-only one (x2APIC: #GP; xAPIC: an illegal register access, read as 0).
fn read(s: &State, reg: u64, x2apic: bool) -> Option<u64> {
    let l = lapic();
    let bits32 = |b: &[u64; 4], k: u64| b[k as usize / 2] >> (32 * (k % 2)) & 0xffff_ffff;
    Some(match reg {
        0x2 if x2apic => s.vcpu,
        0x2 => s.vcpu << 24,
        0x3 => apic::VERSION,
        0x8 => s.cpu.cr8 & 0xff,
        // APR and RRD: not implemented on this processor generation (Table 11-1 note 1); xAPIC reads 0.
        0x9 | 0xc if !x2apic => 0,
        0xa => apic::ppr(s.cpu.cr8, &l.isr),
        0xb if !x2apic => 0,
        0xd if x2apic => apic::x2apic_ldr(s.vcpu),
        0xd => l.ldr,
        0xe if !x2apic => l.dfr,
        0xf => l.svr,
        0x10..=0x17 => bits32(&l.isr, reg - 0x10),
        0x18..=0x1f => tmr_word(s.vcpu, (reg - 0x18) as usize / 2).load(SeqCst) >> (32 * (reg % 2)) & 0xffff_ffff,
        0x20..=0x27 => {
            let w = (reg - 0x20) as usize / 2;
            irr_word(s.vcpu, w).load(SeqCst) >> (32 * (reg % 2)) & 0xffff_ffff
        }
        0x28 => l.esr,
        0x30 if x2apic => l.icr & (apic::ICR_WRITABLE | 0xffff_ffff << 32),
        0x30 => l.icr & apic::ICR_WRITABLE,
        0x31 if !x2apic => l.icr >> 32 & 0xff00_0000,
        0x32..=0x37 => l.lvt[(reg - 0x32) as usize],
        0x38 => l.initial,
        0x39 => current_count(l),
        0x3e => l.dcr,
        _ => return None,
    })
}
/// An xAPIC register write (32 bits): read-only registers ignore it; unimplemented offsets are an illegal register
/// access (ESR bit 7), except APR and RRD.
fn write_xapic(s: &mut State, reg: u64, v: u64) {
    let l = lapic();
    match reg {
        // ID (read-only here: model specific, 11.4.6), version, APR, PPR, RRD, ISR, TMR, IRR, current count.
        0x2 | 0x3 | 0x9 | 0xa | 0xc | 0x10..=0x27 | 0x39 => {}
        0x8 => s.cpu.cr8 = v & 0xff,
        0xb => eoi(s),
        0xd => {
            l.ldr = v & 0xff00_0000;
            publish(s);
        }
        0xe => {
            l.dfr = v & 0xf000_0000 | 0x0fff_ffff;
            publish(s);
        }
        0x31 => l.icr = l.icr & 0xffff_ffff | (v & 0xff00_0000) << 32,
        0x30 => {
            l.icr = l.icr & !0xffff_ffff | v & apic::ICR_WRITABLE;
            let i = l.icr;
            send(s, i, false);
        }
        _ => {
            if !write_common(s, reg, v) {
                error(s, apic::ILLEGAL_REGISTER)
            }
        }
    }
}
/// The registers both modes write alike (`v` already checked for x2APIC's reserved bits); false: not one of them.
fn write_common(s: &mut State, reg: u64, v: u64) -> bool {
    let l = lapic();
    match reg {
        0xf => {
            timer(s); // expiries due under the old LVT state first
            let l = lapic();
            l.svr = v & apic::SVR_WRITABLE;
            // Software disabled: every LVT entry masked, and kept masked (11.4.7.2).
            if l.svr & apic::SVR_ENABLE == 0 {
                l.lvt.iter_mut().for_each(|e| *e |= apic::MASKED);
            }
            publish(s);
        }
        0x28 => (l.esr, l.errors) = (l.errors, 0),
        0x32..=0x37 => {
            let i = (reg - 0x32) as usize;
            if i == apic::TIMER {
                timer(s); // expiries due under the old mode and mask first (a masked LVT drops them, 11.5.1)
            }
            let l = lapic();
            let masked = if l.svr & apic::SVR_ENABLE != 0 { 0 } else { apic::MASKED };
            let new = v & apic::LVT_WRITABLE[i] | masked;
            if i == apic::TIMER && (new ^ l.lvt[i]) & apic::PERIODIC != 0 && l.spent == 0 {
                // A mode change counts no expiry that passed under the old mode (a one-shot already past 0 is spent).
                let periodic = new & apic::PERIODIC != 0;
                l.fired = apic::expiries(l.initial, periodic, ticks(l));
                l.spent = u64::from(!periodic && l.fired != 0);
            }
            l.lvt[i] = new;
            if i == apic::TIMER {
                timer(s);
            }
            if i == apic::LINT0 || i == apic::LINT1 {
                publish(s); // L4a: whether it takes an ExtINT, an NMI at LINT1
            }
        }
        0x38 => {
            (l.initial, l.start, l.fired, l.spent) = (v & 0xffff_ffff, clock::crystal(now(), l.frequency), 0, 0);
            timer(s);
        }
        0x3e => {
            // The count stays where it is and counts on at the new rate: the expiries due first, then the start moved
            // so the elapsed ticks within the period are the same at the new divider.
            timer(s);
            let l = lapic();
            let dcr = v & apic::DCR_WRITABLE;
            if l.initial != 0 && l.spent == 0 {
                let t = ticks(l);
                let phase = if l.lvt[apic::TIMER] & apic::PERIODIC != 0 { t % l.initial } else { t };
                l.start = clock::crystal(now(), l.frequency).saturating_sub(phase * apic::divisor(dcr));
                l.fired = 0;
            }
            l.dcr = dcr;
            timer(s);
        }
        _ => return false,
    }
    true
}

/// RDMSR of an x2APIC register (`index` in 0x800..=0x8ff).
pub(super) fn rdmsr(s: &State, index: u32) -> Result<u64, Exception> {
    if base() & (EN | EXTD) != EN | EXTD {
        return Err(GP);
    }
    read(s, u64::from(index) - 0x800, true).ok_or(GP)
}
/// WRMSR of an x2APIC register.
pub(super) fn wrmsr(s: &mut State, index: u32, v: u64) -> Result<(), Exception> {
    let reg = u64::from(index) - 0x800;
    let reserved = match reg {
        0x8 => !0xff,
        0xb | 0x28 => !0,
        0xf => !apic::SVR_WRITABLE,
        0x30 => !(apic::ICR_WRITABLE | 0xffff_ffff << 32),
        0x32..=0x37 => !apic::LVT_DEFINED[(reg - 0x32) as usize],
        0x38 => !0xffff_ffff,
        0x3e => !apic::DCR_WRITABLE,
        0x3f => !0xff,
        _ => return Err(GP), // read-only, not implemented, or (0x80e DFR, 0x80d LDR, 0x831) absent in x2APIC mode
    };
    if base() & (EN | EXTD) != EN | EXTD || v & reserved != 0 {
        return Err(GP);
    }
    match reg {
        0x8 => s.cpu.cr8 = v,
        0xb => eoi(s),
        0x30 => {
            lapic().icr = v;
            send(s, v, true);
        }
        0x3f if v < 16 => error(s, apic::SEND_ILLEGAL),
        0x3f => raise(s, v),
        _ => {
            write_common(s, reg, v);
        }
    }
    Ok(())
}
/// WRMSR IA32_APIC_BASE of `new` (the profile's checks passed): the 11.12.5 transitions, then the APIC follows.
pub(super) fn set_base(s: &mut State, new: u64) -> Result<(), Exception> {
    let (from, to) = (base() & (EN | EXTD), new & (EN | EXTD));
    if (from == EN | EXTD && to == EN) || (from == 0 && to == EN | EXTD) {
        return Err(GP);
    }
    if to == EN && mmio::ram(new & BASE) {
        halt(s, stop::UNSUPPORTED_SYSTEM)
    }
    let moved = (new ^ base()) & BASE != 0;
    msr_set(base_slot(), new);
    // L3 (amendment): the new page may hold an all-ones shadow (it was unassigned for this vCPU): drop this vCPU's shadows,
    // so its next access finds the APIC. Shadows are per vCPU, as the APIC page is; the old page simply becomes unassigned.
    if moved {
        super::reset_shadows(s);
    }
    if from != 0 && to == 0 {
        // Entering the disabled state resets the APIC (11.12.5.1); the TPR is the bank's. The disabled word is published
        // first, so senders stop posting before the IRR is cleared; one that read the old word may still post after the
        // clear, and that bit is cleared again when the APIC is enabled (nothing is accepted while disabled).
        publish(s);
        let l = lapic();
        *l = Lapic { frequency: l.frequency, reinject: l.reinject, missed: l.missed, reinjected: l.reinjected, ..apic::RESET };
        (0..4).for_each(|w| {
            irr_word(s.vcpu, w).store(0, SeqCst);
            tmr_word(s.vcpu, w).store(0, SeqCst);
        });
        s.cpu.cr8 = 0;
        cntv(None);
    }
    if from == 0 && to != 0 {
        // A post that raced the disable (above).
        (0..4).for_each(|w| {
            irr_word(s.vcpu, w).store(0, SeqCst);
            tmr_word(s.vcpu, w).store(0, SeqCst);
        });
    }
    publish(s);
    Ok(())
}

/// Sends the IPI `icr` (ICR low in bits 31:0, the destination in 63:56 in xAPIC mode, 63:32 in x2APIC mode).
fn send(s: &mut State, icr: u64, x2apic: bool) {
    let (vector, mode, me) = (icr & 0xff, icr::mode(icr), s.vcpu);
    let all = (1u64 << s.vcpus) - 1;
    let dest = if x2apic { icr >> 32 } else { icr >> 56 };
    let targets = match icr::shorthand(icr) {
        icr::SELF => 1 << me,
        icr::ALL => all,
        icr::OTHERS => all & !(1 << me),
        _ => (0..s.vcpus).filter(|&t| apic::in_destination(word_of(t), t, dest, icr & icr::LOGICAL != 0, x2apic)).fold(0, |m, t| m | 1 << t),
    };
    // Counted after the bit is set, before the doorbell (`Events::posts`: the host's rule for ending halted vCPUs).
    let ring = |t: u64| {
        event_word(t, core::mem::offset_of!(Events, posts)).fetch_add(1, SeqCst);
        if t != me {
            cache::kick(t as usize)
        }
    };
    match mode {
        icr::FIXED | icr::LOWEST => {
            if vector < 16 {
                return error(s, apic::SEND_ILLEGAL);
            }
            let accepting = (0..s.vcpus).filter(|&t| targets >> t & 1 != 0 && word_of(t) & apic::SW_ENABLED != 0);
            // Lowest priority: the lowest-numbered member of the set (the arbitration is model specific, 11.6.2.4).
            for t in accepting.take(if mode == icr::LOWEST { 1 } else { 64 }) {
                accept_edge(t, vector);
                ring(t);
            }
        }
        icr::NMI => {
            for t in (0..s.vcpus).filter(|&t| targets >> t & 1 != 0 && word_of(t) & apic::HW_ENABLED != 0) {
                event_word(t, core::mem::offset_of!(Events, nmi)).store(1, SeqCst);
                ring(t);
            }
        }
        // INIT level de-assert (level 0, trigger level): no effect on this processor generation (11.6.1).
        icr::INIT if icr & (icr::ASSERT | icr::LEVEL) == icr::LEVEL => {}
        icr::INIT | icr::STARTUP => ap_start(s),
        _ => halt(s, stop::UNSUPPORTED_SYSTEM),
    }
}
/// INIT and start-up IPIs: a typed stop at the sending instruction, nothing sent. X4 decided to keep it: APs start
/// through the ACPI wakeup mailbox ([`super::events::wait_mailbox`]), which Linux prefers whenever the MADT has one
/// (`do_boot_cpu`); INIT-SIPI-SIPI would start the target in real mode at vector << 12 (SDM Vol.3 9.4.4), and no 16- or
/// 32-bit execution exists, so it is deferred with them (an INIT alone, the wait-for-SIPI state, is deferred with it).
fn ap_start(s: &State) -> ! {
    halt(s, stop::UNSUPPORTED_SYSTEM)
}
