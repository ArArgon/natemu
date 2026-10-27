use super::*;
use natemu_abi::{
    paging::{self, Access, Fault},
    recovery::*,
};

unsafe extern "C" {
    fn resume_exception(frame: *const NativeFrame, pc: u64, spsr: u64) -> !;
    fn leave_exception() -> !;
}
fn state() -> *mut Recovery {
    STATE_VA as *mut Recovery
}
fn guest() -> *mut GuestState {
    GUEST_VA as *mut GuestState
}
/// The owner's guest data pages; ordinary native accesses reach them through
/// shadows. Guest table pages are never shadowed: their writes must stay visible
/// to the runtime.
pub(super) const GUEST_DATA: GuestRam =
    unsafe { GuestRam::new(DATA_VA, 0x3000, PRIVATE + DATA_OFFSET as u64) };
/// The owner's whole guest RAM (table pages and data pages), for the walker.
pub(super) const RAM: GuestRam = unsafe {
    GuestRam::new(0x1000, 0x4000, PRIVATE + TABLE_OFFSET as u64).and(
        DATA_VA,
        0x3000,
        PRIVATE + DATA_OFFSET as u64,
    )
};
pub unsafe fn initialize() {
    unsafe { clear_mapping(DATA_VA) }
}
pub unsafe fn clear_mapping(address: u64) {
    tables::unmap(address).unwrap_or_else(|_| stop(REJECTED, 46))
}
pub unsafe fn walk(address: u64, access: Access) -> Result<paging::Translation, Fault> {
    unsafe {
        let r = state();
        let n = ptr::read_volatile(&raw const (*r).walks);
        ptr::write_volatile(&raw mut (*r).walks, n + 1);
        paging::walk(
            &mut GuestTables::new(RAM),
            ptr::read_volatile(&raw const (*r).control),
            address,
            access,
        )
    }
}
pub unsafe fn guest_fault(address: u64, error: u64, rip: u64) {
    unsafe {
        let r = state();
        ptr::write_volatile(&raw mut (*r).vector, 14);
        ptr::write_volatile(&raw mut (*r).error, error);
        ptr::write_volatile(&raw mut (*r).linear, address);
        ptr::write_volatile(&raw mut (*r).rip, rip);
        ptr::write_volatile(&raw mut (*guest()).control[1], address);
        ptr::write_volatile(&raw mut (*guest()).rip, rip);
    }
}
pub unsafe fn fetch() -> bool {
    unsafe {
        let rip = natemu_abi::translation::SOURCE_VA;
        match walk(rip, Access::Fetch) {
            Ok(t) if t.gpa == rip && t.wb => true,
            Err(Fault::Page { address, error }) => {
                guest_fault(address, error, rip);
                false
            }
            _ => stop(REJECTED, 40),
        }
    }
}
#[inline(never)]
pub unsafe fn completed() {
    unsafe {
        let mut g = read_record(guest());
        let f = read_record(&raw const (*shared()).captured);
        g.materialize(&f, 17, true);
        write_record(guest(), g);
    }
}
pub unsafe fn fault() -> ! {
    unsafe {
        let s = shared();
        let r = state();
        let frame = &*(&raw const (*s).captured);
        let esr = ptr::read_volatile(&raw const (*s).esr);
        let far = ptr::read_volatile(&raw const (*s).far);
        let pc = ptr::read_volatile(&raw const (*s).elr);
        let spsr = ptr::read_volatile(&raw const (*s).spsr);
        let (access, width, operand, guest_offset) = if pressure::enabled() {
            let (i, w, o, g) = pressure::site(pc, frame);
            (if i == 0 { Access::Read } else { Access::Write }, w, o, g)
        } else {
            match pc.checked_sub(CODE_VA) {
                Some(16) => (Access::Read, 8, frame.x[6], 7),
                Some(36) => (Access::Write, 16, frame.x[11], 13),
                _ => stop(NATIVE_FAULT, 41),
            }
        };
        let write = access == Access::Write;
        if esr >> 26 != 0x25
            || ((esr >> 6) & 1) != u64::from(write)
            || !matches!(esr&63,4..=7|12..=15)
            || far != operand
            || operand & (width - 1) != 0
            || operand < DATA_VA
            || operand + width > DATA_VA + 256
            || spsr & 15 != 4
            || frame.sp
                != if pressure::enabled() {
                    STACK_TOP - 768
                } else {
                    STACK_TOP
                }
        {
            stop(NATIVE_FAULT, 42);
        }
        let count = ptr::read_volatile(&raw const (*r).faults) as usize;
        if count >= MAX_FAULTS {
            stop(PANIC, 43);
        }
        if pressure::enabled() {
            pressure::materialize(frame, usize::from(write), false);
            pressure::snapshot(count);
        } else {
            let mut g = read_record(guest());
            g.materialize(frame, guest_offset, false);
            write_record(guest(), g);
        }
        let result = walk(far, access);
        let (outcome, error) = match result {
            Ok(_) => (1, 0),
            Err(Fault::Page { error, .. }) => (2, error),
            _ => (3, 0),
        };
        write_record(
            &raw mut (*r).trace[count],
            FaultTrace {
                frame: *frame,
                esr,
                far,
                elr: pc,
                spsr,
                outcome,
                error,
            },
        );
        ptr::write_volatile(&raw mut (*r).faults, (count + 1) as u64);
        match result {
            Ok(t) => {
                if !t.wb || !(DATA_VA..DATA_VA + 0x3000).contains(&t.gpa) {
                    stop(REJECTED, 44);
                }
                install(far, t);
                let retries = ptr::read_volatile(&raw const (*r).retries);
                ptr::write_volatile(&raw mut (*r).retries, retries + 1);
                resume_exception(&raw const (*s).captured, pc, spsr);
            }
            Err(Fault::Page { address, error }) => {
                guest_fault(
                    address,
                    error,
                    natemu_abi::translation::SOURCE_VA + guest_offset,
                );
                write_record(&raw mut (*s).initial, *frame);
                ptr::write_volatile(&raw mut (*s).lifecycle.event, PAGE_FAULT);
                if pressure::enabled() {
                    pressure::restore_native_fp();
                }
                leave_exception();
            }
            _ => stop(REJECTED, 45),
        }
    }
}

pub unsafe fn install(address: u64, t: paging::Translation) {
    unsafe {
        if !t.wb {
            stop(REJECTED, 44)
        }
        install_ram(address, t.gpa, t.writable && t.dirty);
    }
}

// Physical ownership and native permissions do not require a guest PTE or D bit.
pub unsafe fn install_ram(address: u64, gpa: u64, writable: bool) {
    match tables::install(address, &GUEST_DATA, gpa, writable) {
        Ok(()) => {}
        Err(natemu_abi::shadow::Error::OutsideRam) => stop(REJECTED, 44),
        Err(_) => stop(REJECTED, 46),
    }
}
