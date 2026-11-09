//! L4a: the 8259A pair of a PC (master at 20H/21H, slave at A0H/A1H cascaded on the master's IR2) and the PIIX edge/level
//! control registers (ELCR, 4D0H/4D1H). Transcribed from QEMU 11.1.2's model (`hw/intc/i8259.c`, tag v11.1.2; line
//! numbers of that file): priority `get_priority` (60-72), `pic_get_irq` (75-101), the input `pic_set_irq` (118-154, edge
//! detection by `last_irr`, level by ELCR or ICW1.LTIM), acknowledge `pic_intack` (157-171) and the INTA cycle
//! `pic_read_irq` (173-211, the slave through IR2, spurious IRQ 7 of either chip), port writes `pic_ioport_write`
//! (228-319: ICW1-ICW4, OCW1 the IMR, OCW2 non-specific/specific EOI and the rotations, OCW3 read select, poll and special
//! mask), port reads `pic_ioport_read` (321-349: poll, IRR/ISR, IMR) and the ELCR (356-368, masks F8H and DEH from
//! `hw/intc/i8259_common.c` `i8259_init_chip`), and the ICW1 reset `pic_reset_common` (`i8259_common.c` 36-54: ELCR and
//! LTIM kept). Byte registers (QEMU's `impl.max_access_size` 1: a wider access is split into byte accesses, lowest first).
//! The chip's INT output is [`Pic::output`]; the board ([`crate::irq`]) wires the master's to every LAPIC's LINT0.

/// One 8259A (`PICCommonState`).
#[derive(Default, Clone, Debug)]
pub(crate) struct Chip {
    last_irr: u8,
    irr: u8,
    imr: u8,
    isr: u8,
    priority_add: u8,
    irq_base: u8,
    read_reg_select: bool,
    poll: bool,
    special_mask: bool,
    init_state: u8,
    auto_eoi: bool,
    rotate_on_auto_eoi: bool,
    special_fully_nested_mode: bool,
    init4: bool,
    single_mode: bool,
    elcr: u8,
    elcr_mask: u8,
    ltim: bool,
    master: bool,
}
impl Chip {
    pub(crate) fn irr_bits(&self) -> u8 {
        self.irr
    }
    fn new(master: bool) -> Self {
        Self {
            master,
            elcr_mask: if master { 0xf8 } else { 0xde },
            ..Self::default()
        }
    }
    /// `get_priority`: the highest priority in `mask` (0 highest, after rotation), 8 if none.
    fn priority(&self, mask: u8) -> u8 {
        if mask == 0 {
            return 8;
        }
        (0..8)
            .find(|p| mask & 1 << ((p + self.priority_add) & 7) != 0)
            .unwrap()
    }
    /// `pic_get_irq`: the input the chip requests the CPU for, if any.
    fn get_irq(&self) -> Option<u8> {
        let priority = self.priority(self.irr & !self.imr);
        if priority == 8 {
            return None;
        }
        let mut mask = self.isr;
        if self.special_mask {
            mask &= !self.imr;
        }
        if self.special_fully_nested_mode && self.master {
            mask &= !(1 << 2);
        }
        (priority < self.priority(mask)).then_some((priority + self.priority_add) & 7)
    }
    fn level(&self, mask: u8) -> bool {
        self.ltim || self.elcr & mask != 0
    }
    fn set_irq(&mut self, irq: u8, level: bool) {
        let mask = 1 << irq;
        if self.level(mask) {
            if level {
                (self.irr, self.last_irr) = (self.irr | mask, self.last_irr | mask);
            } else {
                (self.irr, self.last_irr) = (self.irr & !mask, self.last_irr & !mask);
            }
        } else if level {
            if self.last_irr & mask == 0 {
                self.irr |= mask;
            }
            self.last_irr |= mask;
        } else {
            self.last_irr &= !mask;
        }
    }
    fn intack(&mut self, irq: u8) {
        if self.auto_eoi {
            if self.rotate_on_auto_eoi {
                self.priority_add = (irq + 1) & 7;
            }
        } else {
            self.isr |= 1 << irq;
        }
        // A level-sensitive request is not cleared here.
        if !self.level(1 << irq) {
            self.irr &= !(1 << irq);
        }
    }
    /// `pic_init_reset` (`pic_reset_common`): ICW1's reset; ELCR and LTIM are kept, the IRR keeps its level-triggered bits.
    fn init_reset(&mut self) {
        let (elcr, elcr_mask, ltim, master, irr) =
            (self.elcr, self.elcr_mask, self.ltim, self.master, self.irr);
        *self = Self {
            elcr,
            elcr_mask,
            ltim,
            master,
            irr: irr & elcr,
            ..Self::default()
        };
    }
    fn write(&mut self, addr: u64, val: u8) {
        if addr == 0 {
            if val & 0x10 != 0 {
                self.init_reset();
                self.init_state = 1;
                self.init4 = val & 1 != 0;
                self.single_mode = val & 2 != 0;
                self.ltim = val & 8 != 0;
            } else if val & 0x08 != 0 {
                if val & 0x04 != 0 {
                    self.poll = true;
                }
                if val & 0x02 != 0 {
                    self.read_reg_select = val & 1 != 0;
                }
                if val & 0x40 != 0 {
                    self.special_mask = val >> 5 & 1 != 0;
                }
            } else {
                let cmd = val >> 5;
                match cmd {
                    0 | 4 => self.rotate_on_auto_eoi = cmd >> 2 != 0,
                    1 | 5 => {
                        let priority = self.priority(self.isr);
                        if priority != 8 {
                            let irq = (priority + self.priority_add) & 7;
                            self.isr &= !(1 << irq);
                            if cmd == 5 {
                                self.priority_add = (irq + 1) & 7;
                            }
                        }
                    }
                    3 => self.isr &= !(1 << (val & 7)),
                    6 => self.priority_add = (val + 1) & 7,
                    7 => {
                        let irq = val & 7;
                        self.isr &= !(1 << irq);
                        self.priority_add = (irq + 1) & 7;
                    }
                    _ => {}
                }
            }
        } else {
            match self.init_state {
                0 => self.imr = val,
                1 => {
                    self.irq_base = val & 0xf8;
                    self.init_state = if self.single_mode {
                        if self.init4 { 3 } else { 0 }
                    } else {
                        2
                    };
                }
                2 => self.init_state = if self.init4 { 3 } else { 0 },
                _ => {
                    self.special_fully_nested_mode = val >> 4 & 1 != 0;
                    self.auto_eoi = val >> 1 & 1 != 0;
                    self.init_state = 0;
                }
            }
        }
    }
    fn read(&mut self, addr: u64) -> u8 {
        if self.poll {
            self.poll = false;
            return match self.get_irq() {
                Some(irq) => {
                    self.intack(irq);
                    irq | 0x80
                }
                None => 0,
            };
        }
        match (addr, self.read_reg_select) {
            (0, true) => self.isr,
            (0, false) => self.irr,
            _ => self.imr,
        }
    }
}

/// The master/slave pair as a PC wires it: the slave's INT output drives the master's IR2.
#[derive(Clone, Debug)]
pub(crate) struct Pic {
    pub(crate) chips: [Chip; 2],
}
impl Pic {
    pub(crate) fn new() -> Self {
        Self {
            chips: [Chip::new(true), Chip::new(false)],
        }
    }
    /// The slave's output into the master's IR2 (QEMU's `pic_update_irq` on the slave, `qdev_connect_gpio_out`).
    fn cascade(&mut self) {
        let out = self.chips[1].get_irq().is_some();
        self.chips[0].set_irq(2, out);
    }
    /// ISA IRQ `irq` (0..15) at `level`.
    pub(crate) fn set_irq(&mut self, irq: u8, level: bool) {
        self.chips[usize::from(irq >= 8)].set_irq(irq & 7, level);
        self.cascade();
    }
    /// L4b: whether line `irq` (0..15) is masked: at its chip's IMR, or a slave line at the master's IRQ 2.
    pub(crate) fn masked(&self, irq: u8) -> bool {
        let m = |chip: usize, bit: u8| self.chips[chip].imr >> bit & 1 != 0;
        if irq < 8 {
            m(0, irq)
        } else {
            m(1, irq - 8) || m(0, 2)
        }
    }
    /// The master's INT output (`pic_get_output`).
    pub(crate) fn output(&self) -> bool {
        self.chips[0].get_irq().is_some()
    }
    /// The INTA cycle (`pic_read_irq`): the vector the pair puts on the bus (a spurious IRQ 7 of the chip with nothing
    /// to give).
    pub(crate) fn read_irq(&mut self) -> u8 {
        let vector = match self.chips[0].get_irq() {
            Some(2) => {
                let irq2 = match self.chips[1].get_irq() {
                    Some(irq2) => {
                        self.chips[1].intack(irq2);
                        irq2
                    }
                    None => 7,
                };
                self.chips[0].intack(2);
                self.chips[1].irq_base + irq2
            }
            Some(irq) => {
                self.chips[0].intack(irq);
                self.chips[0].irq_base + irq
            }
            None => self.chips[0].irq_base + 7,
        };
        self.cascade();
        vector
    }
    /// A byte port access: `chip` 0 (20H/21H) or 1 (A0H/A1H), `addr` 0 or 1; `elcr`: the chip's ELCR byte instead.
    pub(crate) fn access(&mut self, chip: usize, addr: u64, elcr: bool, data: Option<u8>) -> u8 {
        let c = &mut self.chips[chip];
        let value = match (elcr, data) {
            (true, Some(v)) => {
                c.elcr = v & c.elcr_mask;
                0
            }
            (true, None) => c.elcr,
            (false, Some(v)) => {
                c.write(addr, v);
                0
            }
            (false, None) => c.read(addr),
        };
        self.cascade();
        value
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    /// Linux's `init_8259A(0)` (arch/x86/kernel/i8259.c): vectors 30H/38H, cascade on IR2, normal EOI.
    fn linux_init(p: &mut Pic) {
        for (chip, addr, v) in [
            (0, 1, 0xff),
            (0, 0, 0x11),
            (0, 1, 0x30),
            (0, 1, 0x04),
            (0, 1, 0x01),
            (1, 0, 0x11),
            (1, 1, 0x38),
            (1, 1, 0x02),
            (1, 1, 0x01),
        ] {
            p.access(chip, addr, false, Some(v));
        }
    }
    #[test]
    fn linux_sequences() {
        let mut p = Pic::new();
        // probe_8259A: the IMR reads back what was written.
        p.access(1, 1, false, Some(0xff));
        p.access(0, 1, false, Some(0xfb));
        assert_eq!(p.access(0, 1, false, None), 0xfb);
        linux_init(&mut p);
        p.access(0, 1, false, Some(0xef)); // IRQ4 unmasked
        p.access(1, 1, false, Some(0xff));
        p.set_irq(4, true);
        assert!(p.output());
        assert_eq!(p.read_irq(), 0x34);
        assert!(!p.output());
        // OCW3 ISR read, then back to the IRR (i8259A_irq_real).
        p.access(0, 0, false, Some(0x0b));
        assert_eq!(p.access(0, 0, false, None), 0x10);
        p.access(0, 0, false, Some(0x0a));
        p.access(0, 0, false, Some(0x64)); // specific EOI
        assert_eq!(p.chips[0].isr, 0);
        // A second raise while high is no edge.
        p.set_irq(4, true);
        assert!(!p.output());
        p.set_irq(4, false);
        p.set_irq(4, true);
        assert!(p.output());
        // Slave IRQ 9 through the cascade, level by the ELCR.
        p.access(1, 0, true, Some(0x02 | 0x01));
        assert_eq!(p.access(1, 0, true, None), 0x02); // mask DEH
        p.access(1, 1, false, Some(0xfd));
        p.access(0, 1, false, Some(0xeb));
        p.set_irq(9, true);
        assert_eq!(p.read_irq(), 0x39); // IR2 before IR4
        p.access(0, 0, false, Some(0x20)); // non-specific EOI to the master
        assert_eq!(p.read_irq(), 0x34);
        assert_eq!(p.chips[1].irr & 2, 2); // a level request stays
        // Nothing pending: the master's spurious IRQ 7; the poll command reads 0.
        p.access(1, 1, false, Some(0xff));
        p.access(0, 1, false, Some(0xff));
        assert_eq!(p.read_irq(), 0x37);
        p.access(0, 0, false, Some(0x0c));
        assert_eq!(p.access(0, 0, false, None), 0);
    }
}
