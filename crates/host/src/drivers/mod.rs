//! Test drivers: the host CLI cases, their programs, seeds, mutations and
//! expected results. Each driver builds its fixture and calls the emulator
//! mechanisms (vm, memory, owners, execution, translation) through their
//! ordinary operations; no mechanism module knows these case names.
use crate::*;

mod concurrent;
mod i4v_controls;
mod i4v_off;
mod i4v_off_steps;
mod program;
mod recovery;

/// Dispatches the host CLI (`natemu-host RUNTIME.elf CASE ...`) to its driver.
pub(crate) fn run(a: &[String]) -> Result<()> {
    if a.len() >= 6 && a[2] == "run-program" {
        return program::run(a);
    }
    if a.len() == 9 && a[2] == "i4v-containment" {
        return concurrent::containment::run(a);
    }
    if a.len() == 9 && a[2] == "i4v-progress" {
        return concurrent::run_progress(a);
    }
    if a.len() == 9 && a[2] == "i4v-coherence" {
        return concurrent::run_coherence(a);
    }
    if a.len() == 8 && a[2] == "i4v-fence" {
        return concurrent::run_fence(a);
    }
    if a.len() == 10 && a[2] == "i4v-ordering" {
        return concurrent::run_ordering(a);
    }
    if a.len() == 9 && a[2] == "i4v-address-concurrent" {
        return concurrent::run_address(a);
    }
    if a.len() == 9 && a[2] == "i4v-stack-boundary" {
        return concurrent::run(a);
    }
    if a.len() == 9 && a[2] == "i4v-controls-concurrent" {
        return concurrent::run_controls(a);
    }
    if a.len() == 8 && a[2] == "i4v-controls" {
        return i4v_controls::run(a);
    }
    if (a.len() == 8 || a.len() == 9) && a[2] == "i4v-off-steps" {
        return i4v_off_steps::run(a);
    }
    if (a.len() == 9 || a.len() == 10) && a[2] == "i4v-off-live" {
        return i4v_off::run(a);
    }
    if a.len() == 7 && (a[2] == "i4" || a[2] == "i4v-mixed") {
        return concurrent::run(a);
    }
    Err("usage: natemu-host RUNTIME.elf run-program FEXC PROGRAM.txt WORKDIR [OPTIONS] | i4 ... | i4v-* ...".into())
}

/// Seed input vector `index` (0..4): native entry frame, register bank and data page.
fn seed(index: usize) -> (NativeFrame, [u8; 512], [u8; 256]) {
    let (left, right) = [(0, 0), (u64::MAX, 1), (i64::MAX as u64, 1), (1, 2)][index];
    let mut bank = [0; 512];
    for (i, b) in bank.iter_mut().enumerate() {
        *b = (i.wrapping_mul(37) + 19) as u8;
    }
    let mut data = [0; 256];
    for (i, b) in data.iter_mut().enumerate() {
        *b = (i.wrapping_mul(13) + 7) as u8;
    }
    data[..8].copy_from_slice(&(0xcafe_1234_5678_9000u64 + index as u64).to_le_bytes());
    let mut frame = NativeFrame::ZERO;
    let initial = &mut frame;
    for (i, x) in initial.x.iter_mut().enumerate() {
        *x = 0x1234_0000_0000_0000 + (i as u64) * 0x0101_0101;
    }
    for (i, q) in initial.q.iter_mut().enumerate() {
        for (j, b) in q.iter_mut().enumerate() {
            *b = (i * 17 + j * 3 + 1) as u8;
        }
    }
    initial.sp = STACK_TOP;
    initial.x[28] = BANK_VA;
    initial.x[6] = DATA_VA;
    initial.x[11] = DATA_VA + 128;
    initial.x[7] = left;
    initial.x[5] = right;
    initial.nzcv = 0xa0000000;
    initial.fpcr = 0x01000000;
    initial.fpsr = 0x08000001;
    (frame, bank, data)
}

/// The upper shadow half's pre-linked root, L1 and L2 table words.
pub(super) fn upper_links(memory: &Memory) -> [u64; 3] {
    let word = |offset: usize| u64::from_le_bytes(memory.read::<8>(offset));
    [word(natemu_abi::shadow::UPPER_ROOT + 256 * 8), word(natemu_abi::shadow::UPPER_L1), word(natemu_abi::shadow::UPPER_L2)]
}
