#![no_std]
#![no_main]

use core::{
    arch::{asm, global_asm},
    mem::{offset_of, size_of},
    ptr,
};
use natemu_abi::*;
use natemu_abi::guest_memory::{GuestRam, GuestTables};
use record::*;
mod cache;
mod execution;
mod record;
mod tables;

global_asm!(include_str!("gates.S"),
    frame_size=const size_of::<NativeFrame>(),
    sp_offset=const offset_of!(NativeFrame,sp),
    q_offset=const offset_of!(NativeFrame,q),
    nzcv_offset=const offset_of!(NativeFrame,nzcv),
    fpcr_offset=const offset_of!(NativeFrame,fpcr),
    fpsr_offset=const offset_of!(NativeFrame,fpsr),
    reserved_offset=const offset_of!(NativeFrame,reserved),
    shared_address=const PRIVATE+SHARED_OFFSET as u64,
    stack_top=const STACK_TOP,
    exception_stack_top=const EXCEPTION_STACK_TOP,
    identity_entry=const PRIVATE+0x11008,
    upper_root_pages=const (shadow::UPPER_ROOT-shadow::LOWER_ROOT)/4096,
    l1_code_delta=const natemu_abi::execution::cache::L1_CODE-natemu_abi::execution::cache::DISPATCH-4,
    svc_miss=const natemu_abi::execution::svc::MISS,
);

fn shared() -> *mut Shared {
    (PRIVATE + SHARED_OFFSET as u64) as *mut Shared
}

fn stop(status: u64, detail: u64) -> ! {
    unsafe {
        ptr::write_volatile(&raw mut (*shared()).detail, detail);
        ptr::write_volatile(&raw mut (*shared()).status, status);
        asm!("dsb sy", "hvc #0x49", "b .", options(noreturn));
    }
}

#[unsafe(no_mangle)]
unsafe extern "C" fn runtime_main() -> ! {
    // The host is stopped while this owner executes; exchanged RAM stays raw.
    unsafe {
        let s = shared();
        let mut current_el: u64;
        let mut sctlr: u64;
        let mut actlr: u64;
        let mut tcr: u64;
        asm!("mrs {0}, CurrentEL", "mrs {1}, SCTLR_EL1", "mrs {2}, ACTLR_EL1", "mrs {3}, TCR_EL1",
             out(reg) current_el,out(reg) sctlr,out(reg) actlr,out(reg) tcr);
        ptr::write_volatile(&raw mut (*s).current_el, current_el);
        ptr::write_volatile(&raw mut (*s).sctlr, sctlr);
        ptr::write_volatile(&raw mut (*s).actlr, actlr);
        ptr::write_volatile(&raw mut (*s).tcr, tcr);
        if current_el != 4 || sctlr & 0x1005 != 0x1005 || actlr & 2 != 2 {
            stop(PANIC, 10);
        }
        if ptr::read_volatile(&raw const (*s).mode) == natemu_abi::execution::MODE {
            execution::start();
        }
        stop(REJECTED, 11);
    }
}

#[unsafe(no_mangle)]
unsafe extern "C" fn finish(frame: *const NativeFrame, fault: u64) -> ! {
    unsafe {
        let s = shared();
        copy_frame(&raw mut (*s).captured, frame);
        if ptr::read_volatile(&raw const (*s).mode) == natemu_abi::execution::MODE {
            execution::returned(&*frame, fault);
        }
        stop(PANIC, 11);
    }
}

#[panic_handler]
fn panic(_: &core::panic::PanicInfo) -> ! {
    stop(PANIC, 1)
}
