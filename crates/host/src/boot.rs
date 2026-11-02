//! Linux x86 64-bit boot protocol loader (boot-features plan L1).
//!
//! Follows only the published protocol: `Documentation/arch/x86/boot.rst` (v6.12.107; "64-bit Boot Protocol", the
//! setup-header field table, "Loading The Rest of The Kernel", the init_size runtime-start rule),
//! `Documentation/arch/x86/zero-page.rst` and `arch/x86/include/uapi/asm/bootparam.h` (offsets). Nothing here knows a
//! kernel's internals: the image's header says where and how it is loaded, and the entry state is the protocol's
//! (64-bit mode with paging, identity maps of the kernel's `init_size` window, the zero page and the command line, a
//! GDT with flat `__BOOT_CS` 0x10 / `__BOOT_DS` 0x18, CS = 0x10, DS = ES = SS = 0x18, IF = 0, RSI = &boot_params,
//! RIP = kernel + 0x200).
//!
//! Physical map: INV-03-03 claim 69 (the selected cold-boot manifest), parameterised by the end of low RAM `L` and the
//! size of high RAM `H` above 4 GiB ([`Layout`]); M1 grows guest RAM, L2 places the ACPI tables in the NVS range and
//! passes the tables built by [`acpi`] ([`Inputs::acpi`]), X4 uses the NVS boot-service scratch. The independent judge of the result is
//! `tools/models/bootproto.py` (`tools/run_l1.py`).
use natemu_abi::execution::{self as exec, Cpu, Segment, Table};
use natemu_abi::guest_memory::GuestRam;
use std::fmt;

pub mod acpi;

const MIB: u64 = 1 << 20;
const GIB: u64 = 1 << 30;
const PAGE: u64 = 4096;

/// Setup-header offsets (boot.rst "The Real-Mode Kernel Header"; zero-page.rst for the 0x0xx fields).
mod at {
    pub const SETUP_SECTS: usize = 0x1f1;
    pub const SYSSIZE: usize = 0x1f4;
    pub const BOOT_FLAG: usize = 0x1fe;
    pub const JUMP: usize = 0x200;
    pub const HEADER: usize = 0x202;
    pub const VERSION: usize = 0x206;
    pub const TYPE_OF_LOADER: usize = 0x210;
    pub const LOADFLAGS: usize = 0x211;
    pub const CODE32_START: usize = 0x214;
    pub const RAMDISK_IMAGE: usize = 0x218;
    pub const RAMDISK_SIZE: usize = 0x21c;
    pub const HEAP_END_PTR: usize = 0x224;
    pub const CMD_LINE_PTR: usize = 0x228;
    pub const HARDWARE_SUBARCH: usize = 0x23c;
    pub const HARDWARE_SUBARCH_DATA: usize = 0x240;
    pub const SETUP_DATA: usize = 0x250;
    pub const INITRD_ADDR_MAX: usize = 0x22c;
    pub const KERNEL_ALIGNMENT: usize = 0x230;
    pub const RELOCATABLE_KERNEL: usize = 0x234;
    pub const XLOADFLAGS: usize = 0x236;
    pub const CMDLINE_SIZE: usize = 0x238;
    pub const PAYLOAD_OFFSET: usize = 0x248;
    pub const PAYLOAD_LENGTH: usize = 0x24c;
    pub const PREF_ADDRESS: usize = 0x258;
    pub const INIT_SIZE: usize = 0x260;
    /// The header through `handover_offset` (protocol 2.11), which every supported image has.
    pub const MIN_END: usize = 0x268;
    /// End of the setup-header hole in `struct boot_params` (bootparam.h: `hdr` then `_pad7[0x290-0x1f1-sizeof(hdr)]`).
    pub const MAX_END: usize = 0x290;
    pub const ACPI_RSDP_ADDR: usize = 0x070;
    pub const EXT_RAMDISK_IMAGE: usize = 0x0c0;
    pub const EXT_RAMDISK_SIZE: usize = 0x0c4;
    pub const EXT_CMD_LINE_PTR: usize = 0x0c8;
    pub const E820_ENTRIES: usize = 0x1e8;
    pub const E820_TABLE: usize = 0x2d0;
}
/// The oldest protocol supported: 2.12 (boot.rst "Protocol 2.12": `xloadflags`, whose XLF_KERNEL_64 announces the
/// 64-bit entry; `pref_address` and `init_size` are 2.10).
pub(crate) const MIN_VERSION: u16 = 0x020c;
const LOADED_HIGH: u8 = 1;
const XLF_KERNEL_64: u16 = 1;
/// boot.rst type_of_loader: "Otherwise, enter 0xFF here" (no assigned id).
const TYPE_OF_LOADER: u8 = 0xff;
/// bootparam.h `E820_MAX_ENTRIES_ZEROPAGE`: the map has at most 10 entries (checked in `layout_e820`).
#[cfg(test)]
const E820_MAX: usize = 128;

/// Why an image or a load was refused. The header codes and their order are those of `tools/models/bootproto.py`'s
/// `parse_bzimage` (checked by `tools/run_l1.py rejections`); the others are the loader's own.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LoadError {
    TruncatedHeader(usize),
    BadBootFlag(u16),
    BadMagic,
    VersionTooOld(u16),
    BadJump(u8),
    BadHeaderEnd(usize),
    NotLoadedHigh(u8),
    Not64Bit(u16),
    BadAlignment(u32),
    BadInitSize,
    BadPrefAddress,
    TruncatedImage { have: usize, need: u64 },
    BadPayload { offset: u32, length: u32 },
    /// The physical map's parameters (`Layout`) are outside what the map allows.
    BadLayout(String),
    /// A RAM or NVS extent of the map is not backed by the guest RAM given.
    RamNotBacked { gpa: u64, bytes: u64 },
    CmdlineTooLong { length: usize, limit: usize },
    CmdlineNul,
    /// The kernel's reserved window `[runtime_start, runtime_start + max(init_size, loaded size))` lies in no RAM extent.
    KernelDoesNotFit { start: u64, bytes: u64 },
    /// No page-aligned place for the initrd in low RAM below `initrd_addr_max` above the kernel's window.
    InitrdDoesNotFit { bytes: u64 },
    /// An ACPI blob (the RSDP, a table, the AP mailbox) at this address does not lie in the map's ACPI NVS range.
    BadTables(u64),
}
impl LoadError {
    pub(crate) fn code(&self) -> &'static str {
        use LoadError::*;
        match self {
            TruncatedHeader(_) => "TRUNCATED_HEADER",
            BadBootFlag(_) => "BAD_BOOT_FLAG",
            BadMagic => "BAD_MAGIC",
            VersionTooOld(_) => "VERSION_TOO_OLD",
            BadJump(_) => "BAD_JUMP",
            BadHeaderEnd(_) => "BAD_HEADER_END",
            NotLoadedHigh(_) => "NOT_LOADED_HIGH",
            Not64Bit(_) => "NOT_64BIT",
            BadAlignment(_) => "BAD_ALIGNMENT",
            BadInitSize => "BAD_INIT_SIZE",
            BadPrefAddress => "BAD_PREF_ADDRESS",
            TruncatedImage { .. } => "TRUNCATED_IMAGE",
            BadPayload { .. } => "BAD_PAYLOAD",
            BadLayout(_) => "BAD_LAYOUT",
            RamNotBacked { .. } => "RAM_NOT_BACKED",
            CmdlineTooLong { .. } => "CMDLINE_TOO_LONG",
            CmdlineNul => "CMDLINE_NUL",
            KernelDoesNotFit { .. } => "KERNEL_DOES_NOT_FIT",
            InitrdDoesNotFit { .. } => "INITRD_DOES_NOT_FIT",
            BadTables(_) => "BAD_TABLES",
        }
    }
}
impl fmt::Display for LoadError {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "load refused: {}: {self:x?}", self.code())
    }
}

/// The validated setup header of a bzImage.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Setup {
    pub version: u16,
    /// End of the setup header (0x202 + the byte at 0x201).
    pub(crate) header_end: usize,
    pub(crate) relocatable: bool,
    pub(crate) kernel_alignment: u64,
    pub(crate) pref_address: u64,
    pub(crate) init_size: u64,
    pub(crate) cmdline_size: u64,
    pub(crate) initrd_addr_max: u64,
    /// File offset and size of the protected-mode kernel (`(setup_sects + 1) * 512`, `syssize * 16`).
    pub(crate) pm_offset: usize,
    pub(crate) pm_size: usize,
}

fn u16_at(d: &[u8], o: usize) -> u16 {
    u16::from_le_bytes(d[o..o + 2].try_into().unwrap())
}
fn u32_at(d: &[u8], o: usize) -> u32 {
    u32::from_le_bytes(d[o..o + 4].try_into().unwrap())
}
fn u64_at(d: &[u8], o: usize) -> u64 {
    u64::from_le_bytes(d[o..o + 8].try_into().unwrap())
}

/// Validates the setup header for the 64-bit protocol (each check cites boot.rst's field description).
pub(crate) fn parse(d: &[u8]) -> Result<Setup, LoadError> {
    use LoadError::*;
    if d.len() < at::MIN_END {
        return Err(TruncatedHeader(d.len()));
    }
    // boot_flag: "Contains 0xAA55."
    if u16_at(d, at::BOOT_FLAG) != 0xaa55 {
        return Err(BadBootFlag(u16_at(d, at::BOOT_FLAG)));
    }
    // header: "Contains the magic number HdrS (0x53726448)."
    if &d[at::HEADER..at::HEADER + 4] != b"HdrS" {
        return Err(BadMagic);
    }
    let version = u16_at(d, at::VERSION);
    if version < MIN_VERSION {
        return Err(VersionTooOld(version));
    }
    // jump: "0xEB followed by a signed offset relative to byte 0x202" (the header end, below).
    if d[at::JUMP] != 0xeb {
        return Err(BadJump(d[at::JUMP]));
    }
    let header_end = 0x202 + d[at::JUMP + 1] as usize;
    if !(at::MIN_END..=at::MAX_END).contains(&header_end) {
        return Err(BadHeaderEnd(header_end));
    }
    // loadflags bit 0 LOADED_HIGH: the protected-mode code is loaded at 0x100000 (a bzImage); xloadflags bit 0
    // XLF_KERNEL_64: "this kernel has the legacy 64-bit entry point at 0x200".
    let (loadflags, xloadflags) = (d[at::LOADFLAGS], u16_at(d, at::XLOADFLAGS));
    if loadflags & LOADED_HIGH == 0 {
        return Err(NotLoadedHigh(loadflags));
    }
    if xloadflags & XLF_KERNEL_64 == 0 {
        return Err(Not64Bit(xloadflags));
    }
    // kernel_alignment: "Alignment unit required by the kernel (if relocatable_kernel is true.)"
    let (relocatable, align) = (d[at::RELOCATABLE_KERNEL] != 0, u32_at(d, at::KERNEL_ALIGNMENT));
    if relocatable && !align.is_power_of_two() {
        return Err(BadAlignment(align));
    }
    // init_size: the linear memory the kernel needs from its runtime start; pref_address: where a non-relocatable
    // kernel runs ("runtime_start = pref_address").
    let (pref_address, init_size) = (u64_at(d, at::PREF_ADDRESS), u64::from(u32_at(d, at::INIT_SIZE)));
    if init_size == 0 {
        return Err(BadInitSize);
    }
    if !relocatable && pref_address == 0 {
        return Err(BadPrefAddress);
    }
    // setup_sects: "If this field is 0, the real value is 4"; the protected-mode kernel starts at (setup_sects+1)*512
    // and is syssize 16-byte paragraphs long.
    let sects = match d[at::SETUP_SECTS] {
        0 => 4,
        n => n as usize,
    };
    let (pm_offset, pm_size) = ((sects + 1) * 512, u64::from(u32_at(d, at::SYSSIZE)) * 16);
    if (d.len() as u64) < pm_offset as u64 + pm_size {
        return Err(TruncatedImage { have: d.len(), need: pm_offset as u64 + pm_size });
    }
    let (offset, length) = (u32_at(d, at::PAYLOAD_OFFSET), u32_at(d, at::PAYLOAD_LENGTH));
    if offset != 0 && u64::from(offset) + u64::from(length) > pm_size {
        return Err(BadPayload { offset, length });
    }
    Ok(Setup {
        version,
        header_end,
        relocatable,
        kernel_alignment: u64::from(align),
        pref_address,
        init_size,
        cmdline_size: u64::from(u32_at(d, at::CMDLINE_SIZE)),
        initrd_addr_max: u64::from(u32_at(d, at::INITRD_ADDR_MAX)),
        pm_offset,
        pm_size: pm_size as usize,
    })
}

/// E820 types (`arch/x86/include/asm/e820/types.h`).
pub(crate) const E820_RAM: u32 = 1;
pub(crate) const E820_RESERVED: u32 = 2;
pub(crate) const E820_NVS: u32 = 4;
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct E820 {
    pub addr: u64,
    pub size: u64,
    pub kind: u32,
}

/// The selected physical map (INV-03-03 claim 69), half-open ranges. Low RAM ends at `low_end` (L: 2 GiB by default,
/// 1-3 GiB for a boot; any 2 MiB multiple above [`RAM_START`] up to 3 GiB is accepted, so tests choose their size);
/// `high` bytes of RAM above 4 GiB (H, 0 initially).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Layout {
    pub low_end: u64,
    pub high: u64,
}
/// Start of ordinary low RAM: the kernel's window lies above it (M69 "K >= 16 MiB").
pub(crate) const RAM_START: u64 = 16 * MIB;
/// Loader data in conventional RAM (M69: temporary loader uses): boot_params, the command buffer, the bootstrap stack
/// (RSP at its top) and the GDT.
pub(crate) const ZERO_PAGE: u64 = 0x1_0000;
pub(crate) const CMDLINE: u64 = 0x1_1000;
pub(crate) const CMDLINE_BYTES: u64 = 0x4000;
pub(crate) const STACK_TOP: u64 = 0x1_8000;
pub(crate) const GDT: u64 = 0x1_8000;
/// Reserved loader page tables (M69 `0x100000..0x200000`): PML4, PDPT, then one PD per GiB.
pub(crate) const PAGE_TABLES: u64 = 0x10_0000;
/// ACPI NVS for the RSDP, the table graph, FACS and the AP mailbox (L2), and the boot-service scratch (X4).
pub const ACPI_NVS: (u64, u64) = (0x60_0000, 0x80_0000);
pub(crate) const SCRATCH_NVS: (u64, u64) = (0x80_0000, 0x100_0000);
/// X4: the APs' GDT in the NVS boot scratch.
pub(crate) const AP_GDT: u64 = SCRATCH_NVS.0;
impl Layout {
    /// The default boot map: L = 2 GiB, H = 0 (M69).
    pub const DEFAULT: Layout = Layout { low_end: 2 * GIB, high: 0 };
    /// The E820 table (sorted, disjoint): RAM and NVS where they exist, reserved for the holes.
    pub fn e820(&self) -> Result<Vec<E820>, LoadError> {
        let l = self.low_end;
        if l <= RAM_START || l > 3 * GIB || l % (2 * MIB) != 0 {
            return Err(LoadError::BadLayout(format!(
                "low RAM end {l:#x}: a 2 MiB multiple above the map's RAM start {RAM_START:#x}, at most 3 GiB"
            )));
        }
        if self.high % (2 * MIB) != 0 || self.high > (1 << 40) - 4 * GIB {
            return Err(LoadError::BadLayout(format!("high RAM {:#x}: a 2 MiB multiple within 40-bit physical addresses", self.high)));
        }
        let e = |addr: u64, end: u64, kind| E820 { addr, size: end - addr, kind };
        let mut map = vec![
            e(0, 0x1000, E820_RESERVED),          // reserved low page
            e(0x1000, 0xa_0000, E820_RAM),        // conventional RAM (loader data, Linux's own trampoline later)
            e(0xa_0000, 0x10_0000, E820_RESERVED), // legacy hole
            e(PAGE_TABLES, 0x20_0000, E820_RESERVED), // loader page tables
            e(0x20_0000, ACPI_NVS.0, E820_RESERVED), // normal-map hole (SMRAM banks)
            e(ACPI_NVS.0, ACPI_NVS.1, E820_NVS),
            e(SCRATCH_NVS.0, SCRATCH_NVS.1, E820_NVS),
            e(RAM_START, l, E820_RAM),
            e(l, 4 * GIB, E820_RESERVED), // non-RAM aperture (IOAPIC, LAPIC, virtio windows)
        ];
        if self.high > 0 {
            map.push(e(4 * GIB, 4 * GIB + self.high, E820_RAM));
        }
        Ok(map)
    }
}

/// What the loader is given besides the map.
pub struct Inputs<'a> {
    pub image: &'a [u8],
    pub cmdline: &'a str,
    pub initrd: Option<&'a [u8]>,
    /// The ACPI tables ([`acpi::build`]): every blob is placed (so [`Loaded::writes`] carries them) and must lie in the
    /// ACPI NVS range; the RSDP's address goes to `boot_params.acpi_rsdp_addr`. `None` leaves it 0. Kernels of protocol
    /// 2.12/2.13 predate the field (boot.rst "Protocol 2.14 ... Add ACPI RSDP address"; zero-page.rst marks it ALL) and
    /// ignore it, finding the RSDP by their own search, which these tables do not serve (none lies below 1 MiB).
    pub acpi: Option<&'a acpi::Tables>,
}
/// Where everything went, and the BSP's entry state.
#[derive(Debug, Clone)]
pub struct Loaded {
    pub setup: Setup,
    pub kernel: u64,
    /// The kernel's reserved window `[kernel, kernel + window)`.
    pub window: u64,
    pub zero_page: u64,
    pub cmdline: u64,
    pub initrd: Option<(u64, u64)>,
    pub map: Vec<E820>,
    pub cpu: Cpu,
    /// X4: an AP's state when the ACPI mailbox wakes it (the firmware's wake state, RIP = the wakeup vector then).
    pub ap: Cpu,
    /// Every guest-physical range the loader wrote, in order (gpa, bytes).
    pub writes: Vec<(u64, u64)>,
}

/// `__BOOT_CS` (64-bit code, execute/read, accessed) and `__BOOT_DS` (data, read/write, accessed), 4 GiB flat (G = 1,
/// limit FFFFFH, base 0); entries 0 and 1 are null.
const BOOT_CS: u16 = 0x10;
const BOOT_DS: u16 = 0x18;
const GDT_ENTRIES: [u64; 4] = [0, 0, 0x00af_9b00_0000_ffff, 0x00cf_9300_0000_ffff];
const CR0_NE: u64 = 1 << 5;
const PRESENT_WRITABLE: u64 = 3;
const LARGE: u64 = 1 << 7;

fn place(ram: &GuestRam, writes: &mut Vec<(u64, u64)>, gpa: u64, bytes: &[u8]) -> Result<(), LoadError> {
    writes.push((gpa, bytes.len() as u64));
    ram.copy_in(gpa, bytes).then_some(()).ok_or(LoadError::RamNotBacked { gpa, bytes: bytes.len() as u64 })
}

/// X4: the firmware's mailbox wake state on top of `base` (which supplies paging, CR3, the GDT and flat selectors): CPL 0,
/// IF = 0 (RFLAGS 2), EFER.LME/LMA and the firmware's NXE (see `load`), no IDT, general registers 0, RIP 0 (the
/// resident sets the vector). The loader's `Loaded::ap` and the test driver's `mailbox` sections both use it.
pub fn ap_wake(base: Cpu) -> Cpu {
    Cpu { gpr: [0; 16], rip: 0, rflags: 2, cpl: 0, efer: exec::EFER_LME | exec::EFER_LMA | exec::EFER_NXE, idtr: Table { base: 0, limit: 0 }, ..base }
}

/// M1: `ram` is one range from 0; high RAM (`Layout::high`) needs a multi-range RAM description here (`GuestRam::and`)
/// before it is enabled.
/// Loads `inputs` into `ram` per the 64-bit protocol and the `layout` map; returns the placement and the BSP's state.
/// `ram` must be zero where the loader leaves zeros (fresh guest RAM).
pub fn load(ram: &GuestRam, layout: &Layout, inputs: &Inputs) -> Result<Loaded, LoadError> {
    let s = parse(inputs.image)?;
    let map = layout.e820()?;
    for e in map.iter().filter(|e| e.kind != E820_RESERVED) {
        ram.range(e.addr, e.size).ok_or(LoadError::RamNotBacked { gpa: e.addr, bytes: e.size })?;
    }
    // Command line: "at most cmdline_size characters" plus the NUL, inside the command buffer.
    let limit = (s.cmdline_size as usize).min(CMDLINE_BYTES as usize - 1);
    if inputs.cmdline.len() > limit {
        return Err(LoadError::CmdlineTooLong { length: inputs.cmdline.len(), limit });
    }
    if inputs.cmdline.contains('\0') {
        return Err(LoadError::CmdlineNul);
    }
    // Kernel: boot.rst init_size: relocatable -> runtime_start = align_up(max(load_address, pref_address),
    // kernel_alignment) with the load address the map's RAM start; else runtime_start = pref_address. The window holds
    // init_size and the loaded protected-mode code.
    // The window must lie in ordinary low RAM [RAM_START, L): never over the loader's own data in conventional RAM, the
    // page tables or the NVS ranges, and inside the identity map (claim 69: no overlap with a live boot allocation).
    let kernel = if s.relocatable { RAM_START.max(s.pref_address).checked_next_multiple_of(s.kernel_alignment) } else { Some(s.pref_address) };
    let window = s.init_size.max(s.pm_size as u64);
    let does_not_fit = LoadError::KernelDoesNotFit { start: kernel.unwrap_or(u64::MAX), bytes: window };
    let kernel = kernel.filter(|&k| k >= RAM_START).ok_or(does_not_fit.clone())?;
    let window_end = kernel.checked_add(window).filter(|&end| end <= layout.low_end);
    let window_end = window_end.ok_or(does_not_fit)?;
    // Initrd: page-aligned, as high as low RAM and initrd_addr_max ("the address of the highest safe byte") allow,
    // above the kernel's window.
    let initrd = match inputs.initrd.filter(|d| !d.is_empty()) {
        None => None,
        Some(d) => {
            let bytes = d.len() as u64;
            let top = layout.low_end.min(s.initrd_addr_max.saturating_add(1));
            let at = top.checked_sub(bytes).map(|a| a & !(PAGE - 1)).filter(|&a| a >= window_end);
            Some((at.ok_or(LoadError::InitrdDoesNotFit { bytes })?, bytes))
        }
    };
    // The RSDP, the tables it reaches and the AP mailbox lie in the map's ACPI NVS range (claim 69).
    let blobs = inputs.acpi.map_or(&[][..], |t| &t.blobs[..]);
    if let Some((gpa, _)) = blobs.iter().find(|(a, b)| !(ACPI_NVS.0 <= *a && a.checked_add(b.len() as u64).is_some_and(|end| end <= ACPI_NVS.1))) {
        return Err(LoadError::BadTables(*gpa));
    }

    // boot_params: zeroed, then the setup header from 0x1f1 to its end, then the loader's fields.
    let mut z = vec![0u8; PAGE as usize];
    z[at::SETUP_SECTS..s.header_end].copy_from_slice(&inputs.image[at::SETUP_SECTS..s.header_end]);
    let put32 = |z: &mut Vec<u8>, o: usize, v: u64| z[o..o + 4].copy_from_slice(&(v as u32).to_le_bytes());
    // Every setup-header field boot.rst types "write" is the loader's: none of the image's values passes through.
    // type_of_loader 0xFF with ext_loader_type/ext_loader_ver 0 (no assigned id); ramdisk_image/ramdisk_size and their
    // high halves (0: "Leave at zero if there is no initial ramdisk"); cmd_line_ptr and ext_cmd_line_ptr;
    // hardware_subarch 0 (the default x86/PC) and hardware_subarch_data 0; setup_data 0 (no list); heap_end_ptr 0 and
    // loadflags' loader bits QUIET_FLAG, KEEP_SEGMENTS (obsolete) and CAN_USE_HEAP clear (the heap is the real-mode
    // setup code's, which the 64-bit entry never runs). code32_start ("modify") is set to the loaded code.
    z[at::TYPE_OF_LOADER] = TYPE_OF_LOADER;
    z[at::LOADFLAGS] &= !(1 << 5 | 1 << 6 | 1 << 7);
    put32(&mut z, at::HEAP_END_PTR, 0); // with ext_loader_ver and ext_loader_type (0x224..0x228)
    put32(&mut z, at::CODE32_START, kernel); // "the boot loader must set the code32_start field to point to the loaded code"
    put32(&mut z, at::CMD_LINE_PTR, CMDLINE);
    put32(&mut z, at::EXT_CMD_LINE_PTR, CMDLINE >> 32);
    let (a, n) = initrd.unwrap_or((0, 0));
    put32(&mut z, at::RAMDISK_IMAGE, a);
    put32(&mut z, at::RAMDISK_SIZE, n);
    put32(&mut z, at::EXT_RAMDISK_IMAGE, a >> 32);
    put32(&mut z, at::EXT_RAMDISK_SIZE, n >> 32);
    put32(&mut z, at::HARDWARE_SUBARCH, 0);
    z[at::HARDWARE_SUBARCH_DATA..at::HARDWARE_SUBARCH_DATA + 8].fill(0);
    z[at::SETUP_DATA..at::SETUP_DATA + 8].fill(0);
    z[at::ACPI_RSDP_ADDR..at::ACPI_RSDP_ADDR + 8].copy_from_slice(&inputs.acpi.map_or(0, |t| t.rsdp).to_le_bytes());
    z[at::E820_ENTRIES] = map.len() as u8;
    for (i, e) in map.iter().enumerate() {
        let o = at::E820_TABLE + 20 * i;
        z[o..o + 8].copy_from_slice(&e.addr.to_le_bytes());
        z[o + 8..o + 16].copy_from_slice(&e.size.to_le_bytes());
        z[o + 16..o + 20].copy_from_slice(&e.kind.to_le_bytes());
    }
    let mut w = Vec::new();
    place(ram, &mut w, ZERO_PAGE, &z)?;
    let mut cmd = inputs.cmdline.as_bytes().to_vec();
    cmd.push(0);
    place(ram, &mut w, CMDLINE, &cmd)?;
    place(ram, &mut w, GDT, &GDT_ENTRIES.iter().flat_map(|e| e.to_le_bytes()).collect::<Vec<u8>>())?;
    // X4: the APs' GDT, a copy in the NVS boot scratch (the loader's lies in conventional RAM, which Linux reuses).
    place(ram, &mut w, AP_GDT, &GDT_ENTRIES.iter().flat_map(|e| e.to_le_bytes()).collect::<Vec<u8>>())?;
    // Identity map of [0, L) with 2 MiB pages (it covers the window, boot_params, the command line, the GDT, the initrd
    // and the NVS ranges): PML4 -> PDPT -> one PD per GiB, supervisor read/write/execute, write-back.
    let (pml4, pdpt) = (PAGE_TABLES, PAGE_TABLES + PAGE);
    place(ram, &mut w, pml4, &(pdpt | PRESENT_WRITABLE).to_le_bytes())?;
    for g in 0..layout.low_end.div_ceil(GIB) {
        let pd = PAGE_TABLES + (2 + g) * PAGE;
        place(ram, &mut w, pdpt + 8 * g, &(pd | PRESENT_WRITABLE).to_le_bytes())?;
        let pdes = (0..512).map(|i| g * GIB + i * 2 * MIB).take_while(|&a| a < layout.low_end);
        place(ram, &mut w, pd, &pdes.flat_map(|a| (a | PRESENT_WRITABLE | LARGE).to_le_bytes()).collect::<Vec<u8>>())?;
    }
    place(ram, &mut w, kernel, &inputs.image[s.pm_offset..s.pm_offset + s.pm_size])?;
    if let (Some((a, _)), Some(d)) = (initrd, inputs.initrd) {
        place(ram, &mut w, a, d)?;
    }
    for (gpa, bytes) in blobs {
        place(ram, &mut w, *gpa, bytes)?;
    }

    // The BSP: 64-bit mode, paging (4-level, LA57 = 0), PAE, protected mode, ET (fixed) and NE; long mode; IF = 0.
    let mut gpr = [0; 16];
    (gpr[4], gpr[6]) = (STACK_TOP, ZERO_PAGE); // RSP: the bootstrap stack's top; RSI: &boot_params
    let code = Segment::loaded(BOOT_CS, GDT_ENTRIES[2]);
    let data = Segment::loaded(BOOT_DS, GDT_ENTRIES[3]);
    let cpu = Cpu {
        gpr,
        rip: kernel + 0x200,
        rflags: 2,
        cr0: exec::CR0_PE | exec::CR0_ET | CR0_NE | exec::CR0_PG,
        cr3: pml4,
        cr4: exec::CR4_PAE,
        efer: exec::EFER_LME | exec::EFER_LMA,
        cpl: 0,
        fs_base: 0,
        gs_base: 0,
        cr2: 0,
        cr8: 0,
        dr: exec::DR_RESET,
        seg: [data, code, data, data, Segment::NULL, Segment::NULL],
        ldtr: exec::LDTR_RESET,
        tr: exec::TR_RESET,
        gdtr: Table { base: GDT, limit: (8 * GDT_ENTRIES.len() - 1) as u64 },
        // No IDT (limit 0): an exception before the kernel loads its own escalates to a shutdown.
        idtr: Table { base: 0, limit: 0 },
        mov_ss: 0,
        xmm: [[0; 2]; 16],
        mxcsr: exec::MXCSR_RESET,
        fcw: exec::FCW_INIT,
        xcr0: natemu_abi::cpu_profile::XCR0_RESET,
    };
    // X4: an AP woken by the mailbox (ACPI 6.5 Table 5.44, Intel processors): long mode, paging with the vector's page
    // identity mapped (the loader's map of [0, L), reserved memory: it covers both places a vector may lie, the mailbox's
    // OS area in the ACPI NVS and memory below 1 MiB), IF = 0, every selector flat (CS 64-bit code, the others flat data,
    // from the GDT in the NVS scratch), no IDT (limit 0); the general registers 0. Where the table leaves the state open,
    // the firmware's choice: EFER.NXE set (the profile has NX; UEFI firmware enables it, and under TDX, the mailbox's
    // origin, EFER.NXE is fixed at 1), which an OS may rely on before it writes EFER: Linux's 64-bit trampoline loads page
    // tables whose data pages are XD (reserved with NXE = 0) and pushes onto them.
    let ap = ap_wake(Cpu { seg: [data, code, data, data, data, data], gdtr: Table { base: AP_GDT, limit: (8 * GDT_ENTRIES.len() - 1) as u64 }, ..cpu });
    Ok(Loaded { setup: s, kernel, window, zero_page: ZERO_PAGE, cmdline: CMDLINE, initrd, map, cpu, ap, writes: w })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A minimal valid bzImage: protocol 2.15, relocatable, 64-bit, 2 MiB alignment, pref_address 16 MiB, one setup
    /// sector, `pm` bytes of protected-mode code (a `hlt` loop at +0x200), init_size 4 MiB.
    pub(crate) fn image(pm: usize) -> Vec<u8> {
        let mut d = vec![0u8; 1024 + pm];
        d[at::SETUP_SECTS] = 1;
        d[at::SYSSIZE..at::SYSSIZE + 4].copy_from_slice(&((pm / 16) as u32).to_le_bytes());
        d[at::BOOT_FLAG..at::BOOT_FLAG + 2].copy_from_slice(&0xaa55u16.to_le_bytes());
        d[at::JUMP] = 0xeb;
        d[at::JUMP + 1] = 0x6a; // header end 0x26c
        d[at::HEADER..at::HEADER + 4].copy_from_slice(b"HdrS");
        d[at::VERSION..at::VERSION + 2].copy_from_slice(&0x020fu16.to_le_bytes());
        d[at::LOADFLAGS] = LOADED_HIGH;
        d[at::INITRD_ADDR_MAX..at::INITRD_ADDR_MAX + 4].copy_from_slice(&0x7fff_ffffu32.to_le_bytes());
        d[at::KERNEL_ALIGNMENT..at::KERNEL_ALIGNMENT + 4].copy_from_slice(&0x20_0000u32.to_le_bytes());
        d[at::RELOCATABLE_KERNEL] = 1;
        d[at::RELOCATABLE_KERNEL + 1] = 21;
        d[at::XLOADFLAGS..at::XLOADFLAGS + 2].copy_from_slice(&3u16.to_le_bytes());
        d[at::CMDLINE_SIZE..at::CMDLINE_SIZE + 4].copy_from_slice(&2047u32.to_le_bytes());
        d[at::PREF_ADDRESS..at::PREF_ADDRESS + 8].copy_from_slice(&0x100_0000u64.to_le_bytes());
        d[at::INIT_SIZE..at::INIT_SIZE + 4].copy_from_slice(&0x40_0000u32.to_le_bytes());
        d[1024 + 0x200..1024 + 0x203].copy_from_slice(&[0xf4, 0xeb, 0xfd]);
        d
    }
    fn set(mut d: Vec<u8>, at: usize, bytes: &[u8]) -> Vec<u8> {
        d[at..at + bytes.len()].copy_from_slice(bytes);
        d
    }
    const SMALL: Layout = Layout { low_end: 64 * MIB, high: 0 };
    fn ram(bytes: u64) -> (Vec<u64>, GuestRam) {
        let mut v = vec![0u64; bytes as usize / 8];
        let r = unsafe { GuestRam::new(0, bytes, v.as_mut_ptr() as u64) };
        (v, r)
    }
    fn inputs<'a>(image: &'a [u8], cmdline: &'a str, initrd: Option<&'a [u8]>) -> Inputs<'a> {
        Inputs { image, cmdline, initrd, acpi: None }
    }

    /// One malformed image per header rejection, in `parse`'s order.
    #[test]
    fn header_rejections() {
        use LoadError::*;
        let ok = image(0x1000);
        let cases: Vec<(Vec<u8>, LoadError)> = vec![
            (ok[..0x267].to_vec(), TruncatedHeader(0x267)),
            (set(ok.clone(), at::BOOT_FLAG, &[0x55, 0xab]), BadBootFlag(0xab55)),
            (set(ok.clone(), at::HEADER, b"HdrT"), BadMagic),
            (set(ok.clone(), at::VERSION, &[0x0b, 0x02]), VersionTooOld(0x020b)),
            (set(ok.clone(), at::JUMP, &[0xe9]), BadJump(0xe9)),
            (set(ok.clone(), at::JUMP + 1, &[0x65]), BadHeaderEnd(0x267)),
            (set(ok.clone(), at::JUMP + 1, &[0x8f]), BadHeaderEnd(0x291)),
            (set(ok.clone(), at::LOADFLAGS, &[0x20]), NotLoadedHigh(0x20)),
            (set(ok.clone(), at::XLOADFLAGS, &[2, 0]), Not64Bit(2)),
            (set(ok.clone(), at::KERNEL_ALIGNMENT, &0x30_0000u32.to_le_bytes()), BadAlignment(0x30_0000)),
            (set(ok.clone(), at::KERNEL_ALIGNMENT, &[0; 4]), BadAlignment(0)),
            (set(ok.clone(), at::INIT_SIZE, &[0; 4]), BadInitSize),
            (set(set(ok.clone(), at::RELOCATABLE_KERNEL, &[0]), at::PREF_ADDRESS, &[0; 8]), BadPrefAddress),
            (ok[..ok.len() - 1].to_vec(), TruncatedImage { have: ok.len() - 1, need: ok.len() as u64 }),
            (set(ok.clone(), at::SYSSIZE, &u32::MAX.to_le_bytes()), TruncatedImage { have: ok.len(), need: 1024 + 16 * u32::MAX as u64 }),
            (set(set(ok.clone(), at::PAYLOAD_OFFSET, &0xf00u32.to_le_bytes()), at::PAYLOAD_LENGTH, &0x101u32.to_le_bytes()), BadPayload { offset: 0xf00, length: 0x101 }),
        ];
        for (d, want) in cases {
            assert_eq!(parse(&d), Err(want.clone()), "{}", want.code());
        }
        // A non-relocatable kernel's alignment is not checked; setup_sects 0 means 4.
        assert!(parse(&set(set(ok.clone(), at::RELOCATABLE_KERNEL, &[0]), at::KERNEL_ALIGNMENT, &[0; 4])).is_ok());
        let four = set(ok.clone(), at::SETUP_SECTS, &[0]);
        assert_eq!(parse(&four), Err(TruncatedImage { have: ok.len(), need: 5 * 512 + 0x1000 }));
    }

    /// Each loader-side rejection.
    #[test]
    fn load_rejections() {
        use LoadError::*;
        let ok = image(0x1000);
        let (_v, r) = ram(SMALL.low_end);
        let bad = |layout: Layout, i: Inputs| load(&r, &layout, &i).unwrap_err();
        assert_eq!(bad(Layout { low_end: RAM_START, high: 0 }, inputs(&ok, "", None)).code(), "BAD_LAYOUT");
        assert_eq!(bad(Layout { low_end: 3 * GIB + 2 * MIB, high: 0 }, inputs(&ok, "", None)).code(), "BAD_LAYOUT");
        assert_eq!(bad(Layout { low_end: 33 * MIB, high: 0 }, inputs(&ok, "", None)).code(), "BAD_LAYOUT");
        assert_eq!(bad(Layout { low_end: 64 * MIB, high: MIB }, inputs(&ok, "", None)).code(), "BAD_LAYOUT");
        assert_eq!(bad(Layout { low_end: 128 * MIB, high: 0 }, inputs(&ok, "", None)), RamNotBacked { gpa: RAM_START, bytes: 112 * MIB });
        let long = "x".repeat(2048);
        assert_eq!(bad(SMALL, inputs(&ok, &long, None)), CmdlineTooLong { length: 2048, limit: 2047 });
        assert!(load(&r, &SMALL, &inputs(&ok, &long[..2047], None)).is_ok());
        assert_eq!(bad(SMALL, inputs(&ok, "a\0b", None)), CmdlineNul);
        // init_size 60 MiB from 16 MiB exceeds L = 64 MiB.
        let huge = set(ok.clone(), at::INIT_SIZE, &(60 * MIB as u32).to_le_bytes());
        assert_eq!(bad(SMALL, inputs(&huge, "", None)), KernelDoesNotFit { start: RAM_START, bytes: 60 * MIB });
        // A non-relocatable kernel runs at pref_address, here in the reserved low hole.
        let fixed = set(set(ok.clone(), at::RELOCATABLE_KERNEL, &[0]), at::PREF_ADDRESS, &0x10_0000u64.to_le_bytes());
        assert_eq!(bad(SMALL, inputs(&fixed, "", None)), KernelDoesNotFit { start: 0x10_0000, bytes: 4 * MIB });
        let rd = vec![1u8; 45 * MIB as usize];
        assert_eq!(bad(SMALL, inputs(&ok, "", Some(&rd))), InitrdDoesNotFit { bytes: 45 * MIB });
        // initrd_addr_max 24 MiB - 1: below the kernel's window end (20 MiB) there is no room for 5 MiB.
        let low = set(ok.clone(), at::INITRD_ADDR_MAX, &(24 * MIB as u32 - 1).to_le_bytes());
        assert_eq!(bad(SMALL, inputs(&low, "", Some(&rd[..5 * MIB as usize]))), InitrdDoesNotFit { bytes: 5 * MIB });
        let t = acpi::build(1, 0x1000);
        let i = Inputs { acpi: Some(&t), ..inputs(&ok, "", None) };
        assert_eq!(bad(SMALL, i), BadTables(0x1000));
        let t = acpi::build(1, ACPI_NVS.1 - 35);
        let i = Inputs { acpi: Some(&t), ..inputs(&ok, "", None) };
        assert_eq!(bad(SMALL, i), BadTables(ACPI_NVS.1 - 35)); // crosses into the boot-service scratch
        let t = acpi::build(1, u64::MAX - 8);
        let i = Inputs { acpi: Some(&t), ..inputs(&ok, "", None) };
        assert_eq!(bad(SMALL, i), BadTables(u64::MAX - 8));
        // A non-relocatable kernel over the loader's own data (zero page, GDT) or in high RAM; an alignment overflow.
        for pref in [0x1_0000u64, GDT, 4 * GIB] {
            let fixed = set(set(ok.clone(), at::RELOCATABLE_KERNEL, &[0]), at::PREF_ADDRESS, &pref.to_le_bytes());
            assert_eq!(bad(SMALL, inputs(&fixed, "", None)), KernelDoesNotFit { start: pref, bytes: 4 * MIB });
        }
        let wrap = set(ok.clone(), at::PREF_ADDRESS, &(u64::MAX - 0xfff).to_le_bytes());
        assert_eq!(bad(SMALL, inputs(&wrap, "", None)), KernelDoesNotFit { start: u64::MAX, bytes: 4 * MIB });
        let wrap = set(set(ok.clone(), at::RELOCATABLE_KERNEL, &[0]), at::PREF_ADDRESS, &(u64::MAX - 0xfff).to_le_bytes());
        assert_eq!(bad(SMALL, inputs(&wrap, "", None)).code(), "KERNEL_DOES_NOT_FIT");
        assert_eq!(bad(Layout { low_end: 64 * MIB, high: 1 << 40 }, inputs(&ok, "", None)).code(), "BAD_LAYOUT");
    }

    #[test]
    fn layout_e820() {
        let map = Layout::DEFAULT.e820().unwrap();
        assert!(map.windows(2).all(|w| w[0].addr + w[0].size == w[1].addr), "contiguous and sorted");
        assert_eq!((map[0].addr, map.last().unwrap().addr + map.last().unwrap().size), (0, 4 * GIB));
        let ram: u64 = map.iter().filter(|e| e.kind == E820_RAM).map(|e| e.size).sum();
        assert_eq!(ram, 0x9_f000 + 2 * GIB - RAM_START);
        let high = Layout { low_end: GIB, high: 2 * GIB }.e820().unwrap();
        assert!(high.len() <= E820_MAX);
        assert_eq!(*high.last().unwrap(), E820 { addr: 4 * GIB, size: 2 * GIB, kind: E820_RAM });
    }

    /// Image values in "write" fields never reach the zero page.
    #[test]
    fn write_fields_are_the_loaders() {
        let mut d = image(0x1000);
        for (o, v) in [(at::RAMDISK_IMAGE, 0x200_0000u32), (at::RAMDISK_SIZE, 0x1000), (at::HEAP_END_PTR, 0x1234_5678), (at::HARDWARE_SUBARCH, 2), (at::SETUP_DATA, 0x12_3456), (at::SETUP_DATA + 4, 1), (at::HARDWARE_SUBARCH_DATA, 9)] {
            d[o..o + 4].copy_from_slice(&v.to_le_bytes());
        }
        d[at::LOADFLAGS] = 0xe1;
        let (_v, r) = ram(SMALL.low_end);
        load(&r, &SMALL, &inputs(&d, "", None)).unwrap();
        let mut z = vec![0u8; 4096];
        assert!(r.copy_out(ZERO_PAGE, &mut z));
        assert_eq!(z[at::LOADFLAGS], 1);
        for o in [at::RAMDISK_IMAGE, at::RAMDISK_SIZE, at::HEAP_END_PTR, at::HARDWARE_SUBARCH, at::SETUP_DATA, at::SETUP_DATA + 4, at::HARDWARE_SUBARCH_DATA] {
            assert_eq!(u32_at(&z, o), 0, "{o:#x}");
        }
    }

    /// The placement, zero page, tables and entry state of a good load (the Python checker judges the same from a dump).
    #[test]
    fn loads() {
        let ok = image(0x1000);
        let (_v, r) = ram(SMALL.low_end);
        let rd = vec![7u8; 5000];
        let tables = acpi::build(2, ACPI_NVS.0);
        let l = load(&r, &SMALL, &Inputs { image: &ok, cmdline: "mitigations=off nokaslr", initrd: Some(&rd), acpi: Some(&tables) }).unwrap();
        assert_eq!((l.kernel, l.window), (RAM_START, 4 * MIB));
        assert_eq!(l.initrd, Some((SMALL.low_end - 2 * PAGE, 5000)));
        let mut z = vec![0u8; 4096];
        assert!(r.copy_out(ZERO_PAGE, &mut z));
        let written = [at::TYPE_OF_LOADER..at::TYPE_OF_LOADER + 1, at::CODE32_START..at::RAMDISK_SIZE + 4, at::HEAP_END_PTR..at::CMD_LINE_PTR + 4, at::HARDWARE_SUBARCH..at::HARDWARE_SUBARCH_DATA + 8, at::SETUP_DATA..at::SETUP_DATA + 8];
        for o in (0x1f1..0x26c).filter(|o| !written.iter().any(|w| w.contains(o))) {
            assert_eq!(z[o], ok[o], "header byte {o:#x} copied");
        }
        assert_eq!(z[at::TYPE_OF_LOADER], 0xff);
        assert_eq!(z[0x1ef], 0, "sentinel");
        assert_eq!(u32_at(&z, at::CODE32_START), RAM_START as u32);
        assert_eq!(u32_at(&z, at::CMD_LINE_PTR), CMDLINE as u32);
        assert_eq!((u32_at(&z, at::RAMDISK_IMAGE), u32_at(&z, at::RAMDISK_SIZE)), ((SMALL.low_end - 2 * PAGE) as u32, 5000));
        assert_eq!(u64_at(&z, at::ACPI_RSDP_ADDR), ACPI_NVS.0);
        // Every table byte is in guest RAM and inside one of the loader's recorded writes.
        for (gpa, bytes) in &tables.blobs {
            assert!(r.matches(*gpa, bytes));
            assert!(l.writes.iter().any(|&(a, n)| a <= *gpa && gpa + bytes.len() as u64 <= a + n), "{gpa:#x}");
        }
        assert_eq!(z[at::E820_ENTRIES] as usize, SMALL.e820().unwrap().len());
        assert!(z[0x26c..].iter().enumerate().all(|(i, &b)| b == 0 || (at::E820_TABLE - 0x26c..at::E820_TABLE - 0x26c + 20 * 9).contains(&i)));
        let mut c = vec![0u8; 24];
        assert!(r.copy_out(CMDLINE, &mut c));
        assert_eq!(&c, b"mitigations=off nokaslr\0");
        assert!(r.matches(RAM_START, &ok[1024..]));
        assert!(r.matches(SMALL.low_end - 2 * PAGE, &rd));
        // Identity map: PML4[0] -> PDPT[0] -> PD with 32 2 MiB leaves.
        assert_eq!(r.read::<u64>(PAGE_TABLES), Some(PAGE_TABLES + PAGE | 3));
        assert_eq!(r.read::<u64>(PAGE_TABLES + PAGE), Some(PAGE_TABLES + 2 * PAGE | 3));
        assert_eq!(r.read::<u64>(PAGE_TABLES + 2 * PAGE + 8 * 31), Some(62 * MIB | 0x83));
        assert_eq!(r.read::<u64>(PAGE_TABLES + 2 * PAGE + 8 * 32), Some(0));
        let cpu = l.cpu;
        assert_eq!((cpu.rip, cpu.gpr[6], cpu.rflags & 0x200, cpu.cpl), (RAM_START + 0x200, ZERO_PAGE, 0, 0));
        assert_eq!((cpu.seg[exec::SREG_CS].selector, cpu.seg[exec::SREG_DS].selector, cpu.seg[exec::SREG_SS].selector), (0x10, 0x18, 0x18));
        assert_eq!(cpu.seg[exec::SREG_CS].attributes & (exec::ATTR_L | exec::ATTR_D), exec::ATTR_L);
        assert!(cpu.control().is_some(), "a mode the resident runs");
        // X4: the AP wake state (ACPI 6.5 Table 5.44): long mode, paging through the identity map, IF = 0, flat selectors
        // from the GDT in the NVS scratch (written there), NXE; no IDT.
        let ap = l.ap;
        assert_eq!((ap.cr3, ap.cr0, ap.efer, ap.rflags, ap.gdtr.base, ap.idtr.limit), (PAGE_TABLES, cpu.cr0, 0xd00, 2, AP_GDT, 0));
        assert_eq!(ap.seg.map(|s| s.selector), [0x18, 0x10, 0x18, 0x18, 0x18, 0x18]);
        assert_eq!(r.read::<u64>(AP_GDT + 16), Some(GDT_ENTRIES[2]));
        assert!(ap.control().is_some());
    }
}
