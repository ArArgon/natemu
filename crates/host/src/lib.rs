//! natemu, the emulator library: the VM, guest RAM and the resident image, the per-vCPU service loop (compile, admission,
//! system-instruction decode, devices), the boot-protocol loader and ACPI tables, and the platform devices. The
//! integration entry is [`linux::Boot`]; the test driver (`natemu-host`, `crates/driver`) uses the pieces directly and
//! supplies its test-only behaviour through [`execution::Hook`].
use memory::*;
use natemu_abi::*;
use std::mem::{align_of, size_of};
use std::{ffi::c_void, fs, ptr};
use vm::*;

mod admission;
pub mod boot;
pub mod clock;
pub mod compile_cache;
pub mod devices;
pub mod execution;
mod ioapic;
pub mod irq;
pub mod linux;
pub mod memory;
pub mod owners;
pub mod perf;
mod pic;
mod pit;
pub mod pm;
mod relocation;
pub mod report;
mod rtc;
mod system;
pub mod uart;
pub mod virtio;
pub mod vm;

pub type Result<T> = std::result::Result<T, String>;
