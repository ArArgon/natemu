//! K1's exact SGI regression. Runs the guard from its first word with a test breakpoint at the stop PC.
use super::{Block, KICK_SGI, NativeFrame, SRA, State, cache, halt, load, resume_interrupted, stop};
use natemu_abi::execution::cache::{CODE_VA, DEAD, GUARDED, GUARD_LEAD, PC_HINTS, SYSTEM, ctlb_entry};

pub(super) const BRK_IMM: u64 = 0x53;
const BRK_WORD: u32 = 0xd420_0000 | (BRK_IMM as u32) << 5;
const BODY: u64 = GUARD_LEAD as u64 / 4;
const MISS: u64 = 16;
const LIVE_R10: u64 = 0x1122_3344_5566_7788;

pub(super) fn entry(s: &mut State, f: &mut NativeFrame, code: &Block) -> Option<u64> {
    let c = cache::control();
    let [owner, trigger, layout, word] = c.guard_stop_test;
    let count = c.blocks as usize;
    if layout == 0 || s.cpu.rip != trigger || owner == trigger && code.number as usize + 1 == count {
        return None;
    }
    let fail = |s: &mut State, detail, expected, actual| -> ! {
        s.native = [detail, expected, actual];
        halt(s, stop::BAD_REPLY)
    };
    let Some(n) = count.checked_sub(3) else {
        fail(s, 1, 3, count as u64);
    };
    let (b, zero, after) = (cache::block(n), cache::block(n + 1), cache::block(n + 2));
    if b.rip != owner {
        fail(s, 2, owner, b.rip);
    }
    if b.flags & (DEAD | GUARDED) != GUARDED || b.code_size != 24 || !cache::guards_on() {
        fail(s, 2, u64::from(GUARDED) << 32 | 24, u64::from(b.flags) << 32 | u64::from(b.code_size));
    }
    if zero.flags & SYSTEM == 0 || zero.code != b.code + b.code_size || zero.code_size != 0 {
        fail(s, 3, u64::from(b.code + b.code_size), u64::from(zero.code));
    }
    let correct = match layout {
        1 => after.flags & SYSTEM != 0 && after.code == zero.code && after.code_size == 0,
        2 => after.flags & GUARDED == 0 && after.code == b.code + 32 && after.code_size != 0,
        _ => false,
    };
    if !correct {
        fail(s, 4, u64::from(b.code), u64::from(after.code));
    }
    let (miss, word) = (word >= MISS, word % MISS);
    if !(if miss { word <= 6 || (8..BODY).contains(&word) } else { word <= 7 || word == BODY }) {
        fail(s, 5, layout, word);
    }
    let expected = if miss { 0 } else { ctlb_entry(b.rip, b.source_gpa[0]) };
    let slot = cache::guard_stop_ctlb(b, miss);
    if slot != expected {
        fail(s, 6, expected, slot);
    }
    let pc = if word == BODY { b.entry() } else { b.guard_entry() + word * 4 };
    let offset = pc - CODE_VA;
    let hint = cache::control().pc_hints[(offset >> 2) as usize % PC_HINTS];
    if hint == b.number {
        fail(s, 7, pc, u64::from(hint));
    }
    let original = cache::word(offset);
    cache::rewrite(offset, BRK_WORD);
    cache::control().guard_stop_break = [pc, u64::from(original), 0];
    // Seed a live register in the entry frame without changing `s.cpu`: if an exact SGI at a guard word skips
    // banking (`at_once`), the recorded state keeps the stale `r10` instead of `LIVE_R10`.
    f.x[SRA[10] as usize] = LIVE_R10;
    Some(b.guard_entry())
}

#[inline(never)] // Like `interrupted`, this path banks its own exception-stack state.
pub(super) unsafe fn breakpoint(frame: &NativeFrame, elr: u64, spsr: u64) -> ! {
    let [pc, _, rung] = cache::control().guard_stop_break;
    let mut s = unsafe { load() };
    if pc == 0 || pc != elr || rung > 1 {
        s.native = [8, pc, elr];
        halt(&s, stop::BAD_REPLY)
    }
    // Leave the breakpoint in place until the SGI arrives. Ring once on the first hit (`rung == 0`);
    // if EL0 resumes before the GIC delivers the SGI and hits the same BRK again (`rung == 1`),
    // resume at `elr` without ringing a second time so the SGI cannot land at a later word.
    if rung == 0 {
        cache::control().guard_stop_break[2] = 1;
        cache::kick(s.vcpu as usize);
    }
    unsafe { resume_interrupted(&s, frame, elr, spsr) }
}

pub(super) fn interrupt(s: &State, intid: u64, elr: u64) -> bool {
    let [pc, original, rung] = cache::control().guard_stop_break;
    if pc == 0 {
        return false;
    }
    if intid != KICK_SGI || pc != elr || rung != 1 {
        let mut s = *s;
        s.native = [9, pc, elr];
        halt(&s, stop::BAD_REPLY)
    }
    cache::rewrite(pc - CODE_VA, original as u32);
    cache::control().guard_stop_break = [0; 3];
    true
}
