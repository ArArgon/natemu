//! Outlined SETZ/BFXIL helper: capture first, stage overlapping outputs, restore.
use super::*;
use natemu_abi::recovery::*;
unsafe extern "C" {
    fn helper_gate();
    fn resume_exception(f: *const NativeFrame, pc: u64, spsr: u64) -> !;
}
pub fn enabled() -> bool {
    unsafe { ptr::read_volatile(&raw const (*shared()).mode) == HELPER_MODE }
}
pub unsafe fn patch(body: &mut [u8]) {
    unsafe {
        let delta = (helper_gate as *const () as i64) - (CODE_VA as i64 + 156);
        if delta % 4 != 0 || !(-(1 << 27)..(1 << 27)).contains(&delta) {
            stop(REJECTED, 70)
        }
        body[156..160]
            .copy_from_slice(&(0x14000000u32 | ((delta / 4) as u32 & 0x03ffffff)).to_le_bytes());
        body[160..164].copy_from_slice(&NOP);
        write_record(HELPER_VA as *mut HelperState, HelperState::ZERO);
    }
}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn helper_prepare(frame: *const NativeFrame) {
    unsafe {
        let f = read_record(frame);
        let mut h = read_record(HELPER_VA as *const HelperState);
        if h.calls != 0 || f.sp != STACK_TOP - 768 {
            stop(PANIC, 71)
        }
        h.before = f;
        // Compute using immutable inputs; publish only after the complete frame copy.
        let zero = (f.nzcv >> 30) & 1;
        let dl = (f.x[5] & !255) | zero;
        h.staged = f;
        h.staged.x[20] = zero;
        h.staged.x[5] = dl;
        h.calls = 1;
        h.outputs = 2;
        write_record(HELPER_VA as *mut HelperState, h);
    }
}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn helper_restore(poisoned: *const NativeFrame) -> ! {
    unsafe {
        let mut h = read_record(HELPER_VA as *const HelperState);
        h.poisoned = read_record(poisoned);
        write_record(HELPER_VA as *mut HelperState, h);
        resume_exception(
            &raw const (*(HELPER_VA as *const HelperState)).staged,
            CODE_VA + 164,
            0x3c4 | h.staged.nzcv,
        );
    }
}
