//! Block request timeline for the performance log (`--perf-log`): with the log off every hook is one load. With it on,
//! the block owner's end writes `blk {..}`: counts and summed nanoseconds (`perf::now`) of each step of a request, from
//! the guest's QueueNotify to its InterruptACK. Shared by every virtio device of the process; only the block device
//! uses the owner, worker and completion parts.
use crate::perf;
use std::sync::atomic::{AtomicU64, Ordering::Relaxed};

/// Counters, then the [`NAMES`] order.
pub const NOTIFIES: usize = 0;
/// QueueNotify to the admission that consumed it (owner wake-up and whatever it was doing).
pub const NOTIFY_NS: usize = 1;
pub const ADMISSIONS: usize = 2;
pub const ADMITTED: usize = 3;
/// An admission's capture, gather, decode and submission.
pub const ADMIT_NS: usize = 4;
pub const JOBS: usize = 5;
/// Submission to a worker taking the job, and the job's native calls.
pub const QUEUE_NS: usize = 6;
pub const CALL_NS: usize = 7;
/// A job's end to the owner taking its result.
pub const TAKE_NS: usize = 8;
pub const COMPLETIONS: usize = 9;
pub const COMPLETED: usize = 10;
/// A batch completion: copies, publication, interrupt.
pub const COMPLETE_NS: usize = 11;
/// Interrupts raised from a clear status; InterruptACK writes; raise to the next ACK.
pub const RAISED: usize = 12;
pub const ACKS: usize = 13;
pub const ACK_NS: usize = 14;
pub const STATUS_READS: usize = 15;
/// The owner's waits on its bell (count, ns); its thread CPU; the workers' thread CPU.
pub const WAITS: usize = 16;
pub const WAIT_NS: usize = 17;
pub const OWNER_CPU_NS: usize = 18;
pub const WORKER_CPU_NS: usize = 19;
/// Submission to the owner taking the result (queue + call + take).
pub const SUBMIT_TAKE_NS: usize = 20;
/// Lone requests whose call the owner made itself (no worker).
pub const INLINE: usize = 21;
const N: usize = 22;
const NAMES: [&str; N] = [
    "notifies",
    "notify_ns",
    "admissions",
    "admitted",
    "admit_ns",
    "jobs",
    "queue_ns",
    "call_ns",
    "take_ns",
    "completions",
    "completed",
    "complete_ns",
    "raised",
    "acks",
    "ack_ns",
    "status_reads",
    "waits",
    "wait_ns",
    "owner_cpu_ns",
    "worker_cpu_ns",
    "submit_take_ns",
    "inline",
];
static C: [AtomicU64; N] = [const { AtomicU64::new(0) }; N];
/// Per job slot: its submission and its end; the oldest unconsumed notification and interrupt raise.
const SLOTS: usize = 64;
static SUBMIT: [AtomicU64; SLOTS] = [const { AtomicU64::new(0) }; SLOTS];
static DONE: [AtomicU64; SLOTS] = [const { AtomicU64::new(0) }; SLOTS];
static NOTIFY_AT: AtomicU64 = AtomicU64::new(0);
static RAISE_AT: AtomicU64 = AtomicU64::new(0);

#[inline]
pub fn on() -> bool {
    perf::on()
}
/// Now, or 0 with the log off.
#[inline]
pub fn start() -> u64 {
    if on() { perf::now() } else { 0 }
}
#[inline]
pub fn count(i: usize) {
    if on() {
        C[i].fetch_add(1, Relaxed);
    }
}
#[inline]
pub fn add(i: usize, n: u64) {
    if on() {
        C[i].fetch_add(n, Relaxed);
    }
}
/// Adds the time since `since` (from [`start`]) to counter `i`.
#[inline]
pub fn since(i: usize, since: u64) {
    if on() && since != 0 {
        C[i].fetch_add(perf::now() - since, Relaxed);
    }
}
pub fn notified() {
    if on() {
        C[NOTIFIES].fetch_add(1, Relaxed);
        let _ = NOTIFY_AT.compare_exchange(0, perf::now(), Relaxed, Relaxed);
    }
}
/// An admission that consumed a notification starts now.
pub fn consumed() {
    if on() {
        let t = NOTIFY_AT.swap(0, Relaxed);
        if t != 0 {
            C[NOTIFY_NS].fetch_add(perf::now().saturating_sub(t), Relaxed);
        }
    }
}
pub fn raised(fresh: bool) {
    if on() && fresh {
        C[RAISED].fetch_add(1, Relaxed);
        let _ = RAISE_AT.compare_exchange(0, perf::now(), Relaxed, Relaxed);
    }
}
pub fn acked() {
    if on() {
        C[ACKS].fetch_add(1, Relaxed);
        let t = RAISE_AT.swap(0, Relaxed);
        if t != 0 {
            C[ACK_NS].fetch_add(perf::now().saturating_sub(t), Relaxed);
        }
    }
}
pub fn submitted(slot: usize) {
    if on() && slot < SLOTS {
        SUBMIT[slot].store(perf::now(), Relaxed);
    }
}
/// A worker took the job in `slot`; returns the call's start.
pub fn taken_by_worker(slot: usize) -> u64 {
    let t = start();
    if t != 0 && slot < SLOTS {
        C[JOBS].fetch_add(1, Relaxed);
        C[QUEUE_NS].fetch_add(t.saturating_sub(SUBMIT[slot].load(Relaxed)), Relaxed);
    }
    t
}
pub fn called(slot: usize, began: u64) {
    if on() && began != 0 && slot < SLOTS {
        let t = perf::now();
        C[CALL_NS].fetch_add(t - began, Relaxed);
        DONE[slot].store(t, Relaxed);
    }
}
/// The owner took the result of the job in `slot`.
pub fn result_taken(slot: usize) {
    if on() && slot < SLOTS {
        let t = perf::now();
        C[TAKE_NS].fetch_add(t.saturating_sub(DONE[slot].load(Relaxed)), Relaxed);
        C[SUBMIT_TAKE_NS].fetch_add(t.saturating_sub(SUBMIT[slot].load(Relaxed)), Relaxed);
    }
}
/// The block owner's end: its thread CPU and the line.
pub fn report() {
    if on() {
        C[OWNER_CPU_NS].fetch_add(perf::thread_cpu(), Relaxed);
        let fields: Vec<String> = NAMES
            .iter()
            .zip(&C)
            .map(|(k, v)| format!("\"{k}\":{}", v.load(Relaxed)))
            .collect();
        perf::line(&format!("blk {{{}}}", fields.join(",")));
    }
}
