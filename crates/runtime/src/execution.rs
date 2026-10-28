//! Block execution in the resident (`natemu_abi::execution`): find the block
//! for the banked RIP in the code cache ([`cache`]) or ask the host for it and
//! place it, enter it from the state bank, take exits the fast dispatcher
//! (`gates.S` `dispatch_fast`) cannot resolve back into the bank, and serve
//! shadow misses at the recorded access sites of the block holding the faulting
//! PC by walking the guest tables and retrying the same instruction. A write to a
//! page holding translations drops that page's blocks first, or, when the running
//! block came from that page, runs the writing instruction alone in the slot
//! (self-modifying code, [`code_written`]). Literal exits
//! enter the linker (`gates.S` `link_exit`), which finds their target the same way
//! and links the exit to it. Also the 128/64 division helpers admitted code calls
//! through the frame.
use super::*;
use crate::cache::{self, Block};
use natemu_abi::{
    code_object::{self as object, Header, Site},
    execution::*,
    guest_memory::{self, Observer},
    paging::{self, Access, Fault},
    shadow::{self, READ_ONLY},
};

unsafe extern "C" {
    fn enter_allocated(frame: *const NativeFrame, pc: u64, spsr: u64) -> !;
    fn resume_exception(frame: *const NativeFrame, pc: u64, spsr: u64) -> !;
    fn dispatch_fast();
    fn link_exit();
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
fn frame_word(offset: u32) -> *mut u64 {
    (FRAME_VA + offset as u64) as *mut u64
}

/// Stops the VM; the host reads `reason` and the record.
fn halt(mut s: State, reason: u64) -> ! {
    s.stop = reason;
    unsafe { write_record(record(), s) };
    stop(SUCCESS, reason)
}

pub unsafe fn start() -> ! {
    let s = unsafe { load() };
    if s.cpu.control().is_none() || s.request != NONE {
        halt(s, stop::UNSUPPORTED_MODE)
    }
    cache::reset();
    unsafe {
        frame_word(frame::DISPATCHER_LOOP_TOP).write_volatile(dispatch_fast as *const () as u64);
        frame_word(frame::EXIT_FUNCTION_LINKER).write_volatile(link_exit as *const () as u64);
        frame_word(frame::LUDIV_HANDLER).write_volatile(natemu_ludiv as *const () as u64);
        frame_word(frame::LDIV_HANDLER).write_volatile(natemu_ldiv as *const () as u64);
        dispatch(s)
    }
}

/// Enters the block for `s.cpu.rip`.
unsafe fn dispatch(mut s: State) -> ! {
    let block = unsafe { find(&mut s) };
    unsafe { enter(s, block) }
}
/// The block for `s.cpu.rip`: from the cache (L2; L1 was probed by the fast dispatcher), else asks the host for it and
/// places it. Pending invalidations are taken first. Either may flush the cache (`s.generation`).
unsafe fn find(s: &mut State) -> &'static Block {
    take_invalidations(s);
    let context = s.cpu.code_context();
    cache::select_l1(context);
    let block = match cache::lookup(s.cpu.rip, context) {
        Some(b) => {
            s.l2_hits += 1;
            b
        }
        None => unsafe { translate_block(s, context) },
    };
    cache::fill_l1(s.cpu.rip, block);
    block
}

/// A literal exit (`Pointers.ExitFunctionLinker`, FEX's `ExitFunctionLink`), entered with the exit's state as at a
/// dispatcher exit. `InlineJITBlockHeader` names the block that ran it: only its entry prologue stores there
/// (admission), so it must be a live cache block with a link point to `State.rip`; anything else halts. The target is
/// found as by the dispatcher; the exit is linked to it unless the cache was flushed meanwhile (the source may be
/// gone: FEX re-checks the same way) or the link records are full, then the target is entered. A block run from the
/// single-step slot is never linked.
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
        let generation = s.generation;
        let target = find(&mut s);
        match source {
            // Flushed, or its page invalidated (queued), while the target was found.
            Some(source) if s.generation != generation || source.flags & cache::DEAD != 0 => s.stale_links += 1,
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

/// A miss: the host compiles and admits the block at `s.cpu.rip` into staging; place it (flushing a full cache
/// first) and keep its source pages read-only.
unsafe fn translate_block(s: &mut State, context: [u64; 2]) -> &'static Block {
    let h = unsafe { request(s, REQUEST_TRANSLATE) };
    s.requests += 1;
    let staging = STAGING_VA as *const u8;
    let block = match cache::place(&h, staging, context, s.source_gpa, s.link_points) {
        Some(b) => b,
        None => {
            flush(s);
            cache::place(&h, staging, context, s.source_gpa, s.link_points).unwrap_or_else(|| halt(*s, stop::BAD_REPLY))
        }
    };
    // A write to a translated page must fault: no writable shadow of a source page may survive.
    for gpa in block.source_gpa {
        if gpa != u64::MAX && cache::protect(gpa) {
            reset_shadows(s);
        }
    }
    block
}

/// Asks the host for code at `s.cpu.rip` (`kind`: a block, or one instruction for the slot) and checks the reply staged.
unsafe fn request(s: &mut State, kind: u64) -> Header {
    unsafe {
        s.request = kind;
        s.reply = NONE;
        write_record(record(), *s);
        asm!("dsb sy", "hvc #{imm}", imm = const TRANSLATE_HVC);
        *s = load();
    }
    // The host may have written guest RAM while serving the request (A bits of the fetch walk).
    take_invalidations(s);
    let h = staged();
    let table_end = (h.sites_offset as usize).checked_add(h.sites_count as usize * size_of::<Site>());
    if s.reply != REPLY_ADMITTED
        || h.guest_rip != s.cpu.rip
        || h.code_size as usize > natemu_abi::execution::cache::MAX_BLOCK_BYTES
        || h.code_offset as usize + h.code_size as usize > STAGING_BYTES
        || table_end.is_none_or(|end| end > STAGING_BYTES)
        // Host-supplied link points name words the linker may rewrite: each whole exit (4 words) inside the code.
        || s.link_points.iter().any(|p| p.offset != 0 && (p.offset % 4 != 0 || u64::from(p.offset) + 16 > u64::from(h.code_size)))
    {
        halt(*s, stop::BAD_REPLY)
    }
    s.request = NONE;
    h
}

/// Applies this vCPU's queued invalidations (host writes to guest RAM while it was stopped flush; listed pages drop
/// their blocks).
fn take_invalidations(s: &mut State) {
    match cache::drain() {
        Some(true) => flush(s),
        Some(false) => {}
        None => halt(*s, stop::BAD_LINK),
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
/// Removes every shadow of this vCPU.
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
    f.x[28] = FRAME_VA;
    f.sp = STACK_TOP;
    s.blocks += 1;
    unsafe {
        write_record(record(), s);
        let entry = &raw mut (*shared()).initial;
        write_record(entry, f);
        enter_allocated(entry, block.entry(), BLOCK_SPSR | nzcv)
    }
}

/// Exception capture (`fault` 1), the linker (2) and the dispatcher's slow path (0) end here.
pub unsafe fn returned(frame: &NativeFrame, fault: u64) -> ! {
    unsafe {
        match fault {
            0 => {}
            2 => linker(frame),
            _ => aborted(frame),
        }
        // Block exit: guest state is in the static registers, NZCV (CF inverted) and PF/AF registers.
        let mut s = load();
        bank(&mut s.cpu, frame, frame.nzcv, true);
        s.cpu.rip = frame_word(frame::RIP).read_volatile();
        dispatch(s)
    }
}

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
        object::NZCV_FRAME => unsafe { ((FRAME_VA + frame::PRE_NZCV as u64) as *const u32).read_volatile() as u64 },
        _ => f.nzcv,
    };
    bank(&mut s.cpu, f, nzcv, site.cf_inverted != 0);
    s.cpu.rip = b.rip.wrapping_add(site.guest_offset as u64);
}

/// Self-modifying code: the instruction at `site` of `b` writes `gpa`'s page (a store, a split store or its walk's
/// A/D update), which holds translations. If `b` was translated from that page it must not run past the instruction
/// (whose store may change bytes after it in `b`): the instruction is stepped ([`step`]) instead. Otherwise the
/// page's blocks are invalidated (none of them is running) and the write proceeds. The step block itself never
/// steps: it holds one instruction, already fetched, so its own writes simply invalidate (this ends the step: no loop).
fn code_written(s: &mut State, f: &NativeFrame, b: &Block, site: &Site, gpa: u64) {
    if b.flags & cache::STEP == 0 && b.source_gpa.iter().any(|&g| g != u64::MAX && g >> 12 == gpa >> 12) {
        step(*s, f, b, site)
    }
    match cache::invalidate(gpa) {
        Some(n) => (s.invalidations, s.unlinks) = (s.invalidations + 1, s.unlinks + n),
        None => halt(*s, stop::BAD_LINK),
    }
}
/// Runs the instruction at `site` of `b` alone: banks the exact state at its start (a split store in progress is
/// dropped: it wrote nothing yet) and enters it as an uncached one-instruction block in the slot, which marks no
/// source page. Its write then takes [`code_written`]'s invalidation path and completes; its exit goes to the next
/// instruction, which the next miss compiles from the new bytes (SDM vol. 3, "Handling Self- and Cross-Modifying Code").
fn step(mut s: State, f: &NativeFrame, b: &Block, site: &Site) -> ! {
    bank_at(&mut s, f, b, site);
    s.steps += 1;
    let h = unsafe { request(&mut s, REQUEST_STEP) };
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

/// The walker's A/D writes: the first to a translated page is refused (and recorded), as the guest's own stores are.
struct CodePageWrites(Option<u64>);
impl Observer for CodePageWrites {
    fn write(&mut self, gpa: u64, _: u64, _: u64) -> bool {
        if self.0.is_none() && cache::translated(gpa) {
            self.0 = Some(gpa);
        }
        self.0.is_none()
    }
}

/// Walks `va` with `access` for the guest instruction at `site` of `b` and shadows the page; a guest
/// fault stops with the precise state. Returns the guest-physical address of `va`. Translated pages
/// are shadowed read-only whatever the walk grants; a write to one, or an A/D update in one, takes
/// [`code_written`] first (which may step the instruction instead of returning).
fn translate(s: &mut State, f: &NativeFrame, b: &Block, site: &Site, va: u64, access: Access) -> u64 {
    let Some(control) = s.cpu.control() else {
        halt(*s, stop::UNSUPPORTED_MODE)
    };
    // Each refused A/D write invalidates its page (one vCPU: its bit clears), so the walk is repeated at most once per page.
    let walk = loop {
        let mut tables = GuestTables::observed(RAM, CodePageWrites(None));
        let walk = paging::walk(&mut tables, control, va, access);
        match tables.observer.0 {
            None => break walk,
            Some(gpa) => code_written(s, f, b, site, gpa),
        }
    };
    match walk {
        Ok(t) if t.wb => {
            let writable = access == Access::Write && t.writable && t.dirty;
            if writable && cache::translated(t.gpa) {
                code_written(s, f, b, site, t.gpa);
            }
            let old = tables::descriptor(va);
            if writable && old & 1 != 0 && old & READ_ONLY != 0 {
                s.upgrades += 1;
            }
            install(s, va, t.gpa, writable);
            t.gpa
        }
        // Only write-back guest memory is shadowed.
        Ok(_) => halt(*s, stop::SHADOW),
        Err(Fault::Page { address, error }) => guest_fault(*s, f, b, site, [14, address, error]),
        Err(Fault::Noncanonical) => guest_fault(*s, f, b, site, noncanonical(site)),
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
            step(s, f, b, &site)
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
    let write_abort = (esr >> 6) & 1 != 0;
    let write = site.intent == object::INTENT_WRITE;
    let address = site_address(site, f);
    let width = u64::from(site.width);
    // Data abort from EL1, translation or permission fault, direction, intent and FAR as recorded.
    if esr >> 26 != 0x25
        || write_abort != store
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
    // Every page the access touches, in address order, before the retry.
    for va in touched.into_iter().flatten() {
        translate(&mut s, f, b, site, va, access);
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
    let gpa = [first, second].map(|va| translate(&mut s, f, b, stub, va, Access::Write));
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

/// Shadows linear page `va` with guest-physical `gpa`; on table capacity
/// exhaustion evicts every shadow once and retries.
fn install(s: &mut State, va: u64, gpa: u64, writable: bool) {
    if writable {
        cache::shadowed_writable(gpa);
    }
    match tables::install(va, &RAM, gpa, writable) {
        Ok(()) => {}
        Err(shadow::Error::Capacity) => {
            reset_shadows(s);
            if writable {
                cache::shadowed_writable(gpa);
            }
            s.evictions += 1;
            if tables::install(va, &RAM, gpa, writable).is_err() {
                halt(*s, stop::SHADOW)
            }
        }
        Err(_) => halt(*s, stop::SHADOW),
    }
}
