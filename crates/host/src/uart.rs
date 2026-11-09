//! L3: a 16550A UART on the port bus (COM1 at 3F8H on the PC), the registers Linux's 8250 driver uses: RBR/THR, IER,
//! IIR/FCR, LCR, MCR, LSR, MSR, SCR and the divisor latch (DLL/DLM). Transcribed from QEMU 11.1.2's model
//! (`hw/char/serial.c`, tag v11.1.2; line numbers of that file): register writes `serial_ioport_write` (334-469),
//! reads `serial_ioport_read` (471-555), interrupt priority `serial_update_irq` (123-151), transmit `serial_xmit`
//! (237-287, with a backend that never pushes back: no retry), FCR `serial_write_fcr` (289-314), receive
//! `serial_receive1`/`recv_fifo_put` (602-624, 113-121), the FIFO timeout `fifo_timeout_int` (588-594), the character
//! time `serial_update_parameters` (153-188, baud base 115200: 967) and reset `serial_reset` (856-890; MCR resets to
//! OUT2 there, as QEMU's PC does). Byte registers only: a wider access is split into byte accesses at consecutive offsets,
//! lowest first (QEMU's `serial_io_ops` 952-963: `impl.max_access_size` 1). The modem lines are those of a backend without
//! modem control (QEMU's `serial_update_msl` with a file or null backend sets `poll_msl` -1): MSR reads DCD|DSR|CTS
//! with no deltas, and in loopback the MCR outputs (OUT2 -> DCD, OUT1 -> RI, RTS -> CTS, DTR -> DSR). External transmission accepts every THR write into bounded host staging, spilling full 16-byte batches
//! synchronously. THRE/TEMT and THRI become ready 20 us after the last accepted byte; the existing timer service
//! drains partial batches and publishes the ready edge even without guest accesses. This accelerated-console policy
//! is neither physical baud timing nor a hardware TX FIFO/drop model, nor a scheduler fairness bound. Loopback
//! remains instantaneous: a transmitted byte is received. Output goes to [`Uart::output`] and the driver's sink; input from the host comes through
//! [`feed`] (`--serial-stdin`), as QEMU's character backend gives it (`serial_can_receive` 557-576: never more than the
//! FIFO advertises, so host input never overruns; a guest's own loopback bytes can).
//!
//! **Time.** The FIFO character timeout (CTI, 4 character times after the last receive or RBR read with data left)
//! is evaluated at each register access from the host's monotonic clock: its effect on IIR and the line is the timer's
//! as seen by any access, but the line does not rise between accesses. **IRQ4** ([`Uart::irq`]): the UART drives its
//! interrupt output (high while IIR names an interrupt), wired by L4a (`irq::install`) to ISA IRQ 4: PIC input 4 and I/O
//! APIC pin 4. The hook reports level changes only, and
//! the board treats an edge as a 0 -> 1 change (QEMU's edge-mode I/O APIC delivers on every raise, a raise while already
//! high included: a departure recorded in `ioapic.rs`). Host input is asynchronous: [`feed`]'s thread receives bytes and
//! runs the CTI on time under the UART's lock, so the line rises without an access, through the same hook (IRQ4 into
//! `irq::Irqs::line`, whose delivery rings a halted vCPU). `linux::Prepared::run` holds an `Irqs::busy` guard while it
//! runs, as `irq.rs` asks of asynchronous sources; nothing in a Linux boot reads it today (only the program driver
//! ends halted vCPUs, by `Irqs::live`). Pending external TX holds its own busy guard through ready-IRQ publication.
//! Board shutdown drains accepted output before the final error/count observation; a blocking sink can still block
//! shutdown, as it could block a synchronous THR write. ponytail: a CTI due after a guest's partial read, with no host input, still waits for
//! the next access (Linux's 8250 handler drains the FIFO, so it does not arise there).
use crate::devices::Device;
use std::collections::VecDeque;
use std::io::{Read, Write};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const LCR_DLAB: u8 = 0x80;
const IER_MSI: u8 = 0x08;
const IER_RLSI: u8 = 0x04;
const IER_THRI: u8 = 0x02;
const IER_RDI: u8 = 0x01;
const IIR_NO_INT: u8 = 0x01;
const IIR_ID: u8 = 0x06;
const IIR_MSI: u8 = 0x00;
const IIR_THRI: u8 = 0x02;
const IIR_RDI: u8 = 0x04;
const IIR_RLSI: u8 = 0x06;
const IIR_CTI: u8 = 0x0c;
const IIR_FE: u8 = 0xc0;
const MCR_LOOP: u8 = 0x10;
const MCR_OUT2: u8 = 0x08;
const MSR_DCD: u8 = 0x80;
const MSR_DSR: u8 = 0x20;
const MSR_CTS: u8 = 0x10;
const MSR_ANY_DELTA: u8 = 0x0f;
const LSR_TEMT: u8 = 0x40;
const LSR_THRE: u8 = 0x20;
const LSR_BI: u8 = 0x10;
const LSR_OE: u8 = 0x02;
const LSR_DR: u8 = 0x01;
const LSR_INT_ANY: u8 = 0x1e;
const FCR_XFR: u8 = 0x04;
const FCR_RFR: u8 = 0x02;
const FCR_FE: u8 = 0x01;
const FIFO: usize = 16;
const BAUD_BASE: f32 = 115_200.0;
/// Accelerated-console inactivity interval, not physical baud timing or a scheduler fairness bound.
const TX_READY_US: u64 = 20;
const TX_BATCH: usize = 16;

/// Standalone instances require register accesses to service TX deadlines; production registration
/// through `clock::install` adds asynchronous service and shutdown draining.
pub struct Uart {
    divider: u16,
    rbr: u8,
    ier: u8,
    iir: u8,
    lcr: u8,
    mcr: u8,
    lsr: u8,
    msr: u8,
    scr: u8,
    fcr: u8,
    thr_ipending: bool,
    /// Host output staging, NOT a hardware FIFO: full batches spill synchronously; no THR write is dropped.
    tx: Vec<u8>,
    tx_at: Option<u64>,
    tx_delay: u64,
    tx_stopped: bool,
    tx_irqs: Option<crate::irq::Irqs>,
    tx_busy: Option<crate::irq::Busy>,
    timeout_ipending: bool,
    recv: VecDeque<u8>,
    recv_itl: usize,
    /// The FIFO timeout's deadline (QEMU's `fifo_timeout_timer`), and the time of one character.
    timeout_at: Option<Instant>,
    char_time: Duration,
    /// Every byte transmitted outside loopback, in order, while there is no sink (with one, nothing is kept: R2 review).
    pub output: Vec<u8>,
    /// Where transmitted bytes go instead (the stub's standard output, `--serial`).
    pub sink: Option<Box<dyn Write + Send>>,
    /// Bytes transmitted outside loopback.
    pub sent: u64,
    /// The sink's first write error (the sink is dropped then; later bytes are counted, not kept).
    pub sink_error: Option<String>,
    /// The interrupt output's level.
    pub(crate) line: bool,
    /// L4a's hook (`irq::install`: ISA IRQ 4): called with the new level whenever the interrupt output changes.
    pub(crate) irq: Option<Box<dyn FnMut(bool) + Send>>,
}

impl Uart {
    pub fn new() -> Self {
        Self {
            divider: 0x0c,
            rbr: 0,
            ier: 0,
            iir: IIR_NO_INT,
            lcr: 0,
            mcr: MCR_OUT2,
            lsr: LSR_TEMT | LSR_THRE,
            msr: MSR_DCD | MSR_DSR | MSR_CTS,
            scr: 0,
            fcr: 0,
            thr_ipending: false,
            tx: Vec::with_capacity(TX_BATCH),
            tx_at: None,
            tx_delay: natemu_abi::clock::scale(TX_READY_US, crate::clock::frequency(), 1_000_000),
            tx_stopped: false,
            tx_irqs: None,
            tx_busy: None,
            timeout_ipending: false,
            recv: VecDeque::with_capacity(FIFO),
            recv_itl: 1,
            timeout_at: None,
            char_time: Duration::from_nanos(1_000_000_000 / 9600 * 10),
            output: Vec::new(),
            sink: None,
            sent: 0,
            sink_error: None,
            line: false,
            irq: None,
        }
    }

    fn update_irq(&mut self) {
        let fifo = self.fcr & FCR_FE != 0;
        let id = if self.ier & IER_RLSI != 0 && self.lsr & LSR_INT_ANY != 0 {
            IIR_RLSI
        } else if self.ier & IER_RDI != 0 && self.timeout_ipending {
            IIR_CTI
        } else if self.ier & IER_RDI != 0
            && self.lsr & LSR_DR != 0
            && (!fifo || self.recv.len() >= self.recv_itl)
        {
            IIR_RDI
        } else if self.ier & IER_THRI != 0 && self.thr_ipending {
            IIR_THRI
        } else if self.ier & IER_MSI != 0 && self.msr & MSR_ANY_DELTA != 0 {
            IIR_MSI
        } else {
            IIR_NO_INT
        };
        self.iir = id | self.iir & 0xf0;
        let line = id != IIR_NO_INT;
        if line != self.line {
            self.line = line;
            if let Some(irq) = &mut self.irq {
                irq(line);
            }
        }
    }

    fn update_parameters(&mut self) {
        let parity = u32::from(self.lcr & 0x08 != 0);
        let stop = if self.lcr & 0x04 != 0 { 2 } else { 1 };
        let frame = 1 + parity + u32::from(self.lcr & 3) + 5 + stop;
        let speed = if self.divider == 0 {
            3500.0
        } else {
            BAUD_BASE / f32::from(self.divider)
        };
        self.char_time = Duration::from_nanos(((1e9_f32 / speed) * frame as f32) as u64);
    }

    fn arm_timeout(&mut self, now: Instant) {
        self.timeout_at = Some(now + 4 * self.char_time);
    }

    /// The FIFO timeout, if it fell due before `now` (QEMU's timer callback, run late at the next access).
    fn timeout(&mut self, now: Instant) {
        if self.timeout_at.is_some_and(|t| t <= now) {
            self.timeout_at = None;
            if !self.recv.is_empty() {
                self.timeout_ipending = true;
                self.update_irq();
            }
        }
    }

    /// How many bytes the receive line may take now (QEMU's `serial_can_receive`, 557-576).
    fn can_receive(&self) -> usize {
        match self.fcr & FCR_FE != 0 {
            true if self.recv.len() >= FIFO => 0,
            true if self.recv.len() <= self.recv_itl => self.recv_itl - self.recv.len(),
            true => 1,
            false => usize::from(self.lsr & LSR_DR == 0),
        }
    }

    /// A byte arriving on the receive line (in loopback: every byte transmitted).
    pub(crate) fn receive(&mut self, byte: u8, now: Instant) {
        if self.fcr & FCR_FE != 0 {
            if self.recv.len() < FIFO {
                self.recv.push_back(byte);
            } else {
                self.lsr |= LSR_OE;
            }
            self.lsr |= LSR_DR;
            self.arm_timeout(now);
        } else {
            if self.lsr & LSR_DR != 0 {
                self.lsr |= LSR_OE;
            }
            self.rbr = byte;
            self.lsr |= LSR_DR;
        }
        self.update_irq();
    }

    /// Emission alone does not publish empty status: synchronous spill must not regenerate per-byte IRQs.
    fn flush_tx(&mut self) {
        if self.tx.is_empty() {
            return;
        }
        match &mut self.sink {
            Some(sink) => {
                if let Err(error) = sink.write_all(&self.tx).and_then(|_| sink.flush()) {
                    if self.sink_error.is_none() {
                        self.sink_error = Some(error.to_string());
                    }
                    self.sink = None;
                }
            }
            None if self.sink_error.is_none() => self.output.extend_from_slice(&self.tx),
            None => {}
        }
        self.tx.clear();
    }
    fn finish_tx(&mut self) {
        self.flush_tx();
        self.tx_at = None;
        self.lsr |= LSR_THRE | LSR_TEMT;
        self.thr_ipending = true;
        self.update_irq();
        // A halted guest must not be declared finished between dropping busy and publishing its IRQ.
        self.tx_busy = None;
    }
    fn fire_tx(&mut self, at: u64) {
        if self.tx_at.is_some_and(|deadline| deadline <= at) {
            self.finish_tx();
        }
    }
    fn shutdown(&mut self) {
        if self.tx_stopped {
            return;
        }
        self.tx_stopped = true;
        // The board has detached at VM end. Do not invoke even a detached callback during final drain.
        self.irq = None;
        if self.tx_at.is_some() {
            self.finish_tx();
        } else {
            self.flush_tx();
        }
    }
    fn transmit(&mut self, byte: u8, now: Instant) {
        self.thr_ipending = false;
        self.lsr &= !(LSR_THRE | LSR_TEMT);
        self.update_irq();
        if self.mcr & MCR_LOOP != 0 {
            // Keep the established instantaneous loopback/probe behavior.
            self.lsr |= LSR_THRE;
            self.thr_ipending = true;
            self.update_irq();
            self.receive(byte, now);
            self.lsr |= LSR_TEMT;
        } else {
            // THR acceptance commits the byte and count, independent of later sink failure/reset.
            self.sent += 1;
            if self.tx_busy.is_none() {
                self.tx_busy = self.tx_irqs.as_ref().map(|i| i.busy());
            }
            self.tx.push(byte);
            if self.tx.len() == TX_BATCH {
                self.flush_tx();
            }
            // Inactivity starts after any synchronous native spill, so even a slow sink cannot
            // consume the entire ready delay before this OUT returns. No baud-rate wallclock cap.
            self.tx_at = Some(crate::clock::now().saturating_add(self.tx_delay));
        }
    }

    pub(crate) fn write_at(&mut self, offset: u64, value: u8, now: Instant) {
        self.timeout(now);
        match offset {
            0 | 1 if self.lcr & LCR_DLAB != 0 => {
                let shift = 8 * offset;
                self.divider = self.divider & !(0xff << shift) | u16::from(value) << shift;
                self.update_parameters();
            }
            0 => self.transmit(value, now),
            1 => {
                let changed = (self.ier ^ value) & 0x0f;
                self.ier = value & 0x0f;
                if changed & IER_THRI != 0 {
                    self.thr_ipending = self.ier & IER_THRI != 0 && self.lsr & LSR_THRE != 0;
                }
                if changed != 0 {
                    self.update_irq();
                }
            }
            2 => {
                let mut value = value;
                if (value ^ self.fcr) & FCR_FE != 0 {
                    value |= FCR_XFR | FCR_RFR;
                }
                if value & FCR_RFR != 0 {
                    self.lsr &= !(LSR_DR | LSR_BI);
                    self.timeout_at = None;
                    self.timeout_ipending = false;
                    self.recv.clear();
                }
                if value & FCR_XFR != 0 {
                    // These bytes were already accepted by the old instantaneous sink. Reset
                    // flushes their host staging; it cannot retract or silently discard them.
                    if self.tx_at.is_some() {
                        self.finish_tx();
                    }
                    self.lsr |= LSR_THRE;
                    self.thr_ipending = true;
                }
                self.fcr = value & 0xc9;
                if self.fcr & FCR_FE != 0 {
                    self.iir |= IIR_FE;
                    self.recv_itl = [1, 4, 8, 14][usize::from(self.fcr >> 6)];
                } else {
                    self.iir &= !IIR_FE;
                }
                self.update_irq();
            }
            3 => {
                self.lcr = value;
                self.update_parameters();
            }
            4 => {
                // An external byte remains external even if the guest enters loopback before it drains.
                if (self.mcr ^ value) & MCR_LOOP != 0 && self.tx_at.is_some() {
                    self.finish_tx();
                }
                self.mcr = value & 0x1f;
            }
            7 => self.scr = value,
            _ => {} // LSR, MSR: read-only
        }
    }

    pub(crate) fn read_at(&mut self, offset: u64, now: Instant) -> u8 {
        self.timeout(now);
        match offset {
            0 | 1 if self.lcr & LCR_DLAB != 0 => (self.divider >> (8 * offset)) as u8,
            0 => {
                let value = if self.fcr & FCR_FE != 0 {
                    let value = self.recv.pop_front().unwrap_or(0);
                    if self.recv.is_empty() {
                        self.lsr &= !(LSR_DR | LSR_BI);
                    } else {
                        self.arm_timeout(now);
                    }
                    self.timeout_ipending = false;
                    value
                } else {
                    self.lsr &= !(LSR_DR | LSR_BI);
                    self.rbr
                };
                self.update_irq();
                value
            }
            1 => self.ier,
            2 => {
                let value = self.iir;
                if value & IIR_ID == IIR_THRI {
                    self.thr_ipending = false;
                    self.update_irq();
                }
                value
            }
            3 => self.lcr,
            4 => self.mcr,
            5 => {
                let value = self.lsr;
                if self.lsr & (LSR_BI | LSR_OE) != 0 {
                    self.lsr &= !(LSR_BI | LSR_OE);
                    self.update_irq();
                }
                value
            }
            6 if self.mcr & MCR_LOOP != 0 => {
                (self.mcr & 0x0c) << 4 | (self.mcr & 0x02) << 3 | (self.mcr & 0x01) << 5
            }
            6 => self.msr, // no deltas without modem control
            _ => self.scr,
        }
    }
}

impl Device for Uart {
    fn read(&mut self, _vcpu: usize, offset: u64, size: u64) -> Option<u64> {
        self.fire_tx(crate::clock::now());
        let now = Instant::now();
        Some((0..size).fold(0, |v, k| {
            v | u64::from(self.read_at((offset + k) & 7, now)) << (8 * k)
        }))
    }
    fn write(&mut self, _vcpu: usize, offset: u64, size: u64, value: u64) -> bool {
        if self.tx_stopped {
            return false;
        }
        self.fire_tx(crate::clock::now());
        let now = Instant::now();
        (0..size).for_each(|k| self.write_at((offset + k) & 7, (value >> (8 * k)) as u8, now));
        true
    }
}

/// The existing timer owner services the same UART reached by IRQ wiring and host RX.
/// Outer timer lock -> UART lock; RX never takes the outer timer lock.
struct TimedUart(Arc<Mutex<Uart>>);
impl Device for TimedUart {
    fn read(&mut self, v: usize, o: u64, n: u64) -> Option<u64> {
        self.0
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .read(v, o, n)
    }
    fn write(&mut self, v: usize, o: u64, n: u64, x: u64) -> bool {
        self.0
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .write(v, o, n, x)
    }
}
impl crate::clock::Timed for TimedUart {
    fn next(&self) -> Option<u64> {
        self.0.lock().unwrap_or_else(|e| e.into_inner()).tx_at
    }
    fn fire(&mut self, at: u64) {
        self.0.lock().unwrap_or_else(|e| e.into_inner()).fire_tx(at);
    }
    fn shutdown(&mut self) {
        self.0.lock().unwrap_or_else(|e| e.into_inner()).shutdown();
    }
}
pub(crate) fn install(
    devices: &mut crate::devices::Devices,
    irqs: &crate::irq::Irqs,
    uart: &Arc<Mutex<Uart>>,
) -> crate::Result<Arc<dyn crate::clock::Halt>> {
    uart.lock().unwrap_or_else(|e| e.into_inner()).tx_irqs = Some(irqs.clone());
    let timer = crate::clock::Timer::new(TimedUart(uart.clone()), "uart-tx");
    let halt = timer.halter();
    devices.register_ports(
        crate::boot::acpi::platform::COM1_PORT.into(),
        8,
        Arc::new(Mutex::new(timer)),
    )?;
    Ok(halt)
}

/// Host input bytes [`feed`] holds for the UART (a reader's bytes past them are dropped while it is full).
const QUEUE: usize = 4096;

/// Feeds `input` to `uart`'s receive line until it ends (EOF or a read error: queued bytes are still delivered) or the
/// escape Ctrl-A x, which calls `quit` at once (QEMU's mux escapes: Ctrl-A Ctrl-A sends one Ctrl-A, Ctrl-A and any other
/// byte sends nothing; on piped input too, so a lone 0x01 byte is never passed on). Every other byte is passed on,
/// Ctrl-C included. A reader thread takes `input` as it arrives, acts on the escapes and queues the rest (at most
/// [`QUEUE`] bytes; past that, bytes are dropped), so the escape works while the guest reads nothing; this thread gives
/// each queued byte to the UART when it can take it ([`Uart::can_receive`]) and runs the FIFO timeout when it falls due
/// with nothing queued. For a thread of its own: it returns when the reader ends (which blocks in `input`).
pub fn feed(uart: &Mutex<Uart>, mut input: impl Read + Send, quit: impl FnOnce() + Send) {
    use std::sync::atomic::{AtomicBool, Ordering::SeqCst};
    use std::sync::mpsc::{TryRecvError, sync_channel};
    let lock = || uart.lock().unwrap_or_else(|e| e.into_inner());
    let (queue, queued) = sync_channel::<u8>(QUEUE);
    let quitting = &AtomicBool::new(false);
    std::thread::scope(|s| {
        // `move`: the reader's end drops the queue's sender, which ends this thread's wait.
        s.spawn(move || {
            let (mut buf, mut escape) = ([0u8; 256], false);
            loop {
                let n = match input.read(&mut buf) {
                    Ok(0) => return,
                    Ok(n) => n,
                    Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                    Err(_) => return,
                };
                for &byte in &buf[..n] {
                    match (std::mem::take(&mut escape), byte) {
                        (false, 1) => escape = true,
                        (true, b'x') => {
                            quitting.store(true, SeqCst);
                            return quit();
                        }
                        (true, b) if b != 1 => {}
                        // ponytail: a full queue drops the byte silently; count drops if anyone needs to know.
                        _ => _ = queue.try_send(byte),
                    }
                }
            }
        });
        loop {
            let byte = match queued.try_recv() {
                Ok(byte) => byte,
                Err(ended) => {
                    // Nothing queued: the FIFO timeout on time, then wait for input (unless it ended).
                    loop {
                        let mut u = lock();
                        let now = Instant::now();
                        u.timeout(now);
                        match u.timeout_at {
                            Some(at) if !u.recv.is_empty() => {
                                drop(u);
                                std::thread::sleep(at.saturating_duration_since(now));
                            }
                            _ => break,
                        }
                    }
                    if ended == TryRecvError::Disconnected {
                        return;
                    }
                    match queued.recv() {
                        Ok(byte) => byte,
                        Err(_) => return,
                    }
                }
            };
            // ponytail: polls every millisecond for room; a condvar signalled by RBR reads if typing latency matters.
            loop {
                if quitting.load(SeqCst) {
                    return;
                }
                let mut u = lock();
                let now = Instant::now();
                u.timeout(now);
                if u.can_receive() > 0 {
                    u.receive(byte, now);
                    break;
                }
                drop(u);
                std::thread::sleep(Duration::from_millis(1));
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Linux's 8250 `autoconfig` probe (drivers/tty/serial/8250/8250_port.c, v6.12) and earlyprintk's init, with the values
    /// QEMU's model gives (worked through its source above).
    #[test]
    fn linux_probe_sequences() {
        let t = Instant::now();
        let mut u = Uart::new();
        // Scratch test: IER 0 reads 0, IER 0x0f reads 0x0f.
        u.write_at(1, 0, t);
        assert_eq!(u.read_at(1, t) & 0x0f, 0);
        u.write_at(1, 0x0f, t);
        assert_eq!(u.read_at(1, t) & 0x0f, 0x0f);
        assert_eq!(u.read_at(2, t), IIR_THRI); // THRE pending (THRI resampled), no FIFO yet
        assert_eq!(u.read_at(2, t), IIR_NO_INT); // reading IIR clears the THRE interrupt
        u.write_at(1, 0, t);
        // Loopback: MCR = LOOP | OUT2 | RTS -> MSR high nibble DCD | CTS.
        u.write_at(4, MCR_LOOP | 0x0a, t);
        assert_eq!(u.read_at(6, t) & 0xf0, 0x90);
        u.write_at(4, MCR_OUT2, t);
        // FIFO detection: LCR 0xbf (EFR write lands in FCR: 0), LCR 0, FCR enable -> IIR bits 7:6 = 3 (16550A).
        u.write_at(3, 0xbf, t);
        u.write_at(2, 0, t);
        u.write_at(3, 0, t);
        u.write_at(2, FCR_FE, t);
        assert_eq!(u.read_at(2, t) >> 6, 3);
        // Divisor latch: earlyprintk at 115200 (divisor 1).
        u.write_at(3, 0x03 | LCR_DLAB, t);
        u.write_at(0, 1, t);
        u.write_at(1, 0, t);
        assert_eq!((u.read_at(0, t), u.read_at(1, t)), (1, 0));
        u.write_at(3, 0x03, t);
        assert_eq!(u.read_at(5, t) & 0x60, 0x60);
        u.write_at(0, b'A', t);
        assert_eq!(u.read_at(5, t) & 0x60, 0);
        u.fire_tx(u.tx_at.unwrap());
        assert_eq!(u.read_at(5, t), LSR_TEMT | LSR_THRE);
        assert_eq!(u.output, b"A");
    }

    #[test]
    fn loopback_fifo_overrun_and_timeout() {
        let t = Instant::now();
        let mut u = Uart::new();
        u.write_at(2, FCR_FE | 0xc0, t); // FIFO, trigger 14
        u.write_at(4, MCR_LOOP, t);
        u.write_at(1, IER_RDI | IER_RLSI, t);
        for b in 0..13u8 {
            u.write_at(0, b, t);
        }
        assert_eq!((u.read_at(2, t) & 0x0f, u.line), (IIR_NO_INT, false)); // below the trigger level
        for b in 13..20u8 {
            u.write_at(0, b, t);
        }
        // 16 kept, overrun: RLSI first; reading LSR clears OE, then RDI (16 >= 14).
        assert_eq!(u.read_at(2, t) & 0x0f, IIR_RLSI);
        assert_eq!(u.read_at(5, t) & (LSR_OE | LSR_DR), LSR_OE | LSR_DR);
        assert_eq!((u.read_at(2, t) & 0x0f, u.line), (IIR_RDI, true));
        let got: Vec<u8> = (0..3).map(|_| u.read_at(0, t)).collect();
        assert_eq!(got, [0, 1, 2]);
        assert_eq!(u.read_at(2, t) & 0x0f, IIR_NO_INT); // 13 left, below 14
        let late = t + 5 * u.char_time;
        assert_eq!(u.read_at(2, late) & 0x0f, IIR_CTI); // the character timeout
        assert_eq!(u.read_at(0, late), 3); // an RBR read clears it
        assert_eq!(u.read_at(2, late) & 0x0f, IIR_NO_INT);
        assert!(u.output.is_empty());
    }

    #[test]
    fn wide_accesses_split_into_bytes() {
        let mut u = Uart::new();
        assert!(u.write(0, 6, 2, 0x5a00)); // MSR (ignored), SCR = 0x5a
        assert_eq!(u.read(0, 4, 4), Some(0x5a_b0_60_08)); // MCR, LSR, MSR, SCR
        let seen = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let log = seen.clone();
        u.irq = Some(Box::new(move |level| log.lock().unwrap().push(level)));
        u.write_at(1, IER_THRI, Instant::now()); // THRE pending: the line rises
        u.write_at(0, b'x', Instant::now()); // THR lowers the line; only delayed completion raises it.
        assert_eq!(*seen.lock().unwrap(), [true, false]);
        u.fire_tx(u.tx_at.unwrap());
        assert_eq!(*seen.lock().unwrap(), [true, false, true]);
    }

    #[test]
    fn accepted_batches_wait_for_empty_publication() {
        let t = Instant::now();
        let mut u = Uart::new();
        u.write_at(1, IER_THRI, t);
        u.read_at(2, t);
        for b in 0..35 {
            u.write_at(0, b, t);
        }
        assert_eq!(u.sent, 35);
        assert_eq!(u.output, (0..32).collect::<Vec<u8>>());
        assert_eq!(u.tx, [32, 33, 34]);
        assert!(!u.line);
        assert_eq!(u.read_at(5, t) & 0x60, 0);
        let at = u.tx_at.unwrap();
        u.fire_tx(at - 1);
        assert!(!u.line);
        u.fire_tx(at);
        assert_eq!(u.output, (0..35).collect::<Vec<u8>>());
        assert_eq!(u.read_at(5, t) & 0x60, 0x60);
        assert_eq!(u.read_at(2, t), IIR_THRI);
        assert_eq!(u.read_at(2, t), IIR_NO_INT);
    }

    #[test]
    fn resets_and_loopback_preserve_accepted_external_bytes() {
        let t = Instant::now();
        let mut u = Uart::new();
        u.write_at(0, b'a', t);
        u.write_at(2, FCR_FE, t); // mode change implies TX reset
        assert_eq!(u.output, b"a");
        assert!(u.tx_at.is_none());
        u.write_at(0, b'b', t);
        u.write_at(2, FCR_FE | FCR_XFR, t);
        assert_eq!(u.output, b"ab");
        u.write_at(0, b'c', t);
        u.write_at(4, MCR_LOOP, t);
        u.write_at(0, b'd', t);
        assert_eq!(u.output, b"abc");
        assert_eq!(u.read_at(0, t), b'd');
        assert_eq!(u.sent, 3);
        assert!(u.tx_at.is_none());
    }

    #[test]
    fn partial_output_drains_on_shutdown_once_and_errors_survive() {
        struct Fails(Arc<Mutex<usize>>);
        impl Write for Fails {
            fn write(&mut self, _: &[u8]) -> std::io::Result<usize> {
                *self.0.lock().unwrap() += 1;
                Err(std::io::Error::other("first sink failure"))
            }
            fn flush(&mut self) -> std::io::Result<()> {
                panic!("write failed first")
            }
        }
        let calls = Arc::new(Mutex::new(0));
        let mut u = Uart::new();
        u.sink = Some(Box::new(Fails(calls.clone())));
        u.write_at(0, b'x', Instant::now());
        u.irq = Some(Box::new(|_| panic!("no callback during shutdown")));
        u.shutdown();
        u.shutdown();
        assert_eq!(*calls.lock().unwrap(), 1);
        assert_eq!(u.sink_error.as_deref(), Some("first sink failure"));
        assert_eq!(u.sent, 1);
        assert!(u.output.is_empty());
        assert!(!u.write(0, 0, 1, b'y'.into()));
        assert!(u.tx_at.is_none());
    }

    #[test]
    fn polling_services_due_and_flush_failure_is_retained() {
        struct FlushFails;
        impl Write for FlushFails {
            fn write(&mut self, b: &[u8]) -> std::io::Result<usize> {
                Ok(b.len())
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Err(std::io::Error::other("flush failure"))
            }
        }
        let mut u = Uart::new();
        u.sink = Some(Box::new(FlushFails));
        u.write_at(0, b'x', Instant::now());
        u.tx_at = Some(0); // deterministic due point, serviced by the normal polling path
        assert_eq!(u.read(0, 5, 1), Some(0x60));
        assert_eq!(u.sink_error.as_deref(), Some("flush failure"));
        u.write_at(0, b'y', Instant::now());
        u.shutdown();
        assert_eq!(u.sent, 2);
        assert_eq!(u.sink_error.as_deref(), Some("flush failure"));
        assert!(u.output.is_empty());
    }

    #[test]
    fn installed_uart_acquires_busy_and_fires_without_access() {
        let memory = Arc::new(crate::memory::Memory::with_size(4096).unwrap());
        let uart = Arc::new(Mutex::new(Uart::new()));
        let mut devices = crate::devices::Devices::new(0);
        let irqs = crate::irq::install(&mut devices, &memory, 0, &uart).unwrap();
        let halt = install(&mut devices, &irqs, &uart).unwrap();
        let (send, recv) = std::sync::mpsc::channel();
        {
            let mut u = uart.lock().unwrap();
            u.tx_delay = crate::clock::frequency() / 10;
            u.write_at(1, IER_THRI, Instant::now());
            u.read_at(2, Instant::now());
            let active = irqs.clone();
            u.irq = Some(Box::new(move |level| {
                if level {
                    send.send(active.live()).unwrap();
                }
            }));
        }
        assert_eq!(irqs.live(), 0);
        devices.port(0, 0x3f8, 1, Some(b'x'.into())).unwrap();
        assert_eq!(irqs.live(), 1); // the actual installed path vetoes all-HLT termination
        assert_eq!(recv.recv_timeout(Duration::from_secs(2)).unwrap(), 1);
        let u = uart.lock().unwrap();
        assert_eq!(u.output, b"x");
        assert_eq!(irqs.live(), 0);
        drop(u);
        halt.halt();
    }

    #[test]
    fn concurrent_halt_waits_for_the_owned_drain() {
        struct Gated {
            entered: std::sync::mpsc::Sender<()>,
            release: std::sync::mpsc::Receiver<()>,
        }
        impl Write for Gated {
            fn write(&mut self, b: &[u8]) -> std::io::Result<usize> {
                self.entered.send(()).unwrap();
                self.release.recv_timeout(Duration::from_secs(2)).unwrap();
                Ok(b.len())
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        let (entered, entering) = std::sync::mpsc::channel();
        let (release, released) = std::sync::mpsc::channel();
        let mut u = Uart::new();
        u.tx_delay = crate::clock::frequency() * 60;
        u.sink = Some(Box::new(Gated {
            entered,
            release: released,
        }));
        let uart = Arc::new(Mutex::new(u));
        let mut timer = crate::clock::Timer::new(TimedUart(uart.clone()), "uart-halt-test");
        timer.write(0, 0, 1, b'x'.into());
        let first = timer.halter();
        let second = timer.halter();
        let (done, completed) = std::sync::mpsc::channel();
        let a = std::thread::spawn(move || first.halt());
        entering.recv_timeout(Duration::from_secs(1)).unwrap();
        let b = std::thread::spawn(move || {
            second.halt();
            done.send(()).unwrap();
        });
        let premature = completed.recv_timeout(Duration::from_millis(20)).is_ok();
        release.send(()).unwrap();
        a.join().unwrap();
        b.join().unwrap();
        assert!(
            !premature,
            "a concurrent halt returned before the drain finished"
        );
        assert!(uart.lock().unwrap().tx.is_empty());
    }

    #[test]
    fn timer_publishes_ready_without_port_access_and_halt_drains() {
        use std::sync::atomic::{AtomicUsize, Ordering::SeqCst};
        let busy = Arc::new(AtomicUsize::new(1));
        let check = busy.clone();
        let (send, recv) = std::sync::mpsc::channel();
        let mut u = Uart::new();
        u.write_at(1, IER_THRI, Instant::now());
        u.read_at(2, Instant::now());
        u.tx_busy = Some(crate::irq::Busy(busy.clone()));
        u.irq = Some(Box::new(move |level| {
            if level {
                assert_eq!(check.load(SeqCst), 1);
                send.send(()).unwrap();
            }
        }));
        let uart = Arc::new(Mutex::new(u));
        let mut timer = crate::clock::Timer::new(TimedUart(uart.clone()), "uart-test");
        assert!(timer.write(0, 0, 1, b'x'.into()));
        recv.recv_timeout(Duration::from_secs(1))
            .expect("timer completion without guest access");
        {
            let u = uart.lock().unwrap();
            assert_eq!(u.output, b"x");
            assert_eq!(busy.load(SeqCst), 0);
        }
        // Long deadline makes the second byte a shutdown drain, not a timed completion.
        uart.lock().unwrap().tx_delay = crate::clock::frequency() * 60;
        assert!(timer.write(0, 0, 1, b'y'.into()));
        let h = timer.halter();
        h.halt();
        h.halt();
        assert_eq!(uart.lock().unwrap().output, b"xy");
        drop(timer); // joins; no post-halt callback
    }

    /// Host input on a thread: bytes below the trigger level raise the line by the CTI with no access; the escapes; no
    /// FIFO: the second byte waits until the first is read (no overrun).
    #[test]
    fn feed_raises_the_line_and_waits_for_room() {
        let t = Instant::now();
        let mut u = Uart::new();
        u.write_at(2, FCR_FE | 0x80, t); // FIFO, trigger 8
        u.write_at(1, IER_RDI, t);
        let rises = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let r = rises.clone();
        u.irq = Some(Box::new(move |level| {
            r.fetch_add(usize::from(level), std::sync::atomic::Ordering::SeqCst);
        }));
        let u = Mutex::new(u);
        // The input ends: what was queued is delivered and the CTI runs before `feed` returns.
        feed(&u, &b"ab\x01\x01c\x01zd"[..], || panic!("no Ctrl-A x"));
        let mut g = u.lock().unwrap();
        assert_eq!(
            (g.recv.iter().copied().collect::<Vec<_>>(), g.line),
            (b"ab\x01cd".to_vec(), true)
        );
        assert_eq!(
            (
                g.read_at(2, Instant::now()) & 0x0f,
                rises.load(std::sync::atomic::Ordering::SeqCst)
            ),
            (IIR_CTI, 1)
        );
        g.write_at(2, 0, Instant::now()); // FIFO off (and cleared)
        drop(g);
        std::thread::scope(|s| {
            s.spawn(|| feed(&u, &b"xy"[..], || {}));
            std::thread::sleep(Duration::from_millis(20));
            let mut g = u.lock().unwrap();
            assert_eq!((g.lsr & (LSR_DR | LSR_OE), g.rbr), (LSR_DR, b'x'));
            assert_eq!(g.read_at(0, Instant::now()), b'x');
        });
        let mut g = u.lock().unwrap();
        assert_eq!(
            (g.lsr & (LSR_DR | LSR_OE), g.read_at(0, Instant::now())),
            (LSR_DR, b'y')
        );
    }

    /// Review of b55a0f4: Ctrl-A x works while the guest reads nothing (no FIFO: one byte fits, the rest wait), on input
    /// that has not ended; `feed` returns at once.
    #[test]
    fn the_escape_is_not_stuck_behind_unread_input() {
        let u = Mutex::new(Uart::new());
        let (reader, mut writer) = std::io::pipe().unwrap();
        let quit = std::sync::mpsc::channel();
        std::thread::scope(|s| {
            let fed = s.spawn(|| feed(&u, reader, move || quit.0.send(()).unwrap()));
            writer.write_all(b"abc").unwrap();
            std::thread::sleep(Duration::from_millis(20));
            assert_eq!(u.lock().unwrap().rbr, b'a');
            writer.write_all(b"\x01x").unwrap();
            assert!(quit.1.recv_timeout(Duration::from_secs(2)).is_ok());
            let t = Instant::now();
            while !fed.is_finished() && t.elapsed() < Duration::from_secs(2) {
                std::thread::sleep(Duration::from_millis(1));
            }
            assert!(fed.is_finished());
        });
        drop(writer);
    }
}
