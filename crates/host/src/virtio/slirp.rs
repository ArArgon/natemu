//! User-mode NAT network backend (owner, November 12): libslirp, as QEMU's `-netdev user`, behind a
//! [`PacketPort`]. Hand-written bindings for libslirp 4.9 (config version 6). One thread owns the
//! stack: it feeds guest TX frames in, polls slirp's host sockets and a doorbell, fires slirp's
//! timers and queues slirp's frames for the guest. No root, entitlement or host interface.
//!
//! Network: 10.0.2.0/24, gateway 10.0.2.2, DNS 10.0.2.3 (the host's resolvers), DHCP from 10.0.2.15.
//! IPv4 only; the guest cannot reach the host's loopback. IPv6, ICMPv6 echo, port forwarding and a
//! separate contained process are follow-on work (io-plan.md, INV-03-03 claims 59/73).
use super::PacketPort;
use crate::Result;
use std::collections::VecDeque;
use std::ffi::{CStr, c_char, c_int, c_void};
use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::net::UnixStream;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering::SeqCst};
use std::time::Instant;

#[repr(C)]
struct SlirpStack {
    _opaque: [u8; 0],
}
#[repr(C, align(4))]
#[derive(Clone, Copy)]
struct In6([u8; 16]);
/// `struct in_addr`, network byte order.
#[repr(C)]
#[derive(Clone, Copy)]
struct In4(u32);
const fn ipv4(a: u8, b: u8, c: u8, d: u8) -> In4 {
    In4(u32::from_ne_bytes([a, b, c, d]))
}

#[repr(C)]
struct Config {
    version: u32,
    restricted: c_int,
    in_enabled: bool,
    vnetwork: In4,
    vnetmask: In4,
    vhost: In4,
    in6_enabled: bool,
    vprefix_addr6: In6,
    vprefix_len: u8,
    vhost6: In6,
    vhostname: *const c_char,
    tftp_server_name: *const c_char,
    tftp_path: *const c_char,
    bootfile: *const c_char,
    vdhcp_start: In4,
    vnameserver: In4,
    vnameserver6: In6,
    vdnssearch: *const *const c_char,
    vdomainname: *const c_char,
    if_mtu: usize,
    if_mru: usize,
    disable_host_loopback: bool,
    enable_emu: bool,
    outbound_addr: *const c_void,
    outbound_addr6: *const c_void,
    disable_dns: bool,
    disable_dhcp: bool,
    mfr_id: u32,
    oob_eth_addr: [u8; 6],
}

type Opaque = *mut c_void;
#[repr(C)]
struct Callbacks {
    send_packet: unsafe extern "C" fn(*const c_void, usize, Opaque) -> isize,
    guest_error: unsafe extern "C" fn(*const c_char, Opaque),
    clock_get_ns: unsafe extern "C" fn(Opaque) -> i64,
    timer_new: Option<unsafe extern "C" fn(Opaque, Opaque, Opaque) -> Opaque>,
    timer_free: unsafe extern "C" fn(Opaque, Opaque),
    timer_mod: unsafe extern "C" fn(Opaque, i64, Opaque),
    register_poll_fd: unsafe extern "C" fn(c_int, Opaque),
    unregister_poll_fd: unsafe extern "C" fn(c_int, Opaque),
    notify: unsafe extern "C" fn(Opaque),
    init_completed: Option<unsafe extern "C" fn(*mut SlirpStack, Opaque)>,
    timer_new_opaque: unsafe extern "C" fn(u32, Opaque, Opaque) -> Opaque,
    register_poll_socket: unsafe extern "C" fn(c_int, Opaque),
    unregister_poll_socket: unsafe extern "C" fn(c_int, Opaque),
}

type AddPoll = unsafe extern "C" fn(c_int, c_int, Opaque) -> c_int;
type GetRevents = unsafe extern "C" fn(c_int, Opaque) -> c_int;
// Linked by build.rs.
unsafe extern "C" {
    fn slirp_new(
        cfg: *const Config,
        callbacks: *const Callbacks,
        opaque: Opaque,
    ) -> *mut SlirpStack;
    fn slirp_cleanup(slirp: *mut SlirpStack);
    fn slirp_pollfds_fill_socket(
        slirp: *mut SlirpStack,
        timeout: *mut u32,
        add: AddPoll,
        opaque: Opaque,
    );
    fn slirp_pollfds_poll(slirp: *mut SlirpStack, error: c_int, get: GetRevents, opaque: Opaque);
    fn slirp_input(slirp: *mut SlirpStack, pkt: *const u8, len: c_int);
    fn slirp_handle_timer(slirp: *mut SlirpStack, id: u32, cb_opaque: Opaque);
    fn poll(fds: *mut PollFd, count: u32, timeout_ms: c_int) -> c_int;
}

#[repr(C)]
struct PollFd {
    fd: c_int,
    events: i16,
    revents: i16,
}
/// (slirp, poll(2)) event bits.
const EVENTS: [(c_int, i16); 5] = [(1, 0x1), (2, 0x4), (4, 0x2), (8, 0x8), (16, 0x10)];

/// Frames slirp emitted that the guest has not taken yet; past this, new ones drop (slirp allows it).
/// The device takes one RX frame per TX packet under a full guest upload, so the ACKs for that upload
/// queue here with the downloads' data: 8 streams each way peaked at 718 frames. Drops are costly:
/// slirp's TCP recovers from a lost segment by timeout, and at 256 the downloads collapsed to ~2 Mbit/s
/// (November 12 stress). 4096 frames is at most ~6 MiB.
const RX_QUEUE: usize = 4096;
/// Upper bound on one wait: a refused frame is retried at least this often (ms).
const RETRY_MS: u32 = 100;
/// Guest TX packets fed to slirp per pass: a continuous TX stream must not starve RX delivery
/// (its own ACKs) or slirp's sockets. Each queued TX packet has rung the doorbell, so the next
/// poll returns at once.
const TX_BATCH: usize = 64;

struct Timer {
    id: u32,
    cb_opaque: Opaque,
    /// Expiry in ms of `clock_get_ns` time.
    expire: Option<i64>,
}
/// What slirp's callbacks touch, through their opaque pointer; the loop touches it only between
/// slirp calls.
struct Context {
    rx: VecDeque<Vec<u8>>,
    timers: Vec<*mut Timer>,
    epoch: Instant,
    /// Frames slirp emitted while `rx` was full.
    dropped: u64,
    /// The doorbell's write end.
    ring: UnixStream,
}
impl Context {
    fn now_ns(&self) -> i64 {
        self.epoch.elapsed().as_nanos() as i64
    }
}
unsafe fn context<'a>(opaque: Opaque) -> &'a mut Context {
    unsafe { &mut *opaque.cast::<Context>() }
}
unsafe extern "C" fn send_packet(buf: *const c_void, len: usize, opaque: Opaque) -> isize {
    let c = unsafe { context(opaque) };
    if c.rx.len() < RX_QUEUE {
        c.rx.push_back(unsafe { std::slice::from_raw_parts(buf.cast::<u8>(), len) }.to_vec());
    } else {
        c.dropped += 1;
    }
    len as isize
}
unsafe extern "C" fn guest_error(msg: *const c_char, _: Opaque) {
    eprintln!(
        "natemu: network: {}",
        unsafe { CStr::from_ptr(msg) }.to_string_lossy()
    );
}
unsafe extern "C" fn clock_get_ns(opaque: Opaque) -> i64 {
    unsafe { context(opaque) }.now_ns()
}
unsafe extern "C" fn timer_new_opaque(id: u32, cb_opaque: Opaque, opaque: Opaque) -> Opaque {
    let timer = Box::into_raw(Box::new(Timer {
        id,
        cb_opaque,
        expire: None,
    }));
    unsafe { context(opaque) }.timers.push(timer);
    timer.cast()
}
unsafe extern "C" fn timer_free(timer: Opaque, opaque: Opaque) {
    let timer = timer.cast::<Timer>();
    unsafe { context(opaque) }.timers.retain(|&t| t != timer);
    drop(unsafe { Box::from_raw(timer) });
}
unsafe extern "C" fn timer_mod(timer: Opaque, expire_ms: i64, _: Opaque) {
    unsafe { &mut *timer.cast::<Timer>() }.expire = Some(expire_ms);
}
unsafe extern "C" fn no_fd(_: c_int, _: Opaque) {}
unsafe extern "C" fn notify(opaque: Opaque) {
    let _ = (&unsafe { context(opaque) }.ring).write(&[1]);
}
unsafe extern "C" fn add_poll(fd: c_int, events: c_int, opaque: Opaque) -> c_int {
    let fds = unsafe { &mut *opaque.cast::<Vec<PollFd>>() };
    let events = EVENTS
        .iter()
        .filter(|e| events & e.0 != 0)
        .fold(0, |a, e| a | e.1);
    fds.push(PollFd {
        fd,
        events,
        revents: 0,
    });
    fds.len() as c_int - 1
}
unsafe extern "C" fn get_revents(idx: c_int, opaque: Opaque) -> c_int {
    let fds = unsafe { &*opaque.cast::<Vec<PollFd>>() };
    let revents = fds.get(idx as usize).map_or(0, |f| f.revents);
    EVENTS
        .iter()
        .filter(|e| revents & e.1 != 0)
        .fold(0, |a, e| a | e.0)
}
static CALLBACKS: Callbacks = Callbacks {
    send_packet,
    guest_error,
    clock_get_ns,
    timer_new: None,
    timer_free,
    timer_mod,
    register_poll_fd: no_fd,
    unregister_poll_fd: no_fd,
    notify,
    init_completed: None,
    timer_new_opaque,
    register_poll_socket: no_fd,
    unregister_poll_socket: no_fd,
};

/// A started user-mode network serving one [`PacketPort`]; [`Slirp::run`] on its own thread.
pub struct Slirp {
    stack: *mut SlirpStack,
    context: *mut Context,
    port: PacketPort,
    bell: UnixStream,
    /// Set while the loop may sleep in `poll`: only then does the device write the doorbell (a
    /// syscall it makes under its state lock). Armed before the loop's last look at the device's
    /// state (which takes that lock), so a change the device makes after that look sees it armed.
    armed: Arc<AtomicBool>,
    fds: Vec<PollFd>,
}
// The stack and its context are used by one thread at a time: the creator, then `run`'s.
unsafe impl Send for Slirp {}

impl Slirp {
    /// The guest MAC the device should offer (QEMU's default).
    pub const MAC: [u8; 6] = [0x52, 0x54, 0x00, 0x12, 0x34, 0x56];

    pub fn new(port: PacketPort) -> Result<Self> {
        let (bell, ring) = UnixStream::pair().map_err(|e| format!("network doorbell: {e}"))?;
        for s in [&bell, &ring] {
            s.set_nonblocking(true)
                .map_err(|e| format!("network doorbell: {e}"))?;
        }
        let ring2 = ring
            .try_clone()
            .map_err(|e| format!("network doorbell: {e}"))?;
        let armed = Arc::new(AtomicBool::new(false));
        let armed2 = armed.clone();
        port.on_ready(move || {
            if armed2.swap(false, SeqCst) {
                let _ = (&ring2).write(&[1]);
            }
        });
        let context = Box::into_raw(Box::new(Context {
            rx: VecDeque::new(),
            timers: Vec::new(),
            epoch: Instant::now(),
            dropped: 0,
            ring,
        }));
        let config = Config {
            version: 6,
            restricted: 0,
            in_enabled: true,
            vnetwork: ipv4(10, 0, 2, 0),
            vnetmask: ipv4(255, 255, 255, 0),
            vhost: ipv4(10, 0, 2, 2),
            in6_enabled: false,
            vprefix_addr6: In6([0; 16]),
            vprefix_len: 0,
            vhost6: In6([0; 16]),
            vhostname: std::ptr::null(),
            tftp_server_name: std::ptr::null(),
            tftp_path: std::ptr::null(),
            bootfile: std::ptr::null(),
            vdhcp_start: ipv4(10, 0, 2, 15),
            vnameserver: ipv4(10, 0, 2, 3),
            vnameserver6: In6([0; 16]),
            vdnssearch: std::ptr::null(),
            vdomainname: std::ptr::null(),
            if_mtu: 1500,
            if_mru: 1500,
            disable_host_loopback: true,
            enable_emu: false,
            outbound_addr: std::ptr::null(),
            outbound_addr6: std::ptr::null(),
            disable_dns: false,
            disable_dhcp: false,
            mfr_id: 0,
            oob_eth_addr: [0; 6],
        };
        let stack = unsafe { slirp_new(&config, &CALLBACKS, context.cast()) };
        let slirp = Self {
            stack,
            context,
            port,
            bell,
            armed,
            fds: Vec::new(),
        };
        if stack.is_null() {
            return Err("network: libslirp did not start".into());
        }
        Ok(slirp)
    }

    /// Serve until the device stops or drains. An error is terminal for the VM.
    pub fn run(mut self) -> Result<()> {
        while !self.port.closed() {
            self.service()?;
            // Arm, then look once more: work that arrived before arming is found here, work after
            // it rings the doorbell.
            self.armed.store(true, SeqCst);
            let busy = self.service()? || self.port.closed();
            self.wait(busy);
            self.armed.store(false, SeqCst);
            self.fire_timers();
            self.deliver();
        }
        // A packet accepted just before the stop still gets its one result (the owner waits for it).
        let stack = self.stack;
        while let Ok(true) = self.port.transmit(|frame| {
            unsafe { slirp_input(stack, frame.as_ptr(), frame.len() as c_int) };
            Ok(())
        }) {}
        if crate::perf::on() {
            let dropped = unsafe { &*self.context }.dropped;
            crate::perf::line(&format!(
                "net {{\"backend_dropped\":{dropped},\"guest_lost\":{}}}",
                self.port.lost()
            ));
        }
        Ok(())
    }

    /// Feed up to [`TX_BATCH`] guest TX packets to slirp, then deliver; true if anything moved.
    fn service(&mut self) -> Result<bool> {
        let stack = self.stack;
        let mut moved = false;
        for _ in 0..TX_BATCH {
            let sent = self.port.transmit(|frame| {
                unsafe { slirp_input(stack, frame.as_ptr(), frame.len() as c_int) };
                Ok(())
            })?;
            if !sent {
                break;
            }
            moved = true;
        }
        Ok(self.deliver() || moved)
    }

    /// Hand queued frames to the guest in order until one is refused (slot busy or queue not
    /// ready); true if any was taken.
    fn deliver(&mut self) -> bool {
        let c = unsafe { &mut *self.context };
        let mut moved = false;
        while let Some(frame) = c.rx.front() {
            // Slirp frames lack the FCS; an out-of-profile one could never be taken.
            if (14..=1518).contains(&frame.len()) && !self.port.receive(frame) {
                break;
            }
            c.rx.pop_front();
            moved = true;
        }
        moved
    }

    /// Poll slirp's sockets and the doorbell until something happens or the next deadline (at once
    /// if `busy`), then let slirp act on what is ready.
    fn wait(&mut self, busy: bool) {
        let c = unsafe { &*self.context };
        let mut timeout = match () {
            _ if busy => 0,
            _ if c.rx.is_empty() => 1000,
            _ => RETRY_MS,
        };
        let now_ms = c.now_ns() / 1_000_000;
        for &t in &c.timers {
            if let Some(expire) = unsafe { (*t).expire } {
                timeout =
                    timeout.min(expire.saturating_sub(now_ms).clamp(0, u32::MAX as i64) as u32);
            }
        }
        self.fds.clear();
        self.fds.push(PollFd {
            fd: self.bell.as_raw_fd(),
            events: 0x1,
            revents: 0,
        });
        let fds: Opaque = (&raw mut self.fds).cast();
        unsafe { slirp_pollfds_fill_socket(self.stack, &mut timeout, add_poll, fds) };
        let n = unsafe {
            poll(
                self.fds.as_mut_ptr(),
                self.fds.len() as u32,
                timeout as c_int,
            )
        };
        if self.fds[0].revents != 0 {
            while matches!((&self.bell).read(&mut [0; 64]), Ok(n) if n > 0) {}
        }
        unsafe { slirp_pollfds_poll(self.stack, (n < 0) as c_int, get_revents, fds) };
    }

    fn fire_timers(&mut self) {
        let due: Vec<_> = {
            let c = unsafe { &*self.context };
            let now_ms = c.now_ns() / 1_000_000;
            c.timers
                .iter()
                .copied()
                .filter(|&t| unsafe { (*t).expire }.is_some_and(|e| e <= now_ms))
                .collect()
        };
        for t in due {
            // An earlier handler may have freed or re-armed it. A freed timer's address reused by a new
            // one would fire early; only the IPv6 RA timer exists, so revisit when IPv6 is enabled.
            if !unsafe { &*self.context }.timers.contains(&t) {
                continue;
            }
            let timer = unsafe { &mut *t };
            timer.expire = None;
            unsafe { slirp_handle_timer(self.stack, timer.id, timer.cb_opaque) };
        }
    }
}
impl Drop for Slirp {
    fn drop(&mut self) {
        // Cleanup closes slirp's sockets and frees its timers through the context.
        if !self.stack.is_null() {
            unsafe { slirp_cleanup(self.stack) };
        }
        let context = unsafe { Box::from_raw(self.context) };
        for &t in &context.timers {
            drop(unsafe { Box::from_raw(t) });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::mem::{offset_of, size_of};
    /// Offsets printed by a C program against libslirp 4.9.6's header (aarch64 macOS).
    #[test]
    fn bindings_match_the_c_layout() {
        assert_eq!(size_of::<Config>(), 192);
        assert_eq!(offset_of!(Config, vnetwork), 12);
        assert_eq!(offset_of!(Config, vprefix_addr6), 28);
        assert_eq!(offset_of!(Config, vhost6), 48);
        assert_eq!(offset_of!(Config, vhostname), 64);
        assert_eq!(offset_of!(Config, vdhcp_start), 96);
        assert_eq!(offset_of!(Config, vnameserver6), 104);
        assert_eq!(offset_of!(Config, if_mtu), 136);
        assert_eq!(offset_of!(Config, disable_host_loopback), 152);
        assert_eq!(offset_of!(Config, outbound_addr), 160);
        assert_eq!(offset_of!(Config, disable_dhcp), 177);
        assert_eq!(offset_of!(Config, mfr_id), 180);
        assert_eq!(offset_of!(Config, oob_eth_addr), 184);
        assert_eq!(size_of::<Callbacks>(), 104);
    }
}
