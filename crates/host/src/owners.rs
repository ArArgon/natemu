//! Owner execution: N vCPUs in one VM, each on its own scoped thread.
//!
//! The caller owns the VM and its memory. This module owns the start barrier
//! (no native entry until every owner prepared), supervision, cancellation,
//! join-before-read and vCPU-destroy-before-return ordering ([`supervise`]). What
//! an owner runs is the caller's: the execution service (`execution::run`), or the
//! regression families' resident compiler-service gateway ([`Owners`], with a
//! caller-supplied [`OwnerService`]).
use super::*;
use natemu_abi::translation as wire;
use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread,
    time::{Duration, Instant},
};

/// One owner's handler for the resident compiler-service gateway (HVC #0x4a).
pub(crate) trait OwnerService {
    /// `packet` is x0..x15 at the gateway; the reply is written back to x0..x15.
    fn exchange(&mut self, packet: wire::Packet) -> Result<wire::Packet>;
    /// Requests this owner submitted, collected after a successful run.
    fn requests(&mut self) -> Vec<wire::Packet>;
}
pub(crate) type Service = Box<dyn OwnerService>;

/// Configured owners: owner `id` enters at window `memory + id * RAM_SIZE`.
pub(crate) struct Owners {
    pub n: usize,
    pub tcr: u64,
    /// Host address of guest RAM window 0.
    pub memory: usize,
    /// One owner's declared pre-entry delay after the start barrier.
    pub delayed: Option<(usize, Duration)>,
    pub deadline: Duration,
}

/// Returned after every owner has joined and destroyed its vCPU. A setup
/// failure before the start barrier is an `Err` from [`supervise`] instead.
pub(crate) struct Supervision<T> {
    /// Each owner's result, by id (`None` when it failed; `error` holds the first failure).
    pub outputs: Vec<Option<T>>,
    pub error: Option<String>,
    pub completed: usize,
    pub destroyed: Vec<(usize, i32)>,
    pub cancellations: Vec<i32>,
    pub began: Instant,
}

/// An owner vCPU and its resident service loop.
struct Cpu<'a>(&'a Vcpu);
impl Cpu<'_> {
    fn run(
        &self,
        shared_address: usize,
        service: &mut Option<Service>,
        cancelled: &AtomicBool,
    ) -> Result<()> {
        loop {
            if cancelled.load(Ordering::Acquire) {
                return Err("owner cancelled after peer failure".into());
            }
            let event = self.0.run_to_hvc()?;
            let pc = self.0.reg(PC)?;
            match event {
                0x49 => {
                    let s = unsafe { ptr::read_volatile(shared_address as *const Shared) };
                    return terminal_outcome(s.status, s.detail);
                }
                0x4a => {
                    // Only this owner writes its private record, and is
                    // stopped at the explicit register-transfer gateway.
                    let s = unsafe { ptr::read_volatile(shared_address as *const Shared) };
                    if !resident::contains(s.service_pc, 4) || pc != s.service_pc {
                        return Err("compiler service outside explicit gateway".into());
                    }
                    let mut packet = [0; 16];
                    for (r, v) in packet.iter_mut().enumerate() {
                        *v = self.0.reg(r as u32)?;
                    }
                    let reply = service
                        .as_mut()
                        .ok_or("compiler service on non-owner")?
                        .exchange(packet)?;
                    for (r, v) in reply.iter().enumerate() {
                        self.0.set_reg(r as u32, *v)?;
                    }
                }
                _ => return Err("unexpected owner HVC".into()),
            }
        }
    }
}

// Every concurrent fixture reports expected guest exceptions in its own record
// and terminates with SUCCESS. Resident rejection/fault is never a guest outcome.
fn terminal_outcome(status: u64, detail: u64) -> Result<()> {
    match status {
        SUCCESS => Ok(()),
        REJECTED => Err(format!("resident owner rejected detail={detail}")),
        NATIVE_FAULT => Err(format!("resident owner native fault detail={detail}")),
        PANIC => Err(format!("resident owner panic detail={detail}")),
        _ => Err(format!(
            "resident owner invalid terminal status={status} detail={detail}"
        )),
    }
}

/// Cancels every owner: each stops at its next host exit, and the supervisor forces running vCPUs out.
pub(crate) fn cancel_owners(cancelled: &AtomicBool) {
    if !cancelled.swap(true, Ordering::AcqRel) {
        crate::translation::process_owner::begin_cancellation();
    }
}

impl Owners {
    /// The regression families' owners: each enters at its window and serves the
    /// resident compiler-service gateway. `service(id)` runs on owner `id`'s thread
    /// after the start barrier (and its delay); `None` means the owner has no gateway
    /// service. Returns every owner's submitted requests (see [`supervise`]).
    pub(crate) fn run(
        &self,
        service: &(dyn Fn(usize) -> Result<Option<Service>> + Sync),
        poll: &mut dyn FnMut() -> Option<String>,
        owner_failed: &dyn Fn(&str),
    ) -> Result<Supervision<Vec<wire::Packet>>> {
        let Self { n, tcr, memory, delayed, deadline } = *self;
        supervise(
            n,
            deadline,
            &|id| {
                let cpu = Vcpu::create()?;
                cpu.enter_resident(id * RAM_SIZE, tcr)?;
                Ok(cpu)
            },
            &|id, cpu, cancelled| {
                if let Some((_, delay)) = delayed.filter(|d| d.0 == id) {
                    thread::sleep(delay);
                }
                let mut owned = service(id)?;
                Cpu(cpu).run(memory + id * RAM_SIZE + SHARED_OFFSET, &mut owned, cancelled)?;
                Ok(owned.map_or_else(Vec::new, |mut s| s.requests()))
            },
            poll,
            owner_failed,
        )
    }
}

/// Runs `n` owners in one VM (the caller owns it and its memory), each on its own
/// scoped thread: `prepare(id)` creates and readies the owner's vCPU; once every
/// owner has prepared (start barrier: no native entry before), `body(id, vcpu,
/// cancelled)` runs it. A body error cancels every owner (as [`cancel_owners`],
/// which a body may also call itself); running vCPUs are then forced out
/// repeatedly until every body returned. `poll` runs about every 20 ms and returns
/// an external failure cause; `owner_failed` sees each owner error before
/// cancellation. Every vCPU is destroyed on its own thread before this returns, so
/// the caller may destroy the VM next.
pub(crate) fn supervise<T: Send>(
    n: usize,
    deadline: Duration,
    prepare: &(dyn Fn(usize) -> Result<Vcpu> + Sync),
    body: &(dyn Fn(usize, &Vcpu, &AtomicBool) -> Result<T> + Sync),
    poll: &mut dyn FnMut() -> Option<String>,
    owner_failed: &dyn Fn(&str),
) -> Result<Supervision<T>> {
    let cancelled = AtomicBool::new(false);
    thread::scope(|scope| {
        let (prepared_tx, prepared_rx) = mpsc::channel();
        let (done_tx, done_rx) = mpsc::channel();
        let (destroyed_tx, destroyed_rx) = mpsc::channel();
        let mut starts = Vec::new();
        let mut releases = Vec::new();
        let mut owners = Vec::new();
        for id in 0..n {
            let cancelled = &cancelled;
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
        let began = Instant::now();
        let mut completed = 0;
        let mut cancellations = Vec::new();
        while completed < n {
            let completion = done_rx.recv_timeout(Duration::from_millis(20));
            if let Some(cause) = poll() {
                error.get_or_insert(cause);
            }
            match completion {
                Ok(result) => {
                    completed += 1;
                    if let Err(e) = result {
                        owner_failed(&e);
                        error.get_or_insert(e);
                        cancel_owners(&cancelled);
                    }
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    error.get_or_insert("owner completion disconnected".into());
                    break;
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {}
            }
            if began.elapsed() > deadline {
                error.get_or_insert("concurrent execution exceeded its deadline".into());
            }
            if (error.is_some() || cancelled.load(Ordering::Acquire)) && completed < n {
                cancel_owners(&cancelled);
                // Repeat cancellation: an owner may still be in its declared
                // pre-entry delay when an earlier cancellation is issued.
                cancellations.push(exit_vcpus(&mut ids));
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
        Ok(Supervision {
            outputs,
            error,
            completed,
            destroyed: destroyed_rx.into_iter().collect(),
            cancellations,
            began,
        })
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn concurrent_guest_exceptions_do_not_authorize_runtime_failures() {
        assert!(terminal_outcome(SUCCESS, 0).is_ok());
        for status in [0, REJECTED, NATIVE_FAULT, PANIC, u64::MAX] {
            assert!(terminal_outcome(status, 321).unwrap_err().contains("321"));
        }
        assert!(
            terminal_outcome(REJECTED, 7)
                .unwrap_err()
                .contains("rejected")
        );
        assert!(
            terminal_outcome(NATIVE_FAULT, 7)
                .unwrap_err()
                .contains("native fault")
        );
    }
}
