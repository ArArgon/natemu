//! The server's tests: a real socket in a private folder, a test 8042 without a VM (`crate::ps2::testing`) and a
//! canceller no run watches. Every wait is bounded (generous bounds, for a loaded machine); none relies on a sleep for
//! correctness.
use super::protocol::StopReason;
use super::*;
use std::sync::Arc;
use crate::ps2::testing::{board, Board};
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, ErrorKind, Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering::SeqCst};
use std::time::{Duration, Instant};

/// How long a test waits for anything the server should do promptly.
const PATIENCE: Duration = Duration::from_secs(10);

/// A short private folder for sockets (`sun_path` holds 104 bytes on macOS), removed with everything in it.
struct Dir(PathBuf);
impl Dir {
    fn new() -> Self {
        use std::os::unix::fs::DirBuilderExt;
        static N: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
        let base = std::env::temp_dir();
        let base = if base.as_os_str().len() > 64 { "/tmp".into() } else { base };
        let dir = base.join(format!("nc-{}-{}", std::process::id(), N.fetch_add(1, SeqCst)));
        std::fs::DirBuilder::new().mode(0o700).create(&dir).unwrap();
        Self(dir)
    }
    fn path(&self, name: &str) -> PathBuf {
        self.0.join(name)
    }
    /// The names in the folder.
    fn names(&self) -> Vec<String> {
        let mut names: Vec<String> =
            std::fs::read_dir(&self.0).unwrap().map(|e| e.unwrap().file_name().to_string_lossy().into_owned()).collect();
        names.sort();
        names
    }
}
impl Drop for Dir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// A server on a test 8042 (with the device, or without it) and the handles a test looks through.
struct Rig {
    dir: Dir,
    path: PathBuf,
    board: Board,
    cancel: Canceller,
    /// The PM device the power button presses (`None`: the board has none).
    pm: Option<crate::clock::Timer<crate::pm::Pm>>,
    server: Option<Server>,
}

fn rig(ps2: bool) -> Rig {
    rig_with(ps2, false)
}

fn rig_with(ps2: bool, pm: bool) -> Rig {
    rig_full(ps2, pm, None)
}

fn rig_full(ps2: bool, pm: bool, display: Option<Arc<Display>>) -> Rig {
    let dir = Dir::new();
    let path = dir.path("c.sock");
    let board = board();
    // XLAT off (the set 2 bytes as sent) and the mouse reporting, as a Linux guest leaves them.
    board.command_with(0x60, 0x07);
    assert_eq!(board.mouse(&[0xf4]), [0xfa]);
    let cancel = Canceller::detached();
    let pm = pm.then(|| {
        crate::clock::Timer::new(
            crate::pm::Pm::new(&board.irqs, crate::clock::frequency(), Default::default()),
            "pm-test",
        )
    });
    let target = Target {
        ps2: ps2.then(|| board.input.clone()),
        power_button: pm.as_ref().map(crate::pm::PowerButton::of),
        cancel: cancel.clone(),
        status: StatusSource { vcpus: 2, started: Instant::now() },
        irqs: board.irqs.clone(),
        display,
    };
    let server = serve(&path, target).unwrap();
    Rig { dir, path, board, cancel, pm, server: Some(server) }
}

impl Rig {
    fn client(&self) -> Client {
        Client::connect(&self.path)
    }
    fn server(&self) -> &Server {
        self.server.as_ref().unwrap()
    }
    /// Reads the 8042 until `kbd` keyboard and `mouse` mouse bytes arrived, then 50 ms more for any further byte.
    fn collect(&self, kbd: usize, mouse: usize) -> (Vec<u8>, Vec<u8>) {
        let (mut k, mut m) = (Vec::new(), Vec::new());
        let end = Instant::now() + PATIENCE;
        let mut quiet = None;
        loop {
            match self.board.read() {
                Some((byte, true)) => m.push(byte),
                Some((byte, false)) => k.push(byte),
                None => {
                    if k.len() >= kbd && m.len() >= mouse {
                        let q = *quiet.get_or_insert_with(|| Instant::now() + Duration::from_millis(50));
                        if Instant::now() >= q {
                            return (k, m);
                        }
                    } else if Instant::now() >= end {
                        panic!("timed out: keyboard {k:x?}, mouse {m:x?}");
                    }
                    std::thread::sleep(Duration::from_micros(200));
                }
            }
        }
    }
}

/// A client: lines out, lines in (reads time out after [`PATIENCE`]).
struct Client {
    stream: UnixStream,
    lines: BufReader<UnixStream>,
}
impl Client {
    fn connect(path: &Path) -> Client {
        let stream = UnixStream::connect(path).unwrap();
        stream.set_read_timeout(Some(PATIENCE)).unwrap();
        let lines = BufReader::new(stream.try_clone().unwrap());
        Client { stream, lines }
    }
    fn send(&mut self, line: &str) {
        self.stream.write_all(format!("{line}\n").as_bytes()).unwrap();
    }
    /// The next line, as JSON.
    fn recv(&mut self) -> Value {
        let mut line = String::new();
        let n = self.lines.read_line(&mut line).expect("a line before the timeout");
        assert!(n > 0, "the server closed the connection");
        assert!(line.ends_with('\n'), "a whole line: {line:?}");
        serde_json::from_str(&line).unwrap_or_else(|e| panic!("{e}: {line:?}"))
    }
    /// Whether the server closed the connection (end of file before the timeout).
    fn closed(&mut self) -> bool {
        let mut rest = Vec::new();
        match self.lines.read_to_end(&mut rest) {
            Ok(_) => true,
            Err(e) if e.kind() == ErrorKind::ConnectionReset => true,
            Err(_) => false,
        }
    }
    /// A request and its response.
    fn call(&mut self, id: u64, method: &str, params: Value) -> Value {
        let mut request = json!({"jsonrpc": "2.0", "id": id, "method": method});
        if !params.is_null() {
            request["params"] = params; // null: no params member
        }
        self.send(&request.to_string());
        // Past notifications (a display's display_changed can come at any time).
        let r = loop {
            let r = self.recv();
            if r.get("id").is_some() || r.is_array() {
                break r;
            }
        };
        assert_eq!(r["id"], json!(id), "{r}");
        r
    }
    fn status(&mut self, id: u64) -> Value {
        let r = self.call(id, "query_status", json!({}));
        r["result"].clone()
    }
}

fn key(code: &str, pressed: bool) -> Value {
    json!({"type": "key", "code": code, "pressed": pressed})
}

/// Every request gets exactly one response, in the order the requests arrived, pipelined or not.
#[test]
fn request_gets_one_response_in_order() {
    let r = rig(true);
    let mut c = r.client();
    let mut lines = String::new();
    for id in 1..=40u64 {
        let line = match id % 3 {
            0 => json!({"jsonrpc": "2.0", "id": id, "method": "query_status"}),
            // Four key events (6 bytes: they fit in the 8042 with no reader).
            1 if id < 12 => json!({"jsonrpc": "2.0", "id": id, "method": "send_input", "params": {"events": [key("KeyA", id % 2 == 1)]}}),
            _ => json!({"jsonrpc": "2.0", "id": id, "method": "send_input", "params": {"events": []}}),
        };
        lines.push_str(&format!("{line}\n"));
    }
    c.stream.write_all(lines.as_bytes()).unwrap();
    for id in 1..=40u64 {
        let v = c.recv();
        assert_eq!(v["jsonrpc"], "2.0");
        assert_eq!(v["id"], json!(id), "in order: {v}");
        assert!(v.get("result").is_some() && v.get("error").is_none(), "{v}");
    }
    assert_eq!(r.collect(6, 0).0, [0x1c, 0xf0, 0x1c, 0x1c, 0xf0, 0x1c], "the key events, in order");
    // Nothing more: the next line is the next request's answer.
    assert_eq!(c.call(99, "query_status", json!([]))["id"], 99);
    let status = c.status(100);
    assert_eq!(status["protocol"], 1);
    assert_eq!(status["state"], "running");
    assert_eq!(status["vcpus"], 2);
    assert_eq!(status["devices"], json!({"ps2": true}));
    assert!(status["uptime_ms"].is_u64(), "{status}");
    // A string id comes back as given.
    c.send(r#"{"jsonrpc":"2.0","id":"x7","method":"query_status"}"#);
    assert_eq!(c.recv()["id"], "x7");
    // A client that closes its sending half (`echo ... | socat`) gets the answers to what it sent, a last line without
    // its newline included, then the connection closes.
    let mut once = r.client();
    once.stream.write_all(b"\n{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"query_status\"}\n{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"query_status\"}").unwrap();
    once.stream.shutdown(std::net::Shutdown::Write).unwrap();
    assert_eq!(once.recv()["id"], 1, "a blank line is ignored");
    assert_eq!(once.recv()["id"], 2);
    assert!(once.closed());
}

/// A batch is answered with one array: a response per request, in order; notifications leave no entry, and a batch of
/// notifications only is not answered at all. An empty batch is one invalid request (not an array).
#[test]
fn batch_is_answered_as_array() {
    let r = rig(true);
    let mut c = r.client();
    c.send(&format!(
        "[{},{},{},{},{}]",
        r#"{"jsonrpc":"2.0","id":1,"method":"query_status"}"#,
        r#"{"jsonrpc":"2.0","method":"send_input","params":{"events":[{"type":"key","code":"KeyB","pressed":true}]}}"#,
        r#"{"jsonrpc":"2.0","id":3,"method":"reboot"}"#,
        r#"7"#,
        r#"{"jsonrpc":"2.0","id":5,"method":"send_input","params":{"events":[{"type":"key","code":"KeyB","pressed":false}]}}"#,
    ));
    let v = c.recv();
    let a = v.as_array().unwrap_or_else(|| panic!("an array: {v}"));
    assert_eq!(a.len(), 4, "{v}");
    assert_eq!((&a[0]["id"], &a[0]["result"]["protocol"]), (&json!(1), &json!(1)));
    assert_eq!((&a[1]["id"], &a[1]["error"]["code"]), (&json!(3), &json!(-32601)));
    assert_eq!((&a[2]["id"], &a[2]["error"]["code"]), (&Value::Null, &json!(-32600)));
    assert_eq!((&a[3]["id"], &a[3]["result"]), (&json!(5), &json!({})));
    assert_eq!(r.collect(3, 0).0, [0x32, 0xf0, 0x32], "both input events applied, in order");
    // Notifications only: no response; the next line answers the next request.
    c.send(r#"[{"jsonrpc":"2.0","method":"query_status"},{"jsonrpc":"2.0","method":"nope"}]"#);
    assert_eq!(c.call(6, "query_status", Value::Null)["id"], 6);
    c.send("[]");
    let v = c.recv();
    assert_eq!((&v["id"], &v["error"]["code"]), (&Value::Null, &json!(-32600)), "{v}");
}

/// A request without an id is carried out and never answered, errors included.
#[test]
fn notification_gets_no_response() {
    let r = rig(true);
    let mut c = r.client();
    c.send(r#"{"jsonrpc":"2.0","method":"send_input","params":{"events":[{"type":"key","code":"KeyA","pressed":true}]}}"#);
    c.send(r#"{"jsonrpc":"2.0","method":"query_status"}"#);
    c.send(r#"{"jsonrpc":"2.0","method":"reboot"}"#);
    c.send(r#"{"jsonrpc":"2.0","method":"send_input","params":{"events":[{"type":"key","code":"KeyA"}]}}"#);
    c.send(r#"{"jsonrpc":"2.0","method":"send_input","params":{"events":[{"type":"pointer_motion","dx":70000,"dy":0}]}}"#);
    assert_eq!(c.call(1, "query_status", json!({}))["id"], 1, "the first line back answers the request");
    assert_eq!(r.collect(1, 0).0, [0x1c], "the notification's key reached the 8042");
}

/// Each malformed request gets its code (and the request's id when it has one); a line over 1 MiB gets -32700 and the
/// connection is closed; other clients and later connections keep working.
#[test]
fn protocol_errors() {
    let r = rig(true);
    let mut c = r.client();
    let code = |v: &Value| v["error"]["code"].as_i64().unwrap_or_else(|| panic!("an error: {v}"));
    c.send("{not json");
    let v = c.recv();
    assert_eq!((code(&v), &v["id"]), (-32700, &Value::Null));
    c.send(r#"{"jsonrpc":"2.0","id":2}"#);
    let v = c.recv();
    assert_eq!((code(&v), &v["id"]), (-32600, &json!(2)));
    c.send(r#"{"jsonrpc":"1.0","id":3,"method":"quit"}"#);
    assert_eq!(code(&c.recv()), -32600);
    c.stream.write_all(b"{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"\xff\"}\n").unwrap();
    let v = c.recv();
    assert_eq!((code(&v), &v["id"]), (-32700, &Value::Null), "invalid UTF-8: {v}");
    let v = c.call(4, "reboot", json!({}));
    assert_eq!(code(&v), -32601);
    // Without the device: -32001, naming it.
    let v = c.call(5, "power_button", json!({}));
    assert_eq!((code(&v), &v["error"]["data"]), (-32001, &json!({"device": "power_button"})));
    let v = c.call(6, "quit", json!({"now": true}));
    assert_eq!(code(&v), -32602);
    assert!(!r.cancel.cancelled(), "a refused quit does not quit");
    let v = c.call(7, "send_input", json!({"events": [{"type": "key", "code": "NoSuchKey", "pressed": true}]}));
    assert_eq!(code(&v), -32602);
    assert!(v["error"]["data"].is_string(), "serde's message: {v}");
    // The motion cap: a magnitude over 65535 in any event refuses the whole request, before anything is applied.
    for (events, field) in [
        (json!([key("KeyA", true), {"type": "pointer_motion", "dx": 65536, "dy": 0}]), "events[1].dx"),
        (json!([{"type": "pointer_motion", "dx": 0, "dy": -65536}]), "events[0].dy"),
        (json!([key("KeyA", true), {"type": "pointer_motion", "dx": 1, "dy": 1}, {"type": "scroll", "dy": i32::MIN}]), "events[2].dy"),
    ] {
        let v = c.call(8, "send_input", json!({ "events": events }));
        assert_eq!(code(&v), -32602, "{v}");
        let data = v["error"]["data"].as_str().unwrap();
        assert!(data.contains(field) && data.contains("65535"), "{data}");
    }
    assert_eq!(r.collect(0, 0), (vec![], vec![]), "nothing of a refused request was applied");
    let v = c.call(9, "send_input", json!({"events": [{"type": "pointer_motion", "dx": -255, "dy": 255}, {"type": "scroll", "dy": 0}]}));
    assert_eq!(v["result"], json!({}), "within the cap: {v}");
    assert_eq!(r.collect(0, 3).1, [0x38, 1, 1]);

    // Another client, connected before the long line, is unaffected by it.
    let mut other = r.client();
    assert_eq!(other.status(1)["protocol"], 1);
    // A 2 MiB line: -32700, then the server closes the connection (the writing side may see it closed part-way).
    let mut long = r.client();
    let writer = long.stream.try_clone().unwrap();
    let flood = std::thread::spawn(move || {
        let mut writer = writer;
        let chunk = vec![b'a'; 64 << 10];
        for _ in 0..32 {
            if writer.write_all(&chunk).is_err() {
                return;
            }
        }
        let _ = writer.write_all(b"\n");
    });
    let v = long.recv();
    assert_eq!((code(&v), &v["id"]), (-32700, &Value::Null), "{v}");
    assert!(long.closed(), "the connection is closed after an over-long line");
    flood.join().unwrap();
    assert_eq!(other.status(2)["state"], "running");
    assert_eq!(r.client().status(1)["state"], "running", "a new connection is served");
}

/// Input on a machine without the 8042: -32001 naming the device; the connection and the run go on.
#[test]
fn input_without_device_is_an_error() {
    let r = rig(false);
    let mut c = r.client();
    let v = c.call(1, "send_input", json!({"events": [key("KeyA", true)]}));
    assert_eq!(v["error"]["code"], -32001, "{v}");
    assert_eq!(v["error"]["data"], json!({"device": "ps2"}));
    let v = c.call(2, "send_input", json!({"events": []}));
    assert_eq!(v["error"]["code"], -32001, "even with no events: {v}");
    let s = c.status(3);
    assert_eq!((&s["devices"], &s["state"]), (&json!({"ps2": false}), &json!("running")));
    assert!(!r.cancel.cancelled());
}

/// `quit` cancels the run (as Ctrl-A x): answered `{}`, the canceller set, the state `stopping`; a `quit` notification
/// quits too.
#[test]
fn quit_cancels() {
    let r = rig(true);
    let mut c = r.client();
    assert!(!r.cancel.cancelled());
    let v = c.call(1, "quit", Value::Null);
    assert_eq!(v["result"], json!({}), "{v}");
    assert!(r.cancel.cancelled());
    assert_eq!(c.status(2)["state"], "stopping");
    let r = rig(true);
    let mut c = r.client();
    c.send(r#"{"jsonrpc":"2.0","method":"quit"}"#);
    let end = Instant::now() + PATIENCE;
    while !r.cancel.cancelled() {
        assert!(Instant::now() < end, "the quit notification cancels");
        std::thread::sleep(Duration::from_millis(1));
    }
    assert_eq!(c.status(1)["state"], "stopping", "and is not answered");
}

/// A client that disconnects holding keys and buttons leaves none down in the guest: the breaks and the button mask
/// its leaving causes are sent; what another client holds stays down.
#[test]
fn disconnect_releases_held_keys() {
    let r = rig(true);
    let mut a = r.client();
    let mut b = r.client();
    let v = a.call(1, "send_input", json!({"events": [key("ShiftLeft", true), key("ArrowUp", true), {"type": "button", "button": "left", "pressed": true}]}));
    assert_eq!(v["result"], json!({}), "{v}");
    let v = b.call(1, "send_input", json!({"events": [key("KeyB", true)]}));
    assert_eq!(v["result"], json!({}), "{v}");
    assert_eq!(r.collect(4, 3), (vec![0x12, 0xe0, 0x75, 0x32], vec![0x09, 0, 0]));
    drop(a);
    assert_eq!(
        r.collect(5, 3),
        (vec![0xf0, 0x12, 0xe0, 0xf0, 0x75], vec![0x08, 0, 0]),
        "A's keys broken and its button lifted, B's KeyB kept"
    );
    assert_eq!(b.status(2)["state"], "running");
    drop(b);
    assert_eq!(r.collect(2, 0).0, [0xf0, 0x32]);
}

/// At most 8 clients: a ninth connection is closed at once, and once one of the 8 leaves a new one is served.
#[test]
fn ninth_client_is_refused() {
    let r = rig(true);
    let mut clients: Vec<Client> = (0..8).map(|_| r.client()).collect();
    for (i, c) in clients.iter_mut().enumerate() {
        assert_eq!(c.status(i as u64)["protocol"], 1);
    }
    let mut ninth = r.client();
    assert!(ninth.closed(), "the ninth connection is closed");
    for (i, c) in clients.iter_mut().enumerate() {
        assert_eq!(c.status(10 + i as u64)["protocol"], 1, "the 8 are still served");
    }
    drop(clients.pop());
    // The slot frees once the server has seen the client leave: retry until a new connection is served. A refused
    // attempt may fail at the write (the server closed it first) or at the read.
    let end = Instant::now() + Duration::from_secs(30);
    loop {
        let mut c = r.client();
        let line = r#"{"jsonrpc":"2.0","id":1,"method":"query_status"}"#.to_string() + "\n";
        let mut answer = String::new();
        if c.stream.write_all(line.as_bytes()).is_ok() && c.lines.read_line(&mut answer).unwrap_or(0) > 0 {
            assert_eq!(serde_json::from_str::<Value>(&answer).unwrap()["id"], 1);
            break;
        }
        assert!(Instant::now() < end, "a slot frees after a client leaves");
        std::thread::sleep(Duration::from_millis(5));
    }
}

/// Floods `stream` with pipelined `query_status` requests and never reads; returns once a write fails (the server
/// closed the connection) or `stop` is set.
fn flood(stream: UnixStream, stop: &AtomicBool) -> bool {
    let mut stream = stream;
    stream.set_write_timeout(Some(Duration::from_millis(100))).unwrap();
    let line = r#"{"jsonrpc":"2.0","id":1,"method":"query_status"}"#.to_string() + "\n";
    let batch = line.repeat(256);
    while !stop.load(SeqCst) {
        match stream.write_all(batch.as_bytes()) {
            Ok(()) => {}
            Err(e) if matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) => {}
            Err(_) => return true,
        }
    }
    false
}

/// A client that pipelines requests and never reads is disconnected (by the send timeout or the output cap); another
/// client is answered meanwhile and after, and the run goes on.
#[test]
fn slow_reader_is_dropped() {
    let r = rig(true);
    let mut good = r.client();
    let slow = UnixStream::connect(&r.path).unwrap();
    let stop = AtomicBool::new(false);
    std::thread::scope(|s| {
        let flooder = s.spawn(|| flood(slow.try_clone().unwrap(), &stop));
        // Answered while the slow client floods.
        for id in 0..20 {
            assert_eq!(good.status(id)["state"], "running");
        }
        let end = Instant::now() + PATIENCE;
        while !flooder.is_finished() {
            if Instant::now() >= end {
                stop.store(true, SeqCst);
                panic!("the slow reader was never disconnected");
            }
            assert_eq!(good.status(1)["state"], "running");
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(flooder.join().unwrap(), "the server closed the slow reader's connection");
    });
    assert_eq!(good.status(2)["state"], "running");
    assert!(!r.cancel.cancelled(), "the run goes on");
}

/// `stopped` returns within 2 s although a client never reads (its writer blocked on a full socket, its queue well
/// under the output cap), and a reading client still gets the notification.
#[test]
fn stopped_never_waits() {
    let mut r = rig(true);
    let mut reader = r.client();
    assert_eq!(reader.status(1)["protocol"], 1);
    // 2500 requests: about 125 KB in, 275 KB of responses queued, far more than the sockets' buffers hold.
    let mut slow = UnixStream::connect(&r.path).unwrap();
    let requests = (r#"{"jsonrpc":"2.0","id":1,"method":"query_status"}"#.to_string() + "\n").repeat(2500);
    let writer = std::thread::spawn(move || {
        let _ = slow.write_all(requests.as_bytes());
        slow // kept open, never read
    });
    let server = std::sync::Arc::new(r.server.take().unwrap());
    // Wait (polling, bounded) until a writer is blocked: a queue beyond what the sockets' buffers take.
    let end = Instant::now() + PATIENCE;
    while !server.queued().iter().any(|&q| q > 100 << 10) {
        assert!(Instant::now() < end, "the slow client's responses never piled up: {:?}", server.queued());
        std::thread::sleep(Duration::from_millis(1));
    }
    let (done, finished) = std::sync::mpsc::channel();
    let s = server.clone();
    // Its own thread, so a `stopped` that never returns fails the test instead of hanging it.
    std::thread::spawn(move || {
        let start = Instant::now();
        s.stopped(StopReason::Cancelled);
        let _ = done.send(start.elapsed());
    });
    let took = finished.recv_timeout(PATIENCE).expect("stopped returned");
    assert!(took < Duration::from_secs(2), "stopped took {took:?}");
    assert_eq!(reader.recv(), json!({"jsonrpc": "2.0", "method": "stopped", "params": {"reason": "cancelled"}}));
    assert!(reader.closed());
    drop(writer.join().unwrap());
}

/// `stopped` sends the notification to every client, then closes every connection.
#[test]
fn stopped_reaches_every_client() {
    let r = rig(true);
    let mut clients: Vec<Client> = (0..3).map(|_| r.client()).collect();
    for c in &mut clients {
        assert_eq!(c.status(1)["protocol"], 1);
    }
    r.server().stopped(StopReason::PowerOff);
    for c in &mut clients {
        assert_eq!(c.recv(), json!({"jsonrpc": "2.0", "method": "stopped", "params": {"reason": "power_off"}}));
        assert!(c.closed(), "closed after the notification");
    }
    // A second call does nothing; no new connection is accepted (the socket is gone or refuses).
    r.server().stopped(StopReason::Error);
    assert!(UnixStream::connect(&r.path).is_err());
}

/// The socket's rules: mode 0600, made without leftovers beside it; a stale socket replaced, a live one refused, a
/// non-socket refused, a path over 103 bytes refused; removed when the server is dropped; a busy guard held meanwhile.
#[test]
fn socket_rules() {
    let mut r = rig(true);
    let mode = std::fs::metadata(&r.path).unwrap().permissions().mode();
    assert_eq!(mode & 0o777, 0o600, "{mode:o}");
    assert_eq!(r.dir.names(), ["c.sock"], "no temporary folder left beside the socket");
    let busy = r.board.irqs.live();
    assert!(busy >= 1);
    // A live socket is refused.
    let target = || Target {
        ps2: None,
        power_button: None,
        cancel: Canceller::detached(),
        status: StatusSource { vcpus: 1, started: Instant::now() },
        irqs: r.board.irqs.clone(),
        display: None,
    };
    let e = serve(&r.path, target()).err().expect("a live socket is refused");
    assert!(e.contains("another program listens on this socket"), "{e}");
    assert_eq!(r.client().status(1)["state"], "running", "the first server is unaffected");
    // Removed on drop, the busy guard with it.
    drop(r.server.take());
    assert!(!r.path.exists(), "removed when the server is dropped");
    assert_eq!(r.board.irqs.live(), busy - 1, "the busy guard is released");
    // A stale socket is replaced.
    drop(UnixListener::bind(&r.path).unwrap());
    assert!(r.path.exists());
    let server = serve(&r.path, target()).expect("a stale socket is replaced");
    assert_eq!(r.client().status(1)["devices"]["ps2"], false);
    drop(server);
    assert_eq!(r.dir.names(), Vec::<String>::new());
    // Not a socket; too long.
    std::fs::write(&r.path, b"x").unwrap();
    let e = serve(&r.path, target()).err().expect("a file is refused");
    assert!(e.contains("exists and is not a socket"), "{e}");
    assert_eq!(std::fs::read(&r.path).unwrap(), b"x", "and left alone");
    // Something that appears at the path between the checks and the move into place is not replaced.
    std::fs::remove_file(&r.path).unwrap();
    std::fs::write(&r.path, b"y").unwrap();
    let e = bind_private(&r.path).err().expect("the move into place does not replace");
    assert!(e.contains("another program listens on this socket"), "{e}");
    assert_eq!(std::fs::read(&r.path).unwrap(), b"y", "left alone");
    assert_eq!(r.dir.names(), ["c.sock"], "no temporary folder left behind");
    let long = r.dir.path(&"x".repeat(110));
    let e = serve(&long, target()).err().expect("a long path is refused");
    assert!(e.contains("at most 103 bytes"), "{e}");
    assert_eq!(r.board.irqs.live(), busy - 1, "no guard left by a refused serve");
}

#[test]
fn power_button_presses() {
    let r = rig_with(true, true);
    let pm = r.pm.as_ref().unwrap();
    let mut c = r.client();
    let status = || crate::devices::Device::read(&mut *pm.lock(), 0, 0, 2).unwrap();
    assert_eq!(status() & 1 << 8, 0);
    let v = c.call(1, "power_button", json!({}));
    assert_eq!(v["result"], json!({}), "{v}");
    assert_eq!(status() & 1 << 8, 1 << 8, "PWRBTN_STS set");
    // Pressing again is harmless, and the status still answers.
    assert_eq!(c.call(2, "power_button", json!({}))["result"], json!({}));
    assert_eq!(c.status(3)["state"], "running");
    // While stopping: -32002, and nothing more is pressed.
    assert!(crate::devices::Device::write(&mut *pm.lock(), 0, 0, 2, 1 << 8));
    assert_eq!(c.call(4, "quit", json!({}))["result"], json!({}));
    let v = c.call(5, "power_button", json!({}));
    assert_eq!(v["error"]["code"], json!(-32002), "{v}");
    assert_eq!(status() & 1 << 8, 0, "a refused press sets nothing");
}

#[test]
fn power_button_without_the_device_is_refused() {
    let r = rig(true);
    let mut c = r.client();
    let v = c.call(1, "power_button", json!({}));
    assert_eq!((&v["error"]["code"], &v["error"]["data"]), (&json!(-32001), &json!({"device": "power_button"})), "{v}");
}

// ---- the display's methods -----------------------------------------------------------------------------------------

use crate::display::bochs::{BPP, BYTE_ORDER, DISPI, ENABLE, ENABLED, VIRT_WIDTH, X_OFFSET, XRES, Y_OFFSET, YRES};
use crate::pci::{DeviceMemory, Function};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};

/// A display with a framebuffer of `mib` MiB.
fn display(mib: u64) -> Arc<Display> {
    Display::new(DeviceMemory::new(mib << 20).unwrap(), (1024, 768))
}
fn dispi(d: &Display, index: usize, value: u16) {
    d.bar_access(0, 2, DISPI + 2 * index as u64, 2, Some(value.into()));
}
/// A mode as Linux's driver sets it: `w` x `h` in rows of `pitch` pixels from pixel (`x`, `y`).
fn modeset(d: &Display, (w, h): (u16, u16), pitch: u16, (x, y): (u16, u16)) {
    dispi(d, ENABLE, 0);
    for (r, v) in [(BPP, 32), (XRES, w), (YRES, h), (VIRT_WIDTH, pitch), (X_OFFSET, x), (Y_OFFSET, y)] {
        dispi(d, r, v);
    }
    dispi(d, ENABLE, ENABLED | 0x40);
}
/// The test picture's pixel (x, y) as R, G, B.
fn colour(x: u32, y: u32) -> [u8; 3] {
    [(x * 3 + y) as u8, (y * 7) as u8, (x ^ y) as u8]
}
/// Fills a `pitch`-pixel-wide buffer at byte 0 with the picture placed at (`x0`, `y0`), x8r8g8b8 little-endian.
fn paint(d: &Display, pitch: u32, rows: u32, (x0, y0): (u32, u32)) {
    let mut words = Vec::new();
    for y in 0..rows {
        for x in (0..pitch).step_by(2) {
            let px = |x: u32| {
                if x < x0 || y < y0 {
                    return 0xdead_beef_u64;
                }
                let [r, g, b] = colour(x - x0, y - y0);
                u64::from(r) << 16 | u64::from(g) << 8 | u64::from(b) | 0x7f << 24
            };
            words.push(px(x) | px(x + 1) << 32);
        }
    }
    d.vram().write_words(0, &words);
}
fn picture(w: u32, h: u32) -> Vec<u8> {
    (0..h).flat_map(|y| (0..w).flat_map(move |x| colour(x, y))).collect()
}
/// A PNG's size and RGB pixels (the `png` crate's decoder).
fn decode(png: &[u8]) -> (u32, u32, Vec<u8>) {
    let mut reader = png::Decoder::new(std::io::Cursor::new(png)).read_info().unwrap();
    let mut out = vec![0; reader.output_buffer_size().unwrap()];
    let info = reader.next_frame(&mut out).unwrap();
    assert_eq!((info.color_type, info.bit_depth), (png::ColorType::Rgb, png::BitDepth::Eight));
    out.truncate(info.buffer_size());
    (info.width, info.height, out)
}
fn snapshot_of(c: &mut Client, id: u64) -> Value {
    c.call(id, "snapshot", json!({"format": "png"}))
}
fn png_of(answer: &Value) -> (u32, u32, Vec<u8>) {
    use base64::Engine;
    let b64 = answer["result"]["png"].as_str().unwrap_or_else(|| panic!("a snapshot: {answer}"));
    let (w, h, rgb) = decode(&base64::engine::general_purpose::STANDARD.decode(b64).unwrap());
    assert_eq!((answer["result"]["width"].as_u64(), answer["result"]["height"].as_u64()), (Some(w.into()), Some(h.into())));
    (w, h, rgb)
}

/// Without a display, `snapshot` and `framebuffer` are -32001 naming it, and `query_status` has no display member.
#[test]
fn display_methods_need_the_display() {
    let r = rig(true);
    let mut c = r.client();
    for (id, method) in [(1, "snapshot"), (2, "framebuffer")] {
        let v = c.call(id, method, Value::Null);
        assert_eq!(v["error"], json!({"code": -32001, "message": "Device absent", "data": {"device": "display"}}), "{v}");
    }
    assert_eq!(c.status(3)["devices"], json!({"ps2": true}), "as before the display existed");
    let v = c.call(4, "snapshot", json!({"format": "jpeg"}));
    assert_eq!(v["error"]["code"], -32602, "{v}");
}

/// Before the guest enables the display (and after it disables it): -32004, never an empty picture; query_status says
/// null. Once enabled: the picture and the mode.
#[test]
fn snapshot_while_disabled() {
    let d = display(16);
    let r = rig_full(true, false, Some(d.clone()));
    let mut c = r.client();
    let disabled = json!({"code": -32004, "message": "Display not enabled"});
    assert_eq!(snapshot_of(&mut c, 1)["error"], disabled);
    assert_eq!(c.call(2, "framebuffer", json!({}))["error"], disabled);
    assert_eq!(c.status(3)["devices"], json!({"ps2": true, "display": null}));
    paint(&d, 640, 480, (0, 0));
    modeset(&d, (640, 480), 640, (0, 0));
    assert_eq!(png_of(&snapshot_of(&mut c, 4)), (640, 480, picture(640, 480)));
    assert_eq!(
        c.status(5)["devices"]["display"],
        json!({"width": 640, "height": 480, "stride": 2560, "offset": 0, "format": "x8r8g8b8"})
    );
    dispi(&d, ENABLE, 0);
    assert_eq!(snapshot_of(&mut c, 6)["error"], disabled, "disabled again");
    assert_eq!(c.status(7)["devices"]["display"], Value::Null);
    // past the snapshot bound: refused, naming the sizes
    modeset(&d, (2600, 1000), 2600, (0, 0));
    let v = snapshot_of(&mut c, 8);
    assert_eq!(v["error"]["code"], -32005, "{v}");
    assert_eq!(v["error"]["data"], json!({"width": 2600, "height": 1000, "max_width": 2560, "max_height": 1600}));
}

/// A client that reads with `recvmsg`, keeping the descriptors that come with the bytes.
struct FdClient {
    stream: UnixStream,
    buffer: Vec<u8>,
    fds: Vec<OwnedFd>,
}
impl FdClient {
    fn connect(path: &Path) -> Self {
        let stream = UnixStream::connect(path).unwrap();
        stream.set_read_timeout(Some(PATIENCE)).unwrap();
        FdClient { stream, buffer: Vec::new(), fds: Vec::new() }
    }
    fn send(&self, line: &[u8]) {
        (&self.stream).write_all(line).unwrap();
    }
    /// The next answer (notifications skipped) and every descriptor received since the last answer.
    fn answer(&mut self) -> (Value, Vec<OwnedFd>) {
        loop {
            while let Some(at) = self.buffer.iter().position(|&b| b == b'\n') {
                let line: Vec<u8> = self.buffer.drain(..=at).collect();
                let v: Value = serde_json::from_slice(&line).unwrap();
                if v.get("id").is_some() || v.is_array() {
                    return (v, std::mem::take(&mut self.fds));
                }
            }
            let mut buf = vec![0u8; 64 << 10];
            let mut control = vec![0u64; 64];
            let mut iov = libc::iovec { iov_base: buf.as_mut_ptr().cast(), iov_len: buf.len() };
            let mut msg: libc::msghdr = unsafe { std::mem::zeroed() };
            msg.msg_iov = &mut iov;
            msg.msg_iovlen = 1;
            msg.msg_control = control.as_mut_ptr().cast();
            msg.msg_controllen = (8 * control.len()) as libc::socklen_t;
            let n = unsafe { libc::recvmsg(self.stream.as_raw_fd(), &mut msg, 0) };
            assert!(n > 0, "recvmsg: {n} {}", std::io::Error::last_os_error());
            assert_eq!(msg.msg_flags & libc::MSG_CTRUNC, 0, "control data cut");
            unsafe {
                let mut cmsg = libc::CMSG_FIRSTHDR(&msg);
                while !cmsg.is_null() {
                    assert_eq!(((*cmsg).cmsg_level, (*cmsg).cmsg_type), (libc::SOL_SOCKET, libc::SCM_RIGHTS));
                    let k = ((*cmsg).cmsg_len as usize - libc::CMSG_LEN(0) as usize) / 4;
                    let p = libc::CMSG_DATA(cmsg).cast::<libc::c_int>();
                    self.fds.extend((0..k).map(|i| OwnedFd::from_raw_fd(*p.add(i))));
                    cmsg = libc::CMSG_NXTHDR(&msg, cmsg);
                }
            }
            self.buffer.extend_from_slice(&buf[..n as usize]);
        }
    }
}

/// `framebuffer` answers the mode and the framebuffer's size with one descriptor (SCM_RIGHTS on the answer's line):
/// mapped read-only it shows the picture at the reported offset and stride, as the guest writes it; it cannot be
/// mapped for writing. A batch's descriptors come with its line, in order; a notification's are not sent.
#[test]
fn framebuffer_passes_the_descriptor() {
    let d = display(16);
    let r = rig_full(true, false, Some(d.clone()));
    // a 640 x 480 picture in rows of 800 pixels, from pixel (5, 16): the offset is 4 mod 8
    paint(&d, 800, 496, (5, 16));
    modeset(&d, (640, 480), 800, (5, 16));
    let mut c = FdClient::connect(&r.path);
    c.send(b"{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"framebuffer\"}\n");
    let (v, fds) = c.answer();
    let offset = 16 * 3200 + 5 * 4;
    assert_eq!(
        v["result"],
        json!({"width": 640, "height": 480, "stride": 3200, "offset": offset, "format": "x8r8g8b8", "size": 16 << 20}),
        "{v}"
    );
    assert_eq!(fds.len(), 1, "one descriptor");
    let fd = fds[0].as_raw_fd();
    let len = 16usize << 20;
    let writable = unsafe { libc::mmap(std::ptr::null_mut(), len, libc::PROT_READ | libc::PROT_WRITE, libc::MAP_SHARED, fd, 0) };
    assert_eq!(writable, libc::MAP_FAILED, "not writable");
    let base = unsafe { libc::mmap(std::ptr::null_mut(), len, libc::PROT_READ, libc::MAP_SHARED, fd, 0) };
    assert_ne!(base, libc::MAP_FAILED);
    let fb = unsafe { std::slice::from_raw_parts(base.cast::<u8>(), len) };
    let mut seen = Vec::new();
    for y in 0..480usize {
        for x in 0..640usize {
            let p = &fb[offset + y * 3200 + 4 * x..][..4];
            seen.extend_from_slice(&[p[2], p[1], p[0]]);
        }
    }
    assert_eq!(seen, picture(640, 480), "the visible pixels through the client's mapping");
    // the guest writes: the client's mapping sees it (the same object, no copy)
    d.vram().write_words(offset as u64 - 4, &[0x00ab_cdef_0000_0000]);
    assert_eq!(&fb[offset..offset + 3], [0xef, 0xcd, 0xab]);
    unsafe { libc::munmap(base, len) };
    // a batch: two descriptors with its line; a notification's is dropped, the next answer carries none
    c.send(
        b"[{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"framebuffer\"},{\"jsonrpc\":\"2.0\",\"method\":\"framebuffer\"},\
          {\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"framebuffer\"}]\n",
    );
    let (v, fds) = c.answer();
    assert_eq!(v.as_array().map(|a| a.len()), Some(2), "{v}");
    assert_eq!(fds.len(), 2);
    c.send(b"{\"jsonrpc\":\"2.0\",\"method\":\"framebuffer\"}\n{\"jsonrpc\":\"2.0\",\"id\":4,\"method\":\"query_status\"}\n");
    let (v, fds) = c.answer();
    assert_eq!((v["id"].as_u64(), fds.len()), (Some(4), 0));
    // the big-endian byte order is reported
    d.bar_access(0, 2, BYTE_ORDER, 4, Some(0xbebe_bebe));
    c.send(b"{\"jsonrpc\":\"2.0\",\"id\":5,\"method\":\"framebuffer\"}\n");
    assert_eq!(c.answer().0["result"]["format"], "b8g8r8x8");
}

/// A snapshot whose answer is larger than the output cap (a noisy 1024 x 768 picture: about 3 MiB of base64) is
/// answered whole; the client's next request is answered after it.
#[test]
fn large_snapshot_is_answered() {
    let d = display(16);
    let mut x = 0x2545_f491_4f6c_dd1d_u64;
    let noise: Vec<u64> = (0..(1024 * 768 / 2))
        .map(|_| {
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            x
        })
        .collect();
    d.vram().write_words(0, &noise);
    modeset(&d, (1024, 768), 1024, (0, 0));
    let r = rig_full(true, false, Some(d.clone()));
    let mut c = r.client();
    c.send(r#"{"jsonrpc":"2.0","id":1,"method":"snapshot"}"#);
    let mut line = String::new();
    c.lines.read_line(&mut line).unwrap();
    assert!(line.len() > OUTPUT_CAP, "{} bytes", line.len());
    let v: Value = serde_json::from_str(&line).unwrap();
    let bytes: Vec<u8> = noise.iter().flat_map(|w| w.to_le_bytes()).collect();
    let rgb: Vec<u8> = bytes.chunks_exact(4).flat_map(|p| [p[2], p[1], p[0]]).collect();
    assert_eq!(png_of(&v), (1024, 768, rgb));
    assert_eq!(c.status(2)["state"], "running");
}

/// While a large answer is being written, notifications still reach the client: the guest changes its mode while the
/// client has not yet read a snapshot's answer (longer than OUTPUT_CAP, so its write is in flight), and the client gets
/// the answer, then the display_changed lines, and stays connected.
#[test]
fn notifications_during_a_large_answer() {
    let d = display(16);
    let mut x = 0x9e37_79b9_7f4a_7c15_u64;
    let noise: Vec<u64> = (0..(1024 * 768 / 2))
        .map(|_| {
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            x
        })
        .collect();
    d.vram().write_words(0, &noise);
    modeset(&d, (1024, 768), 1024, (0, 0));
    let r = rig_full(true, false, Some(d.clone()));
    let mut c = r.client();
    c.send(r#"{"jsonrpc":"2.0","id":1,"method":"snapshot"}"#);
    // not reading: once the answer is queued its write blocks on the full socket buffer; meanwhile the mode changes
    // ten times, two windows apart (about a second, longer than the snapshot takes), so display_changed lines are
    // queued before and behind the answer
    for k in 0..10u16 {
        modeset(&d, (640 + k, 480), 1024, (0, 0));
        std::thread::sleep(Duration::from_millis(2 * DISPLAY_CHANGED_EVERY.as_millis() as u64));
    }
    let (mut answer, mut changes) = (None, 0);
    while answer.is_none() || changes == 0 {
        let mut line = String::new();
        assert!(c.lines.read_line(&mut line).unwrap() > 0, "disconnected after {changes} display_changed");
        let v: Value = serde_json::from_str(&line).unwrap();
        if v["id"] == 1 {
            assert!(line.len() > OUTPUT_CAP, "{} bytes", line.len());
            answer = Some(v);
        } else {
            assert_eq!(v["method"], "display_changed", "{v}");
            if v["params"]["width"] == 649 {
                changes += 1;
            }
        }
    }
    assert_eq!(c.status(2)["state"], "running", "still connected");
}

/// `display_changed` follows the published mode: once per change when changes are rare, and at most one per
/// DISPLAY_CHANGED_EVERY while the guest changes the mode quickly, the last always the state after the last change.
#[test]
fn display_changed_is_coalesced() {
    let d = display(16);
    let r = rig_full(true, false, Some(d.clone()));
    let mut c = r.client();
    assert_eq!(c.status(1)["state"], "running", "connected before the first change");
    let next = |c: &mut Client| -> (Value, Instant) {
        let v = c.recv();
        assert_eq!(v["method"], "display_changed", "{v}");
        (v["params"].clone(), Instant::now())
    };
    // one change: one notification, promptly
    modeset(&d, (640, 480), 800, (5, 16));
    let (p, _) = next(&mut c);
    assert_eq!(p, json!({"enabled": true, "width": 640, "height": 480, "stride": 3200, "offset": 51220, "format": "x8r8g8b8"}));
    // a storm: 400 mode changes (each modeset is two: unpublished, then published) over about 400 ms
    std::thread::sleep(Duration::from_millis(100));
    let start = Instant::now();
    for k in 0..400u16 {
        modeset(&d, (640 + k % 7, 480), 1024, (0, 0));
        std::thread::sleep(Duration::from_millis(1));
    }
    let storm = start.elapsed();
    let last = d.mode().unwrap();
    // read everything until 300 ms pass with nothing
    c.stream.set_read_timeout(Some(Duration::from_millis(300))).unwrap();
    let mut got = Vec::new();
    loop {
        let mut line = String::new();
        match c.lines.read_line(&mut line) {
            Ok(n) if n > 0 => {
                let v: Value = serde_json::from_str(&line).unwrap();
                got.push((v["params"].clone(), Instant::now()));
            }
            _ => break,
        }
    }
    c.stream.set_read_timeout(Some(PATIENCE)).unwrap();
    let most = (storm.as_millis() / DISPLAY_CHANGED_EVERY.as_millis()) as usize + 3;
    assert!(!got.is_empty() && got.len() <= most, "{} notifications in {storm:?} (at most {most})", got.len());
    assert_eq!(
        got.last().unwrap().0,
        json!({"enabled": true, "width": last.width, "height": 480, "stride": 4096, "offset": 0, "format": "x8r8g8b8"}),
        "the last is the state after the storm"
    );
    // disabled: enabled false
    dispi(&d, ENABLE, 0);
    let (p, _) = next(&mut c);
    assert_eq!(p, json!({"enabled": false, "width": 0, "height": 0, "stride": 0, "offset": 0}));
    // a second client gets them too
    let mut other = r.client();
    assert_eq!(other.status(2)["state"], "running");
    modeset(&d, (800, 600), 800, (0, 0));
    for client in [&mut c, &mut other] {
        let v = client.recv();
        assert_eq!((v["method"].as_str(), v["params"]["width"].as_u64()), (Some("display_changed"), Some(800)), "{v}");
    }
}
