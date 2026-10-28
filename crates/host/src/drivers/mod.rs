//! Test drivers: the host CLI. `run-program` builds a program description's
//! fixture (tables, bytes, initial state, test-only tampers) and calls the emulator
//! mechanisms (vm, memory, owners, execution) through their ordinary operations;
//! no mechanism module knows a test case.
use crate::*;

mod program;

/// Dispatches the host CLI (`natemu-host RUNTIME.elf run-program ...`) to its driver.
pub(crate) fn run(a: &[String]) -> Result<()> {
    if a.len() >= 6 && a[2] == "run-program" {
        return program::run(a);
    }
    Err("usage: natemu-host RUNTIME.elf run-program FEXC PROGRAM.txt WORKDIR [OPTIONS]".into())
}
