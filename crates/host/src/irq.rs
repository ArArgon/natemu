//! L4a: the PC's interrupt routing (QEMU 11.1.2's `pc` board, `hw/i386/x86-common.c` `gsi_handler` 457-497 and
//! `hw/i386/x86-cpu.c` `pic_irq_request` 41-62): the I/O APIC ([`crate::ioapic`]) at `platform::IOAPIC_BASE`, the 8259
//! pair and its ELCR ([`crate::pic`]) at 20H/21H, A0H/A1H, 4D0H/4D1H, and the wires the MADT describes (L2): ISA IRQ n
//! enters PIC input n and I/O APIC pin n, except IRQ0 -> pin 2; GSIs 16-23 enter the I/O APIC only; the master PIC's INT
//! output drives every LAPIC's LINT0 (each resident applies its own LVT: ExtINT takes the vector from the PIC by an INTA
//! request, `REQUEST_IRQ`). The I/O APIC's messages go to the LAPICs through [`execution::post`] (acceptance, the TMR for
//! a level-triggered one, the post count) and each target's doorbell; a LAPIC's EOI of a vector whose TMR bit is set comes
//! back as `REQUEST_IRQ` (the EOI broadcast). One mutex orders everything the chipset does.
//!
//! **Inputs are levels.** A source sets its line's level ([`Irqs::line`]); the board passes only changes on, so an edge is
//! a 0 -> 1 change, as an edge detector sees it (QEMU's edge-mode I/O APIC delivers on every raise call, a raise while high
//! included: a departure from QEMU, `ioapic.rs`; the comparisons avoid redundant raises). The UART's IRQ4 hook reports
//! changes only (L3).
//!
//! **Asynchronous sources** (L4b's PIT and RTC, UART input, tests): call [`Irqs::line`] from any thread, and hold an
//! [`Irqs::busy`] guard while the source may still fire: the run's driver ends halted vCPUs only when no guard is held
//! (X3's rule: [`Irqs::live`] vetoes the decision as `kickers_left` does). Every delivery posts through `execution::post`
//! (the count the rule reads) before it rings.
use crate::devices::{Device, Devices, Shared};
use crate::execution;
use crate::ioapic::{self, Ioapic, Message};
use crate::memory::Memory;
use crate::pic::Pic;
use crate::vm::Doorbell;
use natemu_abi::execution::{self as exec, MMIO_OFFSET, Mmio, State, mmio};
use natemu_abi::lapic;
use std::sync::atomic::{AtomicUsize, Ordering::SeqCst};
use std::sync::{Arc, Mutex, MutexGuard};

/// The LAPICs as the chipset reaches them: the vCPUs' event records and doorbells.
struct Lapics {
    /// The VM's backing (its `Events`). SAFETY: set from the driver's backing, which outlives the registry and every
    /// thread holding an [`Irqs`] (declared before it; source threads are joined when their device drops).
    memory: *const Memory,
    vcpus: usize,
    doorbell: Option<Doorbell>,
    /// The master PIC's output as last given to the LINT0s.
    lint0: bool,
    /// I/O APIC messages sent (diagnostics and the board's unit test).
    messages: std::cell::Cell<u64>,
}
// SAFETY: see `memory`; every access through it is an atomic word of the global area.
unsafe impl Send for Lapics {}
impl Lapics {
    fn memory(&self) -> &Memory {
        unsafe { &*self.memory }
    }
    fn ring(&self, v: usize) {
        // Before the machine exists nothing runs (the vCPU sees the post at its first boundary); after a vCPU ended its
        // doorbell is moot: a failed ring is not an error of the source.
        if let Some(d) = self.doorbell {
            let _ = d.ring(v);
        }
    }
    /// The vCPUs a message's destination selects (QEMU's `apic_get_delivery_bitmask`, 556-...: an 8-bit destination;
    /// an x2APIC-mode target matches a physical one by its ID, 0xFF not being a broadcast for it, and a logical one as an
    /// x2APIC cluster/bit destination).
    fn targets(&self, m: &Message) -> impl Iterator<Item = usize> + '_ {
        let (dest, logical) = (u64::from(m.dest), m.logical);
        (0..self.vcpus).filter(move |&t| {
            let word = execution::published(self.memory(), t);
            match (word & lapic::X2APIC != 0, logical) {
                (true, false) => dest == t as u64,
                (x2, _) => lapic::in_destination(word, t as u64, dest, logical, x2),
            }
        })
    }
    /// One I/O APIC message onto the APIC bus (QEMU's `apic_bus_deliver`): whether some LAPIC accepted it.
    fn send(&self, m: Message) -> bool {
        self.messages.set(self.messages.get() + 1);
        // ponytail: a vector 0..15 would raise the target's receive-illegal-vector error (SDM 11.5.3), which only its
        // resident can record; it is dropped here (no driver programs one). Add a pending-error word if one must be seen.
        if m.vector < 16 && m.mode != 4 {
            return false;
        }
        let post = |t: usize, event: u64| {
            let accepted = execution::post(self.memory(), t, event, m.level);
            if accepted {
                self.ring(t);
            }
            accepted
        };
        // `fold`, not `any`: every target of a broadcast gets it.
        let all = |event: u64| self.targets(&m).fold(false, |any, t| post(t, event) | any);
        match m.mode {
            // ExtINT: the vector came from the PIC; delivered as fixed (QEMU's `apic_bus_deliver` 280-282).
            0 | 7 => all(u64::from(m.vector)),
            // Lowest priority: the lowest-numbered software-enabled member (X3's arbitration, SDM 11.6.2.4: model
            // specific; QEMU takes the lowest-numbered member, enabled or not).
            1 => {
                let accepting = self.targets(&m).find(|&t| execution::published(self.memory(), t) & lapic::SW_ENABLED != 0);
                accepting.is_some_and(|t| post(t, u64::from(m.vector)))
            }
            4 => all(execution::NMI),
            // SMI (no SMM in the profile) and INIT (X4's start path) reach no core; 3 and 6 are reserved (QEMU: none).
            _ => false,
        }
    }
}
/// The I/O APIC's view of the board while it runs.
struct Wires<'a> {
    pic: &'a mut Pic,
    lapics: &'a Lapics,
}
impl ioapic::Bus for Wires<'_> {
    fn send(&mut self, m: Message) -> bool {
        self.lapics.send(m)
    }
    fn inta(&mut self) -> u8 {
        self.pic.read_irq()
    }
}

/// The interrupt controllers and their wires.
pub(crate) struct Chipset {
    ioapic: Ioapic,
    pic: Pic,
    /// Input levels by line (ISA IRQ 0..15, GSI 16..23).
    lines: u32,
    lapics: Lapics,
}
impl Chipset {
    fn wires(&mut self) -> (&mut Ioapic, Wires<'_>) {
        (&mut self.ioapic, Wires { pic: &mut self.pic, lapics: &self.lapics })
    }
    /// The master PIC's output onto every LINT0, when it changed (a rise posts to and rings the APICs taking ExtINTs).
    fn sync(&mut self) {
        let out = self.pic.output();
        if out != self.lapics.lint0 {
            self.lapics.lint0 = out;
            for v in 0..self.lapics.vcpus {
                let mut ring = execution::set_lint0(self.lapics.memory(), v, out);
                // LVT LINT0 fixed and edge-triggered: the rise is the LVT vector's edge interrupt (SDM 11.5.1).
                let word = execution::published(self.lapics.memory(), v);
                if out && word & lapic::LINT0_FIXED != 0 && word >> 48 & 0xff >= 16 {
                    ring |= execution::post(self.lapics.memory(), v, word >> 48 & 0xff, false);
                }
                if ring {
                    self.lapics.ring(v);
                }
            }
        }
    }
    /// `gsi_handler`: line `irq` (ISA IRQ 0..15 or GSI 16..23) at `level`, passed on when it changes.
    fn line(&mut self, irq: usize, level: bool) {
        if irq >= ioapic::PINS || (self.lines >> irq & 1 != 0) == level {
            return;
        }
        self.lines ^= 1 << irq;
        if irq < 16 {
            self.pic.set_irq(irq as u8, level);
        }
        let pin = if irq == 0 { crate::boot::acpi::platform::IRQ0_GSI as usize } else { irq };
        let (a, mut w) = self.wires();
        a.set_irq(pin, level, &mut w);
        self.sync();
    }
}

/// A shared handle on the chipset, for devices, drivers and asynchronous sources (any thread).
#[derive(Clone)]
pub(crate) struct Irqs {
    chipset: Arc<Mutex<Chipset>>,
    live: Arc<AtomicUsize>,
}
/// While held, a source may still fire: halted vCPUs are not ended ([`Irqs::live`]).
pub(crate) struct Busy(Arc<AtomicUsize>);
impl Drop for Busy {
    fn drop(&mut self) {
        self.0.fetch_sub(1, SeqCst);
    }
}
impl Irqs {
    fn lock(&self) -> MutexGuard<'_, Chipset> {
        self.chipset.lock().unwrap_or_else(|e| e.into_inner())
    }
    /// Sets line `irq` (ISA IRQ 0..15, GSI 16..23) to `level` (1 = asserted), from any thread.
    pub(crate) fn line(&self, irq: usize, level: bool) {
        self.lock().line(irq, level)
    }
    /// A guard a source holds while it may still fire.
    pub(crate) fn busy(&self) -> Busy {
        self.live.fetch_add(1, SeqCst);
        Busy(self.live.clone())
    }
    /// Sources that may still fire (held [`Busy`] guards): while nonzero, no halted vCPU may be ended.
    pub(crate) fn live(&self) -> usize {
        self.live.load(SeqCst)
    }
    /// A pulse on the board's NMI input, wired to every LAPIC's LINT1 (the MADT's Local APIC NMI): an NMI to each APIC
    /// whose LVT LINT1 is unmasked in NMI mode (`lapic::LINT1_NMI`; fixed or ExtINT LINT1 delivery is not wired). No
    /// source drives it on this board yet (no NMI button or watchdog).
    pub(crate) fn nmi(&self) {
        let c = self.lock();
        for v in 0..c.lapics.vcpus {
            if execution::published(c.lapics.memory(), v) & lapic::LINT1_NMI != 0 {
                if execution::post(c.lapics.memory(), v, execution::NMI, false) {
                    c.lapics.ring(v);
                }
            }
        }
    }
    /// The machine exists: deliveries ring its doorbells from now on.
    pub(crate) fn attach(&self, doorbell: Doorbell) {
        self.lock().lapics.doorbell = Some(doorbell);
    }
    /// A LAPIC's EOI of level-triggered `vector` (the broadcast to the I/O APIC).
    fn eoi(&self, vector: u64) {
        let mut c = self.lock();
        let (a, mut w) = c.wires();
        a.eoi_broadcast(vector, &mut w);
        c.sync();
    }
    /// The INTA cycle of a LAPIC taking an ExtINT from LINT0: the PIC's vector.
    fn inta(&self) -> u8 {
        let mut c = self.lock();
        let v = c.pic.read_irq();
        c.sync();
        v
    }
}

/// A register file of the chipset on the device registry: the I/O APIC page, a PIC's two ports, or the ELCR pair.
enum Kind {
    Ioapic,
    Pic(usize),
    Elcr,
}
struct View(Irqs, Kind);
impl View {
    /// A port access, byte by byte, lowest first (QEMU's `impl.max_access_size` 1).
    fn bytes(&self, offset: u64, size: u64, data: Option<u64>) -> u64 {
        let mut c = self.0.lock();
        let mut value = 0;
        for k in 0..size {
            let byte = data.map(|d| (d >> (8 * k)) as u8);
            let b = match self.1 {
                Kind::Pic(chip) => c.pic.access(chip, offset + k, false, byte),
                _ => c.pic.access((offset + k) as usize, 0, true, byte),
            };
            value |= u64::from(b) << (8 * k);
        }
        c.sync();
        value
    }
}
impl Device for View {
    fn read(&mut self, _vcpu: usize, offset: u64, size: u64) -> Option<u64> {
        match self.1 {
            Kind::Ioapic => self.0.lock().ioapic.read(offset, size),
            _ => Some(self.bytes(offset, size, None)),
        }
    }
    fn write(&mut self, _vcpu: usize, offset: u64, size: u64, value: u64) -> bool {
        match self.1 {
            Kind::Ioapic => {
                let mut c = self.0.lock();
                let (a, mut w) = c.wires();
                let done = a.write(offset, size, value, &mut w);
                c.sync();
                done
            }
            _ => {
                self.bytes(offset, size, Some(value));
                true
            }
        }
    }
}

/// Puts the chipset on `devices` for a VM of `vcpus` vCPUs on `memory`, and wires `uart`'s IRQ (COM1, ISA IRQ 4) into it.
pub(crate) fn install(devices: &mut Devices, memory: &Memory, vcpus: usize, uart: &Arc<Mutex<crate::uart::Uart>>) -> crate::Result<Irqs> {
    let lapics = Lapics { memory, vcpus, doorbell: None, lint0: false, messages: Default::default() };
    let chipset = Chipset { ioapic: Ioapic::new(), pic: Pic::new(), lines: 0, lapics };
    let irqs = Irqs { chipset: Arc::new(Mutex::new(chipset)), live: Arc::default() };
    let view = |kind| -> Shared { Arc::new(Mutex::new(View(irqs.clone(), kind))) };
    devices.register(crate::boot::acpi::platform::IOAPIC_BASE.into(), 4096, view(Kind::Ioapic))?;
    devices.register_ports(0x20, 2, view(Kind::Pic(0)))?;
    devices.register_ports(0xa0, 2, view(Kind::Pic(1)))?;
    devices.register_ports(0x4d0, 2, view(Kind::Elcr))?;
    let line = irqs.clone();
    let irq = crate::boot::acpi::platform::COM1_IRQ.into();
    uart.lock().unwrap_or_else(|e| e.into_inner()).irq = Some(Box::new(move |level| line.line(irq, level)));
    devices.irqs = Some(irqs.clone());
    Ok(irqs)
}

/// Serves vCPU `area.vcpu`'s interrupt-controller request ([`exec::REQUEST_IRQ`], the [`Mmio`] record): `write` 1, the EOI
/// broadcast of the level-triggered vector in `value`; `write` 0, the INTA cycle of an ExtINT, its vector into `value`.
pub(crate) fn serve(area: execution::Area, state: &mut State, devices: Option<&Devices>) {
    let mut m: Mmio = area.read_record(MMIO_OFFSET);
    m.status = match devices.and_then(|d| d.irqs.as_ref()) {
        Some(irqs) if m.write == 1 && m.value < 256 => {
            irqs.eoi(m.value);
            mmio::DONE
        }
        Some(irqs) if m.write == 0 => {
            m.value = u64::from(irqs.inta());
            mmio::DONE
        }
        _ => 0,
    };
    area.write_record(MMIO_OFFSET, m);
    state.reply = exec::REPLY_IRQ;
}

/// A test source (`run-program --irq-source PORT`), natemu only: an asynchronous device on 8 ports. A 32-bit write at
/// +0 asks for line bits 7:0 to fire after bits 31:16 microseconds, from a host thread: an edge (0 -> 1 -> 0) with bit 8
/// clear, else the line is asserted and held; with bit 9 set, a pulse on the NMI input (LINT1) instead. A write at +4
/// deasserts line bits 7:0 at once (the handler's acknowledgement of a level-triggered request). A 32-bit read at +0 gives
/// the requests fired so far. Each request holds a [`Busy`] guard until it fired.
pub(crate) struct TestSource {
    jobs: Option<std::sync::mpsc::Sender<(std::time::Instant, usize, bool, Busy)>>,
    fired: Arc<AtomicUsize>,
    worker: Option<std::thread::JoinHandle<()>>,
    irqs: Irqs,
    /// `--irq-source-no-veto` (a negative control of the halted rule): requests hold no guard.
    pub(crate) veto: bool,
}
impl TestSource {
    pub(crate) fn new(irqs: &Irqs) -> Self {
        let (jobs, queue) = std::sync::mpsc::channel::<(std::time::Instant, usize, bool, Busy)>();
        let (lines, fired) = (irqs.clone(), Arc::new(AtomicUsize::new(0)));
        let count = fired.clone();
        let worker = std::thread::spawn(move || {
            let mut pending = Vec::new();
            loop {
                // The earliest due job, or wait for one.
                pending.sort_by_key(|j: &(std::time::Instant, usize, bool, Busy)| std::cmp::Reverse(j.0));
                let timeout = pending.last().map(|j| j.0.saturating_duration_since(std::time::Instant::now()));
                let next = match timeout {
                    Some(t) => queue.recv_timeout(t),
                    None => queue.recv().map_err(|_| std::sync::mpsc::RecvTimeoutError::Disconnected),
                };
                match next {
                    Ok(job) => pending.push(job),
                    Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {
                        let (_, line, level, _busy) = pending.pop().unwrap();
                        if line == NMI_LINE {
                            lines.nmi();
                        } else {
                            lines.line(line, true);
                            if !level {
                                lines.line(line, false);
                            }
                        }
                        count.fetch_add(1, SeqCst);
                    }
                    Err(_) => return,
                }
            }
        });
        Self { jobs: Some(jobs), fired, worker: Some(worker), irqs: irqs.clone(), veto: true }
    }
}
/// The test source's job for the NMI input.
const NMI_LINE: usize = usize::MAX;
impl Drop for TestSource {
    fn drop(&mut self) {
        self.jobs = None;
        if let Some(w) = self.worker.take() {
            let _ = w.join();
        }
    }
}
impl Device for TestSource {
    fn read(&mut self, _vcpu: usize, offset: u64, _size: u64) -> Option<u64> {
        Some(if offset == 0 { self.fired.load(SeqCst) as u64 } else { 0 })
    }
    fn write(&mut self, _vcpu: usize, offset: u64, _size: u64, value: u64) -> bool {
        let line = if value & 0x200 != 0 { NMI_LINE } else { (value & 0xff) as usize };
        match offset {
            0 => {
                let busy = if self.veto { self.irqs.busy() } else { Busy(Arc::new(AtomicUsize::new(1))) };
                let due = std::time::Instant::now() + std::time::Duration::from_micros(value >> 16 & 0xffff);
                let _ = self.jobs.as_ref().map(|j| j.send((due, line, value & 0x100 != 0, busy)));
            }
            4 => self.irqs.line(line, false),
            _ => {}
        }
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    /// The board's change filter: a line raised again while high is no edge (QEMU's I/O APIC would deliver it again);
    /// ISA IRQ0 enters pin 2, IRQ n pin n and PIC input n. No vCPU (`vcpus` 0): only the messages are counted.
    #[test]
    fn line_changes_only() {
        let lapics = Lapics { memory: std::ptr::null(), vcpus: 0, doorbell: None, lint0: false, messages: Default::default() };
        let mut c = Chipset { ioapic: Ioapic::new(), pic: Pic::new(), lines: 0, lapics };
        for (sel, v) in [(0x10 + 2 * 4, 0x41), (0x10 + 2 * 2, 0x42)] {
            let (a, mut w) = c.wires();
            a.write(0, 4, sel, &mut w);
            a.write(0x10, 4, v, &mut w);
        }
        c.line(4, true);
        c.line(4, true);
        assert_eq!(c.lapics.messages.get(), 1);
        c.line(4, false);
        c.line(4, true);
        assert_eq!(c.lapics.messages.get(), 2);
        c.line(0, true);
        assert_eq!((c.lapics.messages.get(), c.pic.chips[0].irr_bits()), (3, 0x11));
        c.line(24, true); // no such line
        assert_eq!(c.lines, 0x11);
    }
}
