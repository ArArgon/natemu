//! Volatile copies of quiescent owner records and native frames.
use core::mem::{MaybeUninit, size_of};
use natemu_abi::NativeFrame;

/// A volatile copy of a quiescent record (no other writer while it runs): 32 bytes at a time (two `ldp`/`stp` pairs),
/// then 8, when both ends and the size are 8-byte multiples (the state record and frames are; a byte copy of them made
/// every resident entry cost about 1 us, an 8-byte one about 0.1 us), else bytewise. The `asm!` is opaque to the compiler
/// (it may touch any memory): as volatile accesses, the copy is neither merged nor reordered with other accesses.
pub(crate) unsafe fn copy_record(dst: *mut u8, src: *const u8, count: usize) {
    if (dst as usize | src as usize | count) % 8 != 0 {
        return unsafe { natemu_abi::guest_memory::copy_volatile(dst, src, count) };
    }
    let mut done = 0;
    while count - done >= 32 {
        // SAFETY: both ranges lie in the record (the caller's contract); the resident runs with SCTLR_EL1.A clear.
        unsafe {
            core::arch::asm!("ldp {a}, {b}, [{s}]", "ldp {c}, {d}, [{s}, #16]", "stp {a}, {b}, [{t}]", "stp {c}, {d}, [{t}, #16]",
                s = in(reg) src.add(done), t = in(reg) dst.add(done),
                a = out(reg) _, b = out(reg) _, c = out(reg) _, d = out(reg) _, options(nostack, preserves_flags));
        }
        done += 32;
    }
    // As MaybeUninit words: a record's padding bytes are copied without being read as integers.
    let (dst, src) = (dst.cast::<MaybeUninit<u64>>(), src.cast::<MaybeUninit<u64>>());
    for i in done / 8..count / 8 {
        unsafe { dst.add(i).write_volatile(src.add(i).read_volatile()) };
    }
}

pub(crate) unsafe fn read_record<T: Copy>(src: *const T) -> T {
    let mut result = MaybeUninit::<T>::uninit();
    unsafe {
        copy_record(result.as_mut_ptr().cast(), src.cast(), size_of::<T>());
        result.assume_init()
    }
}
pub(crate) unsafe fn write_record<T: Copy>(dst: *mut T, value: &T) {
    unsafe { copy_record(dst.cast(), (&raw const *value).cast(), size_of::<T>()) }
}
pub(crate) unsafe fn copy_frame(to: *mut NativeFrame, from: *const NativeFrame) {
    unsafe { copy_record(to.cast(), from.cast(), size_of::<NativeFrame>()) }
}
