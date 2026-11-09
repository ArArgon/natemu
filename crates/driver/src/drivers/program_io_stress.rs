//! Repeated production copy windows against live split atomics and genuine HLT promises.
use natemu::{
    Result,
    execution::{
        self, Backing, Stops,
        io::{CopyPermit, Transfer, WindowStatus},
    },
    vm::Machine,
};
use std::{
    sync::atomic::{AtomicBool, Ordering::SeqCst},
    time::{Duration, Instant},
};
pub(super) const BASE: u64 = 0x200000;
const PTE: u64 = 0x503000;
const FUNCTION: u64 = 0x110000;
const CONTROL: u64 = BASE + 0x800;
const A: u64 = 0x1122334455667788;
const B: u64 = 0x8877665544332211;
const SENTINEL: u64 = 0xcafe1234feed5678;
fn word(b: &[u8], i: usize) -> u64 {
    u64::from_le_bytes(b[i..i + 8].try_into().unwrap())
}
fn need(ok: bool, message: &str) -> Result<()> {
    if ok { Ok(()) } else { Err(message.into()) }
}
struct Run<'a> {
    b: &'a Backing,
    m: &'a Machine,
    s: &'a Stops,
    ended: &'a AtomicBool,
    n: usize,
    began: Instant,
    windows: u64,
    active_reads: u64,
    seed: u64,
}
impl Run<'_> {
    fn live(&self) -> Result<()> {
        need(
            self.began.elapsed() < Duration::from_secs(105)
                && !self.ended.load(SeqCst)
                && !(0..self.n).any(|v| self.s.ended(v)),
            "I/O stress deadline/early participant end",
        )
    }
    fn copy(&mut self, t: &mut [Transfer<'_>]) -> Result<()> {
        self.live()?;
        self.b
            .copy_guest(self.m, self.s, Duration::from_secs(4), t)?;
        self.windows += 1;
        Ok(())
    }
    fn until(&self, mut predicate: impl FnMut() -> bool) -> Result<()> {
        let t = Instant::now();
        while !predicate() {
            self.live()?;
            need(
                t.elapsed() < Duration::from_secs(5),
                "I/O stress stage timeout",
            )?;
            std::thread::yield_now();
        }
        Ok(())
    }
    fn quiet(&self) -> Result<WindowStatus> {
        self.until(|| {
            let s = self.b.io_window_status();
            s.quiescent.iter().all(|q| *q == 1)
                && s.request == s.released_after
                && (0..self.n).all(|v| execution::idle(execution::park_state(self.b.memory(), v)))
        })?;
        Ok(self.b.io_window_status())
    }
    fn wake(&self, v: usize) -> Result<()> {
        need(
            execution::post(self.b.memory(), v, 0x40, false),
            "stress IRQ refused",
        )?;
        need(self.s.kick(self.m, v)?, "stress IRQ target ended")
    }
    fn wake_all(&self) -> Result<()> {
        for v in 0..self.n {
            self.wake(v)?;
        }
        Ok(())
    }
    fn snapshot(&mut self) -> Result<Vec<u8>> {
        let mut b = vec![0; 0x820];
        self.copy(&mut [Transfer::Read {
            gpa: BASE,
            bytes: &mut b,
        }])?;
        need(
            matches!(word(&b, 0x43c), 0 | u64::MAX),
            "torn cross16 split-atomic snapshot",
        )?;
        need(
            word(&b, 0x818) == SENTINEL,
            "canceled write changed sentinel",
        )?;
        Ok(b)
    }
    fn verify(&self, b: &[u8], generation: u64) -> Result<()> {
        for v in 0..self.n {
            let o = v * 64;
            need(
                word(b, o) == generation,
                "per-cycle guest progress mismatch",
            )?;
            need(
                word(b, o + 8) == if generation & 1 == 0 { A } else { B },
                "stale global PTE data",
            )?;
            need(word(b, o + 16) == generation + 1, "stale compiled code")?;
            if generation > 0 {
                need(word(b, o + 32) > 0, "CPU performed no split operations")?;
            }
        }
        Ok(())
    }
    fn jitter(&mut self) {
        self.seed ^= self.seed << 13;
        self.seed ^= self.seed >> 7;
        self.seed ^= self.seed << 17;
        for _ in 0..(self.seed & 31) {
            std::thread::yield_now();
        }
    }
}

pub(super) fn run(
    b: &Backing,
    m: &Machine,
    s: &Stops,
    started: &[AtomicBool],
    ended: &AtomicBool,
    cycles: u64,
    seed: u64,
    forced: bool,
) -> Result<String> {
    let mut r = Run {
        b,
        m,
        s,
        ended,
        n: started.len(),
        began: Instant::now(),
        windows: 0,
        active_reads: 0,
        seed,
    };
    r.until(|| started.iter().all(|s| s.load(SeqCst)))?;
    r.quiet()?;
    let warm = r.snapshot()?;
    r.verify(&warm, 0)?;
    #[cfg(not(feature = "io-test-hooks"))]
    if forced {
        return Err("forced I/O stress requires io-test-hooks driver and resident".into());
    }
    #[cfg(feature = "io-test-hooks")]
    let forced_report = if forced {
        forced::run(&mut r)?
    } else {
        String::from("null")
    };
    #[cfg(not(feature = "io-test-hooks"))]
    let forced_report = String::from("null");
    // Forced prelude has extra IRQ-driven operations. Rebaseline only once all real
    // pending IRQs have been consumed and every CPU has reentered idle HLT.
    r.quiet()?;
    let baseline = r.snapshot()?;
    let split_baseline = word(&baseline, 0x43c);
    let counter_baseline = word(&baseline, 0x47c);
    if !forced {
        need(
            split_baseline == 0 && counter_baseline == 0,
            "normal split baseline was not zero",
        )?;
    }
    let mut counter_final = counter_baseline;
    let mut split_final = split_baseline;
    let mut split_operations = 0u64;
    let mut mixed_reads = 0u64;
    let mut digest = 0u64;
    #[allow(unused_mut)]
    let mut cancellations = Vec::<String>::new();
    for generation in 1..=cycles {
        r.quiet()?;
        r.jitter();
        // A prior table/code epoch must survive a later write, read-only grant and cancellation.
        let pte = (if generation & 1 == 0 {
            0x1000000u64
        } else {
            0x1010000u64
        } | 0x103)
            .to_le_bytes();
        let opposite = (if generation & 1 == 0 {
            0x1010000u64
        } else {
            0x1000000u64
        } | 0x103)
            .to_le_bytes();
        let code = (generation as u32 + 1).to_le_bytes();
        let genbytes = generation.to_le_bytes();
        r.copy(&mut [Transfer::Write {
            gpa: PTE,
            bytes: &opposite,
        }])?;
        r.copy(&mut [
            Transfer::Write {
                gpa: PTE,
                bytes: &pte,
            },
            Transfer::Write {
                gpa: FUNCTION + 1,
                bytes: &code,
            },
            Transfer::Write {
                gpa: CONTROL,
                bytes: &genbytes,
            },
        ])?;
        let mut observed = [0; 8];
        r.copy(&mut [Transfer::Read {
            gpa: PTE,
            bytes: &mut observed,
        }])?;
        need(observed == pte, "PTE readback mismatch")?;
        let before = b.io_window_status();
        let permit = CopyPermit::new();
        need(permit.cancel(), "pre-cancel failed")?;
        let canceled = b.copy_guest_with_permit(
            m,
            s,
            Duration::from_secs(4),
            &mut [Transfer::Write {
                gpa: CONTROL + 24,
                bytes: &0u64.to_le_bytes(),
            }],
            &permit,
        );
        need(canceled.is_err(), "pre-cancel granted")?;
        let after = b.io_window_status();
        need(
            after.request == before.request
                && after.write_windows == before.write_windows
                && after.tables_written == before.tables_written,
            "pre-cancel changed epochs",
        )?;
        #[cfg(feature = "io-test-hooks")]
        if forced {
            cancellations.push(forced::cancel_epoch(&mut r)?);
            let _ = r.snapshot()?;
        }
        r.wake(0)?;
        let mixed_began = Instant::now();
        loop {
            let snapshot = r.snapshot()?;
            if word(&snapshot, 0) == generation {
                need(
                    word(&snapshot, 8) == if generation & 1 == 0 { A } else { B }
                        && word(&snapshot, 16) == generation + 1
                        && word(&snapshot, 32) > 0,
                    "mixed active CPU data/code/progress",
                )?;
                need(
                    (1..r.n).all(|v| {
                        word(&snapshot, v * 64) == generation - 1
                            && execution::idle(execution::park_state(b.memory(), v))
                    }),
                    "mixed peers did not remain in HLT",
                )?;
                need(
                    b.io_window_status().quiescent[0] == 0,
                    "mixed active CPU became quiet",
                )?;
                mixed_reads += 1;
                break;
            }
            need(
                mixed_began.elapsed() < Duration::from_secs(5),
                "mixed progress deadline",
            )?;
        }
        for v in 1..r.n {
            r.wake(v)?;
        }
        // No guest can leave its atomic loop until the host publishes this generation at CONTROL+8.
        let stage = Instant::now();
        loop {
            let snapshot = r.snapshot()?;
            if (0..r.n).all(|v| word(&snapshot, v * 64) == generation) {
                r.verify(&snapshot, generation)?;
                need(
                    b.io_window_status().quiescent.iter().all(|q| *q == 0),
                    "active split snapshot was quiet",
                )?;
                r.active_reads += 1;
                break;
            }
            need(
                stage.elapsed() < Duration::from_secs(5),
                "active split progress deadline",
            )?;
            r.jitter();
        }
        r.jitter();
        r.copy(&mut [Transfer::Write {
            gpa: CONTROL + 8,
            bytes: &genbytes,
        }])?;
        r.quiet()?;
        let snapshot = r.snapshot()?;
        r.verify(&snapshot, generation)?;
        let operations = (0..r.n)
            .try_fold(0u64, |sum, v| sum.checked_add(word(&snapshot, v * 64 + 32)))
            .ok_or("per-cycle split operation count overflow")?;
        let expected = split_final ^ if operations & 1 == 0 { 0 } else { u64::MAX };
        need(
            word(&snapshot, 0x43c) == expected,
            "lost split-atomic updates at quiet boundary",
        )?;
        split_final = expected;
        split_operations = split_operations
            .checked_add(operations)
            .ok_or("cumulative split operation count overflow")?;
        counter_final = word(&snapshot, 0x47c);
        need(
            counter_final
                == counter_baseline
                    .checked_add(split_operations)
                    .ok_or("expected shared counter overflow")?,
            "lost split-atomic counter updates at quiet boundary",
        )?;
        eprintln!(
            "io-stress-cycle: generation={generation} operations={operations} split={split_final:#x} counter={counter_final}"
        );
        digest = digest.rotate_left(7) ^ generation;
        for v in 0..r.n {
            digest ^= word(&snapshot, v * 64 + 32).rotate_left(v as u32);
        }
        if generation % 64 == 0 {
            eprintln!(
                "io-stress-progress: cycles={generation} windows={} active_reads={}",
                r.windows, r.active_reads
            );
        }
    }
    r.copy(&mut [Transfer::Write {
        gpa: CONTROL,
        bytes: &u64::MAX.to_le_bytes(),
    }])?;
    r.wake_all()?;
    let end = Instant::now();
    while !(0..r.n).all(|v| s.ended(v)) {
        need(
            end.elapsed() < Duration::from_secs(5),
            "stress end deadline",
        )?;
        std::thread::yield_now();
    }
    let status = b.io_window_status();
    need(
        status.quiescent.iter().all(|q| *q == 0),
        "quiet flag left set at end",
    )?;
    let canceled_epochs = cancellations.len();
    need(
        status.request == r.windows + canceled_epochs as u64
            && status.request == status.request_after
            && status.request == status.released
            && status.request == status.released_after
            && status.acknowledged.iter().all(|a| *a == status.request),
        "final request/grant/cancel accounting or resident ACK mismatch",
    )?;
    let seconds = r.began.elapsed().as_secs_f64();
    Ok(format!(
        "{{\"cycles\":{cycles},\"seed\":{seed},\"cpus\":{},\"instrumented\":{},\"forced\":{},\"windows\":{},\"canceled_epochs\":{canceled_epochs},\"active_reads\":{},\"mixed_reads\":{mixed_reads},\"seconds\":{seconds},\"cycles_per_second\":{},\"digest\":{digest},\"split_baseline\":{split_baseline},\"split_final\":{split_final},\"split_operations\":{split_operations},\"counter_baseline\":{counter_baseline},\"counter_final\":{counter_final},\"forced_evidence\":{forced_report},\"announced_cancellations\":[{}]}}",
        r.n,
        cfg!(feature = "io-test-hooks"),
        forced,
        r.windows,
        r.active_reads,
        cycles as f64 / seconds,
        cancellations.join(",")
    ))
}

#[cfg(feature = "io-test-hooks")]
#[path = "program_io_stress_forced.rs"]
mod forced;
