//! Finite copy-first I/O access to guest RAM. Backends retain only their own buffers. A window
//! waits for resident acknowledgements or HLT quiescence, copies prevalidated spans, and releases; it runs no
//! parser, callback or backend operation while guest memory is exclusively accessible.
use super::*;
use cache::IoWindow;
use std::mem::offset_of;
use std::sync::atomic::AtomicU8;

pub const MAX_BYTES: usize = 256 * 1024;
/// A full 256-entry queue plus payload/status/used-ring bookkeeping in the same window.
pub const MAX_SPANS: usize = 512;

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
    let (index, shift) = cache::mask_location(page);
    let mask = global_u64(memory, cache::CODE_PAGES + index * 8);
    let cleared = mask.fetch_and(!(cache::CpuMask::MAX << shift), SeqCst);
    let peers = cache::mask_peers(cleared, shift);
    for v in (0..n).filter(|v| peers & (1 << v) != 0) {
        record_word(memory, v, Some(page / 64)).fetch_or(1 << (page % 64), SeqCst);
        let summary = cache::RECORDS
            + v * size_of::<cache::Record>()
            + offset_of!(cache::Record, summary)
            + page / 64 / 64 * 8;
        global_u64(memory, summary).fetch_or(1 << (page / 64 % 64), SeqCst);
        record_word(memory, v, None).fetch_or(cache::PAGES, SeqCst);
    }
    // No kick: every participant is parked and drains before resuming guest access.
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
}
impl WindowCounts {
    /// One `io {...}` JSON line for [`crate::perf::line`].
    pub fn perf_line(&self) -> String {
        format!("io {}", serde_json::to_string(self).unwrap())
    }
}
/// [`WindowCounts`] as the coordinator updates them. Only the window holder writes (under the
/// epoch lock); readers never take that lock, so observing a pending window cannot block.
#[derive(Default)]
pub(crate) struct WindowCounters {
    granted: AtomicU64,
    writes: AtomicU64,
    table_writes: AtomicU64,
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
        let mut sequence = self.lock_epoch(permit, deadline)?;
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
        #[cfg(feature = "io-test-hooks")]
        super::io_test::point(&self.memory, cache::io_test::BEFORE_GRANT);
        if !permit.grant() {
            return Err("I/O window canceled or permit already consumed".into());
        }
        #[cfg(feature = "io-test-hooks")]
        super::io_test::point(&self.memory, cache::io_test::AFTER_GRANT);
        unsafe { std::arch::asm!("dsb sy", options(nostack, preserves_flags)) };
        let counters = &self.io_counts;
        counters.granted.fetch_add(1, Relaxed);
        if spans.iter().any(|span| span.write) {
            counters.writes.fetch_add(1, Relaxed);
            if self.invalidate_written(&spans, epoch) {
                counters.table_writes.fetch_add(1, Relaxed);
            }
        }
        for span in &spans {
            // SAFETY: the complete installed-RAM ranges were checked above; all residents have
            // completed their accesses and are parked. Host buffers are borrowed for this call,
            // cannot overlap guest backing in safe code, and have the checked lengths. Copies are
            // ordered; no exclusive Rust reference is manufactured over guest memory.
            unsafe {
                if span.write {
                    std::ptr::copy_nonoverlapping(span.host, span.guest, span.len);
                } else {
                    std::ptr::copy_nonoverlapping(span.guest, span.host, span.len);
                }
            }
        }
        drop(release);
        Ok(())
    }

    /// Take the window sequence, polling so cancellation and the deadline still apply.
    fn lock_epoch(
        &self,
        permit: &CopyPermit,
        deadline: Instant,
    ) -> Result<std::sync::MutexGuard<'_, u64>> {
        loop {
            if permit.canceled() {
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
        permit: &CopyPermit,
        deadline: Instant,
        mut kick: impl FnMut(usize, bool) -> Result<bool>,
    ) -> Result<()> {
        for v in 0..self.n {
            if permit.canceled() {
                return Err("I/O window canceled".into());
            }
            let ready = acknowledged_or_halted(&self.memory, v, epoch);
            if !kick(v, ready)? {
                return Err(format!("I/O window vCPU {v} has ended"));
            }
        }
        loop {
            if permit.canceled() {
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
mod tests {
    use super::*;

    fn backing(n: usize) -> Backing {
        let ram_at = cache::GLOBAL_OFFSET + cache::GLOBAL_BYTES;
        let memory = Arc::new(Memory::with_size(ram_at + 3 * 4096).unwrap());
        // SAFETY: this fixture owns a separate aligned RAM page after its protocol words.
        let ram = unsafe { GuestRam::new(0, 3 * 4096, memory.0.add(ram_at) as u64) };
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
        let counts = WindowCounts {
            granted: 5,
            writes: 4,
            table_writes: 2,
        };
        assert_eq!(
            b.io_window_counts(),
            counts,
            "only page-table overlaps count"
        );
        let old = format!(
            "io {{\"windows\":{},\"write_windows\":{},\"table_windows\":{}}}",
            counts.granted, counts.writes, counts.table_writes
        );
        assert_eq!(
            counts.perf_line(),
            old,
            "the perf line is unchanged byte for byte"
        );
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
}
