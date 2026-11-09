//! L4a: the I/O APIC (82093AA-compatible, 24 pins), transcribed from QEMU 11.1.2's model (`hw/intc/ioapic.c`, tag
//! v11.1.2; line numbers of that file; constants from `hw/intc/ioapic_internal.h`, reset from `ioapic_common.c`
//! `ioapic_reset_common` 119-130): delivery `ioapic_service` (97-149), the input `ioapic_set_irq` (160-197), the EOI
//! broadcast `ioapic_eoi_broadcast` (230-300), register reads `ioapic_mem_read` (302-344) and writes `ioapic_mem_write`
//! (374-428, with `ioapic_fix_edge_remote_irr` 365-372). Version 20H (QEMU's default, `IOAPIC_VER_DEF` 454: with the EOI
//! register at 40H), 24 entries, ID in bits 27:24 (4 bits), ARB reading as the ID. Registers: IOREGSEL at 00H (any width,
//! 8 bits kept), IOWIN at 10H and EOI at 40H (32-bit only: other widths read 0 and are ignored, as QEMU's), the page
//! folded every 100H (QEMU's `addr &= 0xff`); every other offset reads 0. Entries: vector, delivery mode, destination
//! mode, delivery status (always 0), polarity (stored, not applied: inputs are logical levels, 1 = asserted, as in
//! QEMU), Remote IRR (read-only), trigger mode, mask, destination (bits 63:56; 55:48 stored).
//!
//! Semantics. Edge: a rising input while unmasked is delivered once (a raise while masked is ignored, 82093AA "masked
//! edge interrupts are ignored"). Level: the input's level is kept (also while masked); while it is asserted, unmasked
//! and Remote IRR clear, the entry is delivered and Remote IRR set; the LAPIC's EOI broadcast of the vector (or a write of
//! it to the EOI register) clears Remote IRR and delivers again if the input is still asserted. Every redirection write
//! re-evaluates (an unmasked level input still asserted, with Remote IRR clear, is delivered). Remote IRR is set only when
//! a LAPIC accepted the message (82093AA; QEMU sets it on sending, and its software-disabled LAPICs accept fixed
//! messages, ours do not, X3's reading of SDM 11.4.7.2): a level message nobody accepted leaves it clear.
//!
//! Departures from QEMU (each where QEMU is not the hardware): QEMU's edge input delivers on every raise call, a raise
//! while already high included; here an input is a level and an edge is its 0 -> 1 change (the board reports changes
//! only: [`crate::irq`]). QEMU delays a redelivery after 10,000 successive EOIs of a still-asserted line (a storm
//! heuristic, 279-291); here it is always immediate. QEMU reads the PIC's vector for an ExtINT entry also when the entry
//! is masked or coalesced (an INTA cycle with no delivery, `ioapic_entry_parse` 83-84); here only for a delivery. An
//! 8-byte access is refused (QEMU's core would split or reject it; no driver makes one).
pub(crate) const PINS: usize = 24;
pub(crate) const VERSION: u64 = 0x20;
const MASKED: u64 = 1 << 16;
const LEVEL: u64 = 1 << 15;
const REMOTE_IRR: u64 = 1 << 14;
const DELIVERY_STATUS: u64 = 1 << 12;
const RO_BITS: u64 = REMOTE_IRR | DELIVERY_STATUS;

/// An interrupt message on the APIC bus (QEMU builds an MSI from the entry, `ioapic_entry_parse`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct Message {
    /// Bits 63:56 of the entry.
    pub(crate) dest: u8,
    pub(crate) logical: bool,
    /// Delivery mode: 0 fixed, 1 lowest priority, 2 SMI, 4 NMI, 5 INIT, 7 ExtINT (its vector read from the PIC).
    pub(crate) mode: u8,
    pub(crate) vector: u8,
    pub(crate) level: bool,
}
/// What the I/O APIC is wired to: the LAPICs (messages; true when some LAPIC accepted it) and the PIC (an ExtINT
/// entry's INTA cycle).
pub(crate) trait Bus {
    fn send(&mut self, m: Message) -> bool;
    fn inta(&mut self) -> u8;
}

#[derive(Clone, Debug)]
pub(crate) struct Ioapic {
    id: u8,
    ioregsel: u8,
    /// Pending inputs: edge, a rise not yet delivered; level, the input asserted.
    irr: u32,
    pub(crate) redtbl: [u64; PINS],
}
impl Ioapic {
    pub(crate) fn new() -> Self {
        Self {
            id: 0,
            ioregsel: 0,
            irr: 0,
            redtbl: [MASKED; PINS],
        }
    }
    /// `ioapic_service`: delivers every pending unmasked input (level: once until its EOI).
    fn service(&mut self, bus: &mut dyn Bus) {
        for i in 0..PINS {
            let mask = 1 << i;
            let entry = self.redtbl[i];
            if self.irr & mask == 0 || entry & MASKED != 0 {
                continue;
            }
            if entry & LEVEL == 0 {
                self.irr &= !mask;
            } else {
                let coalesce = entry & REMOTE_IRR != 0;
                self.redtbl[i] |= REMOTE_IRR;
                if coalesce {
                    continue;
                }
            }
            let mode = (entry >> 8 & 7) as u8;
            let vector = if mode == 7 { bus.inta() } else { entry as u8 };
            let accepted = bus.send(Message {
                dest: (entry >> 56) as u8,
                logical: entry >> 11 & 1 != 0,
                mode,
                vector,
                level: entry & LEVEL != 0,
            });
            // 82093AA: Remote IRR "is set to 1 when local APIC(s) accept the level interrupt sent by the IOAPIC" (QEMU sets
            // it at sending; its LAPICs accept every message): with no taker the input stays pending, Remote IRR clear.
            if !accepted && entry & LEVEL != 0 {
                self.redtbl[i] &= !REMOTE_IRR;
            }
        }
    }
    /// `ioapic_set_irq`: input `pin` (0..23) changed to `level` (the board passes changes only).
    pub(crate) fn set_irq(&mut self, pin: usize, level: bool, bus: &mut dyn Bus) {
        let mask = 1 << pin;
        let entry = self.redtbl[pin];
        if entry & LEVEL != 0 {
            if level {
                self.irr |= mask;
                if entry & REMOTE_IRR == 0 {
                    self.service(bus);
                }
            } else {
                self.irr &= !mask;
            }
        } else if level && entry & MASKED == 0 {
            self.irr |= mask;
            self.service(bus);
        }
    }
    /// `ioapic_eoi_broadcast`: the end of a level-triggered `vector` (a LAPIC's EOI, or the EOI register).
    pub(crate) fn eoi_broadcast(&mut self, vector: u64, bus: &mut dyn Bus) {
        for n in 0..PINS {
            let entry = self.redtbl[n];
            if entry & 0xff != vector || entry & LEVEL == 0 || entry & REMOTE_IRR == 0 {
                continue;
            }
            self.redtbl[n] = entry & !REMOTE_IRR;
            if entry & MASKED == 0 && self.irr & 1 << n != 0 {
                self.service(bus);
            }
        }
    }
    /// `ioapic_mem_read` at page offset `offset`, `size` bytes (1, 2, 4; 8 refused).
    pub(crate) fn read(&self, offset: u64, size: u64) -> Option<u64> {
        if size == 8 {
            return None;
        }
        Some(match offset & 0xff {
            0x00 => u64::from(self.ioregsel),
            0x10 if size == 4 => match self.ioregsel {
                0 | 2 => u64::from(self.id) << 24,
                1 => VERSION | (PINS as u64 - 1) << 16,
                sel => match self.index(sel) {
                    Some(i) if sel & 1 != 0 => self.redtbl[i] >> 32,
                    Some(i) => self.redtbl[i] & 0xffff_ffff,
                    None => 0,
                },
            },
            _ => 0,
        })
    }
    fn index(&self, sel: u8) -> Option<usize> {
        (0x10..0x10 + 2 * PINS as u8)
            .contains(&sel)
            .then(|| usize::from(sel - 0x10) >> 1)
    }
    /// `ioapic_mem_write` (8 bytes refused).
    pub(crate) fn write(&mut self, offset: u64, size: u64, val: u64, bus: &mut dyn Bus) -> bool {
        if size == 8 {
            return false;
        }
        match offset & 0xff {
            0x00 => self.ioregsel = val as u8,
            0x10 if size == 4 => match self.ioregsel {
                0 => self.id = (val >> 24 & 0xf) as u8,
                1 | 2 => {}
                sel => {
                    if let Some(i) = self.index(sel) {
                        let ro = self.redtbl[i] & RO_BITS;
                        let e = &mut self.redtbl[i];
                        *e = if sel & 1 != 0 {
                            *e & 0xffff_ffff | val << 32
                        } else {
                            *e & !0xffff_ffff | val
                        };
                        *e = *e & !RO_BITS | ro;
                        if *e & LEVEL == 0 {
                            *e &= !REMOTE_IRR; // ioapic_fix_edge_remote_irr
                        }
                        self.service(bus);
                    }
                }
            },
            0x40 if size == 4 => self.eoi_broadcast(val, bus),
            _ => {}
        }
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[derive(Default)]
    struct Log(Vec<Message>);
    impl Bus for Log {
        fn send(&mut self, m: Message) -> bool {
            self.0.push(m);
            m.vector != 0x5a // a vector nobody accepts
        }
        fn inta(&mut self) -> u8 {
            0x37
        }
    }
    fn entry(a: &mut Ioapic, pin: u8, low: u64, high: u64, bus: &mut Log) {
        for (sel, v) in [(0x11 + 2 * pin, high), (0x10 + 2 * pin, low)] {
            a.write(0, 4, sel.into(), bus);
            a.write(0x10, 4, v, bus);
        }
    }
    fn read(a: &mut Ioapic, sel: u8, bus: &mut Log) -> u64 {
        a.write(0, 4, sel.into(), bus);
        a.read(0x10, 4).unwrap()
    }
    #[test]
    fn registers_edge_and_level() {
        let (mut a, mut b) = (Ioapic::new(), Log::default());
        assert_eq!(
            (
                read(&mut a, 1, &mut b),
                read(&mut a, 0x10, &mut b),
                read(&mut a, 0x11, &mut b)
            ),
            (0x17_0020, MASKED, 0)
        );
        a.write(0, 4, 0, &mut b);
        a.write(0x10, 4, 0xff00_0000, &mut b);
        assert_eq!(
            (read(&mut a, 0, &mut b), read(&mut a, 2, &mut b)),
            (0x0f00_0000, 0x0f00_0000)
        );
        assert_eq!(a.read(0x100, 1), Some(2)); // folded every 100H; IOREGSEL at any width
        // Edge, physical 1: one delivery per rise; a rise while masked is lost.
        entry(&mut a, 4, 0x41, 1 << 24, &mut b);
        a.set_irq(4, true, &mut b);
        a.set_irq(4, false, &mut b);
        assert_eq!(
            b.0,
            [Message {
                dest: 1,
                logical: false,
                mode: 0,
                vector: 0x41,
                level: false
            }]
        );
        entry(&mut a, 4, 0x41 | MASKED, 1 << 24, &mut b);
        a.set_irq(4, true, &mut b);
        entry(&mut a, 4, 0x41, 1 << 24, &mut b);
        assert_eq!(b.0.len(), 1);
        // Level, logical: Remote IRR until the EOI; redelivered while asserted; RO bits survive writes.
        a.set_irq(4, false, &mut b);
        entry(&mut a, 9, LEVEL | 1 << 11 | 0x59 | MASKED, 3 << 24, &mut b);
        a.set_irq(9, true, &mut b);
        assert_eq!(b.0.len(), 1); // masked: kept pending
        entry(&mut a, 9, LEVEL | 1 << 11 | 0x59, 3 << 24, &mut b);
        assert_eq!(
            (b.0.len(), read(&mut a, 0x22, &mut b) & REMOTE_IRR),
            (2, REMOTE_IRR)
        );
        entry(&mut a, 9, LEVEL | 1 << 11 | 0x59, 3 << 24, &mut b);
        assert_eq!(
            (b.0.len(), read(&mut a, 0x22, &mut b) & REMOTE_IRR),
            (2, REMOTE_IRR)
        );
        a.eoi_broadcast(0x59, &mut b);
        assert_eq!(
            (b.0.len(), read(&mut a, 0x22, &mut b) & REMOTE_IRR),
            (3, REMOTE_IRR)
        );
        a.set_irq(9, false, &mut b);
        a.write(0x40, 4, 0x59, &mut b); // the EOI register
        assert_eq!((b.0.len(), read(&mut a, 0x22, &mut b) & REMOTE_IRR), (3, 0));
        assert_eq!(
            b.0[2],
            Message {
                dest: 3,
                logical: true,
                mode: 0,
                vector: 0x59,
                level: true
            }
        );
        // ExtINT: the vector from the PIC.
        entry(&mut a, 0, 0x700, 0, &mut b);
        a.set_irq(0, true, &mut b);
        assert_eq!(
            b.0[3],
            Message {
                dest: 0,
                logical: false,
                mode: 7,
                vector: 0x37,
                level: false
            }
        );
        assert_eq!(a.read(0x10, 8), None);
        // A level message nobody accepted: Remote IRR stays clear, and an entry write sends it again.
        entry(&mut a, 10, LEVEL | 0x5a, 0, &mut b);
        a.set_irq(10, true, &mut b);
        assert_eq!((b.0.len(), read(&mut a, 0x24, &mut b) & REMOTE_IRR), (5, 0));
        entry(&mut a, 10, LEVEL | 0x5a, 0, &mut b);
        assert_eq!(b.0.len(), 7); // each half's write re-evaluates
    }
}
