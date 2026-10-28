//! Private resident code windows, disjoint from tables, state and generated code.
pub const BOOT_END: usize = 0x1000;
pub const TEXT_OFFSET: usize = 0xa0000;
pub const TEXT_END: usize = 0xe0000;
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
const _: () = assert!(TEXT_END <= crate::RAM_SIZE);
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
            (crate::PRIVATE + TEXT_OFFSET as u64 - 1, 2),
            (crate::PRIVATE + TEXT_END as u64 - 1, 2),
            (u64::MAX, 2),
        ] {
            assert!(!contains(address, size));
        }
    }
}
