//! Host-owned guest RAM: allocation, byte/record transfer, the resident image and
//! the resident's private translation tables. Offsets are relative to the base of
//! the backing, which the VM maps at `IPA`; owner `n` uses window `n * RAM_SIZE`.
use super::*;

unsafe extern "C" {
    fn mmap(
        address: *mut c_void,
        length: usize,
        prot: i32,
        flags: i32,
        fd: i32,
        offset: i64,
    ) -> *mut c_void;
    fn munmap(address: *mut c_void, length: usize) -> i32;
    fn sys_icache_invalidate(start: *const c_void, length: usize);
}

/// Anonymous host backing for guest RAM. It must outlive the VM that maps it.
pub struct Memory(pub(crate) *mut u8, pub(crate) usize);
// SAFETY: the backing is shared with running vCPUs anyway; every accessor copies through raw pointers. Owner threads
// touch their own vCPU's records, and areas shared between vCPUs only through atomics.
unsafe impl Sync for Memory {}
// SAFETY: the mapping is not tied to the thread that made it; any thread may unmap it.
unsafe impl Send for Memory {}
impl Memory {
    #[cfg(test)]
    pub(crate) fn new() -> Result<Self> {
        Self::with_size(RAM_SIZE)
    }
    pub fn with_size(size: usize) -> Result<Self> {
        let p = unsafe { mmap(ptr::null_mut(), size, 3, 0x1002, -1, 0) };
        if p as isize == -1 {
            return Err(format!("mmap: {}", std::io::Error::last_os_error()));
        }
        Ok(Self(p.cast(), size))
    }
    pub(crate) fn copy(&self, offset: usize, bytes: &[u8]) {
        assert!(
            offset
                .checked_add(bytes.len())
                .is_some_and(|end| end <= self.1)
        );
        unsafe { ptr::copy_nonoverlapping(bytes.as_ptr(), self.0.add(offset), bytes.len()) };
    }
    pub(crate) fn word(&self, offset: usize, value: u64) {
        self.copy(offset, &value.to_le_bytes());
    }
    pub(crate) fn read<const N: usize>(&self, offset: usize) -> [u8; N] {
        assert!(offset + N <= self.1);
        let mut result = [0; N];
        unsafe { ptr::copy_nonoverlapping(self.0.add(offset), result.as_mut_ptr(), N) };
        result
    }
    /// Stores `value` at `offset`. `T` is a fixed-layout (`repr(C)`) ABI record shared
    /// with the resident; the value is moved into guest RAM and never dropped there.
    /// Call only while no vCPU of this VM runs.
    pub(crate) fn write_record<T>(&self, offset: usize, value: T) {
        self.check_record::<T>(offset);
        unsafe { ptr::write(self.0.add(offset).cast::<T>(), value) };
    }
    /// Loads the fixed-layout ABI record at `offset`, written by a stopped or joined vCPU.
    pub(crate) fn read_record<T: Copy>(&self, offset: usize) -> T {
        self.check_record::<T>(offset);
        unsafe { ptr::read_volatile(self.0.add(offset).cast::<T>()) }
    }
    fn check(&self, offset: usize, length: usize) {
        assert!(offset.checked_add(length).is_some_and(|end| end <= self.1));
    }
    // The backing is page-aligned, so offset alignment is pointer alignment.
    fn check_record<T>(&self, offset: usize) {
        self.check(offset, size_of::<T>());
        assert_eq!(offset % align_of::<T>(), 0, "misaligned record");
    }
    /// Makes host-written code in `[offset, offset + length)` visible to instruction fetch before any vCPU runs (only
    /// the windows' resident text: flushing the whole backing would touch every page of guest RAM).
    pub(crate) fn flush_instruction_cache(&self, offset: usize, length: usize) {
        self.check(offset, length);
        unsafe { sys_icache_invalidate(self.0.add(offset).cast(), length) };
    }
    /// Copies the loaded resident text from window 0 into another owner's window.
    pub(crate) fn replicate_resident_text(&self, window: usize) {
        for offset in (0..RAM_SIZE).step_by(4096) {
            if resident::executable_offset(offset) {
                self.copy(window + offset, &self.read::<4096>(offset));
            }
        }
    }
}
impl Drop for Memory {
    fn drop(&mut self) {
        unsafe {
            munmap(self.0.cast(), self.1);
        }
    }
}

fn u64_at(data: &[u8], offset: usize) -> Result<u64> {
    let bytes = data
        .get(offset..offset.checked_add(8).ok_or("ELF offset overflow")?)
        .ok_or("truncated ELF")?;
    Ok(u64::from_le_bytes(bytes.try_into().unwrap()))
}
fn u16_at(data: &[u8], offset: usize) -> Result<u16> {
    let bytes = data.get(offset..offset + 2).ok_or("truncated ELF")?;
    Ok(u16::from_le_bytes(bytes.try_into().unwrap()))
}
pub fn read_input(path: &str, max: usize) -> Result<Vec<u8>> {
    if fs::metadata(path)
        .map_err(|e| format!("{path}: {e}"))?
        .len()
        > max as u64
    {
        return Err(format!("{path}: oversized input"));
    }
    let bytes = fs::read(path).map_err(|e| format!("{path}: {e}"))?;
    if bytes.len() > max {
        return Err(format!("{path}: input grew beyond limit"));
    }
    Ok(bytes)
}
/// Reads the resident ELF at `path` and loads it into window 0.
pub(crate) fn load_resident(memory: &Memory, path: &str) -> Result<()> {
    load_elf(memory, &read_input(path, 2 * 1024 * 1024)?)
}
pub(crate) fn load_elf(memory: &Memory, data: &[u8]) -> Result<()> {
    if data.get(..7) != Some(b"\x7fELF\x02\x01\x01")
        || u16_at(data, 16)? != 2
        || u16_at(data, 18)? != 183
        || u64_at(data, 24)? != PRIVATE
    {
        return Err("expected static little-endian AArch64 ELF at private entry".into());
    }
    let phoff = usize::try_from(u64_at(data, 32)?).map_err(|_| "ELF offset")?;
    if u16_at(data, 54)? != 56 {
        return Err("unexpected ELF program header size".into());
    }
    let mut spans = Vec::new();
    for i in 0..u16_at(data, 56)? as usize {
        let at = phoff.checked_add(i * 56).ok_or("ELF offset overflow")?;
        let header = data
            .get(at..at.checked_add(56).ok_or("ELF header overflow")?)
            .ok_or("truncated program header")?;
        if u32::from_le_bytes(header[..4].try_into().unwrap()) != 1 {
            continue;
        }
        let file = u64_at(header, 8)?;
        let va = u64_at(header, 16)?;
        let filesz = u64_at(header, 32)?;
        let memsz = u64_at(header, 40)?;
        let end = va.checked_add(memsz).ok_or("ELF address overflow")?;
        let fileend = file.checked_add(filesz).ok_or("ELF file overflow")?;
        if u32::from_le_bytes(header[4..8].try_into().unwrap()) != 5
            || !resident::contains(va, memsz)
            || filesz > memsz
            || fileend > data.len() as u64
        {
            return Err("ELF segment outside resident text".into());
        }
        if spans.iter().any(|&(lo, hi)| va < hi && lo < end) {
            return Err("overlapping ELF segments".into());
        }
        spans.push((va, end));
        memory.copy(
            (va - PRIVATE) as usize,
            &data[file as usize..fileend as usize],
        );
    }
    if !spans.iter().any(|&(lo, hi)| lo == PRIVATE && hi > PRIVATE) {
        return Err("no resident entry segment".into());
    }
    Ok(())
}

/// Protection of a private alias: resident text is EL1 read-only and EL1-executable; generated code (the code area:
/// blocks, the step slot, the dispatcher and L1) is EL1 read-only and EL0 execute-only, so EL1 never executes it and
/// EL0 can neither read nor write it; everything else is EL1 read/write; none of it is executable but as said. No window
/// page is EL0 data: the guest-state page's EL0 alias lies outside the window ([`guest_state_alias`]).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Rights {
    Text,
    Code,
    Data,
}
/// AP 10 (EL1 read-only, no EL0 access), AP 01 (EL1 and EL0 read/write), PXN and UXN.
const READ_ONLY: u64 = 1 << 7;
const EL0_DATA: u64 = 1 << 6;
const PXN: u64 = 1 << 53;
const UXN: u64 = 1 << 54;
/// `bytes` of the resident's private address space at `PRIVATE + offset`,
/// backed by the backing bytes at `target`.
#[derive(Clone, Copy, Debug)]
pub(crate) struct Span {
    pub offset: usize,
    pub bytes: usize,
    pub target: usize,
    pub rights: Rights,
}
impl Span {
    pub(crate) const fn text(offset: usize, bytes: usize, target: usize) -> Self {
        Self {
            offset,
            bytes,
            target,
            rights: Rights::Text,
        }
    }
    pub(crate) const fn code(offset: usize, bytes: usize, target: usize) -> Self {
        Self {
            offset,
            bytes,
            target,
            rights: Rights::Code,
        }
    }
    pub(crate) const fn data(offset: usize, bytes: usize, target: usize) -> Self {
        Self {
            offset,
            bytes,
            target,
            rights: Rights::Data,
        }
    }
}
/// Private descriptors are not global: every owner maps the same private addresses to its own backing, so its
/// entries carry its ASID.
const PAGE: u64 = 3 | 1 << 10 | 1 << 11 | 3 << 8;
/// A 2 MiB block descriptor at level 2.
const BLOCK: u64 = 1 | 1 << 10 | 1 << 11 | 3 << 8;
const PRIVATE_LEAF: usize = 0x14000;
/// Level-2 table of `PRIVATE..PRIVATE + 1 GiB`; its first entry links `PRIVATE_LEAF`.
const PRIVATE_L2: usize = 0x13000;
/// Level-2 table of the private window's GiB `g` (`PRIVATE_L2`, then the guest-RAM alias's, M1).
fn private_l2(g: usize) -> usize {
    if g == 0 {
        PRIVATE_L2
    } else {
        natemu_abi::execution::RAM_TABLES_OFFSET + (g - 1) * 4096
    }
}
/// GiBs of the private window.
const PRIVATE_GIBS: usize = (natemu_abi::execution::PRIVATE_BYTES >> 30) as usize;

/// Private tables of the owner whose window starts at backing offset `window`:
/// TTBR0 (`shadow::LOWER_ROOT`) holds the boot identity alias, the private
/// region `PRIVATE..PRIVATE + 2 MiB` with exactly `spans`, and the lower guest
/// shadow window; with `upper`, TTBR1 (`shadow::UPPER_ROOT`) pre-links the
/// upper window. The user (CPL 3) shadow space's roots start empty but for the
/// private region, linked as in TTBR0. The runtime's table pages, shadow state
/// and shadow pool are always mapped.
pub(crate) fn private_space(memory: &Memory, window: usize, spans: &[Span], upper: bool) {
    let word = |at: usize, value| memory.word(window + at, value);
    let table = |offset: usize| IPA + (window + offset) as u64 + 3;
    word(shadow::LOWER_ROOT, table(0x11000));
    word(shadow::LOWER_ROOT + 256 * 8, table(0x12000));
    word(shadow::USER_LOWER_ROOT + 256 * 8, table(0x12000));
    // Low alias exists only for MMU enable and is removed by startup.
    let identity = IPA + window as u64;
    word(
        0x11000 + ((identity >> 30) as usize) * 8,
        (identity & !((1 << 30) - 1)) | 1 | 1 << 10 | 1 << 11 | 3 << 8,
    );
    word(0x11000, table(0x15000));
    word(0x15000, table(shadow::LOWER_LEAF));
    for g in 0..PRIVATE_GIBS {
        word(0x12000 + 8 * (1 + g), table(private_l2(g)));
    }
    word(PRIVATE_L2, table(PRIVATE_LEAF));
    if upper {
        word(shadow::UPPER_ROOT + 256 * 8, table(shadow::UPPER_L1));
        word(shadow::UPPER_L1, table(shadow::UPPER_L2));
        word(shadow::UPPER_L2, table(shadow::UPPER_LEAF));
    }
    let own = |offset, bytes| Span::data(offset, bytes, window + offset);
    let tables_end = if upper {
        shadow::UPPER_LEAF
    } else {
        shadow::LOWER_LEAF
    } + 4096;
    map_private(
        memory,
        window,
        &[own(shadow::LOWER_ROOT, tables_end - shadow::LOWER_ROOT)],
    );
    map_private(memory, window, spans);
    map_private(
        memory,
        window,
        &[
            own(shadow::STATE_OFFSET, 4096),
            own(
                shadow::USER_LOWER_ROOT,
                shadow::USER_UPPER_ROOT + 4096 - shadow::USER_LOWER_ROOT,
            ),
            own(shadow::POOL_OFFSET, shadow::POOL_PAGES * 4096),
        ],
    );
}

/// Maps `spans` into window `window`'s private region (replacing earlier entries): 4 KiB pages in its first 2 MiB,
/// 2 MiB blocks (offset, size and target 2 MiB aligned) above, up to `execution::PRIVATE_BYTES`.
pub(crate) fn map_private(memory: &Memory, window: usize, spans: &[Span]) {
    const BLOCK_BYTES: usize = 2 * RAM_SIZE;
    for span in spans {
        let protection = match span.rights {
            Rights::Text => READ_ONLY | UXN,
            Rights::Code => READ_ONLY | PXN,
            Rights::Data => PXN | UXN,
        };
        if span.offset >= BLOCK_BYTES {
            assert!((span.offset | span.bytes | span.target) % BLOCK_BYTES == 0);
            assert!(span.offset + span.bytes <= natemu_abi::execution::PRIVATE_BYTES as usize);
            for block in (0..span.bytes).step_by(BLOCK_BYTES) {
                let at = span.offset + block;
                memory.word(
                    window + private_l2(at >> 30) + (at % (1 << 30)) / BLOCK_BYTES * 8,
                    (IPA + (span.target + block) as u64) | BLOCK | protection,
                );
            }
            continue;
        }
        assert!(span.offset % 4096 == 0 && span.target % 4096 == 0);
        assert!(span.offset + span.bytes <= BLOCK_BYTES);
        for page in (0..span.bytes).step_by(4096) {
            memory.word(
                window + PRIVATE_LEAF + (span.offset + page) / 4096 * 8,
                (IPA + (span.target + page) as u64) | PAGE | protection,
            );
        }
    }
}

/// G1: the guest-state page's EL0 alias (the host-selected `va`, x28 of generated code), EL0 and EL1 read/write,
/// never executable, backed by window `window`'s page at backing offset `target` (its frame page): one upper-root entry of
/// both shadow spaces (an entry below 256, which no shadow uses) links the owner's own tables
/// (`execution::GUEST_STATE_TABLES`, never mapped in the window), whose only leaf is this page.
pub(crate) fn guest_state_alias(memory: &Memory, window: usize, target: usize, va: u64) {
    assert!(natemu_abi::execution::guest_state_alias_valid(va));
    let [l1, l2, leaf] = natemu_abi::execution::GUEST_STATE_TABLES;
    let index = |level: u32| (va >> (39 - 9 * level) & 511) as usize * 8;
    let word = |at: usize, value| memory.word(window + at, value);
    let table = |offset: usize| IPA + (window + offset) as u64 + 3;
    for root in [shadow::UPPER_ROOT, shadow::USER_UPPER_ROOT] {
        word(root + index(0), table(l1));
    }
    word(l1 + index(1), table(l2));
    word(l2 + index(2), table(leaf));
    word(
        leaf + index(3),
        (IPA + target as u64) | PAGE | EL0_DATA | PXN | UXN,
    );
}

/// The resident's boot and extended text in window `window`.
pub(crate) fn resident_text(window: usize) -> Vec<Span> {
    let text = |offset, end| Span::text(offset, end - offset, window + offset);
    vec![
        text(0, resident::BOOT_END),
        text(resident::TEXT_OFFSET, resident::TEXT_END),
    ]
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    #[test]
    fn malformed_elf_rejected_without_loading() {
        let memory = Memory::new().unwrap();
        for length in 0..64 {
            assert!(load_elf(&memory, &vec![0; length]).is_err());
        }
    }
    pub(crate) fn resident_elf(segments: &[(u64, u64)]) -> Vec<u8> {
        let payload = 64 + segments.len() * 56;
        let mut data = vec![0; payload + segments.len() * 4];
        data[..7].copy_from_slice(b"\x7fELF\x02\x01\x01");
        data[16..18].copy_from_slice(&2u16.to_le_bytes());
        data[18..20].copy_from_slice(&183u16.to_le_bytes());
        data[24..32].copy_from_slice(&PRIVATE.to_le_bytes());
        data[32..40].copy_from_slice(&64u64.to_le_bytes());
        data[54..56].copy_from_slice(&56u16.to_le_bytes());
        data[56..58].copy_from_slice(&(segments.len() as u16).to_le_bytes());
        for (i, &(address, size)) in segments.iter().enumerate() {
            let at = 64 + i * 56;
            data[at..at + 4].copy_from_slice(&1u32.to_le_bytes());
            data[at + 4..at + 8].copy_from_slice(&5u32.to_le_bytes());
            data[at + 8..at + 16].copy_from_slice(&((payload + i * 4) as u64).to_le_bytes());
            data[at + 16..at + 24].copy_from_slice(&address.to_le_bytes());
            data[at + 32..at + 40].copy_from_slice(&4u64.to_le_bytes());
            data[at + 40..at + 48].copy_from_slice(&size.to_le_bytes());
            data[payload + i * 4..payload + i * 4 + 4].fill(0x40 + i as u8);
        }
        data
    }

    #[test]
    fn resident_elf_loads_boot_and_extended_text() {
        let memory = Memory::new().unwrap();
        let data = resident_elf(&[
            (PRIVATE, resident::BOOT_END as u64),
            (
                PRIVATE + resident::TEXT_OFFSET as u64,
                (resident::TEXT_END - resident::TEXT_OFFSET) as u64,
            ),
        ]);
        load_elf(&memory, &data).unwrap();
        assert_eq!(memory.read::<4>(0), [0x40; 4]);
        assert_eq!(memory.read::<4>(resident::TEXT_OFFSET), [0x41; 4]);
        assert_eq!(memory.read::<4>(resident::BOOT_END), [0; 4]);
        assert_eq!(memory.read::<4>(resident::TEXT_END - 4), [0; 4]);
    }

    #[test]
    fn resident_elf_rejects_gaps_private_tables_and_overflow() {
        for (address, size) in [
            (PRIVATE, resident::BOOT_END as u64 + 1),
            (PRIVATE + resident::BOOT_END as u64, 4),
            (PRIVATE + 0x10000, 4096),
            (PRIVATE + resident::TEXT_OFFSET as u64 - 4, 8),
            (PRIVATE + resident::TEXT_END as u64 - 4, 8),
            (PRIVATE + resident::TEXT_END as u64, 4),
            (u64::MAX - 3, 8),
            (PRIVATE + resident::TEXT_OFFSET as u64, 0),
        ] {
            let memory = Memory::new().unwrap();
            let data = resident_elf(&[(PRIVATE, 4), (address, size)]);
            assert!(
                load_elf(&memory, &data).is_err(),
                "admitted {address:#x}+{size:#x}"
            );
        }
        let memory = Memory::new().unwrap();
        assert!(
            load_elf(
                &memory,
                &resident_elf(&[(PRIVATE + resident::TEXT_OFFSET as u64, 4)])
            )
            .is_err()
        );
        assert!(load_elf(&memory, &resident_elf(&[(PRIVATE, 8), (PRIVATE + 4, 4)])).is_err());
        for flags in [0u32, 4, 6, 7] {
            let mut data = resident_elf(&[(PRIVATE, 4)]);
            data[64 + 4..64 + 8].copy_from_slice(&flags.to_le_bytes());
            assert!(load_elf(&memory, &data).is_err());
        }
    }

    #[test]
    fn private_resident_mapping_has_no_executable_gap() {
        let memory = Memory::new().unwrap();
        let mut spans = resident_text(0);
        spans.push(Span::data(SHARED_OFFSET, 0x8000, SHARED_OFFSET));
        private_space(&memory, 0, &spans, false);
        let leaf = |offset| u64::from_le_bytes(memory.read::<8>(0x14000 + offset / 4096 * 8));
        for offset in (0..RAM_SIZE).step_by(4096) {
            let pte = leaf(offset);
            if resident::executable_offset(offset) {
                assert_eq!(pte, (IPA + offset as u64) | PAGE | READ_ONLY | UXN);
            } else if (resident::BOOT_END..0x10000).contains(&offset) {
                assert_eq!(pte, 0);
            } else if pte != 0 {
                assert_eq!(pte & (PXN | UXN | 3 << 6), PXN | UXN);
            }
        }
    }

    /// G1: the guest-state alias is one EL0 read/write page in both spaces' upper roots, at an upper-root entry below 256
    /// (no guest shadow's), and no window page is EL0 data (the old alias, `PRIVATE + 0x24000`, is unmapped).
    #[test]
    fn guest_state_alias_is_one_el0_page_outside_the_window() {
        use natemu_abi::execution::{FRAME_OFFSET, GUEST_STATE_SLOT, cache::MAX_VCPUS};
        for offset in [
            0,
            4096,
            (1 << 21) - 4096,
            1 << 21,
            (1 << 30) - 4096,
            1 << 30,
            (1 << 39) - 4096,
        ] {
            let va = GUEST_STATE_SLOT.start + offset;
            let memory = Memory::with_size(MAX_VCPUS * RAM_SIZE).unwrap();
            for v in 0..MAX_VCPUS {
                let window = v * RAM_SIZE;
                let mut spans = resident_text(window);
                spans.push(Span::data(FRAME_OFFSET, 4096, window + FRAME_OFFSET));
                private_space(&memory, window, &spans, true);
                guest_state_alias(&memory, window, window + FRAME_OFFSET, va);
                let word = |offset: usize| u64::from_le_bytes(memory.read::<8>(window + offset));
                let walk = |root: usize, va: u64| {
                    let mut table = root;
                    for level in 0..4 {
                        let entry = word(table + (va >> (39 - 9 * level) & 511) as usize * 8);
                        if level == 3 || entry & 1 == 0 {
                            return entry;
                        }
                        table = (entry & 0x0000_ffff_ffff_f000) as usize - IPA as usize - window;
                    }
                    unreachable!()
                };
                assert!((va >> 39 & 511) < 256);
                for root in [shadow::UPPER_ROOT, shadow::USER_UPPER_ROOT] {
                    assert_eq!(
                        walk(root, va),
                        (IPA + (window + FRAME_OFFSET) as u64) | PAGE | EL0_DATA | PXN | UXN
                    );
                    assert_eq!([walk(root, va - 4096), walk(root, va + 4096)], [0, 0]);
                }
                for offset in (0..RAM_SIZE).step_by(4096) {
                    assert_eq!(
                        word(PRIVATE_LEAF + offset / 4096 * 8) & EL0_DATA,
                        0,
                        "{offset:#x}"
                    );
                }
                assert_eq!(word(PRIVATE_LEAF + 0x24000 / 4096 * 8), 0);
            }
        }
    }

    #[test]
    fn spans_above_two_mib_map_as_level_two_blocks() {
        let memory = Memory::new().unwrap();
        let mib = RAM_SIZE;
        map_private(
            &memory,
            0,
            &[
                Span::text(0x1000_0000, 4 * mib, 6 * mib),
                Span::data(0x2000_0000, 2 * mib, 6 * mib),
                Span::code(0x3000_0000, 2 * mib, 6 * mib),
            ],
        );
        let l2 = |offset: usize| {
            u64::from_le_bytes(memory.read::<8>(PRIVATE_L2 + offset / (2 * mib) * 8))
        };
        assert_eq!(
            l2(0x1000_0000),
            (IPA + 6 * mib as u64) | BLOCK | READ_ONLY | UXN
        );
        assert_eq!(
            l2(0x1020_0000),
            (IPA + 8 * mib as u64) | BLOCK | READ_ONLY | UXN
        );
        assert_eq!(l2(0x2000_0000), (IPA + 6 * mib as u64) | BLOCK | PXN | UXN);
        // Generated code: EL1 read-only, EL0 execute-only (AP 10, UXN clear), never EL1-executable.
        assert_eq!(
            l2(0x3000_0000),
            (IPA + 6 * mib as u64) | BLOCK | READ_ONLY | PXN
        );
        assert_eq!(l2(0x1040_0000), 0);
        // M1: the guest-RAM alias's GiBs (1 to 3 of the window) have level-2 tables of their own; a span crossing a GiB
        // continues in the next table, and the last 2 MiB of the window is mappable.
        let gib = 1 << 30;
        map_private(
            &memory,
            0,
            &[
                Span::data(gib + gib - 2 * mib, 4 * mib, 0),
                Span::data(4 * gib - 2 * mib, 2 * mib, 0),
            ],
        );
        let ram = |g: usize, offset: usize| {
            u64::from_le_bytes(memory.read::<8>(private_l2(g) + offset / (2 * mib) * 8))
        };
        assert_eq!(ram(1, gib - 2 * mib), IPA | BLOCK | PXN | UXN);
        assert_eq!(ram(2, 0), (IPA + 2 * mib as u64) | BLOCK | PXN | UXN);
        assert_eq!(ram(3, gib - 2 * mib), IPA | BLOCK | PXN | UXN);
        assert_eq!(
            (PRIVATE_GIBS, private_l2(1), private_l2(3)),
            (37, 0x25000, 0x27000)
        );
    }

    #[test]
    fn startup_identity_follows_owner_backing_above_first_gib() {
        for window in [0, (1 << 30) - (2 << 20), 1 << 30, 17 << 30, 36 << 30] {
            let memory = Memory::with_size(window + 2 * RAM_SIZE).unwrap();
            private_space(&memory, window, &resident_text(window), true);
            let ipa = IPA + window as u64;
            let index = (ipa >> 30) as usize;
            let word = |i| u64::from_le_bytes(memory.read::<8>(window + 0x11000 + 8 * i));
            assert_eq!(word(index) & 0x0000_ffff_c000_0000, ipa & !((1 << 30) - 1));
            if index != 1 {
                assert_eq!(word(1), 0);
            }
        }
    }

    #[test]
    fn runtime_shadows_use_prelinked_windows_then_the_mapped_pool() {
        struct Quiet;
        impl shadow::Maintenance for Quiet {
            fn publish(&mut self) {}
            fn invalidate(&mut self, _: u64) {}
            fn invalidate_all(&mut self) {}
        }
        let memory = Memory::with_size(2 * RAM_SIZE).unwrap();
        let window = RAM_SIZE;
        private_space(&memory, window, &resident_text(window), true);
        let leaf = |offset: usize| {
            u64::from_le_bytes(memory.read::<8>(window + 0x14000 + offset / 4096 * 8))
        };
        // The runtime reaches its tables, state and pool through own-window RW aliases.
        for offset in [
            shadow::LOWER_ROOT,
            shadow::UPPER_LEAF,
            shadow::STATE_OFFSET,
            shadow::USER_LOWER_ROOT,
            shadow::USER_UPPER_ROOT,
        ]
        .into_iter()
        .chain((0..shadow::POOL_PAGES).map(|i| shadow::POOL_OFFSET + i * 4096))
        {
            assert_eq!(
                leaf(offset),
                (IPA + (window + offset) as u64) | PAGE | PXN | UXN
            );
        }
        // Both spaces' TTBR0 roots reach the private region through the same table.
        for root in [shadow::LOWER_ROOT, shadow::USER_LOWER_ROOT] {
            let entry = u64::from_le_bytes(memory.read::<8>(window + root + 256 * 8));
            assert_eq!(entry, IPA + (window + 0x12000) as u64 + 3);
        }
        let mut space = shadow::Space {
            window: unsafe { memory.0.add(window) },
            ipa: IPA + window as u64,
            pool_pages: shadow::POOL_PAGES,
            upper: true,
            user: false,
            tlb: Quiet,
        };
        for va in [0x10_0000, shadow::HIGH_BASE + 0x10_0000] {
            space.map(va, IPA, true).unwrap();
        }
        assert_eq!(space.used(), 0);
        let word = |offset: usize| u64::from_le_bytes(memory.read::<8>(window + offset));
        assert_eq!(
            word(shadow::LOWER_LEAF + 256 * 8),
            IPA | shadow::PAGE | shadow::NOT_GLOBAL
        );
        assert_eq!(
            word(shadow::UPPER_LEAF + 256 * 8),
            IPA | shadow::PAGE | shadow::NOT_GLOBAL
        );
        space.map(0x7fff_0000_0000, IPA, false).unwrap();
        assert_eq!(space.used(), 3);
        // Without `upper` (TCR.EPD1 set) the upper half is refused; the lower
        // half grows from the pool.
        let memory = Memory::new().unwrap();
        private_space(&memory, 0, &resident_text(0), false);
        let mut space = shadow::Space {
            window: memory.0,
            ipa: IPA,
            pool_pages: 2,
            upper: false,
            user: false,
            tlb: Quiet,
        };
        assert_eq!(
            space.map(shadow::HIGH_BASE, IPA, true),
            Err(shadow::Error::Noncanonical)
        );
        assert_eq!(
            space.map(0x7f80_0000_0000, IPA, true),
            Err(shadow::Error::Capacity)
        );
    }
}
