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
use crate::report::Members;
use natemu_abi::execution::{self as exec, Cpu, State, cache};
use natemu_abi::guest_memory::GuestRam;
use natemu_abi::paging::Fault;
use std::collections::HashMap;
use std::io::Read;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::atomic::{
    AtomicBool, AtomicU64,
    Ordering::{Relaxed, SeqCst},
};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

mod diag;
pub use diag::inspect_on_signal;
pub mod io;
#[cfg(feature = "io-test-hooks")]
pub mod io_test;

/// The execution mode's backing for `n` vCPUs. Block 0 holds vCPU 0's window, its lookup area, the global area, its
/// metadata arena and its code area (the `exec`/`cache` offsets); block v (`v * stride` further) holds vCPU v's own
/// window, lookup area, metadata arena and code area at the same offsets and leaves the global area's range unused
/// (never mapped, so never backed): every vCPU maps block 0's. Guest RAM (M1: `ram_bytes`, chosen at prepare) follows
/// the last block, at `n * stride`; only the pages the guest or the host touch are ever backed. With the template pool
/// ([`prepare_with_pool`]) its span (`cache::POOL_BYTES`) lies between the last block and guest RAM (RAM stays the backing's
/// last bytes), every vCPU maps it read-only at `cache::POOL_VA`. With a firmware image ([`prepare_with_firmware`], M2 Part
/// 2) the RAM span reaches 4 GiB at least and the image lies at its guest-physical place in it.
pub struct Backing {
    /// Shared (not borrowed) by whatever outlives a move of the Backing: the chipset's LAPIC view ([`crate::irq`]) and the
    /// VM ([`Machine`]). Not public: replacing it would leave `ram` pointing into a mapping that is gone.
    pub(crate) memory: Arc<Memory>,
    pub(crate) n: usize,
    stride: usize,
    /// Guest-physical RAM through the host's alias: valid while `memory` is mapped, so never handed out by value in safe
    /// code (`GuestRam` is `Copy`: a copy would outlive the backing; R2 review).
    pub(crate) ram: GuestRam,
    /// M2 Part 2: guest RAM and the firmware image (`exec::guest_memory`; RAM alone without one), as `ram`: the vCPUs' view
    /// ([`Area::ram`]) through which the host reads code to translate and reports read memory. Never written: every host
    /// writer (loaders, device DMA, copy windows) uses `ram`, so nothing of the host changes the image.
    pub(crate) readable: GuestRam,
    /// One CNTVCT sample (the host's `mach_absolute_time`: every vCPU's virtual counter has offset 0) taken when the VM
    /// was prepared: each vCPU's TSC starts from it, so all start near 0 and agree.
    epoch: u64,
    /// Serializes finite guest-RAM copy windows; epochs are never reused.
    io_epoch: Mutex<u64>,
    /// Granted windows, for the performance log ([`io::WindowCounts`]).
    io_counts: io::WindowCounters,
    /// The template pool's writer, shared by the run's compilers ([`Compiler::pool`]); `None`: no pool in this backing.
    pub(crate) pool: Option<Arc<crate::publisher::Publisher>>,
}
fn mapped_regions(memory: &Memory) -> &cache::MappedRegions {
    let at = cache::GLOBAL_OFFSET + cache::MAPPED;
    assert!(at % 8 == 0 && at + size_of::<cache::MappedRegions>() <= memory.1);
    // SAFETY: inside the backing, aligned, plain atomics shared with the residents.
    unsafe { &*memory.0.add(at).cast::<cache::MappedRegions>() }
}
/// The region table of a backing, owned ([`Backing::mapped_table`]).
#[derive(Clone)]
pub struct MappedTable(Arc<Memory>);
impl std::ops::Deref for MappedTable {
    type Target = cache::MappedRegions;
    fn deref(&self) -> &cache::MappedRegions {
        mapped_regions(&self.0)
    }
}
/// Mapped device memory: every region of `plan` gets its table entry, disabled (its alias and length, base 0), before any
/// vCPU exists, so the table's `seq` is nonzero exactly when the VM has regions: a resident that reads `seq` 0 (no
/// region, the default launch, which publishes nothing) skips the holding flag safely, as no publish can ever follow
/// (`mmio::mapped` in the runtime). The publisher (the bus, the driver's test device) enables entries later.
fn publish_disabled(table: &cache::MappedRegions, plan: &crate::vm::MappingPlan) {
    for (i, &(alias, len)) in plan.aliases.iter().enumerate() {
        table.publish(i, cache::MappedRegion { base: 0, len, alias, enabled: 0 });
    }
}
impl Backing {
    /// The template pool's publication counters as the performance log's `pool {...}` line (`None`: no pool).
    pub fn pool_report(&self) -> Option<String> {
        self.pool.as_ref().map(|p| p.report())
    }
    /// The backing's mapping (shared: whoever holds a clone keeps it mapped).
    pub fn memory(&self) -> &Arc<Memory> {
        &self.memory
    }
    /// The VM of this backing with the device memory `mapped` (in order; empty: none, the default launch): the machine
    /// maps each region above the backing ([`Machine::create`]), and every window's alias of it is written here before
    /// any vCPU exists, so no machine maps a region its residents cannot reach. The only way to create a machine.
    pub fn machine(&self, mapped: &[Arc<crate::pci::DeviceMemory>]) -> Result<Machine> {
        let machine = Machine::create(self.memory.clone(), mapped)?;
        self.map_mapped(machine.mapping());
        publish_disabled(self.mapped_regions(), machine.mapping());
        Ok(machine)
    }
    /// Writes every window's alias of the VM's mapped device memory ([`crate::memory::mapped_spans`]): after the RAM and
    /// firmware aliases (it replaces their entries, though none lies in its slots: `exec::MAPPED_VA`), before any vCPU
    /// runs. Nothing for a plan without regions.
    fn map_mapped(&self, plan: &crate::vm::MappingPlan) {
        let spans = crate::memory::mapped_spans(plan);
        if spans.is_empty() {
            return;
        }
        for v in 0..self.n {
            map_private(&self.memory, v * self.stride, &spans);
        }
    }
    /// The region table in the global area, which the host alone writes (`MappedRegions::publish`).
    pub fn mapped_regions(&self) -> &cache::MappedRegions {
        mapped_regions(&self.memory)
    }
    /// The region table as an owned handle (it keeps the backing mapped): for a device that publishes from its handler
    /// (the PCI bus, a test device). A publish made in a vCPU's port or device exit is what the resident's remap trigger
    /// sees (`seq` moved across the exit); one made by any other thread is not.
    pub fn mapped_table(&self) -> MappedTable {
        MappedTable(self.memory.clone())
    }
    /// Guest RAM through the host's alias.
    ///
    /// # Safety
    /// The value (and every copy) must not be used after this backing is dropped: it addresses the backing's mapping.
    /// During execution, raw host accesses must be excluded from `copy_guest` windows. Production source reads
    /// occur only while their vCPU is serving a request, before it can acknowledge a window; they may overlap a
    /// windowless device copy's writes, which the code-mark protocol covers (the vCPU marks the page before the
    /// read, and the copy's recheck posts it, so a block compiled from torn bytes is dropped before the completion
    /// is published). Boot writes precede execution. Other running-VM access must use `copy_guest` or
    /// `copy_guest_windowless` instead.
    pub unsafe fn ram(&self) -> GuestRam {
        self.ram
    }
    /// Platform-devices plan (SMBIOS): `bytes` into the legacy hole's backing at guest-physical `gpa`, and the whole pages
    /// they touch made every vCPU's legacy ROM range (`cache::Control::legacy_rom`): read as memory, a store to them dropped
    /// (x86's ROM), never fetched from. [`GuestRam`] leaves the hole out (so no loader or device write reaches it); the
    /// backing maps the whole low span, holes included, at `ram_offset + gpa`, as the resident's alias does. Before any vCPU
    /// runs, once; not with a firmware image (whose boot gets its tables from the firmware).
    ///
    /// # Panics
    /// When the pages are not inside `exec::LEGACY_HOLE`, the VM's mapping and the resident's alias, when a range is
    /// already set, or with a firmware image.
    pub fn write_legacy_rom(&self, gpa: u64, bytes: &[u8]) {
        let range = (gpa & !4095, (gpa + bytes.len() as u64).next_multiple_of(4096));
        assert!(!bytes.is_empty() && exec::legacy_rom_valid(range), "legacy ROM {range:x?}: whole pages inside the legacy hole");
        assert!(self.readable == self.ram, "a legacy ROM range with a firmware image");
        // Guest-physical 0's backing offset: the alias's first region starts there (RAM's from 0).
        let ram_offset = self.ram.range(0, 1).expect("guest RAM from 0") as usize - self.memory.0 as usize;
        assert!(ram_offset + range.1 as usize <= self.memory.1, "legacy ROM {range:x?} past the VM's mapping");
        // The resident's alias of low RAM spans [0, min(R, 3 GiB)), the hole included (`prepare`'s low data span).
        let alias_end = self.memory.1 as u64 - ram_offset as u64;
        assert!(range.1 <= alias_end.min(exec::LOW_RAM_END), "legacy ROM {range:x?} past the resident's alias");
        self.memory.copy(ram_offset + gpa as usize, bytes);
        for v in 0..self.n {
            let area = self.vcpu(v);
            let mut control: cache::Control = area.read_record(cache::LOOKUP_OFFSET + cache::CONTROL);
            assert_eq!(control.legacy_rom, (0, 0), "a legacy ROM range set twice");
            control.legacy_rom = range;
            area.memory.write_record(area.base + cache::LOOKUP_OFFSET + cache::CONTROL, control);
        }
    }
    /// `N` bytes of the legacy ROM range at `gpa` ([`Backing::write_legacy_rom`]), `None` unless all lie inside it (the
    /// test driver's dump of a run).
    pub fn read_legacy_rom<const N: usize>(&self, gpa: u64) -> Option<[u8; N]> {
        let c: cache::Control = self.vcpu(0).read_record(cache::LOOKUP_OFFSET + cache::CONTROL);
        let (start, end) = c.legacy_rom;
        let ram_offset = self.ram.range(0, 1)? as usize - self.memory.0 as usize;
        (start <= gpa && gpa + N as u64 <= end).then(|| self.memory.read::<N>(ram_offset + gpa as usize))
    }
    /// vCPU `v`'s view.
    pub fn vcpu(&self, v: usize) -> Area<'_> {
        assert!(v < self.n);
        Area {
            memory: &self.memory,
            vcpu: v,
            vcpus: self.n,
            epoch: self.epoch,
            base: v * self.stride,
            ram: self.readable,
        }
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
    /// The VM's guest RAM and its firmware image ([`Backing::readable`]): read only (code to translate, reports).
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
        Area {
            memory,
            vcpu: 0,
            vcpus: 1,
            epoch: 0,
            base: 0,
            ram: unsafe { GuestRam::new(0, memory.1 as u64, memory.0 as u64) },
        }
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

/// The template pool's layout: production's, or (the K1 `pool-wrap` test hook) `NATEMU_POOL_RING_KB` of ring and
/// `NATEMU_POOL_INDEX_SLOTS` index slots (both set, a ring a multiple of 64 bytes and at least 4 KiB, slots a power of two of
/// at least 8; the control granule stays 2 MiB).
fn pool_layout_from_env() -> Result<natemu_abi::pool::Layout> {
    let var = |name: &str| match std::env::var(name) {
        Ok(v) => v
            .parse::<usize>()
            .map(Some)
            .map_err(|_| format!("{name}={v}: a number")),
        Err(std::env::VarError::NotPresent) => Ok(None),
        Err(e) => Err(format!("{name}: {e}")),
    };
    let (ring, slots) = (var("NATEMU_POOL_RING_KB")?, var("NATEMU_POOL_INDEX_SLOTS")?);
    let Some((ring, slots)) = ring.zip(slots) else {
        return Ok(natemu_abi::pool::PRODUCTION);
    };
    let layout = natemu_abi::pool::Layout {
        index_slots: slots,
        ring_bytes: ring.saturating_mul(1024),
        control_bytes: natemu_abi::pool::PRODUCTION.control_bytes,
    };
    if !layout.valid() || layout.ring_bytes < 4096 || layout.mapped_bytes() > cache::POOL_BYTES {
        return Err("NATEMU_POOL_RING_KB / NATEMU_POOL_INDEX_SLOTS: a ring of 4 KiB or more and a power of two of 8 or more slots, the whole within the pool's address range".into());
    }
    eprintln!("natemu: the template pool's ring is {ring} KiB, its index {slots} slots (test hook)");
    Ok(layout)
}

/// Backing for the execution mode with `n` vCPUs (1..=`cache::MAX_VCPUS`) and `ram_bytes` of guest RAM (a 2 MiB
/// multiple, at most `exec::MAX_GUEST_RAM_BYTES`): the resident loaded into each vCPU's window, each vCPU's private
/// address space and shared record in place; guest RAM is zero. Each vCPU's code cache holds `code_bytes` of generated
/// code (at most `cache::CODE_BYTES_MAX`).
pub fn prepare(resident: &str, code_bytes: usize, n: usize, ram_bytes: usize) -> Result<Backing> {
    prepare_with_pool(resident, code_bytes, n, ram_bytes, false)
}
/// [`prepare`], with the shared template pool ([`natemu_abi::pool`]) when `pool` is set: its span is backed before guest RAM
/// and mapped read-only into every vCPU's window, and the host publishes admitted templates into it (the resident does not
/// read it yet).
pub fn prepare_with_pool(
    resident: &str,
    code_bytes: usize,
    n: usize,
    ram_bytes: usize,
    pool: bool,
) -> Result<Backing> {
    prepare_with_firmware(resident, code_bytes, n, ram_bytes, pool, &[])
}
/// [`prepare_with_pool`] with a firmware image (M2 Part 2, docs/restructure/firmware-mode.md; empty: none, as
/// [`prepare_with_pool`]): `image` (whole pages, at most `exec::MAX_FIRMWARE_BYTES`) at guest-physical
/// `[4 GiB - its size, 4 GiB)`, read and fetched as RAM, its stores dropped. The backing's RAM span then reaches 4 GiB (address
/// space only: the untouched part is never backed), so the image's backing offset is its guest-physical address's as RAM's
/// is; the resident's alias maps its 2 MiB blocks read-only (`Control::firmware_bytes` tells it the image's size).
pub fn prepare_with_firmware(
    resident: &str,
    code_bytes: usize,
    n: usize,
    ram_bytes: usize,
    pool: bool,
    image: &[u8],
) -> Result<Backing> {
    unsafe extern "C" {
        fn getentropy(buf: *mut u8, len: usize) -> i32;
    }
    let va = random_guest_state_alias(|bytes| {
        // SAFETY: the buffer is writable and 8 bytes, below getentropy's 256-byte limit.
        unsafe { getentropy(bytes.as_mut_ptr(), bytes.len()) }
    })?;
    prepare_at_alias(resident, code_bytes, n, ram_bytes, va, pool, image)
}

fn random_guest_state_alias(fill: impl FnOnce(&mut [u8; 8]) -> i32) -> Result<u64> {
    let mut bytes = [0; 8];
    if fill(&mut bytes) != 0 {
        return Err(format!(
            "guest-state alias entropy: {}",
            std::io::Error::last_os_error()
        ));
    }
    // Uniform among all 2^27 pages; no fallback to a fixed, time-derived or process-derived address.
    let mask = (exec::GUEST_STATE_SLOT.end - exec::GUEST_STATE_SLOT.start - 1) & !4095;
    Ok(exec::GUEST_STATE_SLOT.start + (u64::from_ne_bytes(bytes) & mask))
}

/// Deterministic alias selection for test drivers only, before any tables are populated. Production always uses entropy.
#[cfg(feature = "testing")]
pub fn prepare_with_guest_state_alias(
    resident: &str,
    code_bytes: usize,
    n: usize,
    ram_bytes: usize,
    va: u64,
    pool: bool,
    image: &[u8],
) -> Result<Backing> {
    prepare_at_alias(resident, code_bytes, n, ram_bytes, va, pool, image)
}

fn prepare_at_alias(
    resident: &str,
    code_bytes: usize,
    n: usize,
    ram_bytes: usize,
    va: u64,
    pool: bool,
    image: &[u8],
) -> Result<Backing> {
    if !exec::guest_state_alias_valid(va) {
        return Err(format!(
            "guest-state alias {va:#x}: an aligned page inside the reserved slot is required"
        ));
    }
    if !(1..=cache::MAX_VCPUS).contains(&n) {
        return Err(format!(
            "{n} vCPUs: the execution mode runs 1 to {}",
            cache::MAX_VCPUS
        ));
    }
    if ram_bytes == 0 || ram_bytes % cache::BLOCK != 0 || ram_bytes > exec::MAX_GUEST_RAM_BYTES {
        return Err(format!(
            "guest RAM of {ram_bytes:#x} bytes: a nonzero 2 MiB multiple up to {:#x}",
            exec::MAX_GUEST_RAM_BYTES
        ));
    }
    let firmware = image.len() as u64;
    if firmware != 0 && !exec::firmware_size(firmware) {
        return Err(format!(
            "firmware image of {firmware:#x} bytes: whole 4 KiB pages, at most {:#x}",
            exec::MAX_FIRMWARE_BYTES
        ));
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
    // The pool's span follows the last block and precedes guest RAM, so RAM stays the backing's last bytes (N7).
    let pool_offset = n * stride;
    let pool_layout = if pool {
        pool_layout_from_env()?
    } else {
        natemu_abi::pool::PRODUCTION
    };
    let pool_bytes = if pool { pool_layout.mapped_bytes() } else { 0 };
    let ram_offset = pool_offset + pool_bytes;
    let ram_span = exec::ram_end(ram_bytes as u64) as usize;
    // (A firmware image lies below 4 GiB, at its guest-physical offset in the span.)
    let ram_span = if firmware != 0 {
        ram_span.max(exec::FIRMWARE_END as usize)
    } else {
        ram_span
    };
    let memory = Memory::with_size(ram_offset + ram_span)?;
    let image_end = load_resident(&memory, resident)?;
    for v in 0..n {
        let base = v * stride;
        if v > 0 {
            memory.replicate_resident_text(base, image_end);
        }
        let mut spans = resident_text(base);
        let own = |offset, bytes| Span::data(offset, bytes, base + offset);
        spans.extend([
            own(SHARED_OFFSET, 4096),
            own(exec::STATE_OFFSET, 4096),
            own(exec::FRAME_OFFSET, exec::frame::BYTES),
            own(
                (STACK_TOP - PRIVATE) as usize - exec::STACK_BYTES,
                exec::STACK_BYTES,
            ),
            own((EXCEPTION_STACK_TOP - PRIVATE) as usize - 0x1_4000, 0x1_4000),
            own(exec::STAGING_OFFSET, exec::STAGING_BYTES),
            Span::data(
                (exec::GUEST_RAM_VA - PRIVATE) as usize,
                ram_bytes.min(exec::LOW_RAM_END as usize),
                ram_offset,
            ),
            // Generated code runs at EL0 from the code area (EL0 execute-only); the resident writes it through its alias.
            Span::code(
                (cache::CODE_VA - PRIVATE) as usize,
                code_mapped,
                base + cache::CODE_OFFSET,
            ),
            Span::data(
                (cache::CODE_WRITE_VA - PRIVATE) as usize,
                code_mapped,
                base + cache::CODE_OFFSET,
            ),
            Span::data(
                (cache::META_VA - PRIVATE) as usize,
                cache::META_BYTES,
                base + cache::META_OFFSET,
            ),
            Span::data(
                (cache::LOOKUP_VA - PRIVATE) as usize,
                cache::LOOKUP_BYTES,
                base + cache::LOOKUP_OFFSET,
            ),
            Span::data(
                (cache::GLOBAL_VA - PRIVATE) as usize,
                cache::GLOBAL_BYTES,
                cache::GLOBAL_OFFSET,
            ),
        ]);
        if pool {
            spans.push(Span::read_only(
                (cache::POOL_VA - PRIVATE) as usize,
                pool_bytes,
                pool_offset,
            ));
        }
        if firmware != 0 {
            // The 2 MiB blocks holding the image, read-only: the resident drops the guest's stores to it, and a store that
            // reached the alias would halt it rather than change the image. Their bytes below the image are no guest
            // memory (unassigned, as on a PC).
            let base = (exec::FIRMWARE_END - firmware) as usize & !(cache::BLOCK - 1);
            spans.push(Span::read_only(
                (exec::GUEST_RAM_VA - PRIVATE) as usize + base,
                exec::FIRMWARE_END as usize - base,
                ram_offset + base,
            ));
        }
        if ram_bytes as u64 > exec::LOW_RAM_END {
            spans.push(Span::data(
                (exec::GUEST_RAM_VA - PRIVATE + exec::HIGH_RAM_START) as usize,
                ram_bytes - exec::LOW_RAM_END as usize,
                ram_offset + exec::HIGH_RAM_START as usize,
            ));
        }
        private_space(&memory, base, &spans, true);
        // The guest-state page's EL0 alias (x28 of generated code), backed by the frame page (G1).
        guest_state_alias(&memory, base, base + exec::FRAME_OFFSET, va, Some(base + cache::LOOKUP_OFFSET + cache::DL1));
        // SAFETY: Control is plain integers; the resident owns everything but the capacity.
        let mut control: cache::Control = unsafe { std::mem::zeroed() };
        control.code_bytes = (cache::HEAD_BYTES + code_bytes) as u64;
        control.ram_bytes = ram_bytes as u64;
        control.firmware_bytes = firmware;
        control.guest_state_va = va;
        memory.write_record(base + cache::LOOKUP_OFFSET + cache::CONTROL, control);
        // SAFETY: Shared is plain integers; all-zero is its empty value.
        let mut shared: Shared = unsafe { std::mem::zeroed() };
        shared.mode = exec::MODE;
        memory.write_record(base + SHARED_OFFSET, shared);
        memory.flush_instruction_cache(base, resident::BOOT_END);
        if image_end > resident::TEXT_OFFSET {
            memory.flush_instruction_cache(base + resident::TEXT_OFFSET, image_end - resident::TEXT_OFFSET);
        }
    }
    // Before any vCPU runs: the image's bytes, through the backing (the guest's view of them is read-only).
    if firmware != 0 {
        memory.copy(ram_offset + (exec::FIRMWARE_END - firmware) as usize, image);
    }
    // SAFETY: the alias is the backing's last `ram_span` bytes, alive as long as the Backing holding this value.
    let ram = unsafe { exec::guest_ram(ram_bytes as u64, memory.0 as u64 + ram_offset as u64) };
    // SAFETY: as `ram`; the image lies in the same span.
    let readable = unsafe { exec::guest_memory(ram_bytes as u64, firmware, memory.0 as u64 + ram_offset as u64) };
    let memory = Arc::new(memory);
    // Zero memory, nothing reading yet: the publisher makes the pool empty and on.
    let pool = pool.then(|| {
        Arc::new(crate::publisher::Publisher::with_layout(
            memory.clone(),
            pool_offset,
            pool_layout,
        ))
    });
    Ok(Backing {
        memory,
        n,
        stride,
        ram,
        readable,
        epoch: unsafe { mach_absolute_time() },
        io_epoch: Mutex::new(0),
        io_counts: io::WindowCounters::default(),
        pool,
    })
}
/// Whether this host reports Arm feature `name` (`sysctl hw.optional.arm.<name>` = 1; absent or unreadable: no). Only the
/// probe's sysctl cross-check test uses it: natemu answers from the vCPU's registers ([`crate::probe`]).
#[cfg(test)]
pub fn host_feature(name: &str) -> bool {
    unsafe extern "C" {
        fn sysctlbyname(
            name: *const std::ffi::c_char,
            old: *mut std::ffi::c_void,
            len: *mut usize,
            new: *mut std::ffi::c_void,
            newlen: usize,
        ) -> i32;
    }
    let key = std::ffi::CString::new(format!("hw.optional.arm.{name}"))
        .expect("no NUL in a feature name");
    let (mut value, mut len) = (0i32, size_of::<i32>());
    // SAFETY: `value` and `len` describe a writable 4-byte buffer; no new value is set.
    let rc = unsafe {
        sysctlbyname(
            key.as_ptr(),
            (&raw mut value).cast(),
            &mut len,
            std::ptr::null_mut(),
            0,
        )
    };
    rc == 0 && len == size_of::<i32>() && value == 1
}
/// The longest halt-poll window ([`State::halt_poll_max_us`]): `NATEMU_HALT_POLL_US` (calibration on other hosts; 0
/// turns polling off; capped at 1 ms), else the default.
fn halt_poll_max_us() -> u64 {
    static MAX: std::sync::OnceLock<u64> = std::sync::OnceLock::new();
    *MAX.get_or_init(|| {
        std::env::var("NATEMU_HALT_POLL_US")
            .ok()
            .and_then(|v| v.parse::<u64>().ok())
            .map_or(exec::HALT_POLL_MAX_US, |us| us.min(1000))
    })
}
/// Sets the vCPU's initial architectural state (before it first runs). `speculation_barriers` turns on the resident's
/// page-walk barriers (PCID plan P1) and, together with the host's FEAT_CSV3, is the sole condition for advertising
/// RDCL_NO: a guest never sees "not susceptible to Meltdown" without the barriers that make it true.
pub fn set_cpu(area: Area, cpu: Cpu, speculation_barriers: bool) -> Result<()> {
    set_cpu_with(area, cpu, speculation_barriers, false)
}
/// [`set_cpu`] for the firmware boot mode (M2 Part 2, docs/restructure/firmware-mode.md): the MSRs at their power-on values
/// (`cpu::power_on_msrs`: the MTRRs disabled, IA32_MTRR_DEF_TYPE 0, no variable range), not the virtual firmware's that
/// [`set_cpu`] gives every other start; the TSC and IA32_ARCH_CAPABILITIES as there.
pub fn set_cpu_at_power_on(area: Area, cpu: Cpu, speculation_barriers: bool) -> Result<()> {
    set_cpu_with(area, cpu, speculation_barriers, true)
}
fn set_cpu_with(area: Area, cpu: Cpu, speculation_barriers: bool, power_on: bool) -> Result<()> {
    let mut state = State::new(area.vcpu as u64, cpu);
    state.vcpus = area.vcpus as u64;
    state.halt_poll_max_us = halt_poll_max_us();
    area.write_record(exec::STATE_OFFSET, state);
    // The TSC (L5, `natemu_abi::clock::tsc`) is the resident's CNTVCT scaled to TSC_HZ plus this offset: from the shared epoch.
    const _: () = assert!(natemu_abi::cpu_profile::TSC_HZ == 1_000_000_000); // ticks_ns scales like the resident
    let mut msrs = if power_on {
        natemu_abi::cpu::power_on_msrs(area.vcpu == 0)
    } else {
        natemu_abi::cpu::reset_msrs(area.vcpu == 0)
    };
    msrs[natemu_abi::cpu::msr(natemu_abi::cpu::MSR_TSC).unwrap()] =
        ticks_ns(area.epoch).wrapping_neg();
    // The virtual firmware's MTRRs cover the guest RAM this VM has (M1's run-time size, set at prepare).
    let mut control: cache::Control = area.read_record(cache::LOOKUP_OFFSET + cache::CONTROL);
    control.speculation_barriers = u64::from(speculation_barriers);
    area.write_record(cache::LOOKUP_OFFSET + cache::CONTROL, control);
    if !power_on {
        natemu_abi::cpu::firmware_mtrrs(&mut msrs, control.ram_bytes.min(exec::LOW_RAM_END));
    }
    // IA32_ARCH_CAPABILITIES: RDCL_NO only with the barriers on AND a host whose own loads do not pass faulting data on
    // (FEAT_CSV3). Off by default, so a default-command-line guest keeps PTI (owner decision, November 5).
    let rdcl_no = speculation_barriers && crate::probe::csv3()?;
    msrs[natemu_abi::cpu::msr(natemu_abi::cpu::MSR_ARCH_CAPABILITIES).unwrap()] =
        natemu_abi::cpu::arch_capabilities(rdcl_no);
    area.write_record(exec::MSRS_OFFSET, msrs);
    area.write_record(
        exec::ENTROPY_OFFSET,
        exec::Entropy {
            taken: exec::ENTROPY_WORDS as u64,
            words: [0; exec::ENTROPY_WORDS],
        },
    );
    refill_entropy(area);
    set_lapic(area, natemu_abi::lapic::RESET);
    Ok(())
}
/// Sets the vCPU's local APIC record (X3) and the word peers match IPI destinations against, from its reset
/// IA32_APIC_BASE (before it first runs).
pub fn set_lapic(area: Area, l: natemu_abi::lapic::Lapic) {
    area.write_record(exec::LAPIC_OFFSET, l);
    let base = natemu_abi::cpu::reset_msrs(area.vcpu == 0)
        [natemu_abi::cpu::msr(natemu_abi::cpu::MSR_APIC_BASE).unwrap()];
    let word = natemu_abi::lapic::published(base, &l, area.vcpu as u64);
    event_word(
        area.memory,
        area.vcpu,
        std::mem::offset_of!(cache::Events, apic),
    )
    .store(word, SeqCst);
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
    // (M2 checkpoint C: the resident's third, for the compat root, lies above both: `shadow::asids`.)
    const _: () = assert!(2 * cache::MAX_VCPUS + 1 < natemu_abi::shadow::COMPAT_ASID as usize);
    const _: () = assert!(natemu_abi::shadow::COMPAT_ASID as usize + cache::MAX_VCPUS - 1 <= 255);
    cpu.doorbell(area.vcpu, doorbell)?;
    cpu.enter_resident_with_asid(
        area.base,
        natemu_abi::shadow::TCR_BOTH,
        2 * area.vcpu as u8 + 2,
    )?;
    // The startup probe (host features, TSO readback) runs here, in the setup closure's error path, so a refusal is one
    // setup error and not one per started owner.
    crate::probe::startup(cpu).map(drop)
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
    global_u64(
        memory,
        record + i.map_or(0, |i| std::mem::offset_of!(cache::Record, stale) + 8 * i),
    )
}
/// vCPU `v`'s event record word at `field` (an offset in [`cache::Events`], X2).
fn event_word(memory: &Memory, v: usize, field: usize) -> &AtomicU64 {
    global_u64(
        memory,
        cache::EVENTS + v * size_of::<cache::Events>() + field,
    )
}
/// `post` value of an NMI (else an interrupt vector, 0..=255).
pub const NMI: u64 = 256;
/// `post` value of an INIT (M2 Part 2; test drivers' event sources): sent as a peer's APIC sends it
/// (`natemu_abi::lapic::startup::init_sent`).
pub const INIT: u64 = 257;
/// Posts `event` (a vector, or [`NMI`]) into vCPU `v`'s event record (X2); the caller then rings its doorbell, which makes
/// it stop precisely and take the event when it is eligible. L4a: `level`, a level-triggered interrupt: its TMR bit is
/// set (else cleared) before the IRR bit, as the LAPIC accepts it (SDM Vol.3 11.8.4), so its EOI is broadcast. Returns
/// whether the target accepted it.
pub fn post(memory: &Memory, v: usize, event: u64, level: bool) -> bool {
    // X3: as from a peer's APIC: an NMI needs the target's APIC hardware enabled, an interrupt also software enabled
    // (SDM Vol.3 11.4.7.2); otherwise the message is not accepted.
    let apic = event_word(memory, v, std::mem::offset_of!(cache::Events, apic)).load(SeqCst);
    let needs = if event == NMI || event == INIT {
        natemu_abi::lapic::HW_ENABLED
    } else {
        natemu_abi::lapic::SW_ENABLED
    };
    if apic & needs == 0 {
        return false;
    }
    match event {
        // The pending word's NMI bit (the word holds INIT and SIPI state too: an OR, not a store).
        NMI => {
            event_word(memory, v, std::mem::offset_of!(cache::Events, pending))
                .fetch_or(cache::pending::NMI, SeqCst);
        }
        INIT => {
            let _ = event_word(memory, v, std::mem::offset_of!(cache::Events, pending)).fetch_update(
                SeqCst,
                SeqCst,
                |w| Some(natemu_abi::lapic::startup::init_sent(w)),
            );
        }
        _ => {
            let w = 8 * (event as usize / 64 % 4);
            let tmr = event_word(memory, v, std::mem::offset_of!(cache::Events, tmr) + w);
            if level {
                tmr.fetch_or(1 << (event % 64), SeqCst)
            } else {
                tmr.fetch_and(!(1 << (event % 64)), SeqCst)
            };
            event_word(memory, v, std::mem::offset_of!(cache::Events, vectors) + w)
                .fetch_or(1 << (event % 64), SeqCst);
        }
    }
    // Perf chunk 14: the post's time, for a `perf` resident's wake latency (kept only while 0: the first since it parked).
    if crate::perf::on() {
        let _ = event_word(memory, v, std::mem::offset_of!(cache::Events, rung)).compare_exchange(
            0,
            unsafe { mach_absolute_time() },
            SeqCst,
            SeqCst,
        );
    }
    event_word(memory, v, std::mem::offset_of!(cache::Events, posts)).fetch_add(1, SeqCst);
    true
}
/// L4b: whether `vector` is still in vCPU `v`'s IRR (posted, not yet accepted into service).
pub(crate) fn pending(memory: &Memory, v: usize, vector: u64) -> bool {
    let w = event_word(
        memory,
        v,
        std::mem::offset_of!(cache::Events, vectors) + 8 * (vector as usize / 64 % 4),
    )
    .load(SeqCst);
    w >> (vector % 64) & 1 != 0
}
/// L4a: vCPU `v`'s published APIC word (what senders match destinations against, `natemu_abi::lapic::published`).
pub(crate) fn published(memory: &Memory, v: usize) -> u64 {
    event_word(memory, v, std::mem::offset_of!(cache::Events, apic)).load(SeqCst)
}
/// Test seam: stores vCPU `v`'s published APIC word, and reads a vector's TMR bit.
#[cfg(test)]
pub(crate) fn set_published(memory: &Memory, v: usize, word: u64) {
    event_word(memory, v, std::mem::offset_of!(cache::Events, apic)).store(word, SeqCst);
}
#[cfg(test)]
pub(crate) fn tmr(memory: &Memory, v: usize, vector: u64) -> bool {
    let w = 8 * (vector as usize / 64 % 4);
    event_word(memory, v, std::mem::offset_of!(cache::Events, tmr) + w).load(SeqCst)
        >> (vector % 64)
        & 1
        != 0
}
/// L4a: vCPU `v`'s LINT0 input level (the 8259's output). A rise at an APIC that passes ExtINTs to its core
/// (`natemu_abi::lapic::EXTINT`) counts as a post: true, the caller rings it.
pub(crate) fn set_lint0(memory: &Memory, v: usize, level: bool) -> bool {
    event_word(memory, v, std::mem::offset_of!(cache::Events, lint0))
        .store(u64::from(level), SeqCst);
    let post = level && published(memory, v) & natemu_abi::lapic::EXTINT != 0;
    if post {
        event_word(memory, v, std::mem::offset_of!(cache::Events, posts)).fetch_add(1, SeqCst);
    }
    post
}
/// vCPU `v`'s park state (X3): its `halted` word and its `posts` count, read in that order.
pub fn park_state(memory: &Memory, v: usize) -> (u64, u64) {
    let halted = event_word(memory, v, std::mem::offset_of!(cache::Events, halted)).load(SeqCst);
    (
        halted,
        event_word(memory, v, std::mem::offset_of!(cache::Events, posts)).load(SeqCst),
    )
}
/// A park state in which nothing but a new post can wake the vCPU: halted, its own timer unable to wake it, and every
/// post seen.
pub fn idle((halted, posts): (u64, u64)) -> bool {
    halted & 0xff == 1
        && halted >> cache::HALTED_SEEN_SHIFT == posts & (u64::MAX >> cache::HALTED_SEEN_SHIFT)
}
/// M2 Part 2: vCPU `area.vcpu` starts in the wait-for-SIPI state (an AP the firmware did not start: a boot without the ACPI
/// mailbox, Multiboot, a program driver's `wait-for-sipi` section; its state the INIT or power-on state the caller set).
/// Published in its pending word before any vCPU runs, so a SIPI sent before this one first enters the guest is latched
/// (`cache::pending::WAIT`; the resident's `startup::wait` takes it).
pub fn wait_for_sipi(area: Area) {
    event_word(area.memory, area.vcpu, std::mem::offset_of!(cache::Events, pending))
        .store(cache::pending::WAIT, SeqCst);
}
/// Where vCPU `area.vcpu` waited when its run ended, for the run reports: `sipi` (the wait-for-SIPI state: never started,
/// or parked by an INIT; never reported as halted), `mailbox` (the ACPI wakeup mailbox, never woken), `halted` (HLT), or
/// `None` (it was running).
pub fn waiting(area: Area) -> Option<&'static str> {
    let word = |field| event_word(area.memory, area.vcpu, field).load(SeqCst);
    let state: State = area.read_record(exec::STATE_OFFSET);
    if word(std::mem::offset_of!(cache::Events, pending)) & cache::pending::WAIT != 0 {
        Some("sipi")
    } else if state.mailbox != 0 {
        Some("mailbox")
    } else if word(std::mem::offset_of!(cache::Events, halted)) != 0 {
        Some("halted")
    } else {
        None
    }
}
/// M2 Part 2: vCPU `v`'s pending word (`cache::Events::pending`: NMI, INIT, wait-for-SIPI, a latched SIPI), for reports.
pub fn pending_word(memory: &Memory, v: usize) -> u64 {
    event_word(memory, v, std::mem::offset_of!(cache::Events, pending)).load(SeqCst)
}
/// vCPU `v`'s IRR (`cache::Events::vectors`), for reports.
pub fn irr(memory: &Memory, v: usize) -> [u64; 4] {
    std::array::from_fn(|w| {
        event_word(memory, v, std::mem::offset_of!(cache::Events, vectors) + 8 * w).load(SeqCst)
    })
}
/// Asks vCPU `v` to end its run when it is halted with no event it can take (`stop::HALTED`), or waits for a SIPI with
/// none latched (`stop::WAIT_FOR_SIPI`); the caller rings it.
pub fn end_when_halted(memory: &Memory, v: usize) {
    event_word(memory, v, std::mem::offset_of!(cache::Events, end)).store(1, SeqCst);
}
/// vCPU `area.vcpu`'s shadow resets so far (the resident's counter: evictions, shootdowns, control changes, halts).
pub fn shadow_resets(area: Area) -> u64 {
    global_u64(area.memory, cache::RESETS + 8 * area.vcpu).load(SeqCst)
}
/// K1: vCPU `area.vcpu`'s TLB events and revalidations (`cache::Control`); with M2's data shadows by memory type
/// (`memory_types`: {"UC": n, ...}, the types with a non-zero count) and its UC accesses (`uncached`: instructions run
/// alone for one, the resident's own).
pub fn k1_stats(area: Area) -> K1 {
    let control: cache::Control = area.read_record(cache::LOOKUP_OFFSET + cache::CONTROL);
    // The crash tripwire (`cache::Control::trip`): user-mode faults that look like a crash, on stderr.
    if control.trip_count != 0 {
        let n = control.trip_count;
        eprintln!(
            "natemu: vCPU {} user faults like a crash: {n} (the last {} below)",
            area.vcpu,
            n.min(cache::TRIP_ENTRIES as u64)
        );
        for k in n.saturating_sub(cache::TRIP_ENTRIES as u64)..n {
            let [rip, v, cr2, flags, code_rip, slot, cr3, targets] =
                control.trip[k as usize % cache::TRIP_ENTRIES];
            eprintln!(
                "natemu:   rip {rip:#x} vector {} error {:#x} cr2 {cr2:#x} block flags {:#x} translation {} code rip {code_rip:#x} slot {slot:#x} cr3 {cr3:#x} link targets (low 16 bits) {targets:#x}",
                v & 0xff,
                v >> 8,
                flags & 0xffff_ffff,
                flags >> 32
            );
        }
    }
    // Hot/cold layout (NATEMU_CHECK_LINKS set): the cache self-check after the run, trampolines included, on stderr.
    if std::env::var_os("NATEMU_CHECK_LINKS").is_some() {
        match check_links(area) {
            Ok((points, linked)) => eprintln!("natemu: vCPU {} check_links ok: {points} link points, {linked} linked", area.vcpu),
            Err(e) => eprintln!("natemu: vCPU {} check_links FAILED: {e}", area.vcpu),
        }
    }
    let mut k = K1::new(&control);
    let perf: exec::perf::Perf = area.read_record(exec::perf::OFFSET);
    if perf.magic == exec::perf::MAGIC {
        k.dispatch_misses = perf.count[exec::perf::MISS as usize];
    }
    k.pic_transitions = pic_transitions(area);
    // Translated-code profile (`profile` residents): the counters and the 80 blocks hit most, each with its guest bytes and,
    // when its code is still where its last sample found it, its native code.
    {
        use cache::profile as pr;
        let at = cache::LOOKUP_OFFSET + cache::PCID_STUDY;
        let counters: [u64; pr::COUNTERS] = area.read_record(at);
        if counters[pr::TOTAL] != 0 && control.pcid_study.iter().all(|&v| v == 0) {
            let mut hot: Vec<pr::Hot> = (0..pr::HOT_ENTRIES)
                .map(|i| area.read_record::<pr::Hot>(at + 4096 + i * size_of::<pr::Hot>()))
                .filter(|e| e.key != 0)
                .collect();
            hot.sort_by_key(|e| std::cmp::Reverse(e.count));
            let hex = |b: &[u8]| b.iter().map(|x| format!("{x:02x}")).collect::<String>();
            let cumulative = [10usize, 100, 1000, 10000]
                .map(|n| hot.iter().take(n).map(|e| u64::from(e.count)).sum::<u64>());
            let top = hot
                .iter()
                .take(80)
                .map(|e| {
                    let native = if u64::from(e.flushes) == control.flushes {
                        (0..(e.code_size as usize).min(4096) / 4)
                            .map(|k| hex(&area.read::<4>(cache::CODE_OFFSET + e.code as usize + 4 * k)))
                            .collect::<String>()
                    } else {
                        String::new()
                    };
                    serde_json::json!({"rip": e.rip, "user": e.key >> 63, "count": e.count, "guest_bytes": e.guest_bytes,
                        "code_size": e.code_size, "guest": hex(&e.bytes[..(e.guest_bytes as usize).min(24)]), "native": native})
                })
                .collect::<Vec<_>>();
            k.profile = Some(serde_json::json!({"counters": counters.to_vec(), "blocks": hot.len(), "cumulative": cumulative, "top": top}));
        }
    }
    // Code-size study: the live translations with code of their own at the end (blocks, guest bytes, native bytes; hot/cold
    // layout: bytes entered, a split block's hot copy or the whole code, and blocks split).
    for b in placed_blocks(area) {
        if b.flags & (cache::INSTANCE | cache::SYSTEM | cache::STEP | cache::DEAD) == 0 && b.code_size != 0 {
            k.code_size[0] += 1;
            k.code_size[1] += u64::from(b.guest_bytes);
            k.code_size[2] += u64::from(b.code_size);
            k.code_size[3] += u64::from(if b.hot_size != 0 { b.hot_size } else { b.code_size });
            k.code_size[4] += u64::from(b.hot_size != 0);
        }
    }
    k
}
/// PCID gain study counters are all zero (a resident without `pcid-study`).
fn pcid_study_empty(c: &[u64; natemu_abi::execution::cache::pcid_study::COUNT]) -> bool {
    c.iter().all(|&v| v == 0)
}
/// PIC study: a `pic-count` resident's linked transitions by class (same page, cross-page, position-independent into
/// absolute, absolute into position-independent), counted in four guest-state fields no generated code uses (the
/// resident's `cache::stub`), then its computed exits (32 bits, wrapping: FEX's flag bytes 28-31, `cache::computed`); zero
/// from other residents.
fn pic_transitions(area: Area) -> [u64; 5] {
    let mut t = [0x0, 0x8, 0x10, 0xb0, 0]
        .map(|at| u64::from_le_bytes(area.read::<8>(exec::FRAME_OFFSET + at)));
    t[4] = u64::from(u32::from_le_bytes(
        area.read::<4>(exec::FRAME_OFFSET + exec::frame::FLAGS as usize + 28),
    ));
    t
}
/// [`k1_stats`]: a vCPU's TLB, code-cache and system-instruction counters (times in us).
#[derive(Debug, Default, serde::Serialize)]
pub struct K1 {
    cr3_loads: u64,
    global_flushes: u64,
    invlpgs: u64,
    revalidated: u64,
    remapped: u64,
    capacity_flushes: u64,
    killed: u64,
    unvalidated: u64,
    tlb_us: u64,
    invlpg_us: u64,
    page_faults: u64,
    page_fault_us: u64,
    scanned: u64,
    memory_types: Members<&'static str, u64>,
    uncached: K1Uncached,
    l1_switches: u64,
    l1_clears: u64,
    flushes: u64,
    flush_us: u64,
    full: [u64; 3],
    /// [`Control::seg_stats`]: segments opened, hot bytes abandoned, cold bytes abandoned, bytes placed.
    segments: [u64; 4],
    used: [u64; 3],
    peak: [u64; 3],
    capacity: [u64; 3],
    reclaims: [u64; 2],
    reuse: K1Reuse,
    pool: K1Pool,
    slot_writes: u64,
    kept_resets: u64,
    /// Dispatch study (`natemu_abi::execution::cache::L1_EVICTED` and the rest; `dispatch-study` resident builds only).
    l1_counts: [u64; natemu_abi::execution::cache::L1_COUNTS],
    /// Dispatcher misses entered (the `perf` bucket `miss`'s entry count; 0 from a resident built without `perf`). K1's
    /// L1 programs take it as the difference between two runs of one program at different round counts.
    dispatch_misses: u64,
    systems: K1Systems,
    /// PIC study counters (`natemu_abi::pic::count`) and linked transitions by class ([`pic_transitions`]).
    pic: [u64; natemu_abi::pic::count::COUNTS],
    pic_transitions: [u64; 5],
    /// Page-currency chunk 0 (`page-study` resident builds only: absent when every counter is zero).
    #[serde(skip_serializing_if = "K1PageStudy::is_empty")]
    page_study: K1PageStudy,
    /// Code-size study: live translations with their own code at the vCPU's end: count, guest bytes, native bytes.
    code_size: [u64; 5],
    /// Translated-code profile (`profile` resident builds only).
    #[serde(skip_serializing_if = "Option::is_none")]
    profile: Option<serde_json::Value>,
    /// PCID gain study (`natemu_abi::execution::cache::pcid_study`; `pcid-study` resident builds only).
    #[serde(skip_serializing_if = "pcid_study_empty")]
    pcid_study: [u64; natemu_abi::execution::cache::pcid_study::COUNT],
    /// Page-currency plan chunk 1 (the page record, `Control::pages`) and the fast miss path (fast-miss plan): absent while every
    /// counter is zero.
    #[serde(skip_serializing_if = "K1Page::is_empty")]
    page: K1Page,
    #[serde(skip_serializing_if = "K1FastMiss::is_empty")]
    fast_miss: K1FastMiss,
    /// Relink plan (docs/restructure/relink-plan.md): absent while every counter is zero.
    #[serde(skip_serializing_if = "K1Relink::is_empty")]
    relink: K1Relink,
    /// The reverse map of writable shadows (docs/writable-drop.md, `natemu_abi::writers::count`): absent while every
    /// counter is zero.
    #[serde(skip_serializing_if = "writers_empty")]
    writers: [u64; natemu_abi::writers::count::COUNT],
    page_fault_scanned: u64,
    scanned_instances: u64,
    scanned_stale: u64,
    chain_dropped: u64,
    relisted: u64,
}
fn writers_empty(w: &[u64; natemu_abi::writers::count::COUNT]) -> bool {
    w.iter().all(|&n| n == 0)
}
/// The relink plan's counters: linker entries that linked a target found current as it was (`linked_current`, and those entries'
/// `link` bucket time, `perf` residents only).
#[derive(Debug, Default, serde::Serialize)]
struct K1Relink {
    linked_current: u64,
    linked_current_us: u64,
    /// Records suspended at TLB events, relinked at revalidations, and dropped by a relink; the suspended segment's length now.
    suspended: u64,
    relinked: u64,
    relink_dropped: u64,
    links_susp: u64,
}
impl K1Relink {
    fn is_empty(&self) -> bool {
        self.linked_current | self.linked_current_us | self.suspended | self.relinked | self.relink_dropped | self.links_susp == 0
    }
}
/// `Control::pages` and its counters: fetch walks made, record hits, check-mode differences, 2 MiB leaves not recorded.
#[derive(Debug, Default, serde::Serialize)]
struct K1Page {
    mode: u64,
    walks: u64,
    hits: u64,
    disagree: u64,
    split_2m: u64,
}
impl K1Page {
    fn is_empty(&self) -> bool {
        self.walks | self.hits | self.disagree | self.split_2m == 0
    }
}
/// The fast miss path: misses it served from the exit's frame, and the ones it declined by reason
/// (`natemu_abi::execution::cache::fast_decline`).
#[derive(Debug, Default, serde::Serialize)]
struct K1FastMiss {
    taken: u64,
    declined: Members<&'static str, u64>,
}
impl K1FastMiss {
    fn is_empty(&self) -> bool {
        self.taken == 0 && self.declined.0.is_empty()
    }
}
/// Page-currency plan chunk 0 (`natemu_abi::page_study::Counts`): fetch walks by caller (`[revalidation, placement]`) with the
/// record's would-be answers, its disagreements with the walk, and the walk and probe times in ns (`walk_ns[caller][hit]`: the
/// walks the record would not / would have answered). `class_*[caller]` are `[global, large, user]`; `blocks*` are
/// `[all, global, large, user, crossing]`.
#[derive(Debug, Default, serde::Serialize)]
struct K1PageStudy {
    calls: [u64; 2],
    hit_4k: [u64; 2],
    hit_2m: [u64; 2],
    class_calls: [[u64; 3]; 2],
    class_hits: [[u64; 3]; 2],
    faulted: [u64; 2],
    not_ram: [u64; 2],
    disagree: [u64; 2],
    disagree_frame: u64,
    disagree_flags: u64,
    disagree_fault: u64,
    records: u64,
    evictions: u64,
    split_2m: u64,
    clear_events: u64,
    cleared: u64,
    live_max: u64,
    probe_ns: u64,
    walk_ns: [[u64; 2]; 2],
    walk_count: [[u64; 2]; 2],
    blocks: [u64; natemu_abi::page_study::B_CLASSES],
    blocks_hit: [u64; natemu_abi::page_study::B_CLASSES],
    /// The cost of two counter reads in a row, in ns per pair (x1000: the counter ticks at 24 MHz).
    tick_pair_milli_ns: u64,
}
impl K1PageStudy {
    /// Written by a `page-study` resident only (its start-up measures the counter, so `tick_pairs` is nonzero then).
    fn is_empty(&self) -> bool {
        self.calls == [0; 2] && self.tick_pair_milli_ns == 0
    }
    fn new(c: &natemu_abi::page_study::Counts) -> Self {
        let pair = if c.tick_pairs == 0 {
            0
        } else {
            // (ticks_ns of the whole sum, so the 24 MHz quantum averages out over the pairs.)
            ticks_ns(c.tick_pair_ticks) * 1000 / c.tick_pairs
        };
        Self {
            calls: c.calls,
            hit_4k: c.hit_4k,
            hit_2m: c.hit_2m,
            class_calls: c.class_calls,
            class_hits: c.class_hits,
            faulted: c.faulted,
            not_ram: c.not_ram,
            disagree: c.disagree,
            disagree_frame: c.disagree_frame,
            disagree_flags: c.disagree_flags,
            disagree_fault: c.disagree_fault,
            records: c.records,
            evictions: c.evictions,
            split_2m: c.split_2m,
            clear_events: c.clear_events,
            cleared: c.cleared,
            live_max: c.live_max,
            probe_ns: ticks_ns(c.probe_ticks),
            walk_ns: c.walk_ticks.map(|w| w.map(ticks_ns)),
            walk_count: c.walk_count,
            blocks: c.blocks,
            blocks_hit: c.blocks_hit,
            tick_pair_milli_ns: pair.max(u64::from(c.tick_pairs != 0)),
        }
    }
}
#[derive(Debug, Default, serde::Serialize)]
struct K1Uncached {
    steps: u64,
    resident: u64,
}
#[derive(Debug, Default, serde::Serialize)]
struct K1Reuse {
    copies: u64,
    refused: [u64; 5],
    checks: u64,
}
/// The template pool's counters (cross-vCPU reuse plan chunk 3): lookups, blocks copied, refusals by
/// [`cache::pool_refusal`].
#[derive(Debug, Default, serde::Serialize)]
pub(crate) struct K1Pool {
    pub probes: u64,
    pub hits: u64,
    pub candidates: u64,
    pub refused: Members<&'static str, u64>,
    /// The re-check refusals by reason, `same|other` (the RIP the entry was published at) and `user|kernel` (the context's CPL).
    pub recheck: Members<String, u64>,
}
impl K1Pool {
    pub(crate) fn new(c: &cache::Control) -> Self {
        Self {
            probes: c.pool_probes,
            hits: c.pool_hits,
            candidates: c.pool_candidates,
            recheck: (0..cache::pool_recheck::COUNT * 4)
                .filter(|&i| c.pool_recheck[i] != 0)
                .map(|i| {
                    (
                        format!(
                            "{}.{}.{}",
                            cache::pool_recheck::NAMES[i / 4],
                            if i & 2 != 0 { "same" } else { "other" },
                            if i & 1 != 0 { "user" } else { "kernel" }
                        ),
                        c.pool_recheck[i],
                    )
                })
                .collect(),
            refused: cache::pool_refusal::NAMES
                .into_iter()
                .zip(c.pool_refused)
                .collect(),
        }
    }
}
/// Perf chunk 12: system instructions by kind ([count, transaction us]) and by the resident bucket that reached them.
#[derive(Debug, Default, serde::Serialize)]
struct K1Systems {
    kinds: Members<String, [u64; 2]>,
    from: Members<&'static str, u64>,
    chained: u64,
    declined: Members<&'static str, u64>,
}
impl K1 {
    fn new(c: &cache::Control) -> Self {
        use natemu_abi::execution::perf::{NAMES as BUCKETS, SYS_FAST_DECLINE_NAMES};
        const NAMES: [&str; 8] = ["UC", "WC", "2", "3", "WT", "WP", "WB", "UC-"];
        let us = |ticks| ticks_ns(ticks) / 1000;
        Self {
            cr3_loads: c.cr3_loads,
            global_flushes: c.global_flushes,
            invlpgs: c.invlpgs,
            revalidated: c.revalidated,
            remapped: c.remapped,
            capacity_flushes: c.capacity_flushes,
            killed: c.killed,
            unvalidated: c.unvalidated,
            tlb_us: us(c.tlb_ticks),
            invlpg_us: us(c.invlpg_ticks),
            page_faults: c.page_faults,
            page_fault_us: us(c.page_fault_ticks),
            scanned: c.scanned,
            memory_types: (0..8)
                .filter(|&t| c.memory_types[t] != 0)
                .map(|t| (NAMES[t], c.memory_types[t]))
                .collect(),
            uncached: K1Uncached {
                steps: c.uncached_steps,
                resident: c.uncached_resident,
            },
            l1_switches: c.l1_switches,
            l1_clears: c.l1_clears,
            flushes: c.flushes,
            flush_us: us(c.flush_ticks),
            full: c.full,
            segments: c.seg.stats,
            used: [c.code_used, c.meta_used, c.blocks],
            peak: [
                c.peak[0].max(c.code_used),
                c.peak[1].max(c.meta_used),
                c.peak[2].max(c.blocks),
            ],
            capacity: [
                c.code_bytes,
                cache::META_BYTES as u64,
                cache::MAX_BLOCKS as u64,
            ],
            reclaims: [c.reclaims, c.reclaimed],
            reuse: K1Reuse {
                copies: c.reuse_copies,
                refused: c.reuse_refused,
                checks: c.reuse_checks,
            },
            pool: K1Pool::new(c),
            slot_writes: c.slot_writes,
            kept_resets: c.kept_resets,
            l1_counts: c.l1_counts,
            dispatch_misses: 0,
            systems: K1Systems {
                kinds: (0..128)
                    .filter(|&k| c.system_counts[k] != 0)
                    .map(|k| {
                        (
                            format!("{k:#x}"),
                            [c.system_counts[k], us(c.system_ticks[k])],
                        )
                    })
                    .collect(),
                from: (0..32)
                    .filter(|&b| c.system_from[b] != 0)
                    .map(|b| (*BUCKETS.get(b).unwrap_or(&"?"), c.system_from[b]))
                    .collect(),
                chained: c.system_chained,
                declined: SYS_FAST_DECLINE_NAMES
                    .into_iter()
                    .zip(c.sys_fast_declined)
                    .collect(),
            },
            pic: c.pic_counts,
            pic_transitions: [0; 5],
            page_study: K1PageStudy::new(&c.page_study),
            pcid_study: c.pcid_study,
            code_size: [0; 5],
            profile: None,
            page: K1Page {
                mode: c.pages,
                walks: c.page_walks,
                hits: c.page_hits,
                disagree: c.page_disagree,
                split_2m: c.page_split_2m,
            },
            fast_miss: K1FastMiss {
                taken: c.fast_taken,
                declined: cache::fast_decline::NAMES
                    .into_iter()
                    .zip(c.fast_declined)
                    .filter(|&(_, n)| n != 0)
                    .collect(),
            },
            relink: K1Relink {
                linked_current: c.linked_current,
                linked_current_us: us(c.linked_current_ticks),
                suspended: c.suspended,
                relinked: c.relinked,
                relink_dropped: c.relink_dropped,
                links_susp: c.links_susp,
            },
            writers: c.writers,
            page_fault_scanned: c.page_fault_scanned,
            scanned_instances: c.scanned_instances,
            scanned_stale: c.scanned_stale,
            chain_dropped: c.chain_dropped,
            relisted: c.relisted,
        }
    }
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
        Self {
            kicks: (0..backing.n).map(|_| Mutex::default()).collect(),
            in_host: (0..backing.n).map(|_| AtomicBool::new(true)).collect(),
        }
    }
    /// Stops vCPU `v` of `machine` at its next block boundary; false (nothing rung) once its run [ended](Self::end).
    pub fn kick(&self, machine: &Machine, v: usize) -> Result<bool> {
        self.kick_if_needed(machine, v, true)
    }
    /// An admitted I/O participant still needs the terminal check, but no new doorbell.
    fn kick_if_needed(&self, machine: &Machine, v: usize, needed: bool) -> Result<bool> {
        let mut k = self
            .kicks
            .get(v)
            .ok_or_else(|| format!("kick of vCPU {v}: no such vCPU"))?
            .lock()
            .unwrap();
        if k.ended {
            return Ok(false);
        }
        if !needed {
            return Ok(true);
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
            let ticks = || {
                area.read_record::<u64>(
                    exec::STATE_OFFSET + std::mem::offset_of!(State, stop_ticks),
                )
            };
            let stop = (0..10_000)
                .map(|_| ticks())
                .find(|&t| t >= at)
                .unwrap_or(at);
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
            let meta =
                u32::from_le_bytes(area.read::<4>(cache::LOOKUP_OFFSET + cache::INDEX + 4 * n));
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
        Some(&[_, original])
            if u32::from_le_bytes(area.read::<4>(cache::CODE_OFFSET + offset as usize))
                == exec::STOP_WORD =>
        {
            original
        }
        _ => u32::from_le_bytes(area.read::<4>(cache::CODE_OFFSET + offset as usize)),
    };
    let blocks = placed_blocks(area);
    // Each block's entry (the first in placement order, as a scan would find it).
    let mut entries = HashMap::new();
    for (i, t) in blocks.iter().enumerate().filter(|(_, t)| t.flags & (cache::SYSTEM | cache::INSTANCE) == 0) {
        entries.entry(t.entry() - cache::CODE_VA).or_insert(i);
    }
    let mut linked = HashMap::new();
    let mut points = 0;
    // PIC study stage 3: an instance's link points are its translation's (which holds the words); the dispatcher slots.
    let slots = (cache::DISPATCH + cache::DISPATCH_SLOT_BYTES) as u64
        ..(cache::DISPATCH + cache::DISPATCH_BYTES) as u64;
    for b in blocks
        .iter()
        .filter(|b| b.flags & (cache::INSTANCE | cache::SYSTEM) == 0)
    {
        // Hot/cold layout: a split block's links patch its trampolines (below, as link points); its computed exit's
        // trampoline is `b` where the exit's `b` goes, and its exit words in its code stay as admitted (armed: by their words
        // before the stop).
        for (from, at) in b.trampolines() {
            let point = b.links.iter().find(|p| p.offset != 0 && u64::from(b.code) + u64::from(p.offset) == from);
            let ok = match point {
                Some(p) => word(from) == p.word,
                None => word(at) == exec::trampoline_word(word(from), from, at),
            };
            if !ok {
                return Err(format!("trampoline of the block at {:#x}: word {:#010x}, its exit word {:#010x}", b.rip, word(at), word(from)));
            }
        }
        for p in b.links.iter().filter(|p| p.offset != 0) {
            points += 1;
            let at = b.link_at(p.offset);
            let w = word(at);
            let bad = || {
                format!(
                    "link point {:#x} of the block at {:#x}: word {w:#010x}",
                    p.offset, b.rip
                )
            };
            // (A trampoline linked to the block right after it: `nop`.)
            let branch = |at: u64, w: u32| {
                (w & 0xfc00_0000 == 0x1400_0000)
                    .then(|| at.wrapping_add((((w << 6) as i32 >> 6) as i64 * 4) as u64))
                    .or((b.hot_size != 0 && w == 0xd503_201f).then_some(at + 4))
            };
            if w == b.admitted_at(p) {
                // Its `svc` word: as admitted, or a checked link's `b`.
                let svc = u64::from(b.code)
                    + natemu_abi::pic::svc_slot(|o| word(u64::from(b.code) + o), p);
                let w = word(svc);
                if w == natemu_abi::pic::SVC_LINK {
                    continue;
                }
                // A checked link: `b` to a dispatcher slot.
                let slot =
                    |to: u64| slots.contains(&to) && to % cache::DISPATCH_SLOT_BYTES as u64 == 0;
                if !branch(svc, w).is_some_and(slot) {
                    return Err(bad());
                }
                linked.insert((b.number, (svc - u64::from(b.code)) as u32), cache::CHECKED);
                continue;
            }
            // Its first word: `b` to a block's body. From absolute code, an absolute block keyed (target, context); from
            // shared code, a shared translation of the frame it starts on, at the target's page offset (local).
            let pic = |t: &cache::Block| t.flags & cache::PIC != 0;
            let fits = |t: &cache::Block| {
                t.context == b.context
                    && if pic(b) {
                        pic(t)
                            && t.source_gpa[0] == b.source_gpa[0]
                            && p.target >> 12 == b.rip >> 12
                            && t.rip & 0xfff == p.target & 0xfff
                    } else {
                        !pic(t) && t.rip == p.target
                    }
            };
            // M2 task 4, the second entry: between two blocks that begin with the test for an open x87 run the link goes
            // behind the target's test (its two words; the resident's `link_entry`), and between no others.
            let run_tested =
                |t: &cache::Block| t.code_size != 0 && exec::run_test_end(|o| word(u64::from(t.code) + o), u64::from(t.code_size)).is_some();
            let target = branch(at, w).and_then(|to| {
                entries.get(&to).copied().filter(|&t| !(run_tested(b) && run_tested(&blocks[t]))).or_else(|| {
                    entries.get(&to.wrapping_sub(8)).copied().filter(|&t| run_tested(b) && run_tested(&blocks[t]))
                })
            });
            match target {
                Some(t) if fits(&blocks[t]) => linked.insert((b.number, p.offset), t as u32),
                _ => return Err(bad()),
            };
        }
    }
    // M2 task 4: the one word of a block's code the resident changes besides its link points: the return of the run's end
    // of a top x87 run test, to the third word of the copy the block is entered at (its hot copy's when it has one).
    for b in blocks.iter().filter(|b| b.flags & (cache::INSTANCE | cache::SYSTEM) == 0 && b.code_size != 0) {
        if let Some(e) = exec::run_test_end(|o| word(u64::from(b.code) + o), u64::from(b.code_size)) {
            let at = u64::from(b.code) + e + 36;
            let w = word(at);
            let to = at.wrapping_add((((w << 6) as i32 >> 6) as i64 * 4) as u64);
            if w >> 26 != 5 || to != b.entry() - cache::CODE_VA + 8 {
                return Err(format!("x87 run test of the block at {:#x}: its end returns by {w:#010x}", b.rip));
            }
        }
    }
    // The shared probes of page-crossing stores (`natemu_abi::execution::probe`): the head holds exactly every probe's words,
    // and every probing split stub of a block with code of its own (`adr x0` its first word) branches to its store's probe.
    for i in 0..exec::probe::THUNKS as u32 {
        let at = exec::probe::offset(i);
        if let Some(k) = exec::probe::words(i).iter().enumerate().position(|(k, &w)| word(at + 4 * k as u64) != w) {
            return Err(format!("shared probe {i}: word {k} is {:#010x}", word(at + 4 * k as u64)));
        }
    }
    for n in 0..control.blocks as usize {
        let meta = u32::from_le_bytes(area.read::<4>(cache::LOOKUP_OFFSET + cache::INDEX + 4 * n)) as usize;
        let b: cache::Block = area.read_record(cache::META_OFFSET + meta);
        if b.flags & (cache::INSTANCE | cache::SYSTEM) != 0 || b.code_size == 0 {
            continue;
        }
        let bits32 = exec::context_bits32(b.context);
        for k in 0..b.sites_count as usize {
            let site: natemu_abi::code_object::Site =
                area.read_record(cache::META_OFFSET + meta + size_of::<cache::Block>() + k * size_of::<natemu_abi::code_object::Site>());
            let at = u64::from(b.code) + u64::from(site.native_offset);
            if site.kind != natemu_abi::code_object::kind::SPLIT || exec::probe::adr_x0_target(word(at), at).is_none() {
                continue;
            }
            let want = exec::probe::index(bits32, site.base_reg, u32::from(site.width)).map(|i| exec::probe::branch_word(at + 4, i));
            if want != Some(word(at + 4)) {
                return Err(format!("probing split stub at {:#x} of the block at {:#x}: word {:#010x}", site.native_offset, b.rip, word(at + 4)));
            }
        }
    }
    let records: Vec<cache::LinkRecord> = (0..control.links as usize)
        .map(|i| link_record(area, i))
        .collect();
    // Relink plan: the segments in order (suspended, CR3, TLB, local, checked); the suspended records (their words restored at a
    // TLB event) are not linked points: only the live ones are described by the words below.
    let ends = [control.links_susp, control.links_cr3, control.links_tlb, control.links_local, control.links];
    if ends.windows(2).any(|w| w[0] > w[1]) {
        return Err(format!("link segment ends {ends:?} are not in order"));
    }
    let live = &records[control.links_susp as usize..];
    // Each record also holds the patched word as admitted: the point's first word, or (checked) `svc #LINK`.
    let described = live.iter().all(|r| {
        let Some(b) = blocks.get(r.source as usize) else {
            return false;
        };
        let checked = r.target & cache::CHECKED != 0;
        let svc = |p: &exec::LinkPoint| {
            natemu_abi::pic::svc_slot(|o| word(u64::from(b.code) + o), p) == u64::from(r.offset())
        };
        // (The point its record names: `Block::links[r.point()]`.)
        let point = b.links.get(r.point()).filter(|p| {
            p.offset != 0
                && if checked {
                    svc(p)
                } else {
                    p.offset == r.offset()
                }
        });
        linked.get(&(r.source, r.offset())) == Some(&r.target)
            && point.is_some_and(|p| {
                r.original
                    == if checked {
                        natemu_abi::pic::SVC_LINK
                    } else {
                        b.admitted_at(p)
                    }
            })
    });
    if !described || live.len() != linked.len() {
        return Err(format!(
            "{} live link records for {} linked points",
            live.len(),
            linked.len()
        ));
    }
    // The inbound lists: each block's chain holds exactly the records naming it (checked ones on none), doubly linked.
    let mut on_lists = 0;
    for (n, b) in blocks.iter().enumerate() {
        let (mut at, mut prev) = (b.inbound, 0);
        while at != 0 {
            let r = records
                .get(at as usize - 1)
                .filter(|r| {
                    r.target as usize == n && r.prev_in == prev && on_lists <= records.len()
                })
                .ok_or_else(|| format!("block {n}'s inbound list is broken at record {}", at - 1))?;
            (prev, at, on_lists) = (at, r.next_in, on_lists + 1);
        }
    }
    // The premise of the lists' use at INVLPG and #PF: every non-local record (segments before `links_tlb`) enters a block
    // that is current now (the resident's `current`) with INBOUND set; every local one, a block with LOCAL_INBOUND.
    for (i, r) in records.iter().enumerate() {
        if r.target & cache::CHECKED != 0 {
            continue;
        }
        let Some(t) = blocks.get(r.target as usize) else {
            return Err(format!("record {i} names block {}", r.target));
        };
        let local = i as u64 >= control.links_tlb;
        let current = t.checked == control.epoch
            || (t.flags & cache::GLOBAL != 0 && t.checked >= control.global_epoch);
        // A suspended record (relink plan): a live target (its source may be dead), the target not current (the epoch moved at the event that
        // suspended it, and the target is revalidated only by a lookup, which relinks or drops its records), INBOUND set (a cut
        // must find the list), the point it names present with its admitted word, and that word unlinked, or held by exactly one
        // live record of the same point (the point was linked elsewhere meanwhile: the linker's link into another block of the
        // same RIP).
        if (i as u64) < control.links_susp {
            let Some(source) = blocks.get(r.source as usize) else {
                return Err(format!("suspended record {i} names block {} as its source", r.source));
            };
            // The point the record names (by its index, as the resident's relink reads it), and its pairing with the target as `link`
            // required it: the point's offset is the record's, its target the block's RIP, both in one context, an absolute source
            // and a target that is not an instance (shared and instance links are checked or local: never suspended).
            let point = source.links.get(r.point()).filter(|p| p.offset != 0 && p.offset == r.offset());
            let admitted = point.is_some_and(|p| {
                r.original == source.admitted_at(p)
                    && word(source.link_at(p.offset)) == source.admitted_at(p)
                    && word(u64::from(source.code) + u64::from(p.offset)) == p.word
                    && word(u64::from(source.code) + natemu_abi::pic::svc_slot(|o| word(u64::from(source.code) + o), p))
                        == natemu_abi::pic::SVC_LINK
            });
            let paired = point.is_some_and(|p| {
                p.target == t.rip
                    && source.context == t.context
                    && source.flags & (cache::PIC | cache::INSTANCE | cache::SYSTEM | cache::STEP) == 0
                    && t.flags & (cache::INSTANCE | cache::SYSTEM | cache::STEP) == 0
            });
            let held = linked.contains_key(&(r.source, r.offset()));
            let mut why = None;
            if t.flags & cache::DEAD != 0 {
                why = Some("a dead target");
            } else if current {
                why = Some("a current target");
            } else if t.flags & cache::INBOUND == 0 {
                why = Some("no INBOUND on the target");
            } else if point.is_none() || r.original != point.map_or(0, |p| source.admitted_at(p)) {
                why = Some("no such link point, or another original word");
            } else if !paired {
                why = Some("a source and target that `link` would not pair (RIP, context, shared or instance)");
            } else if !admitted && !held {
                why = Some("a word that is neither unlinked nor held by a live record");
            }
            if let Some(why) = why {
                return Err(format!("suspended record {i} into the block at {:#x}: {why}", t.rip));
            }
            continue;
        }
        // The live CR3 segment's targets are not global, the TLB segment's are.
        if !local && ((i as u64) < control.links_cr3) == (t.flags & cache::GLOBAL != 0) {
            return Err(format!(
                "record {i} ({}) into the block at {:#x}, flags {:#x}",
                if (i as u64) < control.links_cr3 { "CR3 segment" } else { "TLB segment" },
                t.rip,
                t.flags
            ));
        }
        if local && t.flags & cache::LOCAL_INBOUND == 0
            || !local && !(current && t.flags & cache::INBOUND != 0)
        {
            return Err(format!(
                "record {i} ({}) into the block at {:#x}: current {current}, flags {:#x}",
                if local { "local" } else { "non-local" },
                t.rip,
                t.flags
            ));
        }
    }
    let unchecked = records.iter().filter(|r| r.target & cache::CHECKED == 0).count();
    if on_lists != unchecked {
        return Err(format!(
            "{on_lists} records on inbound lists, {unchecked} unchecked links"
        ));
    }
    // A dead block has no link into or out of it (its death undid them): no record names one.
    let dead = |n: u32| blocks.get(n as usize).is_some_and(|b| b.flags & cache::DEAD != 0);
    // (Relink plan: but a suspended record's source may be dead: the record is named by no outbound slot, and the target's next
    // relink or a reclaim drops it.)
    if let Some(i) = records.iter().enumerate().position(|(i, r)| {
        dead(r.source) && i as u64 >= control.links_susp || r.target & cache::CHECKED == 0 && dead(r.target)
    }) {
        return Err(format!("record {i} names a dead block"));
    }
    // The outbound slots: each block's slot k names a live record of its point k (checked ones too) or none, and every live record
    // is named by its source's slot (a suspended one by none: the point may hold another link).
    let mut named = 0;
    for (n, b) in blocks.iter().enumerate() {
        for (k, &slot) in b.out.iter().enumerate().filter(|&(_, &s)| s != 0) {
            records
                .get(slot as usize - 1)
                .filter(|r| r.source as usize == n && r.point() == k && slot as u64 > control.links_susp)
                .ok_or_else(|| format!("block {n}'s outbound slot {k} is broken at record {}", slot - 1))?;
            named += 1;
        }
    }
    if named != live.len() {
        return Err(format!("{named} records in outbound slots, {} live links", live.len()));
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
    /// guest loop ran past the budget) or in the resident (EL1: a resident wait, e.g. for a peer's shadow reset or page
    /// drop, never ended); `asked` = the peers whose `SHADOWS` or `DROPS` request was still pending then (bit p: vCPU p).
    Watchdog { pc: u64, el0: bool, asked: u64 },
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
    fn mmap(
        address: *mut c_void,
        length: usize,
        prot: i32,
        flags: i32,
        fd: i32,
        offset: i64,
    ) -> *mut c_void;
    fn munmap(address: *mut c_void, length: usize) -> i32;
    fn close(fd: i32) -> i32;
    fn fcntl(fd: i32, cmd: i32, ...) -> i32;
    fn os_sync_wait_on_address_with_timeout(
        addr: *mut u8,
        value: u64,
        size: usize,
        flags: u32,
        clock: u32,
        timeout_ns: u64,
    ) -> i32;
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
        let name = std::ffi::CString::new(format!(
            "/natemu.{}.{}",
            std::process::id(),
            COUNT.fetch_add(1, Relaxed)
        ))
        .unwrap();
        const F_SETFD: i32 = 2;
        const FD_CLOEXEC: i32 = 1;
        let fd = unsafe {
            shm_open(
                name.as_ptr(),
                O_RDWR | O_CREAT | O_EXCL,
                0o600 as std::ffi::c_uint,
            )
        };
        if fd < 0 {
            return Err(format!(
                "fexc channel: shm_open: {}",
                std::io::Error::last_os_error()
            ));
        }
        unsafe { shm_unlink(name.as_ptr()) };
        const PROT_RW: i32 = 3;
        const MAP_SHARED: i32 = 1;
        let base = match unsafe {
            fcntl(fd, F_SETFD, FD_CLOEXEC) == 0 && ftruncate(fd, Self::BYTES as i64) == 0
        } {
            true => unsafe {
                mmap(
                    std::ptr::null_mut(),
                    Self::BYTES,
                    PROT_RW,
                    MAP_SHARED,
                    fd,
                    0,
                ) as *mut u8
            },
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
            return Err(std::io::Error::other(format!(
                "a request of {} bytes does not fit the fexc channel",
                body.len()
            )));
        }
        self.n = self.n.wrapping_add(1) & 0x7fff_ffff;
        unsafe {
            channel_copy(
                self.base.add(Self::REQUEST),
                body.as_ptr() as *mut u8,
                body.len(),
                true,
            )
        };
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
    fn reply(
        &self,
        deadline: Instant,
        mut dead: impl FnMut() -> std::io::Result<()>,
    ) -> std::io::Result<Vec<u8>> {
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
            let ns = unsafe {
                os_sync_wait_on_address_with_timeout(
                    self.base.add(64),
                    (v | 1) as u64,
                    4,
                    SHARED,
                    MACH_ABSOLUTE_TIME,
                    left.max(1),
                )
            };
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
            return Err(std::io::Error::other(format!(
                "malformed fexc reply: channel magic {magic:#x}"
            )));
        }
        let len = self.word(68).load(Relaxed) as usize;
        if !(20..=Self::REPLY_CAP).contains(&len) {
            return Err(std::io::Error::other(format!(
                "malformed fexc reply: length {len}"
            )));
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
            if into {
                m.write_volatile(b.read_unaligned())
            } else {
                b.write_unaligned(m.read_volatile())
            }
        }
        for i in words * 8..len {
            if into {
                at.add(i).write_volatile(*buf.add(i))
            } else {
                *buf.add(i) = at.add(i).read_volatile()
            }
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
        crate::perf::mark(&format!(
            "server_start{}",
            std::thread::current()
                .name()
                .unwrap_or("")
                .trim_start_matches("vcpu-")
        ));
        Ok(Self {
            child,
            stdin: Some(stdin),
            stdout,
            reaped: false,
            channel,
        })
    }
    /// [`Self::request_with_posted`] with no observer.
    #[cfg(test)]
    fn request(&mut self, body: &[u8], deadline: Instant) -> std::io::Result<Reply> {
        self.request_with_posted(body, deadline, || Ok(()))
    }
    /// The server's reply to one request body; a `TimedOut` error when the whole reply has not arrived by `deadline`, an
    /// `UnexpectedEof` one when the server died first (its stdout closed; seen within [`Channel::SLICE_NS`]). `posted`
    /// runs once the request is posted, before its reply is read (the test driver's [`Hook::compiler_posted`]); its error
    /// is returned without consuming the reply.
    fn request_with_posted(
        &mut self,
        body: &[u8],
        deadline: Instant,
        posted: impl FnOnce() -> std::io::Result<()>,
    ) -> std::io::Result<Reply> {
        if self.stdin.is_none() {
            return Err(std::io::Error::other("fexc --serve: closed"));
        }
        self.channel.post(body)?;
        posted()?;
        let stdout = &mut self.stdout;
        let mut reply = self.channel.reply(deadline, || Self::alive(stdout))?;
        let field = |at: usize| u32::from_le_bytes(reply[at..at + 4].try_into().unwrap());
        if reply.len() < 20 || reply.len() != 20 + field(4) as usize + field(8) as usize {
            return Err(std::io::Error::other("malformed fexc reply"));
        }
        let (code, json_len, extent, read) =
            (field(0) as i32, field(4) as usize, field(12), field(16));
        let object = reply.split_off(20 + json_len);
        let json = reply.split_off(20);
        Ok(Reply {
            code,
            json,
            object,
            extent,
            read,
        })
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
        let mut fd = PollFd {
            fd: std::os::fd::AsRawFd::as_raw_fd(stdout),
            events: POLLIN,
            revents: 0,
        };
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
                Ok(Some(status)) => {
                    return format!("fexc --serve (pid {pid}) exited ({status}): {e}");
                }
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
        let (pid, mut status, mut r) = (
            self.child.id() as i32,
            0,
            Rusage {
                user: [0; 2],
                system: [0; 2],
                max_rss: 0,
                rest: [0; 13],
            },
        );
        // EOF ends it at once; one that does not end within a second is killed (its CPU time is still reported).
        let mut waited = 0;
        loop {
            match unsafe {
                wait4(
                    pid,
                    &mut status,
                    if waited < 1000 { WNOHANG } else { 0 },
                    &mut r,
                )
            } {
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
        let mut p = [self.code as u32, self.extent, self.read, json.len() as u32]
            .map(u32::to_le_bytes)
            .concat();
        p.extend_from_slice(json);
        p.extend_from_slice(&self.object);
        p
    }
    fn from_payload(p: &[u8]) -> Option<Self> {
        let field = |i: usize| {
            p.get(4 * i..4 * i + 4)
                .map(|b| u32::from_le_bytes(b.try_into().unwrap()))
        };
        let (code, extent, read, json_len) = (field(0)?, field(1)?, field(2)?, field(3)? as usize);
        let json = p.get(16..16 + json_len)?.to_vec();
        Some(Self {
            code: code as i32,
            json,
            object: p[16 + json_len..].to_vec(),
            extent,
            read,
        })
    }
}

/// What fexc answered (errors are not kept): an object, or no code with its end reason and, when the instruction at the RIP
/// cut the block, its opcode map and opcode and whether its gating cut it (`Some`: "mmx" for an MMX form, "null_segment"
/// for an access through a null segment in 32-bit code, else ""), or (`ud`, I10) whether
/// fexc found an encoding the presented CPU raises #UD for (reserved or unadvertised: `cut_ud`).
#[derive(Clone, Debug, PartialEq)]
enum Answer {
    Object(Vec<u8>),
    Cut {
        reason: String,
        cut: Option<(u8, u8)>,
        gated: Option<&'static str>,
        ud: bool,
    },
}
impl Answer {
    fn new(reply: Reply, rip: u64) -> Result<Self> {
        if reply.code == 0 {
            return Ok(Answer::Object(reply.object));
        }
        let end = serde_json::from_slice::<FexcReport>(&reply.json)
            .map_err(|e| format!("fexc report without guest.end.reason: {e}"))?
            .guest
            .end;
        Ok(Answer::Cut {
            cut: end.system_cut(rip),
            gated: end.cut_gated.map(|v| match v.as_str() {
                Some("mmx") => "mmx",
                Some("null_segment") => "null_segment",
                _ => "",
            }),
            ud: end.cut_ud.is_some(),
            reason: end.reason,
        })
    }
    /// This answer, kept for `from`, as fexc's answer at `to` (same half and page offset): an object relocated
    /// ([`crate::relocation`]), a no-code answer fexc decides from the bytes alone; `None` when it would not be fexc's.
    fn at(&self, from: u64, to: u64) -> Option<Self> {
        match self {
            Answer::Object(o) => crate::relocation::object(o, from, to).map(Answer::Object),
            Answer::Cut { reason, .. } => (crate::relocation::same_place(from, to)
                && crate::relocation::no_code_reusable(reason))
            .then(|| self.clone()),
        }
    }
}
/// The number of fexc's gating arguments (`FEXC_ARG_*`, [`Compiler::gating`]): the low bits of its `args_read`. The next
/// bit is the x87 guess's, which is not one of them: 32-bit code's answers carry it in their tagged RIP ([`tag32`]).
pub(crate) const GATING: usize = 15;
/// M2 reuse: the kept answers' RIP of 32-bit code, tagged in its upper half, which a 32-bit address leaves free, with
/// everything of the request that 64-bit code has not: bit 62 (32-bit code; with bit 63 clear, so no canonical address:
/// 64-bit code also runs below 4 GiB, and never shares an entry), the null FS, GS, DS and ES (bits 32 to 35), the limited
/// FS and GS (M2 checkpoint C: bits 49 and 50) and the
/// x87 guess (from bit 36; 13 bits): the request's for an answer whose compile read it, [`GUESS_FREE`] for one that did
/// not (a unit without x87 instructions: one entry, and one publication, under every guess). The answers table alone
/// sees it ([`Answers::find`], [`Answers::insert`], an entry's fingerprint): an answer of another tag is under another
/// key, never a hit; every address use (relocation's distance, admission, fexc's request, the persistent cache's key,
/// the pool) has the plain RIP.
const TAG32: u64 = 1 << 62;
/// The guess field of a 32-bit answer that did not read the guess: no guess's value (a guess is 0 or has bit 12 set and
/// bit 11 clear: 0x1000 | TOP << 8 | tags).
const GUESS_FREE: u64 = 0x800;
fn tag32(null_segments: [bool; 4], limited_segments: [bool; 2], x87_guess: u64) -> Result<u64> {
    if x87_guess >> 13 != 0 {
        return Err(format!("x87 guess {x87_guess:#x}: more than 13 bits").into());
    }
    let null = null_segments.iter().enumerate().fold(0, |m, (i, &null)| m | u64::from(null) << i);
    let limited = limited_segments.iter().enumerate().fold(0, |m, (i, &on)| m | u64::from(on) << i);
    Ok(TAG32 | limited << 49 | x87_guess << 36 | null << 32)
}
/// A tagged RIP ([`tag32`]): no canonical address has bit 62 set and bit 63 clear.
fn tagged(at: u64) -> bool {
    at >> 62 == 1
}
/// The address of a kept answer's RIP, tagged or not.
fn untagged(at: u64) -> u64 {
    if tagged(at) { at & 0xffff_ffff } else { at }
}
/// The memory cache's key (perf chunk 4): the block limit, the RIP's half (bit 63) and page offset, and the first bytes the
/// answer read (at most [`LEAD`], with their count): an answer is found at every RIP of that half and offset. As words
/// ([`Universal`]); no limit is `1 << 32`. `at`: the RIP as the table holds it, 32-bit code's tagged ([`tag32`]): the tag
/// is part of the key with the page offset.
type Key = (u64, u64, u64);
const LEAD: usize = 4;
fn key(limit: Option<u32>, at: u64, lead: &[u8]) -> Key {
    (
        limit.map_or(1 << 32, u64::from),
        at & if tagged(at) { !0xffff_f000 } else { 1 << 63 | 0xfff },
        lead.iter()
            .fold(lead.len() as u64, |v, &b| v << 8 | u64::from(b)),
    )
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
        let r = |i: u64| {
            u128::from(std::hash::BuildHasher::hash_one(&s, 2 * i)) << 64
                | u128::from(std::hash::BuildHasher::hash_one(&s, 2 * i + 1))
        };
        Self([r(0), r(1), r(2), r(3)])
    }
}
impl std::hash::BuildHasher for Universal {
    type Hasher = Words;
    fn build_hasher(&self) -> Words {
        Words {
            keys: self.0,
            sum: self.0[3],
            i: 0,
        }
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
        self.sum = self
            .sum
            .wrapping_add(self.keys[self.i].wrapping_mul(u128::from(x)));
        self.i += 1;
    }
    fn finish(&self) -> u64 {
        ((self.sum >> 64) as u64).reverse_bits()
    }
}
/// Answers per generation of the memory cache ([`Shard::insert`]), over all its shards. Sized for a parallel build's working
/// set (IO5, November 13: a CPython `make -j4` asks for about 660 k distinct blocks; with 2 x 256 k the generations cycled and
/// fexc compiled 1.8 M answers again for other vCPUs); the installer boot keeps about 130 k.
const GENERATION: usize = 1 << 20;
/// And bytes per generation (answers average about 650 bytes: the build's set is about 450 MB).
const GENERATION_BYTES: usize = 512 << 20;
/// The memory cache's shards (perf chunk 13), each locked alone and bounded at a sixteenth of the generation bounds.
const SHARDS: usize = 16;
/// A kept answer and what it was compiled from: its RIP, the gating values (those with a bit in `read` matter) and the guest
/// bytes before the compile's extent (`whole`: the extent reached their end, so their length matters too); `owner`: the vCPU
/// that put it in the map (a compile or a disk answer). Kept by reference (perf chunk 13): another vCPU's insert may drop it
/// from the map between a lookup and its use, which keeps its own reference.
struct Entry {
    rip: u64,
    /// The tag of `rip` in the answers table ([`tag32`]; 0: 64-bit code).
    tag: u64,
    owner: u8,
    read: u32,
    gating: [u64; GATING],
    prefix: Vec<u8>,
    whole: bool,
    answer: Answer,
    /// Option (d): what admission made of `answer` at `rip`, set by the first use there ([`Compiler::answered`]).
    kept: std::sync::OnceLock<Box<Kept>>,
    /// The template pool: the position this answer was last published at (0: never), so it is published once and again only
    /// when that position has fallen below the ring's tail ([`crate::publisher::Publisher::publish`]).
    published: AtomicU64,
}
/// Why an object answer was admitted again, or not ([`Compiler::admissions`]; the performance log's `state.admissions`). A hit
/// that cannot reuse its kept form gets the first of `CPL0`, `BULK`, `SHAPE`, `SHORT_PREFIX` that applies.
pub(crate) mod why {
    /// The kept form was reused: no admission.
    pub const SKIPPED: usize = 0;
    /// The first use of an answer fexc just compiled, one the persistent cache gave, and one fexc compiled again after the
    /// memory cache dropped it.
    pub const FRESH: usize = 1;
    pub const DISK: usize = 2;
    pub const RECOMPILED: usize = 3;
    /// A hit whose answer keeps no form yet (no admission of it at its own RIP, or only at another RIP's first use).
    pub const NO_KEPT: usize = 4;
    pub const CPL0: usize = 5;
    pub const BULK: usize = 6;
    pub const SHAPE: usize = 7;
    pub const SHORT_PREFIX: usize = 8;
    /// The kept form could not be moved to another RIP (a relocation that does not pack, too many, `relocate_code`'s
    /// refusal, or its code differing from the moved answer's).
    pub const MOVE_REFUSED: usize = 9;
    /// An object the host translates again with fewer instructions ([`crate::admission::retry`]): never admitted.
    pub const RETRY: usize = 10;
    pub const NAMES: [&str; 11] = [
        "skipped",
        "fresh",
        "disk",
        "recompiled",
        "no_kept",
        "cpl0",
        "bulk",
        "shape",
        "short_prefix",
        "move_refused",
        "retry",
    ];
}
/// An answer's admitted form (its record without the object, which [`crate::relocation::resolved`] gives back) and the
/// CPL-0, bulk and code-shape values admission ran under: it is reused only where they are the request's. The feature
/// bits (`supported`) are not compared: they are fixed per [`Compiler`], and [`Compiler::share`] joins only maps made for
/// equal ones.
struct Kept {
    cpl0: bool,
    bulk: bool,
    shape: bool,
    form: admission::Admitted,
}
impl Entry {
    /// PIC study: what makes it the same answer (its key with `limit`, gating values read, bytes read), hashed: a later
    /// compile with this fingerprint is one of an answer the map dropped.
    fn fingerprint(&self, limit: Option<u32>) -> u64 {
        let k = key(limit, self.rip | self.tag, &self.prefix[..LEAD.min(self.prefix.len())]);
        let read: Vec<u64> = (0..GATING)
            .filter(|&i| self.read & 1 << i != 0)
            .map(|i| self.gating[i])
            .collect();
        std::hash::BuildHasher::hash_one(
            &std::hash::BuildHasherDefault::<std::collections::hash_map::DefaultHasher>::default(),
            (k, self.read, read, &self.prefix, self.whole),
        )
    }
    /// Bytes it keeps (the memory cache's bound).
    fn size(&self) -> usize {
        size_of::<Self>()
            + self.prefix.len()
            + if let Answer::Object(o) = &self.answer {
                // And its kept form ([`Kept`], its link points at most): set after the answer is, but counted with it.
                o.len() + size_of::<Kept>() + exec::MAX_LINK_POINTS * size_of::<exec::LinkPoint>()
            } else {
                0
            }
    }
    /// fexc gives the same answer for `bytes` under `gating`: it reads nothing that differs (tools/fexc/fexc.cpp, `Extent`
    /// and `FexcArg`).
    fn matches(&self, bytes: &[u8], gating: &[u64; GATING]) -> bool {
        // (Bit `GATING`, the x87 guess: 32-bit code's, matched by the entry's tag.)
        self.read >> (GATING + 1) == 0
            && (0..GATING).all(|i| self.read & 1 << i == 0 || self.gating[i] == gating[i])
            && if self.whole {
                bytes == self.prefix
            } else {
                bytes.starts_with(&self.prefix)
            }
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
    /// The kept answer matching `bytes` at `at` (generation, key, index), from either generation: one kept for that RIP
    /// itself in any bucket first, then one for another RIP. Keys hold the leading bytes the answer read (at most
    /// [`LEAD`]), so each length is tried; one pass, each bucket looked up once. (`at`: tagged for 32-bit code; a bucket's
    /// entries have its key's tag.)
    fn find(
        &self,
        bytes: &[u8],
        at: u64,
        limit: Option<u32>,
        gating: &[u64; GATING],
    ) -> Option<(bool, Key, usize)> {
        let mut other = None;
        let rip = untagged(at);
        for n in (0..=LEAD.min(bytes.len())).rev() {
            let key = key(limit, at, &bytes[..n]);
            for newer in [true, false] {
                let Some(v) = (if newer { &self.cache } else { &self.old }).get(&key) else {
                    continue;
                };
                let mut matching = v
                    .iter()
                    .enumerate()
                    .filter(|(_, e)| e.matches(bytes, gating));
                let Some((first, _)) = matching.next() else {
                    continue;
                };
                let own = if v[first].rip == rip {
                    Some(first)
                } else {
                    matching.find(|(_, e)| e.rip == rip).map(|(i, _)| i)
                };
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
    fn take(
        &mut self,
        (newer, key, i): (bool, Key, usize),
        limit: Option<u32>,
    ) -> (Arc<Entry>, Option<Cache>) {
        if newer {
            return (self.cache[&key][i].clone(), None);
        }
        let entry = self.old.get_mut(&key).unwrap().swap_remove(i);
        let dropped = self.insert(limit, entry.clone());
        (entry, dropped)
    }
    /// Keeps `entry` in the newer generation; when that holds a [`SHARDS`]th of [`GENERATION`] answers or of
    /// [`GENERATION_BYTES`] it becomes the older one (the older is dropped): every answer used or made in the shard's last
    /// generation is kept, at most twice the bound (2 x 512 MiB over the shards). Returns the dropped generation, for the
    /// caller to free after unlocking the shard (up to 16 k answers).
    fn insert(&mut self, limit: Option<u32>, entry: Arc<Entry>) -> Option<Cache> {
        let mut dropped = None;
        if self.cached >= GENERATION / SHARDS || self.cached_bytes >= GENERATION_BYTES / SHARDS {
            dropped = Some(std::mem::replace(
                &mut self.old,
                std::mem::take(&mut self.cache),
            ));
            (self.cached, self.cached_bytes) = (0, 0);
        }
        let key = key(
            limit,
            entry.rip | entry.tag,
            &entry.prefix[..LEAD.min(entry.prefix.len())],
        );
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
    /// PIC study: the fingerprints ([`Entry::fingerprint`]) of the answers dropped with a generation, with their owners, until
    /// compiled again ([`Answers::recompiled`]).
    dropped: Mutex<HashMap<u64, u8>>,
}
impl Answers {
    pub(crate) fn new(supported: u64) -> Arc<Self> {
        Arc::new(Self {
            supported,
            pick: Universal::default(),
            shards: std::array::from_fn(|_| Mutex::default()),
            waits: Default::default(),
            dropped: Mutex::default(),
        })
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
    /// The kept answer matching `bytes` at `rip` ([`Shard::find`]; an older one moves to the newer generation). `rip`, here
    /// and in [`Answers::insert`]: as [`Compiler::at`] gives it (32-bit code's tagged).
    fn find(
        &self,
        bytes: &[u8],
        rip: u64,
        limit: Option<u32>,
        gating: &[u64; GATING],
    ) -> (Option<Arc<Entry>>, u64) {
        let (mut shard, waited) = self.lock(limit, rip);
        let Some(f) = shard.find(bytes, rip, limit, gating) else {
            return (None, waited);
        };
        let (entry, dropped) = shard.take(f, limit);
        drop(shard);
        self.forget(dropped); // freed unlocked
        (Some(entry), waited)
    }
    /// Keeps `entry`, made for `bytes` at `rip`; returns the owner of a matching answer the map held already (perf chunk 13:
    /// after a lookup that found none, one made meanwhile by another vCPU) and the nanoseconds waited.
    fn insert(
        &self,
        limit: Option<u32>,
        entry: Arc<Entry>,
        bytes: &[u8],
        rip: u64,
    ) -> (Option<u8>, u64) {
        let (mut shard, waited) = self.lock(limit, rip);
        let held = shard
            .find(bytes, rip, limit, &entry.gating)
            .map(|f| shard.at(f).owner);
        let dropped = shard.insert(limit, entry);
        drop(shard);
        self.forget(dropped); // freed unlocked
        (held, waited)
    }
    /// Notes the fingerprints of a dropped generation's answers, then frees them.
    fn forget(&self, dropped: Option<Cache>) {
        let Some(d) = dropped else { return };
        let mut set = self.dropped.lock().unwrap_or_else(|e| e.into_inner());
        for (k, v) in &d {
            let limit = (k.0 != 1 << 32).then_some(k.0 as u32);
            for e in v {
                set.insert(e.fingerprint(limit), e.owner);
            }
        }
    }
    /// PIC study: `entry`, just compiled, is an answer the map dropped: the owner of the dropped one.
    fn recompiled(&self, limit: Option<u32>, entry: &Entry) -> Option<u8> {
        let mut set = self.dropped.lock().unwrap_or_else(|e| e.into_inner());
        if set.is_empty() {
            return None;
        }
        set.remove(&entry.fingerprint(limit))
    }
    /// Bytes and entries kept.
    fn kept(&self) -> (usize, usize) {
        self.shards.iter().fold((0, 0), |(b, n), s| {
            let s = s.lock().unwrap_or_else(|e| e.into_inner());
            let all = || s.cache.values().chain(s.old.values()).flatten();
            (
                b + all().map(|e| e.size()).sum::<usize>(),
                n + all().count(),
            )
        })
    }
    /// The performance log's line for the run: each shard's lock waits, `[ns, count]`, and the bytes and answers kept.
    pub fn report(&self) -> String {
        #[derive(serde::Serialize)]
        struct Report {
            lock_waits: Vec<[u64; 2]>,
            kept_bytes: usize,
            kept_answers: usize,
        }
        let (kept_bytes, kept_answers) = self.kept();
        let report = Report {
            lock_waits: self
                .waits
                .iter()
                .map(|[ns, n]| [ns.load(Relaxed), n.load(Relaxed)])
                .collect(),
            kept_bytes,
            kept_answers,
        };
        format!("answers {}", serde_json::to_string(&report).unwrap())
    }
}
/// What [`Compiler::compile`] gives the service loop.
pub(crate) enum Code {
    Block(admission::Admitted),
    System(exec::System),
    None(String),
}

/// The compiler options every request carries: extended blocks of up to 4 conditional branches (here), one stack check per
/// run of stack stores (`--push-run`, [`Compiler::push_run`]: left out for a vCPU whose stack pointer keeps tripping it,
/// `Cpu::stack_traps`) and narrow compares. They are request arguments, so part of every request body and so of the
/// persistent cache's key (docs/restructure/translated-code-layout.md).
const TRANSLATION_ARGS: &[u8] = b"--ext-conds\x004\0";

/// fexc for one vCPU's feature set: one persistent `fexc --serve` process (started at the first compile), a cache of its
/// answers in memory and, when set, the persistent one on disk. Objects are byte-identical to the one-shot CLI's.
/// In memory an answer is kept by block limit, the RIP's half and page offset and its leading bytes (the profile and
/// features are the compiler's), with its RIP and what fexc says the compile read: the guest bytes up to its extent and the
/// gating arguments it consulted. A request equal in those gets it (fexc is deterministic and would read the same), so a
/// block is not compiled again because later bytes on its page or an unrelated control bit changed; at another RIP the
/// answer is relocated ([`crate::relocation`], perf chunk 4), or compiled when it cannot be. On disk, answers are kept by
/// the request with its RIP as half and page offset, with that RIP, and the fexc build, for CPL-3 requests only
/// (`compile_cache`).
/// Every object, from fexc or a cache, goes through admission's mandatory validation when it is used: a damaged or forged
/// cache entry can at worst be refused (a stop) or give the guest the effects of other x86 code inside its own containment,
/// as a faulty fexc could; it cannot give code a way out of it.
pub struct Compiler {
    fexc: PathBuf,
    server: Option<Server>,
    supported: u64,
    /// The request body's `--feature NAME=0` arguments for `supported`, NUL-terminated (formatted once: `body_with`).
    feature_args: Vec<u8>,
    /// Block size limit in guest instructions (fexc's own limit when `None`).
    max_instructions: Option<u32>,
    /// Kept answers: its own, or the run's ([`Compiler::share`]).
    answers: Arc<Answers>,
    /// This vCPU, the owner of the answers it keeps (perf chunk 13).
    pub vcpu: u8,
    /// The persistent cache (`boot-linux` sets it; test drivers never do), used only by requests with `cpl3` set.
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
    /// PIC study: compiles of an answer the memory cache had dropped, and of them of this vCPU's own.
    pub recompiled: [u64; 2],
    /// Answers kept for another RIP and used at this one (perf chunk 4: [`crate::relocation`]); answers that could not be
    /// (compiled instead); relocated objects admission refused (compiled instead, logged; none expected); fresh compiles
    /// made by the reuse check.
    pub relocated: u64,
    pub relocation_refused: u64,
    pub relocation_fallbacks: u64,
    pub reuse_checks: u64,
    /// Every object answer [`Compiler::answered`] handled, by [`why`] (their sum is the total; all but `SKIPPED` ran
    /// admission, or were refused before it as a retry).
    pub admissions: [std::cell::Cell<u64>; why::NAMES.len()],
    /// Debug check (the program driver's `--code-shape` or `--check-reuse`, `boot-linux --check-reuse`): every answer used at
    /// another RIP than its own is also compiled there, and the two must be equal (else a host error).
    pub check_reuse: bool,
    /// Page-currency plan chunk 1: the resident's [`cache::Control::pages`] ([`cache::PAGES_ON`] by default; `--page-currency
    /// on|off|check|check-halt`, [`parse_page_currency`]).
    pub page_currency: u64,
    /// Fault-around plan chunk 1: [`cache::Control::writable_on_read_off`] (a demand read installs the shadow writable when the leaf has D
    /// set and grants the write; on by default; `--writable-on-read on|off`, [`parse_writable_on_read`]; `NATEMU_WRITABLE_ON_READ` overrides it).
    pub writable_on_read: bool,
    /// PIC study stage 2: the resident's [`cache::Control::pic`] ([`cache::PIC_USER`] by default; the program driver's
    /// [`cache::PIC_ALL`]); `NATEMU_PIC=off|user|user64|all` overrides it (`user64`: 32-bit code not shared, copied).
    pub pic: u64,
    /// PIC study: [`cache::Control::pic_check_local`] (the program driver's code-shape runs: local links under the check).
    pub pic_check_local: bool,
    /// How long one request may wait for the server's whole reply (the first one includes the server's start); then the
    /// server is killed and the run fails with a host error ([`COMPILE_DEADLINE`] by default).
    pub deadline: Duration,
    /// The longest request so far (reply received), for the deadline's margin.
    pub slowest: Duration,
    /// Admission's code-shape debug check ([`admission`]); off unless the caller sets it (the program driver's
    /// `--code-shape`).
    pub shape: bool,
    /// M2 task D: a Linux boot's diagnostics ([`diag`]): each stop but a power-off, reset or cancel (and a host error) prints the
    /// vCPU's report on standard error, and a vCPU in the host for the watchdog's time outside a translation (whose wait has its
    /// own deadline) is reported once per stay while the run goes on. Off unless the caller sets it (`linux::Prepared::run` does).
    pub report: bool,
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
    /// `--x87-gated`, `--wait-gated`, `--x87-fpcr`, `--simd-unmasked`, `--x87-unmasked`, and (without AFP) `--daz` and `--ftz`
    /// arguments, and (AFP accurate plan chunk 1) `--accurate`.
    pub(crate) x87: Vec<String>,
    /// AFP accurate plan chunk 1: the process runs in accurate floating-point mode (`--fp accurate`). Fixed for the process; the
    /// vCPU's [`exec::Cpu::accurate_fp`] is set from it in [`run`], and only a host without AFP then changes code (key bit 32).
    pub accurate_fp: bool,
    /// Perf chunk 15: a translate request at CPL 0 (never a step): fexc's `--cpl0`, under which it translates the one-byte
    /// CLI and STI inline (the CPL is part of `Cpu::code_context`, so such a block runs only at CPL 0).
    pub(crate) cpl0: bool,
    /// Perf chunk 15: a CPL-0 translate request on a host whose counter runs at 24 MHz ([`crate::clock::frequency`], fexc's
    /// `--tsc`): RDTSC is translated inline with the resident's formula for that rate.
    pub(crate) tsc: bool,
    /// Perf chunk 27: a request at CPL 3, translate or step: only these look up and store answers in the persistent cache
    /// (`compile_cache`); the cache in memory serves every CPL.
    pub(crate) cpl3: bool,
    /// Perf chunk 26: a translate request (never a step) without alignment checking or a data breakpoint (`alignment_gated`
    /// off, which is part of `Cpu::code_context`): fexc's `--bulk`, under which REP STOS stores the part of its string inside
    /// RDI's page in bulk (each store a site at an iteration boundary). A step runs one element: TF traps after each
    /// iteration, and the watchpoint, UC and code-write paths step the element that met them.
    pub(crate) bulk: bool,
    /// fexc's `--push-run` (the default; false for a vCPU past [`exec::STACK_TRAP_LIMIT`] stack-check firings: part of the
    /// code context, bit 33, and the last gating argument).
    pub(crate) push_run: bool,
    /// M2 task 4: fexc's `--x87-guess` for a request for 32-bit code ([`exec::State::x87_guess`]; 0: none).
    pub(crate) x87_guess: u64,
    /// M2: a request for 32-bit code (the code segment's mode): fexc's `--bitness 32` and admission's 32-bit rules
    /// ([`admission::admit_mode`]). Such a request is always compiled: no kept answer (so none relocated to another RIP), no
    /// persistent cache, no template for the pool or the resident. (`--push-run` is sent as for 64-bit code since M2 task 4: fexc
    /// and admission take the stack slot as 4 bytes there.)
    /// The answers kept in memory are keyed without the mode, which is why 32-bit requests stay out of them.
    pub(crate) bits32: bool,
    /// M2 task 3a, 32-bit requests only: FS, GS, DS, ES hold a null selector ([`exec::NULL_SREGS`], code identity bits
    /// 5 to 8): fexc's `--fs-null` .. `--es-null` cut every access through that segment, which the resident raises as
    /// #GP(0) ([`exec::system::NULL_SEGMENT`]). Not gating values of the kept answers: 32-bit requests keep none.
    pub(crate) null_segments: [bool; 4],
    /// M2 checkpoint C, 32-bit user code: FS, GS is a limited segment ([`exec::Cpu::limited_segment`]: fexc's `--fs-limited`,
    /// `--gs-limited`, under which every instruction's operand through it is checked; admission holds the code to it). Part
    /// of a 32-bit answer's tagged RIP ([`tag32`]), as the null segments.
    pub(crate) limited_segments: [bool; 2],
    /// The performance log's record of this vCPU (`perf::on`; [`Compiler::finish`]).
    pub perf: Option<Box<crate::perf::Recorder>>,
    /// The last compile's answer: how many guest bytes it read (fexc's extent, at most the bytes given), and whether a
    /// halving retry made it ([`Compiler::compile`]); the template rule of reuse inside the resident (perf chunk 10).
    read: usize,
    /// M2 reuse: the last request's answer read the x87 guess (32-bit code with x87 instructions): it is no template
    /// ([`translation`]), since the pool and the resident's templates are found without the guess.
    guess_read: bool,
    halved: bool,
    /// The template pool's writer (the run's; [`Backing::pool`]) and, with it set, the answer the last request used and the
    /// (context, gating values) pairs this vCPU already told it about ([`Compiler::note_gating`]).
    pub(crate) pool: Option<Arc<crate::publisher::Publisher>>,
    last: Option<Arc<Entry>>,
    noted: Vec<(u64, [u64; GATING])>,
}
/// The afp row's bit in `supported` (the features fexc is told): [`Cpu::no_afp`] is its complement.
const AFP: usize = admission::index("afp");
/// fexc's x87 arguments for `cpu` (I6; SDM Vol.3 Table 2-2): every x87 form #NM under CR0.EM or CR0.TS, WAIT/FWAIT under CR0.TS
/// with CR0.MP; x87 arithmetic under its own FPCR when FCW.RC or MXCSR's flush-to-zero/DAZ differ ([`exec::x87_fpcr`]); and
/// (I5c) the MXCSR and FCW exceptions whose masks are clear, each instruction then checked for them (none as Linux runs). The
/// same state is part of `Cpu::code_context`. AFP fallback chunk 1: the FPCR words follow `cpu.no_afp`, which must agree with
/// `supported`'s afp bit (the value fexc is told: [`run`] sets the one from the other, so a disagreement is a host bug that
/// would key shared code wrongly).
pub(crate) fn x87_args(cpu: &exec::Cpu, supported: u64) -> Vec<String> {
    assert_eq!(
        cpu.no_afp != 0,
        supported & 1 << AFP == 0,
        "Cpu::no_afp disagrees with the features fexc is told"
    );
    let mut args = vec![];
    if cpu.cr0 & (exec::CR0_EM | exec::CR0_TS) != 0 {
        args.push("--x87-gated".into());
    }
    if cpu.cr0 & (exec::CR0_TS | exec::CR0_MP) == exec::CR0_TS | exec::CR0_MP {
        args.push("--wait-gated".into());
    }
    match exec::x87_fpcr(cpu.fcw, cpu.mxcsr, cpu.no_afp != 0, cpu.accurate_fp) {
        0 => {}
        fpcr => args.extend(["--x87-fpcr".into(), format!("{fpcr:#x}")]),
    }
    for (name, unmasked) in [
        ("--simd-unmasked", exec::simd_unmasked(cpu.mxcsr)),
        ("--x87-unmasked", exec::x87_unmasked(cpu.fcw)),
    ] {
        if unmasked != 0 {
            args.extend([name.into(), format!("{unmasked:#x}")]);
        }
    }
    // AFP fallback chunk 3a (rows 4-6): without AFP only, DAZ and effective FTZ, which `code_context` keys too.
    for (name, on) in [
        ("--daz", exec::daz_key(cpu.mxcsr, cpu.no_afp != 0)),
        ("--ftz", exec::ftz_key(cpu.mxcsr, cpu.no_afp != 0)),
    ] {
        if on != 0 {
            args.push(name.into());
        }
    }
    // AFP accurate plan chunk 1: accurate mode without AFP only (`code_context` bit 32, the same condition), so accurate mode on an
    // AFP host changes no request.
    if exec::accurate_key(cpu.accurate_fp, cpu.no_afp) != 0 {
        args.push("--accurate".into());
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
/// CPU-features plan chunk 0 (replay gate): `NATEMU_FEXC_RECORD=<path>` appends every request the host sends to fexc, as
/// u32 length + body (the bytes [`Channel::post`] gets), the format `channel_replay_matches_the_pipe` reads. One file for
/// all vCPUs: the file's mutex orders whole records, so a stream is never torn; the interleaving of vCPUs is
/// arbitrary, and the replay does not depend on order (a server's reply is a function of the request alone). The
/// variable is read once; unset, a request costs one atomic load. A file that cannot be opened or written stops the run (a
/// silent miss would waste the boot that was meant to record).
fn record_request(body: &[u8]) {
    static OUT: std::sync::OnceLock<Option<Mutex<std::fs::File>>> = std::sync::OnceLock::new();
    let Some(out) = OUT.get_or_init(|| {
        let path = std::env::var_os("NATEMU_FEXC_RECORD")?;
        let file = std::fs::OpenOptions::new().create(true).append(true).open(&path);
        Some(Mutex::new(file.unwrap_or_else(|e| {
            panic!("NATEMU_FEXC_RECORD {}: {e}", path.to_string_lossy())
        })))
    }) else {
        return;
    };
    write_record(&mut *out.lock().unwrap_or_else(|e| e.into_inner()), body)
        .expect("NATEMU_FEXC_RECORD: write");
}
/// AFP fallback chunk 1 (the replay's body-rewrite hook): `body` (u32 LE length of the NUL-terminated arguments, the arguments,
/// then the guest bytes) with `add` appended to its arguments, e.g. `--feature afp=0` to replay an AFP host's recorded
/// stream as a host without AFP asks for it. Only the replay test uses it.
#[cfg(test)]
fn with_added_args(body: &[u8], add: &[&str]) -> Vec<u8> {
    let n = u32::from_le_bytes(body[..4].try_into().unwrap()) as usize;
    let extra: Vec<u8> = add.iter().flat_map(|a| a.bytes().chain([0])).collect();
    let mut out = ((n + extra.len()) as u32).to_le_bytes().to_vec();
    out.extend_from_slice(&body[4..4 + n]);
    out.extend(extra);
    out.extend_from_slice(&body[4 + n..]);
    out
}
/// One record in a single write (u32 LE length, then `body`).
fn write_record(out: &mut impl std::io::Write, body: &[u8]) -> std::io::Result<()> {
    let mut record = Vec::with_capacity(4 + body.len());
    record.extend((body.len() as u32).to_le_bytes());
    record.extend_from_slice(body);
    out.write_all(&record)
}

impl Compiler {
    /// Now, when the performance log records this vCPU (else 0, unread).
    fn stamp(&self) -> u64 {
        if self.perf.is_some() {
            crate::perf::now()
        } else {
            0
        }
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
    /// The compiler of the vCPU `cpu`, after the resident's setup (`enter`, which ran the startup probe: [`crate::probe`]):
    /// takes the supported features from the vCPU's registers.
    /// `NATEMU_FEXC_FEATURE_OFF=name,...` (test only: the fallback gate, plan section 2.2a) clears the named ones.
    pub fn new(fexc: &Path, cpu: &Vcpu, max_instructions: Option<u32>) -> Result<Self> {
        // `NATEMU_FEXC_FEATURE_OFF` is applied inside the probe, so its line and this set agree ([`crate::probe::Probe`]).
        let supported = crate::probe::Probe::read(cpu)?.supported();
        Ok(Self::with_features(fexc, supported, max_instructions))
    }
    fn with_features(fexc: &Path, supported: u64, max_instructions: Option<u32>) -> Self {
        Self {
            fexc: fexc.into(),
            server: None,
            supported,
            feature_args: admission::feature_overrides(supported)
                .iter()
                .flat_map(|f| b"--feature\0".iter().copied().chain(f.bytes()).chain([0]))
                .chain(TRANSLATION_ARGS.iter().copied())
                .collect(),
            max_instructions,
            answers: Answers::new(supported),
            vcpu: 0,
            disk: None,
            compiles: 0,
            hits: 0,
            disk_hits: 0,
            hits_by_source: [0; cache::MAX_VCPUS],
            duplicates: 0,
            recompiled: [0; 2],
            lock_wait: (0, 0),
            relocated: 0,
            relocation_refused: 0,
            relocation_fallbacks: 0,
            admissions: Default::default(),
            reuse_checks: 0,
            check_reuse: false,
            page_currency: cache::PAGES_ON,
            writable_on_read: true,
            pic: cache::PIC_USER,
            pic_check_local: false,
            deadline: COMPILE_DEADLINE,
            slowest: Duration::ZERO,
            shape: false,
            report: false,
            sse_gated: false,
            avx_gated: false,
            alignment_gated: false,
            x87: vec![],
            accurate_fp: false,
            cpl0: false,
            tsc: false,
            cpl3: false,
            bulk: false,
            push_run: true,
            x87_guess: 0,
            bits32: false,
            null_segments: [false; 4],
            limited_segments: [false; 2],
            perf: None,
            read: 0,
            guess_read: false,
            halved: false,
            pool: None,
            last: None,
            noted: Vec::new(),
        }
    }
    /// The admitted block for `bytes` at `rip` (only its first instruction if `step`); else the system instruction at
    /// `rip` the resident executes, or fexc's end reason when it produced no code.
    fn compile(
        &mut self,
        bytes: &[u8],
        rip: u64,
        step: bool,
        hook: &mut impl Hook,
    ) -> Result<Code> {
        // I9 review: a block whose code is over `MAX_BLOCK_BYTES` (PDEP/PEXT, PCMPxSTRx and other long lowerings in numbers)
        // is translated again from the same RIP with half its instructions, until it fits; one instruction too large for a
        // block stops `code_too_large`. R2 review: the same for a block with a spill area (`spill_area`).
        let mut limit = if step { Some(1) } else { self.max_instructions };
        self.halved = false;
        self.last = None;
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
                Err((_, reason)) => return Ok(Self::refused(reason, bytes, rip)),
            }
        }
    }
    /// The gating arguments' values, in fexc's `FEXC_ARG_*` order: SSE, AVX, alignment, x87, WAIT, x87 FPCR, (I5c) the
    /// unmasked SIMD and x87 exceptions, (perf chunk 15) CPL 0 and the inline TSC, (perf chunk 26) bulk REP STOS, and
    /// (AFP fallback chunk 3a) DAZ and effective FTZ, (AFP accurate plan chunk 1) accurate mode, and `--push-run`. With the
    /// block limit, the RIP's half and page offset, the bytes read (the key), the features (one map per feature set) and
    /// 32-bit code's tag ([`Compiler::at`]) that is every part of a request ([`Compiler::body_with`]).
    fn gating(&self) -> [u64; GATING] {
        let flag = |a: &str| u64::from(self.x87.iter().any(|x| x == a));
        let value = |a: &str| {
            let v = self
                .x87
                .iter()
                .position(|x| x == a)
                .and_then(|i| self.x87.get(i + 1));
            v.and_then(|v| u64::from_str_radix(v.trim_start_matches("0x"), 16).ok())
                .unwrap_or(0)
        };
        [
            self.sse_gated.into(),
            self.avx_gated.into(),
            self.alignment_gated.into(),
            flag("--x87-gated"),
            flag("--wait-gated"),
            value("--x87-fpcr"),
            value("--simd-unmasked"),
            value("--x87-unmasked"),
            self.cpl0.into(),
            self.tsc.into(),
            self.bulk.into(),
            flag("--daz"),
            flag("--ftz"),
            flag("--accurate"),
            self.push_run.into(),
        ]
    }
    /// `rip` as the answers table holds it: 32-bit code's with the request's null segments, as [`Compiler::body_with`]
    /// sends them, and `guess` ([`tag32`]): the request's x87 guess for an answer that read it, [`GUESS_FREE`] for one that
    /// did not.
    fn at(&self, rip: u64, guess: u64) -> Result<u64> {
        if !self.bits32 {
            // (No such RIP is compiled: fexc refuses a non-canonical one.)
            return if tagged(rip) { Err(format!("RIP {rip:#x} is not canonical").into()) } else { Ok(rip) };
        }
        if rip >> 32 != 0 || self.x87_guess & GUESS_FREE != 0 {
            return Err(format!("32-bit code at {rip:#x}, x87 guess {:#x}", self.x87_guess).into());
        }
        Ok(rip | tag32(self.null_segments, self.limited_segments, guess)?)
    }
    /// fexc's `args_read` as a kept answer's `read`: its gating arguments and, for 32-bit code, the x87 guess's bit, which
    /// says under which tag the answer is kept ([`Compiler::at`]). Any other argument is an error, and the guess for 64-bit
    /// code, which is sent none: an answer that read it would be served to a request with another value of it (M2 task 4's
    /// review: the guess, then outside the key).
    fn keyed_read(&self, args_read: u32) -> Result<u32> {
        if args_read >> (GATING + usize::from(self.bits32)) != 0 {
            return Err(format!(
                "fexc read an argument kept answers are not matched by ({args_read:#x}, {}-bit code)",
                if self.bits32 { 32 } else { 64 }
            )
            .into());
        }
        Ok(args_read)
    }
    /// [`Compiler::compile`] with an instruction limit; `Err((n, why))`: the object must be translated again with fewer
    /// than its n guest instructions ([`admission::retry`]).
    fn compile_once(
        &mut self,
        bytes: &[u8],
        rip: u64,
        limit: Option<u32>,
        hook: &mut impl Hook,
    ) -> Result<std::result::Result<Code, (u32, &'static str)>> {
        let gating = self.gating();
        let t = self.stamp();
        // 32-bit code: an answer that read no guess first (most: code without x87 instructions), then one for this guess.
        let at = [self.at(rip, GUESS_FREE)?, self.at(rip, self.x87_guess)?];
        let (mut found, waited) = self.answers.find(bytes, at[0], limit, &gating);
        self.waited(waited);
        if found.is_none() && self.bits32 {
            let waited;
            (found, waited) = self.answers.find(bytes, at[1], limit, &gating);
            self.waited(waited);
        }
        self.charge(crate::perf::LOOKUP, t);
        if let Some(e) = &found {
            if let Some(code) = self.reuse(e, bytes, rip, limit, hook, None)? {
                if self.pool.is_some() {
                    self.last = Some(e.clone());
                }
                self.hits += 1;
                self.hits_by_source[usize::from(e.owner)] += 1;
                self.read = e.prefix.len();
                self.guess_read = e.read >> GATING != 0;
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
            let (compiles, disk_hits) = (self.compiles, self.disk_hits);
            let (reply, from) = self.ask(bytes, rip, limit, disk, hook)?;
            // (Only a damaged persistent entry: the tag takes the upper half of a 32-bit answer's RIP.)
            if self.bits32 && from >> 32 != 0 {
                return Err(format!("an answer for 32-bit code at {rip:#x} was compiled at {from:#x}").into());
            }
            let disk_hit = self.disk_hits > disk_hits;
            let t = self.stamp();
            let (whole, end) = (
                reply.extent as usize >= bytes.len(),
                bytes.len().min(reply.extent as usize),
            );
            let (read, prefix) = (self.keyed_read(reply.read)?, bytes[..end].to_vec());
            let at = at[usize::from(read >> GATING != 0)];
            let entry = Arc::new(Entry {
                rip: from,
                tag: at ^ rip,
                owner: self.vcpu,
                read,
                gating,
                prefix,
                whole,
                answer: Answer::new(reply, from)?,
                kept: Default::default(),
                published: AtomicU64::new(0),
            });
            let (held, waited) = self.answers.insert(limit, entry.clone(), bytes, at);
            self.waited(waited);
            // Perf chunk 13: the lookup found nothing and another vCPU kept a matching answer while this one compiled.
            if disk && self.compiles > compiles && held.is_some_and(|o| o != self.vcpu) {
                self.duplicates += 1;
            }
            // PIC study: fexc compiled again an answer the map had dropped (by this vCPU's own: `.1`).
            let mut recompiled = false;
            if self.compiles > compiles {
                if let Some(owner) = self.answers.recompiled(limit, &entry) {
                    self.recompiled[0] += 1;
                    self.recompiled[1] += u64::from(owner == self.vcpu);
                    recompiled = true;
                }
            }
            self.read = end;
            self.guess_read = read >> GATING != 0;
            self.charge(crate::perf::KEEP, t);
            if let Some(r) = &mut self.perf {
                r.read = (hash(&entry.prefix), entry.prefix.len() as u32);
            }
            let origin = if disk_hit {
                why::DISK
            } else if recompiled {
                why::RECOMPILED
            } else {
                why::FRESH
            };
            match self.reuse(&entry, bytes, rip, limit, hook, Some(origin))? {
                Some(code) => {
                    if self.pool.is_some() {
                        self.last = Some(entry);
                    }
                    return Ok(code);
                }
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
            eprintln!(
                "natemu: vCPU {} presents other features than the run's first compiler: it keeps its own answers",
                self.vcpu
            );
            // The pool is keyed without features: with a vCPU that differs it is off for the run.
            if let Some(pool) = self.pool.take() {
                pool.disable(crate::publisher::off::FEATURES);
            }
        }
    }
    /// The template pool: the answer the last request used, published as the admitted `block` at `rip` under `context` when
    /// the host found it a `template` (once per answer, [`crate::publisher::Publisher::publish`]; `rep_exits`: the reply's).
    fn publish(
        &mut self,
        template: bool,
        context: u64,
        rip: u64,
        block: &admission::Admitted,
        rep_exits: u64,
    ) -> Option<crate::publisher::Published> {
        let (pool, entry) = (self.pool.as_ref()?, self.last.take()?);
        template.then(|| {
            pool.publish(
                &entry.published,
                &crate::publisher::Template {
                    rip,
                    context,
                    prefix: &entry.prefix,
                    block,
                    rep_exits,
                },
            )
        })
    }
    /// Publishes into (and, with `Control::pool`, reads) `backing`'s template pool, if it has one; call before [`Compiler::share`]
    /// (a compiler whose features differ then switches the pool off).
    pub fn attach_pool(&mut self, backing: &Backing) {
        self.pool = backing.pool.clone();
    }
    /// The template pool: a non-step block request under `context` with this compiler's gating values (a vCPU tells the
    /// pool each pair once). A context whose requests differ in them turns the pool off ([`crate::publisher::Publisher::note`]);
    /// under the check mode that is a host error.
    fn note_gating(&mut self, context: u64) -> Result<()> {
        let Some(pool) = &self.pool else { return Ok(()) };
        let gating = self.gating();
        if self.noted.contains(&(context, gating)) {
            return Ok(());
        }
        if !pool.note(context, &gating) && self.check_reuse {
            return Err(format!(
                "pool: gating values {gating:?} under context {context:#x} differ from an earlier request's"
            ));
        }
        if self.noted.len() == 8 {
            self.noted.remove(0);
        }
        self.noted.push((context, gating));
        Ok(())
    }
    /// The kept answer `e`, used at `rip`: as it is for its own RIP; for another, relocated (`None`: it would not be fexc's
    /// at `rip`, or admission refused the relocated object; logged; the caller compiles). `origin`: a [`why`] when this is
    /// the answer's first use, by the request that made it.
    fn reuse(
        &mut self,
        e: &Entry,
        bytes: &[u8],
        rip: u64,
        limit: Option<u32>,
        hook: &mut impl Hook,
        origin: Option<usize>,
    ) -> Result<Option<std::result::Result<Code, (u32, &'static str)>>> {
        let t = self.stamp();
        let (from, read) = (e.rip, (e.read, e.whole, e.prefix.len()));
        let code = if from == rip {
            // The reuse check, of the key: a kept answer of 32-bit code found again at its own RIP (not its first use) is
            // fexc's answer to this request too (M2 reuse: the mode, null segments and x87 guess it is matched by).
            if self.check_reuse && self.bits32 && origin.is_none() {
                self.check(&e.answer, read, bytes, rip, limit, from, hook)?;
            }
            self.answered(&e.answer, bytes, rip, hook, Some(e), origin)?
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
            match self.answered(&answer, bytes, rip, hook, Some(e), origin) {
                Ok(code) => {
                    self.relocated += 1;
                    self.charge(
                        if matches!(code, Ok(Code::Block(_))) {
                            crate::perf::ADMIT
                        } else {
                            crate::perf::DECODE
                        },
                        t,
                    );
                    return Ok(Some(code));
                }
                // The reuse check: a refusal or a kept form that is not admission's is an error, not a fallback.
                Err(e) if self.check_reuse => return Err(e),
                Err(e) => {
                    self.relocation_fallbacks += 1;
                    eprintln!(
                        "natemu: the answer relocated from {from:#x} to {rip:#x} was refused ({e}); compiled at {rip:#x}"
                    );
                    return Ok(None);
                }
            }
        };
        self.charge(
            if matches!(code, Ok(Code::Block(_))) {
                crate::perf::ADMIT
            } else {
                crate::perf::DECODE
            },
            t,
        );
        Ok(Some(code))
    }
    /// The reuse check: fexc's own answer at `rip` (no cache) equals `answer`, relocated from `from`.
    /// `read`: the kept entry's arguments read, whether its extent reached the end of its bytes, and their length to it.
    #[allow(clippy::too_many_arguments)]
    fn check(
        &mut self,
        answer: &Answer,
        read: (u32, bool, usize),
        bytes: &[u8],
        rip: u64,
        limit: Option<u32>,
        from: u64,
        hook: &mut impl Hook,
    ) -> Result<()> {
        let (reply, _) = self.ask(bytes, rip, limit, false, hook)?;
        // Counted as checks, not as the run's compiles.
        self.compiles -= 1;
        if let Some(r) = &mut self.perf {
            r.sources[2] -= 1;
        }
        self.reuse_checks += 1;
        let fresh = (
            self.keyed_read(reply.read)?,
            reply.extent as usize >= bytes.len(),
            bytes.len().min(reply.extent as usize),
        );
        // (Bytes cut exactly at the answer's extent, by a breakpoint or a hook's limit, make fexc's extent reach their end: `whole`
        // then differs from the kept answer's, which only matters when more bytes follow; the answer itself is compared.)
        let same_extent = fresh.0 == read.0 && fresh.2 == read.2 && (fresh.1 == read.1 || bytes.len() == read.2);
        if !same_extent || Answer::new(reply, rip)? != *answer {
            return Err(format!(
                "reuse check: the answer kept for {from:#x} is not fexc's at {rip:#x}"
            ));
        }
        Ok(())
    }
    /// Option (d): the block `e`'s kept form gives for `object` (its answer, moved to `rip`), without admission: only where
    /// the request's CPL 0, bulk and code-shape values are those admission ran under, the answer's bytes cover the block's
    /// ([`crate::relocation::kept`] for the rest). `Err`: admit it, for that [`why`].
    fn skip(&self, e: &Entry, object: &[u8], rip: u64) -> std::result::Result<admission::Admitted, usize> {
        let (Some(k), Answer::Object(from)) = (e.kept.get(), &e.answer) else {
            return Err(why::NO_KEPT);
        };
        if k.cpl0 != self.cpl0 {
            return Err(why::CPL0);
        }
        if k.bulk != self.bulk {
            return Err(why::BULK);
        }
        if k.shape != self.shape {
            return Err(why::SHAPE);
        }
        if e.prefix.len() < k.form.guest_bytes as usize {
            return Err(why::SHORT_PREFIX);
        }
        crate::relocation::kept(&k.form, from, e.rip, object, rip).ok_or(why::MOVE_REFUSED)
    }
    /// [`Compiler::compile_once`]'s use of an answer (`kept`: the entry it is, moved to `rip` or not): admitted, or decoded.
    #[cfg_attr(not(feature = "testing"), allow(unused_variables))]
    fn answered(
        &self,
        answer: &Answer,
        bytes: &[u8],
        rip: u64,
        hook: &mut impl Hook,
        kept: Option<&Entry>,
        origin: Option<usize>,
    ) -> Result<std::result::Result<Code, (u32, &'static str)>> {
        Ok(match answer {
            Answer::Object(object) => match admission::retry(object) {
                Some(n) => {
                    self.admissions[why::RETRY].set(self.admissions[why::RETRY].get() + 1);
                    Err(n)
                }
                None => {
                    // The reason this answer is admitted again: its first use's `origin`, else what the kept form lacks.
                    let skipped = kept
                        .map_or(Err(why::NO_KEPT), |e| self.skip(e, object, rip))
                        .map_err(|r| origin.unwrap_or(r));
                    let n = &self.admissions[skipped.as_ref().map_or_else(|&r| r, |_| why::SKIPPED)];
                    n.set(n.get() + 1);
                    let skipped = skipped.ok();
                    #[cfg_attr(not(feature = "testing"), allow(unused_mut))]
                    let mut admitted = match skipped {
                        Some(a) if !self.check_reuse => a,
                        skipped => {
                            let a = admission::admit_user(
                                object,
                                rip,
                                bytes,
                                self.supported,
                                self.shape,
                                self.cpl0,
                                self.bits32,
                                admission::User32 { cpl3: self.cpl3, limited: self.limited_segments },
                            )?;
                            // The reuse check: the kept form is admission's.
                            if skipped.is_some_and(|s| s != a) {
                                return Err(format!("reuse check: the kept admitted form at {rip:#x} is not admission's").into());
                            }
                            // Perf chunk 26: a bulk REP STOS only where asked for (never a step: TF traps after each iteration).
                            if a.bulk && !self.bulk {
                                return Err(format!("object refused: a bulk REP STOS loop in a request without --bulk at {rip:#x}").into());
                            }
                            // Option (d): kept for the next hit, from the admission at the answer's own RIP.
                            if let Some(e) = kept.filter(|e| e.rip == rip) {
                                let form = admission::Admitted { object: Vec::new(), ..a.clone() };
                                let (cpl0, bulk, shape) = (self.cpl0, self.bulk, self.shape);
                                let _ = e.kept.set(Box::new(Kept { cpl0, bulk, shape, form }));
                            }
                            a
                        }
                    };
                    #[cfg(feature = "testing")]
                    hook.admitted(&mut admitted.object);
                    Ok(Code::Block(admitted))
                }
            },
            Answer::Cut {
                reason,
                cut,
                gated,
                ud,
            } => {
                // An SSE form cut by its gating (I5a): no operand matters, the resident raises #UD or #NM.
                // I6: an x87 form or WAIT (primary d8..df, 9b) cut by its gating: #NM. I8: a VEX form (fexc's map 8): #UD or #NM.
                // I9: an MMX form (fexc's `cut_gated` "mmx"): #UD or #NM by CR0.EM/TS alone. M2 task 3a: an access through a
                // null segment in 32-bit code ("null_segment"): #GP(0).
                let gated_kind = |(map, op): (u8, u8), gate: &str| {
                    let x87 = map == 0 && matches!(op, 0xd8..=0xdf | 0x9b);
                    let kind = if gate == "null_segment" {
                        exec::system::NULL_SEGMENT
                    } else if gate == "mmx" {
                        exec::system::MMX_GATED
                    } else if x87 {
                        exec::system::X87_GATED
                    } else if map == 8 {
                        exec::system::AVX_GATED
                    } else {
                        exec::system::SSE_GATED
                    };
                    // A REP string instruction through a null segment makes no access when its count is 0: the resident
                    // then completes it, so it gets the instruction's length (its prefixes and the one opcode byte).
                    let prefixes = bytes
                        .iter()
                        .take_while(|b| matches!(b, 0x26 | 0x2e | 0x36 | 0x3e | 0x64..=0x67 | 0xf0 | 0xf2 | 0xf3))
                        .count();
                    let rep = gate == "null_segment"
                        && map == 0
                        && matches!(op, 0xa4..=0xa7 | 0xaa..=0xaf)
                        && bytes[..prefixes].iter().any(|b| matches!(b, 0xf2 | 0xf3));
                    Some(exec::System {
                        kind,
                        length: if rep { prefixes as u32 + 1 } else { 1 },
                        cr: rep.into(),
                        ..exec::System::NONE
                    })
                };
                // A system cut: the host's decode of it in the request's mode (64-bit code, or 32-bit code: M2 task 3a).
                // FEX and iced reading different instructions there is refused (logged); one that reads
                // past the bytes waits for the next page (its fetch fault comes before any #UD).
                let bitness = if self.bits32 { 32 } else { 64 };
                let decoded = match cut {
                    Some(c) if gated.is_none() && !*ud => {
                        let decoded = crate::system::decode(bytes, bitness);
                        if reason == "system_instruction" {
                            if !crate::system::cut_matches(bytes, bitness, *c) {
                                return Ok(Ok(Self::refused("system_opcode_mismatch", bytes, rip)));
                            }
                            if decoded.is_err() {
                                return Ok(Ok(Self::refused("partial_decode", bytes, rip)));
                            }
                        }
                        decoded.ok().flatten()
                    }
                    _ => None,
                };
                // I10: an encoding fexc found reserved or unadvertised: #UD whatever its operands (RIP stays at it).
                let ud = ud.then_some(exec::System {
                    kind: exec::system::UD,
                    length: 1,
                    ..exec::System::NONE
                });
                match ud.or_else(|| {
                    cut.and_then(|cut| match gated {
                        Some(gate) => gated_kind(cut, gate),
                        None => decoded,
                    })
                }) {
                    Some(s) if reason == "system_instruction" => Ok(Code::System(s)),
                    // Longer than 15 bytes with all of its first 15 fetched: #GP(0) (a fetch fault was `partial_decode`).
                    _ if reason == "instruction_too_long" => Ok(Code::System(exec::System {
                        kind: exec::system::TOO_LONG,
                        length: 1,
                        ..exec::System::NONE
                    })),
                    // X1c: a form fexc refuses at this RIP that x86 defines as an exception: a direct branch to a non-canonical
                    // target (#GP(0) when taken), or an encoding #UD whatever its operands ([`crate::system::undefined`]).
                    // (64-bit code's: a 32-bit target wraps, and the encodings #UD there differ; a refused 32-bit form is logged.)
                    _ if reason == "unsupported_form" && cut.is_some() && !self.bits32 => {
                        match crate::system::branch(bytes, rip)
                            .or_else(|| crate::system::undefined(bytes))
                        {
                            Some(s) => Ok(Code::System(s)),
                            None => Ok(Self::refused(reason, bytes, rip)),
                        }
                    }
                    _ => Ok(Self::refused(reason, bytes, rip)),
                }
            }
        })
    }
    /// A no-code answer at `rip`: the fetch-limited ones (`partial_decode`, `end_of_input`: the caller fetches on or delivers the
    /// fetch fault) stay no code; every other one is an instruction natemu cannot run yet (a refused form, a system instruction
    /// the host does not decode, a decoder or dispatcher failure, a compiler limit at one instruction), which x86 would run or
    /// fault on: it is delivered as #UD at the instruction ([`exec::system::REFUSED`], the recorded deviation of the owner's rule
    /// of November 6: no instruction a guest program can execute stops the machine), never a machine stop. The first ones are
    /// logged on standard error.
    fn refused(reason: &str, bytes: &[u8], rip: u64) -> Code {
        if matches!(reason, "partial_decode" | "end_of_input") {
            return Code::None(reason.into());
        }
        static LOGGED: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);
        // ponytail: the first 32 per process; a per-form count would need a map behind a lock.
        if LOGGED.fetch_add(1, std::sync::atomic::Ordering::Relaxed) < 32 {
            let hex: String = bytes.iter().take(15).map(|b| format!("{b:02x}")).collect();
            eprintln!(
                "natemu: {reason} at {rip:#x} ({hex}): delivered as #UD (a form natemu does not run yet)"
            );
        }
        Code::System(exec::System {
            kind: exec::system::REFUSED,
            length: 1,
            ..exec::System::NONE
        })
    }
    /// The request body for `bytes` at `rip` (protocol in tools/fexc/fexc.cpp).
    fn body(&self, bytes: &[u8], rip: u64, limit: Option<u32>) -> Vec<u8> {
        self.body_with(bytes, format!("{rip:#x}"), limit)
    }
    /// The persistent cache's key body (perf chunk 4): the request with the RIP replaced by its half and page offset, as the
    /// memory cache keys answers.
    fn disk_body(&self, bytes: &[u8], rip: u64, limit: Option<u32>) -> Vec<u8> {
        self.body_with(
            bytes,
            format!(
                "{}:{:#x}",
                if rip >> 63 != 0 { 'k' } else { 'u' },
                rip & 0xfff
            ),
            limit,
        )
    }
    /// The request body: its NUL-terminated arguments' length (u32), the arguments, then `bytes`. Built for every compile
    /// and every persistent-cache key, so written straight into one buffer (the feature arguments formatted once); the bytes
    /// are the persistent cache's key, unchanged (`body_bytes_are_unchanged`).
    fn body_with(&self, bytes: &[u8], rip: String, limit: Option<u32>) -> Vec<u8> {
        let mut body = Vec::with_capacity(4 + 96 + self.feature_args.len() + bytes.len());
        body.extend([0; 4]);
        let arg = |body: &mut Vec<u8>, a: &[u8]| {
            body.extend_from_slice(a);
            body.push(0);
        };
        for a in [&b"--profile"[..], b"slice1", b"--rip", rip.as_bytes()] {
            arg(&mut body, a);
        }
        if self.bits32 {
            arg(&mut body, b"--bitness");
            arg(&mut body, b"32");
            for (on, a) in self.null_segments.into_iter().zip([&b"--fs-null"[..], b"--gs-null", b"--ds-null", b"--es-null"]) {
                if on {
                    arg(&mut body, a);
                }
            }
            for (on, a) in self.limited_segments.into_iter().zip([&b"--fs-limited"[..], b"--gs-limited"]) {
                if on {
                    arg(&mut body, a);
                }
            }
        }
        if let Some(n) = limit {
            arg(&mut body, b"--max-insts");
            arg(&mut body, n.to_string().as_bytes());
        }
        body.extend_from_slice(&self.feature_args);
        if self.push_run {
            arg(&mut body, b"--push-run");
        }
        arg(&mut body, b"--narrow-cmp");
        for (on, a) in [
            (self.sse_gated, &b"--sse-gated"[..]),
            (self.avx_gated, b"--avx-gated"),
            (self.alignment_gated, b"--alignment-gated"),
        ] {
            if on {
                arg(&mut body, a);
            }
        }
        for a in &self.x87 {
            arg(&mut body, a.as_bytes());
        }
        if self.bits32 && self.x87_guess != 0 {
            arg(&mut body, b"--x87-guess");
            arg(&mut body, format!("{:#x}", self.x87_guess).as_bytes());
        }
        for (on, a) in [(self.cpl0, &b"--cpl0"[..]), (self.tsc, b"--tsc"), (self.bulk, b"--bulk")] {
            if on {
                arg(&mut body, a);
            }
        }
        let n = (body.len() - 4) as u32;
        body[..4].copy_from_slice(&n.to_le_bytes());
        body.extend_from_slice(bytes);
        body
    }
    /// fexc's reply to the request for `bytes` at `rip` and the RIP it was compiled at: from the persistent cache when `disk`
    /// (an answer for any RIP of that half and offset, stored with its RIP), else from the server (then stored); the
    /// persistent cache only at CPL 3 (`cpl3`).
    fn ask(
        &mut self,
        bytes: &[u8],
        rip: u64,
        limit: Option<u32>,
        disk: bool,
        _hook: &mut impl Hook,
    ) -> Result<(Reply, u64)> {
        let t = self.stamp();
        let cache = self.disk.clone().filter(|_| self.cpl3);
        let key = cache
            .as_ref()
            .filter(|d| disk && !d.is_empty())
            .map(|d| d.key(&self.disk_body(bytes, rip, limit)));
        let stored = key.and_then(|k| cache.as_ref().unwrap().get(&k));
        let stored = stored.and_then(|p| {
            Some((
                Reply::from_payload(p.get(8..)?)?,
                u64::from_le_bytes(p.get(..8)?.try_into().unwrap()),
            ))
        });
        self.charge(crate::perf::DISK_GET, t);
        if let Some((reply, at)) = stored.filter(|(r, _)| r.code < 2) {
            self.disk_hits += 1;
            if let Some(r) = &mut self.perf {
                r.sources[1] += 1;
            }
            return Ok((reply, at));
        }
        let body = self.body(bytes, rip, limit);
        record_request(&body);
        let t = self.stamp();
        if self.server.is_none() {
            self.server = Some(Server::start(&self.fexc)?);
        }
        let server = self.server.as_mut().unwrap();
        #[cfg(feature = "testing")]
        _hook.before_compile(self.compiles, server.child.id());
        let began = Instant::now();
        #[cfg(feature = "testing")]
        let pid = server.child.id();
        let response = server.request_with_posted(&body, began + self.deadline, || {
            #[cfg(feature = "testing")]
            _hook.compiler_posted(self.compiles, rip, pid)?;
            Ok(())
        });
        let reply = match response {
            Ok(reply) => reply,
            Err(e) if e.kind() == std::io::ErrorKind::TimedOut => {
                let _ = server.child.kill();
                return Err(format!(
                    "{}: no reply within {:?}, killed",
                    server.failed(e),
                    self.deadline
                ));
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
            return Err(format!(
                "fexc failed at {rip:#x}: {}",
                String::from_utf8_lossy(&reply.json)
            ));
        }
        let t = self.stamp();
        if let Some(disk) = &cache {
            disk.put(
                self.disk_body(bytes, rip, limit),
                [&rip.to_le_bytes()[..], &reply.payload()].concat(),
            );
        }
        self.charge(crate::perf::DISK_PUT, t);
        Ok((reply, rip))
    }
}
/// The reply's `rep_exits`: bit i set when link point i is the self-exit of the REP string instruction that ends the block.
fn rep_exit_mask(block: &admission::Admitted) -> u64 {
    block
        .links
        .iter()
        .enumerate()
        .filter(|(_, p)| Some(p.target) == block.rep_self)
        .fold(0, |m, (k, _)| m | 1 << k)
}
/// The performance log's hash of the guest bytes an answer read.
fn hash(bytes: &[u8]) -> u64 {
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    bytes.hash(&mut h);
    h.finish()
}
/// The part of fexc's no-code report natemu reads: `guest.end`. Other fields are ignored.
#[derive(serde::Deserialize)]
struct FexcReport {
    guest: FexcGuest,
}
#[derive(serde::Deserialize)]
struct FexcGuest {
    end: FexcEnd,
}
#[derive(serde::Deserialize)]
struct FexcEnd {
    reason: String,
    // Kept loose: an odd value only means "no system cut", never a failed answer.
    #[serde(default)]
    rip: serde_json::Value,
    #[serde(default)]
    cut_opcode_map: serde_json::Value,
    #[serde(default)]
    cut_opcode: serde_json::Value,
    /// Present (whatever its value, `null` included) or absent.
    #[serde(default, deserialize_with = "present")]
    cut_gated: Option<serde_json::Value>,
    #[serde(default, deserialize_with = "present")]
    cut_ud: Option<serde_json::Value>,
}
fn present<'de, D: serde::Deserializer<'de>>(
    d: D,
) -> std::result::Result<Option<serde_json::Value>, D::Error> {
    serde::Deserialize::deserialize(d).map(Some)
}
impl FexcEnd {
    /// The opcode map and opcode of the instruction fexc cut the block before, if that is the first one (at `rip`).
    fn system_cut(&self, rip: u64) -> Option<(u8, u8)> {
        let byte = |v: &serde_json::Value| u8::try_from(v.as_u64()?).ok();
        let at = u64::from_str_radix(self.rip.as_str()?.strip_prefix("0x")?, 16).ok()?;
        (at == rip).then_some((byte(&self.cut_opcode_map)?, byte(&self.cut_opcode)?))
    }
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
fn fetched(
    area: Area,
    state: &State,
    limit: Option<u64>,
) -> Result<std::result::Result<Fetched, Stop>> {
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
    Ok(Ok(Fetched {
        bytes,
        more: next.is_some() && !walked,
        next_fault: fault,
        cut,
    }))
}
/// Appends guest RAM's `length` bytes at `gpa` to `out` (false: not all RAM), as `GuestRam::copy_out` reads them (volatile
/// loads: other vCPUs may write them) but eight at a time where aligned (perf chunk 7: its byte loop and the zeroed buffer
/// were about 0.4 us a request).
fn copy_out(ram: &GuestRam, gpa: u64, length: u64, out: &mut Vec<u8>) -> bool {
    let Some(p) = ram.range(gpa, length) else {
        return false;
    };
    let n = length as usize;
    let head = (p as usize).wrapping_neg() % 8;
    let words = n.saturating_sub(head) / 8;
    out.reserve(n);
    // SAFETY: `range` gives `n` bytes of the RAM's mapping at `p`, word-aligned from `p + head`.
    unsafe {
        let byte = |i: usize| std::ptr::read_volatile(p.add(i));
        out.extend((0..head.min(n)).map(byte));
        for w in 0..words {
            out.extend_from_slice(
                &std::ptr::read_volatile(p.add(head + 8 * w).cast::<u64>()).to_ne_bytes(),
            );
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
    /// First and subsequent power-drain polls, after architectural port completion.
    Power,
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
    /// The real channel request was posted; its response has not been consumed. Does not imply
    /// the compiler process is still computing. Test observers must remain finite and releasable.
    #[cfg(feature = "testing")]
    fn compiler_posted(&mut self, _compiles: u64, _rip: u64, _server: u32) -> std::io::Result<()> {
        Ok(())
    }
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
    /// One accepted MMIO operation, retained on the host while the resident services RAM windows.
    pub pending_device: Option<crate::devices::Pending>,
    /// Deadline begins when this vCPU observes a power request; polls never block.
    pub power_started: Option<std::time::Instant>,
    /// K2: system-instruction decodes answered ([`exec::REPLY_SYSTEM`], by kind; the test hooks not included), and
    /// entropy refills asked for ([`exec::REQUEST_ENTROPY`]).
    pub decodes: std::collections::BTreeMap<u32, u64>,
    pub entropy: u64,
    /// M2 Part 2: the interpreter's decode requests answered ([`natemu_abi::interp::REQUEST_DECODE`]).
    pub interpreted: u64,
}

/// `NATEMU_LINK_CAP`: the link record area's capacity ([`cache::Control::link_cap`]), a test hook for K1's `link-full`.
/// Unset: 0 (the resident then uses [`cache::MAX_LINKS`]); otherwise a number within
/// [`cache::MIN_LINK_CAP`]..=[`cache::MAX_LINKS`] (an environment variable is input: nothing else reaches the resident).
fn link_cap_from(var: Option<&str>) -> Result<u64> {
    let Some(v) = var else { return Ok(0) };
    match v.parse::<usize>() {
        Ok(n) if (cache::MIN_LINK_CAP..=cache::MAX_LINKS).contains(&n) => Ok(n as u64),
        _ => Err(format!(
            "NATEMU_LINK_CAP={v}: {} to {}",
            cache::MIN_LINK_CAP,
            cache::MAX_LINKS
        )),
    }
}

/// `--page-currency`'s value ([`cache::Control::pages`]): `on` (the page record answers fetch walks), `off` (every one is a walk),
/// `check` (the record answers and the walk is made too: a difference is counted) or `check-halt` (and stops the run).
pub fn parse_page_currency(text: &str) -> std::result::Result<u64, String> {
    match text {
        "off" => Ok(cache::PAGES_OFF),
        "on" => Ok(cache::PAGES_ON),
        "check" => Ok(cache::PAGES_CHECK),
        "check-halt" => Ok(cache::PAGES_CHECK_HALT),
        other => Err(format!("--page-currency {other}: on, off, check or check-halt")),
    }
}

/// `--writable-on-read`'s (and `NATEMU_WRITABLE_ON_READ`'s) value: whether a demand read installs the shadow writable (chunk 1 of the
/// fault-around plan) or read-only (before it).
pub fn parse_writable_on_read(text: &str) -> std::result::Result<bool, String> {
    match text {
        "on" => Ok(true),
        "off" => Ok(false),
        other => Err(format!("--writable-on-read {other}: on or off")),
    }
}

/// `--fp`'s (and `NATEMU_FP`'s) value: whether the floating-point mode is accurate (AFP accurate plan chunk 1) or fast (the default).
pub fn parse_fp(text: &str) -> std::result::Result<bool, String> {
    match text {
        "fast" => Ok(false),
        "accurate" => Ok(true),
        other => Err(format!("--fp {other}: fast or accurate")),
    }
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
    // AFP fallback chunk 1 (plan section 1): `Cpu::no_afp`, once, from the same `supported` value fexc is told (after
    // `NATEMU_FEXC_FEATURE_OFF`), before the first lookup or compile; every vCPU's run starts here (the loader's and the
    // drivers' `Cpu` values predate the probe and carry 0).
    state.cpu.no_afp = u64::from(compiler.supported & 1 << AFP == 0);
    // AFP accurate plan chunk 1: the mode, once, in the same place (`code_context` bit 32 is it AND `no_afp`).
    state.cpu.accurate_fp = u64::from(compiler.accurate_fp);
    // AFP accurate plan chunk 6, measurement only: `NATEMU_FP_TRAPS` (ufe, ioe, ufe+ioe) turns on fast mode's trap enables (no AFP only).
    if let Ok(v) = std::env::var("NATEMU_FP_TRAPS") {
        state.cpu.accurate_fp |= v.split('+').map(|t| match t { "ufe" => exec::FP_FAST_UFE, "ioe" => exec::FP_FAST_IOE, "" => 0, o => panic!("NATEMU_FP_TRAPS {o}: ufe, ioe or ufe+ioe") }).sum::<u64>();
    }
    area.write_record(exec::STATE_OFFSET, state);
    // Perf chunk 10: reuse inside the resident, checked against the host's answers under the reuse check, and never into
    // a range the hook answers at (off when there are more of them than the resident keeps).
    let mut fences = [[0u64; 2]; 4];
    let ranges = hook.fences();
    for (f, r) in fences.iter_mut().zip(&ranges) {
        *f = [r.start, r.end];
    }
    let mode = if ranges.len() > fences.len() {
        cache::REUSE_OFF
    } else if compiler.check_reuse {
        cache::REUSE_CHECK
    } else {
        cache::REUSE_ON
    };
    let control = cache::LOOKUP_OFFSET + cache::CONTROL;
    area.write_record(control + std::mem::offset_of!(cache::Control, reuse), mode);
    area.write_record(
        control + std::mem::offset_of!(cache::Control, pages),
        compiler.page_currency,
    );
    // A/B measurement only: NATEMU_MISS_FAST=off keeps the resident's fast miss path from being tried.
    area.write_record(
        control + std::mem::offset_of!(cache::Control, miss_fast_off),
        u64::from(std::env::var("NATEMU_MISS_FAST").as_deref() == Ok("off")),
    );
    // Tests only (M2 Part 2): NATEMU_INTERPRETER=off stops a vCPU at the boot-time interpreter's entry, as before it ran.
    area.write_record(
        control + std::mem::offset_of!(cache::Control, interpreter_off),
        u64::from(std::env::var("NATEMU_INTERPRETER").as_deref() == Ok("off")),
    );
    // Fault-around plan chunk 1 (A/B measurement): the flag's value, overridden by NATEMU_WRITABLE_ON_READ=on|off (every driver reads it).
    let writable = match std::env::var("NATEMU_WRITABLE_ON_READ") {
        Ok(v) => parse_writable_on_read(&v)?,
        Err(_) => compiler.writable_on_read,
    };
    area.write_record(
        control + std::mem::offset_of!(cache::Control, writable_on_read_off),
        u64::from(!writable),
    );
    let pic = match std::env::var("NATEMU_PIC").as_deref() {
        Ok("off") => cache::PIC_OFF,
        Ok("user") => cache::PIC_USER,
        Ok("all") => cache::PIC_ALL,
        Ok("user64") => cache::PIC_USER64,
        Ok(other) => return Err(format!("NATEMU_PIC={other}: off, user, user64 or all")),
        Err(_) => compiler.pic,
    };
    area.write_record(control + std::mem::offset_of!(cache::Control, pic), pic);
    area.write_record(
        control + std::mem::offset_of!(cache::Control, pic_check_local),
        u64::from(compiler.pic_check_local),
    );
    // PIC study stage 2 tests (`cache::Control::pic_test`).
    let test = match std::env::var("NATEMU_PIC_TEST").as_deref() {
        Err(_) | Ok("") => 0,
        Ok("and") => cache::PIC_TEST_AND,
        Ok("str") => cache::PIC_TEST_STR,
        Ok("body") => cache::PIC_TEST_BODY,
        Ok("svc") => cache::PIC_TEST_SVC,
        Ok("forge") => cache::PIC_TEST_FORGE,
        Ok("forge-noncanonical") => cache::PIC_TEST_FORGE_NONCANONICAL,
        Ok("forge-half") => cache::PIC_TEST_FORGE_HALF,
        Ok("nolocal") => cache::PIC_TEST_NOLOCAL,
        Ok(other) => {
            return Err(format!(
                "NATEMU_PIC_TEST={other}: and, str, body, svc, forge, forge-noncanonical or forge-half"
            ));
        }
    };
    area.write_record(
        control + std::mem::offset_of!(cache::Control, pic_test),
        test,
    );
    area.write_record(
        control + std::mem::offset_of!(cache::Control, reuse_fences),
        fences,
    );
    // K1 `link-full`: a smaller link record area (`NATEMU_LINK_CAP`; unset: the full `cache::MAX_LINKS`).
    let var = match std::env::var("NATEMU_LINK_CAP") {
        Ok(v) => Some(v),
        Err(std::env::VarError::NotPresent) => None,
        Err(e) => return Err(format!("NATEMU_LINK_CAP: {e}")),
    };
    let cap = link_cap_from(var.as_deref())?;
    if let Some(v) = &var {
        static SAID: std::sync::Once = std::sync::Once::new();
        SAID.call_once(|| eprintln!("natemu: NATEMU_LINK_CAP={v}: link record area capacity reduced (test hook)"));
    }
    area.write_record(control + std::mem::offset_of!(cache::Control, link_cap), cap);
    // The template pool (chunk 3): the resident looks in it (mapped read-only at `cache::POOL_VA` by `prepare_with_pool`) unless
    // `NATEMU_POOL_LOOKUP=off` (publication without lookup, the A/B of the pool's cost).
    if let Some(pool) = &compiler.pool {
        let layout = pool.layout();
        let on = !matches!(
            std::env::var("NATEMU_POOL_LOOKUP").as_deref(),
            Ok("off" | "0")
        );
        area.write_record(
            control + std::mem::offset_of!(cache::Control, pool),
            if on { cache::POOL_ON } else { cache::POOL_OFF },
        );
        area.write_record(
            control + std::mem::offset_of!(cache::Control, pool_layout),
            [
                layout.index_slots as u64,
                layout.ring_bytes as u64,
                layout.control_bytes as u64,
            ],
        );
    }
    // When the vCPU last entered the guest (`perf::now`), or [`IN_HOST`] with its exit count while it is in the host: two
    // stores per exit, which the watchdog samples (perf chunk 7; a channel send at each entry and exit woke it twice per
    // exit). Dropping `done` ends it. M2 task D: the watchdog thread forces the vCPU out for its own stop (`forced`) or for an
    // inspection (`inspect`); `serve` tells them apart, and a forced exit asked for by neither (one that arrived after the vCPU
    // had left on its own and was served there) only resumes it.
    let entered = &AtomicU64::new(IN_HOST);
    let (forced, inspect) = (&AtomicBool::new(false), &AtomicBool::new(false));
    let report = compiler.report;
    let (done, ended) = std::sync::mpsc::channel::<()>();
    let id = cpu.id();
    // A halted vCPU (X2: parked in the VM by HLT) is idle, not stuck: the watchdog waits on; the driver decides when a
    // halted vCPU can never wake ([`end_when_halted`]). A scoped thread (R2 review): it borrows the words, and is joined
    // even when `serve` panics (unwinding drops `done` first, which ends it).
    let halted: &AtomicU64 = event_word(
        area.memory,
        area.vcpu,
        std::mem::offset_of!(cache::Events, halted),
    );
    // SIGINFO is taken by a thread that is not running a vCPU (its handler only counts).
    diag::block_inspect_signal();
    std::thread::scope(move |scope| {
        scope.spawn(move || {
            let limit = u64::try_from(watchdog.as_nanos()).unwrap_or(u64::MAX);
            // The entry (or host stay) last seen, when it is next checked, the wait after a check that found it still in the
            // guest (`limit`, then 100 ms: a request racing its entry into `hv_vcpu_run` might not stop it), when it was
            // first seen, when this thread meant to wake, the host stay already reported, and the inspection requests answered.
            let (mut seen, mut due, mut again, mut since, mut planned, mut told) = (0, 0, limit, 0, u64::MAX, 0);
            let mut asked = diag::INSPECT.load(SeqCst);
            let request = || area.read_record::<u64>(exec::STATE_OFFSET + std::mem::offset_of!(State, request));
            loop {
                let (t, now) = (entered.load(SeqCst), crate::perf::now());
                let host = t & IN_HOST != 0;
                if t != seen {
                    // In the host it is checked `limit` after it was first seen there.
                    (seen, due, again, since) = (t, if host { now } else { t }.saturating_add(limit), limit, now);
                }
                // Woken more than 1 s late (the process was stopped: SIGSTOP, a debugger, a sleeping Mac; CLOCK_UPTIME_RAW ran
                // on): the time since then is no evidence of a hang, so the check starts again.
                if now > planned.saturating_add(1_000_000_000) {
                    due = now.saturating_add(limit);
                }
                let secs = (now - since) as f64 / 1e9;
                if diag::INSPECTING.load(Relaxed) && diag::INSPECT.load(SeqCst) != asked {
                    asked = diag::INSPECT.load(SeqCst);
                    if host {
                        eprint!("{}", diag::report(area, "inspected (it runs on)", diag::At::Host { request: request(), secs }));
                    } else {
                        // `serve` reports it at the forced exit and resumes it.
                        inspect.store(true, SeqCst);
                        exit_vcpus(&mut [id]);
                    }
                } else if host && inspect.swap(false, SeqCst) {
                    // Asked in the guest, but it left before the forced exit and stays in the host: reported here.
                    eprint!("{}", diag::report(area, "inspected (it runs on)", diag::At::Host { request: request(), secs }));
                }
                let wait = if now < due {
                    due - now
                } else if host {
                    // Report only, once per stay (a slow serial sink, Ctrl-S, a full pipe are real waits): the run goes on.
                    let r = request();
                    if report && told != t && !matches!(r, exec::REQUEST_TRANSLATE | exec::REQUEST_STEP) {
                        told = t;
                        let what = format!("the host-side progress check: no return to the guest for {secs:.1} s (the run goes on)");
                        eprint!("{}", diag::report(area, &what, diag::At::Host { request: r, secs }));
                    }
                    due = now.saturating_add(limit);
                    limit.max(1_000_000)
                } else {
                    if halted.load(SeqCst) == 0 {
                        forced.store(true, SeqCst);
                        exit_vcpus(&mut [id]);
                        again = 100_000_000;
                    }
                    due = now.saturating_add(again);
                    again
                };
                let wait = if diag::INSPECTING.load(Relaxed) { wait.min(diag::INSPECT_POLL) } else { wait };
                planned = crate::perf::now().saturating_add(wait);
                if ended.recv_timeout(Duration::from_nanos(wait))
                    != Err(std::sync::mpsc::RecvTimeoutError::Timeout)
                {
                    break;
                }
            }
        });
        let stop = serve(
            cpu, area, compiler, stop_at, requests, stops, entered, forced, inspect, cancelled, hook,
        );
        // M2 task D: the stop's report; none for a power-off or reset the guest asked for, nor for a cancel (another vCPU's
        // stop, which has its own report, or the user's). A vCPU forced out of translated code (the watchdog) has the guest's
        // registers in its own; the bank is current at every other stop, but for a resident stop outside execution (stop 0).
        // A host error ends the vCPU too: reported from where the vCPU last was.
        if report {
            let (what, at) = match &stop {
                Ok(Stop::Power(_) | Stop::Cancelled) => (None, diag::At::Forced(cpu)),
                Ok(stop) => {
                    let el0 = cpu.reg(CPSR).is_ok_and(|c| c & 0xf == 0);
                    let at = match stop {
                        _ if el0 || matches!(stop, Stop::Watchdog { .. }) => diag::At::Forced(cpu),
                        Stop::Resident { stop: 0, .. } => diag::At::Stopped { exact: false },
                        _ => diag::At::Stopped { exact: true },
                    };
                    let name = match stop {
                        Stop::Resident { stop, .. } => format!(" ({})", exec::stop::name(*stop)),
                        _ => String::new(),
                    };
                    (Some(format!("stop {stop:?}{name}")), at)
                }
                Err(e) => (Some(format!("host error: {e}")), diag::At::Forced(cpu)),
            };
            if let Some(what) = what {
                eprint!("{}", diag::report(area, &what, at));
            }
        }
        // ponytail: a cancel the watchdog sent just as the guest exited stays pending on the vCPU and would end its next
        // `hv_vcpu_run` at once; harmless while a vCPU serves one run. Drain it (a zero-length run) if vCPUs get reused.
        drop(done);
        stop
    })
}
/// [`run`]'s `entered` word while the vCPU is in the host (with its exit count below, so each stay is told apart).
const IN_HOST: u64 = 1 << 63;
fn serve(
    cpu: &Vcpu,
    area: Area,
    compiler: &mut Compiler,
    stop_at: Option<u64>,
    requests: &mut Requests,
    stops: &Stops,
    entered: &AtomicU64,
    forced: &AtomicBool,
    inspect: &AtomicBool,
    cancelled: &AtomicBool,
    hook: &mut impl Hook,
) -> Result<Stop> {
    // The host's own stop, once decided: the vCPU was told to park (it drops its shadows first).
    let mut parked = None;
    let v = area.vcpu;
    let mut exits = 0u64;
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
        exits += 1;
        entered.store(IN_HOST | exits, SeqCst);
        lap(compiler, crate::perf::SIGNAL);
        let exit = match exit? {
            vm::Exit::Hvc(immediate) => immediate,
            vm::Exit::Cancelled if cancelled.load(SeqCst) => return Ok(Stop::Cancelled),
            // M2 task D: a forced exit the watchdog did not ask for: an inspection's (reported), or a stale one. Hypervisor.framework
            // resumes the vCPU unchanged. The watchdog's own wins over an inspection asked in the same window.
            vm::Exit::Cancelled if !forced.swap(false, SeqCst) => {
                if inspect.swap(false, SeqCst) {
                    eprint!("{}", diag::report(area, "inspected (it runs on)", diag::At::Forced(cpu)));
                }
                continue;
            }
            vm::Exit::Cancelled => {
                let asked = (0..cache::MAX_VCPUS)
                    .filter(|&p| {
                        p != v
                            && record_word(area.memory, p, None).load(SeqCst) & (cache::SHADOWS | cache::DROPS) != 0
                    })
                    .fold(0, |m, p| m | 1 << p);
                return Ok(Stop::Watchdog {
                    pc: cpu.reg(PC)?,
                    el0: cpu.reg(CPSR)? & 0x1f == 0,
                    asked,
                });
            }
        };
        match exit {
            0x49 => {
                let s: Shared = area.read_record(SHARED_OFFSET);
                let state: State = area.read_record(exec::STATE_OFFSET);
                if s.status == natemu_abi::PANIC && s.detail == natemu_abi::FPTRAPS_DETAIL {
                    return Err(crate::probe::fptraps_refusal(s.fpcr_traps).unwrap_or_else(|| "resident stopped on FPCR".into()));
                }
                if !exec::entry_frame_rest_zero(&s.initial) {
                    return Err("the resident's entry frame holds a field it does not write".into());
                }
                return match parked.take() {
                    Some(stop) if state.stop == exec::stop::PARKED => Ok(stop),
                    _ if state.stop == exec::stop::PARKED => Err("vCPU parked unasked".into()),
                    _ => Ok(Stop::Resident {
                        // (The halt's record on stderr: not every driver's report carries it.)
                        stop: {
                            eprintln!(
                                "natemu: resident stop {} native {:x?}",
                                state.stop, state.native
                            );
                            state.stop
                        },
                        status: s.status,
                        detail: s.detail,
                    }),
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
            // An access is accepted once; a deferred reply resumes the resident's control-only wait.
            exec::REQUEST_DEVICE => {
                // A peer failed after this exit: the device is not accessed (the vCPU ends with the access undone).
                if cancelled.load(SeqCst) {
                    return Ok(Stop::Cancelled);
                }
                crate::devices::serve(
                    area,
                    &mut state,
                    requests.devices.as_deref(),
                    &mut requests.pending_device,
                );
                #[cfg(feature = "testing")]
                hook.reply(ReplyKind::Device, area, &mut state);
                area.write_record(exec::STATE_OFFSET, state);
                lap(compiler, crate::perf::DEVICE);
                continue;
            }
            exec::REQUEST_DEVICE_POLL => {
                if cancelled.load(SeqCst) {
                    return Ok(Stop::Cancelled);
                }
                crate::devices::serve_poll(area, &mut state, &mut requests.pending_device);
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
                if let Some(devices) = requests.devices.as_deref().filter(|d| d.power() != 0) {
                    devices.quiesce()?;
                    requests
                        .power_started
                        .get_or_insert_with(std::time::Instant::now);
                    state.reply = exec::REPLY_PORT_POWER;
                }
                area.write_record(exec::STATE_OFFSET, state);
                lap(compiler, crate::perf::PORT);
                continue;
            }
            exec::REQUEST_POWER_POLL => {
                #[cfg(feature = "testing")]
                hook.reply(ReplyKind::Power, area, &mut state);
                if cancelled.load(SeqCst) {
                    return Ok(Stop::Cancelled);
                }
                let start = requests
                    .power_started
                    .ok_or("power poll without completed power port access")?;
                let devices = requests
                    .devices
                    .as_deref()
                    .ok_or("power poll without devices")?;
                if devices.quiesce()? {
                    return Ok(Stop::Power(devices.power()));
                }
                // Past the disk call limit: the drain's final full sync is one disk call.
                if start.elapsed() > crate::virtio::DISK_CALL_LIMIT + Duration::from_secs(30) {
                    return Err("device shutdown drain timed out".into());
                }
                state.reply = exec::REPLY_POWER_POLL;
                area.write_record(exec::STATE_OFFSET, state);
                std::thread::yield_now();
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
            // M2 Part 2: the interpreter's decode of the bytes the resident sent (iced alone: no fexc, cache or admission).
            // The run's stops come first, at the instruction's linear address (CS.base + EIP): the caller's stop and the
            // hook's (a stop parks the vCPU, as at a translation request); the run ends before the next one, so the
            // resident caches no record at a stop and asks again there.
            natemu_abi::interp::REQUEST_DECODE => {
                let linear = crate::interp::linear_ip(&state);
                let stop = if Some(linear) == stop_at {
                    Some(Stop::Reached)
                } else {
                    match hook.at(area, linear)? {
                        Some(At::Stop(code)) => Some(Stop::Hooked(code)),
                        // (A test driver's event source posted its event: the instruction there runs as itself.)
                        #[cfg(feature = "testing")]
                        Some(At::System(i)) if i.kind == exec::system::EVENT_HOOK => None,
                        #[cfg(feature = "testing")]
                        Some(At::System(_)) => {
                            return Err(format!("a test hook's system answer at {linear:#x}, in interpreted code").into());
                        }
                        None => None,
                    }
                };
                if let Some(stop) = stop {
                    parked = Some(stop);
                    state.reply = exec::REPLY_PARK;
                    area.write_record(exec::STATE_OFFSET, state);
                    continue;
                }
                let next = hook.boundary(linear).into_iter().chain(stop_at).filter(|&l| l > linear).min();
                crate::interp::serve(area, &mut state, next)?;
                requests.interpreted += 1;
                area.write_record(exec::STATE_OFFSET, state);
                lap(compiler, crate::perf::INTERPRET);
                continue;
            }
            _ => return Err("translation exit without a request".into()),
        };
        let (began, cr3, rip, gpa, gpa2) = (
            compiler.stamp(),
            state.cpu.cr3,
            state.cpu.rip,
            state.fetch[0],
            state.fetch[1],
        );
        state.reply = match translation(area, compiler, &mut state, step, stop_at, requests, hook)?
        {
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
        lap(
            compiler,
            if step {
                crate::perf::STEP
            } else {
                crate::perf::TRANSLATE
            },
        );
        if let Some(r) = &mut compiler.perf {
            r.request(
                cr3,
                rip,
                gpa,
                state.reply,
                began,
                step,
                state.generation,
                state.cpu.code_context(),
                gpa2,
            );
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
    let hooks = hook
        .boundary(rip)
        .into_iter()
        .chain(stop_at)
        .filter(|&l| l > rip)
        .min();
    // S4: and before every armed instruction breakpoint, so each is the first instruction of a block (the resident checks
    // it there); one inside an instruction on this path is never reached and is passed over.
    let mut breakpoints: Vec<u64> = state.cpu.code_breakpoints().filter(|&b| b > rip).collect();
    let (code, more, next_fault, cut, bytes_len, nop_run, call) = loop {
        let limit = breakpoints.iter().copied().chain(hooks).min();
        let t = compiler.stamp();
        let fetched = fetched(area, state, limit)?;
        compiler.charge(crate::perf::FETCH, t);
        let Fetched {
            bytes,
            more,
            next_fault,
            cut,
        } = match fetched {
            Ok(fetched) => fetched,
            // RIP's page faults: the resident delivers it (`state.fetch_fault`); nothing is compiled.
            Err(Stop::FetchFault(Fault::Page { .. } | Fault::Noncanonical)) => {
                return Ok(Ok(exec::REPLY_FETCH_FAULT));
            }
            Err(stop) => return Ok(Err(stop)),
        };
        compiler.sse_gated = sse_gated(&state.cpu);
        compiler.avx_gated = avx_gated(&state.cpu);
        compiler.x87 = x87_args(&state.cpu, compiler.supported);
        // M2 task 4: 32-bit code is compiled for the x87 stack top and tags the resident found ([`exec::State::x87_guess`]).
        compiler.x87_guess = if step { 0 } else { state.x87_guess };
        compiler.alignment_gated = state.cpu.bit_tests_cut() && !(step && state.plain_step != 0);
        // M2: the code segment's mode (the resident asks only for 64-bit code and, at CPL 3, 32-bit code with flat
        // segments: `Cpu::compat_refused`), and in 32-bit code the null FS, GS, DS and ES.
        compiler.bits32 = state.cpu.code_mode() == Some(exec::CodeMode::Compat32);
        compiler.null_segments = exec::NULL_SREGS.map(|r| state.cpu.null_segment(r));
        compiler.limited_segments = exec::LIMITED_SREGS.map(|r| state.cpu.limited_segment(r));
        // (The inline CPL-0 forms are 64-bit code's: SWAPGS is #UD outside 64-bit mode; fexc refuses them with --bitness 32.)
        compiler.cpl0 = state.cpu.cpl == 0 && !step && !compiler.bits32;
        compiler.tsc = compiler.cpl0 && crate::clock::frequency() == 24_000_000;
        compiler.cpl3 = state.cpu.cpl == 3;
        compiler.bulk = !step && !compiler.alignment_gated;
        compiler.push_run = state.cpu.stack_traps < exec::STACK_TRAP_LIMIT;
        if !step {
            compiler.note_gating(state.cpu.code_context())?;
        }
        let code = compiler.compile(&bytes, rip, step, hook)?;
        match &code {
            // The instruction at RIP runs past a breakpoint the bytes were cut at.
            Code::None(reason)
                if reason == "partial_decode"
                    && limit != hooks
                    && limit == Some(rip + bytes.len() as u64) =>
            {
                breakpoints.retain(|&b| Some(b) != limit)
            }
            _ => {
                // Perf chunk 12: a translate request whose block is only no-operation instructions before a system
                // instruction is answered as their run ([`exec::system::NOP_RUN`]): the resident commits it and runs the
                // system instruction in the same entry. The bytes are those fexc translated, and so is the count.
                let nop_run = match &code {
                    Code::Block(b)
                        if !step
                            && !compiler.bits32
                            && b.end_reason
                                == natemu_abi::code_object::end_reason::SYSTEM_INSTRUCTION
                            && b.guest_bytes <= exec::system::MAX_NOP_RUN =>
                    {
                        bytes
                            .get(..b.guest_bytes as usize)
                            .and_then(crate::system::nops)
                            .is_some_and(|n| n == b.guest_insns)
                            .then_some(b.guest_bytes)
                    }
                    _ => None,
                };
                // Compile-ahead measurement (log on only): the block ends in a call (e8, or ff /2), so its end is a return site.
                let call = match &code {
                    Code::Block(b) if compiler.perf.is_some() => bytes
                        .get(b.last as usize..b.guest_bytes as usize)
                        .is_some_and(|t| {
                            let (o, _) = crate::system::opcode_at(t);
                            matches!(t.get(o..), Some([0xe8, ..]))
                                || matches!(t.get(o..), Some([0xff, m, ..]) if m >> 3 & 7 == 2)
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
        state.system = exec::System {
            kind: exec::system::NOP_RUN,
            length,
            ..exec::System::NONE
        };
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
                Some(Fault::Page { .. } | Fault::Noncanonical) if reason == "partial_decode" => {
                    return Ok(Ok(exec::REPLY_FETCH_FAULT));
                }
                Some(fault) if reason == "partial_decode" => Stop::FetchFault(fault),
                _ => Stop::NoCode(reason),
            }));
        }
    };
    // A step reply is exactly one instruction (the resident refuses anything else too).
    if step && (block.guest_insns != 1 || block.guest_bytes > 15) {
        return Err(format!(
            "step at {rip:#x}: {} instructions, {} bytes",
            block.guest_insns, block.guest_bytes
        ));
    }
    // Perf chunk 10, a template for reuse inside the resident (chunk 6's count): fexc's answer here is its answer for the
    // same bytes at any RIP of this half and page offset (the relocation proof) when it depended only on bytes of the
    // block's own pages before their end (so the frames say which), no limit cut them and no halving retry made it.
    let last = rip.wrapping_add(u64::from(block.guest_bytes).max(1) - 1) >> 12;
    // (M2 reuse: 32-bit code's too, but for a unit compiled for an x87 guess: the resident's templates and the pool are found
    // by the code context, which holds the code mode and the null segments and no guess.)
    let template = !step
        && !cut
        && !compiler.guess_read
        && !compiler.halved
        && block.end_reason != natemu_abi::code_object::end_reason::UNSUPPORTED_FORM
        && compiler.read < bytes_len
        && rip.wrapping_add(compiler.read.max(1) as u64 - 1) >> 12 <= last;
    if let Some(r) = &mut compiler.perf {
        r.template = template;
        // Compile-ahead measurement: the block's exits (its link targets and a call's return site), and whether each lies
        // on a page this request fetched (RIP's, or the next one when it was walked).
        let end = rip.wrapping_add(u64::from(block.guest_bytes));
        let fetched = |t: u64| {
            t >> 12 == rip >> 12
                || (state.fetch[1] != u64::MAX && t >> 12 == (rip >> 12).wrapping_add(1))
        };
        r.block = (block.guest_bytes, call);
        for t in block
            .links
            .iter()
            .map(|l| l.target)
            .chain(call.then_some(end))
        {
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
    state.rep_exits = rep_exit_mask(&block);
    state.sti_first = u64::from(block.sti_first);
    compiler.charge(crate::perf::STAGE, t);
    // The template pool (cross-vCPU reuse plan, chunk 2): a template is published after its admission, once per answer; the
    // resident does not read the pool yet.
    compiler.publish(template, state.cpu.code_context(), rip, &block, state.rep_exits);
    Ok(Ok(exec::REPLY_ADMITTED))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::publisher::Published;
    #[test]
    fn link_cap_env_is_validated() {
        assert_eq!(link_cap_from(None), Ok(0));
        assert_eq!(link_cap_from(Some("16384")), Ok(16384));
        assert_eq!(
            link_cap_from(Some(&cache::MAX_LINKS.to_string())),
            Ok(cache::MAX_LINKS as u64)
        );
        for bad in ["", "0", "-1", "x", "4095", "524289", "99999999999999999999"] {
            assert!(link_cap_from(Some(bad)).is_err(), "{bad}");
        }
    }
    #[test]
    fn frame_alias_entropy_selects_full_slot_and_fails_closed() {
        for word in [0, 4095, 4096, (1 << 21) - 1, 1 << 21, 1 << 30, u64::MAX] {
            let va = random_guest_state_alias(|bytes| {
                *bytes = word.to_ne_bytes();
                0
            })
            .unwrap();
            assert!(exec::guest_state_alias_valid(va));
            assert_eq!(
                va,
                exec::GUEST_STATE_SLOT.start + (word & ((1 << 39) - 4096))
            );
        }
        assert_eq!(
            random_guest_state_alias(|bytes| {
                *bytes = u64::MAX.to_ne_bytes();
                0
            })
            .unwrap(),
            exec::GUEST_STATE_SLOT.end - 4096
        );
        assert!(
            random_guest_state_alias(|bytes| {
                *bytes = [0xff; 8];
                -1
            })
            .is_err()
        );
        // Reject invalid overrides before loading a resident or allocating memory.
        for va in [
            0,
            exec::GUEST_STATE_SLOT.start + 1,
            exec::GUEST_STATE_SLOT.end,
        ] {
            assert!(
                prepare_at_alias("missing-resident", cache::CODE_BYTES, 1, cache::BLOCK, va, false, &[])
                    .err()
                    .unwrap()
                    .starts_with("guest-state alias")
            );
        }
    }

    /// The former K1 formatter, the reference for [`K1`]'s serialization.
    fn old_k1_stats(area: Area) -> String {
        let c: cache::Control = area.read_record(cache::LOOKUP_OFFSET + cache::CONTROL);
        const NAMES: [&str; 8] = ["UC", "WC", "2", "3", "WT", "WP", "WB", "UC-"];
        let types: Vec<String> = (0..8)
            .filter(|&t| c.memory_types[t] != 0)
            .map(|t| format!("\"{}\":{}", NAMES[t], c.memory_types[t]))
            .collect();
        // Perf chunk 12: system instructions by kind ([count, transaction us]) and by the resident bucket that reached them.
        let kinds: Vec<String> = (0..128)
            .filter(|&k| c.system_counts[k] != 0)
            .map(|k| {
                format!(
                    "\"{k:#x}\":[{},{}]",
                    c.system_counts[k],
                    ticks_ns(c.system_ticks[k]) / 1000
                )
            })
            .collect();
        let declined: Vec<String> = natemu_abi::execution::perf::SYS_FAST_DECLINE_NAMES
            .iter()
            .zip(c.sys_fast_declined)
            .map(|(name, count)| format!("\"{name}\":{count}"))
            .collect();
        let from: Vec<String> = (0..32)
            .filter(|&b| c.system_from[b] != 0)
            .map(|b| {
                format!(
                    "\"{}\":{}",
                    natemu_abi::execution::perf::NAMES.get(b).unwrap_or(&"?"),
                    c.system_from[b]
                )
            })
            .collect();
        format!(
            "{{\"cr3_loads\":{},\"global_flushes\":{},\"invlpgs\":{},\"revalidated\":{},\"remapped\":{},\"capacity_flushes\":{},\"killed\":{},\"unvalidated\":{},\"tlb_us\":{},\"invlpg_us\":{},\"page_faults\":{},\"page_fault_us\":{},\"scanned\":{},\"memory_types\":{{{}}},\"uncached\":{{\"steps\":{},\"resident\":{}}},\"l1_switches\":{},\"l1_clears\":{},\"flushes\":{},\"flush_us\":{},\"full\":{:?},\"used\":[{},{},{}],\"peak\":{:?},\"capacity\":[{},{},{}],\"reclaims\":[{},{}],\"reuse\":{{\"copies\":{},\"refused\":{:?},\"checks\":{}}},\"slot_writes\":{},\"kept_resets\":{},\"l1_counts\":{:?},\"dispatch_misses\":0,\"systems\":{{\"kinds\":{{{}}},\"from\":{{{}}},\"chained\":{},\"declined\":{{{}}}}},\"pic\":{:?},\"pic_transitions\":{:?}}}",
            c.cr3_loads,
            c.global_flushes,
            c.invlpgs,
            c.revalidated,
            c.remapped,
            c.capacity_flushes,
            c.killed,
            c.unvalidated,
            ticks_ns(c.tlb_ticks) / 1000,
            ticks_ns(c.invlpg_ticks) / 1000,
            c.page_faults,
            ticks_ns(c.page_fault_ticks) / 1000,
            c.scanned,
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
            [
                c.peak[0].max(c.code_used),
                c.peak[1].max(c.meta_used),
                c.peak[2].max(c.blocks)
            ],
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
            c.l1_counts,
            kinds.join(","),
            from.join(","),
            c.system_chained,
            declined.join(","),
            c.pic_counts,
            pic_transitions(area)
        )
    }

    #[test]
    fn sys_fast_declines_do_not_count_as_system_instructions() {
        use natemu_abi::execution::{perf::SysFastDecline, system};
        let memory = Memory::with_size(exec::BACKING_BYTES).unwrap();
        let ram = unsafe { GuestRam::new(0, 0, memory.0 as u64) };
        let area = Area {
            memory: &memory,
            vcpu: 0,
            vcpus: 1,
            epoch: 0,
            base: 0,
            ram,
        };
        let offset = cache::LOOKUP_OFFSET + cache::CONTROL;
        let mut c: cache::Control = area.read_record(offset);
        // All old aliases, including the presently unused 0x7f, remain genuine kind slots.
        for (n, kind) in [
            system::MASKMOV,
            system::ROTATE,
            system::DOUBLE_SHIFT16,
            0x7f,
        ]
        .into_iter()
        .enumerate()
        {
            c.system_counts[kind as usize] = 10 + n as u64;
        }
        let counts = c.system_counts;
        for (n, reason) in [
            SysFastDecline::NotCurrent,
            SysFastDecline::Boundary,
            SysFastDecline::Other,
            SysFastDecline::After,
        ]
        .into_iter()
        .enumerate()
        {
            for _ in 0..=n {
                // This is the same increment used by the perf resident's declined() path.
                c.record_sys_fast_decline(reason);
            }
            assert_eq!(c.system_counts, counts);
            assert_eq!(c.system_ticks, [0; 128]);
        }
        assert_eq!(c.sys_fast_declined, [1, 2, 3, 4]);
        area.write_record(offset, c);
        let output = serde_json::to_string(&k1_stats(area)).unwrap();
        let systems = &serde_json::from_str::<serde_json::Value>(&output).unwrap()["systems"];
        assert_eq!(
            serde_json::to_string(&systems["kinds"]).unwrap(),
            r#"{"0x7c":[10,0],"0x7d":[11,0],"0x7e":[12,0],"0x7f":[13,0]}"#
        );
        // In the names' order (serde_json's map would sort them).
        assert!(
            output.contains(r#""declined":{"not_current":1,"boundary":2,"other":3,"after":4}"#)
        );
    }

    #[test]
    fn k1_matches_the_former_format() {
        let memory = Memory::with_size(exec::BACKING_BYTES).unwrap();
        let ram = unsafe { GuestRam::new(0, 0, memory.0 as u64) };
        let area = Area {
            memory: &memory,
            vcpu: 0,
            vcpus: 1,
            epoch: 0,
            base: 0,
            ram,
        };
        let empty: serde_json::Value = serde_json::from_str(&old_k1_stats(area)).unwrap();
        let mut now = serde_json::to_value(k1_stats(area)).unwrap();
        now.as_object_mut().unwrap().remove("pool");
        // The code-size study's record is newer than the reference formatter: it counts live blocks with code of their own,
        // and no block exists here.
        assert_eq!(now.as_object_mut().unwrap().remove("code_size").unwrap(), serde_json::json!([0, 0, 0, 0, 0]));
        now.as_object_mut().unwrap().remove("segments");
        // (So are the page-fault scan's counters, `perf` builds only.)
        for k in ["page_fault_scanned", "scanned_instances", "scanned_stale", "chain_dropped", "relisted"] {
            assert_eq!(now.as_object_mut().unwrap().remove(k), Some(serde_json::json!(0)), "{k}");
        }
        assert_eq!(empty, now);
        let offset = cache::LOOKUP_OFFSET + cache::CONTROL;
        let mut c: cache::Control = area.read_record(offset);
        let mut n = 0;
        let mut next = || {
            n += 1_000_003;
            n
        };
        for field in [
            &mut c.cr3_loads,
            &mut c.global_flushes,
            &mut c.invlpgs,
            &mut c.revalidated,
            &mut c.remapped,
            &mut c.capacity_flushes,
            &mut c.killed,
            &mut c.unvalidated,
            &mut c.tlb_ticks,
            &mut c.invlpg_ticks,
            &mut c.page_faults,
            &mut c.page_fault_ticks,
            &mut c.scanned,
            &mut c.uncached_steps,
            &mut c.uncached_resident,
            &mut c.l1_switches,
            &mut c.l1_clears,
            &mut c.flushes,
            &mut c.flush_ticks,
            &mut c.code_used,
            &mut c.meta_used,
            &mut c.blocks,
            &mut c.code_bytes,
            &mut c.reclaims,
            &mut c.reclaimed,
            &mut c.reuse_copies,
            &mut c.reuse_checks,
            &mut c.slot_writes,
            &mut c.kept_resets,
            &mut c.system_chained,
        ] {
            *field = next();
        }
        c.full = [1, 0, 1];
        c.peak = [u64::MAX, 0, 7];
        c.reuse_refused = [1, 2, 3, 4, 5];
        c.sys_fast_declined = [9, 0, 8, 7];
        (c.memory_types[0], c.memory_types[6], c.memory_types[7]) = (3, 4, 5);
        (
            c.system_counts[0x0f],
            c.system_ticks[0x0f],
            c.system_counts[0x7f],
        ) = (6, 240_000, 1);
        (c.system_from[0], c.system_from[5], c.system_from[31]) = (2, 3, 4);
        area.write_record(offset, c);
        let old: serde_json::Value = serde_json::from_str(&old_k1_stats(area)).unwrap();
        let mut new = serde_json::to_value(k1_stats(area)).unwrap();
        // (The pool's counters are newer than the reference formatter.)
        assert_eq!(new.as_object_mut().unwrap().remove("pool").unwrap()["probes"], 0);
        // (So is the code-size record: no block record exists here, so every count is 0.)
        assert_eq!(new.as_object_mut().unwrap().remove("code_size").unwrap(), serde_json::json!([0, 0, 0, 0, 0]));
        new.as_object_mut().unwrap().remove("segments");
        for k in ["page_fault_scanned", "scanned_instances", "scanned_stale", "chain_dropped", "relisted"] {
            assert_eq!(new.as_object_mut().unwrap().remove(k), Some(serde_json::json!(0)), "{k}");
        }
        assert_eq!(old, new);
    }

    /// Page-currency chunk 0: the study's counters appear in the K1 stats only when a `page-study` resident wrote some (so every
    /// other build's stats keep their form), by caller, in the order `natemu_abi::page_study::Counts` documents.
    #[test]
    fn k1_reports_the_page_study_counters_only_when_written() {
        use natemu_abi::page_study::{B_ALL, B_GLOBAL, C_USER, PLACE, REVAL};
        let memory = Memory::with_size(exec::BACKING_BYTES).unwrap();
        let ram = unsafe { GuestRam::new(0, 0, memory.0 as u64) };
        let area = Area {
            memory: &memory,
            vcpu: 0,
            vcpus: 1,
            epoch: 0,
            base: 0,
            ram,
        };
        assert!(
            serde_json::to_value(k1_stats(area)).unwrap().get("page_study").is_none(),
            "a plain resident writes none"
        );
        let offset = cache::LOOKUP_OFFSET + cache::CONTROL;
        let mut c: cache::Control = area.read_record(offset);
        c.page_study.calls = [7, 3];
        c.page_study.hit_4k[REVAL] = 5;
        c.page_study.class_hits[PLACE][C_USER] = 2;
        c.page_study.disagree_frame = 1;
        c.page_study.blocks[B_ALL] = 4;
        c.page_study.blocks_hit[B_GLOBAL] = 1;
        c.page_study.walk_count[PLACE][1] = 9;
        area.write_record(offset, c);
        let k = serde_json::to_value(k1_stats(area)).unwrap();
        let p = &k["page_study"];
        assert_eq!((p["calls"][0].as_u64(), p["calls"][1].as_u64()), (Some(7), Some(3)));
        assert_eq!(p["hit_4k"][0].as_u64(), Some(5));
        assert_eq!(p["class_hits"][1][2].as_u64(), Some(2));
        assert_eq!(p["disagree_frame"].as_u64(), Some(1));
        assert_eq!((p["blocks"][0].as_u64(), p["blocks_hit"][1].as_u64()), (Some(4), Some(1)));
        assert_eq!(p["walk_count"][1][1].as_u64(), Some(9));
    }

    /// Changes every answer `c` keeps (no other reference is held between compiles).
    fn edit(c: &mut Compiler, f: impl Fn(&mut Entry)) {
        for s in &c.answers.shards {
            let s = &mut *s.lock().unwrap();
            for e in s.cache.values_mut().chain(s.old.values_mut()).flatten() {
                f(Arc::get_mut(e).unwrap());
            }
        }
    }
    fn hit(
        a: &Answers,
        bytes: &[u8],
        rip: u64,
        limit: Option<u32>,
        gating: &[u64; GATING],
    ) -> Option<Arc<Entry>> {
        a.find(bytes, rip, limit, gating).0
    }
    /// Option (d): an answer's kept admitted form is reused, without admission, only where it is admission's decision.
    /// `supported = 0` makes admission refuse every object, so an `Ok` after it is a reuse (the control: it refuses).
    mod kept_form {
        use super::*;
        const USER: &[u8] = include_bytes!("../tests/relocation/user.src");
        const A: (&[u8], u64) = (include_bytes!("../tests/relocation/user-7f1234567100.obj"), 0x7f12_3456_7100);
        const B: (&[u8], u64) = (include_bytes!("../tests/relocation/user-7f1234568100.obj"), 0x7f12_3456_8100);

        fn compiler() -> Compiler {
            let mut c = Compiler::with_features(Path::new("/nonexistent"), u64::MAX, None);
            c.shape = true;
            c
        }
        fn kept_entry(prefix: usize) -> Entry {
            Entry {
                rip: A.1,
                tag: 0,
                owner: 0,
                read: 0,
                gating: [0; GATING],
                prefix: USER[..prefix].into(),
                whole: false,
                answer: Answer::Object(A.0.to_vec()),
                kept: Default::default(),
                published: AtomicU64::new(0),
            }
        }
        fn block(c: &Compiler, answer: &Answer, e: &Entry, rip: u64) -> Result<admission::Admitted> {
            match c.answered(answer, USER, rip, &mut (), Some(e), None)? {
                Ok(Code::Block(b)) => Ok(b),
                _ => panic!("not a block"),
            }
        }
        fn fresh(c: &Compiler, object: &[u8], rip: u64) -> admission::Admitted {
            admission::admit(object, rip, USER, u64::MAX, c.shape, c.cpl0).unwrap()
        }

        #[test]
        fn same_rip_hit_reuses_the_kept_form() {
            let mut c = compiler();
            let e = kept_entry(USER.len());
            let first = block(&c, &e.answer, &e, A.1).unwrap();
            assert!(e.kept.get().is_some());
            assert_eq!(first, fresh(&c, A.0, A.1));
            c.supported = 0;
            assert!(admission::admit(A.0, A.1, USER, 0, true, false).is_err(), "control");
            assert_eq!(block(&c, &e.answer, &e, A.1).unwrap(), first);
        }

        #[test]
        fn other_cpl0_bulk_shape_or_a_short_prefix_admit() {
            let mut c = compiler();
            let e = kept_entry(USER.len());
            block(&c, &e.answer, &e, A.1).unwrap();
            let short = kept_entry(1);
            block(&c, &short.answer, &short, A.1).unwrap();
            assert!((short.prefix.len() as u32) < short.kept.get().unwrap().form.guest_bytes);
            c.supported = 0;
            assert!(block(&c, &short.answer, &short, A.1).is_err(), "short prefix");
            // The boundary: a prefix of exactly the block's guest bytes reuses, one byte less does not.
            let g = e.kept.get().unwrap().form.guest_bytes as usize;
            let (exact, less) = (kept_entry(g), kept_entry(g - 1));
            for x in [&exact, &less] {
                c.supported = u64::MAX;
                block(&c, &x.answer, x, A.1).unwrap();
            }
            c.supported = 0;
            assert!(block(&c, &exact.answer, &exact, A.1).is_ok(), "prefix == guest bytes");
            assert!(block(&c, &less.answer, &less, A.1).is_err(), "prefix < guest bytes");
            let flips: [fn(&mut Compiler); 3] = [|c| c.cpl0 = true, |c| c.bulk = true, |c| c.shape = false];
            for flip in flips {
                let mut d = compiler();
                d.supported = 0;
                flip(&mut d);
                assert!(block(&d, &e.answer, &e, A.1).is_err());
            }
            assert!(block(&c, &e.answer, &e, A.1).is_ok(), "control");
        }

        /// An entry whose kept form is wrong (one more guest instruction than admission gives).
        fn forged(c: &Compiler) -> Entry {
            let e = kept_entry(USER.len());
            block(c, &e.answer, &e, A.1).unwrap();
            let k = e.kept.get().unwrap();
            let form = admission::Admitted { guest_insns: k.form.guest_insns + 1, ..k.form.clone() };
            let f = kept_entry(USER.len());
            f.kept.set(Box::new(Kept { cpl0: k.cpl0, bulk: k.bulk, shape: k.shape, form })).ok().unwrap();
            f
        }

        /// A form kept from another RIP's admission is never used: only the answer's own RIP keeps one.
        #[test]
        fn only_the_answers_own_rip_keeps_a_form() {
            let mut c = compiler();
            let e = kept_entry(USER.len());
            let moved = e.answer.at(A.1, B.1).unwrap();
            block(&c, &moved, &e, B.1).unwrap();
            assert!(e.kept.get().is_none());
            c.supported = 0;
            assert!(block(&c, &e.answer, &e, A.1).is_err());
        }

        /// Under the reuse check a kept form that is not admission's is an error, at its RIP and at another.
        #[test]
        fn reuse_check_catches_a_forged_kept_form() {
            let mut c = compiler();
            let e = forged(&c);
            // Used without the check, the forged form is what comes out (the control: the check is what catches it).
            c.supported = 0;
            assert_ne!(block(&c, &e.answer, &e, A.1).unwrap(), fresh(&c, A.0, A.1));
            c.supported = u64::MAX;
            c.check_reuse = true;
            assert!(block(&c, &e.answer, &e, A.1).is_err());
            let moved = e.answer.at(A.1, B.1).unwrap();
            assert!(block(&c, &moved, &e, B.1).is_err());
            // The honest form passes the check.
            let honest = kept_entry(USER.len());
            block(&c, &honest.answer, &honest, A.1).unwrap();
            assert!(block(&c, &honest.answer, &honest, A.1).is_ok());
        }

        /// The same through `reuse`, whose other-RIP arm would take the error for a relocation fallback (needs fexc, as the
        /// reuse check asks it for its own answer).
        #[test]
        fn reuse_at_another_rip_under_the_check_is_an_error() {
            let Some(fexc) = std::env::var_os("FEXC") else {
                return;
            };
            let mut c = Compiler::with_features(Path::new(&fexc), u64::MAX, None);
            c.shape = true;
            c.check_reuse = true;
            c.compile(USER, A.1, false, &mut ()).unwrap();
            let honest = c.answers.find(USER, A.1, None, &c.gating()).0.unwrap();
            assert!(honest.kept.get().is_some());
            assert!(matches!(c.reuse(&honest, USER, B.1, None, &mut (), None), Ok(Some(_))));
            let k = honest.kept.get().unwrap();
            let form = admission::Admitted { guest_insns: k.form.guest_insns + 1, ..k.form.clone() };
            let f = Entry {
                rip: honest.rip,
                tag: 0,
                owner: 0,
                read: honest.read,
                gating: honest.gating,
                prefix: honest.prefix.clone(),
                whole: honest.whole,
                answer: honest.answer.clone(),
                kept: Default::default(),
                published: AtomicU64::new(0),
            };
            f.kept.set(Box::new(Kept { cpl0: k.cpl0, bulk: k.bulk, shape: k.shape, form })).ok().unwrap();
            assert!(c.reuse(&f, USER, B.1, None, &mut (), None).is_err());
        }

        /// Every object answer is counted under one reason, and the reasons sum to the answers handled.
        #[test]
        fn admissions_are_counted_by_reason_and_sum_to_the_object_answers() {
            let mut c = compiler();
            let mut calls = 0;
            let mut run = |c: &Compiler, e: &Entry, answer: &Answer, rip, origin| {
                calls += 1;
                let _ = c.answered(answer, USER, rip, &mut (), Some(e), origin);
            };
            let e = kept_entry(USER.len());
            run(&c, &e, &e.answer, A.1, Some(why::FRESH));
            run(&c, &e, &e.answer, A.1, None); // reused
            let flips: [fn(&mut Compiler); 3] = [|c| c.cpl0 = true, |c| c.bulk = true, |c| c.shape = false];
            for flip in flips {
                let (cpl0, bulk, shape) = (c.cpl0, c.bulk, c.shape);
                flip(&mut c);
                run(&c, &e, &e.answer, A.1, None);
                (c.cpl0, c.bulk, c.shape) = (cpl0, bulk, shape);
            }
            let short = kept_entry(1);
            run(&c, &short, &short.answer, A.1, Some(why::DISK));
            run(&c, &short, &short.answer, A.1, None);
            let moved = e.answer.at(A.1, B.1).unwrap();
            run(&c, &e, &moved, B.1, None); // reused, moved
            run(&c, &e, &Answer::Object(B.0.to_vec()), B.1 + 8, None); // not movable
            let fresh = kept_entry(USER.len());
            run(&c, &fresh, &fresh.answer, A.1, None);
            let again = kept_entry(USER.len());
            run(&c, &again, &again.answer, A.1, Some(why::RECOMPILED));
            let mut spill = A.0.to_vec();
            let at = std::mem::offset_of!(natemu_abi::code_object::Header, spill_bytes);
            spill[at..at + 4].copy_from_slice(&1u32.to_le_bytes());
            let retry = Entry { answer: Answer::Object(spill), ..kept_entry(USER.len()) };
            run(&c, &retry, &retry.answer, A.1, None);
            let counts: Vec<u64> = c.admissions.iter().map(|n| n.get()).collect();
            assert_eq!(counts, [2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1], "{:?}", why::NAMES);
            assert_eq!(counts.iter().sum::<u64>(), calls);
        }

        #[test]
        fn another_rip_goes_through_relocations_check() {
            let mut c = compiler();
            let e = kept_entry(USER.len());
            block(&c, &e.answer, &e, A.1).unwrap();
            let moved = e.answer.at(A.1, B.1).unwrap();
            let want = fresh(&c, B.0, B.1);
            c.supported = 0;
            assert_eq!(block(&c, &moved, &e, B.1).unwrap(), want);
            // relocate_code refuses another page offset: admitted (and refused here, as admission is).
            let off = Answer::Object(B.0.to_vec());
            assert!(block(&c, &off, &e, B.1 + 8).is_err());
        }
    }


    fn entry(rip: u64, owner: u8, prefix: &[u8], read: u32, gating: [u64; GATING]) -> Arc<Entry> {
        Arc::new(Entry {
            rip,
            tag: 0,
            owner,
            read,
            gating,
            prefix: prefix.into(),
            whole: false,
            answer: Answer::Object(prefix.into()),
            kept: Default::default(),
                published: AtomicU64::new(0),
        })
    }

    #[test]
    fn answers_report_matches_the_former_format() {
        let a = Answers::new(u64::MAX);
        a.insert(
            None,
            entry(0x1100, 0, &[1, 2, 3], 0, [0; GATING]),
            &[1, 2, 3],
            0x1100,
        );
        a.waits[1][0].store(12_345, Relaxed);
        a.waits[1][1].store(6, Relaxed);
        let w: Vec<String> = a
            .waits
            .iter()
            .map(|[ns, n]| format!("[{},{}]", ns.load(Relaxed), n.load(Relaxed)))
            .collect();
        let (bytes, answers) = a.kept();
        let old = format!(
            "answers {{\"lock_waits\":[{}],\"kept_bytes\":{bytes},\"kept_answers\":{answers}}}",
            w.join(",")
        );
        assert_eq!(a.report(), old, "byte for byte");
    }

    /// M2 reuse: an answer is found only under its own code mode, null segments and x87 guess.
    #[test]
    fn answers_of_another_tag_are_never_found() {
        // M2 reuse: the same bytes at the same address (and at another of its page offset) under 64-bit code, 32-bit code,
        // a null segment, an x87 guess and a limited FS or GS are six entries; each is found only under its own tag, at any of its RIPs.
        let a = Answers::new(u64::MAX);
        let g = [0; GATING];
        let tags = [
            0,
            tag32([false; 4], [false; 2], 0).unwrap(),
            tag32([false, false, true, false], [false; 2], 0).unwrap(),
            tag32([false; 4], [false; 2], 0x1000).unwrap(),
            tag32([false; 4], [true, false], 0).unwrap(),
            tag32([false; 4], [false, true], 0).unwrap(),
        ];
        for (owner, &tag) in tags.iter().enumerate() {
            assert_eq!(a.find(&[1, 2, 3], 0x40_1100 | tag, None, &g).0.map(|e| e.owner), None);
            let e = Entry { tag, ..Arc::into_inner(entry(0x40_1100, owner as u8, &[1, 2, 3], 0, g)).unwrap() };
            a.insert(None, Arc::new(e), &[1, 2, 3], 0x40_1100 | tag);
        }
        for (owner, &tag) in tags.iter().enumerate() {
            for rip in [0x40_1100, 0x5655_5100] {
                let found = a.find(&[1, 2, 3], rip | tag, None, &g).0.unwrap();
                assert_eq!((found.owner, found.rip, found.tag), (owner as u8, 0x40_1100, tag));
            }
        }
        assert!(tags[1..].iter().all(|&t| tagged(t | 0xffff_ffff) && !exec::canonical(t) && untagged(t | 0x1234) == 0x1234));
        assert!(!tagged(0x7fff_ffff_f000) && !tagged(0xffff_8000_0000_0000) && tag32([false; 4], [false; 2], 0x2000).is_err());
    }

    /// Perf chunk 13: entries under one key (same limit, half, page offset and lead bytes) are told apart by the rest of
    /// what fexc read (later bytes, consulted gating values), never confused with another half, offset or limit; the
    /// answer kept for the requested RIP comes first.
    #[test]
    fn shared_answers_keep_colliding_keys_apart() {
        let a = Answers::new(u64::MAX);
        let g = [0; GATING];
        let mut sse = [0; GATING];
        sse[0] = 1;
        a.insert(
            None,
            entry(0x1100, 0, &[1, 2, 3, 4, 5], 0, g),
            &[1, 2, 3, 4, 5],
            0x1100,
        );
        a.insert(
            None,
            entry(0x2100, 1, &[1, 2, 3, 4, 6], 0, g),
            &[1, 2, 3, 4, 6],
            0x2100,
        );
        a.insert(
            None,
            entry(0x3100, 2, &[1, 2, 3, 4, 7], 1, sse),
            &[1, 2, 3, 4, 7],
            0x3100,
        ); // read SSE gating 1
        let owner = |bytes: &[u8], rip, limit, gating: &[u64; GATING]| {
            hit(&a, bytes, rip, limit, gating).map(|e| (e.owner, e.rip))
        };
        assert_eq!(
            owner(&[1, 2, 3, 4, 5, 9], 0x9100, None, &g),
            Some((0, 0x1100))
        );
        assert_eq!(owner(&[1, 2, 3, 4, 6], 0x9100, None, &g), Some((1, 0x2100)));
        assert_eq!(owner(&[1, 2, 3, 4, 7], 0x9100, None, &g), None); // other SSE gating
        assert_eq!(
            owner(&[1, 2, 3, 4, 7], 0x9100, None, &sse),
            Some((2, 0x3100))
        );
        assert_eq!(owner(&[1, 2, 3, 4, 8], 0x9100, None, &g), None);
        assert_eq!(owner(&[1, 2, 3, 4, 5], 0x9200, None, &g), None); // another page offset
        assert_eq!(owner(&[1, 2, 3, 4, 5], 1 << 63 | 0x1100, None, &g), None); // the other half
        assert_eq!(owner(&[1, 2, 3, 4, 5], 0x1100, Some(1), &g), None); // another limit
        // A second answer for the same bytes kept for the requested RIP is preferred; a duplicate's insert names the holder.
        let (held, _) = a.insert(
            None,
            entry(0x9100, 3, &[1, 2, 3, 4, 5], 0, g),
            &[1, 2, 3, 4, 5],
            0x9100,
        );
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
        a.insert(
            None,
            entry(0x1100, 0, &[0xaa; 32], 0, g),
            &[0xaa; 32],
            0x1100,
        );
        let held = hit(&a, &[0xaa; 32], 0x1100, None, &g).unwrap();
        // Same limit and page offset: the same shard; distinct bytes.
        for i in 0..2 * GENERATION / SHARDS + 1 {
            let b = (i as u64).to_le_bytes();
            a.insert(
                None,
                entry(0x1100 + (i as u64 + 1) * 0x1000, 1, &b, 0, g),
                &b,
                0x1100,
            );
        }
        assert!(hit(&a, &[0xaa; 32], 0x1100, None, &g).is_none());
        assert_eq!(
            (held.rip, held.prefix.as_slice(), Arc::strong_count(&held)),
            (0x1100, &[0xaa; 32][..], 1)
        );
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
                            assert!(
                                e.matches(&ob, &g)
                                    && e.prefix == ob
                                    && e.owner == o as u8
                                    && e.rip == rip(o, j)
                            );
                            cross.fetch_add(1, Relaxed);
                            held.get_or_insert(e);
                        }
                    }
                    let e = held.expect("no cross-thread hit");
                    assert!(
                        e.owner == ((t + 1) % 4) as u8
                            && matches!(&e.answer, Answer::Object(o) if *o == e.prefix)
                    );
                });
            }
        });
        let (_, kept) = a.kept();
        // 4n = 12 bounds inserted into one shard: it flipped at least four times and keeps at most two generations.
        assert!(
            cross.load(Relaxed) > 0 && kept <= 2 * bound && kept < 4 * n as usize,
            "{} {kept}",
            cross.load(Relaxed)
        );
        let k = key(None, rip(0, 0), &[]);
        let one = std::hash::BuildHasher::hash_one(&a.pick, (k.0, k.1)) as usize % SHARDS;
        let s = a.shards[one].lock().unwrap();
        // Since its last flip the shard holds at least a generation's answers (hits only move them to the newer one).
        assert!(
            !s.old.is_empty() && s.cached <= bound && kept >= bound,
            "{} {}",
            s.old.len(),
            s.cached
        );
    }

    /// With `FEXC` set (perf chunk 13): two compilers sharing one map on two threads: the second's request at another RIP is
    /// the first's answer, relocated and admitted at its own RIP (checked against its own compile), counted by source.
    #[test]
    fn compilers_share_answers_across_threads() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let answers = std::sync::OnceLock::new();
        let user = include_bytes!("../tests/relocation/user.src");
        let run = |v: u8, rip: u64| {
            let mut c = Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
            (c.vcpu, c.check_reuse) = (v, true);
            c.share(&answers);
            let Code::Block(b) = c.compile(user, rip, false, &mut ()).unwrap() else {
                panic!("no block")
            };
            (b, c.compiles, c.relocated, c.reuse_checks, c.hits_by_source)
        };
        let (a, ..) = std::thread::scope(|s| s.spawn(|| run(0, 0x7f12_3456_7100)).join().unwrap());
        let (b, compiles, relocated, checks, by) =
            std::thread::scope(|s| s.spawn(|| run(1, 0x7f12_3456_8100)).join().unwrap());
        assert_eq!(
            (a.guest_insns, b.guest_insns, compiles, relocated, checks),
            (4, 4, 0, 1, 1)
        );
        assert_eq!(by[0], 1);
        assert!(by[1..].iter().all(|&n| n == 0));
        assert_ne!(a.object, b.object);
        assert_eq!(answers.get().unwrap().kept().1, 1);
    }
    /// With `FEXC` set (R2 review): an answer with a spill area is not a host error. The block is translated again with half
    /// its instructions; a spill area at one instruction is delivered as #UD (`REFUSED`, no-stop change; it was the typed stop
    /// `spill_area`). fexc spills on none of the corpora,
    /// so the cached answers are given a spill area (header `spill_bytes`).
    #[test]
    fn spill_areas_retry_then_stop() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        let code = [0x48, 0x01, 0xd8, 0x48, 0x01, 0xd8, 0x48, 0x01, 0xd8, 0xc3]; // add rax, rbx x3; ret
        let spill = |c: &mut super::Compiler| {
            edit(c, |e| {
                if let Answer::Object(o) = &mut e.answer {
                    o[60..64].copy_from_slice(&8u32.to_le_bytes());
                }
            })
        };
        let insns =
            |c: &mut super::Compiler| match c.compile(&code, 0x1000, false, &mut ()).unwrap() {
                Code::Block(b) => Ok(b.guest_insns),
                Code::None(reason) => Err(reason),
                Code::System(s) if s.kind == exec::system::REFUSED => Err("refused".into()),
                Code::System(_) => panic!("system"),
            };
        assert_eq!(insns(&mut c), Ok(4));
        spill(&mut c);
        assert_eq!(insns(&mut c), Ok(2)); // translated again at 2
        c.compile(&code, 0x1000, true, &mut ()).unwrap(); // the step translation (limit 1) cached too
        spill(&mut c);
        assert_eq!(insns(&mut c), Err("refused".into()));
    }

    /// With `FEXC` set: a repeated request is answered from the cache, a different RIP, bytes or block limit is compiled.
    /// The request body (and so every persistent-cache key) is byte for byte what the per-argument `String` version built.
    /// The recorder's records are what `channel_replay_matches_the_pipe` reads: u32 length, then the body as posted.
    #[test]
    fn recorded_requests_read_back_as_the_replay_reads_them() {
        let mut out = Vec::new();
        for body in [&b"\x05\0\0\0abcde+code"[..], b"x"] {
            super::write_record(&mut out, body).unwrap();
        }
        let mut at = 0;
        let mut seen = Vec::new();
        while at < out.len() {
            let n = u32::from_le_bytes(out[at..at + 4].try_into().unwrap()) as usize;
            seen.push(&out[at + 4..at + 4 + n]);
            at += 4 + n;
        }
        assert_eq!(seen, [&b"\x05\0\0\0abcde+code"[..], b"x"]);
    }

    #[test]
    fn added_replay_arguments_keep_the_body_well_formed() {
        let c = Compiler::with_features(Path::new("/nonexistent"), u64::MAX, None);
        let body = c.body_with(&[0x90, 0xc3], "0x1234".into(), Some(4));
        assert_eq!(with_added_args(&body, &[]), body);
        let added = with_added_args(&body, &["--feature", "afp=0"]);
        let args = |b: &[u8]| {
            let n = u32::from_le_bytes(b[..4].try_into().unwrap()) as usize;
            (b[4..4 + n].to_vec(), b[4 + n..].to_vec())
        };
        let ((old, bytes), (new, bytes2)) = (args(&body), args(&added));
        assert_eq!((bytes, new), (bytes2, [&old[..], b"--feature\0afp=0\0"].concat()));
    }
    #[test]
    fn translation_options_are_always_requested() {
        let c = Compiler::with_features(Path::new("/nonexistent"), u64::MAX, None);
        let body = c.body_with(&[0x90], "0x1234".into(), None);
        for a in [&b"--ext-conds\x004\0"[..], b"--push-run\0", b"--narrow-cmp\0"] {
            assert!(body.windows(a.len()).any(|w| w == a));
        }
    }
    #[test]
    fn body_bytes_are_unchanged() {
        fn reference(c: &Compiler, bytes: &[u8], rip: String, limit: Option<u32>) -> Vec<u8> {
            let mut args = vec!["--profile".to_string(), "slice1".into(), "--rip".into(), rip];
            if let Some(n) = limit {
                args.extend(["--max-insts".into(), n.to_string()]);
            }
            for feature in admission::feature_overrides(c.supported) {
                args.extend(["--feature".into(), feature]);
            }
            args.extend(["--ext-conds", "4"].map(String::from));
            if c.push_run {
                args.push("--push-run".into());
            }
            args.push("--narrow-cmp".into());
            for (on, a) in [(c.sse_gated, "--sse-gated"), (c.avx_gated, "--avx-gated"), (c.alignment_gated, "--alignment-gated")] {
                if on {
                    args.push(a.into());
                }
            }
            args.extend(c.x87.iter().cloned());
            for (on, a) in [(c.cpl0, "--cpl0"), (c.tsc, "--tsc"), (c.bulk, "--bulk")] {
                if on {
                    args.push(a.into());
                }
            }
            let args: Vec<u8> = args.iter().flat_map(|a| a.bytes().chain([0])).collect();
            let mut body = (args.len() as u32).to_le_bytes().to_vec();
            body.extend(args);
            body.extend(bytes);
            body
        }
        for supported in [0, u64::MAX, 0b1011] {
            let mut c = Compiler::with_features(Path::new("/nonexistent"), supported, None);
            for flags in 0..64u32 {
                (c.sse_gated, c.avx_gated, c.alignment_gated) = (flags & 1 != 0, flags & 2 != 0, flags & 4 != 0);
                (c.cpl0, c.tsc, c.bulk) = (flags & 8 != 0, flags & 16 != 0, flags & 32 != 0);
                c.push_run = flags % 5 != 0;
                c.x87 = if flags % 3 == 0 { vec![] } else { vec!["--x87".into(), "mode".into()] };
                for limit in [None, Some(1), Some(64)] {
                    let bytes = [0x90, 0xc3, flags as u8];
                    assert_eq!(c.body_with(&bytes, "0x1234".into(), limit), reference(&c, &bytes, "0x1234".into(), limit));
                    assert_eq!(c.disk_body(&bytes, 0xffff_8000_0000_1234, limit), reference(&c, &bytes, "k:0x234".into(), limit));
                }
            }
        }
    }
    /// M2, with `FEXC` set: a request for 32-bit code asks fexc for `--bitness 32` (with `--push-run`, task 4), is admitted under
    /// the 32-bit rules, and its answer is kept and moved to other EIPs as 64-bit code's is (M2 reuse), apart from a 64-bit
    /// request's for the same bytes and from one under other null segments or another x87 guess it read.
    #[test]
    fn answers_for_32_bit_code_are_kept_by_mode_segments_and_guess() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        c.shape = true;
        c.bits32 = true;
        let body = c.body(&[0x90], 0x1000, None);
        let args = String::from_utf8_lossy(&body[4..body.len() - 1]).replace('\0', " ");
        assert!(args.contains("--bitness 32") && args.contains("--push-run"), "{args}");
        let code = [0x40, 0x01, 0xd8, 0xc3]; // inc eax; add eax, ebx; ret (64-bit code: REX add; ret)
        let insns = |c: &mut super::Compiler, code: &[u8], rip| match c.compile(code, rip, false, &mut ()).unwrap() {
            Code::Block(b) => b.guest_insns,
            _ => panic!("no block"),
        };
        // M2 reuse: kept by code mode, under the reuse check (a relocated answer is fexc's at the new EIP, a kept form
        // admission's). The same bytes are another answer in 64-bit code, at the same RIP and at another.
        c.check_reuse = true;
        assert_eq!(insns(&mut c, &code, 0x1000), 3);
        assert_eq!(insns(&mut c, &code, 0x1000), 3);
        assert_eq!(insns(&mut c, &code, 0x2000), 3);
        assert_eq!((c.compiles, c.hits, c.relocated, c.disk_hits), (1, 2, 1, 0));
        c.bits32 = false;
        assert_eq!(insns(&mut c, &code, 0x1000), 2);
        assert_eq!(insns(&mut c, &code, 0x3000), 2);
        assert_eq!((c.compiles, c.hits, c.relocated), (2, 3, 2));
        c.bits32 = true;
        assert_eq!(insns(&mut c, &code, 0x3000), 3);
        assert_eq!((c.compiles, c.hits, c.relocated), (2, 4, 3));
        // Guest-EIP constants (a CALL's return address and target, a branch's two exits, the PC-thunk CALL to the next
        // instruction) at library and program addresses: relocated, the moves in their 32-bit form.
        let pic = [0x40, 0xe8, 0x00, 0x00, 0x00, 0x00]; // inc eax; call $+5
        let jcc = [0x40, 0x75, 0x10, 0x90]; // inc eax; jne $+0x12; ...
        let far = [0x40, 0xe8, 0x00, 0x10, 0xff, 0xff]; // inc eax; call $-0xf000+6
        for code in [&pic[..], &jcc, &far] {
            let (compiles, relocated) = (c.compiles, c.relocated);
            for rip in [0xf7d1_2340u64, 0xf7c5_6340, 0x5655_5340, 0x5661_2340] {
                insns(&mut c, code, rip);
            }
            assert_eq!((c.compiles - compiles, c.relocated - relocated), (1, 3), "{code:x?}");
        }
        // Refused, so compiled: anything that wraps at 4 GiB at either end (the target below 0; the return address at 4
        // GiB). A value with a zero half (once another word count) moves: 32-bit code's one form.
        let (refused, relocated) = (c.relocation_refused, c.relocated);
        insns(&mut c, &pic, 0x0000_2340); // 0x2346
        assert_eq!((c.relocation_refused, c.relocated), (refused, relocated + 1));
        insns(&mut c, &far, 0x0000_e340); // the target wraps below 0
        let fwd = [0x40, 0xe8, 0x00, 0x10, 0x00, 0x00]; // inc eax; call $+0x1006
        insns(&mut c, &fwd, 0x5655_5340);
        insns(&mut c, &fwd, 0xffff_f340); // the target wraps past 4 GiB
        assert_eq!(c.relocation_refused, refused + 2);
        // The null segments are the answer's, and the x87 guess where the compile read it (x87 code): another value is
        // another answer. An answer that read no guess is one entry under every guess, and says so (`guess_read`).
        let fld = [0xd9, 0xe8, 0xd9, 0xe8, 0xde, 0xc1, 0xdd, 0x1b, 0xc3]; // fld1; fld1; faddp; fstp qword [ebx]; ret
        let same = |c: &mut super::Compiler, code: &[u8], rip| {
            let n = c.compiles;
            insns(c, code, rip);
            c.compiles == n
        };
        c.x87_guess = 0x1000;
        assert!(!same(&mut c, &fld, 0x5655_5800) && same(&mut c, &fld, 0xf7c5_6800));
        assert!(c.guess_read);
        assert!(same(&mut c, &code, 0x4000) && !c.guess_read, "no x87: one answer under every guess");
        // A FISTP m64 that starts its unit compares its own RIP with the kept integer's: a relocation, moved with the block
        // (a plain constant before, which the libm32 campaign's reuse check found).
        let fistp = [0xdf, 0x7c, 0x24, 0x20, 0xc3]; // fistp qword [esp + 0x20]; ret
        assert!(!same(&mut c, &fistp, 0x5655_5800) && same(&mut c, &fistp, 0xf7c5_6800));
        c.x87_guess = 0x1701;
        assert!(!same(&mut c, &fld, 0x5655_5800) && same(&mut c, &fld, 0xf7c5_6800));
        c.x87_guess = 0;
        assert!(!same(&mut c, &fld, 0x5655_5800));
        c.x87_guess = 0x1000;
        assert!(same(&mut c, &fld, 0x5655_5800));
        c.null_segments = [true, false, false, false];
        assert!(!same(&mut c, &fld, 0x5655_5800) && !same(&mut c, &code, 0x4000));
        c.null_segments = [false; 4];
        assert!(same(&mut c, &code, 0x4000));
        c.x87_guess = 0;
        // The tag is the table's alone: an entry's RIP is its address, and no tagged RIP is a canonical one.
        let at = c.at(0x5655_5340, 0).unwrap();
        assert!(super::tagged(at) && !exec::canonical(at) && super::untagged(at) == 0x5655_5340);
        assert_eq!(at, 0x4000_0000_5655_5340);
        assert_eq!(c.at(0x5655_5340, super::GUESS_FREE).unwrap(), 0x4000_8000_5655_5340);
        c.null_segments = [true, false, false, true];
        c.x87_guess = 0x17ff;
        assert_eq!(c.at(0xffff_ffff, c.x87_guess).unwrap(), 0x4001_7ff9_ffff_ffff);
        assert!(c.at(1 << 32, 0).is_err());
        // (No guess has the bit that marks an answer without one.)
        c.x87_guess = 0x1800;
        assert!(c.at(0x1000, 0).is_err());
        (c.null_segments, c.x87_guess, c.bits32) = ([false; 4], 0, false);
        assert_eq!(c.at(0x40_0000, 0).unwrap(), 0x40_0000);
        assert!(c.at(at, 0).is_err() && c.at(0xffff_8000_0000_0000, 0).is_ok());
        // An argument beyond the key: the guess is 32-bit code's (in its tag), nothing above it is anyone's.
        assert_eq!((c.keyed_read(0x7fff), c.keyed_read(0x8000).is_err(), c.keyed_read(0x1_0000).is_err()), (Ok(0x7fff), true, true));
        c.bits32 = true;
        assert_eq!((c.keyed_read(0x8081), c.keyed_read(0x1_0000).is_err()), (Ok(0x8081), true));
        c.check_reuse = false;
        c.cpl3 = true; // (32-bit code is user code: admission refuses an FS or GS operand of any other request)
        // M2 task 3a: system cuts are decoded in 32-bit mode (40 is INC EAX before the INT, not a REX prefix), and an
        // access through a null FS or GS is the #GP cut; the other segment's, and 64-bit code's, are translated.
        let kind = |c: &mut super::Compiler, code: &[u8]| match c.compile(code, 0x1000, false, &mut ()).unwrap() {
            Code::System(s) => Some((s.kind, s.cr)),
            _ => None,
        };
        assert_eq!(kind(&mut c, &[0xcd, 0x80]), Some((exec::system::INT, 0x80)));
        assert_eq!(kind(&mut c, &[0x8e, 0xe8]).map(|k| k.0), Some(exec::system::MOV_TO_SREG)); // mov gs, eax
        let load = [0x64, 0x8b, 0x03, 0xc3]; // mov eax, fs:[ebx]; ret
        assert_eq!(kind(&mut c, &load), None);
        c.null_segments = [false, true, true, true];
        assert_eq!(kind(&mut c, &load), None);
        assert_eq!(kind(&mut c, &load[1..]), Some((exec::system::NULL_SEGMENT, 0))); // mov eax, [ebx]: DS
        // The resident checks the based source and implicit ES destination after testing ECX.
        match c.compile(&[0x65, 0xf3, 0xa5, 0x90], 0x1000, false, &mut ()).unwrap() {
            Code::System(s) => {
                assert_eq!((s.kind, s.length, s.size), (exec::system::MOVS, 3, 4));
                assert_eq!(s.segment, exec::SREG_GS as u32);
                assert_eq!((s.prefixes, s.cr), (exec::system::ADDRESS32 | exec::system::REP, 0));
            }
            _ => panic!("rep movsd with a GS source must use the resident string path"),
        }
        assert_eq!(kind(&mut c, &[0xa5, 0x90]), Some((exec::system::NULL_SEGMENT, 0)));
        c.null_segments = [true, false, false, false];
        let args = String::from_utf8_lossy(&c.body(&load, 0x1000, None)).replace('\0', " ");
        assert!(args.contains("--fs-null") && !args.contains("--gs-null") && !args.contains("--ds-null"), "{args}");
        assert_eq!(kind(&mut c, &load), Some((exec::system::NULL_SEGMENT, 0)));
        assert_eq!(kind(&mut c, &load[1..]), None);
        c.bits32 = false;
        assert_eq!(kind(&mut c, &load), None);
        assert!(!String::from_utf8_lossy(&c.body(&load, 0x1000, None)).contains("--fs-null"));
    }
    /// M2 reuse, with `FEXC` set: 32-bit code's one move form (`movz w; movk w, lsl #16; nop`) moves between any two EIPs
    /// below 4 GiB, each moved answer compared with fexc's compile at the new EIP by the reuse check: values that once took
    /// FEX's one-word `movn`, `orr #bitmask` and lone `movz` forms and other word counts (zero halves, a target of 0) all
    /// move; a block ending at 4 GiB is refused.
    #[test]
    fn the_one_move_form_of_32_bit_code_moves_as_fexc_compiles_it() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        (c.bits32, c.check_reuse) = (true, true);
        let jcc = [0x40, 0x75, 0x10, 0x90]; // inc eax; jne $+0x12; ...
        let pic = [0x40, 0xe8, 0x00, 0x00, 0x00, 0x00]; // inc eax; call $+5
        let zero = [0x40, 0xe9, 0xba, 0xec, 0xff, 0xff]; // inc eax; jmp 0 (at 0x1340)
        // (code, EIP, relocated, refused): in order; an answer is moved from the first kept one of its bytes.
        let steps: [(&[u8], u64, u64, u64); 12] = [
            (&jcc, 0xffff_1340, 0, 0),
            (&jcc, 0xffff_8340, 1, 0), // once movn to movn
            (&jcc, 0x5655_5340, 1, 0), // once movn to movz + movk
            (&pic, 0x0ffe_fffa, 0, 0), // 0x0ffff000: once orr #bitmask
            (&pic, 0x00fe_fffa, 1, 0), // 0x00fff000: once another bitmask
            (&pic, 0x0f0f_fffa, 1, 0), // 0x0f100000: once one movz
            (&pic, 0x5655_5ffa, 1, 0), // 0x56556000
            (&pic, 0xffff_fffa, 0, 1), // ends at 4 GiB: the return address is 0
            (&pic, 0xffff_fffa, 0, 0), // (its own answer, found again and checked)
            (&zero, 0x0000_1340, 0, 0), // the target is 0
            (&zero, 0x5655_5340, 1, 0), // 0x56554000
            (&zero, 0x0000_1340, 0, 0),
        ];
        for (i, (code, rip, relocated, refused)) in steps.into_iter().enumerate() {
            let before = (c.relocated, c.relocation_refused);
            let Code::Block(b) = c.compile(code, rip, false, &mut ()).unwrap() else {
                panic!("step {i}: no block");
            };
            assert_eq!((c.relocated - before.0, c.relocation_refused - before.1), (relocated, refused), "step {i} at {rip:#x}");
            // Every move is the one form, the `nop` after it.
            let h: natemu_abi::code_object::Header = crate::relocation::read(&b.object, 0).unwrap();
            let word = |at: u32| u32::from_le_bytes(b.object[(h.code_offset + at) as usize..][..4].try_into().unwrap());
            for k in 0..h.relocs_count as usize {
                let r: natemu_abi::code_object::Reloc = crate::relocation::read(&b.object, h.relocs_offset as usize + 16 * k).unwrap();
                if r.kind == natemu_abi::code_object::RELOC_GUEST_RIP_MOVE {
                    let [z, m] = exec::guest_rip_move32(r.value, r.register.into());
                    assert_eq!([word(r.offset), word(r.offset + 4), word(r.offset + 8)], [z, m, exec::NOP], "step {i}");
                }
            }
        }
        assert_eq!((c.compiles, c.reuse_checks), (4, 8));
    }
    /// The exactness of 32-bit sharing, with `FEXC` set: a block compiled at A on the last page below 4 GiB, whose call
    /// target wraps to page 1, rewritten position-independent, is at B's page base word for word fexc's compile at B (the
    /// static proof against B's admitted code, its link targets moved modulo 2^32); not with targets moved by a 64-bit add,
    /// and not against A's code.
    #[test]
    fn a_shared_32_bit_template_is_fexcs_compile_at_another_base() {
        use natemu_abi::execution::{LinkPoint, MAX_LINK_POINTS, pack_reloc};
        use natemu_abi::pic;
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let code = [0x40, 0xe8, 0xfa, 0x1f, 0x00, 0x00]; // inc eax; call $+0x2000 (and the pushed return address)
        let admitted = |rip: u64| {
            let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
            c.bits32 = true;
            let Code::Block(b) = c.compile(&code, rip, false, &mut ()).unwrap() else {
                panic!("no block at {rip:#x}");
            };
            let h: natemu_abi::code_object::Header = crate::relocation::read(&b.object, 0).unwrap();
            let bytes = b.object[h.code_offset as usize..][..h.code_size as usize].to_vec();
            let mut relocs: Vec<u32> = (0..h.relocs_count as usize)
                .flat_map(|i| pack_reloc(&crate::relocation::read(&b.object, h.relocs_offset as usize + 16 * i).unwrap(), h.code_size).unwrap())
                .collect();
            relocs.sort();
            let mut links = [LinkPoint::NONE; MAX_LINK_POINTS];
            links[..b.links.len()].copy_from_slice(&b.links);
            (bytes, relocs, links)
        };
        let (a, b) = (0xffff_f340u64, 0x5655_5340u64);
        let (original, relocs, mut links) = admitted(a);
        assert!(links.iter().any(|p| p.offset != 0 && p.target == 0x1340), "the call's target wraps: {links:x?}");
        assert!(relocs.iter().filter(|&&r| r & exec::RELOC_MOVE != 0).count() >= 2, "{relocs:x?}");
        let mut shared = original.clone();
        assert_eq!(pic::rewrite(&mut shared, &relocs, &mut links, a, true), Ok(()));
        let w = |c: &[u8]| {
            let c = c.to_vec();
            move |o: usize| u32::from_le_bytes(c[o..o + 4].try_into().unwrap())
        };
        assert!(pic::proved(w(&original), w(&shared), shared.len(), &relocs, &links, a, 0, true));
        let (there, relocs_b, _) = admitted(b);
        assert_eq!(relocs_b, relocs);
        let (mut at_b, mut unmasked) = (links, links);
        for (m, u) in at_b.iter_mut().zip(&mut unmasked).filter(|(m, _)| m.offset != 0) {
            (m.target, u.target) = (pic::moved(m.target, a, b, true), pic::moved(u.target, a, b, false));
        }
        assert!(pic::proved(w(&there), w(&shared), shared.len(), &relocs, &at_b, b, 0, true));
        assert!(!pic::proved(w(&there), w(&shared), shared.len(), &relocs, &unmasked, b, 0, true));
        assert!(!pic::proved(w(&original), w(&shared), shared.len(), &relocs, &at_b, b, 0, true));
    }
    #[test]
    fn compiler_keys_answers_by_what_fexc_read() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        let code = [0x48, 0x01, 0xd8, 0xc3];
        let object = |c: &mut super::Compiler, code: &[u8], rip| match c
            .compile(code, rip, false, &mut ())
            .unwrap()
        {
            Code::Block(b) => b.object,
            _ => panic!("no block"),
        };
        let first = object(&mut c, &code, 0x1000);
        assert_eq!((c.compiles, c.hits), (1, 0));
        assert_eq!(object(&mut c, &code, 0x1000), first);
        assert_eq!((c.compiles, c.hits), (1, 1));
        // Perf chunk 4: the same bytes at another RIP of the half and page offset: the answer relocated (no moves here).
        assert_eq!(
            object(&mut c, &code, 0x2000)[24..32],
            0x2000u64.to_le_bytes()
        );
        assert_eq!((c.compiles, c.hits, c.relocated), (1, 2, 1));
        object(&mut c, &[0x48, 0x29, 0xd8, 0xc3], 0x1000);
        c.max_instructions = Some(1);
        object(&mut c, &code, 0x1000);
        assert_eq!((c.compiles, c.hits), (3, 2));
        // A system instruction at the RIP is decoded (and cached with fexc's answer); one later in the block cuts it.
        let mov_cr3 = [0x0f, 0x22, 0xd8, 0x90];
        let decoded = |c: &mut super::Compiler, rip| match c
            .compile(&mov_cr3, rip, false, &mut ())
            .unwrap()
        {
            Code::System(s) => Some(s),
            _ => None,
        };
        let s = decoded(&mut c, 0x3000).unwrap();
        assert_eq!(
            (s.kind, s.cr, s.reg, s.length),
            (exec::system::MOV_TO_CR, 3, 0, 3)
        );
        assert_eq!(decoded(&mut c, 0x3000), Some(s));
        assert_eq!((c.compiles, c.hits), (4, 3));
        assert!(
            matches!(c.compile(&[0xf4], 0x3000, false, &mut ()).unwrap(), Code::System(s) if s.kind == exec::system::HLT)
        ); // X2
        // SYSENTER is decoded since X1c (the resident delivers its fault); this test predates it (it expected no code).
        assert!(
            matches!(c.compile(&[0x0f, 0x34], 0x3000, false, &mut ()).unwrap(), Code::System(s) if s.kind == exec::system::SYSENTER)
        );
        // Longer than 15 bytes (fexc series28): #GP(0) through the resident.
        let long = [
            &[0x66; 4][..],
            &[0x4a, 0xc7, 0x84, 0x83, 0, 1, 0, 0, 0, 0, 0, 0],
        ]
        .concat();
        assert!(
            matches!(c.compile(&long, 0x3000, false, &mut ()).unwrap(), Code::System(s) if s.kind == exec::system::TOO_LONG)
        );
        assert!(matches!(
            c.compile(&[0x90, 0x0f, 0x22, 0xd8], 0x3000, false, &mut ())
                .unwrap(),
            Code::Block(_)
        ));
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
        assert!(
            matches!(c.compile(&movaps, 0x2000, false, &mut ()).unwrap(), Code::System(s) if s.kind == exec::system::SSE_GATED)
        );
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
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        (c.shape, c.cpl0) = (true, true);
        let compile =
            |c: &mut super::Compiler, code: &[u8]| c.compile(code, 0x1000, false, &mut ()).unwrap();
        for code in [
            &[0x0f, 0x20, 0xd0, 0xc3][..],
            &[0x41, 0x0f, 0x20, 0xd1, 0xc3],
            &[0x48, 0x0f, 0x20, 0xd0, 0xc3],
        ] {
            assert!(
                matches!(compile(&mut c, code), Code::Block(b) if b.guest_insns == 2),
                "{code:02x?}"
            );
        }
        for code in [
            &[0x44, 0x0f, 0x20, 0xd0, 0xc3][..],
            &[0x0f, 0x20, 0xd8, 0xc3],
            &[0x66, 0x0f, 0x20, 0xd0, 0xc3],
        ] {
            assert!(
                matches!(compile(&mut c, code), Code::System(_)),
                "{code:02x?}"
            );
        }
        let code = [0x0f, 0x20, 0xd0, 0xc3];
        let Answer::Object(o) = c
            .answers
            .find(&code, 0x1000, None, &c.gating())
            .0
            .unwrap()
            .answer
            .clone()
        else {
            panic!()
        };
        assert!(admission::admit(&o, 0x1000, &code, u64::MAX, true, true).is_ok());
        assert!(
            admission::admit(&o, 0x1000, &code, u64::MAX, false, false)
                .err()
                .unwrap()
                .contains("frame access")
        );
        c.cpl0 = false;
        assert!(matches!(compile(&mut c, &code), Code::System(_)));
    }

    #[test]
    fn cpl0_translates_verw_inline() {
        // Perf P2: VERW m16 (a REX allowed) after another instruction of a CPL-0 request is translated, its exit to its own RIP a
        // link point; first in its block, the register form, with a legacy prefix, VERR, or at CPL 3 it is the resident's.
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        (c.shape, c.cpl0) = (true, true);
        let compile =
            |c: &mut super::Compiler, code: &[u8]| c.compile(code, 0x1000, false, &mut ()).unwrap();
        for code in [
            &[0x90, 0x0f, 0x00, 0x28, 0xc3][..],
            &[0x90, 0x41, 0x0f, 0x00, 0x29, 0xc3],
            &[0x90, 0x0f, 0x00, 0x2d, 0x10, 0, 0, 0, 0xc3],
            &[0x90, 0x42, 0x0f, 0x00, 0x2c, 0x8b, 0xc3],
            &[0x90, 0x48, 0x0f, 0x00, 0x6e, 0x08, 0xc3],
        ] {
            assert!(
                matches!(compile(&mut c, code), Code::Block(b) if b.guest_insns == 3 && b.links.iter().any(|p| p.target == 0x1001)),
                "{code:02x?}"
            );
        }
        for code in [
            &[0x90, 0x66, 0x0f, 0x00, 0xe8, 0xc3][..],
            &[0x90, 0x64, 0x0f, 0x00, 0x28, 0xc3],
            &[0x90, 0x67, 0x0f, 0x00, 0x28, 0xc3],
            &[0x90, 0xf3, 0x0f, 0x00, 0x28, 0xc3],
            &[0x90, 0x0f, 0x00, 0x20, 0xc3],
            &[0x90, 0x0f, 0x00, 0xe8, 0xc3],
            &[0x90, 0x41, 0x0f, 0x00, 0xe9, 0xc3],
        ] {
            assert!(
                matches!(compile(&mut c, code), Code::Block(b) if b.guest_insns == 1),
                "{code:02x?}"
            );
        }
        assert!(matches!(
            compile(&mut c, &[0x0f, 0x00, 0x28, 0xc3]),
            Code::System(_)
        ));
        let code = [0x90, 0x0f, 0x00, 0x28, 0xc3];
        let Answer::Object(o) = c
            .answers
            .find(&code, 0x1000, None, &c.gating())
            .0
            .unwrap()
            .answer
            .clone()
        else {
            panic!()
        };
        assert!(admission::admit(&o, 0x1000, &code, u64::MAX, true, true).is_ok());
        assert!(
            admission::admit(&o, 0x1000, &code, u64::MAX, false, false)
                .err()
                .unwrap()
                .contains("VERW")
        );
        c.cpl0 = false;
        assert!(matches!(compile(&mut c, &code), Code::Block(b) if b.guest_insns == 1));
    }

    #[test]
    fn bulk_rep_stos_only_when_asked() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        c.shape = true;
        let block = |c: &mut super::Compiler, code: &[u8]| match c
            .compile(code, 0x1000, false, &mut ())
            .unwrap()
        {
            Code::Block(b) => b,
            _ => panic!("{code:02x?}"),
        };
        let stos = [0xf3, 0x48, 0xab];
        c.bulk = true;
        assert!(
            block(&mut c, &stos).bulk
                && block(&mut c, &[0x66, 0xf3, 0xab]).bulk
                && block(&mut c, &[0xf3, 0xaa]).bulk
        );
        // 0x67 (EDI/ECX), MOVS, and other code: no loop.
        for code in [
            &[0x67, 0xf3, 0x48, 0xab][..],
            &[0xf3, 0x48, 0xa5],
            &[0x48, 0xab, 0xc3],
            &[0x90, 0xc3],
        ] {
            assert!(!block(&mut c, code).bulk, "{code:02x?}");
        }
        let Answer::Object(o) = c
            .answers
            .find(&stos, 0x1000, None, &c.gating())
            .0
            .unwrap()
            .answer
            .clone()
        else {
            panic!()
        };
        c.bulk = false;
        assert!(!block(&mut c, &stos).bulk);
        let e = c
            .answered(&Answer::Object(o), &stos, 0x1000, &mut (), None, None)
            .err()
            .unwrap();
        assert!(e.contains("--bulk"), "{e}");
    }

    #[test]
    fn cpl0_translates_cli_and_sti_inline() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        (c.shape, c.cpl0) = (true, true);
        let block = |c: &mut super::Compiler, code: &[u8]| match c
            .compile(code, 0x1000, false, &mut ())
            .unwrap()
        {
            Code::Block(b) => Ok(b),
            Code::System(s) => Err(s.kind),
            Code::None(r) => panic!("{r}"),
        };
        let inline = block(&mut c, &[0x90, 0xfa, 0xfb, 0x48, 0xff, 0xc0, 0xc3]).unwrap(); // nop; cli; sti; inc rax; ret
        assert_eq!(inline.guest_insns, 5);
        assert!(
            inline.links.iter().any(|p| p.target == 0x1002),
            "the STI's exit to its own RIP"
        );
        assert_eq!(block(&mut c, &[0xfa, 0xc3]).unwrap().guest_insns, 2); // CLI first
        assert_eq!(
            block(&mut c, &[0x90, 0xfb, 0xfa, 0xc3])
                .unwrap()
                .guest_insns,
            4
        ); // sti; cli
        // Perf chunk 25: STI first (sti; nop; ret): inline, the block marked, its exit to its own start a link point.
        let first = block(&mut c, &[0xfb, 0x90, 0xc3]).unwrap();
        assert!(
            first.guest_insns == 3
                && first.sti_first
                && first.links.iter().any(|p| p.target == 0x1000)
        );
        assert!(!inline.sti_first && !block(&mut c, &[0xfa, 0xc3]).unwrap().sti_first);
        // Admission's PC table starts at guest offset 0: an object whose entries at offset 0 (the block entry and the STI)
        // name the NOP after the STI instead (which would leave the STI unseen and the block unmarked) is refused.
        let Answer::Object(mut o) = c
            .answers
            .find(&[0xfb, 0x90, 0xc3], 0x1000, None, &c.gating())
            .0
            .unwrap()
            .answer
            .clone()
        else {
            panic!()
        };
        let h: natemu_abi::code_object::Header = unsafe {
            o.as_ptr()
                .cast::<natemu_abi::code_object::Header>()
                .read_unaligned()
        };
        assert!(
            admission::admit(&o, 0x1000, &[0xfb, 0x90, 0xc3], u64::MAX, true, true)
                .is_ok_and(|a| a.sti_first)
        );
        for k in 0..h.pcs_count as usize {
            let at = h.pcs_offset as usize
                + k * size_of::<natemu_abi::code_object::PcEntry>()
                + std::mem::offset_of!(natemu_abi::code_object::PcEntry, guest_offset);
            if o[at..at + 4] == [0; 4] {
                o[at..at + 4].copy_from_slice(&1u32.to_le_bytes());
            }
        }
        assert!(
            admission::admit(&o, 0x1000, &[0xfb, 0x90, 0xc3], u64::MAX, true, true)
                .err()
                .unwrap()
                .contains("PC table")
        );
        for kept in [
            &[0x90, 0xfb, 0xf4][..],
            &[0x90, 0xfb, 0xf3, 0xaa],
            &[0x90, 0xfb, 0xf0, 0xf7, 0x18],
            &[0x90, 0x66, 0xfb, 0x90, 0xc3],
            &[0x90, 0xfb],
        ] {
            assert_eq!(block(&mut c, kept).unwrap().guest_insns, 1, "{kept:02x?}"); // sti; hlt / rep stosb / lock neg / 66 sti / last
        }
        // First, but before a system instruction, a self-exit, or alone: still the resident's.
        for kept in [
            &[0xfb, 0xf4][..],
            &[0xfb, 0xf3, 0xaa],
            &[0xfb, 0xf0, 0xf7, 0x18],
            &[0xfb],
        ] {
            assert_eq!(
                block(&mut c, kept).err(),
                Some(exec::system::STI),
                "{kept:02x?}"
            );
        }
        // RDTSC with `--tsc` (CPL 0, a 24 MHz counter), first or not; with REX, or without the flag, the resident's.
        c.tsc = true;
        assert_eq!(block(&mut c, &[0x0f, 0x31, 0xc3]).unwrap().guest_insns, 2);
        assert_eq!(
            block(&mut c, &[0x90, 0x0f, 0x31, 0xfb, 0x90, 0xc3])
                .unwrap()
                .guest_insns,
            5
        ); // rdtsc; sti inline too
        assert_eq!(
            block(&mut c, &[0x48, 0x0f, 0x31, 0xc3]).err(),
            Some(exec::system::RDTSC)
        );
        let Answer::Object(o) = c
            .answers
            .find(&[0x0f, 0x31, 0xc3], 0x1000, None, &c.gating())
            .0
            .unwrap()
            .answer
            .clone()
        else {
            panic!()
        };
        assert!(
            admission::admit(&o, 0x1000, &[0x0f, 0x31, 0xc3], u64::MAX, true, false)
                .err()
                .unwrap()
                .contains("RDTSC")
        );
        // A vCPU without FEAT_ECV (feature 22, `ecv`): fexc reads the count by `isb; mrs cntvct_el0`, admitted with the shape.
        let mut d = super::Compiler::with_features(
            std::path::Path::new(&fexc),
            u64::MAX & !(1 << 22),
            None,
        );
        (d.shape, d.cpl0, d.tsc) = (true, true, true);
        assert_eq!(block(&mut d, &[0x0f, 0x31, 0xc3]).unwrap().guest_insns, 2);
        c.tsc = false;
        assert_eq!(
            block(&mut c, &[0x0f, 0x31, 0xc3]).err(),
            Some(exec::system::RDTSC)
        );
        c.cpl0 = false;
        assert_eq!(
            block(&mut c, &[0x90, 0xfa, 0xfb, 0x48, 0xff, 0xc0, 0xc3])
                .unwrap()
                .guest_insns,
            1
        );
        assert_eq!(block(&mut c, &[0xfa, 0xc3]).err(), Some(exec::system::CLI));
        assert_eq!(
            block(&mut c, &[0xfb, 0x90, 0xc3]).err(),
            Some(exec::system::STI)
        ); // a CPL-3 STI first: never the CPL-0 answer
        let Answer::Object(o) = c
            .answers
            .find(
                &[0x90, 0xfa, 0xfb, 0x48, 0xff, 0xc0, 0xc3],
                0x1000,
                None,
                &{
                    let mut g = c.gating();
                    g[8] = 1;
                    g[9] = 0;
                    g
                },
            )
            .0
            .unwrap()
            .answer
            .clone()
        else {
            panic!()
        };
        let source = [0x90, 0xfa, 0xfb, 0x48, 0xff, 0xc0, 0xc3];
        assert!(admission::admit(&o, 0x1000, &source, u64::MAX, true, true).is_ok());
        assert!(
            admission::admit(&o, 0x1000, &source, u64::MAX, false, false)
                .err()
                .unwrap()
                .contains("CLI/STI")
        );
    }

    /// With `FEXC` set (perf chunk 25): STMXCSR m32 and VSTMXCSR m32 are translated (their stores access sites; the MXCSR
    /// field's load admitted with the code shape), at either CPL; under the SSE or AVX gating or as LDMXCSR/VLDMXCSR they
    /// stay the resident's, and with a 66/F2/F3 prefix (not STMXCSR) fexc makes no code.
    #[test]
    fn stmxcsr_is_translated() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        c.shape = true;
        let block = |c: &mut super::Compiler, code: &[u8]| match c
            .compile(code, 0x1000, false, &mut ())
            .unwrap()
        {
            Code::Block(b) => Ok(b),
            Code::System(s) => Err(s.kind),
            Code::None(r) => panic!("{r}"),
        };
        // stmxcsr [rax]; ret / stmxcsr fs:[rbx + 8*rcx + 0x40]; ret / addss xmm0, xmm1; stmxcsr [rsp - 4]; ret / vstmxcsr [rdi]; ret
        for code in [
            &[0x0f, 0xae, 0x18, 0xc3][..],
            &[0x64, 0x0f, 0xae, 0x5c, 0xcb, 0x40, 0xc3],
            &[0xf3, 0x0f, 0x58, 0xc1, 0x0f, 0xae, 0x5c, 0x24, 0xfc, 0xc3],
            &[0xc5, 0xf8, 0xae, 0x1f, 0xc3],
        ] {
            let b = block(&mut c, code).unwrap();
            assert!(b.guest_insns >= 2, "{code:02x?}");
            let Answer::Object(o) = c
                .answers
                .find(code, 0x1000, None, &c.gating())
                .0
                .unwrap()
                .answer
                .clone()
            else {
                panic!()
            };
            for cpl0 in [false, true] {
                assert!(
                    admission::admit(&o, 0x1000, code, u64::MAX, true, cpl0).is_ok(),
                    "{code:02x?}"
                );
            }
        }
        // 66, F3 or F2 before 0f ae /3 m: not STMXCSR (no code: delivered as #UD, the no-stop change's REFUSED), whatever map
        // FEX files it under.
        for code in [
            &[0x66, 0x0f, 0xae, 0x18, 0xc3][..],
            &[0xf3, 0x0f, 0xae, 0x18, 0xc3],
            &[0xf2, 0x0f, 0xae, 0x18, 0xc3],
        ] {
            assert!(
                matches!(c.compile(code, 0x1000, false, &mut ()).unwrap(), Code::System(s) if s.kind == exec::system::REFUSED),
                "{code:02x?}"
            );
        }
        assert_eq!(
            block(&mut c, &[0x0f, 0xae, 0x10, 0xc3]).err(),
            Some(exec::system::LDMXCSR)
        );
        assert_eq!(
            block(&mut c, &[0xc5, 0xf8, 0xae, 0x17, 0xc3]).err(),
            Some(exec::system::VLDMXCSR)
        );
        c.sse_gated = true;
        assert_eq!(
            block(&mut c, &[0x0f, 0xae, 0x18, 0xc3]).err(),
            Some(exec::system::SSE_GATED)
        );
        assert!(block(&mut c, &[0xc5, 0xf8, 0xae, 0x1f, 0xc3]).is_ok());
        c.sse_gated = false;
        c.avx_gated = true;
        assert_eq!(
            block(&mut c, &[0xc5, 0xf8, 0xae, 0x1f, 0xc3]).err(),
            Some(exec::system::AVX_GATED)
        );
    }

    /// With `FEXC` set (perf chunk 17, fexc patch 0033): at CPL 0 the plain SWAPGS is translated inline, first or not, twice in
    /// one block, and as the instruction after an inline STI; with REX, LOCK or 66 it is the resident's, and a CPL-3 request
    /// cuts it (never reusing the CPL-0 answer). Admission refuses the inline form, and its accesses to the KERNEL_GS_BASE
    /// field, outside a CPL-0 block.
    #[test]
    fn cpl0_translates_swapgs_inline() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        (c.shape, c.cpl0) = (true, true);
        let block = |c: &mut super::Compiler, code: &[u8]| match c
            .compile(code, 0x1000, false, &mut ())
            .unwrap()
        {
            Code::Block(b) => Ok(b),
            Code::System(s) => Err(s.kind),
            Code::None(r) => panic!("{r}"),
        };
        // endbr64; swapgs; mov rax, gs:[16]; swapgs; nop dword [rax]; ret (Linux's entry and exit shapes)
        let linux = [
            0xf3, 0x0f, 0x1e, 0xfa, 0x0f, 0x01, 0xf8, 0x65, 0x48, 0x8b, 0x04, 0x25, 0x10, 0, 0, 0,
            0x0f, 0x01, 0xf8, 0x0f, 0x1f, 0x00, 0xc3,
        ];
        assert_eq!(block(&mut c, &linux).unwrap().guest_insns, 6);
        assert_eq!(
            block(&mut c, &[0x0f, 0x01, 0xf8, 0xc3])
                .unwrap()
                .guest_insns,
            2
        ); // first
        assert_eq!(
            block(&mut c, &[0x90, 0xfb, 0x0f, 0x01, 0xf8, 0xc3])
                .unwrap()
                .guest_insns,
            4
        ); // sti; swapgs: both inline
        for kept in [
            &[0x48, 0x0f, 0x01, 0xf8, 0xc3][..],
            &[0x66, 0x0f, 0x01, 0xf8, 0xc3],
        ] {
            assert_eq!(
                block(&mut c, kept).err(),
                Some(exec::system::SWAPGS),
                "{kept:02x?}"
            );
        }
        // LOCK SWAPGS: kept too, and #UD (no instruction).
        assert_eq!(
            block(&mut c, &[0xf0, 0x0f, 0x01, 0xf8, 0xc3]).err(),
            Some(exec::system::UD)
        );
        c.cpl0 = false;
        assert_eq!(
            block(&mut c, &[0x0f, 0x01, 0xf8, 0xc3]).err(),
            Some(exec::system::SWAPGS)
        );
        assert_eq!(block(&mut c, &linux).unwrap().guest_insns, 1); // endbr64, cut before the SWAPGS
        let Answer::Object(o) = c
            .answers
            .find(&linux, 0x1000, None, &{
                let mut g = c.gating();
                g[8] = 1;
                g
            })
            .0
            .unwrap()
            .answer
            .clone()
        else {
            panic!()
        };
        assert!(admission::admit(&o, 0x1000, &linux, u64::MAX, true, true).is_ok());
        assert!(
            admission::admit(&o, 0x1000, &linux, u64::MAX, false, false)
                .err()
                .unwrap()
                .contains("SWAPGS")
        );
    }

    /// With `FEXC` set (perf chunk 4): answers are reused at other RIPs of the same half and page offset, under the reuse
    /// check (each also compiled and compared); the relocator's refusals are compiled, the reverse canonical case included.
    #[test]
    fn answers_are_reused_across_addresses() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        c.check_reuse = true;
        let block = |c: &mut super::Compiler, code: &[u8], rip| match c
            .compile(code, rip, false, &mut ())
            .unwrap()
        {
            Code::Block(b) => b,
            _ => panic!("no block"),
        };
        let user = include_bytes!("../tests/relocation/user.src");
        let a = block(&mut c, user, 0x7f12_3456_7100);
        let b = block(&mut c, user, 0x7f12_3456_8100);
        assert_eq!(
            (
                a.guest_insns,
                b.guest_insns,
                c.compiles,
                c.relocated,
                c.reuse_checks
            ),
            (4, 4, 1, 1, 1)
        ); // the check's compile is not counted
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
            assert!(
                matches!(c.compile(&mov_cr3, rip, false, &mut ()).unwrap(), Code::System(s) if s.kind == exec::system::MOV_TO_CR)
            );
        }
        assert_eq!((c.compiles, c.relocated, c.reuse_checks), (5, 2, 2));
    }

    /// With `FEXC` set, the reuse check's negative control: a kept object whose guest-RIP literal value is tampered with
    /// relocates (the relocator does not judge values) but is not fexc's answer at the new RIP: under the check a host
    /// error; without it admission refuses the relocated object and the request is compiled (a counted fallback).
    #[test]
    fn reuse_check_catches_a_tampered_relocation() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let user = include_bytes!("../tests/relocation/user.src");
        for check in [true, false] {
            let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
            c.check_reuse = check;
            assert!(matches!(
                c.compile(user, 0x7f12_3456_7100, false, &mut ()),
                Ok(Code::Block(_))
            ));
            edit(&mut c, |e| {
                // The edited answer is no longer the one its kept form (option (d)) was admitted from.
                e.kept = Default::default();
                if let Answer::Object(o) = &mut e.answer {
                    let word = |o: &[u8], at: usize| {
                        u32::from_le_bytes(o[at..at + 4].try_into().unwrap()) as usize
                    };
                    let (at, n) = (word(o, 80), word(o, 84));
                    let literal = (0..n)
                        .map(|i| at + 16 * i)
                        .find(|&r| o[r + 4] == natemu_abi::code_object::RELOC_GUEST_RIP_LITERAL)
                        .unwrap();
                    let v = u64::from_le_bytes(o[literal + 8..literal + 16].try_into().unwrap());
                    o[literal + 8..literal + 16].copy_from_slice(&(v + 0x10).to_le_bytes());
                }
            });
            let r = c.compile(user, 0x7f12_3456_8100, false, &mut ());
            if check {
                assert!(
                    r.as_ref().is_err_and(|e| e.contains("reuse check")),
                    "{:?}",
                    r.err()
                );
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
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let (fexc, dir) = (
            std::path::PathBuf::from(fexc),
            std::env::temp_dir().join(format!("natemu-pc-{}", std::process::id())),
        );
        let _ = std::fs::remove_dir_all(&dir);
        let open = |d: &str| {
            Some(std::sync::Arc::new(
                crate::compile_cache::DiskCache::open(&dir.join(d), &fexc, 1 << 30).unwrap(),
            ))
        };
        let code = [0x48, 0x01, 0xd8, 0xc3];
        let new = |disk| super::Compiler {
            disk,
            cpl3: true,
            ..super::Compiler::with_features(&fexc, u64::MAX, None)
        };
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
        assert_eq!(
            (block(&mut c).unwrap(), c.compiles, c.disk_hits),
            (first, 0, 1)
        );
        // Perf chunk 4: kept on disk with the RIP it was compiled at, used at another of its half and page offset.
        let mut moved = new(open("good"));
        assert!(
            matches!(moved.compile(&code, 0x9000, false, &mut ()), Ok(Code::Block(b)) if b.object[24..32] == 0x9000u64.to_le_bytes())
        );
        assert_eq!(
            (moved.compiles, moved.disk_hits, moved.relocated),
            (0, 1, 1)
        );
        // M2 reuse: 32-bit code's answers too, under their own key (the request's `--bitness 32`, null segments and guess):
        // stored, then served at another EIP, relocated; never to a 64-bit request for the same bytes, nor under a null DS.
        let call = [0x40, 0xe8, 0x00, 0x00, 0x00, 0x00]; // inc eax; call $+5 (64-bit code: a REX call)
        let mut c32 = super::Compiler { bits32: true, ..new(open("good")) };
        assert!(matches!(c32.compile(&call, 0x5655_5000, false, &mut ()), Ok(Code::Block(b)) if b.guest_insns == 2));
        assert_eq!((c32.compiles, c32.disk_hits), (1, 0));
        drop(c32);
        let mut c32 = super::Compiler { bits32: true, check_reuse: true, ..new(open("good")) };
        assert!(
            matches!(c32.compile(&call, 0xf7c5_6000, false, &mut ()), Ok(Code::Block(b)) if b.object[24..32] == 0xf7c5_6000u64.to_le_bytes())
        );
        assert_eq!((c32.compiles, c32.disk_hits, c32.relocated), (0, 1, 1));
        c32.null_segments = [false, false, true, false];
        c32.compile(&call, 0xf7c5_7000, false, &mut ()).unwrap();
        assert_eq!((c32.compiles, c32.disk_hits), (1, 1));
        // M2 checkpoint C: nor under a limited FS or GS, at the same EIP (the request's `--fs-limited`, the answer's tag).
        c32.null_segments = [false; 4];
        for (n, limited) in [[true, false], [false, true], [true, true]].into_iter().enumerate() {
            c32.limited_segments = limited;
            c32.compile(&call, 0xf7c5_6000, false, &mut ()).unwrap();
            assert_eq!((c32.compiles, c32.disk_hits), (2 + n as u64, 1), "{limited:?}");
            c32.compile(&call, 0xf7c5_6000, false, &mut ()).unwrap();
            assert_eq!((c32.compiles, c32.disk_hits), (2 + n as u64, 1), "{limited:?}: its own answer is kept");
        }
        c32.limited_segments = [false; 2];
        c32.bits32 = false;
        assert!(matches!(c32.compile(&call, 0x5655_5000, false, &mut ()), Ok(Code::Block(b)) if b.guest_insns == 1));
        assert_eq!((c32.compiles, c32.disk_hits), (5, 1));
        drop(c32);
        let disk = c.disk.clone().unwrap();
        let stored = disk.get(&disk.key(&body)).unwrap();
        assert_eq!(stored[..8], 0x5000u64.to_le_bytes()); // the RIP it was compiled at
        let mut reply = super::Reply::from_payload(&stored[8..]).unwrap();
        let word =
            |o: &[u8], at: usize| u32::from_le_bytes(o[at..at + 4].try_into().unwrap()) as usize;
        let at = word(&reply.object, 40) + word(&reply.object, 48);
        reply.object[at..at + 4].copy_from_slice(&0xd61f_0380u32.to_le_bytes()); // br x28
        let forged = open("forged").unwrap();
        forged.put(body.clone(), [&stored[..8], &reply.payload()[..]].concat());
        drop(forged);
        let mut c = new(open("forged"));
        let e = block(&mut c).unwrap_err();
        assert!(e.contains("refused") && c.compiles == 0, "{e}");
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// Perf chunk 27: only CPL-3 requests use the persistent cache. A CPL-0 one is neither stored nor looked up (it reaches
    /// fexc even when a CPL-3 request stored the same answer); a CPL-3 one is stored and answered from the cache.
    #[test]
    fn persistent_cache_serves_cpl3_requests_only() {
        let empty = format!(
            "struct.pack_into('<IIIIII', m, REP, 1, 0, 0, 0, 0, 0); struct.pack_into('<II', m, 68, 20, {})",
            Channel::MAGIC
        );
        let fexc = fake_server("cpl3", &empty);
        let dir = std::env::temp_dir().join(format!("natemu-cpl3-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let ask = |cpl3| {
            let disk = Some(std::sync::Arc::new(
                crate::compile_cache::DiskCache::open(&dir, &fexc, 1 << 30).unwrap(),
            ));
            let mut c = super::Compiler {
                disk,
                cpl3,
                ..super::Compiler::with_features(&fexc, u64::MAX, None)
            };
            c.ask(&[0x90], 0x1000, None, true, &mut ()).unwrap();
            (c.compiles, c.disk_hits) // dropped: the cache writes what it was given
        };
        assert_eq!([ask(false), ask(false)], [(1, 0), (1, 0)]);
        assert_eq!(std::fs::read_dir(&dir).unwrap().count(), 0); // nothing stored
        assert_eq!([ask(true), ask(true), ask(false)], [(1, 0), (0, 1), (1, 0)]);
        std::fs::remove_dir_all(&dir).unwrap();
        std::fs::remove_file(fexc).unwrap();
    }

    /// With `FEXC` set (perf plan §2): finishing reaps the server by `wait4` with its CPU time; a compiler that never
    /// started one has none.
    #[test]
    fn finishing_reaps_the_server_with_its_cpu_time() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        assert_eq!(c.finish(), None);
        assert!(matches!(
            c.compile(&[0x48, 0x01, 0xd8, 0xc3], 0x1000, false, &mut ())
                .unwrap(),
            Code::Block(_)
        ));
        let (user, system, rss) = c.finish().unwrap();
        assert!(user + system > 0 && rss > 0, "{user} {system} {rss}");
        assert!(c.server.as_ref().unwrap().reaped);
    }

    /// Perf chunk 21: a stand-in for `fexc --serve --channel FD` (a Python script; no wakes, so the host sees each reply
    /// at its next slice) that answers request 1 with `reply` (Python statements on `m`, the mapping, and `n`, the
    /// request number) and request 2 and later with a well-formed empty reply. It exits once orphaned, so a test process that
    /// dies (a negative control's crash) leaves no stand-in behind.
    fn fake_server(name: &str, reply: &str) -> PathBuf {
        let path =
            std::env::temp_dir().join(format!("natemu-fake-fexc-{name}-{}.py", std::process::id()));
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
            reply = reply
                .lines()
                .map(|l| format!("        {l}\n"))
                .collect::<String>()
        );
        std::fs::write(&path, script).unwrap();
        std::fs::set_permissions(&path, std::os::unix::fs::PermissionsExt::from_mode(0o755))
            .unwrap();
        path
    }

    #[test]
    fn channel_posted_observer_leaves_reply_unconsumed_on_error() {
        let path = fake_server(
            "posted-observer",
            "struct.pack_into('<IIIIII', m, REP, 1, 0, 0, 0, 0, 0); struct.pack_into('<I', m, 68, 20)",
        );
        let mut server = Server::start(&path).unwrap();
        let deadline = Instant::now() + Duration::from_secs(10);
        let mut called = 0;
        let error = server
            .request_with_posted(&[0; 4], deadline, || {
                called += 1;
                Err(std::io::Error::other("observer stopped"))
            })
            .err()
            .unwrap();
        assert_eq!(error.to_string(), "observer stopped");
        assert_eq!(called, 1);
        // The observer ran after posting, and its failure did not consume or replace the response.
        let stdout = &mut server.stdout;
        let reply = server
            .channel
            .reply(deadline, || Server::alive(stdout))
            .unwrap();
        assert_eq!(reply.len(), 20);
        assert_eq!(u32::from_le_bytes(reply[..4].try_into().unwrap()), 1);
        assert!(server.finish().is_some());
        std::fs::remove_file(path).unwrap();
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
            let e = server
                .request(b"\0\0\0\0", Instant::now() + Duration::from_secs(10))
                .err()
                .unwrap();
            assert!(e.to_string().contains(want), "{name}: {e}");
            if name != "stdout" {
                let r = server
                    .request(b"\0\0\0\0", Instant::now() + Duration::from_secs(10))
                    .unwrap();
                assert_eq!((r.code, r.json.len(), r.object.len()), (1, 0, 0), "{name}");
                assert!(
                    server.finish().is_some(),
                    "{name}: the empty request ends it"
                );
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
            assert!(
                c.post(&vec![7; len])
                    .err()
                    .unwrap()
                    .to_string()
                    .contains("does not fit"),
                "{len}"
            );
        }
        c.post(&vec![7; Channel::REQUEST_CAP]).unwrap();
        assert_eq!(
            (c.word(0).load(Relaxed), c.word(4).load(Relaxed) as usize),
            (2, Channel::REQUEST_CAP)
        );
    }

    /// Perf chunk 21: both wakes, with the real server. A server asleep (idle past its 20 us spin; its own sleeps end every
    /// 100 ms) answers a request within 10 ms, so the host woke it; a host asleep (fork mode's compiles take about 0.5 ms,
    /// past the host's 30 us spin; its sleeps end every 5 ms) has each reply within 2.5 ms (median), so the server woke it.
    /// A request of the whole request area reaches the server (an error reply: no arguments).
    #[test]
    fn channel_wakes_both_sides() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let c = super::Compiler::with_features(Path::new(&fexc), u64::MAX, None);
        let body = c.body(&[0x48, 0x01, 0xd8, 0xc3], 0x1000, None);
        let deadline = || Instant::now() + Duration::from_secs(10);
        let mut server = Server::start(Path::new(&fexc)).unwrap();
        assert_eq!(server.request(&body, deadline()).unwrap().code, 0);
        for _ in 0..5 {
            std::thread::sleep(Duration::from_millis(30));
            let t = Instant::now();
            assert_eq!(server.request(&body, deadline()).unwrap().code, 0);
            assert!(
                t.elapsed() < Duration::from_millis(10),
                "a sleeping server answered in {:?}",
                t.elapsed()
            );
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
        assert!(
            took[5] < Duration::from_micros(2500),
            "a sleeping host had its replies in {took:?}"
        );
    }

    /// Perf chunk 21: a server that dies holding a request fails it promptly (stdout's EOF, seen within a slice), well
    /// before the deadline; one that never answers fails it at the deadline.
    #[test]
    fn channel_detects_a_dead_or_hung_server() {
        let path = fake_server("killed", "os.kill(os.getpid(), signal.SIGKILL)");
        let mut server = Server::start(&path).unwrap();
        let t = Instant::now();
        let e = server
            .request(b"\0\0\0\0", t + Duration::from_secs(30))
            .err()
            .unwrap();
        assert_eq!(e.kind(), std::io::ErrorKind::UnexpectedEof, "{e}");
        assert!(t.elapsed() < Duration::from_secs(2), "{:?}", t.elapsed());
        assert!(server.failed(e).contains("signal: 9"));
        std::fs::remove_file(path).unwrap();
        let path = fake_server("hung", "while os.getppid() != 1: time.sleep(0.05)");
        let mut server = Server::start(&path).unwrap();
        let t = Instant::now();
        let e = server
            .request(b"\0\0\0\0", t + Duration::from_millis(300))
            .err()
            .unwrap();
        assert_eq!(e.kind(), std::io::ErrorKind::TimedOut, "{e}");
        assert!(
            (Duration::from_millis(300)..Duration::from_secs(2)).contains(&t.elapsed()),
            "{:?}",
            t.elapsed()
        );
        std::fs::remove_file(path).unwrap();
    }

    /// Perf chunk 21's identity gate (`NATEMU_REPLAY` = a recorded request stream, u32 length + body as the host wrote
    /// them; `NATEMU_REPLAY_OLD` = a compiler run over the pipe, `FEXC` = one run over the channel as the host runs it;
    /// `NATEMU_REPLAY_ARGS`: server options for both, e.g. `--report`; `NATEMU_REPLAY_ADD_ARGS`: request arguments added to
    /// each body sent to `FEXC` only, e.g. `--feature afp=0`, a replay in M2 emulation, where the replies differ from the
    /// recorded host's by design): every reply identical (code, JSON, object, extent, args_read: the whole payload).
    #[test]
    #[ignore]
    fn channel_replay_matches_the_pipe() {
        use std::io::Write;
        let (Some(stream), Some(old), Some(new)) = (
            std::env::var_os("NATEMU_REPLAY"),
            std::env::var_os("NATEMU_REPLAY_OLD"),
            std::env::var_os("FEXC"),
        ) else {
            panic!("NATEMU_REPLAY, NATEMU_REPLAY_OLD and FEXC")
        };
        let extra = std::env::var("NATEMU_REPLAY_ARGS").unwrap_or_default();
        let extra: Vec<&str> = extra.split_whitespace().collect();
        // `NATEMU_REPLAY_ADD_ARGS`: request arguments added to every replayed body for the new compiler (`FEXC`) only, e.g.
        // `--feature afp=0` (the old compiler gets the recorded bodies), so a recorded stream replays as a host without AFP.
        let add = std::env::var("NATEMU_REPLAY_ADD_ARGS").unwrap_or_default();
        let add: Vec<&str> = add.split_whitespace().collect();
        // `NATEMU_REPLAY_OLD_ADD_ARGS`: the same for the old compiler, so two compilers are compared under the same added
        // arguments (AFP fallback chunk 3a: the common mode of M2 emulation, afp=0, against the previous compiler's).
        let old_add = std::env::var("NATEMU_REPLAY_OLD_ADD_ARGS").unwrap_or_default();
        let old_add: Vec<&str> = old_add.split_whitespace().collect();
        let mut pipe = Command::new(old)
            .arg("--serve")
            .args(&extra)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let (mut to, mut from) = (pipe.stdin.take().unwrap(), pipe.stdout.take().unwrap());
        let mut server = Server::spawn(Path::new(&new), &extra).unwrap();
        let mut input =
            std::io::BufReader::with_capacity(1 << 20, std::fs::File::open(stream).unwrap());
        let (mut n, mut differ, mut other, mut word) = (0u64, 0u64, 0u64, [0u8; 4]);
        while input.read_exact(&mut word).is_ok() {
            let mut body = vec![0; u32::from_le_bytes(word) as usize];
            input.read_exact(&mut body).unwrap();
            let recorded = if old_add.is_empty() { body.clone() } else { with_added_args(&body, &old_add) };
            to.write_all(&(recorded.len() as u32).to_le_bytes()).unwrap();
            to.write_all(&recorded).unwrap();
            from.read_exact(&mut word).unwrap();
            let mut want = vec![0; u32::from_le_bytes(word) as usize];
            from.read_exact(&mut want).unwrap();
            let sent = if add.is_empty() { body } else { with_added_args(&body, &add) };
            let sent_copy = sent.clone();
            let r = server
                .request(&sent, Instant::now() + COMPILE_DEADLINE)
                .unwrap();
            let got: Vec<u8> = [
                r.code as u32,
                r.json.len() as u32,
                r.object.len() as u32,
                r.extent,
                r.read,
            ]
            .iter()
            .flat_map(|w| w.to_le_bytes())
            .chain(r.json)
            .chain(r.object)
            .collect();
            n += 1;
            if got != want {
                differ += 1;
                // `NATEMU_REPLAY_DUMP`: a directory that gets each differing request (`N.req`: the body sent) and both replies
                // (`N.old`, `N.new`: the whole payload, as compared), to classify the differences. A dump run never fails:
                // read the count from the printed `replay:` line.
                if let Some(dir) = std::env::var_os("NATEMU_REPLAY_DUMP") {
                    let dir = Path::new(&dir);
                    std::fs::write(dir.join(format!("{n}.req")), &sent_copy).unwrap();
                    std::fs::write(dir.join(format!("{n}.old")), &want).unwrap();
                    std::fs::write(dir.join(format!("{n}.new")), &got).unwrap();
                }
                // (the word at 16..20 is args_read: a compiler that reads more arguments differs there alone)
                other += u64::from(got[..16] != want[..16] || got[20..] != want[20..]);
                if differ <= 10 {
                    eprintln!(
                        "request {n}: pipe {:02x?} channel {:02x?}",
                        &want[..20.min(want.len())],
                        &got[..20]
                    );
                }
            }
        }
        drop(to);
        pipe.wait().unwrap();
        eprintln!("replay: {n} requests, {differ} differences ({other} beyond args_read)");
        // With added arguments the replies differ from the recorded host's by design (M2 emulation): only the count gates,
        // unless the old compiler got the same arguments: then only args_read may differ (a compiler reading more arguments
        // of the same request, e.g. 3a's --daz and --ftz without AFP; the code, JSON and object are the gate).
        assert!(n > 0 && (std::env::var_os("NATEMU_REPLAY_DUMP").is_some() || differ == 0 || (!add.is_empty() && (add != old_add || other == 0))));
    }

    #[test]
    fn configured_capacity_limits() {
        for n in [0, cache::MAX_VCPUS + 1] {
            let e = super::prepare("/nonexistent", cache::CODE_BYTES, n, 4 << 20)
                .err()
                .unwrap();
            assert!(e.contains(&format!("{n} vCPUs")), "{e}");
        }
        assert_eq!(cache::MAX_VCPUS, 64);
        // M1: guest RAM is a nonzero 2 MiB multiple up to 3 GiB.
        for ram in [0, 3 << 20, exec::MAX_GUEST_RAM_BYTES + (2 << 20)] {
            let e = super::prepare("/nonexistent", cache::CODE_BYTES, 1, ram)
                .err()
                .unwrap();
            assert!(e.contains("guest RAM"), "{e}");
        }
    }

    /// The template pool's backing (cross-vCPU reuse plan, N7): its span follows the last vCPU's block and precedes guest RAM
    /// (RAM stays the backing's last bytes, shifted by exactly the span), every vCPU maps it at `cache::POOL_VA` read-only and
    /// never executable, and the pool starts empty and on. Without a pool nothing changes: RAM follows the last block and
    /// the pool's address range is unmapped.
    #[test]
    fn the_pool_is_backed_before_guest_ram_and_mapped_read_only() {
        let path =
            std::env::temp_dir().join(format!("natemu-pool-backing-{}.elf", std::process::id()));
        std::fs::write(
            &path,
            crate::memory::tests::resident_elf(&[(crate::PRIVATE, 4)]),
        )
        .unwrap();
        let resident = path.to_str().unwrap();
        let ram = 4usize << 20;
        let entry = (cache::POOL_VA - PRIVATE) as usize % (1 << 30) / cache::BLOCK * 8;
        for pool in [false, true] {
            let b = prepare_with_pool(resident, cache::CODE_BYTES, 2, ram, pool).unwrap();
            let span = if pool { cache::POOL_BYTES } else { 0 };
            let (pool_offset, ram_offset) = (2 * b.stride, 2 * b.stride + span);
            assert_eq!(b.memory.1, ram_offset + exec::ram_end(ram as u64) as usize);
            // Guest RAM's alias starts at the end of the span, whatever is before it.
            assert_eq!(
                b.ram.range(0, 8).unwrap() as usize,
                b.memory.0 as usize + ram_offset
            );
            assert_eq!(b.pool.is_some(), pool);
            for v in 0..2 {
                let window = v * b.stride;
                let descriptor = |table: usize, index: usize| {
                    u64::from_le_bytes(b.memory.read::<8>(window + table + index * 8))
                };
                // The pool's block descriptors (the first, a middle one, the last), in the window's second GiB.
                let blocks = cache::POOL_BYTES / cache::BLOCK;
                for k in [0, blocks / 2, blocks - 1] {
                    let d = descriptor(exec::RAM_TABLES_OFFSET, entry / 8 + k);
                    if !pool {
                        assert_eq!(d, 0);
                        continue;
                    }
                    assert_eq!(d & 3, 1, "a block descriptor");
                    assert_eq!(
                        d & 0x0000_ffff_ffe0_0000,
                        crate::IPA + (pool_offset + k * cache::BLOCK) as u64
                    );
                    assert_eq!(d >> 53 & 3, 3, "never executable (PXN, UXN)");
                    assert_eq!(d >> 6 & 3, 2, "AP: EL1 read-only, no EL0 access");
                }
                // Guest RAM's first block, in the third GiB, targets the shifted offset.
                assert_eq!(
                    descriptor(exec::RAM_TABLES_OFFSET + 4096, 0) & 0x0000_ffff_ffe0_0000,
                    crate::IPA + ram_offset as u64
                );
            }
            if let Some(p) = &b.pool {
                let pool = p.pool();
                assert!(pool.enabled() && pool.tail() == 0);
                // The writer's span is the one the windows map: its first word is the switch, in the pool's first block.
                assert_eq!(
                    b.memory.read::<8>(pool_offset),
                    1u64.to_le_bytes(),
                    "the switch"
                );
            }
        }
        std::fs::remove_file(path).unwrap();
    }

    /// Compiles with the real fexc (`FEXC`): the answer a vCPU compiles is published once, whichever vCPU's request used
    /// it; the pool holds the block's code at the RIP it was admitted at with the bytes it read.
    #[test]
    fn answers_are_published_once_whichever_vcpu_uses_them() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let layout = natemu_abi::pool::PRODUCTION;
        let memory = Arc::new(Memory::with_size(layout.span_bytes()).unwrap());
        let publisher = Arc::new(crate::publisher::Publisher::new(memory, 0));
        let answers = std::sync::OnceLock::new();
        let compiler = |vcpu| {
            let mut c = Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
            c.vcpu = vcpu;
            c.pool = Some(publisher.clone());
            c.share(&answers);
            c
        };
        let (mut c0, mut c1) = (compiler(0), compiler(1));
        let mut bytes = vec![0x48, 0x01, 0xd8, 0xc3];
        bytes.resize(64, 0x90);
        let context = 7;
        let mut block = |c: &mut Compiler, rip| {
            c.note_gating(context).unwrap();
            let Code::Block(b) = c.compile(&bytes, rip, false, &mut ()).unwrap() else {
                panic!()
            };
            let mask = rep_exit_mask(&b);
            (c.publish(true, context, rip, &b, mask), b)
        };
        let (first, b0) = block(&mut c0, 0x40_1000);
        let Some(Published::Done(at)) = first else {
            panic!("{first:?}")
        };
        // Another vCPU's request, at another RIP of the half and page offset, is a hit of that answer: already published.
        let (second, b1) = block(&mut c1, 0x50_1000);
        assert_eq!(second, Some(Published::Already));
        assert_eq!((c1.hits, c1.compiles, c0.compiles), (1, 0, 1));
        // A request the host did not find a template (the answer is in hand, `last` set): not published, and `last` is spent.
        let Code::Block(again) = c0.compile(&bytes, 0x40_1000, false, &mut ()).unwrap() else {
            panic!()
        };
        assert!(c0.last.is_some());
        assert_eq!(c0.publish(false, context, 0x40_1000, &again, 0), None);
        assert!(c0.last.is_none());
        // The same hit as a template: the answer is in the ring already (and published nowhere twice).
        let Code::Block(again) = c0.compile(&bytes, 0x40_1000, false, &mut ()).unwrap() else {
            panic!()
        };
        assert_eq!(
            c0.publish(true, context, 0x40_1000, &again, 0),
            Some(Published::Already)
        );
        let pool = publisher.pool();
        let mut staging = vec![0; natemu_abi::pool::MAX_ENTRY_BYTES];
        let e = pool.fetch(at, &mut staging).unwrap();
        assert_eq!((e.rip, e.context, e.guest_bytes), (0x40_1000, context, b0.guest_bytes));
        let h: natemu_abi::code_object::Header =
            crate::relocation::read(&b0.object, 0).unwrap();
        assert_eq!(
            e.code,
            &b0.object[h.code_offset as usize..][..h.code_size as usize]
        );
        assert!(e.prefix.len() >= 4 && e.prefix.len() < bytes.len() && e.prefix == &bytes[..e.prefix.len()]);
        let key = natemu_abi::pool::Key::new(context, 0x7000_1000, &bytes, natemu_abi::pool::LEAD);
        assert!(pool.probe(&key).contains(&at));
        // Published by the admitted block at `rip`, whichever vCPU: the other RIP's block has other literals but the same size.
        assert_eq!(b1.object.len(), b0.object.len());
        // Gating: another vCPU's requests of the same context with other values turn the pool off (a host error under the check).
        c1.sse_gated = true;
        c1.check_reuse = true;
        assert!(c1.note_gating(context).is_err());
        assert!(publisher.is_off() && !pool.enabled());
        let (third, _) = {
            c1.check_reuse = false;
            block(&mut c0, 0x60_1000)
        };
        assert_eq!(third, Some(Published::Skipped(crate::publisher::Why::Off)));
    }

    /// AFP fallback plan section 4 (the M4 byte-identity gate the replay cannot see: these are built by the host): with AFP on
    /// (`no_afp` 0), `code_context`, `fpcr`, `x87_fpcr`, `mxcsr_flags` and `x87_args` are what they were before `no_afp` existed,
    /// for every MXCSR value (all 16 bits), every FCW.RC and every combination of FCW's six mask bits. The `old_*` functions are
    /// verbatim copies of the pre-change ones (the commit before chunk 1); `fpsr` runs over every flag byte.
    mod before_no_afp {
        use super::*;
        pub const fn fpcr(mxcsr: u64) -> u64 {
            let rmode = [0, 2, 1, 3][(mxcsr >> 13 & 3) as usize];
            1 << 1 | 1 << 2 | (mxcsr >> 6 & 1) | (mxcsr >> 15 & mxcsr >> 11 & 1) << 24 | rmode << 22
        }
        pub const fn x87_fpcr(fcw: u64, mxcsr: u64) -> u64 {
            // (M2 task 4: no keyed x87 word while every x87 exception is masked: x87 runs make it from FCW.RC.)
            if !fcw & 0x3f == 0 {
                return 0;
            }
            let x87 = 1 << 1 | 1 << 2 | [0, 2, 1, 3][(fcw >> 10 & 3) as usize] << 22;
            if x87 == fpcr(mxcsr) { 0 } else { x87 }
        }
        pub const fn mxcsr_flags(fpsr: u64, mxcsr: u64) -> u64 {
            let de = if mxcsr & exec::MXCSR_DAZ == 0 {
                fpsr >> 7 & 1
            } else {
                0
            };
            (fpsr & 1)
                | de << 1
                | (fpsr >> 1 & 1) << 2
                | (fpsr >> 2 & 1) << 3
                | (fpsr >> 3 & 1) << 4
                | (fpsr >> 4 & 1) << 5
        }
        pub fn code_context(c: &Cpu) -> u64 {
            let bit = |on: bool, n: u32| u64::from(on) << n;
            let word = c.cpl & 3
                | bit(true, 2)
                | bit(c.cr0 & exec::CR0_EM != 0, 9)
                | bit(c.cr0 & exec::CR0_TS != 0, 10)
                | bit(c.cr4 & exec::CR4_OSFXSR != 0, 11)
                | bit(c.bit_tests_cut(), 12)
                | bit(c.cr0 & exec::CR0_MP != 0, 13)
                | (x87_fpcr(c.fcw, c.mxcsr) != 0) as u64 * (1 | (c.fcw >> 10 & 3) << 1) << 14
                | bit(c.avx_enabled(), 17)
                | exec::simd_unmasked(c.mxcsr) << 18
                | exec::x87_unmasked(c.fcw) << 24;
            word
        }
        pub fn x87_args(cpu: &Cpu) -> Vec<String> {
            let mut args = vec![];
            if cpu.cr0 & (exec::CR0_EM | exec::CR0_TS) != 0 {
                args.push("--x87-gated".into());
            }
            if cpu.cr0 & (exec::CR0_TS | exec::CR0_MP) == exec::CR0_TS | exec::CR0_MP {
                args.push("--wait-gated".into());
            }
            match x87_fpcr(cpu.fcw, cpu.mxcsr) {
                0 => {}
                fpcr => args.extend(["--x87-fpcr".into(), format!("{fpcr:#x}")]),
            }
            for (name, unmasked) in [
                ("--simd-unmasked", exec::simd_unmasked(cpu.mxcsr)),
                ("--x87-unmasked", exec::x87_unmasked(cpu.fcw)),
            ] {
                if unmasked != 0 {
                    args.extend([name.into(), format!("{unmasked:#x}")]);
                }
            }
            args
        }
    }
    #[test]
    fn with_afp_on_the_fp_words_are_unchanged_for_every_mxcsr_and_fcw() {
        let afp = u64::MAX; // `supported` with the afp bit set
        let mut cpu = test_cpu(0x1000);
        assert_eq!(cpu.no_afp, 0);
        cpu.cr0 |= exec::CR0_MP;
        let (mut tested, mut keyed) = (0u64, 0u64);
        let mut compilers = [(); 2].map(|_| {
            let one = || Compiler::with_features(Path::new("fexc"), afp, None);
            (one(), one())
        });
        for mxcsr in 0..1u64 << 16 {
            for fpsr in 0..=0xffu64 {
                assert_eq!(
                    exec::mxcsr_flags(fpsr, mxcsr, false),
                    before_no_afp::mxcsr_flags(fpsr, mxcsr),
                    "mxcsr_flags {fpsr:#x} {mxcsr:#x}"
                );
            }
            assert_eq!(exec::fpcr(mxcsr, false), before_no_afp::fpcr(mxcsr), "fpcr {mxcsr:#x}");
            // AFP accurate plan chunk 3: the trap enables are accurate mode without AFP only; with AFP, in either mode, the FPCR a block is
            // entered with is the old one (the `accurate` argument is `accurate_key`, 0 with AFP).
            for accurate_fp in 0..8 {
                assert_eq!(exec::fpcr_run(mxcsr, false, accurate_fp), before_no_afp::fpcr(mxcsr), "fpcr_run {mxcsr:#x}");
            }
            cpu.mxcsr = mxcsr;
            // AFP fallback chunk 3a: the DAZ and FTZ keys exist only without AFP (the context bits and fexc's `--daz`/`--ftz`);
            // with AFP the words compared below (no key bit, no argument) are the old ones.
            let off = Cpu { no_afp: 1, ..cpu };
            let args = x87_args(&off, u64::MAX & !(1 << AFP));
            assert_eq!(args.iter().any(|a| a == "--daz"), mxcsr >> 6 & 1 == 1, "{mxcsr:#x}");
            assert_eq!(args.iter().any(|a| a == "--ftz"), mxcsr >> 15 & mxcsr >> 11 & 1 == 1, "{mxcsr:#x}");
            assert_eq!(off.code_context() >> 30, mxcsr >> 6 & 1 | (mxcsr >> 15 & mxcsr >> 11 & 1) << 1, "{mxcsr:#x}");
            assert_eq!(cpu.code_context() >> 30, 0);
            assert!(!x87_args(&cpu, afp).iter().any(|a| a == "--daz" || a == "--ftz"));
            // FCW: every RC (bits 11:10) with every combination of the six masks (5:0); precision control and the rest fixed.
            for rc in 0..4u64 {
                for masks in 0..1u64 << 6 {
                    let fcw = (exec::FCW_INIT & !(0x3f | 3 << 10)) | rc << 10 | masks;
                    cpu.fcw = fcw;
                    assert_eq!(
                        exec::x87_fpcr(fcw, mxcsr, false, 0),
                        before_no_afp::x87_fpcr(fcw, mxcsr),
                        "x87_fpcr {fcw:#x} {mxcsr:#x}"
                    );
                    assert_eq!(
                        cpu.code_context(),
                        before_no_afp::code_context(&cpu),
                        "code_context {fcw:#x} {mxcsr:#x}"
                    );
                    assert_eq!(
                        x87_args(&cpu, afp),
                        before_no_afp::x87_args(&cpu),
                        "x87_args {fcw:#x} {mxcsr:#x}"
                    );
                    // AFP accurate plan chunk 1: accurate mode on an AFP host changes nothing: the context, the block request's and
                    // the step request's arguments and the gating values are those of fast mode (and so the old ones). `fpcr`,
                    // `x87_fpcr` and `mxcsr_flags` take no mode (their signatures are the proof; compared above).
                    let accurate = Cpu { accurate_fp: 1, ..cpu };
                    assert_eq!(accurate.code_context(), cpu.code_context(), "code_context {fcw:#x} {mxcsr:#x}");
                    for (step, (fast_c, accurate_c)) in [false, true].into_iter().zip(&mut compilers) {
                        let args = x87_args(&cpu, afp);
                        assert_eq!(x87_args(&accurate, afp), args, "step {step}: x87_args {fcw:#x} {mxcsr:#x}");
                        (fast_c.x87, accurate_c.x87) = (args, x87_args(&accurate, afp));
                        assert_eq!(fast_c.gating(), accurate_c.gating(), "step {step}: gating {fcw:#x} {mxcsr:#x}");
                    }
                    tested += 1;
                    keyed += u64::from(exec::x87_fpcr(fcw, mxcsr, false, 0) != 0);
                }
            }
        }
        assert_eq!(tested, 65536 * 256);
        assert!(keyed > 0 && keyed < tested, "both keyed and unkeyed cases ran");
    }
    /// The same inputs without AFP: every x87 word is the AFP-less one (AH, NEP and FIZ never set), `x87_args` agrees with
    /// `supported`'s afp bit (and panics when it does not), and `code_context` keys the x87 rounding exactly when `--x87-fpcr`
    /// is sent.
    #[test]
    fn without_afp_x87_args_follow_the_bit_and_must_agree_with_supported() {
        let no_afp = u64::MAX & !(1 << AFP);
        let mut cpu = Cpu { no_afp: 1, ..test_cpu(0x1000) };
        for mxcsr in [0x1f80u64, 0x1f80 | 3 << 13, 0x1f80 | 1 << 15, 0x1f80 | 1 << 6, 0x1f80 | 3 << 13 | 1 << 15 | 1 << 6, 0] {
            for fcw in [exec::FCW_INIT, exec::FCW_INIT | 1 << 10, exec::FCW_INIT | 3 << 10, 0x3f | 2 << 10] {
                (cpu.mxcsr, cpu.fcw) = (mxcsr, fcw);
                let args = x87_args(&cpu, no_afp);
                let fpcr = exec::x87_fpcr(fcw, mxcsr, true, 0);
                assert_eq!(fpcr & 7, 0, "no AH, NEP or FIZ");
                assert_eq!(args.iter().any(|a| a == "--x87-fpcr"), fpcr != 0);
                assert_eq!(cpu.code_context() >> 14 & 1, u64::from(fpcr != 0));
                // AFP fallback chunk 3a: DAZ and effective FTZ are sent exactly when keyed.
                assert_eq!(args.iter().any(|a| a == "--daz"), cpu.code_context() >> 30 & 1 == 1);
                assert_eq!(args.iter().any(|a| a == "--ftz"), cpu.code_context() >> 31 & 1 == 1);
            }
        }
        // The default 1F80H with RC nearest: nothing is sent and nothing is keyed, as on an AFP host.
        (cpu.mxcsr, cpu.fcw) = (exec::MXCSR_RESET, exec::FCW_INIT);
        assert_eq!(x87_args(&cpu, no_afp), Vec::<String>::new());
        assert_eq!(cpu.code_context(), Cpu { no_afp: 0, ..cpu }.code_context());
        for (bit, supported) in [(0, no_afp), (1, u64::MAX)] {
            let cpu = Cpu { no_afp: bit, ..cpu };
            let e = std::panic::catch_unwind(|| x87_args(&cpu, supported)).unwrap_err();
            let e = e.downcast_ref::<String>().cloned().unwrap_or_default();
            assert!(e.contains("disagrees"), "{e}");
        }
    }

    /// AFP accurate plan chunk 1: without AFP, accurate mode sets `code_context` bit 32 and sends `--accurate`
    /// exactly then; fast mode and accurate mode on an AFP host send and set neither; and the two modes share
    /// no compiled code: the context, the request body (the persistent cache's key and the in-memory answers' gating), the
    /// pool's key differ.
    #[test]
    fn without_afp_accurate_mode_sets_bit_32_and_the_arguments_exactly_then() {
        let no_afp = u64::MAX & !(1 << AFP);
        let afp = u64::MAX;
        let mut cpu = Cpu { no_afp: 1, ..test_cpu(0x1000) };
        for mxcsr in [0x1f80u64, 0x1f80 | 3 << 13, 0x1f80 | 1 << 15 | 1 << 6, 0] {
            for fcw in [exec::FCW_INIT, exec::FCW_INIT | 3 << 10, 0x3f | 2 << 10] {
                (cpu.mxcsr, cpu.fcw) = (mxcsr, fcw);
                let (fast, accurate) = (Cpu { accurate_fp: 0, ..cpu }, Cpu { accurate_fp: 1, ..cpu });
                // Bit 32 alone is the difference, and no other bit moves.
                assert_eq!(fast.code_context() >> 32, 0);
                assert_eq!(accurate.code_context() >> 32, 1);
                // (Chunk 3: but the x87 rounding bits 14-16 and `--x87-fpcr`, which accurate mode's UFE makes an x87 block switch to.)
                const X87_BITS: u64 = 7 << 14;
                assert_eq!(accurate.code_context() & !(1 << 32 | X87_BITS), fast.code_context() & !X87_BITS);
                let strip = |mut v: Vec<String>| {
                    if let Some(i) = v.iter().position(|x| x == "--x87-fpcr") {
                        v.drain(i..i + 2);
                    }
                    v
                };
                {
                    let (f, a) = (x87_args(&fast, no_afp), strip(x87_args(&accurate, no_afp)));
                    let f = strip(f);
                    let mut with = f.clone();
                    with.push("--accurate".into());
                    assert_eq!(a, with, "the mode's argument follows the rest");
                    assert!(!f.iter().any(|x| x == "--accurate"));
                    // The two modes' requests differ, so a persistent-cache key and an in-memory answer differ.
                    let compiler = |args: Vec<String>| {
                        let mut c = Compiler::with_features(Path::new("fexc"), no_afp, None);
                        c.x87 = args;
                        c
                    };
                    let (cf, ca) = (compiler(f), compiler(a));
                    assert_ne!(cf.body_with(&[0x90], "0x1000".into(), None), ca.body_with(&[0x90], "0x1000".into(), None));
                    assert_ne!(cf.gating(), ca.gating());
                    // The accurate-mode flag is the gating argument before the last (`--push-run`).
                    assert_eq!(ca.gating()[GATING - 2], 1);
                    assert_eq!(cf.gating()[GATING - 2], 0);
                }
                // The pool's key and the publication tag hash the whole 64-bit word.
                let key = |c: &Cpu| natemu_abi::pool::Key::new(c.code_context(), 0x1000, &[0x90], 1);
                assert_ne!(key(&fast), key(&accurate));
                assert_ne!(crate::publisher::context_tag_of(fast.code_context()), crate::publisher::context_tag_of(accurate.code_context()));
                // Accurate mode on an AFP host, and fast mode anywhere, send and set nothing of the mode.
                let on_afp = Cpu { no_afp: 0, accurate_fp: 1, ..cpu };
                assert_eq!(on_afp.code_context() >> 32, 0);
                assert!(!x87_args(&on_afp, afp).iter().any(|x| x == "--accurate"));
            }
        }
    }

    /// A CPL-3 CPU at `rip` as the fetch tests use.
    fn test_cpu(rip: u64) -> Cpu {
        Cpu {
            gpr: [0; 16],
            rip,
            rflags: 2,
            cr0: 0,
            cr3: 0,
            cr4: 0,
            efer: 0,
            cpl: 3,
            cr2: 0,
            cr8: 0,
            dr: exec::DR_RESET,
            seg: exec::flat_segments(3),
            ldtr: exec::LDTR_RESET,
            tr: exec::TR_RESET,
            gdtr: exec::TABLE_RESET,
            idtr: exec::TABLE_RESET,
            mov_ss: 0,
            xmm: [[0; 2]; 16],
            mxcsr: exec::MXCSR_RESET,
            fcw: exec::FCW_INIT,
            xcr0: 1,
            no_afp: 0,
            accurate_fp: 0,
            stack_traps: 0,
        }
    }

    /// The template pool through the request path (`translation`, with the real fexc, `FEXC`): a template request is
    /// published after its block is staged and its context's gating values are noted; a step, a block cut at a limit and a block
    /// whose answer read to the end of the bytes are not published (a step is not even noted); a context whose gating
    /// differs turns the pool off, or is a host error under the check mode.
    #[test]
    fn translation_publishes_templates_only_and_notes_gating() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let memory = Memory::with_size(exec::BACKING_BYTES + (2 << 20)).unwrap();
        let ram =
            unsafe { GuestRam::new(0, 2 << 20, memory.0 as u64 + exec::BACKING_BYTES as u64) };
        let area = Area {
            memory: &memory,
            vcpu: 0,
            vcpus: 1,
            epoch: 0,
            base: 0,
            ram,
        };
        let mut code = vec![0x48, 0x01, 0xd8, 0xc3];
        code.resize(64, 0x90);
        assert!(ram.copy_in(0x5000, &code) && ram.copy_in(0x5ffc, &code[..4]));
        let fresh = || {
            let layout = natemu_abi::pool::Layout {
                index_slots: 64,
                ring_bytes: 1 << 20,
                control_bytes: 128,
            };
            let m = Arc::new(Memory::with_size(layout.span_bytes()).unwrap());
            let p = Arc::new(crate::publisher::Publisher::with_layout(m, 0, layout));
            let mut c = Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
            c.pool = Some(p.clone());
            (c, p)
        };
        let published = |p: &crate::publisher::Publisher| {
            let r: serde_json::Value =
                serde_json::from_str(p.report().strip_prefix("pool ").unwrap()).unwrap();
            r["published"].as_u64().unwrap()
        };
        let request = |c: &mut Compiler, rip: u64, step: bool, stop_at: Option<u64>| {
            let mut s = State::new(0, test_cpu(rip));
            s.fetch = [0x5000, u64::MAX];
            let reply = translation(area, c, &mut s, step, stop_at, &mut Requests::default(), &mut ());
            (reply.unwrap().unwrap(), s.cpu.code_context())
        };
        // A template: published, its context noted.
        let (mut c, p) = fresh();
        let (reply, context) = request(&mut c, 0x40_1000, false, None);
        assert_eq!(reply, exec::REPLY_ADMITTED);
        assert_eq!(published(&p), 1);
        assert!(p.gating_of(context).is_some(), "a block request notes its context's gating");
        // A step request (its gating values are another vector's) is neither noted nor published.
        let (mut c, p) = fresh();
        let (reply, context) = request(&mut c, 0x40_1000, true, None);
        assert_eq!(reply, exec::REPLY_ADMITTED);
        assert_eq!((published(&p), p.gating_of(context)), (0, None));
        // A block cut at a limit (the stop RIP inside its bytes) is no template; the request is still noted.
        let (mut c, p) = fresh();
        let (reply, context) = request(&mut c, 0x40_1000, false, Some(0x40_1008));
        assert_eq!(reply, exec::REPLY_ADMITTED);
        assert_eq!(published(&p), 0);
        assert!(p.gating_of(context).is_some());
        // The answer read to the end of the bytes the walk gave (the block ends at its page's end, the next not walked).
        let (mut c, p) = fresh();
        let (reply, _) = request(&mut c, 0x40_1ffc, false, None);
        assert_eq!(reply, exec::REPLY_ADMITTED);
        assert_eq!(published(&p), 0);
        // The same request, the first template again: a hit published once (by whichever request reaches it first).
        let (mut c, p) = fresh();
        request(&mut c, 0x40_1000, false, None);
        request(&mut c, 0x50_1000, false, None);
        assert_eq!((published(&p), c.hits), (1, 1));
        // M2 reuse: 32-bit code's templates are published too, under their own context (the code mode is in it), once for
        // every x87 guess; a unit compiled for a guess (x87 code, whatever the guess) is no template.
        let request32 = |c: &mut Compiler, code: &[u8], rip: u64, guess: u64| {
            let mut bytes = code.to_vec();
            bytes.resize(64, 0x90);
            assert!(ram.copy_in(0x6000, &bytes));
            let mut cpu = test_cpu(rip);
            let cs = &mut cpu.seg[exec::SREG_CS].attributes;
            *cs = *cs & !exec::ATTR_L | exec::ATTR_D;
            let mut s = State::new(0, cpu);
            (s.fetch, s.x87_guess) = ([0x6000, u64::MAX], guess);
            let reply = translation(area, c, &mut s, false, None, &mut Requests::default(), &mut ());
            assert_eq!(reply.unwrap().unwrap(), exec::REPLY_ADMITTED);
            (s.cpu.code_context(), s.relocatable)
        };
        let (mut c, p) = fresh();
        let plain = [0x40, 0x01, 0xd8, 0xc3]; // inc eax; add eax, ebx; ret
        let x87 = [0xd9, 0xe8, 0xdd, 0x1b, 0xc3]; // fld1; fstp qword [ebx]; ret
        let (context32, template) = request32(&mut c, &plain, 0x5655_6000, 0x1000);
        assert_eq!((published(&p), template, c.compiles), (1, 1, 1));
        assert_ne!(context32, test_context());
        assert_eq!(request32(&mut c, &plain, 0xf7c5_6000, 0x1701).1, 1);
        assert_eq!((published(&p), c.compiles, c.relocated), (1, 1, 1));
        for guess in [0x1000, 0] {
            assert_eq!(request32(&mut c, &x87, 0x5655_6000, guess).1, 0, "guess {guess:#x}");
        }
        assert_eq!((published(&p), c.compiles), (1, 3));
        assert!(p.gating_of(context32).is_some() && !p.is_off(), "one gating vector for the context under every guess");
        // Another vector under a context the pool knows: off (the reply is still the block); a host error under the check.
        let (mut c, p) = fresh();
        let (_, context) = request(&mut c, 0x40_1000, false, None);
        assert!(p.note(context, &[1; GATING]) == false && p.is_off());
        let (mut c, p) = fresh();
        let context = test_context();
        assert!(p.note(context, &[1; GATING]));
        c.check_reuse = true;
        let mut s = State::new(0, test_cpu(0x40_1000));
        s.fetch = [0x5000, u64::MAX];
        let e = translation(area, &mut c, &mut s, false, None, &mut Requests::default(), &mut ())
            .err()
            .unwrap();
        assert!(e.contains("pool: gating values"), "{e}");
        c.check_reuse = false;
        c.noted.clear();
        let (reply, _) = request(&mut c, 0x40_1000, false, None);
        assert_eq!(reply, exec::REPLY_ADMITTED);
        assert!(p.is_off());
        assert_eq!(published(&p), 0);
    }
    fn test_context() -> u64 {
        test_cpu(0).code_context()
    }

    /// The pool's backing with guest RAM above the PC hole: the high alias is mapped at the shifted offset too, and the span
    /// is before all RAM (only touched pages are ever backed).
    #[test]
    fn high_guest_ram_follows_the_pool_too() {
        let path =
            std::env::temp_dir().join(format!("natemu-pool-high-{}.elf", std::process::id()));
        std::fs::write(
            &path,
            crate::memory::tests::resident_elf(&[(crate::PRIVATE, 4)]),
        )
        .unwrap();
        let ram = exec::LOW_RAM_END as usize + (2 << 20);
        for pool in [false, true] {
            let b = prepare_with_pool(path.to_str().unwrap(), cache::CODE_BYTES, 1, ram, pool)
                .unwrap();
            let span = if pool { cache::POOL_BYTES } else { 0 };
            let ram_offset = b.stride + span;
            assert_eq!(b.memory.1, ram_offset + exec::ram_end(ram as u64) as usize);
            // The high alias's first block: the third GiB table is RAM's low part, the high part starts 4 GiB up.
            let high = (exec::GUEST_RAM_VA - PRIVATE + exec::HIGH_RAM_START) as usize;
            let d = u64::from_le_bytes(b.memory.read::<8>(
                exec::RAM_TABLES_OFFSET + ((high >> 30) - 1) * 4096 + (high % (1 << 30)) / cache::BLOCK * 8,
            ));
            assert_eq!(
                d & 0x0000_ffff_ffe0_0000,
                crate::IPA + (ram_offset + exec::HIGH_RAM_START as usize) as u64
            );
            // And the host's own view: a page written through `ram` above the hole lands there.
            assert!(b.ram.copy_in(exec::HIGH_RAM_START, &[0x5a; 8]));
            assert_eq!(
                b.memory.read::<8>(ram_offset + exec::HIGH_RAM_START as usize),
                [0x5a; 8]
            );
        }
        std::fs::remove_file(path).unwrap();
    }

    /// `Backing::machine`'s table rule: no region publishes nothing (`seq` stays 0, the default launch); regions publish
    /// each entry disabled with its alias and length, so `seq` is nonzero before any vCPU runs.
    #[test]
    fn regions_are_published_before_any_vcpu() {
        let table: Box<cache::MappedRegions> = Box::new(unsafe { std::mem::zeroed() });
        publish_disabled(&table, &crate::vm::mapping_plan(1 << 30, &[]));
        assert_eq!(table.read(), (0, [cache::MappedRegion::default(); exec::MAX_MAPPED_REGIONS]));
        let plan = crate::vm::mapping_plan(1 << 30, &[2 << 20, 4 << 20]);
        publish_disabled(&table, &plan);
        let (seq, entries) = table.read();
        assert_ne!(seq, 0);
        for (i, e) in entries.iter().enumerate() {
            let want = plan.aliases.get(i).map_or(cache::MappedRegion::default(), |&(alias, len)| cache::MappedRegion {
                base: 0,
                len,
                alias,
                enabled: 0,
            });
            assert_eq!(*e, want);
        }
    }

    /// The mapped regions' alias (`exec::MAPPED_VA`, the const assert's bound: up to `MAX_MAPPED_BYTES` in the 3-4 GiB
    /// hole's slots, below the firmware image's) in a backing with RAM above 3 GiB and the largest firmware image: no RAM
    /// or firmware alias entry lies in its slots before it is written, and writing it changes no other entry of any
    /// window's alias tables; each of its entries is an EL1 read/write, never executable block onto the region's IPA.
    #[test]
    fn alias_fits_the_hole_slots() {
        let path = std::env::temp_dir().join(format!("natemu-mapped-alias-{}.elf", std::process::id()));
        std::fs::write(&path, crate::memory::tests::resident_elf(&[(crate::PRIVATE, 4)])).unwrap();
        let ram = exec::LOW_RAM_END as usize + cache::BLOCK;
        let image = vec![0xcc; exec::MAX_FIRMWARE_BYTES as usize];
        let b = prepare_with_firmware(path.to_str().unwrap(), cache::CODE_BYTES, 2, ram, false, &image).unwrap();
        std::fs::remove_file(path).unwrap();
        let plan = crate::vm::mapping_plan(IPA + b.memory.1 as u64, &[exec::MAX_MAPPED_BYTES / 8; 8]);
        let tables = exec::RAM_TABLES_OFFSET..exec::RAM_TABLES_OFFSET + (exec::PRIVATE_GIBS - 1) * 4096;
        // The table word of private offset `at` (2 MiB blocks, one level-2 table per GiB after the first).
        let word = |at: usize| exec::RAM_TABLES_OFFSET + ((at >> 30) - 1) * 4096 + (at % (1 << 30)) / cache::BLOCK * 8;
        let slots: Vec<usize> = plan
            .aliases
            .iter()
            .flat_map(|&(va, len)| ((va - PRIVATE) as usize..(va - PRIVATE + len) as usize).step_by(cache::BLOCK))
            .collect();
        assert_eq!(slots.len() * cache::BLOCK, exec::MAX_MAPPED_BYTES as usize);
        let read = |window: usize, at: usize| u64::from_le_bytes(b.memory.read::<8>(window + at));
        let snapshot = |window: usize| -> Vec<u64> { tables.clone().step_by(8).map(|at| read(window, at)).collect() };
        let before: Vec<Vec<u64>> = (0..b.n).map(|v| snapshot(v * b.stride)).collect();
        let firmware = word((exec::GUEST_RAM_VA - PRIVATE + exec::FIRMWARE_END - exec::MAX_FIRMWARE_BYTES) as usize);
        for v in 0..b.n {
            let window = v * b.stride;
            assert_ne!(read(window, firmware), 0, "the firmware alias is there");
            assert_ne!(read(window, word((exec::GUEST_RAM_VA - PRIVATE + exec::HIGH_RAM_START) as usize)), 0, "high RAM");
            for &at in &slots {
                assert_eq!(read(window, word(at)), 0, "a RAM or firmware alias entry at {at:#x}");
                assert!(word(at) < firmware);
            }
        }
        b.map_mapped(&plan);
        let (pxn, uxn, ap) = (1u64 << 53, 1u64 << 54, 3u64 << 6);
        for v in 0..b.n {
            let window = v * b.stride;
            let mut expected = before[v].clone();
            for (k, &at) in slots.iter().enumerate() {
                let d = read(window, word(at));
                assert_eq!((d & 3, d & (pxn | uxn), d & ap), (1, pxn | uxn, 0), "{d:#x}");
                assert_eq!(d & 0x0000_ffff_ffe0_0000, plan.maps[0].0 + (k * cache::BLOCK) as u64);
                expected[(word(at) - tables.start) / 8] = d;
            }
            assert_eq!(snapshot(window), expected, "window {v}: only the region slots changed");
        }
    }

    /// The legacy ROM page (SMBIOS's entry point at F0000H) in a prepared backing: the bytes land in the hole's backing,
    /// where the resident's alias of guest-physical F0000H (the address its read-only shadows take, `mmio::rom`) reads
    /// them back through the window's own block descriptor, while the host's guest RAM still leaves the hole out; every
    /// vCPU's `Control::legacy_rom` names the page. Refused outside the hole, twice, or with a firmware image.
    #[test]
    fn legacy_rom_reads_back_through_the_resident_alias() {
        let path = std::env::temp_dir().join(format!("natemu-legacy-rom-{}.elf", std::process::id()));
        std::fs::write(&path, crate::memory::tests::resident_elf(&[(crate::PRIVATE, 4)])).unwrap();
        let resident = path.to_str().unwrap();
        let entry: Vec<u8> = (0..24u8).map(|k| k.wrapping_mul(37) ^ 0x5f).collect();
        for (n, pool) in [(1, false), (2, true)] {
            let b = prepare_with_pool(resident, cache::CODE_BYTES, n, 4 << 20, pool).unwrap();
            for v in 0..n {
                let c: cache::Control = b.vcpu(v).read_record(cache::LOOKUP_OFFSET + cache::CONTROL);
                assert_eq!(c.legacy_rom, (0, 0), "none until written");
            }
            b.write_legacy_rom(0xf_0000, &entry);
            for v in 0..n {
                let window = v * b.stride;
                let c: cache::Control = b.vcpu(v).read_record(cache::LOOKUP_OFFSET + cache::CONTROL);
                assert_eq!(c.legacy_rom, (0xf_0000, 0xf_1000));
                // The alias address the resident shadows for gpa F0000H: GUEST_RAM_VA + F0000H, in the window's third GiB
                // (its level-2 table, `RAM_TABLES_OFFSET` + 4096), block 0. Its output address is the backing's IPA.
                let alias = (exec::GUEST_RAM_VA - PRIVATE) as usize + 0xf_0000;
                let table = exec::RAM_TABLES_OFFSET + ((alias >> 30) - 1) * 4096;
                let d = u64::from_le_bytes(b.memory.read::<8>(window + table + (alias % (1 << 30)) / cache::BLOCK * 8));
                assert_eq!(d & 3, 1, "a block descriptor");
                let at = (d & 0x0000_ffff_ffe0_0000) - crate::IPA + (alias % cache::BLOCK) as u64;
                assert_eq!(b.memory.read::<24>(at as usize).to_vec(), entry);
                assert_eq!(b.memory.read::<8>(at as usize + 24), [0; 8], "the rest of the page is zero");
            }
            // The host's guest RAM (loaders, devices) cannot reach the hole; nor can the readable view.
            assert_eq!((b.ram.range(0xf_0000, 1), b.readable.range(0xf_0000, 1)), (None, None));
            let again = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| b.write_legacy_rom(0xf_0000, &entry)));
            assert!(again.is_err(), "a second range");
        }
        let b = prepare_with_pool(resident, cache::CODE_BYTES, 1, 4 << 20, false).unwrap();
        for (gpa, len) in [(0x9_f000, 0x2000), (0xf_f000, 0x1001), (0x10_0000, 1), (0xf_0000, 0)] {
            let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| b.write_legacy_rom(gpa, &vec![1; len])));
            assert!(r.is_err(), "{gpa:#x}+{len:#x}");
        }
        let fw = prepare_with_firmware(resident, cache::CODE_BYTES, 1, 4 << 20, false, &[0xcc; 4096]).unwrap();
        assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| fw.write_legacy_rom(0xf_0000, &entry))).is_err());
        std::fs::remove_file(path).unwrap();
    }

    /// Diagnostic (not a gate): how often the resident's placement re-check ([`natemu_abi::execution::relocate_code`]) refuses a
    /// pool entry, by reason and by how far the RIP moves, over the blocks of a real x86-64 program. Needs `FEXC` and `SURVEY_ELF`
    /// (a static x86-64 ELF, e.g. busybox); `SURVEY_BLOCKS` blocks (default 20000):
    /// `FEXC=... SURVEY_ELF=... cargo test -p natemu --release pool_relocation_survey -- --ignored --nocapture`.
    #[test]
    #[ignore]
    fn pool_relocation_survey() {
        use natemu_abi::execution::{LinkPoint, MAX_LINK_POINTS, link_point, pack_reloc, refusal, relocate_code};
        let (Some(fexc), Some(elf)) = (std::env::var_os("FEXC"), std::env::var_os("SURVEY_ELF")) else {
            return;
        };
        let data = std::fs::read(elf).unwrap();
        let u = |at: usize, n: usize| u64::from_le_bytes({
            let mut b = [0; 8];
            b[..n].copy_from_slice(&data[at..at + n]);
            b
        });
        // The executable PT_LOAD.
        let (phoff, phnum) = (u(32, 8) as usize, u(56, 2) as usize);
        let (mut text, mut vaddr) = (&data[..0], 0u64);
        for i in 0..phnum {
            let h = phoff + 56 * i;
            if u(h, 4) == 1 && u(h + 4, 4) & 1 != 0 {
                let (off, va, sz) = (u(h + 8, 8) as usize, u(h + 16, 8), u(h + 32, 8) as usize);
                if sz > text.len() {
                    (text, vaddr) = (&data[off..off + sz], va);
                }
            }
        }
        let blocks: usize = std::env::var("SURVEY_BLOCKS").ok().and_then(|v| v.parse().ok()).unwrap_or(20000);
        let mut c = Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        c.cpl3 = true;
        // Moves: [same RIP, +1 page, a library-like base, a PIE-like base]; per refusal kind and the reply checks.
        let moves = |from: u64| [from, from + 0x1000, 0x7f12_3440_0000 + (from & 0xf_ffff), 0x5555_5555_4000 + (from & 0xfff) + (from & 0xf_f000)];
        let names = ["same", "+1 page", "lib base", "PIE base"];
        let mut refused = [[0u64; 6]; 4]; // by `refusal::*` (1 place, 2 literal, 3 move, 4 link); [5] the total
        let (mut seen, mut templates, mut reply_bad, mut no_move_link) = (0u64, 0u64, 0u64, 0u64);
        // (The program's text is compiled at a high base, as a PIE or a library runs: below 4 GiB FEX's RIP moves are the 32-bit
        // form, which relocation refuses.)
        let base = std::env::var("SURVEY_BASE").ok().and_then(|v| u64::from_str_radix(v.trim_start_matches("0x"), 16).ok()).unwrap_or(0x7f12_3440_0000);
        let (vaddr, end) = (base, base + text.len() as u64);
        let mut rip = vaddr;
        while seen < blocks as u64 && rip + 16 < end {
            let at = (rip - vaddr) as usize;
            let bytes = &text[at..text.len().min(at + 8192)];
            let code = match c.compile(bytes, rip, false, &mut ()) {
                Ok(Code::Block(b)) => b,
                Ok(Code::System(s)) => {
                    rip += u64::from(s.length.max(1));
                    continue;
                }
                _ => {
                    rip += 1;
                    continue;
                }
            };
            seen += 1;
            let next = rip + u64::from(code.guest_bytes);
            let last = rip.wrapping_add(u64::from(code.guest_bytes).max(1) - 1) >> 12;
            let template = !c.halved
                && code.end_reason != natemu_abi::code_object::end_reason::UNSUPPORTED_FORM
                && c.read < bytes.len()
                && rip.wrapping_add(c.read.max(1) as u64 - 1) >> 12 <= last;
            if template {
                templates += 1;
                let o = &code.object;
                let h: natemu_abi::code_object::Header = crate::relocation::read(o, 0).unwrap();
                let code_bytes = &o[h.code_offset as usize..][..h.code_size as usize];
                let mut relocs = vec![];
                let mut packable = h.relocs_count <= cache::MAX_RELOCS;
                for i in 0..h.relocs_count as usize {
                    let r: natemu_abi::code_object::Reloc = crate::relocation::read(o, h.relocs_offset as usize + 16 * i).unwrap();
                    match pack_reloc(&r, h.code_size) {
                        Some(p) => relocs.extend(p),
                        None => packable = false,
                    }
                }
                relocs.sort();
                let mut links0 = [LinkPoint::NONE; MAX_LINK_POINTS];
                links0[..code.links.len()].copy_from_slice(&code.links);
                let word = |at: u64| u32::from_le_bytes(code_bytes[at as usize..at as usize + 4].try_into().unwrap());
                // The resident's reply checks on the entry as published.
                if code.links.iter().any(|p| !link_point(word, h.code_size, p, false))
                    || code.dispatch_exit.is_some_and(|d| word(u64::from(d)) != natemu_abi::execution::DISPATCH_PLACEHOLDER)
                {
                    reply_bad += 1;
                }
                // Every link point has a move relocation at its offset (relocate_code requires it).
                if code.links.iter().any(|p| !relocs.iter().any(|&r| r & natemu_abi::execution::RELOC_MOVE != 0 && (r >> 6) * 4 == p.offset)) {
                    no_move_link += 1;
                }
                if packable {
                    for (m, to) in moves(rip).into_iter().enumerate() {
                        let mut code2 = code_bytes.to_vec();
                        let mut links = links0;
                        refused[m][5] += 1;
                        if let Err(k) = relocate_code(&mut code2, &relocs, &mut links, rip, to, code.guest_bytes) {
                            refused[m][k.min(4)] += 1;
                        }
                    }
                }
            }
            rip = next.max(rip + 1);
        }
        eprintln!("{seen} blocks, {templates} templates, {reply_bad} fail the reply checks as published, {no_move_link} with a link point lacking a move relocation");
        for (m, r) in refused.iter().enumerate() {
            eprintln!(
                "{:9}: of {} relocated: place {} literal {} move {} link {}",
                names[m], r[5], r[refusal::PLACE], r[refusal::LITERAL], r[refusal::MOVE], r[refusal::LINK]
            );
        }
    }

    /// Diagnostic (not a gate): of the blocks of a real i386 program compiled as 32-bit user code where a library runs,
    /// how many templates the resident would share position-independent ([`natemu_abi::pic::eligible`] of 32-bit code;
    /// its refusals by kind; `share`'s other refusal, the x87 run test), and fexc's code bytes. Needs `FEXC` and
    /// `SURVEY_ELF` (an i386 ELF, e.g. the i386 campaign image's libc.so.6); `SURVEY_BLOCKS` blocks (default 20000) from
    /// `SURVEY_BASE` (default 0xf7c00000) on; with `NATEMU_FEXC_RECORD` set its requests are a replay stream
    /// (tools/fexc/replay.py --stream, --old32):
    /// `FEXC=... SURVEY_ELF=... cargo test -p natemu --release pic32_survey -- --ignored --nocapture`.
    #[test]
    #[ignore]
    fn pic32_survey() {
        use natemu_abi::execution::{LinkPoint, MAX_LINK_POINTS, pack_reloc, run_test_end};
        use natemu_abi::pic::{self, refusal};
        let (Some(fexc), Some(elf)) = (std::env::var_os("FEXC"), std::env::var_os("SURVEY_ELF")) else {
            return;
        };
        let data = std::fs::read(elf).unwrap();
        let u = |at: usize, n: usize| u64::from_le_bytes({
            let mut b = [0; 8];
            b[..n].copy_from_slice(&data[at..at + n]);
            b
        });
        assert_eq!((&data[..4], data[4], u(18, 2)), (&b"\x7fELF"[..], 1, 3), "an i386 ELF");
        // The largest executable PT_LOAD (ELF32 program headers: 32 bytes each).
        let (phoff, phnum) = (u(28, 4) as usize, u(44, 2) as usize);
        let (mut text, mut vaddr) = (&data[..0], 0u64);
        for i in 0..phnum {
            let h = phoff + 32 * i;
            if u(h, 4) == 1 && u(h + 24, 4) & 1 != 0 && u(h + 16, 4) as usize > text.len() {
                let (off, va, sz) = (u(h + 4, 4) as usize, u(h + 8, 4), u(h + 16, 4) as usize);
                (text, vaddr) = (&data[off..off + sz], va);
            }
        }
        let blocks: u64 = std::env::var("SURVEY_BLOCKS").ok().and_then(|v| v.parse().ok()).unwrap_or(20000);
        let base = std::env::var("SURVEY_BASE").ok().and_then(|v| u64::from_str_radix(v.trim_start_matches("0x"), 16).ok()).unwrap_or(0xf7c0_0000);
        let mut c = Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        (c.cpl3, c.bits32) = (true, true);
        let (start, end) = (base + vaddr, base + vaddr + text.len() as u64);
        assert!(end >> 32 == 0, "the text ends at {end:#x}");
        let (mut rip, mut seen, mut refused_blocks, mut templates, mut packed, mut run_tested) = (start, 0u64, 0u64, 0u64, 0u64, 0u64);
        let (mut code_bytes, mut moves, mut eligible) = (0u64, 0u64, [0u64; 8]);
        while seen < blocks && rip + 16 < end {
            let at = (rip - start) as usize;
            let bytes = &text[at..text.len().min(at + 8192)];
            let code = match c.compile(bytes, rip, false, &mut ()) {
                Ok(Code::Block(b)) => b,
                Ok(Code::System(s)) => {
                    rip += u64::from(s.length.max(1));
                    continue;
                }
                Err(e) => {
                    refused_blocks += 1;
                    if refused_blocks <= 5 {
                        eprintln!("{rip:#x}: {e}");
                    }
                    rip += 1;
                    continue;
                }
                _ => {
                    rip += 1;
                    continue;
                }
            };
            seen += 1;
            let h: natemu_abi::code_object::Header = crate::relocation::read(&code.object, 0).unwrap();
            let body = &code.object[h.code_offset as usize..][..h.code_size as usize];
            code_bytes += u64::from(h.code_size);
            let next = rip + u64::from(code.guest_bytes);
            let last = rip.wrapping_add(u64::from(code.guest_bytes).max(1) - 1) >> 12;
            let template = !c.halved
                && !c.guess_read
                && code.end_reason != natemu_abi::code_object::end_reason::UNSUPPORTED_FORM
                && c.read < bytes.len()
                && rip.wrapping_add(c.read.max(1) as u64 - 1) >> 12 <= last;
            let mut relocs = vec![];
            let mut packable = h.relocs_count <= cache::MAX_RELOCS && code.links.len() <= MAX_LINK_POINTS;
            for i in 0..h.relocs_count as usize {
                let r: natemu_abi::code_object::Reloc = crate::relocation::read(&code.object, h.relocs_offset as usize + 16 * i).unwrap();
                moves += u64::from(r.kind == natemu_abi::code_object::RELOC_GUEST_RIP_MOVE);
                match pack_reloc(&r, h.code_size) {
                    Some(p) => relocs.extend(p),
                    None => packable = false,
                }
            }
            relocs.sort();
            if template {
                templates += 1;
            }
            if template && packable {
                packed += 1;
                let word = |o: u64| u32::from_le_bytes(body[o as usize..o as usize + 4].try_into().unwrap());
                let mut links = [LinkPoint::NONE; MAX_LINK_POINTS];
                links[..code.links.len()].copy_from_slice(&code.links);
                if run_test_end(word, u64::from(h.code_size)).is_some() {
                    run_tested += 1;
                } else {
                    let k = pic::eligible(body, &relocs, &links, rip, true).err().unwrap_or(0);
                    eligible[k] += 1;
                }
            }
            rip = next.max(rip + 1);
        }
        eprintln!(
            "{seen} blocks ({refused_blocks} refused), {code_bytes} code bytes, {moves} guest-RIP moves; {templates} templates, {packed} with \
             their relocations packed: {} shareable, {run_tested} with the x87 run test, refused {} for a move ({} out of reach, {} one word)",
            eligible[0], eligible[refusal::MOVE], eligible[refusal::REACH], eligible[refusal::ONE_WORD]
        );
    }

    /// The bytes come only from the pages the resident walked: RIP's page to its end (more may follow until the next
    /// page is walked or its walk failed), on through the next page, never past the limit; a failed walk of RIP's page
    /// is the fetch fault stop.
    #[test]
    fn fetched_bytes_follow_the_resident_walk() {
        let memory = Memory::with_size(exec::BACKING_BYTES + (2 << 20)).unwrap();
        let ram =
            unsafe { GuestRam::new(0, 2 << 20, memory.0 as u64 + exec::BACKING_BYTES as u64) };
        assert!(ram.copy_in(0x5ff0, &[0xaa; 16]) && ram.copy_in(0x9000, &[0xbb; 16]));
        let area = Area {
            memory: &memory,
            vcpu: 0,
            vcpus: 1,
            epoch: 0,
            base: 0,
            ram,
        };
        let mut s = State::new(
            0,
            Cpu {
                gpr: [0; 16],
                rip: 0x1ff0,
                rflags: 2,
                cr0: 0,
                cr3: 0,
                cr4: 0,
                efer: 0,
                cpl: 3,
                cr2: 0,
                cr8: 0,
                dr: exec::DR_RESET,
                seg: exec::flat_segments(3),
                ldtr: exec::LDTR_RESET,
                tr: exec::TR_RESET,
                gdtr: exec::TABLE_RESET,
                idtr: exec::TABLE_RESET,
                mov_ss: 0,
                xmm: [[0; 2]; 16],
                mxcsr: exec::MXCSR_RESET,
                fcw: exec::FCW_INIT,
                xcr0: 1,
                no_afp: 0,
                accurate_fp: 0,
                stack_traps: 0,
            },
        );
        s.fetch = [0x5000, u64::MAX];
        let f = fetched(area, &s, None).unwrap().unwrap();
        assert_eq!(
            (f.bytes, f.more, f.next_fault),
            (vec![0xaa; 16], true, None)
        );
        let f = fetched(area, &s, Some(0x1ff8)).unwrap().unwrap();
        assert_eq!((f.bytes.len(), f.more), (8, false));
        s.fetch_fault = exec::fetch::encode(Fault::Page {
            address: 0x2000,
            error: 0x15,
        });
        let f = fetched(area, &s, None).unwrap().unwrap();
        assert_eq!(
            (f.more, f.next_fault),
            (
                false,
                Some(Fault::Page {
                    address: 0x2000,
                    error: 0x15
                })
            )
        );
        (s.fetch, s.fetch_fault) = ([0x5000, 0x9000], [exec::fetch::NONE; 3]);
        let f = fetched(area, &s, Some(0x2004)).unwrap().unwrap();
        assert_eq!(
            (&f.bytes[14..], f.more),
            (&[0xaa, 0xaa, 0xbb, 0xbb, 0xbb, 0xbb][..], false)
        );
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
        assert!(matches!(
            fetched(area, &s, None),
            Ok(Err(Stop::FetchFault(Fault::Noncanonical)))
        ));
        // The word copy (perf chunk 7) equals the byte copy at every alignment and length around a word, and refuses past RAM.
        let pattern: Vec<u8> = (0..64).map(|i| i as u8 ^ 0x5a).collect();
        assert!(ram.copy_in(0x7000, &pattern));
        for (at, n) in (0..9).flat_map(|a| (0..26).map(move |n| (a, n))) {
            let (mut words, mut bytes) = (vec![1], vec![0; n]);
            assert!(
                copy_out(&ram, 0x7000 + at, n as u64, &mut words)
                    && ram.copy_out(0x7000 + at, &mut bytes)
            );
            assert_eq!(words[1..], bytes[..]);
        }
        assert!(!copy_out(&ram, (2 << 20) - 4, 8, &mut vec![]));
    }

    #[test]
    fn host_writes_to_guest_ram_flush_every_vcpu() {
        let memory = Memory::with_size(exec::BACKING_BYTES).unwrap();
        guest_ram_written(&memory);
        assert!(
            (0..cache::MAX_VCPUS)
                .all(|v| record_word(&memory, v, None).load(SeqCst) & cache::FLUSH != 0)
        );
    }

    #[test]
    fn fexc_report_is_read_from_json() {
        let end = |json: &str| serde_json::from_str::<super::FexcReport>(json).map(|r| r.guest.end);
        let plain = end(r#"{"guest":{"detail":{"reason":"no"},"end":{"rip":"0x10","reason":"system_instruction","detail":"hlt"}}}"#).unwrap();
        assert_eq!(plain.reason, "system_instruction");
        assert!(
            plain.cut_gated.is_none() && plain.cut_ud.is_none() && plain.system_cut(0x10).is_none()
        );
        assert!(end("{}").is_err() && end(r#"{"guest":{"end":{}}}"#).is_err());
        let cut = end(r#"{"guest":{"end":{"rip":"0x10","reason":"system_instruction","detail":"x","cut_opcode_map":1,"cut_opcode":34}}}"#).unwrap();
        assert_eq!(
            (cut.system_cut(0x10), cut.system_cut(0x11)),
            (Some((1, 0x22)), None)
        );
        // As before: a present key counts even when null; an odd opcode only drops the cut.
        let odd = end(r#"{"guest":{"end":{"rip":"0x10","reason":"r","cut_ud":null,"cut_gated":null,"cut_opcode_map":1,"cut_opcode":300}}}"#).unwrap();
        assert!(
            odd.cut_ud.is_some()
                && odd
                    .cut_gated
                    .as_ref()
                    .is_some_and(|v| v.as_str() != Some("mmx"))
        );
        assert_eq!(odd.system_cut(0x10), None);
    }
}
