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
//! with no deltas, and in loopback the MCR outputs (OUT2 -> DCD, OUT1 -> RI, RTS -> CTS, DTR -> DSR). Transmission is
//! instantaneous, as in QEMU (THRE and TEMT are set again by the time the THR write completes); in loopback a
//! transmitted byte is received. Output goes to [`Uart::output`] and the driver's sink; input from the host comes through
//! [`feed`] (`--serial-stdin`), as QEMU's character backend gives it (`serial_can_receive` 557-576: never more than the
//! FIFO advertises, so host input never overruns; a guest's own loopback bytes can).
//!
//! **Time.** The FIFO character timeout (CTI, 4 character times after the last receive or RBR read with data left)
//! is evaluated at each register access from the host's monotonic clock: its effect on IIR and the line is the timer's
//! as seen by any access, but the line does not rise between accesses. **IRQ4** ([`Uart::irq`]): the UART drives its
//! interrupt output (high while IIR names an interrupt), wired by L4a (`irq::install`) to ISA IRQ 4: PIC input 4 and I/O
//! APIC pin 4. Today every change is made by a vCPU's own access (synchronous). The hook reports level changes only, and
//! the board treats an edge as a 0 -> 1 change (QEMU's edge-mode I/O APIC delivers on every raise, a raise while already
//! high included: a departure recorded in `ioapic.rs`). Host input is asynchronous: [`feed`]'s thread receives bytes and
//! runs the CTI on time under the UART's lock, so the line rises without an access, through the same hook (IRQ4 into
//! `irq::Irqs::line`, whose delivery rings a halted vCPU); its caller holds an `Irqs::busy` guard while it runs (X3's
//! rule for ending halted vCPUs). ponytail: a CTI due after a guest's partial read, with no host input, still waits for
//! the next access (Linux's 8250 handler drains the FIFO, so it does not arise there).
use crate::devices::Device;
use std::collections::VecDeque;
use std::io::{Read, Write};
use std::sync::Mutex;
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
        } else if self.ier & IER_RDI != 0 && self.lsr & LSR_DR != 0 && (!fifo || self.recv.len() >= self.recv_itl) {
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
        let speed = if self.divider == 0 { 3500.0 } else { BAUD_BASE / f32::from(self.divider) };
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

    fn transmit(&mut self, byte: u8, now: Instant) {
        // The THR write (with a FIFO, through the transmit FIFO, which the instantaneous transmitter empties at once).
        self.thr_ipending = false;
        self.lsr &= !(LSR_THRE | LSR_TEMT);
        self.update_irq();
        self.lsr |= LSR_THRE;
        if !self.thr_ipending {
            self.thr_ipending = true;
            self.update_irq();
        }
        if self.mcr & MCR_LOOP != 0 {
            self.receive(byte, now);
        } else {
            self.sent += 1;
            match &mut self.sink {
                Some(sink) => {
                    if let Err(e) = sink.write_all(&[byte]).and_then(|_| sink.flush()) {
                        self.sink_error = Some(e.to_string());
                        self.sink = None;
                    }
                }
                None if self.sink_error.is_none() => self.output.push(byte),
                None => {}
            }
        }
        self.lsr |= LSR_TEMT;
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
            4 => self.mcr = value & 0x1f,
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
            6 if self.mcr & MCR_LOOP != 0 => (self.mcr & 0x0c) << 4 | (self.mcr & 0x02) << 3 | (self.mcr & 0x01) << 5,
            6 => self.msr, // no deltas without modem control
            _ => self.scr,
        }
    }
}

impl Device for Uart {
    fn read(&mut self, _vcpu: usize, offset: u64, size: u64) -> Option<u64> {
        let now = Instant::now();
        Some((0..size).fold(0, |v, k| v | u64::from(self.read_at((offset + k) & 7, now)) << (8 * k)))
    }
    fn write(&mut self, _vcpu: usize, offset: u64, size: u64, value: u64) -> bool {
        let now = Instant::now();
        (0..size).for_each(|k| self.write_at((offset + k) & 7, (value >> (8 * k)) as u8, now));
        true
    }
}

/// Feeds `input` to `uart`'s receive line until it ends (EOF or a read error) or the escape Ctrl-A x, which calls `quit`
/// (QEMU's mux escapes: Ctrl-A Ctrl-A sends one Ctrl-A, Ctrl-A and any other byte sends nothing). Every other byte is
/// passed on, Ctrl-C included. Each byte waits until the UART can take it ([`Uart::can_receive`]); after each read the
/// FIFO timeout is run when it falls due. For a thread of its own: it blocks in `input`.
pub fn feed(uart: &Mutex<Uart>, mut input: impl Read, quit: impl FnOnce()) {
    let lock = || uart.lock().unwrap_or_else(|e| e.into_inner());
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
                (false, 1) => {
                    escape = true;
                    continue;
                }
                (true, b'x') => return quit(),
                (true, b) if b != 1 => continue,
                _ => {}
            }
            // ponytail: polls every millisecond for room; a condvar signalled by RBR reads if typing latency matters.
            loop {
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
    }
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
        u.write_at(0, b'x', Instant::now()); // the THR write lowers it, the instantaneous transmit raises it again (an edge)
        assert_eq!(*seen.lock().unwrap(), [true, false, true]);
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
        let quit = std::sync::atomic::AtomicBool::new(false);
        // Two reads: the CTI runs after the first, the escape ends the second.
        feed(&u, Read::chain(&b"ab\x01\x01c\x01zd"[..], &b"\x01xe"[..]), || quit.store(true, std::sync::atomic::Ordering::SeqCst));
        let mut g = u.lock().unwrap();
        assert!(quit.into_inner());
        assert_eq!((g.recv.iter().copied().collect::<Vec<_>>(), g.line), (b"ab\x01cd".to_vec(), true));
        assert_eq!((g.read_at(2, Instant::now()) & 0x0f, rises.load(std::sync::atomic::Ordering::SeqCst)), (IIR_CTI, 1));
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
        assert_eq!((g.lsr & (LSR_DR | LSR_OE), g.read_at(0, Instant::now())), (LSR_DR, b'y'));
    }
}
