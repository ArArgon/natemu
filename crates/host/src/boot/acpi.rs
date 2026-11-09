//! ACPI tables of the virtual firmware (boot-features plan L2): RSDP, XSDT, FADT, FACS, DSDT, MADT and the AP wakeup
//! mailbox page, built by the host at load time into the map's ACPI NVS range and published through
//! `boot_params.acpi_rsdp_addr` ([`super::Inputs::acpi`]; the loader places every blob, so `Loaded::writes` carries them).
//!
//! Layouts: ACPI 6.5 (the plan's reference; the repo's snapshot is 6.6 chapter 5,
//! `artifacts/research/track01/source-snapshots/2026-09-03/acpi-6.6-ch05.txt`, whose offsets are the same for every
//! field written here). Contents: INV-03-03 claim 69 (the cold-boot manifest: RSDP/XSDT/FADT/FACS/DSDT/MADT; DSDT with
//! serial PNP0501, `_S5` and the enabled CPUs; MADT with the enabled APICs, the IOAPIC, the IRQ0 override and the
//! version-0 wakeup mailbox; PM base 0xb000; reset byte 0xb040 value 6; ACPI-only PM mode with a zero SMI command; no
//! S3/S4, hotplug, HPET or EFI tables), claims 22-24, and, where claim 69 names no value, QEMU 11.1.2's PC tables
//! (`-machine pc` under SeaBIOS, read back with `iasl -d`; owner decision: the Rust builder with QEMU's values): FADT
//! flags and the interrupt-model byte, century register, C2/C3 latencies, the SCI override (IRQ9 -> GSI9, level, active
//! high: QEMU's PIIX4 PM drives 1 = asserted), the LINT1 NMI entry, the RTC device. Nothing here knows a kernel.
//!
//! Deliberately absent: `_CST` and P_BLK (processors are `Device (ACPI0007)` objects without them, and the FADT's
//! P_LVL2/P_LVL3 latencies say C2/C3 are unsupported), so only C1 = HLT idle is reachable (plan §1a "Idle states");
//! GPE blocks (QEMU's PIIX4 has GPE0 at 0xafe0; a zero block is legal and no hotplug or GPE source is wired); the
//! disabled virtio-MMIO devices. Enabled devices are supplied by the same launch manifest as the MMIO registry.
//! Deviations from QEMU's PC tables, each for a stated reason, are
//! listed at [`fadt`] and in the plan's "Built (L2)" note. The platform constants below are the one description
//! L3/L4a/L4b/X3/X4 implement: if a device moves, change it here and the tables follow.

/// The machine the tables describe (claim 69, M23, M60; X3 for the local APIC).
pub mod platform {
    /// xAPIC MMIO base (IA32_APIC_BASE reset, X3).
    pub(crate) const LAPIC_BASE: u32 = 0xfee0_0000;
    /// The IOAPIC (L4a): ID 0 (QEMU's `ACPI_BUILD_IOAPIC_ID`), MMIO base, GSI base 0.
    pub(crate) const IOAPIC_BASE: u32 = 0xfec0_0000;
    pub(crate) const IOAPIC_ID: u8 = 0;
    /// ISA IRQ0 (PIT) enters the IOAPIC at GSI 2 (M23); every other ISA IRQ n is GSI n.
    pub(crate) const IRQ0_GSI: u32 = 2;
    /// COM1 (L3, M60): 8 ports at 0x3f8, ISA IRQ 4, edge, active high.
    pub const COM1_PORT: u16 = 0x3f8;
    pub(crate) const COM1_IRQ: u8 = 4;
    /// ACPI PM registers (L4b): PM1a event block (PM1_STS, PM1_EN) at PM_BASE, PM1a control at +4, the 24-bit PM timer
    /// at +8. The SCI is ISA IRQ 9 (FADT SCI_INT, ACPI 6.5 5.2.9: "a sharable, level, active low interrupt" unless an
    /// interrupt source override says otherwise); the MADT's override (5.2.12.5) makes it GSI 9, level, active high, as
    /// QEMU's PIIX4 PM drives it (1 = asserted).
    pub(crate) const PM_BASE: u16 = 0xb000;
    pub(crate) const PM1A_EVT: u16 = PM_BASE;
    pub(crate) const PM1_EVT_LEN: u8 = 4;
    pub(crate) const PM1A_CNT: u16 = PM_BASE + 4;
    pub(crate) const PM1_CNT_LEN: u8 = 2;
    pub(crate) const PM_TMR: u16 = PM_BASE + 8;
    pub(crate) const PM_TMR_LEN: u8 = 4;
    pub(crate) const SCI_IRQ: u8 = 9;
    /// MPS INTI flags of the SCI override: active high (01), level (11 << 2).
    pub(crate) const SCI_FLAGS: u16 = 0x000d;
    /// `_S5` sleep type for PM1a_CNT.SLP_TYP: SLP_TYP = 0 with SLP_EN is the shutdown request (M22).
    pub(crate) const S5_SLP_TYP: u8 = 0;
    /// The ACPI reset register (claim 69): a byte port; writing RESET_VALUE resets the machine.
    pub(crate) const RESET_PORT: u16 = 0xb040;
    pub(crate) const RESET_VALUE: u8 = 6;
    /// The RTC (L4b): index/data ports 0x70-0x71, ISA IRQ 8 (edge, active high); CMOS index of the century (QEMU's
    /// mc146818 `RTC_CENTURY`).
    pub(crate) const RTC_PORT: u16 = 0x70;
    pub(crate) const RTC_PORT_LEN: u8 = 2;
    pub(crate) const RTC_IRQ: u8 = 8;
    pub(crate) const RTC_CENTURY: u8 = 0x32;
}
use platform::*;

/// The built tables: the RSDP's address, the AP wakeup mailbox page (X4), and every blob at its guest-physical address.
#[derive(Debug, Clone)]
pub struct Tables {
    pub(crate) rsdp: u64,
    #[allow(dead_code)] // X4's responder watches it
    pub mailbox: u64,
    pub(crate) blobs: Vec<(u64, Vec<u8>)>,
}

/// One enabled modern virtio-MMIO device, shared by launch discovery and the MMIO registry.
#[derive(Clone, Copy, Debug)]
pub struct VirtioMmio {
    pub uid: u8,
    pub base: u32,
    pub gsi: u32,
}

const OEM_ID: &[u8; 6] = b"NATEMU";
const OEM_TABLE_ID: &[u8; 8] = b"NATEMU  ";
const CREATOR_ID: &[u8; 4] = b"NTMU";
const HEADER: usize = 36;

fn checksum(b: &[u8]) -> u8 {
    0u8.wrapping_sub(b.iter().fold(0u8, |s, &x| s.wrapping_add(x)))
}
fn put(t: &mut [u8], at: usize, v: &[u8]) {
    t[at..at + v.len()].copy_from_slice(v);
}
/// A system description table: the 36-byte header (ACPI 6.5 5.2.6) over `body`, checksummed.
fn table(signature: &[u8; 4], revision: u8, body: &[u8]) -> Vec<u8> {
    let mut t = vec![0u8; HEADER];
    put(&mut t, 0, signature);
    put(&mut t, 4, &((HEADER + body.len()) as u32).to_le_bytes());
    t[8] = revision;
    put(&mut t, 10, OEM_ID);
    put(&mut t, 16, OEM_TABLE_ID);
    put(&mut t, 24, &1u32.to_le_bytes()); // OEM revision
    put(&mut t, 28, CREATOR_ID);
    put(&mut t, 32, &1u32.to_le_bytes()); // creator revision
    t.extend_from_slice(body);
    t[9] = checksum(&t);
    t
}
/// Generic Address Structure (5.2.3.2): address space 1 = system I/O; access size 0 = undefined (legacy), 1 = byte.
fn gas(bits: u8, access: u8, port: u16) -> [u8; 12] {
    let mut g = [0u8; 12];
    (g[0], g[1], g[2], g[3]) = (1, bits, 0, access);
    put(&mut g, 4, &u64::from(port).to_le_bytes());
    g
}

/// The FADT (5.2.9, revision 6, minor 5: 276 bytes): not hardware-reduced (the PM1/PM timer registers of L4b exist).
fn fadt(facs: u64, dsdt: u64) -> Vec<u8> {
    let mut f = vec![0u8; 276 - HEADER];
    let mut at = |o: usize, v: &[u8]| put(&mut f, o - HEADER, v);
    at(36, &(facs as u32).to_le_bytes()); // FIRMWARE_CTRL (X_FIRMWARE_CTRL stays 0: "must be zero" when this is used)
    at(40, &(dsdt as u32).to_le_bytes()); // DSDT, and X_DSDT below
    // 44: ACPI 1.0's INT_MODEL, reserved since 2.0 ("should be zero but ... one should also be supported"); QEMU's 1.
    at(44, &[1]);
    // 45 Preferred_PM_Profile 0 (unspecified); 48 SMI_CMD 0 with ACPI_ENABLE/DISABLE 0: ACPI-only mode (claim 69; QEMU's
    // PC tables have SMI_CMD 0xb2 with SeaBIOS's SMM, which this machine has not).
    at(46, &u16::from(SCI_IRQ).to_le_bytes());
    at(56, &u32::from(PM1A_EVT).to_le_bytes());
    at(64, &u32::from(PM1A_CNT).to_le_bytes());
    at(76, &u32::from(PM_TMR).to_le_bytes());
    at(88, &[PM1_EVT_LEN, PM1_CNT_LEN, 0, PM_TMR_LEN]); // PM1_EVT_LEN, PM1_CNT_LEN, PM2_CNT_LEN, PM_TMR_LEN; no GPE blocks
    // P_LVL2_LAT > 100 and P_LVL3_LAT > 1000: C2 and C3 unsupported (QEMU's 0xfff).
    at(96, &0x0fffu16.to_le_bytes());
    at(98, &0x0fffu16.to_le_bytes());
    at(108, &[RTC_CENTURY]);
    // IAPC_BOOT_ARCH: LEGACY_DEVICES (COM1 is an ISA device), VGA Not Present; no 8042, CMOS RTC present. QEMU's PC
    // FADT is revision 1, which has no such field (Linux reads it from revision 2): these bits describe this machine.
    at(109, &0x0005u16.to_le_bytes());
    // Flags, QEMU's 0x80a5 except where noted: WBINVD (0), PROC_C1 (2), PWR_BUTTON clear (4: the fixed power button
    // of PM1, which the PIIX4 model has), SLP_BUTTON (5: none), FIX_RTC clear (6: RTC_STS/RTC_EN in PM1),
    // USE_PLATFORM_CLOCK (15); TMR_VAL_EXT clear: a 24-bit PM timer. Deviations: RESET_REG_SUP (10) set, for claim
    // 69's reset register; RTC_S4 (7) clear, since S4 is not supported (claim 22).
    at(
        112,
        &(1u32 | 1 << 2 | 1 << 5 | 1 << 10 | 1 << 15).to_le_bytes(),
    );
    at(116, &gas(8, 1, RESET_PORT));
    at(128, &[RESET_VALUE]);
    at(131, &[5]); // FADT minor version
    at(140, &dsdt.to_le_bytes());
    at(148, &gas(8 * PM1_EVT_LEN, 0, PM1A_EVT));
    at(172, &gas(8 * PM1_CNT_LEN, 0, PM1A_CNT));
    at(208, &gas(8 * PM_TMR_LEN, 0, PM_TMR));
    table(b"FACP", 6, &f)
}

/// The FACS (5.2.10, version 3): 64 bytes, no checksum, nothing else set (no S4BIOS, no 64-bit wake).
fn facs() -> Vec<u8> {
    let mut f = vec![0u8; 64];
    put(&mut f, 0, b"FACS");
    put(&mut f, 4, &64u32.to_le_bytes());
    f[32] = 3;
    f
}

/// AML (ACPI 6.5 chapter 20) for the few objects the DSDT needs.
mod aml {
    /// PkgLength (20.2.4): the encoded length counts its own bytes.
    fn pkg_length(body: usize) -> Vec<u8> {
        for n in 1..=4usize {
            let total = body + n;
            if n == 1 && total < 0x40 {
                return vec![total as u8];
            }
            if n > 1 && total < 1 << (4 + 8 * (n - 1)) {
                let mut v = vec![((n - 1) << 6 | (total & 0xf)) as u8];
                v.extend((0..n - 1).map(|i| (total >> (4 + 8 * i)) as u8));
                return v;
            }
        }
        panic!("AML package too long");
    }
    /// `op PkgLength body`.
    pub(super) fn package_op(op: &[u8], body: &[u8]) -> Vec<u8> {
        let mut v = op.to_vec();
        v.extend(pkg_length(body.len()));
        v.extend_from_slice(body);
        v
    }
    pub(super) fn name(seg: &[u8; 4], value: Vec<u8>) -> Vec<u8> {
        [&[0x08][..], seg, &value].concat()
    }
    pub(super) fn integer(v: u64) -> Vec<u8> {
        match v {
            0 => vec![0x00],
            1 => vec![0x01],
            2..=0xff => vec![0x0a, v as u8],
            0x100..=0xffff => [&[0x0b][..], &(v as u16).to_le_bytes()].concat(),
            0x1_0000..=0xffff_ffff => [&[0x0c][..], &(v as u32).to_le_bytes()].concat(),
            _ => [&[0x0e][..], &v.to_le_bytes()].concat(),
        }
    }
    pub(super) fn string(s: &str) -> Vec<u8> {
        [&[0x0d][..], s.as_bytes(), &[0]].concat()
    }
    /// EISAID (19.6.44): three compressed letters and four hex digits, as a DWord constant.
    pub(super) fn eisa_id(id: &str) -> Vec<u8> {
        let b = id.as_bytes();
        let l = |c: u8| u16::from(c - b'@');
        let hex = u16::from_str_radix(&id[3..], 16).unwrap();
        let v = [
            (l(b[0]) << 10 | l(b[1]) << 5 | l(b[2])).to_be_bytes(),
            hex.to_be_bytes(),
        ]
        .concat();
        [&[0x0c][..], &v].concat()
    }
    pub(super) fn package(elements: &[u64]) -> Vec<u8> {
        let mut body = vec![elements.len() as u8];
        body.extend(elements.iter().flat_map(|&e| integer(e)));
        package_op(&[0x12], &body)
    }
    pub(super) fn buffer(bytes: &[u8]) -> Vec<u8> {
        package_op(
            &[0x11],
            &[integer(bytes.len() as u64), bytes.to_vec()].concat(),
        )
    }
    pub(super) fn device(seg: &[u8; 4], terms: &[Vec<u8>]) -> Vec<u8> {
        package_op(&[0x5b, 0x82], &[seg.to_vec(), terms.concat()].concat())
    }
    /// `Scope (\SEG)`.
    pub(super) fn root_scope(seg: &[u8; 4], terms: &[Vec<u8>]) -> Vec<u8> {
        package_op(&[0x10], &[&[b'\\'][..], seg, &terms.concat()].concat())
    }
}

/// The DSDT (5.2.11.1, revision 2: 64-bit integers): `\_S5`, COM1, the RTC and one processor device per vCPU.
fn dsdt(vcpus: usize, virtio: &[VirtioMmio]) -> Vec<u8> {
    use aml::*;
    // COM1's resources (6.4): IO (Decode16, 0x3f8, 0x3f8, 1, 8), IRQNoFlags () {4} (edge, active high), end tag.
    let [lo, hi] = COM1_PORT.to_le_bytes();
    let irq = (1u16 << COM1_IRQ).to_le_bytes();
    let crs = [
        0x47, 0x01, lo, hi, lo, hi, 0x01, 0x08, 0x22, irq[0], irq[1], 0x79, 0x00,
    ];
    let com1 = device(
        b"COM1",
        &[
            name(b"_HID", eisa_id("PNP0501")),
            name(b"_UID", integer(1)),
            name(b"_CRS", buffer(&crs)),
        ],
    );
    // RTC (QEMU's `Device (RTC)` under its ISA bridge): IO (Decode16, 0x70, 0x70, 1, 2), IRQNoFlags () {8}.
    let [lo, hi] = RTC_PORT.to_le_bytes();
    let irq = (1u16 << RTC_IRQ).to_le_bytes();
    let crs = [
        0x47,
        0x01,
        lo,
        hi,
        lo,
        hi,
        0x01,
        RTC_PORT_LEN,
        0x22,
        irq[0],
        irq[1],
        0x79,
        0x00,
    ];
    let rtc = device(
        b"RTC_",
        &[
            name(b"_HID", eisa_id("PNP0B00")),
            name(b"_CRS", buffer(&crs)),
        ],
    );
    let mut sb = vec![com1, rtc];
    for d in virtio {
        // Memory32Fixed(ReadWrite, base, 4096), ExtendedInterrupt(ResourceConsumer,
        // Level, ActiveHigh, Exclusive, GSI), EndTag. ACPI 6.5 6.4.3.4/6.4.3.6.
        let mut crs = vec![0x86, 9, 0, 1];
        crs.extend(d.base.to_le_bytes());
        crs.extend(4096u32.to_le_bytes());
        crs.extend([0x89, 6, 0, 1, 1]);
        crs.extend(d.gsi.to_le_bytes());
        crs.extend([0x79, 0]);
        let seg: [u8; 4] = format!("V{:03X}", d.uid).into_bytes().try_into().unwrap();
        sb.push(device(
            &seg,
            &[
                name(b"_HID", string("LNRO0005")),
                name(b"_UID", integer(d.uid.into())),
                name(b"_CCA", integer(1)),
                name(b"_CRS", buffer(&crs)),
            ],
        ));
    }
    for v in 0..vcpus {
        let seg: [u8; 4] = format!("C{v:03X}").into_bytes().try_into().unwrap();
        sb.push(device(
            &seg,
            &[
                name(b"_HID", string("ACPI0007")),
                name(b"_UID", integer(v as u64)),
            ],
        ));
    }
    let s5 = name(
        b"_S5_",
        package(&[S5_SLP_TYP.into(), S5_SLP_TYP.into(), 0, 0]),
    );
    table(b"DSDT", 2, &[s5, root_scope(b"_SB_", &sb)].concat())
}

/// The MADT (5.2.12, revision 6): APIC IDs = processor UIDs = vCPU index (CPUID 1/0xB, X3), enabled; the IOAPIC;
/// ISA IRQ0 -> GSI 2 (flags 0: conforms to ISA, edge/high) and the SCI, IRQ9 -> GSI 9 level/high (QEMU's; not its PCI
/// link overrides for IRQs 5, 10, 11: no PCI devices); LINT1 of every processor is NMI; the version-0
/// multiprocessor wakeup mailbox (5.2.12.19 as of 6.5: 16 bytes). PCAT_COMPAT: the dual 8259 is present (L4a).
fn madt(vcpus: usize, mailbox: u64) -> Vec<u8> {
    let mut m = [LAPIC_BASE.to_le_bytes(), 1u32.to_le_bytes()].concat();
    for v in 0..vcpus as u8 {
        m.extend([0, 8, v, v, 1, 0, 0, 0]);
    }
    m.extend([1, 12, IOAPIC_ID, 0]);
    m.extend(IOAPIC_BASE.to_le_bytes());
    m.extend(0u32.to_le_bytes());
    m.extend([2, 10, 0, 0]);
    m.extend(IRQ0_GSI.to_le_bytes());
    m.extend(0u16.to_le_bytes());
    m.extend([2, 10, 0, SCI_IRQ]);
    m.extend(u32::from(SCI_IRQ).to_le_bytes());
    m.extend(SCI_FLAGS.to_le_bytes());
    m.extend([4, 6, 0xff, 0, 0, 1]);
    m.extend([0x10, 16, 0, 0, 0, 0, 0, 0]);
    m.extend(mailbox.to_le_bytes());
    table(b"APIC", 6, &m)
}

/// Builds the tables for `vcpus` processors with the RSDP at `base`, each next table 64-byte aligned after it, then
/// the mailbox page. Addresses wrap rather than overflow; the loader refuses any blob outside the ACPI NVS range.
pub fn build(vcpus: usize, base: u64) -> Tables {
    build_with_virtio(vcpus, base, &[])
}

/// As [`build`], advertising exactly the enabled devices from the launch manifest.
pub fn build_with_virtio(vcpus: usize, base: u64, virtio: &[VirtioMmio]) -> Tables {
    assert!(
        (1..=254).contains(&vcpus),
        "xAPIC MADT entries hold APIC IDs below 255"
    );
    let next = |at: u64, len: usize, align: u64| {
        (at.wrapping_add(len as u64)).wrapping_add(align - 1) & !(align - 1)
    };
    let (rsdp, xsdt) = (base, next(base, 36, 64));
    let fadt_at = next(xsdt, HEADER + 16, 64);
    let facs_at = next(fadt_at, 276, 64);
    let dsdt_at = next(facs_at, 64, 64);
    let dsdt = dsdt(vcpus, virtio);
    let madt_at = next(dsdt_at, dsdt.len(), 64);
    let madt_len = 44 + 8 * vcpus + 12 + 2 * 10 + 6 + 16;
    let mailbox = next(madt_at, madt_len, 4096);
    let madt = madt(vcpus, mailbox);
    debug_assert_eq!(madt.len(), madt_len);
    // RSDP (5.2.5.3, revision 2): no RSDT (RsdtAddress 0), the XSDT; checksum over 0..20, extended over all 36.
    let mut r = vec![0u8; 36];
    put(&mut r, 0, b"RSD PTR ");
    put(&mut r, 9, OEM_ID);
    r[15] = 2;
    put(&mut r, 20, &36u32.to_le_bytes());
    put(&mut r, 24, &xsdt.to_le_bytes());
    r[8] = checksum(&r[..20]);
    r[32] = checksum(&r);
    let xsdt_body = [fadt_at.to_le_bytes(), madt_at.to_le_bytes()].concat();
    let blobs = vec![
        (rsdp, r),
        (xsdt, table(b"XSDT", 1, &xsdt_body)),
        (fadt_at, fadt(facs_at, dsdt_at)),
        (facs_at, facs()),
        (dsdt_at, dsdt),
        (madt_at, madt),
        // The mailbox starts as Noop (all zero): written so the dump and the QEMU image carry it.
        (mailbox, vec![0u8; 4096]),
    ];
    Tables {
        rsdp,
        mailbox,
        blobs,
    }
}

#[cfg(test)]
mod tests {
    use super::super::{ACPI_NVS, E820_NVS, E820_RESERVED, Layout};
    use super::*;
    use natemu_abi::cpu::{Dynamic, cpuid};

    fn u32_at(b: &[u8], o: usize) -> u32 {
        u32::from_le_bytes(b[o..o + 4].try_into().unwrap())
    }
    fn u64_at(b: &[u8], o: usize) -> u64 {
        u64::from_le_bytes(b[o..o + 8].try_into().unwrap())
    }
    fn at(t: &Tables, gpa: u64) -> &[u8] {
        &t.blobs
            .iter()
            .find(|(a, _)| *a == gpa)
            .expect("a table at the pointer")
            .1
    }
    fn sum(b: &[u8]) -> u8 {
        b.iter().fold(0u8, |s, &x| s.wrapping_add(x))
    }

    /// Signatures, lengths, checksums and the pointer graph.
    #[test]
    fn checksums_lengths_pointers() {
        for n in 1..=4 {
            let t = build(n, ACPI_NVS.0);
            let r = at(&t, t.rsdp);
            assert_eq!(
                (&r[..8], r[15], u32_at(r, 16), u32_at(r, 20)),
                (&b"RSD PTR "[..], 2, 0, 36)
            );
            assert_eq!((sum(&r[..20]), sum(r)), (0, 0));
            for (gpa, b) in &t.blobs[1..6] {
                let len = if &b[..4] == b"FACS" {
                    64
                } else {
                    u32_at(b, 4) as usize
                };
                assert_eq!(len, b.len(), "{gpa:#x}");
                if &b[..4] != b"FACS" {
                    assert_eq!(sum(b), 0, "{}", String::from_utf8_lossy(&b[..4]));
                }
            }
            let x = at(&t, u64_at(r, 24));
            assert_eq!(&x[..4], b"XSDT");
            let (f, m) = (at(&t, u64_at(x, 36)), at(&t, u64_at(x, 44)));
            assert_eq!((&f[..4], f.len(), f[8], f[131]), (&b"FACP"[..], 276, 6, 5));
            assert_eq!((&m[..4], m[8]), (&b"APIC"[..], 6));
            assert_eq!(&at(&t, u32_at(f, 36).into())[..4], b"FACS");
            assert_eq!(
                u64_at(f, 132),
                0,
                "X_FIRMWARE_CTRL zero when FIRMWARE_CTRL is used"
            );
            assert_eq!(u64::from(u32_at(f, 40)), u64_at(f, 140));
            assert_eq!(&at(&t, u64_at(f, 140))[..4], b"DSDT");
            assert_eq!(t.mailbox % 4096, 0);
            assert!(
                t.blobs
                    .windows(2)
                    .all(|w| w[0].0 + w[0].1.len() as u64 <= w[1].0),
                "disjoint, in order"
            );
        }
    }

    /// MADT entries, read back by type.
    fn madt_entries(m: &[u8]) -> Vec<&[u8]> {
        let mut out = Vec::new();
        let mut o = 44;
        while o < m.len() {
            out.push(&m[o..o + m[o + 1] as usize]);
            o += m[o + 1] as usize;
        }
        assert_eq!(o, m.len());
        out
    }

    /// MADT <-> CPUID <-> LAPIC model <-> DSDT processors <-> mailbox <-> e820 through the CPU-mask capacity.
    #[test]
    fn cross_agreement() {
        let map = Layout::DEFAULT.e820().unwrap();
        let kind = |a: u64, len: u64| {
            map.iter()
                .find(|e| e.addr <= a && a + len <= e.addr + e.size)
                .map(|e| e.kind)
        };
        for n in [1, 2, 3, 4, 10, 16, 17, 32, 64] {
            let t = build(n, ACPI_NVS.0);
            let x = at(&t, u64_at(at(&t, t.rsdp), 24));
            let (f, m) = (at(&t, u64_at(x, 36)), at(&t, u64_at(x, 44)));
            let e = madt_entries(m);
            // LAPIC address = IA32_APIC_BASE's reset base (X3); IDs = CPUID's initial and x2APIC IDs per vCPU.
            assert_eq!(
                u64::from(u32_at(m, 36)),
                natemu_abi::cpu_profile::APIC_BASE_BSP_RESET & !0xfff
            );
            let lapics: Vec<_> = e
                .iter()
                .filter(|s| s[0] == 0)
                .map(|s| (s[2], s[3], u32_at(s, 4)))
                .collect();
            assert_eq!(lapics.len(), n);
            for (v, &(uid, id, flags)) in lapics.iter().enumerate() {
                let d = Dynamic {
                    vcpus: n as u64,
                    vcpu: v as u64,
                    cr4: 0,
                    apic_base: 0xfee0_0800,
                    misc_enable: 0,
                    xcr0: 1,
                };
                assert_eq!(u32::from(id), cpuid(1, 0, &d)[1] >> 24);
                assert_eq!(u32::from(id), cpuid(0xb, 0, &d)[3]);
                assert_eq!(
                    cpuid(1, 0, &d)[1] >> 16 & 0xff,
                    n as u32,
                    "logical processor count"
                );
                assert_eq!((uid, flags), (v as u8, 1));
                assert_ne!(
                    cpuid(1, 0, &d)[2] & 1 << 21,
                    0,
                    "x2APIC advertised; IDs < 255 use xAPIC entries"
                );
            }
            // The DSDT's processors: one ACPI0007 device per MADT UID, in order.
            let d = at(&t, u64_at(f, 140));
            for v in 0..n {
                let dev = [
                    &b"\x08_HID\x0dACPI0007\x00\x08_UID"[..],
                    &aml::integer(v as u64),
                ]
                .concat();
                assert!(d.windows(dev.len()).any(|w| w == dev), "processor {v}");
            }
            assert_eq!(d.windows(4).filter(|w| w == b"ACPI").count(), n);
            // IOAPIC, IRQ0 override, LINT1 NMI, mailbox.
            let io: Vec<_> = e.iter().filter(|s| s[0] == 1).collect();
            assert_eq!(
                (io.len(), io[0][2], u32_at(io[0], 4), u32_at(io[0], 8)),
                (1, IOAPIC_ID, IOAPIC_BASE, 0)
            );
            let iso: Vec<_> = e
                .iter()
                .filter(|s| s[0] == 2)
                .map(|s| (s[2], s[3], u32_at(s, 4), s[8]))
                .collect();
            assert_eq!(
                iso,
                [(0, 0, 2, 0), (0, 9, 9, 0x0d)],
                "IRQ0 -> GSI2 and the SCI, level/high"
            );
            assert_eq!(
                e.iter()
                    .filter(|s| s[0] == 4)
                    .map(|s| s.to_vec())
                    .collect::<Vec<_>>(),
                [vec![4, 6, 0xff, 0, 0, 1]]
            );
            let wake: Vec<_> = e.iter().filter(|s| s[0] == 0x10).collect();
            assert_eq!(
                (wake.len(), wake[0].len(), u64_at(wake[0], 8)),
                (1, 16, t.mailbox)
            );
            assert_eq!(&wake[0][2..8], &[0; 6], "mailbox version 0");
            // e820: every blob (tables and mailbox) in the ACPI NVS extent; the APICs in the reserved aperture.
            for (gpa, b) in &t.blobs {
                assert_eq!(kind(*gpa, b.len() as u64), Some(E820_NVS), "{gpa:#x}");
                assert!(ACPI_NVS.0 <= *gpa && gpa + b.len() as u64 <= ACPI_NVS.1);
            }
            assert_eq!(kind(LAPIC_BASE.into(), 4096), Some(E820_RESERVED));
            assert_eq!(kind(IOAPIC_BASE.into(), 4096), Some(E820_RESERVED));
            // FADT <-> the device set.
            assert_eq!(
                (
                    u16::from_le_bytes([f[46], f[47]]),
                    u32_at(f, 56),
                    u32_at(f, 64),
                    u32_at(f, 76)
                ),
                (9, 0xb000, 0xb004, 0xb008)
            );
            assert_eq!(
                (
                    u64_at(f, 152),
                    u64_at(f, 176),
                    u64_at(f, 212),
                    u64_at(f, 120),
                    f[128]
                ),
                (0xb000, 0xb004, 0xb008, 0xb040, 6)
            );
            assert_eq!(
                (u32_at(f, 48), u32_at(f, 80), u32_at(f, 84), f[92]),
                (0, 0, 0, 0),
                "no SMI command, no GPE blocks"
            );
            assert_eq!(
                (u32_at(f, 112), f[44]),
                (0x80a5 & !(1 << 7) | 1 << 10, 1),
                "QEMU's flags less RTC_S4, plus RESET_REG_SUP"
            );
            let rtc = [
                &b"RTC_\x08_HID"[..],
                &aml::eisa_id("PNP0B00"),
                b"\x08_CRS\x11\x10\x0a\x0d\x47\x01\x70\x00\x70\x00\x01\x02\x22\x00\x01",
            ]
            .concat();
            assert!(
                d.windows(rtc.len()).any(|w| w == rtc),
                "RTC: 0x70 length 2, IRQ 8"
            );
        }
    }

    /// AML encodings with known iasl outputs.
    #[test]
    fn aml_encodings() {
        assert_eq!(aml::eisa_id("PNP0501"), [0x0c, 0x41, 0xd0, 0x05, 0x01]);
        assert_eq!(aml::package(&[0, 0, 0, 0]), [0x12, 0x06, 0x04, 0, 0, 0, 0]);
        let long = aml::buffer(&[0u8; 100]);
        assert_eq!(
            &long[..4],
            &[0x11, 0x40 | (100 + 2 + 2) & 0xf, (100 + 2 + 2) >> 4, 0x0a]
        );
        assert_eq!(aml::integer(0x3f8), [0x0b, 0xf8, 0x03]);
    }
}
