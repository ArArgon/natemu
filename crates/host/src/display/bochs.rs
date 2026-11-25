//! The Bochs display function's registers (QEMU's `bochs-display`, as QEMU's `docs/specs/standard-vga.rst` describes its
//! MMIO BAR, with QEMU's `hw/display/bochs-display.c` read as the behaviour reference; no code copied) and its
//! configuration space.
//!
//! Identity: vendor 1234H, device 1111H, revision 2, class code 0380H (base class 03H display, subclass 80H other),
//! header type 0, subsystem 1AF4H:1100H, no interrupt pin, no capabilities. BAR0: the framebuffer, 32-bit prefetchable
//! memory of the video memory's size (as QEMU's); BAR2: these registers, 4 KiB of 32-bit memory.
//!
//! BAR2 (every field little-endian; an access of any width is taken byte by byte at each byte's address, as a
//! little-endian memory region holds them):
//!
//! | Offset | Contents |
//! | --- | --- |
//! | 000H-07FH | the EDID block ([`super::edid`]), read-only |
//! | 080H-4FFH | read 0, writes dropped (400H-41FH are the VGA ports on a VGA-class device; this one has none) |
//! | 500H-515H | the DISPI registers, 16 bits each at 500H + 2 x index |
//! | 516H-5FFH | read 0, writes dropped (no register past index 10) |
//! | 600H | the extended region's size: 8, read-only |
//! | 604H | the framebuffer's byte order: 1E1E1E1EH little-endian (the default), BEBEBEBEH big-endian |
//! | 608H-FFFH | read 0, writes dropped |
//!
//! A write to 604H changes the byte order only when it writes all four bytes (any width, in one access): BEBEBEBEH
//! selects big-endian, 1E1E1E1EH little-endian, any other value is dropped and the order stays (as QEMU's, which acts
//! on those two values only).
//!
//! DISPI registers (index: name): 0 ID (reads B0C5H; writes are stored and never read back), 1 XRES, 2 YRES, 3 BPP,
//! 4 ENABLE (ENABLED 01H is the only bit acted on; LFB_ENABLED 40H and the rest are stored), 5 BANK, 6 VIRT_WIDTH,
//! 7 VIRT_HEIGHT, 8 X_OFFSET, 9 Y_OFFSET (all read back as written), 10 VIDEO_MEMORY_64K (read-only: the video memory's
//! size in 64 KiB units). A register write only stores its value: nothing is refused and nothing in video memory is
//! cleared. A byte write changes that byte of its register (the other byte kept).
//!
//! The mode is evaluated after every write ([`Registers::mode`]), by `bochs-display`'s rule.
use super::Mode;
use crate::pci::configuration::{
    PciBarConfiguration, PciBarPrefetchable, PciClassCode, PciConfiguration, PciHeaderType,
    PciSubclass,
};
use crate::pci::BarKind;

pub const VENDOR: u16 = 0x1234;
pub const DEVICE: u16 = 0x1111;
pub const REVISION: u8 = 2;
/// QEMU's subsystem IDs for a device that sets none (Red Hat / Qumranet, QEMU).
pub const SUBSYSTEM: (u16, u16) = (0x1af4, 0x1100);
/// BAR2's length.
pub const REGISTERS_LEN: u64 = 0x1000;

/// The DISPI register indices.
pub const ID: usize = 0;
pub const XRES: usize = 1;
pub const YRES: usize = 2;
pub const BPP: usize = 3;
pub const ENABLE: usize = 4;
pub const BANK: usize = 5;
pub const VIRT_WIDTH: usize = 6;
pub const VIRT_HEIGHT: usize = 7;
pub const X_OFFSET: usize = 8;
pub const Y_OFFSET: usize = 9;
pub const VIDEO_MEMORY_64K: usize = 10;
const DISPI_COUNT: usize = 11;
/// What ID reads (`VBE_DISPI_ID5`).
pub const ID_VALUE: u16 = 0xb0c5;
/// ENABLE's ENABLED bit.
pub const ENABLED: u16 = 0x01;

/// BAR2's regions.
pub const EDID: u64 = 0x000;
pub const DISPI: u64 = 0x500;
const DISPI_END: u64 = DISPI + 2 * DISPI_COUNT as u64;
pub const EXTENDED: u64 = 0x600;
const EXTENDED_END: u64 = 0x608;
pub const BYTE_ORDER: u64 = 0x604;
/// What 600H reads: the extended region's size.
pub const EXTENDED_SIZE: u32 = 8;
pub const LITTLE_ENDIAN: u32 = 0x1e1e_1e1e;
pub const BIG_ENDIAN: u32 = 0xbebe_bebe;

/// The register file: the DISPI registers as written, and the byte order.
#[derive(Default)]
pub struct Registers {
    dispi: [u16; DISPI_COUNT],
    big_endian: bool,
}

impl Registers {
    /// An access's value: `size` bytes at `offset` of BAR2, little-endian.
    pub fn read(&self, edid: &[u8; 128], vram_len: u64, offset: u64, size: u64) -> u64 {
        (0..size).fold(0, |v, k| v | u64::from(self.byte(edid, vram_len, offset + k)) << (8 * k))
    }
    /// The byte at `at` of BAR2.
    fn byte(&self, edid: &[u8; 128], vram_len: u64, at: u64) -> u8 {
        let le = |v: u32| v.to_le_bytes()[(at & 3) as usize];
        match at {
            EDID..0x080 => edid[(at - EDID) as usize],
            DISPI..DISPI_END => {
                let index = ((at - DISPI) / 2) as usize;
                let value = match index {
                    ID => ID_VALUE,
                    VIDEO_MEMORY_64K => (vram_len >> 16) as u16,
                    _ => self.dispi[index],
                };
                value.to_le_bytes()[(at & 1) as usize]
            }
            EXTENDED..BYTE_ORDER => le(EXTENDED_SIZE),
            BYTE_ORDER..EXTENDED_END => le(if self.big_endian { BIG_ENDIAN } else { LITTLE_ENDIAN }),
            _ => 0,
        }
    }
    /// A write of `size` bytes of `value` at `offset` of BAR2, byte by byte (module documentation).
    pub fn write(&mut self, offset: u64, size: u64, value: u64) {
        let mut order = [None; 4];
        for k in 0..size {
            let (at, byte) = (offset + k, (value >> (8 * k)) as u8);
            match at {
                DISPI..DISPI_END => {
                    let index = ((at - DISPI) / 2) as usize;
                    if index != VIDEO_MEMORY_64K {
                        let mut bytes = self.dispi[index].to_le_bytes();
                        bytes[(at & 1) as usize] = byte;
                        self.dispi[index] = u16::from_le_bytes(bytes);
                    }
                }
                BYTE_ORDER..EXTENDED_END => order[(at - BYTE_ORDER) as usize] = Some(byte),
                _ => {}
            }
        }
        if let [Some(a), Some(b), Some(c), Some(d)] = order {
            match u32::from_le_bytes([a, b, c, d]) {
                BIG_ENDIAN => self.big_endian = true,
                LITTLE_ENDIAN => self.big_endian = false,
                _ => {}
            }
        }
    }
    /// The mode these registers select in `vram_len` bytes of video memory, or none: `bochs-display`'s rule. ENABLED
    /// set, BPP 32, XRES and YRES at least 64; rows VIRT_WIDTH x 4 bytes apart (VIRT_WIDTH 0 means XRES), from
    /// Y_OFFSET x stride + X_OFFSET x 4; and `offset + stride x height` within the video memory.
    ///
    /// One check `bochs-display` does not make: VIRT_WIDTH (when set) at least XRES. With narrower rows the last row's
    /// pixels would run past `offset + stride x height`, past the video memory at its end; QEMU then shows rows that
    /// overlap. Linux's driver always sets VIRT_WIDTH to the framebuffer's pitch, at least XRES.
    pub fn mode(&self, vram_len: u64) -> Option<Mode> {
        let r = |i: usize| u64::from(self.dispi[i]);
        let (width, height) = (r(XRES), r(YRES));
        let virt_width = if r(VIRT_WIDTH) == 0 { width } else { r(VIRT_WIDTH) };
        let stride = virt_width * 4;
        let offset = r(Y_OFFSET) * stride + r(X_OFFSET) * 4;
        let valid = self.dispi[ENABLE] & ENABLED != 0
            && r(BPP) == 32
            && width >= 64
            && height >= 64
            && virt_width >= width
            && offset + stride * height <= vram_len;
        valid.then_some(Mode {
            width: width as u32,
            height: height as u32,
            stride: stride as u32,
            offset,
            big_endian: self.big_endian,
        })
    }
}

struct Subclass(u8);
impl PciSubclass for Subclass {
    fn get_register_value(&self) -> u8 {
        self.0
    }
}

/// The function's BAR requests: BAR0 the framebuffer of `vram_len` bytes, BAR2 the registers.
pub fn requests(vram_len: u64) -> Vec<(u8, BarKind, u64)> {
    vec![(0, BarKind::Mem32, vram_len), (2, BarKind::Mem32, REGISTERS_LEN)]
}

/// The configuration space of the identity above, with `requests`' BARs.
pub fn configuration(requests: &[(u8, BarKind, u64)]) -> PciConfiguration {
    let mut config = PciConfiguration::new(
        VENDOR,
        DEVICE,
        REVISION,
        PciClassCode::DisplayController,
        &Subclass(0x80),
        None,
        PciHeaderType::Device,
        SUBSYSTEM.0,
        SUBSYSTEM.1,
        None,
    );
    for &(index, kind, len) in requests {
        // The framebuffer prefetchable (as QEMU's), the registers not.
        let prefetch = if index == 0 {
            PciBarPrefetchable::Prefetchable
        } else {
            PciBarPrefetchable::NotPrefetchable
        };
        let bar = PciBarConfiguration::new(index.into(), len, kind.into(), prefetch);
        config.add_pci_bar(&bar).expect("display BAR");
    }
    config
}
