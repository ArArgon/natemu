//! QEMU's `isa-debug-exit` device (`hw/misc/debugexit.c`), a test option only (boot-interpreter plan section 7: the
//! exit port of kvm-unit-tests, whose `exit()` writes its status to port F4H, `lib/x86/io.c`): registered at ports
//! F4H-F7H (QEMU `-device isa-debug-exit,iobase=0xf4,iosize=0x4`, as kvm-unit-tests' `x86/run` adds it). A write of any
//! width at any of its ports with value V ends the run as QEMU's process does with exit status (V << 1) | 1: the writing
//! vCPU's run ends with the typed stop `Stop::Power(pm::DEBUG_EXIT)` after the write, and [`DebugExit::status`] gives the
//! status. Reads answer 0, as QEMU's `debug_exit_read` does.
use crate::devices::Device;
use std::sync::Arc;
use std::sync::atomic::{AtomicU8, AtomicU64, Ordering::SeqCst};

/// The ports.
pub const PORT: u16 = 0xf4;
pub const PORTS: u32 = 4;

/// The device: the power request it makes and the value written ([`NONE`] until a write).
pub struct DebugExit {
    power: Arc<AtomicU8>,
    value: Arc<AtomicU64>,
}
/// No write yet.
pub const NONE: u64 = u64::MAX;
impl DebugExit {
    /// The device over the registry's power request, and the value cell a run's owner reads afterwards.
    pub(crate) fn new(power: Arc<AtomicU8>) -> (Self, Arc<AtomicU64>) {
        let value = Arc::new(AtomicU64::new(NONE));
        (
            Self {
                power,
                value: value.clone(),
            },
            value,
        )
    }
    /// QEMU's exit status for a write of `value`: (value << 1) | 1, of which a waiting parent sees the low 8 bits.
    pub fn status(value: u64) -> u64 {
        value << 1 | 1
    }
}
impl Device for DebugExit {
    fn read(&mut self, _vcpu: usize, _offset: u64, _size: u64) -> Option<u64> {
        Some(0)
    }
    fn write(&mut self, _vcpu: usize, _offset: u64, _size: u64, value: u64) -> bool {
        // The first write ends the run; a second (another vCPU's, before the run ends) keeps the first's value.
        let _ = self.value.compare_exchange(NONE, value, SeqCst, SeqCst);
        self.power.store(crate::pm::DEBUG_EXIT, SeqCst);
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::devices::Devices;
    use std::sync::Mutex;

    /// kvm-unit-tests' `outl(code, 0xf4)`: the value and QEMU's status ((0 << 1) | 1 = 1 is its pass), the power request
    /// made; a byte write at F6H too; reads 0 (QEMU's); nothing outside F4H-F7H.
    #[test]
    fn a_write_ends_the_run_with_qemus_status() {
        for (port, size, data) in [(0xf4u16, 4u64, 0u64), (0xf4, 4, 1), (0xf6, 1, 0x40 | 13)] {
            let mut devices = Devices::new(1 << 30);
            let (device, value) = DebugExit::new(devices.power.clone());
            devices
                .register_ports(PORT.into(), PORTS, Arc::new(Mutex::new(device)))
                .unwrap();
            assert_eq!(devices.port(0, 0xf4, 4, None), Ok(0));
            assert_eq!(devices.port(0, 0xf5, 2, None), Ok(0));
            assert_eq!(
                devices.port(0, 0xf8, 1, None),
                Ok(0xff),
                "unassigned past F7H"
            );
            assert_eq!((devices.power(), value.load(SeqCst)), (0, NONE));
            assert_eq!(devices.port(0, port, size, Some(data)), Ok(0));
            assert_eq!(devices.power(), crate::pm::DEBUG_EXIT);
            assert_eq!(value.load(SeqCst), data);
            // A later write keeps the first value.
            assert_eq!(devices.port(1, 0xf4, 4, Some(99)), Ok(0));
            assert_eq!(value.load(SeqCst), data);
        }
        assert_eq!((DebugExit::status(0), DebugExit::status(1)), (1, 3));
        assert_eq!(DebugExit::status(0x40 | 13) & 0xff, 0x9b);
        // Only F4H-F7H: F8H and F3H stay unassigned.
        let mut devices = Devices::new(1 << 30);
        let (device, _) = DebugExit::new(devices.power.clone());
        devices
            .register_ports(PORT.into(), PORTS, Arc::new(Mutex::new(device)))
            .unwrap();
        assert_eq!(devices.port(0, 0xf8, 1, Some(0)), Ok(0));
        assert_eq!(devices.port(0, 0xf3, 1, Some(0)), Ok(0));
        assert_eq!(devices.power(), 0);
    }
}
