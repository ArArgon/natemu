//! Block execution in the resident (`natemu_abi::execution`): find the block
//! for the banked RIP in the code cache ([`cache`]) or walk its fetch, mark its
//! pages as code (shooting down peers' writable shadows of them), ask the host for
//! it and place it, enter it from the state bank, take exits the dispatcher
//! (`gates.S` `dispatch_template`, run from the code area) cannot resolve back into the bank, and serve
//! shadow misses at the recorded access sites of the block holding the faulting
//! PC by walking the guest tables and retrying the same instruction. A write to a
//! page holding translations drops that page's blocks first, or, when the running
//! block came from that page, runs the writing instruction alone in the slot
//! (self-modifying code, [`code_written`]). Literal exits
//! enter the linker (`svc #LINK`), which finds their target the same way
//! and links the exit to it. A miss at a system instruction (every block ends before one) gets the host's decode of
//! it instead of a block, which the resident keeps in the code cache like a block of those bytes (K2: decoded once per
//! translation, later runs make no host exit) and executes as a transaction ([`system`]). Also the 128/64 division
//! generated code asks for with `svc #DIV_U/DIV_S`. Generated code runs at EL0 and reaches the resident only by
//! exceptions (`svc`, check stubs, aborts, traps, interrupts); it reads and writes no emulator data, and the hardware
//! enforces it: every private page is EL1-only except the code area, which EL0 may only execute. Interrupts are doorbells (a host SPI, a
//! peer's SGI): each stops the vCPU precisely at a block boundary ([`interrupted`]) and the resident drains there.
use super::*;
use crate::cache::{self, Block};
mod delivery;
mod events;
mod helpers;
mod lapic;
mod mmio;
mod ports;
mod segments;
mod sse;
mod syscalls;
mod watch;
mod x87;
mod xsave;
use delivery::{Event, deliver};
use natemu_abi::{
    code_object::{self as object, Header, Site},
    cpu, cpu_profile,
    execution::*,
    guest_memory::{self, CodePages, CodeWrites},
    paging::{self, Access, Fault},
    shadow::{self, READ_ONLY},
};

unsafe extern "C" {
    fn enter_allocated(frame: *const NativeFrame, pc: u64, spsr: u64, alignment: u64) -> !;
    fn resume_exception(frame: *const NativeFrame, pc: u64, spsr: u64, alignment: u64) -> !;
    fn dispatch_template();
    fn dispatch_template_end();
}
/// The dispatcher's code (copied into the code area); its last word is its `svc #MISS`.
fn dispatcher() -> &'static [u8] {
    let (start, end) = (dispatch_template as *const u8, dispatch_template_end as *const u8);
    unsafe { core::slice::from_raw_parts(start, end as usize - start as usize) }
}

/// Guest-physical RAM `[0, R)` less the legacy hole (`LEGACY_HOLE`), through the resident's alias (M1: R is the VM's,
/// `Control::ram_bytes`, set by the host).
fn guest_ram() -> GuestRam {
    unsafe { natemu_abi::execution::guest_ram(cache::control().ram_bytes, GUEST_RAM_VA) }
}
/// EL0t, IRQs unmasked (the doorbell), D, A and F masked; NZCV is or-ed in. The resident itself runs with DAIF masked.
const BLOCK_SPSR: u64 = 0x340;
/// SPSR.M of generated code (EL0t): every exception the resident serves came from there.
fn from_el0(spsr: u64) -> bool {
    spsr & 0x1f == 0
}
/// The resident's private window (its pages are EL1-only, the code area EL0 execute-only; the guest-RAM alias included).
const WINDOW: core::ops::Range<u64> = PRIVATE..PRIVATE + PRIVATE_BYTES;

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

/// Stops this vCPU; the host reads `reason` and the record. The host may never resume it, so it first drops every
/// shadow and writable bit unasked (as if serving `SHADOWS`): no peer ever waits on a vCPU that has stopped.
fn halt(s: &State, reason: u64) -> ! {
    let mut s = *s; // the one copy, in this frame (callers pass a reference: the resident stack bound)
    served(&mut s);
    reset_shadows(&mut s);
    s.stop = reason;
    unsafe { write_record(record(), s) };
    stop(SUCCESS, reason)
}

pub unsafe fn start() -> ! {
    let mut s = unsafe { load() };
    if s.cpu.control().is_none() || s.request != NONE || s.vcpu >= s.vcpus || s.vcpus as usize > cache::MAX_VCPUS {
        // Before anything is shadowed or marked (and before the vCPU number is known to be valid).
        s.stop = stop::UNSUPPORTED_MODE;
        unsafe { write_record(record(), s) };
        stop(SUCCESS, s.stop)
    }
    // The dispatcher's miss is its last word, where L1 entries branch to.
    if dispatcher().len() + cache::DISPATCH != cache::DISPATCH_MISS + 4 {
        halt(&s, stop::UNSUPPORTED_MODE)
    }
    cache::reset();
    cache::fill_head(dispatcher());
    lapic::init();
    // The GIC CPU interface: system registers on, every priority unmasked, group 1 (the doorbells) enabled.
    unsafe {
        asm!("mrs {t}, S3_0_C12_C12_5", "orr {t}, {t}, #1", "msr S3_0_C12_C12_5, {t}", "isb", // ICC_SRE_EL1.SRE
             "mov {t}, #0xff", "msr S3_0_C4_C6_0, {t}",                                     // ICC_PMR_EL1
             "mov {t}, #1", "msr S3_0_C12_C12_7, {t}", "isb",                                // ICC_IGRPEN1_EL1
             t = out(reg) _);
    }
    // X4: an AP waits for the ACPI mailbox before it first runs.
    if s.mailbox != 0 {
        events::wait_mailbox(&mut s);
    }
    unsafe { dispatch(s) }
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
        // X3: the LAPIC timer's expiries due by now (its virtual-timer doorbell only stopped the vCPU).
        lapic::poll(s);
        // S4 (one test on the common path): a pending trap, TF or an armed breakpoint.
        let debugging = s.debug & debug::TRAP != 0 || s.cpu.rflags & RFLAGS_TF != 0 || s.cpu.dr[5] & DR7_ENABLES != 0;
        // S4: a trap-class #DB owed by the instruction that just completed comes first (Table 7-2 class 4).
        if debugging && trap_due(s) {
            continue;
        }
        // X2: the events this boundary may take; an event a shadow alone blocks waits for exactly one more instruction,
        // run alone (never cached or linked: the linker does not link to the step block).
        let held = s.debug & debug::TRAP; // (a data trap a MOV SS shadow holds)
        let shadowed = events::take(s);
        // S4: an event's frame store that met a data breakpoint: its trap comes before the handler's first instruction.
        if s.debug & debug::TRAP != held {
            continue;
        }
        // S4: an armed instruction breakpoint at RIP (Table 7-2 class 7).
        if debugging && code_break(s) {
            continue;
        }
        // S4: under TF the instruction runs alone and owes a single-step trap when it completes (SDM Vol.3 18.3.1.4).
        let traced = s.cpu.rflags & RFLAGS_TF != 0;
        // (A data breakpoint trap held by a MOV SS shadow waits for exactly one more instruction, SDM Vol.3 7.15 #DB.)
        if shadowed || traced || s.debug & debug::TRAP != 0 {
            s.debug = s.debug & !debug::STEP | if traced { debug::STEP } else { 0 };
            if traced {
                cache::control().debug_steps += 1;
            } else {
                s.shadow_steps += 1;
            }
            match unsafe { step_here(s) } {
                Some(block) => return Some(block),
                None => continue,
            }
        }
        let context = s.cpu.code_context();
        cache::select_l1(context);
        let block = match cache::lookup(s.cpu.rip, context, |b| mapped(s, b)) {
            // K2: a system instruction's decode, kept when it was translated: executed with no host exit.
            Some(b) if b.flags & cache::SYSTEM != 0 => {
                s.l2_hits += 1;
                match unsafe { executed(s, cache::system_of(b)) } {
                    Some(block) => return Some(block),
                    None => continue,
                }
            }
            Some(b) => {
                s.l2_hits += 1;
                b
            }
            None => match (s.generation, unsafe { request(s, REQUEST_TRANSLATE) }) {
                (missed, Reply::Block(h)) => {
                    s.requests += 1;
                    let Some(block) = cache::place(&h, STAGING_VA as *const u8, context, s.source_gpa, s.link_points, s.dispatch_exit, s.rep_exits) else {
                        // Full: flush (which also drops this vCPU's marks) and miss again; an empty cache holds any block.
                        if flushed {
                            halt(s, stop::BAD_REPLY)
                        }
                        cache::control().capacity_flushes += 1;
                        flush(s);
                        flushed = true;
                        continue;
                    };
                    if !kept(s, block, missed) {
                        return None;
                    }
                    block
                }
                // K2: the decode is kept as the translation of its bytes (a test driver's hooks are not instructions:
                // they are answered at every request), under the rule a block's placement keeps ([`cache::place_system`]).
                (missed, Reply::System(instruction)) => {
                    if !matches!(instruction.kind, system::SET_CPL | system::EVENT_HOOK) {
                        // Crossing into a second page: at least up to its first byte, so INVLPG of either page covers it
                        // (some kinds, e.g. #UD forms, do not use `length`).
                        let bytes = if s.source_gpa[1] == u64::MAX { instruction.length } else { instruction.length.max(4097 - (s.cpu.rip & 4095) as u32) };
                        let Some(record) = cache::place_system(s.cpu.rip, context, s.source_gpa, bytes, &instruction) else {
                            if flushed {
                                halt(s, stop::BAD_REPLY)
                            }
                            cache::control().capacity_flushes += 1;
                            flush(s);
                            flushed = true;
                            continue;
                        };
                        if !kept(s, record, missed) {
                            return None;
                        }
                    }
                    match unsafe { executed(s, instruction) } {
                        Some(block) => return Some(block),
                        None => continue,
                    }
                }
                (_, Reply::Fault(fault)) => {
                    deliver(s, Event::Exception(fault));
                    continue;
                }
            },
        };
        // S4: a block at an armed instruction breakpoint is entered only from here, after the check above.
        if !at_breakpoint(&s.cpu, block) {
            cache::fill_l1(s.cpu.rip, block);
        }
        return Some(block);
    }
}
/// S4: whether `b` starts at an armed instruction breakpoint: it is never linked to nor put in L1 (every entry passes
/// [`find`]'s check).
fn at_breakpoint(c: &Cpu, b: &Block) -> bool {
    c.dr[5] & DR7_ENABLES != 0 && c.code_breakpoints().any(|a| a == b.rip)
}
/// S4: delivers the trap-class #DB pending at this boundary (`State::debug_trap`), unless a MOV SS shadow holds, which
/// discards a single-step trap (delivery_model A9). True when delivered.
#[cold]
#[inline(never)]
fn trap_due(s: &mut State) -> bool {
    let hits = s.debug & debug::TRAP;
    if hits == 0 {
        return false;
    }
    if s.cpu.mov_ss != 0 {
        s.debug &= !DR6_BS;
        return false;
    }
    cache::control().debug_traps += 1;
    s.debug &= !debug::TRAP;
    let mid_string = s.mid_string != 0;
    debug_exception(s, hits, Event::Debug { length: 0, mid_string });
    true
}
/// S4: an armed instruction breakpoint at RIP is a #DB fault before the instruction, unless RF is set or a MOV SS shadow
/// suppresses it (delivery_model A9), or the boundary is between two iterations of a REP string; its frame holds the
/// current RF, 0 (Vol.3 18.3.1.1). True when delivered.
#[cold]
#[inline(never)]
fn code_break(s: &mut State) -> bool {
    // Never between two iterations of a REP string: the instruction has begun (its start was checked, or a MOV SS shadow
    // suppressed the check there; RF may still be 0 after such a suppression or at a port string's element).
    if s.cpu.mov_ss != 0 || s.mid_string != 0 || s.cpu.rflags & RFLAGS_RF != 0 || !s.cpu.code_breakpoints().any(|b| b == s.cpu.rip) {
        return false;
    }
    cache::control().code_breaks += 1;
    debug_exception(s, 0, Event::Debug { length: 0, mid_string: false });
    true
}
/// S4: delivers a #DB (`event`) after setting DR6: `hits` (BS) or-ed in, B0..B3 replaced by the breakpoints whose
/// condition RIP meets (enabled or not: the SDM lets the processor report disabled ones, Vol.3 18.2.3, and clear B0..B3
/// at a #DB; pinned QEMU does both). BD, BS and BT stay as they were (sticky).
fn debug_exception(s: &mut State, hits: u64, event: Event) {
    let matched = (0..4).filter(|&n| dr7_rw(s.cpu.dr[5], n) == 0 && s.cpu.dr[n] == s.cpu.rip).fold(0, |m, n| m | 1 << n);
    s.cpu.dr[4] = s.cpu.dr[4] & !0xf | matched | hits;
    deliver(s, event);
}

/// Whether `b`, just placed for a request made at cache generation `missed`, may stay: invalidations are taken; a
/// flush meanwhile (`false`: it is gone), a write that raced its fetch (dead), or a source page whose mark this vCPU lost
/// while it was fetched discards it (`false`: the caller steps the instruction from fresh bytes; see [`find`]).
fn kept(s: &mut State, b: &Block, missed: u64) -> bool {
    let placed = s.generation;
    take_invalidations(s);
    if s.generation != placed {
        return false; // flushed: the block is gone
    }
    let unmarked = |g: u64| g != u64::MAX && !cache::marked(g);
    if placed != missed || b.flags & cache::DEAD != 0 || b.source_gpa.iter().any(|&g| unmarked(g)) {
        if b.flags & cache::DEAD == 0 && cache::discard(b).is_none() {
            halt(s, stop::BAD_LINK)
        }
        return false;
    }
    true
}
/// Executes the decoded system instruction `i` at `s.cpu.rip` ([`system`]); a BT* form found aligned under alignment
/// checking (X2) then runs alone, compiled without the cut: its step block.
unsafe fn executed(s: &mut State, i: System) -> Option<&'static Block> {
    system(s, i);
    if s.plain_step != 0 { unsafe { step_here(s) } } else { None }
}
/// K2: the decode kept for the system instruction at `s.cpu.rip` in this context, unless the instruction must be compiled
/// plain (`State::plain_step`).
fn kept_system(s: &mut State) -> Option<System> {
    let b = cache::lookup(s.cpu.rip, s.cpu.code_context(), |b| mapped(s, b)).filter(|b| b.flags & cache::SYSTEM != 0)?;
    if s.plain_step != 0 {
        return None;
    }
    Some(cache::system_of(b))
}

/// The instruction at `s.cpu.rip` alone, placed in the step slot (X2: after a shadow-setting instruction with an event
/// pending, or a BT* form under alignment checking, `State::plain_step`, which this request consumes); `None` when it was
/// a system instruction (executed) or its fetch faulted (delivered): the caller looks at the next boundary.
unsafe fn step_here(s: &mut State) -> Option<&'static Block> {
    // A system instruction whose decode is kept runs from it (K2): nothing to compile.
    if let Some(i) = kept_system(s) {
        system(s, i);
        if s.plain_step == 0 {
            return None;
        }
    }
    loop {
        let reply = unsafe { request(s, REQUEST_STEP) };
        s.plain_step = 0;
        return match reply {
            Reply::Block(h) => {
                let context = s.cpu.code_context();
                let placed = cache::place_step(&h, STAGING_VA as *const u8, context, s.source_gpa, s.link_points, s.dispatch_exit, s.rep_exits);
                Some(placed.unwrap_or_else(|| halt(s, stop::BAD_REPLY)))
            }
            Reply::System(instruction) => {
                system(s, instruction);
                // A BT* form found aligned (X2) runs at once, compiled plain: no boundary lies between its check and it.
                if s.plain_step != 0 {
                    continue;
                }
                None
            }
            Reply::Fault(fault) => {
                deliver(s, Event::Exception(fault));
                None
            }
        };
    }
}

/// A literal exit (`svc #LINK` with the target RIP in x0, FEX's `ExitFunctionLink`), entered with the exit's state as
/// at a dispatcher exit. The `svc` names the block that ran it (ELR is the word after it): a live cache block with a
/// link point to the RIP, or the step block; anything else halts. The target is
/// found as by the dispatcher; the exit is linked to it unless the cache was flushed meanwhile (the source may be
/// gone: FEX re-checks the same way) or the link records are full, then the target is entered. A block run from the
/// single-step slot is never linked. A target killed as it was placed ([`find`]) is stepped, not linked (`stale_links`).
#[inline(never)] // its frame stays out of `returned` (the nested-entry stack bound)
unsafe fn linker(frame: &NativeFrame, elr: u64) -> ! {
    unsafe {
        let mut s = load();
        bank(&mut s.cpu, frame, frame.nzcv, true);
        s.cpu.rip = frame.x[0];
        s.linker += 1;
        // A REP string's self-exit (X2): the boundary is between two of its iterations.
        if let Some(b) = cache::block_at(elr.wrapping_sub(4)).filter(|b| link_point_of(b, elr.wrapping_sub(4)).is_some_and(|o| b.rep_exit(o))) {
            rep_exited(&mut s, b);
        }
        completed(&mut s);
        // The RIP must be one of the block's link targets (its `svc #LINK` follows that target's move).
        let target = |b: &Block| b.links.iter().any(|p| p.offset != 0 && p.target == s.cpu.rip);
        let source = match cache::block_at(elr.wrapping_sub(4)) {
            Some(b) if b.flags & cache::STEP != 0 && target(b) => None,
            Some(b) if b.flags & cache::DEAD == 0 && target(b) => Some(b),
            _ => {
                s.native = [0, 0, elr];
                halt(&s, stop::BAD_LINK)
            }
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
            // An instruction run alone to end a shadow (X2): never linked to.
            Some(_) if target.flags & cache::STEP != 0 => {}
            // S4: nor a block at an armed instruction breakpoint.
            Some(_) if at_breakpoint(&s.cpu, target) => {}
            Some(source) => match cache::link(source, target) {
                cache::Link::Linked(n) => s.links += n,
                cache::Link::Full => s.full_links += 1,
                cache::Link::Bad => halt(&s, stop::BAD_LINK),
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
    /// The instruction fetch at `s.cpu.rip` faults ([`REPLY_FETCH_FAULT`]): the exception to deliver.
    Fault([u64; 3]),
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
        // Walked at RIP itself: a fault's CR2 is the fetched address, not its page.
        fetch_page(s, s.cpu.rip, 0, block);
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
            halt(s, stop::BAD_REPLY)
        }
        fetch_page(s, page.wrapping_add(4096), 1, block);
    }
    if s.reply == REPLY_PARK {
        halt(s, stop::PARKED)
    }
    if s.reply == REPLY_FETCH_FAULT {
        s.request = NONE;
        return Reply::Fault(match fetch::decode(s.fetch_fault) {
            Some(Fault::Page { address, error }) => [14, error, address],
            Some(Fault::Noncanonical) => [13, 0, 0],
            _ => halt(s, stop::BAD_REPLY),
        });
    }
    // (A step request may be answered with a decode too: X2's step after a shadow-setting instruction.)
    if s.reply == REPLY_SYSTEM {
        if !s.system.well_formed() {
            halt(s, stop::BAD_REPLY)
        }
        // The pages its bytes came from (the next one only when the decode needed it, [`REPLY_NEXT_PAGE`]).
        s.source_gpa = s.fetch;
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
        // Host-supplied link points name words the linker may rewrite: each whole exit inside the code, as admitted (a
        // move of the point's target into x0 starting with the point's word, `svc #LINK` right after it).
        || s.link_points.iter().any(|p| p.offset != 0 && !link_point(&h, p))
        // The placeholder the resident relocates ([`cache::place`]).
        || (s.dispatch_exit != 0
            && (s.dispatch_exit % 4 != 0
                || s.dispatch_exit + 4 > u64::from(h.code_size)
                || staged_word(&h, s.dispatch_exit) != DISPATCH_PLACEHOLDER))
    {
        halt(s, stop::BAD_REPLY)
    }
    s.request = NONE;
    Reply::Block(h)
}
/// The staged block's code word at `offset` (inside its code).
fn staged_word(h: &Header, offset: u64) -> u32 {
    unsafe { ((STAGING_VA + u64::from(h.code_offset) + offset) as *const u32).read_unaligned() }
}
/// `p` names an exit of the staged block as admission returns it: its word is in the code and is `p.word`, the first
/// of a move (`movz|movn|orr` from the zero register, then `movk`s; at most four words) building `p.target` in x0, and
/// an `svc #LINK` follows the move (inside the code).
fn link_point(h: &Header, p: &LinkPoint) -> bool {
    let at = u64::from(p.offset);
    let svc_link = 0xd400_0001 | u32::from(svc::LINK) << 5;
    if at % 4 != 0 || at < u64::from(ENTRY_OFFSET) || at + 8 > u64::from(h.code_size) || staged_word(h, at) != p.word {
        return false;
    }
    let mut words = [0u32; 5];
    let n = ((u64::from(h.code_size) - at) / 4).min(5) as usize;
    for (k, w) in words[..n].iter_mut().enumerate() {
        *w = staged_word(h, at + 4 * k as u64);
    }
    matches!(move_value(&words[..n], 0), Some((k, value)) if k <= 4 && k < n && words[k] == svc_link && value == p.target)
}
/// Walks the fetch of `va` (its page) for a request into `s.fetch[i]` (or its failure into `s.fetch_fault`); for a block,
/// marks it ([`mark`]).
fn fetch_page(s: &mut State, va: u64, i: usize, block: bool) {
    match fetch_walk(s, va) {
        Ok((gpa, _)) => {
            s.fetch[i] = gpa;
            if block {
                mark(s, gpa);
            }
        }
        Err(fault) => s.fetch_fault = fault,
    }
}
/// The guest-physical page `va` is fetched from and whether its leaf is global (the walk sets A bits like the CPU's), or
/// why not ([`fetch`]). A walk
/// losing its A/D races to peers walks again, as x86 does (the watchdog contains a livelock). No block of this vCPU runs
/// now (a step's source block is abandoned, and the linker checks that its source is not dead after finding the
/// target), so an A write into a page holding translations just invalidates it ([`cache::written`]).
fn fetch_walk(s: &mut State, va: u64) -> Result<(u64, bool), [u64; 3]> {
    let Some(control) = s.cpu.control() else {
        halt(s, stop::UNSUPPORTED_MODE)
    };
    loop {
        let mut tables = GuestTables::observed(guest_ram(), CodeWrites(FetchWrites(s)));
        match paging::walk(&mut tables, control, va, Access::Fetch) {
            Ok(t) if t.wb && guest_ram().range(t.gpa & !4095, 4096).is_some() => return Ok((t.gpa & !4095, t.global)),
            Ok(_) => return Err([fetch::NOT_RAM, 0, 0]),
            Err(Fault::Contended) => {}
            Err(f) => return Err(fetch::encode(f)),
        }
    }
}
/// K1: whether `b`'s source pages are where its linear pages are fetched from now (a fetch walk of each, as the CPU's
/// after a TLB miss: A bits set, any fault meaning no): `Some(global)`, global when every leaf is and CR4.PGE is set.
/// The walk's A writes may invalidate `b` ([`cache::lookup`] looks again).
fn mapped(s: &mut State, b: &Block) -> Option<bool> {
    let mut global = s.cpu.cr4 & CR4_PGE != 0;
    for (k, &gpa) in b.source_gpa.iter().enumerate() {
        if gpa != u64::MAX {
            let (_, g) = fetch_walk(s, (b.rip & !4095).wrapping_add(4096 * k as u64)).ok().filter(|&(at, _)| at == gpa)?;
            global &= g;
        }
    }
    Some(global)
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
            None => halt(s, stop::BAD_LINK),
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
    let Some(pending) = cache::drain() else { halt(s, stop::BAD_LINK) };
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
        _ => halt(s, stop::BAD_LINK),
    }
    cache::reset();
    s.generation += 1;
    s.shadow_generation += 1;
}
/// K1: a TLB flush (a CR3 load; `global`: a paging control change or an MTRR write, global entries too): every shadow
/// goes (the data TLB) and blocks no longer current leave L1 and their links ([`cache::tlb`]); no translation is dropped.
fn tlb(s: &mut State, global: bool) {
    let t0 = ticks();
    match cache::tlb(global) {
        Some(n) => s.unlinks += n,
        None => halt(s, stop::BAD_LINK),
    }
    reset_shadows(s);
    cache::control().tlb_ticks += ticks() - t0;
}
fn ticks() -> u64 {
    let t: u64;
    unsafe { asm!("isb", "mrs {0}, cntvct_el0", out(reg) t) };
    t
}
/// K1: a #PF invalidates the TLB entries of its linear address (SDM Vol.3 4.10.4.1): the blocks with bytes on `cr2`'s
/// page stop being current (its shadow, if any, is the faulting access's miss: none to drop).
pub(super) fn page_faulted(s: &mut State, cr2: u64) {
    let t0 = ticks();
    match cache::invalidate_linear(cr2 & !4095, 4096) {
        Some(n) => s.unlinks += n,
        None => halt(s, stop::BAD_LINK),
    }
    let c = cache::control();
    c.page_faults += 1;
    c.page_fault_ticks += ticks() - t0;
}
/// Removes every shadow of this vCPU (both spaces) and its writable bits, and counts the reset.
fn reset_shadows(s: &mut State) {
    tables::reset();
    cache::shadows_reset();
    s.shadow_generation += 1;
}

/// Enters `block` from the bank: GPRs and PF/AF in their static registers, NZCV with C = !CF, the guest-state page in
/// x28 with its flag bytes (TF, IF, DF, IOPL, NT, AC, VIF, VIP, ID; RF and VM as 0) and the FS/GS bases (FEX's
/// `fs_cached`/`gs_cached`, which every FS-/GS-prefixed access adds) written from the bank. The bases are rewritten at
/// every entry and never read back, so the bank stays authoritative: a guest store into the page (the documented
/// deviation) changes only what that vCPU's own prefixed accesses use until the next `enter`.
unsafe fn enter(mut s: State, block: &Block) -> ! {
    served(&mut s);
    s.mid_string = 0; // the next resident entry is at another boundary
    // X2: a shadow still set here covers the block's first instruction with no event pending (else `find` stepped it).
    // Every later resident entry follows a completed instruction but a stop at this block's first word, which arms
    // instead ([`interrupted`]): the shadow ends here, exactly.
    let shadowed = s.cpu.mov_ss != 0 || s.sti_shadow != 0;
    s.shadow_entry = if shadowed { block.entry() } else { 0 };
    (s.cpu.mov_ss, s.sti_shadow) = (0, 0);
    for (at, value) in frame::flag_bytes(s.cpu.rflags) {
        // SAFETY: the frame page is mapped EL1 read/write at FRAME_VA; `at` lies inside it.
        unsafe { (FRAME_VA as *mut u8).add(at as usize).write_volatile(value) };
    }
    for (at, base) in [(frame::FS_BASE, s.cpu.fs_base), (frame::GS_BASE, s.cpu.gs_base)] {
        // SAFETY: as above; the fields are 8-byte aligned (abi `guest_state_page_layout`).
        unsafe { (FRAME_VA as *mut u64).byte_add(at as usize).write_volatile(base) };
    }
    // I6: FCW, which FNSTCW reads there; like the bases, never read back.
    unsafe { (FRAME_VA as *mut u16).byte_add(frame::X87_FCW as usize).write_volatile(s.cpu.fcw as u16) };
    let mut f = NativeFrame::ZERO;
    for (&reg, &value) in SRA.iter().zip(&s.cpu.gpr) {
        f.x[reg as usize] = value;
    }
    let (nzcv, pf, af) = native_flags(s.cpu.rflags);
    f.x[SRA[16] as usize] = pf;
    f.x[SRA[17] as usize] = af;
    f.x[28] = FRAME_POINTER;
    f.sp = STACK_TOP;
    sse::fill(&mut f, &s.cpu);
    s.blocks += 1;
    unsafe {
        write_record(record(), s);
        let entry = &raw mut (*shared()).initial;
        write_record(entry, f);
        tables::select(s.cpu.cpl);
        enter_allocated(entry, block.entry(), BLOCK_SPSR | nzcv, alignment_check(&s.cpu))
    }
}

/// Exception capture (`kind` 1: an `svc` entry, a check stub, a stop, an abort or a trap from generated code) and the
/// IRQ gate (3) end here. An exception from EL1 (the resident's own fault) halts at once (`unknown_abort`). Any stop
/// armed is disarmed first ([`cache::disarm`]). The `svc` entries come from EL0: a literal exit (the linker), the
/// dispatcher's miss (only from its own `svc`), and a division; any other `svc` is an unknown abort.
pub unsafe fn returned(frame: &NativeFrame, kind: u64) -> ! {
    unsafe {
        let (esr, far, elr, spsr): (u64, u64, u64, u64);
        asm!("mrs {0}, esr_el1", "mrs {1}, far_el1", "mrs {2}, elr_el1", "mrs {3}, spsr_el1",
             out(reg) esr, out(reg) far, out(reg) elr, out(reg) spsr);
        if !from_el0(spsr) {
            resident_fault([esr, far, elr])
        }
        let Some(armed) = cache::disarm() else { halt_native([0, 0, frame.x[30]], stop::BAD_STOP) };
        // S4: a watched access's software step completed; any other entry abandons a step in flight.
        if esr >> 26 == 0x32 && kind != 3 {
            watch::stepped(elr, spsr)
        }
        let spsr = watch::cancel(spsr);
        if kind == 3 {
            interrupted(frame)
        }
        if esr >> 26 == 0x3c && esr & 0xffff == u64::from(STOP_IMM) {
            stopped(frame, elr, spsr, armed)
        }
        if esr >> 26 != 0x15 {
            aborted(frame)
        }
        let miss = cache::CODE_VA + cache::DISPATCH_MISS as u64 + 4;
        match (esr & 0xffff) as u16 {
            svc::LINK => linker(frame, elr),
            svc::MISS if elr == miss => missed(frame),
            imm @ (svc::DIV_U | svc::DIV_S) => division(imm == svc::DIV_S, frame, elr, spsr),
            svc::X87 => helpers::x87(frame, elr, spsr),
            svc::STRING => helpers::string(frame, elr, spsr),
            _ => {}
        }
        halt_native([esr, 0, elr], stop::UNKNOWN_ABORT)
    }
}
/// A block exit to the dispatcher's miss: guest state is in the static registers, NZCV (CF inverted) and PF/AF registers.
/// Not inlined, as [`halt_native`]: `returned` holds no state copy (the nested-entry stack bound).
#[inline(never)]
unsafe fn missed(frame: &NativeFrame) -> ! {
    let mut s = unsafe { load() };
    bank(&mut s.cpu, frame, frame.nzcv, true);
    completed(&mut s);
    s.cpu.rip = frame.x[0];
    unsafe { dispatch(s) }
}
/// Halts with `native` in the record.
#[inline(never)]
fn halt_native(native: [u64; 3], reason: u64) -> ! {
    let mut s = unsafe { load() };
    s.native = native;
    halt(&s, reason)
}

/// An exception taken from EL1: the resident's own fault (`native` = ESR, FAR, ELR), never a guest's.
#[inline(never)]
fn resident_fault(native: [u64; 3]) -> ! {
    let mut s = unsafe { load() };
    s.native = native;
    halt(&s, stop::UNKNOWN_ABORT)
}

/// The 128/64 division (`svc #DIV_U/DIV_S` from a block, FEX's LUDIV/LDIV): x0 high, x1 low, x2 divisor in; x0
/// quotient, x1 remainder and x2 = 0 out, or x2 = 1 for #DE (the block then branches to its divide stub). Nothing else
/// changes; the block resumes after the `svc`.
#[inline(never)] // its frame stays out of `returned` (the nested-entry stack bound)
unsafe fn division(signed: bool, f: &NativeFrame, elr: u64, spsr: u64) -> ! {
    unsafe {
        let s = load();
        if cache::block_at(elr.wrapping_sub(4)).is_none() {
            let mut s = s;
            s.native = [0, 0, elr];
            halt(&s, stop::UNKNOWN_ABORT)
        }
        let resume = &raw mut (*shared()).captured;
        let (quotient, remainder, de) = match divide(signed, f.x[0], f.x[1], f.x[2]) {
            Some((q, r)) => (q, r, 0),
            None => (f.x[0], f.x[1], 1),
        };
        ((*resume).x[0], (*resume).x[1], (*resume).x[2]) = (quotient, remainder, de);
        self::resume(&s, resume, elr, spsr)
    }
}

/// Resumes generated code mid-block at `pc` (a division, a retried access, a split store): a stop still wanted is
/// armed again in the block holding `pc` (the entry disarmed it).
unsafe fn resume(s: &State, frame: *const NativeFrame, pc: u64, spsr: u64) -> ! {
    if s.stop_wanted != 0 {
        if !cache::block_at(pc).is_some_and(cache::arm) {
            halt(s, stop::BAD_STOP)
        }
        cache::control().rearms += 1;
    }
    tables::select(s.cpu.cpl);
    unsafe { resume_exception(frame, pc, spsr, alignment_check(&s.cpu)) }
}
/// A wanted stop is served by a resident entry at a block boundary (the resident drained or will drain before the
/// next block runs; [`enter`], [`halt`]).
fn served(s: &mut State) {
    if s.stop_wanted != 0 {
        s.stop_wanted = 0;
        s.stops_resident += 1;
        taken(s);
    }
}
fn taken(s: &mut State) {
    s.stops += 1;
    unsafe { asm!("mrs {0}, cntvct_el0", out(reg) s.stop_ticks) };
}

/// A doorbell (the IRQ gate, `gates.S` `irq_gate`; the resident runs with interrupts masked, so only generated code at
/// EL0 is interrupted, at an instruction boundary with ELR = the next instruction): acknowledged and ended at once (one
/// INTID per IRQ; more pending ones are taken in turn). Interrupted in the dispatcher or L1 code (the next RIP in x0,
/// guest state in the static registers: they write only x1, x2) or at a block's first word (its entry state), the vCPU
/// stops there at once. Inside a block, the stop is armed ([`cache::arm`]): the block runs to an exit, which traps
/// ([`stopped`]), or into the resident, which serves the stop at its next block boundary. Anything else halts.
#[inline(never)] // its frame stays out of `returned` (the nested-entry stack bound)
unsafe fn interrupted(frame: &NativeFrame) -> ! {
    let (intid, t0, elr, spsr): (u64, u64, u64, u64);
    unsafe {
        asm!("isb", "mrs {t0}, cntvct_el0", "mrs {i}, S3_0_C12_C12_0", "mrs {elr}, elr_el1", "mrs {spsr}, spsr_el1", // ICC_IAR1_EL1
             i = out(reg) intid, t0 = out(reg) t0, elr = out(reg) elr, spsr = out(reg) spsr);
    }
    let mut s = unsafe { load() };
    // A spurious INTID (1020-1023) is neither ended nor counted: generated code resumes (a stop still wanted is armed again).
    if intid >= 1020 {
        unsafe { resume(&s, &raw const (*shared()).captured, elr, spsr) }
    }
    if intid == lapic::TIMER_PPI {
        lapic::timer_irq();
    }
    let t1: u64;
    unsafe { asm!("msr S3_0_C12_C12_1, {i}", "isb", "mrs {t1}, cntvct_el0", i = in(reg) intid, t1 = out(reg) t1) }; // ICC_EOIR1_EL1
    let c = cache::control();
    c.irqs += 1;
    c.irq_sgis += u64::from(intid < 16);
    c.irq_ticks += t1 - t0;
    let bad = |mut s: State| -> ! {
        s.native = [intid, 0, elr];
        halt(&s, stop::BAD_STOP)
    };
    if !from_el0(spsr) {
        bad(s)
    }
    let at_once = |mut s: State, rip: u64| -> State {
        bank(&mut s.cpu, frame, frame.nzcv, true);
        s.cpu.rip = rip;
        s.stop_wanted = 0;
        taken(&mut s);
        s
    };
    if (cache::DISPATCH_VA..cache::CODE_VA + cache::HEAD_BYTES as u64).contains(&elr) {
        let mut s = at_once(s, frame.x[0]);
        completed(&mut s); // a block exited into the dispatcher
        if elr < cache::L1_VA {
            s.stops_dispatch += 1;
        } else {
            s.stops_l1 += 1;
        }
        unsafe { dispatch(s) }
    }
    let Some(b) = cache::block_at(elr).filter(|b| b.flags & cache::DEAD == 0) else { bad(s) };
    s.irqs_step += u64::from(b.flags & cache::STEP != 0);
    // At a block's first word the vCPU stops at once, except in a block that starts with a REP string (X2): it may have
    // been entered by its own self-link between iterations, so the stop is armed and taken at an exit, where the kind of
    // boundary is known.
    // Likewise at the first word of the block entered right after a shadow-setting instruction (`State::shadow_entry`):
    // the shadow was cleared at that entry, so the event is taken after at least one more instruction.
    if elr == b.entry() && (b.flags & cache::STEP != 0 || (!b.starts_with_rep() && s.shadow_entry != elr)) {
        let mut s = at_once(s, b.rip);
        s.stops_entry += 1;
        // The step block is entered again (see [`stopped`]).
        if b.flags & cache::STEP != 0 {
            take_invalidations(&mut s);
            unsafe { enter(s, b) }
        }
        unsafe { dispatch(s) }
    }
    if s.stop_wanted != 0 {
        s.merged += 1;
    } else {
        s.arms += 1;
        s.stop_wanted = 1;
    }
    if !cache::arm(b) {
        bad(s)
    }
    tables::select(s.cpu.cpl);
    unsafe {
        write_record(record(), s);
        resume_exception(&raw const (*shared()).captured, elr, spsr, alignment_check(&s.cpu))
    }
}
/// x86 alignment checking is on (SDM Vol.3 7.15 #AC: CR0.AM, RFLAGS.AC and CPL 3), as a flag for the gates: generated
/// code then runs with SCTLR_EL1.A, so a misaligned data access at any site takes an alignment fault before any
/// translation ([`access_aborted`] makes it #AC(0)); the resident's own explicit accesses at CPL 3 check it in
/// `segments::linear`.
fn alignment_check(c: &Cpu) -> u64 {
    u64::from(c.alignment_check())
}

/// A precise stop at an armed exit: a [`STOP_WORD`] trap at one of the words [`cache::arm`] patched (`armed`, already
/// restored), from EL0, in a live block or the step block. The state is the exit's (static registers, NZCV with CF
/// inverted); RIP is the link point's target, or x0 at the computed exit's `b`. Drains and re-dispatches. Anything else
/// halts: a stop word nobody armed is not a stop.
#[inline(never)] // its frame stays out of `returned` (the nested-entry stack bound)
unsafe fn stopped(frame: &NativeFrame, elr: u64, spsr: u64, (armed, n): ([u32; MAX_LINK_POINTS + 1], usize)) -> ! {
    let mut s = unsafe { load() };
    let at = elr.wrapping_sub(cache::CODE_VA);
    let block = cache::block_at(elr).filter(|b| b.flags & cache::DEAD == 0 && s.stop_wanted != 0 && from_el0(spsr));
    let rip = block.filter(|_| armed[..n].iter().any(|&o| u64::from(o) == at)).and_then(|b| {
        let offset = at - u64::from(b.code);
        match b.links.iter().find(|p| p.offset != 0 && u64::from(p.offset) == offset) {
            Some(p) => Some(p.target),
            None => (b.dispatch_exit != 0 && offset == u64::from(b.dispatch_exit)).then_some(frame.x[0]),
        }
    });
    let Some(rip) = rip else {
        s.native = [0, 0, elr];
        halt(&s, stop::BAD_STOP)
    };
    bank(&mut s.cpu, frame, frame.nzcv, true);
    s.cpu.rip = rip;
    // At a REP string's self-exit (X2): between two of its iterations.
    let offset = at.wrapping_sub(block.map_or(0, |b| u64::from(b.code)));
    if let Some(b) = block.filter(|b| b.rep_exit(offset)) {
        rep_exited(&mut s, b);
    }
    completed(&mut s);
    s.stop_wanted = 0;
    s.stops_exit += 1;
    taken(&mut s);
    unsafe { dispatch(s) }
}

/// CR0 bits (SDM vol. 3 §2.5): those defined; the others in 31:0 are reserved and ignored on a load (they stay clear),
/// those in 63:32 raise #GP. Every defined bit is accepted (PG, PE are fixed in 64-bit code: clearing either raises
/// #GP; ET is hard-wired). WP is the walker's. MP, EM, TS, NE are recorded: they gate x87/MMX/SSE forms (#NM with TS
/// or EM, #UD for SSE with EM; NE selects native x87 error reporting); MP, EM, TS are in a block's context
/// ([`Cpu::code_context`]), so a block never runs under other gating than it was made under (K1). CD and NW are recorded: RAM is write-back whatever they say (memory types are M2's). AM with
/// RFLAGS.AC at CPL 3 turns alignment checking on (X2, [`alignment_check`]); translations depend on it only through the
/// BT* cut, and it is part of [`Cpu::code_context`].
const CR0_DEFINED: u64 = cpu_profile::CR0_VALID;
const CR0_CD: u64 = 1 << 30;
const CR0_NW: u64 = 1 << 29;
/// CR4: a bit outside the presented CPU's valid set (`cpu_profile::CR4_VALID`: its CPUID feature is advertised, SDM
/// Vol.3 2.5) is reserved (#GP); every valid bit is accepted. PAE is fixed in long mode; PGE, SMEP are the walker's; PSE
/// has no effect with 4-level paging (SDM Vol.3 4.1); TSD makes RDTSC/RDTSCP #GP at CPL > 0, PCE allows RDPMC there
/// (which then #GPs for want of counters), DE makes DR4/DR5 #UD. OSFXSR, OSXMMEXCPT, OSXSAVE (CPUID.1:ECX.OSXSAVE
/// follows it) gate SSE and XSAVE forms (I5); FSGSBASE gates RDFSBASE..WRGSBASE ([`syscalls`]).
const CR4_TSD: u64 = 1 << 2;
/// K1: the CR4 bits a walk depends on (a change flushes the TLBs, global entries too; SDM Vol.3 4.10.4.1).
const CR4_PAGING: u64 = CR4_PGE | CR4_PAE | CR4_SMEP | CR4_SMAP | CR4_PCIDE | CR4_LA57;
const UD: [u64; 3] = [6, 0, 0];
const DB: [u64; 3] = [1, 0, 0];
const GP: [u64; 3] = [13, 0, 0];

/// Executes the system instruction at `s.cpu.rip` (`i`, the host's decode, checked by [`request`]) as a transaction
/// with x86 semantics in 64-bit mode: its checks raise their exception (#UD, then #GP) with the state unchanged;
/// otherwise its effect commits, RIP moves past it and RFLAGS stay. K1: a CR3 load, a change of a paging control bit
/// (CR0.PG/WP, CR4's [`CR4_PAGING`], EFER.NXE/LME/LMA) and an MTRR write are TLB flushes ([`tlb`]): this vCPU's shadows
/// (both privilege spaces, its data TLB) go, and its blocks (its instruction TLB entries) stop being current, but for
/// global ones at a CR3 load; a block is run again only after a fetch walk finds its source pages where its RIP is
/// fetched from ([`cache::lookup`]), so no translation is dropped. Other control bits change no walk; those a
/// translation depends on are in its context ([`Cpu::code_context`]). INVLPG drops the shadows (both spaces) of the 2
/// MiB-aligned range holding the address (a superset of its page in either page size) and makes the blocks with bytes
/// there not current. Only this vCPU's state, cache and shadows change. Forms and operand values the resident does not
/// implement stop it (`unsupported_system`) before any effect. A test driver's [`system::SET_CPL`] changes the CPL
/// ([`set_cpl`]) at any CPL. CPUID, RDMSR/WRMSR, RDTSC and RDTSCP answer from the presented CPU ([`natemu_abi::cpu`]).
/// CPUID is serialising: like every system instruction it is a draining entry, since [`find`] drains this vCPU's
/// invalidations before the request and again before the next instruction is looked up.
/// An exception a check raises is delivered through the IDT ([`deliver`], X1a) from the state at the instruction (DR6.BD
/// set for a general-detect #DB); INT n and INT3 are delivered as software interrupts, INTO (64-bit mode) and UD0/1/2
/// raise #UD. IRET releases NMI blocking once past the LOCK #UD check, even when it then faults (SDM Vol.3 7.7.2).
fn system(s: &mut State, i: System) {
    s.systems += 1;
    // A shadow covers this instruction and ends with it (X2: MOV SS and STI set theirs again in the transaction); the
    // boundary is not between REP iterations. A BT* form under alignment checking does not complete here (it is stepped).
    if i.kind != system::BT_ALIGNED {
        (s.cpu.mov_ss, s.sti_shadow, s.mid_string) = (0, 0, 0);
    }
    if let Err(e) = transaction(s, i) {
        deliver(s, Event::Exception(e))
    }
}
/// [`system`]'s checks and effect: `Err` with the exception due at the instruction.
fn transaction(s: &mut State, i: System) -> Result<(), [u64; 3]> {
    if i.kind == system::SET_CPL {
        // The test hook owns the rest of the transition: the flat CS and SS of the new CPL (RPL = CPL).
        let flat = flat_segments(u64::from(i.cr));
        (s.cpu.seg[SREG_CS], s.cpu.seg[SREG_SS]) = (flat[SREG_CS], flat[SREG_SS]);
        set_cpl(s, u64::from(i.cr));
        return Ok(commit(s, i, false));
    }
    let c = s.cpu;
    let unsupported = |s: &State| -> ! { halt(s, stop::UNSUPPORTED_SYSTEM) };
    let moves = matches!(i.kind, system::MOV_TO_CR | system::MOV_FROM_CR);
    let debug = matches!(i.kind, system::MOV_TO_DR | system::MOV_FROM_DR);
    // MOV to/from Sreg: ModRM.reg 6, 7 name no segment register; CS cannot be loaded.
    let sreg = matches!(i.kind, system::MOV_TO_SREG | system::MOV_FROM_SREG)
        && (i.cr as usize > SREG_GS || (i.kind == system::MOV_TO_SREG && i.cr as usize == SREG_CS));
    // (LOCK BTS/BTR/BTC are valid: BT_ALIGNED checks LOCK itself.)
    if (i.lock != 0 && !(i.kind == system::BT_ALIGNED && i.cr != 0)) || (moves && !matches!(i.cr, 0 | 2 | 3 | 4 | 8)) || (debug && i.cr > 7) || sreg {
        return Err(UD);
    }
    // IRET (past its #UD checks) releases NMI blocking before anything else, even when it then faults.
    if i.kind == system::IRET {
        s.nmi_blocked = 0;
    }
    match i.kind {
        system::INT | system::INT3 => {
            let vector = if i.kind == system::INT3 { 3 } else { u64::from(i.cr) };
            deliver(s, Event::Software { vector, length: u64::from(i.length) });
            return Ok(());
        }
        // S4: INT1 delivers #DB as a trap (RIP past it, the current RF; model A12), DR6 unchanged.
        system::INT1 => {
            deliver(s, Event::Debug { length: u64::from(i.length), mid_string: false });
            return Ok(());
        }
        system::INTO | system::UD => return Err(UD),
        system::TOO_LONG => return Err(GP),
        // X2: interrupt-flag control, HLT (it parks after its commit) and the test drivers' event hook.
        // X2: a BT* memory form under alignment checking: #AC(0) when misaligned, else it is stepped (`find`), RIP kept.
        system::BT_ALIGNED => {
            events::bit_test(s, i)?;
            s.plain_step = 1;
            return Ok(());
        }
        system::CLI | system::STI | system::POPF | system::HLT | system::EVENT_HOOK => {
            events::transaction(s, i)?;
            commit(s, i, false);
            // S4: a pending single-step trap wakes a HLT at once (delivery_model A10): no park.
            if i.kind == system::HLT && s.debug & debug::TRAP == 0 {
                events::park(s);
            }
            return Ok(());
        }
        system::LDMXCSR | system::STMXCSR | system::SSE_GATED | system::VLDMXCSR | system::VSTMXCSR => {
            sse::transaction(s, i)?;
            return Ok(commit(s, i, false));
        }
        // I8: a VEX AVX form under its gating: #UD without CR4.OSXSAVE or with XCR0[2:1] != 11b, else #NM with CR0.TS (SDM
        // Vol.2 2.8, Vol.1 14.3: the XCR0/OSXSAVE checks come before CR0.TS; CR0.EM and CR4.OSFXSR do not gate VEX forms).
        system::AVX_GATED => {
            if !s.cpu.avx_enabled() {
                return Err(UD);
            }
            if s.cpu.cr0 & CR0_TS != 0 {
                return Err([7, 0, 0]);
            }
            halt(s, stop::BAD_REPLY) // the host cut it as gated under these very controls
        }
        // I9: an MMX form under its gating: #UD with CR0.EM, else #NM with CR0.TS (SDM Vol.3 Table 12-1).
        system::MMX_GATED => {
            if s.cpu.cr0 & CR0_EM != 0 {
                return Err(UD);
            }
            if s.cpu.cr0 & CR0_TS != 0 {
                return Err([7, 0, 0]);
            }
            halt(s, stop::BAD_REPLY) // the host cut it as gated under these very controls
        }
        // I6: the resident's x87 forms and a gated x87 form or WAIT (#NM).
        system::X87_GATED..=system::FSTP80 => {
            x87::transaction(s, i)?;
            return Ok(commit(s, i, false));
        }
        // I5b: FXSAVE/FXRSTOR, XSAVE/XRSTOR, XGETBV/XSETBV ([`xsave`]; XSETBV checks its CPL). No flush: FCW and MXCSR are
        // keyed in `Cpu::code_context`, and XCR0 gates no admitted form.
        system::FXSAVE..=system::XSETBV => {
            xsave::transaction(s, i)?;
            return Ok(commit(s, i, false));
        }
        // L3: port I/O ([`ports`]): each checks its own privilege (IOPL, the TSS I/O bitmap) and commits itself (a REP
        // INS/OUTS stays at the instruction between elements).
        system::IN..=system::OUTS => return ports::transaction(s, i),
        // S3: each checks its own privilege ([`syscalls`]); SYSCALL/SYSRET commit to their target RIP.
        system::SWAPGS..=system::CLFLUSH => {
            let c = s.cpu;
            return match syscalls::execute(s, i) {
                Ok(None) => Ok(commit(s, i, false)),
                Ok(Some(rip)) => {
                    s.cpu.rip = rip;
                    Ok(commit(s, System { length: 0, ..i }, false))
                }
                Err(e) => {
                    s.cpu = c;
                    Err(e)
                }
            };
        }
        _ => {}
    }
    // CPUID runs at any CPL; RDTSC/RDTSCP at CPL > 0 unless CR4.TSD, RDPMC with CR4.PCE; SGDT/SIDT/SLDT/STR (CR4.UMIP is
    // not in the profile), segment register moves and far transfers at any CPL; the rest at CPL 0 only.
    let unprivileged = match i.kind {
        system::CPUID => true,
        system::SGDT | system::SIDT | system::SLDT | system::STR => true,
        system::MOV_TO_SREG | system::MOV_FROM_SREG | system::JMP_FAR | system::CALL_FAR | system::RET_FAR | system::IRET => true,
        system::RDTSC | system::RDTSCP => c.cr4 & CR4_TSD == 0,
        system::RDPMC => c.cr4 & CR4_PCE != 0,
        _ => false,
    };
    if c.cpl != 0 && !unprivileged {
        return Err(GP)
    }
    let gpr = |r: u32| c.gpr[r as usize];
    let slot = |index: u32| cpu::msr(index).unwrap();
    // Whether the vCPU's translations must go.
    let changed = match i.kind {
        system::MOV_FROM_CR => {
            s.cpu.gpr[i.reg as usize] = match i.cr {
                0 => c.cr0,
                2 => c.cr2,
                3 => c.cr3,
                4 => c.cr4,
                // X3: CR8 is TPR[7:4] (SDM Vol.3 11.8.6.1); the bank's `cr8` holds the whole TPR.
                _ => c.cr8 >> 4,
            };
            false
        }
        system::MOV_TO_CR => {
            let v = gpr(i.reg);
            match i.cr {
                0 => {
                    let new = v & CR0_DEFINED | CR0_ET;
                    if v >> 32 != 0 || new & CR0_PG == 0 || new & CR0_PE == 0 || new & (CR0_NW | CR0_CD) == CR0_NW {
                        return Err(GP)
                    }
                    s.cpu.cr0 = new;
                }
                3 => {
                    // Without PCIDE, bits 63:MAXPHYADDR are reserved (bit 63 included); PWT/PCD are not modelled.
                    if v & !(paging::PA_MASK | 0xfff) != 0 {
                        return Err(GP)
                    }
                    if v & !(paging::PA_MASK | CR3_IGNORED) != 0 {
                        unsupported(s)
                    }
                    s.cpu.cr3 = v;
                    tlb(s, false); // a load of the same value too
                    return Ok(commit(s, i, false));
                }
                4 => {
                    if v & !cpu_profile::CR4_VALID != 0 || (v ^ c.cr4) & (CR4_PAE | CR4_LA57) != 0 {
                        return Err(GP)
                    }
                    s.cpu.cr4 = v;
                }
                // CR2 takes any value (no canonical check, SDM Vol.2 MOV CR); CR8 bits 63:4 are reserved. Neither
                // touches a translation.
                2 => {
                    s.cpu.cr2 = v;
                    return Ok(commit(s, i, false));
                }
                _ => {
                    if v >> 4 != 0 {
                        return Err(GP)
                    }
                    s.cpu.cr8 = v << 4; // TPR[3:0] cleared (11.8.6.1)
                    return Ok(commit(s, i, false));
                }
            }
            // K1: a change of a bit the walks depend on flushes the TLBs; the gating and rounding bits are in the context.
            (s.cpu.cr0 ^ c.cr0) & (CR0_PG | CR0_WP) != 0 || (s.cpu.cr4 ^ c.cr4) & CR4_PAGING != 0
        }
        // SDM Vol.2 MOV DR, Vol.3 17.2: (DR8..DR15 #UD and CPL > 0 #GP above) DR4/DR5 #UD with CR4.DE, else DR6/DR7;
        // general detect (DR7.GD): #DB, a fault before the MOV, with DR6.BD set (B0..B3 kept: the SDM lets the CPU
        // clear them or not); delivering it clears DR7.GD (X1/S4). Then a write of 1 to bits 63:32 of DR6/DR7 is #GP.
        // DR0..DR3 take any value. S4: DR7 arms instruction breakpoints (R/W = 00); a write that arms an address not armed
        // before drops this vCPU's translations (blocks are cut before the armed ones, [`Cpu::code_breakpoints`]). Data
        // breakpoints (R/W 01, 11) arm watchpoints ([`watch::program`], after every write); I/O breakpoints (R/W 10) need
        // CR4.DE (without it the SDM leaves R/W 10 undefined: refused, `unsupported_system`).
        system::MOV_FROM_DR | system::MOV_TO_DR => {
            if i.cr & 6 == 4 && c.cr4 & CR4_DE != 0 {
                return Err(UD)
            }
            if c.dr[5] & DR7_GD != 0 {
                s.cpu.dr[4] |= DR6_BD;
                return Err(DB)
            }
            let n = if i.cr < 4 { i.cr as usize } else { 4 + (i.cr & 1) as usize };
            if i.kind == system::MOV_FROM_DR {
                s.cpu.gpr[i.reg as usize] = c.dr[n];
            } else {
                let v = gpr(i.reg);
                s.cpu.dr[n] = match n {
                    0..=3 => v,
                    _ if v >> 32 != 0 => return Err(GP),
                    4 => v & DR6_WRITABLE | DR6_FIXED,
                    _ if (0..4).any(|n| dr7_enabled(v, n) && dr7_rw(v, n) == 2) && c.cr4 & CR4_DE == 0 => unsupported(s),
                    _ => v & DR7_WRITABLE | DR7_FIXED,
                };
                if s.cpu.code_breakpoints().any(|b| !c.code_breakpoints().any(|a| a == b)) {
                    flush(s);
                }
                watch::program(s);
            }
            false
        }
        // The profile has no performance counters (CPUID.0AH = 0): every ECX names an invalid counter (SDM Vol.2 RDPMC).
        system::RDPMC => return Err(GP),
        system::INVLPG => {
            let address = segments::address(&c, &i); // INVLPG drops segment overrides (system::decode)
            // A non-canonical address invalidates nothing.
            if paging::canonical(address) {
                let t0 = ticks();
                let start = address & !(LARGE_PAGE - 1);
                if tables::unmap_large(start).is_err() {
                    halt(s, stop::SHADOW)
                }
                cache::control().invlpgs += 1;
                match cache::invalidate_linear(start, LARGE_PAGE) {
                    Some(n) => s.unlinks += n,
                    None => halt(s, stop::BAD_LINK),
                }
                cache::control().invlpg_ticks += ticks() - t0;
            }
            false
        }
        system::CPUID => {
            let d = cpu::Dynamic {
                vcpus: s.vcpus,
                vcpu: s.vcpu,
                cr4: c.cr4,
                apic_base: msr_get(slot(cpu::MSR_APIC_BASE)),
                misc_enable: msr_get(slot(cpu::MSR_MISC_ENABLE)),
                xcr0: c.xcr0,
            };
            let [eax, ebx, ecx, edx] = cpu::cpuid(gpr(0) as u32, gpr(1) as u32, &d);
            [0, 3, 1, 2].into_iter().zip([eax, ebx, ecx, edx]).for_each(|(r, v)| s.cpu.gpr[r] = u64::from(v));
            if gpr(0) as u32 == 1 {
                msr_set(slot(cpu::MSR_UCODE_REV), 0); // CPUID.1 reloads the latch with virtual revision 0 (CPU 17)
            }
            false
        }
        system::RDTSC | system::RDTSCP => {
            let t = tsc();
            (s.cpu.gpr[0], s.cpu.gpr[2]) = (t & 0xffff_ffff, t >> 32);
            if i.kind == system::RDTSCP {
                s.cpu.gpr[1] = msr_get(slot(cpu::MSR_TSC_AUX)) & 0xffff_ffff;
            }
            false
        }
        system::RDMSR | system::WRMSR => {
            // ECX selects; the profile's checks (cpu::read_ok/write_ok) decide #GP(0) before any effect.
            let index = gpr(1) as u32;
            // X3: the x2APIC registers are the local APIC's ([`lapic`]).
            if (0x800..=0x8ff).contains(&index) {
                if i.kind == system::RDMSR {
                    let v = lapic::rdmsr(s, index)?;
                    (s.cpu.gpr[0], s.cpu.gpr[2]) = (v & 0xffff_ffff, v >> 32);
                } else {
                    lapic::wrmsr(s, index, gpr(2) << 32 | gpr(0) & 0xffff_ffff)?;
                }
                return Ok(commit(s, i, false));
            }
            let Some(at) = cpu::msr(index) else { return Err(GP) };
            let m = cpu_profile::MSRS[at];
            if i.kind == system::RDMSR {
                if !cpu::read_ok(&m) {
                    return Err(GP)
                }
                let v = match index {
                    cpu::MSR_EFER => c.efer,
                    cpu::MSR_FS_BASE => c.fs_base,
                    cpu::MSR_GS_BASE => c.gs_base,
                    cpu::MSR_TSC => tsc(),
                    _ => msr_get(at),
                };
                (s.cpu.gpr[0], s.cpu.gpr[2]) = (v & 0xffff_ffff, v >> 32);
                false
            } else {
                let v = gpr(2) << 32 | gpr(0) & 0xffff_ffff;
                if !cpu::write_ok(&m, v, c.cr0, c.efer) {
                    return Err(GP)
                }
                match index {
                    cpu::MSR_EFER => s.cpu.efer = v & !EFER_LMA | c.efer & EFER_LMA, // LMA writes are ignored
                    // The bank's bases, which every block entry writes into the guest-state page (I7): no flush.
                    cpu::MSR_FS_BASE => s.cpu.fs_base = v,
                    cpu::MSR_GS_BASE => s.cpu.gs_base = v,
                    cpu::MSR_TSC => msr_set(at, v.wrapping_sub(tsc().wrapping_sub(msr_get(at)))),
                    cpu::MSR_MISC_ENABLE => msr_set(at, cpu::misc_enable_after_write(v)),
                    // EN/EXTD and the base select the LAPIC's mode and page (X3: its transitions, [`lapic::set_base`]).
                    cpu::MSR_APIC_BASE => lapic::set_base(s, v)?,
                    // UCODE_REV keeps the high half (the low is 0 by the mask); KERNEL_GS_BASE is SWAPGS's ([`syscalls`]);
                    // PAT and the MTRRs are recorded: memory types are the walker's (M2), RAM runs as write-back.
                    _ => msr_set(at, v),
                }
                // A change of EFER's paging bits flushes the TLBs (SCE gates only SYSCALL/SYSRET, checked as they run);
                // an MTRR write invalidates TLBs including global entries (CPU 9). No translation depends on either.
                (s.cpu.efer ^ c.efer) & (EFER_NXE | EFER_LME | EFER_LMA) != 0 || cpu::is_mtrr(index)
            }
        }
        // Descriptor tables, segment registers and far transfers ([`segments`]). The resident has no jump tables (an
        // unlisted `br` fails tools/check_resident_stack.py; the runtime target builds with `-C jump-tables=no`), and
        // the kinds are grouped in ranges besides.
        system::LGDT..=system::IRET => match segments::execute(s, i) {
            Ok(None) => false,
            // A far transfer: RIP is the target (nothing to add), and the mode check of [`commit`] applies.
            Ok(Some(rip)) => {
                s.cpu.rip = rip;
                return Ok(commit(s, System { length: 0, ..i }, false));
            }
            Err(e) => {
                s.cpu = c;
                return Err(e);
            }
        },
        _ => halt(s, stop::BAD_REPLY),
    };
    Ok(commit(s, i, changed))
}
/// Ends a system transaction: RIP past the instruction (it completed: RF ends, except the RF an IRET just loaded); with
/// `flush_all`, a global TLB flush ([`tlb`]: shadows go, translations stay).
fn commit(s: &mut State, i: System, flush_all: bool) {
    if s.cpu.control().is_none() {
        halt(s, stop::UNSUPPORTED_MODE)
    }
    if i.kind != system::IRET {
        completed(s);
    } else {
        stepped(s); // an IRET with TF at its start traps after it, whatever TF it loads
    }
    if flush_all {
        tlb(s, true);
    }
    s.cpu.rip = s.cpu.rip.wrapping_add(u64::from(i.length));
}
const LARGE_PAGE: u64 = 0x20_0000;

/// MSR slot `at` of the vCPU's MSR record (`MSRS_VA`, its own; the host wrote the reset values), read and written.
fn msr_get(at: usize) -> u64 {
    assert!(at < cpu_profile::MSRS.len());
    // SAFETY: the record is mapped EL1 read/write in this vCPU's window and only this vCPU's resident uses it.
    unsafe { (MSRS_VA as *const u64).add(at).read_volatile() }
}
fn msr_set(at: usize, value: u64) {
    assert!(at < cpu_profile::MSRS.len());
    // SAFETY: as in msr_get.
    unsafe { (MSRS_VA as *mut u64).add(at).write_volatile(value) }
}
/// The vCPU's TSC (L5, `natemu_abi::clock`): the profile's rate (`cpu_profile::TSC_HZ`, 1 GHz = the 24 MHz crystal x
/// 125 / 3, CPUID.15H) from V = CNTVCT_EL0 at CNTFRQ_EL0, exactly, plus the vCPU's offset (the TSC MSR's slot: the host
/// sets it from one CNTVCT sample shared by all vCPUs, so every TSC starts near 0 at VM creation and the vCPUs agree; a
/// WRMSR of the TSC moves it).
fn tsc() -> u64 {
    let (count, hz): (u64, u64);
    unsafe { asm!("isb", "mrs {c}, cntvct_el0", "mrs {f}, cntfrq_el0", c = out(reg) count, f = out(reg) hz) };
    natemu_abi::clock::tsc(count, hz.max(1)).wrapping_add(msr_get(cpu::msr(cpu::MSR_TSC).unwrap()))
}

/// Changes the vCPU's CPL (0..=3), at a block boundary with the state banked: the one place a CPL changes (exception
/// delivery, SYSCALL/SYSRET, IRET and RET far call it, and the test drivers' [`system::SET_CPL`]). Nothing is flushed
/// or reset: blocks are keyed by CPL ([`Cpu::code_context`]: a fetch's U/S and SMEP checks were made at the CPL it was
/// translated at), and shadows live in one space per privilege class, the CPL's loaded at every entry into generated
/// code ([`tables::select`]), so a supervisor shadow never serves a CPL3 access. Implicit supervisor accesses
/// (descriptor tables, the delivery frame) are the resident's, through the walker, never shadows. A CPL the bank
/// cannot run at ([`Cpu::control`]: e.g. a compatibility-mode CS) stops
/// `unsupported_mode` here, with the new CPL in the stop record and RIP still at the instruction.
pub fn set_cpl(s: &mut State, cpl: u64) {
    s.cpu.cpl = cpl;
    if cpl > 3 || s.cpu.control().is_none() {
        halt(s, stop::UNSUPPORTED_MODE)
    }
}

/// Guest GPRs and arithmetic flags from the native state: the static-register map,
/// PF/AF registers and `nzcv` (PSTATE layout) with the given CF inversion.
/// DF from the guest-state page (`cld`/`std` write it; the other flag bytes are the bank's own, never written by code).
fn bank(cpu: &mut Cpu, f: &NativeFrame, nzcv: u64, cf_inverted: bool) {
    let reg = |i: usize| f.x[SRA[i] as usize];
    for (i, value) in cpu.gpr.iter_mut().enumerate() {
        *value = reg(i);
    }
    // SAFETY: the frame page is mapped EL1 read/write at FRAME_VA.
    let df = frame::df(unsafe { (FRAME_VA as *const u8).add(frame::DF as usize).read_volatile() });
    cpu.rflags = guest_flags(cpu.rflags, nzcv, reg(16), reg(17), cf_inverted) & !RFLAGS_DF | if df { RFLAGS_DF } else { 0 };
    sse::bank(cpu, f);
}
const RFLAGS_DF: u64 = 1 << 10;
/// An instruction completed: RF (an IRET's frame may set it) ends (SDM Vol.3 "RF": cleared after every instruction but
/// IRET). Applied where the resident sees an instruction complete: a block's exit (linker, dispatcher miss or a stop in the
/// dispatcher or L1, armed exit) and a committed system instruction; a stop at a block's entry or a fault at its first
/// instruction keeps it.
/// S4: between two iterations of a REP string (`mid_string`) the instruction has not completed: RF stays (x86 clears it
/// only when the REP completes), while an iteration under TF owes its single-step trap ([`stepped`]).
fn completed(s: &mut State) {
    if s.mid_string == 0 {
        s.cpu.rflags &= !RFLAGS_RF;
    }
    stepped(s);
}
/// S4: the instruction run alone under TF (`State::debug_step`) completed (or did one REP iteration): a single-step trap
/// is pending at the next boundary.
#[inline(always)]
fn stepped(s: &mut State) {
    if s.debug & !debug::TRAP != 0 {
        // The data and I/O breakpoints it met (or this REP iteration met) trap after it, with a single step's BS.
        let bs = if s.debug & debug::STEP != 0 { DR6_BS } else { 0 };
        s.debug = s.debug & debug::TRAP | bs | s.debug >> debug::HITS & 0xf;
    }
}
const RFLAGS_RF: u64 = 1 << 16;
const RFLAGS_TF: u64 = 1 << 8;
/// The vCPU left `b` by its REP string's self-exit (X2), with RIP the REP's: between two iterations (`mid_string`), or,
/// after its last element (RCX = 0: I2's pass exits to its own RIP whatever the count left), past the instruction, the
/// boundary x86 has there (the REP ends `b`; a REP with RCX = 0 changes nothing).
fn rep_exited(s: &mut State, b: &Block) {
    if s.cpu.gpr[1] == 0 {
        s.cpu.rip = b.rip.wrapping_add(u64::from(b.guest_bytes));
    } else {
        s.mid_string = 1;
    }
}
/// The native offset of the link point whose `svc #LINK` is at native `svc` in `b`: the last one before the `svc` (each
/// is a move of its target into x0 followed by its `svc`, admission).
fn link_point_of(b: &Block, svc: u64) -> Option<u64> {
    let at = svc.wrapping_sub(b.code_va());
    b.links.iter().map(|p| u64::from(p.offset)).filter(|&o| o != 0 && o < at).max()
}

/// An x86 exception due at `site`'s guest instruction in `b` (`vector`,
/// CR2, error code): the bank holds the exact state at that instruction's
/// start, which the precision guarantee places at the site (or its stub):
/// GPRs and PF/AF in their static registers, NZCV in PSTATE or, for
/// `nzcv=saved` sites, in [`PRE_NZCV_REG`]. The exception is delivered through the IDT ([`deliver`], X1a) and the
/// handler dispatched.
fn guest_fault(mut s: State, f: &NativeFrame, b: &Block, site: &Site, [vector, cr2, error]: [u64; 3]) -> ! {
    bank_at(&mut s, f, b, site);
    deliver(&mut s, Event::Exception([vector, error, cr2]));
    unsafe { dispatch(s) }
}
/// Banks the exact state at the start of `site`'s guest instruction in `b` (see [`guest_fault`]).
fn bank_at(s: &mut State, f: &NativeFrame, b: &Block, site: &Site) {
    let nzcv = match site.nzcv {
        object::NZCV_SAVED => f.x[PRE_NZCV_REG as usize] & 0xf000_0000,
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
        None => halt(s, stop::BAD_LINK),
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
    loop {
        s.steps += 1;
        // A system instruction whose decode is kept runs from it (K2), as a decoded reply below does.
        let reply = match kept_system(&mut s) {
            Some(i) => Reply::System(i),
            None => unsafe { request(&mut s, REQUEST_STEP) },
        };
        s.plain_step = 0;
        let h = match reply {
            Reply::Block(h) => h,
            // Its fetch faults (a peer changed the tables): delivered, and the handler's first instruction is stepped.
            // (Then the handler's first instruction is a boundary like any: `find`'s checks, breakpoints included.)
            Reply::Fault(fault) => {
                deliver(&mut s, Event::Exception(fault));
                match unsafe { find(&mut s) } {
                    Some(block) => unsafe { enter(s, block) },
                    None => continue,
                }
            }
            // A system instruction (a peer replaced the instruction, e.g. by an INT3 as Linux's text_poke_bp does): executed
            // as `find` executes it, then the next boundary is `find`'s (events a POPF, IRET or STI made eligible, invalidations),
            // unless a BT* form found aligned must run itself, compiled plain.
            Reply::System(instruction) => {
                system(&mut s, instruction);
                if s.plain_step != 0 {
                    continue;
                }
                // (In this loop, not through `dispatch`, which would recurse into `step`: `None` steps the next instruction.)
                match unsafe { find(&mut s) } {
                    Some(block) => unsafe { enter(s, block) },
                    None => continue,
                }
            }
        };
        let context = s.cpu.code_context();
        match cache::place_step(&h, STAGING_VA as *const u8, context, s.source_gpa, s.link_points, s.dispatch_exit, s.rep_exits) {
            Some(block) => unsafe { enter(s, block) },
            None => halt(&s, stop::BAD_REPLY),
        }
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
/// faults and walks again, as x86 re-walks). A device page (not guest RAM) is walked but not shadowed: its address is
/// returned for the device path (L3: an unassigned one may then be shadowed onto the all-ones page, `mmio::ones`). Translated pages are shadowed read-only whatever the
/// walk grants; a write to one, or an A/D update in one, takes [`code_written`] first (which may step
/// the instruction instead of returning), and each A/D update is re-checked after it is made.
fn translate(s: &mut State, f: &NativeFrame, b: &Block, site: &Site, va: u64, access: Access) -> Option<u64> {
    let Some(control) = s.cpu.control() else {
        halt(s, stop::UNSUPPORTED_MODE)
    };
    let walk = paging::walk(&mut GuestTables::observed(guest_ram(), CodeWrites(DataWrites { s: &mut *s, f, b, site })), control, va, access);
    match walk {
        // M3: a page that is not guest RAM is a device page whatever its memory type (device ranges are uncached): never
        // shadowed; the caller completes the access through the device path ([`mmio::device`]).
        Ok(t) if !mmio::ram(t.gpa) => Some(t.gpa),
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
            let old = tables::descriptor(s.cpu.cpl, va);
            if writable && old & 1 != 0 && old & READ_ONLY != 0 {
                s.upgrades += 1;
            }
            install(s, va, t.gpa, writable);
            Some(t.gpa)
        }
        // Only write-back guest memory is shadowed.
        Ok(_) => halt(s, stop::SHADOW),
        Err(Fault::Page { address, error }) => guest_fault(*s, f, b, site, [14, address, error]),
        Err(Fault::Noncanonical) => guest_fault(*s, f, b, site, noncanonical(site)),
        Err(Fault::Contended) => None,
        Err(Fault::Unsupported) => halt(s, stop::UNSUPPORTED_MODE),
    }
}

/// A synchronous exception from generated code (EL0) other than an `svc` or a stop, classified by site first: a `brk`
/// at a check stub of the block holding ELR, or a data abort at one of its access sites whose recorded address range
/// holds FAR ([`access_aborted`]: a shadow miss to retry, or a guest fault; any non-canonical address, the private
/// window included, is #GP or #SS). Only an exception at no site, or not of its site's kind, halts: an instruction the
/// hardware refused at EL0 (`trapped`), an abort in the resident's private window or the guest-state alias's slot
/// (`private_access`), else
/// `site_mismatch` at a site and `unknown_abort` elsewhere.
unsafe fn aborted(f: &NativeFrame) -> ! {
    unsafe {
        let (esr, far, elr, spsr): (u64, u64, u64, u64);
        asm!("mrs {0}, esr_el1", "mrs {1}, far_el1", "mrs {2}, elr_el1", "mrs {3}, spsr_el1",
             out(reg) esr, out(reg) far, out(reg) elr, out(reg) spsr);
        let mut s = load();
        let block = cache::block_at(elr);
        let all = block.map_or(&[][..], cache::sites);
        // `native` for every outcome: ESR, FAR, and ELR as an offset in its block when it is in one, else absolute.
        let at = block.map_or(elr, |b| elr - b.code_va());
        s.native = [esr, far, at];
        let found = all.binary_search_by_key(&at, |site| site.native_offset as u64).ok().map(|i| all[i]);
        // The syndrome must be the class this site's kind produces, before anything is taken or stepped: BRK from
        // AArch64 with the stub's immediate (1 branch target, 2 split, 3 divide), else a data abort from EL0 inside the
        // site's access (access_aborted checks the rest).
        let brk = |imm: u64| esr >> 26 == 0x3c && esr & 0xffff == imm;
        let ours = |site: &Site| {
            from_el0(spsr)
                && match site.kind {
                    object::kind::GUARD => brk(1),
                    object::kind::SPLIT => brk(2),
                    object::kind::DIVIDE => brk(3),
                    object::kind::ALIGN => brk(4),
                    // S4: a watchpoint (EC 0x34, from EL0) at an access site: FAR names a watched byte, not necessarily the access's.
                    _ if esr >> 26 == 0x34 => !matches!(site.kind, object::kind::OTHER),
                    _ => esr >> 26 == 0x24 && far.wrapping_sub(site_address(site, f)) < u64::from(site.width),
                }
        };
        let (Some(b), Some(site)) = (block, found.filter(ours)) else {
            // UNDEFINED (system registers, TLB maintenance, `eret`, `hvc`, `udf`), WFI/WFE, illegal execution state,
            // MSR/MRS/SYS traps (DAIF, cache maintenance, CTR_EL0, debug channel, counters), SME, PC and SP alignment.
            if matches!(esr >> 26, 0x00 | 0x01 | 0x0e | 0x18 | 0x1d | 0x22 | 0x26) {
                halt(&s, stop::TRAPPED)
            }
            // Instruction or data abort from EL0 in the private window or the guest-state alias's slot (G1: a fetch from
            // the page, or an access beside it; FAR is valid for both), at no access site.
            if matches!(esr >> 26, 0x20 | 0x24) && (WINDOW.contains(&far) || GUEST_STATE_SLOT.contains(&far)) {
                halt(&s, stop::PRIVATE_ACCESS)
            }
            halt(&s, if found.is_some() { stop::SITE_MISMATCH } else { stop::UNKNOWN_ABORT })
        };
        // Invalidations queued while `b` ran (a resident entry takes them): if they dropped `b`, it must not run on.
        let generation = s.generation;
        take_invalidations(&mut s);
        if b.flags & cache::STEP == 0 && (s.generation != generation || b.flags & cache::DEAD != 0) {
            step_from(s, f, b, &site)
        }
        if esr >> 26 == 0x34 {
            watch::hit(s, f, b, &site, elr, spsr)
        }
        match site.kind {
            // A branch target checked non-canonical: #GP(0) at the branch.
            object::kind::GUARD => guest_fault(s, f, b, &site, noncanonical(&site)),
            // #DE: no error code.
            object::kind::DIVIDE => guest_fault(s, f, b, &site, [0, 0, 0]),
            // A legacy SSE operand that is not 16-byte aligned (I5a): #GP(0) before any walk.
            object::kind::ALIGN => guest_fault(s, f, b, &site, [13, 0, 0]),
            object::kind::SPLIT => split(s, f, b, all, &site, spsr),
            _ => access_aborted(s, f, b, &site, [esr, far, elr, spsr]),
        }
    }
}

/// A data abort at an access site: a shadow miss to retry, or a guest fault.
unsafe fn access_aborted(mut s: State, f: &NativeFrame, b: &Block, site: &Site, [esr, far, elr, spsr]: [u64; 4]) -> ! {
    let store = matches!(site.kind, object::kind::STORE | object::kind::PUSH | object::kind::STORE_PAIR);
    // A swap (memory XCHG) or LOCK atomic (I3: ld<op>al, casal, caspal) may report either direction: its translation is a
    // write (Arm ARM MemAtomic, also when a CAS comparison fails), but ESR.WnR is not reliable for atomics (the M4 reports a
    // translation fault of one as a read); it writes nothing.
    let swap = matches!(site.kind, object::kind::SWAP | object::kind::ATOMIC);
    let write_abort = (esr >> 6) & 1 != 0;
    let write = site.intent == object::INTENT_WRITE;
    let address = site_address(site, f);
    let width = u64::from(site.width);
    let touched = pages(address, width);
    let alignment = esr & 63 == 0x21 && esr >> 26 == 0x24;
    // CMPXCHG16B (a 16-byte caspal) on an operand that is not 16-byte aligned: #GP(0) (SDM CMPXCHG16B), checked before the
    // address is translated (QEMU's order; Arm ARM abort prioritization: an alignment fault from the instruction's own
    // alignment requirement is taken before any translation, access-flag or permission fault).
    if site.kind == object::kind::ATOMIC && width == 16 && alignment && address & 15 != 0 {
        guest_fault(s, f, b, site, [13, 0, 0])
    }
    // X2: under x86 alignment checking (SCTLR_EL1.A while it runs), a misaligned access is #AC(0) after the canonical
    // check (made first, as for every access) and before any page walk, so before a split lock too. A 16- or 32-byte vector
    // access (I8: a VEX.256 operand, one Q-register pair) that is 8-byte aligned is not one the SDM lets fault (MOVUPS and the
    // like: "may or may not" only below 8-byte alignment, Vol.3 7.15); completing it is not built: refused with the state at
    // the instruction.
    if alignment && alignment_check(&s.cpu) != 0 {
        if touched.is_none() {
            guest_fault(s, f, b, site, noncanonical(site))
        }
        if width >= 16 && site.data_class == object::CLASS_FPR && address & 7 == 0 {
            bank_at(&mut s, f, b, site);
            halt(&s, stop::UNSUPPORTED_MODE)
        }
        guest_fault(s, f, b, site, [17, 0, 0])
    }
    // A swap or atomic whose operand crosses 16 bytes takes an alignment fault (LSE2) before any effect: a split lock, not
    // completed (owner decision: LOCKed accesses crossing 16 bytes stop), unless x86's canonical check (made first) fails.
    if swap && alignment && (address & 15) + width > 16 {
        if touched.is_none() {
            guest_fault(s, f, b, site, noncanonical(site))
        }
        bank_at(&mut s, f, b, site);
        halt(&s, stop::UNSUPPORTED_ATOMIC)
    }
    // Data abort from EL0, translation or permission fault, direction, intent and FAR as recorded.
    if esr >> 26 != 0x24
        || (write_abort != store && !swap)
        || (write_abort && !write)
        || !matches!(esr & 63, 4..=7 | 12..=15)
        || far.wrapping_sub(address) >= width
    {
        halt(&s, stop::SITE_MISMATCH)
    }
    // A non-canonical address (unmapped, EL1-only, or execute-only in the private window, or beside the guest-state alias
    // in its slot: generated code runs at EL0 with no address guard) is #GP, or #SS for a stack-segment site, whichever byte faulted.
    let Some(touched) = touched else {
        guest_fault(s, f, b, site, noncanonical(site))
    };
    // A repeat of the last retry with its shadow untouched, and no shadow reset or flush since, would loop.
    // (Native PCs repeat legitimately: cached blocks run again.)
    if s.last_retry == [elr, far, tables::descriptor(s.cpu.cpl, far), s.shadow_generation] {
        halt(&s, stop::NO_PROGRESS)
    }
    let access = if write { Access::Write } else { Access::Read };
    // Every page the access touches, in address order, before the retry. A contended walk resumes at once, outside
    // the no-progress test (the abort repeats until the walk wins; the watchdog contains a livelock). M3: a device page
    // among them (walked, so every fault it owes came first) takes the device path instead of a retry.
    let mut device = [None; 2];
    for (k, va) in touched.into_iter().flatten().enumerate() {
        match translate(&mut s, f, b, site, va, access) {
            None => unsafe {
                write_record(record(), s);
                resume(&s, &raw const (*shared()).captured, elr, spsr)
            },
            Some(gpa) if !mmio::ram(gpa) => device[k] = Some(gpa),
            Some(_) => {}
        }
    }
    // L3 (amendment): a read whose non-RAM pages are all unassigned shadows them onto the all-ones page and retries, so
    // every read form runs natively; anything else (a write, a device's page) takes the device path.
    if device != [None; 2] && (write || !mmio::ones(&mut s, f, b, site, touched, device)) {
        mmio::device(s, f, b, site, device[0].filter(|_| touched[1].is_none()))
    }
    s.last_retry = [elr, far, tables::descriptor(s.cpu.cpl, far), s.shadow_generation];
    s.retries += 1;
    unsafe {
        write_record(record(), s);
        resume(&s, &raw const (*shared()).captured, elr, spsr)
    }
}

/// `ACCESS_COMPLETED` for a page-crossing store (split stub): translate both pages
/// with write intent, then write the access's data register through owned RAM in
/// guest byte order, apply its writeback and resume after it. A fault on either page
/// stops before any byte is written.
unsafe fn split(mut s: State, f: &NativeFrame, b: &Block, all: &[Site], stub: &Site, spsr: u64) -> ! {
    let Ok(i) = all.binary_search_by_key(&stub.guest_offset2, |site| site.native_offset) else {
        halt(&s, stop::SITE_MISMATCH)
    };
    let access = all[i];
    let width = access.width as usize;
    let vector_pair = access.kind == object::kind::STORE_PAIR && access.data_class == object::CLASS_FPR;
    if (width > 16 && !(width == 32 && vector_pair)) || (width > 8 && access.data_class != object::CLASS_FPR && access.kind != object::kind::STORE_PAIR) {
        halt(&s, stop::SITE_MISMATCH)
    }
    let address = site_address(stub, f);
    // S4: a page-crossing store meeting a data breakpoint traps after its instruction: run alone (its step block's split
    // records the bits).
    if s.cpu.dr[5] & DR7_ENABLES != 0 && data_hits(&s.cpu.dr, address, width as u64, true) != 0 {
        if b.flags & cache::STEP == 0 {
            step_from(s, f, b, stub)
        }
        s.debug |= data_hits(&s.cpu.dr, address, width as u64, true) << debug::HITS;
    }
    let Some([Some(first), Some(second)]) = pages(address, width as u64) else {
        match pages(address, width as u64) {
            None => guest_fault(s, f, b, stub, noncanonical(stub)),
            Some(_) => halt(&s, stop::SITE_MISMATCH), // the check fired for an access within one page
        }
    };
    // X2: a store crossing a page is misaligned: #AC(0) under alignment checking (an 8-byte aligned 16-byte vector store
    // excepted, as in [`access_aborted`]: completed here).
    if alignment_check(&s.cpu) != 0 && !(width >= 16 && access.data_class == object::CLASS_FPR && address & 7 == 0) {
        guest_fault(s, f, b, stub, [17, 0, 0])
    }
    // A contended walk resumes at the stub, which takes this path again.
    let gpa = [first, second].map(|va| translate(&mut s, f, b, stub, va, Access::Write));
    let [Some(gpa0), Some(gpa1)] = gpa else {
        unsafe {
            write_record(record(), s);
            resume(&s, &raw const (*shared()).captured, b.code_va() + stub.native_offset as u64, spsr)
        }
    };
    // M3: a page-crossing store with a device page on either side is refused (both pages walked first).
    if !mmio::ram(gpa0) || !mmio::ram(gpa1) {
        mmio::refuse(s, f, b, stub, natemu_abi::execution::mmio::CROSSING)
    }
    let gpa = [gpa0, gpa1];
    // One data register: a general one, or the low `width` bytes of a vector one (I5a); or MOVAPS m128's X-register pair
    // (I5a, `STORE_PAIR`: aligned, so it never crosses a page, but completed alike); or a VEX.256 operand's Q-register pair
    // (I8: `stp qA, qB`, 32 bytes, low half first).
    let x = |r: u8| if r == 31 { 0 } else { f.x[r as usize] };
    let mut bytes = [0u8; 32];
    match (access.data_class, access.data_reg[0]) {
        (object::CLASS_FPR, r) => {
            bytes[..16].copy_from_slice(&f.q[r as usize & 31]);
            if vector_pair {
                bytes[16..].copy_from_slice(&f.q[access.data_reg[1] as usize & 31]);
            }
        }
        (_, r) if access.kind == object::kind::STORE_PAIR => {
            bytes[..16].copy_from_slice(&(u128::from(x(r)) | u128::from(x(access.data_reg[1])) << 64).to_le_bytes())
        }
        (_, r) => bytes[..16].copy_from_slice(&u128::from(x(r)).to_le_bytes()),
    }
    let head = (4096 - (address & 4095)) as usize;
    let (Some(to0), Some(to1)) = (guest_ram().range(gpa[0], head as u64), guest_ram().range(gpa[1], (width - head) as u64)) else {
        halt(&s, stop::SHADOW)
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
        self::resume(&s, resume, b.code_va() + access.native_offset as u64 + 4, spsr)
    }
}

/// L3 (amendment): shadows linear page `va` (of an unassigned guest-physical page) read-only onto the all-ones page; on
/// table capacity exhaustion evicts every shadow once and retries.
fn install_ones(s: &mut State, va: u64) {
    if tables::install_ones(s.cpu.cpl, va).is_ok() {
        return;
    }
    reset_shadows(s);
    s.evictions += 1;
    if tables::install_ones(s.cpu.cpl, va).is_err() {
        halt(s, stop::SHADOW)
    }
}

/// Shadows linear page `va` with guest-physical `gpa` (a writable shadow's bit is already set, see [`translate`]); on
/// table capacity exhaustion evicts every shadow once and retries. The eviction clears the writable bits: the bit is
/// set again and the marks read again, and a page marked meanwhile is shadowed read-only (the access faults again and
/// takes the code-write path).
fn install(s: &mut State, va: u64, gpa: u64, writable: bool) {
    match tables::install(s.cpu.cpl, va, &guest_ram(), gpa, writable) {
        Ok(()) => {}
        Err(shadow::Error::Capacity) => {
            reset_shadows(s);
            let writable = writable && {
                cache::shadowed_writable(gpa);
                !cache::translated(gpa)
            };
            s.evictions += 1;
            if tables::install(s.cpu.cpl, va, &guest_ram(), gpa, writable).is_err() {
                halt(s, stop::SHADOW)
            }
        }
        Err(_) => halt(s, stop::SHADOW),
    }
}
