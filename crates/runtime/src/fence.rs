//! Live generated dependencies cross a private capture/completion boundary.
use super::*;
use core::sync::atomic::Ordering::{AcqRel, Acquire, Release};
use natemu_abi::{
    fence as o,
    paging::{self, Access, Control, Tables},
    shadow, translation as wire,
};
unsafe extern "C" {
    fn service(packet: *mut wire::Packet);
    fn service_resume();
    fn enter_allocated(frame: *const NativeFrame, pc: u64, spsr: u64) -> !;
    fn returned_allocated();
    fn fence_sink_gate();
}
fn owner() -> *mut o::Owner {
    (PRIVATE + o::OWNER_OFFSET as u64) as *mut o::Owner
}
fn common() -> &'static o::Common {
    unsafe { &*((PRIVATE + o::COMMON_OFFSET as u64) as *const o::Common) }
}
fn config() -> o::Config {
    let c = common();
    o::Config::new(c.n, c.case).unwrap_or_else(|| stop(REJECTED, 500))
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
        let bytes = o::source();
        core::slice::from_raw_parts(
            (PRIVATE + o::RAM_OFFSET as u64 + o::RIP - o::DATA + id() * 256) as *const u8,
            bytes.len(),
        ) == bytes.as_slice()
    }
}
unsafe fn mapping(address: u64, present: bool, writable: bool) {
    unsafe {
        if !matches!(address, o::DATA | o::RESULTS) {
            stop(PANIC, 503)
        }
        let slot = shadow::leaf_offset(address).unwrap_or_else(|| stop(PANIC, 503));
        let pte = (PRIVATE + slot as u64) as *mut u64;
        ptr::write_volatile(pte, 0);
        asm!("dsb ishst", "tlbi vmalle1", "dsb ish", "isb");
        if present {
            let pa = IPA + o::RAM_OFFSET as u64 + address - o::DATA;
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
        (*s).service_pc = service_resume as *const () as u64;
        for chunk in 0..o::CHUNKS {
            let request = o::request((*s).lifecycle.vm, config(), id(), chunk as u64)
                .unwrap_or_else(|| stop(REJECTED, 604));
            (*r).requests[chunk] = request;
            (*r).expected = request;
            let mut packet = request;
            service(&mut packet);
            while packet[0] == wire::BUSY {
                packet = request;
                packet[0] = natemu_abi::concurrency::POLL_COMPILE;
                (*r).expected = packet;
                service(&mut packet);
            }
            (*r).expected = request;
            (*r).replies[chunk] = packet;
            (*r).transfers += 1;
            if Some(packet) != o::reply(&request) || !source_matches() {
                stop(REJECTED, 605)
            }
        }
        (*r).sink_pc = fence_sink_gate as *const () as u64;
        (*r).return_pc = returned_allocated as *const () as u64;
        (*r).code =
            o::image(CODE_VA, (*r).sink_pc, (*r).return_pc).unwrap_or_else(|| stop(REJECTED, 606));
        (*r).code_length = o::CODE_BYTES as u64;
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
        if cpu != (*r).id
            || cpu >= c.n
            || (*s).object.configured_vcpus as u64 != c.n
            || (*r).trial != 0
            || (*r).completed != 0
            || (*r).transfers != 0
            || (*r).walks != 0
            || (*r).table_reads != 0
            || (*r).table_writes != 0
            || (*r).initial.sp != STACK_TOP
            || (*r).initial.x[28] != BANK_VA
            || (*r).initial.reserved != 0
            || (*s).lifecycle.vm == 0
            || core::slice::from_raw_parts(CODE_VA as *const u8, o::CODE_BYTES)
                != [0; o::CODE_BYTES]
        {
            stop(REJECTED, 607)
        }
        (*r).routes = [
            walk(o::DATA, Access::Write),
            walk(o::RESULTS, Access::Write),
            walk(o::RIP + cpu * 256, Access::Fetch),
        ];
        if (*r).table_reads != 12 || (*r).table_writes != 0 || !source_matches() {
            stop(REJECTED, 609)
        }
        mapping(o::DATA, true, true);
        mapping(o::RESULTS, true, true);
        compile();
        common().compiled.value.fetch_or(1 << cpu, AcqRel);
        while common().compiled.value.load(Acquire) != all() {
            pause();
        }
        begin_trial()
    }
}
fn all_gate(gates: &[o::Gate; 4], epoch: u64) -> bool {
    (0..common().n as usize).all(|i| gates[i].value.load(Acquire) == epoch)
}
#[inline(never)]
unsafe fn begin_trial() -> ! {
    unsafe {
        let r = owner();
        let t = trial();
        let index = (*r).trial;
        if index >= o::TRIALS as u64
            || (*r).completed != index
            || (*t).completed != 0
            || (*t).sink_count != 0
        {
            stop(PANIC, 610)
        }
        let selected = config().selected(id(), index);
        let operands = o::operands(selected).unwrap();
        let vectors = o::vectors(selected).unwrap();
        copy_frame(&raw mut (*r).current, &raw const (*r).initial);
        (*r).current.x[7] = operands[0];
        (*r).current.x[5] = operands[1];
        (*r).current.x[11] = o::RESULTS + id() * 128;
        (*r).current.fpcr = 0;
        (*r).current.fpsr = 0;
        for lane in 0..4 {
            (&mut (*r).current.q[16])[lane * 4..lane * 4 + 4]
                .copy_from_slice(&vectors[0][lane].to_le_bytes());
            (&mut (*r).current.q[17])[lane * 4..lane * 4 + 4]
                .copy_from_slice(&vectors[1][lane].to_le_bytes());
        }
        (*t).epoch = index + 1;
        (*t).case = selected;
        (*t).mxcsr_before = o::mxcsr_before(selected).unwrap() as u64;
        (*t).continuation = CODE_VA + o::CONTINUATION as u64;
        copy_frame(&raw mut (*t).initial, &raw const (*r).current);
        copy_frame(&raw mut (*shared()).initial, &raw const (*r).current);
        (*shared()).capture_pc = (*r).return_pc;
        common().ready[id() as usize]
            .value
            .store(index + 1, Release);
        if id() == 0 {
            while !all_gate(&common().ready, index + 1) {
                pause();
            }
            ptr::write_bytes(
                (PRIVATE + o::RAM_OFFSET as u64) as *mut u8,
                0xa5,
                o::IMAGE_BYTES,
            );
            asm!("dsb sy");
            common().epoch.value.store(index + 1, Release);
        } else {
            while common().epoch.value.load(Acquire) != index + 1 {
                pause();
            }
        }
        enter_allocated(&raw const (*r).current, CODE_VA, 0x3c4 | (*r).current.nzcv)
    }
}
#[unsafe(no_mangle)]
#[inline(never)]
unsafe extern "C" fn fence_sink_handler(frame: *const NativeFrame) -> ! {
    unsafe {
        let r = owner();
        let t = trial();
        if (*shared()).mode != o::MODE
            || (*r).trial >= o::TRIALS as u64
            || (*t).epoch != (*r).trial + 1
            || (*t).sink_count != 0
            || (*t).completed != 0
        {
            stop(PANIC, 611)
        }
        // Assembly has already stored every native root and completed those stores.
        copy_frame(&raw mut (*t).sink, frame);
        copy_record(
            (*t).sink_output.as_mut_ptr(),
            (PRIVATE + o::RAM_OFFSET as u64 + 4096 + id() * 128) as *const u8,
            40,
        );
        if (*t).sink.sp != STACK_TOP || (*t).sink.reserved != 0 || (*t).sink_output != [0xa5; 40] {
            stop(REJECTED, 612)
        }
        (*t).sink_count = 1;
        asm!("dsb sy", "isb");
        (*t).fence_completed = 1;
        // Full ERET restore retains x16/x30 and deferred flags; there is no peer gate.
        enter_allocated(
            &raw const (*t).sink,
            (*t).continuation,
            0x3c4 | (*t).sink.nzcv,
        )
    }
}
#[inline(never)]
pub unsafe fn finish(fault: u64) -> ! {
    unsafe {
        let r = owner();
        let t = trial();
        let index = (*r).trial;
        if index >= o::TRIALS as u64
            || (*t).completed != 0
            || (*t).epoch != index + 1
            || (*t).sink_count != 1
            || (*t).fence_completed != 1
        {
            stop(PANIC, 613)
        }
        if fault != 0 {
            stop(NATIVE_FAULT, 614)
        }
        copy_frame(&raw mut (*r).current, &raw const (*shared()).captured);
        copy_frame(&raw mut (*t).captured, &raw const (*r).current);
        let actlr: u64;
        asm!("mrs {0},ACTLR_EL1",out(reg)actlr);
        (*t).actlr = actlr;
        if actlr & 2 == 0 {
            stop(PANIC, 615)
        }
        if !source_matches()
            || (*r).current.sp != STACK_TOP
            || (*r).current.x[11] != o::RESULTS + id() * 128
            || (*r).current.fpcr != 0
            || (*r).current.fpsr & !0x10 != 0
        {
            stop(REJECTED, 616)
        }
        // Actual STMXCSR bytes use the admitted masked-RN status adapter. FEX's
        // GetMXCSR drops exception bits; preserve actual computed FPSR instead.
        let fpsr = (*r).current.fpsr;
        let status = (fpsr & 1)
            | ((fpsr >> 7) & 1) << 1
            | ((fpsr >> 1) & 1) << 2
            | ((fpsr >> 2) & 1) << 3
            | ((fpsr >> 3) & 1) << 4
            | ((fpsr >> 4) & 1) << 5;
        (*t).mxcsr = (*t).mxcsr_before | status;
        ptr::write_volatile(
            (PRIVATE + o::RAM_OFFSET as u64 + 4096 + id() * 128 + 32) as *mut u32,
            (*t).mxcsr as u32,
        );
        (*t).completed = 1;
        (*r).completed += 1;
        asm!("dsb sy");
        common().done[id() as usize].value.store(index + 1, Release);
        if id() == 0 {
            while !all_gate(&common().done, index + 1) {
                pause();
            }
            copy_record(
                (PRIVATE + o::IMAGE_VA_OFFSET as u64 + index * o::IMAGE_BYTES as u64) as *mut u8,
                (PRIVATE + o::RAM_OFFSET as u64) as *const u8,
                o::IMAGE_BYTES,
            );
            asm!("dsb sy");
        }
        (*r).trial += 1;
        if (*r).trial == o::TRIALS as u64 {
            common().stopped.value.fetch_or(1 << id(), AcqRel);
            while common().stopped.value.load(Acquire) != all() {
                pause();
            }
            stop(SUCCESS, 0)
        }
        begin_trial()
    }
}
