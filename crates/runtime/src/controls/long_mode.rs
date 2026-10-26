//! IA-32e fixture transactions reuse the control dispatcher and memory authority.
use super::*;
use natemu_abi::{
    modes as m,
    paging::{Tables as TableAccess, Translation},
};

pub(super) fn state() -> *mut m::State {
    (PRIVATE + m::OFFSET as u64) as *mut m::State
}
unsafe fn visit() -> *mut m::Visit {
    unsafe {
        (&raw mut (*state()).visits)
            .cast::<m::Visit>()
            .add((*super::state()).visits as usize - 1)
    }
}
struct Tables;
impl TableAccess for Tables {
    fn read(&mut self, gpa: u64) -> Option<u64> {
        if !(0x6000..0xa000).contains(&gpa) || gpa & 7 != 0 {
            return None;
        }
        Some(unsafe {
            ptr::read_volatile((PRIVATE + m::TABLE_OFFSET as u64 + gpa - 0x6000) as *const u64)
        })
    }
    fn write(&mut self, gpa: u64, value: u64) -> bool {
        if !(0x6000..0xa000).contains(&gpa) || gpa & 7 != 0 {
            return false;
        }
        unsafe {
            ptr::write_volatile(
                (PRIVATE + m::TABLE_OFFSET as u64 + gpa - 0x6000) as *mut u64,
                value,
            );
            asm!("dsb ish");
        }
        true
    }
}
pub(super) fn walk(control: Control, address: u64, access: Access) -> Result<Translation, Fault> {
    natemu_abi::paging::walk(&mut Tables, control, address, access)
}
pub(super) unsafe fn initial_valid(profile: u64) -> bool {
    unsafe {
        let p = state();
        let start = if profile >= 22 {
            if (*p).gdtr != [DATA_VA + 0x200, 0x37]
                || (*p).tr != [0x28, 0, 0x67, if profile == 36 { 0x83 } else { 0x8b }]
                || (*p).pointer != DATA_VA + 0x100
                || (*guest()).gpr[1] != 0xc0000080
                || (*guest()).gpr[2] != 0
                || (*guest()).gpr[6] != (*p).pointer
            {
                return false;
            }
            7
        } else {
            0
        };
        (start..size_of::<m::State>() / 8).all(|i| ptr::read_volatile(p.cast::<u64>().add(i)) == 0)
    }
}
pub(super) unsafe fn begin() {
    unsafe {
        (*visit()).cs_before = (*guest()).segments[0];
        (*visit()).mode_before = c::mode(&*guest()).unwrap_or_else(|| stop(REJECTED, 300));
    }
}
pub(super) unsafe fn summarize() {
    unsafe {
        (*visit()).cs_after = (*guest()).segments[0];
        (*visit()).mode_after = c::mode(&*guest()).unwrap_or_else(|| stop(REJECTED, 301));
    }
}
unsafe fn transfer_error(error: m::TransferError) -> ! {
    match error {
        m::TransferError::Fault { vector, error } => unsafe { exception(vector, 0, error) },
        m::TransferError::Unsupported => stop(REJECTED, 302),
    }
}
unsafe fn read_guest(address: u64, bytes: usize) -> [u8; 16] {
    unsafe {
        let (gpa, _) = route_or_fault(address, bytes as u64, Access::Read);
        let mut value = [0; 16];
        copy_record(
            value.as_mut_ptr(),
            (PRIVATE + DATA_OFFSET as u64 + gpa - DATA_VA) as *const u8,
            bytes,
        );
        (*state()).reads += 1;
        value
    }
}
pub(super) unsafe fn instruction(source: &[u8]) -> bool {
    unsafe {
        if (*super::state()).profile < 22 {
            return false;
        }
        let g = guest();
        if source == [0x0f, 0x30] {
            (*super::visit()).kind = 4;
            let next = match m::write_efer(&*g, (*super::state()).cpl) {
                Ok(next) => next,
                Err(c::Error::GeneralProtection) => exception(13, 0, 0),
                _ => stop(REJECTED, 303),
            };
            let generation = (*super::state())
                .generation
                .checked_add(1)
                .unwrap_or_else(|| stop(REJECTED, 304));
            (*g).control = next;
            (*super::state()).generation = generation;
            invalidate();
            (*g).rip += 2;
            complete_visit();
            return true;
        }
        if source.first() != Some(&0xea) && source != [0x48, 0xff, 0x2e] {
            return false;
        }
        (*super::visit()).kind = 5;
        let mode = c::mode(&*g).unwrap_or_else(|| stop(REJECTED, 305));
        let (target, selector) = if source[0] == 0xea {
            if mode == 4 {
                exception(6, 0, 0);
            }
            if mode != 3 || source.len() != 7 {
                stop(REJECTED, 306);
            }
            (
                u32::from_le_bytes(source[1..5].try_into().unwrap()) as u64,
                u16::from_le_bytes(source[5..7].try_into().unwrap()) as u64,
            )
        } else {
            if mode != 4 {
                stop(REJECTED, 307);
            }
            let address = (*g).gpr[6];
            (*visit()).pointer_address = address;
            let pointer = read_guest(address, 10);
            (*visit()).pointer_bytes = pointer;
            (*state()).pointer_reads += 1;
            (
                u64::from_le_bytes(pointer[..8].try_into().unwrap()),
                u16::from_le_bytes(pointer[8..10].try_into().unwrap()) as u64,
            )
        };
        let address = match m::descriptor_address(&(*state()).gdtr, selector) {
            Ok(address) => address,
            Err(error) => transfer_error(error),
        };
        (*visit()).descriptor_address = address;
        let bytes = read_guest(address, 8);
        let descriptor = u64::from_le_bytes(bytes[..8].try_into().unwrap());
        (*visit()).descriptor = descriptor;
        (*state()).descriptor_reads += 1;
        let cs = match m::code_segment(&*g, (*super::state()).cpl, selector, descriptor, target) {
            Ok(cs) => cs,
            Err(error) => transfer_error(error),
        };
        let generation = (*super::state())
            .generation
            .checked_add(1)
            .unwrap_or_else(|| stop(REJECTED, 308));
        (*g).segments[0] = cs;
        (*g).rip = target;
        (*super::state()).generation = generation;
        invalidate();
        if (*super::state()).profile == 35 {
            ptr::write_volatile(
                (PRIVATE + m::TABLE_OFFSET as u64 + 0x3000 + 258 * 8) as *mut u64,
                0,
            );
            asm!("dsb ish");
            (*state()).mutations += 1;
        }
        complete_visit();
        true
    }
}
pub(super) unsafe fn after_native(opcode: u8) {
    unsafe {
        if (*super::state()).profile == 34 && opcode == 0x48 {
            ptr::write_volatile(
                (PRIVATE + m::TABLE_OFFSET as u64 + 0x3000 + 256 * 8) as *mut u64,
                0,
            );
            asm!("dsb ish");
            (*state()).mutations += 1;
        }
    }
}
