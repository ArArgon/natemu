//! Posted real FEX request with response consumption held; no compiler answer or RAM is forged.
use natemu::{
    Result,
    execution::{
        Backing, Stops,
        io::{CopyPermit, Transfer, WindowStatus},
    },
    vm::Machine,
};
use serde_json::{Value, json};
use std::{
    sync::{
        Arc, Condvar, Mutex,
        atomic::{AtomicBool, Ordering::SeqCst},
    },
    time::{Duration, Instant},
};

pub(super) const BASE: u64 = 0x200000;
const FUNCTION: u64 = 0x110000;
const TARGETS: [u64; 2] = [0x120000, 0x130000];
#[derive(Default)]
struct State {
    armed: Option<usize>,
    posted: [bool; 2],
    released: [bool; 2],
    aborted: bool,
    events: Vec<Value>,
}
pub(super) struct Control {
    state: Mutex<State>,
    wake: Condvar,
    start: Instant,
}
impl Control {
    pub(super) fn new() -> Arc<Self> {
        Arc::new(Self {
            state: Mutex::new(State::default()),
            wake: Condvar::new(),
            start: Instant::now(),
        })
    }
    /// Appends `{"event": event, "ns": .., ..fields}` (the first 128 events).
    fn record(&self, s: &mut State, event: &str, fields: Value) {
        if s.events.len() < 128 {
            let mut entry = json!({"event": event, "ns": self.start.elapsed().as_nanos() as u64});
            if let (Some(e), Value::Object(f)) = (entry.as_object_mut(), fields) {
                e.extend(f);
            }
            s.events.push(entry);
        }
    }
    fn event(&self, event: &str, fields: Value) {
        self.record(
            &mut self.state.lock().unwrap_or_else(|e| e.into_inner()),
            event,
            fields,
        );
    }
    pub(super) fn json(&self) -> Value {
        let s = self.state.lock().unwrap_or_else(|e| e.into_inner());
        json!({"posted": s.posted, "events": s.events})
    }
    fn arm(&self, stage: usize) {
        let mut s = self.state.lock().unwrap_or_else(|e| e.into_inner());
        s.armed = Some(stage);
        self.record(&mut s, "armed", json!({"stage": stage}));
    }
    fn release(&self, stage: usize) {
        let mut s = self.state.lock().unwrap_or_else(|e| e.into_inner());
        s.released[stage] = true;
        self.record(&mut s, "released-consumer", json!({"stage": stage}));
        self.wake.notify_all();
    }
    pub(super) fn abort(&self) {
        let mut s = self.state.lock().unwrap_or_else(|e| e.into_inner());
        s.aborted = true;
        self.wake.notify_all();
    }
    pub(super) fn posted(&self, count: u64, rip: u64, pid: u32) -> std::io::Result<()> {
        let Some(stage) = TARGETS.iter().position(|&target| target == rip) else {
            return Ok(());
        };
        let mut s = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if s.posted[stage] && s.released[stage] {
            self.record(
                &mut s,
                "posted-after-release",
                json!({"stage": stage, "rip": rip, "count": count, "pid": pid}),
            );
            return Ok(());
        }
        if s.armed != Some(stage) || s.posted[stage] {
            return Err(std::io::Error::other(
                "pending compiler fixture unexpected target request",
            ));
        }
        s.posted[stage] = true;
        self.record(
            &mut s,
            "posted-held",
            json!({"stage": stage, "rip": rip, "count": count, "pid": pid}),
        );
        self.wake.notify_all();
        let (mut s, _) = self
            .wake
            .wait_timeout_while(s, Duration::from_secs(5), |s| {
                !s.released[stage] && !s.aborted
            })
            .unwrap_or_else(|e| e.into_inner());
        if s.aborted || !s.released[stage] {
            return Err(std::io::Error::new(
                std::io::ErrorKind::TimedOut,
                "pending compiler observer aborted or timed out",
            ));
        }
        self.record(&mut s, "resume-consumption", json!({"stage": stage}));
        Ok(())
    }
    fn wait_posted(&self, stage: usize) -> Result<()> {
        let s = self.state.lock().unwrap_or_else(|e| e.into_inner());
        let (s, _) = self
            .wake
            .wait_timeout_while(s, Duration::from_secs(5), |s| {
                !s.posted[stage] && !s.aborted
            })
            .unwrap_or_else(|e| e.into_inner());
        if s.posted[stage] && !s.aborted {
            Ok(())
        } else {
            Err("target did not post a real compiler request".into())
        }
    }
}
fn status(s: &WindowStatus) -> Value {
    json!({
        "request": s.request,
        "released": s.released,
        "write_windows": s.write_windows,
        "acknowledged": s.acknowledged,
        "released_after": s.released_after,
        "request_after": s.request_after,
    })
}
struct Abort<'a>(&'a Control);
impl Drop for Abort<'_> {
    fn drop(&mut self) {
        self.0.abort();
    }
}

pub(super) fn run(
    control: &Control,
    backing: &Backing,
    machine: &Machine,
    stops: &Stops,
    started: &[AtomicBool],
    ended: &AtomicBool,
) -> Result<Value> {
    use Transfer::{Read, Write};
    let _abort = Abort(control);
    let start = Instant::now();
    let live = || -> Result<()> {
        if ended.load(SeqCst) || stops.ended(0) || stops.ended(1) {
            Err("pending compiler participant ended".into())
        } else if start.elapsed() > Duration::from_secs(45) {
            Err("pending compiler fixture deadline".into())
        } else {
            Ok(())
        }
    };
    while !started.iter().all(|s| s.load(SeqCst)) {
        live()?;
        std::thread::sleep(Duration::from_millis(1));
    }
    let copy = |transfers: &mut [Transfer<'_>]| -> Result<()> {
        live()?;
        backing.copy_guest(machine, stops, Duration::from_secs(2), transfers)
    };
    let word = |bytes: &[u8], at| u64::from_le_bytes(bytes[at..at + 8].try_into().unwrap());
    let snapshot = |bytes: &mut [u8; 0x240]| -> Result<()> {
        copy(&mut [Read { gpa: BASE, bytes }])?;
        if !matches!(
            word(bytes, 0x3c),
            0x5555_5555_5555_5555 | 0xaaaa_aaaa_aaaa_aaaa
        ) {
            return Err("torn split atomic snapshot".into());
        }
        Ok(())
    };
    let mut bytes = [0; 0x240];
    loop {
        snapshot(&mut bytes)?;
        if (0..2).all(|v| word(&bytes, 0x100 + 8 * v) != 0 && word(&bytes, 0x200 + 8 * v) == 1) {
            break;
        }
        std::thread::sleep(Duration::from_millis(1));
    }
    let before = [word(&bytes, 0x100), word(&bytes, 0x108)];
    for stage in 0..2 {
        control.arm(stage);
        copy(&mut [Write {
            gpa: BASE,
            bytes: &[(stage + 1) as u8],
        }])?;
        control.wait_posted(stage)?;
        let prior = backing.io_window_status();
        let permit = CopyPermit::new();
        let timeout = Duration::from_millis(if stage == 0 { 400 } else { 2000 });
        let (observed, result, marker, finished) = std::thread::scope(|scope| {
            let worker = scope.spawn(|| {
                let mut marker = [0];
                let result = backing.copy_guest_with_permit(
                    machine,
                    stops,
                    timeout,
                    &mut [
                        Read {
                            gpa: BASE + 0x21,
                            bytes: &mut marker,
                        },
                        Write {
                            gpa: BASE + 0x30,
                            bytes: &[if stage == 0 { 0x77 } else { 0x5a }],
                        },
                        Write {
                            gpa: FUNCTION + 1,
                            bytes: &2u32.to_le_bytes(),
                        },
                    ],
                    &permit,
                );
                (result, marker[0])
            });
            let pending = |s: &WindowStatus| {
                s.request == s.request_after
                    && s.request > prior.request
                    && s.released < s.request
                    && s.released_after < s.request
                    && s.write_windows == prior.write_windows
                    && s.acknowledged[0] != s.request
                    && s.acknowledged[1] == s.request
            };
            let observe = || -> Result<WindowStatus> {
                let began = Instant::now();
                while began.elapsed() < Duration::from_millis(300) {
                    live()?;
                    let s = backing.io_window_status();
                    let copy_pending = !worker.is_finished();
                    if pending(&s) && copy_pending {
                        control.event(
                            "pending-window",
                            json!({
                                "stage": stage,
                                "copy_pending": copy_pending,
                                "prior": status(&prior),
                                "status": status(&s),
                            }),
                        );
                        return Ok(s);
                    }
                    std::thread::sleep(Duration::from_millis(1));
                }
                Err("no positively observed pending epoch and peer acknowledgement".into())
            };
            let mut observed = observe();
            if observed.is_err() {
                permit.cancel();
                control.release(stage);
            } else if stage == 1 {
                std::thread::sleep(Duration::from_millis(100));
                let s = backing.io_window_status();
                let copy_pending = !worker.is_finished();
                control.event(
                    "held-recheck",
                    json!({"stage": stage, "copy_pending": copy_pending, "status": status(&s)}),
                );
                if !pending(&s)
                    || !copy_pending
                    || s.request != observed.as_ref().unwrap().request
                    || live().is_err()
                {
                    permit.cancel();
                    observed =
                        Err("copy no longer pending under the second held observation".into());
                }
                control.release(stage);
            }
            let (result, marker) = worker
                .join()
                .unwrap_or_else(|_| (Err("copy owner panicked".into()), 0));
            let finished = backing.io_window_status();
            let ack_timeout = result
                .as_ref()
                .is_err_and(|e| e == "I/O window acknowledgement timed out");
            control.event(
                "window-result",
                json!({
                    "stage": stage,
                    "ok": result.is_ok(),
                    "marker": marker,
                    "ack_timeout": ack_timeout,
                    "status": status(&finished),
                }),
            );
            if stage == 0 {
                control.release(stage);
            }
            (observed, result, marker, finished)
        });
        let observed = observed?;
        if finished.request != observed.request
            || finished.request_after != observed.request
            || finished.released != observed.request
            || finished.released_after != observed.request
        {
            return Err("window did not release its exact epoch".into());
        }
        if stage == 0 {
            if !result
                .as_ref()
                .is_err_and(|e| e == "I/O window acknowledgement timed out")
                || finished.write_windows != prior.write_windows
                || finished.acknowledged[0] == observed.request
                || finished.acknowledged[1] != observed.request
            {
                return Err(format!(
                    "timeout window unexpectedly changed disposition: {result:?}"
                ));
            }
            let (mut sentinel, mut function) = ([0], [0; 4]);
            copy(&mut [
                Read {
                    gpa: BASE + 0x30,
                    bytes: &mut sentinel,
                },
                Read {
                    gpa: FUNCTION + 1,
                    bytes: &mut function,
                },
            ])?;
            if sentinel != [0xa5] || function != 1u32.to_le_bytes() {
                return Err("timeout safe-read oracle failed".into());
            }
            control.event("timeout-safe-read", json!({"sentinel": 165, "function": 1}));
            loop {
                snapshot(&mut bytes)?;
                if bytes[0x20] == 1 {
                    break;
                }
                std::thread::sleep(Duration::from_millis(1));
            }
        } else {
            result?;
            // The pending doorbell may dispatch before this new block's first instruction.
            // Classify that clean boundary; require eventual execution after release below.
            if marker > 1
                || finished.write_windows != prior.write_windows + 1
                || finished.acknowledged != [observed.request; 2]
            {
                return Err(
                    "granted window lacks exact acknowledgements or has invalid marker".into(),
                );
            }
        }
    }
    loop {
        snapshot(&mut bytes)?;
        if bytes[0x21] == 1 && (0..2).all(|v| word(&bytes, 0x200 + 8 * v) == 2) {
            break;
        }
        std::thread::sleep(Duration::from_millis(1));
    }
    for _ in 0..64 {
        std::thread::sleep(Duration::from_millis(1));
        snapshot(&mut bytes)?;
    }
    let after = [word(&bytes, 0x100), word(&bytes, 0x108)];
    if before.iter().zip(after).any(|(b, a)| a <= *b)
        || bytes[0x30] != 0x5a
        || bytes[0x20..0x22] != [1, 1]
    {
        return Err("post-grant progress or marker mismatch".into());
    }
    copy(&mut [Write {
        gpa: BASE + 8,
        bytes: &[1],
    }])?;
    control.event(
        "complete",
        json!({
            "snapshots": 64,
            "before": before,
            "after": after,
            "code": [2, 2],
            "markers": [1, 1],
        }),
    );
    Ok(control.json())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The events, status and result against the former format strings, over the same values.
    #[test]
    fn json_matches_the_former_format() {
        let w = WindowStatus {
            request: 3,
            released: 2,
            tables_written: 9,
            acknowledged: vec![3, 2],
            quiescent: vec![0, 1],
            write_windows: 1,
            released_after: 2,
            request_after: 3,
        };
        let old_status = format!(
            "{{\"request\":{},\"released\":{},\"write_windows\":{},\"acknowledged\":{:?},\"released_after\":{},\"request_after\":{}}}",
            w.request,
            w.released,
            w.write_windows,
            w.acknowledged,
            w.released_after,
            w.request_after
        );
        assert_eq!(
            serde_json::from_str::<Value>(&old_status).unwrap(),
            status(&w)
        );
        let control = Control::new();
        let (stage, rip, count, pid, copy_pending, marker) =
            (1usize, 0x120000u64, 4u64, 77u32, true, 2u64);
        let (before, after) = ([1u64, 2], [3u64, 4]);
        let s = status(&w);
        let cases: Vec<(&str, String, Value)> = vec![
            (
                "armed",
                format!(",\"stage\":{stage}"),
                json!({"stage": stage}),
            ),
            (
                "posted-held",
                format!(",\"stage\":{stage},\"rip\":{rip},\"count\":{count},\"pid\":{pid}"),
                json!({"stage": stage, "rip": rip, "count": count, "pid": pid}),
            ),
            (
                "pending-window",
                format!(
                    ",\"stage\":{stage},\"copy_pending\":{copy_pending},\"prior\":{},\"status\":{}",
                    old_status, old_status
                ),
                json!({"stage": stage, "copy_pending": copy_pending, "prior": s, "status": s}),
            ),
            (
                "window-result",
                format!(
                    ",\"stage\":{stage},\"ok\":{},\"marker\":{marker},\"ack_timeout\":{},\"status\":{}",
                    true, false, old_status
                ),
                json!({"stage": stage, "ok": true, "marker": marker, "ack_timeout": false, "status": s}),
            ),
            (
                "timeout-safe-read",
                ",\"sentinel\":165,\"function\":1".into(),
                json!({"sentinel": 165, "function": 1}),
            ),
            (
                "complete",
                format!(
                    ",\"snapshots\":64,\"before\":{before:?},\"after\":{after:?},\"code\":[2,2],\"markers\":[1,1]"
                ),
                json!({"snapshots": 64, "before": before, "after": after, "code": [2, 2], "markers": [1, 1]}),
            ),
        ];
        let mut old_events = Vec::new();
        for (event, old_fields, fields) in cases {
            control.event(event, fields);
            let ns = control.state.lock().unwrap().events.last().unwrap()["ns"].clone();
            old_events.push(format!(
                "{{\"event\":\"{event}\",\"ns\":{}{old_fields}}}",
                ns
            ));
        }
        control.state.lock().unwrap().posted = [true, false];
        let old = format!(
            "{{\"posted\":{:?},\"events\":[{}]}}",
            [true, false],
            old_events.join(",")
        );
        assert_eq!(serde_json::from_str::<Value>(&old).unwrap(), control.json());
    }
}
