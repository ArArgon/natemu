//! Finite REP MOVSB helper with a changed-on-read device source. State remains
//! canonical between elements; an internal shadow retry resumes only the store.
use super::*;
use natemu_abi::{
    paging::{Access, Fault},
    recovery::*,
};
unsafe extern "C" {
    fn partial_store(address: u64, value: u64);
    fn resume_exception(frame: *const NativeFrame, pc: u64, spsr: u64) -> !;
    fn leave_exception() -> !;
}
fn state() -> *mut PartialState {
    PARTIAL_VA as *mut PartialState
}
pub fn enabled() -> bool {
    unsafe { ptr::read_volatile(&raw const (*shared()).mode) == PARTIAL_MODE }
}
pub fn owns(pc: u64) -> bool {
    enabled() && pc == partial_store as *const () as u64
}
#[inline(never)]
pub unsafe fn run() -> u64 {
    unsafe {
        let mut g = read_record(GUEST_VA as *const GuestState);
        let prefix = g;
        // Explicit control-plane fixture admission; these are the next block's inputs,
        // not hidden effects attributed to the preceding FEX fragment.
        g.gpr[1] = 2;
        g.gpr[6] = DEVICE_VA;
        g.gpr[7] = DATA_VA + 4095;
        g.rip = natemu_abi::translation::SOURCE_VA + 256;
        if g.rflags & (1 << 10) != 0 || ptr::read_volatile(g.rip as *const u16) != 0xa4f3 {
            stop(REJECTED, 80)
        }
        match recovery::walk(g.rip, Access::Fetch) {
            Ok(t) if t.wb && t.gpa == g.rip => (),
            _ => stop(REJECTED, 81),
        }
        let p = PartialState {
            reads: 0,
            writes: 0,
            faults: 0,
            retries: 0,
            phase: 0,
            value: 0,
            read_values: [0; 2],
            prefix,
            store_pc: partial_store as *const () as u64,
            at_fault_counts: [0; 4],
            before: g,
            trace: FaultTrace::ZERO,
        };
        write_record(state(), p);
        write_record(GUEST_VA as *mut GuestState, g);
        for index in 0..2 {
            if ptr::read_volatile(&raw const (*state()).phase) != 0
                || g.gpr[6] != DEVICE_VA + index
                || g.gpr[1] != 2 - index
            {
                stop(PANIC, 82)
            }
            let reads = ptr::read_volatile(&raw const (*state()).reads) + 1;
            let value = 0x40 + reads * 0x17;
            ptr::write_volatile(&raw mut (*state()).reads, reads);
            ptr::write_volatile(&raw mut (*state()).value, value);
            ptr::write_volatile(&raw mut (*state()).read_values[index as usize], value);
            ptr::write_volatile(&raw mut (*state()).phase, 1);
            partial_store(g.gpr[7], value);
            // Native return means this element's single-byte write completed.
            ptr::write_volatile(&raw mut (*state()).writes, index + 1);
            ptr::write_volatile(&raw mut (*state()).phase, 0);
            g.gpr[1] -= 1;
            g.gpr[6] += 1;
            g.gpr[7] += 1;
            write_record(GUEST_VA as *mut GuestState, g);
        }
        g.rip += 2;
        write_record(GUEST_VA as *mut GuestState, g);
        PARTIAL_DONE
    }
}
pub unsafe fn fault() -> ! {
    unsafe {
        let s = shared();
        let f = read_record(&raw const (*s).captured);
        let esr = (*s).esr;
        let far = (*s).far;
        let pc = (*s).elr;
        let spsr = (*s).spsr;
        let g = read_record(GUEST_VA as *const GuestState);
        if pc != partial_store as *const () as u64
            || esr >> 26 != 0x25
            || (esr >> 6) & 1 != 1
            || !matches!(esr&63,4..=7|12..=15)
            || spsr & 15 != 4
            || far != g.gpr[7]
            || f.x[0] != far
            || f.x[1] != (*state()).value
            || (*state()).phase != 1
            || (*state()).faults != 0
            || (*state()).reads != 2
            || (*state()).writes != 1
            || g.gpr[1] != 1
            || far != DATA_VA + 4096
        {
            stop(NATIVE_FAULT, 83)
        }
        write_record((PRIVATE + 0x27000) as *mut GuestState, g);
        ptr::write_volatile(
            &raw mut (*state()).at_fault_counts,
            [
                (*state()).reads,
                (*state()).writes,
                (*state()).phase,
                (*state()).value,
            ],
        );
        ptr::write_volatile(&raw mut (*state()).faults, 1);
        let result = recovery::walk(far, Access::Write);
        let (outcome, error) = match result {
            Ok(_) => (1, 0),
            Err(Fault::Page { error, .. }) => (2, error),
            _ => (3, 0),
        };
        write_record(
            &raw mut (*state()).trace,
            FaultTrace {
                frame: f,
                esr,
                far,
                elr: pc,
                spsr,
                outcome,
                error,
            },
        );
        match result {
            Ok(t) if t.wb && t.gpa == far && t.writable && t.dirty => {
                recovery::install(far, t);
                ptr::write_volatile(&raw mut (*state()).retries, 1);
                resume_exception(&raw const (*s).captured, pc, spsr);
            }
            Err(Fault::Page { address, error }) => {
                recovery::guest_fault(address, error, g.rip);
                ptr::write_volatile(&raw mut (*s).lifecycle.event, PAGE_FAULT);
                leave_exception();
            }
            _ => stop(REJECTED, 84),
        }
    }
}
