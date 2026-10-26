#![no_std]
#![no_main]

use core::{
    arch::{asm, global_asm},
    mem::{offset_of, size_of},
    ptr,
};
use natemu_abi::*;
mod concurrent;
mod coordinator;
mod controls;
mod helper;
mod paging_off32;
mod paging_off_steps;
mod partial;
mod pressure;
mod recovery;
mod stack;
mod translation;
mod validation;

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
);

unsafe extern "C" {
    fn enter(frame: *const NativeFrame) -> !;
    fn returned();
}

// Copy quiescent owner records without LLVM expanding aggregate volatile loads
// into thousands of byte instructions. MaybeUninit preserves structure padding.
#[inline(never)]
unsafe fn copy_record(dst: *mut u8, src: *const u8, count: usize) {
    for i in 0..count {
        unsafe {
            let b = ptr::read_volatile(src.add(i).cast::<core::mem::MaybeUninit<u8>>());
            ptr::write_volatile(dst.add(i).cast::<core::mem::MaybeUninit<u8>>(), b);
        }
    }
}
unsafe fn read_record<T: Copy>(src: *const T) -> T {
    let mut result = core::mem::MaybeUninit::<T>::uninit();
    unsafe {
        copy_record(result.as_mut_ptr().cast(), src.cast(), size_of::<T>());
        result.assume_init()
    }
}
unsafe fn write_record<T: Copy>(dst: *mut T, value: T) {
    unsafe { copy_record(dst.cast(), (&raw const value).cast(), size_of::<T>()) }
}
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
        if ptr::read_volatile(&raw const (*s).mode) == natemu_abi::concurrency::MODE {
            concurrent::initialize();
        }
        if ptr::read_volatile(&raw const (*s).mode) == natemu_abi::validation::MODE {
            validation::initialize();
        }
        if ptr::read_volatile(&raw const (*s).mode) >= natemu_abi::translation::MODE {
            translation::initialize();
        }
        let object = ptr::read_volatile(&raw const (*s).object);
        let mut initial = read_record(&raw const (*s).initial);
        if object.code_length > 48 {
            stop(REJECTED, 3);
        }
        let code = core::slice::from_raw_parts(CODE_VA as *const u8, object.code_length as usize);
        if let Err(reason) = object.validate(&initial, code) {
            stop(REJECTED, reason);
        }
        let capture_pc = returned as *const () as u64;
        initial.x[16] = CODE_VA;
        initial.x[30] = capture_pc;
        write_record(&raw mut (*s).initial, initial);
        ptr::write_volatile(&raw mut (*s).capture_pc, capture_pc);
        asm!("dsb ish", "ic iallu", "dsb ish", "isb");
        enter(&raw const (*s).initial);
    }
}

#[unsafe(no_mangle)]
unsafe extern "C" fn finish(frame: *const NativeFrame, fault: u64) -> ! {
    unsafe {
        let s = shared();
        copy_record(
            (&raw mut (*s).captured).cast(),
            frame.cast(),
            size_of::<NativeFrame>(),
        );
        if ptr::read_volatile(&raw const (*s).mode) == natemu_abi::concurrency::MODE {
            concurrent::completed(fault);
        }
        if fault != 0 {
            let esr: u64;
            let far: u64;
            let elr: u64;
            let spsr: u64;
            asm!("mrs {0}, ESR_EL1", "mrs {1}, FAR_EL1", "mrs {2}, ELR_EL1", "mrs {3}, SPSR_EL1",
                 out(reg) esr,out(reg) far,out(reg) elr,out(reg) spsr);
            ptr::write_volatile(&raw mut (*s).esr, esr);
            ptr::write_volatile(&raw mut (*s).far, far);
            ptr::write_volatile(&raw mut (*s).elr, elr);
            ptr::write_volatile(&raw mut (*s).spsr, spsr);
            if ptr::read_volatile(&raw const (*s).mode) == natemu_abi::validation::MODE {
                validation::finish(fault);
            }
            if ptr::read_volatile(&raw const (*s).mode) >= natemu_abi::recovery::MODE {
                if partial::owns(elr) {
                    partial::fault();
                }
                recovery::fault();
            }
            stop(NATIVE_FAULT, 0);
        }
        if ptr::read_volatile(&raw const (*s).mode) == natemu_abi::validation::MODE {
            validation::finish(fault);
        }
        if ptr::read_volatile(&raw const (*s).mode) >= natemu_abi::translation::MODE {
            if ptr::read_volatile(&raw const (*s).mode) >= natemu_abi::recovery::MODE {
                if pressure::enabled() {
                    pressure::completed()
                } else {
                    recovery::completed()
                };
            }
            translation::completed();
        }
        stop(SUCCESS, 0);
    }
}

#[panic_handler]
fn panic(_: &core::panic::PanicInfo) -> ! {
    stop(PANIC, 1)
}
