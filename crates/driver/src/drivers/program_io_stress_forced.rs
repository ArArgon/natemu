//! Forced observations qualify scheduling, not weak-memory barrier necessity (latches are SeqCst).
use super::*;
use natemu::execution::io_test::{
    AFTER_ACK, AFTER_GRANT, AFTER_WITHDRAW, BEFORE_GRANT, BEFORE_QUIET, BEFORE_SPLIT, HITS, HOST,
    Hooks, LATE_SHADOWS, MAGIC, REACHED, READY, TIMEOUT,
};
// Drop before scope's implicit joins, including panic unwind: blocked copy/CPU hooks release.
struct Abort<'a, 'b>(&'a Hooks<'b>);
impl Drop for Abort<'_, '_> {
    fn drop(&mut self) {
        self.0.abort();
    }
}
fn reached(r: &Run<'_>, h: &Hooks<'_>, v: usize, tag: u64) -> Result<()> {
    r.until(|| h.get(v, REACHED) == tag)?;
    need(h.get(v, TIMEOUT) == 0, "hook timed out")
}
fn pending(r: &Run<'_>, before: &WindowStatus, missing: usize) -> Result<WindowStatus> {
    let s = r.b.io_window_status();
    need(
        s.request == before.request + 1
            && s.request_after == s.request
            && s.released == before.released
            && s.released_after < s.request
            && s.write_windows == before.write_windows
            && s.tables_written == before.tables_written
            && s.acknowledged[missing] < s.request,
        "forced pending epoch/ACK/effects observation failed",
    )?;
    Ok(s)
}
fn clean(h: &Hooks<'_>, n: usize) -> Result<()> {
    for v in (0..n).chain([HOST]) {
        need(h.get(v, TIMEOUT) == 0, "forced hook barrier timeout")?;
    }
    Ok(())
}
fn event(name: &str, s: &WindowStatus, pending: bool) -> String {
    let row = format!(
        "{{\"stage\":\"{name}\",\"request\":{},\"released\":{},\"released_after\":{},\"write_windows\":{},\"tables_written\":{},\"ack\":{:?},\"quiet\":{:?},\"copy_pending\":{pending}}}",
        s.request,
        s.released,
        s.released_after,
        s.write_windows,
        s.tables_written,
        s.acknowledged,
        s.quiescent
    );
    eprintln!("io-stress-stage: {row}");
    row
}

pub(super) fn run(r: &mut Run<'_>) -> Result<String> {
    let h = Hooks::new(r.b);
    for v in 0..r.n {
        need(
            h.get(v, READY) == MAGIC,
            "forced stress resident lacks io-test-hooks",
        )?;
    }
    let mut rows = Vec::new();
    // A real peer begins its split operation AFTER CPU0's exact ACK. No host-made SHADOWS.
    need(
        h.holding(0) == 1,
        "late-shadow target has no live shadow holding",
    )?;
    let split = h.arm(1, BEFORE_SPLIT)?;
    r.wake(1)?;
    reached(r, &h, 1, split)?;
    let withdraw = h.arm(0, AFTER_WITHDRAW)?;
    r.wake(0)?;
    reached(r, &h, 0, withdraw)?;
    let baseline = r.b.io_window_status();
    let late = h.get(0, LATE_SHADOWS);
    let resets = h.resets(0);
    std::thread::scope(|scope| -> Result<()> {
        let _abort_before_join = Abort(&h);
        let b = r.b;
        let m = r.m;
        let s = r.s;
        let worker = scope.spawn(move || {
            let mut bytes = [0; 8];
            b.copy_guest(
                m,
                s,
                Duration::from_secs(4),
                &mut [Transfer::Read {
                    gpa: BASE + 0x43c,
                    bytes: &mut bytes,
                }],
            )?;
            Ok::<_, String>(bytes)
        });
        let result: Result<()> = (|| {
            r.until(|| r.b.io_window_status().request > baseline.request)?;
            let ack = h.arm(0, AFTER_ACK)?;
            h.release(0, withdraw);
            reached(r, &h, 0, ack)?;
            let observed = pending(r, &baseline, 1)?;
            need(
                observed.acknowledged[0] == observed.request && !worker.is_finished(),
                "late-shadow ACK is not exact/pending",
            )?;
            rows.push(event("late-shadow-exact-ack", &observed, true));
            h.release(1, split);
            r.until(|| h.shadows(0))?;
            need(
                r.b.io_window_status().acknowledged[1] < observed.request && !worker.is_finished(),
                "split peer acknowledged before shadow service",
            )?;
            rows.push(event("real-peer-shadows", &r.b.io_window_status(), true));
            h.release(0, ack);
            r.until(|| h.get(0, LATE_SHADOWS) > late && h.resets(0) > resets)?;
            Ok(())
        })();
        if let Err(primary) = result {
            h.abort();
            let copy_error = match worker.join() {
                Ok(Ok(_)) => None,
                Ok(Err(error)) => Some(error),
                Err(_) => Some(String::from("forced copy worker panicked")),
            };
            let copy_error_json = copy_error.as_ref().map_or_else(
                || String::from("null"),
                |error| {
                    let body: String = error
                        .chars()
                        .map(|c| match c {
                            '\"' => String::from("\\\""),
                            '\\' => String::from("\\\\"),
                            c if c < ' ' => format!("\\u{:04x}", c as u32),
                            c => c.to_string(),
                        })
                        .collect();
                    format!("\"{body}\"")
                },
            );
            let status = event("late-shadow-failure", &r.b.io_window_status(), false);
            let timeouts: Vec<_> = (0..r.n).chain([HOST]).map(|v| h.get(v, TIMEOUT)).collect();
            let last = rows.last().map(String::as_str).unwrap_or("null");
            eprintln!(
                "io-stress-late-failure: {{\"last_stage\":{last},\"status\":{status},\"late_before\":{late},\"late_after\":{},\"resets_before\":{resets},\"resets_after\":{},\"hook_timeouts\":{timeouts:?},\"copy_error\":{copy_error_json}}}",
                h.get(0, LATE_SHADOWS),
                h.resets(0)
            );
            return Err(primary);
        }
        let bytes = worker.join().map_err(|_| "forced copy worker panicked")??;
        need(
            matches!(u64::from_le_bytes(bytes), 0 | u64::MAX),
            "forced split snapshot torn",
        )?;
        Ok(())
    })?;
    r.windows += 1;
    r.quiet()?;
    clean(&h, r.n)?;
    rows.push(format!("{{\"stage\":\"late-shadow-served\",\"late_before\":{late},\"late_after\":{},\"resets_before\":{resets},\"resets_after\":{}}}",h.get(0,LATE_SHADOWS),h.resets(0)));
    // Publication precedes quiet entry; the actual kick is still pending at the resident hold.
    for (name, point) in [
        ("request-before-quiet", BEFORE_QUIET),
        ("withdraw-before-request", AFTER_WITHDRAW),
    ] {
        let tag = h.arm(0, point)?;
        r.wake(0)?;
        reached(r, &h, 0, tag)?;
        let baseline = r.b.io_window_status();
        need(
            baseline.quiescent[0] == 0,
            "held resident unexpectedly quiet",
        )?;
        std::thread::scope(|scope| -> Result<()> {
            let _abort_before_join = Abort(&h);
            let b = r.b;
            let m = r.m;
            let s = r.s;
            let worker = scope.spawn(move || {
                let mut bytes = [0; 8];
                b.copy_guest(
                    m,
                    s,
                    Duration::from_secs(4),
                    &mut [Transfer::Read {
                        gpa: CONTROL + 24,
                        bytes: &mut bytes,
                    }],
                )?;
                Ok::<_, String>(bytes)
            });
            let result: Result<()> = (|| {
                r.until(|| {
                    r.b.io_window_status().request > baseline.request
                        && r.s.ringing(r.m, 0).ok() == Some(Some(true))
                })?;
                let observation = pending(r, &baseline, 0)?;
                need(
                    !worker.is_finished(),
                    "copy completed while resident unacknowledged",
                )?;
                rows.push(event(name, &observation, true));
                h.release(0, tag);
                Ok(())
            })();
            if result.is_err() {
                h.abort();
            }
            result?;
            need(
                u64::from_le_bytes(worker.join().map_err(|_| "forced worker panicked")??)
                    == SENTINEL,
                "forced read sentinel",
            )?;
            Ok(())
        })?;
        r.windows += 1;
        r.quiet()?;
        clean(&h, r.n)?;
    }
    // Host has observed quiet at admission, then CPU0 withdraws before the final grant.
    let grant = h.arm(HOST, BEFORE_GRANT)?;
    let baseline = r.b.io_window_status();
    std::thread::scope(|scope| -> Result<()> {
        let _abort_before_join = Abort(&h);
        let b = r.b;
        let m = r.m;
        let s = r.s;
        let worker = scope.spawn(move || {
            let mut bytes = [0; 8];
            b.copy_guest(
                m,
                s,
                Duration::from_secs(4),
                &mut [Transfer::Read {
                    gpa: CONTROL + 24,
                    bytes: &mut bytes,
                }],
            )?;
            Ok::<_, String>(bytes)
        });
        let result: Result<()> = (|| {
            reached(r, &h, HOST, grant)?;
            let observed = pending(r, &baseline, 0)?;
            need(
                observed.quiescent[0] == 1 && !worker.is_finished(),
                "host did not observe quiet before grant",
            )?;
            rows.push(event("request-before-withdraw", &observed, true));
            let tag = h.arm(0, AFTER_WITHDRAW)?;
            r.wake(0)?;
            reached(r, &h, 0, tag)?;
            let withdrawn = pending(r, &baseline, 0)?;
            need(
                withdrawn.quiescent[0] == 0,
                "withdraw hook did not clear quiet",
            )?;
            rows.push(event("quiet-withdrawn-before-grant", &withdrawn, true));
            let ack = h.arm(0, AFTER_ACK)?;
            h.release(0, tag);
            reached(r, &h, 0, ack)?;
            let observed = r.b.io_window_status();
            need(
                observed.acknowledged[0] == observed.request && !worker.is_finished(),
                "withdrawal did not checkpoint before pending grant",
            )?;
            rows.push(event("withdrawal-checkpoint", &observed, true));
            h.release(HOST, grant);
            r.until(|| worker.is_finished())?;
            h.release(0, ack);
            Ok(())
        })();
        if result.is_err() {
            h.abort();
        }
        result?;
        need(
            u64::from_le_bytes(worker.join().map_err(|_| "forced worker panicked")??) == SENTINEL,
            "withdrawal read sentinel",
        )?;
        Ok(())
    })?;
    r.windows += 1;
    r.quiet()?;
    clean(&h, r.n)?;
    // After the CAS the permit cannot cancel; one exact copy completes before guest wake.
    let grant = h.arm(HOST, AFTER_GRANT)?;
    let permit = CopyPermit::new();
    let baseline = r.b.io_window_status();
    std::thread::scope(|scope| -> Result<()> {
        let _abort_before_join = Abort(&h);
        let b = r.b;
        let m = r.m;
        let s = r.s;
        let p = &permit;
        let worker = scope.spawn(move || {
            b.copy_guest_with_permit(
                m,
                s,
                Duration::from_secs(4),
                &mut [Transfer::Write {
                    gpa: CONTROL + 16,
                    bytes: &0x12345678u64.to_le_bytes(),
                }],
                p,
            )
        });
        let result: Result<()> = (|| {
            reached(r, &h, HOST, grant)?;
            need(
                !permit.cancel() && !worker.is_finished(),
                "granted permit unexpectedly canceled",
            )?;
            let obs = r.b.io_window_status();
            need(
                obs.request == baseline.request + 1
                    && obs.released_after == baseline.released
                    && obs.write_windows == baseline.write_windows,
                "post-CAS hold already effected",
            )?;
            rows.push(event("cancel-lost-after-grant", &obs, true));
            h.release(HOST, grant);
            Ok(())
        })();
        if result.is_err() {
            h.abort();
        }
        result?;
        worker.join().map_err(|_| "forced worker panicked")??;
        Ok(())
    })?;
    r.windows += 1;
    let got = r.snapshot()?;
    need(
        word(&got, 0x810) == 0x12345678,
        "granted write did not take exact effect",
    )?;
    clean(&h, r.n)?;
    let hits: Vec<_> = (0..r.n).chain([HOST]).map(|v| h.get(v, HITS)).collect();
    let timeouts: Vec<_> = (0..r.n).chain([HOST]).map(|v| h.get(v, TIMEOUT)).collect();
    rows.push(format!(
        "{{\"stage\":\"hook-counts\",\"hits\":{hits:?},\"timeouts\":{timeouts:?}}}"
    ));
    Ok(format!("[{}]", rows.join(",")))
}

/// Announced cancellation after each cycle's table/code writes and read-only successor.
pub(super) fn cancel_epoch(r: &mut Run<'_>) -> Result<String> {
    let h = Hooks::new(r.b);
    let tag = h.arm(HOST, BEFORE_GRANT)?;
    let before = r.b.io_window_status();
    let permit = CopyPermit::new();
    let mut announced = String::new();
    std::thread::scope(|scope| -> Result<()> {
        let _abort_before_join = Abort(&h);
        let b = r.b;
        let m = r.m;
        let s = r.s;
        let p = &permit;
        let worker = scope.spawn(move || {
            b.copy_guest_with_permit(
                m,
                s,
                Duration::from_secs(4),
                &mut [Transfer::Write {
                    gpa: CONTROL + 24,
                    bytes: &0u64.to_le_bytes(),
                }],
                p,
            )
        });
        let result: Result<()> = (|| {
            reached(r, &h, HOST, tag)?;
            let observed = r.b.io_window_status();
            need(
                observed.request == before.request + 1
                    && observed.request_after == observed.request
                    && observed.released_after == before.released
                    && observed.write_windows == before.write_windows
                    && observed.tables_written == before.tables_written
                    && !worker.is_finished(),
                "announced pre-CAS cancellation observation",
            )?;
            announced = event("announced-cancel-pending", &observed, true);
            need(permit.cancel(), "pre-CAS cancel lost")?;
            h.release(HOST, tag);
            Ok(())
        })();
        if result.is_err() {
            h.abort();
        }
        result?;
        let result = worker.join().map_err(|_| "canceled worker panicked")?;
        need(
            result.as_ref().is_err_and(|e| e.contains("canceled")),
            "announced canceled window did not reject",
        )?;
        Ok(())
    })?;
    let after = r.b.io_window_status();
    need(
        after.request == before.request + 1
            && after.released == after.request
            && after.released_after == after.request
            && after.write_windows == before.write_windows
            && after.tables_written == before.tables_written,
        "announced cancellation changed effects or failed release",
    )?;
    clean(&h, r.n)?;
    let completed = event("announced-canceled", &after, false);
    Ok(format!(
        "{{\"pending\":{announced},\"completed\":{completed},\"cancel_won\":true}}"
    ))
}
