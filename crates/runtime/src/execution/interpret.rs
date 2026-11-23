//! M2 Part 2: the boot-time interpreter in the resident (docs/restructure/m2-boot-interpreter-plan.md sections 3-6;
//! docs/restructure/interp-integration.md). A vCPU reaches a state only the interpreter runs ([`Cpu::interpreted_mode`]:
//! real mode, protected mode outside IA-32e mode, compatibility mode at CPL 0) only at a cold point: the end of a system
//! transaction (its `commit`, or `set_cpl` inside it), where the resident halted before, records it ([`record`]:
//! [`debug::INTERPRET`] in `State::debug`), and the next boundary enters the interpreter once the transaction has completed
//! ([`boundary`], from `find_from`'s loop, before a trap-class #DB). Every path out of a system transaction reaches that
//! loop, and none needs a test of its own: the fast system-call path declines when `State::debug` is nonzero, and the
//! loop's first look at the debug state is at its trap bits, which hold the record. The vCPU's start enters directly
//! ([`run`]). [`run`] returns to that loop when the state is one the translator runs again (no cycle through `dispatch`).
//!
//! [`run`] executes one instruction per pass, after `find`'s boundary prologue: the record at CS.base + EIP (the host's
//! decode, `natemu_abi::interp`, cached by linear address and [`interpreted_key`] and validated by its bytes at every
//! execution, [`cache::place_interpreted`]) through the abi's executor core (`natemu_abi::interp::exec`) over [`Mem`], the
//! memory layers; CPUID, RDMSR, WRMSR and WBINVD through the system transactions, LGDT, LIDT and MOV from CR here, and
//! the protected-mode far transfers and segment loads, MOV to CR0/CR3/CR4 with the mode transitions and HLT in
//! [`transitions`] (docs/restructure/interp-transitions.md). A far transfer that loads a 64-bit CS with IA-32e mode active
//! leaves interpreted state: [`run`] then returns to the translator. What it does not run is a typed stop
//! (`stop::INTERP_FAULT` ..), precise, with nothing committed: exceptions (interpreted modes deliver none), events, TF and
//! breakpoints, device pages, the forms outside the first version, gates, task switches and privilege changes, legacy and
//! PAE paging.
use super::*;
use core::ptr::{addr_of, addr_of_mut};
use natemu_abi::execution::stop::interp as detail;
use natemu_abi::interp::{
    DECODE_OFFSET, DECODED_OFFSET, Decode, Decoded, Insn, MAX_FETCH, MAX_RUN, REPLY_DECODE,
    REQUEST_DECODE,
    exec::{self, Exit, Memory, Outcome, Resident},
    op,
};

mod transitions;

/// [`ends_in_mode`]'s arms where [`controls`] halts with `reason`: a state the interpreter runs is recorded for the next
/// boundary; any other halts as [`controls`] does (`unsupported_mode` for the paging mode or CPL, `unsupported_code_mode`
/// for the code segment), at the instruction.
///
/// In compatibility mode, RIP above the CS limit halts `unsupported_code_mode` with `native[0]` =
/// `stop::code_mode::PAST_LIMIT`: only a far JMP, CALL or RET of 64-bit kernel code ([`kernel_compatibility`]'s) gets
/// here that way, because that CPL-0 route checks only that the target is canonical,
/// where x86 raises #GP(0) at the instruction when it lies outside the new CS limit (a RIP at or above 4 GiB among
/// them). The transfer has completed (`commit`, before it cuts RIP to 32 bits): the recorded deviation, a loud stop in
/// place of the code running at the wrong address. Any other transaction ending in compatibility mode is at an
/// instruction the interpreter fetched, within the limit.
#[cold]
#[inline(never)]
pub(super) fn record(s: &mut State, reason: u64) {
    if s.cpu.interpreted_mode().is_some() {
        let limit = u64::from(s.cpu.seg[SREG_CS].limit);
        if s.cpu.efer & EFER_LMA != 0 && s.cpu.rip > limit {
            s.native = [stop::code_mode::PAST_LIMIT, limit, s.cpu.rip];
            halt(s, stop::UNSUPPORTED_CODE_MODE)
        }
        s.debug |= debug::INTERPRET;
    } else {
        halt(s, reason)
    }
}

/// The boundary after a transaction [`record`]ed a state the interpreter runs (`trap_due`'s first check, which it ends):
/// clears the record and runs the interpreter; true, so `find_from` looks at the boundary again (a state that is no longer
/// one, the transaction having restored the bank after it recorded, then goes on as any other).
#[cold]
#[inline(never)]
pub(super) fn boundary(s: &mut State) -> bool {
    s.debug &= !debug::INTERPRET;
    if s.cpu.interpreted_mode().is_some() {
        run(s);
    }
    true
}

/// `segments::target_mode`'s CPL-0 arm (plan section 5, B4): a far JMP, CALL or RET of 64-bit kernel code whose
/// target is not 64-bit code, checked before any effect. A compatibility-mode code segment (L = 0) at CPL 0 is the
/// interpreter's: this returns, the transfer completes in `far_transfer`/`far_return`, and its commit records the state
/// for the next boundary ([`record`], through `ends_in_mode`). Anything else halts `unsupported_code_mode` as the arm did
/// (the record naming the CS it would have loaded): L = D = 1 (reserved in IA-32e mode), a return to CPL 1 or 2, and any
/// IRET (an IRET into compatibility mode may load a null SS, which x86 refuses with #GP(0) and the 64-bit IRET does not
/// check: no known code makes one). Out of line and cold, so the 64-bit far transfers keep only a branch in that arm: it
/// returns `refuse_compatibility`'s result (always `Ok`), so the call there is a tail call and that function's common
/// path needs no frame.
#[cold]
#[inline(never)]
pub(super) fn kernel_compatibility(s: &State, selector: u16, raw: u64, cpl: u64) -> Result<(), segments::Exception> {
    let mut t = *s;
    if cpl != 0 || raw >> 53 & 1 != 0 || iret_at(&mut t) {
        t.cpu.seg[SREG_CS] = Segment::loaded(selector, raw);
        halt(&t, stop::UNSUPPORTED_CODE_MODE)
    }
    Ok(())
}
/// Whether the instruction at RIP (64-bit code at CPL 0, the transaction's: its fetch has succeeded) is an IRET: CFH after
/// its prefixes (legacy and REX). A byte that cannot be read is taken as one (the halt).
fn iret_at(t: &mut State) -> bool {
    for k in 0..15 {
        let va = t.cpu.rip.wrapping_add(k);
        let Some(p) = fetch_walk(t, va).ok().and_then(|(page, _)| guest_ram().range(page | va & 4095, 1)) else {
            return true;
        };
        // SAFETY: a guest RAM byte in the resident's alias.
        match unsafe { p.read_volatile() } {
            0x26 | 0x2e | 0x36 | 0x3e | 0x64..=0x67 | 0xf0 | 0xf2 | 0xf3 | 0x40..=0x4f => {}
            opcode => return opcode == 0xcf,
        }
    }
    true
}

/// A typed stop: its code (`stop::INTERP_FAULT` ..), its detail (`State::native[0]` bits 31:24) and the first three words
/// of `State::fault` (the fourth is RIP).
#[derive(Clone, Copy)]
struct Stop {
    code: u64,
    detail: u64,
    fault: [u64; 3],
}
impl Stop {
    const fn new(code: u64, detail: u64, fault: [u64; 3]) -> Self {
        Self { code, detail, fault }
    }
    /// An exception the instruction raises: its vector, error code and (#PF) CR2.
    const fn exception(vector: u64, error: u64, cr2: u64) -> Self {
        Self::new(stop::INTERP_FAULT, 0, [vector, cr2, error])
    }
    /// A memory stop (`detail::DEVICE` ..) at linear `va` on guest-physical page `page`.
    const fn memory(detail: u64, va: u64, page: u64) -> Self {
        Self::new(stop::INTERP_MEMORY, detail, [0, va, page])
    }
    const fn unsupported(detail: u64) -> Self {
        Self::new(stop::INTERP_UNSUPPORTED, detail, [0; 3])
    }
}
const GP0: Stop = Stop::exception(13, 0, 0);
const UD: Stop = Stop::exception(6, 0, 0);

/// What a typed stop names of the instruction: its operation (0xff: none decoded) and its bytes as far as they are known
/// (the record's, or those fetched).
struct At {
    op: u8,
    count: u8,
    bytes: [u8; 16],
}
impl At {
    const NONE: Self = Self {
        op: 0xff,
        count: 0,
        bytes: [0; 16],
    };
}

/// The interpreter from the bank's state ([`Cpu::interpreted_mode`]), one instruction per pass, returning once the state
/// is one the translator runs. Each pass starts with `find`'s boundary prologue in its order ([`find_from`]); a pass that
/// cannot run its instruction stops the vCPU ([`stopped`]).
pub(super) fn run(s: &mut State) {
    // (Tests only: the stop made at the interpreter's entry before it ran, `Control::interpreter_off`.)
    if cache::control().interpreter_off != 0 {
        halt(s, if s.cpu.paging().is_none() { stop::UNSUPPORTED_MODE } else { stop::UNSUPPORTED_CODE_MODE })
    }
    loop {
        s.cpu.rip &= 0xffff_ffff;
        io_checkpoint(s);
        take_invalidations(s);
        lapic::poll(s);
        if s.cpu.interpreted_mode().is_none() {
            return;
        }
        let mut at = At::NONE;
        if let Err(why) = pass(s, &mut at) {
            stopped(s, &at, why)
        }
    }
}

/// One instruction at a boundary: the debug state, the events and the mode first (each a typed stop when it asks for
/// what the interpreter does not do), then the record at CS.base + EIP and its execution.
fn pass(s: &mut State, at: &mut At) -> Result<(), Stop> {
    debugging(s)?;
    events(s)?;
    let paging = walks(s)?;
    let Some(key) = interpreted_key(&s.cpu) else {
        halt(s, stop::INTERP_BUG)
    };
    let linear = (s.cpu.seg[SREG_CS].base as u32).wrapping_add(s.cpu.rip as u32);
    let i = match cache::interpreted_at(u64::from(linear), key).map(|b| cache::interpreted_of(b)) {
        Some(i) if current(s, paging, &i) => i,
        _ => decode(s, paging, linear, key, at)?,
    };
    (at.op, at.count, at.bytes) = (i.op, i.length, i.bytes);
    execute(s, paging, &i)?;
    // The interpreter sets neither shadow and honours none (no event is delivered here, plan section 5); one it found
    // (a start state's) ends with the instruction, as a shadow does.
    (s.cpu.mov_ss, s.sti_shadow) = (0, 0);
    Ok(())
}

/// A pending trap-class #DB, RFLAGS.TF or an armed breakpoint (DR7): the interpreter delivers no #DB and checks no
/// breakpoint, so each is a typed stop (plan section 5). (Not [`debug::INTERPRET`], which [`boundary`] cleared.) A pending
/// INIT comes first (SDM Vol.3 Table 7-2: class 3, before traps and breakpoints, as `find`'s `trap_due` leaves it):
/// [`events`] takes it, and its state has neither TF nor a breakpoint.
fn debugging(s: &State) -> Result<(), Stop> {
    if startup::init_pending(s) {
        return Ok(());
    }
    let trap = s.debug & debug::TRAP & !debug::INTERPRET;
    let (d, value) = if trap != 0 {
        (detail::TRAP, trap)
    } else if s.cpu.rflags & RFLAGS_TF != 0 {
        (detail::TF, 0)
    } else if s.cpu.dr[5] & DR7_ENABLES != 0 {
        (detail::BREAKPOINT, s.cpu.dr[5])
    } else {
        return Ok(());
    };
    Err(Stop::new(stop::INTERP_DEBUG, d, [value, 0, 0]))
}

/// The events this boundary would take: the one place the interpreter reads them (`find` takes them with
/// `events::take`), through the CPU start-up's event point ([`events::interpreted`]): it takes a pending INIT itself (the
/// bank is then the INIT or SIPI state, which the rest of the pass reads afresh) and names any other event, which no
/// interpreted mode delivers (plan section 6): a typed stop ([`event_stop`]), the event left pending.
fn events(s: &mut State) -> Result<(), Stop> {
    match events::interpreted(s) {
        None => Ok(()),
        Some(e) => Err(event_stop(e)),
    }
}
/// The typed stop for an event an interpreted mode does not deliver: its kind (`stop::interp::NMI` ..) and vector (2 for
/// an NMI; an ExtINT's comes from the PIC only when it is taken: 0).
fn event_stop(e: events::Undelivered) -> Stop {
    let (kind, vector) = match e {
        events::Undelivered::Nmi => (detail::NMI, 2),
        events::Undelivered::Interrupt(v) => (detail::INTERRUPT, v),
        events::Undelivered::ExtInt => (detail::EXTINT, 0),
    };
    Stop::new(stop::INTERP_EVENT, kind, [vector, 0, 0])
}

/// The walk controls of the state's paging (`None`: paging off, the identity), or the typed stop of a state the
/// interpreter does not run: protected mode at CPL > 0, paging outside IA-32e mode (legacy and PAE paging), or paging
/// controls the walker has none for ([`Cpu::paging`]).
fn walks(s: &State) -> Result<Option<paging::Control>, Stop> {
    let c = &s.cpu;
    let refused = Err(Stop::new(stop::INTERP_MODE, 0, [0; 3]));
    if c.cr0 & CR0_PE != 0 && c.cpl != 0 {
        return refused;
    }
    if c.cr0 & CR0_PG == 0 {
        return Ok(None);
    }
    match c.paging() {
        Some(p) if c.efer & EFER_LMA != 0 => Ok(Some(p)),
        _ => refused,
    }
}

/// The page walk of `va` for `access` at CPL 0 (the interpreter's only CPL with paging): A and D set as the CPU sets them,
/// a walk that lost a race to a peer made again; a #PF with its error code. No block of this vCPU runs, so an A or D write
/// into a page holding translations just invalidates them ([`FetchWrites`]).
fn walk(
    s: &mut State,
    c: paging::Control,
    va: u64,
    access: Access,
) -> Result<paging::Translation, (u64, u64)> {
    loop {
        match paging::walk(
            &mut GuestTables::observed(guest_ram(), CodeWrites(FetchWrites(s))),
            c,
            va,
            access,
            cache::control().speculation_barriers != 0,
        ) {
            Ok(t) => return Ok(t),
            Err(Fault::Page { address, error }) => return Err((error, address)),
            Err(Fault::Contended) => {}
            Err(_) => halt(s, stop::UNSUPPORTED_MODE),
        }
    }
}

/// The guest-physical page linear `va` is fetched from (paging off: its own, wrapping at 4 GiB; else a fetch walk), when
/// it is guest RAM or the firmware image (docs/restructure/firmware-mode.md: the reset vector's code).
fn fetch_page(s: &mut State, paging: Option<paging::Control>, va: u64) -> Result<u64, Stop> {
    let gpa = match paging {
        None => paging::unpaged(va, 1).0,
        Some(c) => walk(s, c, va, Access::Fetch)
            .map_err(|(error, cr2)| Stop::exception(14, error, cr2))?
            .gpa,
    } & !4095;
    match ram_and_rom().range(gpa, 4096) {
        Some(_) => Ok(gpa),
        None => Err(Stop::memory(detail::FETCH, va, gpa)),
    }
}

/// Fetches `out.len()` bytes of the instruction stream from EIP + `from` (plan section 4): each inside the CS limit
/// (#GP(0) past it; CS's execute right is its load's to check), through the current translation, from guest RAM or the
/// firmware image, one page
/// at a time (a page's walk only when a byte of it is wanted). Short: how many bytes came, and the stop the first one not
/// fetched raises.
fn fetch(
    s: &mut State,
    paging: Option<paging::Control>,
    from: usize,
    out: &mut [u8],
) -> Result<(), (usize, Stop)> {
    let cs = s.cpu.seg[SREG_CS];
    let (eip, limit) = (s.cpu.rip as u32, u64::from(cs.limit));
    let mut done = 0;
    while done < out.len() {
        let offset = u64::from(eip) + (from + done) as u64;
        if offset > limit {
            return Err((done, GP0));
        }
        let linear = (cs.base as u32).wrapping_add(offset as u32);
        let n = (out.len() - done)
            .min(4096 - (linear & 4095) as usize)
            .min((limit - offset + 1).min(4096) as usize);
        let page = fetch_page(s, paging, u64::from(linear)).map_err(|why| (done, why))?;
        let Some(p) = ram_and_rom().range(page | u64::from(linear & 4095), n as u64) else {
            halt(s, stop::INTERP_BUG)
        };
        // SAFETY: `p` is `n` bytes of guest RAM or the image in the resident's alias; `out` holds them.
        unsafe { guest_memory::copy_volatile(out[done..].as_mut_ptr(), p, n) };
        done += n;
    }
    Ok(())
}

/// Whether the cached record `i` holds the bytes at EIP now (byte validation, plan section 5: fetched as for its decode,
/// through the current translation and inside the CS limit): equivalent to fetching every time. A fetch that fails is a
/// no: the decode raises what it raises.
fn current(s: &mut State, paging: Option<paging::Control>, i: &Insn) -> bool {
    let mut bytes = [0u8; 16];
    let n = usize::from(i.length);
    fetch(s, paging, 0, &mut bytes[..n]).is_ok() && bytes[..n] == i.bytes[..n]
}

/// The request's bytes in the staging area (`Decode::bytes`).
fn request_bytes() -> *mut u8 {
    // SAFETY: the staging area is this vCPU's, EL1 read/write; only an address is formed.
    unsafe { addr_of_mut!((*((STAGING_VA + DECODE_OFFSET as u64) as *mut Decode)).bytes).cast() }
}

/// The host's decode at EIP (`REQUEST_DECODE`, the contract in `natemu_abi::interp`): the bytes to the end of RIP's page
/// (at most [`MAX_FETCH`]; short where a byte cannot be fetched, that byte's stop kept), and only when the first
/// instruction needs more, its bytes on the next page, up to 15 in all: a fault there, or past the CS limit, is raised
/// only when the instruction reaches it. The host answers a stop at CS.base + EIP first (its `Hook::at`), and ends the run
/// before the next one, so no record is ever cached at a stop. Each record is checked against the bytes sent and cached
/// but stop records; the first is returned, its bytes the snapshot just fetched (not validated again: no livelock under a
/// peer rewriting them).
fn decode(
    s: &mut State,
    paging: Option<paging::Control>,
    linear: u32,
    key: u64,
    at: &mut At,
) -> Result<Insn, Stop> {
    let bytes = request_bytes();
    // (Nothing is fetched at the host's stop RIP, as at a translation request: the host ends the run there.)
    let first = if u64::from(linear) == s.stop_at { 0 } else { (4096 - (linear & 4095) as usize).min(MAX_FETCH) };
    // SAFETY: `first` <= MAX_FETCH bytes of the request's buffer.
    let (mut available, mut short) = match fetch(s, paging, 0, unsafe {
        core::slice::from_raw_parts_mut(bytes, first)
    }) {
        Ok(()) => (first, None),
        Err((n, why)) => (n, Some(why)),
    };
    let mut extended = false;
    let count = loop {
        let n = available.min(15);
        // SAFETY: `n` bytes of the request's buffer, written above.
        unsafe { core::ptr::copy_nonoverlapping(bytes, at.bytes.as_mut_ptr(), n) };
        at.count = n as u8;
        let count = request(s, available);
        if count != 0 {
            break count;
        }
        // Truncated: the first instruction needs a byte past `available`.
        if let Some(why) = short {
            return Err(why);
        }
        if extended || available >= 15 {
            halt(s, stop::BAD_REPLY)
        }
        extended = true;
        // SAFETY: bytes `available..15` of the request's buffer.
        let rest = unsafe { core::slice::from_raw_parts_mut(bytes.add(available), 15 - available) };
        match fetch(s, paging, available, rest) {
            Ok(()) => available = 15,
            Err((n, why)) => (available, short) = (available + n, Some(why)),
        }
    };
    let reply = (STAGING_VA + DECODED_OFFSET as u64) as *const Decoded;
    let (mut offset, mut first_record, mut flushed) = (0, Insn::NONE, false);
    for k in 0..count {
        // SAFETY: record `k` < MAX_RUN of the reply, in the staging area, read in place.
        let i = unsafe { addr_of!((*reply).records[k]).read_volatile() };
        let n = usize::from(i.length);
        // SAFETY: `available` bytes of the request's buffer.
        let sent = unsafe { core::slice::from_raw_parts(bytes, available) };
        if !i.well_formed()
            || offset + n > available
            || i.bytes[..n] != sent[offset..offset + n]
            || i.bytes[n..].iter().any(|&b| b != 0)
        {
            halt(s, stop::BAD_REPLY)
        }
        if k == 0 {
            first_record = i;
        }
        // Stop records end the run: never cached.
        if i.op < op::UD {
            let va = u64::from(linear.wrapping_add(offset as u32));
            while cache::place_interpreted(va, key, &i).is_none() {
                if flushed {
                    halt(s, stop::BAD_REPLY)
                }
                cache::control().capacity_flushes += 1;
                flush(s);
                flushed = true;
            }
        }
        offset += n;
    }
    Ok(first_record)
}

/// Asks the host to decode the first `available` bytes of the request's buffer (16- or 32-bit code: CS.D) into at most
/// [`MAX_RUN`] records: the count it replied with, the records in place. A stop the host's hook answers parks the vCPU.
fn request(s: &mut State, available: usize) -> usize {
    let q = (STAGING_VA + DECODE_OFFSET as u64) as *mut Decode;
    let bits = if s.cpu.seg[SREG_CS].attributes & ATTR_D != 0 { 32 } else { 16 };
    // SAFETY: the request's fields in the staging area; the host reads them while this vCPU waits in `hvc`.
    unsafe {
        addr_of_mut!((*q).bits).write_volatile(bits);
        addr_of_mut!((*q).max).write_volatile(MAX_RUN as u32);
        addr_of_mut!((*q).available).write_volatile(available as u32);
        s.request = REQUEST_DECODE;
        s.reply = NONE;
        write_record(super::record(), s);
        let p = crate::perf::enter(crate::perf::HOST);
        asm!("dsb sy", "hvc #{imm}", imm = const TRANSLATE_HVC);
        crate::perf::leave(p);
        *s = super::load();
    }
    s.request = NONE;
    match s.reply {
        REPLY_DECODE => {}
        REPLY_PARK => halt(s, stop::PARKED),
        _ => halt(s, stop::BAD_REPLY),
    }
    // SAFETY: the reply's count, in the staging area.
    let count = unsafe {
        addr_of!((*((STAGING_VA + DECODED_OFFSET as u64) as *const Decoded)).count).read_volatile()
    } as usize;
    if count > MAX_RUN {
        halt(s, stop::BAD_REPLY)
    }
    count
}

/// Executes `i` at EIP: the core on a copy of the bank (the memory layer borrows the record), written back once it
/// committed; a resident operation; or the typed stop of what stopped it, nothing committed.
fn execute(s: &mut State, paging: Option<paging::Control>, i: &Insn) -> Result<(), Stop> {
    let ctx = exec::Context::of(&s.cpu);
    let mut cpu = s.cpu;
    let mut m = Mem {
        s,
        paging,
        pages: [[0; 2]; 4],
        count: 0,
        stopped: [0; 2],
    };
    let done = exec::step(&mut cpu, ctx, i, &mut m);
    let stopped = m.stopped;
    match done {
        Ok(Outcome::Done) => {
            s.cpu = cpu;
            s.cpu.rflags &= !RFLAGS_RF; // the instruction completed
            Ok(())
        }
        Ok(Outcome::Resident(r)) => resident(s, paging, i, r),
        Err(e) => Err(exited(e, stopped)),
    }
}
/// The typed stop of a core or memory exit; `stopped` is the memory layer's [`Mem::stopped`] (a memory stop's address).
fn exited(e: Exit, stopped: [u64; 2]) -> Stop {
    match e {
        Exit::Fault {
            vector,
            error,
            address,
        } => Stop::exception(u64::from(vector), u64::from(error), u64::from(address)),
        Exit::Unsupported => Stop::unsupported(0),
        Exit::Stop(d) => Stop::memory(u64::from(d), stopped[0], stopped[1]),
    }
}

/// A resident operation the core returned with its operands read and checked (CPL 0: [`walks`]). CPUID, RDMSR, WRMSR and
/// WBINVD through the system transaction (its commit moves EIP past the instruction, wrapping at 32 bits as the core
/// does, and records the interpreted state, [`ends_in_mode`], which goes: this is the interpreter); an exception it raises
/// is a typed stop, nothing committed. Far transfers and segment loads in protected mode, MOV to CR and HLT: the
/// interpreter's own ([`transitions`]).
fn resident(s: &mut State, paging: Option<paging::Control>, i: &Insn, r: Resident) -> Result<(), Stop> {
    let kind = match r {
        Resident::LoadTable { idt, base, limit } => {
            let table = Table {
                base: u64::from(base),
                limit: u64::from(limit),
            };
            *if idt { &mut s.cpu.idtr } else { &mut s.cpu.gdtr } = table;
            return Ok(complete(s, i));
        }
        // CR8 is 64-bit mode's; the other numbers are #UD (SDM Vol.2 MOV CR). Upper bits of a 64-bit CR3: gone at 32 bits.
        Resident::MovFromCr { cr, reg } => {
            let v = match cr {
                0 => s.cpu.cr0,
                2 => s.cpu.cr2,
                3 => s.cpu.cr3,
                4 => s.cpu.cr4,
                _ => return Err(UD),
            };
            s.cpu.gpr[usize::from(reg & 7)] = v & 0xffff_ffff;
            return Ok(complete(s, i));
        }
        Resident::Cpuid => system::CPUID,
        Resident::Rdmsr => system::RDMSR,
        Resident::Wrmsr => system::WRMSR,
        Resident::Wbinvd => system::WBINVD,
        Resident::FarJump { selector, offset } => {
            return transitions::far_jump(s, paging, selector, offset);
        }
        Resident::FarReturn {
            selector,
            offset,
            release,
        } => return transitions::far_return(s, paging, selector, offset, release),
        Resident::LoadSegment { sreg, selector } => {
            return transitions::load_segment(s, paging, i, sreg, selector);
        }
        Resident::MovToCr { cr, value } => return transitions::mov_to_cr(s, i, cr, value),
        Resident::Hlt => return Ok(transitions::hlt(s, i)),
    };
    let done = transaction(
        s,
        System {
            kind,
            length: u32::from(i.length),
            ..System::NONE
        },
    );
    s.debug &= !debug::INTERPRET;
    done.map_err(|[vector, error, cr2]| Stop::exception(vector, error, cr2))
}
/// A resident operation's completion: EIP past it (wrapping at 32 bits), RF cleared.
fn complete(s: &mut State, i: &Insn) {
    exec::complete(&mut s.cpu, i);
    s.cpu.rflags &= !RFLAGS_RF;
}

/// Stops the vCPU with the typed stop `why` at the instruction `at` names, nothing of it committed: the record of
/// `stop::INTERP_FAULT`'s documentation (`State::native`, `State::fault`).
#[cold]
#[inline(never)]
fn stopped(s: &mut State, at: &At, why: Stop) -> ! {
    let mode = s.cpu.interpreted_mode().map_or(0, |m| m as u8);
    let word = |k: usize| u64::from_le_bytes(at.bytes[k..k + 8].try_into().unwrap());
    s.native = [detail::word(mode, at.op, at.count, why.detail), word(0), word(8)];
    s.fault = [why.fault[0], why.fault[1], why.fault[2], s.cpu.rip];
    halt(s, why.code)
}

/// The interpreter's memory for one instruction (plan section 4, layers 2-5), at linear addresses the core has checked
/// against their segments (layer 1). [`Memory::check`] translates every page an access touches (paging off: the identity,
/// wrapping at 4 GiB; compatibility mode: the 4-level walk at CPL 0, A and D set, #PF with its error code), classifies it
/// (guest RAM; the firmware image, docs/restructure/firmware-mode.md: read as RAM, its stores dropped as x86 drops a store
/// to ROM; unassigned, the legacy hole among them: reads all-ones, writes ignored; a device's: a typed stop) and makes
/// the access wait for a peer's split lock; the core makes every check before it commits anything. RAM of type UC is
/// accessed in program order as the resident's own accesses are (`segments::pages`). Accesses: one Arm access for 1, 2 or
/// 4 bytes inside 16 (single-copy atomic, aligned or not: FEAT_LSE2), byte by byte across 16 bytes (the tearing deviation
/// translated code has too); a store into a page holding translations goes through the code-write protocol first. LOCK:
/// one `casal` inside 16 bytes, else under the VM's split lock as translated code's locked accesses crossing 16 bytes.
struct Mem<'a> {
    s: &'a mut State,
    paging: Option<paging::Control>,
    /// The pages the instruction's checks found: [linear page, guest-physical page | its class, [`RAM`], [`ROM`] or 0 for
    /// unassigned] (at most two checks of two pages: far RET's slots).
    pages: [[u64; 2]; 4],
    count: usize,
    /// A memory stop's linear address and guest-physical page.
    stopped: [u64; 2],
}
/// The classes of [`Mem::pages`]' pages: guest RAM, and the firmware image (read as RAM, a store to it dropped); neither is
/// unassigned.
const RAM: u64 = 1;
const ROM: u64 = 2;
impl Mem<'_> {
    /// The guest-physical page of `linear`'s page and its class ([`RAM`], [`ROM`], 0), from the checks made.
    fn page(&self, linear: u32) -> (u64, u64) {
        let p = u64::from(linear & !4095);
        match self.pages[..self.count].iter().find(|e| e[0] == p) {
            Some(e) => (e[1] & !3, e[1] & 3),
            None => halt(self.s, stop::INTERP_BUG), // an access no check covered
        }
    }
    /// The resident's pointer to `n` bytes of guest RAM or the firmware image at `gpa`.
    fn pointer(&self, gpa: u64, n: u8) -> *mut u8 {
        match ram_and_rom().range(gpa, u64::from(n)) {
            Some(p) => p,
            None => halt(self.s, stop::INTERP_BUG), // a RAM page whose bytes are not: a classification bug
        }
    }
    /// One Arm access suffices: `size` bytes at `linear` lie inside one 16-byte block (so inside a page).
    fn single(linear: u32, size: u8) -> bool {
        (linear & 15) + u32::from(size) <= 16
    }
    fn byte(&self, linear: u32) -> u32 {
        match self.page(linear) {
            (g, RAM | ROM) => {
                let p = self.pointer(g | u64::from(linear & 4095), 1);
                // SAFETY: a guest RAM or image byte in the alias.
                u32::from(unsafe { p.read_volatile() })
            }
            _ => 0xff,
        }
    }
    /// A store to RAM page `page` made by `store`: the code-write protocol around it, as `segments::copy`'s.
    fn stored(&mut self, page: u64, store: impl FnOnce()) {
        if cache::translated(page) {
            FetchWrites(self.s).code_write(page);
        }
        store();
        // SAFETY: a barrier; no other effect.
        unsafe { asm!("dsb ish", options(nostack, preserves_flags)) };
        cache::rechecked(page);
    }
}
impl Memory for Mem<'_> {
    fn check(&mut self, linear: u32, size: u32, write: bool) -> Result<(), Exit> {
        let (first, next) = paging::unpaged(u64::from(linear), u64::from(size));
        let touched = [Some(first), next];
        let access = if write { Access::Write } else { Access::Read };
        // Both pages translated before either is classified: a #PF comes before a device's stop.
        let mut found = [(0u64, None); 2];
        for (k, va) in touched.into_iter().flatten().enumerate() {
            found[k] = match self.paging {
                None => (va, None),
                Some(c) => {
                    let t = walk(self.s, c, va, access).map_err(|(error, cr2)| Exit::Fault {
                        vector: 14,
                        error: error as u32,
                        address: cr2 as u32,
                    })?;
                    (t.gpa, Some(t.pat))
                }
            };
        }
        let n = 1 + usize::from(next.is_some());
        let c = cache::control();
        let owed = c.uncached_count != 0 || c.uncached_fence != 0;
        let mut uncached = false;
        let mut gpa = [0u64; 2];
        for (k, va) in touched.into_iter().flatten().enumerate() {
            let (g, pat) = found[k];
            let page = g & !4095;
            let ram = mmio::ram(page);
            let class = if ram {
                RAM
            } else if mmio::rom(page) {
                ROM
            } else {
                0
            };
            if ram {
                let kind = match pat {
                    None => cpu::mtrr_type(msrs(), page), // without paging the PAT is not used
                    Some(p) => cpu::memory_type(msrs(), g, p),
                };
                if kind == cpu::memory_type::UC {
                    let c = cache::control();
                    (c.uncached_fence, c.uncached_resident) = (1, c.uncached_resident + 1);
                    uncached = true;
                }
            } else if class == 0 && let Err(d) = mmio::unassigned(self.s, page) {
                self.stopped = [va, page];
                return Err(Exit::Stop(d as u32));
            }
            gpa[k] = page;
            if self.count == self.pages.len() {
                halt(self.s, stop::INTERP_BUG)
            }
            self.pages[self.count] = [va & !4095, page | class];
            self.count += 1;
        }
        if owed || uncached {
            // SAFETY: a barrier; no other effect.
            unsafe { asm!("dmb ish") };
        }
        split_wait(self.s, &gpa[..n]);
        Ok(())
    }
    fn read(&mut self, linear: u32, size: u8) -> u32 {
        if Self::single(linear, size) {
            return match self.page(linear) {
                (g, RAM | ROM) => {
                    let p = self.pointer(g | u64::from(linear & 4095), size);
                    // SAFETY: `size` bytes of guest RAM or the image in the alias.
                    unsafe { load_word(p, size) }
                }
                _ => exec::mask(size),
            };
        }
        (0..u32::from(size))
            .rev()
            .fold(0, |v, k| v << 8 | self.byte(linear.wrapping_add(k)))
    }
    /// A store to RAM; to the firmware image or an unassigned page, dropped.
    fn write(&mut self, linear: u32, size: u8, value: u32) {
        if Self::single(linear, size) {
            if let (g, RAM) = self.page(linear) {
                let p = self.pointer(g | u64::from(linear & 4095), size);
                // SAFETY: `size` bytes of guest RAM in the alias.
                self.stored(g, || unsafe { store_word(p, size, value) });
            }
            return;
        }
        for k in 0..u32::from(size) {
            let at = linear.wrapping_add(k);
            if let (g, RAM) = self.page(at) {
                let p = self.pointer(g | u64::from(at & 4095), 1);
                // SAFETY: a guest RAM byte in the alias.
                self.stored(g, || unsafe { p.write_volatile((value >> (8 * k)) as u8) });
            }
        }
    }
    fn compare_exchange(&mut self, linear: u32, size: u8, old: u32, new: u32) -> u32 {
        let m = exec::mask(size);
        let (g, class) = self.page(linear);
        if class != 0 && Self::single(linear, size) {
            let p = self.pointer(g | u64::from(linear & 4095), size);
            if class == ROM {
                // The firmware image: what it holds; the locked write is dropped.
                // SAFETY: `size` bytes of the image in the alias.
                return unsafe { load_word(p, size) };
            }
            let mut was = 0;
            // SAFETY: `size` bytes of guest RAM in the alias, inside one 16-byte block.
            self.stored(g, || was = unsafe { cas(p, size, old & m, new & m) });
            return was;
        }
        let (last, last_class) = self.page(linear.wrapping_add(u32::from(size) - 1));
        // No byte of it RAM or the image: all-ones read, the write ignored, nothing to lock.
        if class == 0 && last_class == 0 {
            return m;
        }
        // Crossing 16 bytes, or partly RAM: under the VM's split lock, as translated code's (`split_lock`); a byte of the
        // image is read and its store dropped.
        let s = &mut *self.s;
        s.splits += 1;
        cache::split_begin([g, last], s.vcpus as usize, || {
            let bits = cache::take_shadows();
            if bits != 0 {
                serve_peers(s, bits)
            }
        });
        let was = self.read(linear, size);
        if was == old & m {
            self.write(linear, size, new);
        }
        cache::split_end();
        was
    }
}

/// One Arm load of `size` (1, 2, 4) bytes at `p`, aligned or not (the resident runs with SCTLR_EL1.A clear; inside 16
/// bytes it is single-copy atomic, FEAT_LSE2). Not a Rust unaligned read: the target's strict alignment would split it.
///
/// # Safety
/// `p` points to `size` bytes of Normal memory.
unsafe fn load_word(p: *const u8, size: u8) -> u32 {
    let v: u32;
    // SAFETY: as the caller says.
    unsafe {
        match size {
            1 => asm!("ldrb {v:w}, [{p}]", v = out(reg) v, p = in(reg) p, options(nostack, preserves_flags, readonly)),
            2 => asm!("ldrh {v:w}, [{p}]", v = out(reg) v, p = in(reg) p, options(nostack, preserves_flags, readonly)),
            _ => asm!("ldr {v:w}, [{p}]", v = out(reg) v, p = in(reg) p, options(nostack, preserves_flags, readonly)),
        }
    }
    v
}
/// One Arm store of `size` bytes of `v` at `p`, as [`load_word`].
///
/// # Safety
/// As [`load_word`].
unsafe fn store_word(p: *mut u8, size: u8, v: u32) {
    // SAFETY: as the caller says.
    unsafe {
        match size {
            1 => asm!("strb {v:w}, [{p}]", v = in(reg) v, p = in(reg) p, options(nostack, preserves_flags)),
            2 => asm!("strh {v:w}, [{p}]", v = in(reg) v, p = in(reg) p, options(nostack, preserves_flags)),
            _ => asm!("str {v:w}, [{p}]", v = in(reg) v, p = in(reg) p, options(nostack, preserves_flags)),
        }
    }
}
/// LOCK's compare-and-exchange of `size` bytes at `p`: an LSE `casal` (acquire and release), aligned or not inside 16 bytes
/// (FEAT_LSE2); returns what the bytes held.
///
/// # Safety
/// As [`load_word`], and the bytes lie inside one 16-byte block.
unsafe fn cas(p: *mut u8, size: u8, old: u32, new: u32) -> u32 {
    let mut v = old;
    // SAFETY: as the caller says.
    unsafe {
        match size {
            1 => asm!("casalb {v:w}, {n:w}, [{p}]", v = inout(reg) v, n = in(reg) new, p = in(reg) p, options(nostack, preserves_flags)),
            2 => asm!("casalh {v:w}, {n:w}, [{p}]", v = inout(reg) v, n = in(reg) new, p = in(reg) p, options(nostack, preserves_flags)),
            _ => asm!("casal {v:w}, {n:w}, [{p}]", v = inout(reg) v, n = in(reg) new, p = in(reg) p, options(nostack, preserves_flags)),
        }
    }
    v
}
