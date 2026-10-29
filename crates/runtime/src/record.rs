//! Volatile copies of quiescent owner records and native frames.
use core::mem::{MaybeUninit, size_of};
use natemu_abi::NativeFrame;

/// A volatile copy of a quiescent record (no other writer while it runs): 8 bytes at a time when both ends and the size
/// allow it (the state record and frames do; a byte copy of them made every resident entry cost about 1 us), else bytewise.
pub(crate) unsafe fn copy_record(dst: *mut u8, src: *const u8, count: usize) {
    if (dst as usize | src as usize | count) % 8 != 0 {
        return unsafe { natemu_abi::guest_memory::copy_volatile(dst, src, count) };
    }
    // As MaybeUninit words: a record's padding bytes are copied without being read as integers.
    let (dst, src) = (dst.cast::<MaybeUninit<u64>>(), src.cast::<MaybeUninit<u64>>());
    for i in 0..count / 8 {
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
pub(crate) unsafe fn write_record<T: Copy>(dst: *mut T, value: T) {
    unsafe { copy_record(dst.cast(), (&raw const value).cast(), size_of::<T>()) }
}
pub(crate) unsafe fn copy_frame(to: *mut NativeFrame, from: *const NativeFrame) {
    unsafe { copy_record(to.cast(), from.cast(), size_of::<NativeFrame>()) }
}
