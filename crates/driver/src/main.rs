//! `natemu-host`, the test driver: `run-program` and `boot-linux` over the natemu library ([`natemu`]).
use natemu::vm::*;
use natemu::{
    Result, boot, clock, compile_cache, devices, execution, irq, memory, owners, pm, uart,
};
use natemu_abi::*;
use std::fs;
use std::mem::size_of;

mod drivers;

fn main() {
    if let Err(error) = drivers::run() {
        eprintln!("{error}");
        std::process::exit(1);
    }
}
