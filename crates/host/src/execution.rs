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
use std::io::Read;
use std::path::{Path, PathBuf};
use std::os::unix::process::CommandExt;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering::{Relaxed, SeqCst}};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// The execution mode's backing for `n` vCPUs. Block 0 holds vCPU 0's window, its lookup area, the global area, its
/// metadata arena and its code area (the `exec`/`cache` offsets); block v (`v * stride` further) holds vCPU v's own
/// window, lookup area, metadata arena and code area at the same offsets and leaves the global area's range unused
/// (never mapped, so never backed): every vCPU maps block 0's. Guest RAM (M1: `ram_bytes`, chosen at prepare) follows
/// the last block, at `n * stride`; only the pages the guest or the host touch are ever backed.
pub struct Backing {
    /// Shared (not borrowed) by whatever outlives a move of the Backing: the chipset's LAPIC view ([`crate::irq`]) and the
    /// VM ([`Machine`]). Not public: replacing it would leave `ram` pointing into a mapping that is gone.
    pub(crate) memory: Arc<Memory>,
    pub(crate) n: usize,
    stride: usize,
    /// Guest-physical RAM through the host's alias: valid while `memory` is mapped, so never handed out by value in safe
    /// code (`GuestRam` is `Copy`: a copy would outlive the backing; R2 review).
    pub(crate) ram: GuestRam,
    /// One CNTVCT sample (the host's `mach_absolute_time`: every vCPU's virtual counter has offset 0) taken when the VM
    /// was prepared: each vCPU's TSC starts from it, so all start near 0 and agree.
    epoch: u64,
}
impl Backing {
    /// The backing's mapping (shared: whoever holds a clone keeps it mapped).
    pub fn memory(&self) -> &Arc<Memory> {
        &self.memory
    }
    /// Guest RAM through the host's alias.
    ///
    /// # Safety
    /// The value (and every copy) must not be used after this backing is dropped: it addresses the backing's mapping.
    pub unsafe fn ram(&self) -> GuestRam {
        self.ram
    }
    /// vCPU `v`'s view.
    pub fn vcpu(&self, v: usize) -> Area<'_> {
        assert!(v < self.n);
        Area { memory: &self.memory, vcpu: v, vcpus: self.n, epoch: self.epoch, base: v * self.stride, ram: self.ram }
    }
}
/// One vCPU's own part of the backing: offsets are those of block 0 (`exec`/`cache`), relocated to its block.
#[derive(Clone, Copy)]
pub struct Area<'a> {
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
impl<'a> Area<'a> {
    pub fn memory(&self) -> &'a Memory {
        self.memory
    }
    pub fn vcpu(&self) -> usize {
        self.vcpu
    }
    pub fn epoch(&self) -> u64 {
        self.epoch
    }
    pub fn read_record<T: Copy>(&self, offset: usize) -> T {
        self.memory.read_record(self.base + offset)
    }
    pub(crate) fn read<const N: usize>(&self, offset: usize) -> [u8; N] {
        self.memory.read(self.base + offset)
    }
}
/// Raw writes into a vCPU's records and code cache, past admission and the resident's checks: test drivers only (the
/// `testing` feature; the `natemu` stub and library users never have it).
#[cfg(feature = "testing")]
impl<'a> Area<'a> {
    pub fn write_record<T>(&self, offset: usize, value: T) {
        self.memory.write_record(self.base + offset, value)
    }
    pub fn copy(&self, offset: usize, bytes: &[u8]) {
        self.memory.copy(self.base + offset, bytes)
    }
    /// vCPU 0 of one, over the whole of `memory` as its block and its guest RAM (a driver's unit tests).
    pub fn over(memory: &'a Memory) -> Self {
        // SAFETY: `memory` is mapped for its length and outlives the area.
        Area { memory, vcpu: 0, vcpus: 1, epoch: 0, base: 0, ram: unsafe { GuestRam::new(0, memory.1 as u64, memory.0 as u64) } }
    }
}
#[cfg(not(feature = "testing"))]
impl Area<'_> {
    pub(crate) fn write_record<T>(&self, offset: usize, value: T) {
        self.memory.write_record(self.base + offset, value)
    }
    pub(crate) fn copy(&self, offset: usize, bytes: &[u8]) {
        self.memory.copy(self.base + offset, bytes)
    }
}

/// Backing for the execution mode with `n` vCPUs (1..=`cache::MAX_VCPUS`) and `ram_bytes` of guest RAM (a 2 MiB
/// multiple, at most `exec::MAX_GUEST_RAM_BYTES`): the resident loaded into each vCPU's window, each vCPU's private
/// address space and shared record in place; guest RAM is zero. Each vCPU's code cache holds `code_bytes` of generated
/// code (at most `cache::CODE_BYTES_MAX`).
pub fn prepare(resident: &str, code_bytes: usize, n: usize, ram_bytes: usize) -> Result<Backing> {
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
        // The guest-state page's EL0 alias (x28 of generated code), backed by the frame page (G1).
        guest_state_alias(&memory, base, base + exec::FRAME_OFFSET);
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
    Ok(Backing { memory: Arc::new(memory), n, stride, ram, epoch: unsafe { mach_absolute_time() } })
}
/// Sets the vCPU's initial architectural state (before it first runs).
pub fn set_cpu(area: Area, cpu: Cpu) {
    let mut state = State::new(area.vcpu as u64, cpu);
    state.vcpus = area.vcpus as u64;
    area.write_record(exec::STATE_OFFSET, state);
    // The TSC (L5, `natemu_abi::clock::tsc`) is the resident's CNTVCT scaled to TSC_HZ plus this offset: from the shared epoch.
    const _: () = assert!(natemu_abi::cpu_profile::TSC_HZ == 1_000_000_000); // ticks_ns scales like the resident
    let mut msrs = natemu_abi::cpu::reset_msrs(area.vcpu == 0);
    msrs[natemu_abi::cpu::msr(natemu_abi::cpu::MSR_TSC).unwrap()] = ticks_ns(area.epoch).wrapping_neg();
    // The virtual firmware's MTRRs cover the guest RAM this VM has (M1's run-time size, set at prepare).
    let control: cache::Control = area.read_record(cache::LOOKUP_OFFSET + cache::CONTROL);
    natemu_abi::cpu::firmware_mtrrs(&mut msrs, control.ram_bytes);
    area.write_record(exec::MSRS_OFFSET, msrs);
    area.write_record(exec::ENTROPY_OFFSET, exec::Entropy { taken: exec::ENTROPY_WORDS as u64, words: [0; exec::ENTROPY_WORDS] });
    refill_entropy(area);
    set_lapic(area, natemu_abi::lapic::RESET);
}
/// Sets the vCPU's local APIC record (X3) and the word peers match IPI destinations against, from its reset
/// IA32_APIC_BASE (before it first runs).
pub fn set_lapic(area: Area, l: natemu_abi::lapic::Lapic) {
    area.write_record(exec::LAPIC_OFFSET, l);
    let base = natemu_abi::cpu::reset_msrs(area.vcpu == 0)[natemu_abi::cpu::msr(natemu_abi::cpu::MSR_APIC_BASE).unwrap()];
    let word = natemu_abi::lapic::published(base, &l, area.vcpu as u64);
    event_word(area.memory, area.vcpu, std::mem::offset_of!(cache::Events, apic)).store(word, SeqCst);
}
/// The vCPU's local APIC record (X3; the resident's once it runs).
pub fn lapic(area: Area) -> natemu_abi::lapic::Lapic {
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
pub fn enter(cpu: &Vcpu, area: Area, doorbell: bool) -> Result<()> {
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
pub const NMI: u64 = 256;
/// Posts `event` (a vector, or [`NMI`]) into vCPU `v`'s event record (X2); the caller then rings its doorbell, which makes
/// it stop precisely and take the event when it is eligible. L4a: `level`, a level-triggered interrupt: its TMR bit is
/// set (else cleared) before the IRR bit, as the LAPIC accepts it (SDM Vol.3 11.8.4), so its EOI is broadcast. Returns
/// whether the target accepted it.
pub fn post(memory: &Memory, v: usize, event: u64, level: bool) -> bool {
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
    // Perf chunk 14: the post's time, for a `perf` resident's wake latency (kept only while 0: the first since it parked).
    if crate::perf::on() {
        let _ = event_word(memory, v, std::mem::offset_of!(cache::Events, rung)).compare_exchange(0, unsafe { mach_absolute_time() }, SeqCst, SeqCst);
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
pub fn park_state(memory: &Memory, v: usize) -> (u64, u64) {
    let halted = event_word(memory, v, std::mem::offset_of!(cache::Events, halted)).load(SeqCst);
    (halted, event_word(memory, v, std::mem::offset_of!(cache::Events, posts)).load(SeqCst))
}
/// A park state in which nothing but a new post can wake the vCPU: halted, its own timer unable to wake it, and every
/// post seen.
pub fn idle((halted, posts): (u64, u64)) -> bool {
    halted & 0xff == 1 && halted >> cache::HALTED_SEEN_SHIFT == posts & (u64::MAX >> cache::HALTED_SEEN_SHIFT)
}
/// Asks vCPU `v` to end its run when it is halted with no event it can take (`stop::HALTED`); the caller rings it.
pub fn end_when_halted(memory: &Memory, v: usize) {
    event_word(memory, v, std::mem::offset_of!(cache::Events, end)).store(1, SeqCst);
}
/// vCPU `area.vcpu`'s shadow resets so far (the resident's counter: evictions, shootdowns, control changes, halts).
pub fn shadow_resets(area: Area) -> u64 {
    global_u64(area.memory, cache::RESETS + 8 * area.vcpu).load(SeqCst)
}
/// K1: vCPU `area.vcpu`'s TLB events and revalidations (`cache::Control`), as a JSON object; with M2's data shadows by
/// memory type (`memory_types`: {"UC": n, ...}, the types with a non-zero count) and its UC accesses (`uncached`:
/// instructions run alone for one, the resident's own).
pub fn k1_stats(area: Area) -> String {
    let c: cache::Control = area.read_record(cache::LOOKUP_OFFSET + cache::CONTROL);
    const NAMES: [&str; 8] = ["UC", "WC", "2", "3", "WT", "WP", "WB", "UC-"];
    let types: Vec<String> = (0..8).filter(|&t| c.memory_types[t] != 0).map(|t| format!("\"{}\":{}", NAMES[t], c.memory_types[t])).collect();
    // Perf chunk 12: system instructions by kind ([count, transaction us]) and by the resident bucket that reached them.
    let kinds: Vec<String> = (0..128).filter(|&k| c.system_counts[k] != 0).map(|k| format!("\"{k:#x}\":[{},{}]", c.system_counts[k], ticks_ns(c.system_ticks[k]) / 1000)).collect();
    let from: Vec<String> = (0..32).filter(|&b| c.system_from[b] != 0).map(|b| format!("\"{}\":{}", natemu_abi::execution::perf::NAMES.get(b).unwrap_or(&"?"), c.system_from[b])).collect();
    format!(
        "{{\"cr3_loads\":{},\"global_flushes\":{},\"invlpgs\":{},\"revalidated\":{},\"remapped\":{},\"capacity_flushes\":{},\"killed\":{},\"unvalidated\":{},\"tlb_us\":{},\"invlpg_us\":{},\"page_faults\":{},\"page_fault_us\":{},\"scanned\":{},\"memory_types\":{{{}}},\"uncached\":{{\"steps\":{},\"resident\":{}}},\"l1_switches\":{},\"l1_clears\":{},\"flushes\":{},\"flush_us\":{},\"full\":{:?},\"used\":[{},{},{}],\"peak\":{:?},\"capacity\":[{},{},{}],\"reclaims\":[{},{}],\"reuse\":{{\"copies\":{},\"refused\":{:?},\"checks\":{}}},\"slot_writes\":{},\"kept_resets\":{},\"systems\":{{\"kinds\":{{{}}},\"from\":{{{}}},\"chained\":{}}}}}",
        c.cr3_loads, c.global_flushes, c.invlpgs, c.revalidated, c.remapped, c.capacity_flushes, c.killed, c.unvalidated,
        ticks_ns(c.tlb_ticks) / 1000, ticks_ns(c.invlpg_ticks) / 1000, c.page_faults, ticks_ns(c.page_fault_ticks) / 1000, c.scanned,
        types.join(","),
        c.uncached_steps,
        c.uncached_resident,
        c.l1_switches,
        c.l1_clears,
        c.flushes,
        ticks_ns(c.flush_ticks) / 1000,
        c.full,
        c.code_used,
        c.meta_used,
        c.blocks,
        [c.peak[0].max(c.code_used), c.peak[1].max(c.meta_used), c.peak[2].max(c.blocks)],
        c.code_bytes,
        cache::META_BYTES,
        cache::MAX_BLOCKS,
        c.reclaims,
        c.reclaimed,
        c.reuse_copies,
        c.reuse_refused,
        c.reuse_checks,
        c.slot_writes,
        c.kept_resets,
        kinds.join(","),
        from.join(","),
        c.system_chained
    )
}
/// Guest RAM was written by the host (only while every vCPU is stopped): each vCPU drops its cached code before it
/// next runs guest code.
pub fn guest_ram_written(memory: &Memory) {
    for v in 0..cache::MAX_VCPUS {
        record_word(memory, v, None).fetch_or(cache::FLUSH, SeqCst);
    }
}
/// Precise stops from the host. A kick rings vCPU v's doorbell (its GIC SPI, from any thread, no host exit); the
/// resident's IRQ handler stops v at its next block boundary, drains its invalidation record and counts the stop
/// (`State::stops`, with its CNTVCT, which is the host's `mach_absolute_time`). A kick that finds v in the host or the
/// resident (interrupts masked) stays pending in the GIC and stops v as soon as it runs generated code again; a kick
/// while one is pending merges into it in the GIC. Peers' residents ring v's SGI directly (`cache::kick`), never the host.
pub struct Stops {
    kicks: Vec<Mutex<Kick>>,
    /// vCPU v is in the host (not in `hv_vcpu_run`). Accounting only (`Kick::in_host`, latency classes).
    in_host: Vec<AtomicBool>,
}
/// One vCPU's host kicks.
#[derive(Default, Clone)]
pub struct Kick {
    /// Kicks (doorbells rung), of which while the vCPU was in the host.
    pub issued: u64,
    pub in_host: u64,
    /// Nanoseconds spent in the `hv_gic_set_spi` calls.
    pub ring_ns: u64,
    /// Its doorbell was still pending when it ended (read before the VM goes).
    pub pending: bool,
    /// Per kick a stop followed, as the driver reported it ([`Stops::followed`]): nanoseconds from the ring to the
    /// resident's stop, and whether the kick found the vCPU in the host.
    pub latency: Vec<(u64, bool)>,
    /// The last ring: `mach_absolute_time`, in the host.
    rung: Option<(u64, bool)>,
    /// The vCPU's run ended (or it never ran): no kick rings it any more ([`Stops::end`]).
    ended: bool,
}
unsafe extern "C" {
    fn mach_absolute_time() -> u64;
}
/// Nanoseconds of `ticks` of `mach_absolute_time` (and CNTVCT: 24 MHz on Apple silicon).
pub fn ticks_ns(ticks: u64) -> u64 {
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
    /// Kicks of `backing`'s vCPUs.
    pub fn new(backing: &Backing) -> Self {
        Self { kicks: (0..backing.n).map(|_| Mutex::default()).collect(), in_host: (0..backing.n).map(|_| AtomicBool::new(true)).collect() }
    }
    /// Stops vCPU `v` of `machine` at its next block boundary; false (nothing rung) once its run [ended](Self::end).
    pub fn kick(&self, machine: &Machine, v: usize) -> Result<bool> {
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
    pub fn end(&self, v: usize) {
        self.kicks[v].lock().unwrap().ended = true;
    }
    pub fn ended(&self, v: usize) -> bool {
        self.kicks[v].lock().unwrap().ended
    }
    /// Whether vCPU `v`'s doorbell is still pending (rung, not acknowledged), asked under its kick mutex: `None` once
    /// its run [ended](Self::end).
    pub fn ringing(&self, machine: &Machine, v: usize) -> Result<Option<bool>> {
        let k = self.kicks[v].lock().unwrap();
        if k.ended {
            return Ok(None);
        }
        machine.ringing(v).map(Some)
    }
    /// Stops vCPU `v` (`area`) has taken (the resident's count).
    pub fn taken(&self, area: Area) -> u64 {
        area.read_record(exec::STATE_OFFSET + std::mem::offset_of!(State, stops))
    }
    /// A stop of `area`'s vCPU followed the last kick (a kicker that waits for it says so): its latency is kept.
    pub fn followed(&self, area: Area) {
        let mut k = self.kicks[area.vcpu].lock().unwrap();
        if let Some((at, in_host)) = k.rung.take() {
            // The resident writes its record field by field: the stop's time may land just after its count.
            let ticks = || area.read_record::<u64>(exec::STATE_OFFSET + std::mem::offset_of!(State, stop_ticks));
            let stop = (0..10_000).map(|_| ticks()).find(|&t| t >= at).unwrap_or(at);
            k.latency.push((ticks_ns(stop - at), in_host));
        }
    }
    /// Records whether vCPU `v`'s doorbell is still pending (after it and its kicker ended, before the VM goes).
    pub fn pending_at_end(&self, machine: &Machine, v: usize) -> Result<()> {
        let pending = machine.ringing(v)?;
        self.kicks[v].lock().unwrap().pending = pending;
        Ok(())
    }
    /// vCPU `v`'s accounting.
    pub fn report(&self, v: usize) -> Kick {
        self.kicks[v].lock().unwrap().clone()
    }
}

/// The vCPU's placed blocks, in placement order (read from its metadata arena).
pub fn placed_blocks(area: Area) -> Vec<cache::Block> {
    let control: cache::Control = area.read_record(cache::LOOKUP_OFFSET + cache::CONTROL);
    (0..control.blocks as usize)
        .map(|n| {
            let meta = u32::from_le_bytes(area.read::<4>(cache::LOOKUP_OFFSET + cache::INDEX + 4 * n));
            area.read_record(cache::META_OFFSET + meta as usize)
        })
        .collect()
}
/// The link record `i` (written by the resident's linker).
pub fn link_record(area: Area, i: usize) -> cache::LinkRecord {
    area.read_record(cache::LOOKUP_OFFSET + cache::LINKS + i * size_of::<cache::LinkRecord>())
}

/// Self-check of the vCPU's cache after a run (the contract's rule (d), independent of the resident): every link point of
/// every placed block holds its admitted word (the first word of its RIP move) or `b` to the entry of a placed block
/// keyed (target, the block's context), and the resident's link records describe exactly the linked ones. A word an
/// armed stop patched (a vCPU forced out with a stop armed) is judged by its word before the patch. Returns (link
/// points, linked).
pub fn check_links(area: Area) -> Result<(u64, u64)> {
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
pub enum Stop {
    /// RIP reached the caller's stop address at a block boundary.
    Reached,
    /// The instruction fetch at RIP faults in a way the resident cannot deliver (`Fault::Unsupported`); #PF and #GP from
    /// a fetch are delivered (X1a, [`exec::REPLY_FETCH_FAULT`]).
    FetchFault(Fault),
    /// The driver's [`Hook::at`] ended the run at RIP ([`At::Stop`], with its code), before anything there was fetched.
    Hooked(u32),
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

/// One `fexc --serve` process (protocol in tools/fexc/fexc.cpp): length-prefixed requests and replies through a mapping
/// shared with it ([`Channel`], perf chunk 21); its stdin and stdout carry nothing, but stdout's EOF says it died.
struct Server {
    child: Child,
    /// `None` once [`Server::finish`] closed it.
    stdin: Option<ChildStdin>,
    stdout: ChildStdout,
    /// [`Server::finish`] reaped it.
    reaped: bool,
    channel: Channel,
}
unsafe extern "C" {
    fn shm_open(name: *const std::ffi::c_char, flags: i32, ...) -> i32;
    fn shm_unlink(name: *const std::ffi::c_char) -> i32;
    fn ftruncate(fd: i32, length: i64) -> i32;
    fn mmap(address: *mut c_void, length: usize, prot: i32, flags: i32, fd: i32, offset: i64) -> *mut c_void;
    fn munmap(address: *mut c_void, length: usize) -> i32;
    fn close(fd: i32) -> i32;
    fn fcntl(fd: i32, cmd: i32, ...) -> i32;
    fn os_sync_wait_on_address_with_timeout(addr: *mut u8, value: u64, size: usize, flags: u32, clock: u32, timeout_ns: u64) -> i32;
    fn os_sync_wake_by_address_any(addr: *mut u8, size: usize, flags: u32) -> i32;
}
/// Perf chunk 21: a server's requests and replies, through a mapping it shares with the host (layout and protocol in
/// tools/fexc/fexc.cpp, `--channel`). The server can write any byte of it at any time, so the host reads nothing there
/// it has not bounds-checked and copied out first, and nothing it trusts lives there.
struct Channel {
    base: *mut u8,
    fd: i32,
    /// The last request's number.
    n: u32,
}
// The mapping is the channel's own; one thread uses it at a time (`&mut Server`).
unsafe impl Send for Channel {}
impl Channel {
    const BYTES: usize = 16 << 20;
    const REQUEST: usize = 4096;
    const REQUEST_CAP: usize = 1 << 20;
    const REPLY: usize = Self::REQUEST + Self::REQUEST_CAP;
    const REPLY_CAP: usize = Self::BYTES - Self::REPLY;
    /// The layout's magic (and version), "CHN1": the host writes it at 8 when it makes the mapping, the server checks it and
    /// writes it at 72 before it answers; the host checks that with every reply (fexc.cpp's kChanMagic).
    const MAGIC: u32 = 0x314e_4843;
    /// How long the host spins for a reply before it sleeps: 99.6% of chunk 21's round trips took at most 30 us. (The server
    /// spins 20 us for the next request; with only one side spinning the other's wake-up costs what the pipe did.)
    const SPIN: Duration = Duration::from_micros(30);
    /// A sleep's longest stretch: then the host checks whether the server died (and its deadline).
    const SLICE_NS: u64 = 5_000_000;
    /// A new mapping (close-on-exec: `shm_open` takes no O_CLOEXEC; its name is unlinked at once).
    fn new() -> Result<Self> {
        static COUNT: AtomicU64 = AtomicU64::new(0);
        const O_RDWR: i32 = 2;
        const O_CREAT: i32 = 0x200;
        const O_EXCL: i32 = 0x800;
        let name = std::ffi::CString::new(format!("/natemu.{}.{}", std::process::id(), COUNT.fetch_add(1, Relaxed))).unwrap();
        const F_SETFD: i32 = 2;
        const FD_CLOEXEC: i32 = 1;
        let fd = unsafe { shm_open(name.as_ptr(), O_RDWR | O_CREAT | O_EXCL, 0o600 as std::ffi::c_uint) };
        if fd < 0 {
            return Err(format!("fexc channel: shm_open: {}", std::io::Error::last_os_error()));
        }
        unsafe { shm_unlink(name.as_ptr()) };
        const PROT_RW: i32 = 3;
        const MAP_SHARED: i32 = 1;
        let base = match unsafe { fcntl(fd, F_SETFD, FD_CLOEXEC) == 0 && ftruncate(fd, Self::BYTES as i64) == 0 } {
            true => unsafe { mmap(std::ptr::null_mut(), Self::BYTES, PROT_RW, MAP_SHARED, fd, 0) as *mut u8 },
            false => usize::MAX as *mut u8,
        };
        if base as usize == usize::MAX {
            let e = std::io::Error::last_os_error();
            unsafe { close(fd) };
            return Err(format!("fexc channel: {e}"));
        }
        let channel = Self { base, fd, n: 0 };
        channel.word(8).store(Self::MAGIC, Relaxed);
        Ok(channel)
    }
    fn word(&self, at: usize) -> &std::sync::atomic::AtomicU32 {
        unsafe { &*(self.base.add(at) as *const std::sync::atomic::AtomicU32) }
    }
    /// Posts a request payload: copied in, its length, then its number in the request word (waking a sleeping server).
    fn post(&mut self, body: &[u8]) -> std::io::Result<()> {
        if body.is_empty() || body.len() > Self::REQUEST_CAP {
            return Err(std::io::Error::other(format!("a request of {} bytes does not fit the fexc channel", body.len())));
        }
        self.n = self.n.wrapping_add(1) & 0x7fff_ffff;
        unsafe { channel_copy(self.base.add(Self::REQUEST), body.as_ptr() as *mut u8, body.len(), true) };
        self.word(4).store(body.len() as u32, Relaxed);
        self.publish();
        Ok(())
    }
    /// The request word: request `n` posted; a sleeping server woken.
    fn publish(&self) {
        const SHARED: u32 = 1;
        if self.word(0).swap(self.n << 1, SeqCst) & 1 != 0 {
            unsafe { os_sync_wake_by_address_any(self.base, 4, SHARED) };
        }
    }
    /// Ends the server: an empty request.
    fn end(&mut self) {
        self.n = self.n.wrapping_add(1) & 0x7fff_ffff;
        self.word(4).store(0, Relaxed);
        self.publish();
    }
    /// Waits for the reply to the last request (spin, then sleep in slices), checking `dead` after every sleep that ends without
    /// it and `deadline` throughout; then its payload, bounds-checked and copied out.
    fn reply(&self, deadline: Instant, mut dead: impl FnMut() -> std::io::Result<()>) -> std::io::Result<Vec<u8>> {
        const SHARED: u32 = 1;
        const MACH_ABSOLUTE_TIME: u32 = 32;
        // Retried, as os/os_sync_wait_on_address.h says (spurious wake-up, kernel memory, a failed copyin of a valid address).
        const ETIMEDOUT: i32 = 60;
        const EINTR: i32 = 4;
        const ENOMEM: i32 = 12;
        const EFAULT: i32 = 14;
        let w = self.word(64);
        let spin = Instant::now() + Self::SPIN;
        loop {
            let v = w.load(std::sync::atomic::Ordering::Acquire);
            if v >> 1 == self.n {
                break;
            }
            let now = Instant::now();
            if now < spin {
                std::hint::spin_loop();
                continue;
            }
            if now >= deadline {
                return Err(std::io::ErrorKind::TimedOut.into());
            }
            if v & 1 == 0 && w.compare_exchange(v, v | 1, SeqCst, SeqCst).is_err() {
                continue;
            }
            let left = (deadline - now).as_nanos().min(Self::SLICE_NS as u128) as u64;
            let ns = unsafe { os_sync_wait_on_address_with_timeout(self.base.add(64), (v | 1) as u64, 4, SHARED, MACH_ABSOLUTE_TIME, left.max(1)) };
            if ns < 0 {
                let e = std::io::Error::last_os_error();
                if !matches!(e.raw_os_error(), Some(ETIMEDOUT | EINTR | ENOMEM | EFAULT)) {
                    return Err(e);
                }
            }
            if w.load(std::sync::atomic::Ordering::Acquire) >> 1 != self.n {
                dead()?;
            }
        }
        let magic = self.word(72).load(Relaxed);
        if magic != Self::MAGIC {
            return Err(std::io::Error::other(format!("malformed fexc reply: channel magic {magic:#x}")));
        }
        let len = self.word(68).load(Relaxed) as usize;
        if !(20..=Self::REPLY_CAP).contains(&len) {
            return Err(std::io::Error::other(format!("malformed fexc reply: length {len}")));
        }
        let mut reply = vec![0; len];
        unsafe { channel_copy(self.base.add(Self::REPLY), reply.as_mut_ptr(), len, false) };
        Ok(reply)
    }
}
/// Copies `len` bytes into (`into`) or out of the channel's mapping at `at`, which the server may write at any time: volatile
/// accesses to the mapping, eight bytes at a time where they fit (both areas start 4 KiB-aligned), so a racing writer can only
/// make the copy's bytes wrong, which the reply's checks then see; `buf` is the host's own memory.
unsafe fn channel_copy(at: *mut u8, buf: *mut u8, len: usize, into: bool) {
    unsafe {
        let words = len / 8;
        for i in 0..words {
            let (m, b) = ((at as *mut u64).add(i), buf.add(i * 8) as *mut u64);
            if into { m.write_volatile(b.read_unaligned()) } else { b.write_unaligned(m.read_volatile()) }
        }
        for i in words * 8..len {
            if into { at.add(i).write_volatile(*buf.add(i)) } else { *buf.add(i) = at.add(i).read_volatile() }
        }
    }
}
impl Drop for Channel {
    fn drop(&mut self) {
        unsafe {
            munmap(self.base as *mut c_void, Self::BYTES);
            close(self.fd);
        }
    }
}
impl Server {
    fn start(fexc: &Path) -> Result<Self> {
        Self::spawn(fexc, &[])
    }
    /// `start` with further server options (tests: `--report`, `--serve-mode fork`).
    fn spawn(fexc: &Path, extra: &[&str]) -> Result<Self> {
        // One spawn at a time in the process. On macOS, std makes a child's pipes with `pipe` and only then marks them
        // close-on-exec, so a server spawned by another owner in between inherits this one's pipe end: a server holding
        // a killed sibling's stdout hides its death (no EOF) until the holder exits, and the request waits for its
        // deadline or the peers' watchdogs (seen with `lsof` in the compile-server-kill runs).
        static SPAWN: std::sync::Mutex<()> = std::sync::Mutex::new(());
        let _spawn = SPAWN.lock().unwrap_or_else(|e| e.into_inner());
        let channel = Channel::new()?;
        // Its own process group (R2 review): a terminal's Ctrl-C goes to the foreground group, and must reach only the
        // embedder (the stub cancels the run), not kill the server under a compile. The channel's fd is inherited: it is
        // not close-on-exec only for this spawn (under SPAWN). The host must never fork while a VM runs: no `pre_exec`,
        // `uid`, `gid` or anything else that makes std fork instead of `posix_spawn` here. A fork makes the private guest
        // RAM copy-on-write under the running VM; with it the multi-vCPU stress cases (`run_x3.py`, `run_l4a.py` stress)
        // lost interrupts (perf chunk 21).
        const F_SETFD: i32 = 2;
        const FD_CLOEXEC: i32 = 1;
        if unsafe { fcntl(channel.fd, F_SETFD, 0) } < 0 {
            return Err(format!("fexc channel: {}", std::io::Error::last_os_error()));
        }
        let child = Command::new(fexc)
            .args(["--serve", "--channel", &channel.fd.to_string()])
            .args(extra)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .process_group(0)
            .spawn();
        let cloexec = unsafe { fcntl(channel.fd, F_SETFD, FD_CLOEXEC) };
        let mut child = child.map_err(|e| format!("fexc --serve: {e}"))?;
        if cloexec < 0 {
            let _ = child.kill();
            let _ = child.wait();
            return Err(format!("fexc channel: {}", std::io::Error::last_os_error()));
        }
        let (stdin, stdout) = (child.stdin.take().unwrap(), child.stdout.take().unwrap());
        crate::perf::mark(&format!("server_start{}", std::thread::current().name().unwrap_or("").trim_start_matches("vcpu-")));
        Ok(Self { child, stdin: Some(stdin), stdout, reaped: false, channel })
    }
    /// The server's reply to one request body; a `TimedOut` error when the whole reply has not arrived by `deadline`, an
    /// `UnexpectedEof` one when the server died first (its stdout closed; seen within [`Channel::SLICE_NS`]).
    fn request(&mut self, body: &[u8], deadline: Instant) -> std::io::Result<Reply> {
        if self.stdin.is_none() {
            return Err(std::io::Error::other("fexc --serve: closed"));
        }
        self.channel.post(body)?;
        let stdout = &mut self.stdout;
        let mut reply = self.channel.reply(deadline, || Self::alive(stdout))?;
        let field = |at: usize| u32::from_le_bytes(reply[at..at + 4].try_into().unwrap());
        if reply.len() < 20 || reply.len() != 20 + field(4) as usize + field(8) as usize {
            return Err(std::io::Error::other("malformed fexc reply"));
        }
        let (code, json_len, extent, read) = (field(0) as i32, field(4) as usize, field(12), field(16));
        let object = reply.split_off(20 + json_len);
        let json = reply.split_off(20);
        Ok(Reply { code, json, object, extent, read })
    }
    /// Whether the server still runs: its stdout carries nothing in channel mode, so readable means EOF (it died) or a
    /// protocol error.
    fn alive(stdout: &mut ChildStdout) -> std::io::Result<()> {
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
        let mut fd = PollFd { fd: std::os::fd::AsRawFd::as_raw_fd(stdout), events: POLLIN, revents: 0 };
        if unsafe { poll(&mut fd, 1, 0) } <= 0 {
            return Ok(());
        }
        match stdout.read(&mut [0; 64])? {
            0 => Err(std::io::ErrorKind::UnexpectedEof.into()),
            _ => Err(std::io::Error::other("fexc --serve wrote to its stdout")),
        }
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
impl Server {
    /// Ends it (an empty request; its stdin closed too) and reaps it: its user and system CPU nanoseconds and maximum resident
    /// bytes (`wait4`'s rusage; the performance log).
    fn finish(&mut self) -> Option<(u64, u64, u64)> {
        #[repr(C)]
        struct Rusage {
            user: [i64; 2],
            system: [i64; 2],
            max_rss: i64,
            rest: [i64; 13],
        }
        unsafe extern "C" {
            fn wait4(pid: i32, status: *mut i32, options: i32, rusage: *mut Rusage) -> i32;
        }
        const WNOHANG: i32 = 1;
        self.channel.end();
        self.stdin = None;
        let (pid, mut status, mut r) = (self.child.id() as i32, 0, Rusage { user: [0; 2], system: [0; 2], max_rss: 0, rest: [0; 13] });
        // EOF ends it at once; one that does not end within a second is killed (its CPU time is still reported).
        let mut waited = 0;
        loop {
            match unsafe { wait4(pid, &mut status, if waited < 1000 { WNOHANG } else { 0 }, &mut r) } {
                0 => {
                    if waited == 1000 {
                        let _ = self.child.kill();
                    } else {
                        std::thread::sleep(Duration::from_millis(1));
                        waited += 1;
                    }
                }
                n if n == pid => break,
                _ => return None,
            }
        }
        self.reaped = true;
        let ns = |t: [i64; 2]| t[0] as u64 * 1_000_000_000 + (t[1] as i32) as u64 * 1000;
        Some((ns(r.user), ns(r.system), r.max_rss as u64))
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        if !self.reaped {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}

/// fexc's reply to one request (protocol in tools/fexc/fexc.cpp): its exit code (0 object, 1 no code, else an error), the
/// JSON report (error text for an error), the object; `extent` and `read` say what the compile depended on: the guest bytes
/// before `extent` (and their length only when `extent` reaches it) and the gating arguments with a bit in `read` (bits in
/// [`Compiler::gating`] order).
struct Reply {
    code: i32,
    json: Vec<u8>,
    object: Vec<u8>,
    extent: u32,
    read: u32,
}
impl Reply {
    /// As the persistent cache stores it (the JSON only for an answer without code: nothing else reads it).
    fn payload(&self) -> Vec<u8> {
        let json: &[u8] = if self.code == 0 { &[] } else { &self.json };
        let mut p = [self.code as u32, self.extent, self.read, json.len() as u32].map(u32::to_le_bytes).concat();
        p.extend_from_slice(json);
        p.extend_from_slice(&self.object);
        p
    }
    fn from_payload(p: &[u8]) -> Option<Self> {
        let field = |i: usize| p.get(4 * i..4 * i + 4).map(|b| u32::from_le_bytes(b.try_into().unwrap()));
        let (code, extent, read, json_len) = (field(0)?, field(1)?, field(2)?, field(3)? as usize);
        let json = p.get(16..16 + json_len)?.to_vec();
        Some(Self { code: code as i32, json, object: p[16 + json_len..].to_vec(), extent, read })
    }
}

/// What fexc answered (errors are not kept): an object, or no code with its end reason and, when the instruction at the RIP
/// cut the block, its opcode map and opcode and whether its gating cut it (`Some(mmx)`: an MMX form), or (`ud`, I10) whether
/// fexc found an encoding the presented CPU raises #UD for (reserved or unadvertised: `cut_ud`).
#[derive(Clone, Debug, PartialEq)]
enum Answer {
    Object(Vec<u8>),
    Cut { reason: String, cut: Option<(u8, u8)>, gated: Option<bool>, ud: bool },
}
impl Answer {
    fn new(reply: Reply, rip: u64) -> Result<Self> {
        if reply.code == 0 {
            return Ok(Answer::Object(reply.object));
        }
        let json = crate::json::parse(&String::from_utf8_lossy(&reply.json))?;
        let reason = end_reason(&json).ok_or("fexc report without guest.end.reason")?.to_string();
        let end = json.get("guest").and_then(|g| g.get("end"));
        let gated = end.and_then(|e| e.get("cut_gated")).map(|v| v.as_str() == Some("mmx"));
        let ud = end.and_then(|e| e.get("cut_ud")).is_some();
        Ok(Answer::Cut { reason, cut: system_cut(&json, rip), gated, ud })
    }
    /// This answer, kept for `from`, as fexc's answer at `to` (same half and page offset): an object relocated
    /// ([`crate::relocation`]), a no-code answer fexc decides from the bytes alone; `None` when it would not be fexc's.
    fn at(&self, from: u64, to: u64) -> Option<Self> {
        match self {
            Answer::Object(o) => crate::relocation::object(o, from, to).map(Answer::Object),
            Answer::Cut { reason, .. } => (crate::relocation::same_place(from, to) && crate::relocation::no_code_reusable(reason)).then(|| self.clone()),
        }
    }
}
/// The number of fexc's gating arguments (`FEXC_ARG_*`, [`Compiler::gating`]).
const GATING: usize = 11;
/// The memory cache's key (perf chunk 4): the block limit, the RIP's half (bit 63) and page offset, and the first bytes the
/// answer read (at most [`LEAD`], with their count): an answer is found at every RIP of that half and offset. As words
/// ([`Universal`]); no limit is `1 << 32`.
type Key = (u64, u64, u64);
const LEAD: usize = 4;
fn key(limit: Option<u32>, rip: u64, lead: &[u8]) -> Key {
    (limit.map_or(1 << 32, u64::from), rip & (1 << 63 | 0xfff), lead.iter().fold(lead.len() as u64, |v, &b| v << 8 | u64::from(b)))
}
/// The memory cache's hash (perf chunk 7; SipHash's was about 25 ns a probe): Thorup's strongly universal multiply-shift for
/// vectors of words ("High Speed Hashing for Integers and Strings", 2015, 3.4) under random keys per map. The guest chooses
/// the keys (its bytes, RIPs) but never sees a hash, so they collide no more than by chance: no quadratic buckets.
type Cache = HashMap<Key, Vec<Arc<Entry>>, Universal>;
#[derive(Clone)]
struct Universal([u128; 4]);
impl Default for Universal {
    fn default() -> Self {
        let s = std::collections::hash_map::RandomState::new();
        let r = |i: u64| u128::from(std::hash::BuildHasher::hash_one(&s, 2 * i)) << 64 | u128::from(std::hash::BuildHasher::hash_one(&s, 2 * i + 1));
        Self([r(0), r(1), r(2), r(3)])
    }
}
impl std::hash::BuildHasher for Universal {
    type Hasher = Words;
    fn build_hasher(&self) -> Words {
        Words { keys: self.0, sum: self.0[3], i: 0 }
    }
}
/// The sum of key i times word i (mod 2^128) plus the last key; the hash is its top 64 bits, reversed (hashbrown indexes by
/// the low bits, and the top ones are the universal ones). Words only: the three of a [`Key`].
struct Words {
    keys: [u128; 4],
    sum: u128,
    i: usize,
}
impl std::hash::Hasher for Words {
    fn write(&mut self, _: &[u8]) {
        unreachable!("the memory cache hashes its keys' words");
    }
    fn write_u64(&mut self, x: u64) {
        self.sum = self.sum.wrapping_add(self.keys[self.i].wrapping_mul(u128::from(x)));
        self.i += 1;
    }
    fn finish(&self) -> u64 {
        ((self.sum >> 64) as u64).reverse_bits()
    }
}
/// Answers per generation of the memory cache ([`Shard::insert`]), over all its shards: the installer boot keeps about 130 k.
const GENERATION: usize = 1 << 18;
/// And bytes per generation (about 80 MB for the installer's answers).
const GENERATION_BYTES: usize = 128 << 20;
/// The memory cache's shards (perf chunk 13), each locked alone and bounded at a sixteenth of the generation bounds.
const SHARDS: usize = 16;
/// A kept answer and what it was compiled from: its RIP, the gating values (those with a bit in `read` matter) and the guest
/// bytes before the compile's extent (`whole`: the extent reached their end, so their length matters too); `owner`: the vCPU
/// that put it in the map (a compile or a disk answer). Kept by reference (perf chunk 13): another vCPU's insert may drop it
/// from the map between a lookup and its use, which keeps its own reference.
struct Entry {
    rip: u64,
    owner: u8,
    read: u32,
    gating: [u64; GATING],
    prefix: Vec<u8>,
    whole: bool,
    answer: Answer,
}
impl Entry {
    /// Bytes it keeps (the memory cache's bound).
    fn size(&self) -> usize {
        size_of::<Self>() + self.prefix.len() + if let Answer::Object(o) = &self.answer { o.len() } else { 0 }
    }
    /// fexc gives the same answer for `bytes` under `gating`: it reads nothing that differs (tools/fexc/fexc.cpp, `Extent`
    /// and `FexcArg`).
    fn matches(&self, bytes: &[u8], gating: &[u64; GATING]) -> bool {
        self.read >> GATING == 0
            && (0..GATING).all(|i| self.read & 1 << i == 0 || self.gating[i] == gating[i])
            && if self.whole { bytes == self.prefix } else { bytes.starts_with(&self.prefix) }
    }
}
/// One shard of the kept answers in two generations ([`Shard::insert`]): `cache` the newer (`cached` entries), `old` the older.
#[derive(Default)]
struct Shard {
    cache: Cache,
    old: Cache,
    cached: usize,
    cached_bytes: usize,
}
impl Shard {
    /// The kept answer matching `bytes` at `rip` (generation, key, index), from either generation: one kept for `rip`
    /// itself in any bucket first, then one for another RIP. Keys hold the leading bytes the answer read (at most
    /// [`LEAD`]), so each length is tried; one pass, each bucket looked up once.
    fn find(&self, bytes: &[u8], rip: u64, limit: Option<u32>, gating: &[u64; GATING]) -> Option<(bool, Key, usize)> {
        let mut other = None;
        for n in (0..=LEAD.min(bytes.len())).rev() {
            let key = key(limit, rip, &bytes[..n]);
            for newer in [true, false] {
                let Some(v) = (if newer { &self.cache } else { &self.old }).get(&key) else { continue };
                let mut matching = v.iter().enumerate().filter(|(_, e)| e.matches(bytes, gating));
                let Some((first, _)) = matching.next() else { continue };
                let own = if v[first].rip == rip { Some(first) } else { matching.find(|(_, e)| e.rip == rip).map(|(i, _)| i) };
                match own {
                    Some(i) => return Some((newer, key, i)),
                    None => other = other.or(Some((newer, key, first))),
                }
            }
        }
        other
    }
    fn at(&self, (newer, key, i): (bool, Key, usize)) -> &Arc<Entry> {
        &(if newer { &self.cache } else { &self.old })[&key][i]
    }
    /// The entry [`Shard::find`] gave; one in the older generation moves to the newer (with the generation it may drop,
    /// [`Shard::insert`]).
    fn take(&mut self, (newer, key, i): (bool, Key, usize), limit: Option<u32>) -> (Arc<Entry>, Option<Cache>) {
        if newer {
            return (self.cache[&key][i].clone(), None);
        }
        let entry = self.old.get_mut(&key).unwrap().swap_remove(i);
        let dropped = self.insert(limit, entry.clone());
        (entry, dropped)
    }
    /// Keeps `entry` in the newer generation; when that holds a [`SHARDS`]th of [`GENERATION`] answers or of
    /// [`GENERATION_BYTES`] it becomes the older one (the older is dropped): every answer used or made in the shard's last
    /// generation is kept, at most twice the bound (2 x 128 MiB over the shards). Returns the dropped generation, for the
    /// caller to free after unlocking the shard (up to 16 k answers).
    fn insert(&mut self, limit: Option<u32>, entry: Arc<Entry>) -> Option<Cache> {
        let mut dropped = None;
        if self.cached >= GENERATION / SHARDS || self.cached_bytes >= GENERATION_BYTES / SHARDS {
            dropped = Some(std::mem::replace(&mut self.old, std::mem::take(&mut self.cache)));
            (self.cached, self.cached_bytes) = (0, 0);
        }
        let key = key(limit, entry.rip, &entry.prefix[..LEAD.min(entry.prefix.len())]);
        self.cached_bytes += entry.size();
        self.cache.entry(key).or_default().push(entry);
        self.cached += 1;
        dropped
    }
}
/// fexc's answers kept in memory (perf chunk 13): one map for every vCPU of a run whose compilers present the same
/// features (`supported`, which the key leaves out and fexc's request carries), in [`SHARDS`] shards chosen by the block
/// limit and the RIP's half and page offset, so that every lead length of one request is in one shard (one lock a lookup).
/// Each vCPU still relocates and admits every answer at its own RIP, with its own bytes.
pub struct Answers {
    supported: u64,
    pick: Universal,
    shards: [Mutex<Shard>; SHARDS],
    /// Per shard: nanoseconds waited for its lock and the waits (a lock taken at the first try is not timed).
    waits: [[AtomicU64; 2]; SHARDS],
}
impl Answers {
    pub(crate) fn new(supported: u64) -> Arc<Self> {
        Arc::new(Self { supported, pick: Universal::default(), shards: std::array::from_fn(|_| Mutex::default()), waits: Default::default() })
    }
    /// The shard of `limit` and `rip`, locked, and the nanoseconds waited for it.
    fn lock(&self, limit: Option<u32>, rip: u64) -> (std::sync::MutexGuard<'_, Shard>, u64) {
        let k = key(limit, rip, &[]);
        let i = std::hash::BuildHasher::hash_one(&self.pick, (k.0, k.1)) as usize % SHARDS;
        match self.shards[i].try_lock() {
            Ok(g) => (g, 0),
            Err(std::sync::TryLockError::Poisoned(e)) => (e.into_inner(), 0),
            Err(std::sync::TryLockError::WouldBlock) => {
                let t = crate::perf::now();
                let g = self.shards[i].lock().unwrap_or_else(|e| e.into_inner());
                let waited = crate::perf::now() - t;
                self.waits[i][0].fetch_add(waited, Relaxed);
                self.waits[i][1].fetch_add(1, Relaxed);
                (g, waited)
            }
        }
    }
    /// The kept answer matching `bytes` at `rip` ([`Shard::find`]; an older one moves to the newer generation).
    fn find(&self, bytes: &[u8], rip: u64, limit: Option<u32>, gating: &[u64; GATING]) -> (Option<Arc<Entry>>, u64) {
        let (mut shard, waited) = self.lock(limit, rip);
        let Some(f) = shard.find(bytes, rip, limit, gating) else { return (None, waited) };
        let (entry, dropped) = shard.take(f, limit);
        drop(shard);
        drop(dropped); // freed unlocked
        (Some(entry), waited)
    }
    /// Keeps `entry`, made for `bytes` at `rip`; returns the owner of a matching answer the map held already (perf chunk 13:
    /// after a lookup that found none, one made meanwhile by another vCPU) and the nanoseconds waited.
    fn insert(&self, limit: Option<u32>, entry: Arc<Entry>, bytes: &[u8], rip: u64) -> (Option<u8>, u64) {
        let (mut shard, waited) = self.lock(limit, rip);
        let held = shard.find(bytes, rip, limit, &entry.gating).map(|f| shard.at(f).owner);
        let dropped = shard.insert(limit, entry);
        drop(shard);
        drop(dropped); // freed unlocked
        (held, waited)
    }
    /// Bytes and entries kept.
    fn kept(&self) -> (usize, usize) {
        self.shards.iter().fold((0, 0), |(b, n), s| {
            let s = s.lock().unwrap_or_else(|e| e.into_inner());
            let all = || s.cache.values().chain(s.old.values()).flatten();
            (b + all().map(|e| e.size()).sum::<usize>(), n + all().count())
        })
    }
    /// The performance log's line for the run: each shard's lock waits, `[ns, count]`, and the bytes and answers kept.
    pub fn report(&self) -> String {
        let w: Vec<String> = self.waits.iter().map(|[ns, n]| format!("[{},{}]", ns.load(Relaxed), n.load(Relaxed))).collect();
        let (bytes, answers) = self.kept();
        format!("answers {{\"lock_waits\":[{}],\"kept_bytes\":{bytes},\"kept_answers\":{answers}}}", w.join(","))
    }
}
/// What [`Compiler::compile`] gives the service loop.
pub(crate) enum Code {
    Block(admission::Admitted),
    System(exec::System),
    None(String),
}

/// fexc for one vCPU's feature set: one persistent `fexc --serve` process (started at the first compile), a cache of its
/// answers in memory and, when set, the persistent one on disk. Objects are byte-identical to the one-shot CLI's.
/// In memory an answer is kept by block limit, the RIP's half and page offset and its leading bytes (the profile and
/// features are the compiler's), with its RIP and what fexc says the compile read: the guest bytes up to its extent and the
/// gating arguments it consulted. A request equal in those gets it (fexc is deterministic and would read the same), so a
/// block is not compiled again because later bytes on its page or an unrelated control bit changed; at another RIP the
/// answer is relocated ([`crate::relocation`], perf chunk 4), or compiled when it cannot be. On disk, answers are kept by
/// the request with its RIP as half and page offset, with that RIP, and the fexc build (`compile_cache`).
/// Every object, from fexc or a cache, goes through admission's mandatory validation when it is used: a damaged or forged
/// cache entry can at worst be refused (a stop) or give the guest the effects of other x86 code inside its own containment,
/// as a faulty fexc could; it cannot give code a way out of it.
pub struct Compiler {
    fexc: PathBuf,
    server: Option<Server>,
    supported: u64,
    /// Block size limit in guest instructions (fexc's own limit when `None`).
    max_instructions: Option<u32>,
    /// Kept answers: its own, or the run's ([`Compiler::share`]).
    answers: Arc<Answers>,
    /// This vCPU, the owner of the answers it keeps (perf chunk 13).
    pub vcpu: u8,
    /// The persistent cache (`boot-linux` sets it; test drivers never do).
    pub disk: Option<std::sync::Arc<crate::compile_cache::DiskCache>>,
    /// Requests that reached fexc.
    pub compiles: u64,
    /// Requests answered from the cache in memory, and from the persistent one.
    pub(crate) hits: u64,
    pub(crate) disk_hits: u64,
    /// Perf chunk 13: memory hits by the vCPU that kept the answer (this one's own included); compiles whose answer another
    /// vCPU made while this one compiled (nothing matched before, a match was kept at the insert); nanoseconds waited for
    /// shard locks, and the waits.
    pub hits_by_source: [u64; cache::MAX_VCPUS],
    pub duplicates: u64,
    pub lock_wait: (u64, u64),
    /// Answers kept for another RIP and used at this one (perf chunk 4: [`crate::relocation`]); answers that could not be
    /// (compiled instead); relocated objects admission refused (compiled instead, logged; none expected); fresh compiles
    /// made by the reuse check.
    pub relocated: u64,
    pub relocation_refused: u64,
    pub relocation_fallbacks: u64,
    pub reuse_checks: u64,
    /// Debug check (the program driver's `--code-shape` or `--check-reuse`, `boot-linux --check-reuse`): every answer used at
    /// another RIP than its own is also compiled there, and the two must be equal (else a host error).
    pub check_reuse: bool,
    /// How long one request may wait for the server's whole reply (the first one includes the server's start); then the
    /// server is killed and the run fails with a host error ([`COMPILE_DEADLINE`] by default).
    pub deadline: Duration,
    /// The longest request so far (reply received), for the deadline's margin.
    pub slowest: Duration,
    /// Admission's code-shape debug check ([`admission`]); off unless the caller sets it (the program driver's
    /// `--code-shape`).
    pub shape: bool,
    /// The vCPU's controls gate SSE (I5a: CR0.EM, CR0.TS or no CR4.OSFXSR, [`sse_gated`]): fexc cuts every SSE form as a
    /// system instruction, which the resident raises as #UD or #NM. Set per request (part of the request, so of the cache key).
    pub(crate) sse_gated: bool,
    /// The vCPU's controls gate the VEX AVX forms (I8: no CR4.OSXSAVE, XCR0[2:1] != 11b, or CR0.TS, [`avx_gated`]): fexc cuts
    /// them as system instructions, which the resident raises as #UD or #NM. Set per request, like `sse_gated`.
    pub(crate) avx_gated: bool,
    /// The vCPU runs under alignment checking (X2: CR0.AM, RFLAGS.AC, CPL 3) or with a data breakpoint armed (S4,
    /// `Cpu::bit_tests_cut`): fexc cuts every BT* memory form as a system instruction, which the resident checks (#AC, the
    /// operand-sized unit against the breakpoints) and then steps compiled without it (`State::plain_step`).
    pub(crate) alignment_gated: bool,
    /// The vCPU's x87 gating and rounding (I6, [`x87_args`]) and its unmasked floating-point exceptions (I5c): fexc's
    /// `--x87-gated`, `--wait-gated`, `--x87-fpcr`, `--simd-unmasked` and `--x87-unmasked` arguments.
    pub(crate) x87: Vec<String>,
    /// Perf chunk 15: a translate request at CPL 0 (never a step): fexc's `--cpl0`, under which it translates the one-byte
    /// CLI and STI inline (the CPL is part of `Cpu::code_context`, so such a block runs only at CPL 0).
    pub(crate) cpl0: bool,
    /// Perf chunk 15: a CPL-0 translate request on a host whose counter runs at 24 MHz ([`crate::clock::frequency`], fexc's
    /// `--tsc`): RDTSC is translated inline with the resident's formula for that rate.
    pub(crate) tsc: bool,
    /// Perf chunk 26: a translate request (never a step) without alignment checking or a data breakpoint (`alignment_gated`
    /// off, which is part of `Cpu::code_context`): fexc's `--bulk`, under which REP STOS stores the part of its string inside
    /// RDI's page in bulk (each store a site at an iteration boundary). A step runs one element: TF traps after each
    /// iteration, and the watchpoint, UC and code-write paths step the element that met them.
    pub(crate) bulk: bool,
    /// The performance log's record of this vCPU (`perf::on`; [`Compiler::finish`]).
    pub perf: Option<Box<crate::perf::Recorder>>,
    /// The last compile's answer: how many guest bytes it read (fexc's extent, at most the bytes given), and whether a
    /// halving retry made it ([`Compiler::compile`]); the template rule of reuse inside the resident (perf chunk 10).
    read: usize,
    halved: bool,
}
/// fexc's x87 arguments for `cpu` (I6; SDM Vol.3 Table 2-2): every x87 form #NM under CR0.EM or CR0.TS, WAIT/FWAIT under CR0.TS
/// with CR0.MP; x87 arithmetic under its own FPCR when FCW.RC or MXCSR's flush-to-zero/DAZ differ ([`exec::x87_fpcr`]); and
/// (I5c) the MXCSR and FCW exceptions whose masks are clear, each instruction then checked for them (none as Linux runs). The
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
    for (name, unmasked) in [("--simd-unmasked", exec::simd_unmasked(cpu.mxcsr)), ("--x87-unmasked", exec::x87_unmasked(cpu.fcw))] {
        if unmasked != 0 {
            args.extend([name.into(), format!("{unmasked:#x}")]);
        }
    }
    args
}
/// Whether `cpu`'s controls gate SSE instructions (SDM Vol.3 Table 2-2, Vol.2 2.5: #UD with CR0.EM or without CR4.OSFXSR,
/// #NM with CR0.TS). Blocks are made under one gating state: the gating bits are part of their context
/// (`Cpu::code_context`), so a block is never found under other gating (K1).
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
pub const COMPILE_DEADLINE: Duration = Duration::from_secs(30);
impl Compiler {
    /// Now, when the performance log records this vCPU (else 0, unread).
    fn stamp(&self) -> u64 {
        if self.perf.is_some() { crate::perf::now() } else { 0 }
    }
    /// Charges the time since `since` to the current request's part `k` ([`crate::perf::Recorder::add`]).
    fn charge(&mut self, k: usize, since: u64) {
        if let Some(r) = &mut self.perf {
            r.add(k, since);
        }
    }
    /// The run's end for the performance log: its server's CPU time (its stdin closed, reaped by `wait4`).
    pub fn finish(&mut self) -> Option<(u64, u64, u64)> {
        self.server.as_mut().and_then(Server::finish)
    }
    pub fn new(fexc: &Path, cpu: &Vcpu, max_instructions: Option<u32>) -> Result<Self> {
        let mut id = [0; admission::ID_REGISTERS.len()];
        for (value, (name, reg)) in id.iter_mut().zip(admission::ID_REGISTERS) {
            *value = cpu.sys(reg).map_err(|e| format!("{name}: {e}"))?;
        }
        Ok(Self::with_features(fexc, admission::supported_features(&id), max_instructions))
    }
    fn with_features(fexc: &Path, supported: u64, max_instructions: Option<u32>) -> Self {
        Self { fexc: fexc.into(), server: None, supported, max_instructions, answers: Answers::new(supported), vcpu: 0, disk: None, compiles: 0, hits: 0, disk_hits: 0,
               hits_by_source: [0; cache::MAX_VCPUS], duplicates: 0, lock_wait: (0, 0),
               relocated: 0, relocation_refused: 0, relocation_fallbacks: 0, reuse_checks: 0, check_reuse: false,
               deadline: COMPILE_DEADLINE, slowest: Duration::ZERO, shape: false, sse_gated: false, avx_gated: false, alignment_gated: false, x87: vec![], cpl0: false, tsc: false, bulk: false, perf: None, read: 0, halved: false }
    }
    /// The admitted block for `bytes` at `rip` (only its first instruction if `step`); else the system instruction at
    /// `rip` the resident executes, or fexc's end reason when it produced no code.
    fn compile(&mut self, bytes: &[u8], rip: u64, step: bool, hook: &mut impl Hook) -> Result<Code> {
        // I9 review: a block whose code is over `MAX_BLOCK_BYTES` (PDEP/PEXT, PCMPxSTRx and other long lowerings in numbers)
        // is translated again from the same RIP with half its instructions, until it fits; one instruction too large for a
        // block stops `code_too_large`. R2 review: the same for a block with a spill area (`spill_area`).
        let mut limit = if step { Some(1) } else { self.max_instructions };
        self.halved = false;
        loop {
            match self.compile_once(bytes, rip, limit, hook)? {
                Ok(code) => return Ok(code),
                // Halving the smaller of the reply's count and the limit asked: the limit falls every turn even if fexc
                // answered with more instructions than asked (the loop always ends).
                Err((n, _)) if limit.map_or(n, |l| n.min(l)) > 1 => {
                    limit = Some(limit.map_or(n, |l| n.min(l)) / 2);
                    self.halved = true;
                    if let Some(r) = &mut self.perf {
                        r.halved = true;
                    }
                }
                Err((_, reason)) => return Ok(Code::None(reason.into())),
            }
        }
    }
    /// The gating arguments' values, in fexc's `FEXC_ARG_*` order: SSE, AVX, alignment, x87, WAIT, x87 FPCR, (I5c) the
    /// unmasked SIMD and x87 exceptions, (perf chunk 15) CPL 0 and the inline TSC, and (perf chunk 26) bulk REP STOS.
    fn gating(&self) -> [u64; GATING] {
        let flag = |a: &str| u64::from(self.x87.iter().any(|x| x == a));
        let value = |a: &str| {
            let v = self.x87.iter().position(|x| x == a).and_then(|i| self.x87.get(i + 1));
            v.and_then(|v| u64::from_str_radix(v.trim_start_matches("0x"), 16).ok()).unwrap_or(0)
        };
        [self.sse_gated.into(), self.avx_gated.into(), self.alignment_gated.into(), flag("--x87-gated"), flag("--wait-gated"),
         value("--x87-fpcr"), value("--simd-unmasked"), value("--x87-unmasked"), self.cpl0.into(), self.tsc.into(), self.bulk.into()]
    }
    /// [`Compiler::compile`] with an instruction limit; `Err((n, why))`: the object must be translated again with fewer
    /// than its n guest instructions ([`admission::retry`]).
    fn compile_once(&mut self, bytes: &[u8], rip: u64, limit: Option<u32>, hook: &mut impl Hook) -> Result<std::result::Result<Code, (u32, &'static str)>> {
        let gating = self.gating();
        let t = self.stamp();
        let (found, waited) = self.answers.find(bytes, rip, limit, &gating);
        self.waited(waited);
        self.charge(crate::perf::LOOKUP, t);
        if let Some(e) = &found {
            if let Some(code) = self.reuse(e, bytes, rip, limit, hook)? {
                self.hits += 1;
                self.hits_by_source[usize::from(e.owner)] += 1;
                self.read = e.prefix.len();
                if let Some(r) = &mut self.perf {
                    r.sources[0] += 1;
                    r.read = (hash(&e.prefix), e.prefix.len() as u32);
                }
                return Ok(code);
            }
        }
        // Nothing kept, or an answer kept for another RIP that is not fexc's here: the persistent cache only in the first
        // case (it is keyed as memory is, so it would hold the same answer).
        let mut disk = found.is_none();
        loop {
            let compiles = self.compiles;
            let (reply, at) = self.ask(bytes, rip, limit, disk, hook)?;
            let t = self.stamp();
            let (whole, end) = (reply.extent as usize >= bytes.len(), bytes.len().min(reply.extent as usize));
            let (read, prefix) = (reply.read, bytes[..end].to_vec());
            let entry = Arc::new(Entry { rip: at, owner: self.vcpu, read, gating, prefix, whole, answer: Answer::new(reply, at)? });
            let (held, waited) = self.answers.insert(limit, entry.clone(), bytes, rip);
            self.waited(waited);
            // Perf chunk 13: the lookup found nothing and another vCPU kept a matching answer while this one compiled.
            if disk && self.compiles > compiles && held.is_some_and(|o| o != self.vcpu) {
                self.duplicates += 1;
            }
            self.read = end;
            self.charge(crate::perf::KEEP, t);
            if let Some(r) = &mut self.perf {
                r.read = (hash(&entry.prefix), entry.prefix.len() as u32);
            }
            match self.reuse(&entry, bytes, rip, limit, hook)? {
                Some(code) => return Ok(code),
                None => disk = false, // a disk answer for another RIP that is not fexc's here
            }
        }
    }
    fn waited(&mut self, ns: u64) {
        if ns > 0 {
            self.lock_wait = (self.lock_wait.0 + ns, self.lock_wait.1 + 1);
        }
    }
    /// Uses the run's `answers` (perf chunk 13: one map for its vCPUs; the first compiler makes it) when they were made for
    /// this compiler's features; else keeps its own.
    pub fn share(&mut self, answers: &std::sync::OnceLock<Arc<Answers>>) {
        let shared = answers.get_or_init(|| self.answers.clone());
        if shared.supported == self.supported {
            self.answers = shared.clone();
        } else {
            eprintln!("natemu: vCPU {} presents other features than the run's first compiler: it keeps its own answers", self.vcpu);
        }
    }
    /// The kept answer `e`, used at `rip`: as it is for its own RIP; for another, relocated (`None`: it would not be fexc's
    /// at `rip`, or admission refused the relocated object; logged; the caller compiles).
    fn reuse(&mut self, e: &Entry, bytes: &[u8], rip: u64, limit: Option<u32>, hook: &mut impl Hook) -> Result<Option<std::result::Result<Code, (u32, &'static str)>>> {
        let t = self.stamp();
        let (from, read) = (e.rip, (e.read, e.whole, e.prefix.len()));
        let code = if from == rip {
            self.answered(&e.answer, bytes, rip, hook)?
        } else {
            let Some(answer) = e.answer.at(from, rip) else {
                self.relocation_refused += 1;
                return Ok(None);
            };
            // Admission's share of a relocated hit is timed apart from the relocation (perf chunk 7).
            self.charge(crate::perf::RELOCATE, t);
            if self.check_reuse {
                self.check(&answer, read, bytes, rip, limit, from, hook)?;
            }
            let t = self.stamp();
            match self.answered(&answer, bytes, rip, hook) {
                Ok(code) => {
                    self.relocated += 1;
                    self.charge(if matches!(code, Ok(Code::Block(_))) { crate::perf::ADMIT } else { crate::perf::DECODE }, t);
                    return Ok(Some(code));
                }
                Err(e) => {
                    self.relocation_fallbacks += 1;
                    eprintln!("natemu: the answer relocated from {from:#x} to {rip:#x} was refused ({e}); compiled at {rip:#x}");
                    return Ok(None);
                }
            }
        };
        self.charge(if matches!(code, Ok(Code::Block(_))) { crate::perf::ADMIT } else { crate::perf::DECODE }, t);
        Ok(Some(code))
    }
    /// The reuse check: fexc's own answer at `rip` (no cache) equals `answer`, relocated from `from`.
    /// `read`: the kept entry's arguments read, whether its extent reached the end of its bytes, and their length to it.
    #[allow(clippy::too_many_arguments)]
    fn check(&mut self, answer: &Answer, read: (u32, bool, usize), bytes: &[u8], rip: u64, limit: Option<u32>, from: u64, hook: &mut impl Hook) -> Result<()> {
        let (reply, _) = self.ask(bytes, rip, limit, false, hook)?;
        // Counted as checks, not as the run's compiles.
        self.compiles -= 1;
        if let Some(r) = &mut self.perf {
            r.sources[2] -= 1;
        }
        self.reuse_checks += 1;
        let fresh = (reply.read, reply.extent as usize >= bytes.len(), bytes.len().min(reply.extent as usize));
        if fresh != read || Answer::new(reply, rip)? != *answer {
            return Err(format!("reuse check: the answer relocated from {from:#x} is not fexc's at {rip:#x}"));
        }
        Ok(())
    }
    /// [`Compiler::compile_once`]'s use of an answer: admitted, or decoded.
    #[cfg_attr(not(feature = "testing"), allow(unused_variables))]
    fn answered(&self, answer: &Answer, bytes: &[u8], rip: u64, hook: &mut impl Hook) -> Result<std::result::Result<Code, (u32, &'static str)>> {
        Ok(match answer {
            Answer::Object(object) => match admission::retry(object) {
                Some(n) => Err(n),
                None => {
                    #[cfg_attr(not(feature = "testing"), allow(unused_mut))]
                    let mut admitted = admission::admit(object, rip, bytes, self.supported, self.shape, self.cpl0)?;
                    // Perf chunk 26: a bulk REP STOS only where asked for (never a step: TF traps after each iteration).
                    if admitted.bulk && !self.bulk {
                        return Err(format!("object refused: a bulk REP STOS loop in a request without --bulk at {rip:#x}").into());
                    }
                    #[cfg(feature = "testing")]
                    hook.admitted(&mut admitted.object);
                    Ok(Code::Block(admitted))
                }
            },
            Answer::Cut { reason, cut, gated, ud } => {
                let decoded = |(map, opcode)| {
                    crate::system::decode(bytes, map, opcode).or_else(|| crate::system::lock_ud(bytes)).or_else(|| crate::system::vex_ud(bytes))
                };
                // An SSE form cut by its gating (I5a): no operand matters, the resident raises #UD or #NM.
                // I6: an x87 form or WAIT (primary d8..df, 9b) cut by its gating: #NM. I8: a VEX form (fexc's map 8): #UD or #NM.
                // I9: an MMX form (fexc's `cut_gated` "mmx"): #UD or #NM by CR0.EM/TS alone.
                let gated_kind = |(map, op): (u8, u8), mmx: bool| {
                    let x87 = map == 0 && matches!(op, 0xd8..=0xdf | 0x9b);
                    let kind = if mmx { exec::system::MMX_GATED } else if x87 { exec::system::X87_GATED } else if map == 8 { exec::system::AVX_GATED } else { exec::system::SSE_GATED };
                    Some(exec::System { kind, length: 1, ..exec::System::NONE })
                };
                // I10: an encoding fexc found reserved or unadvertised: #UD whatever its operands (RIP stays at it).
                let ud = ud.then_some(exec::System { kind: exec::system::UD, length: 1, ..exec::System::NONE });
                match ud.or_else(|| {
                    cut.and_then(|cut| match gated {
                        Some(mmx) => gated_kind(cut, *mmx),
                        None => decoded(cut),
                    })
                }) {
                    Some(s) if reason == "system_instruction" => Ok(Code::System(s)),
                    // Longer than 15 bytes with all of its first 15 fetched: #GP(0) (a fetch fault was `partial_decode`).
                    _ if reason == "instruction_too_long" => Ok(Code::System(exec::System { kind: exec::system::TOO_LONG, length: 1, ..exec::System::NONE })),
                    // X1c: a form fexc refuses at this RIP that x86 defines as an exception: a direct branch to a non-canonical
                    // target (#GP(0) when taken), or an encoding #UD whatever its operands ([`crate::system::undefined`]).
                    _ if reason == "unsupported_form" && cut.is_some() => {
                        match crate::system::branch(bytes, rip).or_else(|| crate::system::undefined(bytes)) {
                            Some(s) => Ok(Code::System(s)),
                            None => Ok(Code::None(reason.clone())),
                        }
                    }
                    _ => Ok(Code::None(reason.clone())),
                }
            }
        })
    }
    /// The request body for `bytes` at `rip` (protocol in tools/fexc/fexc.cpp).
    fn body(&self, bytes: &[u8], rip: u64, limit: Option<u32>) -> Vec<u8> {
        self.body_with(bytes, format!("{rip:#x}"), limit)
    }
    /// The persistent cache's key body (perf chunk 4): the request with the RIP replaced by its half and page offset, as the
    /// memory cache keys answers.
    fn disk_body(&self, bytes: &[u8], rip: u64, limit: Option<u32>) -> Vec<u8> {
        self.body_with(bytes, format!("{}:{:#x}", if rip >> 63 != 0 { 'k' } else { 'u' }, rip & 0xfff), limit)
    }
    fn body_with(&self, bytes: &[u8], rip: String, limit: Option<u32>) -> Vec<u8> {
        let mut args = vec!["--profile".to_string(), "slice1".into(), "--rip".into(), rip];
        if let Some(n) = limit {
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
        if self.cpl0 {
            args.push("--cpl0".into());
        }
        if self.tsc {
            args.push("--tsc".into());
        }
        if self.bulk {
            args.push("--bulk".into());
        }
        let args: Vec<u8> = args.iter().flat_map(|a| a.bytes().chain([0])).collect();
        let mut body = (args.len() as u32).to_le_bytes().to_vec();
        body.extend(args);
        body.extend(bytes);
        body
    }
    /// fexc's reply to the request for `bytes` at `rip` and the RIP it was compiled at: from the persistent cache when `disk`
    /// (an answer for any RIP of that half and offset, stored with its RIP), else from the server (then stored).
    fn ask(&mut self, bytes: &[u8], rip: u64, limit: Option<u32>, disk: bool, _hook: &mut impl Hook) -> Result<(Reply, u64)> {
        let t = self.stamp();
        let key = self.disk.as_ref().filter(|d| disk && !d.is_empty()).map(|d| d.key(&self.disk_body(bytes, rip, limit)));
        let stored = key.and_then(|k| self.disk.as_ref().unwrap().get(&k));
        let stored = stored.and_then(|p| Some((Reply::from_payload(p.get(8..)?)?, u64::from_le_bytes(p.get(..8)?.try_into().unwrap()))));
        self.charge(crate::perf::DISK_GET, t);
        if let Some((reply, at)) = stored.filter(|(r, _)| r.code < 2) {
            self.disk_hits += 1;
            if let Some(r) = &mut self.perf {
                r.sources[1] += 1;
            }
            return Ok((reply, at));
        }
        let body = self.body(bytes, rip, limit);
        let t = self.stamp();
        if self.server.is_none() {
            self.server = Some(Server::start(&self.fexc)?);
        }
        let server = self.server.as_mut().unwrap();
        #[cfg(feature = "testing")]
        _hook.before_compile(self.compiles, server.child.id());
        let began = Instant::now();
        let reply = match server.request(&body, began + self.deadline) {
            Ok(reply) => reply,
            Err(e) if e.kind() == std::io::ErrorKind::TimedOut => {
                let _ = server.child.kill();
                return Err(format!("{}: no reply within {:?}, killed", server.failed(e), self.deadline));
            }
            Err(e) => return Err(server.failed(e)),
        };
        self.slowest = self.slowest.max(began.elapsed());
        self.compiles += 1;
        self.charge(crate::perf::FEXC, t);
        if let Some(r) = &mut self.perf {
            r.sources[2] += 1;
        }
        if reply.code >= 2 {
            return Err(format!("fexc failed at {rip:#x}: {}", String::from_utf8_lossy(&reply.json)));
        }
        let t = self.stamp();
        if let Some(disk) = &self.disk {
            disk.put(self.disk_body(bytes, rip, limit), rip >> 63 != 0, [&rip.to_le_bytes()[..], &reply.payload()].concat());
        }
        self.charge(crate::perf::DISK_PUT, t);
        Ok((reply, rip))
    }
}
/// The performance log's hash of the guest bytes an answer read.
fn hash(bytes: &[u8]) -> u64 {
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    bytes.hash(&mut h);
    h.finish()
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
    /// The limit cut them (before the end of a page fetched).
    cut: bool,
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
            // X1c: a page that is not guest RAM (a device or unassigned page): the owner decisions of October 29 give such
            // pages reads of all-ones and writes ignored, and (amendment after L3) map unassigned ones read-only and
            // non-executable; the SDM defines no exception for fetching from memory-mapped I/O. Code runs only from guest
            // RAM, so it is a typed stop (`no_code`, reason `fetch_not_ram`) at the RIP, nothing executed.
            None => Ok(Err(Stop::NoCode("fetch_not_ram".into()))),
        };
    }
    // Bytes of [va, the end of its page), cut at the limit when it lies inside or at the page's end (a limit at the next
    // page's first byte must stop the next page from being fetched or asked for); and whether it did.
    let span = |va: u64| {
        let rest = 4096 - (va & 4095);
        match limit {
            Some(l) if l > va && l - va <= rest => (l - va, true),
            _ => (rest, false),
        }
    };
    let mut bytes = Vec::with_capacity(8192);
    let mut copy = |gpa: u64, length: u64| match copy_out(&ram, gpa, length, &mut bytes) {
        true => Ok(()),
        false => Err(format!("code at {rip:#x} is outside guest RAM")),
    };
    let (length, mut cut) = span(rip);
    copy(state.fetch[0] | rip & 4095, length)?;
    // The last page of the address space has no next page.
    let next = rip.checked_add(length).filter(|_| !cut);
    if let (Some(va), true) = (next, state.fetch[1] != u64::MAX) {
        let (length, second) = span(va);
        copy(state.fetch[1], length)?;
        cut = second;
    }
    let walked = state.fetch[1] != u64::MAX || state.fetch_fault[0] != exec::fetch::NONE;
    Ok(Ok(Fetched { bytes, more: next.is_some() && !walked, next_fault: fault, cut }))
}
/// Appends guest RAM's `length` bytes at `gpa` to `out` (false: not all RAM), as `GuestRam::copy_out` reads them (volatile
/// loads: other vCPUs may write them) but eight at a time where aligned (perf chunk 7: its byte loop and the zeroed buffer
/// were about 0.4 us a request).
fn copy_out(ram: &GuestRam, gpa: u64, length: u64, out: &mut Vec<u8>) -> bool {
    let Some(p) = ram.range(gpa, length) else { return false };
    let n = length as usize;
    let head = (p as usize).wrapping_neg() % 8;
    let words = n.saturating_sub(head) / 8;
    out.reserve(n);
    // SAFETY: `range` gives `n` bytes of the RAM's mapping at `p`, word-aligned from `p + head`.
    unsafe {
        let byte = |i: usize| std::ptr::read_volatile(p.add(i));
        out.extend((0..head.min(n)).map(byte));
        for w in 0..words {
            out.extend_from_slice(&std::ptr::read_volatile(p.add(head + 8 * w).cast::<u64>()).to_ne_bytes());
        }
        out.extend((head.min(n) + 8 * words..n).map(byte));
    }
    true
}

/// What [`Hook::at`] makes of the translation request at a RIP.
pub enum At {
    /// End the run there ([`Stop::Hooked`] with this code), nothing fetched or compiled.
    Stop(u32),
    /// Answer with this system reply ([`exec::REPLY_SYSTEM`]; not counted in [`Requests::decodes`]): any kind, the
    /// resident's test kinds (`SET_CPL`, `EVENT_HOOK`) included, so test drivers only (the `testing` feature).
    #[cfg(feature = "testing")]
    System(exec::System),
}
/// Which reply [`Hook::reply`] sees.
#[cfg(feature = "testing")]
pub enum ReplyKind {
    /// A translation request's reply (a block, a decode, a next page or a fetch fault), never a park.
    Translation,
    /// A device access's reply (M3).
    Device,
}
/// A driver's view into one vCPU's [`run`], at its host exits only (never in the VM). Every method defaults to doing
/// nothing; `()` is the hook of a run without one, and compiles to nothing. The test driver supplies its run-time defence
/// checks (corrupted replies and objects, a killed compile server) and its test programs' hooks (CPL switches, event
/// sources, the logging IDT) through it. Without the `testing` feature only [`Hook::at`] (ending a run) and
/// [`Hook::boundary`] exist: nothing that changes what the resident is given.
pub trait Hook {
    /// At a translation request for `rip`, after the caller's stop RIP: end the run, or answer it, or (`None`) translate.
    fn at(&mut self, _area: Area, _rip: u64) -> Result<Option<At>> {
        Ok(None)
    }
    /// The lowest RIP above `rip` [`Hook::at`] answers: blocks are fetched up to it, so it is reached at a block boundary.
    fn boundary(&self, _rip: u64) -> Option<u64> {
        None
    }
    /// Every RIP [`Hook::at`] may answer and every [`Hook::boundary`] (perf chunk 10: the resident copies no block whose
    /// fetch would reach one, so each is still the host's to answer or cut at).
    fn fences(&self) -> Vec<std::ops::Range<u64>> {
        Vec::new()
    }
    /// Each admitted object, before it is staged.
    #[cfg(feature = "testing")]
    fn admitted(&mut self, _object: &mut [u8]) {}
    /// Before a request goes to the compile server (`server` is its process ID), after `compiles` requests reached it.
    #[cfg(feature = "testing")]
    fn before_compile(&mut self, _compiles: u64, _server: u32) {}
    /// Each reply, written into `state` (and the block or decode in place) but not yet seen by the resident.
    #[cfg(feature = "testing")]
    fn reply(&mut self, _kind: ReplyKind, _area: Area, _state: &mut State) {}
}
impl Hook for () {}

/// Host exits of one [`run`].
#[derive(Default)]
pub struct Requests {
    /// Translation requests answered with a block, and the distinct RIPs among them when the caller asks (`Some`; the program
    /// driver: a boot's million-entry set cost about 0.1 us a request); single-step requests not included.
    pub blocks: u64,
    pub rips: Option<std::collections::HashSet<u64>>,
    /// M3: the VM's device ranges (shared by every vCPU thread; see [`crate::devices`]).
    pub devices: Option<std::sync::Arc<crate::devices::Devices>>,
    /// K2: system-instruction decodes answered ([`exec::REPLY_SYSTEM`], by kind; the test hooks not included), and
    /// entropy refills asked for ([`exec::REQUEST_ENTROPY`]).
    pub decodes: std::collections::BTreeMap<u32, u64>,
    pub entropy: u64,
}

/// Runs the vCPU (entered at the resident, see [`enter`]) until it stops; `stop_at` is a RIP at
/// which execution stops at a block boundary (blocks are fetched up to it, and
/// never cached at it, so reaching it is a miss). A vCPU that stays in the guest
/// for `watchdog` without a host exit is forced out ([`Stop::Watchdog`]); host
/// work between exits (compiles) is not counted. `cancelled` (a peer failed) ends the
/// run at the next host exit ([`Stop::Cancelled`]; the supervisor forces the vCPU out).
pub fn run(
    cpu: &Vcpu,
    area: Area,
    compiler: &mut Compiler,
    stop_at: Option<u64>,
    watchdog: Duration,
    requests: &mut Requests,
    stops: &Stops,
    cancelled: &AtomicBool,
    hook: &mut impl Hook,
) -> Result<Stop> {
    // The resident fetches nothing at the stop RIP (the run ends there).
    let mut state: State = area.read_record(exec::STATE_OFFSET);
    state.stop_at = stop_at.unwrap_or(u64::MAX);
    area.write_record(exec::STATE_OFFSET, state);
    // Perf chunk 10: reuse inside the resident, checked against the host's answers under the reuse check, and never into
    // a range the hook answers at (off when there are more of them than the resident keeps).
    let mut fences = [[0u64; 2]; 4];
    let ranges = hook.fences();
    for (f, r) in fences.iter_mut().zip(&ranges) {
        *f = [r.start, r.end];
    }
    let mode = if ranges.len() > fences.len() { cache::REUSE_OFF } else if compiler.check_reuse { cache::REUSE_CHECK } else { cache::REUSE_ON };
    let control = cache::LOOKUP_OFFSET + cache::CONTROL;
    area.write_record(control + std::mem::offset_of!(cache::Control, reuse), mode);
    area.write_record(control + std::mem::offset_of!(cache::Control, reuse_fences), fences);
    // When the vCPU last entered the guest (`perf::now`), 0 while it is in the host: two stores per exit, which the watchdog
    // samples (perf chunk 7; a channel send at each entry and exit woke it twice per exit). Dropping `done` ends it.
    let entered = &AtomicU64::new(0);
    let (done, ended) = std::sync::mpsc::channel::<()>();
    let id = cpu.id();
    // A halted vCPU (X2: parked in the VM by HLT) is idle, not stuck: the watchdog waits on; the driver decides when a
    // halted vCPU can never wake ([`end_when_halted`]). A scoped thread (R2 review): it borrows the words, and is joined
    // even when `serve` panics (unwinding drops `done` first, which ends it).
    let halted: &AtomicU64 = event_word(area.memory, area.vcpu, std::mem::offset_of!(cache::Events, halted));
    std::thread::scope(move |scope| {
        scope.spawn(move || {
            let limit = u64::try_from(watchdog.as_nanos()).unwrap_or(u64::MAX);
            // The entry last seen, when it is next checked, and the wait after a check that found it still in the guest:
            // `limit`, then 100 ms (a request racing its entry into `hv_vcpu_run` might not stop it).
            let (mut seen, mut due, mut again) = (0, 0, limit);
            loop {
                let (t, now) = (entered.load(SeqCst), crate::perf::now());
                if t != seen {
                    (seen, due, again) = (t, t.saturating_add(limit), limit);
                }
                // In the host it looks again after `limit` (at least 1 ms); an entry it then finds is checked at entry + `limit`.
                let wait = if t == 0 {
                    limit.max(1_000_000)
                } else if now < due {
                    due - now
                } else {
                    if halted.load(SeqCst) == 0 {
                        exit_vcpus(&mut [id]);
                        again = 100_000_000;
                    }
                    due = now.saturating_add(again);
                    again
                };
                if ended.recv_timeout(Duration::from_nanos(wait)) != Err(std::sync::mpsc::RecvTimeoutError::Timeout) {
                    break;
                }
            }
        });
        let stop = serve(cpu, area, compiler, stop_at, requests, stops, entered, cancelled, hook);
        // ponytail: a cancel the watchdog sent just as the guest exited stays pending on the vCPU and would end its next
        // `hv_vcpu_run` at once; harmless while a vCPU serves one run. Drain it (a zero-length run) if vCPUs get reused.
        drop(done);
        stop
    })
}
fn serve(
    cpu: &Vcpu,
    area: Area,
    compiler: &mut Compiler,
    stop_at: Option<u64>,
    requests: &mut Requests,
    stops: &Stops,
    entered: &AtomicU64,
    cancelled: &AtomicBool,
    hook: &mut impl Hook,
) -> Result<Stop> {
    // The host's own stop, once decided: the vCPU was told to park (it drops its shadows first).
    let mut parked = None;
    let v = area.vcpu;
    // The performance log: each part of the loop is charged to its kind (`perf::Recorder::lap`).
    let lap = |compiler: &mut Compiler, k: usize| {
        if let Some(r) = &mut compiler.perf {
            r.lap(k);
        }
    };
    loop {
        if cancelled.load(SeqCst) {
            return Ok(Stop::Cancelled);
        }
        entered.store(crate::perf::now(), SeqCst);
        stops.in_host[v].store(false, SeqCst);
        lap(compiler, crate::perf::SIGNAL);
        let exit = cpu.run_until_exit();
        lap(compiler, crate::perf::RUN);
        stops.in_host[v].store(true, SeqCst);
        entered.store(0, SeqCst);
        lap(compiler, crate::perf::SIGNAL);
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
                if !exec::entry_frame_rest_zero(&s.initial) {
                    return Err("the resident's entry frame holds a field it does not write".into());
                }
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
        lap(compiler, crate::perf::EXIT);
        let step = match state.request {
            exec::REQUEST_TRANSLATE => false,
            exec::REQUEST_STEP => true,
            // M3: a device access, completed (or refused) once; never parked (a driver's hook sees the reply).
            exec::REQUEST_DEVICE => {
                // A peer failed after this exit: the device is not accessed (the vCPU ends with the access undone).
                if cancelled.load(SeqCst) {
                    return Ok(Stop::Cancelled);
                }
                crate::devices::serve(area, &mut state, requests.devices.as_deref());
                #[cfg(feature = "testing")]
                hook.reply(ReplyKind::Device, area, &mut state);
                area.write_record(exec::STATE_OFFSET, state);
                lap(compiler, crate::perf::DEVICE);
                continue;
            }
            // L3 (amendment): a read reached a non-RAM page: claimed by a device, or unassigned (all-ones shadow, counted).
            exec::REQUEST_PAGE => {
                if cancelled.load(SeqCst) {
                    return Ok(Stop::Cancelled);
                }
                crate::devices::serve_page(area, &mut state, requests.devices.as_deref());
                area.write_record(exec::STATE_OFFSET, state);
                lap(compiler, crate::perf::PAGE);
                continue;
            }
                        // L3: a port access, made once on the port bus (never parked).
            exec::REQUEST_PORT => {
                if cancelled.load(SeqCst) {
                    return Ok(Stop::Cancelled);
                }
                crate::devices::serve_port(area, &mut state, requests.devices.as_deref());
                area.write_record(exec::STATE_OFFSET, state);
                lap(compiler, crate::perf::PORT);
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
                lap(compiler, crate::perf::IRQ);
                continue;
            }
            // K2: an RDRAND found the pool empty; it was topped up above.
            exec::REQUEST_ENTROPY => {
                requests.entropy += 1;
                state.reply = exec::REPLY_ENTROPY;
                area.write_record(exec::STATE_OFFSET, state);
                lap(compiler, crate::perf::ENTROPY);
                continue;
            }
            _ => return Err("translation exit without a request".into()),
        };
        let (began, cr3, rip, gpa, gpa2) = (compiler.stamp(), state.cpu.cr3, state.cpu.rip, state.fetch[0], state.fetch[1]);
        state.reply = match translation(area, compiler, &mut state, step, stop_at, requests, hook)? {
            Ok(reply) => reply,
            Err(stop) => {
                parked = Some(stop);
                exec::REPLY_PARK
            }
        };
        #[cfg(feature = "testing")]
        if parked.is_none() {
            hook.reply(ReplyKind::Translation, area, &mut state);
        }
        area.write_record(exec::STATE_OFFSET, state);
        lap(compiler, if step { crate::perf::STEP } else { crate::perf::TRANSLATE });
        if let Some(r) = &mut compiler.perf {
            r.request(cr3, rip, gpa, state.reply, began, step, state.generation, state.cpu.code_context(), gpa2);
        }
        lap(compiler, crate::perf::LOG_LINE);
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
    hook: &mut impl Hook,
) -> Result<std::result::Result<u64, Stop>> {
    let rip = state.cpu.rip;
    if Some(rip) == stop_at {
        return Ok(Err(Stop::Reached));
    }
    // (A step request may be at one: X2 steps the instruction after a shadow-setting one.)
    match hook.at(area, rip)? {
        Some(At::Stop(code)) => return Ok(Err(Stop::Hooked(code))),
        #[cfg(feature = "testing")]
        Some(At::System(system)) => {
            state.system = system;
            return Ok(Ok(exec::REPLY_SYSTEM));
        }
        None => {}
    }
    // Blocks end at the stop RIP and at the hook's next RIP.
    let hooks = hook.boundary(rip).into_iter().chain(stop_at).filter(|&l| l > rip).min();
    // S4: and before every armed instruction breakpoint, so each is the first instruction of a block (the resident checks
    // it there); one inside an instruction on this path is never reached and is passed over.
    let mut breakpoints: Vec<u64> = state.cpu.code_breakpoints().filter(|&b| b > rip).collect();
    let (code, more, next_fault, cut, bytes_len, nop_run, call) = loop {
        let limit = breakpoints.iter().copied().chain(hooks).min();
        let t = compiler.stamp();
        let fetched = fetched(area, state, limit)?;
        compiler.charge(crate::perf::FETCH, t);
        let Fetched { bytes, more, next_fault, cut } = match fetched {
            Ok(fetched) => fetched,
            // RIP's page faults: the resident delivers it (`state.fetch_fault`); nothing is compiled.
            Err(Stop::FetchFault(Fault::Page { .. } | Fault::Noncanonical)) => return Ok(Ok(exec::REPLY_FETCH_FAULT)),
            Err(stop) => return Ok(Err(stop)),
        };
        compiler.sse_gated = sse_gated(&state.cpu);
        compiler.avx_gated = avx_gated(&state.cpu);
        compiler.x87 = x87_args(&state.cpu);
        compiler.alignment_gated = state.cpu.bit_tests_cut() && !(step && state.plain_step != 0);
        compiler.cpl0 = state.cpu.cpl == 0 && !step;
        compiler.tsc = compiler.cpl0 && crate::clock::frequency() == 24_000_000;
        compiler.bulk = !step && !compiler.alignment_gated;
        let code = compiler.compile(&bytes, rip, step, hook)?;
        match &code {
            // The instruction at RIP runs past a breakpoint the bytes were cut at.
            Code::None(reason) if reason == "partial_decode" && limit != hooks && limit == Some(rip + bytes.len() as u64) => {
                breakpoints.retain(|&b| Some(b) != limit)
            }
            _ => {
                // Perf chunk 12: a translate request whose block is only no-operation instructions before a system
                // instruction is answered as their run ([`exec::system::NOP_RUN`]): the resident commits it and runs the
                // system instruction in the same entry. The bytes are those fexc translated, and so is the count.
                let nop_run = match &code {
                    Code::Block(b) if !step && b.end_reason == natemu_abi::code_object::end_reason::SYSTEM_INSTRUCTION && b.guest_bytes <= exec::system::MAX_NOP_RUN => {
                        bytes.get(..b.guest_bytes as usize).and_then(crate::system::nops).is_some_and(|n| n == b.guest_insns).then_some(b.guest_bytes)
                    }
                    _ => None,
                };
                // Compile-ahead measurement (log on only): the block ends in a call (e8, or ff /2), so its end is a return site.
                let call = match &code {
                    Code::Block(b) if compiler.perf.is_some() => bytes.get(b.last as usize..b.guest_bytes as usize).is_some_and(|t| {
                        let (o, _) = crate::system::opcode_at(t);
                        matches!(t.get(o..), Some([0xe8, ..])) || matches!(t.get(o..), Some([0xff, m, ..]) if m >> 3 & 7 == 2)
                    }),
                    _ => false,
                };
                break (code, more, next_fault, cut, bytes.len(), nop_run, call);
            }
        }
    };
    let ran_out = match &code {
        Code::Block(b) => b.end_reason == natemu_abi::code_object::end_reason::END_OF_INPUT,
        Code::None(reason) => reason == "partial_decode" || reason == "end_of_input",
        Code::System(_) => false,
    };
    if more && ran_out {
        return Ok(Ok(exec::REPLY_NEXT_PAGE));
    }
    if let Some(length) = nop_run {
        *requests.decodes.entry(exec::system::NOP_RUN).or_default() += 1;
        state.system = exec::System { kind: exec::system::NOP_RUN, length, ..exec::System::NONE };
        return Ok(Ok(exec::REPLY_SYSTEM));
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
    // Perf chunk 10, a template for reuse inside the resident (chunk 6's count): fexc's answer here is its answer for the
    // same bytes at any RIP of this half and page offset (the relocation proof) when it depended only on bytes of the
    // block's own pages before their end (so the frames say which), no limit cut them and no halving retry made it.
    let last = rip.wrapping_add(u64::from(block.guest_bytes).max(1) - 1) >> 12;
    let template = !step && !cut && !compiler.halved && block.end_reason != natemu_abi::code_object::end_reason::UNSUPPORTED_FORM
        && compiler.read < bytes_len && rip.wrapping_add(compiler.read.max(1) as u64 - 1) >> 12 <= last;
    if let Some(r) = &mut compiler.perf {
        r.template = template;
        // Compile-ahead measurement: the block's exits (its link targets and a call's return site), and whether each lies
        // on a page this request fetched (RIP's, or the next one when it was walked).
        let end = rip.wrapping_add(u64::from(block.guest_bytes));
        let fetched = |t: u64| t >> 12 == rip >> 12 || (state.fetch[1] != u64::MAX && t >> 12 == (rip >> 12).wrapping_add(1));
        r.block = (block.guest_bytes, call);
        for t in block.links.iter().map(|l| l.target).chain(call.then_some(end)) {
            if !r.candidates.iter().any(|&(u, _)| u == t) {
                r.candidates.push((t, fetched(t)));
            }
        }
    }
    state.relocatable = u64::from(template);
    let t = compiler.stamp();
    area.copy(exec::STAGING_OFFSET, &block.object);
    state.system = exec::System::NONE;
    if !step {
        requests.blocks += 1;
        if let Some(rips) = &mut requests.rips {
            rips.insert(rip);
        }
    }
    state.link_points = [exec::LinkPoint::NONE; exec::MAX_LINK_POINTS];
    state.link_points[..block.links.len()].copy_from_slice(&block.links);
    state.dispatch_exit = block.dispatch_exit.map_or(0, u64::from);
    state.rep_exits = block.links.iter().enumerate().filter(|(_, p)| Some(p.target) == block.rep_self).fold(0, |m, (k, _)| m | 1 << k);
    state.sti_first = u64::from(block.sti_first);
    compiler.charge(crate::perf::STAGE, t);
    Ok(Ok(exec::REPLY_ADMITTED))
}

#[cfg(test)]
mod tests {
    use super::*;
    /// Changes every answer `c` keeps (no other reference is held between compiles).
    fn edit(c: &mut Compiler, f: impl Fn(&mut Entry)) {
        for s in &c.answers.shards {
            let s = &mut *s.lock().unwrap();
            for e in s.cache.values_mut().chain(s.old.values_mut()).flatten() {
                f(Arc::get_mut(e).unwrap());
            }
        }
    }
    fn hit(a: &Answers, bytes: &[u8], rip: u64, limit: Option<u32>, gating: &[u64; GATING]) -> Option<Arc<Entry>> {
        a.find(bytes, rip, limit, gating).0
    }
    fn entry(rip: u64, owner: u8, prefix: &[u8], read: u32, gating: [u64; GATING]) -> Arc<Entry> {
        Arc::new(Entry { rip, owner, read, gating, prefix: prefix.into(), whole: false, answer: Answer::Object(prefix.into()) })
    }

    /// Perf chunk 13: entries under one key (same limit, half, page offset and lead bytes) are told apart by the rest of
    /// what fexc read (later bytes, consulted gating values), never confused with another half, offset or limit; the
    /// answer kept for the requested RIP comes first.
    #[test]
    fn shared_answers_keep_colliding_keys_apart() {
        let a = Answers::new(u64::MAX);
        let g = [0; GATING];
        let sse = [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0];
        a.insert(None, entry(0x1100, 0, &[1, 2, 3, 4, 5], 0, g), &[1, 2, 3, 4, 5], 0x1100);
        a.insert(None, entry(0x2100, 1, &[1, 2, 3, 4, 6], 0, g), &[1, 2, 3, 4, 6], 0x2100);
        a.insert(None, entry(0x3100, 2, &[1, 2, 3, 4, 7], 1, sse), &[1, 2, 3, 4, 7], 0x3100); // read SSE gating 1
        let owner = |bytes: &[u8], rip, limit, gating: &[u64; GATING]| hit(&a, bytes, rip, limit, gating).map(|e| (e.owner, e.rip));
        assert_eq!(owner(&[1, 2, 3, 4, 5, 9], 0x9100, None, &g), Some((0, 0x1100)));
        assert_eq!(owner(&[1, 2, 3, 4, 6], 0x9100, None, &g), Some((1, 0x2100)));
        assert_eq!(owner(&[1, 2, 3, 4, 7], 0x9100, None, &g), None); // other SSE gating
        assert_eq!(owner(&[1, 2, 3, 4, 7], 0x9100, None, &sse), Some((2, 0x3100)));
        assert_eq!(owner(&[1, 2, 3, 4, 8], 0x9100, None, &g), None);
        assert_eq!(owner(&[1, 2, 3, 4, 5], 0x9200, None, &g), None); // another page offset
        assert_eq!(owner(&[1, 2, 3, 4, 5], 1 << 63 | 0x1100, None, &g), None); // the other half
        assert_eq!(owner(&[1, 2, 3, 4, 5], 0x1100, Some(1), &g), None); // another limit
        // A second answer for the same bytes kept for the requested RIP is preferred; a duplicate's insert names the holder.
        let (held, _) = a.insert(None, entry(0x9100, 3, &[1, 2, 3, 4, 5], 0, g), &[1, 2, 3, 4, 5], 0x9100);
        assert_eq!(held, Some(0));
        assert_eq!(owner(&[1, 2, 3, 4, 5], 0x9100, None, &g), Some((3, 0x9100)));
        assert_eq!(owner(&[1, 2, 3, 4, 5], 0x5100, None, &g), Some((0, 0x1100)));
    }

    /// Perf chunk 13: an answer dropped from the map by later inserts (two generation flips of its shard) between a lookup
    /// and its use stays whole for the holder; the map no longer finds it.
    #[test]
    fn an_answer_evicted_between_lookup_and_use_stays_whole() {
        let a = Answers::new(u64::MAX);
        let g = [0; GATING];
        a.insert(None, entry(0x1100, 0, &[0xaa; 32], 0, g), &[0xaa; 32], 0x1100);
        let held = hit(&a, &[0xaa; 32], 0x1100, None, &g).unwrap();
        // Same limit and page offset: the same shard; distinct bytes.
        for i in 0..2 * GENERATION / SHARDS + 1 {
            let b = (i as u64).to_le_bytes();
            a.insert(None, entry(0x1100 + (i as u64 + 1) * 0x1000, 1, &b, 0, g), &b, 0x1100);
        }
        assert!(hit(&a, &[0xaa; 32], 0x1100, None, &g).is_none());
        assert_eq!((held.rip, held.prefix.as_slice(), Arc::strong_count(&held)), (0x1100, &[0xaa; 32][..], 1));
        assert!(matches!(&held.answer, Answer::Object(o) if o == &[0xaa; 32]));
    }

    /// Perf chunk 13: four threads insert and look up in one hot shard (every RIP at one page offset) while it flips
    /// generations many times: every answer found is one kept for the bytes asked, answers kept by other threads are found,
    /// a reference held across the flips stays whole, and the shard stays within its bound.
    #[test]
    fn shared_answers_under_concurrent_insert_lookup_and_eviction() {
        let a = Answers::new(u64::MAX);
        let g = [0; GATING];
        let bound = GENERATION / SHARDS;
        // The lead bytes are the count's, so the keys differ (no long bucket); the thread's number follows.
        let bytes = |t: u64, i: u64| [i.to_le_bytes(), t.to_le_bytes()].concat();
        let rip = |t: u64, i: u64| 0x7f00_0000_0100 + (t << 32) + (i << 12);
        let n = 3 * bound as u64;
        let cross = AtomicU64::new(0);
        std::thread::scope(|s| {
            for t in 0..4u64 {
                let (a, cross) = (&a, &cross);
                s.spawn(move || {
                    let mut held = None;
                    for i in 0..n {
                        let b = bytes(t, i);
                        a.insert(None, entry(rip(t, i), t as u8, &b, 0, g), &b, rip(t, i));
                        // Another thread's answer at about the same point of its run, looked up at this thread's RIP.
                        let (o, j) = ((t + 1) % 4, i.saturating_sub(8));
                        let ob = bytes(o, j);
                        if let Some(e) = hit(a, &ob, rip(t, j), None, &g) {
                            assert!(e.matches(&ob, &g) && e.prefix == ob && e.owner == o as u8 && e.rip == rip(o, j));
                            cross.fetch_add(1, Relaxed);
                            held.get_or_insert(e);
                        }
                    }
                    let e = held.expect("no cross-thread hit");
                    assert!(e.owner == ((t + 1) % 4) as u8 && matches!(&e.answer, Answer::Object(o) if *o == e.prefix));
                });
            }
        });
        let (_, kept) = a.kept();
        // 4n = 12 bounds inserted into one shard: it flipped at least four times and keeps at most two generations.
        assert!(cross.load(Relaxed) > 0 && kept <= 2 * bound && kept < 4 * n as usize, "{} {kept}", cross.load(Relaxed));
        let k = key(None, rip(0, 0), &[]);
        let one = std::hash::BuildHasher::hash_one(&a.pick, (k.0, k.1)) as usize % SHARDS;
        let s = a.shards[one].lock().unwrap();
        // Since its last flip the shard holds at least a generation's answers (hits only move them to the newer one).
        assert!(!s.old.is_empty() && s.cached <= bound && kept >= bound, "{} {}", s.old.len(), s.cached);
    }

    /// With `FEXC` set (perf chunk 13): two compilers sharing one map on two threads: the second's request at another RIP is
    /// the first's answer, relocated and admitted at its own RIP (checked against its own compile), counted by source.
    #[test]
    fn compilers_share_answers_across_threads() {
        let Some(fexc) = std::env::var_os("FEXC") else { return };
        let answers = std::sync::OnceLock::new();
        let user = include_bytes!("../tests/relocation/user.src");
        let run = |v: u8, rip: u64| {
            let mut c = Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
            (c.vcpu, c.check_reuse) = (v, true);
            c.share(&answers);
            let Code::Block(b) = c.compile(user, rip, false, &mut ()).unwrap() else { panic!("no block") };
            (b, c.compiles, c.relocated, c.reuse_checks, c.hits_by_source)
        };
        let (a, ..) = std::thread::scope(|s| s.spawn(|| run(0, 0x7f12_3456_7100)).join().unwrap());
        let (b, compiles, relocated, checks, by) = std::thread::scope(|s| s.spawn(|| run(1, 0x7f12_3456_8100)).join().unwrap());
        assert_eq!((a.guest_insns, b.guest_insns, compiles, relocated, checks, by), (4, 4, 0, 1, 1, [1, 0, 0, 0]));
        assert_ne!(a.object, b.object);
        assert_eq!(answers.get().unwrap().kept().1, 1);
    }
    /// With `FEXC` set (R2 review): an answer with a spill area is not a host error. The block is translated again with half
    /// its instructions; a spill area at one instruction is the typed stop `spill_area`. fexc spills on none of the corpora,
    /// so the cached answers are given a spill area (header `spill_bytes`).
    #[test]
    fn spill_areas_retry_then_stop() {
        let Some(fexc) = std::env::var_os("FEXC") else { return };
        let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        let code = [0x48, 0x01, 0xd8, 0x48, 0x01, 0xd8, 0x48, 0x01, 0xd8, 0xc3]; // add rax, rbx x3; ret
        let spill = |c: &mut super::Compiler| {
            edit(c, |e| {
                if let Answer::Object(o) = &mut e.answer {
                    o[60..64].copy_from_slice(&8u32.to_le_bytes());
                }
            })
        };
        let insns = |c: &mut super::Compiler| match c.compile(&code, 0x1000, false, &mut ()).unwrap() {
            Code::Block(b) => Ok(b.guest_insns),
            Code::None(reason) => Err(reason),
            Code::System(_) => panic!("system"),
        };
        assert_eq!(insns(&mut c), Ok(4));
        spill(&mut c);
        assert_eq!(insns(&mut c), Ok(2)); // translated again at 2
        c.compile(&code, 0x1000, true, &mut ()).unwrap(); // the step translation (limit 1) cached too
        spill(&mut c);
        assert_eq!(insns(&mut c), Err("spill_area".into()));
    }

    /// With `FEXC` set: a repeated request is answered from the cache, a different RIP, bytes or block limit is compiled.
    #[test]
    fn compiler_keys_answers_by_what_fexc_read() {
        let Some(fexc) = std::env::var_os("FEXC") else { return };
        let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        let code = [0x48, 0x01, 0xd8, 0xc3];
        let object = |c: &mut super::Compiler, code: &[u8], rip| match c.compile(code, rip, false, &mut ()).unwrap() {
            Code::Block(b) => b.object,
            _ => panic!("no block"),
        };
        let first = object(&mut c, &code, 0x1000);
        assert_eq!((c.compiles, c.hits), (1, 0));
        assert_eq!(object(&mut c, &code, 0x1000), first);
        assert_eq!((c.compiles, c.hits), (1, 1));
        // Perf chunk 4: the same bytes at another RIP of the half and page offset: the answer relocated (no moves here).
        assert_eq!(object(&mut c, &code, 0x2000)[24..32], 0x2000u64.to_le_bytes());
        assert_eq!((c.compiles, c.hits, c.relocated), (1, 2, 1));
        object(&mut c, &[0x48, 0x29, 0xd8, 0xc3], 0x1000);
        c.max_instructions = Some(1);
        object(&mut c, &code, 0x1000);
        assert_eq!((c.compiles, c.hits), (3, 2));
        // A system instruction at the RIP is decoded (and cached with fexc's answer); one later in the block cuts it.
        let mov_cr3 = [0x0f, 0x22, 0xd8, 0x90];
        let decoded = |c: &mut super::Compiler, rip| match c.compile(&mov_cr3, rip, false, &mut ()).unwrap() {
            Code::System(s) => Some(s),
            _ => None,
        };
        let s = decoded(&mut c, 0x3000).unwrap();
        assert_eq!((s.kind, s.cr, s.reg, s.length), (exec::system::MOV_TO_CR, 3, 0, 3));
        assert_eq!(decoded(&mut c, 0x3000), Some(s));
        assert_eq!((c.compiles, c.hits), (4, 3));
        assert!(matches!(c.compile(&[0xf4], 0x3000, false, &mut ()).unwrap(), Code::System(s) if s.kind == exec::system::HLT)); // X2
        // SYSENTER is decoded since X1c (the resident delivers its fault); this test predates it (it expected no code).
        assert!(matches!(c.compile(&[0x0f, 0x34], 0x3000, false, &mut ()).unwrap(), Code::System(s) if s.kind == exec::system::SYSENTER));
        // Longer than 15 bytes (fexc series28): #GP(0) through the resident.
        let long = [&[0x66; 4][..], &[0x4a, 0xc7, 0x84, 0x83, 0, 1, 0, 0, 0, 0, 0, 0]].concat();
        assert!(matches!(c.compile(&long, 0x3000, false, &mut ()).unwrap(), Code::System(s) if s.kind == exec::system::TOO_LONG));
        assert!(matches!(c.compile(&[0x90, 0x0f, 0x22, 0xd8], 0x3000, false, &mut ()).unwrap(), Code::Block(_)));
        // P3: an answer is kept by what fexc read. Bytes after the block's end (`ret`) and a gating argument its forms do not
        // consult leave the request the same; the bytes of a block that ran to the end of its input, or an SSE form under other
        // SSE gating, do not.
        let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        let tail = |t: u8| [&code[..], &[t; 64]].concat();
        let first = object(&mut c, &tail(0x90), 0x1000);
        assert_eq!(object(&mut c, &tail(0xcc), 0x1000), first);
        c.sse_gated = true;
        c.x87 = vec!["--x87-gated".into()];
        assert_eq!(object(&mut c, &tail(0x0f), 0x1000), first);
        assert_eq!((c.compiles, c.hits), (1, 2));
        object(&mut c, &code[..3], 0x1000);
        object(&mut c, &[&code[..3], &[0x90]].concat(), 0x1000);
        assert_eq!((c.compiles, c.hits), (3, 2));
        let movaps = [0x0f, 0x28, 0xc1, 0xc3];
        assert!(matches!(c.compile(&movaps, 0x2000, false, &mut ()).unwrap(), Code::System(s) if s.kind == exec::system::SSE_GATED));
        c.sse_gated = false;
        object(&mut c, &movaps, 0x2000);
        assert_eq!((c.compiles, c.hits), (5, 2));
    }

    /// With `FEXC` set (perf chunk 15): at CPL 0 the one-byte CLI is translated inline anywhere and STI where it is not the
    /// last instruction of its block (its exit to its own RIP a link point; perf chunk 25: first too, the block marked
    /// `sti_first`); a prefix, a following system
    /// instruction, REP string or LOCK NEG keeps it the resident's; a CPL-3 request cuts both, and never reuses a CPL-0
    /// answer that read the flag; admission refuses the inline forms outside a CPL-0 block.
    /// Perf chunk 26: `--bulk` gives REP STOS (64-bit addressing) its bulk loop and nothing else changes; without it no loop;
    /// the answer is keyed by the flag (fexc reads it only for such a REP STOS), and a bulk object admitted for a request
    /// without the flag (a step, or under alignment gating) is refused.
    /// Perf chunk 26: MOV r64, CR2 of a CPL-0 request is translated (a load of CR2's guest-state field, also into R9 by REX.B);
    /// with REX.R (CR10), another control register, a 66 prefix, or at CPL 3 it stays the resident's; admission refuses the
    /// field's load in an object admitted as a CPL-3 request's.
    #[test]
    fn cpl0_translates_mov_from_cr2() {
        let Some(fexc) = std::env::var_os("FEXC") else { return };
        let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        (c.shape, c.cpl0) = (true, true);
        let compile = |c: &mut super::Compiler, code: &[u8]| c.compile(code, 0x1000, false, &mut ()).unwrap();
        for code in [&[0x0f, 0x20, 0xd0, 0xc3][..], &[0x41, 0x0f, 0x20, 0xd1, 0xc3], &[0x48, 0x0f, 0x20, 0xd0, 0xc3]] {
            assert!(matches!(compile(&mut c, code), Code::Block(b) if b.guest_insns == 2), "{code:02x?}");
        }
        for code in [&[0x44, 0x0f, 0x20, 0xd0, 0xc3][..], &[0x0f, 0x20, 0xd8, 0xc3], &[0x66, 0x0f, 0x20, 0xd0, 0xc3]] {
            assert!(matches!(compile(&mut c, code), Code::System(_)), "{code:02x?}");
        }
        let code = [0x0f, 0x20, 0xd0, 0xc3];
        let Answer::Object(o) = c.answers.find(&code, 0x1000, None, &c.gating()).0.unwrap().answer.clone() else { panic!() };
        assert!(admission::admit(&o, 0x1000, &code, u64::MAX, true, true).is_ok());
        assert!(admission::admit(&o, 0x1000, &code, u64::MAX, false, false).err().unwrap().contains("frame access"));
        c.cpl0 = false;
        assert!(matches!(compile(&mut c, &code), Code::System(_)));
    }

    #[test]
    fn bulk_rep_stos_only_when_asked() {
        let Some(fexc) = std::env::var_os("FEXC") else { return };
        let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        c.shape = true;
        let block = |c: &mut super::Compiler, code: &[u8]| match c.compile(code, 0x1000, false, &mut ()).unwrap() {
            Code::Block(b) => b,
            _ => panic!("{code:02x?}"),
        };
        let stos = [0xf3, 0x48, 0xab];
        c.bulk = true;
        assert!(block(&mut c, &stos).bulk && block(&mut c, &[0x66, 0xf3, 0xab]).bulk && block(&mut c, &[0xf3, 0xaa]).bulk);
        // 0x67 (EDI/ECX), MOVS, and other code: no loop.
        for code in [&[0x67, 0xf3, 0x48, 0xab][..], &[0xf3, 0x48, 0xa5], &[0x48, 0xab, 0xc3], &[0x90, 0xc3]] {
            assert!(!block(&mut c, code).bulk, "{code:02x?}");
        }
        let Answer::Object(o) = c.answers.find(&stos, 0x1000, None, &c.gating()).0.unwrap().answer.clone() else { panic!() };
        c.bulk = false;
        assert!(!block(&mut c, &stos).bulk);
        let e = c.answered(&Answer::Object(o), &stos, 0x1000, &mut ()).err().unwrap();
        assert!(e.contains("--bulk"), "{e}");
    }

    #[test]
    fn cpl0_translates_cli_and_sti_inline() {
        let Some(fexc) = std::env::var_os("FEXC") else { return };
        let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        (c.shape, c.cpl0) = (true, true);
        let block = |c: &mut super::Compiler, code: &[u8]| match c.compile(code, 0x1000, false, &mut ()).unwrap() {
            Code::Block(b) => Ok(b),
            Code::System(s) => Err(s.kind),
            Code::None(r) => panic!("{r}"),
        };
        let inline = block(&mut c, &[0x90, 0xfa, 0xfb, 0x48, 0xff, 0xc0, 0xc3]).unwrap(); // nop; cli; sti; inc rax; ret
        assert_eq!(inline.guest_insns, 5);
        assert!(inline.links.iter().any(|p| p.target == 0x1002), "the STI's exit to its own RIP");
        assert_eq!(block(&mut c, &[0xfa, 0xc3]).unwrap().guest_insns, 2); // CLI first
        assert_eq!(block(&mut c, &[0x90, 0xfb, 0xfa, 0xc3]).unwrap().guest_insns, 4); // sti; cli
        // Perf chunk 25: STI first (sti; nop; ret): inline, the block marked, its exit to its own start a link point.
        let first = block(&mut c, &[0xfb, 0x90, 0xc3]).unwrap();
        assert!(first.guest_insns == 3 && first.sti_first && first.links.iter().any(|p| p.target == 0x1000));
        assert!(!inline.sti_first && !block(&mut c, &[0xfa, 0xc3]).unwrap().sti_first);
        // Admission's PC table starts at guest offset 0: an object whose entries at offset 0 (the block entry and the STI)
        // name the NOP after the STI instead (which would leave the STI unseen and the block unmarked) is refused.
        let Answer::Object(mut o) = c.answers.find(&[0xfb, 0x90, 0xc3], 0x1000, None, &c.gating()).0.unwrap().answer.clone() else { panic!() };
        let h: natemu_abi::code_object::Header = unsafe { o.as_ptr().cast::<natemu_abi::code_object::Header>().read_unaligned() };
        assert!(admission::admit(&o, 0x1000, &[0xfb, 0x90, 0xc3], u64::MAX, true, true).is_ok_and(|a| a.sti_first));
        for k in 0..h.pcs_count as usize {
            let at = h.pcs_offset as usize + k * size_of::<natemu_abi::code_object::PcEntry>() + std::mem::offset_of!(natemu_abi::code_object::PcEntry, guest_offset);
            if o[at..at + 4] == [0; 4] {
                o[at..at + 4].copy_from_slice(&1u32.to_le_bytes());
            }
        }
        assert!(admission::admit(&o, 0x1000, &[0xfb, 0x90, 0xc3], u64::MAX, true, true).err().unwrap().contains("PC table"));
        for kept in [&[0x90, 0xfb, 0xf4][..], &[0x90, 0xfb, 0xf3, 0xaa], &[0x90, 0xfb, 0xf0, 0xf7, 0x18], &[0x90, 0x66, 0xfb, 0x90, 0xc3], &[0x90, 0xfb]] {
            assert_eq!(block(&mut c, kept).unwrap().guest_insns, 1, "{kept:02x?}"); // sti; hlt / rep stosb / lock neg / 66 sti / last
        }
        // First, but before a system instruction, a self-exit, or alone: still the resident's.
        for kept in [&[0xfb, 0xf4][..], &[0xfb, 0xf3, 0xaa], &[0xfb, 0xf0, 0xf7, 0x18], &[0xfb]] {
            assert_eq!(block(&mut c, kept).err(), Some(exec::system::STI), "{kept:02x?}");
        }
        // RDTSC with `--tsc` (CPL 0, a 24 MHz counter), first or not; with REX, or without the flag, the resident's.
        c.tsc = true;
        assert_eq!(block(&mut c, &[0x0f, 0x31, 0xc3]).unwrap().guest_insns, 2);
        assert_eq!(block(&mut c, &[0x90, 0x0f, 0x31, 0xfb, 0x90, 0xc3]).unwrap().guest_insns, 5); // rdtsc; sti inline too
        assert_eq!(block(&mut c, &[0x48, 0x0f, 0x31, 0xc3]).err(), Some(exec::system::RDTSC));
        let Answer::Object(o) = c.answers.find(&[0x0f, 0x31, 0xc3], 0x1000, None, &c.gating()).0.unwrap().answer.clone() else { panic!() };
        assert!(admission::admit(&o, 0x1000, &[0x0f, 0x31, 0xc3], u64::MAX, true, false).err().unwrap().contains("RDTSC"));
        // A vCPU without FEAT_ECV (feature 22, `ecv`): fexc reads the count by `isb; mrs cntvct_el0`, admitted with the shape.
        let mut d = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX & !(1 << 22), None);
        (d.shape, d.cpl0, d.tsc) = (true, true, true);
        assert_eq!(block(&mut d, &[0x0f, 0x31, 0xc3]).unwrap().guest_insns, 2);
        c.tsc = false;
        assert_eq!(block(&mut c, &[0x0f, 0x31, 0xc3]).err(), Some(exec::system::RDTSC));
        c.cpl0 = false;
        assert_eq!(block(&mut c, &[0x90, 0xfa, 0xfb, 0x48, 0xff, 0xc0, 0xc3]).unwrap().guest_insns, 1);
        assert_eq!(block(&mut c, &[0xfa, 0xc3]).err(), Some(exec::system::CLI));
        assert_eq!(block(&mut c, &[0xfb, 0x90, 0xc3]).err(), Some(exec::system::STI)); // a CPL-3 STI first: never the CPL-0 answer
        let Answer::Object(o) = c.answers.find(&[0x90, 0xfa, 0xfb, 0x48, 0xff, 0xc0, 0xc3], 0x1000, None, &{ let mut g = c.gating(); g[8] = 1; g[9] = 0; g }).0.unwrap().answer.clone() else { panic!() };
        let source = [0x90, 0xfa, 0xfb, 0x48, 0xff, 0xc0, 0xc3];
        assert!(admission::admit(&o, 0x1000, &source, u64::MAX, true, true).is_ok());
        assert!(admission::admit(&o, 0x1000, &source, u64::MAX, false, false).err().unwrap().contains("CLI/STI"));
    }

    /// With `FEXC` set (perf chunk 25): STMXCSR m32 and VSTMXCSR m32 are translated (their stores access sites; the MXCSR
    /// field's load admitted with the code shape), at either CPL; under the SSE or AVX gating or as LDMXCSR/VLDMXCSR they
    /// stay the resident's, and with a 66/F2/F3 prefix (not STMXCSR) fexc makes no code.
    #[test]
    fn stmxcsr_is_translated() {
        let Some(fexc) = std::env::var_os("FEXC") else { return };
        let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        c.shape = true;
        let block = |c: &mut super::Compiler, code: &[u8]| match c.compile(code, 0x1000, false, &mut ()).unwrap() {
            Code::Block(b) => Ok(b),
            Code::System(s) => Err(s.kind),
            Code::None(r) => panic!("{r}"),
        };
        // stmxcsr [rax]; ret / stmxcsr fs:[rbx + 8*rcx + 0x40]; ret / addss xmm0, xmm1; stmxcsr [rsp - 4]; ret / vstmxcsr [rdi]; ret
        for code in [&[0x0f, 0xae, 0x18, 0xc3][..], &[0x64, 0x0f, 0xae, 0x5c, 0xcb, 0x40, 0xc3], &[0xf3, 0x0f, 0x58, 0xc1, 0x0f, 0xae, 0x5c, 0x24, 0xfc, 0xc3],
                     &[0xc5, 0xf8, 0xae, 0x1f, 0xc3]] {
            let b = block(&mut c, code).unwrap();
            assert!(b.guest_insns >= 2, "{code:02x?}");
            let Answer::Object(o) = c.answers.find(code, 0x1000, None, &c.gating()).0.unwrap().answer.clone() else { panic!() };
            for cpl0 in [false, true] {
                assert!(admission::admit(&o, 0x1000, code, u64::MAX, true, cpl0).is_ok(), "{code:02x?}");
            }
        }
        // 66, F3 or F2 before 0f ae /3 m: not STMXCSR (no code; the host's), whatever map FEX files it under.
        for code in [&[0x66, 0x0f, 0xae, 0x18, 0xc3][..], &[0xf3, 0x0f, 0xae, 0x18, 0xc3], &[0xf2, 0x0f, 0xae, 0x18, 0xc3]] {
            assert!(matches!(c.compile(code, 0x1000, false, &mut ()).unwrap(), Code::None(_)), "{code:02x?}");
        }
        assert_eq!(block(&mut c, &[0x0f, 0xae, 0x10, 0xc3]).err(), Some(exec::system::LDMXCSR));
        assert_eq!(block(&mut c, &[0xc5, 0xf8, 0xae, 0x17, 0xc3]).err(), Some(exec::system::VLDMXCSR));
        c.sse_gated = true;
        assert_eq!(block(&mut c, &[0x0f, 0xae, 0x18, 0xc3]).err(), Some(exec::system::SSE_GATED));
        assert!(block(&mut c, &[0xc5, 0xf8, 0xae, 0x1f, 0xc3]).is_ok());
        c.sse_gated = false;
        c.avx_gated = true;
        assert_eq!(block(&mut c, &[0xc5, 0xf8, 0xae, 0x1f, 0xc3]).err(), Some(exec::system::AVX_GATED));
    }

    /// With `FEXC` set (perf chunk 17, fexc patch 0033): at CPL 0 the plain SWAPGS is translated inline, first or not, twice in
    /// one block, and as the instruction after an inline STI; with REX, LOCK or 66 it is the resident's, and a CPL-3 request
    /// cuts it (never reusing the CPL-0 answer). Admission refuses the inline form, and its accesses to the KERNEL_GS_BASE
    /// field, outside a CPL-0 block.
    #[test]
    fn cpl0_translates_swapgs_inline() {
        let Some(fexc) = std::env::var_os("FEXC") else { return };
        let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        (c.shape, c.cpl0) = (true, true);
        let block = |c: &mut super::Compiler, code: &[u8]| match c.compile(code, 0x1000, false, &mut ()).unwrap() {
            Code::Block(b) => Ok(b),
            Code::System(s) => Err(s.kind),
            Code::None(r) => panic!("{r}"),
        };
        // endbr64; swapgs; mov rax, gs:[16]; swapgs; nop dword [rax]; ret (Linux's entry and exit shapes)
        let linux = [0xf3, 0x0f, 0x1e, 0xfa, 0x0f, 0x01, 0xf8, 0x65, 0x48, 0x8b, 0x04, 0x25, 0x10, 0, 0, 0, 0x0f, 0x01, 0xf8, 0x0f, 0x1f, 0x00, 0xc3];
        assert_eq!(block(&mut c, &linux).unwrap().guest_insns, 6);
        assert_eq!(block(&mut c, &[0x0f, 0x01, 0xf8, 0xc3]).unwrap().guest_insns, 2); // first
        assert_eq!(block(&mut c, &[0x90, 0xfb, 0x0f, 0x01, 0xf8, 0xc3]).unwrap().guest_insns, 4); // sti; swapgs: both inline
        for kept in [&[0x48, 0x0f, 0x01, 0xf8, 0xc3][..], &[0xf0, 0x0f, 0x01, 0xf8, 0xc3], &[0x66, 0x0f, 0x01, 0xf8, 0xc3]] {
            assert_eq!(block(&mut c, kept).err(), Some(exec::system::SWAPGS), "{kept:02x?}");
        }
        c.cpl0 = false;
        assert_eq!(block(&mut c, &[0x0f, 0x01, 0xf8, 0xc3]).err(), Some(exec::system::SWAPGS));
        assert_eq!(block(&mut c, &linux).unwrap().guest_insns, 1); // endbr64, cut before the SWAPGS
        let Answer::Object(o) = c.answers.find(&linux, 0x1000, None, &{ let mut g = c.gating(); g[8] = 1; g }).0.unwrap().answer.clone() else { panic!() };
        assert!(admission::admit(&o, 0x1000, &linux, u64::MAX, true, true).is_ok());
        assert!(admission::admit(&o, 0x1000, &linux, u64::MAX, false, false).err().unwrap().contains("SWAPGS"));
    }

    /// With `FEXC` set (perf chunk 4): answers are reused at other RIPs of the same half and page offset, under the reuse
    /// check (each also compiled and compared); the relocator's refusals are compiled, the reverse canonical case included.
    #[test]
    fn answers_are_reused_across_addresses() {
        let Some(fexc) = std::env::var_os("FEXC") else { return };
        let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        c.check_reuse = true;
        let block = |c: &mut super::Compiler, code: &[u8], rip| match c.compile(code, rip, false, &mut ()).unwrap() {
            Code::Block(b) => b,
            _ => panic!("no block"),
        };
        let user = include_bytes!("../tests/relocation/user.src");
        let a = block(&mut c, user, 0x7f12_3456_7100);
        let b = block(&mut c, user, 0x7f12_3456_8100);
        assert_eq!((a.guest_insns, b.guest_insns, c.compiles, c.relocated, c.reuse_checks), (4, 4, 1, 1, 1)); // the check's compile is not counted
        assert_ne!(a.object, b.object); // other RIP values
        block(&mut c, user, 0x7f12_0000_0100); // move length: refused, compiled
        assert_eq!((c.compiles, c.relocated, c.relocation_refused), (2, 1, 1));
        // add rax, rbx; jmp +0x7fffffff: cut before the jmp at the half's top; the whole block 15 MiB lower.
        let cut = include_bytes!("../tests/relocation/cut.src");
        assert_eq!(block(&mut c, cut, 0x7fff_ff00_0100).guest_bytes, 3);
        assert_eq!(block(&mut c, cut, 0x7ff0_ff00_0100).guest_bytes, 8);
        assert_eq!((c.compiles, c.relocated, c.relocation_refused), (4, 1, 2));
        // A system instruction (no code from fexc, decided from the bytes): reused, decoded at the new RIP.
        let mov_cr3 = [0x0f, 0x22, 0xd8, 0x90];
        for rip in [0x7f00_0000_3000, 0x7f00_1234_5000] {
            assert!(matches!(c.compile(&mov_cr3, rip, false, &mut ()).unwrap(), Code::System(s) if s.kind == exec::system::MOV_TO_CR));
        }
        assert_eq!((c.compiles, c.relocated, c.reuse_checks), (5, 2, 2));
    }

    /// With `FEXC` set, the reuse check's negative control: a kept object whose guest-RIP literal value is tampered with
    /// relocates (the relocator does not judge values) but is not fexc's answer at the new RIP: under the check a host
    /// error; without it admission refuses the relocated object and the request is compiled (a counted fallback).
    #[test]
    fn reuse_check_catches_a_tampered_relocation() {
        let Some(fexc) = std::env::var_os("FEXC") else { return };
        let user = include_bytes!("../tests/relocation/user.src");
        for check in [true, false] {
            let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
            c.check_reuse = check;
            assert!(matches!(c.compile(user, 0x7f12_3456_7100, false, &mut ()), Ok(Code::Block(_))));
            edit(&mut c, |e| {
                if let Answer::Object(o) = &mut e.answer {
                    let word = |o: &[u8], at: usize| u32::from_le_bytes(o[at..at + 4].try_into().unwrap()) as usize;
                    let (at, n) = (word(o, 80), word(o, 84));
                    let literal = (0..n).map(|i| at + 16 * i).find(|&r| o[r + 4] == natemu_abi::code_object::RELOC_GUEST_RIP_LITERAL).unwrap();
                    let v = u64::from_le_bytes(o[literal + 8..literal + 16].try_into().unwrap());
                    o[literal + 8..literal + 16].copy_from_slice(&(v + 0x10).to_le_bytes());
                }
            });
            let r = c.compile(user, 0x7f12_3456_8100, false, &mut ());
            if check {
                assert!(r.as_ref().is_err_and(|e| e.contains("reuse check")), "{:?}", r.err());
            } else {
                assert!(matches!(r, Ok(Code::Block(_))));
                assert_eq!((c.compiles, c.relocated, c.relocation_fallbacks), (2, 0, 1));
            }
        }
    }

    /// With `FEXC` set: the persistent cache answers a later compiler (no request reaches its fexc); an entry forged under a
    /// request's key (here: an object whose code jumps through x28, the resident's state register) is refused by admission,
    /// never staged.
    #[test]
    fn persistent_cache_answers_and_forgeries_are_refused() {
        let Some(fexc) = std::env::var_os("FEXC") else { return };
        let (fexc, dir) = (std::path::PathBuf::from(fexc), std::env::temp_dir().join(format!("natemu-pc-{}", std::process::id())));
        let _ = std::fs::remove_dir_all(&dir);
        let open = |d: &str| Some(std::sync::Arc::new(crate::compile_cache::DiskCache::open(&dir.join(d), &fexc, 1 << 30).unwrap()));
        let code = [0x48, 0x01, 0xd8, 0xc3];
        let new = |disk| super::Compiler { disk, ..super::Compiler::with_features(&fexc, u64::MAX, None) };
        let block = |c: &mut super::Compiler| match c.compile(&code, 0x5000, false, &mut ()) {
            Ok(Code::Block(b)) => Ok(b.object),
            Ok(_) => Err("no block".to_string()),
            Err(e) => Err(e),
        };
        let mut c = new(open("good"));
        let first = block(&mut c).unwrap();
        let body = c.disk_body(&code, 0x5000, None);
        drop(c); // writes the pack
        let mut c = new(open("good"));
        assert_eq!((block(&mut c).unwrap(), c.compiles, c.disk_hits), (first, 0, 1));
        // Perf chunk 4: kept on disk with the RIP it was compiled at, used at another of its half and page offset.
        let mut moved = new(open("good"));
        assert!(matches!(moved.compile(&code, 0x9000, false, &mut ()), Ok(Code::Block(b)) if b.object[24..32] == 0x9000u64.to_le_bytes()));
        assert_eq!((moved.compiles, moved.disk_hits, moved.relocated), (0, 1, 1));
        let disk = c.disk.clone().unwrap();
        let stored = disk.get(&disk.key(&body)).unwrap();
        assert_eq!(stored[..8], 0x5000u64.to_le_bytes()); // the RIP it was compiled at
        let mut reply = super::Reply::from_payload(&stored[8..]).unwrap();
        let word = |o: &[u8], at: usize| u32::from_le_bytes(o[at..at + 4].try_into().unwrap()) as usize;
        let at = word(&reply.object, 40) + word(&reply.object, 48);
        reply.object[at..at + 4].copy_from_slice(&0xd61f_0380u32.to_le_bytes()); // br x28
        let forged = open("forged").unwrap();
        forged.put(body.clone(), false, [&stored[..8], &reply.payload()[..]].concat());
        drop(forged);
        let mut c = new(open("forged"));
        let e = block(&mut c).unwrap_err();
        assert!(e.contains("refused") && c.compiles == 0, "{e}");
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// With `FEXC` set (perf plan §2): finishing reaps the server by `wait4` with its CPU time; a compiler that never
    /// started one has none.
    #[test]
    fn finishing_reaps_the_server_with_its_cpu_time() {
        let Some(fexc) = std::env::var_os("FEXC") else { return };
        let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        assert_eq!(c.finish(), None);
        assert!(matches!(c.compile(&[0x48, 0x01, 0xd8, 0xc3], 0x1000, false, &mut ()).unwrap(), Code::Block(_)));
        let (user, system, rss) = c.finish().unwrap();
        assert!(user + system > 0 && rss > 0, "{user} {system} {rss}");
        assert!(c.server.as_ref().unwrap().reaped);
    }

    /// Perf chunk 21: a stand-in for `fexc --serve --channel FD` (a Python script; no wakes, so the host sees each reply
    /// at its next slice) that answers request 1 with `reply` (Python statements on `m`, the mapping, and `n`, the
    /// request number) and request 2 and later with a well-formed empty reply. It exits once orphaned, so a test process that
    /// dies (a negative control's crash) leaves no stand-in behind.
    fn fake_server(name: &str, reply: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!("natemu-fake-fexc-{name}-{}.py", std::process::id()));
        let script = format!(
            "#!/usr/bin/python3\nimport mmap, os, signal, struct, sys, time\nassert sys.argv[1:3] == ['--serve', '--channel'] and len(sys.argv) == 4, sys.argv\n\
             m = mmap.mmap(int(sys.argv[3]), {bytes})\nREP = {rep}\nassert struct.unpack_from('<I', m, 8)[0] == {magic}\n\
             struct.pack_into('<I', m, 72, {magic})\nfor n in range(1, 1 << 30):\n    while struct.unpack_from('<I', m, 0)[0] >> 1 != n: time.sleep(0.0002); os.getppid() == 1 and sys.exit(1)\n\
             \x20   if struct.unpack_from('<I', m, 4)[0] == 0: sys.exit(0)\n    if n == 1:\n{reply}\n    else:\n\
             \x20       struct.pack_into('<IIIIII', m, REP, 1, 0, 0, 0, 0, 0); struct.pack_into('<II', m, 68, 20, {magic})\n\
             \x20   struct.pack_into('<I', m, 64, n << 1)\n",
            bytes = Channel::BYTES,
            rep = Channel::REPLY,
            magic = Channel::MAGIC,
            reply = reply.lines().map(|l| format!("        {l}\n")).collect::<String>()
        );
        std::fs::write(&path, script).unwrap();
        std::fs::set_permissions(&path, std::os::unix::fs::PermissionsExt::from_mode(0o755)).unwrap();
        path
    }

    /// Perf chunk 21: the channel's reply is untrusted input. A length past the reply area or under the header, a header
    /// whose JSON and object lengths do not add up to it, or a wrong layout magic is refused (an error, nothing read beyond
    /// the area); the server's stray output on stdout is refused too; a well-formed reply after a refused one is read as usual.
    #[test]
    fn channel_refuses_malformed_replies() {
        let cases = [
            ("over", format!("struct.pack_into('<I', m, 68, {})", Channel::REPLY_CAP + 1), "length"),
            ("huge", "struct.pack_into('<I', m, 68, 0xffffffff)".to_string(), "length"),
            ("short", "struct.pack_into('<I', m, 68, 19)".to_string(), "length"),
            ("sum", "struct.pack_into('<IIIIII', m, REP, 0, 8, 8, 0, 0, 0); struct.pack_into('<I', m, 68, 28)".to_string(), "malformed fexc reply"),
            ("wrap", "struct.pack_into('<IIIIII', m, REP, 0, 0xfffffff0, 0x20, 0, 0, 0); struct.pack_into('<I', m, 68, 36)".to_string(), "malformed fexc reply"),
            ("stdout", "os.write(1, b'x'); time.sleep(0.05); struct.pack_into('<I', m, 68, 20)".to_string(), "wrote to its stdout"),
            ("magic", "struct.pack_into('<IIIIII', m, REP, 1, 0, 0, 0, 0, 0); struct.pack_into('<II', m, 68, 20, 0)".to_string(), "channel magic"),
        ];
        for (name, reply, want) in cases {
            let path = fake_server(name, &reply);
            let mut server = Server::start(&path).unwrap();
            let e = server.request(b"\0\0\0\0", Instant::now() + Duration::from_secs(10)).err().unwrap();
            assert!(e.to_string().contains(want), "{name}: {e}");
            if name != "stdout" {
                let r = server.request(b"\0\0\0\0", Instant::now() + Duration::from_secs(10)).unwrap();
                assert_eq!((r.code, r.json.len(), r.object.len()), (1, 0, 0), "{name}");
                assert!(server.finish().is_some(), "{name}: the empty request ends it");
            }
            std::fs::remove_file(path).unwrap();
        }
    }

    /// Perf chunk 21: the host posts nothing empty (the empty request ends the server) and nothing over the request area.
    #[test]
    fn channel_caps_requests() {
        let mut c = Channel::new().unwrap();
        assert_eq!(c.word(8).load(Relaxed), Channel::MAGIC);
        for len in [0, Channel::REQUEST_CAP + 1] {
            assert!(c.post(&vec![7; len]).err().unwrap().to_string().contains("does not fit"), "{len}");
        }
        c.post(&vec![7; Channel::REQUEST_CAP]).unwrap();
        assert_eq!((c.word(0).load(Relaxed), c.word(4).load(Relaxed) as usize), (2, Channel::REQUEST_CAP));
    }

    /// Perf chunk 21: both wakes, with the real server. A server asleep (idle past its 20 us spin; its own sleeps end every
    /// 100 ms) answers a request within 10 ms, so the host woke it; a host asleep (fork mode's compiles take about 0.5 ms,
    /// past the host's 30 us spin; its sleeps end every 5 ms) has each reply within 2.5 ms (median), so the server woke it.
    /// A request of the whole request area reaches the server (an error reply: no arguments).
    #[test]
    fn channel_wakes_both_sides() {
        let Some(fexc) = std::env::var_os("FEXC") else { return };
        let c = super::Compiler::with_features(Path::new(&fexc), u64::MAX, None);
        let body = c.body(&[0x48, 0x01, 0xd8, 0xc3], 0x1000, None);
        let deadline = || Instant::now() + Duration::from_secs(10);
        let mut server = Server::start(Path::new(&fexc)).unwrap();
        assert_eq!(server.request(&body, deadline()).unwrap().code, 0);
        for _ in 0..5 {
            std::thread::sleep(Duration::from_millis(30));
            let t = Instant::now();
            assert_eq!(server.request(&body, deadline()).unwrap().code, 0);
            assert!(t.elapsed() < Duration::from_millis(10), "a sleeping server answered in {:?}", t.elapsed());
        }
        let mut whole = vec![0; Channel::REQUEST_CAP];
        whole[..4].copy_from_slice(&0u32.to_le_bytes());
        assert_eq!(server.request(&whole, deadline()).unwrap().code, 2);
        let mut fork = Server::spawn(Path::new(&fexc), &["--serve-mode", "fork"]).unwrap();
        assert_eq!(fork.request(&body, deadline()).unwrap().code, 0);
        let mut took: Vec<Duration> = (0..11)
            .map(|_| {
                let t = Instant::now();
                assert_eq!(fork.request(&body, deadline()).unwrap().code, 0);
                t.elapsed()
            })
            .collect();
        took.sort();
        assert!(took[5] < Duration::from_micros(2500), "a sleeping host had its replies in {took:?}");
    }

    /// Perf chunk 21: a server that dies holding a request fails it promptly (stdout's EOF, seen within a slice), well
    /// before the deadline; one that never answers fails it at the deadline.
    #[test]
    fn channel_detects_a_dead_or_hung_server() {
        let path = fake_server("killed", "os.kill(os.getpid(), signal.SIGKILL)");
        let mut server = Server::start(&path).unwrap();
        let t = Instant::now();
        let e = server.request(b"\0\0\0\0", t + Duration::from_secs(30)).err().unwrap();
        assert_eq!(e.kind(), std::io::ErrorKind::UnexpectedEof, "{e}");
        assert!(t.elapsed() < Duration::from_secs(2), "{:?}", t.elapsed());
        assert!(server.failed(e).contains("signal: 9"));
        std::fs::remove_file(path).unwrap();
        let path = fake_server("hung", "while os.getppid() != 1: time.sleep(0.05)");
        let mut server = Server::start(&path).unwrap();
        let t = Instant::now();
        let e = server.request(b"\0\0\0\0", t + Duration::from_millis(300)).err().unwrap();
        assert_eq!(e.kind(), std::io::ErrorKind::TimedOut, "{e}");
        assert!((Duration::from_millis(300)..Duration::from_secs(2)).contains(&t.elapsed()), "{:?}", t.elapsed());
        std::fs::remove_file(path).unwrap();
    }

    /// Perf chunk 21's identity gate (`NATEMU_REPLAY` = a recorded request stream, u32 length + body as the host wrote
    /// them; `NATEMU_REPLAY_OLD` = a compiler run over the pipe, `FEXC` = one run over the channel as the host runs it;
    /// `NATEMU_REPLAY_ARGS`: server options for both, e.g. `--report`): every reply identical (code, JSON, object, extent,
    /// args_read: the whole payload).
    #[test]
    #[ignore]
    fn channel_replay_matches_the_pipe() {
        use std::io::Write;
        let (Some(stream), Some(old), Some(new)) = (std::env::var_os("NATEMU_REPLAY"), std::env::var_os("NATEMU_REPLAY_OLD"), std::env::var_os("FEXC")) else {
            panic!("NATEMU_REPLAY, NATEMU_REPLAY_OLD and FEXC")
        };
        let extra = std::env::var("NATEMU_REPLAY_ARGS").unwrap_or_default();
        let extra: Vec<&str> = extra.split_whitespace().collect();
        let mut pipe = Command::new(old).arg("--serve").args(&extra).stdin(Stdio::piped()).stdout(Stdio::piped()).spawn().unwrap();
        let (mut to, mut from) = (pipe.stdin.take().unwrap(), pipe.stdout.take().unwrap());
        let mut server = Server::spawn(Path::new(&new), &extra).unwrap();
        let mut input = std::io::BufReader::with_capacity(1 << 20, std::fs::File::open(stream).unwrap());
        let (mut n, mut differ, mut word) = (0u64, 0u64, [0u8; 4]);
        while input.read_exact(&mut word).is_ok() {
            let mut body = vec![0; u32::from_le_bytes(word) as usize];
            input.read_exact(&mut body).unwrap();
            to.write_all(&word).unwrap();
            to.write_all(&body).unwrap();
            from.read_exact(&mut word).unwrap();
            let mut want = vec![0; u32::from_le_bytes(word) as usize];
            from.read_exact(&mut want).unwrap();
            let r = server.request(&body, Instant::now() + COMPILE_DEADLINE).unwrap();
            let got: Vec<u8> = [r.code as u32, r.json.len() as u32, r.object.len() as u32, r.extent, r.read].iter().flat_map(|w| w.to_le_bytes()).chain(r.json).chain(r.object).collect();
            n += 1;
            if got != want {
                differ += 1;
                if differ <= 10 {
                    eprintln!("request {n}: pipe {:02x?} channel {:02x?}", &want[..20.min(want.len())], &got[..20]);
                }
            }
        }
        drop(to);
        pipe.wait().unwrap();
        eprintln!("replay: {n} requests, {differ} differences");
        assert!(n > 0 && differ == 0);
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
        // A limit exactly at the next page's first byte: nothing of that page, walked or not, and it is not asked for.
        let f = fetched(area, &s, Some(0x2000)).unwrap().unwrap();
        assert_eq!((f.bytes, f.more, f.cut), (vec![0xaa; 16], false, true));
        s.fetch = [0x5000, u64::MAX];
        let f = fetched(area, &s, Some(0x2000)).unwrap().unwrap();
        assert_eq!((f.bytes.len(), f.more, f.cut), (16, false, true));
        // Past the page: not a cut.
        let f = fetched(area, &s, Some(0x2001)).unwrap().unwrap();
        assert_eq!((f.bytes.len(), f.more, f.cut), (16, true, false));
        (s.fetch, s.fetch_fault) = ([u64::MAX; 2], exec::fetch::encode(Fault::Noncanonical));
        assert!(matches!(fetched(area, &s, None), Ok(Err(Stop::FetchFault(Fault::Noncanonical)))));
        // The word copy (perf chunk 7) equals the byte copy at every alignment and length around a word, and refuses past RAM.
        let pattern: Vec<u8> = (0..64).map(|i| i as u8 ^ 0x5a).collect();
        assert!(ram.copy_in(0x7000, &pattern));
        for (at, n) in (0..9).flat_map(|a| (0..26).map(move |n| (a, n))) {
            let (mut words, mut bytes) = (vec![1], vec![0; n]);
            assert!(copy_out(&ram, 0x7000 + at, n as u64, &mut words) && ram.copy_out(0x7000 + at, &mut bytes));
            assert_eq!(words[1..], bytes[..]);
        }
        assert!(!copy_out(&ram, (2 << 20) - 4, 8, &mut vec![]));
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
