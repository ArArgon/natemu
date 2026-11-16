//! User-mode NAT network backend (owner, November 12): libslirp, as QEMU's `-netdev user`, behind a
//! [`PacketPort`]. Hand-written bindings for libslirp 4.9 (config version 6). One thread owns the
//! stack: it feeds guest TX frames in, polls slirp's host sockets and a doorbell, fires slirp's
//! timers and queues slirp's frames for the guest. No root, entitlement or host interface.
//!
//! Network: 10.0.2.0/24, gateway 10.0.2.2, DNS 10.0.2.3 (the host's resolvers), DHCP from 10.0.2.15.
//! IPv4 only; localhost forwards are static and guest access to host loopback is opt-in.
//! IPv6, ICMPv6 echo and a separate contained process are follow-on work (io-plan.md, INV-03-03 claims 59/73).
use super::PacketPort;
use crate::Result;
use std::collections::VecDeque;
use std::ffi::{CStr, c_char, c_int, c_void};
use std::io::{Read, Write};
use std::num::NonZeroU16;
use std::os::fd::AsRawFd;
use std::os::unix::net::UnixStream;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering::SeqCst};
use std::time::Instant;

/// Transport of a static localhost listener forwarded into the guest.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ForwardProtocol {
    Tcp,
    Udp,
}

impl std::fmt::Display for ForwardProtocol {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Tcp => "tcp",
            Self::Udp => "udp",
        })
    }
}

/// A localhost listener targeting the fixed DHCP address, 10.0.2.15.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PortForward {
    pub protocol: ForwardProtocol,
    pub host_port: NonZeroU16,
    pub guest_port: NonZeroU16,
}

impl std::fmt::Display for PortForward {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "{} 127.0.0.1:{} -> 10.0.2.15:{}",
            self.protocol, self.host_port, self.guest_port
        )
    }
}

/// Per-launch configuration; host-loopback access is disabled by default and required for UDP forwards.
#[derive(Clone, Debug, Default)]
pub struct SlirpOptions {
    pub forwards: Vec<PortForward>,
    pub host_loopback: bool,
}

impl SlirpOptions {
    /// Validate the complete configuration before any listeners or VM resources are created.
    pub fn validate(&self) -> Result<()> {
        for (i, rule) in self.forwards.iter().enumerate() {
            if rule.protocol == ForwardProtocol::Udp {
                if !self.host_loopback {
                    return Err("network: UDP forwarding requires --net-host-loopback (libslirp replies use host loopback)".into());
                }
                if rule.host_port.get() == 53 {
                    return Err("network: UDP forwarding from host port 53 is unsupported (libslirp reserves gateway DNS)".into());
                }
            }
            if self.forwards[..i]
                .iter()
                .any(|prior| prior.protocol == rule.protocol && prior.host_port == rule.host_port)
            {
                return Err(format!(
                    "network: duplicate {} listener 127.0.0.1:{}",
                    rule.protocol, rule.host_port
                ));
            }
            if rule.protocol == ForwardProtocol::Udp
                && self.forwards[..i].iter().any(|prior| {
                    prior.protocol == ForwardProtocol::Udp && prior.guest_port == rule.guest_port
                })
            {
                return Err(format!(
                    "network: duplicate UDP guest port {} is unsupported (libslirp selects replies by guest port)",
                    rule.guest_port
                ));
            }
        }
        Ok(())
    }
}

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
    fn slirp_add_hostfwd(
        slirp: *mut SlirpStack,
        is_udp: c_int,
        host_addr: In4,
        host_port: c_int,
        guest_addr: In4,
        guest_port: c_int,
    ) -> c_int;
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
    rx: VecDeque<super::net::Frame>,
    buffers: super::net::Buffers,
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
        let frame = unsafe { std::slice::from_raw_parts(buf.cast::<u8>(), len) };
        // Slirp built the frame, checksums included.
        c.rx.push_back(super::net::Frame {
            bytes: c.buffers.take(frame),
            meta: super::net::RxMeta {
                checksum: super::net::RxChecksum::Valid,
                gso: None,
            },
        });
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
    /// Slirp builds every frame it emits, checksums included, in software, and takes frames whose
    /// partial checksum the port completed.
    pub const OFFLOADS: super::Offloads = super::Offloads {
        tx_checksum: true,
        rx_checksum: true,
        tx_segmentation: false,
        rx_segmentation: false,
    };

    pub fn new(port: PacketPort, options: &SlirpOptions) -> Result<Self> {
        options.validate()?;
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
            buffers: port.buffers(),
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
            disable_host_loopback: !options.host_loopback,
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
        // The owner exists before installing anything: every failure drops the stack and closes
        // listeners installed by earlier rules. Addresses are network order; ports are host order.
        for rule in &options.forwards {
            let result = unsafe {
                slirp_add_hostfwd(
                    stack,
                    (rule.protocol == ForwardProtocol::Udp) as c_int,
                    ipv4(127, 0, 0, 1),
                    rule.host_port.get().into(),
                    ipv4(10, 0, 2, 15),
                    rule.guest_port.get().into(),
                )
            };
            if result < 0 {
                return Err(format!("network: could not install {rule}"));
            }
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
            let closed = self.port.closed(); // takes the device lock before the TX and RX looks
            let busy = self.service()? || closed;
            self.wait(busy);
            self.armed.store(false, SeqCst);
            self.fire_timers();
            self.deliver();
        }
        // Packets the guest saw complete before the stop or drain are still sent, once, in order;
        // a terminal failure here still fails the run.
        let stack = self.stack;
        while self.port.transmit(|frame, _| {
            unsafe { slirp_input(stack, frame.as_ptr(), frame.len() as c_int) };
            Ok(())
        })? {}
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
            let sent = self.port.transmit(|frame, _| {
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
        // Slirp frames lack the FCS; an out-of-profile one is dropped there.
        let c = unsafe { &mut *self.context };
        !c.rx.is_empty() && self.port.receive_from(&mut c.rx)
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
    use std::net::{TcpListener, UdpSocket};

    fn device() -> (Arc<super::super::Virtio>, PacketPort) {
        let rng = super::super::tests::device();
        super::super::Virtio::network(
            rng.irqs.clone(),
            super::super::NET_RESOURCE.gsi as usize,
            Slirp::MAC,
            Slirp::OFFLOADS,
        )
        .unwrap()
    }

    enum Listener {
        Tcp(TcpListener),
        Udp(UdpSocket),
    }
    impl Listener {
        fn bind(protocol: ForwardProtocol, port: u16) -> std::io::Result<Self> {
            match protocol {
                ForwardProtocol::Tcp => TcpListener::bind(("127.0.0.1", port)).map(Self::Tcp),
                ForwardProtocol::Udp => UdpSocket::bind(("127.0.0.1", port)).map(Self::Udp),
            }
        }

        fn port(&self) -> NonZeroU16 {
            match self {
                Self::Tcp(s) => s.local_addr(),
                Self::Udp(s) => s.local_addr(),
            }
            .unwrap()
            .port()
            .try_into()
            .unwrap()
        }
    }

    fn rule(protocol: ForwardProtocol, host_port: NonZeroU16) -> PortForward {
        PortForward {
            protocol,
            host_port,
            guest_port: 5201.try_into().unwrap(),
        }
    }

    #[test]
    fn udp_forwarding_requires_explicit_host_loopback() {
        let mut options = SlirpOptions::default();
        assert!(!options.host_loopback);
        options.validate().unwrap();
        options.forwards = vec![rule(ForwardProtocol::Tcp, 15201.try_into().unwrap())];
        options.validate().unwrap();
        options
            .forwards
            .push(rule(ForwardProtocol::Udp, 15201.try_into().unwrap()));
        let error = options
            .validate()
            .expect_err("UDP needs explicit loopback access");
        assert!(error.contains("requires --net-host-loopback"), "{error}");
        assert!(
            !options.host_loopback,
            "validation must not enable loopback"
        );
        options.host_loopback = true;
        options.validate().unwrap();
    }

    #[test]
    fn udp_forwarding_refuses_reserved_host_dns_port_but_accepts_guest_dns() {
        let mut options = SlirpOptions {
            host_loopback: true,
            forwards: vec![rule(ForwardProtocol::Udp, 53.try_into().unwrap())],
        };
        let error = options
            .validate()
            .expect_err("libslirp reserves host UDP port 53");
        assert!(
            error.contains("UDP forwarding from host port 53 is unsupported"),
            "{error}"
        );
        options.forwards[0].protocol = ForwardProtocol::Tcp;
        options.host_loopback = false;
        options.validate().unwrap();
        options.forwards[0] = PortForward {
            protocol: ForwardProtocol::Udp,
            host_port: 19053.try_into().unwrap(),
            guest_port: 53.try_into().unwrap(),
        };
        options.host_loopback = true;
        options.validate().unwrap();
        for host in [67, 69] {
            options.forwards[0].host_port = host.try_into().unwrap();
            options.validate().unwrap();
        }
    }

    #[test]
    fn only_udp_requires_unique_guest_ports() {
        let mut options = SlirpOptions {
            host_loopback: true,
            forwards: vec![
                rule(ForwardProtocol::Udp, 15201.try_into().unwrap()),
                rule(ForwardProtocol::Udp, 15202.try_into().unwrap()),
            ],
        };
        let error = options
            .validate()
            .expect_err("UDP reply sockets match only the guest endpoint");
        assert!(error.contains("duplicate UDP guest port 5201"), "{error}");
        options.forwards[1].guest_port = 5202.try_into().unwrap();
        options.validate().unwrap();
        options.forwards[1].guest_port = 5201.try_into().unwrap();
        options.forwards[1].protocol = ForwardProtocol::Tcp;
        options.validate().unwrap();
        options.forwards[0].protocol = ForwardProtocol::Tcp;
        options.host_loopback = false;
        options.validate().unwrap();
    }

    #[test]
    fn udp_policy_validation_precedes_any_forward_installation() {
        let occupied = Listener::bind(ForwardProtocol::Tcp, 0).unwrap();
        let tcp = rule(ForwardProtocol::Tcp, occupied.port());
        let udp = rule(ForwardProtocol::Udp, 15201.try_into().unwrap());
        for (options, expected) in [
            (
                SlirpOptions {
                    forwards: vec![tcp, udp],
                    host_loopback: false,
                },
                "requires --net-host-loopback",
            ),
            (
                SlirpOptions {
                    forwards: vec![
                        tcp,
                        PortForward {
                            host_port: 53.try_into().unwrap(),
                            ..udp
                        },
                    ],
                    host_loopback: true,
                },
                "UDP forwarding from host port 53 is unsupported",
            ),
            (
                SlirpOptions {
                    forwards: vec![
                        tcp,
                        udp,
                        PortForward {
                            host_port: 15202.try_into().unwrap(),
                            ..udp
                        },
                    ],
                    host_loopback: true,
                },
                "duplicate UDP guest port 5201",
            ),
        ] {
            let (_device, port) = device();
            let error = Slirp::new(port, &options)
                .err()
                .expect("invalid UDP configuration");
            assert!(
                error.contains(expected),
                "policy validation must precede the TCP bind failure: {error}"
            );
        }
    }

    #[test]
    fn forwards_bind_until_the_stack_drops() {
        for protocol in [ForwardProtocol::Tcp, ForwardProtocol::Udp] {
            let reservation = Listener::bind(protocol, 0).unwrap();
            let host = reservation.port();
            drop(reservation);
            let options = SlirpOptions {
                forwards: vec![rule(protocol, host)],
                host_loopback: protocol == ForwardProtocol::Udp,
            };
            let (_device, port) = device();
            let stack = Slirp::new(port, &options).unwrap();
            assert!(
                Listener::bind(protocol, host.get()).is_err(),
                "{protocol} listener missing"
            );
            drop(stack);
            Listener::bind(protocol, host.get()).expect("drop releases the listener");
        }
    }

    #[test]
    fn a_failed_rule_releases_every_previously_installed_listener() {
        for protocol in [ForwardProtocol::Tcp, ForwardProtocol::Udp] {
            let occupied = Listener::bind(protocol, 0).unwrap();
            let available = Listener::bind(protocol, 0).unwrap();
            let first = rule(protocol, available.port());
            let second = PortForward {
                guest_port: 5202.try_into().unwrap(),
                ..rule(protocol, occupied.port())
            };
            drop(available);
            let options = SlirpOptions {
                forwards: vec![first, second],
                host_loopback: protocol == ForwardProtocol::Udp,
            };
            let (_device, port) = device();
            let error = Slirp::new(port, &options)
                .err()
                .expect("occupied port must refuse launch");
            assert!(error.contains(&second.to_string()), "{error}");
            Listener::bind(protocol, first.host_port.get())
                .expect("rollback releases the first rule");
            assert!(
                Listener::bind(protocol, second.host_port.get()).is_err(),
                "collision fixture stays bound"
            );
        }
    }

    #[test]
    fn tcp_and_udp_can_forward_the_same_numerical_port() {
        let tcp = Listener::bind(ForwardProtocol::Tcp, 0).unwrap();
        let host = tcp.port();
        let udp = Listener::bind(ForwardProtocol::Udp, host.get()).unwrap();
        drop((tcp, udp));
        let options = SlirpOptions {
            forwards: vec![
                rule(ForwardProtocol::Tcp, host),
                rule(ForwardProtocol::Udp, host),
            ],
            host_loopback: true,
        };
        let (_device, port) = device();
        let stack = Slirp::new(port, &options).unwrap();
        for protocol in [ForwardProtocol::Tcp, ForwardProtocol::Udp] {
            assert!(Listener::bind(protocol, host.get()).is_err());
        }
        drop(stack);
        let _tcp = Listener::bind(ForwardProtocol::Tcp, host.get()).unwrap();
        let _udp = Listener::bind(ForwardProtocol::Udp, host.get()).unwrap();
    }

    #[test]
    fn duplicate_validation_precedes_any_forward_installation() {
        for protocol in [ForwardProtocol::Tcp, ForwardProtocol::Udp] {
            let occupied = Listener::bind(protocol, 0).unwrap();
            let rule = rule(protocol, occupied.port());
            let options = SlirpOptions {
                forwards: vec![rule, rule],
                host_loopback: protocol == ForwardProtocol::Udp,
            };
            let (_device, port) = device();
            let error = Slirp::new(port, &options).err().expect("duplicate refused");
            assert!(
                error.contains(&format!(
                    "duplicate {protocol} listener 127.0.0.1:{}",
                    rule.host_port
                )),
                "{error}"
            );
        }
    }

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
