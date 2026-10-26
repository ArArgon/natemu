//! Finite native coherence phases; guest walks and mutation hold coordinator authority.
use super::*;
use core::sync::atomic::Ordering::Relaxed;
use natemu_abi::{
    coherence as c, concurrency as n, pae,
    paging::{self, Access, Control, Fault},
    paging32, shadow, translation as wire,
};
unsafe extern "C" {
    fn service(packet: *mut wire::Packet);
    fn service_resume();
    fn enter_allocated(frame: *const NativeFrame, pc: u64, spsr: u64) -> !;
    fn resume_exception(frame: *const NativeFrame, pc: u64, spsr: u64) -> !;
    fn returned_allocated();
}
fn owner() -> *mut c::Owner {
    (PRIVATE + c::OWNER_OFFSET as u64) as *mut c::Owner
}
fn common() -> *mut c::Common {
    (PRIVATE + c::COMMON_OFFSET as u64) as *mut c::Common
}
fn coord() -> &'static n::Coordinator {
    unsafe { &*((PRIVATE + n::COORD_OFFSET as u64) as *const n::Coordinator) }
}
fn config() -> c::Config {
    unsafe {
        c::Config::new((*common()).n, (*common()).profile, (*common()).actor)
            .unwrap_or_else(|| stop(REJECTED, 700))
    }
}
fn id() -> u64 {
    unsafe { (*owner()).id }
}
fn all() -> u64 {
    (1 << config().n) - 1
}
fn index() -> usize {
    unsafe { (*owner()).visit as usize }
}
fn visit() -> *mut c::Visit {
    (PRIVATE + c::RECORD_OFFSET as u64 + index() as u64 * size_of::<c::Visit>() as u64)
        as *mut c::Visit
}
fn pause() {
    core::hint::spin_loop();
}
fn checked<T>(r: Result<T, u64>) -> T {
    r.unwrap_or_else(|e| stop(PANIC, e))
}
unsafe fn copy_frame(to: *mut NativeFrame, from: *const NativeFrame) {
    unsafe {
        copy_record(to.cast(), from.cast(), size_of::<NativeFrame>());
    }
}
fn table_pointer(gpa: u64) -> Option<*mut u8> {
    (0x1000..0x7000)
        .contains(&gpa)
        .then_some((PRIVATE + c::TABLE_OFFSET as u64 + gpa - 0x1000) as *mut u8)
}
unsafe fn table_read(gpa: u64) -> u64 {
    unsafe {
        let p = table_pointer(gpa).unwrap_or_else(|| stop(PANIC, 701));
        if config().entry_bytes() == 4 {
            ptr::read_volatile(p.cast::<u32>()) as u64
        } else {
            ptr::read_volatile(p.cast::<u64>())
        }
    }
}
unsafe fn table_write(gpa: u64, value: u64) {
    unsafe {
        let p = table_pointer(gpa).unwrap_or_else(|| stop(PANIC, 702));
        if config().entry_bytes() == 4 {
            ptr::write_volatile(p.cast::<u32>(), value as u32);
        } else {
            ptr::write_volatile(p.cast::<u64>(), value);
        }
        asm!("dsb ish");
    }
}
struct Tables(*mut c::Walk);
impl paging::Tables for Tables {
    fn read(&mut self, gpa: u64) -> Option<u64> {
        unsafe {
            if gpa & 7 != 0 || table_pointer(gpa).is_none() || (*self.0).read_count >= 4 {
                return None;
            }
            let value = table_read(gpa);
            let i = (*self.0).read_count as usize;
            (*self.0).reads[i] = [gpa, value];
            (*self.0).read_count += 1;
            Some(value)
        }
    }
    fn write(&mut self, gpa: u64, value: u64) -> bool {
        unsafe {
            if gpa & 7 != 0 || table_pointer(gpa).is_none() || (*self.0).write_count >= 4 {
                return false;
            }
            let old = table_read(gpa);
            let i = (*self.0).write_count as usize;
            (*self.0).writes[i] = [gpa, old, value];
            (*self.0).write_count += 1;
            table_write(gpa, value);
            true
        }
    }
}
impl paging32::Tables for Tables {
    fn read32(&mut self, gpa: u64) -> Option<u32> {
        unsafe {
            if gpa & 3 != 0 || table_pointer(gpa).is_none() || (*self.0).read_count >= 4 {
                return None;
            }
            let value = table_read(gpa);
            let i = (*self.0).read_count as usize;
            (*self.0).reads[i] = [gpa, value];
            (*self.0).read_count += 1;
            Some(value as u32)
        }
    }
    fn write32(&mut self, gpa: u64, value: u32) -> bool {
        unsafe {
            if gpa & 3 != 0 || table_pointer(gpa).is_none() || (*self.0).write_count >= 4 {
                return false;
            }
            let old = table_read(gpa);
            let i = (*self.0).write_count as usize;
            (*self.0).writes[i] = [gpa, old, value as u64];
            (*self.0).write_count += 1;
            table_write(gpa, value as u64);
            true
        }
    }
}
unsafe fn walk_record(
    out: *mut c::Walk,
    address: u64,
    access: Access,
) -> Result<paging::Translation, Fault> {
    unsafe {
        ptr::write(out, c::Walk::ZERO);
        (*out).address = address;
        (*out).intent = match access {
            Access::Read => 0,
            Access::Write => 1,
            Access::Fetch => 2,
        };
        let cfg = config();
        let control = Control {
            cr3: cfg.cr3(),
            cpl: 0,
            wp: 1,
            nxe: u64::from(cfg.four()),
            smep: 0,
        };
        let result = if cfg.four() {
            paging::walk(&mut Tables(out), control, address, access)
        } else if cfg.pae() {
            pae::walk(
                &mut Tables(out),
                &(*owner()).hidden_after,
                control,
                address,
                access,
            )
        } else {
            paging32::walk(&mut Tables(out), control, address, access)
        };
        match result {
            Ok(t) => {
                (*out).gpa = t.gpa;
                (*out).leaf_bytes = t.leaf_bytes;
                (*out).writable = t.writable as u64;
                (*out).dirty = t.dirty as u64;
                (*out).user = t.user as u64;
                (*out).executable = t.executable as u64;
                (*out).wb = t.wb as u64;
            }
            Err(Fault::Page { error, .. }) => {
                (*out).vector = 14;
                (*out).error = error;
            }
            _ => stop(REJECTED, 703),
        }
        result
    }
}
unsafe fn walk(
    g: &coordinator::Guard<'_>,
    address: u64,
    access: Access,
) -> Result<paging::Translation, Fault> {
    unsafe {
        let v = visit();
        let slot = (*v).walk_count as usize;
        if slot >= c::MAX_WALKS {
            stop(PANIC, 704)
        }
        let result = walk_record(&raw mut (*v).walks[slot], address, access);
        (*v).walk_count += 1;
        checked(g.trace(0x1000 + index() as u64 * 8 + slot as u64, id()));
        result
    }
}
unsafe fn physical(gpa: u64) -> *mut u8 {
    if !(c::DATA..c::RIP + 4096).contains(&gpa) {
        stop(REJECTED, 705)
    }
    (PRIVATE + c::RAM_OFFSET as u64 + gpa - c::DATA) as *mut u8
}
unsafe fn memory4(gpa: u64) -> [u8; 4] {
    unsafe {
        let mut out = [0; 4];
        copy_record(out.as_mut_ptr(), physical(gpa), 4);
        out
    }
}
unsafe fn leaves() -> [u64; 4] {
    unsafe {
        core::array::from_fn(|i| {
            let address =
                c::DATA + (i % 2) as u64 * 4096 + if i >= 2 { shadow::HIGH_BASE } else { 0 };
            ptr::read_volatile(
                (PRIVATE + shadow::leaf_offset(address).unwrap() as u64) as *const u64,
            )
        })
    }
}
unsafe fn invalidate() {
    unsafe {
        // Guest large leaves still use native4KiB shadows. Clear the whole admitted
        // two-MiB window under both roots, rather than only the faulting subpage.
        for offset in [shadow::LOWER_LEAF, shadow::UPPER_LEAF] {
            for i in 0..512 {
                ptr::write_volatile((PRIVATE + offset as u64 + i * 8) as *mut u64, 0);
            }
        }
        asm!("dsb ishst", "tlbi vmalle1", "dsb ish", "isb");
        (*owner()).mapping_gpa = [0; 2];
        (*owner()).mapping_writable = [0; 2];
        (*owner()).mapping_leaf_bytes = [0; 2];
    }
}
unsafe fn install(address: u64, t: paging::Translation) {
    unsafe {
        if !t.wb || !(c::DATA..c::DATA + 8192).contains(&t.gpa) {
            stop(REJECTED, 706)
        }
        let slot = shadow::leaf_offset(address & !4095).unwrap_or_else(|| stop(REJECTED, 707));
        let p = (PRIVATE + slot as u64) as *mut u64;
        ptr::write_volatile(p, 0);
        asm!("dsb ishst", "tlbi vmalle1", "dsb ish", "isb");
        let pa = (IPA + c::RAM_OFFSET as u64 + t.gpa - c::DATA) & !4095;
        ptr::write_volatile(
            p,
            pa | 3 | 1 << 10 | 3 << 8 | 3 << 53 | if t.writable && t.dirty { 0 } else { 1 << 7 },
        );
        asm!("dsb ishst", "tlbi vmalle1", "dsb ish", "isb");
    }
}
unsafe fn install_pair(g: &coordinator::Guard<'_>, address: u64, t: paging::Translation) {
    unsafe {
        install(address, t);
        if config().four() {
            let alternate = address ^ shadow::HIGH_BASE;
            let other = walk(g, alternate, Access::Read).unwrap_or_else(|_| stop(PANIC, 708));
            if other.gpa != t.gpa {
                stop(PANIC, 709)
            }
            install(alternate, other);
        }
        let page = index() % 2;
        (*owner()).mapping_gpa[page] = t.gpa;
        (*owner()).mapping_writable[page] = (t.writable && t.dirty) as u64;
        (*owner()).mapping_leaf_bytes[page] = t.leaf_bytes;
    }
}
unsafe fn clear_active(address: u64) {
    unsafe {
        let p = (PRIVATE + shadow::leaf_offset(address & !4095).unwrap() as u64) as *mut u64;
        ptr::write_volatile(p, 0);
        asm!("dsb ishst", "tlbi vmalle1", "dsb ish", "isb");
    }
}
unsafe fn publish(bytes: *const u8) {
    unsafe {
        let pte = (PRIVATE + 0x14000 + CODE_OFFSET as u64 / 4096 * 8) as *mut u64;
        let pa = IPA + id() * RAM_SIZE as u64 + CODE_OFFSET as u64;
        let attrs = 3 | 1 << 10 | 3 << 8;
        ptr::write_volatile(pte, 0);
        asm!("dsb ishst", "tlbi vmalle1", "dsb ish", "isb");
        ptr::write_volatile(pte, pa | attrs | 3 << 53);
        asm!("dsb ishst", "tlbi vmalle1", "dsb ish", "isb");
        copy_record(CODE_VA as *mut u8, bytes, c::CODE_BYTES);
        for offset in (0..c::CODE_BYTES).step_by(4) {
            asm!("dc cvau,{0}",in(reg)CODE_VA+offset as u64);
        }
        asm!("dsb ish");
        for offset in (0..c::CODE_BYTES).step_by(4) {
            asm!("ic ivau,{0}",in(reg)CODE_VA+offset as u64);
        }
        asm!("dsb ish", "isb");
        ptr::write_volatile(pte, 0);
        asm!("dsb ishst", "tlbi vmalle1", "dsb ish", "isb");
        ptr::write_volatile(pte, pa | attrs | 1 << 7);
        asm!("dsb ishst", "tlbi vmalle1", "dsb ish", "isb");
    }
}
unsafe fn source_matches(kind: usize, address: u64) -> bool {
    unsafe { core::slice::from_raw_parts(physical(address), 2) == c::source(kind).unwrap() }
}
unsafe fn code_matches() -> bool {
    unsafe { core::slice::from_raw_parts(CODE_VA as *const u8, c::CODE_BYTES) == (*owner()).code }
}
#[inline(never)]
unsafe fn compile() {
    unsafe {
        let o = owner();
        let s = shared();
        (*s).service_pc = service_resume as *const () as u64;
        for kind in 0..2 {
            let p = c::request((*s).lifecycle.vm, config(), id(), kind)
                .unwrap_or_else(|| stop(REJECTED, 710));
            {
                let g = coordinator::lock(coord());
                let t = walk_record(&raw mut (*o).compile_walks[kind], p[7], Access::Fetch)
                    .unwrap_or_else(|_| stop(REJECTED, 711));
                if t.gpa != p[7] || !t.wb || !source_matches(kind, t.gpa) {
                    stop(REJECTED, 712)
                }
                checked(g.trace(0x8000 + kind as u64, id()));
            }
            (*o).requests[kind] = p;
            (*o).expected = p;
            let mut reply = p;
            service(&mut reply);
            while reply[0] == wire::BUSY {
                reply = p;
                reply[0] = n::POLL_COMPILE;
                (*o).expected = reply;
                service(&mut reply);
            }
            (*o).expected = p;
            (*o).replies[kind] = reply;
            (*o).transfers += 1;
            if c::reply(&p) != Some(reply) {
                stop(REJECTED, 713)
            }
        }
        (*o).return_pc = returned_allocated as *const () as u64;
        (*s).capture_pc = (*o).return_pc;
        (*o).code = c::image((*o).return_pc).unwrap_or_else(|| stop(REJECTED, 714));
        publish((*o).code.as_ptr());
        let g = coordinator::lock(coord());
        (*common()).compiled |= 1 << id();
        checked(g.trace(c::COMPILED, id()));
    }
}
unsafe fn snapshot(kind: u64, visit: u64) {
    unsafe {
        let s = common();
        let i = (*s).snapshot_count as usize;
        if i >= c::MAX_SNAPSHOTS {
            stop(PANIC, 715)
        }
        let base =
            (PRIVATE + c::IMAGE_VA_OFFSET as u64 + i as u64 * c::SNAPSHOT_BYTES as u64) as *mut u8;
        asm!("dsb sy");
        copy_record(
            base,
            (PRIVATE + c::RAM_OFFSET as u64) as *const u8,
            c::DATA_BYTES,
        );
        copy_record(
            base.add(c::DATA_BYTES),
            (PRIVATE + c::TABLE_OFFSET as u64) as *const u8,
            c::TABLE_BYTES,
        );
        (*s).snapshots[i] = c::Snapshot {
            kind,
            visit,
            mutation: (*s).mutations,
            generation: coord().generation.load(Relaxed),
            active: coord().active.load(Relaxed),
            queued: coord().queued.load(Relaxed),
            servicing: coord().servicing.load(Relaxed),
            retained: coord().retained.load(Relaxed),
            acknowledgements: (*s).acknowledgements,
        };
        (*s).snapshot_count += 1;
    }
}
#[inline(never)]
pub unsafe fn initialize() -> ! {
    unsafe {
        let o = owner();
        let cfg = config();
        let s = shared();
        if (*o).id != (*s).object.vcpu_id as u64
            || (*s).object.configured_vcpus as u64 != cfg.n
            || id() >= cfg.n
            || (*o).visit != 0
            || (*o).transfers != 0
            || (*o).initial.sp != STACK_TOP
            || (*o).initial.x[28] != BANK_VA
            || (*s).lifecycle.vm == 0
        {
            stop(REJECTED, 716)
        }
        if cfg.pae() {
            let _g = coordinator::lock(coord());
            let mut entries = [0; 4];
            for i in 0..4 {
                let at = (cfg.cr3() & !31) + i as u64 * 8;
                (*o).hidden_reads[i] = at;
                entries[i] = table_read(at);
            }
            if entries
                .iter()
                .any(|e| e & 1 != 0 && e & !(paging::PA_MASK | 0xe19) != 0)
            {
                stop(REJECTED, 717)
            }
            (*o).hidden_before = pae::Hidden {
                entries,
                generation: 1,
            };
            (*o).hidden_after = (*o).hidden_before;
            (*o).hidden_loads = 1;
        }
        copy_frame(&raw mut (*o).current, &raw const (*o).initial);
        (*o).guest_after = (*o).guest_before;
        invalidate();
        compile();
        loop {
            let g = coordinator::lock(coord());
            if (*common()).compiled == all() && (*common()).initialized == 0 && id() == cfg.actor {
                snapshot(0, 0);
                if cfg.pae() {
                    table_write(0x1020, 0x6001);
                    (*common()).pdpt_changed = 1;
                    checked(g.trace(c::PDPT_CHANGED, id()));
                    snapshot(4, 0);
                }
                (*common()).initialized = 1;
                checked(g.trace(c::START, id()));
            }
            if (*common()).initialized != 0 {
                drop(g);
                begin();
            }
            drop(g);
            pause();
        }
    }
}
unsafe fn remember(t: paging::Translation) {
    unsafe {
        let v = visit();
        (*v).saved_gpa = t.gpa;
        (*v).saved_writable = (t.writable && t.dirty) as u64;
        (*v).saved_leaf_bytes = t.leaf_bytes;
        (*v).memory_before = memory4(t.gpa);
    }
}
#[inline(never)]
unsafe fn begin() -> ! {
    unsafe {
        let o = owner();
        let i = index();
        if i >= c::VISITS {
            retire()
        }
        let v = visit();
        let cfg = config();
        let kind = c::kind(i);
        let address = cfg.address(id(), i);
        loop {
            let g = coordinator::lock(coord());
            if (*common()).index != i as u64
                || (*common()).prepared & (1 << id()) != 0
                || coord().phase.load(Relaxed) != n::OPEN
            {
                stop(PANIC, 718)
            }
            if !crate::schedule::permit(
                &g,
                natemu_abi::schedule::ADMIT | i as u64,
                all(),
                all(),
                coord().phase.load(Relaxed),
                i as u64,
            ) {
                drop(g);
                pause();
                continue;
            }
            ptr::write(v, c::Visit::ZERO);
            (*v).index = i as u64;
            (*v).stage = (i / 2) as u64;
            (*v).page = (i % 2) as u64;
            (*v).kind = kind as u64;
            (*v).address = address;
            (*v).rip = c::RIP + id() * 256 + kind as u64 * 16;
            (*o).current.x[6] = address;
            if kind == c::STORE {
                (*o).current.x[4] = c::token(id(), i);
            }
            (*o).guest_after.gpr[0] = (*o).current.x[4];
            (*o).guest_after.gpr[3] = address;
            (*o).guest_after.rip = (*v).rip;
            (*v).cr2_before = (*o).guest_after.control[1];
            (*v).cr2_after = (*v).cr2_before;
            copy_frame(&raw mut (*v).initial, &raw const (*o).current);
            copy_frame(&raw mut (*shared()).initial, &raw const (*o).current);
            let fetch = walk(&g, (*v).rip, Access::Fetch).unwrap_or_else(|_| stop(REJECTED, 719));
            if fetch.gpa != (*v).rip
                || !fetch.wb
                || !source_matches(kind, fetch.gpa)
                || !code_matches()
            {
                stop(REJECTED, 720)
            }
            if i / 2 == 0 || c::retained(i) {
                let t = walk(&g, address, Access::Write).unwrap_or_else(|_| stop(REJECTED, 721));
                remember(t);
                install_pair(&g, address, t);
                if c::retained(i) {
                    clear_active(address);
                }
            } else if (*o).mapping_gpa[i % 2] != 0 {
                (*v).saved_gpa = (*o).mapping_gpa[i % 2];
                (*v).saved_writable = (*o).mapping_writable[i % 2];
                (*v).saved_leaf_bytes = (*o).mapping_leaf_bytes[i % 2];
                (*v).memory_before = memory4((*v).saved_gpa);
            }
            (*v).native_before = leaves();
            checked(g.admit(id()));
            (*v).admitted = 1;
            (*common()).prepared |= 1 << id();
            checked(g.trace(0x2000 + i as u64, id()));
            break;
        }
        loop {
            let g = coordinator::lock(coord());
            let ready = (*common()).prepared == all();
            drop(g);
            if ready {
                break;
            }
            pause();
        }
        enter_allocated(
            &raw const (*o).current,
            CODE_VA + kind as u64 * 16,
            0x3c4 | (*o).current.nzcv,
        )
    }
}
unsafe fn capture_fault() {
    unsafe {
        let v = visit();
        let (esr, far, pc, spsr): (u64, u64, u64, u64);
        asm!("mrs {0},ESR_EL1","mrs {1},FAR_EL1","mrs {2},ELR_EL1","mrs {3},SPSR_EL1",out(reg)esr,out(reg)far,out(reg)pc,out(reg)spsr);
        if (*v).faults != 0 || !c::direct_abort(config(), index(), esr, far, pc, spsr, id()) {
            stop(NATIVE_FAULT, 722)
        }
        copy_frame(&raw mut (*v).fault, &raw const (*owner()).current);
        (*v).faults = 1;
        (*v).esr = esr;
        (*v).far = far;
        (*v).pc = pc;
        (*v).spsr = spsr;
        (*owner()).faults += 1;
    }
}
#[inline(never)]
unsafe fn recover() -> ! {
    unsafe {
        capture_fault();
        let v = visit();
        let o = owner();
        if c::retained(index()) {
            loop {
                let g = coordinator::lock(coord());
                if (*v).saved_gpa == 0
                    || (*v).saved_writable != 1
                    || !matches!((*v).esr & 63, 4..=7)
                {
                    stop(PANIC, 723)
                }
                if !crate::schedule::permit(
                    &g,
                    natemu_abi::schedule::DETACH | index() as u64,
                    all(),
                    all(),
                    coord().phase.load(Relaxed),
                    index() as u64,
                ) {
                    drop(g);
                    pause();
                    continue;
                }
                checked(g.detach_fault(id()));
                checked(g.trace(c::RETAINED, id()));
                break;
            }
            loop {
                let g = coordinator::lock(coord());
                if coord().queued.load(Relaxed) == all() {
                    if (*common()).reuse_blocked & (1 << index()) == 0 {
                        if coord().retained.load(Relaxed) != all() || try_clear() {
                            stop(PANIC, 724)
                        }
                        (*common()).reuse_blocked |= 1 << index();
                        (*o).reuse_blocked += 1;
                        checked(g.trace(c::REUSE_BLOCKED, id()));
                    }
                    if checked(g.select()) == id() {
                        let t = paging::Translation {
                            gpa: (*v).saved_gpa,
                            leaf_bytes: (*v).saved_leaf_bytes,
                            writable: true,
                            dirty: true,
                            user: true,
                            executable: true,
                            wb: true,
                        };
                        install((*v).address, t);
                        (*v).retries = 1;
                        (*v).resume_pc = (*v).pc;
                        (*o).retries += 1;
                        checked(g.trace(c::RETRY, id()));
                        drop(g);
                        resume_exception(&raw const (*o).current, (*v).pc, (*v).spsr);
                    }
                }
                drop(g);
                pause();
            }
        }
        {
            let g = coordinator::lock(coord());
            if config().ad() && index() / 2 == 2 {
                stop(PANIC, 725)
            }
            match walk(
                &g,
                (*v).address,
                if c::kind(index()) == c::STORE {
                    Access::Write
                } else {
                    Access::Read
                },
            ) {
                Ok(t) => {
                    remember(t);
                    install_pair(&g, (*v).address, t);
                    (*v).retries = 1;
                    (*v).resume_pc = (*v).pc;
                    (*o).retries += 1;
                    checked(g.trace(c::REPAIR, id()));
                    drop(g);
                    resume_exception(&raw const (*o).current, (*v).pc, (*v).spsr);
                }
                Err(Fault::Page { address, error }) => {
                    if index() / 2 != 7 || error != 3 {
                        stop(REJECTED, 726)
                    }
                    (*v).vector = 14;
                    (*v).error = error;
                    (*v).cr2_after = address;
                    (*o).guest_after.control[1] = address;
                    (*o).guest_after.rip = (*v).rip;
                    copy_frame(&raw mut (*v).captured, &raw const (*o).current);
                    (*v).native_after = leaves();
                    (*v).memory_after = memory4((*v).saved_gpa);
                    coord().active.fetch_and(!(1 << id()), Relaxed);
                    (*common()).done |= 1 << id();
                    checked(g.trace(0x4000 + index() as u64, id()));
                    (*o).visit += 1;
                }
                _ => stop(REJECTED, 727),
            }
        }
        advance()
    }
}
unsafe fn mutate(number: u64) {
    unsafe {
        let cfg = config();
        if coord().active.load(Relaxed) != 0
            || coord().servicing.load(Relaxed) != n::NO_CPU
            || coord().head.load(Relaxed) != coord().length.load(Relaxed)
        {
            stop(PANIC, 728)
        }
        if cfg.ad() {
            if number <= 2 {
                for page in 0..2 {
                    let at = cfg.first() + (256 + page) * cfg.entry_bytes() as u64;
                    table_write(at, table_read(at) & !0x60);
                }
            } else {
                table_write(cfg.parent(), table_read(cfg.parent()) & !2);
            }
        } else if cfg.profile == 0 {
            table_write(
                cfg.parent(),
                match number {
                    1 => cfg.second() | 7,
                    2 => cfg.first() | 7,
                    _ => cfg.first() | 5,
                },
            );
        } else {
            table_write(
                cfg.parent(),
                match number {
                    1 => 0x87,
                    2 => cfg.second() | 7,
                    _ => 0x85,
                },
            );
        }
        (*common()).mutations = number;
        (*common()).acknowledgements = 0;
        (*common()).blocked = 0;
        coord().generation.fetch_add(1, Relaxed);
        coord().acknowledgements.store(0, Relaxed);
    }
}
unsafe fn reset_round() {
    let c = coord();
    c.phase.store(n::OPEN, Relaxed);
    c.active.store(0, Relaxed);
    c.queued.store(0, Relaxed);
    c.head.store(0, Relaxed);
    c.length.store(0, Relaxed);
    c.servicing.store(n::NO_CPU, Relaxed);
    c.completed.store(0, Relaxed);
    for slot in &c.queue {
        slot.store(n::NO_CPU, Relaxed);
    }
}
#[inline(never)]
unsafe fn advance() -> ! {
    unsafe {
        let cfg = config();
        let target = index();
        let previous = target - 1;
        let mutation = match previous {
            3 => 1,
            7 => 2,
            11 => 3,
            _ => 0,
        };
        loop {
            let g = coordinator::lock(coord());
            if (*common()).index == target as u64 {
                drop(g);
                begin();
            }
            if (*common()).index != previous as u64 {
                stop(PANIC, 729)
            }
            if (*common()).done == all() {
                if id() == cfg.actor && (*common()).prepared != 0 {
                    snapshot(1, previous as u64);
                    (*common()).prepared = 0;
                    if mutation != 0 {
                        coord().phase.store(n::EXCLUSIVE, Relaxed);
                        mutate(mutation);
                        checked(g.trace(c::MUTATION, id()));
                        snapshot(2, previous as u64);
                    }
                }
                if (*common()).prepared == 0 {
                    if mutation != 0 {
                        let delayed = (cfg.actor + cfg.n - 1) % cfg.n;
                        if id() == delayed
                            && (*common()).acknowledgements == all() ^ (1 << delayed)
                            && (*common()).blocked == 0
                        {
                            if g.admit(id()).is_ok() {
                                stop(PANIC, 730)
                            }
                            (*common()).blocked = 1;
                            (*owner()).entry_blocked += 1;
                            checked(g.trace(c::ENTRY_BLOCKED, id()));
                        }
                        if (*common()).acknowledgements & (1 << id()) == 0
                            && (id() != delayed || (*common()).blocked == 1)
                            && crate::schedule::permit(
                                &g,
                                natemu_abi::schedule::ACK | mutation,
                                all(),
                                crate::schedule::except_last(
                                    all(),
                                    delayed,
                                    (*common()).acknowledgements,
                                ),
                                coord().phase.load(Relaxed),
                                mutation,
                            )
                        {
                            if !(cfg.ad() && mutation == 1) {
                                invalidate();
                            }
                            (*owner()).ack_leaves[mutation as usize - 1] = leaves();
                            (*owner()).acknowledgements |= 1 << (mutation - 1);
                            (*common()).acknowledgements |= 1 << id();
                            coord()
                                .acknowledgements
                                .store((*common()).acknowledgements, Relaxed);
                            checked(g.trace(c::ACK, id()));
                        }
                    }
                    if id() == cfg.actor && (mutation == 0 || (*common()).acknowledgements == all())
                    {
                        if mutation != 0 {
                            snapshot(3, previous as u64);
                        }
                        reset_round();
                        (*common()).done = 0;
                        (*common()).index = target as u64;
                        checked(g.trace(c::OPENED, id()));
                        drop(g);
                        begin();
                    }
                }
            }
            drop(g);
            pause();
        }
    }
}
unsafe fn try_clear() -> bool {
    unsafe {
        if (*common()).index != c::VISITS as u64
            || (*common()).released != all()
            || coord().retained.load(Relaxed) != 0
            || coord().active.load(Relaxed) != 0
            || (*common()).acknowledgements != all()
        {
            return false;
        }
        copy_record(
            (*owner()).code_before_clear.as_mut_ptr(),
            CODE_VA as *const u8,
            c::CODE_BYTES,
        );
        if (*owner()).code_before_clear != (*owner()).code {
            stop(PANIC, 731)
        }
        publish([0u8; c::CODE_BYTES].as_ptr());
        copy_record(
            (*owner()).code_after_clear.as_mut_ptr(),
            CODE_VA as *const u8,
            c::CODE_BYTES,
        );
        if code_matches() {
            stop(PANIC, 735)
        }
        (*owner()).entry_rejected = 1;
        true
    }
}
#[inline(never)]
unsafe fn retire() -> ! {
    unsafe {
        loop {
            let g = coordinator::lock(coord());
            let cfg = config();
            let delayed = cfg.actor;
            if id() == delayed
                && (*owner()).released == 0
                && (*common()).released == all() ^ (1 << delayed)
            {
                if (*common()).index != c::VISITS as u64
                    || coord().retained.load(Relaxed) != 1 << delayed
                    || (*common()).acknowledgements != all()
                    || try_clear()
                {
                    stop(PANIC, 736)
                }
                copy_record(
                    (*owner()).code_while_held.as_mut_ptr(),
                    CODE_VA as *const u8,
                    c::CODE_BYTES,
                );
                if (*owner()).code_while_held != (*owner()).code {
                    stop(PANIC, 737)
                }
                (*owner()).terminal_reuse_blocked = 1;
                checked(g.trace(c::TERMINAL_REUSE_BLOCKED, id()));
            }
            if (*owner()).released == 0
                && (id() != delayed || (*common()).released == all() ^ (1 << delayed))
                && crate::schedule::permit(
                    &g,
                    natemu_abi::schedule::RELEASE,
                    all(),
                    crate::schedule::except_last(all(), delayed, (*common()).released),
                    coord().phase.load(Relaxed),
                    c::VISITS as u64,
                )
            {
                (*owner()).released = 1;
                (*common()).released |= 1 << id();
                coord().retained.fetch_and(!(1 << id()), Relaxed);
                checked(g.trace(c::RELEASE, id()));
            }
            if (*common()).released == all() && (*common()).reclaimed & (1 << id()) == 0 {
                if !try_clear() {
                    stop(PANIC, 732)
                }
                (*common()).reclaimed |= 1 << id();
                checked(g.trace(c::CODE_CLEARED, id()));
            }
            if (*common()).reclaimed == all() {
                coord().reclaimed.store(1, Relaxed);
                coord().finished.fetch_or(1 << id(), Relaxed);
                checked(g.trace(c::TERMINAL, id()));
                drop(g);
                stop(SUCCESS, 0);
            }
            drop(g);
            pause();
        }
    }
}
#[inline(never)]
pub unsafe fn finish(fault: u64) -> ! {
    unsafe {
        if index() >= c::VISITS {
            stop(PANIC, 733)
        }
        let o = owner();
        let v = visit();
        copy_frame(&raw mut (*o).current, &raw const (*shared()).captured);
        if fault != 0 {
            recover()
        }
        {
            let g = coordinator::lock(coord());
            if (*v).completed != 0 || (*v).vector != 0 || (*v).admitted != 1 || index() / 2 == 7 {
                stop(PANIC, 734)
            }
            copy_frame(&raw mut (*v).captured, &raw const (*o).current);
            (*v).completed = 1;
            (*o).completed += 1;
            (*v).native_after = leaves();
            (*v).memory_after = memory4((*v).saved_gpa);
            (*o).guest_after.gpr[0] = (*o).current.x[4];
            (*o).guest_after.gpr[3] = (*o).current.x[6];
            (*o).guest_after.rip = (*v).rip + 2;
            if c::retained(index()) {
                checked(g.complete(id()));
            } else {
                coord().active.fetch_and(!(1 << id()), Relaxed);
            }
            (*common()).done |= 1 << id();
            checked(g.trace(0x3000 + index() as u64, id()));
            (*o).visit += 1;
        }
        advance()
    }
}
