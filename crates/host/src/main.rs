use diagnostics::*;
use memory::*;
use natemu_abi::*;
use std::mem::{align_of, size_of};
use std::{env, ffi::c_void, fs, ptr};
use vm::*;

mod admission;
mod diagnostics;
mod drivers;
mod execution;
mod json;
#[cfg(test)]
#[path = "../../runtime/src/coordinator.rs"]
mod coordinator_tests;
mod memory;
mod owners;
mod translation;
mod vm;

type Result<T> = std::result::Result<T, String>;

unsafe extern "C" {
    fn getentropy(buffer: *mut c_void, length: usize) -> i32;
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if let Err(error) = drivers::run(&args) {
        eprintln!("{error}");
        std::process::exit(1);
    }
}
