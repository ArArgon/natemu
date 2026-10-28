//! Block execution in the resident (`natemu_abi::execution`): find the block
//! for the banked RIP in the code cache ([`cache`]) or walk its fetch, mark its
//! pages as code (shooting down peers' writable shadows of them), ask the host for
//! it and place it, enter it from the state bank, take exits the fast dispatcher
//! (`gates.S` `dispatch_fast`) cannot resolve back into the bank, and serve
//! shadow misses at the recorded access sites of the block holding the faulting
//! PC by walking the guest tables and retrying the same instruction. A write to a
//! page holding translations drops that page's blocks first, or, when the running
//! block came from that page, runs the writing instruction alone in the slot
//! (self-modifying code, [`code_written`]). Literal exits
//! enter the linker (`gates.S` `link_exit`), which finds their target the same way
//! and links the exit to it. A miss at a system instruction (every block ends before one) gets the host's decode of
//! it instead of a block, and the resident executes it as a transaction ([`system`]). Also the 128/64 division
//! helpers admitted code calls through the frame.
use super::*;
use crate::cache::{self, Block};
use natemu_abi::{
    code_object::{self as object, Header, Site},
    execution::*,
    guest_memory::{self, CodePages, CodeWrites},
    paging::{self, Access, Fault},
    shadow::{self, READ_ONLY},
};

unsafe extern "C" {
    fn enter_allocated(frame: *const NativeFrame, pc: u64, spsr: u64) -> !;
    fn resume_exception(frame: *const NativeFrame, pc: u64, spsr: u64) -> !;
    fn dispatch_fast();
    fn link_exit();
    fn stop_gate();
    fn natemu_ludiv();
    fn natemu_ldiv();
}

// FEX's LUDIV/LDIV handler ABI (JIT/ALUOps.cpp: a 64-bit div/idiv whose dividend needs 128 bits calls
// `Pointers.LUDIVHandler/LDIVHandler` with `str lr, [sp, #-16]!; blr x3`): x0 = dividend high, x1 = low, x2 = divisor;
// returns x0 = quotient, x1 = remainder. natemu adds x2 = 0, or 1 for #DE (zero divisor, or a quotient that does
// not fit 64 bits; x0/x1 are then meaningless), which the code tests before writing any guest register. Only x0-x3
// change: NZCV and every other register are preserved, and nothing but the native stack below sp is touched
// (`HELPER_STACK_BYTES` with FEX's lr push). Unsigned division of |high:low| by |divisor| after the
// `high < divisor` check (which also rejects a zero divisor), one quotient bit per step; the signed helper divides
// magnitudes, checks the quotient against 2^63 - 1 (2^63 when negative) and applies the signs (remainder: the
// dividend's).
// ponytail: 64 iterations (about 600 instructions) on this rare path; a normalised udiv-based 128/64 (Knuth D) if it matters.
global_asm!(
    ".pushsection .text.natemu_div, \"ax\"",
    ".balign 4",
    ".global natemu_ludiv",
    ".global natemu_ldiv",
    "natemu_ludiv:",
    "stp x4, x5, [sp, #-48]!",
    "stp x6, x7, [sp, #16]",
    "mrs x4, nzcv",
    "str x4, [sp, #32]",
    "mov x5, #0", // quotient sign mask
    "mov x6, #0", // remainder sign mask
    "mov x7, #0", // signed
    "b 2f",
    "natemu_ldiv:",
    "stp x4, x5, [sp, #-48]!",
    "stp x6, x7, [sp, #16]",
    "mrs x4, nzcv",
    "str x4, [sp, #32]",
    "mov x7, #1",
    "asr x6, x0, #63",
    "asr x5, x2, #63",
    "eor x5, x5, x6",
    // |high:low| = (v ^ m) - m over 128 bits, m = the dividend's sign mask
    "eor x0, x0, x6",
    "eor x1, x1, x6",
    "subs x1, x1, x6",
    "sbc x0, x0, x6",
    "asr x4, x2, #63",
    "eor x2, x2, x4",
    "sub x2, x2, x4",
    "2:",
    "cmp x0, x2",
    "b.hs 8f",
    "mov x3, #64",
    "3:",
    "lsr x4, x0, #63",       // the bit shifted out of the partial remainder
    "extr x0, x0, x1, #63",  // remainder:dividend <<= 1
    "lsl x1, x1, #1",
    "cmp x0, x2",
    "ccmp x4, #1, #2, lo",   // C: remainder (65 bits) >= divisor
    "csel x4, x2, xzr, hs",
    "sub x0, x0, x4",
    "cinc x1, x1, hs",       // the quotient bit
    "subs x3, x3, #1",
    "b.ne 3b",
    "cbz x7, 6f",
    "mov x4, #0x7fffffffffffffff",
    "sub x4, x4, x5",
    "cmp x1, x4",
    "b.hi 8f",
    "6:",
    "eor x1, x1, x5",
    "sub x1, x1, x5",
    "eor x0, x0, x6",
    "sub x0, x0, x6",
    "mov x2, x0",
    "mov x0, x1",
    "mov x1, x2",
    "mov x2, #0",
    "b 9f",
    "8:",
    "mov x2, #1",
    "9:",
    "ldr x4, [sp, #32]",
    "msr nzcv, x4",
    "ldp x6, x7, [sp, #16]",
    "ldp x4, x5, [sp], #48",
    "ret",
    ".popsection",
);

/// Guest-physical RAM through the resident's alias.
const RAM: GuestRam = unsafe { GuestRam::new(0, GUEST_RAM_BYTES as u64, GUEST_RAM_VA) };
/// EL1t, DAIF masked; NZCV is or-ed in.
const BLOCK_SPSR: u64 = 0x3c4;

fn record() -> *mut State {
    STATE_VA as *mut State
}
unsafe fn load() -> State {
    unsafe { read_record(record()) }
}
/// The staged object's header (host-written at a translation reply).
fn staged() -> Header {
    unsafe { read_record(STAGING_VA as *const Header) }
}
/// A field of FEX's frame (x28 = [`FRAME_POINTER`]).
fn frame_word(offset: u32) -> *mut u64 {
    (FRAME_POINTER + offset as u64) as *mut u64
}

/// Stops this vCPU; the host reads `reason` and the record. The host may never resume it, so it first drops every
/// shadow and writable bit unasked (as if serving `SHADOWS`): no peer ever waits on a vCPU that has stopped.
fn halt(mut s: State, reason: u64) -> ! {
    reset_shadows(&mut s);
    s.stop = reason;
    unsafe { write_record(record(), s) };
    stop(SUCCESS, reason)
}

pub unsafe fn start() -> ! {
    let mut s = unsafe { load() };
    if s.cpu.control().is_none() || s.request != NONE || s.vcpu as usize >= cache::MAX_VCPUS {
        // Before anything is shadowed or marked (and before the vCPU number is known to be valid).
        s.stop = stop::UNSUPPORTED_MODE;
        unsafe { write_record(record(), s) };
        stop(SUCCESS, s.stop)
    }
    cache::reset();
    s.stop_gate = stop_gate as *const () as u64;
    unsafe {
        frame_word(frame::DISPATCHER_LOOP_TOP).write_volatile(dispatch_fast as *const () as u64);
        frame_word(frame::EXIT_FUNCTION_LINKER).write_volatile(link_exit as *const () as u64);
        frame_word(frame::LUDIV_HANDLER).write_volatile(natemu_ludiv as *const () as u64);
        frame_word(frame::LDIV_HANDLER).write_volatile(natemu_ldiv as *const () as u64);
        dispatch(s)
    }
}

/// Enters the block for `s.cpu.rip`, or steps its first instruction when a write killed it as it was placed.
unsafe fn dispatch(mut s: State) -> ! {
    match unsafe { find(&mut s) } {
        Some(block) => unsafe { enter(s, block) },
        None => step(s),
    }
}
/// The block for `s.cpu.rip`: from the cache (L2; L1 was probed by the fast dispatcher), else asks the host for it and
/// places it. Pending invalidations are taken first, and again after placing. A write that raced the block's fetch (a
/// peer's, or this vCPU's own fetch walk writing an A bit into it) killed it, cleared this vCPU's mark of a source page
/// (its post may have been drained before the block existed, in [`mark`]'s wait) or flushed the cache during the miss:
/// the block, if still live, is then killed, and `None` says to step the instruction instead (re-dispatching could
/// starve under a peer rewriting the page on every fetch; the step always runs one instruction). This keeps the
/// invariant that every live block's source pages carry this vCPU's mark, unless a write since cleared it, which then
/// either posted the page to this vCPU (a peer's) or invalidates its blocks at once (its own, [`code_written`]). A
/// system instruction at the RIP is executed ([`system`]) and the search goes on at the next RIP (so the block found
/// may be for a later RIP than the one asked for).
unsafe fn find(s: &mut State) -> Option<&'static Block> {
    let mut flushed = false;
    loop {
        take_invalidations(s);
        let context = s.cpu.code_context();
        cache::select_l1(context);
        let block = match cache::lookup(s.cpu.rip, context) {
            Some(b) => {
                s.l2_hits += 1;
                b
            }
            None => match (s.generation, unsafe { request(s, REQUEST_TRANSLATE) }) {
                (missed, Reply::Block(h)) => {
                    s.requests += 1;
                    let Some(block) = cache::place(&h, STAGING_VA as *const u8, context, s.source_gpa, s.link_points) else {
                        // Full: flush (which also drops this vCPU's marks) and miss again; an empty cache holds any block.
                        if flushed {
                            halt(*s, stop::BAD_REPLY)
                        }
                        flush(s);
                        flushed = true;
                        continue;
                    };
                    let placed = s.generation;
                    take_invalidations(s);
                    if s.generation != placed {
                        return None; // flushed: the block is gone
                    }
                    let unmarked = |g: u64| g != u64::MAX && !cache::marked(g);
                    if placed != missed || block.flags & cache::DEAD != 0 || block.source_gpa.iter().any(|&g| unmarked(g)) {
                        if block.flags & cache::DEAD == 0 && cache::discard(block).is_none() {
                            halt(*s, stop::BAD_LINK)
                        }
                        return None;
                    }
                    block
                }
                (_, Reply::System(instruction)) => {
                    system(s, instruction);
                    continue;
                }
            },
        };
        cache::fill_l1(s.cpu.rip, block);
        return Some(block);
    }
}

/// A literal exit (`Pointers.ExitFunctionLinker`, FEX's `ExitFunctionLink`), entered with the exit's state as at a
/// dispatcher exit. `InlineJITBlockHeader` names the block that ran it: only its entry prologue stores there
/// (admission), so it must be a live cache block with a link point to `State.rip`; anything else halts. The target is
/// found as by the dispatcher; the exit is linked to it unless the cache was flushed meanwhile (the source may be
/// gone: FEX re-checks the same way) or the link records are full, then the target is entered. A block run from the
/// single-step slot is never linked. A target killed as it was placed ([`find`]) is stepped, not linked (`stale_links`).
unsafe fn linker(frame: &NativeFrame) -> ! {
    unsafe {
        let mut s = load();
        bank(&mut s.cpu, frame, frame.nzcv, true);
        s.cpu.rip = frame_word(frame::RIP).read_volatile();
        s.linker += 1;
        let header = frame_word(frame::BLOCK_HEADER).read_volatile();
        let source = match header {
            SLOT_VA => None,
            _ => Some(cache::block_with_header(header).unwrap_or_else(|| halt(s, stop::BAD_LINK))),
        };
        let (generation, rip) = (s.generation, s.cpu.rip);
        // A target killed as it was placed is neither linked to nor entered: its instruction is stepped.
        let Some(target) = find(&mut s) else {
            if source.is_some() {
                s.stale_links += 1;
            }
            step(s)
        };
        match source {
            // Flushed, or its page invalidated (queued), while the target was found.
            Some(source) if s.generation != generation || source.flags & cache::DEAD != 0 => s.stale_links += 1,
            // The exit's target was a system instruction: the block found follows it, nothing to link.
            Some(_) if target.rip != rip => {}
            Some(source) => match cache::link(source, target) {
                cache::Link::Linked(n) => s.links += n,
                cache::Link::Full => s.full_links += 1,
                cache::Link::Bad => halt(s, stop::BAD_LINK),
            },
            None => {}
        }
        enter(s, target)
    }
}

/// What the host answered a request with.
enum Reply {
    /// A block staged (its header).
    Block(Header),
    /// The system instruction at `s.cpu.rip`, decoded (translate requests only).
    System(System),
}
/// Asks the host for code at `s.cpu.rip` (`kind`: a block, or one instruction for the slot) and checks the reply: the
/// staged block, or a system instruction's decode. The resident walks the instruction fetch itself (RIP's page, and the
/// next page when the host's bytes ran out at the page end, [`REPLY_NEXT_PAGE`]) and, for a block, marks each page
/// before the host copies its bytes ([`mark`]), so a write to it from then on posts it to this vCPU; the step block
/// marks nothing (see [`code_written`]). A walk's fault goes to the host with the request (the host ends the run with
/// it, or cuts the block before the next page). The block's source pages are those its bytes reach.
unsafe fn request(s: &mut State, kind: u64) -> Reply {
    let block = kind == REQUEST_TRANSLATE;
    let page = s.cpu.rip & !4095;
    (s.fetch, s.fetch_fault) = ([u64::MAX; 2], [fetch::NONE; 3]);
    // The host stops the run at its stop RIP: nothing there is fetched.
    if s.cpu.rip != s.stop_at {
        fetch_page(s, page, 0, block);
    }
    loop {
        unsafe {
            s.request = kind;
            s.reply = NONE;
            write_record(record(), *s);
            asm!("dsb sy", "hvc #{imm}", imm = const TRANSLATE_HVC);
            *s = load();
        }
        if s.reply != REPLY_NEXT_PAGE {
            break;
        }
        // Once, after RIP's page was fetched.
        if s.fetch[0] == u64::MAX || s.fetch[1] != u64::MAX || s.fetch_fault[0] != fetch::NONE {
            halt(*s, stop::BAD_REPLY)
        }
        fetch_page(s, page.wrapping_add(4096), 1, block);
    }
    if s.reply == REPLY_PARK {
        halt(*s, stop::PARKED)
    }
    if s.reply == REPLY_SYSTEM && block {
        if !s.system.well_formed() {
            halt(*s, stop::BAD_REPLY)
        }
        s.request = NONE;
        return Reply::System(s.system);
    }
    let h = staged();
    let table_end = (h.sites_offset as usize).checked_add(h.sites_count as usize * size_of::<Site>());
    let crosses = (s.cpu.rip & 4095) + u64::from(h.guest_bytes) > 4096;
    s.source_gpa = [s.fetch[0], if crosses { s.fetch[1] } else { u64::MAX }];
    if s.reply != REPLY_ADMITTED
        || h.guest_rip != s.cpu.rip
        || h.guest_bytes == 0
        || s.source_gpa[0] == u64::MAX
        || (crosses && s.source_gpa[1] == u64::MAX)
        || h.code_size as usize > natemu_abi::execution::cache::MAX_BLOCK_BYTES
        || h.code_offset as usize + h.code_size as usize > STAGING_BYTES
        || table_end.is_none_or(|end| end > STAGING_BYTES)
        // Host-supplied link points name words the linker may rewrite: each whole exit (4 words) inside the code.
        || s.link_points.iter().any(|p| p.offset != 0 && (p.offset % 4 != 0 || u64::from(p.offset) + 16 > u64::from(h.code_size)))
    {
        halt(*s, stop::BAD_REPLY)
    }
    s.request = NONE;
    Reply::Block(h)
}
/// Walks the fetch of page `va` for a request into `s.fetch[i]` (or its failure into `s.fetch_fault`); for a block,
/// marks it ([`mark`]).
fn fetch_page(s: &mut State, va: u64, i: usize, block: bool) {
    match fetch_walk(s, va) {
        Ok(gpa) => {
            s.fetch[i] = gpa;
            if block {
                mark(s, gpa);
            }
        }
        Err(fault) => s.fetch_fault = fault,
    }
}
/// The guest-physical page `va` is fetched from (the walk sets A bits like the CPU's), or why not ([`fetch`]). A walk
/// losing its A/D races to peers walks again, as x86 does (the watchdog contains a livelock). No block of this vCPU runs
/// now (a step's source block is abandoned, and the linker checks that its source is not dead after finding the
/// target), so an A write into a page holding translations just invalidates it ([`cache::written`]).
fn fetch_walk(s: &mut State, va: u64) -> Result<u64, [u64; 3]> {
    let Some(control) = s.cpu.control() else {
        halt(*s, stop::UNSUPPORTED_MODE)
    };
    loop {
        let mut tables = GuestTables::observed(RAM, CodeWrites(FetchWrites(s)));
        match paging::walk(&mut tables, control, va, Access::Fetch) {
            Ok(t) if t.wb && RAM.range(t.gpa & !4095, 4096).is_some() => return Ok(t.gpa & !4095),
            Ok(_) => return Err([fetch::NOT_RAM, 0, 0]),
            Err(Fault::Contended) => {}
            Err(f) => return Err(fetch::encode(f)),
        }
    }
}
/// The fetch walk's code-page writes (see [`fetch_walk`]).
struct FetchWrites<'a>(&'a mut State);
impl CodePages for FetchWrites<'_> {
    fn translated(&mut self, gpa: u64) -> bool {
        cache::translated(gpa)
    }
    fn code_write(&mut self, gpa: u64) {
        let s = &mut *self.0;
        match cache::written(gpa) {
            Some(n) => (s.invalidations, s.unlinks) = (s.invalidations + 1, s.unlinks + n),
            None => halt(*s, stop::BAD_LINK),
        }
    }
    fn rechecked(&mut self, gpa: u64) {
        cache::rechecked(gpa)
    }
}

/// Marks `gpa`'s page as holding this vCPU's translations before the host copies its bytes, then makes sure no vCPU
/// keeps a writable shadow of it (the one synchronous shootdown): this vCPU's own shadows are reset; each peer with a
/// writable bit is asked to reset its shadows (`SHADOWS`, a kick) and waited for until its reset counter moves (not
/// until its bit clears: it may set it again, but then it sees this mark and takes the code-write path). The counter is
/// read before the bit is checked again, so a reset in between is never waited for twice (a vCPU that has stopped for
/// good reset its shadows first). While waiting, this vCPU drains its own record, so two vCPUs waiting for each other
/// both progress; a write to the page that clears this vCPU's mark ends the wait (the block placed from it is stepped,
/// see [`find`]).
fn mark(s: &mut State, gpa: u64) {
    let writers = cache::mark(gpa);
    let own = 1u8 << s.vcpu;
    if writers & own != 0 {
        reset_shadows(s);
    }
    for p in (0..cache::MAX_VCPUS).filter(|&p| writers & !own & 1 << p != 0) {
        let count = cache::resets_of(p);
        if cache::writable(gpa) & 1 << p == 0 {
            continue;
        }
        s.shootdowns += 1;
        cache::ask_shadows(p);
        while cache::resets_of(p) == count && cache::marked(gpa) {
            take_invalidations(s);
            core::hint::spin_loop();
        }
    }
}

/// Applies this vCPU's pending invalidations (listed pages drop their blocks; host writes to guest RAM while it was
/// stopped flush) and resets its shadows when a peer asks.
fn take_invalidations(s: &mut State) {
    let Some(pending) = cache::drain() else { halt(*s, stop::BAD_LINK) };
    if pending & cache::PAGES != 0 {
        s.invalidations += 1;
    }
    if pending & cache::FLUSH != 0 {
        flush(s);
    }
    if pending & cache::SHADOWS != 0 {
        reset_shadows(s);
    }
}

/// Drops every block (capacity, or an invalidation request), with nothing generated live and the state banked: first
/// undoes every link and checks that every link point holds its admitted word again.
fn flush(s: &mut State) {
    match cache::unlink_all() {
        Some(n) if cache::links_restored() => s.unlinks += n,
        _ => halt(*s, stop::BAD_LINK),
    }
    cache::reset();
    s.generation += 1;
    s.shadow_generation += 1;
}
/// Removes every shadow of this vCPU and its writable bits, and counts the reset.
fn reset_shadows(s: &mut State) {
    tables::space().reset();
    cache::shadows_reset();
    s.shadow_generation += 1;
}

/// Enters `block` from the bank: GPRs and PF/AF in their static registers, NZCV with C = !CF, the frame in x28.
unsafe fn enter(mut s: State, block: &Block) -> ! {
    let mut f = NativeFrame::ZERO;
    for (&reg, &value) in SRA.iter().zip(&s.cpu.gpr) {
        f.x[reg as usize] = value;
    }
    let (nzcv, pf, af) = native_flags(s.cpu.rflags);
    f.x[SRA[16] as usize] = pf;
    f.x[SRA[17] as usize] = af;
    f.x[28] = FRAME_POINTER;
    f.sp = STACK_TOP;
    s.blocks += 1;
    unsafe {
        write_record(record(), s);
        let entry = &raw mut (*shared()).initial;
        write_record(entry, f);
        enter_allocated(entry, block.entry(), BLOCK_SPSR | nzcv)
    }
}

/// Exception capture (`fault` 1), the linker (2), the stop gate (3) and the dispatcher's slow path (0) end here.
pub unsafe fn returned(frame: &NativeFrame, fault: u64) -> ! {
    unsafe {
        match fault {
            0 => {}
            2 => linker(frame),
            3 => stopped(frame),
            _ => aborted(frame),
        }
        // Block exit: guest state is in the static registers, NZCV (CF inverted) and PF/AF registers.
        let mut s = load();
        bank(&mut s.cpu, frame, frame.nzcv, true);
        s.cpu.rip = frame_word(frame::RIP).read_volatile();
        dispatch(s)
    }
}

/// A precise stop (the stop gate, `gates.S` `stop_gate`): a kick write-protected this vCPU's stop page, so a block
/// entry's stop store faulted to the host, which entered the gate with ELR/SPSR as the fault left them. ELR must be
/// the stop store of a live cache block or of the step block, entered from EL1t; anything else halts. Nothing of the
/// block ran but its prologue's header store, so the state is its entry state (static registers, NZCV with CF
/// inverted) at its RIP (`State.rip` is stale after a linked edge). Drains (the kick's reason) and re-dispatches; the
/// step block is entered again instead: it is uncached and holds one instruction already fetched, so no drain drops
/// it, and re-entering it means kicks arriving faster than a block entry cannot keep discarding the step.
unsafe fn stopped(frame: &NativeFrame) -> ! {
    let (elr, spsr): (u64, u64);
    unsafe { asm!("mrs {0}, elr_el1", "mrs {1}, spsr_el1", out(reg) elr, out(reg) spsr) };
    let mut s = unsafe { load() };
    let block = cache::block_at(elr).filter(|b| elr == b.code_va() + u64::from(STOP_STORE) && b.flags & cache::DEAD == 0);
    let Some(b) = block.filter(|_| spsr & 0x1f == 4) else {
        s.native = [0, 0, elr];
        halt(s, stop::BAD_STOP)
    };
    bank(&mut s.cpu, frame, frame.nzcv, true);
    s.cpu.rip = b.rip;
    s.stops += 1;
    if b.flags & cache::STEP != 0 {
        take_invalidations(&mut s);
        unsafe { enter(s, b) }
    }
    unsafe { dispatch(s) }
}

/// CR0 bits (SDM vol. 3 §2.5): those defined; the others in 31:0 are reserved and ignored on a load (they stay clear),
/// those in 63:32 raise #GP. Of the defined bits the resident implements changes of WP only (PG, PE are fixed in
/// 64-bit code: clearing either raises #GP; ET is hard-wired).
const CR0_DEFINED: u64 = 0xe005_003f;
const CR0_CD: u64 = 1 << 30;
const CR0_NW: u64 = 1 << 29;
/// CR4 bits the resident implements (PAE is fixed in long mode), and the defined bits whose change it refuses as
/// unsupported (VME, PVI, TSD, DE, PSE, MCE, PCE, OSFXSR, OSXMMEXCPT, UMIP, LA57, VMXE, SMXE, FSGSBASE, OSXSAVE, PKE).
/// Every other bit is reserved for natemu's CPU model (#GP): the architecturally reserved ones and the features it
/// does not have, PCIDE and SMAP among them (KL, CET, PKS, UINTR, LAM, FRED).
const CR4_SUPPORTED: u64 = CR4_PAE | CR4_PGE | CR4_SMEP;
const CR4_UNSUPPORTED: u64 = 0x0045_7f5f;
/// EFER bits of natemu's model (Intel: SCE, LME, LMA, NXE); others reserved (#GP).
const EFER_DEFINED: u64 = EFER_SCE | EFER_LME | EFER_LMA | EFER_NXE;
const UD: [u64; 2] = [6, 0];
const GP: [u64; 2] = [13, 0];

/// Executes the system instruction at `s.cpu.rip` (`i`, the host's decode, checked by [`request`]) as a transaction
/// with x86 semantics in 64-bit mode: its checks raise their exception (#UD, then #GP) with the state unchanged;
/// otherwise its effect commits, RIP moves past it and RFLAGS stay. Every control change (and every CR3 load) drops
/// this vCPU's cached code and shadows: they are its instruction and data TLBs, which may always lose entries, and are
/// keyed by values a change makes stale (a block's context, a shadow's rights). INVLPG drops the shadows and blocks of
/// the 2 MiB-aligned range holding the address (a superset of its page in either page size). Only this vCPU's
/// state, cache and shadows change. Forms and operand values the resident does not implement stop it
/// (`unsupported_system`) before any effect.
fn system(s: &mut State, i: System) {
    s.systems += 1;
    let c = s.cpu;
    let fault = |s: &mut State, [vector, error]: [u64; 2]| -> ! {
        s.fault = [vector, 0, error, s.cpu.rip];
        halt(*s, stop::GUEST_FAULT)
    };
    let unsupported = |s: &State| -> ! { halt(*s, stop::UNSUPPORTED_SYSTEM) };
    let moves = matches!(i.kind, system::MOV_TO_CR | system::MOV_FROM_CR);
    if i.lock != 0 || (moves && !matches!(i.cr, 0 | 2 | 3 | 4 | 8)) {
        fault(s, UD)
    }
    if c.cpl != 0 {
        fault(s, GP)
    }
    let gpr = |r: u32| c.gpr[r as usize];
    // Whether the vCPU's translations must go.
    let changed = match i.kind {
        system::MOV_FROM_CR => {
            s.cpu.gpr[i.reg as usize] = match i.cr {
                0 => c.cr0,
                3 => c.cr3,
                4 => c.cr4,
                _ => unsupported(s), // CR2 (no exception delivery yet), CR8 (no APIC)
            };
            false
        }
        system::MOV_TO_CR => {
            let v = gpr(i.reg);
            match i.cr {
                0 => {
                    let new = v & CR0_DEFINED | CR0_ET;
                    if v >> 32 != 0 || new & CR0_PG == 0 || new & CR0_PE == 0 || new & (CR0_NW | CR0_CD) == CR0_NW {
                        fault(s, GP)
                    }
                    if (new ^ c.cr0) & !CR0_WP != 0 {
                        unsupported(s)
                    }
                    s.cpu.cr0 = new;
                }
                3 => {
                    // Without PCIDE, bits 63:MAXPHYADDR are reserved (bit 63 included); PWT/PCD are not modelled.
                    if v & !(paging::PA_MASK | 0xfff) != 0 {
                        fault(s, GP)
                    }
                    if v & !(paging::PA_MASK | CR3_IGNORED) != 0 {
                        unsupported(s)
                    }
                    s.cpu.cr3 = v;
                    return commit(s, i, true); // a load of the same value too
                }
                4 => {
                    if v & !(CR4_SUPPORTED | CR4_UNSUPPORTED) != 0 || (v ^ c.cr4) & (CR4_PAE | CR4_LA57) != 0 {
                        fault(s, GP)
                    }
                    if (v ^ c.cr4) & CR4_UNSUPPORTED != 0 {
                        unsupported(s)
                    }
                    s.cpu.cr4 = v;
                }
                _ => unsupported(s),
            }
            s.cpu != c
        }
        system::INVLPG => {
            let base = match i.base {
                system::RIP => c.rip.wrapping_add(u64::from(i.length)),
                system::NO_REG => 0,
                r => gpr(r),
            };
            let index = if i.index == system::NO_REG { 0 } else { gpr(i.index) << i.scale };
            let address = base.wrapping_add(index).wrapping_add(i.disp as u64);
            // A non-canonical address invalidates nothing.
            if paging::canonical(address) {
                let start = address & !(LARGE_PAGE - 1);
                for page in (0..LARGE_PAGE / 4096).map(|k| start + k * 4096) {
                    if tables::descriptor(page) & 1 != 0 && tables::unmap(page).is_err() {
                        halt(*s, stop::SHADOW)
                    }
                }
                match cache::invalidate_linear(start, LARGE_PAGE) {
                    Some(n) => s.unlinks += n,
                    None => halt(*s, stop::BAD_LINK),
                }
            }
            false
        }
        system::RDMSR | system::WRMSR => {
            if gpr(1) as u32 != system::MSR_EFER {
                fault(s, GP) // natemu's model implements no other MSR
            }
            if i.kind == system::RDMSR {
                (s.cpu.gpr[0], s.cpu.gpr[2]) = (c.efer & 0xffff_ffff, c.efer >> 32);
                false
            } else {
                let v = gpr(2) << 32 | gpr(0) & 0xffff_ffff;
                if v & !EFER_DEFINED != 0 || (v ^ c.efer) & EFER_LME != 0 && c.cr0 & CR0_PG != 0 {
                    fault(s, GP)
                }
                s.cpu.efer = v & !EFER_LMA | c.efer & EFER_LMA; // LMA writes are ignored
                s.cpu.efer != c.efer
            }
        }
        _ => halt(*s, stop::BAD_REPLY),
    };
    commit(s, i, changed)
}
/// Ends a system transaction: RIP past the instruction; with `flush`, this vCPU's cached code and shadows go.
fn commit(s: &mut State, i: System, flush_all: bool) {
    if s.cpu.control().is_none() {
        halt(*s, stop::UNSUPPORTED_MODE)
    }
    if flush_all {
        flush(s);
        reset_shadows(s);
    }
    s.cpu.rip = s.cpu.rip.wrapping_add(u64::from(i.length));
}
const LARGE_PAGE: u64 = 0x20_0000;

/// Guest GPRs and arithmetic flags from the native state: the static-register map,
/// PF/AF registers and `nzcv` (PSTATE layout) with the given CF inversion.
fn bank(cpu: &mut Cpu, f: &NativeFrame, nzcv: u64, cf_inverted: bool) {
    let reg = |i: usize| f.x[SRA[i] as usize];
    for (i, value) in cpu.gpr.iter_mut().enumerate() {
        *value = reg(i);
    }
    cpu.rflags = guest_flags(cpu.rflags, nzcv, reg(16), reg(17), cf_inverted);
}

/// Stops with an x86 exception due at `site`'s guest instruction in `b` (`vector`,
/// CR2, error code): the bank holds the exact state at that instruction's
/// start, which the precision guarantee places at the site (or its stub):
/// GPRs and PF/AF in their static registers, NZCV in PSTATE or, for
/// `nzcv=frame` sites, in the frame's pre-NZCV slot. No IDT delivery yet.
fn guest_fault(mut s: State, f: &NativeFrame, b: &Block, site: &Site, [vector, cr2, error]: [u64; 3]) -> ! {
    bank_at(&mut s, f, b, site);
    s.fault = [vector, cr2, error, s.cpu.rip];
    halt(s, stop::GUEST_FAULT)
}
/// Banks the exact state at the start of `site`'s guest instruction in `b` (see [`guest_fault`]).
fn bank_at(s: &mut State, f: &NativeFrame, b: &Block, site: &Site) {
    let nzcv = match site.nzcv {
        object::NZCV_FRAME => unsafe { ((FRAME_POINTER + frame::PRE_NZCV as u64) as *const u32).read_volatile() as u64 },
        _ => f.nzcv,
    };
    bank(&mut s.cpu, f, nzcv, site.cf_inverted != 0);
    s.cpu.rip = b.rip.wrapping_add(site.guest_offset as u64);
}

/// Self-modifying code: the instruction at `site` of `b` writes `gpa`'s page (a store, a split store or its walk's
/// A/D update), which holds translations. If `b` was translated from that page it must not run past the instruction
/// (whose store may change bytes after it in `b`): the instruction is stepped ([`step_from`]) instead. Otherwise the
/// page's blocks are invalidated (none of them is running) and the write proceeds. The step block itself never
/// steps: it holds one instruction, already fetched, so its own writes simply invalidate (this ends the step: no loop).
fn code_written(s: &mut State, f: &NativeFrame, b: &Block, site: &Site, gpa: u64) {
    if b.flags & cache::STEP == 0 && b.source_gpa.iter().any(|&g| g != u64::MAX && g >> 12 == gpa >> 12) {
        step_from(*s, f, b, site)
    }
    match cache::written(gpa) {
        Some(n) => (s.invalidations, s.unlinks) = (s.invalidations + 1, s.unlinks + n),
        None => halt(*s, stop::BAD_LINK),
    }
}
/// Steps the instruction at `site` of `b` ([`step`]) from the exact state at its start (a split store in progress is
/// dropped: it wrote nothing yet).
fn step_from(mut s: State, f: &NativeFrame, b: &Block, site: &Site) -> ! {
    bank_at(&mut s, f, b, site);
    step(s)
}
/// Runs the instruction at `s.cpu.rip` (the bank holds the state at its start) alone, as an uncached one-instruction
/// block in the slot, which marks no source page. A write it makes to a page holding translations takes
/// [`code_written`]'s invalidation path and completes; its exit goes to the next instruction, which the next miss
/// compiles from the new bytes (SDM vol. 3, "Handling Self- and Cross-Modifying Code").
fn step(mut s: State) -> ! {
    s.steps += 1;
    let Reply::Block(h) = (unsafe { request(&mut s, REQUEST_STEP) }) else { halt(s, stop::BAD_REPLY) };
    let context = s.cpu.code_context();
    match cache::place_step(&h, STAGING_VA as *const u8, context, s.source_gpa, s.link_points) {
        Some(block) => unsafe { enter(s, block) },
        None => halt(s, stop::BAD_REPLY),
    }
}
/// #GP(0), or #SS(0) for a stack-segment access: a non-canonical address.
fn noncanonical(site: &Site) -> [u64; 3] {
    [if site.segment == object::SEG_SS { 12 } else { 13 }, 0, 0]
}

/// The address a site accesses, from the registers before the access.
fn site_address(site: &Site, f: &NativeFrame) -> u64 {
    let reg = |n: u8| if n == 31 { f.sp } else { f.x[n as usize] };
    let mut address = reg(site.base_reg).wrapping_add(site.imm as i64 as u64);
    if site.index_reg != object::NO_REG {
        let index = reg(site.index_reg);
        let index = match site.extend {
            object::EXT_UXTW => index as u32 as u64,
            object::EXT_SXTW => index as u32 as i32 as i64 as u64,
            _ => index,
        };
        address = address.wrapping_add(index << site.shift);
    }
    address
}
/// The linear pages `[address, address + width)` touches, in address order (the second is `None` if
/// it stays in one page). x86 checks the whole access is canonical before any walk.
fn pages(address: u64, width: u64) -> Option<[Option<u64>; 2]> {
    let last = address.wrapping_add(width - 1);
    (paging::canonical(address) && paging::canonical(last))
        .then(|| [Some(address), ((last ^ address) >> 12 != 0).then_some(last & !4095)])
}

/// A data walk's code-page writes: an A/D write into a page holding translations is a write by the guest instruction
/// at `site` of `b` ([`code_written`], which may step it instead of returning).
struct DataWrites<'a> {
    s: &'a mut State,
    f: &'a NativeFrame,
    b: &'a Block,
    site: &'a Site,
}
impl CodePages for DataWrites<'_> {
    fn translated(&mut self, gpa: u64) -> bool {
        cache::translated(gpa)
    }
    fn code_write(&mut self, gpa: u64) {
        code_written(self.s, self.f, self.b, self.site, gpa)
    }
    fn rechecked(&mut self, gpa: u64) {
        cache::rechecked(gpa)
    }
}

/// Walks `va` with `access` for the guest instruction at `site` of `b` and shadows the page; a guest
/// fault stops with the precise state. Returns the guest-physical address of `va`, or `None` when the
/// walk kept losing A/D races to peers (nothing installed: the caller resumes the instruction, which
/// faults and walks again, as x86 re-walks). Translated pages are shadowed read-only whatever the
/// walk grants; a write to one, or an A/D update in one, takes [`code_written`] first (which may step
/// the instruction instead of returning), and each A/D update is re-checked after it is made.
fn translate(s: &mut State, f: &NativeFrame, b: &Block, site: &Site, va: u64, access: Access) -> Option<u64> {
    let Some(control) = s.cpu.control() else {
        halt(*s, stop::UNSUPPORTED_MODE)
    };
    let walk = paging::walk(&mut GuestTables::observed(RAM, CodeWrites(DataWrites { s: &mut *s, f, b, site })), control, va, access);
    match walk {
        Ok(t) if t.wb => {
            let writable = access == Access::Write && t.writable && t.dirty;
            // As a mark is set before the writable bits are read ([`mark`]), the writable bit is set before the marks
            // are read: a vCPU marking the page then either shoots this shadow down or is seen here, and the write
            // takes the code-write path.
            if writable {
                cache::shadowed_writable(t.gpa);
                if cache::translated(t.gpa) {
                    code_written(s, f, b, site, t.gpa);
                }
            }
            let old = tables::descriptor(va);
            if writable && old & 1 != 0 && old & READ_ONLY != 0 {
                s.upgrades += 1;
            }
            install(s, va, t.gpa, writable);
            Some(t.gpa)
        }
        // Only write-back guest memory is shadowed.
        Ok(_) => halt(*s, stop::SHADOW),
        Err(Fault::Page { address, error }) => guest_fault(*s, f, b, site, [14, address, error]),
        Err(Fault::Noncanonical) => guest_fault(*s, f, b, site, noncanonical(site)),
        Err(Fault::Contended) => None,
        Err(Fault::Unsupported) => halt(*s, stop::UNSUPPORTED_MODE),
    }
}

unsafe fn aborted(f: &NativeFrame) -> ! {
    unsafe {
        let (esr, far, elr, spsr): (u64, u64, u64, u64);
        asm!("mrs {0}, esr_el1", "mrs {1}, far_el1", "mrs {2}, elr_el1", "mrs {3}, spsr_el1",
             out(reg) esr, out(reg) far, out(reg) elr, out(reg) spsr);
        let mut s = load();
        s.native = [esr, far, elr];
        let Some(b) = cache::block_at(elr) else {
            halt(s, stop::UNKNOWN_ABORT)
        };
        let at = elr - b.code_va();
        s.native[2] = at;
        let all = cache::sites(b);
        let Ok(i) = all.binary_search_by_key(&at, |site| site.native_offset as u64) else {
            halt(s, stop::UNKNOWN_ABORT)
        };
        let site = all[i];
        if spsr & 15 != 4 {
            halt(s, stop::SITE_MISMATCH)
        }
        // The syndrome must be the class this site's kind produces, before anything is taken or stepped (a malformed
        // abort stops here): BRK from AArch64 with the stub's immediate (1 guard, 2 split, 3 divide), else a data
        // abort from EL1 (access_aborted checks the rest).
        let expected = match site.kind {
            object::kind::GUARD => (0x3c, 1),
            object::kind::SPLIT => (0x3c, 2),
            object::kind::DIVIDE => (0x3c, 3),
            _ => (0x25, esr & 0xffff),
        };
        if (esr >> 26, esr & 0xffff) != expected {
            halt(s, stop::SITE_MISMATCH)
        }
        // Invalidations queued while `b` ran (a resident entry takes them): if they dropped `b`, it must not run on.
        let generation = s.generation;
        take_invalidations(&mut s);
        if b.flags & cache::STEP == 0 && (s.generation != generation || b.flags & cache::DEAD != 0) {
            step_from(s, f, b, &site)
        }
        match site.kind {
            object::kind::GUARD | object::kind::SPLIT | object::kind::DIVIDE => {
                match site.kind {
                    object::kind::GUARD => guest_fault(s, f, b, &site, noncanonical(&site)),
                    // #DE: no error code.
                    object::kind::DIVIDE => guest_fault(s, f, b, &site, [0, 0, 0]),
                    _ => split(s, f, b, all, &site, spsr),
                }
            }
            _ => access_aborted(s, f, b, &site, [esr, far, elr, spsr]),
        }
    }
}

/// A data abort at an access site: a shadow miss to retry, or a guest fault.
unsafe fn access_aborted(mut s: State, f: &NativeFrame, b: &Block, site: &Site, [esr, far, elr, spsr]: [u64; 4]) -> ! {
    let store = matches!(site.kind, object::kind::STORE | object::kind::STORE_PAIR | object::kind::PUSH);
    // A swap (memory XCHG) may report either direction (WnR is not defined strictly for atomics); it writes nothing.
    let swap = site.kind == object::kind::SWAP;
    let write_abort = (esr >> 6) & 1 != 0;
    let write = site.intent == object::INTENT_WRITE;
    let address = site_address(site, f);
    let width = u64::from(site.width);
    // A swap whose operand crosses 16 bytes takes an alignment fault (LSE2) before any effect: a split lock, not completed.
    if swap && esr & 63 == 0x21 && esr >> 26 == 0x25 && (address & 15) + width > 16 {
        bank_at(&mut s, f, b, site);
        halt(s, stop::UNSUPPORTED_ATOMIC)
    }
    // Data abort from EL1, translation or permission fault, direction, intent and FAR as recorded.
    if esr >> 26 != 0x25
        || (write_abort != store && !swap)
        || (write_abort && !write)
        || !matches!(esr & 63, 4..=7 | 12..=15)
        || far.wrapping_sub(address) >= width
    {
        halt(s, stop::SITE_MISMATCH)
    }
    // A non-canonical access that passed the guard (other than A[63] != A[47]: level-0 or gap faults).
    let Some(touched) = pages(address, width) else {
        guest_fault(s, f, b, site, noncanonical(site))
    };
    // A repeat of the last retry with its shadow untouched, and no shadow reset or flush since, would loop.
    // (Native PCs repeat legitimately: cached blocks run again.)
    if s.last_retry == [elr, far, tables::descriptor(far), s.shadow_generation] {
        halt(s, stop::NO_PROGRESS)
    }
    let access = if write { Access::Write } else { Access::Read };
    // Every page the access touches, in address order, before the retry. A contended walk resumes at once, outside
    // the no-progress test (the abort repeats until the walk wins; the watchdog contains a livelock).
    for va in touched.into_iter().flatten() {
        if translate(&mut s, f, b, site, va, access).is_none() {
            unsafe {
                write_record(record(), s);
                resume_exception(&raw const (*shared()).captured, elr, spsr)
            }
        }
    }
    s.last_retry = [elr, far, tables::descriptor(far), s.shadow_generation];
    s.retries += 1;
    unsafe {
        write_record(record(), s);
        resume_exception(&raw const (*shared()).captured, elr, spsr)
    }
}

/// `ACCESS_COMPLETED` for a page-crossing store (split stub): translate both pages
/// with write intent, then write the access's data register(s) through owned RAM in
/// guest byte order, apply its writeback and resume after it. A fault on either page
/// stops before any byte is written.
unsafe fn split(mut s: State, f: &NativeFrame, b: &Block, all: &[Site], stub: &Site, spsr: u64) -> ! {
    let Ok(i) = all.binary_search_by_key(&stub.guest_offset2, |site| site.native_offset) else {
        halt(s, stop::SITE_MISMATCH)
    };
    let access = all[i];
    let pair = access.kind == object::kind::STORE_PAIR;
    let width = access.width as usize;
    if !pair && width > 8 {
        halt(s, stop::SITE_MISMATCH)
    }
    let address = site_address(stub, f);
    let Some([Some(first), Some(second)]) = pages(address, width as u64) else {
        match pages(address, width as u64) {
            None => guest_fault(s, f, b, stub, noncanonical(stub)),
            Some(_) => halt(s, stop::SITE_MISMATCH), // the check fired for an access within one page
        }
    };
    // A contended walk resumes at the stub, which takes this path again.
    let gpa = [first, second].map(|va| translate(&mut s, f, b, stub, va, Access::Write));
    let [Some(gpa0), Some(gpa1)] = gpa else {
        unsafe {
            write_record(record(), s);
            resume_exception(&raw const (*shared()).captured, b.code_va() + stub.native_offset as u64, spsr)
        }
    };
    let gpa = [gpa0, gpa1];
    let reg = |r: u8| if r == 31 { 0 } else { f.x[r as usize] };
    let mut bytes = [0u8; 16];
    if pair {
        let half = width / 2;
        bytes[..half].copy_from_slice(&reg(access.data_reg[0]).to_le_bytes()[..half]);
        bytes[half..width].copy_from_slice(&reg(access.data_reg[1]).to_le_bytes()[..half]);
    } else {
        bytes[..width].copy_from_slice(&reg(access.data_reg[0]).to_le_bytes()[..width]);
    }
    let head = (4096 - (address & 4095)) as usize;
    let (Some(to0), Some(to1)) = (RAM.range(gpa[0], head as u64), RAM.range(gpa[1], (width - head) as u64)) else {
        halt(s, stop::SHADOW)
    };
    unsafe {
        guest_memory::copy_volatile(to0, bytes.as_ptr(), head);
        guest_memory::copy_volatile(to1, bytes[head..].as_ptr(), width - head);
        asm!("dsb ish");
        // A peer may have marked either page since its code bits were cleared (or found clear) in `translate`.
        cache::rechecked(gpa[0]);
        cache::rechecked(gpa[1]);
        // Writeback: the access's base becomes what the store would have left in it, derived from the
        // address checked at the stub (base + imm), never from the base register's value at the stub, which
        // need not hold the access's base yet. Pre-index (imm = delta): the address; post-index (imm = 0):
        // the address + delta.
        let resume = &raw mut (*shared()).captured;
        if access.writeback != object::WB_NONE {
            let base = address.wrapping_sub(access.imm as i64 as u64).wrapping_add(access.writeback_delta as i64 as u64);
            match access.base_reg {
                31 => (*resume).sp = base,
                n => (*resume).x[n as usize] = base,
            }
        }
        s.splits += 1;
        write_record(record(), s);
        resume_exception(resume, b.code_va() + access.native_offset as u64 + 4, spsr)
    }
}

/// Shadows linear page `va` with guest-physical `gpa` (a writable shadow's bit is already set, see [`translate`]); on
/// table capacity exhaustion evicts every shadow once and retries. The eviction clears the writable bits: the bit is
/// set again and the marks read again, and a page marked meanwhile is shadowed read-only (the access faults again and
/// takes the code-write path).
fn install(s: &mut State, va: u64, gpa: u64, writable: bool) {
    match tables::install(va, &RAM, gpa, writable) {
        Ok(()) => {}
        Err(shadow::Error::Capacity) => {
            reset_shadows(s);
            let writable = writable && {
                cache::shadowed_writable(gpa);
                !cache::translated(gpa)
            };
            s.evictions += 1;
            if tables::install(va, &RAM, gpa, writable).is_err() {
                halt(*s, stop::SHADOW)
            }
        }
        Err(_) => halt(*s, stop::SHADOW),
    }
}
