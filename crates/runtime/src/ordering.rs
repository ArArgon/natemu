//! Native participant blocks run between, never inside, shared trial gates.
use super::*;
use core::sync::atomic::Ordering::{AcqRel, Acquire, Release};
use natemu_abi::{
    ordering as o,
    paging::{self, Access, Control, Tables},
    shadow, translation as wire,
};
unsafe extern "C" {
    fn service(packet: *mut wire::Packet);
    fn service_resume();
    fn resume_exception(frame: *const NativeFrame, pc: u64, spsr: u64) -> !;
}
fn owner() -> *mut o::Owner {
    (PRIVATE + o::OWNER_OFFSET as u64) as *mut o::Owner
}
fn common() -> &'static o::Common {
    unsafe { &*((PRIVATE + o::COMMON_OFFSET as u64) as *const o::Common) }
}
fn config() -> o::Config {
    let c = common();
    o::Config::new(c.n, c.profile, c.rotation, c.seed).unwrap_or_else(|| stop(REJECTED, 500))
}
fn id() -> u64 {
    unsafe { (*owner()).id }
}
fn all() -> u64 {
    (1 << common().n) - 1
}
fn trial() -> *mut o::Trial {
    unsafe {
        (PRIVATE + o::RECORD_OFFSET as u64 + (*owner()).trial * size_of::<o::Trial>() as u64)
            as *mut o::Trial
    }
}
unsafe fn copy_frame(to: *mut NativeFrame, from: *const NativeFrame) {
    unsafe {
        copy_record(to.cast(), from.cast(), size_of::<NativeFrame>());
    }
}
fn pause() {
    core::hint::spin_loop();
}
struct GuestTables;
impl Tables for GuestTables {
    fn read(&mut self, gpa: u64) -> Option<u64> {
        if !(0x1000..0x5000).contains(&gpa) || gpa & 7 != 0 {
            return None;
        }
        unsafe {
            (*owner()).table_reads += 1;
            Some(ptr::read_volatile(
                (PRIVATE + o::TABLE_OFFSET as u64 + gpa - 0x1000) as *const u64,
            ))
        }
    }
    fn write(&mut self, _gpa: u64, _value: u64) -> bool {
        // Host supplies a genuinely warm A/D-satisfied guest-table fixture.
        unsafe {
            (*owner()).table_writes += 1;
        }
        false
    }
}
unsafe fn walk(address: u64, access: Access) -> u64 {
    unsafe {
        (*owner()).walks += 1;
        match paging::walk(
            &mut GuestTables,
            Control {
                cr3: 0x1000,
                cpl: 0,
                wp: 1,
                nxe: 1,
                smep: 0,
            },
            address,
            access,
        ) {
            Ok(t)
                if t.gpa == address
                    && t.wb
                    && (access != Access::Write || t.writable && t.dirty) =>
            {
                t.gpa
            }
            _ => stop(REJECTED, 501),
        }
    }
}
unsafe fn source_matches() -> bool {
    unsafe {
        let bytes = o::source((*owner()).variant as usize).unwrap_or_else(|| stop(REJECTED, 502));
        core::slice::from_raw_parts(
            (PRIVATE + o::RAM_OFFSET as u64 + o::RIP - o::DATA + id() * 256) as *const u8,
            bytes.len(),
        ) == bytes
    }
}
unsafe fn mapping(address: u64, present: bool, writable: bool) {
    unsafe {
        let gpa = if address == o::RESULTS {
            atomic_progress::result_gpa()
        } else {
            address
        };
        mapping_gpa(address, gpa, present, writable);
    }
}
pub(super) unsafe fn mapping_gpa(address: u64, gpa: u64, present: bool, writable: bool) {
    unsafe {
        if !matches!(address, o::DATA | o::RESULTS)
            || (gpa != address
                && !(atomic_progress::enabled()
                    && address == o::RESULTS
                    && gpa == natemu_abi::atomic_progress::NEW_RESULTS))
        {
            stop(PANIC, 503)
        }
        let slot = shadow::leaf_offset(address).unwrap_or_else(|| stop(PANIC, 503));
        let pte = (PRIVATE + slot as u64) as *mut u64;
        ptr::write_volatile(pte, 0);
        asm!("dsb ishst", "tlbi vmalle1", "dsb ish", "isb");
        if present {
            let pa = IPA + o::RAM_OFFSET as u64 + gpa - o::DATA;
            let entry = pa | 3 | 1 << 10 | 3 << 8 | 3 << 53 | if writable { 0 } else { 1 << 7 };
            ptr::write_volatile(pte, entry);
        }
        asm!("dsb ishst", "tlbi vmalle1", "dsb ish", "isb");
    }
}
unsafe fn publish() {
    unsafe {
        let code = &(*owner()).code;
        let pte = (PRIVATE + 0x14000 + CODE_OFFSET as u64 / 4096 * 8) as *mut u64;
        let pa = IPA + id() * RAM_SIZE as u64 + CODE_OFFSET as u64;
        let attrs = 3 | 1 << 10 | 3 << 8;
        let replace = |value| {
            ptr::write_volatile(pte, 0);
            asm!("dsb ishst", "tlbi vmalle1", "dsb ish", "isb");
            ptr::write_volatile(pte, value);
            asm!("dsb ishst", "tlbi vmalle1", "dsb ish", "isb");
        };
        replace(pa | attrs | 3 << 53);
        copy_record(CODE_VA as *mut u8, code.as_ptr(), o::CODE_BYTES);
        for offset in (0..o::CODE_BYTES).step_by(4) {
            asm!("dc cvau, {0}",in(reg)CODE_VA+offset as u64);
        }
        asm!("dsb ish");
        for offset in (0..o::CODE_BYTES).step_by(4) {
            asm!("ic ivau, {0}",in(reg)CODE_VA+offset as u64);
        }
        asm!("dsb ish", "isb");
        replace(pa | attrs | 1 << 7);
    }
}
#[inline(never)]
unsafe fn compile() {
    unsafe {
        let r = owner();
        let s = shared();
        let request =
            o::request((*s).lifecycle.vm, config(), id()).unwrap_or_else(|| stop(REJECTED, 504));
        (*r).request = request;
        (*r).expected = request;
        (*s).service_pc = service_resume as *const () as u64;
        let mut packet = request;
        service(&mut packet);
        while packet[0] == wire::BUSY {
            packet = request;
            packet[0] = natemu_abi::concurrency::POLL_COMPILE;
            (*r).expected = packet;
            service(&mut packet);
        }
        (*r).expected = request;
        (*r).reply = packet;
        if Some(packet) != o::reply(&request) || !source_matches() {
            stop(REJECTED, 505)
        }
        let (image, len) = o::image((*r).variant as usize).unwrap_or_else(|| stop(REJECTED, 506));
        (*r).code = image;
        (*r).code_length = len as u64;
        publish();
    }
}
#[inline(never)]
pub unsafe fn initialize() -> ! {
    unsafe {
        let c = config();
        let r = owner();
        let s = shared();
        let cpu = (*s).object.vcpu_id as u64;
        let isar0: u64;
        asm!("mrs {0},ID_AA64ISAR0_EL1",out(reg)isar0);
        (*r).isar0 = isar0;
        if cpu != (*r).id
            || cpu >= c.n
            || (*s).object.configured_vcpus as u64 != c.n
            || (*r).variant as usize != c.variant(cpu)
            || (*r).trial != 0
            || (*r).completed != 0
            || (*r).walks != 0
            || (*r).table_reads != 0
            || (*r).table_writes != 0
            || (*r).initial.sp != STACK_TOP
            || (*r).initial.x[28] != BANK_VA
            || (*r).initial.reserved != 0
            || (*s).lifecycle.vm == 0
            || !o::admitted_lse(isar0)
            || core::slice::from_raw_parts(CODE_VA as *const u8, o::CODE_BYTES)
                != [0; o::CODE_BYTES]
        {
            stop(REJECTED, 507)
        }
        let pointers = c.pointers(cpu);
        if [(*r).initial.x[6], (*r).initial.x[5], (*r).initial.x[10]] != pointers {
            stop(REJECTED, 508)
        }
        (*r).routes = [
            walk(o::DATA, Access::Write),
            walk(o::RESULTS, Access::Write),
            walk(o::RIP + cpu * 256, Access::Fetch),
            pointers[2],
        ];
        if (*r).table_reads != 12 || (*r).table_writes != 0 || !source_matches() {
            stop(REJECTED, 509)
        }
        if atomic_progress::enabled() {
            atomic_progress::initialize();
        }
        compile();
        common().compiled.value.fetch_or(1 << cpu, AcqRel);
        while common().compiled.value.load(Acquire) != all() {
            pause();
        }
        begin_trial()
    }
}
// Only owner0 touches shared RAM here; every prior trial has already drained.
#[inline(never)]
unsafe fn reset() {
    unsafe {
        let base = (PRIVATE + o::RAM_OFFSET as u64) as *mut u8;
        ptr::write_bytes(base, 0xa5, o::IMAGE_BYTES);
        for offset in [0, 128] {
            ptr::write_volatile(base.add(offset).cast::<u32>(), 0);
        }
        for role in 0..common().n as usize {
            ptr::write_volatile(base.add(256 + role * 128).cast::<u32>(), 0);
            ptr::write_volatile(base.add(4096 + role * 128).cast::<u32>(), 0);
        }
        if atomic_progress::enabled() {
            atomic_progress::reset_spare();
        }
        asm!("dsb sy");
    }
}
fn all_gate(gates: &[o::Gate; 4], epoch: u64) -> bool {
    (0..common().n as usize).all(|i| gates[i].value.load(Acquire) == epoch)
}
#[inline(never)]
unsafe fn begin_trial() -> ! {
    unsafe {
        let r = owner();
        let c = config();
        let index = (*r).trial;
        if index >= o::TRIALS as u64 || (*r).completed != index || (*trial()).completed != 0 {
            stop(PANIC, 510)
        }
        copy_frame(&raw mut (*r).current, &raw const (*r).initial);
        (*r).current.x[4] = c.token(id(), index);
        (*r).current.x[16] = CODE_VA;
        (*r).current.x[30] = returned as *const () as u64;
        let fault = if atomic_progress::first() {
            2
        } else {
            c.fault(id())
        };
        mapping(o::DATA, true, fault != 1);
        mapping(o::RESULTS, fault != 2, true);
        (*trial()).epoch = index + 1;
        (*trial()).token = (*r).current.x[4];
        copy_frame(&raw mut (*shared()).initial, &raw const (*r).current);
        (*shared()).capture_pc = returned as *const () as u64;
        let actlr: u64;
        asm!("mrs {0},ACTLR_EL1",out(reg)actlr);
        if actlr & 2 == 0 {
            stop(PANIC, 511)
        }
        if atomic_progress::enabled() {
            atomic_progress::admit(index);
        }
        common().ready[id() as usize]
            .value
            .store(index + 1, Release);
        if id() == 0 {
            while !all_gate(&common().ready, index + 1) {
                pause();
            }
            reset();
            common().epoch.value.store(index + 1, Release);
        } else {
            while common().epoch.value.load(Acquire) != index + 1 {
                pause();
            }
        }
        // No gate, helper or coordinator operation is inserted into this body.
        enter(&raw const (*shared()).initial)
    }
}
#[inline(never)]
unsafe fn recover() -> ! {
    unsafe {
        let r = owner();
        let t = trial();
        let fault = if atomic_progress::first() {
            2
        } else {
            config().fault(id())
        };
        let (esr, far, pc, spsr): (u64, u64, u64, u64);
        asm!("mrs {0},ESR_EL1","mrs {1},FAR_EL1","mrs {2},ELR_EL1","mrs {3},SPSR_EL1",out(reg)esr,out(reg)far,out(reg)pc,out(reg)spsr);
        let expected_pc = CODE_VA + if fault == 1 { 4 } else { 16 };
        let expected_address = if fault == 1 {
            o::DATA
        } else {
            o::RESULTS + id() * 128
        };
        if fault == 0
            || (*t).faults != 0
            || (*r).variant != 7
            || esr >> 26 != 0x25
            || esr & (1 << 6) == 0
            || esr & ((1 << 7) | (1 << 8) | (1 << 10)) != 0
            || spsr & 15 != 4
            || pc != expected_pc
            || far != expected_address
            || (fault == 1 && !matches!(esr & 63, 12..=15))
            || (fault == 2 && !matches!(esr & 63, 4..=7))
            || (*r).current.sp != STACK_TOP
            || (fault == 1 && (*r).current.x[4] != (*t).token)
        {
            stop(NATIVE_FAULT, 512)
        }
        let f = &raw mut (*t).fault[0];
        copy_frame(&raw mut (*f).frame, &raw const (*r).current);
        (*f).esr = esr;
        (*f).far = far;
        (*f).pc = pc;
        (*f).spsr = spsr;
        (*f).resume = pc;
        (*t).faults = 1;
        if atomic_progress::first() {
            atomic_progress::park(pc, spsr);
        }
        mapping(if fault == 1 { o::DATA } else { o::RESULTS }, true, true);
        // Post-RMW result-store repair resumes only STLR, never the prior SWPAL.
        resume_exception(&raw const (*r).current, pc, spsr)
    }
}
#[inline(never)]
pub unsafe fn finish(fault: u64) -> ! {
    unsafe {
        let r = owner();
        let t = trial();
        let index = (*r).trial;
        if index >= o::TRIALS as u64 || (*t).completed != 0 || (*t).epoch != index + 1 {
            stop(PANIC, 513)
        }
        copy_frame(&raw mut (*r).current, &raw const (*shared()).captured);
        if fault != 0 {
            recover()
        }
        if (*t).faults != u64::from(atomic_progress::first() || config().fault(id()) != 0) {
            stop(PANIC, 514)
        }
        // Actual native return capture supplies stores consuming all admitted live
        // register/vector results before the local fence completion boundary.
        copy_frame(&raw mut (*t).captured, &raw const (*r).current);
        let actlr: u64;
        asm!("mrs {0},ACTLR_EL1",out(reg)actlr);
        (*t).actlr = actlr;
        if actlr & 2 == 0 {
            stop(PANIC, 515)
        }
        if config().profile >= 10 {
            asm!("dsb sy", "isb");
            (*t).fence_completed = 1;
        }
        (*t).completed = 1;
        (*r).completed += 1;
        if atomic_progress::enabled() {
            atomic_progress::returned(index);
        }
        asm!("dsb sy");
        common().done[id() as usize].value.store(index + 1, Release);
        if id() == 0 {
            while !all_gate(&common().done, index + 1) {
                pause();
            }
            if atomic_progress::enabled() {
                atomic_progress::copy_image(
                    (PRIVATE
                        + o::IMAGE_VA_OFFSET as u64
                        + index * natemu_abi::atomic_progress::IMAGE_BYTES as u64)
                        as *mut u8,
                );
            } else {
                copy_record(
                    (PRIVATE + o::IMAGE_VA_OFFSET as u64 + index * o::IMAGE_BYTES as u64)
                        as *mut u8,
                    (PRIVATE + o::RAM_OFFSET as u64) as *const u8,
                    o::IMAGE_BYTES,
                );
            }
            asm!("dsb sy");
        }
        (*r).trial += 1;
        if (*r).trial == o::TRIALS as u64 {
            if atomic_progress::enabled() {
                atomic_progress::stopped();
            }
            common().stopped.value.fetch_or(1 << id(), AcqRel);
            while common().stopped.value.load(Acquire) != all() {
                pause();
            }
            stop(SUCCESS, 0)
        }
        begin_trial()
    }
}
