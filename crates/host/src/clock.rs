//! L5, the host's side of the clock service: V (the Arm virtual count, which every vCPU reads with offset 0, is the host's
//! `mach_absolute_time`), its rate, and the timer threads of the asynchronous devices (PIT, RTC, PM and UART transmit readiness).
//!
//! **Time.** Every device clock is an exact rational function of V (`natemu_abi::clock::scale`, floors; its inverse
//! `count_at` gives the V of an edge), as the resident's TSC and LAPIC timer are: no clock can drift from another. The
//! host's V rate is that of `mach_absolute_time` (`mach_timebase_info`: 125/3 ns per tick on the M4, so 24 MHz, the
//! guest's CNTFRQ); a rate that is not a whole number of hertz is refused.
//!
//! **Wake-ups only prompt rechecks.** A device's thread sleeps until the V of its next event and then lets the device
//! act as of that V ([`Timed::fire`]): lateness delays an interrupt, never changes a count or a register. macOS fires a
//! default thread's timer up to half its interval late (measured: `nanosleep` of 0.2, 2 and 10 ms woke 0.1, 1 and 5 ms
//! late); a thread under the time-constraint policy wakes 1-5 us late on average (worst seen 9 us). The device threads
//! take that policy: they block between events and compute for microseconds per event.
use crate::devices::Device;
use std::sync::atomic::{AtomicBool, Ordering::SeqCst};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};

unsafe extern "C" {
    fn mach_absolute_time() -> u64;
}
/// V now.
pub fn now() -> u64 {
    unsafe { mach_absolute_time() }
}
/// V's rate in hertz (1e9 x denominator / numerator of the mach timebase, exact).
pub(crate) fn frequency() -> u64 {
    #[repr(C)]
    struct Timebase {
        numer: u32,
        denom: u32,
    }
    unsafe extern "C" {
        fn mach_timebase_info(info: *mut Timebase) -> i32;
    }
    let mut t = Timebase { numer: 0, denom: 0 };
    unsafe { mach_timebase_info(&mut t) };
    let (n, d) = (u64::from(t.numer.max(1)), u64::from(t.denom));
    assert!(
        1_000_000_000 * d % n == 0,
        "mach timebase {n}/{d}: not a whole number of hertz"
    );
    1_000_000_000 * d / n
}

/// A device with timed events: [`Timer`] runs it on its own thread.
pub(crate) trait Timed: Device {
    /// The V of its next event, if any.
    fn next(&self) -> Option<u64>;
    /// Acts on the event due at V = `at` (now is at or after it: a device may take every event due by now at once).
    fn fire(&mut self, at: u64);
    /// Final owned-output drain after the VM stops; default timed sources have no staged output.
    fn shutdown(&mut self) {}
}
struct Inner<T> {
    device: Mutex<T>,
    changed: Condvar,
    stop: AtomicBool,
}
/// Ends a [`Timer`]'s thread without dropping the device: the port bus still reaches it, each access first taking the
/// events due ([`Timer::access`]), but nothing fires asynchronously any more (R2 review: a run's end).
pub(crate) trait Halt: Send + Sync {
    fn halt(&self);
}
impl<T: Timed + 'static> Halt for Inner<T> {
    fn halt(&self) {
        // Under the lock: the thread is either waiting (and is woken) or has not yet looked at `stop`.
        let mut device = self.device.lock().unwrap_or_else(|e| e.into_inner());
        if !self.stop.swap(true, SeqCst) {
            device.shutdown();
        }
        drop(device);
        self.changed.notify_one();
    }
}
/// The shortest time between two turns of a device's thread.
const GAP_US: u64 = 20;
/// A [`Timed`] device and its thread: the port bus reaches the device through it (an access that moves the next event
/// wakes the thread); dropping it ends and joins the thread.
pub(crate) struct Timer<T: Timed + 'static> {
    inner: Arc<Inner<T>>,
    thread: Option<std::thread::JoinHandle<()>>,
}
impl<T: Timed + 'static> Timer<T> {
    pub(crate) fn new(device: T, name: &str) -> Self {
        let inner = Arc::new(Inner {
            device: Mutex::new(device),
            changed: Condvar::new(),
            stop: AtomicBool::new(false),
        });
        let me = inner.clone();
        let thread = std::thread::Builder::new()
            .name(name.into())
            .spawn(move || {
                time_constraint();
                let mut d = me.device.lock().unwrap_or_else(|e| e.into_inner());
                // At most one turn per GAP_US: events closer together are taken together (a device catches up on every
                // event due at once), so a fast source (a PIT count of 2 changes IRQ 0 every 838 ns) cannot keep the
                // device locked against its accesses.
                let gap = natemu_abi::clock::scale(GAP_US, frequency(), 1_000_000);
                let mut last = 0;
                while !me.stop.load(SeqCst) {
                    // One reading of V per turn: a second one later than `at` would make `at - now` wrap.
                    let n = now();
                    d = match d.next().map(|at| at.max(last + gap)) {
                        Some(at) if n >= at => {
                            last = n;
                            d.fire(at);
                            // Accesses may come in between two events.
                            drop(d);
                            me.device.lock().unwrap_or_else(|e| e.into_inner())
                        }
                        Some(at) => {
                            let wait =
                                std::time::Duration::from_nanos(crate::execution::ticks_ns(at - n));
                            me.changed
                                .wait_timeout(d, wait)
                                .unwrap_or_else(|e| e.into_inner())
                                .0
                        }
                        None => me.changed.wait(d).unwrap_or_else(|e| e.into_inner()),
                    };
                }
            })
            .expect("timer thread");
        Self {
            inner,
            thread: Some(thread),
        }
    }
    /// Its thread's off switch.
    pub(crate) fn halter(&self) -> Arc<dyn Halt> {
        self.inner.clone()
    }
    /// The device, locked (its thread waits meanwhile).
    pub(crate) fn lock(&self) -> MutexGuard<'_, T> {
        self.inner.device.lock().unwrap_or_else(|e| e.into_inner())
    }
}
impl<T: Timed + 'static> Timer<T> {
    /// One access to the device: first every event already due (so an access never sees, or overwrites, a state the
    /// thread has not caught up with yet: a reload cannot drop a due edge, a status read sees every flag due); then the
    /// access; the thread is woken only when the next event moved (an access every few microseconds would otherwise
    /// wake it every time).
    fn access<R>(&self, f: impl FnOnce(&mut T) -> R) -> R {
        let mut d = self.lock();
        while let Some(at) = d.next().filter(|&at| at <= now()) {
            d.fire(at);
        }
        let before = d.next();
        let r = f(&mut d);
        if d.next() != before {
            self.inner.changed.notify_one();
        }
        r
    }
}
impl<T: Timed + 'static> Device for Timer<T> {
    fn read(&mut self, vcpu: usize, offset: u64, size: u64) -> Option<u64> {
        Timer::access(self, |d| d.read(vcpu, offset, size))
    }
    fn write(&mut self, vcpu: usize, offset: u64, size: u64, value: u64) -> bool {
        Timer::access(self, |d| d.write(vcpu, offset, size, value))
    }
}
impl<T: Timed + 'static> Drop for Timer<T> {
    fn drop(&mut self) {
        self.inner.halt();
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }
}

/// Puts the calling thread under the time-constraint scheduling policy (computation 50 us within 100 us, preemptible),
/// so its timed waits end on time. A failure leaves it at the default policy (its wake-ups are then later, never wrong).
fn time_constraint() {
    #[repr(C)]
    struct Policy {
        period: u32,
        computation: u32,
        constraint: u32,
        preemptible: u32,
    }
    unsafe extern "C" {
        fn pthread_self() -> usize;
        fn pthread_mach_thread_np(thread: usize) -> u32;
        fn thread_policy_set(thread: u32, flavor: u32, policy: *const Policy, count: u32) -> i32;
    }
    const THREAD_TIME_CONSTRAINT_POLICY: u32 = 2;
    let us = |n: u64| natemu_abi::clock::scale(n, frequency(), 1_000_000) as u32;
    let p = Policy {
        period: 0,
        computation: us(50),
        constraint: us(100),
        preemptible: 1,
    };
    unsafe {
        thread_policy_set(
            pthread_mach_thread_np(pthread_self()),
            THREAD_TIME_CONSTRAINT_POLICY,
            &p,
            4,
        )
    };
}

/// L5 (owner decision, October 30): the lost-tick policy of each periodic interrupt source, for periods that pass while
/// the vCPU cannot take the interrupt (held in the host, interrupts off): *reinject* (each missed period is delivered
/// later, one per acceptance of the previous) or *discard* (they merge, as an IRR does). One-shot timers are unaffected.
/// Defaults, from what QEMU 11.1.2 and KVM (Linux 6.12) do, the hypervisor's practice where they differ:
/// - PIT (channel 0, modes 2 and 3): **reinject**. KVM's in-kernel PIT reinjects (`kvm_create_pit` ->
///   `kvm_pit_set_reinject(pit, true)`, arch/x86/kvm/i8254.c 702; `pit_timer_fn` counts `pending`, `pit_do_work` injects
///   the next only after `irq_ack`, 248-273), and QEMU's `kvm-pit` keeps it (`lost_tick_policy` default `delay`,
///   hw/i386/kvm/i8254.c 322-323); QEMU's own PIT (hw/timer/i8254.c) has no policy: its edges merge.
/// - RTC periodic interrupt: **discard**. QEMU's mc146818 default (`lost_tick_policy` `discard`, hw/rtc/mc146818rtc.c
///   950-955; `slew` is the `-rtc driftfix=slew` option); KVM has no RTC.
/// - LAPIC periodic timer: **discard**. KVM merges: `apic_timer_expired` returns while an expiry is pending
///   (arch/x86/kvm/lapic.c 1920-1921); QEMU's APIC likewise sets the IRR bit again.
#[derive(Clone, Copy, Debug)]
pub struct LostTicks {
    pub(crate) pit: bool,
    pub(crate) rtc: bool,
    pub lapic: bool,
}
impl LostTicks {
    pub const DEFAULT: Self = Self {
        pit: true,
        rtc: false,
        lapic: false,
    };
    /// `pit=reinject,rtc=discard,lapic=reinject` (any subset; the rest default).
    pub fn parse(text: &str) -> crate::Result<Self> {
        let mut p = Self::DEFAULT;
        for item in text.split(',') {
            let (dev, policy) = item
                .split_once('=')
                .ok_or(format!("lost-ticks {item}: DEVICE=reinject|discard"))?;
            let reinject = match policy {
                "reinject" => true,
                "discard" => false,
                _ => return Err(format!("lost-ticks {item}: reinject or discard")),
            };
            match dev {
                "pit" => p.pit = reinject,
                "rtc" => p.rtc = reinject,
                "lapic" => p.lapic = reinject,
                _ => return Err(format!("lost-ticks {item}: pit, rtc or lapic")),
            }
        }
        Ok(p)
    }
}
/// A periodic source's rising edges and what became of them (the run report, `--clock-trace`): delivered into a clear
/// IRR bit, merged into a pending one, taken by nobody, missed in all (merged, here or in an IRR), and reinjected.
#[derive(Default, Debug, serde::Serialize)]
pub struct TickStats {
    pub rises: std::sync::atomic::AtomicU64,
    pub fresh: std::sync::atomic::AtomicU64,
    pub coalesced: std::sync::atomic::AtomicU64,
    pub dropped: std::sync::atomic::AtomicU64,
    pub missed: std::sync::atomic::AtomicU64,
    pub reinjected: std::sync::atomic::AtomicU64,
}
/// The board's timed sources, for reports.
pub struct Board {
    pub pit: Arc<TickStats>,
    pub rtc: Arc<TickStats>,
    /// The PIT's, RTC's, PM block's and UART TX threads ([`Board::stop`]).
    timers: Vec<Arc<dyn Halt>>,
}
impl Board {
    /// Ends the timed devices' threads (a run's end: no source fires any more; the devices stay readable).
    pub fn stop(&self) {
        self.timers.iter().for_each(|t| t.halt());
    }
}

/// L4b: puts the board's timed devices on `devices`, wired into `irqs`: the PIT at ports 40H-43H and 61H, the RTC at
/// 70H-71H, the UART at COM1, the ACPI PM block at B000H-B00BH and its reset register at B040H (`boot::acpi::platform`). Each runs on its
/// own [`Timer`] thread, joined when `devices` drops.
pub fn install(
    devices: &mut crate::devices::Devices,
    irqs: &crate::irq::Irqs,
    lost: LostTicks,
    uart: &Arc<Mutex<crate::uart::Uart>>,
) -> crate::Result<Board> {
    use crate::boot::acpi::platform as p;
    use crate::devices::{At, Shared};
    let f = frequency();
    let pit_device = crate::pit::Pit::new(irqs, f, lost.pit);
    let pit_stats = pit_device.stats.clone();
    let pit = Timer::new(pit_device, "pit");
    let mut timers = vec![pit.halter(), crate::uart::install(devices, irqs, uart)?];
    let pit: Shared = Arc::new(Mutex::new(pit));
    devices.register_ports(0x40, 4, pit.clone())?;
    devices.register_ports(0x61, 1, Arc::new(Mutex::new(At(pit, crate::pit::PORT_61))))?;
    let r = crate::rtc::Rtc::new(irqs, f, lost.rtc);
    let rtc_stats = r.stats.clone();
    let rtc = Timer::new(r, "rtc");
    timers.push(rtc.halter());
    let rtc: Shared = Arc::new(Mutex::new(rtc));
    devices.register_ports(p::RTC_PORT.into(), p::RTC_PORT_LEN.into(), rtc)?;
    let pm = Timer::new(crate::pm::Pm::new(irqs, f, devices.power.clone()), "pm");
    timers.push(pm.halter());
    let pm: Shared = Arc::new(Mutex::new(pm));
    let at = |offset: u64| -> Shared { Arc::new(Mutex::new(At(pm.clone(), offset))) };
    devices.register_ports(p::PM1A_EVT.into(), p::PM1_EVT_LEN.into(), at(0))?;
    devices.register_ports(
        p::PM1A_CNT.into(),
        p::PM1_CNT_LEN.into(),
        at(crate::pm::CNT),
    )?;
    devices.register_ports(p::PM_TMR.into(), p::PM_TMR_LEN.into(), at(crate::pm::TIMER))?;
    devices.register_ports(p::RESET_PORT.into(), 1, at(crate::pm::RESET_REG))?;
    Ok(Board {
        pit: pit_stats,
        rtc: rtc_stats,
        timers,
    })
}

#[cfg(test)]
mod tests {
    #[test]
    fn tick_stats_json_matches_the_former_format() {
        let t = super::TickStats::default();
        for (n, a) in [
            &t.rises,
            &t.fresh,
            &t.coalesced,
            &t.dropped,
            &t.missed,
            &t.reinjected,
        ]
        .into_iter()
        .enumerate()
        {
            a.store(n as u64 * 7 + 1, super::SeqCst);
        }
        let l = |a: &std::sync::atomic::AtomicU64| a.load(super::SeqCst);
        let old = format!(
            "{{\"rises\":{},\"fresh\":{},\"coalesced\":{},\"dropped\":{},\"missed\":{},\"reinjected\":{}}}",
            l(&t.rises),
            l(&t.fresh),
            l(&t.coalesced),
            l(&t.dropped),
            l(&t.missed),
            l(&t.reinjected)
        );
        let old: serde_json::Value = serde_json::from_str(&old).unwrap();
        assert_eq!(old, serde_json::to_value(&t).unwrap());
    }
}
