//! The test driver's subcommands, over the natemu library. `run-program` builds a program description's fixture (tables,
//! bytes, initial state, test devices) and calls the emulator mechanisms (vm, memory, owners, execution) through their
//! ordinary operations; its test-only behaviour (tampers, the logging IDT, CPL switches, event sources) is its
//! `execution::Hook`, so no library module knows a test case. `boot-linux` runs a bzImage on the library's boot entry
//! ([`natemu::linux::Boot`]), with no fixture or hook.
use crate::*;

mod linux;
mod program;
mod test_devices;

/// Dispatches the host CLI (`natemu-host RUNTIME.elf run-program|boot-linux ...`) to its driver.
pub(crate) fn run(a: &[String]) -> Result<()> {
    if a.len() >= 6 && a[2] == "run-program" {
        return program::run(a);
    }
    if a.len() >= 5 && a[2] == "boot-linux" {
        return linux::run(a);
    }
    Err("usage: natemu-host RUNTIME.elf run-program FEXC PROGRAM.txt WORKDIR [OPTIONS] | boot-linux FEXC BZIMAGE [OPTIONS]".into())
}
