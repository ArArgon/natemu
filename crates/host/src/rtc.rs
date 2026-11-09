//! L4b: the MC146818 RTC and its CMOS at ports 70H-71H, ISA IRQ 8, as QEMU 11.1.2's model (`hw/rtc/mc146818rtc.c`,
//! the `pc` machine's: base year 2000 so the century byte at 32H counts, lost-tick policy `discard`), timed from V.
//!
//! **Time.** The clock starts at the host's UTC time at VM creation (whole seconds, QEMU's `rtc_set_date_from_host`
//! with `-rtc base=utc`) and advances with V: its nanoseconds are `scale(V, 1e9, CNTFRQ)` and the periodic interrupt's
//! 32,768 Hz divider chain is `scale(V, 32768, CNTFRQ)`, exactly (QEMU computes both from its ns clock). Every register
//! rule is QEMU's: time and date registers in BCD or binary (B.DM) and 12/24-hour (B.24H) from `gmtime` of the seconds
//! count; SET stops updates; the divider bits (A.6:4) 110/111 hold the dividers in reset, and leaving reset starts the
//! first update half a second later; UIP set in the last 244 us before each update (and latched until it); the
//! update-ended (UF), alarm (AF, with don't-care values C0H-FFH) and periodic (PF, rate A.3:0) flags in C, IRQF and IRQ 8
//! raised when a new flag's enable bit (B.UIE/AIE/PIE) is set, or when B enables a flag already set; reading C clears
//! it and lowers IRQ 8; C and D are read-only (D = 80H); index 37H aliases the century; port 70H reads FFH and its bit 7
//! (the NMI mask on PCs) is dropped, as QEMU drops it. Other CMOS bytes are plain memory, zero at start (QEMU's
//! `pc_cmos_init` writes firmware data there: no firmware here).
//!
//! **Asynchronous source:** a [`clock::Timer`] thread runs the periodic and update timers; the device holds an
//! [`Irqs::busy`] guard while one of them can raise IRQ 8 (PIE with a rate; UIE or AIE with the update timer running).
//! **Lost ticks** (`clock::LostTicks`, default *discard*, QEMU's): a periodic interrupt that finds IRQ 8 still high (C
//! unread) or its vector pending merges; under *reinject* (QEMU's `slew`) it is owed and given back one per read of C
//! (QEMU's reinject-on-ack, without its 20-in-a-row cap and its coalesced-interrupt timer).
//! **Departure (a fix per the datasheet):** 12-hour alarm hours ([`Rtc::hour_of`]).
use crate::clock::{self, Timed};
use crate::devices::Device;
use crate::irq::{Busy, Delivery, Irqs};
use natemu_abi::clock::{count_at, scale};
use std::sync::atomic::Ordering::SeqCst;

const SEC: u64 = 1_000_000_000;
const A: usize = 10;
const B: usize = 11;
const C: usize = 12;
const D: usize = 13;
const CENTURY: usize = 0x32;
const UIP: u8 = 0x80;
const SET: u8 = 0x80;
const PIE: u8 = 0x40;
const AIE: u8 = 0x20;
const UIE: u8 = 0x10;
const DM: u8 = 0x04;
const H24: u8 = 0x02;
const IRQF: u8 = 0x80;
const PF: u8 = 0x40;
const AF: u8 = 0x20;
const UF: u8 = 0x10;
const RATE: u64 = 32768;
/// `UIP_HOLD_LENGTH` (8 cycles of the 32 kHz clock, in whole ns).
const UIP_HOLD: u64 = 8 * SEC / RATE;

pub(crate) struct Rtc {
    cmos: [u8; 128],
    index: u8,
    /// Seconds since the epoch at `last_update`, the ns clock then, and the sub-second offset (QEMU's fields).
    base: i64,
    last_update: u64,
    offset: u64,
    frequency: u64,
    /// The periodic timer: its period in 32 kHz cycles (0: off) and its next 32 kHz edge.
    period: u64,
    next_periodic: Option<u64>,
    /// The update timer's next time (ns clock) and the alarm's.
    next_update: Option<u64>,
    next_alarm: u64,
    irqs: Irqs,
    busy: Option<Busy>,
    /// The periodic interrupt's lost-tick policy (reinject: true), its periods still to deliver, and the report.
    reinject: bool,
    backlog: u64,
    pub(crate) stats: std::sync::Arc<crate::clock::TickStats>,
}

/// QEMU's `mktimegm` (arithmetic on out-of-range fields included): seconds since the epoch.
fn mktimegm(year: i64, month: i64, day: i64, hour: i64, min: i64, sec: i64) -> i64 {
    let (mut y, mut m) = (year, month);
    if m < 3 {
        m += 12;
        y -= 1;
    }
    86400
        * (day + (153 * m - 457) / 5 + 365 * y + y.div_euclid(4) - y.div_euclid(100)
            + y.div_euclid(400)
            - 719469)
        + 3600 * hour
        + 60 * min
        + sec
}
/// `gmtime`: (year, month 1..12, day, hour, min, sec, weekday 0 = Sunday).
fn gmtime(t: i64) -> (i64, i64, i64, i64, i64, i64, i64) {
    let (days, s) = (t.div_euclid(86400), t.rem_euclid(86400));
    // Civil from days (H. Hinnant's algorithm).
    let z = days + 719468;
    let era = z.div_euclid(146097);
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    (
        year,
        month,
        day,
        s / 3600,
        s / 60 % 60,
        s % 60,
        (days + 4).rem_euclid(7),
    )
}

impl Rtc {
    /// At VM creation: QEMU's realize values (A 26H, B 02H, C 0, D 80H) and the host's time.
    pub(crate) fn new(irqs: &Irqs, frequency: u64, reinject: bool) -> Self {
        let mut r = Self {
            cmos: [0; 128],
            index: 0,
            base: 0,
            last_update: 0,
            offset: 0,
            frequency,
            period: 0,
            next_periodic: None,
            next_update: None,
            next_alarm: 0,
            irqs: irqs.clone(),
            busy: None,
            reinject,
            backlog: 0,
            stats: Default::default(),
        };
        (r.cmos[A], r.cmos[B], r.cmos[C], r.cmos[D]) = (0x26, 0x02, 0, 0x80);
        let host = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| d.as_secs() as i64);
        (r.base, r.last_update) = (host, r.ns());
        r.set_cmos(host);
        r.check_update_timer();
        r
    }
    /// The ns clock now.
    fn ns(&self) -> u64 {
        scale(clock::now(), SEC, self.frequency)
    }
    fn running(&self) -> bool {
        self.cmos[B] & SET == 0 && self.cmos[A] & 0x70 <= 0x20
    }
    fn since_update(&self, t: u64) -> u64 {
        t - self.last_update + self.offset
    }
    fn to_bcd(&self, a: i64) -> u8 {
        if self.cmos[B] & DM != 0 {
            a as u8
        } else {
            ((a / 10) << 4 | a % 10) as u8
        }
    }
    fn from_bcd(&self, a: u8) -> i64 {
        match a {
            _ if a & 0xc0 == 0xc0 => -1,
            _ if self.cmos[B] & DM != 0 => a.into(),
            _ => i64::from(a >> 4) * 10 + i64::from(a & 15),
        }
    }
    fn set_cmos(&mut self, t: i64) {
        let (year, month, day, hour, min, sec, wday) = gmtime(t);
        self.cmos[0] = self.to_bcd(sec);
        self.cmos[2] = self.to_bcd(min);
        self.cmos[4] = if self.cmos[B] & H24 != 0 {
            self.to_bcd(hour)
        } else {
            let h = if hour % 12 != 0 { hour % 12 } else { 12 };
            self.to_bcd(h) | if hour >= 12 { 0x80 } else { 0 }
        };
        self.cmos[6] = self.to_bcd(wday + 1);
        self.cmos[7] = self.to_bcd(day);
        self.cmos[8] = self.to_bcd(month);
        self.cmos[9] = self.to_bcd(year % 100);
        self.cmos[CENTURY] = self.to_bcd(year / 100);
    }
    /// `rtc_get_time` + `mktimegm`.
    fn get_time(&self) -> i64 {
        let mut hour = self.from_bcd(self.cmos[4] & 0x7f);
        if self.cmos[B] & H24 == 0 {
            hour %= 12;
            if self.cmos[4] & 0x80 != 0 {
                hour += 12;
            }
        }
        let year = self.from_bcd(self.cmos[9]) + self.from_bcd(self.cmos[CENTURY]) * 100;
        mktimegm(
            year,
            self.from_bcd(self.cmos[8]),
            self.from_bcd(self.cmos[7]),
            hour,
            self.from_bcd(self.cmos[2]),
            self.from_bcd(self.cmos[0]),
        )
    }
    fn set_time(&mut self, t: u64) {
        (self.base, self.last_update) = (self.get_time(), t);
    }
    fn update_time(&mut self, t: u64) {
        if self.cmos[B] & SET == 0 {
            self.set_cmos(self.base + (self.since_update(t) / SEC) as i64);
        }
    }
    /// An hours register (time or alarm) as 0..23, or -1 for an alarm's don't-care value (C0H-FFH). In 12-hour mode
    /// bit 7 is that register's own PM flag and bits 6:0 the hour 1-12 (MC146818 datasheet, Table 3). **Departure:**
    /// QEMU (`convert_hour`, `get_next_alarm`) decodes the hour with bit 7 still set and adds 12 by the *time*
    /// register's PM flag for the alarm too, so a 12-hour alarm in the other half of the day, or at 8-12 PM, fires at
    /// the wrong time.
    fn hour_of(&self, a: u8) -> i64 {
        match a {
            _ if a & 0xc0 == 0xc0 => -1,
            _ if self.cmos[B] & H24 == 0 => {
                self.from_bcd(a & 0x7f) % 12 + if a & 0x80 != 0 { 12 } else { 0 }
            }
            _ => self.from_bcd(a),
        }
    }
    /// `get_next_alarm`: seconds to the next alarm (1..=86400).
    fn next_alarm_sec(&mut self, t: u64) -> i64 {
        self.update_time(t);
        let (mut asec, mut amin) = (self.from_bcd(self.cmos[1]), self.from_bcd(self.cmos[3]));
        let mut ahour = self.hour_of(self.cmos[5]);
        let (csec, cmin) = (self.from_bcd(self.cmos[0]), self.from_bcd(self.cmos[2]));
        let chour = self.hour_of(self.cmos[4]);
        if ahour == -1 {
            ahour = chour;
            if amin == -1 {
                amin = cmin;
                if asec == -1 {
                    asec = csec + 1;
                } else if csec > asec {
                    amin += 1;
                }
            } else if cmin == amin {
                if asec == -1 {
                    asec = csec + 1;
                } else if csec > asec {
                    ahour += 1;
                }
                if asec == 60 {
                    asec = 0;
                    ahour += 1;
                }
            } else if cmin > amin {
                ahour += 1;
            }
        } else if chour == ahour {
            if amin == -1 {
                amin = cmin;
                if asec == -1 {
                    asec = csec + 1;
                } else if csec > asec {
                    amin += 1;
                }
                if asec == 60 {
                    asec = 0;
                    amin += 1;
                }
                amin %= 60;
            } else if cmin == amin {
                if asec == -1 {
                    asec = csec + 1;
                }
                asec %= 60;
            }
        }
        if amin == -1 {
            amin = 0;
        }
        if asec == -1 {
            asec = 0;
        }
        if asec == 60 {
            asec = 0;
            amin += 1;
        }
        if amin == 60 {
            amin = 0;
            ahour += 1;
        }
        ahour %= 24;
        let sec = ((ahour - chour) * 60 + amin - cmin) * 60 + asec - csec;
        if sec <= 0 { sec + 86400 } else { sec }
    }
    /// `check_update_timer`.
    fn check_update_timer(&mut self) {
        if self.cmos[A] & 0x60 == 0x60 {
            self.next_update = None;
            return self.guard();
        }
        let t = self.ns();
        let mut next = t + SEC - self.since_update(t) % SEC;
        let alarm = self.next_alarm_sec(t);
        self.next_alarm = (next as i64 + (alarm - 1) * SEC as i64) as u64;
        if self.cmos[A] & UIP == 0 && self.cmos[C] & UF != 0 {
            if self.cmos[B] & SET != 0 || self.cmos[C] & AF != 0 {
                self.next_update = None;
                return self.guard();
            }
            next = self.next_alarm;
        }
        self.next_update = Some(next);
        self.guard();
    }
    /// `rtc_update_timer` at ns clock `t`.
    fn update_timer(&mut self, t: u64) {
        self.update_time(t);
        self.cmos[A] &= !UIP;
        let mut irqs = UF;
        if t >= self.next_alarm {
            irqs |= AF;
        }
        let new = irqs & !self.cmos[C];
        self.cmos[C] |= irqs;
        if new & self.cmos[B] != 0 {
            self.cmos[C] |= IRQF;
            self.irqs.line(8, true);
        }
        self.check_update_timer();
    }
    /// The periodic rate in 32 kHz cycles (`rtc_periodic_clock_ticks`): 0 without PIE or with rate 0.
    fn periodic_ticks(&self) -> u64 {
        let code = u32::from(self.cmos[A] & 15);
        match code {
            _ if self.cmos[B] & PIE == 0 || code == 0 => 0,
            1 | 2 => 1 << (code + 6),
            _ => 1 << (code - 1),
        }
    }
    /// `periodic_timer_update` (lost-tick policy `discard`) at 32 kHz edge `edge`.
    fn periodic_update(&mut self, edge: u64, old_period: u64, period_change: bool) {
        self.period = self.periodic_ticks();
        if self.period == 0 {
            self.next_periodic = None;
            return self.guard();
        }
        let lost = match self.next_periodic {
            Some(next) if old_period != 0 && period_change => {
                edge.saturating_sub(next.saturating_sub(old_period))
            }
            _ => 0,
        };
        self.next_periodic = Some(edge + self.period - lost.min(self.period));
        self.guard();
    }
    fn edge(&self) -> u64 {
        scale(clock::now(), RATE, self.frequency)
    }
    /// `update_in_progress` at ns clock `t`.
    fn update_in_progress(&mut self, t: u64) -> bool {
        if !self.running() {
            return false;
        }
        if self
            .next_update
            .is_some_and(|n| t >= n.saturating_sub(UIP_HOLD))
        {
            self.cmos[A] |= UIP;
            return true;
        }
        self.since_update(t) % SEC >= SEC - UIP_HOLD
    }
    /// Holds the guard while a timer can raise IRQ 8.
    fn guard(&mut self) {
        let fires = self.next_periodic.is_some()
            || self.next_update.is_some() && self.cmos[B] & (UIE | AIE) != 0;
        self.busy = if fires {
            self.busy.take().or_else(|| Some(self.irqs.busy()))
        } else {
            None
        };
    }
    fn write_data(&mut self, data: u8) {
        let i = usize::from(self.index);
        match i {
            1 | 3 | 5 => {
                self.cmos[i] = data;
                self.check_update_timer();
            }
            0x37 | CENTURY | 0 | 2 | 4 | 6..=9 => {
                if i == 0x37 {
                    self.index = CENTURY as u8;
                }
                self.cmos[usize::from(self.index)] = data;
                if self.running() {
                    self.set_time(self.ns());
                    self.check_update_timer();
                }
            }
            A => {
                let update_periodic = (self.cmos[A] ^ data) & 15 != 0;
                let old_period = self.periodic_ticks();
                let t = self.ns();
                if data & 0x60 == 0x60 {
                    if self.running() {
                        self.update_time(t);
                    }
                    self.cmos[A] &= !UIP;
                } else if self.cmos[A] & 0x60 == 0x60 && data & 0x70 <= 0x20 {
                    // Leaving divider reset: the first update cycle begins half a second later.
                    if self.cmos[B] & SET == 0 {
                        self.offset = SEC / 2;
                        self.set_time(t);
                    }
                    self.cmos[A] &= !UIP;
                }
                self.cmos[A] = data & !UIP | self.cmos[A] & UIP;
                if update_periodic {
                    self.periodic_update(self.edge(), old_period, true);
                }
                self.check_update_timer();
            }
            B => {
                let mut data = data;
                let update_periodic = (self.cmos[B] ^ data) & PIE != 0;
                let old_period = self.periodic_ticks();
                let t = self.ns();
                if data & SET != 0 {
                    if self.running() {
                        self.update_time(t);
                    }
                    self.cmos[A] &= !UIP;
                    data &= !UIE;
                } else if self.cmos[B] & SET != 0 && self.cmos[A] & 0x70 <= 0x20 {
                    self.offset = self.since_update(t) % SEC;
                    self.set_time(t);
                }
                // A flag already set when its interrupt becomes enabled raises at once.
                if data & self.cmos[C] & 0x70 != 0 {
                    self.cmos[C] |= IRQF;
                    self.irqs.line(8, true);
                } else {
                    self.cmos[C] &= !IRQF;
                    self.irqs.line(8, false);
                }
                self.cmos[B] = data;
                if update_periodic {
                    self.periodic_update(self.edge(), old_period, true);
                }
                self.check_update_timer();
            }
            C | D => {}
            _ => self.cmos[i] = data,
        }
    }
    fn read_data(&mut self) -> u8 {
        if self.index == 0x37 {
            self.index = CENTURY as u8;
        }
        let i = usize::from(self.index);
        let t = self.ns();
        match i {
            CENTURY | 0 | 2 | 4 | 6..=9 => {
                if self.running() {
                    self.update_time(t);
                }
                self.cmos[i]
            }
            A => self.cmos[A] | if self.update_in_progress(t) { UIP } else { 0 },
            C => {
                let ret = self.cmos[C];
                self.irqs.line(8, false);
                self.cmos[C] = 0;
                if ret & (UF | AF) != 0 {
                    self.check_update_timer();
                }
                // Reinject on acknowledge (QEMU's `slew` policy, `cmos_ioport_read` C): one owed period per read of C.
                if self.backlog > 0 && self.cmos[B] & PIE != 0 {
                    self.cmos[C] |= IRQF | PF;
                    match self.irqs.line(8, true) {
                        Delivery::Fresh => {
                            self.backlog -= 1;
                            self.stats.reinjected.fetch_add(1, SeqCst);
                        }
                        Delivery::Coalesced => {}
                        Delivery::None => self.backlog = 0,
                    }
                }
                ret
            }
            _ => self.cmos[i],
        }
    }
}
impl Device for Rtc {
    // Byte registers (QEMU's `max_access_size` 1): a wider access is its bytes, lowest first.
    fn read(&mut self, _vcpu: usize, offset: u64, size: u64) -> Option<u64> {
        Some((0..size).fold(0, |v, k| {
            v | u64::from(if (offset + k) & 1 == 0 {
                0xff
            } else {
                self.read_data()
            }) << (8 * k)
        }))
    }
    fn write(&mut self, _vcpu: usize, offset: u64, size: u64, value: u64) -> bool {
        for k in 0..size {
            let b = (value >> (8 * k)) as u8;
            if (offset + k) & 1 == 0 {
                self.index = b & 0x7f
            } else {
                self.write_data(b)
            }
        }
        true
    }
}
impl Timed for Rtc {
    fn next(&self) -> Option<u64> {
        let periodic = self
            .next_periodic
            .map(|e| count_at(e, RATE, self.frequency));
        let update = self.next_update.map(|n| count_at(n, SEC, self.frequency));
        periodic.into_iter().chain(update).min()
    }
    fn fire(&mut self, at: u64) {
        if let Some(edge) = self
            .next_periodic
            .filter(|&e| count_at(e, RATE, self.frequency) <= at)
        {
            // `rtc_periodic_timer`; a period whose interrupt is still unread in C (IRQ 8 high), or merged in an IRR, is
            // missed: owed under reinject (QEMU's `slew`: `irq_coalesced`), else lost.
            self.periodic_update(edge, self.period, false);
            self.cmos[C] |= PF;
            if self.cmos[B] & PIE != 0 {
                self.stats.rises.fetch_add(1, SeqCst);
                let d = if self.cmos[C] & IRQF != 0 {
                    Delivery::Coalesced
                } else {
                    Delivery::None
                };
                self.cmos[C] |= IRQF;
                let d = if d == Delivery::Coalesced {
                    d
                } else {
                    self.irqs.line(8, true)
                };
                [
                    &self.stats.fresh,
                    &self.stats.coalesced,
                    &self.stats.dropped,
                ][d as usize]
                    .fetch_add(1, SeqCst);
                match d {
                    Delivery::Coalesced => {
                        self.stats.missed.fetch_add(1, SeqCst);
                        self.backlog += u64::from(self.reinject);
                    }
                    Delivery::None => self.backlog = 0,
                    Delivery::Fresh => {}
                }
            }
        }
        if let Some(n) = self
            .next_update
            .filter(|&n| count_at(n, SEC, self.frequency) <= at)
        {
            self.update_timer(n.max(scale(at, SEC, self.frequency)));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn calendar() {
        for t in [
            0i64,
            951_782_400,
            1_761_782_399,
            4_102_444_800,
            1_709_208_000,
        ] {
            let (y, mo, d, h, mi, s, _) = gmtime(t);
            assert_eq!(mktimegm(y, mo, d, h, mi, s), t, "{t}");
        }
        // 2025-10-29 23:59:59 UTC, a Wednesday; 2000-02-29, a Tuesday (Python's datetime).
        assert_eq!(gmtime(1_761_782_399), (2025, 10, 29, 23, 59, 59, 3));
        assert_eq!(gmtime(951_782_400), (2000, 2, 29, 0, 0, 0, 2));
    }
}
