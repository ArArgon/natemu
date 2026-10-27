//! Mixed paging modes use the I4 coordinator, with private contextual code slots.
use super::*;
use natemu_abi::{families::mixed32 as m, recovery as r, translation as wire};
unsafe extern "C" {
    fn service(packet: *mut wire::Packet);
    fn service_resume();
}
pub fn enabled() -> bool {
    coordinator().mixed_profile != 0
}
fn state() -> *mut m::State {
    (PRIVATE + m::OFFSET as u64) as *mut m::State
}
fn guest() -> *mut r::GuestState {
    r::GUEST_VA as *mut r::GuestState
}
fn id() -> u64 {
    unsafe { (*shared()).object.vcpu_id as u64 }
}
fn all() -> u64 {
    (1 << coordinator().participants) - 1
}

// Caller holds coordinator authority for every paged table access/A-D update.
pub unsafe fn resolve(address: u64, bytes: u64, access: Access) -> m::Route {
    unsafe {
        let g = &*guest();
        let mode = m::mode(g, (*state()).a20).unwrap_or_else(|| stop(REJECTED, 210));
        if mode != (*state()).mode {
            stop(REJECTED, 210);
        }
        if mode != 0 {
            let r = r::STATE_VA as *mut r::Recovery;
            (*r).walks += 1;
        }
        m::resolve(
            &mut GuestTables::new(recovery::RAM),
            g,
            (*state()).a20,
            address,
            bytes,
            access,
        )
        .unwrap_or_else(|_| stop(REJECTED, 211))
    }
}
pub unsafe fn expected_gpa(address: u64) -> u64 {
    unsafe {
        if (*state()).mode == 0 {
            address
        } else {
            let pte = (PRIVATE + r::TABLE_OFFSET as u64 + 0x3000 + 256 * 8) as *const u64;
            (ptr::read_volatile(pte) & natemu_abi::paging::PA_MASK) + (address & 4095)
        }
    }
}
#[inline(never)]
pub unsafe fn materialize(frame: &NativeFrame, offset: u64) {
    unsafe {
        let mut g = read_record(guest());
        for (guest, native) in [(0, 4), (1, 7), (2, 5), (3, 6)] {
            g.gpr[guest] = frame.x[native] & 0xffff_ffff;
        }
        g.rip = m::RIP + offset;
        write_record(guest(), g);
    }
}
pub unsafe fn validate(object: &Object, frame: &NativeFrame, pass: u64) -> Result<(), u64> {
    unsafe {
        let c = coordinator();
        let s = &*state();
        let generation = if pass == 0 { 1 } else { 2 };
        let code = if pass == 0 { CODE_VA } else { CODE_NEXT };
        if object.version != VERSION
            || object.profile != PROFILE
            || object.size != size_of::<Object>() as u32
            || object.reserved != 0
            || object.entry_offset != 0
            || object.code_length != 24
            || object.code_address != code
            || object.kind != if pass == 0 { LIVE_LOAD } else { LIVE_OR }
            || object.configured_vcpus as u64 != c.participants
            || object.vcpu_id as u64 >= c.participants
            || m::owner_mode(c.mixed_profile, object.vcpu_id as u64) != Some(s.mode)
            || m::mode(&*guest(), s.a20) != Some(s.mode)
            || s.context != s.mode + 1
            || s.incarnation != 1
            || frame.sp != STACK_TOP
            || frame.x[28] != BANK_VA
            || frame.reserved != 0
            || frame.x[6] != DATA_VA + object.vcpu_id as u64 * 256
            || frame.x[5] != frame.x[6] + 128
            || frame.x[4] > u32::MAX as u64
            || frame.x[7] > u32::MAX as u64
            || core::slice::from_raw_parts(code as *const u8, 20) != m::body(generation).unwrap()
            || core::slice::from_raw_parts((code + 20) as *const u8, 4) != RET
        {
            return Err(212);
        }
        Ok(())
    }
}
// Each owner's code pages have private backing; publication never modifies a peer's slot.
unsafe fn publish(address: u64, body: &[u8; 20]) {
    unsafe {
        if !matches!(address, CODE_VA | CODE_NEXT) {
            stop(PANIC, 213);
        }
        let offset = address - PRIVATE;
        let at = (PRIVATE + 0x14000 + offset / 4096 * 8) as *mut u64;
        let pa = IPA + id() * RAM_SIZE as u64 + offset;
        let attrs = 3 | 1 << 10 | 3 << 8;
        let replace = |value| {
            ptr::write_volatile(at, 0);
            asm!("dsb ishst", "tlbi vmalle1", "dsb ish", "isb");
            ptr::write_volatile(at, value);
            asm!("dsb ishst", "tlbi vmalle1", "dsb ish", "isb");
        };
        replace(pa | attrs | 3 << 53);
        ptr::copy_nonoverlapping(body.as_ptr(), address as *mut u8, 20);
        ptr::copy_nonoverlapping(RET.as_ptr(), (address + 20) as *mut u8, 4);
        for off in (0..24).step_by(4) {
            asm!("dc cvau, {0}", in(reg)address+off);
        }
        asm!("dsb ish");
        for off in (0..24).step_by(4) {
            asm!("ic ivau, {0}", in(reg)address+off);
        }
        asm!("dsb ish", "isb");
        replace(pa | attrs | 1 << 7);
    }
}
unsafe fn request(generation: u64, serial: u64) -> wire::Packet {
    unsafe {
        let s = &*state();
        m::request(
            (*shared()).lifecycle.vm,
            id(),
            s.incarnation,
            generation,
            s.context,
            s.mode,
            serial,
        )
        .unwrap_or_else(|| stop(REJECTED, 214))
    }
}
unsafe fn exchange(expected: wire::Packet, action: u64) -> wire::Packet {
    unsafe {
        let mut packet = expected;
        packet[0] = action;
        write_record(&raw mut (*state()).expected, packet);
        service(&mut packet);
        if action == DEFER_COMPILE {
            write_record(&raw mut (*state()).expected, expected);
            return packet;
        }
        while packet[0] == wire::BUSY {
            events::wait_check();
            for _ in 0..16 {
                asm!("yield", options(nomem, nostack));
            }
            packet = expected;
            packet[0] = POLL_COMPILE;
            write_record(&raw mut (*state()).expected, packet);
            service(&mut packet);
        }
        write_record(&raw mut (*state()).expected, expected);
        packet
    }
}
unsafe fn fetch(generation: u64) {
    unsafe {
        let route = resolve(m::RIP, 6, Access::Fetch);
        if route.gpa() != m::RIP {
            stop(PANIC, 215);
        }
        (*state()).fetch_gpa[generation as usize - 1] = route.gpa();
        let bytes = read_record((PRIVATE + r::SOURCE_OFFSET as u64) as *const [u8; 6]);
        if Some(bytes) != m::source(generation) {
            stop(REJECTED, 216);
        }
        if generation == 1 {
            (*state()).source_before = bytes;
        } else {
            (*state()).source_after = bytes;
        }
    }
}
#[inline(never)]
unsafe fn prepare() {
    unsafe {
        let c = coordinator();
        if c.participants == 0
            || c.participants > MAX_CPUS as u64
            || c.scenario != 5
            || c.alias_profile != 0
            || c.stack_profile != 0
            || m::owner_mode(c.mixed_profile, id()) != Some((*state()).mode)
        {
            stop(REJECTED, 217);
        }
        (*shared()).service_pc = service_resume as *const () as u64;
        if id() == 0 {
            events::post_all(18);
        }
        {
            let _g = coordinator::lock(c);
            fetch(1);
        }
        let p = request(1, 1);
        (*state()).requests[0] = p;
        let reply = exchange(p, m::MISS);
        (*state()).replies[0] = reply;
        let body = m::validate_reply(&p, &reply).unwrap_or_else(|| stop(REJECTED, 218));
        publish(CODE_VA, &body);
        let deferred = request(1, 2);
        (*state()).requests[1] = deferred;
        if exchange(deferred, DEFER_COMPILE)[0] != wire::BUSY {
            stop(PANIC, 219);
        }
        let mut f = read_record(&raw const (*shared()).initial);
        f.x[16] = CODE_VA;
        f.x[30] = returned as *const () as u64;
        checked(validate(&(*shared()).object, &f, 0));
        write_record(&raw mut (*shared()).initial, f);
        (*shared()).capture_pc = f.x[30];
        recovery::clear_mapping(DATA_VA);
        {
            let g = coordinator::lock(c);
            let route = resolve(f.x[6], 4, Access::Read);
            if route.gpa() != f.x[6] {
                stop(PANIC, 220);
            }
            (*round()).old_gpa = route.gpa();
            (*state()).routes[0] = route.gpa();
            events::initial_boundary(&g, id());
            checked(g.admit(id()));
        }
    }
}
pub unsafe fn initialize() -> ! {
    unsafe {
        prepare();
        let c = coordinator();
        c.ready.fetch_or(1 << id(), Ordering::AcqRel);
        while c.ready.load(Ordering::Acquire) != all() {
            asm!("yield", options(nomem, nostack));
        }
        // Last owner remains admitted until every other owner has queued a fault.
        if id() == c.participants - 1 {
            while c.queued.load(Ordering::Acquire).count_ones() as u64 != c.participants - 1 {
                asm!("yield", options(nomem, nostack));
            }
            checked(coordinator::lock(c).trace(7, id()));
        }
        enter(&raw const (*shared()).initial)
    }
}
pub unsafe fn fault_valid(esr: u64, far: u64, pc: u64, spsr: u64, first: bool) -> bool {
    esr >> 26 == 0x25
        && esr & (1 << 6) != 0
        && if first {
            matches!(esr & 63, 4..=7)
        } else {
            matches!(esr & 63, 12..=15)
        }
        && far == DATA_VA + id() * 256
        && pc == (if first { CODE_VA } else { CODE_NEXT }) + 8
        && spsr & 15 == 4
}
#[inline(never)]
unsafe fn record_fault(first: bool) {
    unsafe {
        let (esr, far, pc, spsr): (u64, u64, u64, u64);
        asm!("mrs {0}, ESR_EL1","mrs {1}, FAR_EL1","mrs {2}, ELR_EL1","mrs {3}, SPSR_EL1",out(reg)esr,out(reg)far,out(reg)pc,out(reg)spsr);
        if !fault_valid(esr, far, pc, spsr, first) || (*shared()).captured.sp != STACK_TOP {
            stop(NATIVE_FAULT, 221);
        }
        materialize(&(*shared()).captured, 2);
        if first {
            if (*round()).faults != 0 {
                stop(PANIC, 222);
            }
            (*round()).faults = 1;
            copy_frame(&raw mut (*round()).fault_frame, &raw const (*shared()).captured);
            (*round()).fault_esr = esr;
            (*round()).fault_far = far;
            (*round()).fault_pc = pc;
            (*round()).fault_spsr = spsr;
        } else {
            if (*round()).dirty_faults != 0 {
                stop(PANIC, 222);
            }
            (*round()).dirty_faults = 1;
            copy_frame(&raw mut (*round()).dirty_frame, &raw const (*shared()).captured);
            (*round()).dirty_esr = esr;
            (*round()).dirty_far = far;
            (*round()).dirty_pc = pc;
            (*round()).dirty_spsr = spsr;
        }
    }
}
unsafe fn retry(first: bool) -> ! {
    unsafe {
        let c = coordinator();
        {
            let g = coordinator::lock(c);
            let address = DATA_VA + id() * 256;
            let route = resolve(address, 4, Access::Write);
            if route.gpa()
                != if first {
                    (*round()).old_gpa
                } else {
                    expected_gpa(address)
                }
            {
                stop(PANIC, 223);
            }
            recovery::install_ram(address, route.gpa(), route.writable());
            if !first {
                checked(g.trace(15, id()));
                events::attempt(&g, id(), 0);
            }
        }
        let (pc, spsr) = if first {
            ((*round()).fault_pc, (*round()).fault_spsr)
        } else {
            ((*round()).dirty_pc, (*round()).dirty_spsr)
        };
        resume_exception(&raw const (*shared()).captured, pc, spsr)
    }
}
pub unsafe fn completed(fault: u64) -> ! {
    unsafe {
        let c = coordinator();
        if (*round()).stage == 3 {
            maintenance::finish(fault);
        }
        let first = (*round()).stage == 0;
        if fault != 0 {
            record_fault(first);
            if !first {
                retry(false);
            }
            {
                let g = coordinator::lock(c);
                checked(g.detach_fault(id()));
                events::attempt(&g, id(), 0);
            }
            loop {
                let selected = {
                    let g = coordinator::lock(c);
                    checked(g.select()) == id()
                };
                if selected {
                    retry(true);
                }
                asm!("yield", options(nomem, nostack));
            }
        }
        materialize(&(*shared()).captured, 6);
        if first {
            copy_frame(&raw mut (*round()).first_frame, &raw const (*shared()).captured);
            copy_record(
                (&raw mut (*state()).old_memory_after).cast(),
                (PRIVATE + DATA_OFFSET as u64 + id() * 256) as *const u8,
                256,
            );
            (*round()).stage = 1;
            {
                let g = coordinator::lock(c);
                checked(g.complete(id()));
                events::attempt(&g, id(), 1);
            }
            remap();
        }
        let address = PRIVATE + DATA_OFFSET as u64 + (*state()).routes[1] - DATA_VA;
        copy_record(
            (&raw mut (*state()).new_memory_after).cast(),
            address as *const u8,
            256,
        );
        {
            let g = coordinator::lock(c);
            let bit = 1 << id();
            if c.active.load(Ordering::Relaxed) & bit == 0 {
                stop(PANIC, 224);
            }
            c.active.fetch_and(!bit, Ordering::Relaxed);
            c.new_references.fetch_and(!bit, Ordering::Relaxed);
            c.new_completed.fetch_or(bit, Ordering::Relaxed);
            checked(g.trace(12, id()));
            events::clear_fixture_controls();
            checked(g.trace(21, id()));
            events::attempt(&g, id(), 2);
        }
        maintenance::start(id())
    }
}
#[inline(never)]
unsafe fn replacement() {
    unsafe {
        let c = coordinator();
        if id() == 0 {
            events::post_all(19);
        }
        let current = request(2, 3);
        (*state()).requests[2] = current;
        let late = exchange((*state()).requests[1], TAKE_COMPILE);
        write_record(&raw mut (*state()).expected, current);
        (*state()).replies[1] = late;
        if m::validate_reply(&current, &late).is_some() {
            stop(PANIC, 225);
        }
        (*state()).stale_replies = 1;
        let reply = exchange(current, m::MISS);
        (*state()).replies[2] = reply;
        let body = m::validate_reply(&current, &reply).unwrap_or_else(|| stop(REJECTED, 226));
        let g = coordinator::lock(c);
        if c.phase.load(Ordering::Relaxed) != COMPILING || c.generation.load(Ordering::Relaxed) != 2
        {
            stop(PANIC, 227);
        }
        fetch(2);
        for field in [3, 8] {
            let mut wrong = reply;
            wrong[field] ^= 1;
            if m::validate_reply(&current, &wrong).is_some() {
                stop(PANIC, 228);
            }
        }
        (*state()).wrong_owner = 1;
        (*state()).wrong_mode = 1;
        if core::slice::from_raw_parts(CODE_NEXT as *const u8, 24) != [0; 24] {
            stop(PANIC, 229);
        }
        (*state()).blank_before_publish = 1;
        publish(CODE_NEXT, &body);
        c.mixed_compiled.fetch_or(1 << id(), Ordering::Relaxed);
        c.stale_replies.fetch_or(1 << id(), Ordering::Relaxed);
        checked(g.trace(16, id()));
        if c.mixed_compiled.load(Ordering::Relaxed) == all() {
            c.published.store(2, Ordering::Relaxed);
            c.phase.store(RELEASING, Ordering::Relaxed);
        }
    }
}
unsafe fn remap() -> ! {
    unsafe {
        let c = coordinator();
        let bit = 1 << id();
        loop {
            {
                let g = coordinator::lock(c);
                if c.phase.load(Ordering::Relaxed) == EXCLUSIVE
                    && c.completed.load(Ordering::Relaxed) == all()
                    && id() == 0
                {
                    aliases::set_leaf((DATA_VA + 4096) | 7);
                    let route = resolve(m::RIP, 6, Access::Write);
                    if route.gpa() != m::RIP {
                        stop(PANIC, 215);
                    }
                    c.old_eligible.store(0, Ordering::Relaxed);
                    ptr::copy_nonoverlapping(
                        m::REPLACEMENT.as_ptr(),
                        (PRIVATE + r::SOURCE_OFFSET as u64) as *mut u8,
                        6,
                    );
                    asm!("dsb ish");
                    c.generation.store(2, Ordering::Relaxed);
                    c.phase.store(COMPILING, Ordering::Relaxed);
                    checked(g.trace(8, id()));
                }
                if c.phase.load(Ordering::Relaxed) == RELEASING
                    && c.acknowledgements.load(Ordering::Relaxed) & bit == 0
                    && (id() != c.participants - 1
                        || c.acknowledgements.load(Ordering::Relaxed) == all() ^ bit)
                {
                    let address = DATA_VA + id() * 256;
                    let route = resolve(address, 4, Access::Read);
                    if route.gpa() != expected_gpa(address) {
                        stop(PANIC, 230);
                    }
                    (*state()).routes[1] = route.gpa();
                    recovery::install_ram(address, route.gpa(), false);
                    asm!("dsb ish", "ic iallu", "dsb ish", "isb");
                    c.acknowledgements.fetch_or(bit, Ordering::Relaxed);
                    checked(g.trace(9, id()));
                    if c.acknowledgements.load(Ordering::Relaxed) == all() {
                        c.phase.store(OPEN, Ordering::Relaxed);
                        checked(g.trace(10, id()));
                    }
                }
            }
            if c.phase.load(Ordering::Acquire) == COMPILING
                && c.mixed_compiled.load(Ordering::Acquire) & bit == 0
            {
                replacement();
            }
            if c.phase.load(Ordering::Acquire) == OPEN {
                break;
            }
            asm!("yield", options(nomem, nostack));
        }
        if id() == c.participants - 1 && c.participants > 1 {
            while c.new_entries.load(Ordering::Acquire) == 0 {
                asm!("yield", options(nomem, nostack));
            }
        }
        {
            let g = coordinator::lock(c);
            if source::eligible(1, CODE_VA) || !source::eligible(2, CODE_NEXT) {
                stop(PANIC, 231);
            }
            c.stale_entries.fetch_or(bit, Ordering::Relaxed);
            if c.retained.load(Ordering::Relaxed) & bit == 0
                || try_reuse_old()
                || core::slice::from_raw_parts(CODE_VA as *const u8, 20) != m::BODY
            {
                stop(PANIC, 232);
            }
            c.reuse_blocked.fetch_or(bit, Ordering::Relaxed);
            checked(g.trace(17, id()));
            c.retained.fetch_and(!bit, Ordering::Relaxed);
            checked(g.trace(6, id()));
            if !try_reuse_old() {
                stop(PANIC, 233);
            }
            if c.retained.load(Ordering::Relaxed) == 0 {
                c.reclaimed.store(1, Ordering::Relaxed);
                checked(g.trace(14, id()));
            }
            events::attempt(&g, id(), 1);
            c.active.fetch_or(bit, Ordering::Relaxed);
            c.new_references.fetch_or(bit, Ordering::Relaxed);
            c.new_entries.fetch_or(bit, Ordering::Release);
            checked(g.trace(11, id()));
        }
        start_new()
    }
}
// Caller holds coordinator authority. No peer holds this private code slot.
unsafe fn try_reuse_old() -> bool {
    unsafe {
        let c = coordinator();
        let bit = 1 << id();
        if c.phase.load(Ordering::Relaxed) != OPEN
            || c.old_eligible.load(Ordering::Relaxed) != 0
            || c.published.load(Ordering::Relaxed) != 2
            || c.mixed_compiled.load(Ordering::Relaxed) & bit == 0
            || c.retained.load(Ordering::Relaxed) & bit != 0
            || c.active.load(Ordering::Relaxed) & bit != 0
        {
            return false;
        }
        publish(CODE_VA, &[0; 20]);
        true
    }
}
#[inline(never)]
unsafe fn start_new() -> ! {
    unsafe {
        let s = shared();
        let mut f = read_record(&raw const (*s).captured);
        f.x[16] = CODE_NEXT;
        f.x[30] = returned as *const () as u64;
        f.sp = STACK_TOP;
        (*s).object.kind = LIVE_OR;
        (*s).object.code_address = CODE_NEXT;
        checked(validate(&(*s).object, &f, 1));
        let address = PRIVATE + DATA_OFFSET as u64 + (*state()).routes[1] - DATA_VA;
        copy_record(
            (&raw mut (*state()).new_memory_before).cast(),
            address as *const u8,
            256,
        );
        write_record(&raw mut (*round()).new_initial, f);
        write_record(&raw mut (*s).initial, f);
        (*round()).stage = 2;
        enter(&raw const (*s).initial)
    }
}
