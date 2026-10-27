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
pub(crate) struct Memory(pub(crate) *mut u8, pub(crate) usize);
impl Memory {
    pub(crate) fn new() -> Result<Self> {
        Self::with_size(RAM_SIZE)
    }
    pub(crate) fn with_size(size: usize) -> Result<Self> {
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
    /// Makes host-written code visible to instruction fetch before any vCPU runs.
    pub(crate) fn flush_instruction_cache(&self) {
        unsafe { sys_icache_invalidate(self.0.cast(), self.1) };
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
pub(crate) fn read_input(path: &str, max: usize) -> Result<Vec<u8>> {
    if fs::metadata(path).map_err(|e| e.to_string())?.len() > max as u64 {
        return Err("oversized input".into());
    }
    let bytes = fs::read(path).map_err(|e| e.to_string())?;
    if bytes.len() > max {
        return Err("input grew beyond limit".into());
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

/// Single-owner private tables rooted at `shadow::LOWER_ROOT` in window 0.
pub(crate) fn page_tables(memory: &Memory) {
    let table = |offset| IPA + offset + 3;
    let attrs = 3 | 1 << 10 | 3 << 8;
    memory.word(0x10000, table(0x11000));
    memory.word(0x10000 + 256 * 8, table(0x12000));
    // Low alias exists only for MMU enable and is removed by startup.
    memory.word(0x11000 + 8, IPA | 1 | 1 << 10 | 3 << 8);
    memory.word(0x11000, table(0x15000));
    memory.word(0x15000, table(0x16000));
    memory.word(
        0x16000 + 256 * 8,
        (IPA + DATA_OFFSET as u64) | attrs | (3 << 53),
    );
    memory.word(0x16000 + 258 * 8, (IPA + 0x72000) | attrs | (3 << 53));
    memory.word(0x12000 + 8, table(0x13000));
    memory.word(0x13000, table(0x14000));
    // Private aliases cover only declared text, tables, state and stacks.
    for offset in (0..RAM_SIZE).step_by(4096) {
        let text = resident::executable_offset(offset) || offset == CODE_OFFSET;
        let writable = (0x10000..0x17000).contains(&offset)
            || (SHARED_OFFSET..SHARED_OFFSET + 0x8000).contains(&offset)
            || (BANK_OFFSET..BANK_OFFSET + 0x2000).contains(&offset)
            || (0x70000..0x84000).contains(&offset)
            || (0x50000..0x54000).contains(&offset)
            || (0x60000..0x64000).contains(&offset);
        if text || writable {
            let protection = if text { 1 << 7 } else { 3 << 53 };
            memory.word(
                0x14000 + offset / 4096 * 8,
                (IPA + offset as u64) | attrs | protection,
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn malformed_elf_rejected_without_loading() {
        let memory = Memory::new().unwrap();
        for length in 0..64 {
            assert!(load_elf(&memory, &vec![0; length]).is_err());
        }
    }
    fn resident_elf(segments: &[(u64, u64)]) -> Vec<u8> {
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
        page_tables(&memory);
        let leaf = |offset| u64::from_le_bytes(memory.read::<8>(0x14000 + offset / 4096 * 8));
        for offset in (0..RAM_SIZE).step_by(4096) {
            let pte = leaf(offset);
            if resident::executable_offset(offset) {
                assert_eq!(pte, (IPA + offset as u64) | 3 | 1 << 10 | 3 << 8 | 1 << 7);
            } else if (resident::BOOT_END..0x10000).contains(&offset) {
                assert_eq!(pte, 0);
            } else if pte != 0 && offset != CODE_OFFSET {
                assert_eq!(pte & (3 << 53), 3 << 53);
            }
        }
    }
}
