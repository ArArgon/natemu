use super::validation::{exception_at, guest, recovery_state, state};
use super::*;
use natemu_abi::{
    paging::{self, Access, Fault},
    recovery::{FaultTrace, MAX_FAULTS, SOURCE_OFFSET},
    stack as profile,
};

unsafe extern "C" {
    fn service(packet: *mut natemu_abi::validation::Packet);
    fn service_resume();
    fn resume_exception(frame: *const NativeFrame, pc: u64, spsr: u64) -> !;
}
fn trace() -> *mut profile::State {
    (PRIVATE + profile::STATE_OFFSET as u64) as *mut profile::State
}

unsafe fn guard(step: usize, frame: &NativeFrame) {
    unsafe {
        let width = profile::WIDTHS[step];
        let operand = if step < 3 {
            frame.x[8].wrapping_sub(width)
        } else {
            frame.x[5].wrapping_sub(if step == 3 { 8 } else { 12 })
        };
        let v = state();
        ptr::write_volatile(&raw mut (*v).operand, operand);
        ptr::write_volatile(&raw mut (*v).width, width);
        if !paging::canonical_span(operand, width) {
            exception_at(
                if step < 3 { 12 } else { 13 },
                operand,
                0,
                (*v).guest_rip + profile::GUEST_OFFSETS[step],
            );
        }
        // Single-page aligned accesses in this slice. Noncanonical spans above
        // still fault architecturally; other unsupported spans never execute.
        if operand & (width - 1) != 0 || shadow::leaf_offset(operand & !4095).is_none() {
            stop(REJECTED, 114)
        }
    }
}

unsafe fn enter_step() -> ! {
    unsafe {
        let v = state();
        let step = (*v).completed as usize;
        if step >= 5 || (*trace()).entries[step] != 0 {
            stop(REJECTED, 116)
        }
        let s = shared();
        let mut frame = read_record(&raw const (*s).initial);
        guard(step, &frame);
        frame.x[16] = CODE_VA + (step * profile::SLOT_BYTES) as u64;
        frame.x[30] = returned as *const () as u64;
        write_record(&raw mut (*s).initial, frame);
        ptr::write_volatile(&raw mut (*s).capture_pc, frame.x[30]);
        ptr::write_volatile(&raw mut (*v).stage, 1);
        ptr::write_volatile(&raw mut (*trace()).entries[step], 1);
        enter(&raw const (*s).initial)
    }
}

pub unsafe fn initialize() -> ! {
    unsafe {
        let s = shared();
        let v = state();
        let g = guest();
        let rip = (*v).guest_rip;
        if (*v).case != profile::CASE
            || (*v).stage != 0
            || (*v).completed != 0
            || (*v).live != 1
            || (*v).requests != 0
            || (*s).lifecycle.vm == 0
            || (*v).request != [0; 16]
            || (*v).reply != [0; 16]
            || (*s).tcr != shadow::TCR_BOTH
            || (*g).rip != rip
            || (*trace()).transfers != 0
            || (*trace()).entries != [0; 5]
            || (*trace()).completions != [0; 5]
            || core::slice::from_raw_parts(CODE_VA as *const u8, profile::CODE_BYTES)
                != [0; profile::CODE_BYTES]
        {
            stop(REJECTED, 110)
        }
        let frame = &*(&raw const (*s).initial);
        if frame.sp != STACK_TOP
            || frame.x[8] != (*g).gpr[4]
            || frame.x[5] != (*g).gpr[2]
            || frame.x[6] != (*g).gpr[3]
        {
            stop(REJECTED, 111)
        }
        if !paging::canonical_span(rip, profile::SOURCE.len() as u64) {
            exception_at(13, rip, 0, rip)
        }
        match recovery::walk(rip, Access::Fetch) {
            Ok(t) if t.gpa == natemu_abi::translation::SOURCE_VA && t.wb => {}
            Err(Fault::Page { address, error }) => exception_at(14, address, error, rip),
            _ => stop(REJECTED, 112),
        }
        if core::slice::from_raw_parts(
            (PRIVATE + SOURCE_OFFSET as u64) as *const u8,
            profile::SOURCE.len(),
        ) != profile::SOURCE
        {
            stop(REJECTED, 113)
        }
        guard(0, frame);
        ptr::write_volatile(&raw mut (*v).requests, 1);
        ptr::write_volatile(&raw mut (*s).service_pc, service_resume as *const () as u64);
        let mut body = [0; 52];
        for chunk in 0..3 {
            let mut packet = profile::request((*s).lifecycle.vm, rip, chunk as u64);
            let expected = profile::reply(&packet).unwrap_or_else(|| stop(REJECTED, 115));
            write_record(&raw mut (*trace()).requests[chunk], packet);
            if chunk == 0 {
                write_record(&raw mut (*v).request, packet)
            }
            ptr::write_volatile(&raw mut (*trace()).transfers, chunk as u64 + 1);
            service(&mut packet);
            write_record(&raw mut (*trace()).replies[chunk], packet);
            if chunk == 0 {
                write_record(&raw mut (*v).reply, packet)
            }
            if packet != expected {
                stop(REJECTED, 115)
            }
            let mut bytes = [0; 24];
            for i in 0..3 {
                bytes[i * 8..i * 8 + 8].copy_from_slice(&packet[10 + i].to_le_bytes());
            }
            let start = chunk * 24;
            let count = packet[15] as usize;
            body[start..start + count].copy_from_slice(&bytes[..count]);
        }
        if body != profile::BODY
            || core::slice::from_raw_parts(
                (PRIVATE + SOURCE_OFFSET as u64) as *const u8,
                profile::SOURCE.len(),
            ) != profile::SOURCE
        {
            stop(REJECTED, 115)
        }
        translation::replace_bytes(&profile::image(&body));
        enter_step()
    }
}

pub unsafe fn finish(fault: u64) -> ! {
    unsafe {
        let s = shared();
        let v = state();
        let r = recovery_state();
        let t = trace();
        let step = (*v).completed as usize;
        if step >= 5 || (*v).stage != 1 || (*t).entries[step] != 1 || (*t).completions[step] != 0 {
            stop(REJECTED, 116)
        }
        let frame = &*(&raw const (*s).captured);
        let initial = &*(&raw const (*s).initial);
        if frame.sp != STACK_TOP || frame.x[5] != initial.x[5] {
            stop(NATIVE_FAULT, 117)
        }
        if fault == 0 {
            if frame.x[8] != if step < 3 { (*v).operand } else { initial.x[8] } {
                stop(NATIVE_FAULT, 117)
            }
            if step < 3 {
                ptr::write_volatile(&raw mut (*guest()).gpr[4], frame.x[8]);
            }
            if step == 3 {
                ptr::write_volatile(&raw mut (*guest()).gpr[0], frame.x[4]);
            }
            if step == 4 {
                ptr::write_volatile(&raw mut (*guest()).gpr[3], frame.x[6]);
            }
            ptr::write_volatile(
                &raw mut (*guest()).rip,
                (*v).guest_rip + profile::GUEST_OFFSETS[step + 1],
            );
            write_record(&raw mut (*t).prefixes[step], *frame);
            ptr::write_volatile(&raw mut (*t).completions[step], 1);
            ptr::write_volatile(&raw mut (*v).completed, step as u64 + 1);
            if step == 4 {
                ptr::write_volatile(&raw mut (*v).stage, 2);
                stop(SUCCESS, 0)
            }
            // Continue from the captured roots, never from the original seed.
            write_record(&raw mut (*s).initial, *frame);
            enter_step()
        }
        let (esr, far, pc, spsr) = ((*s).esr, (*s).far, (*s).elr, (*s).spsr);
        let write = step < 3;
        if esr >> 26 != 0x25
            || (esr >> 6) & 1 != u64::from(write)
            || !matches!(esr&63,4..=7|12..=15)
            || far != (*v).operand
            || pc != CODE_VA + (step * profile::SLOT_BYTES) as u64 + profile::SITES[step]
            || spsr & 15 != 4
            || frame.x[8] != initial.x[8]
            || frame.x[4] != initial.x[4]
            || frame.x[6] != initial.x[6]
        {
            stop(NATIVE_FAULT, 117)
        }
        let count = (*r).faults as usize;
        if count >= MAX_FAULTS {
            stop(REJECTED, 118)
        }
        let result = recovery::walk(far, if write { Access::Write } else { Access::Read });
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
            Ok(route) => {
                recovery::install(far, route);
                ptr::write_volatile(&raw mut (*r).retries, (*r).retries + 1);
                resume_exception(&raw const (*s).captured, pc, spsr)
            }
            Err(Fault::Page { address, error }) => exception_at(
                14,
                address,
                error,
                (*v).guest_rip + profile::GUEST_OFFSETS[step],
            ),
            _ => stop(REJECTED, 119),
        }
    }
}
