//! ENTER frame construction. Register state commits only after the final stack permission probe.
use super::*;
use segments::{Exception, linear, pages};

fn word(s: &mut State, at: u64, width: u64, write: bool, value: u64) -> Result<u64, Exception> {
    const SS: Exception = [12, 0, 0];
    let last = at.wrapping_add(width - 1);
    if !paging::canonical(at) || !paging::canonical(last) || last < at {
        return Err(SS);
    }
    if s.cpu.alignment_check() && at & (width - 1) != 0 {
        return Err([17, 0, 0]);
    }
    if (at & 4095) + width > 4096 {
        let mut bytes = value.to_le_bytes();
        if write {
            unsafe { asm!("dmb ish") };
        }
        linear(s, at, &mut bytes[..width as usize], write, false, SS)?;
        unsafe {
            if write {
                asm!("dsb ish")
            } else {
                asm!("dmb ish")
            }
        };
        return Ok(u64::from_le_bytes(bytes));
    }
    let gpa = pages(s, at, width, write, false, SS)?[0];
    if !mmio::ram(gpa) {
        mmio::system_access(s);
    }
    let Some(p) = guest_ram().range(gpa, width) else {
        halt(s, stop::SHADOW)
    };
    // Each aligned frame word must remain indivisible to peers; byte-copying a push would weaken x86 atomicity.
    let result = if write {
        if cache::translated(gpa) {
            FetchWrites(s).code_write(gpa);
        }
        // SAFETY: checked RAM range, held against split-lock exclusion, with the scalar's natural width.
        unsafe { rotate::store(p, width, value) };
        cache::rechecked(gpa);
        value
    } else {
        // SAFETY: same checked range and exclusion as the store path.
        unsafe { rotate::load(p, width) }
    };
    watch::access(s, at, width, write);
    Ok(result)
}

pub(super) fn enter(s: &mut State, i: System) -> Result<(), Exception> {
    const SS: Exception = [12, 0, 0];
    let width = u64::from(i.size);
    let bp = s.cpu.gpr[5];
    let mut sp = s.cpu.gpr[4];
    let push = |s: &mut State, sp: &mut u64, value: u64| -> Result<(), Exception> {
        let next = sp.wrapping_sub(width);
        word(s, next, width, true, value)?;
        *sp = next;
        Ok(())
    };
    // Frame stores occur in order, so a later fault can leave earlier stores visible while RSP/RBP stay unchanged.
    // This is the retained AMD-observed prefix policy; no Intel-specific prefix-survival claim is made.
    push(s, &mut sp, bp)?;
    let frame = sp;
    if i.cr != 0 {
        for level in 1..i.cr {
            let value = word(
                s,
                bp.wrapping_sub(u64::from(level) * width),
                width,
                false,
                0,
            )?;
            push(s, &mut sp, value)?;
        }
        push(s, &mut sp, frame)?;
    }
    sp = sp.wrapping_sub(i.disp as u64);
    // SDM Vol.3C 31.4.4: ENTER faults as if writing the byte at final SP. This is a permission probe, not a store
    // or a data-breakpoint access; it applies even when nesting is zero.
    pages(s, sp, 1, true, false, SS)?;
    s.cpu.gpr[4] = sp;
    s.cpu.gpr[5] = if width == 2 {
        bp & !0xffff | frame & 0xffff
    } else {
        frame
    };
    Ok(())
}
