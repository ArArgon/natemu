//! The mapped-memory test function (`[debug] mapped_testdev`, a test option; docs/restructure/mapped-device-memory-plan.md):
//! natemu's own test function for mapped device memory, for Linux guest cases. No driver binds it and it is never on by
//! default.
//!
//! Identity: vendor 1B36H, device 0005H (as the multi-BAR test function, [`super::testdev`]), revision 01H, class code
//! 00FFH, header type 0, subsystem 1B36H:0100H (natemu's choice, so a guest can tell the two apart), no interrupt pin, no
//! capabilities.
//!
//! BARs:
//! - BAR0-1: [`REGION`] (2 MiB) of 64-bit prefetchable memory, mapped ([`Function::mapped`]): host-backed device memory
//!   ([`DeviceMemory`]) that the guest reads and writes natively wherever BAR0 decodes. It starts zeroed. An access that
//!   still reaches the bus inside it (none should) reads all-ones and its write is dropped.
//! - BAR2: 4 KiB of 32-bit memory, the control page, emulated. 8-byte registers: `SUM` (+8, read: the wrapping 64-bit sum
//!   of the region's 8-byte words, computed through a second, read-only mapping of the object, [`super::memory::View`],
//!   never the guest's) and `FILL` (+16, write: the host fills the region, word k = [`fill_word`]`(value, k)`). Every
//!   other access reads 0 or is dropped.
use super::configuration::{
    PciBarConfiguration, PciBarPrefetchable, PciClassCode, PciConfiguration, PciHeaderType, PciSubclass,
};
use super::memory::View;
use super::{BARS, Bar, BarKind, Bars, DeviceMemory, Function};
use crate::devices::{Access, mask};
use std::sync::{Arc, Mutex};

const VENDOR: u16 = 0x1b36;
const DEVICE: u16 = 0x0005;
const REVISION: u8 = 0x01;
const SUBSYSTEM: (u16, u16) = (0x1b36, 0x0100);
/// BAR0's region, mapped.
pub const REGION: u64 = 2 << 20;
/// BAR2, the control page.
const CONTROL: u64 = 0x1000;
/// The control page's registers.
pub const SUM: u64 = 8;
pub const FILL: u64 = 16;

struct Subclass(u8);
impl PciSubclass for Subclass {
    fn get_register_value(&self) -> u8 {
        self.0
    }
}

/// `FILL`'s pattern: word `k` of the region for the value written (as the driver's test device, `fill_word`).
pub fn fill_word(value: u64, k: u64) -> u64 {
    value ^ k.wrapping_mul(0x9e37_79b9_7f4a_7c15)
}

/// The test function.
pub struct MappedTestDev {
    config: Mutex<PciConfiguration>,
    requests: Vec<(u8, BarKind, u64)>,
    bars: Bars,
    memory: Arc<DeviceMemory>,
    view: View,
}

fn lock<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

impl MappedTestDev {
    /// The function with a new, zeroed region, its BARs at 0 until the bus places them.
    pub fn new() -> crate::Result<Arc<Self>> {
        let memory = DeviceMemory::new(REGION)?;
        let view = memory.view()?;
        let mut config = PciConfiguration::new(
            VENDOR,
            DEVICE,
            REVISION,
            PciClassCode::TooOld,
            &Subclass(0xff),
            None,
            PciHeaderType::Device,
            SUBSYSTEM.0,
            SUBSYSTEM.1,
            None,
        );
        let requests = vec![(0, BarKind::Mem64, REGION), (2, BarKind::Mem32, CONTROL)];
        for &(index, kind, len) in &requests {
            let prefetch = if index == 0 {
                PciBarPrefetchable::Prefetchable
            } else {
                PciBarPrefetchable::NotPrefetchable
            };
            let bar = PciBarConfiguration::new(index.into(), len, kind.into(), prefetch);
            config.add_pci_bar(&bar).map_err(|e| format!("mapped test function BAR{index}: {e:?}"))?;
        }
        let bars = Bars::new(&requests);
        bars.publish(&config);
        Ok(Arc::new(MappedTestDev { config: Mutex::new(config), requests, bars, memory, view }))
    }
    /// The wrapping sum of the region's words, through the view.
    pub fn sum(&self) -> u64 {
        let mut words = vec![0u64; 4096];
        let mut sum = 0u64;
        for at in (0..self.memory.len()).step_by(8 * words.len()) {
            self.view.read_words(at, &mut words);
            sum = words.iter().fold(sum, |a, &w| a.wrapping_add(w));
        }
        sum
    }
}

impl Function for MappedTestDev {
    fn config_read(&self, reg_idx: usize) -> u32 {
        lock(&self.config).read_reg(reg_idx)
    }
    fn config_write(&self, reg_idx: usize, offset: u64, data: &[u8]) {
        let mut config = lock(&self.config);
        let _ = config.write_config_register(reg_idx, offset, data);
        self.bars.publish(&config);
    }
    fn bars(&self) -> [Option<Bar>; BARS] {
        self.bars.get()
    }
    fn bar_requests(&self) -> Vec<(u8, BarKind, u64)> {
        self.requests.clone()
    }
    fn mapped(&self, bar: u8) -> Option<Arc<DeviceMemory>> {
        (bar == 0).then(|| self.memory.clone())
    }
    fn assign(&self, index: u8, base: u64) {
        self.bars.assign(&mut lock(&self.config), index, base);
    }
    fn bar_access(&self, _: usize, bar: u8, offset: u64, size: u64, value: Option<u64>) -> Access {
        Access::Completed(match (bar, offset, size, value) {
            // The mapped region: what reaches the bus reads all-ones, its writes are dropped.
            (0, _, _, None) => mask(size),
            (2, SUM, 8, None) => self.sum(),
            (2, FILL, 8, Some(v)) => {
                let words: Vec<u64> = (0..self.memory.len() / 8).map(|k| fill_word(v, k)).collect();
                self.memory.write_words(0, &words);
                0
            }
            _ => 0,
        })
    }
    fn bar_poll(&self, _: usize, _: u8, _: u64) -> Access {
        Access::Refused
    }
    fn quiesce(&self) -> crate::Result<bool> {
        Ok(true)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identity_bars_and_registers() {
        let f = MappedTestDev::new().unwrap();
        assert_eq!(f.config_read(0), 0x0005_1b36, "vendor, device");
        assert_eq!(f.config_read(2), 0x00ff_0001, "class 00H, subclass FFH, revision 1");
        assert_eq!(f.config_read(11), 0x0100_1b36, "subsystem");
        assert_eq!(f.config_read(15) >> 8 & 0xff, 0, "no interrupt pin");
        assert_eq!(f.bar_requests(), vec![(0, BarKind::Mem64, 2 << 20), (2, BarKind::Mem32, 0x1000)]);
        assert!(f.mapped(0).is_some_and(|m| m.len() == 2 << 20) && f.mapped(2).is_none());
        assert_eq!(f.config_read(4) & 0xf, 0xc, "BAR0: 64-bit prefetchable memory");
        assert_eq!(f.config_read(6) & 0xf, 0, "BAR2: 32-bit memory");
        // SUM of the zeroed region, FILL, and SUM again (through the view)
        let read = |off, size| match f.bar_access(0, 2, off, size, None) {
            Access::Completed(v) => v,
            other => panic!("{other:?}"),
        };
        assert_eq!(read(SUM, 8), 0);
        f.bar_access(0, 2, FILL, 8, Some(0x0123_4567_89ab_cdef));
        let want = (0..REGION / 8).fold(0u64, |a, k| a.wrapping_add(fill_word(0x0123_4567_89ab_cdef, k)));
        assert_eq!(read(SUM, 8), want);
        let mut w = [0u64; 2];
        f.mapped(0).unwrap().read_words(8, &mut w);
        assert_eq!(w, [fill_word(0x0123_4567_89ab_cdef, 1), fill_word(0x0123_4567_89ab_cdef, 2)]);
        // the region through the bus: all-ones, writes dropped
        assert!(matches!(f.bar_access(0, 0, 16, 4, None), Access::Completed(0xffff_ffff)));
        f.bar_access(0, 0, 16, 8, Some(5));
        f.mapped(0).unwrap().read_words(16, &mut w[..1]);
        assert_eq!(w[0], fill_word(0x0123_4567_89ab_cdef, 2));
        assert_eq!(read(0, 8), 0, "other registers read 0");
    }
}
