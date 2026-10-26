//! Bounded native shadow windows, separate from guest translation and GPA routing.
//! The first V1 profile admits one 2MiB window in each guest canonical half.
pub const HIGH_BASE: u64 = 0xffff_8000_0000_0000;
pub const WINDOW_BYTES: u64 = 0x20_0000;
pub const LOWER_LEAF: usize = 0x16000;
pub const UPPER_ROOT: usize = 0x17000;
pub const UPPER_L1: usize = 0x18000;
pub const UPPER_L2: usize = 0x19000;
pub const UPPER_LEAF: usize = 0x1a000;
pub const LOWER_ROOT: usize = 0x10000;
pub const TCR_BOTH: u64 =
    16 | 1 << 8 | 1 << 10 | 3 << 12 | 16 << 16 | 1 << 24 | 1 << 26 | 3 << 28 | 2 << 30 | 2 << 32;

/// A valid guest address outside these windows is unsupported by this finite
/// mapping reserve. That is not itself an architectural guest page fault.
pub fn leaf_offset(page: u64) -> Option<usize> {
    if page & 4095 != 0 {
        return None;
    }
    let (base, offset) = if page < WINDOW_BYTES {
        (LOWER_LEAF, page)
    } else if (HIGH_BASE..HIGH_BASE + WINDOW_BYTES).contains(&page) {
        (UPPER_LEAF, page - HIGH_BASE)
    } else {
        return None;
    };
    Some(base + (offset >> 12) as usize * 8)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn distinct_roots_preserve_high_bits_without_exposing_private_tables() {
        for page in (0..WINDOW_BYTES).step_by(4096) {
            let low = leaf_offset(page).unwrap();
            let high = leaf_offset(HIGH_BASE + page).unwrap();
            assert!((LOWER_LEAF..LOWER_LEAF + 4096).contains(&low));
            assert!((UPPER_LEAF..UPPER_LEAF + 4096).contains(&high));
            assert_eq!(high - low, UPPER_LEAF - LOWER_LEAF);
        }
        for page in [
            crate::PRIVATE,
            0x8000_0000_0000,
            WINDOW_BYTES,
            HIGH_BASE + WINDOW_BYTES,
            u64::MAX & !4095,
            1,
        ] {
            assert_eq!(leaf_offset(page), None);
        }
    }
}
