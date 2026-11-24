//! The server's tests: a real socket in a private folder, a test 8042 without a VM (`crate::ps2::testing`) and a
//! canceller no run watches. Every wait is bounded (generous bounds, for a loaded machine); none relies on a sleep for
//! correctness.
use super::protocol::StopReason;
use super::*;
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
        let r = self.recv();
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
