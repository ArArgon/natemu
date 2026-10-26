//! Generated instruction boundaries retain owner-specific segmented routes.
use super::*;
use natemu_abi::{
    address_concurrent as a, maintenance as m, paging::Control, paging32, recovery as r,
    translation as wire,
};
unsafe extern "C" {
    fn service(packet: *mut wire::Packet);
    fn service_resume();
}
fn owner() -> *mut a::Owner {
    (PRIVATE + a::OWNER_OFFSET as u64) as *mut a::Owner
}
fn common() -> *mut a::Shared {
    (PRIVATE + a::SHARED_OFFSET as u64) as *mut a::Shared
}
fn guest() -> *mut r::GuestState {
    r::GUEST_VA as *mut r::GuestState
}
fn pressure() -> *mut m::State {
    m::VA as *mut m::State
}
fn id() -> u64 {
    unsafe { (*owner()).id }
}
fn config() -> a::Config {
    unsafe {
        a::Config::new((*common()).n, (*common()).profile, (*common()).actor)
            .unwrap_or_else(|| stop(REJECTED, 400))
    }
}
fn all() -> u64 {
    (1 << config().n) - 1
}
fn visit() -> *mut a::Visit {
    unsafe {
        (PRIVATE
            + a::RECORD_OFFSET as u64
            + (*owner()).current_visit * size_of::<a::Visit>() as u64) as *mut a::Visit
    }
}
pub fn enabled() -> bool {
    unsafe { ptr::read_volatile(&raw const (*owner()).enabled) == 1 }
}
unsafe fn frame(to: *mut NativeFrame, from: *const NativeFrame) {
    unsafe {
        copy_record(to.cast(), from.cast(), size_of::<NativeFrame>());
    }
}
unsafe fn canonical(to: *mut r::GuestState, from: *const r::GuestState) {
    unsafe {
        copy_record(to.cast(), from.cast(), size_of::<r::GuestState>());
    }
}
fn phys(gpa: u64, bytes: u64) -> *mut u8 {
    (PRIVATE + a::ram(config(), gpa, bytes).unwrap_or_else(|| stop(REJECTED, 401)) as u64)
        as *mut u8
}
fn leaf(index: u64) -> *mut u32 {
    (PRIVATE + r::TABLE_OFFSET as u64 + 4096 + index * 4) as *mut u32
}
struct Tables;
impl paging32::Tables for Tables {
    fn read32(&mut self, gpa: u64) -> Option<u32> {
        if !(0x1000..0x5000).contains(&gpa) || gpa & 3 != 0 {
            return None;
        }
        unsafe {
            (*owner()).reads += 1;
            Some(ptr::read_volatile(
                (PRIVATE + r::TABLE_OFFSET as u64 + gpa - 0x1000) as *const u32,
            ))
        }
    }
    fn write32(&mut self, gpa: u64, value: u32) -> bool {
        if !(0x1000..0x5000).contains(&gpa) || gpa & 3 != 0 {
            return false;
        }
        unsafe {
            (*owner()).writes += 1;
            ptr::write_volatile(
                (PRIVATE + r::TABLE_OFFSET as u64 + gpa - 0x1000) as *mut u32,
                value,
            );
            asm!("dsb ish");
        }
        true
    }
}
// Caller holds shared coordinator authority; A20 follows the final guest walk.
unsafe fn resolve(
    segment: usize,
    offset: u64,
    bytes: u64,
    access: Access,
) -> Result<a::Address, a::AddressError> {
    unsafe {
        let mut out = a::form((*guest()).segments[segment], offset, bytes, access)?;
        out.raw = if (*owner()).mode == 0 {
            out.linear
        } else {
            (*owner()).walks += 1;
            match paging32::walk(
                &mut Tables,
                Control {
                    cr3: (*guest()).control[2],
                    cpl: 0,
                    wp: 1,
                    nxe: 0,
                    smep: 0,
                },
                out.linear,
                access,
            ) {
                Ok(t) if t.wb => t.gpa,
                _ => return Err(a::AddressError::Unsupported),
            }
        };
        out.gpa = config().mask(out.raw);
        if a::ram(config(), out.gpa, bytes).is_none() {
            return Err(a::AddressError::Unsupported);
        }
        Ok(out)
    }
}
unsafe fn materialize(offset: u64) {
    unsafe {
        let f = &(*owner()).current;
        let g = &mut *guest();
        for (i, native) in [
            (0, 4),
            (1, 7),
            (2, 5),
            (3, 6),
            (4, 8),
            (5, 9),
            (6, 10),
            (7, 11),
        ] {
            g.gpr[i] = f.x[native] & 0xffff_ffff;
        }
        g.rip = a::EIP + offset;
    }
}
fn code() -> u64 {
    unsafe {
        if (*owner()).generation == 1 {
            CODE_VA
        } else {
            CODE_NEXT
        }
    }
}
unsafe fn publish(address: u64, bytes: &[u8]) {
    unsafe {
        if !matches!(address, CODE_VA | CODE_NEXT) || bytes.len() != a::CODE_BYTES {
            stop(PANIC, 402)
        }
        let offset = address - PRIVATE;
        let pte = (PRIVATE + 0x14000 + offset / 4096 * 8) as *mut u64;
        let pa = IPA + id() * RAM_SIZE as u64 + offset;
        let attrs = 3 | 1 << 10 | 3 << 8;
        let replace = |value| {
            ptr::write_volatile(pte, 0);
            asm!("dsb ishst", "tlbi vmalle1", "dsb ish", "isb");
            ptr::write_volatile(pte, value);
            asm!("dsb ishst", "tlbi vmalle1", "dsb ish", "isb");
        };
        replace(pa | attrs | 3 << 53);
        copy_record(address as *mut u8, bytes.as_ptr(), bytes.len());
        for off in (0..bytes.len()).step_by(4) {
            asm!("dc cvau, {0}",in(reg)address+off as u64);
        }
        asm!("dsb ish");
        for off in (0..bytes.len()).step_by(4) {
            asm!("ic ivau, {0}",in(reg)address+off as u64);
        }
        asm!("dsb ish", "isb");
        replace(pa | attrs | 1 << 7);
    }
}
unsafe fn eligible(step: usize) -> bool {
    unsafe {
        (*owner()).generation == coordinator().generation.load(Ordering::Relaxed)
            && (*owner()).code_context == (*owner()).context
            && (if (*owner()).generation == 1 {
                coordinator().retained.load(Ordering::Relaxed)
            } else {
                coordinator().new_references.load(Ordering::Relaxed)
            }) & (1 << id())
                != 0
            && a::body(config().profile, (*owner()).generation, step).is_some_and(|body| {
                core::slice::from_raw_parts(
                    (code() + step as u64 * a::SLOT_BYTES as u64) as *const u8,
                    body.len(),
                ) == body
            })
    }
}
unsafe fn compiler_wait() {
    unsafe {
        let e = EVENT_VA as *mut EventRecord;
        let c = coordinator();
        if (*e).wait_checks == 0 {
            // Repeated notifications identify the same pending level request.
            for _ in 0..2 {
                let _ = c.event_pending[id() as usize].compare_exchange(
                    0,
                    1,
                    Ordering::AcqRel,
                    Ordering::Acquire,
                );
                c.event_notifications[id() as usize].fetch_add(1, Ordering::Release);
            }
        }
        events::wait_check();
    }
}
unsafe fn exchange(expected: wire::Packet, action: u64) -> wire::Packet {
    unsafe {
        let mut packet = expected;
        packet[0] = action;
        (*owner()).expected = packet;
        (*shared()).service_pc = service_resume as *const () as u64;
        service(&mut packet);
        if action != DEFER_COMPILE {
            while packet[0] == wire::BUSY {
                compiler_wait();
                packet = expected;
                packet[0] = POLL_COMPILE;
                (*owner()).expected = packet;
                service(&mut packet);
            }
        }
        (*owner()).expected = expected;
        packet
    }
}
unsafe fn packet(step: usize, fetch: a::Address) -> wire::Packet {
    unsafe {
        (*owner()).serial = (*owner())
            .serial
            .checked_add(1)
            .unwrap_or_else(|| stop(PANIC, 403));
        a::request(
            (*shared()).lifecycle.vm,
            config(),
            id(),
            (*owner()).generation,
            (*owner()).serial,
            (*owner()).terminal != 0,
            step,
            fetch.gpa,
        )
        .unwrap_or_else(|| stop(REJECTED, 404))
    }
}
unsafe fn source_matches(address: a::Address, step: usize) -> bool {
    unsafe {
        let bytes = a::source(config().profile, (*owner()).generation, step).unwrap();
        core::slice::from_raw_parts(phys(address.gpa, bytes.len() as u64), bytes.len()) == bytes
    }
}
#[inline(never)]
unsafe fn compile(step: usize, fetch: a::Address) {
    unsafe {
        let request = packet(step, fetch);
        (*visit()).request = request;
        let reply = exchange(request, a::MISS);
        (*visit()).reply = reply;
        if Some(reply) != a::reply(&request) {
            stop(REJECTED, 405)
        }
        let mut image = [0; a::CODE_BYTES];
        copy_record(image.as_mut_ptr(), code() as *const u8, a::CODE_BYTES);
        let start = step * a::SLOT_BYTES;
        if image[start..start + a::SLOT_BYTES] != [0; a::SLOT_BYTES] {
            stop(PANIC, 406)
        }
        let body = a::body(config().profile, (*owner()).generation, step).unwrap();
        image[start..start + body.len()].copy_from_slice(body);
        image[start + body.len()..start + body.len() + 4].copy_from_slice(&RET);
        publish(code(), &image);
        (*owner()).code_context = (*owner()).context;
        if (*owner()).program == 0 && step == 0 {
            let deferred = packet(step, fetch);
            (*owner()).deferred = deferred;
            if exchange(deferred, DEFER_COMPILE) != wire::command(wire::BUSY) {
                stop(REJECTED, 407)
            }
        }
    }
}
unsafe fn event_boundary(g: &crate::coordinator::Guard<'_>) {
    unsafe {
        let e = EVENT_VA as *mut EventRecord;
        events::attempt(g, id(), (*owner()).visits);
        if (*e).shadow != 0 || (*e).priority != 0 {
            (*e).shadow = 0;
            (*e).priority = 0;
            checked(g.trace(22, id()));
            events::attempt(g, id(), (*owner()).visits);
        }
    }
}
#[inline(never)]
pub unsafe fn initialize() -> ! {
    unsafe {
        let c = coordinator();
        let o = owner();
        let cfg = config();
        let sid = (*shared()).object.vcpu_id as u64;
        if cfg.n != c.participants
            || sid != (*o).id
            || (*o).mode != cfg.mode(sid)
            || (*o).context != cfg.context(sid, false)
            || (*o).generation != 1
            || (*o).visits != 0
            || (*guest()).control[0] != (if (*o).mode == 0 { 0x10001 } else { 0x80010001 })
            || (*guest()).control[2] != 0x1000
            || (*guest()).control[3] != 0x600
            || (*guest()).control[4] != 0
            || (*guest()).segments[0] != cfg.cs(sid, false)
            || (*guest()).segments[1] != cfg.ds(sid)
            || (*guest()).rip != a::EIP
            || (*shared()).initial.x[6] != cfg.ebx(sid, false)
            || (*shared()).initial.x[5] != cfg.edx(sid)
            || (*shared()).initial.sp != STACK_TOP
            || (*shared()).initial.x[28] != BANK_VA
            || (*shared()).lifecycle.vm == 0
        {
            stop(REJECTED, 408)
        }
        frame(&raw mut (*o).initial, &raw const (*shared()).initial);
        frame(&raw mut (*o).current, &raw const (*o).initial);
        canonical(&raw mut (*o).guest_before, guest());
        {
            let g = coordinator::lock(c);
            checked(g.admit(id()));
            c.ready.fetch_or(1 << id(), Ordering::Release);
        }
        while c.ready.load(Ordering::Acquire) != all() {
            core::hint::spin_loop();
        }
        begin_step()
    }
}
#[inline(never)]
unsafe fn begin_step() -> ! {
    unsafe {
        let o = owner();
        let step = (*o).step as usize;
        if step >= 3 || (*o).visits as usize >= a::MAX_VISITS {
            stop(PANIC, 409)
        }
        (*o).current_visit = (*o).visits;
        (*o).visits += 1;
        let v = visit();
        (*v).program = (*o).program;
        (*v).step = step as u64;
        (*v).generation = (*o).generation;
        (*v).terminal = (*o).terminal;
        materialize(a::offset(config().profile, step).unwrap());
        let fetch;
        {
            let g = coordinator::lock(coordinator());
            let bytes = a::source(config().profile, (*o).generation, step).unwrap();
            fetch = match resolve(0, (*guest()).rip, bytes.len() as u64, Access::Fetch) {
                Ok(a) => a,
                Err(a::AddressError::GeneralProtection) => {
                    drop(g);
                    terminal_fault(0)
                }
                _ => stop(REJECTED, 410),
            };
            (*v).fetch = fetch;
            if !source_matches(fetch, step) {
                stop(REJECTED, 411)
            }
            checked(g.trace(60, id()));
            if step != 0 {
                let offset = a::operand(config().profile, &(*o).current, step).unwrap();
                match a::form(
                    (*guest()).segments[1],
                    offset,
                    4,
                    if step == 1 {
                        Access::Write
                    } else {
                        Access::Read
                    },
                ) {
                    Ok(a) => (*v).access = a,
                    Err(a::AddressError::GeneralProtection) => {
                        drop(g);
                        terminal_fault(offset)
                    }
                    _ => stop(REJECTED, 412),
                }
            }
        }
        if (*o).program <= 1 || (*o).terminal != 0 {
            compile(step, fetch);
        }
        {
            let g = coordinator::lock(coordinator());
            if !eligible(step) || !source_matches(fetch, step) {
                stop(REJECTED, 413)
            }
            if step != 0 {
                recovery::clear_mapping((*v).access.offset);
            }
            checked(g.trace(64 + step as u64, id()));
        }
        (*o).current.x[16] = code() + step as u64 * a::SLOT_BYTES as u64;
        (*o).current.x[30] = returned as *const () as u64;
        (*o).current.sp = STACK_TOP;
        frame(&raw mut (*shared()).initial, &raw const (*o).current);
        (*shared()).capture_pc = returned as *const () as u64;
        (*v).entered = 1;
        enter(&raw const (*shared()).initial)
    }
}
#[inline(never)]
unsafe fn service_access() -> ! {
    unsafe {
        let o = owner();
        let v = visit();
        let step = (*o).step as usize;
        let g = coordinator::lock(coordinator());
        let address = if (*o).program == 0 {
            if step == 1 {
                (*o).saved_store
            } else {
                (*o).saved_load
            }
        } else if (*o).program == 6 && id() != config().actor {
            if step == 1 {
                (*o).saved_store
            } else {
                (*o).saved_load
            }
        } else {
            resolve(
                1,
                (*v).access.offset,
                4,
                if step == 1 {
                    Access::Write
                } else {
                    Access::Read
                },
            )
            .unwrap_or_else(|_| stop(REJECTED, 414))
        };
        (*v).access = address;
        let ptr = phys(address.gpa, 4);
        copy_record((&raw mut (*v).memory_before).cast(), ptr, 4);
        let value = if step == 1 {
            (*o).current.x[if (*o).generation == 1 { 7 } else { 5 }] as u32
        } else {
            u32::from_le_bytes((*v).memory_before)
        };
        if step == 1 {
            copy_record(ptr, value.to_le_bytes().as_ptr(), 4);
        } else {
            (*o).current.x[4] = value as u64;
        }
        copy_record((&raw mut (*v).memory_after).cast(), ptr, 4);
        (*v).value = value as u64;
        (*v).service = 1;
        (*v).continuation = (*v).pc + 4;
        checked(g.trace(72 + step as u64, id()));
        drop(g);
        resume_exception(&raw const (*o).current, (*v).continuation, (*v).spsr)
    }
}
#[inline(never)]
pub unsafe fn finish(fault: u64) -> ! {
    unsafe {
        let o = owner();
        let v = visit();
        let step = (*o).step as usize;
        if (*v).entered != 1 || (*v).completed != 0 {
            stop(PANIC, 415)
        }
        frame(&raw mut (*o).current, &raw const (*shared()).captured);
        if fault != 0 {
            let (esr, far, pc, spsr): (u64, u64, u64, u64);
            asm!("mrs {0},ESR_EL1","mrs {1},FAR_EL1","mrs {2},ELR_EL1","mrs {3},SPSR_EL1",out(reg)esr,out(reg)far,out(reg)pc,out(reg)spsr);
            let (site, reg) =
                a::site(config().profile, step).unwrap_or_else(|| stop(NATIVE_FAULT, 416));
            if esr >> 26 != 0x25
                || (esr >> 6) & 1 != u64::from(step == 1)
                || esr & ((1 << 7) | (1 << 8) | (1 << 10)) != 0
                || !matches!(esr & 63, 4..=7)
                || far != (*v).access.offset
                || (*o).current.x[reg] != far
                || pc != code() + step as u64 * a::SLOT_BYTES as u64 + site
                || spsr & 15 != 4
                || (*v).service != 0
            {
                stop(NATIVE_FAULT, 417)
            }
            (*v).esr = esr;
            (*v).far = far;
            (*v).pc = pc;
            (*v).spsr = spsr;
            frame(&raw mut (*v).fault, &raw const (*o).current);
            if step == 1 && ((*o).program == 0 || ((*o).program == 6 && id() != config().actor)) {
                let c = coordinator();
                if (*o).program == 0 && id() == config().actor {
                    while c.queued.load(Ordering::Acquire) != all() ^ (1 << id()) {
                        core::hint::spin_loop();
                    }
                }
                {
                    let g = coordinator::lock(c);
                    (*o).saved_store = resolve(1, (*v).access.offset, 4, Access::Write)
                        .unwrap_or_else(|_| stop(REJECTED, 418));
                    let load = a::operand(config().profile, &(*o).current, 2).unwrap();
                    (*o).saved_load =
                        resolve(1, load, 4, Access::Read).unwrap_or_else(|_| stop(REJECTED, 418));
                    if (*o).program == 0 {
                        checked(g.detach_fault(id()));
                    } else {
                        let s = common();
                        let index = (*s).terminal_length as usize;
                        if index >= 4 {
                            stop(PANIC, 419)
                        }
                        (*s).terminal_queue[index] = id();
                        (*s).terminal_length += 1;
                        (*s).terminal_queued |= 1 << id();
                        c.active.fetch_and(!(1 << id()), Ordering::Relaxed);
                        checked(g.trace(92, id()));
                    }
                }
                loop {
                    let g = coordinator::lock(c);
                    let selected = if (*o).program == 0 {
                        checked(g.select()) == id()
                    } else {
                        let s = common();
                        if (*s).terminal_done & (1 << config().actor) != 0
                            && (*s).terminal_servicing == NO_CPU
                            && (*s).terminal_head < (*s).terminal_length
                        {
                            (*s).terminal_servicing =
                                (*s).terminal_queue[(*s).terminal_head as usize];
                            checked(g.trace(93, (*s).terminal_servicing));
                        }
                        (*s).terminal_servicing == id()
                    };
                    drop(g);
                    if selected {
                        break;
                    }
                    core::hint::spin_loop();
                }
            }
            service_access()
        }
        frame(&raw mut (*v).prefix, &raw const (*o).current);
        (*v).completed = 1;
        materialize(a::offset(config().profile, step + 1).unwrap());
        {
            let g = coordinator::lock(coordinator());
            checked(g.trace(68 + step as u64, id()));
        }
        if step == 2 {
            program_done()
        }
        (*o).step += 1;
        begin_step()
    }
}
unsafe fn acknowledge() -> [u64; 6] {
    unsafe {
        let offset = a::operand(config().profile, &(*owner()).current, 1).unwrap();
        recovery::clear_mapping(offset);
        let load = a::operand(config().profile, &(*owner()).current, 2).unwrap();
        recovery::clear_mapping(load);
        let route = resolve(1, offset, 4, Access::Read).unwrap_or_else(|_| stop(REJECTED, 420));
        let native = ptr::read_volatile(
            (PRIVATE + shadow::leaf_offset(offset & !4095).unwrap() as u64) as *const u64,
        );
        if native != 0 {
            stop(PANIC, 421)
        }
        [
            (*pressure()).epoch,
            route.offset,
            route.linear,
            route.raw,
            route.gpa,
            native,
        ]
    }
}
#[inline(never)]
unsafe fn source_cut() -> ! {
    unsafe {
        let c = coordinator();
        let s = common();
        let o = owner();
        loop {
            let g = coordinator::lock(c);
            if (*s).old_done == all() {
                if id() == 0 && (*s).source_changed == 0 {
                    if c.active.load(Ordering::Relaxed) != 0
                        || c.servicing.load(Ordering::Relaxed) != NO_CPU
                    {
                        stop(PANIC, 422)
                    }
                    let cfg = config();
                    let source = cfg.fetch(id(), false, 0).unwrap();
                    let at = if cfg.profile == 3 { 4 } else { 3 };
                    (*s).source_leaf_before = ptr::read_volatile(leaf(2)) as u64;
                    let offset = ((source.linear + at) as u32)
                        .wrapping_sub((*guest()).segments[1][1] as u32)
                        as u64;
                    let route = resolve(1, offset, 1, Access::Write)
                        .unwrap_or_else(|_| stop(REJECTED, 423));
                    if route.gpa != source.gpa + at {
                        stop(PANIC, 424)
                    }
                    (*s).source_write = route;
                    copy_record((&raw mut (*s).source_before).cast(), phys(source.gpa, 8), 8);
                    ptr::write_volatile(
                        phys(route.gpa, 1),
                        if cfg.profile == 3 { 0x10 } else { 0x13 },
                    );
                    copy_record((&raw mut (*s).source_after).cast(), phys(source.gpa, 8), 8);
                    (*s).source_leaf_after = ptr::read_volatile(leaf(2)) as u64;
                    (*s).data_leaf_before = ptr::read_volatile(leaf(cfg.data_leaf())) as u64;
                    ptr::write_volatile(leaf(cfg.data_leaf()), (cfg.data_raw(true) | 7) as u32);
                    asm!("dsb ish");
                    (*s).data_leaf_after = ptr::read_volatile(leaf(cfg.data_leaf())) as u64;
                    c.generation.store(2, Ordering::Relaxed);
                    c.old_eligible.store(0, Ordering::Relaxed);
                    (*s).source_changed = 1;
                    checked(g.trace(80, id()));
                }
                if (*s).source_changed != 0 {
                    let delayed = (config().actor + config().n - 1) % config().n;
                    if (*s).acknowledgements & (1 << id()) == 0
                        && (id() != delayed || (*s).acknowledgements == all() ^ (1 << id()))
                    {
                        (*o).source_ack = acknowledge();
                        (*s).acknowledgements |= 1 << id();
                        checked(g.trace(81, id()));
                    }
                    if (*s).acknowledgements == all() {
                        drop(g);
                        break;
                    }
                }
            }
            drop(g);
            core::hint::spin_loop();
        }
        let deferred = (*o).deferred;
        let stale = exchange(deferred, TAKE_COMPILE);
        (*o).stale_reply = stale;
        if Some(stale) != a::reply(&deferred) {
            stop(REJECTED, 425)
        }
        let mut current = deferred;
        current[5] = 2;
        if Some(stale) == a::reply(&current) {
            stop(PANIC, 426)
        }
        (*o).stale_rejected = 1;
        let mut wrong = a::reply(&current).unwrap();
        wrong[3] ^= 1;
        if Some(wrong) == a::reply(&current) {
            stop(PANIC, 427)
        }
        (*o).wrong_owner_rejected = 1;
        {
            let g = coordinator::lock(c);
            checked(g.trace(82, id()));
            if eligible(0) {
                stop(PANIC, 428)
            }
            (*o).entry_rejected |= 1;
            copy_record(
                (&raw mut (*s).code_before[id() as usize]).cast(),
                CODE_VA as *const u8,
                a::CODE_BYTES,
            );
            (*o).retained_before_release = c.retained.load(Ordering::Relaxed);
            c.retained.fetch_and(!(1 << id()), Ordering::Relaxed);
            (*s).released |= 1 << id();
            checked(g.trace(83, id()));
            publish(CODE_VA, &[0; a::CODE_BYTES]);
            copy_record(
                (&raw mut (*s).code_after[id() as usize]).cast(),
                CODE_VA as *const u8,
                a::CODE_BYTES,
            );
            (*o).reclaimed = 1;
            (*s).reclaimed |= 1 << id();
            checked(g.trace(84, id()));
        }
        loop {
            let g = coordinator::lock(c);
            if (*s).reclaimed == all() {
                c.reclaimed.store(1, Ordering::Relaxed);
                c.phase.store(OPEN, Ordering::Relaxed);
                c.published.store(2, Ordering::Relaxed);
                c.new_references.fetch_or(1 << id(), Ordering::Relaxed);
                c.active.fetch_or(1 << id(), Ordering::Relaxed);
                drop(g);
                break;
            }
            drop(g);
            core::hint::spin_loop();
        }
        (*o).generation = 2;
        (*o).program = 1;
        (*o).step = 0;
        begin_step()
    }
}
#[inline(never)]
unsafe fn program_done() -> ! {
    unsafe {
        let c = coordinator();
        let o = owner();
        let program = (*o).program;
        {
            let g = coordinator::lock(c);
            canonical(&raw mut (*o).guest_after, guest());
            if program == 0 {
                checked(g.complete(id()));
                (*common()).old_done |= 1 << id();
            } else if program == 1 {
                c.active.fetch_and(!(1 << id()), Ordering::Relaxed);
                (*common()).new_done |= 1 << id();
                event_boundary(&g);
                checked(g.trace(85, id()));
            } else if program < 6 {
                if (*pressure()).ordinary != id() || c.active.load(Ordering::Relaxed) != 1 << id() {
                    stop(PANIC, 429)
                }
                c.active.store(0, Ordering::Relaxed);
                checked((*pressure()).finish_ordinary(id()));
                event_boundary(&g);
                checked(g.trace(30, id()));
            } else {
                let s = common();
                if id() == config().actor || (*s).terminal_servicing != id() {
                    stop(PANIC, 430)
                }
                (*s).terminal_done |= 1 << id();
                (*s).terminal_head += 1;
                (*s).terminal_servicing = NO_CPU;
                checked(g.trace(95, id()));
            }
        }
        if program == 0 {
            source_cut()
        }
        if program == 6 {
            finish_all()
        }
        pressure_loop()
    }
}
#[inline(never)]
unsafe fn pressure_loop() -> ! {
    unsafe {
        let c = coordinator();
        let p = pressure();
        let s = common();
        {
            let _g = coordinator::lock(c);
            (*p).ready |= 1 << id();
        }
        loop {
            let g = coordinator::lock(c);
            if (*s).new_done == all() && (*p).ready == all() {
                if (*s).maintenance_started == 0 {
                    (*s).maintenance_started = 1;
                    (*p).compose_leaf = ptr::read_volatile(leaf(config().data_leaf())) as u64;
                }
                if (*p).finished != 0 {
                    drop(g);
                    if config().profile >= 4 {
                        terminal_start()
                    } else {
                        finish_all()
                    }
                }
                if (*p).pending[id() as usize] == 0 && (*p).ordinary != id() {
                    if checked((*p).enqueue(id() as usize)).is_some() {
                        let serial = (*p).serial;
                        let len = (*p).length;
                        if (*p).enqueue(id() as usize) != Err(m::DUPLICATE)
                            || (*p).serial != serial
                            || (*p).length != len
                        {
                            stop(PANIC, 431)
                        }
                        (*p).duplicate_rejections += 1;
                        checked(g.trace(23, id()));
                    }
                }
                if (*p).maximum_depth == (*p).n
                    && (*p).current.serial == 0
                    && (*p).ordinary == NO_CPU
                {
                    if c.active.load(Ordering::Relaxed) != 0 {
                        stop(PANIC, 432)
                    }
                    if (*p).ordinary_due() {
                        let cpu = checked((*p).begin_ordinary());
                        (*p).closing = 1;
                        c.phase.store(OPEN, Ordering::Relaxed);
                        c.active.store(1 << cpu, Ordering::Relaxed);
                        checked(g.trace(29, cpu));
                    } else if let Some(request) = checked((*p).start_head()) {
                        c.phase.store(EXCLUSIVE, Ordering::Relaxed);
                        let old =
                            ptr::read_volatile(leaf(config().data_leaf())) as u64 & 0xfffff000;
                        let next = config().data_raw(old == config().data_raw(false));
                        ptr::write_volatile(leaf(config().data_leaf()), (next | 7) as u32);
                        asm!("dsb ish");
                        checked(g.trace(25, request.cpu));
                    }
                }
                if (*p).current.serial != 0
                    && (*p).acknowledgements & (1 << id()) == 0
                    && (id() != (*p).epoch % (*p).n || (*p).acknowledgements == all() ^ (1 << id()))
                {
                    if c.active.load(Ordering::Relaxed) != 0 {
                        stop(PANIC, 433)
                    }
                    (*owner()).maintenance_acks[(*p).total as usize] = acknowledge();
                    checked((*p).acknowledge(id() as usize));
                    checked(g.trace(26, id()));
                    if (*p).acknowledgements == all() {
                        (*p).leaf_history[(*p).total as usize] =
                            ptr::read_volatile(leaf(config().data_leaf())) as u64;
                        checked((*p).complete_head());
                        checked(g.trace(27, id()));
                        c.phase.store(CLOSING, Ordering::Relaxed);
                    }
                }
                if (*p).ordinary == id() {
                    let index = (*p).intervals[id() as usize];
                    if index >= 4 {
                        stop(PANIC, 434)
                    }
                    (*owner()).program = 2 + index;
                    (*owner()).step = 0;
                    drop(g);
                    begin_step()
                }
            }
            drop(g);
            core::hint::spin_loop();
        }
    }
}
#[inline(never)]
unsafe fn terminal_start() -> ! {
    unsafe {
        let c = coordinator();
        let s = common();
        let o = owner();
        {
            let g = coordinator::lock(c);
            if (*s).terminal_ready == 0 {
                if c.active.load(Ordering::Relaxed) != 0 {
                    stop(PANIC, 435)
                }
                copy_record(
                    (&raw mut (*s).terminal_tables_before).cast(),
                    (PRIVATE + r::TABLE_OFFSET as u64) as *const u8,
                    16384,
                );
                copy_record(
                    (&raw mut (*s).terminal_data_before).cast(),
                    (PRIVATE + DATA_OFFSET as u64) as *const u8,
                    8192,
                );
                (*s).terminal_servicing = NO_CPU;
            }
            (*o).program = 6;
            (*o).step = 0;
            if id() == config().actor {
                (*o).terminal = 1;
                (*o).context = config().context(id(), true);
                (*guest()).segments[0] = config().cs(id(), true);
                (*o).current.x[6] = config().ebx(id(), true);
                if eligible(0) {
                    stop(PANIC, 436)
                }
                (*o).entry_rejected |= 2;
                publish(CODE_NEXT, &[0; a::CODE_BYTES]);
            }
            materialize(0);
            frame(&raw mut (*o).terminal_initial, &raw const (*o).current);
            (*s).terminal_ready |= 1 << id();
            c.active.fetch_or(1 << id(), Ordering::Relaxed);
            c.phase.store(OPEN, Ordering::Relaxed);
            checked(g.trace(90, id()));
        }
        loop {
            let g = coordinator::lock(c);
            let ready = (*s).terminal_ready == all();
            drop(g);
            if ready {
                break;
            }
            core::hint::spin_loop();
        }
        begin_step()
    }
}
#[inline(never)]
unsafe fn terminal_fault(address: u64) -> ! {
    unsafe {
        if (*owner()).program != 6
            || id() != config().actor
            || (*owner()).step != 1
            || config().profile < 4
        {
            stop(REJECTED, 437)
        }
        loop {
            let g = coordinator::lock(coordinator());
            if (*common()).terminal_queued == all() ^ (1 << id()) {
                let o = owner();
                (*o).vector = 13;
                (*o).error = 0;
                (*o).fault_address = address;
                (*o).fault_rip = (*guest()).rip;
                canonical(&raw mut (*o).guest_after, guest());
                (*common()).terminal_done |= 1 << id();
                coordinator()
                    .active
                    .fetch_and(!(1 << id()), Ordering::Relaxed);
                checked(g.trace(91, id()));
                drop(g);
                finish_all()
            }
            drop(g);
            core::hint::spin_loop();
        }
    }
}
#[inline(never)]
unsafe fn finish_all() -> ! {
    unsafe {
        let c = coordinator();
        loop {
            let g = coordinator::lock(c);
            if config().profile < 4 || (*common()).terminal_done == all() {
                if c.active.load(Ordering::Relaxed) != 0 {
                    stop(PANIC, 438)
                }
                c.new_references.fetch_and(!(1 << id()), Ordering::Relaxed);
                c.finished.fetch_or(1 << id(), Ordering::Release);
                checked(g.trace(94, id()));
                drop(g);
                stop(SUCCESS, 0)
            }
            drop(g);
            core::hint::spin_loop();
        }
    }
}
