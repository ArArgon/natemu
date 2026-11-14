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
    /// The VM's backing (its `Events`), shared with the [`execution::Backing`]: the chipset outlives a move of it.
    memory: Arc<Memory>,
    vcpus: usize,
    doorbell: Option<Doorbell>,
    /// The master PIC's output as last given to the LINT0s.
    lint0: bool,
    /// I/O APIC messages sent (diagnostics and the board's unit test).
    messages: std::cell::Cell<u64>,
    /// L4b: fixed interrupts accepted into an IRR where the vector was clear (fresh) or already pending (coalesced).
    fresh: std::cell::Cell<u64>,
    coalesced: std::cell::Cell<u64>,
}
impl Lapics {
    fn memory(&self) -> &Memory {
        &self.memory
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
            let was = event != execution::NMI && execution::pending(self.memory(), t, event);
            let accepted = execution::post(self.memory(), t, event, m.level);
            if accepted {
                let c = if was { &self.coalesced } else { &self.fresh };
                c.set(c.get() + 1);
            }
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
                let accepting = self
                    .targets(&m)
                    .find(|&t| execution::published(self.memory(), t) & lapic::SW_ENABLED != 0);
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
        (
            &mut self.ioapic,
            Wires {
                pic: &mut self.pic,
                lapics: &self.lapics,
            },
        )
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
    /// `gsi_handler`: line `irq` (ISA IRQ 0..15 or GSI 16..23) at `level`, passed on when it changes; what a rise did.
    fn line(&mut self, irq: usize, level: bool) -> Delivery {
        if irq >= ioapic::PINS || (self.lines >> irq & 1 != 0) == level {
            return Delivery::None;
        }
        self.lines ^= 1 << irq;
        let (fresh, coalesced) = (self.lapics.fresh.get(), self.lapics.coalesced.get());
        let mut pic = Delivery::None;
        if irq < 16 {
            // Through the 8259: an unmasked rise with an APIC taking the PIC's output (ExtINT or LINT0 fixed) is fresh,
            // or coalesced when its IRR bit was already set.
            let (chip, bit) = (irq / 8, 1u8 << (irq % 8));
            let was = self.pic.chips[chip].irr_bits() & bit != 0;
            self.pic.set_irq(irq as u8, level);
            let taker = (0..self.lapics.vcpus).any(|v| {
                execution::published(self.lapics.memory(), v) & (lapic::EXTINT | lapic::LINT0_FIXED)
                    != 0
            });
            if level && taker && !self.pic.masked(irq as u8) {
                pic = if was {
                    Delivery::Coalesced
                } else {
                    Delivery::Fresh
                };
            }
        }
        let pin = if irq == 0 {
            crate::boot::acpi::platform::IRQ0_GSI as usize
        } else {
            irq
        };
        let (a, mut w) = self.wires();
        a.set_irq(pin, level, &mut w);
        self.sync();
        match (
            self.lapics.fresh.get() > fresh,
            self.lapics.coalesced.get() > coalesced,
        ) {
            (true, _) => Delivery::Fresh,
            (_, true) => Delivery::Coalesced,
            _ => pic,
        }
    }
}

/// L4b: what a rising edge on a line did (for the lost-tick policies): some APIC took it into a clear IRR bit, or every
/// taker had it pending already (it merged), or nothing took it (masked, or no destination accepts).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Delivery {
    Fresh,
    Coalesced,
    None,
}
/// A shared handle on the chipset, for devices, drivers and asynchronous sources (any thread).
#[derive(Clone)]
pub struct Irqs {
    chipset: Arc<Mutex<Chipset>>,
    live: Arc<AtomicUsize>,
}
/// While held, a source may still fire: halted vCPUs are not ended ([`Irqs::live`]).
pub struct Busy(pub Arc<AtomicUsize>);
impl Drop for Busy {
    fn drop(&mut self) {
        self.0.fetch_sub(1, SeqCst);
    }
}
impl Irqs {
    fn lock(&self) -> MutexGuard<'_, Chipset> {
        self.chipset.lock().unwrap_or_else(|e| e.into_inner())
    }
    /// Sets line `irq` (ISA IRQ 0..15, GSI 16..23) to `level` (1 = asserted), from any thread; what a rise did.
    pub fn line(&self, irq: usize, level: bool) -> Delivery {
        self.lock().line(irq, level)
    }
    /// A guard a source holds while it may still fire.
    pub fn busy(&self) -> Busy {
        self.live.fetch_add(1, SeqCst);
        Busy(self.live.clone())
    }
    /// A [`Busy`] count handed across threads: `hold` takes it, `release` drops it.
    pub fn hold(&self) {
        self.live.fetch_add(1, SeqCst);
    }
    pub fn release(&self) {
        self.live.fetch_sub(1, SeqCst);
    }
    /// Sources that may still fire (held [`Busy`] guards): while nonzero, no halted vCPU may be ended.
    pub fn live(&self) -> usize {
        self.live.load(SeqCst)
    }
    /// A message-signalled interrupt (SDM Vol.3 11.11.1, 11.11.2): `address` bits 31:20 are 0xFEE, the destination ID is
    /// bits 19:12, the redirection hint bit 3 (with fixed delivery it makes the message lowest-priority) and the
    /// destination mode bit 2 (set: logical); `data` bits 7:0 are the vector and bits 10:8 the delivery mode. The trigger
    /// mode (data bit 15) is ignored: always edge, so the TMR bit is clear and the EOI stays inside the LAPIC. Whether some
    /// LAPIC accepted it; an address outside the window posts nothing.
    pub fn msi(&self, address: u64, data: u32) -> bool {
        if address >> 20 != 0xfee {
            return false;
        }
        let mode = (data >> 8 & 7) as u8;
        self.lock().lapics.send(Message {
            dest: (address >> 12) as u8,
            logical: address & 4 != 0,
            mode: if address & 8 != 0 && mode == 0 { 1 } else { mode },
            vector: data as u8,
            level: false,
        })
    }
    /// A pulse on the board's NMI input, wired to every LAPIC's LINT1 (the MADT's Local APIC NMI): an NMI to each APIC
    /// whose LVT LINT1 is unmasked in NMI mode (`lapic::LINT1_NMI`; fixed or ExtINT LINT1 delivery is not wired). No
    /// source drives it on this board yet (no NMI button or watchdog).
    pub fn nmi(&self) {
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
    pub fn attach(&self, doorbell: Doorbell) {
        self.lock().lapics.doorbell = Some(doorbell);
    }
    /// Before the machine goes: no delivery rings its doorbells any more (rings happen under the chipset lock, so none is
    /// in progress when this returns; one could otherwise reach a destroyed VM, or the SPIs of the process's next one).
    pub fn detach(&self) {
        self.lock().lapics.doorbell = None;
    }
    /// A machine's doorbells are attached.
    pub fn attached(&self) -> bool {
        self.lock().lapics.doorbell.is_some()
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
pub fn install(
    devices: &mut Devices,
    memory: &Arc<Memory>,
    vcpus: usize,
    uart: &Arc<Mutex<crate::uart::Uart>>,
) -> crate::Result<Irqs> {
    let lapics = Lapics {
        memory: memory.clone(),
        vcpus,
        doorbell: None,
        lint0: false,
        messages: Default::default(),
        fresh: Default::default(),
        coalesced: Default::default(),
    };
    let chipset = Chipset {
        ioapic: Ioapic::new(),
        pic: Pic::new(),
        lines: 0,
        lapics,
    };
    let irqs = Irqs {
        chipset: Arc::new(Mutex::new(chipset)),
        live: Arc::default(),
    };
    let view = |kind| -> Shared { Arc::new(Mutex::new(View(irqs.clone(), kind))) };
    devices.register(
        crate::boot::acpi::platform::IOAPIC_BASE.into(),
        4096,
        view(Kind::Ioapic),
    )?;
    devices.register_ports(0x20, 2, view(Kind::Pic(0)))?;
    devices.register_ports(0xa0, 2, view(Kind::Pic(1)))?;
    devices.register_ports(0x4d0, 2, view(Kind::Elcr))?;
    let line = irqs.clone();
    let irq = crate::boot::acpi::platform::COM1_IRQ.into();
    uart.lock().unwrap_or_else(|e| e.into_inner()).irq = Some(Box::new(move |level| {
        line.line(irq, level);
    }));
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

#[cfg(test)]
mod tests {
    use super::*;
    /// The board's change filter: a line raised again while high is no edge (QEMU's I/O APIC would deliver it again);
    /// ISA IRQ0 enters pin 2, IRQ n pin n and PIC input n. No vCPU (`vcpus` 0): only the messages are counted.
    #[test]
    fn line_changes_only() {
        let lapics = Lapics {
            memory: Arc::new(Memory::with_size(4096).unwrap()),
            vcpus: 0,
            doorbell: None,
            lint0: false,
            messages: Default::default(),
            fresh: Default::default(),
            coalesced: Default::default(),
        };
        let mut c = Chipset {
            ioapic: Ioapic::new(),
            pic: Pic::new(),
            lines: 0,
            lapics,
        };
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
        assert_eq!(
            (c.lapics.messages.get(), c.pic.chips[0].irr_bits()),
            (3, 0x11)
        );
        c.line(24, true); // no such line
        assert_eq!(c.lines, 0x11);
    }

    /// A board of `words.len()` vCPUs whose published APIC words are `words`.
    fn board(words: &[u64]) -> Lapics {
        let memory = Arc::new(Memory::with_size(exec::BACKING_BYTES).unwrap());
        for (v, &w) in words.iter().enumerate() {
            execution::set_published(&memory, v, w);
        }
        Lapics {
            memory,
            vcpus: words.len(),
            doorbell: None,
            lint0: false,
            messages: Default::default(),
            fresh: Default::default(),
            coalesced: Default::default(),
        }
    }
    fn irqs(lapics: Lapics) -> Irqs {
        Irqs {
            chipset: Arc::new(Mutex::new(Chipset {
                ioapic: Ioapic::new(),
                pic: Pic::new(),
                lines: 0,
                lapics,
            })),
            live: Arc::default(),
        }
    }
    const ON: u64 = lapic::HW_ENABLED | lapic::SW_ENABLED;
    /// xAPIC flat-model word with LDR byte `ldr`.
    fn xapic(ldr: u64) -> u64 {
        ON | 0xf << 32 | ldr << 24
    }
    fn pending(i: &Irqs, v: usize, vector: u64) -> bool {
        execution::pending(i.lock().lapics.memory(), v, vector)
    }

    /// A fixed physical MSI reaches only its destination, as an edge (TMR clear).
    #[test]
    fn msi_fixed_physical_reaches_only_its_destination() {
        for words in [[xapic(0), xapic(0)], [ON | lapic::X2APIC; 2]] {
            let i = irqs(board(&words));
            // The TMR of the vector starts set: an edge message must clear it.
            execution::post(i.lock().lapics.memory(), 1, 0x41, true);
            assert!(i.msi(0xFEE0_1000, 0x41));
            assert!(pending(&i, 1, 0x41) && !pending(&i, 0, 0x41));
            assert!(!execution::tmr(i.lock().lapics.memory(), 1, 0x41));
        }
    }

    /// Logical destination mode (address bit 2) selects by the LDR; the redirection hint (bit 3) picks one member.
    #[test]
    fn msi_logical_destination_follows_the_ldr() {
        let i = irqs(board(&[xapic(0x01), xapic(0x02)]));
        assert!(i.msi(0xFEE0_2004, 0x50)); // flat, destination bit 1
        assert!(pending(&i, 1, 0x50) && !pending(&i, 0, 0x50));
        assert!(i.msi(0xFEE0_F004, 0x51)); // both members
        assert!(pending(&i, 0, 0x51) && pending(&i, 1, 0x51));
        assert!(i.msi(0xFEE0_F00C, 0x52)); // redirection hint: lowest-numbered member only
        assert!(pending(&i, 0, 0x52) && !pending(&i, 1, 0x52));
        // x2APIC logical: cluster 0, bit n of the destination's low half.
        let x = ON | lapic::X2APIC;
        let i = irqs(board(&[x | lapic::x2apic_ldr(0), x | lapic::x2apic_ldr(1)]));
        assert!(i.msi(0xFEE0_2004, 0x53));
        assert!(pending(&i, 1, 0x53) && !pending(&i, 0, 0x53));
    }

    /// Vectors 0..15 are illegal for a fixed interrupt (SDM 11.5.3): dropped.
    #[test]
    fn msi_vector_below_16_is_dropped() {
        let i = irqs(board(&[xapic(0), xapic(0)]));
        assert!(!i.msi(0xFEE0_1000, 0x0f));
        assert!(!pending(&i, 1, 0x0f));
    }

    /// Only 0xFEExxxxx is the MSI window (SDM 11.11.1): anything else posts nothing.
    #[test]
    fn msi_outside_fee_is_dropped() {
        let i = irqs(board(&[xapic(0), xapic(0)]));
        assert!(!i.msi(0xFEC0_0000, 0x41));
        assert!(!i.msi(0x1_FEE0_1000, 0x41));
        assert!(!pending(&i, 0, 0x41) && !pending(&i, 1, 0x41));
        assert_eq!(i.lock().lapics.messages.get(), 0);
    }
}
