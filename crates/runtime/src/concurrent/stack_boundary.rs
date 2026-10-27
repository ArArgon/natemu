//! Boundary accesses keep peer grants until saved-route completion.
use super::*;
use natemu_abi::{
    paging::{self, Fault},
    recovery as r, shadow, stack as p, stack_boundary as b,
};
fn common() -> *mut b::Shared {
    (PRIVATE + b::SHARED_OFFSET as u64) as *mut b::Shared
}
fn owner() -> *mut b::Owner {
    (PRIVATE + b::OWNER_OFFSET as u64) as *mut b::Owner
}
fn wave() -> &'static Coordinator {
    unsafe { &*(&raw const (*common()).wave) }
}
fn id() -> u64 {
    unsafe { (*shared()).object.vcpu_id as u64 }
}
fn all() -> u64 {
    (1 << wave().participants) - 1
}
fn peers() -> u64 {
    unsafe { all() ^ (1 << (*common()).actor) }
}
fn actor() -> bool {
    unsafe { id() == (*common()).actor }
}
fn guest() -> *mut r::GuestState {
    r::GUEST_VA as *mut r::GuestState
}
fn leaf(index: u64) -> *mut u64 {
    (PRIVATE + r::TABLE_OFFSET as u64 + 0x3000 + index * 8) as *mut u64
}
fn physical(gpa: u64) -> *mut u8 {
    if !(DATA_VA..DATA_VA + 8192).contains(&gpa) {
        stop(PANIC, 350);
    }
    (PRIVATE + DATA_OFFSET as u64 + gpa - DATA_VA) as *mut u8
}
pub fn enabled() -> bool {
    unsafe { ptr::read_volatile((PRIVATE + b::ENABLE_OFFSET as u64) as *const u64) == 1 }
}
unsafe fn materialize(offset: u64) {
    unsafe {
        let f = &(*owner()).current;
        let g = &mut *guest();
        g.gpr[0] = f.x[4];
        g.gpr[2] = f.x[5];
        g.gpr[3] = f.x[6];
        g.gpr[4] = f.x[8];
        g.rip = natemu_abi::alias::source_rip(coordinator().alias_profile) + offset;
    }
}
unsafe fn eligible() -> bool {
    unsafe {
        (*common()).reclaimed == 0
            && wave().retained.load(Ordering::Relaxed) & (1 << id()) != 0
            && source::eligible(2, CODE_NEXT)
            && core::slice::from_raw_parts(CODE_NEXT as *const u8, p::CODE_BYTES)
                == (*common()).code_before
    }
}
fn native_pair(address: u64) -> [u64; 2] {
    [address & !4095, (address & !4095) + 4096].map(tables::descriptor)
}
#[inline(never)]
unsafe fn snapshot() {
    unsafe {
        let o = owner();
        let s = shared();
        copy_frame(&raw mut (*o).before_frame, &raw const (*s).captured);
        copy_record(
            (&raw mut (*o).before_guest).cast(),
            guest().cast(),
            size_of::<r::GuestState>(),
        );
        copy_record(
            (&raw mut (*o).lower_before).cast(),
            (PRIVATE + shadow::LOWER_LEAF as u64) as *const u8,
            4096,
        );
        copy_record(
            (&raw mut (*o).upper_before).cast(),
            (PRIVATE + shadow::UPPER_LEAF as u64) as *const u8,
            4096,
        );
    }
}
#[inline(never)]
unsafe fn setup() {
    unsafe {
        let s = common();
        let c = coordinator();
        if c.stack_profile != 1
            || c.generation.load(Ordering::Relaxed) != 2
            || c.active.load(Ordering::Relaxed) != 0
            || c.new_references.load(Ordering::Relaxed) != 0
            || c.retained.load(Ordering::Relaxed) != 0
            || c.participants != (*s).n
            || !(1..=5).contains(&(*s).profile)
            || (*s).actor >= (*s).n
        {
            stop(PANIC, 351);
        }
        copy_record(
            (&raw mut (*s).data_before).cast(),
            (PRIVATE + DATA_OFFSET as u64) as *const u8,
            8192,
        );
        copy_record(
            (&raw mut (*s).tables_before).cast(),
            (PRIVATE + r::TABLE_OFFSET as u64) as *const u8,
            16384,
        );
        copy_record(
            (&raw mut (*s).code_before).cast(),
            CODE_NEXT as *const u8,
            p::CODE_BYTES,
        );
        (*s).trace_start = c.trace_length.load(Ordering::Relaxed);
        (*s).finished_before = c.finished.load(Ordering::Relaxed);
        (*s).old_page = ptr::read_volatile(leaf(256)) & paging::PA_MASK;
        (*s).other_page = if (*s).old_page == DATA_VA {
            DATA_VA + 4096
        } else {
            DATA_VA
        };
        if (*s).old_page != DATA_VA && (*s).old_page != DATA_VA + 4096 {
            stop(PANIC, 352);
        }
        if (*s).profile == b::SS {
            ptr::write_volatile(leaf(0), (*s).other_page | 7);
        }
        if (*s).profile >= b::SPLIT {
            ptr::write_volatile(leaf(260), (*s).other_page | 7);
            ptr::write_volatile(
                leaf(261),
                match (*s).profile {
                    b::SPLIT_NP => 0,
                    b::SPLIT_RO => (*s).old_page | 5,
                    _ => (*s).old_page | 7,
                },
            );
        }
        (*s).configured_leaves = [
            ptr::read_volatile(leaf(0)),
            ptr::read_volatile(leaf(260)),
            ptr::read_volatile(leaf(261)),
        ];
        asm!("dsb ish");
        (*s).setup = 1;
    }
}
#[inline(never)]
pub unsafe fn start() -> ! {
    unsafe {
        snapshot();
        {
            let _g = coordinator::lock(wave());
            (*common()).ready |= 1 << id();
        }
        loop {
            let g = coordinator::lock(wave());
            if (*common()).ready == all() {
                if (*common()).setup == 0 {
                    setup();
                    checked(g.trace(40, id()));
                }
                break;
            }
            drop(g);
            core::hint::spin_loop();
        }
        let o = owner();
        copy_frame(&raw mut (*o).current, &raw const (*o).before_frame);
        if actor() {
            let (rsp, rdx) =
                b::actor_roots((*common()).profile, (*o).current.x[8], (*o).current.x[5]).unwrap();
            (*o).current.x[8] = rsp;
            (*o).current.x[5] = rdx;
        }
        (*o).current.x[16] = CODE_NEXT;
        (*o).current.x[30] = returned as *const () as u64;
        copy_frame(&raw mut (*o).initial, &raw const (*o).current);
        materialize(0);
        copy_record(
            (&raw mut (*o).initial_guest).cast(),
            guest().cast(),
            size_of::<r::GuestState>(),
        );
        for page in [
            0,
            DATA_VA,
            DATA_VA + 4096,
            DATA_VA + 0x4000,
            DATA_VA + 0x5000,
        ] {
            recovery::clear_mapping(page);
            recovery::clear_mapping(shadow::HIGH_BASE + page);
        }
        {
            let g = coordinator::lock(wave());
            let address = (*o).current.x[8] - 8;
            if actor() && (*common()).profile >= b::SPLIT {
                (*o).saved_page = (*common()).other_page;
                (*o).saved_writable = 1;
            } else {
                let t = recovery::walk(address, Access::Write).unwrap_or_else(|_| stop(PANIC, 353));
                let expected = if actor() && (*common()).profile == b::SS {
                    (*common()).other_page
                } else {
                    (*common()).old_page
                };
                if t.gpa & !4095 != expected || !t.wb {
                    stop(PANIC, 354);
                }
                (*o).saved_page = expected;
                (*o).saved_writable = u64::from(t.writable);
                recovery::install(address, t);
            }
            (*o).native_pair_before = native_pair(address);
            checked(g.admit(id()));
            wave().ready.fetch_or(1 << id(), Ordering::Release);
            (*round()).stage = b::STAGE;
        }
        while wave().ready.load(Ordering::Acquire) != all() {
            core::hint::spin_loop();
        }
        enter_step()
    }
}
unsafe fn wait_peers() {
    while wave().queued.load(Ordering::Acquire) != peers() {
        core::hint::spin_loop();
    }
}
#[inline(never)]
unsafe fn terminal(vector: u64, address: u64, error: u64) -> ! {
    unsafe {
        wait_peers();
        let o = owner();
        let g = coordinator::lock(wave());
        materialize(p::GUEST_OFFSETS[(*o).step as usize]);
        (*o).vector = vector;
        (*o).address = address;
        (*o).error = error;
        (*o).rip = (*guest()).rip;
        if vector == 14 {
            (*guest()).control[1] = address;
        }
        copy_record(
            (&raw mut (*o).guest_after).cast(),
            guest().cast(),
            size_of::<r::GuestState>(),
        );
        checked(g.trace(43, id()));
        drop(g);
        done()
    }
}
#[inline(never)]
unsafe fn enter_step() -> ! {
    unsafe {
        let o = owner();
        let step = (*o).step as usize;
        if step >= 5 || (*o).entries & (1 << step) != 0 {
            stop(PANIC, 355);
        }
        let address = natemu_abi::stack_concurrent::due(&(*o).current, step);
        let width = p::WIDTHS[step];
        if !paging::canonical_span(address, width) {
            terminal(if step < 3 { 12 } else { 13 }, address, 0);
        }
        if step >= 3 && address & (width - 1) != 0 {
            stop(REJECTED, 356);
        }
        {
            let _g = coordinator::lock(wave());
            if !eligible() {
                stop(PANIC, 357);
            }
        }
        (*o).current.x[16] = CODE_NEXT + step as u64 * p::SLOT_BYTES as u64;
        (*o).current.x[30] = returned as *const () as u64;
        (*o).current.sp = STACK_TOP;
        copy_frame(&raw mut (*shared()).initial, &raw const (*o).current);
        (*shared()).capture_pc = returned as *const () as u64;
        (*o).entries |= 1 << step;
        enter(&raw const (*shared()).initial)
    }
}
unsafe fn service_bytes(out: *mut u8) {
    unsafe {
        copy_record(out, physical((*common()).other_page + 4092), 4);
        copy_record(out.add(4), physical((*common()).old_page), 4);
    }
}
#[inline(never)]
unsafe fn split_service() -> ! {
    unsafe {
        wait_peers();
        let o = owner();
        let g = coordinator::lock(wave());
        let value = (*o).current.x[20].to_le_bytes();
        if value != [0xbb, 0xbc, 0xbd, 0xbe, 0xff, 0xff, 0xff, 0xff] {
            stop(NATIVE_FAULT, 361);
        }
        (*o).service_attempts = 1;
        (*o).service_pc = (*o).pc;
        (*o).dirty_before = [ptr::read_volatile(leaf(260)), ptr::read_volatile(leaf(261))];
        service_bytes((&raw mut (*o).data_before_service).cast());
        let mut memory = b::DeferredDirty::new(GuestTables::new(recovery::RAM));
        let control = ptr::read_volatile(&raw const (*(r::STATE_VA as *const r::Recovery)).control);
        for i in 0..2 {
            let address = if i == 0 { b::BOUNDARY - 4 } else { b::BOUNDARY };
            let leaf_gpa = 0x4000 + (260 + i as u64) * 8;
            (*o).fragments[i] = b::Fragment {
                linear: address,
                bytes: 4,
                gpa: 0,
                leaf: leaf_gpa,
                before: ptr::read_volatile(leaf(260 + i as u64)),
                after: 0,
                ready: 0,
            };
            let outcome = paging::walk(&mut memory, control, address, Access::Write);
            (*o).fragments[i].after = ptr::read_volatile(leaf(260 + i as u64));
            match outcome {
                Ok(t) => {
                    let expected = if i == 0 {
                        (*common()).other_page + 4092
                    } else {
                        (*common()).old_page
                    };
                    if t.gpa != expected || !t.wb || !t.writable {
                        stop(PANIC, 358);
                    }
                    (*o).fragments[i].gpa = t.gpa;
                    (*o).fragments[i].ready = 1;
                    (*o).ready_mask |= 1 << i;
                }
                Err(Fault::Page { address, error }) => {
                    (*o).dirty_after =
                        [ptr::read_volatile(leaf(260)), ptr::read_volatile(leaf(261))];
                    service_bytes((&raw mut (*o).data_after_service).cast());
                    checked(g.trace(45, id()));
                    drop(g);
                    terminal(14, address, error)
                }
                _ => stop(PANIC, 359),
            }
        }
        if (*o).ready_mask != 3 || !memory.commit() {
            stop(PANIC, 360);
        }
        copy_record(physical((*o).fragments[0].gpa), value.as_ptr(), 4);
        copy_record(physical((*o).fragments[1].gpa), value.as_ptr().add(4), 4);
        (*o).current.x[8] = b::BOUNDARY - 4;
        (*o).dirty_after = [ptr::read_volatile(leaf(260)), ptr::read_volatile(leaf(261))];
        service_bytes((&raw mut (*o).data_after_service).cast());
        for i in 0..2 {
            recovery::install_ram((*o).fragments[i].linear, (*o).fragments[i].gpa, true);
        }
        (*o).native_pair_after = native_pair(b::BOUNDARY - 4);
        (*o).service_completed = 1;
        (*o).service_resume_pc = (*o).pc + 4;
        checked(g.trace(44, id()));
        drop(g);
        resume_exception(&raw const (*o).current, (*o).service_resume_pc, (*o).spsr)
    }
}
#[inline(never)]
pub unsafe fn finish(fault: u64) -> ! {
    unsafe {
        let o = owner();
        let step = (*o).step as usize;
        let s = shared();
        if step >= 5 || (*o).entries & (1 << step) == 0 || (*o).completed & (1 << step) != 0 {
            stop(PANIC, 362);
        }
        copy_frame(&raw mut (*o).current, &raw const (*s).captured);
        if fault != 0 {
            let (esr, far, pc, spsr): (u64, u64, u64, u64);
            asm!("mrs {0},ESR_EL1","mrs {1},FAR_EL1","mrs {2},ELR_EL1","mrs {3},SPSR_EL1",out(reg)esr,out(reg)far,out(reg)pc,out(reg)spsr);
            let split = actor() && (*common()).profile >= b::SPLIT;
            let address = natemu_abi::stack_concurrent::due(&(*s).initial, step);
            if step != if split { 0 } else { 1 }
                || !b::direct_store_translation_abort(esr)
                || pc != CODE_NEXT + step as u64 * p::SLOT_BYTES as u64 + p::SITES[step]
                || !(address..address + p::WIDTHS[step]).contains(&far)
                || spsr & 15 != 4
                || (*o).current.x[8] != (*s).initial.x[8]
                || (*o).faults != 0
            {
                stop(NATIVE_FAULT, 363);
            }
            (*o).esr = esr;
            (*o).far = far;
            (*o).pc = pc;
            (*o).spsr = spsr;
            (*o).faults = 1;
            copy_frame(&raw mut (*o).fault_frame, &raw const (*o).current);
            if split {
                split_service();
            }
            let delayed = ((*common()).actor + (*common()).n - 1) % (*common()).n;
            if (*common()).n >= 3 && id() == delayed {
                while wave().queued.load(Ordering::Acquire) != peers() ^ (1 << id()) {
                    core::hint::spin_loop();
                }
            }
            {
                let g = coordinator::lock(wave());
                checked(g.detach_fault(id()));
                checked(g.trace(42, id()));
            }
            loop {
                let g = coordinator::lock(wave());
                if (*common()).actor_done != 0 && checked(g.select()) == id() {
                    let gpa = (*o).saved_page + (address & 4095);
                    recovery::install_ram(address, gpa, (*o).saved_writable != 0);
                    checked(g.trace(46, id()));
                    drop(g);
                    resume_exception(&raw const (*o).current, pc, spsr)
                }
                drop(g);
                core::hint::spin_loop();
            }
        }
        let expected_rsp = if step < 3 {
            natemu_abi::stack_concurrent::due(&(*s).initial, step)
        } else {
            (*s).initial.x[8]
        };
        if (*o).current.x[8] != expected_rsp || (*o).current.x[5] != (*s).initial.x[5] {
            stop(NATIVE_FAULT, 364);
        }
        copy_frame(&raw mut (*o).prefixes[step], &raw const (*o).current);
        (*o).completed |= 1 << step;
        materialize(p::GUEST_OFFSETS[step + 1]);
        if step == 4 {
            done();
        }
        (*o).step += 1;
        if !actor() && step == 0 {
            recovery::clear_mapping((*o).current.x[8]);
        }
        enter_step()
    }
}
#[inline(never)]
unsafe fn done() -> ! {
    unsafe {
        if actor() {
            wait_peers();
        }
        let o = owner();
        copy_record(
            (&raw mut (*o).guest_after).cast(),
            guest().cast(),
            size_of::<r::GuestState>(),
        );
        {
            let g = coordinator::lock(wave());
            if actor() {
                (*common()).actor_done = 1;
                (*o).retained_at_outcome = wave().retained.load(Ordering::Relaxed);
                copy_record(
                    (&raw mut (*common()).code_at_outcome).cast(),
                    CODE_NEXT as *const u8,
                    p::CODE_BYTES,
                );
                wave().active.fetch_and(!(1 << id()), Ordering::Relaxed);
                wave().completed.fetch_or(1 << id(), Ordering::Relaxed);
                checked(g.trace(47, id()));
            } else {
                checked(g.complete(id()));
            }
            (*common()).done |= 1 << id();
        }
        retire()
    }
}
// The same gate rejects early reuse and performs the eventual physical clear.
#[inline(never)]
unsafe fn try_reclaim() -> bool {
    unsafe {
        let s = common();
        if (*s).done != all()
            || (*s).acknowledgements != all()
            || (*s).reclaimed != 0
            || wave().active.load(Ordering::Relaxed) != 0
            || wave().retained.load(Ordering::Relaxed) != 0
        {
            return false;
        }
        source::publish_slot(CODE_NEXT, &[0; p::CODE_BYTES]);
        (*s).reclaimed = 1;
        wave().reclaimed.store(1, Ordering::Release);
        true
    }
}
#[inline(never)]
unsafe fn retire() -> ! {
    unsafe {
        let o = owner();
        let s = common();
        loop {
            let g = coordinator::lock(wave());
            if (*s).done == all() {
                if (*s).remapped == 0 {
                    wave().phase.store(EXCLUSIVE, Ordering::Relaxed);
                    if wave().active.load(Ordering::Relaxed) != 0
                        || wave().servicing.load(Ordering::Relaxed) != NO_CPU
                    {
                        stop(PANIC, 365);
                    }
                    ptr::write_volatile(leaf(256), (*s).other_page | 7);
                    if (*s).profile == b::SS {
                        ptr::write_volatile(leaf(0), (*s).old_page | 7);
                    }
                    if (*s).profile >= b::SPLIT {
                        ptr::write_volatile(leaf(260), (*s).old_page | 7);
                        ptr::write_volatile(leaf(261), (*s).other_page | 7);
                    }
                    asm!("dsb ish");
                    (*s).remapped = 1;
                    wave().generation.store(2, Ordering::Relaxed);
                    wave().old_eligible.store(0, Ordering::Relaxed);
                    checked(g.trace(48, id()));
                }
                let delayed = ((*s).actor + (*s).n - 1) % (*s).n;
                if (*s).acknowledgements & (1 << id()) == 0
                    && (id() != delayed || (*s).acknowledgements == all() ^ (1 << id()))
                {
                    for page in [0, DATA_VA, DATA_VA + 0x4000, DATA_VA + 0x5000] {
                        recovery::clear_mapping(page);
                        recovery::clear_mapping(shadow::HIGH_BASE + page);
                    }
                    for (i, address) in [DATA_VA, shadow::HIGH_BASE + DATA_VA]
                        .into_iter()
                        .enumerate()
                    {
                        let t = recovery::walk(address, Access::Read)
                            .unwrap_or_else(|_| stop(PANIC, 366));
                        if t.gpa != (*s).other_page {
                            stop(PANIC, 367);
                        }
                        recovery::install_ram(address, t.gpa, false);
                        (*o).ack_pair[i] = tables::descriptor(address);
                    }
                    (*s).acknowledgements |= 1 << id();
                    checked(g.trace(49, id()));
                }
                if (*s).acknowledgements == all() {
                    if wave().retained.load(Ordering::Relaxed) == 0
                        || core::slice::from_raw_parts(CODE_NEXT as *const u8, p::CODE_BYTES)
                            != (*s).code_before
                    {
                        stop(PANIC, 368);
                    }
                    if (*s).retained_before_release == 0 {
                        (*s).retained_before_release = wave().retained.load(Ordering::Relaxed);
                    }
                    if try_reclaim() {
                        stop(PANIC, 368);
                    }
                    (*s).reuse_blocked |= 1 << id();
                    if id() != delayed || wave().retained.load(Ordering::Relaxed) == 1 << id() {
                        wave().retained.fetch_and(!(1 << id()), Ordering::Relaxed);
                        (*o).released = 1;
                        (*s).released |= 1 << id();
                        checked(g.trace(50, id()));
                        if (*s).released == all() {
                            if !try_reclaim() {
                                stop(PANIC, 370);
                            }
                            checked(g.trace(51, id()));
                        }
                        break;
                    }
                }
            }
            drop(g);
            core::hint::spin_loop();
        }
        while wave().reclaimed.load(Ordering::Acquire) == 0 {
            core::hint::spin_loop();
        }
        {
            let g = coordinator::lock(wave());
            if eligible() {
                stop(PANIC, 369);
            }
            (*s).entry_rejected |= 1 << id();
            wave().finished.fetch_or(1 << id(), Ordering::Release);
            checked(g.trace(52, id()));
        }
        stop(SUCCESS, 0)
    }
}
