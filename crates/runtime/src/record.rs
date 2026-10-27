//! Volatile copies of quiescent owner records and native frames.
use core::mem::{MaybeUninit, size_of};
use natemu_abi::NativeFrame;
pub(crate) use natemu_abi::guest_memory::copy_volatile as copy_record;

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
