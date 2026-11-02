//! `natemu-host`, the test driver: `run-program` and `boot-linux` over the natemu library ([`natemu`]).
use natemu::vm::*;
use natemu::{Result, boot, clock, compile_cache, devices, execution, irq, memory, owners, pm, uart};
use natemu_abi::*;
use std::mem::size_of;
use std::{env, fs};

mod drivers;

fn main() {
    let args: Vec<String> = env::args().collect();
    if let Err(error) = drivers::run(&args) {
        eprintln!("{error}");
        std::process::exit(1);
    }
}
