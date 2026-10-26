//! Actual fetched control operations split live FEX instruction executions.
use super::validation::{guest, recovery_state, state as validation};
use super::*;
use natemu_abi::{
    controls as c,
    paging::{Access, Control, Fault},
    paging32, recovery as r, translation as wire,
};
unsafe extern "C" {
    fn service(packet: *mut wire::Packet);
    fn service_resume();
    fn resume_exception(frame: *const NativeFrame, pc: u64, spsr: u64) -> !;
}
fn state() -> *mut c::State {
    (PRIVATE + c::OFFSET as u64) as *mut c::State
}
fn at(index: usize) -> *mut c::Visit {
    (PRIVATE + c::RECORD_OFFSET as u64 + index as u64 * size_of::<c::Visit>() as u64)
        as *mut c::Visit
}
fn visit() -> *mut c::Visit {
    unsafe { at((*state()).visits as usize - 1) }
}
struct Tables;
impl paging32::Tables for Tables {
    fn read32(&mut self, gpa: u64) -> Option<u32> {
        if !(0x1000..0x5000).contains(&gpa) || gpa & 3 != 0 {
            return None;
        }
        Some(unsafe {
            ptr::read_volatile((PRIVATE + r::TABLE_OFFSET as u64 + gpa - 0x1000) as *const u32)
        })
    }
    fn write32(&mut self, gpa: u64, value: u32) -> bool {
        if !(0x1000..0x5000).contains(&gpa) || gpa & 3 != 0 {
            return false;
        }
        unsafe {
            ptr::write_volatile(
                (PRIVATE + r::TABLE_OFFSET as u64 + gpa - 0x1000) as *mut u32,
                value,
            );
            asm!("dsb ish");
        }
        true
    }
}
unsafe fn route(address: u64, bytes: u64, access: Access) -> Result<(u64, bool), Fault> {
    unsafe {
        let base = if access == Access::Fetch {
            c::RIP
        } else {
            DATA_VA
        };
        let end = address
            .checked_add(bytes.checked_sub(1).ok_or(Fault::Unsupported)?)
            .ok_or(Fault::Unsupported)?;
        if address < base || end >= base + 4096 {
            return Err(Fault::Unsupported);
        }
        let mode = c::mode(&*guest()).ok_or(Fault::Unsupported)?;
        let (gpa, writable) = if mode == 0 {
            (address, true)
        } else {
            (*state()).walks += 1;
            let g = &*guest();
            let t = paging32::walk(
                &mut Tables,
                Control {
                    cr3: g.control[2],
                    cpl: (*state()).cpl,
                    wp: (g.control[0] >> 16) & 1,
                    nxe: 0,
                    smep: 0,
                },
                address,
                access,
            )?;
            if !t.wb {
                return Err(Fault::Unsupported);
            }
            (t.gpa, t.writable && t.dirty)
        };
        if if access == Access::Fetch {
            gpa != address
        } else {
            !(DATA_VA..DATA_VA + 8192).contains(&gpa) || gpa & 4095 != address & 4095
        } {
            return Err(Fault::Unsupported);
        }
        Ok((gpa, writable))
    }
}
unsafe fn summarize() {
    unsafe {
        let v = visit();
        let g = guest();
        (*v).after = (*g).control;
        (*v).generation_after = (*state()).generation;
        (*v).gpr_after.copy_from_slice(&(&(*g).gpr)[..4]);
        (*v).flags_after = (*g).rflags;
        copy_record(
            (&raw mut (*v).frame).cast(),
            (&raw const (*state()).current).cast(),
            size_of::<NativeFrame>(),
        );
    }
}
unsafe fn exception(vector: u64, address: u64, error: u64) -> ! {
    unsafe {
        (*visit()).vector = vector;
        (*visit()).error = error;
        (*visit()).linear = address;
        (*recovery_state()).vector = vector;
        (*recovery_state()).error = error;
        (*recovery_state()).linear = address;
        (*recovery_state()).rip = (*guest()).rip;
        if vector == 14 {
            (*guest()).control[1] = address;
        }
        summarize();
        (*validation()).stage = 3;
        stop(SUCCESS, 0)
    }
}
unsafe fn route_or_fault(address: u64, bytes: u64, access: Access) -> (u64, bool) {
    unsafe {
        match route(address, bytes, access) {
            Ok(t) => t,
            Err(Fault::Page { address, error }) => exception(14, address, error),
            _ => stop(REJECTED, 270),
        }
    }
}
unsafe fn exchange(expected: wire::Packet, action: u64) -> wire::Packet {
    unsafe {
        let mut p = expected;
        p[0] = action;
        (*state()).expected = p;
        service(&mut p);
        if action != natemu_abi::concurrency::DEFER_COMPILE {
            while p[0] == wire::BUSY {
                for _ in 0..16 {
                    asm!("yield", options(nomem, nostack));
                }
                p = expected;
                p[0] = natemu_abi::concurrency::POLL_COMPILE;
                (*state()).expected = p;
                service(&mut p);
            }
            (*state()).transfers += 1;
        }
        (*state()).expected = expected;
        p
    }
}
unsafe fn next_request(source: &[u8]) -> wire::Packet {
    unsafe {
        (*state()).serial = (*state())
            .serial
            .checked_add(1)
            .unwrap_or_else(|| stop(REJECTED, 271));
        c::request(
            (*shared()).lifecycle.vm,
            (*state()).generation,
            c::mode(&*guest()).unwrap(),
            (*guest()).rip,
            source,
            (*state()).serial,
        )
        .unwrap_or_else(|| stop(REJECTED, 272))
    }
}
// This is the same context/source guard used by real ordinary entries.
unsafe fn eligible(index: usize) -> bool {
    unsafe {
        let r = &*at(index);
        r.request[6] == (*state()).generation
            && r.request[7] == (*guest()).rip
            && r.request[8] == c::mode(&*guest()).unwrap_or(u64::MAX)
            && c::valid(&r.request, &r.reply)
    }
}
#[inline(never)]
unsafe fn compile(source: &[u8]) {
    unsafe {
        let r = state();
        let v = visit();
        let p = next_request(source);
        (*v).request = p;
        if (*r).profile == 0 && (*r).visits == 16 {
            if (*guest()).control != (*r).old_controls
                || (*guest()).rip != c::RIP
                || (*r).generation == (*r).old_slot_context
            {
                stop(PANIC, 273);
            }
            if eligible(0) {
                stop(PANIC, 274);
            }
            (*r).stale_entry = 1;
            let late = exchange((*r).deferred, natemu_abi::concurrency::TAKE_COMPILE);
            (*r).late_reply = late;
            (*r).expected = p;
            if c::valid(&p, &late) {
                stop(PANIC, 275);
            }
            // Remove serial mismatch: context alone must still reject the old result.
            let mut rebound = late;
            rebound[12] = p[12];
            (*r).rebound_reply = rebound;
            if c::valid(&p, &rebound) {
                stop(PANIC, 275);
            }
            (*r).stale_reply = 2;
        }
        (*validation()).requests += 1;
        let reply = exchange(p, c::MISS);
        (*v).reply = reply;
        if !c::valid(&p, &reply) {
            stop(REJECTED, 276);
        }
        let index = (*r).visits as usize - 1;
        let mut image = read_record(CODE_VA as *const [u8; c::MAX_VISITS * c::SLOT_BYTES]);
        let start = index * c::SLOT_BYTES;
        let count = p[13] as usize;
        if image[start..start + c::SLOT_BYTES] != [0; c::SLOT_BYTES] {
            stop(REJECTED, 277);
        }
        image[start..start + count].copy_from_slice(&reply[14].to_le_bytes()[..count]);
        image[start + count..start + count + 4].copy_from_slice(&RET);
        translation::replace_bytes(&image);
        if (*r).profile == 0 && (*r).visits == 1 {
            (*r).old_slot_context = (*r).generation;
            let deferred = next_request(source);
            (*r).deferred = deferred;
            (*validation()).requests += 1;
            if exchange(deferred, natemu_abi::concurrency::DEFER_COMPILE)[0] != wire::BUSY {
                stop(PANIC, 278);
            }
        }
    }
}
#[inline(never)]
unsafe fn begin_visit() {
    unsafe {
        let s = state();
        let g = guest();
        if (*s).visits as usize >= c::MAX_VISITS {
            stop(REJECTED, 279);
        }
        let at = at((*s).visits as usize);
        (*s).visits += 1;
        (*at).rip = (*g).rip;
        (*at).before = (*g).control;
        (*at).generation_before = (*s).generation;
        (*at).gpr_before.copy_from_slice(&(&(*g).gpr)[..4]);
        (*at).flags_before = (*g).rflags;
        (*validation()).stage = 0;
    }
}
unsafe fn complete_visit() {
    unsafe {
        summarize();
        (*state()).completed += 1;
        (*validation()).completed = (*state()).completed;
    }
}
unsafe fn invalidate() {
    unsafe {
        recovery::clear_mapping(DATA_VA);
        (*state()).mapping = 0;
        (*state()).invalidations += 1;
    }
}
unsafe fn control(source: &[u8]) {
    unsafe {
        (*visit()).kind = 1;
        let operation = c::decode(source).and_then(|op| {
            c::apply(
                &mut *guest(),
                &mut (*state()).generation,
                (*state()).cpl,
                op,
            )?;
            Ok(op)
        });
        match operation {
            Ok(op) => {
                invalidate();
                (*guest()).rip = (*guest()).rip.wrapping_add(op.bytes as u64) & 0xffff_ffff;
                complete_visit();
            }
            Err(c::Error::GeneralProtection) => exception(13, 0, 0),
            Err(c::Error::Undefined) => exception(6, 0, 0),
            Err(c::Error::Unsupported) => stop(REJECTED, 280),
        }
    }
}
pub unsafe fn initialize() -> ! {
    unsafe {
        let s = shared();
        let r = state();
        let g = guest();
        let cpl = (*r).cpl;
        if c::instructions((*r).profile).is_none()
            || cpl != if (*r).profile == 6 { 3 } else { 0 }
            || (*r).generation != 1
            || (*r).visits != 0
            || (*r).completed != 0
            || (*r).serial != 0
            || (*g).rip != c::RIP
            || (*g).control[0] != 0x10011
            || (*g).control[2] != 0x1000
            || (*g).control[3] != 0x600
            || (*g).control[4] != 0
            || (*s).initial.x[6] != DATA_VA
            || (*s).initial.x[4] > u32::MAX as u64
            || (*s).initial.sp != STACK_TOP
            || (*s).initial.x[28] != BANK_VA
            || (*s).object.configured_vcpus != 1
            || (*s).object.vcpu_id != 0
            || (*s).object.version != VERSION
            || (*s).object.profile != PROFILE
            || (*s).object.size != size_of::<Object>() as u32
            || (*s).object.reserved != 0
            || (*s).object.entry_offset != 0
            || (*s).object.code_address != CODE_VA
            || (*s).object.code_length != (c::MAX_VISITS * c::SLOT_BYTES) as u64
            || (*s).object.kind != LIVE_LOAD
            || (*s).initial.reserved != 0
            || (*r).initial != (*s).initial
            || (*r).current != (*s).initial
            || (*r).old_controls != (*g).control
            || (*s).lifecycle.vm == 0
            || (*s).tcr != shadow::TCR_BOTH
            || (*validation()).case != c::CASE
            || (*validation()).live != 1
            || (*validation()).stage != 0
            || (*g).segments[0] != [8 | cpl, 0, 0xffff_ffff, 0xc09b | (cpl << 5)]
            || !(&(*g).segments)[1..]
                .iter()
                .all(|seg| *seg == [0x10 | cpl, 0, 0xffff_ffff, 0xc093 | (cpl << 5)])
        {
            stop(REJECTED, 281);
        }
        for (guest_index, native) in [4, 7, 5, 6, 8, 9, 10, 11].into_iter().enumerate() {
            if (*g).gpr[guest_index] != (*s).initial.x[native] & 0xffff_ffff {
                stop(REJECTED, 281);
            }
        }
        (*s).service_pc = service_resume as *const () as u64;
        recovery::clear_mapping(DATA_VA);
        dispatch()
    }
}
#[inline(never)]
unsafe fn dispatch() -> ! {
    unsafe {
        loop {
            let r = state();
            let g = guest();
            let (_, bytes) = c::source((*r).profile).unwrap();
            if ((*r).profile == 0 && (*r).completed == 17)
                || ((*r).profile != 0 && (*g).rip == c::RIP + bytes as u64)
            {
                (*validation()).stage = 2;
                stop(SUCCESS, 0);
            }
            begin_visit();
            let instruction =
                c::instruction((*r).profile, (*g).rip).unwrap_or_else(|| stop(REJECTED, 282));
            let (gpa, _) = route_or_fault((*g).rip, instruction.len as u64, Access::Fetch);
            let mut actual = [0; 8];
            copy_record(
                actual.as_mut_ptr(),
                (PRIVATE + r::SOURCE_OFFSET as u64 + gpa - c::RIP) as *const u8,
                instruction.len,
            );
            if actual[..instruction.len] != instruction.bytes[..instruction.len] {
                stop(REJECTED, 283);
            }
            (*visit()).fetch_gpa = gpa;
            (*visit()).bytes = instruction.len as u64;
            (*visit()).source = u64::from_le_bytes(actual);
            let source = &actual[..instruction.len];
            if actual[0] == 0xe9 {
                (*visit()).kind = 2;
                let delta = i32::from_le_bytes(actual[1..5].try_into().unwrap()) as i64;
                (*g).rip = ((*g).rip + 5).wrapping_add(delta as u64) & 0xffff_ffff;
                complete_visit();
                continue;
            }
            if c::body(source).is_none() {
                control(source);
                continue;
            }
            if matches!(actual[0], 0x89 | 0x8b) && (*r).current.x[6] != DATA_VA {
                stop(REJECTED, 284);
            }
            compile(source);
            let index = (*r).visits as usize - 1;
            if !eligible(index) {
                stop(REJECTED, 285);
            }
            let mut frame = read_record(&raw const (*r).current);
            frame.x[16] = CODE_VA + (index * c::SLOT_BYTES) as u64;
            frame.x[30] = returned as *const () as u64;
            frame.sp = STACK_TOP;
            write_record(&raw mut (*shared()).initial, frame);
            (*shared()).capture_pc = frame.x[30];
            (*visit()).entries = 1;
            (*validation()).stage = 1;
            if matches!(actual[0], 0x89 | 0x8b) && (*r).mapping != 0 {
                memory_before((*r).mapping);
            }
            enter(&raw const (*shared()).initial)
        }
    }
}
unsafe fn memory_before(gpa: u64) {
    unsafe {
        (*visit()).gpa = gpa;
        copy_record(
            (&raw mut (*visit()).memory_before).cast(),
            (PRIVATE + DATA_OFFSET as u64 + gpa - DATA_VA) as *const u8,
            16,
        );
    }
}
#[inline(never)]
pub unsafe fn finish(fault: u64) -> ! {
    unsafe {
        let r = state();
        let v = visit();
        let s = shared();
        let g = guest();
        if (*validation()).stage != 1 || (*v).entries != 1 {
            stop(PANIC, 286);
        }
        copy_record(
            (&raw mut (*r).current).cast(),
            (&raw const (*s).captured).cast(),
            size_of::<NativeFrame>(),
        );
        for (guest, native) in [(0, 4), (1, 7), (2, 5), (3, 6)] {
            (*g).gpr[guest] = (*r).current.x[native] & 0xffff_ffff;
        }
        let opcode = (*v).source as u8;
        if fault != 0 {
            let write = opcode == 0x89;
            let site =
                CODE_VA + ((*r).visits - 1) * c::SLOT_BYTES as u64 + if write { 4 } else { 0 };
            let (esr, far, pc, spsr) = ((*s).esr, (*s).far, (*s).elr, (*s).spsr);
            if !matches!(opcode, 0x89 | 0x8b)
                || esr >> 26 != 0x25
                || (esr >> 6) & 1 != u64::from(write)
                || !matches!(esr&63,4..=7|12..=15)
                || far != DATA_VA
                || pc != site
                || spsr & 15 != 4
                || (*v).faults != 0
            {
                stop(NATIVE_FAULT, 287);
            }
            (*v).faults = 1;
            (*v).exception = [esr, far, pc, spsr];
            copy_record(
                (&raw mut (*v).fault_frame).cast(),
                (&raw const (*s).captured).cast(),
                size_of::<NativeFrame>(),
            );
            let (gpa, writable) =
                route_or_fault(far, 4, if write { Access::Write } else { Access::Read });
            memory_before(gpa);
            (*r).mapping = gpa;
            recovery::install_ram(far, gpa, writable);
            resume_exception(&raw const (*s).captured, pc, spsr)
        }
        if matches!(opcode, 0x89 | 0x8b) {
            if (*v).gpa == 0 || (*v).gpa != (*r).mapping {
                stop(PANIC, 288);
            }
            copy_record(
                (&raw mut (*v).memory_after).cast(),
                (PRIVATE + DATA_OFFSET as u64 + (*v).gpa - DATA_VA) as *const u8,
                16,
            );
        }
        (*g).rip += (*v).bytes;
        complete_visit();
        if (*r).profile == 1 && (*r).completed == 3 {
            let at = (PRIVATE + shadow::leaf_offset(DATA_VA).unwrap() as u64) as *const u64;
            (*r).warm_before_reload = ptr::read_volatile(at);
            if (*r).mapping != DATA_VA || (*r).warm_before_reload & 3 != 3 {
                stop(PANIC, 289);
            }
            // Protected fixture writer changes the guest PTE while the old native mapping remains.
            ptr::write_volatile(
                (PRIVATE + r::TABLE_OFFSET as u64 + 0x1000 + 256 * 4) as *mut u32,
                (DATA_VA + 4096) as u32 | 7,
            );
            asm!("dsb ish");
            (*r).fixture_mutated = 1;
        }
        dispatch()
    }
}
