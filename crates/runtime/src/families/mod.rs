//! Regression families (`families` feature): the scripted I1–I4/I4-V resident
//! scenarios the regression runners select through `Shared::mode`. The general
//! resident hands every mode other than block execution to `start`/`finish`.
use super::*;
use natemu_abi::guest_memory::Observer;
unsafe extern "C" {
    fn enter(frame: *const NativeFrame) -> !;
    fn returned();
}
mod atomic_progress;
mod coherence;
mod concurrent;
mod containment;
mod controls;
mod fence;
mod helper;
mod ordering;
mod paging_off32;
mod paging_off_steps;
mod partial;
mod partial_progress;
mod pressure;
mod recovery;
mod schedule;
mod stack;
mod table_log;
mod translation;
mod validation;

fn mode() -> u64 {
    unsafe { ptr::read_volatile(&raw const (*shared()).mode) }
}

/// Owner start for every family mode; mode 0 is the I1 fixed-body entry.
pub unsafe fn start() -> ! {
    unsafe {
        let s = shared();
        if mode() == natemu_abi::families::partial_progress::MODE {
            partial_progress::initialize();
        }
        if mode() == natemu_abi::families::coherence::MODE {
            coherence::initialize();
        }
        if mode() == natemu_abi::families::fence::MODE {
            fence::initialize();
        }
        if mode() == natemu_abi::families::ordering::MODE {
            ordering::initialize();
        }
        if mode() == natemu_abi::concurrency::MODE {
            concurrent::initialize();
        }
        if mode() == natemu_abi::families::validation::MODE {
            validation::initialize();
        }
        if mode() >= natemu_abi::translation::MODE {
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

/// Capture/return for every family mode; `Shared::captured` already holds the frame.
pub unsafe fn finish(fault: u64) -> ! {
    unsafe {
        let s = shared();
        if mode() == natemu_abi::families::partial_progress::MODE {
            partial_progress::finish(fault);
        }
        if mode() == natemu_abi::families::coherence::MODE {
            coherence::finish(fault);
        }
        if mode() == natemu_abi::families::fence::MODE {
            fence::finish(fault);
        }
        if mode() == natemu_abi::families::ordering::MODE {
            ordering::finish(fault);
        }
        if mode() == natemu_abi::concurrency::MODE {
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
            if mode() == natemu_abi::families::validation::MODE {
                validation::finish(fault);
            }
            if mode() >= natemu_abi::recovery::MODE {
                if partial::owns(elr) {
                    partial::fault();
                }
                recovery::fault();
            }
            stop(NATIVE_FAULT, 0);
        }
        if mode() == natemu_abi::families::validation::MODE {
            validation::finish(fault);
        }
        if mode() >= natemu_abi::translation::MODE {
            if mode() >= natemu_abi::recovery::MODE {
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
