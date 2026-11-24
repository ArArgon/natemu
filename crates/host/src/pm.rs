//! L4b: the ACPI fixed-hardware PM block the FADT describes (L2, `boot::acpi::platform`): PM1a event (PM1_STS, PM1_EN)
//! at B000H, PM1a control at B004H, the 24-bit PM timer at B008H, the SCI on ISA IRQ 9 (level, active high), and the
//! reset register, one byte at B040H. QEMU 11.1.2's ACPI core (`hw/acpi/core.c`, the PIIX4 PM's registers) except
//! where noted; ACPI 6.5 4.8.
//!
//! **PM timer** (4.8.3.3): `clock::PM_TIMER_HZ` from V, counted from VM creation (QEMU counts from its clock's 0 at VM
//! start): `scale(V) - scale(V0)` mod 2^24, exactly; writes ignored. TMR_STS (PM1_STS bit 0) is set when bit 23 of the
//! count changes and stays set until written with 1 (QEMU's overflow time, the next multiple of 2^23 after the clear).
//! **Departure:** QEMU starts with its overflow time 0, so TMR_STS reads 1 from reset; here it is first set at the
//! first toggle, 2^23 ticks after creation (4.8.3.3: set "whenever the MSB toggles").
//!
//! **PM1 event** (4.8.3.1): PM1_STS bits are cleared by writing 1 (any bit, as QEMU); PM1_EN reads back as written
//! (all 16 bits, as QEMU). The SCI is asserted while a status bit is set with its enable among TMR_EN, GBL_EN,
//! PWRBTN_EN and RTC_EN (`acpi_update_sci`); TMR_STS and PWRBTN_STS have sources on this board (the timer, and
//! [`PowerButton::press`] for the host; no global lock owner or RTC wake). With TMR_EN and TMR_STS clear the next toggle
//! is scheduled on a [`clock::Timer`] thread, which holds an [`Irqs::busy`] guard meanwhile. A write to both halves (32 bits at B000H) is one update.
//!
//! **PM1 control** (4.8.3.2.1): SCI_EN is set from reset (ACPI-only mode, no SMI command) and read-only: the hardware
//! owns it (Table 4.13, "OSPM always preserves this bit position"; QEMU lets a write clear it). SLP_EN is write-only
//! (reads 0); SLP_EN with SLP_TYP = `_S5`'s value (0) powers off: the vCPU's run ends with the typed stop `poweroff`.
//! Another SLP_TYP with SLP_EN (no S1-S4 here: the DSDT has only `_S5`) is refused: the vCPU stops `unsupported_port`
//! at the instruction, the register unchanged. **Reset register** (4.8.3.6): writing `RESET_VALUE` (6) ends the run
//! with the typed stop `reset`; other values do nothing; it reads 0. (QEMU's PIIX4 has none; this is the FADT's.)
//! **Keyboard-controller reset** (port 64H, owner decision November 11), on a board without the 8042 (`machine.ps2`
//! off: `clock::install` registers this register only then; with it, port 64H is the 8042's, `crate::ps2`, whose reset
//! pulse and output-port reset bit make the same request): the FADT says there is no 8042, but writing its pulse-reset
//! command FEH there ends the run the same way, so every guest reboot method that reaches a device ends with `reset`
//! after the disk drain; other writes do nothing; it reads 0 (both buffers empty, so a guest's wait for the input buffer
//! ends at once). A guest reboot ends the run: natemu does not reboot.
//!
//! Widths: every register is reached by bytes, a wider access being its bytes (QEMU decodes PM1 at 16 and the timer at
//! 32 bits, which are the widths ACPI uses).
use crate::clock::{self, Timed};
use crate::devices::Device;
use crate::irq::{Busy, Irqs};
use natemu_abi::clock::{PM_TIMER_HZ, count_at, scale};
use std::sync::Arc;
use std::sync::atomic::{AtomicU8, Ordering::SeqCst};

const TMR: u16 = 1;
const GBL: u16 = 1 << 5;
const PWRBTN: u16 = 1 << 8;
const RTC: u16 = 1 << 10;
const SCI_EN: u16 = 1;
const SLP_EN: u16 = 1 << 13;
/// The run's power request ([`crate::devices::Devices::power`]).
pub const POWEROFF: u8 = 1;
pub const RESET: u8 = 2;
/// A test option's exit port was written ([`crate::debug_exit`]).
pub const DEBUG_EXIT: u8 = 3;
/// The guest reported a panic to the pvpanic device ([`crate::pvpanic`]).
pub const PANIC: u8 = 4;
/// Offsets from B000H.
pub(crate) const CNT: u64 = 4;
pub(crate) const TIMER: u64 = 8;
pub(crate) const RESET_REG: u64 = 0x40;
/// Not in the PM block: port 64H's offset in this device (without the 8042), and the 8042 command that pulses the reset
/// line.
pub(crate) const KBD_REG: u64 = 0x80;
pub(crate) const KBD_PORT: u16 = 0x64;
const KBD_RESET: u8 = 0xfe;

pub(crate) struct Pm {
    sts: u16,
    en: u16,
    cnt: u16,
    /// The PM timer count at which TMR_STS is next set (from creation).
    overflow: u64,
    /// V's rate and the PM clock at creation.
    frequency: u64,
    start: u64,
    irqs: Irqs,
    busy: Option<Busy>,
    power: Arc<AtomicU8>,
}
impl Pm {
    pub(crate) fn new(irqs: &Irqs, frequency: u64, power: Arc<AtomicU8>) -> Self {
        let start = scale(clock::now(), PM_TIMER_HZ, frequency);
        Self {
            sts: 0,
            en: 0,
            cnt: SCI_EN,
            overflow: 1 << 23,
            frequency,
            start,
            irqs: irqs.clone(),
            busy: None,
            power,
        }
    }
    /// The PM timer's count since creation (not wrapped).
    fn ticks(&self) -> u64 {
        scale(clock::now(), PM_TIMER_HZ, self.frequency) - self.start
    }
    /// `acpi_pm1_evt_get_sts`: TMR_STS set once the count passed the overflow.
    fn status(&mut self) -> u16 {
        if self.ticks() >= self.overflow {
            self.sts |= TMR;
        }
        self.sts
    }
    /// `acpi_update_sci`: the SCI level, and the overflow scheduled (with its guard) while TMR_EN and TMR_STS clear.
    fn update_sci(&mut self) {
        let sts = self.status();
        self.irqs
            .line(9, sts & self.en & (RTC | PWRBTN | GBL | TMR) != 0);
        self.busy = if self.en & TMR != 0 && sts & TMR == 0 {
            self.busy.take().or_else(|| Some(self.irqs.busy()))
        } else {
            None
        };
    }
    fn image(&mut self, offset: u64) -> u32 {
        match offset {
            0 => u32::from(self.status()) | u32::from(self.en) << 16,
            CNT => u32::from(self.cnt),
            TIMER => (self.ticks() & 0xff_ffff) as u32,
            _ => 0,
        }
    }
}
/// The register block holding `offset` and its length.
fn block(offset: u64) -> Option<(u64, u64)> {
    match offset {
        0..4 => Some((0, 4)),
        4..6 => Some((CNT, 2)),
        8..12 => Some((TIMER, 4)),
        RESET_REG => Some((RESET_REG, 1)),
        KBD_REG => Some((KBD_REG, 1)),
        _ => None,
    }
}
impl Device for Pm {
    fn read(&mut self, _vcpu: usize, offset: u64, size: u64) -> Option<u64> {
        let (base, _) = block(offset).filter(|&(b, l)| offset + size <= b + l)?;
        Some(u64::from(self.image(base)) >> (8 * (offset - base)) & (u64::MAX >> (64 - 8 * size)))
    }
    fn write(&mut self, _vcpu: usize, offset: u64, size: u64, value: u64) -> bool {
        let Some((base, _)) = block(offset).filter(|&(b, l)| offset + size <= b + l) else {
            return false;
        };
        let shift = 8 * (offset - base);
        let (mask, value) = ((u64::MAX >> (64 - 8 * size)) << shift, value << shift);
        match base {
            0 => {
                // PM1_STS bytes written: write-1-to-clear (a cleared TMR_STS schedules the next toggle); PM1_EN merged.
                let sts = (value & mask & 0xffff) as u16;
                if self.status() & sts & TMR != 0 {
                    self.overflow = (self.ticks() + 0x80_0000) & !0x7f_ffff;
                }
                self.sts &= !sts;
                if mask >> 16 != 0 {
                    self.en = ((u64::from(self.en) << 16 & !mask | value & mask) >> 16) as u16;
                }
                self.update_sci();
            }
            CNT => {
                let v = (u64::from(self.cnt) & !mask | value & mask) as u16;
                if v & SLP_EN != 0
                    && u64::from(v >> 10 & 7) != u64::from(crate::boot::acpi::platform::S5_SLP_TYP)
                {
                    return false;
                }
                self.cnt = v & !SLP_EN | SCI_EN;
                if v & SLP_EN != 0 {
                    self.power.store(POWEROFF, SeqCst);
                }
            }
            RESET_REG => {
                if value as u8 == crate::boot::acpi::platform::RESET_VALUE {
                    self.power.store(RESET, SeqCst);
                }
            }
            KBD_REG => {
                if value as u8 == KBD_RESET {
                    self.power.store(RESET, SeqCst);
                }
            }
            _ => {} // the PM timer
        }
        true
    }
}
impl Pm {
    /// The power button's press (ACPI 6.5 4.8.3.1.1: PWRBTN_STS set, the SCI raised if PWRBTN_EN).
    fn press(&mut self) {
        self.sts |= PWRBTN;
        self.update_sci();

    }
}
/// The fixed power button, for a host thread (the control socket's `power_button`): a cloneable handle on the PM device.
/// A press takes only the PM device's own lock, the one every guest access to B000H takes, so it never holds a vCPU
/// beyond that.
#[derive(Clone)]
pub struct PowerButton(Arc<clock::Inner<Pm>>);
impl PowerButton {
    pub(crate) fn of(pm: &clock::Timer<Pm>) -> Self {
        Self(pm.inner())
    }
    /// Presses the button: PWRBTN_STS (bit 8 of PM1_STS) is set, and the SCI (IRQ 9) rises if PWRBTN_EN is set. Pressing
    /// again while the status is set changes nothing; the guest clears the status by writing 1 to it.
    pub fn press(&self) {
        self.0.access(Pm::press);
    }
}
impl Timed for Pm {
    fn next(&self) -> Option<u64> {
        self.busy
            .as_ref()
            .map(|_| count_at(self.start + self.overflow, PM_TIMER_HZ, self.frequency))
    }
    fn fire(&mut self, _at: u64) {
        self.update_sci();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::clock::Timer;
    use crate::devices::{Device, Devices};
    use std::sync::Mutex;

    struct Rig {
        irqs: Irqs,
        pm: Timer<Pm>,
    }
    /// A PM device on a registry with the interrupt controllers (no vCPU): the SCI through the chipset's input level.
    fn rig() -> Rig {
        let memory = Arc::new(crate::memory::Memory::with_size(4096).unwrap());
        let uart = Arc::new(Mutex::new(crate::uart::Uart::new()));
        let mut devices = Devices::new(1 << 30);
        let irqs = crate::irq::install(&mut devices, &memory, 0, &uart).unwrap();
        let pm = Timer::new(Pm::new(&irqs, clock::frequency(), Arc::default()), "pm-test");
        Rig { irqs, pm }
    }
    impl Rig {
        fn sts(&mut self) -> u16 {
            self.pm.read(0, 0, 2).unwrap() as u16
        }
        fn enable(&mut self, en: u16) {
            assert!(self.pm.write(0, 2, 2, en.into()));
        }
    }

    #[test]
    fn press_sets_pwrbtn_sts() {
        let mut r = rig();
        let button = PowerButton::of(&r.pm);
        assert_eq!(r.sts() & PWRBTN, 0);
        button.press();
        assert_eq!(r.sts() & PWRBTN, PWRBTN);
        // A handle clone and a second press change nothing more.
        button.clone().press();
        assert_eq!(r.sts() & PWRBTN, PWRBTN);
    }

    #[test]
    fn sci_follows_pwrbtn_en() {
        let mut r = rig();
        let button = PowerButton::of(&r.pm);
        button.press();
        assert!(!r.irqs.level(9), "no SCI while PWRBTN_EN is clear");
        r.enable(PWRBTN);
        assert!(r.irqs.level(9), "the SCI once enabled with the status set");
        r.enable(0);
        assert!(!r.irqs.level(9), "disabling drops the line");
        r.enable(PWRBTN);
        assert!(r.irqs.level(9));
        // Pressing with the enable already set raises the line at once.
        assert!(r.pm.write(0, 0, 2, PWRBTN.into()));
        assert!(!r.irqs.level(9));
        button.press();
        assert!(r.irqs.level(9));
    }

    #[test]
    fn write_one_clears_pwrbtn_sts() {
        let mut r = rig();
        let button = PowerButton::of(&r.pm);
        r.enable(PWRBTN);
        button.press();
        assert!(r.irqs.level(9));
        // Writing 0 to the bit, or 1 to another bit, leaves it.
        assert!(r.pm.write(0, 0, 2, u64::from(RTC)));
        assert_eq!(r.sts() & PWRBTN, PWRBTN);
        assert!(r.pm.write(0, 0, 2, PWRBTN.into()));
        assert_eq!(r.sts() & PWRBTN, 0);
        assert!(!r.irqs.level(9), "the line follows the status down");
        assert_eq!(r.pm.read(0, 2, 2), Some(PWRBTN.into()), "PM1_EN is untouched");
    }
}
