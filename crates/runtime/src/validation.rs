use super::*;
use natemu_abi::{
    paging::{self, Access, Fault},
    recovery::{FaultTrace, GUEST_VA, GuestState, MAX_FAULTS, Recovery, SOURCE_OFFSET, STATE_VA},
    validation::*,
};

unsafe extern "C" {
    fn resume_exception(frame: *const NativeFrame, pc: u64, spsr: u64) -> !;
    fn service(packet: *mut Packet);
    fn service_resume();
}
pub(super) fn state() -> *mut State {
    (PRIVATE + STATE_OFFSET as u64) as *mut State
}
pub(super) fn recovery_state() -> *mut Recovery {
    STATE_VA as *mut Recovery
}
pub(super) fn guest() -> *mut GuestState {
    GUEST_VA as *mut GuestState
}

unsafe fn exception(vector: u64, address: u64, error: u64) -> ! {
    unsafe { exception_at(vector, address, error, (*state()).guest_rip) }
}
pub(super) unsafe fn exception_at(vector: u64, address: u64, error: u64, rip: u64) -> ! {
    unsafe {
        let r = recovery_state();
        ptr::write_volatile(&raw mut (*r).vector, vector);
        ptr::write_volatile(&raw mut (*r).error, error);
        ptr::write_volatile(&raw mut (*r).linear, address);
        ptr::write_volatile(&raw mut (*r).rip, rip);
        ptr::write_volatile(&raw mut (*guest()).rip, rip);
        if vector == 14 {
            ptr::write_volatile(&raw mut (*guest()).control[1], address);
        }
        ptr::write_volatile(&raw mut (*state()).stage, 3);
        stop(SUCCESS, 0)
    }
}

pub unsafe fn initialize() -> ! {
    unsafe {
        if (*state()).case == natemu_abi::stack::CASE {
            super::stack::initialize()
        }
        let s = shared();
        let v = state();
        let g = guest();
        let rip = (*v).guest_rip;
        if (*v).case != 0
            || (*v).stage != 0
            || (*v).completed != 0
            || (*g).rip != rip
            || (*s).tcr != shadow::TCR_BOTH
            || (*v).live > 1
            || (*v).requests != 0
            || (*v).request != [0; 16]
            || (*v).reply != [0; 16]
        {
            stop(REJECTED, 90)
        }
        let code = core::slice::from_raw_parts(CODE_VA as *const u8, BODY.len() + RETURN.len());
        if if (*v).live == 0 {
            code[..8] != BODY || code[8..] != RETURN
        } else {
            (*s).lifecycle.vm == 0 || code != [0; 12]
        } {
            stop(REJECTED, 90)
        }
        let mut frame = read_record(&raw const (*s).initial);
        if frame.sp != STACK_TOP || frame.x[6] != (*g).gpr[3] {
            stop(REJECTED, 91)
        }
        let operand = frame.x[6];
        ptr::write_volatile(&raw mut (*v).operand, operand);
        ptr::write_volatile(&raw mut (*v).width, 8);
        if !paging::canonical_span(rip, SOURCE.len() as u64) {
            exception(13, rip, 0)
        }
        match recovery::walk(rip, Access::Fetch) {
            Ok(t) if t.gpa == natemu_abi::translation::SOURCE_VA && t.wb => {}
            Err(Fault::Page { address, error }) => exception(14, address, error),
            _ => stop(REJECTED, 92),
        }
        if core::slice::from_raw_parts((PRIVATE + SOURCE_OFFSET as u64) as *const u8, SOURCE.len())
            != SOURCE
        {
            stop(REJECTED, 93)
        }
        // The one due instruction is guarded before entering native code. Never
        // let an x86 noncanonical address reach the native-private address range.
        if !paging::canonical_span(operand, 8) {
            exception(13, operand, 0)
        }
        // This first profile admits aligned ordinary RAM within a single page.
        // Wider spans require per-subaccess effects and are still unsupported.
        if operand & 7 != 0 || shadow::leaf_offset(operand & !4095).is_none() {
            stop(REJECTED, 94)
        }
        if (*v).live == 1 {
            let mut packet = request((*s).lifecycle.vm, rip);
            let expected = reply(&packet).unwrap_or_else(|| stop(REJECTED, 99));
            write_record(&raw mut (*v).request, packet);
            ptr::write_volatile(&raw mut (*v).requests, 1);
            ptr::write_volatile(&raw mut (*s).service_pc, service_resume as *const () as u64);
            service(&mut packet);
            write_record(&raw mut (*v).reply, packet);
            if packet != expected
                || core::slice::from_raw_parts(
                    (PRIVATE + SOURCE_OFFSET as u64) as *const u8,
                    SOURCE.len(),
                ) != SOURCE
            {
                stop(REJECTED, 100)
            }
            let mut native = [0; 12];
            native[..8].copy_from_slice(&packet[12].to_le_bytes());
            native[8..].copy_from_slice(&RETURN);
            // The first immutable slot has no entrants or saved continuations.
            translation::replace_bytes(&native);
        }
        let capture_pc = returned as *const () as u64;
        frame.x[16] = CODE_VA;
        frame.x[30] = capture_pc;
        write_record(&raw mut (*s).initial, frame);
        ptr::write_volatile(&raw mut (*s).capture_pc, capture_pc);
        ptr::write_volatile(&raw mut (*v).stage, 1);
        asm!("dsb ish", "ic iallu", "dsb ish", "isb");
        enter(&raw const (*s).initial)
    }
}

pub unsafe fn finish(fault: u64) -> ! {
    unsafe {
        if (*state()).case == natemu_abi::stack::CASE {
            super::stack::finish(fault)
        }
        let s = shared();
        let r = recovery_state();
        let v = state();
        let frame = &*(&raw const (*s).captured);
        if (*v).stage != 1 {
            stop(REJECTED, 95)
        }
        if fault == 0 {
            ptr::write_volatile(&raw mut (*guest()).gpr[0], frame.x[4]);
            ptr::write_volatile(
                &raw mut (*guest()).rip,
                (*v).guest_rip + SOURCE.len() as u64,
            );
            ptr::write_volatile(&raw mut (*v).completed, 1);
            ptr::write_volatile(&raw mut (*v).stage, 2);
            stop(SUCCESS, 0)
        }
        let (esr, far, pc, spsr) = ((*s).esr, (*s).far, (*s).elr, (*s).spsr);
        if esr >> 26 != 0x25
            || (esr >> 6) & 1 != 0
            || !matches!(esr & 63, 4..=7 | 12..=15)
            || far != (*v).operand
            || pc != CODE_VA
            || spsr & 15 != 4
            || frame.sp != STACK_TOP
            || frame.x[6] != (*v).operand
            || frame.x[4] != (*s).initial.x[4]
        {
            stop(NATIVE_FAULT, 96)
        }
        let count = (*r).faults as usize;
        if count >= MAX_FAULTS {
            stop(REJECTED, 97)
        }
        let result = recovery::walk(far, Access::Read);
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
        ptr::write_volatile(&raw mut (*r).faults, count as u64 + 1);
        match result {
            Ok(t) => {
                recovery::install(far, t);
                ptr::write_volatile(&raw mut (*r).retries, (*r).retries + 1);
                resume_exception(&raw const (*s).captured, pc, spsr)
            }
            Err(Fault::Page { address, error }) => exception(14, address, error),
            _ => stop(REJECTED, 98),
        }
    }
}
