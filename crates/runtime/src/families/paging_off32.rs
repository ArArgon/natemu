//! Native execution with guest paging disabled; only the admitted first store can fault.
use super::validation::{guest, recovery_state, state};
use super::*;
use natemu_abi::{paging::Access, families::paging_off32 as p, recovery::FaultTrace};

unsafe extern "C" {
    fn service(packet: *mut natemu_abi::families::validation::Packet);
    fn service_resume();
    fn resume_exception(frame: *const NativeFrame, pc: u64, spsr: u64) -> !;
}
fn record() -> *mut p::State {
    (PRIVATE + p::STATE_OFFSET as u64) as *mut p::State
}
unsafe fn physical(address: u64, bytes: u64, access: Access) -> u64 {
    unsafe {
        p::physical(&*guest(), (*record()).a20, address, bytes, access)
            .unwrap_or_else(|| stop(REJECTED, 201))
    }
}
unsafe fn source_matches(rip: u64) -> bool {
    unsafe {
        let gpa = physical(rip, p::SOURCE.len() as u64, Access::Fetch);
        (*record()).fetch_gpa = gpa;
        core::slice::from_raw_parts(
            (PRIVATE + natemu_abi::recovery::SOURCE_OFFSET as u64 + gpa
                - natemu_abi::translation::SOURCE_VA) as *const u8,
            p::SOURCE.len(),
        ) == p::SOURCE
    }
}

#[inline(never)]
pub unsafe fn initialize() -> ! {
    unsafe {
        let s = shared();
        let v = state();
        let r = record();
        let rip = (*v).guest_rip;
        if (*v).case != p::CASE
            || (*v).stage != 0
            || (*v).completed != 0
            || (*v).live != 1
            || (*v).requests != 0
            || (*v).request != [0; 16]
            || (*v).reply != [0; 16]
            || (*s).lifecycle.vm == 0
            || (*s).tcr != shadow::TCR_BOTH
            || (*guest()).rip != rip
            || (*r).scenario > 2
            || (*r).transfers != 0
            || (*r).requests != [[0; 16]; 3]
            || (*r).replies != [[0; 16]; 3]
            || !p::admitted(&*guest(), (*r).a20)
            || !matches!(rip, natemu_abi::translation::SOURCE_VA | 0x102080)
            || (*s).initial.sp != STACK_TOP
            || (*s).initial.x[28] != BANK_VA
            || (*s).initial.reserved != 0
            || (*recovery_state()).walks != 0
            || (*recovery_state()).faults != 0
            || (*recovery_state()).retries != 0
            || core::slice::from_raw_parts(CODE_VA as *const u8, p::CODE_BYTES)
                != [0; p::CODE_BYTES]
        {
            stop(REJECTED, 200);
        }
        if !source_matches(rip) {
            stop(REJECTED, 202);
        }
        // This exact body has three accesses to this one owned flat RAM span.
        // Wider spans and segment profiles reject before any guest execution.
        physical(DATA_VA, 4, Access::Write);
        physical(DATA_VA, 4, Access::Read);
        let mut code = [0u8; p::CODE_BYTES];
        (*v).requests = 1;
        (*s).service_pc = service_resume as *const () as u64;
        for chunk in 0..3 {
            let mut packet = p::request((*s).lifecycle.vm, rip, chunk);
            let expected = p::reply(&packet).unwrap_or_else(|| stop(REJECTED, 203));
            write_record(&raw mut (*r).requests[chunk as usize], packet);
            write_record(&raw mut (*v).request, packet);
            service(&mut packet);
            write_record(&raw mut (*r).replies[chunk as usize], packet);
            write_record(&raw mut (*v).reply, packet);
            (*r).transfers += 1;
            if packet != expected || !source_matches(rip) {
                stop(REJECTED, 204);
            }
            let offset = chunk as usize * 24;
            for i in 0..packet[13] as usize {
                code[offset + i] = packet[9 + i / 8].to_le_bytes()[i % 8];
            }
        }
        code[p::BODY.len()..].copy_from_slice(&RET);
        translation::replace_bytes(&code);
        (*s).initial.x[16] = CODE_VA;
        (*s).initial.x[30] = returned as *const () as u64;
        (*s).capture_pc = (*s).initial.x[30];
        (*v).stage = 1;
        enter(&raw const (*s).initial)
    }
}

// Keep full guest/native snapshots out of the common completion dispatch frame.
#[inline(never)]
pub unsafe fn finish(fault: u64) -> ! {
    unsafe {
        let s = shared();
        let v = state();
        let r = record();
        let recovery = recovery_state();
        let g = guest();
        let f = &(*s).captured;
        if (*v).stage != 1 || (*v).completed != 0 || !p::admitted(&*g, (*r).a20) {
            stop(REJECTED, 205);
        }
        if fault == 0 {
            (*g).gpr[0] = f.x[4] as u32 as u64;
            (*g).gpr[1] = f.x[7] as u32 as u64;
            (*g).gpr[2] = f.x[5] as u32 as u64;
            (*g).rip = (*v).guest_rip + p::SOURCE.len() as u64;
            (*v).completed = 9;
            (*v).stage = 2;
            stop(SUCCESS, 0);
        }
        let (esr, far, pc, spsr) = ((*s).esr, (*s).far, (*s).elr, (*s).spsr);
        if esr >> 26 != 0x25
            || esr & (1 << 6) == 0
            || !matches!(esr & 63,4..=7 | 12..=15)
            || far != DATA_VA
            || pc != CODE_VA + 16
            || spsr & 15 != 4
            || f.sp != STACK_TOP
            || f.x[4] != 0x42
            || f.x[5] != DATA_VA
            || f.x[20] != DATA_VA
            || (*recovery).faults != 0
        {
            stop(NATIVE_FAULT, 206);
        }
        (*g).gpr[0] = f.x[4] as u32 as u64;
        (*g).gpr[2] = f.x[5] as u32 as u64;
        (*g).rip = (*v).guest_rip + 10;
        copy_record(
            (&raw mut (*r).prefix).cast(),
            g.cast(),
            size_of::<natemu_abi::recovery::GuestState>(),
        );
        let gpa = physical(far, 4, Access::Write);
        (*r).repair_gpa = gpa;
        copy_record(
            (&raw mut (*r).data_before_retry).cast(),
            (PRIVATE + DATA_OFFSET as u64) as *const u8,
            16,
        );
        write_record(
            &raw mut (*recovery).trace[0],
            FaultTrace {
                frame: *f,
                esr,
                far,
                elr: pc,
                spsr,
                outcome: 1,
                error: 0,
            },
        );
        (*recovery).faults = 1;
        recovery::install_ram(far, gpa, true);
        (*recovery).retries = 1;
        resume_exception(&raw const (*s).captured, pc, spsr)
    }
}
