//! Each generated guest instruction completes before the next segment/fetch guard.
use super::validation::{exception_at, guest, recovery_state, state};
use super::*;
use natemu_abi::{paging::Access, families::paging_off_steps as p};
unsafe extern "C" {
    fn service(packet: *mut natemu_abi::families::validation::Packet);
    fn service_resume();
    fn resume_exception(frame: *const NativeFrame, pc: u64, spsr: u64) -> !;
}
fn record() -> *mut p::State {
    (PRIVATE + p::STATE_OFFSET as u64) as *mut p::State
}
unsafe fn address(segment: usize, offset: u64, bytes: u64, access: Access) -> p::Address {
    unsafe {
        match p::address(
            (*guest()).segments[segment],
            (*record()).a20,
            offset,
            bytes,
            access,
        ) {
            Ok(a) => a,
            Err(p::AddressError::GeneralProtection) => exception_at(13, 0, 0, (*guest()).rip),
            Err(p::AddressError::Unsupported) => stop(REJECTED, 221),
        }
    }
}
fn ram(gpa: u64, bytes: u64) -> usize {
    match p::route(gpa, bytes) {
        Some(p::Route::Ram(offset)) => offset,
        _ => stop(REJECTED, 222),
    }
}
unsafe fn source_matches(step: usize, gpa: u64) -> bool {
    unsafe {
        let expected = p::source(step).unwrap_or_else(|| stop(PANIC, 223));
        let offset = ram(gpa, expected.len() as u64);
        core::slice::from_raw_parts((PRIVATE + offset as u64) as *const u8, expected.len())
            == expected
    }
}
#[inline(never)]
unsafe fn begin_step() -> ! {
    unsafe {
        let s = shared();
        let v = state();
        let r = record();
        let g = guest();
        let step = (*v).completed as usize;
        (*v).operand = 0;
        (*v).width = 0;
        (*v).stage = 0;
        (*r).step = step as u64;
        if step >= 9 || (*r).entries[step] != 0 || (*g).rip != p::EIP + p::OFFSETS[step] as u64 {
            stop(REJECTED, 220);
        }
        let source = p::source(step).unwrap();
        let fetch = address(p::CS, (*g).rip, source.len() as u64, Access::Fetch);
        (*r).fetch_linear[step] = fetch.linear;
        (*r).fetch_gpa[step] = fetch.gpa;
        if !source_matches(step, fetch.gpa) {
            stop(REJECTED, 223);
        }
        if matches!(step, 2 | 4 | 7) {
            let width = if step == 7 { 2 } else { 4 };
            let offset = if step == 2 {
                (*s).initial.x[5] as u32 as u64
            } else {
                DATA_VA
            };
            (*v).operand = offset;
            (*v).width = width;
            let a = address(
                p::DS,
                offset,
                width,
                if step == 2 {
                    Access::Write
                } else {
                    Access::Read
                },
            );
            if p::route(a.gpa, width).is_none() {
                stop(REJECTED, 222);
            }
            (*r).data_linear[step] = a.linear;
            (*r).data_gpa[step] = a.gpa;
            // Each due access exercises native miss classification independently.
            recovery::clear_mapping(offset);
        }
        let mut packet = p::request((*s).lifecycle.vm, (*r).profile, step)
            .unwrap_or_else(|| stop(REJECTED, 223));
        let expected = p::reply(&packet).unwrap_or_else(|| stop(REJECTED, 223));
        write_record(&raw mut (*r).requests[step], packet);
        write_record(&raw mut (*v).request, packet);
        (*v).requests += 1;
        (*s).service_pc = service_resume as *const () as u64;
        service(&mut packet);
        write_record(&raw mut (*r).replies[step], packet);
        write_record(&raw mut (*v).reply, packet);
        (*r).transfers += 1;
        if packet != expected || !source_matches(step, fetch.gpa) {
            stop(REJECTED, 224);
        }
        let mut image = read_record(CODE_VA as *const [u8; p::CODE_BYTES]);
        let start = step * p::SLOT_BYTES;
        if image[start..start + p::SLOT_BYTES] != [0; p::SLOT_BYTES] {
            stop(REJECTED, 225);
        }
        let mut body = [0; 16];
        body[..8].copy_from_slice(&packet[9].to_le_bytes());
        body[8..].copy_from_slice(&packet[10].to_le_bytes());
        let count = p::LENGTHS[step];
        image[start..start + count].copy_from_slice(&body[..count]);
        image[start + count..start + count + 4].copy_from_slice(&RET);
        // Every earlier instruction returned; no live continuation enters its slot.
        translation::replace_bytes(&image);
        (*s).initial.x[16] = CODE_VA + start as u64;
        (*s).initial.x[30] = returned as *const () as u64;
        (*s).capture_pc = (*s).initial.x[30];
        (*r).entries[step] = 1;
        (*v).stage = 1;
        enter(&raw const (*s).initial)
    }
}
#[inline(never)]
pub unsafe fn initialize() -> ! {
    unsafe {
        let s = shared();
        let v = state();
        let r = record();
        let g = guest();
        if (*v).case != p::CASE
            || (*v).stage != 0
            || (*v).completed != 0
            || (*v).requests != 0
            || (*v).live != 1
            || (*v).guest_rip != p::EIP
            || (*g).rip != p::EIP
            || !p::admitted(&*g, (*r).profile, (*r).a20)
            || (*s).lifecycle.vm == 0
            || (*s).tcr != shadow::TCR_BOTH
            || (*s).initial.sp != STACK_TOP
            || (*s).initial.x[28] != BANK_VA
            || (*s).initial.reserved != 0
            || (*r).transfers != 0
            || (*r).entries != [0; 9]
            || (*r).requests != [[0; 16]; 9]
            || (*r).replies != [[0; 16]; 9]
            || (*r).service_count != 0
            || (*r).device_value != 0x12345678
            || (*recovery_state()).faults != 0
            || (*recovery_state()).retries != 0
            || (*recovery_state()).walks != 0
            || core::slice::from_raw_parts(CODE_VA as *const u8, p::CODE_BYTES)
                != [0; p::CODE_BYTES]
        {
            stop(REJECTED, 220);
        }
        begin_step()
    }
}
#[inline(never)]
unsafe fn materialize(frame: *const NativeFrame, next: usize) {
    unsafe {
        let g = guest();
        (*g).gpr[0] = (*frame).x[4] as u32 as u64;
        (*g).gpr[1] = (*frame).x[7] as u32 as u64;
        (*g).gpr[2] = (*frame).x[5] as u32 as u64;
        (*g).rip = p::EIP + p::OFFSETS[next] as u64;
    }
}
#[inline(never)]
pub unsafe fn finish(fault: u64) -> ! {
    unsafe {
        let s = shared();
        let v = state();
        let r = record();
        let g = guest();
        let recovery = recovery_state();
        let step = (*r).step as usize;
        if step >= 9
            || (*v).stage != 1
            || (*r).entries[step] != 1
            || (*v).completed as usize != step
            || !p::admitted(&*g, (*r).profile, (*r).a20)
        {
            stop(REJECTED, 226);
        }
        if fault == 0 {
            copy_frame(&raw mut (*r).prefixes[step], &raw const (*s).captured);
            materialize(&raw const (*s).captured, step + 1);
            (*r).guest_prefixes[step] = [(*g).gpr[0], (*g).gpr[1], (*g).gpr[2], (*g).rip];
            (*v).completed += 1;
            if step == 8 {
                (*v).stage = 2;
                stop(SUCCESS, 0);
            }
            copy_frame(&raw mut (*s).initial, &raw const (*s).captured);
            begin_step()
        }
        let (esr, far, pc, spsr) = ((*s).esr, (*s).far, (*s).elr, (*s).spsr);
        let count = (*recovery).faults as usize;
        if !matches!(step, 2 | 4 | 7)
            || count >= 3
            || count
                != match step {
                    2 => 0,
                    4 => 1,
                    _ => 2,
                }
            || esr >> 26 != 0x25
            || (esr >> 6) & 1 != u64::from(step == 2)
            || !matches!(esr & 63, 4..=7)
            || far != DATA_VA
            || pc != CODE_VA + (step * p::SLOT_BYTES) as u64 + 4
            || spsr & 15 != 4
            || (*s).captured.sp != STACK_TOP
            || (*g).rip != p::EIP + p::OFFSETS[step] as u64
        {
            stop(NATIVE_FAULT, 227);
        }
        let width = if step == 7 { 2 } else { 4 };
        let a = address(
            p::DS,
            far,
            width,
            if step == 2 {
                Access::Write
            } else {
                Access::Read
            },
        );
        if a.linear != (*r).data_linear[step] || a.gpa != (*r).data_gpa[step] {
            stop(PANIC, 228);
        }
        let trace = &raw mut (*recovery).trace[count];
        copy_frame(&raw mut (*trace).frame, &raw const (*s).captured);
        (*trace).esr = esr;
        (*trace).far = far;
        (*trace).elr = pc;
        (*trace).spsr = spsr;
        (*trace).error = 0;
        (*r).fault_steps[count] = step as u64;
        (*recovery).faults += 1;
        match p::route(a.gpa, width).unwrap_or_else(|| stop(REJECTED, 222)) {
            p::Route::Ram(offset) => {
                copy_record(
                    (&raw mut (*r).data_before_retry[count]).cast(),
                    (PRIVATE + (offset & !4095) as u64) as *const u8,
                    16,
                );
                (*trace).outcome = 1;
                // begin_step invalidated this leaf; ownership and low-bit alignment were checked above.
                tables::space()
                    .map(far & !4095, IPA + (offset & !4095) as u64, true)
                    .unwrap_or_else(|_| stop(REJECTED, 222));
                (*recovery).retries += 1;
                resume_exception(&raw const (*s).captured, pc, spsr)
            }
            route => {
                let index = (*r).service_count as usize;
                if index != count || index >= 3 {
                    stop(PANIC, 229);
                }
                let mask = if width == 4 { 0xffff_ffff } else { 0xffff };
                let value = if step == 2 {
                    (*s).captured.x[4] & mask
                } else if route == p::Route::Device {
                    (*r).device_value & mask
                } else {
                    mask
                };
                if step == 2 {
                    if route == p::Route::Device {
                        (*r).device_value = value;
                    }
                } else {
                    (*s).captured.x[if step == 7 { 20 } else { 4 }] = value;
                }
                (*r).services[index] = [
                    step as u64,
                    u64::from(step == 2),
                    width,
                    a.linear,
                    a.gpa,
                    value,
                ];
                (*r).service_count += 1;
                (*trace).outcome = 4;
                // The bounded service owns precisely this memory subaccess; generated merge/continuation still runs.
                resume_exception(&raw const (*s).captured, pc + 4, spsr)
            }
        }
    }
}
