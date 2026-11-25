//! Private resident code windows, disjoint from tables, state and generated code.
//!
//! The boot page is the window's first page (its MMU-enable code runs through the host's identity alias). The rest of
//! the resident (`.text`, `.rodata`) lives in its own 2 MiB block right after the window's first 2 MiB (its 4 KiB pages),
//! mapped as one level-2 block, EL1 read-only and EL1-executable, backed by the vCPU's own block of the backing at the
//! same offset; the shadow-table pool follows it (`shadow::POOL_OFFSET`). M2 Part 2 (the boot-time interpreter): it was
//! 256 KiB at 0xa0000-0xe0000, over nine tenths used; 2 MiB, the mapping granule of every area past the window's pages,
//! holds the interpreter's estimated 2,500-4,000 lines several times over.
pub const BOOT_END: usize = 0x1000;
pub const TEXT_OFFSET: usize = 0x20_0000;
pub const TEXT_END: usize = 0x40_0000;
pub fn executable_offset(offset: usize) -> bool {
    offset < BOOT_END || (TEXT_OFFSET..TEXT_END).contains(&offset)
}
pub fn contains(address: u64, bytes: u64) -> bool {
    if bytes == 0 {
        return false;
    }
    let Some(offset) = address.checked_sub(crate::PRIVATE) else {
        return false;
    };
    let Some(end) = offset.checked_add(bytes) else {
        return false;
    };
    (offset < BOOT_END as u64 && end <= BOOT_END as u64)
        || (offset >= TEXT_OFFSET as u64 && offset < TEXT_END as u64 && end <= TEXT_END as u64)
}
// The text block follows the window's 4 KiB pages (2 MiB) and is mapped as level-2 blocks.
const _: () = assert!(TEXT_OFFSET == 2 * crate::RAM_SIZE);
const _: () = assert!(TEXT_OFFSET % (2 << 20) == 0 && (TEXT_END - TEXT_OFFSET) % (2 << 20) == 0);
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn entire_segment_must_fit_one_executable_window() {
        assert!(contains(crate::PRIVATE, BOOT_END as u64));
        assert!(contains(
            crate::PRIVATE + TEXT_OFFSET as u64,
            (TEXT_END - TEXT_OFFSET) as u64
        ));
        for (address, size) in [
            (crate::PRIVATE - 1, 1),
            (crate::PRIVATE, 0),
            (crate::PRIVATE + BOOT_END as u64 - 1, 2),
            (crate::PRIVATE + 0x10000, 8),
            (crate::PRIVATE + 0x3_0000, 48),
            // The window before M2 Part 2, now the gap between the staging area and the compat roots.
            (crate::PRIVATE + 0xa_0000, 4),
            (crate::PRIVATE + 0xe_0000, 4),
            // The shadow pool: the window's last pages until November 25, after the text block since.
            (crate::PRIVATE + 0xe_3000, 4),
            (crate::PRIVATE + crate::shadow::POOL_OFFSET as u64, 4),
            (crate::PRIVATE + TEXT_OFFSET as u64 - 1, 2),
            (crate::PRIVATE + TEXT_END as u64 - 1, 2),
            (u64::MAX, 2),
        ] {
            assert!(!contains(address, size));
        }
    }
}
