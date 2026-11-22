//! The processor states of the SDM's "Processor State After Reset" (Vol. 3A 12.1.1, Table 12-1): the power-on and RESET
//! column ([`power_on`]), the INIT column ([`init`]) and what a start-up IPI then changes ([`sipi`]). Pure functions on a
//! [`Cpu`] (boot-interpreter plan section 6): the host calls [`power_on`] (with [`crate::cpu::power_on_msrs`]) for a
//! firmware boot, the resident [`init`] when it takes an INIT and [`sipi`] when a waiting CPU takes its SIPI.
//!
//! Built to the table itself; the tests compare every field with its values, which are also those of the reference model
//! `tools/models/initsipi_model.py` `init_state` over `segment_model.py` `reset_state` (M2 Part 2) where it states them:
//!
//! - both columns: EFLAGS 2, EIP FFF0H, CR2-CR4 0, CS F000H with base FFFF0000H, limit FFFFH and attributes "Present, R/W,
//!   Accessed" (9BH: a readable, accessed code segment, as KVM loads it; segment_model B15), SS DS ES FS GS 0 / 0 / FFFFH
//!   (93H), GDTR and IDTR base 0 limit FFFFH, LDTR and TR selector 0 base 0 limit FFFFH "Present, R/W" (KVM's types 82H,
//!   an LDT, and 8BH, a busy TSS: [`LDTR_RESET`], [`TR_RESET`]), EDX the profile's signature (CPUID.1:EAX), EAX 0 (no
//!   BIST) and every other GPR (R8-R15 too) 0, DR0-DR3 0, DR6
//!   FFFF0FF0H, DR7 400H, IA32_EFER 0 and the FS and GS bases 0 (they are `Cpu` fields: [`Cpu::seg`]), CPL 0. CR8 is the
//!   local APIC's TPR ([`Cpu::cr8`]), which INIT and RESET clear with the rest of the APIC (13.4.7.3): 0.
//! - power-on and RESET only: CR0 60000010H, x87 FCW 0040H ([`FCW_RESET`]), XMM 0, MXCSR 1F80H, XCR0 1.
//! - INIT keeps CR0.CD and CR0.NW (Table 12-1 note 2: CR0 = CD and NW as before | ET), and leaves the x87, SSE and AVX
//!   state, MXCSR and XCR0 unchanged; and every MSR but EFER and the FS/GS bases (initsipi_model A4: MTRRs, PAT,
//!   MISC_ENABLE, SYSENTER, STAR/LSTAR, KERNEL_GS_BASE, TSC, the APIC base): nothing of the MSR record
//!   ([`crate::cpu::Msrs`]) changes, so there is no INIT function for it. IA32_XSS and PKRU, which INIT also keeps, are not
//!   in the profile (no XSAVES, no protection keys).
//!
//! Not `Cpu` fields, and so the caller's: the x87 registers, FSW and FTW of the guest-state page (power-on: +0.0, 0,
//! 5555H; INIT keeps them), the upper YMM halves, the local APIC record, the TSC (power-on: 0, the MSR record's offset),
//! and the translations and shadows INIT invalidates. Fields only the host or the resident's bookkeeping own stay as they
//! are ([`Cpu::no_afp`], [`Cpu::accurate_fp`]); [`Cpu::stack_traps`] is cleared with CR3, as a CR3 load clears it.
use crate::cpu_profile as profile;
use crate::execution::{
    CR0_ET, Cpu, DR_RESET, LDTR_RESET, MXCSR_RESET, SREG_CS, Segment, TABLE_RESET, TR_RESET,
};

/// CR0 at power-on and RESET: CD | NW | ET.
pub const CR0_RESET: u64 = 0x6000_0010;
pub const CR0_CD: u64 = 1 << 30;
pub const CR0_NW: u64 = 1 << 29;
/// EIP at power-on, RESET and INIT (CS.base + EIP = FFFFFFF0H, the reset vector).
pub const EIP_RESET: u64 = 0xfff0;
/// CS after power-on, RESET and INIT: selector F000H, base FFFF0000H, limit FFFFH, "Present, R/W, Accessed" (9BH).
pub const CS_RESET: Segment = Segment {
    selector: 0xf000,
    attributes: 0x9b,
    limit: 0xffff,
    base: 0xffff_0000,
};
/// SS, DS, ES, FS and GS after power-on, RESET and INIT: selector 0, base 0, limit FFFFH, "Present, R/W, Accessed" (93H).
pub const DATA_RESET: Segment = Segment {
    selector: 0,
    attributes: 0x93,
    limit: 0xffff,
    base: 0,
};
/// The x87 FPU control word at power-on and RESET (Table 12-1: 0040H; FNINIT's is [`crate::execution::FCW_INIT`]).
/// QEMU and KVM start a CPU with 037FH instead (FNINIT's); no instruction of the interpreter's first version reads it.
pub const FCW_RESET: u64 = 0x40;
/// EDX after power-on, RESET and INIT: the processor signature, the profile's CPUID.1:EAX (`cpu_profile.json`'s
/// `identity.signature`).
pub const SIGNATURE: u64 = {
    let mut i = 0;
    while profile::CPUID[i].0 != 1 {
        i += 1;
    }
    profile::CPUID[i].2[0] as u64
};
const RDX: usize = 2;

/// What both columns of Table 12-1 set alike (everything but CR0 and the floating-point state).
fn both(cpu: &mut Cpu) {
    cpu.gpr = [0; 16];
    cpu.gpr[RDX] = SIGNATURE;
    cpu.rip = EIP_RESET;
    cpu.rflags = 2;
    (cpu.cr2, cpu.cr3, cpu.cr4, cpu.cr8) = (0, 0, 0, 0);
    cpu.efer = 0;
    cpu.cpl = 0;
    cpu.dr = DR_RESET;
    cpu.seg = [DATA_RESET; 6];
    cpu.seg[SREG_CS] = CS_RESET;
    (cpu.ldtr, cpu.tr) = (LDTR_RESET, TR_RESET);
    (cpu.gdtr, cpu.idtr) = (TABLE_RESET, TABLE_RESET);
    cpu.mov_ss = 0;
    cpu.stack_traps = 0;
}

/// Puts `cpu` into the power-on and RESET state (Table 12-1, "Power up / Reset" column).
pub fn power_on(cpu: &mut Cpu) {
    both(cpu);
    cpu.cr0 = CR0_RESET;
    cpu.xmm = [[0; 2]; 16];
    cpu.mxcsr = MXCSR_RESET;
    cpu.fcw = FCW_RESET;
    cpu.xcr0 = profile::XCR0_RESET;
}

/// A processor at power-on ([`power_on`]), the fields Table 12-1 does not name zero (the host's own: [`Cpu::no_afp`],
/// [`Cpu::accurate_fp`], [`Cpu::stack_traps`]): the firmware boot mode's start (docs/restructure/firmware-mode.md).
pub fn reset() -> Cpu {
    let mut cpu = Cpu {
        gpr: [0; 16],
        rip: 0,
        rflags: 0,
        cr0: 0,
        cr3: 0,
        cr4: 0,
        efer: 0,
        cpl: 0,
        cr2: 0,
        cr8: 0,
        dr: [0; 6],
        seg: [Segment::NULL; 6],
        ldtr: Segment::NULL,
        tr: Segment::NULL,
        gdtr: TABLE_RESET,
        idtr: TABLE_RESET,
        mov_ss: 0,
        xmm: [[0; 2]; 16],
        mxcsr: 0,
        fcw: 0,
        xcr0: 0,
        no_afp: 0,
        accurate_fp: 0,
        stack_traps: 0,
    };
    power_on(&mut cpu);
    cpu
}

/// Puts `cpu` into the INIT state (Table 12-1, INIT column): as [`power_on`], but CR0.CD and CR0.NW, the x87 and SSE state
/// and XCR0 kept.
pub fn init(cpu: &mut Cpu) {
    let kept = cpu.cr0 & (CR0_CD | CR0_NW);
    both(cpu);
    cpu.cr0 = kept | CR0_ET;
}

/// The start a SIPI with `vector` gives a CPU waiting for one (in the INIT state): CS selector VV00H and base VV000H, EIP
/// 0; CS's hidden limit and attributes, and everything else, as INIT left them (initsipi_model A3; KVM
/// `kvm_vcpu_deliver_sipi_vector`, QEMU `cpu_x86_load_seg_cache_sipi`).
pub fn sipi(cpu: &mut Cpu, vector: u8) {
    let cs = &mut cpu.seg[SREG_CS];
    cs.selector = u16::from(vector) << 8;
    cs.base = u64::from(vector) << 12;
    cpu.rip = 0;
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::execution::{SREG_DS, SREG_ES, SREG_FS, SREG_GS, SREG_SS, Table, flat_segments};

    /// A running 64-bit CPU with every field `init` resets or keeps set to something else.
    fn busy() -> Cpu {
        let mut seg = flat_segments(0);
        (seg[SREG_FS].base, seg[SREG_GS].base) = (0x7fff_0000_1000, 0xffff_8000_0000_2000);
        Cpu {
            gpr: core::array::from_fn(|i| 0x1111 * (i as u64 + 1)),
            rip: 0xffff_ffff_8100_0000,
            rflags: 0x246,
            cr0: 0x8005_0033 | CR0_CD | CR0_NW,
            cr3: 0x1234_5000,
            cr4: 0x3606f0,
            efer: 0xd01,
            cpl: 3,
            cr2: 0xdead_beef,
            cr8: 0xf,
            dr: [1, 2, 3, 4, 0xffff_4ff1, 0x401],
            seg,
            ldtr: Segment {
                selector: 0x50,
                attributes: 0x82,
                limit: 0x1f,
                base: 0x9000,
            },
            tr: Segment {
                selector: 0x40,
                attributes: 0x8b,
                limit: 0x67,
                base: 0xa000,
            },
            gdtr: Table {
                base: 0xb000,
                limit: 0x7f,
            },
            idtr: Table {
                base: 0xc000,
                limit: 0xfff,
            },
            mov_ss: 1,
            xmm: [[0x5555, 0xaaaa]; 16],
            mxcsr: 0x9fc0,
            fcw: 0x27f,
            xcr0: 7,
            no_afp: 1,
            accurate_fp: 1,
            stack_traps: 3,
        }
    }

    /// `initsipi_model.init_state()` (over `segment_model.reset_state`), field by field in the model's terms: rip, rflags,
    /// cs and the hidden parts (selector, attributes; base, limit), cr0 from CD/NW, cr2, cr4, efer, gdtr and idtr, the
    /// GPRs (rdx the signature), dr0-dr3, dr6, dr7, fs_base, gs_base; plus CR3 and CPL 0 (the model's state starts there).
    fn assert_model(c: &Cpu, cd_nw: u64, sipi: Option<u64>) {
        let cs_sel = sipi.map_or(0xF000, |v| v << 8);
        let (rip, cs_base) = sipi.map_or((0xFFF0, 0xFFFF0000), |v| (0, v << 12));
        assert_eq!((c.rip, c.rflags, c.cpl), (rip, 2, 0));
        assert_eq!(c.cr0, cd_nw & 0x60000000 | 0x10);
        assert_eq!((c.cr2, c.cr3, c.cr4, c.efer), (0, 0, 0, 0));
        let cs = c.seg[SREG_CS];
        assert_eq!(
            (cs.selector as u64, cs.attributes, cs.base, cs.limit),
            (cs_sel, 0x9B, cs_base, 0xFFFF)
        );
        for r in [SREG_ES, SREG_SS, SREG_DS, SREG_FS, SREG_GS] {
            let s = c.seg[r];
            assert_eq!(
                (s.selector, s.attributes, s.base, s.limit),
                (0, 0x93, 0, 0xFFFF),
                "segment {r}"
            );
        }
        assert_eq!(
            (c.gdtr.base, c.gdtr.limit, c.idtr.base, c.idtr.limit),
            (0, 0xFFFF, 0, 0xFFFF)
        );
        // rax rcx rdx rbx rsp rbp rsi rdi (R8-R15 too): 0 but rdx, the signature.
        assert_eq!(
            c.gpr,
            [0, 0, 0x306c3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        );
        assert_eq!(c.dr, [0, 0, 0, 0, 0xFFFF0FF0, 0x400]);
        assert_eq!((c.seg[SREG_FS].base, c.seg[SREG_GS].base), (0, 0));
        // LDTR and TR (Table 12-1: selector 0, base 0, limit FFFFH, present; KVM's types 82H and 8BH).
        assert_eq!((c.ldtr, c.tr), (LDTR_RESET, TR_RESET));
        let (l, t) = (c.ldtr, c.tr);
        assert_eq!(
            (l.selector, l.base, l.limit, l.attributes),
            (0, 0, 0xFFFF, 0x82)
        );
        assert_eq!(
            (t.selector, t.base, t.limit, t.attributes),
            (0, 0, 0xFFFF, 0x8B)
        );
        // The local APIC's TPR (the model's apic tpr 0) and the MOV SS shadow.
        assert_eq!((c.cr8, c.mov_ss), (0, 0));
    }

    /// EDX is the profile's signature, which `tools/models/cpu_profile.json` states (`identity.signature`, the model's
    /// source for rdx).
    #[test]
    fn signature_is_the_profiles() {
        let json = include_str!("../../../tools/models/cpu_profile.json");
        let at = json.find("\"signature\": \"").unwrap() + "\"signature\": \"".len();
        let text = &json[at..at + json[at..].find('"').unwrap()];
        assert_eq!(
            SIGNATURE,
            u64::from_str_radix(text.trim_start_matches("0x"), 16).unwrap()
        );
        assert_eq!(SIGNATURE, 0x306c3);
    }

    #[test]
    fn init_is_the_models_init_state() {
        for kept in [0, CR0_CD, CR0_NW, CR0_CD | CR0_NW] {
            let mut c = busy();
            c.cr0 = 0x8005_0033 | kept;
            init(&mut c);
            assert_model(&c, kept, None);
            // Kept by INIT (initsipi_model KEPT_BY_INIT): x87 control, SSE, MXCSR, XCR0; and the host's fields.
            let b = busy();
            assert_eq!(
                (c.xmm, c.mxcsr, c.fcw, c.xcr0),
                (b.xmm, b.mxcsr, b.fcw, b.xcr0)
            );
            assert_eq!((c.no_afp, c.accurate_fp, c.stack_traps), (1, 1, 0));
        }
    }

    #[test]
    fn power_on_is_the_models_reset_state_with_reset_cr0_and_fpu() {
        let mut c = busy();
        power_on(&mut c);
        // segment_model.reset_state's default cd_nw is 60000000H: CR0 = 60000010H.
        assert_model(&c, 0x60000000, None);
        assert_eq!(c.cr0, 0x6000_0010);
        assert_eq!(
            (c.xmm, c.mxcsr, c.fcw, c.xcr0),
            ([[0; 2]; 16], 0x1F80, 0x40, 1)
        );
        assert_eq!((c.no_afp, c.accurate_fp), (1, 1));
    }

    /// `reset` is `power_on` over any state, with the host's own fields zero.
    #[test]
    fn reset_is_power_on() {
        let mut c = busy();
        power_on(&mut c);
        (c.no_afp, c.accurate_fp, c.stack_traps) = (0, 0, 0);
        assert_eq!(reset(), c);
        assert_model(&reset(), 0x60000000, None);
    }

    #[test]
    fn sipi_is_the_models_sipi_state() {
        for vector in [0x00u8, 0x08, 0x9A, 0xFF] {
            let mut c = busy();
            init(&mut c);
            sipi(&mut c, vector);
            assert_model(&c, CR0_CD | CR0_NW, Some(u64::from(vector)));
        }
        // From a power-on state too (a BSP's INIT-SIPI is never taken, but the rule is the same): only CS and EIP move.
        let (mut a, mut b) = (busy(), busy());
        power_on(&mut a);
        power_on(&mut b);
        sipi(&mut b, 0x9A);
        b.seg[SREG_CS] = a.seg[SREG_CS];
        b.rip = a.rip;
        assert_eq!(a, b);
    }
}
