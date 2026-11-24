//! The pvpanic device (platform-devices plan, pvpanic): an ISA device at port 505H, one byte, described in the DSDT as
//! `Device (PEVT)` with `_HID` "QEMU0001" (`boot::acpi`, [`crate::boot::acpi::Board::pvpanic`]). The register contract is
//! QEMU's (`docs/specs/pvpanic.rst`, read for facts only): a read returns the events the device supports, a write
//! reports events.
//!
//! Supported events: bit 0 PANICKED and bit 1 CRASH_LOADED, so a read returns 3; bit 2 SHUTDOWN is not offered (S5
//! already ends the run, `pm`). A write keeps only the supported bits. PANICKED stores the power request
//! [`pm::PANIC`]: the writing vCPU's run ends with `Stop::Power(pm::PANIC)` after the same quiesce and drain as any
//! power request (power requests are last-writer-wins, as `pm` and `debug_exit`). CRASH_LOADED (the guest is about to
//! run its crash kernel) is counted in [`PvPanicStats::crash_loaded`] and the run continues. A write with both bits is a
//! panic and is not counted (QEMU handles PANICKED first and ignores the rest). Other bits are ignored.
use crate::devices::{Device, Devices};
use crate::pm;
use std::sync::atomic::{AtomicU8, AtomicU64, Ordering::SeqCst};
use std::sync::{Arc, Mutex};

/// The device's one port.
pub const PORT: u16 = 0x505;
const PANICKED: u8 = 1;
const CRASH_LOADED: u8 = 1 << 1;
const SUPPORTED: u8 = PANICKED | CRASH_LOADED;

/// The device's counts, for the run report.
#[derive(Debug, Default, serde::Serialize)]
pub struct PvPanicStats {
    /// CRASH_LOADED writes.
    pub crash_loaded: AtomicU64,
}

struct PvPanic {
    power: Arc<AtomicU8>,
    stats: Arc<PvPanicStats>,
}
impl Device for PvPanic {
    fn read(&mut self, _vcpu: usize, _offset: u64, _size: u64) -> Option<u64> {
        Some(SUPPORTED.into())
    }
    fn write(&mut self, _vcpu: usize, _offset: u64, _size: u64, value: u64) -> bool {
        let events = value as u8 & SUPPORTED;
        if events & PANICKED != 0 {
            self.power.store(pm::PANIC, SeqCst);
        } else if events & CRASH_LOADED != 0 {
            self.stats.crash_loaded.fetch_add(1, SeqCst);
        }
        true
    }
}

/// Puts the device on `devices` at [`PORT`], over the registry's power request; its counts.
pub(crate) fn install(devices: &mut Devices) -> crate::Result<Arc<PvPanicStats>> {
    let stats = Arc::new(PvPanicStats::default());
    let device = PvPanic {
        power: devices.power.clone(),
        stats: stats.clone(),
    };
    devices.register_ports(PORT.into(), 1, Arc::new(Mutex::new(device)))?;
    Ok(stats)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn board() -> (Devices, Arc<PvPanicStats>) {
        let mut devices = Devices::new(1 << 30);
        let stats = install(&mut devices).unwrap();
        (devices, stats)
    }

    /// 505H reads the supported events, PANICKED and CRASH_LOADED (3); one port only (504H and 506H unassigned).
    #[test]
    fn read_reports_panicked_and_crash_loaded() {
        let (devices, _) = board();
        assert_eq!(devices.port(0, PORT, 1, None), Ok(3));
        assert_eq!(devices.port(0, PORT - 1, 1, None), Ok(0xff));
        assert_eq!(devices.port(0, PORT + 1, 1, None), Ok(0xff));
        assert_eq!(devices.port(0, PORT, 2, None), Ok(0xff03), "a word read: 505H, then unassigned 506H");
        assert_eq!(devices.power(), 0);
    }

    /// Writing PANICKED makes the power request `pm::PANIC` (the run ends with that typed stop); not counted.
    #[test]
    fn panicked_write_requests_panic() {
        for value in [PANICKED, PANICKED | CRASH_LOADED, 0xff] {
            let (devices, stats) = board();
            assert_eq!(devices.port(0, PORT, 1, Some(value.into())), Ok(0));
            assert_eq!(devices.power(), pm::PANIC, "{value:#x}");
            assert_eq!(stats.crash_loaded.load(SeqCst), 0, "{value:#x}");
        }
    }

    /// CRASH_LOADED is counted and the run goes on (no power request).
    #[test]
    fn crash_loaded_counts_and_continues() {
        let (devices, stats) = board();
        assert_eq!(devices.port(0, PORT, 1, Some(CRASH_LOADED.into())), Ok(0));
        assert_eq!((devices.power(), stats.crash_loaded.load(SeqCst)), (0, 1));
        assert_eq!(devices.port(1, PORT, 1, Some(CRASH_LOADED.into())), Ok(0));
        assert_eq!((devices.power(), stats.crash_loaded.load(SeqCst)), (0, 2));
    }

    /// SHUTDOWN (04H) is not offered and other bits do nothing: no power request, no count.
    #[test]
    fn unsupported_bits_are_ignored() {
        for value in [1u64 << 2, 0, 0xfc] {
            let (devices, stats) = board();
            assert_eq!(devices.port(0, PORT, 1, Some(value)), Ok(0));
            assert_eq!((devices.power(), stats.crash_loaded.load(SeqCst)), (0, 0), "{value:#x}");
        }
    }
}
