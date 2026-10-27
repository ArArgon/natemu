//! Block execution in the resident (`natemu_abi::execution`): place each
//! host-admitted block, enter it from the state bank, take its exit through the
//! dispatcher gate back into the bank, and serve shadow misses at its recorded
//! access sites by walking the guest tables and retrying the same instruction.
use super::*;
use natemu_abi::{
    code_object::{self as object, Header, Site},
    execution::*,
    paging::{self, Access, Fault},
    shadow::{self, READ_ONLY},
};

unsafe extern "C" {
    fn enter_allocated(frame: *const NativeFrame, pc: u64, spsr: u64) -> !;
    fn resume_exception(frame: *const NativeFrame, pc: u64, spsr: u64) -> !;
    fn returned_allocated();
}

/// Guest-physical RAM through the resident's alias.
const RAM: GuestRam = unsafe { GuestRam::new(0, GUEST_RAM_BYTES as u64, GUEST_RAM_VA) };
/// EL1t, DAIF masked; NZCV is or-ed in.
const BLOCK_SPSR: u64 = 0x3c4;

fn record() -> *mut State {
    STATE_VA as *mut State
}
unsafe fn load() -> State {
    unsafe { read_record(record()) }
}
fn header() -> Header {
    unsafe { read_record(STAGING_VA as *const Header) }
}
/// The admitted block's sites, sorted by native offset (checked by the host).
fn sites(h: &Header) -> &'static [Site] {
    let at = (STAGING_VA + h.sites_offset as u64) as *const Site;
    unsafe { core::slice::from_raw_parts(at, h.sites_count as usize) }
}
fn frame_word(offset: u32) -> *mut u64 {
    (FRAME_VA + offset as u64) as *mut u64
}

/// Stops the VM; the host reads `reason` and the record.
fn halt(mut s: State, reason: u64) -> ! {
    s.stop = reason;
    unsafe { write_record(record(), s) };
    stop(SUCCESS, reason)
}

pub unsafe fn start() -> ! {
    let s = unsafe { load() };
    if s.cpu.control().is_none() || s.request != NONE {
        halt(s, stop::UNSUPPORTED_MODE)
    }
    unsafe { next(s) }
}

/// Asks the host for the block at `s.cpu.rip`, places it and enters it.
unsafe fn next(mut s: State) -> ! {
    unsafe {
        s.request = REQUEST_TRANSLATE;
        s.reply = NONE;
        write_record(record(), s);
        asm!("dsb sy", "hvc #{imm}", imm = const TRANSLATE_HVC);
        let mut s = load();
        let h = header();
        if s.reply != REPLY_ADMITTED
            || h.code_size as usize > SLOT_BYTES
            || h.code_offset as usize + h.code_size as usize > STAGING_BYTES
            || h.guest_rip != s.cpu.rip
        {
            halt(s, stop::BAD_REPLY)
        }
        s.request = NONE;
        // A write to the block's own source must fault into the SMC check.
        for (va, gpa) in s.source_va.into_iter().zip(s.source_gpa) {
            if va == u64::MAX {
                continue;
            }
            let d = tables::descriptor(va);
            if d & 1 != 0 && d & READ_ONLY == 0 {
                install(&mut s, va, gpa, false);
            }
        }
        let code = h.code_size as usize;
        copy_record(SLOT_WRITE_VA as *mut u8, (STAGING_VA + h.code_offset as u64) as *const u8, code);
        synchronize_code(code);
        frame_word(frame::DISPATCHER_LOOP_TOP).write_volatile(returned_allocated as *const () as u64);
        frame_word(frame::RIP).write_volatile(0);

        let reg = |i: usize| h.sra_gpr[i] as usize;
        let mut f = NativeFrame::ZERO;
        for (i, &value) in s.cpu.gpr.iter().enumerate() {
            f.x[reg(i)] = value;
        }
        let (nzcv, pf, af) = native_flags(s.cpu.rflags);
        f.x[reg(16)] = pf;
        f.x[reg(17)] = af;
        f.x[28] = FRAME_VA;
        f.sp = STACK_TOP;
        s.blocks += 1;
        // Blocks are straight-line: a native PC repeats only after a retry.
        s.last_retry = [0; 3];
        write_record(record(), s);
        let entry = &raw mut (*shared()).initial;
        write_record(entry, f);
        enter_allocated(entry, SLOT_VA + ENTRY_OFFSET as u64, BLOCK_SPSR | nzcv)
    }
}

/// Written through the data alias, executed through the code alias.
unsafe fn synchronize_code(bytes: usize) {
    unsafe {
        let ctr: u64;
        asm!("mrs {0}, ctr_el0", out(reg) ctr);
        for offset in (0..bytes).step_by(4 << ((ctr >> 16) & 15)) {
            asm!("dc cvau, {0}", in(reg) SLOT_WRITE_VA + offset as u64);
        }
        asm!("dsb ish");
        for offset in (0..bytes).step_by(4 << (ctr & 15)) {
            asm!("ic ivau, {0}", in(reg) SLOT_VA + offset as u64);
        }
        asm!("dsb ish", "isb");
    }
}

/// Exception capture and the dispatcher gate both end here.
pub unsafe fn returned(frame: &NativeFrame, fault: u64) -> ! {
    unsafe {
        if fault != 0 {
            aborted(frame)
        }
        // Block exit: guest state is in the static registers, NZCV (CF inverted) and PF/AF registers.
        let mut s = load();
        let h = header();
        let reg = |i: usize| frame.x[h.sra_gpr[i] as usize];
        for (i, value) in s.cpu.gpr.iter_mut().enumerate() {
            *value = reg(i);
        }
        s.cpu.rflags = guest_flags(s.cpu.rflags, frame.nzcv, reg(16), reg(17), true);
        s.cpu.rip = frame_word(frame::RIP).read_volatile();
        next(s)
    }
}

/// The address a site accesses, from the registers before the access.
fn site_address(site: &Site, f: &NativeFrame) -> u64 {
    let reg = |n: u8| if n == 31 { f.sp } else { f.x[n as usize] };
    let mut address = reg(site.base_reg).wrapping_add(site.imm as i64 as u64);
    if site.index_reg != object::NO_REG {
        let index = reg(site.index_reg);
        let index = match site.extend {
            object::EXT_UXTW => index as u32 as u64,
            object::EXT_SXTW => index as u32 as i32 as i64 as u64,
            _ => index,
        };
        address = address.wrapping_add(index << site.shift);
    }
    address
}

unsafe fn aborted(f: &NativeFrame) -> ! {
    unsafe {
        let (esr, far, elr, spsr): (u64, u64, u64, u64);
        asm!("mrs {0}, esr_el1", "mrs {1}, far_el1", "mrs {2}, elr_el1", "mrs {3}, spsr_el1",
             out(reg) esr, out(reg) far, out(reg) elr, out(reg) spsr);
        let mut s = load();
        let at = elr.wrapping_sub(SLOT_VA);
        s.native = [esr, far, at];
        let h = header();
        let all = sites(&h);
        let Ok(i) = all.binary_search_by_key(&at, |site| site.native_offset as u64) else {
            halt(s, stop::UNKNOWN_ABORT)
        };
        let site = all[i];
        let store = matches!(site.kind, object::kind::STORE | object::kind::STORE_PAIR | object::kind::PUSH);
        let write_abort = (esr >> 6) & 1 != 0;
        let write = site.intent == object::INTENT_WRITE;
        let address = site_address(&site, f);
        let width = u64::from(site.width);
        // Data abort from EL1, translation or permission fault, direction, intent and FAR as recorded.
        if esr >> 26 != 0x25
            || write_abort != store
            || (write_abort && !write)
            || !matches!(esr & 63, 4..=7 | 12..=15)
            || far.wrapping_sub(address) >= width
            || spsr & 15 != 4
        {
            halt(s, stop::SITE_MISMATCH)
        }
        // A repeat of the last retry with its shadow untouched would loop.
        if s.last_retry == [elr, far, tables::descriptor(far)] {
            halt(s, stop::NO_PROGRESS)
        }
        let Some(control) = s.cpu.control() else {
            halt(s, stop::UNSUPPORTED_MODE)
        };
        let access = if write { Access::Write } else { Access::Read };
        let rip = h.guest_rip.wrapping_add(site.guest_offset as u64);
        // Every page the access touches, in address order, before the retry.
        let last = address.wrapping_add(width - 1);
        let pages = if (last ^ address) >> 12 == 0 { 1 } else { 2 };
        for page in 0..pages {
            let va = if page == 0 { address } else { last & !4095 };
            match paging::walk(&mut GuestTables::new(RAM), control, va, access) {
                Ok(t) if t.wb => {
                    let writable = write && t.writable && t.dirty;
                    if writable && s.source_gpa.contains(&(t.gpa & !4095)) {
                        halt(s, stop::UNSUPPORTED_SMC)
                    }
                    let old = tables::descriptor(va);
                    if writable && old & 1 != 0 && old & READ_ONLY != 0 {
                        s.upgrades += 1;
                    }
                    install(&mut s, va, t.gpa, writable);
                }
                // Only write-back guest memory is shadowed.
                Ok(_) => halt(s, stop::SHADOW),
                Err(Fault::Page { address, error }) => {
                    s.fault = [14, address, error, rip];
                    halt(s, stop::GUEST_FAULT)
                }
                Err(Fault::Noncanonical) => {
                    let vector = if site.segment == object::SEG_SS { 12 } else { 13 };
                    s.fault = [vector, va, 0, rip];
                    halt(s, stop::GUEST_FAULT)
                }
                Err(Fault::Unsupported) => halt(s, stop::UNSUPPORTED_MODE),
            }
        }
        s.last_retry = [elr, far, tables::descriptor(far)];
        s.retries += 1;
        write_record(record(), s);
        resume_exception(&raw const (*shared()).captured, elr, spsr)
    }
}

/// Shadows linear page `va` with guest-physical `gpa`; on table capacity
/// exhaustion evicts every shadow once and retries.
fn install(s: &mut State, va: u64, gpa: u64, writable: bool) {
    match tables::install(va, &RAM, gpa, writable) {
        Ok(()) => {}
        Err(shadow::Error::Capacity) => {
            tables::space().reset();
            s.evictions += 1;
            if tables::install(va, &RAM, gpa, writable).is_err() {
                halt(*s, stop::SHADOW)
            }
        }
        Err(_) => halt(*s, stop::SHADOW),
    }
}
