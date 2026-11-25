//! The control API (`docs/restructure/control-api-plan.md`, client reference `docs/control-api.md`): a program on the
//! Mac drives the VM through JSON-RPC 2.0 requests on a Unix socket. [`protocol`] is the wire format; [`ps2`] turns
//! input events into the 8042's keyboard and mouse bytes, with flow control so no input is lost; this module is the
//! server.
//!
//! **Server.** [`serve`] makes the socket (mode 0600: bound inside a fresh 0700 folder beside the path, then renamed
//! into place; the console's rules for a stale or live socket, `crate::virtio::claim_socket_path`) and starts one
//! listener thread (`control-listen`), which accepts at most [`MAX_CLIENTS`] connections (a further one is closed at
//! once). Each connection has a reader thread (`control-client`: reads lines, decodes, dispatches, waits while its input
//! is delivered) and a writer thread (`control-write`), which writes the responses queued for it with a send timeout
//! ([`SEND_TIMEOUT`]); a client whose queue grows beyond [`OUTPUT_CAP`] or whose write times out is disconnected, so a
//! client that stops reading never blocks the server. A disconnect releases what the client held ([`ps2::Input::release`]).
//! While the listener exists the server holds an [`Irqs::busy`] guard: a guest idle in HLT waiting for input is not ended
//! as idle. [`Server::stopped`] queues the `stopped` notification for every client, waits for the writers at most
//! [`SEND_TIMEOUT`], shuts every connection down and joins every thread; dropping the server does the same without the
//! notification and removes the socket.
//!
//! **Display.** With a display ([`Target::display`]), `snapshot` answers the picture as a base64 PNG and `framebuffer`
//! the mode with the framebuffer's read-only descriptor, sent as `SCM_RIGHTS` on the answer's first `sendmsg`. One
//! thread (`control-display`) follows the display's change counter and sends every client `display_changed`, at most
//! once per [`DISPLAY_CHANGED_EVERY`]: a change within that time after one sent is sent at its end, with the state
//! then (so the last state is always sent, and a guest flipping its mode quickly costs a client one line per window).
//! A snapshot's answer can exceed [`OUTPUT_CAP`]: a line is queued over the cap when nothing else waits for the client
//! (a client that does not read is still dropped by the next line, or by the send timeout).
pub mod protocol;
pub mod ps2;

use crate::irq::{Busy, Irqs};
use crate::linux::Canceller;
use crate::display::{Display, Mode, SnapshotError};
use crate::ps2::Ps2Input;
use protocol::{Call, Error, ErrorCode, Incoming, InputEvent, Request, Response, StopReason};
use serde_json::{json, Value};
use std::collections::VecDeque;
use std::io::{ErrorKind, Read, Write};
use std::net::Shutdown;
use std::os::fd::{AsRawFd, OwnedFd};
use std::os::unix::fs::MetadataExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering::SeqCst};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, Weak};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

/// The most connections served at once; a further one is closed at once.
pub const MAX_CLIENTS: usize = 8;
/// The longest line a client may send (without its newline): beyond it, -32700 and the connection is closed.
pub const MAX_LINE: usize = 1 << 20;
/// The most response bytes queued for a client that it has not taken: beyond it, the client is disconnected.
pub const OUTPUT_CAP: usize = 1 << 20;
/// How long one write to a client may block before the client is disconnected; also how long [`Server::stopped`]
/// waits for the notification to be written.
pub const SEND_TIMEOUT: Duration = Duration::from_secs(1);
/// The largest |dx| and |dy| of a `pointer_motion` and |dy| of a `scroll`: beyond it, -32602.
pub const MAX_MOTION: u32 = 65535;
/// The least time between two `display_changed` notifications.
pub const DISPLAY_CHANGED_EVERY: Duration = Duration::from_millis(50);

/// What `query_status` reports besides the devices and the state.
pub struct StatusSource {
    /// The VM's processors.
    pub vcpus: usize,
    /// When the run started (`uptime_ms` counts from it).
    pub started: Instant,
}

/// The handles the server drives.
pub struct Target {
    /// The 8042's host input (`None` without `machine.ps2`: input requests are -32001).
    pub ps2: Option<Ps2Input>,
    /// The PM block's fixed power button (`None`: `power_button` is -32001).
    pub power_button: Option<crate::pm::PowerButton>,
    /// `quit` cancels the run with it.
    pub cancel: Canceller,
    pub status: StatusSource,
    /// The busy guard's source.
    pub irqs: Irqs,
    /// The display (`None`: `snapshot` and `framebuffer` are -32001).
    pub display: Option<Arc<Display>>,
}

/// What the client threads share.
struct Shared {
    input: Option<ps2::Input>,
    power_button: Option<crate::pm::PowerButton>,
    cancel: Canceller,
    status: StatusSource,
    display: Option<Arc<Display>>,
    /// `quit` was asked or the run ended.
    stopping: AtomicBool,
}

/// What a call answers: its result, and the descriptors that go with the answer's line.
struct Answer {
    outcome: Result<Value, Error>,
    fds: Vec<OwnedFd>,
}

/// A listening control socket ([`serve`]).
pub struct Server {
    path: PathBuf,
    /// The socket file's device and inode, so the drop removes only this server's socket.
    file: (u64, u64),
    shared: Arc<Shared>,
    /// Wakes the listener: a byte, or the write end closing.
    bell: UnixStream,
    clients: Arc<Mutex<Vec<Slot>>>,
    /// Taken by the first [`Server::close`].
    running: Mutex<Option<Running>>,
}

struct Running {
    listener: JoinHandle<()>,
    /// The `display_changed` thread, with a display.
    watcher: Option<JoinHandle<()>>,
    _busy: Busy,
}

/// A served connection: its reader thread (which joins the writer) and, while either thread runs, the client. Only the
/// threads own the client, so its socket closes when they end: a shutdown alone does not wake a peer blocked writing to
/// it (macOS), the close does.
struct Slot {
    client: Weak<Client>,
    thread: JoinHandle<()>,
}

/// Starts the control server on a Unix socket at `path`: the console's socket rules (a path of at most 103 bytes, a
/// stale socket replaced, a live one or a non-socket refused) and mode 0600.
pub fn serve(path: &Path, target: Target) -> crate::Result<Server> {
    let fail = |e: String| format!("control socket {}: {e}", path.display());
    crate::virtio::claim_socket_path(path).map_err(fail)?;
    let listener = bind_private(path).map_err(fail)?;
    // From here on the socket is in place: a failure removes it.
    let ps2 = target.ps2;
    let placed = (|| {
        let meta = std::fs::symlink_metadata(path).map_err(|e| e.to_string())?;
        let (bell, ring) = UnixStream::pair().map_err(|e| e.to_string())?;
        no_sigpipe(&bell).map_err(|e| e.to_string())?;
        let input = match ps2 {
            Some(p) => Some(ps2::Input::start(p, ps2::STALL).map_err(|e| format!("an input thread: {e}"))?),
            None => None,
        };
        Ok::<_, String>((meta, bell, ring, input))
    })();
    let (meta, bell, ring, input) = match placed {
        Ok(p) => p,
        Err(e) => {
            let _ = std::fs::remove_file(path);
            return Err(fail(e));
        }
    };
    let shared = Arc::new(Shared {
        input,
        power_button: target.power_button,
        cancel: target.cancel,
        status: target.status,
        display: target.display,
        stopping: AtomicBool::new(false),
    });
    let clients = Arc::new(Mutex::new(Vec::new()));
    let (s, c) = (shared.clone(), clients.clone());
    let started = std::thread::Builder::new()
        .name("control-listen".into())
        .spawn(move || listen(listener, ring, s, c));
    let listener = match started {
        Ok(t) => t,
        Err(e) => {
            let _ = std::fs::remove_file(path);
            return Err(fail(e.to_string()));
        }
    };
    let watcher = match shared.display.clone() {
        None => None,
        Some(display) => {
            let (s, c) = (shared.clone(), clients.clone());
            let started = std::thread::Builder::new()
                .name("control-display".into())
                .spawn(move || watch_display(&display, &s, &c));
            match started {
                Ok(t) => Some(t),
                Err(e) => {
                    shared.stopping.store(true, SeqCst);
                    let _ = (&bell).write(&[1]);
                    let _ = listener.join();
                    let _ = std::fs::remove_file(path);
                    return Err(fail(e.to_string()));
                }
            }
        }
    };
    Ok(Server {
        path: path.to_owned(),
        file: (meta.dev(), meta.ino()),
        shared,
        bell,
        clients,
        running: Mutex::new(Some(Running { listener, watcher, _busy: target.irqs.busy() })),
    })
}

/// A write to a peer that left fails with EPIPE instead of raising SIGPIPE.
fn no_sigpipe(stream: &UnixStream) -> std::io::Result<()> {
    let one: libc::c_int = 1;
    let ok = unsafe {
        libc::setsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_NOSIGPIPE,
            &one as *const _ as *const libc::c_void,
            std::mem::size_of_val(&one) as libc::socklen_t,
        )
    };
    if ok == 0 { Ok(()) } else { Err(std::io::Error::last_os_error()) }
}

/// Moves `from` to `to` unless something is at `to` (`renamex_np` with `RENAME_EXCL`): EEXIST then.
fn rename_exclusive(from: &Path, to: &Path) -> std::io::Result<()> {
    use std::os::unix::ffi::OsStrExt;
    let c = |p: &Path| std::ffi::CString::new(p.as_os_str().as_bytes()).map_err(|_| ErrorKind::InvalidInput);
    let (from, to) = (c(from)?, c(to)?);
    if unsafe { libc::renamex_np(from.as_ptr(), to.as_ptr(), libc::RENAME_EXCL) } == 0 {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error())
    }
}

/// A listening socket at `path`, mode 0600 before anyone can reach it: bound inside a fresh 0700 folder beside `path`,
/// made 0600 there, then moved into place without replacing anything (no process-wide umask, so it is safe with
/// threads running; something that appeared at `path` since the checks is refused as a live socket).
fn bind_private(path: &Path) -> std::result::Result<UnixListener, String> {
    use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
    let parent = path.parent().filter(|p| !p.as_os_str().is_empty()).unwrap_or(Path::new("."));
    let mut n = 0u32;
    let dir = loop {
        let dir = parent.join(format!(".c{:x}{n}", std::process::id()));
        match std::fs::DirBuilder::new().mode(0o700).create(&dir) {
            Ok(()) => break dir,
            Err(e) if e.kind() == ErrorKind::AlreadyExists && n < 100 => n += 1,
            Err(e) => return Err(format!("a private folder beside it: {e}")),
        }
    };
    let mut renamed = false;
    let made = (|| {
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).map_err(|e| e.to_string())?;
        let temp = dir.join("s");
        if temp.as_os_str().len() >= crate::virtio::SUN_PATH {
            return Err(format!(
                "its temporary path {} is over {} bytes; use a shorter path",
                temp.display(),
                crate::virtio::SUN_PATH - 1
            ));
        }
        let listener = UnixListener::bind(&temp).map_err(|e| e.to_string())?;
        std::fs::set_permissions(&temp, std::fs::Permissions::from_mode(0o600)).map_err(|e| e.to_string())?;
        rename_exclusive(&temp, path).map_err(|e| match e.kind() {
            ErrorKind::AlreadyExists => "another program listens on this socket".to_string(),
            _ => e.to_string(),
        })?;
        renamed = true;
        listener.set_nonblocking(true).map_err(|e| e.to_string())?;
        Ok(listener)
    })();
    let _ = std::fs::remove_file(dir.join("s"));
    let _ = std::fs::remove_dir(&dir);
    if made.is_err() && renamed {
        // Renamed before a later step failed: not left behind.
        let _ = std::fs::remove_file(path);
    }
    made
}

/// The listener thread: accepts connections until the bell rings.
fn listen(listener: UnixListener, bell: UnixStream, shared: Arc<Shared>, clients: Arc<Mutex<Vec<Slot>>>) {
    let mut next: ps2::ClientId = 0;
    loop {
        let mut fds = [
            libc::pollfd { fd: listener.as_raw_fd(), events: libc::POLLIN, revents: 0 },
            libc::pollfd { fd: bell.as_raw_fd(), events: libc::POLLIN, revents: 0 },
        ];
        let n = unsafe { libc::poll(fds.as_mut_ptr(), 2, -1) };
        if n < 0 {
            if std::io::Error::last_os_error().kind() == ErrorKind::Interrupted {
                continue;
            }
            return;
        }
        if fds[1].revents != 0 {
            return;
        }
        loop {
            let stream = match listener.accept() {
                Ok((stream, _)) => stream,
                Err(e) if e.kind() == ErrorKind::WouldBlock => break,
                Err(e) if e.kind() == ErrorKind::Interrupted => continue,
                // A connection that went away before it was taken, or out of descriptors: keep listening, without
                // spinning on a condition that persists.
                Err(_) => {
                    std::thread::sleep(Duration::from_millis(10));
                    break;
                }
            };
            let mut slots = lock(&clients);
            slots.retain(|s| !s.thread.is_finished());
            if slots.len() >= MAX_CLIENTS {
                drop(stream); // closed at once
                continue;
            }
            next += 1;
            match Client::start(next, stream, shared.clone()) {
                Some(slot) => slots.push(slot),
                None => continue,
            }
        }
    }
}

/// The `control-display` thread: each change of the display's published mode is sent to every client as
/// `display_changed`, at most once per [`DISPLAY_CHANGED_EVERY`] (module documentation), until the server stops.
fn watch_display(display: &Display, shared: &Shared, clients: &Mutex<Vec<Slot>>) {
    let mut seen = display.changes();
    let poll = Duration::from_millis(100);
    while !shared.stopping.load(SeqCst) {
        let now = display.wait_change(seen, poll);
        if now == seen {
            continue;
        }
        seen = now;
        let line = protocol::notification("display_changed", display_changed(display.mode()));
        let live: Vec<Arc<Client>> = lock(clients).iter().filter_map(|s| s.client.upgrade()).collect();
        for c in &live {
            c.push(line.clone(), Vec::new());
        }
        drop(live);
        // The window: a change meanwhile is sent after it, with the state then.
        let end = Instant::now() + DISPLAY_CHANGED_EVERY;
        while !shared.stopping.load(SeqCst) && Instant::now() < end {
            std::thread::sleep((end - Instant::now()).min(Duration::from_millis(10)));
        }
    }
}

/// `display_changed`'s parameters for the published mode `mode`.
fn display_changed(mode: Option<Mode>) -> Value {
    match mode {
        Some(m) => json!({
            "enabled": true, "width": m.width, "height": m.height, "stride": m.stride, "offset": m.offset,
            "format": format(&m),
        }),
        None => json!({"enabled": false, "width": 0, "height": 0, "stride": 0, "offset": 0}),
    }
}

/// A mode's pixel format, named as pixman and QEMU name it (the 32-bit value's channels from the top, little-endian):
/// `x8r8g8b8` is B, G, R, X in memory, `b8g8r8x8` (the big-endian byte order) X, R, G, B.
fn format(m: &Mode) -> &'static str {
    if m.big_endian { "b8g8r8x8" } else { "x8r8g8b8" }
}

/// A mode as `query_status` and `framebuffer` report it.
fn mode_json(m: &Mode) -> Value {
    json!({"width": m.width, "height": m.height, "stride": m.stride, "offset": m.offset, "format": format(m)})
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// What a connection's writer holds.
#[derive(Default)]
struct Out {
    /// Lines (with their newline) not yet written, each with the descriptors sent with its first bytes.
    queue: VecDeque<(Vec<u8>, Vec<OwnedFd>)>,
    /// Their bytes, and the line being written.
    bytes: usize,
    /// Close once the queue is written (an over-long line's -32700).
    closing: bool,
    /// Disconnected: nothing more is written.
    dead: bool,
}

/// One connection's socket and writer state.
struct Client {
    id: ps2::ClientId,
    stream: UnixStream,
    out: Mutex<Out>,
    /// Notified when a line is queued, the queue empties or the client dies.
    changed: Condvar,
}

impl Client {
    /// Sets the socket up and starts the reader thread (which starts the writer); none if either fails.
    fn start(id: ps2::ClientId, stream: UnixStream, shared: Arc<Shared>) -> Option<Slot> {
        // An accepted socket inherits the listener's O_NONBLOCK.
        stream.set_nonblocking(false).ok()?;
        stream.set_write_timeout(Some(SEND_TIMEOUT)).ok()?;
        no_sigpipe(&stream).ok()?;
        let reader = stream.try_clone().ok()?;
        let client = Arc::new(Client { id, stream, out: Mutex::default(), changed: Condvar::new() });
        let c = client.clone();
        let thread = std::thread::Builder::new()
            .name("control-client".into())
            .spawn(move || c.serve(reader, &shared))
            .ok()?;
        Some(Slot { client: Arc::downgrade(&client), thread })
    }

    /// The reader thread: starts the writer, reads and answers lines until the connection ends, then releases what
    /// the client held and joins the writer.
    fn serve(self: Arc<Self>, mut reader: UnixStream, shared: &Shared) {
        let c = self.clone();
        let writer = std::thread::Builder::new().name("control-write".into()).spawn(move || c.write());
        if writer.is_err() {
            self.kill();
        }
        let mut buf: Vec<u8> = Vec::new();
        let mut chunk = vec![0u8; 64 << 10];
        'read: while !self.is_dead() {
            let n = match reader.read(&mut chunk) {
                Ok(0) => {
                    // A last line without its newline still counts.
                    if !buf.is_empty() && buf.len() <= MAX_LINE {
                        self.line(&buf, shared);
                    }
                    break;
                }
                Ok(n) => n,
                Err(e) if e.kind() == ErrorKind::Interrupted => continue,
                Err(_) => break,
            };
            let mut scanned = buf.len();
            buf.extend_from_slice(&chunk[..n]);
            while let Some(at) = buf[scanned..].iter().position(|&b| b == b'\n').map(|i| scanned + i) {
                if at > MAX_LINE {
                    break;
                }
                let line: Vec<u8> = buf.drain(..=at).collect();
                self.line(&line[..at], shared);
                scanned = 0;
                if self.is_dead() {
                    break 'read;
                }
            }
            if buf.len() > MAX_LINE && !buf[..=MAX_LINE].contains(&b'\n') {
                let error = Error::new(ErrorCode::ParseError)
                    .with_data(json!(format!("a line is at most {MAX_LINE} bytes")));
                self.push(Response { id: Value::Null, outcome: Err(error) }.to_line(), Vec::new());
                self.close_after_flush();
                break;
            }
        }
        // Gone, or done sending: what it held is let go; the responses already queued are still written (a client
        // that closed only its sending half gets them), then the connection closes.
        if let Some(input) = &shared.input {
            input.release(self.id);
        }
        self.close_after_flush();
        if let Ok(writer) = writer {
            let _ = writer.join();
        }
        self.kill();
    }

    /// One line: decoded, carried out, answered (none for notifications only).
    fn line(&self, line: &[u8], shared: &Shared) {
        let Ok(text) = std::str::from_utf8(line) else {
            let error = Error::new(ErrorCode::ParseError).with_data(json!("a line is UTF-8"));
            self.push(Response { id: Value::Null, outcome: Err(error) }.to_line(), Vec::new());
            return;
        };
        if text.trim().is_empty() {
            return; // a blank line is nothing
        }
        // A notification's descriptors are dropped with its answer.
        let mut fds = Vec::new();
        let mut answer = |r: Result<Request, protocol::Rejected>| match r {
            Ok(request) => {
                let a = shared.call(self.id, &request.call);
                let response = request.answer(a.outcome);
                if response.is_some() {
                    fds.extend(a.fds);
                }
                response
            }
            Err(rejected) => rejected.response(),
        };
        match protocol::decode_line(text) {
            Incoming::Single(r) => {
                if let Some(response) = answer(r) {
                    self.push(response.to_line(), fds);
                }
            }
            Incoming::Batch(requests) => {
                let lines: Vec<String> = requests.into_iter().filter_map(&mut answer).map(|r| r.to_line()).collect();
                if !lines.is_empty() {
                    self.push(format!("[{}]", lines.join(",")), fds);
                }
            }
        }
    }

    /// Queues `line` for the writer, `fds` to go with its first bytes; over the output cap the client is disconnected
    /// instead, unless nothing else waits for it (one long answer, a snapshot's, is queued alone).
    fn push(&self, line: String, fds: Vec<OwnedFd>) {
        let mut out = lock(&self.out);
        if out.dead || out.closing {
            return;
        }
        if out.bytes > 0 && out.bytes + line.len() + 1 > OUTPUT_CAP {
            drop(out);
            self.kill();
            return;
        }
        let mut bytes = line.into_bytes();
        bytes.push(b'\n');
        out.bytes += bytes.len();
        out.queue.push_back((bytes, fds));
        self.changed.notify_all();
    }

    fn close_after_flush(&self) {
        lock(&self.out).closing = true;
        self.changed.notify_all();
    }

    fn is_dead(&self) -> bool {
        lock(&self.out).dead
    }

    /// Disconnects: nothing more is read or written (a blocked read or write returns).
    fn kill(&self) {
        let mut out = lock(&self.out);
        out.dead = true;
        out.queue.clear();
        out.bytes = 0;
        drop(out);
        let _ = self.stream.shutdown(Shutdown::Both);
        self.changed.notify_all();
    }

    /// Waits until the queue is written or the client is gone, at most until `deadline`.
    fn flushed(&self, deadline: Instant) {
        let mut out = lock(&self.out);
        while !out.dead && out.bytes > 0 {
            let now = Instant::now();
            if now >= deadline {
                return;
            }
            out = match self.changed.wait_timeout(out, deadline - now) {
                Ok((o, _)) => o,
                Err(e) => e.into_inner().0,
            };
        }
    }

    /// The writer thread: writes queued lines; a write that fails or times out disconnects the client.
    fn write(&self) {
        let mut stream = &self.stream;
        loop {
            let line = {
                let mut out = lock(&self.out);
                loop {
                    if out.dead {
                        return;
                    }
                    if let Some(line) = out.queue.pop_front() {
                        break line;
                    }
                    if out.closing {
                        drop(out);
                        self.kill();
                        return;
                    }
                    out = self.changed.wait(out).unwrap_or_else(|e| e.into_inner());
                }
            };
            let (line, fds) = line;
            let sent = if fds.is_empty() { Ok(0) } else { send_fds(&self.stream, &line, &fds) };
            drop(fds); // sent (the peer holds its own) or not: this process's copies close
            if sent.and_then(|n| stream.write_all(&line[n..])).is_err() {
                self.kill();
                return;
            }
            let mut out = lock(&self.out);
            out.bytes = out.bytes.saturating_sub(line.len());
            if out.bytes == 0 {
                self.changed.notify_all();
            }
        }
    }
}

/// Sends the first bytes of `line` with `fds` as one `SCM_RIGHTS` message (one `sendmsg`, under the socket's send
/// timeout); how many bytes went, at least one.
fn send_fds(stream: &UnixStream, line: &[u8], fds: &[OwnedFd]) -> std::io::Result<usize> {
    let raw: Vec<libc::c_int> = fds.iter().map(|f| f.as_raw_fd()).collect();
    let data = std::mem::size_of_val(&raw[..]) as libc::c_uint;
    let space = unsafe { libc::CMSG_SPACE(data) } as usize;
    // u64 words: the control buffer aligned for cmsghdr.
    let mut control = vec![0u64; space.div_ceil(8)];
    let mut iov = libc::iovec { iov_base: line.as_ptr() as *mut libc::c_void, iov_len: line.len() };
    let mut msg: libc::msghdr = unsafe { std::mem::zeroed() };
    msg.msg_iov = &mut iov;
    msg.msg_iovlen = 1;
    msg.msg_control = control.as_mut_ptr().cast();
    msg.msg_controllen = space as libc::socklen_t;
    // SAFETY: the control buffer holds one header and `raw`'s descriptors (CMSG_SPACE of their size); `iov` and
    // `control` outlive the call.
    unsafe {
        let cmsg = libc::CMSG_FIRSTHDR(&msg);
        (*cmsg).cmsg_level = libc::SOL_SOCKET;
        (*cmsg).cmsg_type = libc::SCM_RIGHTS;
        (*cmsg).cmsg_len = libc::CMSG_LEN(data) as libc::socklen_t;
        std::ptr::copy_nonoverlapping(raw.as_ptr(), libc::CMSG_DATA(cmsg).cast::<libc::c_int>(), raw.len());
    }
    loop {
        let n = unsafe { libc::sendmsg(stream.as_raw_fd(), &msg, 0) };
        if n > 0 {
            return Ok(n as usize);
        }
        let e = std::io::Error::last_os_error();
        if n < 0 && e.kind() == ErrorKind::Interrupted {
            continue;
        }
        return Err(if n == 0 { ErrorKind::WriteZero.into() } else { e });
    }
}

impl Shared {
    /// Carries out one call for `client`, with the descriptors its answer carries.
    fn call(&self, client: ps2::ClientId, call: &Call) -> Answer {
        match call {
            Call::Framebuffer => match self.framebuffer() {
                Ok((value, fd)) => Answer { outcome: Ok(value), fds: vec![fd] },
                Err(e) => Answer { outcome: Err(e), fds: Vec::new() },
            },
            call => Answer { outcome: self.call_plain(client, call), fds: Vec::new() },
        }
    }

    /// `framebuffer`: the mode and a descriptor of the framebuffer (a duplicate of its read-only one).
    fn framebuffer(&self) -> Result<(Value, OwnedFd), Error> {
        let display = self.display.as_ref().ok_or_else(|| Error::no_device("display"))?;
        let (mode, fd) = display.framebuffer().ok_or_else(|| Error::new(ErrorCode::DisplayNotEnabled))?;
        let fd = fd.try_clone_to_owned().map_err(|e| {
            Error::new(ErrorCode::NoDevice).with_data(json!({"device": "display", "error": e.to_string()}))
        })?;
        let mut value = mode_json(&mode);
        value["size"] = json!(display.vram().len());
        Ok((value, fd))
    }

    /// `snapshot`: the picture as a base64 PNG.
    fn snapshot(&self) -> Result<Value, Error> {
        use base64::Engine;
        let display = self.display.as_ref().ok_or_else(|| Error::no_device("display"))?;
        let frame = display.snapshot().map_err(|e| match e {
            SnapshotError::NotEnabled => Error::new(ErrorCode::DisplayNotEnabled),
            SnapshotError::TooLarge { width, height } => Error::new(ErrorCode::SnapshotTooLarge).with_data(json!({
                "width": width, "height": height,
                "max_width": crate::display::SNAPSHOT_MAX.0, "max_height": crate::display::SNAPSHOT_MAX.1,
            })),
        })?;
        let png = base64::engine::general_purpose::STANDARD.encode(frame.png());
        Ok(json!({"width": frame.width, "height": frame.height, "png": png}))
    }

    /// The calls whose answers carry no descriptor.
    fn call_plain(&self, client: ps2::ClientId, call: &Call) -> Result<Value, Error> {
        match call {
            Call::Framebuffer => unreachable!("answered by call"),
            Call::Snapshot(_) => self.snapshot(),
            Call::SendInput(p) => {
                check_motion(&p.events)?;
                let input = self.input.as_ref().ok_or_else(|| Error::no_device("ps2"))?;
                if self.stopping.load(SeqCst) {
                    return Err(Error::new(ErrorCode::Stopping));
                }
                input.send(client, &p.events)?;
                Ok(json!({}))
            }
            Call::PowerButton => {
                let button = self.power_button.as_ref().ok_or_else(|| Error::no_device("power_button"))?;
                if self.stopping.load(SeqCst) {
                    return Err(Error::new(ErrorCode::Stopping));
                }
                button.press();
                Ok(json!({}))
            }
            Call::Quit => {
                self.stopping.store(true, SeqCst);
                self.cancel.cancel();
                Ok(json!({}))
            }
            Call::QueryStatus => {
                let stopping = self.stopping.load(SeqCst) || self.cancel.cancelled();
                let mut status = json!({
                    "protocol": protocol::PROTOCOL,
                    "state": if stopping { "stopping" } else { "running" },
                    "vcpus": self.status.vcpus,
                    "uptime_ms": self.status.started.elapsed().as_millis() as u64,
                    "devices": { "ps2": self.input.is_some() },
                });
                // With a display: its mode, null while none is published. Without one the member is absent, so a VM
                // without a display answers as before the display existed.
                if let Some(d) = &self.display {
                    status["devices"]["display"] = d.mode().map_or(Value::Null, |m| mode_json(&m));
                }
                Ok(status)
            }
        }
    }
}

/// The motion cap, over every event before any is applied: -32602 naming the first field beyond it.
fn check_motion(events: &[InputEvent]) -> Result<(), Error> {
    for (i, event) in events.iter().enumerate() {
        let fields: &[(&str, i32)] = match event {
            InputEvent::PointerMotion { dx, dy } => &[("dx", *dx), ("dy", *dy)],
            InputEvent::Scroll { dy } => &[("dy", *dy)],
            _ => &[],
        };
        if let Some((name, _)) = fields.iter().find(|(_, v)| v.unsigned_abs() > MAX_MOTION) {
            return Err(Error::new(ErrorCode::InvalidParams)
                .with_data(json!(format!("events[{i}].{name}: at most {MAX_MOTION} in magnitude"))));
        }
    }
    Ok(())
}

impl Server {
    /// The run ended (`reason`): every client is sent the `stopped` notification (waiting at most [`SEND_TIMEOUT`] for
    /// it to be written), then every connection is closed and every thread joined. Later calls do nothing.
    pub fn stopped(&self, reason: StopReason) {
        self.close(Some(reason));
    }

    fn close(&self, reason: Option<StopReason>) {
        let Some(Running { listener, watcher, _busy: busy }) = lock(&self.running).take() else { return };
        self.shared.stopping.store(true, SeqCst);
        // No new connection; the listener's socket closes with its thread.
        let _ = (&self.bell).write(&[1]);
        let _ = listener.join();
        // No more display_changed (the thread sees `stopping` within its poll).
        if let Some(w) = watcher {
            let _ = w.join();
        }
        let slots = std::mem::take(&mut *lock(&self.clients));
        // Pending input fails with -32002, so every reader can answer and move on.
        if let Some(input) = &self.shared.input {
            input.stop();
        }
        let live: Vec<Arc<Client>> = slots.iter().filter_map(|s| s.client.upgrade()).collect();
        if let Some(reason) = reason {
            let line = protocol::stopped(reason);
            for c in &live {
                c.push(line.clone(), Vec::new());
            }
            let deadline = Instant::now() + SEND_TIMEOUT;
            for c in &live {
                c.flushed(deadline);
            }
        }
        for c in &live {
            c.kill();
        }
        drop(live);
        for s in slots {
            let _ = s.thread.join();
        }
        drop(busy);
    }
}

#[cfg(test)]
impl Server {
    /// Each live client's queued response bytes (tests: a writer blocked on a full socket).
    fn queued(&self) -> Vec<usize> {
        lock(&self.clients).iter().filter_map(|s| s.client.upgrade()).map(|c| lock(&c.out).bytes).collect()
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        self.close(None);
        // Only this server's socket: another may have replaced it since.
        if std::fs::symlink_metadata(&self.path).is_ok_and(|m| (m.dev(), m.ino()) == self.file) {
            let _ = std::fs::remove_file(&self.path);
        }
    }
}

#[cfg(test)]
mod tests;
