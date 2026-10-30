//! Host side of block execution (`natemu_abi::execution`): the execution mode's
//! private address space (code cache areas included), and the service loop that
//! answers the resident's translation requests, which arrive only on a cache miss:
//! copy the guest bytes at RIP from the guest-physical pages the resident walked, compile one block with fexc
//! (`--profile slice1`, a persistent server), admit it and stage it. A step request
//! (self-modifying code) is the same with a one-instruction block. A watchdog
//! stops a vCPU that runs too long without a host exit. A kick ([`Stops`]) rings a
//! vCPU's doorbell, which stops it precisely at its next block boundary in the VM.
use super::*;
use crate::admission;
use natemu_abi::execution::{self as exec, Cpu, State, cache};
use natemu_abi::guest_memory::GuestRam;
use natemu_abi::paging::Fault;
use std::collections::HashMap;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering::SeqCst};
use std::sync::Mutex;
use std::time::{Duration, Instant};

/// The execution mode's backing for `n` vCPUs. Block 0 holds vCPU 0's window, its lookup area, the global area, its
/// metadata arena and its code area (the `exec`/`cache` offsets); block v (`v * stride` further) holds vCPU v's own
/// window, lookup area, metadata arena and code area at the same offsets and leaves the global area's range unused
/// (never mapped, so never backed): every vCPU maps block 0's. Guest RAM (M1: `ram_bytes`, chosen at prepare) follows
/// the last block, at `n * stride`; only the pages the guest or the host touch are ever backed.
pub(crate) struct Backing {
    pub(crate) memory: Memory,
    pub(crate) n: usize,
    stride: usize,
    /// Guest-physical RAM through the host's alias.
    pub(crate) ram: GuestRam,
    /// One CNTVCT sample (the host's `mach_absolute_time`: every vCPU's virtual counter has offset 0) taken when the VM
    /// was prepared: each vCPU's TSC starts from it, so all start near 0 and agree.
    epoch: u64,
}
impl Backing {
    /// vCPU `v`'s view.
    pub(crate) fn vcpu(&self, v: usize) -> Area<'_> {
        assert!(v < self.n);
        Area { memory: &self.memory, vcpu: v, vcpus: self.n, epoch: self.epoch, base: v * self.stride, ram: self.ram }
    }
}
/// One vCPU's own part of the backing: offsets are those of block 0 (`exec`/`cache`), relocated to its block.
#[derive(Clone, Copy)]
pub(crate) struct Area<'a> {
    pub(crate) memory: &'a Memory,
    pub(crate) vcpu: usize,
    /// The VM's vCPU count, and the CNTVCT its TSCs start from ([`Backing`]).
    pub(crate) vcpus: usize,
    pub(crate) epoch: u64,
    /// Backing offset of its block (its window).
    pub(crate) base: usize,
    /// The VM's guest RAM ([`Backing::ram`]).
    pub(crate) ram: GuestRam,
}
impl Area<'_> {
    pub(crate) fn read_record<T: Copy>(&self, offset: usize) -> T {
        self.memory.read_record(self.base + offset)
    }
    pub(crate) fn write_record<T>(&self, offset: usize, value: T) {
        self.memory.write_record(self.base + offset, value)
    }
    pub(crate) fn read<const N: usize>(&self, offset: usize) -> [u8; N] {
        self.memory.read(self.base + offset)
    }
    pub(crate) fn copy(&self, offset: usize, bytes: &[u8]) {
        self.memory.copy(self.base + offset, bytes)
    }
}

/// Backing for the execution mode with `n` vCPUs (1..=`cache::MAX_VCPUS`) and `ram_bytes` of guest RAM (a 2 MiB
/// multiple, at most `exec::MAX_GUEST_RAM_BYTES`): the resident loaded into each vCPU's window, each vCPU's private
/// address space and shared record in place; guest RAM is zero. Each vCPU's code cache holds `code_bytes` of generated
/// code (at most `cache::CODE_BYTES_MAX`).
pub(crate) fn prepare(resident: &str, code_bytes: usize, n: usize, ram_bytes: usize) -> Result<Backing> {
    if !(1..=cache::MAX_VCPUS).contains(&n) {
        return Err(format!("{n} vCPUs: the execution mode runs 1 to {}", cache::MAX_VCPUS));
    }
    if ram_bytes == 0 || ram_bytes % cache::BLOCK != 0 || ram_bytes > exec::MAX_GUEST_RAM_BYTES {
        return Err(format!("guest RAM of {ram_bytes:#x} bytes: a nonzero 2 MiB multiple up to {:#x}", exec::MAX_GUEST_RAM_BYTES));
    }
    if !(cache::MAX_BLOCK_BYTES..=cache::CODE_BYTES_MAX - cache::HEAD_BYTES).contains(&code_bytes) {
        return Err(format!(
            "code cache of {code_bytes} bytes: it must hold the largest block ({} bytes) and at most {} bytes",
            cache::MAX_BLOCK_BYTES,
            cache::CODE_BYTES_MAX - cache::HEAD_BYTES
        ));
    }
    let code_mapped = (cache::HEAD_BYTES + code_bytes).next_multiple_of(cache::BLOCK);
    let stride = exec::BACKING_BYTES + code_mapped;
    let ram_offset = n * stride;
    let memory = Memory::with_size(ram_offset + ram_bytes)?;
    load_resident(&memory, resident)?;
    for v in 0..n {
        let base = v * stride;
        if v > 0 {
            memory.replicate_resident_text(base);
        }
        let mut spans = resident_text(base);
        let own = |offset, bytes| Span::data(offset, bytes, base + offset);
        spans.extend([
            own(SHARED_OFFSET, 4096),
            own(exec::STATE_OFFSET, 4096),
            own(exec::FRAME_OFFSET, exec::frame::BYTES),
            // The guest-state page's EL0 alias (x28 of generated code), backed by the same page (G1 minimum).
            Span::guest(exec::GUEST_STATE_OFFSET, exec::frame::BYTES, base + exec::FRAME_OFFSET),
            own((STACK_TOP - PRIVATE) as usize - exec::STACK_BYTES, exec::STACK_BYTES),
            own((EXCEPTION_STACK_TOP - PRIVATE) as usize - 0x8000, 0x8000),
            own(exec::STAGING_OFFSET, exec::STAGING_BYTES),
            Span::data((exec::GUEST_RAM_VA - PRIVATE) as usize, ram_bytes, ram_offset),
            // Generated code runs at EL0 from the code area (EL0 execute-only); the resident writes it through its alias.
            Span::code((cache::CODE_VA - PRIVATE) as usize, code_mapped, base + cache::CODE_OFFSET),
            Span::data((cache::CODE_WRITE_VA - PRIVATE) as usize, code_mapped, base + cache::CODE_OFFSET),
            Span::data((cache::META_VA - PRIVATE) as usize, cache::META_BYTES, base + cache::META_OFFSET),
            Span::data((cache::LOOKUP_VA - PRIVATE) as usize, cache::LOOKUP_BYTES, base + cache::LOOKUP_OFFSET),
            Span::data((cache::GLOBAL_VA - PRIVATE) as usize, cache::GLOBAL_BYTES, cache::GLOBAL_OFFSET),
        ]);
        private_space(&memory, base, &spans, true);
        // SAFETY: Control is plain integers; the resident owns everything but the capacity.
        let mut control: cache::Control = unsafe { std::mem::zeroed() };
        control.code_bytes = (cache::HEAD_BYTES + code_bytes) as u64;
        control.ram_bytes = ram_bytes as u64;
        memory.write_record(base + cache::LOOKUP_OFFSET + cache::CONTROL, control);
        // SAFETY: Shared is plain integers; all-zero is its empty value.
        let mut shared: Shared = unsafe { std::mem::zeroed() };
        shared.mode = exec::MODE;
        memory.write_record(base + SHARED_OFFSET, shared);
        memory.flush_instruction_cache(base, RAM_SIZE);
    }
    // SAFETY: the alias is the backing's last `ram_bytes`, which lives as long as the Backing holding this value.
    let ram = unsafe { exec::guest_ram(ram_bytes as u64, memory.0 as u64 + ram_offset as u64) };
    Ok(Backing { memory, n, stride, ram, epoch: unsafe { mach_absolute_time() } })
}
/// Sets the vCPU's initial architectural state (before it first runs).
pub(crate) fn set_cpu(area: Area, cpu: Cpu) {
    let mut state = State::new(area.vcpu as u64, cpu);
    state.vcpus = area.vcpus as u64;
    area.write_record(exec::STATE_OFFSET, state);
    // The TSC (L5, `natemu_abi::clock::tsc`) is the resident's CNTVCT scaled to TSC_HZ plus this offset: from the shared epoch.
    const _: () = assert!(natemu_abi::cpu_profile::TSC_HZ == 1_000_000_000); // ticks_ns scales like the resident
    let mut msrs = natemu_abi::cpu::reset_msrs(area.vcpu == 0);
    msrs[natemu_abi::cpu::msr(natemu_abi::cpu::MSR_TSC).unwrap()] = ticks_ns(area.epoch).wrapping_neg();
    area.write_record(exec::MSRS_OFFSET, msrs);
    area.write_record(exec::ENTROPY_OFFSET, exec::Entropy { taken: exec::ENTROPY_WORDS as u64, words: [0; exec::ENTROPY_WORDS] });
    refill_entropy(area);
    set_lapic(area, natemu_abi::lapic::RESET);
}
/// Sets the vCPU's local APIC record (X3) and the word peers match IPI destinations against, from its reset
/// IA32_APIC_BASE (before it first runs).
pub(crate) fn set_lapic(area: Area, l: natemu_abi::lapic::Lapic) {
    area.write_record(exec::LAPIC_OFFSET, l);
    let base = natemu_abi::cpu::reset_msrs(area.vcpu == 0)[natemu_abi::cpu::msr(natemu_abi::cpu::MSR_APIC_BASE).unwrap()];
    let word = natemu_abi::lapic::published(base, &l, area.vcpu as u64);
    event_word(area.memory, area.vcpu, std::mem::offset_of!(cache::Events, apic)).store(word, SeqCst);
}
/// The vCPU's local APIC record (X3; the resident's once it runs).
pub(crate) fn lapic(area: Area) -> natemu_abi::lapic::Lapic {
    area.read_record(exec::LAPIC_OFFSET)
}
/// Tops up the vCPU's entropy pool (RDRAND, [`exec::Entropy`]) while it is in the host: the words it took are replaced
/// from the host's entropy source (`getentropy`, macOS's kernel CSPRNG). If the source fails, the pool stays as it is
/// and an empty pool makes RDRAND return CF = 0 (its architectural failure), never a predictable value.
fn refill_entropy(area: Area) {
    unsafe extern "C" {
        fn getentropy(buf: *mut u8, len: usize) -> i32;
    }
    let mut pool: exec::Entropy = area.read_record(exec::ENTROPY_OFFSET);
    let taken = (pool.taken as usize).min(exec::ENTROPY_WORDS);
    if taken == 0 {
        return;
    }
    let mut fresh = [0u64; exec::ENTROPY_WORDS];
    // SAFETY: `fresh` is 256 bytes, getentropy's maximum.
    if unsafe { getentropy(fresh.as_mut_ptr().cast(), 8 * taken) } == 0 {
        pool.words[..taken].copy_from_slice(&fresh[..taken]);
        pool.taken = 0;
        area.write_record(exec::ENTROPY_OFFSET, pool);
    }
}
/// Enters the vCPU at the resident in its window, its tables tagged with its own ASIDs (2v + 2 for its supervisor
/// shadow space, loaded at boot; the resident uses 2v + 3 for its user space), as GIC vCPU `area.vcpu` with its peer
/// doorbell enabled (`doorbell`; disabled only by a test driver's never-answering peer).
pub(crate) fn enter(cpu: &Vcpu, area: Area, doorbell: bool) -> Result<()> {
    // 8-bit ASIDs, never 0.
    const _: () = assert!(2 * cache::MAX_VCPUS + 1 <= 255);
    cpu.doorbell(area.vcpu, doorbell)?;
    cpu.enter_resident_with_asid(area.base, natemu_abi::shadow::TCR_BOTH, 2 * area.vcpu as u8 + 2)
}

/// The global area word at `offset` (shared by all vCPUs; atomic access only).
fn global_u64(memory: &Memory, offset: usize) -> &AtomicU64 {
    let at = cache::GLOBAL_OFFSET + offset;
    assert!(at % 8 == 0 && at + 8 <= memory.1);
    unsafe { AtomicU64::from_ptr(memory.0.add(at).cast()) }
}
/// vCPU `v`'s invalidation record: `pending`, or `stale` word `i`.
fn record_word(memory: &Memory, v: usize, i: Option<usize>) -> &AtomicU64 {
    let record = cache::RECORDS + v * size_of::<cache::Record>();
    global_u64(memory, record + i.map_or(0, |i| std::mem::offset_of!(cache::Record, stale) + 8 * i))
}
/// vCPU `v`'s event record word at `field` (an offset in [`cache::Events`], X2).
fn event_word(memory: &Memory, v: usize, field: usize) -> &AtomicU64 {
    global_u64(memory, cache::EVENTS + v * size_of::<cache::Events>() + field)
}
/// `post` value of an NMI (else an interrupt vector, 0..=255).
pub(crate) const NMI: u64 = 256;
/// Posts `event` (a vector, or [`NMI`]) into vCPU `v`'s event record (X2); the caller then rings its doorbell, which makes
/// it stop precisely and take the event when it is eligible. L4a: `level`, a level-triggered interrupt: its TMR bit is
/// set (else cleared) before the IRR bit, as the LAPIC accepts it (SDM Vol.3 11.8.4), so its EOI is broadcast. Returns
/// whether the target accepted it.
pub(crate) fn post(memory: &Memory, v: usize, event: u64, level: bool) -> bool {
    // X3: as from a peer's APIC: an NMI needs the target's APIC hardware enabled, an interrupt also software enabled
    // (SDM Vol.3 11.4.7.2); otherwise the message is not accepted.
    let apic = event_word(memory, v, std::mem::offset_of!(cache::Events, apic)).load(SeqCst);
    let needs = if event == NMI { natemu_abi::lapic::HW_ENABLED } else { natemu_abi::lapic::SW_ENABLED };
    if apic & needs == 0 {
        return false;
    }
    match event {
        NMI => event_word(memory, v, std::mem::offset_of!(cache::Events, nmi)).store(1, SeqCst),
        _ => {
            let w = 8 * (event as usize / 64 % 4);
            let tmr = event_word(memory, v, std::mem::offset_of!(cache::Events, tmr) + w);
            if level { tmr.fetch_or(1 << (event % 64), SeqCst) } else { tmr.fetch_and(!(1 << (event % 64)), SeqCst) };
            event_word(memory, v, std::mem::offset_of!(cache::Events, vectors) + w).fetch_or(1 << (event % 64), SeqCst);
        }
    }
    event_word(memory, v, std::mem::offset_of!(cache::Events, posts)).fetch_add(1, SeqCst);
    true
}
/// L4b: whether `vector` is still in vCPU `v`'s IRR (posted, not yet accepted into service).
pub(crate) fn pending(memory: &Memory, v: usize, vector: u64) -> bool {
    let w = event_word(memory, v, std::mem::offset_of!(cache::Events, vectors) + 8 * (vector as usize / 64 % 4)).load(SeqCst);
    w >> (vector % 64) & 1 != 0
}
/// L4a: vCPU `v`'s published APIC word (what senders match destinations against, `natemu_abi::lapic::published`).
pub(crate) fn published(memory: &Memory, v: usize) -> u64 {
    event_word(memory, v, std::mem::offset_of!(cache::Events, apic)).load(SeqCst)
}
/// L4a: vCPU `v`'s LINT0 input level (the 8259's output). A rise at an APIC that passes ExtINTs to its core
/// (`natemu_abi::lapic::EXTINT`) counts as a post: true, the caller rings it.
pub(crate) fn set_lint0(memory: &Memory, v: usize, level: bool) -> bool {
    event_word(memory, v, std::mem::offset_of!(cache::Events, lint0)).store(u64::from(level), SeqCst);
    let post = level && published(memory, v) & natemu_abi::lapic::EXTINT != 0;
    if post {
        event_word(memory, v, std::mem::offset_of!(cache::Events, posts)).fetch_add(1, SeqCst);
    }
    post
}
/// vCPU `v`'s park state (X3): its `halted` word and its `posts` count, read in that order.
pub(crate) fn park_state(memory: &Memory, v: usize) -> (u64, u64) {
    let halted = event_word(memory, v, std::mem::offset_of!(cache::Events, halted)).load(SeqCst);
    (halted, event_word(memory, v, std::mem::offset_of!(cache::Events, posts)).load(SeqCst))
}
/// A park state in which nothing but a new post can wake the vCPU: halted, its own timer unable to wake it, and every
/// post seen.
pub(crate) fn idle((halted, posts): (u64, u64)) -> bool {
    halted & 0xff == 1 && halted >> cache::HALTED_SEEN_SHIFT == posts & (u64::MAX >> cache::HALTED_SEEN_SHIFT)
}
/// Asks vCPU `v` to end its run when it is halted with no event it can take (`stop::HALTED`); the caller rings it.
pub(crate) fn end_when_halted(memory: &Memory, v: usize) {
    event_word(memory, v, std::mem::offset_of!(cache::Events, end)).store(1, SeqCst);
}
/// vCPU `area.vcpu`'s shadow resets so far (the resident's counter: evictions, shootdowns, control changes, halts).
pub(crate) fn shadow_resets(area: Area) -> u64 {
    global_u64(area.memory, cache::RESETS + 8 * area.vcpu).load(SeqCst)
}
/// Guest RAM was written by the host (only while every vCPU is stopped): each vCPU drops its cached code before it
/// next runs guest code.
pub(crate) fn guest_ram_written(memory: &Memory) {
    for v in 0..cache::MAX_VCPUS {
        record_word(memory, v, None).fetch_or(cache::FLUSH, SeqCst);
    }
}
/// Precise stops from the host. A kick rings vCPU v's doorbell (its GIC SPI, from any thread, no host exit); the
/// resident's IRQ handler stops v at its next block boundary, drains its invalidation record and counts the stop
/// (`State::stops`, with its CNTVCT, which is the host's `mach_absolute_time`). A kick that finds v in the host or the
/// resident (interrupts masked) stays pending in the GIC and stops v as soon as it runs generated code again; a kick
/// while one is pending merges into it in the GIC. Peers' residents ring v's SGI directly (`cache::kick`), never the host.
pub(crate) struct Stops {
    kicks: Vec<Mutex<Kick>>,
    /// vCPU v is in the host (not in `hv_vcpu_run`). Accounting only (`Kick::in_host`, latency classes).
    in_host: Vec<AtomicBool>,
    /// Per-stop latencies are kept (`Kick::latency`) only when the driver asks.
    latencies: bool,
}
/// One vCPU's host kicks.
#[derive(Default, Clone)]
pub(crate) struct Kick {
    /// Kicks (doorbells rung), of which while the vCPU was in the host.
    pub(crate) issued: u64,
    pub(crate) in_host: u64,
    /// Nanoseconds spent in the `hv_gic_set_spi` calls.
    pub(crate) ring_ns: u64,
    /// Its doorbell was still pending when it ended (read before the VM goes).
    pub(crate) pending: bool,
    /// Per kick a stop followed, if the driver asked ([`Stops::new`]): nanoseconds from the ring to the resident's stop,
    /// and whether the kick found the vCPU in the host.
    pub(crate) latency: Vec<(u64, bool)>,
    /// The last ring: `mach_absolute_time`, in the host.
    rung: Option<(u64, bool)>,
    /// The vCPU's run ended (or it never ran): no kick rings it any more ([`Stops::end`]).
    ended: bool,
}
unsafe extern "C" {
    fn mach_absolute_time() -> u64;
}
/// Nanoseconds of `ticks` of `mach_absolute_time` (and CNTVCT: 24 MHz on Apple silicon).
pub(crate) fn ticks_ns(ticks: u64) -> u64 {
    #[repr(C)]
    struct Timebase {
        numer: u32,
        denom: u32,
    }
    unsafe extern "C" {
        fn mach_timebase_info(info: *mut Timebase) -> i32;
    }
    let mut t = Timebase { numer: 0, denom: 0 };
    unsafe { mach_timebase_info(&mut t) };
    (ticks as u128 * u128::from(t.numer) / u128::from(t.denom.max(1))) as u64
}
impl Stops {
    /// Kicks of `backing`'s vCPUs; `latencies`: keep every stop's latency.
    pub(crate) fn new(backing: &Backing, latencies: bool) -> Self {
        Self {
            kicks: (0..backing.n).map(|_| Mutex::default()).collect(),
            in_host: (0..backing.n).map(|_| AtomicBool::new(true)).collect(),
            latencies,
        }
    }
    /// Stops vCPU `v` of `machine` at its next block boundary; false (nothing rung) once its run [ended](Self::end).
    pub(crate) fn kick(&self, machine: &Machine, v: usize) -> Result<bool> {
        let mut k = self.kicks.get(v).ok_or(format!("kick of vCPU {v}: no such vCPU"))?.lock().unwrap();
        if k.ended {
            return Ok(false);
        }
        let in_host = self.in_host[v].load(SeqCst);
        let at = unsafe { mach_absolute_time() };
        machine.ring(v)?;
        k.ring_ns += ticks_ns(unsafe { mach_absolute_time() } - at);
        k.rung = Some((at, in_host));
        k.issued += 1;
        k.in_host += u64::from(in_host);
        Ok(true)
    }
    /// vCPU `v`'s run ended (or it never started): from now on no kick rings it, and a kick in progress has completed
    /// (the mutex). That every GIC call is serialized with vCPU create and destroy is `vm.rs`'s (its GIC lock).
    pub(crate) fn end(&self, v: usize) {
        self.kicks[v].lock().unwrap().ended = true;
    }
    pub(crate) fn ended(&self, v: usize) -> bool {
        self.kicks[v].lock().unwrap().ended
    }
    /// Whether vCPU `v`'s doorbell is still pending (rung, not acknowledged), asked under its kick mutex: `None` once
    /// its run [ended](Self::end).
    pub(crate) fn ringing(&self, machine: &Machine, v: usize) -> Result<Option<bool>> {
        let k = self.kicks[v].lock().unwrap();
        if k.ended {
            return Ok(None);
        }
        machine.ringing(v).map(Some)
    }
    /// Stops vCPU `v` (`area`) has taken (the resident's count).
    pub(crate) fn taken(&self, area: Area) -> u64 {
        area.read_record(exec::STATE_OFFSET + std::mem::offset_of!(State, stops))
    }
    /// A stop of `area`'s vCPU followed the last kick: its latency.
    pub(crate) fn followed(&self, area: Area) {
        let mut k = self.kicks[area.vcpu].lock().unwrap();
        if let (true, Some((at, in_host))) = (self.latencies, k.rung.take()) {
            // The resident writes its record field by field: the stop's time may land just after its count.
            let ticks = || area.read_record::<u64>(exec::STATE_OFFSET + std::mem::offset_of!(State, stop_ticks));
            let stop = (0..10_000).map(|_| ticks()).find(|&t| t >= at).unwrap_or(at);
            k.latency.push((ticks_ns(stop - at), in_host));
        }
    }
    /// Records whether vCPU `v`'s doorbell is still pending (after it and its kicker ended, before the VM goes).
    pub(crate) fn pending_at_end(&self, machine: &Machine, v: usize) -> Result<()> {
        let pending = machine.ringing(v)?;
        self.kicks[v].lock().unwrap().pending = pending;
        Ok(())
    }
    /// vCPU `v`'s accounting.
    pub(crate) fn report(&self, v: usize) -> Kick {
        self.kicks[v].lock().unwrap().clone()
    }
}

/// The vCPU's placed blocks, in placement order (read from its metadata arena).
pub(crate) fn placed_blocks(area: Area) -> Vec<cache::Block> {
    let control: cache::Control = area.read_record(cache::LOOKUP_OFFSET + cache::CONTROL);
    (0..control.blocks as usize)
        .map(|n| {
            let meta = u32::from_le_bytes(area.read::<4>(cache::LOOKUP_OFFSET + cache::INDEX + 4 * n));
            area.read_record(cache::META_OFFSET + meta as usize)
        })
        .collect()
}
/// The link record `i` (written by the resident's linker).
pub(crate) fn link_record(area: Area, i: usize) -> cache::LinkRecord {
    area.read_record(cache::LOOKUP_OFFSET + cache::LINKS + i * size_of::<cache::LinkRecord>())
}

/// Self-check of the vCPU's cache after a run (the contract's rule (d), independent of the resident): every link point of
/// every placed block holds its admitted word (the first word of its RIP move) or `b` to the entry of a placed block
/// keyed (target, the block's context), and the resident's link records describe exactly the linked ones. A word an
/// armed stop patched (a vCPU forced out with a stop armed) is judged by its word before the patch. Returns (link
/// points, linked).
pub(crate) fn check_links(area: Area) -> Result<(u64, u64)> {
    let lookup = |offset: usize| cache::LOOKUP_OFFSET + offset;
    let control: cache::Control = area.read_record(lookup(cache::CONTROL));
    let armed = &control.armed_words[..(control.armed as usize).min(control.armed_words.len())];
    let word = |offset: u64| match armed.iter().find(|w| u64::from(w[0]) == offset) {
        Some(&[_, original]) if u32::from_le_bytes(area.read::<4>(cache::CODE_OFFSET + offset as usize)) == exec::STOP_WORD => original,
        _ => u32::from_le_bytes(area.read::<4>(cache::CODE_OFFSET + offset as usize)),
    };
    let blocks = placed_blocks(area);
    let mut linked = HashMap::new();
    let mut points = 0;
    for b in &blocks {
        for p in b.links.iter().filter(|p| p.offset != 0) {
            points += 1;
            let at = u64::from(b.code) + u64::from(p.offset);
            let w = word(at);
            let bad = || format!("link point {:#x} of the block at {:#x}: word {w:#010x}", p.offset, b.rip);
            if w == p.word {
                continue;
            }
            if w & 0xfc00_0000 != 0x1400_0000 {
                return Err(bad());
            }
            let to = at.wrapping_add((((w << 6) as i32 >> 6) as i64 * 4) as u64);
            match blocks.iter().position(|t| t.flags & cache::SYSTEM == 0 && u64::from(t.code) + u64::from(exec::ENTRY_OFFSET) == to) {
                Some(t) if blocks[t].rip == p.target && blocks[t].context == b.context => linked.insert((b.number, p.offset), t),
                _ => return Err(bad()),
            };
        }
    }
    let records: Vec<cache::LinkRecord> = (0..control.links as usize).map(|i| link_record(area, i)).collect();
    // Each record also holds the point's admitted first word.
    let described = records.iter().all(|r| {
        let Some(b) = blocks.get(r.source as usize) else { return false };
        let point = b.links.iter().find(|p| p.offset == r.offset && p.offset != 0);
        linked.get(&(r.source, r.offset)) == Some(&(r.target as usize))
            && point.is_some_and(|p| p.word == r.original && blocks.get(r.target as usize).is_some_and(|t| t.rip == p.target))
    });
    if !described || records.len() != linked.len() {
        return Err(format!("{} link records for {} linked points", records.len(), linked.len()));
    }
    Ok((points, linked.len() as u64))
}

/// Why execution stopped.
#[derive(Debug)]
pub(crate) enum Stop {
    /// RIP reached the caller's stop address at a block boundary.
    Reached,
    /// The instruction fetch at RIP faults in a way the resident cannot deliver (`Fault::Unsupported`); #PF and #GP from
    /// a fetch are delivered (X1a, [`exec::REPLY_FETCH_FAULT`]).
    FetchFault(Fault),
    /// RIP reached a handler of the test driver's logging IDT ([`Requests::handlers`]): an exception was delivered there.
    Logged,
    /// fexc translated nothing at RIP; its end reason (e.g. `system_instruction`).
    NoCode(String),
    /// The resident stopped the VM: `State::stop` (0 for a non-execution stop) and the shared status/detail.
    Resident { stop: u64, status: u64, detail: u64 },
    /// The watchdog forced the vCPU out (imprecise: the bank is not current) at native `pc`: in generated code (`el0`: a
    /// guest loop ran past the budget) or in the resident (EL1: a resident wait, e.g. for a peer's shadow reset, never
    /// ended); `asked` = the peers whose `SHADOWS` request was still pending then (bit p: vCPU p).
    Watchdog { pc: u64, el0: bool, asked: u8 },
    /// A peer's failure cancelled the run (imprecise when the vCPU was forced out).
    Cancelled,
    /// L4b: a port write asked the platform to power off (PM1a_CNT SLP_EN with `_S5`'s SLP_TYP, `pm::POWEROFF`) or to
    /// reset (the FADT reset register, `pm::RESET`); the write was made, RIP is at the writing instruction.
    Power(u8),
}

/// One `fexc --serve` process (protocol in tools/fexc/fexc.cpp): length-prefixed requests and replies on its stdin/stdout.
struct Server {
    child: Child,
    stdin: ChildStdin,
    stdout: ChildStdout,
}
impl Server {
    fn start(fexc: &Path) -> Result<Self> {
        // One spawn at a time in the process. On macOS, std makes a child's pipes with `pipe` and only then marks them
        // close-on-exec, so a server spawned by another owner in between inherits this one's pipe end: a server holding
        // a killed sibling's stdout hides its death (no EOF) until the holder exits, and the request waits for its
        // deadline or the peers' watchdogs (seen with `lsof` in the compile-server-kill runs).
        static SPAWN: std::sync::Mutex<()> = std::sync::Mutex::new(());
        let _spawn = SPAWN.lock().unwrap_or_else(|e| e.into_inner());
        let mut child = Command::new(fexc)
            .arg("--serve")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .map_err(|e| format!("fexc --serve: {e}"))?;
        let (stdin, stdout) = (child.stdin.take().unwrap(), child.stdout.take().unwrap());
        Ok(Self { child, stdin, stdout })
    }
    /// The server's reply to one request body: (fexc's exit code for it, JSON report or error text, object); a
    /// `TimedOut` error when the whole reply has not arrived by `deadline`. (The request itself is written without a
    /// deadline: a body is at most a few KiB, which the pipe takes whether or not the server reads.)
    fn request(&mut self, body: &[u8], deadline: Instant) -> std::io::Result<(i32, Vec<u8>, Vec<u8>)> {
        self.stdin.write_all(&(body.len() as u32).to_le_bytes())?;
        self.stdin.write_all(body)?;
        let mut word = [0; 4];
        self.read_by(&mut word, deadline)?;
        let mut reply = vec![0; u32::from_le_bytes(word) as usize];
        self.read_by(&mut reply, deadline)?;
        let field = |at: usize| u32::from_le_bytes(reply[at..at + 4].try_into().unwrap()) as usize;
        if reply.len() < 12 || reply.len() != 12 + field(4) + field(8) {
            return Err(std::io::Error::other("malformed fexc reply"));
        }
        let (code, json_len) = (field(0) as i32, field(4));
        let object = reply.split_off(12 + json_len);
        let json = reply.split_off(12);
        Ok((code, json, object))
    }
    /// Fills `buf` from the server's stdout, waiting (poll) at most until `deadline`.
    fn read_by(&mut self, buf: &mut [u8], deadline: Instant) -> std::io::Result<()> {
        #[repr(C)]
        struct PollFd {
            fd: i32,
            events: i16,
            revents: i16,
        }
        unsafe extern "C" {
            fn poll(fds: *mut PollFd, count: u32, timeout_ms: i32) -> i32;
        }
        const POLLIN: i16 = 1;
        let mut done = 0;
        while done < buf.len() {
            let left = deadline.saturating_duration_since(Instant::now());
            let mut fd = PollFd { fd: std::os::fd::AsRawFd::as_raw_fd(&self.stdout), events: POLLIN, revents: 0 };
            match unsafe { poll(&mut fd, 1, left.as_micros().div_ceil(1000).min(i32::MAX as u128) as i32) } {
                0 => return Err(std::io::ErrorKind::TimedOut.into()),
                n if n < 0 => {
                    let e = std::io::Error::last_os_error();
                    if e.kind() != std::io::ErrorKind::Interrupted {
                        return Err(e);
                    }
                }
                _ => match self.stdout.read(&mut buf[done..])? {
                    0 => return Err(std::io::ErrorKind::UnexpectedEof.into()),
                    n => done += n,
                },
            }
        }
        Ok(())
    }
    /// A failed request's error, naming the server and, if it has exited, how (its pipes close as it exits, so a dead
    /// server is normally reaped here at once; wait briefly for the exit status).
    fn failed(&mut self, e: std::io::Error) -> String {
        let pid = self.child.id();
        for _ in 0..100 {
            match self.child.try_wait() {
                Ok(Some(status)) => return format!("fexc --serve (pid {pid}) exited ({status}): {e}"),
                Ok(None) => std::thread::sleep(Duration::from_millis(1)),
                Err(_) => break,
            }
        }
        format!("fexc --serve (pid {pid}) failed, still running: {e}")
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// What fexc answered for one request (errors are not kept): an object, a system instruction at the RIP (decoded,
/// [`crate::system`]), or no code with fexc's end reason.
enum Compiled {
    Object(Vec<u8>),
    System(exec::System),
    NoCode(String),
}
/// What [`Compiler::compile`] gives the service loop.
pub(crate) enum Code {
    Block(admission::Admitted),
    System(exec::System),
    None(String),
}

/// fexc for one vCPU's feature set: one persistent `fexc --serve` process (started at the first compile) and a cache of its
/// answers keyed by the whole request (arguments: RIP, profile, block limit, features; and the guest bytes), so a repeated
/// request is not compiled again. Objects are byte-identical to the one-shot CLI's.
pub(crate) struct Compiler {
    fexc: PathBuf,
    server: Option<Server>,
    supported: u64,
    /// Block size limit in guest instructions (fexc's own limit when `None`).
    max_instructions: Option<u32>,
    cache: HashMap<Vec<u8>, Compiled>,
    /// Requests that reached fexc.
    pub(crate) compiles: u64,
    /// Requests answered from the cache.
    pub(crate) hits: u64,
    /// Test drivers only: rewrites each admitted object before it is staged, to check the resident's run-time defences
    /// against code admission refuses.
    pub(crate) tamper: Option<fn(&mut [u8])>,
    /// How long one request may wait for the server's whole reply (the first one includes the server's start); then the
    /// server is killed and the run fails with a host error ([`COMPILE_DEADLINE`] by default).
    pub(crate) deadline: Duration,
    /// The longest request so far (reply received), for the deadline's margin.
    pub(crate) slowest: Duration,
    /// Test drivers only: once `.0` requests have reached the server, it gets signal `.1` before the next (SIGKILL: the
    /// server dies mid-run; SIGSTOP: it stays alive and never answers). The run must fail with the cause.
    pub(crate) signal_at: Option<(u64, i32)>,
    /// Admission's code-shape debug check ([`admission`]); off unless the caller sets it (the program driver's
    /// `--code-shape`).
    pub(crate) shape: bool,
    /// The vCPU's controls gate SSE (I5a: CR0.EM, CR0.TS or no CR4.OSFXSR, [`sse_gated`]): fexc cuts every SSE form as a
    /// system instruction, which the resident raises as #UD or #NM. Set per request (part of the request, so of the cache key).
    pub(crate) sse_gated: bool,
    /// The vCPU's controls gate the VEX AVX forms (I8: no CR4.OSXSAVE, XCR0[2:1] != 11b, or CR0.TS, [`avx_gated`]): fexc cuts
    /// them as system instructions, which the resident raises as #UD or #NM. Set per request, like `sse_gated`.
    pub(crate) avx_gated: bool,
    /// The vCPU runs under alignment checking (X2: CR0.AM, RFLAGS.AC, CPL 3): fexc cuts every BT* memory form as a system
    /// instruction, which the resident checks for #AC and then steps compiled without it (`State::plain_step`).
    pub(crate) alignment_gated: bool,
    /// The vCPU's x87 gating and rounding (I6, [`x87_args`]): fexc's `--x87-gated`, `--wait-gated` and `--x87-fpcr` arguments.
    pub(crate) x87: Vec<String>,
}
/// fexc's x87 arguments for `cpu` (I6; SDM Vol.3 Table 2-2): every x87 form #NM under CR0.EM or CR0.TS, WAIT/FWAIT under CR0.TS
/// with CR0.MP; x87 arithmetic under its own FPCR when FCW.RC or MXCSR's flush-to-zero/DAZ differ ([`exec::x87_fpcr`]). The
/// same state is part of `Cpu::code_context`.
pub(crate) fn x87_args(cpu: &exec::Cpu) -> Vec<String> {
    let mut args = vec![];
    if cpu.cr0 & (exec::CR0_EM | exec::CR0_TS) != 0 {
        args.push("--x87-gated".into());
    }
    if cpu.cr0 & (exec::CR0_TS | exec::CR0_MP) == exec::CR0_TS | exec::CR0_MP {
        args.push("--wait-gated".into());
    }
    match exec::x87_fpcr(cpu.fcw, cpu.mxcsr) {
        0 => {}
        fpcr => args.extend(["--x87-fpcr".into(), format!("{fpcr:#x}")]),
    }
    args
}
/// Whether `cpu`'s controls gate SSE instructions (SDM Vol.3 Table 2-2, Vol.2 2.5: #UD with CR0.EM or without CR4.OSFXSR,
/// #NM with CR0.TS). Blocks are made under one gating state: every CR0/CR4 change drops the vCPU's code.
pub(crate) fn sse_gated(cpu: &exec::Cpu) -> bool {
    cpu.cr0 & (exec::CR0_EM | exec::CR0_TS) != 0 || cpu.cr4 & exec::CR4_OSFXSR == 0
}
/// Whether `cpu`'s controls gate the VEX AVX forms (SDM Vol.2 2.8: #UD without CR4.OSXSAVE or with XCR0[2:1] != 11b, #NM
/// with CR0.TS; CR0.EM and CR4.OSFXSR do not apply). XCR0 changes by XSETBV without a flush: [`exec::Cpu::code_context`] keys it.
pub(crate) fn avx_gated(cpu: &exec::Cpu) -> bool {
    !cpu.avx_enabled() || cpu.cr0 & exec::CR0_TS != 0
}
/// The default [`Compiler::deadline`]: a large multiple of the slowest compile measured on the regression sets
/// (S3h: at most 13.4 ms, server start included, over 6,080 vCPU runs of the corpora at N = 4 and N = 2 with block size 1).
pub(crate) const COMPILE_DEADLINE: Duration = Duration::from_secs(30);
impl Compiler {
    pub(crate) fn new(fexc: &Path, cpu: &Vcpu, max_instructions: Option<u32>) -> Result<Self> {
        let mut id = [0; admission::ID_REGISTERS.len()];
        for (value, (name, reg)) in id.iter_mut().zip(admission::ID_REGISTERS) {
            *value = cpu.sys(reg).map_err(|e| format!("{name}: {e}"))?;
        }
        Ok(Self::with_features(fexc, admission::supported_features(&id), max_instructions))
    }
    fn with_features(fexc: &Path, supported: u64, max_instructions: Option<u32>) -> Self {
        Self { fexc: fexc.into(), server: None, supported, max_instructions, cache: HashMap::new(), compiles: 0, hits: 0, tamper: None, deadline: COMPILE_DEADLINE, slowest: Duration::ZERO, signal_at: None,
               shape: false, sse_gated: false, avx_gated: false, alignment_gated: false, x87: vec![] }
    }
    /// The admitted block for `bytes` at `rip` (only its first instruction if `step`); else the system instruction at
    /// `rip` the resident executes, or fexc's end reason when it produced no code.
    fn compile(&mut self, bytes: &[u8], rip: u64, step: bool) -> Result<Code> {
        let mut args = vec!["--profile".to_string(), "slice1".into(), "--rip".into(), format!("{rip:#x}")];
        if let Some(n) = if step { Some(1) } else { self.max_instructions } {
            args.extend(["--max-insts".into(), n.to_string()]);
        }
        for feature in admission::feature_overrides(self.supported) {
            args.extend(["--feature".into(), feature]);
        }
        if self.sse_gated {
            args.push("--sse-gated".into());
        }
        if self.avx_gated {
            args.push("--avx-gated".into());
        }
        if self.alignment_gated {
            args.push("--alignment-gated".into());
        }
        args.extend(self.x87.iter().cloned());
        let args: Vec<u8> = args.iter().flat_map(|a| a.bytes().chain([0])).collect();
        let mut body = (args.len() as u32).to_le_bytes().to_vec();
        body.extend(args);
        body.extend(bytes);
        if !self.cache.contains_key(&body) {
            if self.server.is_none() {
                self.server = Some(Server::start(&self.fexc)?);
            }
            let server = self.server.as_mut().unwrap();
            if let Some((_, signal)) = self.signal_at.filter(|s| s.0 == self.compiles) {
                unsafe extern "C" {
                    fn kill(pid: i32, signal: i32) -> i32;
                }
                unsafe { kill(server.child.id() as i32, signal) };
            }
            let began = Instant::now();
            let (code, json, object) = match server.request(&body, began + self.deadline) {
                Ok(reply) => reply,
                Err(e) if e.kind() == std::io::ErrorKind::TimedOut => {
                    let _ = server.child.kill();
                    return Err(format!("{}: no reply within {:?}, killed", server.failed(e), self.deadline));
                }
                Err(e) => return Err(server.failed(e)),
            };
            self.slowest = self.slowest.max(began.elapsed());
            self.compiles += 1;
            let compiled = match code {
                0 => Compiled::Object(object),
                1 => {
                    let json = crate::json::parse(&String::from_utf8_lossy(&json))?;
                    let reason = end_reason(&json).ok_or("fexc report without guest.end.reason")?;
                    let decoded = |(map, opcode)| {
                        crate::system::decode(bytes, map, opcode).or_else(|| crate::system::lock_ud(bytes)).or_else(|| crate::system::vex_ud(bytes))
                    };
                    // An SSE form cut by its gating (I5a): no operand matters, the resident raises #UD or #NM.
                    // I6: an x87 form or WAIT (primary d8..df, 9b) cut by its gating: #NM. I8: a VEX form (fexc's map 8): #UD or #NM.
                    let gated = |(map, op): (u8, u8)| {
                        let x87 = map == 0 && matches!(op, 0xd8..=0xdf | 0x9b);
                        let kind = if x87 { exec::system::X87_GATED } else if map == 8 { exec::system::AVX_GATED } else { exec::system::SSE_GATED };
                        Some(exec::System { kind, length: 1, ..exec::System::NONE })
                    };
                    let gated_cut = json.get("guest").and_then(|g| g.get("end")).and_then(|e| e.get("cut_gated")).is_some();
                    match system_cut(&json, rip).and_then(|cut| if gated_cut { gated(cut) } else { decoded(cut) }) {
                        Some(s) if reason == "system_instruction" => Compiled::System(s),
                        // Longer than 15 bytes with all of its first 15 fetched: #GP(0) (a fetch fault was `partial_decode`).
                        _ if reason == "instruction_too_long" => Compiled::System(exec::System { kind: exec::system::TOO_LONG, length: 1, ..exec::System::NONE }),
                        _ => Compiled::NoCode(reason.into()),
                    }
                }
                _ => return Err(format!("fexc failed at {rip:#x}: {}", String::from_utf8_lossy(&json))),
            };
            // ponytail: wholesale reset at a size cap; a real eviction policy when runs outgrow it
            if self.cache.len() >= 1 << 16 {
                self.cache.clear();
            }
            self.cache.insert(body.clone(), compiled);
        } else {
            self.hits += 1;
        }
        match &self.cache[&body] {
            Compiled::Object(object) => {
                let mut admitted = admission::admit(object, rip, bytes, self.supported, self.shape)?;
                if let Some(tamper) = self.tamper {
                    tamper(&mut admitted.object);
                }
                Ok(Code::Block(admitted))
            }
            Compiled::System(s) => Ok(Code::System(*s)),
            Compiled::NoCode(reason) => Ok(Code::None(reason.clone())),
        }
    }
}
/// `guest.end.reason` of fexc's report.
fn end_reason(json: &crate::json::Value) -> Option<&str> {
    json.get("guest")?.get("end")?.get("reason")?.as_str()
}
/// The opcode map and opcode of the instruction fexc cut the block before, if that is the first one (at `rip`).
fn system_cut(json: &crate::json::Value, rip: u64) -> Option<(u8, u8)> {
    let end = json.get("guest")?.get("end")?;
    let number = |key: &str| match end.get(key)? {
        crate::json::Value::Number(n) => n.parse().ok(),
        _ => None,
    };
    let at = u64::from_str_radix(end.get("rip")?.as_str()?.strip_prefix("0x")?, 16).ok()?;
    (at == rip).then_some((number("cut_opcode_map")?, number("cut_opcode")?))
}

/// The guest bytes at a RIP, copied from the guest-physical pages the resident walked for the request.
struct Fetched {
    bytes: Vec<u8>,
    /// They end at the end of RIP's page and the next page was not walked yet: more may follow.
    more: bool,
    /// The fetch fault of the next page, when its walk failed (the block is cut before it).
    next_fault: Option<Fault>,
}
/// The bytes at `state.cpu.rip` up to the end of its page, continuing through the next page when the resident walked
/// it, never past `limit` (when it lies ahead); or the vCPU's stop, when the walk of RIP's page failed.
fn fetched(area: Area, state: &State, limit: Option<u64>) -> Result<std::result::Result<Fetched, Stop>> {
    let ram = area.ram;
    let rip = state.cpu.rip;
    let fault = exec::fetch::decode(state.fetch_fault);
    if state.fetch[0] == u64::MAX {
        return match fault {
            Some(fault) => Ok(Err(Stop::FetchFault(fault))),
            None => Err(format!("code at {rip:#x} is not write-back guest RAM")),
        };
    }
    // Bytes of [va, the end of its page), cut at the limit when it lies inside; and whether it did.
    let span = |va: u64| {
        let rest = 4096 - (va & 4095);
        match limit {
            Some(l) if l > va && l - va < rest => (l - va, true),
            _ => (rest, false),
        }
    };
    let copy = |gpa: u64, length: u64| -> Result<Vec<u8>> {
        let mut chunk = vec![0; length as usize];
        if !ram.copy_out(gpa, &mut chunk) {
            return Err(format!("code at {rip:#x} is outside guest RAM"));
        }
        Ok(chunk)
    };
    let (length, cut) = span(rip);
    let mut bytes = copy(state.fetch[0] | rip & 4095, length)?;
    // The last page of the address space has no next page.
    let next = rip.checked_add(length).filter(|_| !cut);
    if let (Some(va), true) = (next, state.fetch[1] != u64::MAX) {
        bytes.extend(copy(state.fetch[1], span(va).0)?);
    }
    let walked = state.fetch[1] != u64::MAX || state.fetch_fault[0] != exec::fetch::NONE;
    Ok(Ok(Fetched { bytes, more: next.is_some() && !walked, next_fault: fault }))
}

/// Host exits of one [`run`].
#[derive(Default)]
pub(crate) struct Requests {
    /// Translation requests answered with a block, and the distinct RIPs among them (single-step requests not included).
    pub(crate) blocks: u64,
    pub(crate) rips: std::collections::HashSet<u64>,
    /// Test drivers only: called at each reply but a park, with the vCPU stopped, the reply (`State::reply`) and its block
    /// or decode in place but not yet seen by the resident, until it returns true (it corrupted something, possibly the
    /// reply kind); then `tampered` is set. Checks the resident's run-time defences.
    pub(crate) tamper: Option<fn(Area, &mut State) -> bool>,
    /// The same for device replies (M3), only.
    pub(crate) tamper_device: Option<fn(Area, &mut State) -> bool>,
    pub(crate) tampered: bool,
    /// Test drivers only: RIPs whose one-byte instruction is executed as a CPL change to the value
    /// ([`exec::system::SET_CPL`]); blocks are fetched up to each, so it is reached at a block boundary.
    pub(crate) switches: std::collections::BTreeMap<u64, u64>,
    /// Test drivers only: the handler range of the driver's logging IDT; reaching it ends the run ([`Stop::Logged`]) before
    /// anything there is fetched or compiled.
    pub(crate) handlers: Option<std::ops::Range<u64>>,
    /// Test drivers only (X2 event sources): RIPs whose one-byte instruction (a `nop`) posts an event into the vCPU's
    /// record ([`post`]: a vector, or [`NMI`]) and rings its doorbell (`doorbell`), then completes as
    /// [`exec::system::EVENT_HOOK`]; blocks are fetched up to each, as for `switches`.
    pub(crate) posts: std::collections::BTreeMap<u64, u64>,
    pub(crate) doorbell: Option<crate::vm::Doorbell>,
    /// M3: the VM's device ranges (shared by every vCPU thread; see [`crate::devices`]).
    pub(crate) devices: Option<std::sync::Arc<crate::devices::Devices>>,
    /// K2: system-instruction decodes answered ([`exec::REPLY_SYSTEM`], by kind; the test hooks not included), and
    /// entropy refills asked for ([`exec::REQUEST_ENTROPY`]).
    pub(crate) decodes: std::collections::BTreeMap<u32, u64>,
    pub(crate) entropy: u64,
}

/// Runs the vCPU (entered at the resident, see [`enter`]) until it stops; `stop_at` is a RIP at
/// which execution stops at a block boundary (blocks are fetched up to it, and
/// never cached at it, so reaching it is a miss). A vCPU that stays in the guest
/// for `watchdog` without a host exit is forced out ([`Stop::Watchdog`]); host
/// work between exits (compiles) is not counted. `cancelled` (a peer failed) ends the
/// run at the next host exit ([`Stop::Cancelled`]; the supervisor forces the vCPU out).
pub(crate) fn run(
    cpu: &Vcpu,
    area: Area,
    compiler: &mut Compiler,
    stop_at: Option<u64>,
    watchdog: Duration,
    requests: &mut Requests,
    stops: &Stops,
    cancelled: &AtomicBool,
) -> Result<Stop> {
    // The resident fetches nothing at the stop RIP (the run ends there).
    let mut state: State = area.read_record(exec::STATE_OFFSET);
    state.stop_at = stop_at.unwrap_or(u64::MAX);
    area.write_record(exec::STATE_OFFSET, state);
    // `true`: the vCPU enters the guest; `false`: it is back in the host. Dropping the sender ends the watchdog.
    let (running, events) = std::sync::mpsc::channel::<bool>();
    let id = cpu.id();
    // A halted vCPU (X2: parked in the VM by HLT) is idle, not stuck: the watchdog waits on; the driver decides when a
    // halted vCPU can never wake ([`end_when_halted`]). The word lives in the backing, which outlives this thread.
    let halted = event_word(area.memory, area.vcpu, std::mem::offset_of!(cache::Events, halted)) as *const AtomicU64 as usize;
    let dog = std::thread::spawn(move || {
        use std::sync::mpsc::RecvTimeoutError::Timeout;
        // SAFETY: see above (joined before `run` returns).
        let halted = unsafe { &*(halted as *const AtomicU64) };
        while let Ok(true) = events.recv() {
            let mut limit = watchdog;
            // Repeated while the vCPU stays in: a request racing its entry into `hv_vcpu_run` might not stop it.
            while events.recv_timeout(limit) == Err(Timeout) {
                if halted.load(SeqCst) != 0 {
                    continue;
                }
                exit_vcpus(&mut [id]);
                limit = Duration::from_millis(100);
            }
        }
    });
    let stop = serve(cpu, area, compiler, stop_at, requests, stops, &running, cancelled);
    // ponytail: a cancel the watchdog sent just as the guest exited stays pending on the vCPU and would end its next
    // `hv_vcpu_run` at once; harmless while a vCPU serves one run. Drain it (a zero-length run) if vCPUs get reused.
    drop(running);
    let _ = dog.join();
    stop
}
fn serve(
    cpu: &Vcpu,
    area: Area,
    compiler: &mut Compiler,
    stop_at: Option<u64>,
    requests: &mut Requests,
    stops: &Stops,
    running: &std::sync::mpsc::Sender<bool>,
    cancelled: &AtomicBool,
) -> Result<Stop> {
    // The host's own stop, once decided: the vCPU was told to park (it drops its shadows first).
    let mut parked = None;
    let v = area.vcpu;
    loop {
        if cancelled.load(SeqCst) {
            return Ok(Stop::Cancelled);
        }
        let _ = running.send(true);
        stops.in_host[v].store(false, SeqCst);
        let exit = cpu.run_until_exit();
        stops.in_host[v].store(true, SeqCst);
        let _ = running.send(false);
        let exit = match exit? {
            vm::Exit::Hvc(immediate) => immediate,
            vm::Exit::Cancelled if cancelled.load(SeqCst) => return Ok(Stop::Cancelled),
            vm::Exit::Cancelled => {
                let asked = (0..cache::MAX_VCPUS)
                    .filter(|&p| p != v && record_word(area.memory, p, None).load(SeqCst) & cache::SHADOWS != 0)
                    .fold(0, |m, p| m | 1 << p);
                return Ok(Stop::Watchdog { pc: cpu.reg(PC)?, el0: cpu.reg(CPSR)? & 0x1f == 0, asked });
            }
        };
        match exit {
            0x49 => {
                let s: Shared = area.read_record(SHARED_OFFSET);
                let state: State = area.read_record(exec::STATE_OFFSET);
                return match parked.take() {
                    Some(stop) if state.stop == exec::stop::PARKED => Ok(stop),
                    _ if state.stop == exec::stop::PARKED => Err("vCPU parked unasked".into()),
                    _ => Ok(Stop::Resident { stop: state.stop, status: s.status, detail: s.detail }),
                };
            }
            exec::TRANSLATE_HVC if parked.is_none() => {}
            other => return Err(format!("unexpected HVC immediate {other:#x}")),
        }
        refill_entropy(area);
        let mut state: State = area.read_record(exec::STATE_OFFSET);
        let step = match state.request {
            exec::REQUEST_TRANSLATE => false,
            exec::REQUEST_STEP => true,
            // M3: a device access, completed (or refused) once; never parked (test drivers may tamper with the reply).
            exec::REQUEST_DEVICE => {
                // A peer failed after this exit: the device is not accessed (the vCPU ends with the access undone).
                if cancelled.load(SeqCst) {
                    return Ok(Stop::Cancelled);
                }
                crate::devices::serve(area, &mut state, requests.devices.as_deref());
                if let Some(tamper) = requests.tamper_device {
                    if tamper(area, &mut state) {
                        (requests.tamper_device, requests.tampered) = (None, true);
                    }
                }
                area.write_record(exec::STATE_OFFSET, state);
                continue;
            }
            // L3 (amendment): a read reached a non-RAM page: claimed by a device, or unassigned (all-ones shadow, counted).
            exec::REQUEST_PAGE => {
                if cancelled.load(SeqCst) {
                    return Ok(Stop::Cancelled);
                }
                crate::devices::serve_page(area, &mut state, requests.devices.as_deref());
                area.write_record(exec::STATE_OFFSET, state);
                continue;
            }
                        // L3: a port access, made once on the port bus (never parked).
            exec::REQUEST_PORT => {
                if cancelled.load(SeqCst) {
                    return Ok(Stop::Cancelled);
                }
                crate::devices::serve_port(area, &mut state, requests.devices.as_deref());
                area.write_record(exec::STATE_OFFSET, state);
                // L4b: power off or reset ends the run here (the driver cancels the other vCPUs).
                match requests.devices.as_deref().map_or(0, |d| d.power()) {
                    0 => {}
                    power => return Ok(Stop::Power(power)),
                }
                continue;
            }
            // L4a: an EOI broadcast or an ExtINT's INTA cycle, at the interrupt controllers (never parked).
            exec::REQUEST_IRQ => {
                if cancelled.load(SeqCst) {
                    return Ok(Stop::Cancelled);
                }
                crate::irq::serve(area, &mut state, requests.devices.as_deref());
                area.write_record(exec::STATE_OFFSET, state);
                continue;
            }
            // K2: an RDRAND found the pool empty; it was topped up above.
            exec::REQUEST_ENTROPY => {
                requests.entropy += 1;
                state.reply = exec::REPLY_ENTROPY;
                area.write_record(exec::STATE_OFFSET, state);
                continue;
            }
            _ => return Err("translation exit without a request".into()),
        };
        state.reply = match translation(area, compiler, &mut state, step, stop_at, requests)? {
            Ok(reply) => reply,
            Err(stop) => {
                parked = Some(stop);
                exec::REPLY_PARK
            }
        };
        if let Some(tamper) = requests.tamper.filter(|_| parked.is_none()) {
            if tamper(area, &mut state) {
                (requests.tamper, requests.tampered) = (None, true);
            }
        }
        area.write_record(exec::STATE_OFFSET, state);
    }
}
/// Serves one request: stages the admitted block, or decodes the system instruction at RIP, and fills `state`'s
/// reply fields (returns the reply); or asks for the next page ([`exec::REPLY_NEXT_PAGE`]) when the bytes of RIP's
/// page ran out before the block or its first instruction ended; or returns the vCPU's stop.
fn translation(
    area: Area,
    compiler: &mut Compiler,
    state: &mut State,
    step: bool,
    stop_at: Option<u64>,
    requests: &mut Requests,
) -> Result<std::result::Result<u64, Stop>> {
    let rip = state.cpu.rip;
    if Some(rip) == stop_at {
        return Ok(Err(Stop::Reached));
    }
    if requests.handlers.as_ref().is_some_and(|h| h.contains(&rip)) {
        return Ok(Err(Stop::Logged));
    }
    // (A step request may be at one: X2 steps the instruction after a shadow-setting one.)
    if let Some(&cpl) = requests.switches.get(&rip) {
        state.system = exec::System { kind: exec::system::SET_CPL, length: 1, cr: cpl as u32, ..exec::System::NONE };
        return Ok(Ok(exec::REPLY_SYSTEM));
    }
    if let Some(&event) = requests.posts.get(&rip) {
        let _ = post(area.memory, area.vcpu, event, false);
        requests.doorbell.ok_or("an event source without a doorbell")?.ring(area.vcpu)?;
        state.system = exec::System { kind: exec::system::EVENT_HOOK, length: 1, ..exec::System::NONE };
        return Ok(Ok(exec::REPLY_SYSTEM));
    }
    // Blocks end at the stop RIP and at every switch and event source after RIP.
    let hooks = requests.switches.range(rip..).map(|(&r, _)| r).chain(requests.posts.range(rip..).map(|(&r, _)| r));
    let limit = hooks.chain(stop_at).filter(|&l| l > rip).min();
    let Fetched { bytes, more, next_fault } = match fetched(area, state, limit)? {
        Ok(fetched) => fetched,
        // RIP's page faults: the resident delivers it (`state.fetch_fault`); nothing is compiled.
        Err(Stop::FetchFault(Fault::Page { .. } | Fault::Noncanonical)) => return Ok(Ok(exec::REPLY_FETCH_FAULT)),
        Err(stop) => return Ok(Err(stop)),
    };
    compiler.sse_gated = sse_gated(&state.cpu);
    compiler.avx_gated = avx_gated(&state.cpu);
    compiler.x87 = x87_args(&state.cpu);
    compiler.alignment_gated = state.cpu.alignment_check() && !(step && state.plain_step != 0);
    let code = compiler.compile(&bytes, rip, step)?;
    let ran_out = match &code {
        Code::Block(b) => b.end_reason == natemu_abi::code_object::end_reason::END_OF_INPUT,
        Code::None(reason) => reason == "partial_decode" || reason == "end_of_input",
        Code::System(_) => false,
    };
    if more && ran_out {
        return Ok(Ok(exec::REPLY_NEXT_PAGE));
    }
    let block = match code {
        Code::Block(block) => block,
        // A step request at a system instruction (X2: after a shadow-setting instruction) gets its decode too.
        Code::System(s) => {
            *requests.decodes.entry(s.kind).or_default() += 1;
            state.system = s;
            return Ok(Ok(exec::REPLY_SYSTEM));
        }
        Code::None(reason) => {
            return Ok(Err(match next_fault {
                // The instruction at RIP continues into a page whose fetch faults: delivered by the resident.
                Some(Fault::Page { .. } | Fault::Noncanonical) if reason == "partial_decode" => return Ok(Ok(exec::REPLY_FETCH_FAULT)),
                Some(fault) if reason == "partial_decode" => Stop::FetchFault(fault),
                _ => Stop::NoCode(reason),
            }));
        }
    };
    // A step reply is exactly one instruction (the resident refuses anything else too).
    if step && (block.guest_insns != 1 || block.guest_bytes > 15) {
        return Err(format!("step at {rip:#x}: {} instructions, {} bytes", block.guest_insns, block.guest_bytes));
    }
    area.copy(exec::STAGING_OFFSET, &block.object);
    state.system = exec::System::NONE;
    if !step {
        requests.blocks += 1;
        requests.rips.insert(rip);
    }
    state.link_points = [exec::LinkPoint::NONE; exec::MAX_LINK_POINTS];
    state.link_points[..block.links.len()].copy_from_slice(&block.links);
    state.dispatch_exit = block.dispatch_exit.map_or(0, u64::from);
    state.rep_exits = block.links.iter().enumerate().filter(|(_, p)| Some(p.target) == block.rep_self).fold(0, |m, (k, _)| m | 1 << k);
    Ok(Ok(exec::REPLY_ADMITTED))
}

#[cfg(test)]
mod tests {
    use super::*;
    /// With `FEXC` set: a repeated request is answered from the cache, a different RIP, bytes or block limit is compiled.
    #[test]
    fn compiler_caches_exact_requests() {
        let Some(fexc) = std::env::var_os("FEXC") else { return };
        let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        let code = [0x48, 0x01, 0xd8, 0xc3];
        let object = |c: &mut super::Compiler, code: &[u8], rip| match c.compile(code, rip, false).unwrap() {
            Code::Block(b) => b.object,
            _ => panic!("no block"),
        };
        let first = object(&mut c, &code, 0x1000);
        assert_eq!((c.compiles, c.hits), (1, 0));
        assert_eq!(object(&mut c, &code, 0x1000), first);
        assert_eq!((c.compiles, c.hits), (1, 1));
        object(&mut c, &code, 0x2000);
        object(&mut c, &[0x48, 0x29, 0xd8, 0xc3], 0x1000);
        c.max_instructions = Some(1);
        object(&mut c, &code, 0x1000);
        assert_eq!((c.compiles, c.hits), (4, 1));
        // A system instruction at the RIP is decoded (and cached with fexc's answer); one later in the block cuts it.
        let mov_cr3 = [0x0f, 0x22, 0xd8, 0x90];
        let decoded = |c: &mut super::Compiler, rip| match c.compile(&mov_cr3, rip, false).unwrap() {
            Code::System(s) => Some(s),
            _ => None,
        };
        let s = decoded(&mut c, 0x3000).unwrap();
        assert_eq!((s.kind, s.cr, s.reg, s.length), (exec::system::MOV_TO_CR, 3, 0, 3));
        assert_eq!(decoded(&mut c, 0x3000), Some(s));
        assert_eq!((c.compiles, c.hits), (5, 2));
        assert!(matches!(c.compile(&[0xf4], 0x3000, false).unwrap(), Code::System(s) if s.kind == exec::system::HLT)); // X2
        // A system instruction the host does not decode (SYSENTER) is no code.
        assert!(matches!(c.compile(&[0x0f, 0x34], 0x3000, false).unwrap(), Code::None(r) if r == "system_instruction"));
        // Longer than 15 bytes (fexc series28): #GP(0) through the resident.
        let long = [&[0x66; 4][..], &[0x4a, 0xc7, 0x84, 0x83, 0, 1, 0, 0, 0, 0, 0, 0]].concat();
        assert!(matches!(c.compile(&long, 0x3000, false).unwrap(), Code::System(s) if s.kind == exec::system::TOO_LONG));
        assert!(matches!(c.compile(&[0x90, 0x0f, 0x22, 0xd8], 0x3000, false).unwrap(), Code::Block(_)));
    }

    #[test]
    fn one_to_four_vcpus() {
        for n in [0, cache::MAX_VCPUS + 1] {
            let e = super::prepare("/nonexistent", cache::CODE_BYTES, n, 4 << 20).err().unwrap();
            assert!(e.contains(&format!("{n} vCPUs")), "{e}");
        }
        assert_eq!(cache::MAX_VCPUS, 4);
        // M1: guest RAM is a nonzero 2 MiB multiple up to 3 GiB.
        for ram in [0, 3 << 20, exec::MAX_GUEST_RAM_BYTES + (2 << 20)] {
            let e = super::prepare("/nonexistent", cache::CODE_BYTES, 1, ram).err().unwrap();
            assert!(e.contains("guest RAM"), "{e}");
        }
    }

    /// The bytes come only from the pages the resident walked: RIP's page to its end (more may follow until the next
    /// page is walked or its walk failed), on through the next page, never past the limit; a failed walk of RIP's page
    /// is the fetch fault stop.
    #[test]
    fn fetched_bytes_follow_the_resident_walk() {
        let memory = Memory::with_size(exec::BACKING_BYTES + (2 << 20)).unwrap();
        let ram = unsafe { GuestRam::new(0, 2 << 20, memory.0 as u64 + exec::BACKING_BYTES as u64) };
        assert!(ram.copy_in(0x5ff0, &[0xaa; 16]) && ram.copy_in(0x9000, &[0xbb; 16]));
        let area = Area { memory: &memory, vcpu: 0, vcpus: 1, epoch: 0, base: 0, ram };
        let mut s = State::new(0, Cpu { gpr: [0; 16], rip: 0x1ff0, rflags: 2, cr0: 0, cr3: 0, cr4: 0, efer: 0, cpl: 3, fs_base: 0, gs_base: 0, cr2: 0, cr8: 0, dr: exec::DR_RESET, seg: exec::flat_segments(3), ldtr: exec::LDTR_RESET, tr: exec::TR_RESET, gdtr: exec::TABLE_RESET, idtr: exec::TABLE_RESET, mov_ss: 0, xmm: [[0; 2]; 16], mxcsr: exec::MXCSR_RESET, fcw: exec::FCW_INIT, xcr0: 1 });
        s.fetch = [0x5000, u64::MAX];
        let f = fetched(area, &s, None).unwrap().unwrap();
        assert_eq!((f.bytes, f.more, f.next_fault), (vec![0xaa; 16], true, None));
        let f = fetched(area, &s, Some(0x1ff8)).unwrap().unwrap();
        assert_eq!((f.bytes.len(), f.more), (8, false));
        s.fetch_fault = exec::fetch::encode(Fault::Page { address: 0x2000, error: 0x15 });
        let f = fetched(area, &s, None).unwrap().unwrap();
        assert_eq!((f.more, f.next_fault), (false, Some(Fault::Page { address: 0x2000, error: 0x15 })));
        (s.fetch, s.fetch_fault) = ([0x5000, 0x9000], [exec::fetch::NONE; 3]);
        let f = fetched(area, &s, Some(0x2004)).unwrap().unwrap();
        assert_eq!((&f.bytes[14..], f.more), (&[0xaa, 0xaa, 0xbb, 0xbb, 0xbb, 0xbb][..], false));
        (s.fetch, s.fetch_fault) = ([u64::MAX; 2], exec::fetch::encode(Fault::Noncanonical));
        assert!(matches!(fetched(area, &s, None), Ok(Err(Stop::FetchFault(Fault::Noncanonical)))));
    }

    #[test]
    fn host_writes_to_guest_ram_flush_every_vcpu() {
        let memory = Memory::with_size(exec::BACKING_BYTES).unwrap();
        guest_ram_written(&memory);
        assert!((0..cache::MAX_VCPUS).all(|v| record_word(&memory, v, None).load(SeqCst) & cache::FLUSH != 0));
    }

    #[test]
    fn end_reason_is_read_from_fexc_json() {
        let json = r#"{"guest":{"detail":{"reason":"no"},"end":{"rip":"0x10","reason":"system_instruction","detail":"hlt"}}}"#;
        let json = crate::json::parse(json).unwrap();
        assert_eq!(super::end_reason(&json), Some("system_instruction"));
        assert_eq!(super::end_reason(&crate::json::parse("{}").unwrap()), None);
        let cut = r#"{"guest":{"end":{"rip":"0x10","reason":"system_instruction","detail":"x","cut_opcode_map":1,"cut_opcode":34}}}"#;
        let cut = crate::json::parse(cut).unwrap();
        assert_eq!((super::system_cut(&cut, 0x10), super::system_cut(&cut, 0x11)), (Some((1, 0x22)), None));
    }
}
