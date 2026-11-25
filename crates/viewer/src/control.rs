//! A connection to natemu's control socket (docs/control-api.md): JSON-RPC 2.0, one JSON value per line, with the
//! `framebuffer` answer's descriptor passed as `SCM_RIGHTS` on the message carrying the line's first bytes. Lines are
//! read with `recvmsg` so that descriptor is never lost; descriptors are queued in the order they arrive, which is the
//! order of the answers that carry them.
use serde_json::{Value, json};
use std::collections::VecDeque;
use std::io::{self, Write};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::net::UnixStream;
use std::path::Path;

/// The most descriptors one `recvmsg` takes (natemu sends one per `framebuffer` answer; a batch could send several).
const MAX_FDS: usize = 8;

pub struct Connection {
    stream: UnixStream,
    buf: Vec<u8>,
    fds: VecDeque<OwnedFd>,
    next_id: u64,
}

impl Connection {
    pub fn open(path: &Path) -> io::Result<Connection> {
        let stream = UnixStream::connect(path)?;
        Ok(Connection { stream, buf: Vec::new(), fds: VecDeque::new(), next_id: 1 })
    }

    /// A second handle on the same connection (for a reader thread), with its own line buffer.
    pub fn try_clone(&self) -> io::Result<Connection> {
        Ok(Connection { stream: self.stream.try_clone()?, buf: Vec::new(), fds: VecDeque::new(), next_id: 1 })
    }

    /// Sends a request and returns its id.
    pub fn request(&mut self, method: &str, params: Option<Value>) -> io::Result<u64> {
        let id = self.next_id;
        self.next_id += 1;
        let mut msg = json!({"jsonrpc": "2.0", "id": id, "method": method});
        if let Some(p) = params {
            msg["params"] = p;
        }
        let mut line = serde_json::to_vec(&msg).expect("a JSON value serializes");
        line.push(b'\n');
        self.stream.write_all(&line)?;
        Ok(id)
    }

    /// The next message natemu sent, parsed; `None` once natemu closed the connection.
    pub fn next(&mut self) -> io::Result<Option<Value>> {
        loop {
            if let Some(end) = self.buf.iter().position(|&b| b == b'\n') {
                let line: Vec<u8> = self.buf.drain(..=end).collect();
                let line = &line[..line.len() - 1];
                if line.iter().all(u8::is_ascii_whitespace) {
                    continue;
                }
                return serde_json::from_slice(line)
                    .map(Some)
                    .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, format!("natemu sent a line that is not JSON: {e}")));
            }
            if self.receive()? == 0 {
                return Ok(None);
            }
        }
    }

    /// The oldest descriptor received and not yet taken.
    pub fn take_fd(&mut self) -> Option<OwnedFd> {
        self.fds.pop_front()
    }

    /// One `recvmsg`: appends the bytes to the line buffer and queues the descriptors. Returns the byte count (0: EOF).
    fn receive(&mut self) -> io::Result<usize> {
        let mut data = [0u8; 64 * 1024];
        // u64 words keep the control buffer aligned for cmsghdr.
        let space = unsafe { libc::CMSG_SPACE((MAX_FDS * size_of::<libc::c_int>()) as u32) } as usize;
        let mut control = vec![0u64; space.div_ceil(8)];
        let mut iov = libc::iovec { iov_base: data.as_mut_ptr().cast(), iov_len: data.len() };
        let mut msg: libc::msghdr = unsafe { std::mem::zeroed() };
        msg.msg_iov = &mut iov;
        msg.msg_iovlen = 1;
        msg.msg_control = control.as_mut_ptr().cast();
        msg.msg_controllen = (control.len() * 8) as _;
        let n = loop {
            let n = unsafe { libc::recvmsg(self.stream.as_raw_fd(), &mut msg, 0) };
            if n >= 0 {
                break n as usize;
            }
            let e = io::Error::last_os_error();
            if e.kind() != io::ErrorKind::Interrupted {
                return Err(e);
            }
        };
        // Take every descriptor first, even if the message was truncated, so none leaks.
        unsafe {
            let mut c = libc::CMSG_FIRSTHDR(&msg);
            while !c.is_null() {
                if (*c).cmsg_level == libc::SOL_SOCKET && (*c).cmsg_type == libc::SCM_RIGHTS {
                    let len = (*c).cmsg_len as usize - libc::CMSG_LEN(0) as usize;
                    let p = libc::CMSG_DATA(c).cast::<libc::c_int>();
                    for i in 0..len / size_of::<libc::c_int>() {
                        let fd = p.add(i).read_unaligned();
                        libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC);
                        self.fds.push_back(OwnedFd::from_raw_fd(fd));
                    }
                }
                c = libc::CMSG_NXTHDR(&msg, c);
            }
        }
        if msg.msg_flags & libc::MSG_CTRUNC != 0 {
            return Err(io::Error::other("natemu sent more descriptors than one message takes"));
        }
        self.buf.extend_from_slice(&data[..n]);
        Ok(n)
    }
}

/// An error answer's code and message, for a log line.
pub fn describe_error(err: &Value) -> String {
    let code = err.get("code").and_then(Value::as_i64).unwrap_or(0);
    let message = err.get("message").and_then(Value::as_str).unwrap_or("?");
    match err.get("data") {
        Some(d) => format!("{code} {message} ({d})"),
        None => format!("{code} {message}"),
    }
}
