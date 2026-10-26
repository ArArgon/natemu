//! Alias repair stays under the same ownership as the original I4 protocol.
use super::*;
use natemu_abi::{alias as profile, paging::Translation, recovery as r, shadow};

fn record() -> *mut profile::Record {
    (PRIVATE + profile::OFFSET as u64) as *mut profile::Record
}
pub unsafe fn leaves() -> [u64; 2] {
    unsafe {
        [
            ptr::read_volatile(
                (PRIVATE + shadow::leaf_offset(DATA_VA).unwrap() as u64) as *const u64,
            ),
            ptr::read_volatile(
                (PRIVATE + shadow::leaf_offset(other(DATA_VA)).unwrap() as u64) as *const u64,
            ),
        ]
    }
}
pub fn data_address(id: u64, pass: u64) -> u64 {
    if coordinator().stack_profile >= 2 {
        DATA_VA + id * 256
    } else {
        profile::data_address(coordinator().alias_profile, id, pass)
    }
}
pub fn other(address: u64) -> u64 {
    natemu_abi::stack_concurrent::other_alias(coordinator().stack_profile, address)
        .unwrap_or_else(|| stop(PANIC, 152))
}
// Caller holds mutation authority; the two leaf entries are one physical alias.
pub unsafe fn set_leaf(value: u64) {
    unsafe {
        let base = PRIVATE + r::TABLE_OFFSET as u64 + 0x3000;
        ptr::write_volatile((base + 256 * 8) as *mut u64, value);
        if coordinator().stack_profile == 3 {
            ptr::write_volatile((base + 257 * 8) as *mut u64, value & !2);
        }
    }
}
pub fn validate(object: &Object, frame: &NativeFrame, code: &[u8], pass: u64) -> Result<(), u64> {
    if stack::enabled() {
        return unsafe { stack::validate(object, frame, pass) };
    }
    let profile = coordinator().alias_profile;
    if profile == 0 {
        validate_object(object, frame, code)
    } else {
        profile::validate_object(object, frame, code, profile, pass)
    }
}

// Caller holds coordinator authority, including for guest A/D updates.
pub unsafe fn fetch() {
    unsafe {
        let rip = profile::source_rip(coordinator().alias_profile);
        let route = recovery::walk(rip, Access::Fetch).unwrap_or_else(|_| stop(PANIC, 150));
        if route.gpa != natemu_abi::translation::SOURCE_VA || !route.wb {
            stop(PANIC, 150)
        }
    }
}
pub unsafe fn prepare(id: u64, route: Translation) {
    unsafe {
        let ttbr1: u64;
        asm!("mrs {0}, TTBR1_EL1",out(reg)ttbr1);
        if ttbr1 != IPA + id * RAM_SIZE as u64 + shadow::UPPER_ROOT as u64 {
            stop(PANIC, 151)
        }
        ptr::write_volatile(&raw mut (*record()).ttbr1, ttbr1);
        recovery::clear_mapping(DATA_VA);
        recovery::clear_mapping(other(DATA_VA));
        let address = data_address(id, 0);
        let alternate = other(address);
        let alternate_route =
            recovery::walk(alternate, Access::Read).unwrap_or_else(|_| stop(PANIC, 152));
        if alternate_route.gpa != route.gpa {
            stop(PANIC, 152);
        }
        recovery::install(alternate, alternate_route);
        write_record(&raw mut (*record()).warm, leaves());
    }
}
#[inline(never)]
pub unsafe fn install_pair(address: u64, route: Translation) {
    unsafe {
        recovery::install(address, route);
        if coordinator().alias_profile != 0 {
            let alternate = other(address);
            // Physical identity does not confer the other alias's permissions.
            let alternate_route =
                recovery::walk(alternate, Access::Read).unwrap_or_else(|_| stop(PANIC, 152));
            if alternate_route.gpa != route.gpa {
                stop(PANIC, 152);
            }
            recovery::install(alternate, alternate_route);
        }
    }
}
pub unsafe fn acknowledge(epoch: u64) {
    unsafe {
        let at = record();
        let count = (*at).ack_count as usize;
        if count >= profile::MAX_ACKS {
            stop(PANIC, 153)
        }
        let pair = leaves();
        write_record(&raw mut (*at).acks[count], [epoch, pair[0], pair[1]]);
        ptr::write_volatile(&raw mut (*at).ack_count, count as u64 + 1);
    }
}

pub unsafe fn rewrite_source(source: &[u8]) {
    unsafe {
        let c = coordinator();
        if source.len() != if c.stack_profile != 0 { 20 } else { 17 }
            || c.phase.load(Ordering::Relaxed) != EXCLUSIVE
            || c.active.load(Ordering::Relaxed) != 0
        {
            stop(PANIC, 154)
        }
        let address = natemu_abi::translation::SOURCE_VA
            + if c.alias_profile == 2 || c.stack_profile == 2 {
                0
            } else {
                shadow::HIGH_BASE
            };
        let at = record();
        let leaf = (PRIVATE + r::TABLE_OFFSET as u64 + 0x3000 + 258 * 8) as *const u64;
        ptr::write_volatile(&raw mut (*at).source_leaf_before, ptr::read_volatile(leaf));
        write_record(
            &raw mut (*at).source_before,
            read_record((PRIVATE + r::SOURCE_OFFSET as u64) as *const [u8; 17]),
        );
        let route = recovery::walk(address, Access::Write).unwrap_or_else(|_| stop(PANIC, 155));
        if route.gpa != natemu_abi::translation::SOURCE_VA || !route.wb {
            stop(PANIC, 155)
        }
        recovery::install(address, route);
        // A protected fixture mutation, routed through the actual opposite
        // guest alias; this is not a translated guest self-modifying store.
        ptr::copy_nonoverlapping(source.as_ptr(), address as *mut u8, source.len());
        asm!("dsb ish");
        ptr::write_volatile(&raw mut (*at).source_address, address);
        ptr::write_volatile(&raw mut (*at).source_gpa, route.gpa);
        ptr::write_volatile(&raw mut (*at).source_leaf_after, ptr::read_volatile(leaf));
        write_record(
            &raw mut (*at).source_after,
            read_record((PRIVATE + r::SOURCE_OFFSET as u64) as *const [u8; 17]),
        );
    }
}

pub unsafe fn check_wrong_rip(
    state: &natemu_abi::translation::Lifecycle,
    current: &natemu_abi::translation::Packet,
) {
    unsafe {
        let mut wrong = *current;
        wrong[13] ^= shadow::HIGH_BASE;
        if state.validate_reply(&wrong) != Err(natemu_abi::translation::STALE)
            || state.pending != current[2]
            || core::slice::from_raw_parts(CODE_NEXT as *const u8, 48) != [0; 48]
        {
            stop(PANIC, 156)
        }
        ptr::write_volatile(&raw mut (*record()).wrong_rip_rejections, 1);
        ptr::write_volatile(&raw mut (*record()).blank_before_publish, 1);
    }
}
