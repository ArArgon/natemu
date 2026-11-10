//! Finite copy-first I/O access to guest RAM. Backends retain only their own buffers. A window
//! waits for resident acknowledgements or HLT quiescence, copies prevalidated spans, and releases; it runs no
//! parser, callback or backend operation while guest memory is exclusively accessible. Device
//! copies are windowless ([`Backing::copy_guest_windowless`]): only vCPUs holding translations of
//! written pages are posted, and only they are awaited before the completion is published.
use super::*;
use cache::IoWindow;
use std::mem::offset_of;
use std::sync::atomic::{AtomicU8, AtomicU16, AtomicU32, Ordering::Acquire, fence};

/// Room for a batch of block requests of up to 128 KiB each; larger batches split.
pub const MAX_BYTES: usize = 1024 * 1024;
/// Room for a batch of 32 block requests, each of up to 32 data segments plus header, status and
/// used element, and the window's ring bookkeeping (a queue's metadata snapshot, used index and
/// available-ring read). Larger batches split.
pub const MAX_SPANS: usize = 32 * 36 + 8;
/// How long a windowless publication waits for posted vCPUs to drain before it falls back to a
/// window. A running holder drains at its next block boundary after its doorbell (microseconds).
pub const DRAIN_DEADLINE: Duration = Duration::from_millis(50);

/// One-use admission decision shared by a device reset and its I/O owner. Cancellation and grant
/// are one atomic choice: a canceled window cannot copy; a granted window must drain before reset.
#[derive(Default)]
pub struct CopyPermit {
    state: AtomicU8,
}
impl CopyPermit {
    pub fn new() -> Self {
        Self::default()
    }
    /// True if no grant can occur (including an already canceled permit); false if it already won
    /// admission. A failed or timed-out copy also consumes the permit. Never reuse it for another window.
    pub fn cancel(&self) -> bool {
        match self.state.compare_exchange(0, 2, SeqCst, SeqCst) {
            Ok(_) | Err(2) => true,
            Err(_) => false,
        }
    }
    fn canceled(&self) -> bool {
        self.state.load(SeqCst) == 2
    }
    fn grant(&self) -> bool {
        self.state.compare_exchange(0, 1, SeqCst, SeqCst).is_ok()
    }
}
struct CancelPending<'a>(&'a CopyPermit);
impl Drop for CancelPending<'_> {
    fn drop(&mut self) {
        self.0.cancel();
    }
}

/// Copies occur in order, including overlapping guest destinations. The caller owns every buffer;
/// no guest pointer escapes the window. Zero-length spans have no effect.
pub enum Transfer<'a> {
    Read { gpa: u64, bytes: &'a mut [u8] },
    Write { gpa: u64, bytes: &'a [u8] },
}
/// What a windowless copy asks of a vCPU's doorbell.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Peer {
    /// Ring it (it stops at its next block boundary and drains).
    Ring,
    /// Only ask whether its run has ended.
    Ask,
}
impl Transfer<'_> {
    fn is_empty(&self) -> bool {
        match self {
            Self::Read { bytes, .. } => bytes.is_empty(),
            Self::Write { bytes, .. } => bytes.is_empty(),
        }
    }
}

/// The shared rendezvous word at `field` (an offset in [`IoWindow`]).
fn word(memory: &Memory, field: usize) -> &AtomicU64 {
    global_u64(memory, cache::IO_WINDOW + field)
}
fn acknowledged(memory: &Memory, v: usize) -> &AtomicU64 {
    word(memory, offset_of!(IoWindow, acknowledged) + v * 8)
}
fn quiescent(memory: &Memory, v: usize) -> &AtomicU64 {
    word(memory, offset_of!(IoWindow, quiescent) + v * 8)
}

fn acknowledged_or_halted(memory: &Memory, v: usize, epoch: u64) -> bool {
    acknowledged(memory, v).load(SeqCst) == epoch || quiescent(memory, v).load(SeqCst) == 1
}

/// Whether some resident has ever walked `page` as a page table ([`cache::TABLE_PAGES`]).
fn table_page(memory: &Memory, page: u64) -> bool {
    let index = page as usize / 64;
    global_u64(memory, cache::TABLE_PAGES + index * 8).load(SeqCst) & (1 << (page % 64)) != 0
}

/// Called only inside a granted window, before any RAM mutation. All residents are parked:
/// their code-page marks cannot grow until release. Mirror the resident's written/post protocol;
/// an already-cleared mark may have an older pending invalidation, which these ORs preserve.
fn invalidate_page(memory: &Memory, page: usize, n: usize) {
    post_page(memory, page, clear_marks(memory, page), n);
    // No kick: every participant is parked and drains before resuming guest access.
}

/// Clears every vCPU's code mark for `page` at once; returns the vCPUs that had one. The common
/// unmarked case only loads the shared word.
fn clear_marks(memory: &Memory, page: usize) -> cache::CpuMask {
    let (index, shift) = cache::mask_location(page);
    let mask = global_u64(memory, cache::CODE_PAGES + index * 8);
    if cache::mask_peers(mask.load(SeqCst), shift) == 0 {
        return 0;
    }
    cache::mask_peers(
        mask.fetch_and(!(cache::CpuMask::MAX << shift), SeqCst),
        shift,
    )
}

/// Posts `page` to each vCPU in `peers` as the resident's `post` does: stale bit, then summary
/// bit, then `PAGES` (its drain relies on that order). The caller kicks, if needed, afterwards.
fn post_page(memory: &Memory, page: usize, peers: cache::CpuMask, n: usize) {
    for v in (0..n).filter(|v| peers & (1 << v) != 0) {
        record_word(memory, v, Some(page / 64)).fetch_or(1 << (page % 64), SeqCst);
        let summary = cache::RECORDS
            + v * size_of::<cache::Record>()
            + offset_of!(cache::Record, summary)
            + page / 64 / 64 * 8;
        global_u64(memory, summary).fetch_or(1 << (page / 64 % 64), SeqCst);
        record_word(memory, v, None).fetch_or(cache::PAGES, SeqCst);
    }
}

/// Whether vCPU `v` still has `page`'s stale bit, i.e. has not drained it since it was posted.
fn stale(memory: &Memory, v: usize, page: usize) -> bool {
    record_word(memory, v, Some(page / 64)).load(SeqCst) & (1 << (page % 64)) != 0
}

/// Always releases an announced epoch, including a timeout, kick failure or unwind. No guest
/// bytes change before every participant is admitted, so an aborted closing phase has no partial effect.
struct Release<'a> {
    memory: &'a Memory,
    epoch: u64,
}
impl Drop for Release<'_> {
    fn drop(&mut self) {
        // Complete host loads/stores before permitting native guest accesses again. No `nomem`:
        // this is also the compiler boundary around the raw copies.
        unsafe { std::arch::asm!("dsb sy", options(nostack, preserves_flags)) };
        word(self.memory, offset_of!(IoWindow, released)).store(self.epoch, SeqCst);
    }
}

/// Host-side window counts for the run's performance log. Page-table marks are never cleared,
/// so as the guest recycles former page-table pages for data, more write windows overlap them
/// and force every vCPU to drop its translations; `table_writes` against `writes` shows that.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, serde::Serialize)]
pub struct WindowCounts {
    /// Windows that won their grant and copied at least one byte.
    #[serde(rename = "windows")]
    pub granted: u64,
    /// Granted windows with at least one guest-RAM write.
    #[serde(rename = "write_windows")]
    pub writes: u64,
    /// Granted windows whose writes overlapped a page ever walked as a page table.
    #[serde(rename = "table_windows")]
    pub table_writes: u64,
    /// Summed microseconds from announcing granted windows to releasing them: an upper bound on
    /// the time each running vCPU spent paused by them.
    pub window_us: u64,
    /// The part of `window_us` spent waiting for acknowledgements (kicks and resident checkpoints).
    pub admit_us: u64,
    /// Summed microseconds granted windows waited for the epoch lock held by another device's window.
    pub lock_wait_us: u64,
    /// Batches copied without a window ([`Backing::copy_guest_windowless`]), including those
    /// whose publication fell back to a window after a drain deadline.
    pub windowless: u64,
    /// Windowless batches that awaited at least one vCPU before publication (it held a written
    /// page's translations, or still owed a drain of one).
    pub windowless_posted: u64,
    /// (vCPU, page) posts made by the rechecks after the body and after publication: marks taken
    /// during the copy. Those found after publication are not awaited, so they may come from a
    /// batch not counted in `windowless_posted`.
    pub recheck_holders: u64,
    /// Waits for posted vCPUs to drain before publication: count, summed and longest time.
    pub drain_waits: u64,
    pub drain_us: u64,
    pub drain_max_us: u64,
    /// Drain waits that reached their deadline: the publication then used a full window (also
    /// counted in `windows`; the batch stays counted in `windowless`).
    pub drain_expiries: u64,
    /// Windowless requests whose writes overlapped a page-table mark: the whole batch used a
    /// full window (also counted in `windows`).
    pub table_fallbacks: u64,
}
impl WindowCounts {
    /// One `io {...}` JSON line for [`crate::perf::line`].
    pub fn perf_line(&self) -> String {
        format!("io {}", serde_json::to_string(self).unwrap())
    }
}
/// [`WindowCounts`] as the copiers update them (atomic adds: windows under the epoch lock,
/// windowless batches from any device owner); readers never take that lock.
#[derive(Default)]
pub(crate) struct WindowCounters {
    granted: AtomicU64,
    writes: AtomicU64,
    table_writes: AtomicU64,
    window_ns: AtomicU64,
    admit_ns: AtomicU64,
    lock_wait_ns: AtomicU64,
    windowless: AtomicU64,
    windowless_posted: AtomicU64,
    recheck_holders: AtomicU64,
    drain_waits: AtomicU64,
    drain_ns: AtomicU64,
    drain_max_ns: AtomicU64,
    drain_expiries: AtomicU64,
    table_fallbacks: AtomicU64,
}

/// Read-only test observation, not a transactionally consistent snapshot of the words.
#[cfg(feature = "testing")]
#[derive(Debug, Clone)]
pub struct WindowStatus {
    pub request: u64,
    pub released: u64,
    pub tables_written: u64,
    pub acknowledged: Vec<u64>,
    pub quiescent: Vec<u64>,
    /// Host count of granted windows that wrote guest RAM ([`WindowCounts::writes`]).
    pub write_windows: u64,
    pub released_after: u64,
    pub request_after: u64,
}

/// One validated, nonempty transfer, resolved to host addresses before the window opens.
struct Span {
    gpa: u64,
    guest: *mut u8,
    host: *mut u8,
    len: usize,
    write: bool,
}

/// Validate the whole batch before any effect: bounded span count and bytes, every span wholly
/// inside installed guest RAM. Zero-length transfers are dropped. The mapping lives as long as
/// the Backing; guest page tables, permissions and A/D bits are not DMA routing.
fn resolve(ram: &GuestRam, transfers: &mut [Transfer<'_>]) -> Result<Vec<Span>> {
    if transfers.len() > MAX_SPANS {
        return Err("I/O window has too many spans".into());
    }
    let mut spans = Vec::with_capacity(transfers.len());
    let mut total = 0usize;
    for transfer in transfers.iter_mut() {
        let (gpa, len, host, write) = match transfer {
            Transfer::Read { gpa, bytes } => (*gpa, bytes.len(), bytes.as_mut_ptr(), false),
            Transfer::Write { gpa, bytes } => (*gpa, bytes.len(), bytes.as_ptr().cast_mut(), true),
        };
        if len == 0 {
            continue;
        }
        total = total
            .checked_add(len)
            .filter(|&n| n <= MAX_BYTES)
            .ok_or("I/O window exceeds byte bound")?;
        let guest = ram
            .range(gpa, len as u64)
            .ok_or("I/O span is not wholly installed guest RAM")?;
        // `copy_span` relies on guest-physical alignment being host alignment.
        if (guest as u64).wrapping_sub(gpa) % 4096 != 0 {
            return Err("I/O span's RAM alias is not page aligned".into());
        }
        spans.push(Span {
            gpa,
            guest,
            host,
            len,
            write,
        });
    }
    Ok(spans)
}

fn ns(d: Duration) -> u64 {
    u64::try_from(d.as_nanos()).unwrap_or(u64::MAX)
}

/// Every page a write span touches, sorted, each once. Nonempty installed-RAM spans were checked
/// for overflow in `resolve`.
fn written_pages(spans: &[Span]) -> Vec<u64> {
    let mut pages: Vec<u64> = spans
        .iter()
        .filter(|span| span.write)
        .flat_map(|span| (span.gpa >> 12)..=((span.gpa + span.len as u64 - 1) >> 12))
        .collect();
    pages.sort_unstable();
    pages.dedup();
    pages
}

/// Copies one span. A span of up to 16 bytes (a ring index, flags, a status byte, a used
/// element) moves in naturally aligned single-copy-atomic accesses, so a field the guest reads
/// or writes concurrently is never torn; longer spans (payloads, metadata tables) use `memcpy`.
///
/// # Safety
/// `span` was produced by `resolve` and its host buffer is still borrowed.
unsafe fn copy_span(span: &Span) {
    // Host buffers are borrowed for the call and cannot overlap guest backing in safe code; no
    // exclusive Rust reference is manufactured over guest memory.
    unsafe {
        if span.len > 16 {
            if span.write {
                std::ptr::copy_nonoverlapping(span.host, span.guest, span.len);
            } else {
                std::ptr::copy_nonoverlapping(span.guest, span.host, span.len);
            }
            return;
        }
        let mut at = 0;
        while at < span.len {
            // The alias is page aligned, so guest-physical alignment is host alignment.
            let gpa = span.gpa as usize + at;
            let width = [8, 4, 2, 1]
                .into_iter()
                .find(|&w| gpa % w == 0 && span.len - at >= w)
                .unwrap();
            let (guest, host) = (span.guest.add(at), span.host.add(at));
            macro_rules! moved {
                ($atomic:ty, $int:ty) => {{
                    let field = <$atomic>::from_ptr(guest.cast());
                    if span.write {
                        field.store(host.cast::<$int>().read_unaligned(), Relaxed)
                    } else {
                        host.cast::<$int>().write_unaligned(field.load(Relaxed))
                    }
                }};
            }
            match width {
                8 => moved!(AtomicU64, u64),
                4 => moved!(AtomicU32, u32),
                2 => moved!(AtomicU16, u16),
                _ => moved!(AtomicU8, u8),
            }
            at += width;
        }
    }
}

/// Copies `spans` in order. A read that follows a write is ordered after it by a full barrier
/// (the used index before the available ring's flags, virtio's `virtio_mb` pairing); every read
/// is followed by an acquire barrier, so a later span sees what the guest published before the
/// index it read (the available index before its ring entries and descriptors).
///
/// # Safety
/// As [`copy_span`], for each span.
unsafe fn copy_spans(spans: &[Span]) {
    let mut wrote = false;
    for span in spans {
        if !span.write && wrote {
            fence(SeqCst);
        }
        unsafe { copy_span(span) };
        wrote = span.write;
        if !span.write {
            fence(Acquire);
        }
    }
}

impl Backing {
    #[cfg(feature = "testing")]
    pub fn io_window_status(&self) -> WindowStatus {
        let memory = &self.memory;
        WindowStatus {
            request: word(memory, offset_of!(IoWindow, request)).load(SeqCst),
            released: word(memory, offset_of!(IoWindow, released)).load(SeqCst),
            tables_written: word(memory, offset_of!(IoWindow, tables_written)).load(SeqCst),
            acknowledged: (0..self.n)
                .map(|v| acknowledged(memory, v).load(SeqCst))
                .collect(),
            quiescent: (0..self.n)
                .map(|v| quiescent(memory, v).load(SeqCst))
                .collect(),
            write_windows: self.io_window_counts().writes,
            released_after: word(memory, offset_of!(IoWindow, released)).load(SeqCst),
            request_after: word(memory, offset_of!(IoWindow, request)).load(SeqCst),
        }
    }
    /// Windows granted so far; a window in progress may or may not be counted yet.
    pub fn io_window_counts(&self) -> WindowCounts {
        let counters = &self.io_counts;
        WindowCounts {
            granted: counters.granted.load(Relaxed),
            writes: counters.writes.load(Relaxed),
            table_writes: counters.table_writes.load(Relaxed),
            window_us: counters.window_ns.load(Relaxed) / 1000,
            admit_us: counters.admit_ns.load(Relaxed) / 1000,
            lock_wait_us: counters.lock_wait_ns.load(Relaxed) / 1000,
            windowless: counters.windowless.load(Relaxed),
            windowless_posted: counters.windowless_posted.load(Relaxed),
            recheck_holders: counters.recheck_holders.load(Relaxed),
            drain_waits: counters.drain_waits.load(Relaxed),
            drain_us: counters.drain_ns.load(Relaxed) / 1000,
            drain_max_us: counters.drain_max_ns.load(Relaxed) / 1000,
            drain_expiries: counters.drain_expiries.load(Relaxed),
            table_fallbacks: counters.table_fallbacks.load(Relaxed),
        }
    }
    /// Copy a bounded ordered batch after every configured vCPU reaches a resident safe boundary.
    /// A stopped/unstarted participant, slow compiler, or missing resident admission prevents the copy.
    /// `timeout` bounds admission polling; native kick calls and scheduling still require the VM's
    /// process supervision. Call from an independent I/O owner, never a vCPU's request
    /// service thread (that vCPU must return to its resident to acknowledge). Callers must not hold
    /// a device/VM lock needed by those service threads. The owner must keep the VM busy while a
    /// window is pending (the ordinary all-halted rule must not end it), and join before VM teardown.
    /// Return means the copies are published and residents may resume. Each resident invalidates
    /// affected code and translation currency before guest access; this call does not await that work.
    pub fn copy_guest(
        &self,
        machine: &Machine,
        stops: &Stops,
        timeout: Duration,
        transfers: &mut [Transfer<'_>],
    ) -> Result<()> {
        self.copy_guest_with_permit(machine, stops, timeout, transfers, &CopyPermit::new())
    }

    /// As `copy_guest`, with an externally cancelable one-use admission decision. Once grant wins,
    /// cancellation cannot undo the copy; the device must wait for this call before completing reset.
    pub fn copy_guest_with_permit(
        &self,
        machine: &Machine,
        stops: &Stops,
        timeout: Duration,
        transfers: &mut [Transfer<'_>],
        permit: &CopyPermit,
    ) -> Result<()> {
        self.copy_with_permit(timeout, transfers, permit, |v, acknowledged| {
            stops.kick_if_needed(machine, v, !acknowledged)
        })
    }

    #[cfg(test)]
    fn copy_with(
        &self,
        timeout: Duration,
        transfers: &mut [Transfer<'_>],
        mut kick: impl FnMut(usize) -> Result<bool>,
    ) -> Result<()> {
        self.copy_with_permit(timeout, transfers, &CopyPermit::new(), |v, acknowledged| {
            if acknowledged { Ok(true) } else { kick(v) }
        })
    }

    /// The window protocol: validate, take the epoch lock, announce an epoch, wait for every
    /// participant, grant, invalidate written pages, copy, release. `kick(v, acknowledged)` must
    /// return false once vCPU `v` has ended; it rings `v` only when not yet acknowledged.
    fn copy_with_permit(
        &self,
        timeout: Duration,
        transfers: &mut [Transfer<'_>],
        permit: &CopyPermit,
        kick: impl FnMut(usize, bool) -> Result<bool>,
    ) -> Result<()> {
        let _cancel = CancelPending(permit);
        if permit.state.load(SeqCst) != 0 {
            return Err("I/O window permit already consumed".into());
        }
        let deadline = Instant::now()
            .checked_add(timeout)
            .ok_or("I/O window timeout overflow")?;
        let spans = resolve(&self.ram, transfers)?;
        if spans.is_empty() {
            return if permit.grant() {
                Ok(())
            } else {
                Err("I/O window canceled".into())
            };
        }
        self.window(&spans, deadline, Some(permit), kick)
    }

    /// One window over resolved, nonempty `spans`. `permit` is granted after admission; `None`
    /// means the caller already holds a granted permit (a windowless batch's fallback).
    fn window(
        &self,
        spans: &[Span],
        deadline: Instant,
        permit: Option<&CopyPermit>,
        kick: impl FnMut(usize, bool) -> Result<bool>,
    ) -> Result<()> {
        let locking = Instant::now();
        let mut sequence = self.lock_epoch(permit, deadline)?;
        let announced = Instant::now();
        let epoch = sequence
            .checked_add(1)
            .ok_or("I/O window epoch exhausted")?;
        *sequence = epoch;
        let release = Release {
            memory: &self.memory,
            epoch,
        };
        word(&self.memory, offset_of!(IoWindow, request)).store(epoch, SeqCst);
        // Pair with HLT withdrawal: either we see it leave, or it sees our request before RAM access.
        unsafe { std::arch::asm!("dsb sy", options(nostack, preserves_flags)) };
        self.admit(epoch, permit, deadline, kick)?;
        let admitted = Instant::now();
        #[cfg(feature = "io-test-hooks")]
        super::io_test::point(&self.memory, cache::io_test::BEFORE_GRANT);
        if permit.is_some_and(|p| !p.grant()) {
            return Err("I/O window canceled or permit already consumed".into());
        }
        #[cfg(feature = "io-test-hooks")]
        super::io_test::point(&self.memory, cache::io_test::AFTER_GRANT);
        unsafe { std::arch::asm!("dsb sy", options(nostack, preserves_flags)) };
        let counters = &self.io_counts;
        counters.granted.fetch_add(1, Relaxed);
        if spans.iter().any(|span| span.write) {
            counters.writes.fetch_add(1, Relaxed);
            if self.invalidate_written(spans, epoch) {
                counters.table_writes.fetch_add(1, Relaxed);
            }
        }
        // SAFETY: the complete installed-RAM ranges were checked in `resolve`; all residents have
        // completed their accesses and are parked.
        unsafe { copy_spans(spans) };
        drop(release);
        counters
            .window_ns
            .fetch_add(ns(announced.elapsed()), Relaxed);
        counters
            .admit_ns
            .fetch_add(ns(admitted - announced), Relaxed);
        counters
            .lock_wait_ns
            .fetch_add(ns(announced - locking), Relaxed);
        Ok(())
    }

    /// As [`Self::copy_guest_with_permit`], without pausing vCPUs in the usual case (the
    /// "windowless copies" design in `docs/restructure/io-plan.md`). `transfers[..publish]` is the
    /// body (payload, status, used elements, metadata reads); `transfers[publish..]` is the
    /// publication (the used index, then reads of the available ring and any metadata refresh),
    /// copied only after every vCPU posted a code invalidation for a written page has drained it,
    /// is halted, or has ended. Read-only batches touch no marks. A write overlapping a
    /// page-table mark sends the whole batch through a window instead; a drain that outlasts
    /// [`DRAIN_DEADLINE`] sends the publication through one. The permit is granted before any
    /// write, so a reset either cancels the batch before its first effect or waits for this
    /// call, as for a granted window. Takes no epoch lock, except in a fallback window. Callers
    /// must hold no device/VM lock the vCPUs' service threads need. An error after the grant
    /// (a doorbell failure, or a fallback window that times out) may leave the body written
    /// without publication: the caller must treat it as fatal to the VM, never as a cancellation
    /// (check the permit: [`CopyPermit::cancel`] returns false once granted).
    pub fn copy_guest_windowless(
        &self,
        machine: &Machine,
        stops: &Stops,
        timeout: Duration,
        transfers: &mut [Transfer<'_>],
        publish: usize,
        permit: &CopyPermit,
    ) -> Result<()> {
        self.windowless(
            timeout,
            DRAIN_DEADLINE,
            transfers,
            publish,
            permit,
            |v, peer| stops.kick_if_needed(machine, v, peer == Peer::Ring),
            |_point| {
                #[cfg(feature = "io-test-hooks")]
                super::io_test::point(&self.memory, _point);
            },
        )
    }

    /// The windowless protocol. `kick(v, Peer::Ring)` rings `v`, `kick(v, Peer::Ask)` only asks;
    /// both return false once `v` has ended. `point` is called at the windowless test points.
    fn windowless(
        &self,
        timeout: Duration,
        drain: Duration,
        transfers: &mut [Transfer<'_>],
        publish: usize,
        permit: &CopyPermit,
        mut kick: impl FnMut(usize, Peer) -> Result<bool>,
        mut point: impl FnMut(u64),
    ) -> Result<()> {
        // The window protocol's `kick(v, acknowledged)`, for the fallbacks.
        fn as_window(peer: bool) -> Peer {
            if peer { Peer::Ask } else { Peer::Ring }
        }
        let _cancel = CancelPending(permit);
        if permit.state.load(SeqCst) != 0 {
            return Err("I/O copy permit already consumed".into());
        }
        let deadline = Instant::now()
            .checked_add(timeout)
            .ok_or("I/O copy timeout overflow")?;
        let body = transfers
            .get(..publish)
            .ok_or("I/O publication index out of range")?
            .iter()
            .filter(|t| !t.is_empty())
            .count();
        let spans = resolve(&self.ram, transfers)?;
        let counters = &self.io_counts;
        let pages = written_pages(&spans);
        if pages.iter().any(|&page| table_page(&self.memory, page)) {
            // A page-table shadow is not tracked for guest stores: pause everyone, as before.
            counters.table_fallbacks.fetch_add(1, Relaxed);
            return self.window(&spans, deadline, Some(permit), |v, a| kick(v, as_window(a)));
        }
        if !permit.grant() {
            return Err("I/O copy canceled".into());
        }
        counters.windowless.fetch_add(1, Relaxed);
        let mut posted = Vec::new();
        self.post_written(&pages, &mut posted, &mut kick, true)?;
        point(cache::io_test::AFTER_PRECHECK);
        let (body, publication) = spans.split_at(body);
        // SAFETY (both copies): installed-RAM ranges were checked in `resolve`. vCPUs run: the
        // device owns every written buffer and the used slots it fills; ring fields are accessed
        // whole (`copy_span`), and the barriers below order them as virtio requires.
        unsafe { copy_spans(body) };
        point(cache::io_test::BEFORE_RECHECK);
        // As the resident's `rechecked`: a vCPU that marked a page after the check above (or
        // after its mark was cleared) may have compiled torn bytes; it is posted like the rest.
        fence(SeqCst);
        let before = posted.len();
        self.post_written(&written_pages(body), &mut posted, &mut kick, false)?;
        counters
            .recheck_holders
            .fetch_add((posted.len() - before) as u64, Relaxed);
        if !posted.is_empty() {
            counters.windowless_posted.fetch_add(1, Relaxed);
            let until = deadline.min(Instant::now() + drain);
            if !self.drained(&posted, until, &mut kick)? {
                counters.drain_expiries.fetch_add(1, Relaxed);
                // Every body byte is in place; the window parks the holders still owing a
                // drain, and they drain after release before any guest access.
                return if publication.is_empty() {
                    Ok(())
                } else {
                    self.window(publication, deadline, None, |v, a| kick(v, as_window(a)))
                };
            }
        }
        point(cache::io_test::BEFORE_PUBLISH);
        // The body's stores are ordered before the used index by the full barrier after the body
        // (`dmb ish`, which is also the index store's release).
        unsafe { copy_spans(publication) };
        fence(SeqCst);
        // A mark of the used ring's page taken now executes a ring the device writes (a guest
        // race): post it, with no wait (the completion is already visible).
        let before = posted.len();
        self.post_written(&written_pages(publication), &mut posted, &mut kick, false)?;
        counters
            .recheck_holders
            .fetch_add((posted.len() - before) as u64, Relaxed);
        Ok(())
    }

    /// Clears the code marks of every page in `pages`, posts each page to its holders, then
    /// rings each posted vCPU once unless it is halted (posting before kicking, as the resident
    /// does; a halted vCPU drains when it withdraws). Appends `(vCPU, page)` per vCPU to await.
    /// `owed`: also await every vCPU that still owes a drain of the page from an earlier writer
    /// (a resident's `written`, another batch or a window cleared the mark first, so this batch
    /// finds none, but that holder may still run blocks of the old bytes).
    fn post_written(
        &self,
        pages: &[u64],
        posted: &mut Vec<(usize, usize)>,
        kick: &mut impl FnMut(usize, Peer) -> Result<bool>,
        owed: bool,
    ) -> Result<()> {
        let mut ring: cache::CpuMask = 0;
        for &page in pages {
            let page = page as usize;
            let mut peers = clear_marks(&self.memory, page);
            if peers != 0 {
                post_page(&self.memory, page, peers, self.n);
            }
            if owed {
                for v in (0..self.n).filter(|&v| stale(&self.memory, v, page)) {
                    peers |= 1 << v;
                }
            }
            posted.extend(
                (0..self.n)
                    .filter(|v| peers & (1 << v) != 0)
                    .map(|v| (v, page)),
            );
            ring |= peers;
        }
        for v in (0..self.n).filter(|v| ring & (1 << v) != 0) {
            // The post preceded this SeqCst load (see `drained`). An ended vCPU (false) runs no
            // guest code again: nothing to ring.
            if quiescent(&self.memory, v).load(SeqCst) != 1 {
                kick(v, Peer::Ring)?;
            }
        }
        Ok(())
    }

    /// Wait until every posted vCPU has drained its post, is halted (it drains when it withdraws,
    /// before guest access), or has ended. False at `until`.
    fn drained(
        &self,
        posted: &[(usize, usize)],
        until: Instant,
        kick: &mut impl FnMut(usize, Peer) -> Result<bool>,
    ) -> Result<bool> {
        let began = Instant::now();
        let mut waiting = posted.to_vec();
        let mut pass = 0u32;
        let result = loop {
            let mut i = 0;
            // Asking whether a vCPU ended takes its kick mutex: only every 64th pass.
            let ask = pass % 64 == 63;
            while i < waiting.len() {
                let (v, page) = waiting[i];
                // The post preceded these SeqCst loads, so a vCPU seen halted sees the post when
                // it withdraws (its quiescent store and drain load are SeqCst, after `dsb sy`).
                let done = !stale(&self.memory, v, page)
                    || quiescent(&self.memory, v).load(SeqCst) == 1
                    || ask && !kick(v, Peer::Ask)?;
                if done {
                    waiting.swap_remove(i);
                } else {
                    i += 1;
                }
            }
            if waiting.is_empty() {
                break true;
            }
            if Instant::now() >= until {
                break false;
            }
            // A running holder drains within microseconds of its doorbell: spin first.
            if pass < 256 {
                std::hint::spin_loop();
            } else {
                std::thread::yield_now();
            }
            pass = pass.wrapping_add(1);
        };
        let counters = &self.io_counts;
        let waited = ns(began.elapsed());
        counters.drain_waits.fetch_add(1, Relaxed);
        counters.drain_ns.fetch_add(waited, Relaxed);
        counters.drain_max_ns.fetch_max(waited, Relaxed);
        Ok(result)
    }

    /// Take the window sequence, polling so cancellation and the deadline still apply.
    fn lock_epoch(
        &self,
        permit: Option<&CopyPermit>,
        deadline: Instant,
    ) -> Result<std::sync::MutexGuard<'_, u64>> {
        loop {
            if permit.is_some_and(CopyPermit::canceled) {
                return Err("I/O window canceled".into());
            }
            match self.io_epoch.try_lock() {
                Ok(lock) => return Ok(lock),
                Err(std::sync::TryLockError::Poisoned(_)) => {
                    return Err("I/O window coordinator poisoned".into());
                }
                Err(std::sync::TryLockError::WouldBlock) if Instant::now() < deadline => {
                    std::thread::yield_now()
                }
                Err(_) => return Err("I/O window coordinator timed out".into()),
            }
        }
    }

    /// Ring every participant that has not yet acknowledged `epoch`, then wait until each has
    /// acknowledged or is halted. A resident in HLT may stay asleep: its withdrawal checkpoints
    /// before any guest access. An ended participant refuses the window.
    fn admit(
        &self,
        epoch: u64,
        permit: Option<&CopyPermit>,
        deadline: Instant,
        mut kick: impl FnMut(usize, bool) -> Result<bool>,
    ) -> Result<()> {
        for v in 0..self.n {
            if permit.is_some_and(CopyPermit::canceled) {
                return Err("I/O window canceled".into());
            }
            let ready = acknowledged_or_halted(&self.memory, v, epoch);
            if !kick(v, ready)? {
                return Err(format!("I/O window vCPU {v} has ended"));
            }
        }
        loop {
            if permit.is_some_and(CopyPermit::canceled) {
                return Err("I/O window canceled".into());
            }
            if Instant::now() >= deadline {
                return Err("I/O window acknowledgement timed out".into());
            }
            if (0..self.n).all(|v| acknowledged_or_halted(&self.memory, v, epoch)) {
                return Ok(());
            }
            std::thread::yield_now();
        }
    }

    /// Post code invalidations for every page a write span touches and, if any of them was ever
    /// walked as a page table, publish `epoch` as `tables_written`. Returns whether it did. All
    /// residents are quiescent, so table marks cannot grow until release.
    fn invalidate_written(&self, spans: &[Span], epoch: u64) -> bool {
        let mut tables = false;
        for span in spans.iter().filter(|span| span.write) {
            // Nonempty installed-RAM spans were checked for overflow in `resolve`. At most
            // MAX_BYTES/4096 + 2*MAX_SPANS page visits, including repeated pages.
            let first = span.gpa >> 12;
            let last = (span.gpa + span.len as u64 - 1) >> 12;
            for page in first..=last {
                if table_page(&self.memory, page) {
                    word(&self.memory, offset_of!(IoWindow, tables_written)).store(epoch, SeqCst);
                    tables = true;
                }
                invalidate_page(&self.memory, page as usize, self.n);
            }
        }
        tables
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    /// A windowless copy on a fixture with no vCPU holding code (nothing to post or drain).
    pub(crate) fn windowless(
        b: &Backing,
        transfers: &mut [Transfer<'_>],
        publish: usize,
    ) -> Result<()> {
        windowless_permit(b, transfers, publish, &CopyPermit::new())
    }
    /// As [`windowless`], under the caller's permit (a device reset may cancel it).
    pub(crate) fn windowless_permit(
        b: &Backing,
        transfers: &mut [Transfer<'_>],
        publish: usize,
        permit: &CopyPermit,
    ) -> Result<()> {
        b.windowless(
            BUDGET,
            BUDGET,
            transfers,
            publish,
            permit,
            |_, _| Ok(true),
            |_| {},
        )
    }
    /// A fixture of `n` vCPUs and three pages of guest RAM at guest-physical 0.
    pub(crate) fn backing(n: usize) -> Backing {
        backing_with(n, 3 * 4096)
    }
    /// A fixture of `n` vCPUs and `bytes` (a page multiple) of guest RAM at guest-physical 0.
    pub(crate) fn backing_with(n: usize, bytes: usize) -> Backing {
        let ram_at = cache::GLOBAL_OFFSET + cache::GLOBAL_BYTES;
        let memory = Arc::new(Memory::with_size(ram_at + bytes).unwrap());
        // SAFETY: this fixture owns a separate aligned RAM range after its protocol words.
        let ram = unsafe { GuestRam::new(0, bytes as u64, memory.0.add(ram_at) as u64) };
        Backing {
            memory,
            n,
            stride: 0,
            ram,
            epoch: 0,
            io_epoch: Mutex::new(0),
            io_counts: WindowCounters::default(),
        }
    }
    fn read(b: &Backing, at: usize, len: usize) -> Vec<u8> {
        // SAFETY: called only outside a running window; fixture RAM is initialized and in bounds.
        unsafe {
            std::slice::from_raw_parts(b.ram.range(at as u64, len as u64).unwrap(), len).to_vec()
        }
    }
    fn load(b: &Backing, field: usize) -> u64 {
        word(&b.memory, field).load(SeqCst)
    }
    fn acknowledge(b: &Backing, v: usize) -> Result<bool> {
        let epoch = load(b, offset_of!(IoWindow, request));
        acknowledged(&b.memory, v).store(epoch, SeqCst);
        Ok(true)
    }
    const BUDGET: Duration = Duration::from_secs(1);

    #[test]
    fn halted_participants_admit_multiple_windows_without_advancing_acknowledgements() {
        let b = backing(2);
        for v in 0..2 {
            quiescent(&b.memory, v).store(1, SeqCst);
        }
        global_u64(&b.memory, cache::TABLE_PAGES).store(1, SeqCst);
        let mut checked = 0;
        b.copy_with_permit(
            BUDGET,
            &mut [Transfer::Write {
                gpa: 0,
                bytes: &[7],
            }],
            &CopyPermit::new(),
            |_, ready| {
                assert!(ready);
                checked += 1;
                Ok(true)
            },
        )
        .unwrap();
        let mut bytes = [0];
        b.copy_with_permit(
            BUDGET,
            &mut [Transfer::Read {
                gpa: 0,
                bytes: &mut bytes,
            }],
            &CopyPermit::new(),
            |_, ready| {
                assert!(ready);
                checked += 1;
                Ok(true)
            },
        )
        .unwrap();
        assert_eq!(
            checked, 4,
            "terminal checks still run for every participant"
        );
        assert_eq!(bytes, [7]);
        assert_eq!(load(&b, offset_of!(IoWindow, released)), 2);
        assert_eq!(load(&b, offset_of!(IoWindow, tables_written)), 1);
        for v in 0..2 {
            assert_eq!(acknowledged(&b.memory, v).load(SeqCst), 0);
        }
    }

    #[test]
    fn withdrawing_halt_requires_an_acknowledgement_before_grant() {
        let b = backing(2);
        let quiet = quiescent(&b.memory, 1);
        quiet.store(1, SeqCst);
        let (posted, receive) = std::sync::mpsc::channel();
        std::thread::scope(|scope| {
            let writer = scope.spawn(|| {
                b.copy_with_permit(
                    BUDGET,
                    &mut [Transfer::Write {
                        gpa: 0,
                        bytes: &[9],
                    }],
                    &CopyPermit::new(),
                    |v, ready| {
                        if v == 0 {
                            acknowledge(&b, v)?;
                        } else {
                            assert!(ready);
                            quiet.store(0, SeqCst);
                            posted.send(()).unwrap();
                        }
                        Ok(true)
                    },
                )
            });
            receive.recv_timeout(BUDGET).unwrap();
            assert_eq!(read(&b, 0, 1), [0]);
            assert_eq!(load(&b, offset_of!(IoWindow, released)), 0);
            acknowledge(&b, 1).unwrap();
            writer.join().unwrap().unwrap();
        });
        assert_eq!(read(&b, 0, 1), [9]);
    }

    #[test]
    fn halted_but_ended_participant_cannot_admit_a_copy() {
        let b = backing(1);
        quiescent(&b.memory, 0).store(1, SeqCst);
        let error = b
            .copy_with_permit(
                BUDGET,
                &mut [Transfer::Write {
                    gpa: 0,
                    bytes: &[9],
                }],
                &CopyPermit::new(),
                |_, ready| {
                    assert!(ready);
                    Ok(false)
                },
            )
            .unwrap_err();
        assert!(error.contains("has ended"));
        assert_eq!(read(&b, 0, 1), [0]);
        assert_eq!(load(&b, offset_of!(IoWindow, released)), 1);
    }

    #[test]
    fn validates_entire_batch_before_first_effect() {
        let b = backing(2);
        let mut ops = [
            Transfer::Write {
                gpa: 4,
                bytes: &[1, 2],
            },
            Transfer::Write {
                gpa: 3 * 4096 - 1,
                bytes: &[3, 4],
            },
        ];
        assert!(
            b.copy_with(BUDGET, &mut ops, |_| panic!("invalid batch must not kick"))
                .is_err()
        );
        assert_eq!(read(&b, 4, 2), [0, 0]);
        assert_eq!(load(&b, offset_of!(IoWindow, request)), 0);
        let mut overflow = [Transfer::Read {
            gpa: u64::MAX,
            bytes: &mut [0; 2],
        }];
        assert!(
            b.copy_with(BUDGET, &mut overflow, |_| panic!("overflow must not kick"))
                .is_err()
        );
    }

    #[test]
    fn waits_for_every_peer_then_copies_aliases_in_order() {
        let b = backing(2);
        let mut result = [0; 5];
        let mut ops = [
            Transfer::Write {
                gpa: 1,
                bytes: &[1, 2, 3, 4],
            },
            Transfer::Write {
                gpa: 3,
                bytes: &[8, 9],
            },
            Transfer::Read {
                gpa: 0,
                bytes: &mut result,
            },
        ];
        b.copy_with(BUDGET, &mut ops, |v| {
            assert_eq!(
                read(&b, 0, 5),
                [0; 5],
                "no copy before all acknowledgements"
            );
            acknowledge(&b, v)
        })
        .unwrap();
        assert_eq!(result, [0, 1, 2, 8, 9]);
        assert_eq!(load(&b, offset_of!(IoWindow, released)), 1);
        assert_eq!(b.io_window_counts().writes, 1);
        let mut snapshot = [0; 5];
        b.copy_with(
            BUDGET,
            &mut [Transfer::Read {
                gpa: 0,
                bytes: &mut snapshot,
            }],
            |v| acknowledge(&b, v),
        )
        .unwrap();
        assert_eq!(snapshot, result);
        assert_eq!(load(&b, offset_of!(IoWindow, released)), 2);
        assert_eq!(
            b.io_window_counts().writes,
            1,
            "read-only release does not request code flush"
        );
    }

    #[test]
    fn dma_posts_written_pages_only_and_preserves_older_invalidations() {
        let b = backing(2);
        let marked = |page: usize| global_u64(&b.memory, cache::CODE_PAGES + page * 8);
        marked(0).store(1, SeqCst);
        marked(1).store(3, SeqCst);
        marked(2).store(2, SeqCst);
        // CPU 0 already owes a page-2 invalidation, so its mark was cleared earlier.
        record_word(&b.memory, 0, Some(0)).store(4, SeqCst);
        global_u64(
            &b.memory,
            cache::RECORDS + offset_of!(cache::Record, summary),
        )
        .store(1, SeqCst);
        record_word(&b.memory, 0, None).store(cache::PAGES, SeqCst);
        let mut snapshot = [0; 1];
        let mut ops = [
            Transfer::Write {
                gpa: 4095,
                bytes: &[7, 8],
            },
            Transfer::Read {
                gpa: 8192,
                bytes: &mut snapshot,
            },
        ];
        b.copy_with(BUDGET, &mut ops, |v| {
            assert_eq!((marked(0).load(SeqCst), marked(1).load(SeqCst)), (1, 3));
            acknowledge(&b, v)
        })
        .unwrap();
        assert_eq!(read(&b, 4095, 2), [7, 8]);
        assert_eq!(
            (
                marked(0).load(SeqCst),
                marked(1).load(SeqCst),
                marked(2).load(SeqCst)
            ),
            (0, 0, 2)
        );
        assert_eq!(record_word(&b.memory, 0, Some(0)).load(SeqCst), 7);
        assert_eq!(record_word(&b.memory, 1, Some(0)).load(SeqCst), 2);
        for v in 0..2 {
            assert_eq!(record_word(&b.memory, v, None).load(SeqCst), cache::PAGES);
            assert_eq!(
                global_u64(
                    &b.memory,
                    cache::RECORDS
                        + v * size_of::<cache::Record>()
                        + offset_of!(cache::Record, summary)
                )
                .load(SeqCst),
                1
            );
        }
    }

    #[test]
    fn table_write_epoch_requires_overlap_and_marks_survive_page_reuse() {
        let b = backing(2);
        let table_marks = global_u64(&b.memory, cache::TABLE_PAGES);
        let table_epoch = word(&b.memory, offset_of!(IoWindow, tables_written));
        b.copy_with(
            BUDGET,
            &mut [Transfer::Write {
                gpa: 0,
                bytes: &[1],
            }],
            |v| acknowledge(&b, v),
        )
        .unwrap();
        assert_eq!(table_epoch.load(SeqCst), 0);
        // The last participant publishes its mark only while entering the first table-write window.
        b.copy_with(
            BUDGET,
            &mut [Transfer::Write {
                gpa: 4095,
                bytes: &[2, 3],
            }],
            |v| {
                if v == 1 {
                    table_marks.fetch_or(2, SeqCst);
                }
                acknowledge(&b, v)
            },
        )
        .unwrap();
        assert_eq!(table_epoch.load(SeqCst), 2);
        let mut snapshot = [0];
        b.copy_with(
            BUDGET,
            &mut [Transfer::Read {
                gpa: 4096,
                bytes: &mut snapshot,
            }],
            |v| acknowledge(&b, v),
        )
        .unwrap();
        assert_eq!(table_epoch.load(SeqCst), 2);
        b.copy_with(
            BUDGET,
            &mut [Transfer::Write {
                gpa: 4096,
                bytes: &[0],
            }],
            |v| acknowledge(&b, v),
        )
        .unwrap();
        assert_eq!(table_epoch.load(SeqCst), 4);
        assert_eq!(
            table_marks.load(SeqCst),
            2,
            "never forget earlier table use"
        );
        b.copy_with(
            BUDGET,
            &mut [Transfer::Write {
                gpa: 0,
                bytes: &[0],
            }],
            |v| acknowledge(&b, v),
        )
        .unwrap();
        assert_eq!(
            table_epoch.load(SeqCst),
            4,
            "ordinary write must not advance table epoch"
        );
        let counts = b.io_window_counts();
        assert_eq!(
            (counts.granted, counts.writes, counts.table_writes),
            (5, 4, 2),
            "only page-table overlaps count"
        );
        assert!(
            counts.admit_us <= counts.window_us,
            "admission is part of the window"
        );
        let line: serde_json::Value =
            serde_json::from_str(counts.perf_line().strip_prefix("io ").unwrap()).unwrap();
        for key in [
            "windows",
            "write_windows",
            "table_windows",
            "window_us",
            "admit_us",
            "lock_wait_us",
        ] {
            assert!(line[key].is_u64(), "{key} in the perf line");
        }
    }

    #[test]
    fn canceled_grant_preserves_code_marks_and_pending_records() {
        let b = backing(1);
        global_u64(&b.memory, cache::CODE_PAGES).store(1, SeqCst);
        global_u64(&b.memory, cache::TABLE_PAGES).store(1, SeqCst);
        let permit = CopyPermit::new();
        let mut ops = [Transfer::Write {
            gpa: 0,
            bytes: &[9],
        }];
        assert!(
            b.copy_with_permit(BUDGET, &mut ops, &permit, |v, _| {
                assert!(permit.cancel());
                acknowledge(&b, v)
            })
            .is_err()
        );
        assert_eq!(read(&b, 0, 1), [0]);
        assert_eq!(global_u64(&b.memory, cache::CODE_PAGES).load(SeqCst), 1);
        assert_eq!(record_word(&b.memory, 0, None).load(SeqCst), 0);
        assert_eq!(b.io_window_counts().writes, 0);
        assert_eq!(load(&b, offset_of!(IoWindow, tables_written)), 0);
    }

    #[test]
    fn timeout_releases_and_late_ack_cannot_grant_next_epoch() {
        let b = backing(2);
        let mut ops = [Transfer::Write {
            gpa: 0,
            bytes: &[5],
        }];
        assert!(
            b.copy_with(Duration::from_millis(1), &mut ops, |v| if v == 0 {
                acknowledge(&b, v)
            } else {
                Ok(true)
            })
            .is_err()
        );
        assert_eq!(read(&b, 0, 1), [0]);
        assert_eq!(load(&b, offset_of!(IoWindow, released)), 1);
        assert_eq!(b.io_window_counts().writes, 0);
        acknowledge(&b, 1).unwrap(); // late arrival after cancellation
        assert!(
            b.copy_with(Duration::from_millis(1), &mut ops, |_| Ok(true))
                .is_err()
        );
        assert_eq!(
            read(&b, 0, 1),
            [0],
            "old acknowledgements cannot authorize new copies"
        );
        assert_eq!(load(&b, offset_of!(IoWindow, released)), 2);
        b.copy_with(BUDGET, &mut ops, |v| acknowledge(&b, v))
            .unwrap();
        assert_eq!(read(&b, 0, 1), [5]);
        assert_eq!(b.io_window_counts().writes, 1);
        let mut snapshot = [0];
        b.copy_with(
            BUDGET,
            &mut [Transfer::Read {
                gpa: 0,
                bytes: &mut snapshot,
            }],
            |v| acknowledge(&b, v),
        )
        .unwrap();
        assert_eq!(snapshot, [5]);
        assert_eq!(b.io_window_counts().writes, 1);
        assert_eq!(load(&b, offset_of!(IoWindow, released)), 4);
    }

    #[test]
    fn already_acknowledged_peer_needs_no_doorbell() {
        let b = backing(4);
        for epoch in 1..=2 {
            let mut kicked = Vec::new();
            b.copy_with(
                BUDGET,
                &mut [Transfer::Write {
                    gpa: 0,
                    bytes: &[epoch],
                }],
                |v| {
                    kicked.push(v);
                    acknowledge(&b, v)?;
                    if v == 0 {
                        // These peers reached a checkpoint without this window's doorbell.
                        acknowledge(&b, 1)?;
                        acknowledge(&b, 2)?;
                    }
                    Ok(true)
                },
            )
            .unwrap();
            assert_eq!(
                kicked,
                [0, 3],
                "only this epoch's acknowledgement can suppress a kick"
            );
            assert_eq!(read(&b, 0, 1), [epoch]);
        }
    }

    #[test]
    fn acknowledged_but_ended_peer_prevents_copy() {
        let b = backing(2);
        let mut seen = Vec::new();
        let result = b.copy_with_permit(
            BUDGET,
            &mut [Transfer::Write {
                gpa: 0,
                bytes: &[9],
            }],
            &CopyPermit::new(),
            |v, acknowledged| {
                seen.push((v, acknowledged));
                if v == 0 {
                    acknowledge(&b, 0)?;
                    acknowledge(&b, 1)?;
                }
                Ok(v == 0)
            },
        );
        assert!(result.is_err());
        assert_eq!(seen, [(0, false), (1, true)]);
        assert_eq!(read(&b, 0, 1), [0]);
        assert_eq!(load(&b, offset_of!(IoWindow, released)), 1);
        assert_eq!(b.io_window_counts().writes, 0);
    }

    #[test]
    fn kick_failure_and_unwind_release_without_writing() {
        let b = backing(2);
        let mut ops = [Transfer::Write {
            gpa: 0,
            bytes: &[5],
        }];
        assert!(
            b.copy_with(BUDGET, &mut ops, |v| if v == 0 {
                acknowledge(&b, v)
            } else {
                Err("kick failed".into())
            })
            .is_err()
        );
        assert_eq!(load(&b, offset_of!(IoWindow, released)), 1);
        let panic = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            b.copy_with(BUDGET, &mut ops, |_| panic!("injected kick panic"))
        }));
        assert!(panic.is_err());
        assert_eq!(load(&b, offset_of!(IoWindow, released)), 2);
        assert_eq!(read(&b, 0, 1), [0]);
        assert!(
            b.copy_with(BUDGET, &mut ops, |_| panic!("poison must prevent new kick"))
                .is_err()
        );
    }

    #[test]
    fn stopped_participant_and_busy_owner_never_grant() {
        let b = backing(2);
        let mut ops = [Transfer::Write {
            gpa: 0,
            bytes: &[5],
        }];
        assert!(
            b.copy_with(BUDGET, &mut ops, |v| if v == 0 {
                acknowledge(&b, v)
            } else {
                Ok(false)
            })
            .is_err()
        );
        assert_eq!(read(&b, 0, 1), [0]);
        assert_eq!(load(&b, offset_of!(IoWindow, released)), 1);
        let _owner = b.io_epoch.lock().unwrap();
        assert!(
            b.copy_with(Duration::from_millis(1), &mut ops, |_| panic!(
                "busy owner must not kick"
            ))
            .is_err()
        );
        assert_eq!(load(&b, offset_of!(IoWindow, request)), 1);
        assert_eq!(load(&b, offset_of!(IoWindow, released)), 1);
        assert_eq!(read(&b, 0, 1), [0]);
    }

    #[test]
    fn next_write_waits_for_a_peer_still_finishing_previous_release() {
        let b = backing(2);
        b.copy_with(
            BUDGET,
            &mut [Transfer::Write {
                gpa: 0,
                bytes: &[3],
            }],
            |v| acknowledge(&b, v),
        )
        .unwrap();
        // The first release need not wait for peer 1's code flush. Its old acknowledgement must
        // not grant a second write while it is still finishing that flush.
        let (kicked, receive) = std::sync::mpsc::channel();
        std::thread::scope(|scope| {
            let second = scope.spawn(|| {
                b.copy_with(
                    Duration::from_secs(5),
                    &mut [Transfer::Write {
                        gpa: 0,
                        bytes: &[7],
                    }],
                    |v| {
                        if v == 0 {
                            acknowledge(&b, v)?;
                        } else {
                            kicked.send(()).unwrap();
                        }
                        Ok(true)
                    },
                )
            });
            receive.recv_timeout(BUDGET).unwrap();
            assert_eq!(load(&b, offset_of!(IoWindow, request)), 2);
            assert_eq!(read(&b, 0, 1), [3]);
            assert_eq!(load(&b, offset_of!(IoWindow, released)), 1);
            acknowledge(&b, 1).unwrap(); // peer finished its flush and entered the next checkpoint
            second.join().unwrap().unwrap();
        });
        assert_eq!(read(&b, 0, 1), [7]);
        assert_eq!(b.io_window_counts().writes, 2);
    }

    #[test]
    fn bounds_and_epoch_exhaustion_have_no_effect() {
        let b = backing(1);
        let bytes = vec![5; MAX_BYTES + 1];
        assert!(
            b.copy_with(
                BUDGET,
                &mut [Transfer::Write {
                    gpa: 0,
                    bytes: &bytes
                }],
                |_| panic!("oversize")
            )
            .is_err()
        );
        let mut many: Vec<_> = (0..MAX_SPANS + 1)
            .map(|_| Transfer::Write {
                gpa: 0,
                bytes: &[1][..],
            })
            .collect();
        assert!(
            b.copy_with(BUDGET, &mut many, |_| panic!("too many spans"))
                .is_err()
        );
        *b.io_epoch.lock().unwrap() = u64::MAX;
        assert!(
            b.copy_with(
                BUDGET,
                &mut [Transfer::Write {
                    gpa: 0,
                    bytes: &[1]
                }],
                |_| panic!("epoch wrap")
            )
            .is_err()
        );
        assert_eq!(load(&b, offset_of!(IoWindow, request)), 0);
        assert_eq!(read(&b, 0, 1), [0]);
    }

    #[test]
    fn canceled_permit_never_announces_a_window() {
        let b = backing(1);
        let permit = CopyPermit::new();
        assert!(permit.cancel());
        assert!(permit.cancel());
        assert!(
            b.copy_with_permit(
                BUDGET,
                &mut [Transfer::Write {
                    gpa: 0,
                    bytes: &[9]
                }],
                &permit,
                |_, _| panic!("canceled")
            )
            .is_err()
        );
        assert_eq!(load(&b, offset_of!(IoWindow, request)), 0);
        assert_eq!(read(&b, 0, 1), [0]);
    }
    #[test]
    fn cancellation_during_closing_releases_without_copy() {
        let b = backing(2);
        let permit = CopyPermit::new();
        assert!(
            b.copy_with_permit(
                BUDGET,
                &mut [Transfer::Write {
                    gpa: 0,
                    bytes: &[9]
                }],
                &permit,
                |v, _| {
                    assert_eq!(v, 0);
                    acknowledge(&b, v)?;
                    assert!(permit.cancel());
                    Ok(true)
                }
            )
            .is_err()
        );
        assert_eq!(load(&b, offset_of!(IoWindow, released)), 1);
        assert_eq!(read(&b, 0, 1), [0]);
    }
    #[test]
    fn cancellation_after_last_ack_still_prevents_grant() {
        let b = backing(2);
        let permit = CopyPermit::new();
        assert!(
            b.copy_with_permit(
                BUDGET,
                &mut [Transfer::Write {
                    gpa: 0,
                    bytes: &[9]
                }],
                &permit,
                |v, _| {
                    acknowledge(&b, v)?;
                    if v == 1 {
                        assert!(permit.cancel());
                    }
                    Ok(true)
                }
            )
            .is_err()
        );
        assert_eq!(read(&b, 0, 1), [0]);
        assert_eq!(b.io_window_counts().writes, 0);
    }
    #[test]
    fn completed_grant_cannot_be_canceled_or_reused() {
        let b = backing(1);
        let permit = CopyPermit::new();
        b.copy_with_permit(
            BUDGET,
            &mut [Transfer::Write {
                gpa: 0,
                bytes: &[9],
            }],
            &permit,
            |v, _| acknowledge(&b, v),
        )
        .unwrap();
        assert!(!permit.cancel());
        assert!(
            b.copy_with_permit(
                BUDGET,
                &mut [Transfer::Write {
                    gpa: 0,
                    bytes: &[8]
                }],
                &permit,
                |_, _| panic!("reused")
            )
            .is_err()
        );
        assert_eq!(read(&b, 0, 1), [9]);
        assert_eq!(load(&b, offset_of!(IoWindow, request)), 1);
    }
    #[test]
    fn timed_out_permit_cannot_be_reused() {
        let b = backing(1);
        let permit = CopyPermit::new();
        assert!(
            b.copy_with_permit(
                Duration::ZERO,
                &mut [Transfer::Write {
                    gpa: 0,
                    bytes: &[9]
                }],
                &permit,
                |_, _| Ok(true)
            )
            .is_err()
        );
        assert!(permit.cancel());
        assert!(
            b.copy_with_permit(
                BUDGET,
                &mut [Transfer::Write {
                    gpa: 0,
                    bytes: &[9]
                }],
                &permit,
                |_, _| panic!("reused")
            )
            .is_err()
        );
        assert_eq!(read(&b, 0, 1), [0]);
    }
    #[test]
    fn rejected_batch_consumes_its_permit() {
        let b = backing(1);
        let permit = CopyPermit::new();
        assert!(
            b.copy_with_permit(
                BUDGET,
                &mut [Transfer::Write {
                    gpa: u64::MAX,
                    bytes: &[9]
                }],
                &permit,
                |_, _| panic!("invalid")
            )
            .is_err()
        );
        assert!(permit.cancel());
        assert!(
            b.copy_with_permit(
                BUDGET,
                &mut [Transfer::Write {
                    gpa: 0,
                    bytes: &[9]
                }],
                &permit,
                |_, _| panic!("reused")
            )
            .is_err()
        );
    }
    #[test]
    fn grant_and_cancel_have_exactly_one_winner() {
        for _ in 0..64 {
            let permit = CopyPermit::new();
            let barrier = std::sync::Barrier::new(2);
            std::thread::scope(|scope| {
                let grant = scope.spawn(|| {
                    barrier.wait();
                    permit.grant()
                });
                barrier.wait();
                let canceled = permit.cancel();
                assert_ne!(grant.join().unwrap(), canceled);
                assert!(!permit.grant(), "a consumed permit can never grant again");
            });
        }
    }

    // Windowless copies. Page 0 holds a "payload", page 1 a "used ring": body writes page 0,
    // the publication writes the used index at 0x1002 and reads the available ring at 0x2000.
    const DRAIN: Duration = Duration::from_millis(5);
    fn code_mark(b: &Backing, page: usize) -> &AtomicU64 {
        global_u64(&b.memory, cache::CODE_PAGES + page * 8)
    }
    fn completion<'a>(payload: &'a [u8], avail: &'a mut [u8]) -> [Transfer<'a>; 3] {
        [
            Transfer::Write {
                gpa: 0x10,
                bytes: payload,
            },
            Transfer::Write {
                gpa: 0x1002,
                bytes: &[5, 0],
            },
            Transfer::Read {
                gpa: 0x2000,
                bytes: avail,
            },
        ]
    }
    fn no_ring(_: usize, peer: Peer) -> Result<bool> {
        assert_eq!(peer, Peer::Ask, "nothing to ring");
        Ok(true)
    }

    #[test]
    fn usual_write_copies_without_window_marks_or_kicks() {
        let b = backing(2);
        // SAFETY: fixture RAM, in bounds and aligned, outside any copy.
        unsafe { b.ram.range(0x2002, 2).unwrap().cast::<u16>().write(7) };
        let mut avail = [0; 4];
        b.windowless(
            BUDGET,
            DRAIN,
            &mut completion(&[1, 2, 3], &mut avail),
            1,
            &CopyPermit::new(),
            no_ring,
            |_| {},
        )
        .unwrap();
        assert_eq!(read(&b, 0x10, 3), [1, 2, 3]);
        assert_eq!(read(&b, 0x1002, 2), [5, 0]);
        assert_eq!(avail, [0, 0, 7, 0]);
        let counts = b.io_window_counts();
        assert_eq!(
            (counts.windowless, counts.granted, counts.drain_waits),
            (1, 0, 0)
        );
        assert_eq!(load(&b, offset_of!(IoWindow, request)), 0, "no epoch");
    }

    #[test]
    fn holders_of_written_pages_drain_before_publication() {
        let b = backing(3);
        code_mark(&b, 0).store(0b010, SeqCst);
        code_mark(&b, 1).store(0b100, SeqCst);
        let (kicked, receive) = std::sync::mpsc::channel();
        std::thread::scope(|scope| {
            let copy = scope.spawn(|| {
                let mut avail = [0; 4];
                b.windowless(
                    BUDGET,
                    BUDGET,
                    &mut completion(&[9], &mut avail),
                    1,
                    &CopyPermit::new(),
                    |v, ready| {
                        if ready == Peer::Ring {
                            kicked.send(v).unwrap();
                        }
                        Ok(true)
                    },
                    |_| {},
                )
            });
            let mut rung = [receive.recv().unwrap(), receive.recv().unwrap()];
            rung.sort();
            assert_eq!(rung, [1, 2], "both holders rung");
            assert_eq!(
                (code_mark(&b, 0).load(SeqCst), code_mark(&b, 1).load(SeqCst)),
                (0, 0)
            );
            assert_eq!(record_word(&b.memory, 1, None).load(SeqCst), cache::PAGES);
            while read(&b, 0x10, 1) != [9] {
                std::thread::yield_now();
            }
            // vCPU 1 drains; vCPU 2 (holding the used ring's page) still owes its drain.
            record_word(&b.memory, 1, Some(0)).fetch_and(!1, SeqCst);
            std::thread::sleep(Duration::from_millis(20));
            assert_eq!(
                read(&b, 0x1002, 2),
                [0, 0],
                "not published before every drain"
            );
            // vCPU 2 halts instead: it drains when it withdraws, before guest access.
            quiescent(&b.memory, 2).store(1, SeqCst);
            copy.join().unwrap().unwrap();
        });
        assert_eq!(read(&b, 0x1002, 2), [5, 0]);
        assert!(stale(&b.memory, 2, 1), "a halted holder keeps its post");
        let counts = b.io_window_counts();
        assert_eq!(
            (
                counts.windowless_posted,
                counts.drain_waits,
                counts.drain_expiries
            ),
            (1, 1, 0)
        );
        assert!(counts.drain_max_us >= 20_000 && counts.drain_us >= counts.drain_max_us);
    }

    #[test]
    fn a_drain_owed_to_an_earlier_writer_is_awaited_and_halted_holders_are_not_rung() {
        // An earlier writer cleared page 0's mark and posted it to vCPUs 1 and 2: this batch
        // finds no mark, but vCPU 1 may still run blocks of the old bytes. vCPU 2 is halted.
        let b = backing(3);
        record_word(&b.memory, 1, Some(0)).store(1, SeqCst);
        record_word(&b.memory, 2, Some(0)).store(1, SeqCst);
        quiescent(&b.memory, 2).store(1, SeqCst);
        let rung = std::sync::Mutex::new(Vec::new());
        std::thread::scope(|scope| {
            let copy = scope.spawn(|| {
                let mut avail = [0; 4];
                b.windowless(
                    BUDGET,
                    BUDGET,
                    &mut completion(&[2], &mut avail),
                    1,
                    &CopyPermit::new(),
                    |v, peer| {
                        if peer == Peer::Ring {
                            rung.lock().unwrap().push(v);
                        }
                        Ok(true)
                    },
                    |_| {},
                )
            });
            while read(&b, 0x10, 1) != [2] {
                std::thread::yield_now();
            }
            std::thread::sleep(Duration::from_millis(20));
            assert_eq!(
                read(&b, 0x1002, 2),
                [0, 0],
                "published before the owed drain"
            );
            record_word(&b.memory, 1, Some(0)).store(0, SeqCst);
            copy.join().unwrap().unwrap();
        });
        assert_eq!(read(&b, 0x1002, 2), [5, 0]);
        assert_eq!(
            *rung.lock().unwrap(),
            [1],
            "only the running owing vCPU is rung"
        );
        assert_eq!(b.io_window_counts().windowless_posted, 1);
    }

    #[test]
    fn marks_raced_during_the_copy_are_posted_and_drained() {
        // Body pages are rechecked before the drain wait; the used index's own page after it.
        for (at, page, awaited) in [
            (cache::io_test::AFTER_PRECHECK, 0, true),
            (cache::io_test::BEFORE_RECHECK, 0, true),
            (cache::io_test::BEFORE_RECHECK, 1, false),
            (cache::io_test::BEFORE_PUBLISH, 1, false),
        ] {
            let b = backing(2);
            let mut kicks = Vec::new();
            let mut avail = [0; 4];
            b.windowless(
                BUDGET,
                BUDGET,
                &mut completion(&[4], &mut avail),
                1,
                &CopyPermit::new(),
                |v, ready| {
                    if ready == Peer::Ring {
                        // The holder drains at its next block boundary.
                        kicks.push(v);
                        record_word(&b.memory, v, Some(0)).store(0, SeqCst);
                    }
                    Ok(true)
                },
                |point| {
                    if point == at {
                        code_mark(&b, page).store(1, SeqCst);
                    }
                },
            )
            .unwrap();
            assert_eq!(kicks, [0], "{at} {page}");
            assert_eq!(code_mark(&b, page).load(SeqCst), 0);
            assert_eq!(read(&b, 0x1002, 2), [5, 0]);
            let counts = b.io_window_counts();
            assert_eq!(counts.recheck_holders, 1);
            // A mark of the used index's page found after publication is posted, not awaited.
            assert_eq!(counts.drain_waits, u64::from(awaited));
        }
    }

    #[test]
    fn page_table_mark_sends_the_whole_batch_through_a_window() {
        let b = backing(2);
        global_u64(&b.memory, cache::TABLE_PAGES).store(0b10, SeqCst);
        code_mark(&b, 0).store(1, SeqCst);
        let mut avail = [0; 4];
        b.windowless(
            BUDGET,
            DRAIN,
            &mut completion(&[3], &mut avail),
            1,
            &CopyPermit::new(),
            |v, _| acknowledge(&b, v),
            |_| panic!("no windowless point"),
        )
        .unwrap();
        assert_eq!(
            (read(&b, 0x10, 1), read(&b, 0x1002, 2)),
            (vec![3], vec![5, 0])
        );
        let counts = b.io_window_counts();
        assert_eq!(
            (
                counts.table_fallbacks,
                counts.windowless,
                counts.granted,
                counts.table_writes
            ),
            (1, 0, 1, 1)
        );
        assert_eq!(load(&b, offset_of!(IoWindow, tables_written)), 1);
        assert_eq!(code_mark(&b, 0).load(SeqCst), 0);
    }

    #[test]
    fn reads_only_batch_leaves_marks_and_kicks_alone() {
        let b = backing(2);
        code_mark(&b, 0).store(3, SeqCst);
        global_u64(&b.memory, cache::TABLE_PAGES).store(1, SeqCst);
        let mut bytes = [0; 32];
        let mut reads = [Transfer::Read {
            gpa: 0,
            bytes: &mut bytes,
        }];
        b.windowless(
            BUDGET,
            DRAIN,
            &mut reads,
            1,
            &CopyPermit::new(),
            no_ring,
            |_| {},
        )
        .unwrap();
        assert_eq!(code_mark(&b, 0).load(SeqCst), 3);
        let counts = b.io_window_counts();
        assert_eq!(
            (counts.windowless, counts.table_fallbacks, counts.granted),
            (1, 0, 0)
        );
    }

    #[test]
    fn reset_cancels_before_the_first_write_or_waits_for_the_copy() {
        let b = backing(1);
        code_mark(&b, 0).store(1, SeqCst);
        let permit = CopyPermit::new();
        assert!(permit.cancel());
        let mut avail = [0; 4];
        assert!(
            b.windowless(
                BUDGET,
                DRAIN,
                &mut completion(&[8], &mut avail),
                1,
                &permit,
                |_, _| panic!("canceled batch must not kick"),
                |_| panic!("canceled batch must not copy"),
            )
            .is_err()
        );
        assert_eq!(read(&b, 0x10, 1), [0]);
        assert_eq!(
            code_mark(&b, 0).load(SeqCst),
            1,
            "a canceled batch posts nothing"
        );
        assert_eq!(b.io_window_counts().windowless, 0);
        // Once granted, a reset loses the race and must wait; the batch completes whole.
        let permit = CopyPermit::new();
        let mut canceled = None;
        b.windowless(
            BUDGET,
            DRAIN,
            &mut completion(&[8], &mut avail),
            1,
            &permit,
            |_, _| Ok(false), // the holder has ended
            |point| {
                if point == cache::io_test::AFTER_PRECHECK {
                    canceled = Some(permit.cancel());
                }
            },
        )
        .unwrap();
        assert_eq!(canceled, Some(false));
        assert_eq!(
            (read(&b, 0x10, 1), read(&b, 0x1002, 2)),
            (vec![8], vec![5, 0])
        );
        assert_eq!(
            b.io_window_counts().drain_expiries,
            0,
            "an ended holder is not awaited"
        );
        assert!(
            b.windowless(BUDGET, DRAIN, &mut [], 0, &permit, no_ring, |_| {})
                .is_err(),
            "a consumed permit cannot be reused"
        );
    }

    #[test]
    fn drain_deadline_publishes_through_a_window() {
        let b = backing(2);
        code_mark(&b, 0).store(0b10, SeqCst);
        let mut avail = [0; 4];
        let mut points = Vec::new();
        b.windowless(
            BUDGET,
            DRAIN,
            &mut completion(&[6], &mut avail),
            1,
            &CopyPermit::new(),
            |v, ready| {
                if ready == Peer::Ring && load(&b, offset_of!(IoWindow, request)) != 0 {
                    acknowledge(&b, v)?;
                }
                Ok(true) // vCPU 1 never drains its post
            },
            |point| points.push(point),
        )
        .unwrap();
        assert_eq!(
            points,
            [
                cache::io_test::AFTER_PRECHECK,
                cache::io_test::BEFORE_RECHECK
            ],
            "the publication did not go windowless"
        );
        assert_eq!(
            (read(&b, 0x10, 1), read(&b, 0x1002, 2)),
            (vec![6], vec![5, 0])
        );
        assert_eq!(load(&b, offset_of!(IoWindow, released)), 1);
        let counts = b.io_window_counts();
        assert_eq!(
            (
                counts.drain_expiries,
                counts.granted,
                counts.writes,
                counts.windowless
            ),
            (1, 1, 1, 1)
        );
        assert!(
            stale(&b.memory, 1, 0),
            "it drains after release, before guest access"
        );
        let line: serde_json::Value =
            serde_json::from_str(counts.perf_line().strip_prefix("io ").unwrap()).unwrap();
        for key in [
            "windowless",
            "windowless_posted",
            "recheck_holders",
            "drain_waits",
            "drain_us",
            "drain_max_us",
            "drain_expiries",
            "table_fallbacks",
        ] {
            assert!(line[key].is_u64(), "{key} in the perf line");
        }
    }

    #[test]
    fn small_spans_move_as_whole_aligned_fields() {
        let b = backing(1);
        let mut bytes = [0; 7];
        let mut ops = [
            Transfer::Write {
                gpa: 0x101,
                bytes: &[1, 2, 3, 4, 5, 6, 7],
            },
            Transfer::Read {
                gpa: 0x101,
                bytes: &mut bytes,
            },
        ];
        b.windowless(
            BUDGET,
            DRAIN,
            &mut ops,
            2,
            &CopyPermit::new(),
            no_ring,
            |_| {},
        )
        .unwrap();
        assert_eq!(bytes, [1, 2, 3, 4, 5, 6, 7]);
        assert_eq!(read(&b, 0x100, 9), [0, 1, 2, 3, 4, 5, 6, 7, 0]);
    }
}
