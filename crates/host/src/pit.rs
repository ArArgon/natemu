//! L4b: the 8254 PIT at ports 40H-43H and port 61H (channel 2's gate, the speaker data bit, OUT2 and the refresh toggle),
//! as QEMU 11.1.2's models (`hw/timer/i8254.c`, `i8254_common.c`, `hw/audio/pcspk.c`), timed from V.
//!
//! **Time.** The counters count the PIT clock, `clock::PIT_HZ` edges at V = `scale(V, PIT_HZ, CNTFRQ)`: a count loaded at
//! V has run d = edges(now) - edges(V) ticks (QEMU's `muldiv64(now - count_load_time, PIT_FREQ, 1e9)` in ns), and an
//! event d ticks after the load is at the first V reaching that edge (`clock::count_at`), exactly.
//!
//! **Registers** (QEMU's): the count (`pit_get_count`: modes 0/1/4/5 count - d mod 2^16, mode 3 count - 2d mod count,
//! mode 2 count - d mod count), OUT (`pit_get_out`), the control word (counter latch, read-back with count and status
//! latches, access LSB/MSB/word, mode and BCD stored), the LSB/MSB read and write toggles, count 0 = 10000H; gate
//! rising edges restart modes 1, 2, 3 and 5. As QEMU: counting is binary (BCD is stored and read back only), the
//! status has no NULL COUNT bit, a control word changes the mode but neither restarts the count nor updates OUT's
//! interrupt (the next scheduled transition sees the new mode), and the gate does not stop modes 0 and 4.
//!
//! **Channel 0** drives ISA IRQ 0 (I/O APIC pin 2, PIC input 0) at the level QEMU's interrupt timer gives it
//! (`pit_irq_timer_update`, see [`Channel::line`]): set at a count load or gate change, and followed up to now at each
//! event and before every access (`clock::Timer`), every change due taken at once: rising edges that passed together make
//! one edge (they would merge in an IRR); under the lost-tick policy *reinject* (`clock::LostTicks`, the default for the
//! PIT, KVM's) each edge that merged, here or into a still pending IRR bit, is owed and delivered later, one per
//! acceptance (a retry each quarter count until the chipset reports a fresh acceptance); a count load drops the debt, as
//! does a rise nothing takes (masked), as KVM's PIT does. It holds an [`Irqs::busy`] guard while a transition
//! is scheduled. **Departures from QEMU:** at reset no channel counts until its first count is written (QEMU's reset
//! programs every channel to mode 3, 10000H, running, as firmware would: this machine has none, and an 8254's state is
//! undefined until programmed), so IRQ0 is quiet until the guest programs channel 0; modes 6 and 7 behave as 2 and 3
//! (8254 datasheet: M2..M0 = X10, X11; QEMU computes OUT for them as mode 0).
//!
//! **Port 61H** (`pcspk_io_read`/`write`): reads gate 2 (bit 0), the speaker data bit (1), the refresh clock (bit 4,
//! toggled by every read) and OUT2 (bit 5); writes set gate 2 and the data bit.
use crate::clock::{self, Timed};
use crate::devices::Device;
use crate::irq::{Busy, Delivery, Irqs};
use natemu_abi::clock::{PIT_HZ, count_at, scale};
use std::sync::atomic::Ordering::SeqCst;

const MSB: u8 = 2;
const WORD0: u8 = 3;
const WORD1: u8 = 4;

#[derive(Clone, Copy, Default)]
struct Channel {
    /// The count loaded (1..=10000H).
    count: u64,
    /// The PIT edge it was loaded at; `None` until the first count write (it does not count).
    load: Option<u64>,
    latched_count: u16,
    count_latched: u8,
    status_latched: bool,
    status: u8,
    read_state: u8,
    write_state: u8,
    write_latch: u8,
    rw_mode: u8,
    mode: u8,
    bcd: u8,
    gate: bool,
}
impl Channel {
    /// QEMU's `pit_reset_common` registers.
    fn reset(gate: bool) -> Self {
        Self {
            count: 0x10000,
            mode: 3,
            gate,
            ..Default::default()
        }
    }
    /// The counting mode (6 and 7 are 2 and 3).
    fn mode(&self) -> u8 {
        if self.mode >= 6 {
            self.mode - 4
        } else {
            self.mode
        }
    }
    /// Ticks since the load at PIT edge `edge`.
    fn d(&self, edge: u64) -> u64 {
        self.load.map_or(0, |l| edge - l)
    }
    fn count_at(&self, edge: u64) -> u64 {
        let (c, d) = (self.count, self.d(edge));
        match self.mode() {
            3 => c - (2 * d) % c,
            2 => c - d % c,
            _ => c.wrapping_sub(d) & 0xffff,
        }
    }
    /// `pit_get_out`.
    fn out(&self, edge: u64) -> bool {
        let (c, d) = (self.count, self.d(edge));
        match self.mode() {
            2 => d % c == 0 && d != 0,
            3 => d % c < (c + 1) >> 1,
            4 | 5 => d == c,
            _ => d >= c,
        }
    }
    /// IRQ0's level: OUT as QEMU's interrupt timer drives it. QEMU re-evaluates OUT only at the transitions it
    /// schedules, and its ns round trip (`muldiv64` both ways, floored) evaluates a transition scheduled at tick k at tick
    /// k - 1; the `next_time <= current_time` fix then moves equal times 1 ns on. The level that results is `out` except
    /// in mode 2, where it is low only at d = c - 1 (mod c), from d = 2c - 1 on, and high from d = c (rising edges at
    /// every multiple of c, the rate generator's period, the 8254 datasheet's OUT pulse).
    fn line(&self, edge: u64) -> bool {
        let (c, d) = (self.count, self.d(edge));
        match self.mode() {
            2 => d >= c && !(c > 1 && d % c == c - 1),
            _ => self.out(edge),
        }
    }
    /// The rising edges of [`Channel::line`] in (dp, dn] ticks after the load.
    fn rises(&self, dp: u64, dn: u64) -> u64 {
        let c = self.count;
        match self.mode() {
            2 | 3 if c > 1 => dn / c - dp / c,
            3 => 0,
            2 => u64::from(dp < 1 && dn >= 1),
            _ => u64::from(dp < c && dn >= c),
        }
    }
    /// The next PIT edge at which [`Channel::line`] changes, if any.
    fn next_change(&self, edge: u64) -> Option<u64> {
        let (c, d) = (self.count, self.d(edge));
        let base = d - d % c;
        let next = match self.mode() {
            2 if d < c => c,
            2 if c > 1 && d % c == c - 1 => base + c,
            2 if c > 1 => base + c - 1,
            3 if c > 1 && d % c < (c + 1) >> 1 => base + ((c + 1) >> 1),
            3 if c > 1 => base + c,
            4 | 5 if d < c => c,
            4 | 5 if d == c => c + 1,
            0 | 1 if d < c => c,
            _ => return None,
        };
        Some(self.load? + next)
    }
}

/// The PIT and port 61H. Registered at ports 40H-43H (offsets 0-3) and 61H (offset 0x21) through a [`clock::Timer`].
pub(crate) struct Pit {
    channels: [Channel; 3],
    /// V's rate.
    frequency: u64,
    /// Channel 0: IRQ0's level as last set, the PIT edge up to which it has been followed, its next change, and the
    /// guard held while a change or a reinjection is to come.
    level: bool,
    done: u64,
    next: Option<u64>,
    busy: Option<Busy>,
    irqs: Irqs,
    /// The lost-tick policy (reinject: true), its rising edges still to deliver, and the PIT edge of the next attempt.
    reinject: bool,
    backlog: u64,
    retry: Option<u64>,
    /// IRQ0's rising edges and what became of them (the run report).
    pub(crate) stats: std::sync::Arc<clock::TickStats>,
    /// Port 61H: the speaker data bit and the refresh clock bit.
    data_on: bool,
    refresh: u8,
}
/// Port 61H's offset in the device (40H-43H are 0-3).
pub(crate) const PORT_61: u64 = 0x21;
impl Pit {
    pub(crate) fn new(irqs: &Irqs, frequency: u64, reinject: bool) -> Self {
        let channels = [
            Channel::reset(true),
            Channel::reset(true),
            Channel::reset(false),
        ];
        Self {
            channels,
            frequency,
            level: false,
            done: 0,
            next: None,
            busy: None,
            irqs: irqs.clone(),
            reinject,
            backlog: 0,
            retry: None,
            stats: Default::default(),
            data_on: false,
            refresh: 0,
        }
    }
    fn edge(&self) -> u64 {
        scale(clock::now(), PIT_HZ, self.frequency)
    }
    fn set_line(&mut self, level: bool) -> Delivery {
        self.level = level;
        self.irqs.line(0, level)
    }
    /// A rising edge on IRQ0 now (from low, whatever its level), then its level back.
    fn pulse(&mut self, level: bool) -> Delivery {
        self.set_line(false);
        let d = self.set_line(true);
        self.set_line(level);
        d
    }
    fn guard(&mut self) {
        let armed = self.next.is_some() || self.retry.is_some();
        self.busy = if armed {
            self.busy.take().or_else(|| Some(self.irqs.busy()))
        } else {
            None
        };
    }
    /// `pit_irq_timer_update` for channel 0 at a count load or gate restart (PIT edge `edge`): OUT onto IRQ0, the next
    /// change scheduled; a load drops the backlog (KVM resets its pending count when the timer is reprogrammed).
    fn irq_update(&mut self, edge: u64) {
        let c = self.channels[0];
        if c.load.is_none() {
            return;
        }
        self.set_line(c.line(edge));
        (self.done, self.next, self.backlog, self.retry) = (edge, c.next_change(edge), 0, None);
        self.guard();
    }
    /// Follows channel 0 up to PIT edge `now`: every change of IRQ0's level due by then at once. Rising edges that
    /// passed together make one edge on the line (they would merge in the IRR anyway); with the reinject policy each
    /// edge that merged, here or in a pending IRR, is delivered later, one per acceptance ([`clock::LostTicks`]).
    fn catch_up(&mut self, now: u64) {
        let c = self.channels[0];
        let Some(load) = c.load else { return };
        if self.next.is_some_and(|n| n <= now) {
            let rises = c.rises(self.done - load, now - load);
            let level = c.line(now);
            if rises > 0 {
                let d = self.pulse(level);
                self.account(rises, d, now);
            } else {
                self.set_line(level);
            }
            (self.done, self.next) = (now, c.next_change(now));
        }
        if self.retry.is_some_and(|r| r <= now) {
            let level = c.line(now);
            let d = self.pulse(level);
            self.stats
                .reinjected
                .fetch_add(u64::from(d == Delivery::Fresh), SeqCst);
            match d {
                Delivery::Fresh => self.backlog -= 1,
                Delivery::Coalesced => {}
                Delivery::None => self.backlog = 0,
            }
            self.retry = (self.backlog > 0).then(|| now + (c.count / 4).max(1));
        }
        self.guard();
    }
    /// `rises` rising edges were made one (delivered as `d`): the ones that merged join the backlog under reinject.
    fn account(&mut self, rises: u64, d: Delivery, now: u64) {
        let missed = rises - 1 + u64::from(d == Delivery::Coalesced);
        let st = &self.stats;
        st.rises.fetch_add(rises, SeqCst);
        st.missed.fetch_add(missed, SeqCst);
        [&st.fresh, &st.coalesced, &st.dropped][d as usize].fetch_add(1, SeqCst);
        if !self.reinject {
            return;
        }
        // Nothing took it (masked, no destination): nothing is owed (KVM's mask notifier drops the pending count).
        self.backlog = if d == Delivery::None {
            0
        } else {
            self.backlog + missed
        };
        if self.backlog > 0 && self.retry.is_none() {
            self.retry = Some(now + (self.channels[0].count / 4).max(1));
        }
    }
    fn load(&mut self, ch: usize, value: u64) {
        let edge = self.edge();
        let c = &mut self.channels[ch];
        (c.count, c.load) = (if value == 0 { 0x10000 } else { value }, Some(edge));
        if ch == 0 {
            self.irq_update(edge);
        }
    }
    fn latch(&mut self, ch: usize, edge: u64) {
        let c = &mut self.channels[ch];
        if c.count_latched == 0 {
            c.latched_count = c.count_at(edge) as u16;
            c.count_latched = c.rw_mode;
        }
    }
    fn set_gate(&mut self, ch: usize, gate: bool) {
        let c = &mut self.channels[ch];
        if matches!(c.mode(), 1 | 2 | 3 | 5) && !c.gate && gate && c.load.is_some() {
            let edge = self.edge();
            self.channels[ch].load = Some(edge);
            if ch == 0 {
                self.irq_update(edge);
            }
        }
        self.channels[ch].gate = gate;
    }
    fn write_byte(&mut self, port: u64, val: u8) {
        if port == 3 {
            let edge = self.edge();
            let channel = val >> 6;
            if channel == 3 {
                // Read-back: bit 5 clear latches the counts, bit 4 clear the statuses, of the channels in bits 3:1.
                for ch in 0..3 {
                    if val & 2 << ch == 0 {
                        continue;
                    }
                    if val & 0x20 == 0 {
                        self.latch(ch, edge);
                    }
                    let c = &mut self.channels[ch];
                    if val & 0x10 == 0 && !c.status_latched {
                        c.status =
                            u8::from(c.out(edge)) << 7 | c.rw_mode << 4 | c.mode << 1 | c.bcd;
                        c.status_latched = true;
                    }
                }
                return;
            }
            let ch = usize::from(channel);
            let access = val >> 4 & 3;
            if access == 0 {
                return self.latch(ch, edge);
            }
            let c = &mut self.channels[ch];
            (c.rw_mode, c.read_state, c.write_state) = (access, access, access);
            (c.mode, c.bcd) = (val >> 1 & 7, val & 1);
            return;
        }
        let ch = port as usize;
        let c = &mut self.channels[ch];
        match c.write_state {
            MSB => self.load(ch, u64::from(val) << 8),
            WORD0 => (c.write_latch, c.write_state) = (val, WORD1),
            WORD1 => {
                let v = u64::from(c.write_latch) | u64::from(val) << 8;
                c.write_state = WORD0;
                self.load(ch, v);
            }
            // LSB, and the reset state 0 (QEMU's `default`).
            _ => self.load(ch, u64::from(val)),
        }
    }
    fn read_byte(&mut self, port: u64) -> u8 {
        if port == 3 {
            return 0; // the control word register is write-only (QEMU reads 0)
        }
        let edge = self.edge();
        let c = &mut self.channels[port as usize];
        if c.status_latched {
            c.status_latched = false;
            return c.status;
        }
        if c.count_latched != 0 {
            let l = c.latched_count;
            return match c.count_latched {
                MSB => {
                    c.count_latched = 0;
                    (l >> 8) as u8
                }
                WORD0 => {
                    c.count_latched = MSB;
                    l as u8
                }
                _ => {
                    c.count_latched = 0;
                    l as u8
                }
            };
        }
        let count = c.count_at(edge);
        match c.read_state {
            MSB => (count >> 8) as u8,
            WORD0 => {
                c.read_state = WORD1;
                count as u8
            }
            WORD1 => {
                c.read_state = WORD0;
                (count >> 8) as u8
            }
            _ => count as u8,
        }
    }
    /// Port 61H.
    fn port61(&mut self, write: Option<u8>) -> u8 {
        match write {
            Some(v) => {
                self.data_on = v & 2 != 0;
                self.set_gate(2, v & 1 != 0);
                0
            }
            None => {
                self.refresh ^= 1 << 4;
                let c = &self.channels[2];
                u8::from(c.gate)
                    | u8::from(self.data_on) << 1
                    | self.refresh
                    | u8::from(c.out(self.edge())) << 5
            }
        }
    }
}
impl Device for Pit {
    // Byte registers (QEMU's `max_access_size` 1): a wider access is its bytes, lowest first.
    fn read(&mut self, _vcpu: usize, offset: u64, size: u64) -> Option<u64> {
        let byte = |p: &mut Self, k: u64| {
            if offset == PORT_61 {
                p.port61(None)
            } else {
                p.read_byte((offset + k) & 3)
            }
        };
        Some((0..size).fold(0, |v, k| v | u64::from(byte(self, k)) << (8 * k)))
    }
    fn write(&mut self, _vcpu: usize, offset: u64, size: u64, value: u64) -> bool {
        for k in 0..size {
            let b = (value >> (8 * k)) as u8;
            if offset == PORT_61 {
                self.port61(Some(b));
            } else {
                self.write_byte((offset + k) & 3, b);
            }
        }
        true
    }
}
impl Timed for Pit {
    fn next(&self) -> Option<u64> {
        self.next
            .into_iter()
            .chain(self.retry)
            .min()
            .map(|e| count_at(e, PIT_HZ, self.frequency))
    }
    fn fire(&mut self, _at: u64) {
        self.catch_up(self.edge());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    /// QEMU's formulas (count, OUT) and IRQ0's level and changes, per mode, at a few points (load at edge 100).
    #[test]
    fn counts_out_and_changes() {
        let c = |mode, count| Channel {
            mode,
            count,
            load: Some(100),
            ..Default::default()
        };
        // The level from `next_change` must equal `line` everywhere: walk each mode's changes over 6 periods.
        for ch in [
            c(0, 7),
            c(2, 10),
            c(2, 2),
            c(3, 10),
            c(3, 7),
            c(3, 2),
            c(4, 5),
            c(2, 1),
            c(3, 1),
        ] {
            let (mut e, mut level) = (100, ch.line(100));
            while let Some(n) = ch.next_change(e).filter(|&n| n < 160) {
                assert!(
                    n > e && (e + 1..n).all(|k| ch.line(k) == level) && ch.line(n) != level,
                    "mode {} count {} at {n}",
                    ch.mode,
                    ch.count
                );
                (e, level) = (n, !level);
            }
            assert!(
                (e + 1..160).all(|k| ch.line(k) == level),
                "mode {} count {}: a change missed after {e}",
                ch.mode,
                ch.count
            );
        }
        let m2 = c(2, 10);
        assert_eq!(
            [100, 101, 109, 110, 111].map(|e| m2.count_at(e)),
            [10, 9, 1, 10, 9]
        );
        assert_eq!(
            [100, 110, 111, 120].map(|e| m2.out(e)),
            [false, true, false, true]
        );
        // Rising edges every 10 ticks from d = 10; low at d = 19, 29, ...
        assert_eq!(
            [109, 110, 118, 119, 120].map(|e| m2.line(e)),
            [false, true, true, false, true]
        );
        let m3 = c(3, 10);
        assert_eq!([100, 101, 104, 105].map(|e| m3.count_at(e)), [10, 8, 2, 10]);
        assert_eq!(
            [100, 104, 105, 109, 110].map(|e| m3.out(e)),
            [true, true, false, false, true]
        );
        assert_eq!(
            [100, 105, 110].map(|e| m3.next_change(e)),
            [Some(105), Some(110), Some(115)]
        );
        let m0 = c(0, 0x10000);
        assert_eq!(
            (
                m0.count_at(100),
                m0.count_at(101),
                m0.out(100 + 0xffff),
                m0.out(100 + 0x10000)
            ),
            (0, 0xffff, false, true)
        );
        assert_eq!(
            (m0.next_change(100), m0.next_change(100 + 0x10000)),
            (Some(100 + 0x10000), None)
        );
        let m4 = c(4, 5);
        assert_eq!([104, 105, 106].map(|e| m4.out(e)), [false, true, false]);
        assert_eq!(
            [100, 105, 106].map(|e| m4.next_change(e)),
            [Some(105), Some(106), None]
        );
        // Never loaded: frozen, no change.
        let idle = Channel::reset(true);
        assert_eq!(
            (idle.count_at(5000) & 0xffff, idle.next_change(5000)),
            (0, None)
        );
    }
}
