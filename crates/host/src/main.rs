use memory::*;
use natemu_abi::*;
use std::mem::{align_of, size_of};
use std::{env, ffi::c_void, fs, ptr};
use vm::*;

mod admission;
mod boot;
mod clock;
mod compile_cache;
mod devices;
mod drivers;
mod execution;
mod ioapic;
mod irq;
mod json;
mod memory;
mod owners;
mod pic;
mod pit;
mod pm;
mod rtc;
mod system;
mod uart;
mod vm;

type Result<T> = std::result::Result<T, String>;

fn main() {
    let args: Vec<String> = env::args().collect();
    if let Err(error) = drivers::run(&args) {
        eprintln!("{error}");
        std::process::exit(1);
    }
}
