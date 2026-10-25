use super::*;
use natemu_abi::{
    pressure::*,
    recovery::{GUEST_VA, GuestState, Recovery, STATE_VA},
    translation as wire,
};
unsafe extern "C" {
    fn enter_allocated(frame: *const NativeFrame, pc: u64, spsr: u64) -> !;
    fn returned_allocated();
    fn leave_exception() -> !;
}
pub fn enabled() -> bool {
    unsafe { ptr::read_volatile(&raw const (*shared()).mode) >= MODE }
}
pub fn fp() -> bool {
    unsafe { ptr::read_volatile(&raw const (*shared()).mode) == FP_MODE }
}
unsafe fn active() -> Export {
    unsafe { read_record(ACTIVE as *const Export) }
}
pub unsafe fn initialize() {
    unsafe {
        write_record(STAGING as *mut Stream, Stream::ZERO);
    }
}
pub unsafe fn receive(state: &mut wire::Lifecycle, p: &wire::Packet) -> Result<(), u64> {
    unsafe {
        let mut stream = read_record(STAGING as *const Stream);
        let complete = stream.ingest(state, p)?;
        write_record(STAGING as *mut Stream, stream);
        if complete {
            let export = ptr::read_unaligned(stream.bytes.as_ptr().cast::<Export>());
            export.validate(&state.source())?;
            write_record(ACTIVE as *mut Export, export);
            let mut code = [0u8; 528];
            let len = export.body_len as usize;
            code[..len].copy_from_slice(&export.body[..len]);
            if helper::enabled() {
                helper::patch(&mut code[..len]);
            }
            let delta = (returned_allocated as *const () as i64) - (CODE_VA as i64 + len as i64);
            if delta % 4 != 0 || !(-(1 << 27)..(1 << 27)).contains(&delta) {
                stop(REJECTED, 64)
            }
            code[len..len + 4].copy_from_slice(
                &(0x14000000u32 | ((delta / 4) as u32 & 0x03ffffff)).to_le_bytes(),
            );
            super::translation::replace_bytes(&code[..len + 4]);
            state.cached = 1;
            state.pending = 0;
            execute(*state);
        }
        Ok(())
    }
}
pub unsafe fn execute(state: wire::Lifecycle) -> ! {
    unsafe {
        let s = shared();
        let g = read_record(GUEST_VA as *const GuestState);
        if state.pending != 0
            || state.cached != 1
            || g.gpr[0] != DATA_VA
            || g.gpr[7] != DATA_VA + 128
        {
            stop(REJECTED, 60)
        }
        let mut f = read_record(&raw const (*s).initial);
        for (role, &n) in ENTRY.iter().enumerate() {
            f.x[n] = g.gpr[GPR_ORDER[role]];
        }
        for i in 0..16 {
            f.q[16 + i].copy_from_slice(&g.ymm[i][..16]);
        }
        f.sp = STACK_TOP;
        if fp() {
            f.fpcr = 0;
            f.fpsr = 0;
        }
        f.x[30] = returned_allocated as *const () as u64;
        write_record(&raw mut (*s).initial, f);
        ptr::write_volatile(&raw mut (*s).capture_pc, f.x[30]);
        ptr::write_volatile(&raw mut (*s).lifecycle, state);
        enter_allocated(&raw const (*s).initial, CODE_VA, 0x3c4 | f.nzcv);
    }
}
unsafe fn scalar(f: &NativeFrame, base: u64, loc: u64) -> u64 {
    unsafe {
        match loc & 255 {
            1 => f.x[(loc >> 8) as usize],
            3 => ptr::read_volatile((base + (loc >> 8)) as *const u64),
            _ => stop(PANIC, 61),
        }
    }
}
pub unsafe fn materialize(f: &NativeFrame, index: usize, complete: bool) -> GuestState {
    unsafe {
        let e = active();
        let site = &e.sites[index];
        let base = STACK_TOP - e.spill_bytes;
        if f.sp != if complete { STACK_TOP } else { base } {
            stop(NATIVE_FAULT, 62)
        }
        let mut g = read_record(GUEST_VA as *const GuestState);
        for (role, &guest) in GPR_ORDER.iter().enumerate() {
            g.gpr[guest] = scalar(f, base, site.roots[role]);
        }
        for i in 0..16 {
            g.ymm[i][..16].copy_from_slice(&f.q[(site.roots[16 + i] >> 8) as usize]);
        }
        let sum = scalar(f, base, site.roots[32]);
        let af = scalar(f, base, site.roots[33]);
        let nzcv = scalar(f, base, site.roots[34]);
        let flags = ((nzcv >> 29) & 1)
            | u64::from((sum as u8).count_ones() % 2 == 0) << 2
            | ((af ^ sum) & 16)
            | ((nzcv >> 30) & 1) << 6
            | ((nzcv >> 31) & 1) << 7
            | ((nzcv >> 28) & 1) << 11;
        g.rflags = (g.rflags & !0x8d5) | flags;
        g.rip = wire::SOURCE_VA
            + if complete {
                e.source_len
            } else {
                site.guest_offset
            };
        if fp() {
            // RN, masked finite ADDPS profile: ARM cumulative flags to x86 MXCSR.
            let a = f.fpsr;
            g.mxcsr |= ((a & 1)
                | ((a >> 7) & 1) << 1
                | ((a >> 1) & 1) << 2
                | ((a >> 2) & 1) << 3
                | ((a >> 3) & 1) << 4
                | ((a >> 4) & 1) << 5) as u32;
        }
        write_record(GUEST_VA as *mut GuestState, g);
        g
    }
}
pub unsafe fn site(pc: u64, f: &NativeFrame) -> (usize, u64, u64, u64) {
    unsafe {
        let e = active();
        for (i, s) in e.sites.iter().enumerate() {
            if pc == CODE_VA + s.offset {
                let word = u32::from_le_bytes(
                    e.body[s.offset as usize..s.offset as usize + 4]
                        .try_into()
                        .unwrap(),
                );
                let operand = f.x[((word >> 5) & 31) as usize];
                return (i, if i == 0 { 8 } else { 16 }, operand, s.guest_offset);
            }
        }
        stop(NATIVE_FAULT, 63)
    }
}
pub unsafe fn snapshot(index: usize) {
    unsafe {
        let e = active();
        copy_record(
            (SPILLS + index as u64 * 768) as *mut u8,
            (STACK_TOP - e.spill_bytes) as *const u8,
            768,
        );
    }
}
pub unsafe fn restore_native_fp() {
    unsafe {
        let r = STATE_VA as *const Recovery;
        let c = ptr::read_volatile(&raw const (*r).native_fpcr);
        let s = ptr::read_volatile(&raw const (*r).native_fpsr);
        asm!("msr fpcr, {0}","msr fpsr, {1}",in(reg)c,in(reg)s);
    }
}
pub unsafe fn completed() {
    unsafe {
        let f = read_record(&raw const (*shared()).captured);
        materialize(&f, 1, true);
        snapshot(3);
        restore_native_fp();
    }
}
pub unsafe fn dispatch_after_completion() -> ! {
    unsafe { leave_exception() }
}
