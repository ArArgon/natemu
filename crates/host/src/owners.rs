//! Owner execution: N vCPUs in one VM, each on its own scoped thread.
//!
//! The caller owns the VM and its memory. This module owns the start barrier
//! (no native entry until every owner prepared), supervision, cancellation,
//! join-before-read and vCPU-destroy-before-return ordering ([`supervise`]). What
//! an owner runs is the caller's: the execution service (`execution::run`).
use super::*;
use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread,
    time::Duration,
};

/// Returned after every owner has joined and destroyed its vCPU. A setup
/// failure before the start barrier is an `Err` from [`supervise`] instead.
pub struct Supervision<T> {
    /// Each owner's result, by id (`None` when it failed; `error` holds the first failure).
    pub outputs: Vec<Option<T>>,
    pub error: Option<String>,
    pub destroyed: Vec<(usize, i32)>,
}

/// Cancels every owner: each stops at its next host exit, and the supervisor forces running vCPUs out.
pub fn cancel_owners(cancelled: &AtomicBool) {
    cancelled.store(true, Ordering::Release);
}

/// Runs `n` owners in one VM (the caller owns it and its memory), each on its own
/// scoped thread: `prepare(id)` creates and readies the owner's vCPU; once every
/// owner has prepared (start barrier: no native entry before), `body(id, vcpu,
/// cancelled)` runs it. A body error cancels every owner (as [`cancel_owners`],
/// which a body may also call itself); running vCPUs are then forced out
/// repeatedly until every body returned. Every vCPU is destroyed on its own thread
/// before this returns, so the caller may destroy the VM next.
/// `cancelled` is the run's flag: a caller's [`cancel_owners`] on it from any thread (before or during the run) cancels it.
pub fn supervise<T: Send>(
    n: usize,
    prepare: &(dyn Fn(usize) -> Result<Vcpu> + Sync),
    body: &(dyn Fn(usize, &Vcpu, &AtomicBool) -> Result<T> + Sync),
    cancelled: &AtomicBool,
) -> Result<Supervision<T>> {
    thread::scope(|scope| {
        let (prepared_tx, prepared_rx) = mpsc::channel();
        let (done_tx, done_rx) = mpsc::channel();
        let (destroyed_tx, destroyed_rx) = mpsc::channel();
        let mut starts = Vec::new();
        let mut releases = Vec::new();
        let mut owners = Vec::new();
        for id in 0..n {
            let (start_tx, start_rx) = mpsc::channel::<()>();
            let (release_tx, release_rx) = mpsc::channel::<()>();
            let prepared = prepared_tx.clone();
            let done = done_tx.clone();
            let destroyed = destroyed_tx.clone();
            let owner = thread::Builder::new()
                .name(format!("vcpu-{id}"))
                .spawn_scoped(scope, move || -> Result<T> {
                    let cpu = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| prepare(id)))
                        .unwrap_or_else(|_| Err("owner panicked during setup".into()));
                    prepared
                        .send(cpu.as_ref().map(Vcpu::id).map_err(Clone::clone))
                        .map_err(|_| "setup receiver closed")?;
                    let mut cpu = cpu?;
                    // No native entry until *all* owners successfully prepare.
                    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                        start_rx.recv().map_err(|_| "owner startup cancelled")?;
                        body(id, &cpu, cancelled)
                    }))
                    .unwrap_or_else(|_| Err("owner panicked during execution".into()));
                    let _ = done.send(result.as_ref().map(|_| ()).map_err(Clone::clone));
                    // Keep IDs alive while the supervisor may cancel peers.
                    let _ = release_rx.recv();
                    let destroy_result = cpu.destroy();
                    let _ = destroyed.send((id, destroy_result));
                    let output = result?;
                    hv(destroy_result, "destroy owner vCPU")?;
                    Ok(output)
                })
                .map_err(|e| e.to_string());
            // A setup failure drops `starts`: unstarted owners exit and the
            // scope joins them before this returns.
            owners.push(owner?);
            starts.push(start_tx);
            releases.push(release_tx);
        }
        drop(prepared_tx);
        drop(done_tx);
        drop(destroyed_tx);
        let mut ids = Vec::new();
        for _ in 0..n {
            ids.push(
                prepared_rx
                    .recv()
                    .map_err(|_| "owner setup disconnected")??,
            );
        }
        let mut error = None;
        for start in starts {
            if start.send(()).is_err() {
                error.get_or_insert("owner disappeared before entry".into());
            }
        }
        let mut completed = 0;
        while completed < n {
            match done_rx.recv_timeout(Duration::from_millis(20)) {
                Ok(result) => {
                    completed += 1;
                    if let Err(e) = result {
                        error.get_or_insert(e);
                        cancel_owners(cancelled);
                    }
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    error.get_or_insert("owner completion disconnected".into());
                    break;
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {}
            }
            if (error.is_some() || cancelled.load(Ordering::Acquire)) && completed < n {
                cancel_owners(cancelled);
                // Repeat cancellation: an owner may still be in its declared
                // pre-entry delay when an earlier cancellation is issued.
                exit_vcpus(&mut ids);
            }
        }
        drop(releases);
        let mut outputs = Vec::new();
        for (id, h) in owners.into_iter().enumerate() {
            outputs.push(match h.join() {
                Ok(Ok(output)) => Some(output),
                Ok(Err(e)) => {
                    eprintln!("owner {id}: {e}");
                    error.get_or_insert(e);
                    None
                }
                Err(_) => {
                    error.get_or_insert("owner panicked".into());
                    None
                }
            });
        }
        Ok(Supervision { outputs, error, destroyed: destroyed_rx.into_iter().collect() })
    })
}
