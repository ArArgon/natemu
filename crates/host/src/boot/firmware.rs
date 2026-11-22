//! M2 Part 2, the firmware boot mode (boot-time interpreter plan section 7; docs/restructure/firmware-mode.md): a firmware
//! image at the top of the 4 GiB space, read and fetched as RAM, its stores dropped as x86 drops a store to ROM
//! (`execution::prepare_with_firmware` places it), with every processor in the SDM's power-on state: the boot processor
//! (vCPU 0) at the reset vector, the others waiting for a start-up IPI. No kernel, no ACPI tables, no e820 map: the image
//! is the firmware, natemu its board.
use super::Loaded;
use natemu_abi::execution::{Cpu, FIRMWARE_END, SREG_CS, Segment};

/// Where the boot processor starts.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Start {
    /// The power-on state (`natemu_abi::power_on::power_on`): real mode, CS F000H with base FFFF0000H, EIP FFF0H.
    #[default]
    Reset,
    /// The same, but in 32-bit protected mode with flat segments at the reset vector FFFFFFF0H (CR0.PE set): the start a
    /// TDX module gives, which OVMF's reset vector takes to `Main32` (docs/restructure/real-code-suites.md).
    Flat32,
}
impl Start {
    /// `reset` or `flat32`.
    pub fn parse(text: &str) -> Result<Self, String> {
        match text {
            "reset" => Ok(Start::Reset),
            "flat32" => Ok(Start::Flat32),
            _ => Err(format!("firmware start {text:?}: reset or flat32")),
        }
    }
}

/// A firmware boot: the image (whole 4 KiB pages, at most `exec::MAX_FIRMWARE_BYTES`) and the boot processor's start.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Firmware {
    pub image: Vec<u8>,
    pub start: Start,
}

/// `flat32`'s CS: selector 08H, base 0, limit FFFFFFFFH, a 32-bit execute/read code segment, accessed (C09BH: G, D, P, S).
const FLAT_CODE: Segment = Segment {
    selector: 0x08,
    attributes: 0xc09b,
    limit: 0xffff_ffff,
    base: 0,
};
/// `flat32`'s SS, DS, ES, FS and GS: selector 10H, base 0, limit FFFFFFFFH, read/write data, accessed (C093H).
const FLAT_DATA: Segment = Segment {
    selector: 0x10,
    attributes: 0xc093,
    limit: 0xffff_ffff,
    base: 0,
};

/// The boot processor's state for `start`.
pub fn bsp(start: Start) -> Cpu {
    let mut cpu = natemu_abi::power_on::reset();
    if start == Start::Flat32 {
        cpu.cr0 |= natemu_abi::execution::CR0_PE;
        cpu.seg = [FLAT_DATA; 6];
        cpu.seg[SREG_CS] = FLAT_CODE;
        cpu.rip = 0xffff_fff0;
    }
    cpu
}

/// What the boot left in guest memory, for the reports: the image's range as the "kernel" and its one write, the boot
/// processor's state and the other processors' (the power-on state: they wait for a SIPI).
pub fn loaded(firmware: &Firmware) -> Loaded {
    let bytes = firmware.image.len() as u64;
    Loaded {
        setup: None,
        kernel: FIRMWARE_END - bytes,
        window: bytes,
        zero_page: 0,
        cmdline: 0,
        initrd: None,
        map: Vec::new(),
        cpu: bsp(firmware.start),
        ap: natemu_abi::power_on::reset(),
        writes: vec![(FIRMWARE_END - bytes, bytes)],
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use natemu_abi::execution::{SREG_DS, SREG_SS};

    #[test]
    fn starts() {
        let reset = bsp(Start::Reset);
        assert_eq!((reset.rip, reset.cr0, reset.seg[SREG_CS].base), (0xfff0, 0x6000_0010, 0xffff_0000));
        assert_eq!(reset.seg[SREG_CS].base + reset.rip, 0xffff_fff0);
        let flat = bsp(Start::Flat32);
        assert_eq!((flat.rip, flat.cr0), (0xffff_fff0, 0x6000_0011));
        assert_eq!(flat.seg[SREG_CS], FLAT_CODE);
        assert_eq!((flat.seg[SREG_SS], flat.seg[SREG_DS]), (FLAT_DATA, FLAT_DATA));
        // Everything else as at power-on.
        assert_eq!((flat.gpr, flat.rflags, flat.efer, flat.gdtr), (reset.gpr, 2, 0, reset.gdtr));
        assert_eq!(Start::parse("flat32"), Ok(Start::Flat32));
        assert!(Start::parse("main32").is_err());
        let l = loaded(&Firmware {
            image: vec![0; 0x1_0000],
            start: Start::Reset,
        });
        assert_eq!((l.kernel, l.window, l.writes), (0xffff_0000, 0x1_0000, vec![(0xffff_0000, 0x1_0000)]));
        assert_eq!(l.ap, natemu_abi::power_on::reset());
    }
}
