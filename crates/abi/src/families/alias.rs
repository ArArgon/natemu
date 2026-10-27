//! Address-alias extension of the existing concurrent maintenance profile.
use crate::{DATA_VA, NativeFrame, Object, shadow::HIGH_BASE, translation::SOURCE_VA};
pub const OFFSET: usize = 0x2f000;
pub const MAX_ACKS: usize = 37;

pub fn source_rip(profile: u64) -> u64 {
    SOURCE_VA + if profile == 2 { HIGH_BASE } else { 0 }
}
pub fn data_address(profile: u64, id: u64, pass: u64) -> u64 {
    DATA_VA
        + id * 256
        + if profile != 0 && (id + pass) % 2 == 0 {
            HIGH_BASE
        } else {
            0
        }
}
pub fn low_address(address: u64) -> Option<u64> {
    if (DATA_VA..DATA_VA + 4096).contains(&address) {
        Some(address)
    } else if (HIGH_BASE + DATA_VA..HIGH_BASE + DATA_VA + 4096).contains(&address) {
        Some(address - HIGH_BASE)
    } else {
        None
    }
}
pub fn other_address(address: u64) -> Option<u64> {
    low_address(address).map(|low| if address == low { HIGH_BASE + low } else { low })
}
pub fn validate_object(
    object: &Object,
    frame: &NativeFrame,
    code: &[u8],
    profile: u64,
    pass: u64,
) -> Result<(), u64> {
    if !(1..=2).contains(&profile) || object.vcpu_id >= 4 || pass > 6 {
        return Err(150);
    }
    let address = data_address(profile, object.vcpu_id as u64, pass);
    if frame.x[6] != address || frame.x[11] != address + 128 {
        return Err(151);
    }
    let mut normalized = *frame;
    normalized.x[6] = low_address(frame.x[6]).ok_or(151u64)?;
    normalized.x[11] = low_address(frame.x[11]).ok_or(151u64)?;
    crate::concurrency::validate_object(object, &normalized, code)
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct Record {
    pub ttbr1: u64,
    pub warm: [u64; 2],
    pub ack_count: u64,
    // epoch, lower leaf, upper leaf; composed remap is epoch2, then3..=38.
    pub acks: [[u64; 3]; MAX_ACKS],
    pub source_address: u64,
    pub source_gpa: u64,
    pub source_leaf_before: u64,
    pub source_leaf_after: u64,
    pub source_before: [u8; 17],
    pub source_after: [u8; 17],
    pub wrong_rip_rejections: u64,
    pub blank_before_publish: u64,
}
impl Record {
    pub const ZERO: Self = Self {
        ttbr1: 0,
        warm: [0; 2],
        ack_count: 0,
        acks: [[0; 3]; MAX_ACKS],
        source_address: 0,
        source_gpa: 0,
        source_leaf_before: 0,
        source_leaf_after: 0,
        source_before: [0; 17],
        source_after: [0; 17],
        wrong_rip_rejections: 0,
        blank_before_publish: 0,
    };
}
const _: () = assert!(core::mem::size_of::<Record>() <= 4096);
const _: () =
    assert!(OFFSET >= crate::families::maintenance::RECORD_OFFSET + crate::families::maintenance::MAX_INTERVALS * 4096);
const _: () = assert!(
    OFFSET
        >= crate::families::maintenance::BASELINE_OFFSET
            + core::mem::size_of::<crate::families::maintenance::Baseline>()
);
const _: () = assert!(OFFSET + 4096 <= crate::CODE_OFFSET);

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn address_conversion_excludes_private_and_unowned_ranges() {
        for id in 0..4 {
            for pass in 0..7 {
                let address = data_address(2, id, pass);
                assert_eq!(low_address(address), Some(DATA_VA + id * 256));
                assert_eq!(
                    other_address(other_address(address).unwrap()),
                    Some(address)
                );
            }
        }
        for bad in [
            crate::PRIVATE,
            0x800000100000,
            DATA_VA - 1,
            DATA_VA + 4096,
            HIGH_BASE + DATA_VA + 4096,
        ] {
            assert_eq!(low_address(bad), None);
        }
    }
}
