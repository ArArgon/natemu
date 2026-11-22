//! Multiboot v1 loader (boot-interpreter plan section 7): the Multiboot Specification 0.6.96's OS image format (3.1),
//! machine state (3.2) and boot information (3.3), as GRUB legacy and QEMU's `-kernel` (`hw/i386/multiboot.c`) implement
//! them. kvm-unit-tests' x86 tests are such images (`x86/cstart.S`, `cstart64.S`: an ELF32 with flags 0, entered at
//! `start`), and so are the reference rigs' stubs (`tests/interp/interp_stub.asm`: the address-field form).
//!
//! **Header.** The first 32-bit aligned magic 1BADB002H in the image's first 8192 bytes whose checksum holds (magic +
//! flags + checksum = 0 mod 2^32; a magic with a bad checksum is skipped, as GRUB and QEMU do). Flags bits 0-15 are
//! requirements: bit 0 (modules page-aligned: they always are here) and bit 1 (memory information: always given) are
//! met; any other (bit 2, a video mode, among them) refuses the image, as the specification requires of a loader that
//! does not provide it. Bit 16: the address fields (header_addr, load_addr, load_end_addr, bss_end_addr, entry_addr)
//! say where the image goes, and take precedence over an ELF header; without it the image must be an ELF32 i386
//! executable (`EM_386`, as QEMU requires), whose PT_LOAD segments are loaded at their physical addresses (`p_paddr`,
//! the bytes past `p_filesz` up to `p_memsz` zeroed) and whose entry is `e_entry`, translated to the physical address of
//! the segment whose virtual range holds it (GRUB 2's rule; for kvm-unit-tests virtual = physical).
//!
//! **Physical map.** The board's, without the Linux boot's loader reservations ([`super::Layout::e820`]: its page tables
//! at 1-2 MiB, the 2-16 MiB hole and NVS ranges): RAM [0, A0000H) and [1 MiB, L), the legacy hole reserved, and above L
//! the same reserved aperture and high RAM as that map. No ACPI tables: Multiboot has no field for the RSDP and the BIOS
//! areas an OS searches (the EBDA, E0000H-FFFFFH) are unassigned here, so a Multiboot OS starts its other processors
//! with INIT and SIPI ([`super::Loaded::ap`] is the INIT state).
//!
//! **Placement.** Every segment (with its bss) inside one RAM extent of the map, below 4 GiB. The modules follow the
//! kernel's highest byte (at 1 MiB at least), each page-aligned and in order; then, page-aligned, the boot data: a GDT
//! (null, flat 32-bit code 08H, flat data 10H, accessed), the information structure, the module list, the memory map and
//! the strings (the command line, each module's string, the loader's name "natemu"), every one inside [1 MiB, L).
//!
//! **Information** (3.3): flags MEMORY | CMDLINE | MODS | MMAP | LOADER_NAME. `mem_lower` 640 and `mem_upper` the KiB of
//! RAM from 1 MiB to the first hole above it ((L - 1 MiB) / 1024; kvm-unit-tests takes `mem_upper * 1024` as the end of
//! its memory); `cmdline` the command line as given (QEMU and GRUB put the kernel's name first, then the arguments:
//! kvm-unit-tests' `argv[0]`, so callers that want that pass it so); the modules in order (kvm-unit-tests reads exactly
//! one as its environment, `lib/x86/setup.c` `setup_multiboot`, and its `NR_CPUS` decides how many CPUs `smp_init`
//! waits for, since this board has no fw_cfg); the memory map as above (20-byte entries: size, base, length, type, with
//! the map's E820 types). No boot device, symbols, drives, configuration, APM or video information.
//!
//! **Entry state** (3.2): 32-bit protected mode, paging off: EAX = 2BADB002H, EBX = the information structure, CS = 08H
//! (base 0, limit 4 GiB, execute/read, 32-bit), DS = ES = FS = GS = SS = 10H (base 0, limit 4 GiB, read/write), GDTR the
//! loader's GDT, IDTR base 0 limit 3FFH (the real-mode IVT a BIOS leaves; the specification leaves it undefined), CR0 =
//! PE | ET, CR3 = CR4 = 0, EFER 0, EFLAGS 2 (IF = VM = 0), the other GPRs (ESP among them: the OS sets its own stack) 0,
//! CPL 0; the floating-point state as after FNINIT, caches on and the MSRs as firmware leaves them (`execution::set_cpu`:
//! `reset_msrs`, the MTRRs on).
use super::{E820, E820_RAM, E820_RESERVED, Layout, LoadError, Loaded, MIB, PAGE, place};
use natemu_abi::execution::{self as exec, Cpu, SREG_CS, Segment, Table};
use natemu_abi::guest_memory::GuestRam;

/// The header's magic, and the magic the OS finds in EAX.
pub const HEADER_MAGIC: u32 = 0x1bad_b002;
pub const BOOT_MAGIC: u32 = 0x2bad_b002;
/// The header lies in the image's first 8192 bytes.
const SEARCH: usize = 8192;
/// Header flags (3.1.2): the requirements this loader meets, and the address fields.
const PAGE_ALIGN: u32 = 1 << 0;
const MEMORY_INFO: u32 = 1 << 1;
const ADDRESS: u32 = 1 << 16;
/// Information flags (3.3).
mod info {
    pub const MEMORY: u32 = 1 << 0;
    pub const CMDLINE: u32 = 1 << 2;
    pub const MODS: u32 = 1 << 3;
    pub const MMAP: u32 = 1 << 6;
    pub const LOADER_NAME: u32 = 1 << 9;
    /// The structure through the framebuffer fields (offset 115).
    pub const BYTES: usize = 116;
}
/// The loader's GDT: null, flat 32-bit code (execute/read, accessed), flat data (read/write, accessed).
pub const CODE_SELECTOR: u16 = 0x08;
pub const DATA_SELECTOR: u16 = 0x10;
const GDT_ENTRIES: [u64; 3] = [0, 0x00cf_9b00_0000_ffff, 0x00cf_9300_0000_ffff];
/// The longest command line accepted (the boot data is placed after the modules, so this is only a sanity bound).
pub const CMDLINE_MAX: usize = 0xffff;
const LOADER_NAME: &str = "natemu";
const LOW_RAM: u64 = 0xa_0000;

/// A found header: its file offset, flags and, with flag bit 16, the address fields
/// [header_addr, load_addr, load_end_addr, bss_end_addr, entry_addr].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Header {
    pub offset: usize,
    pub flags: u32,
    pub address: Option<[u32; 5]>,
}

fn u16_at(d: &[u8], o: usize) -> u16 {
    u16::from_le_bytes(d[o..o + 2].try_into().unwrap())
}
fn u32_at(d: &[u8], o: usize) -> u32 {
    u32::from_le_bytes(d[o..o + 4].try_into().unwrap())
}

/// The image's Multiboot header, or `None` (no aligned magic with a valid checksum in its first 8192 bytes). A header
/// whose address fields (flag bit 16) end past those 8192 bytes is refused (`MultibootAddress`: the specification wants
/// the whole header inside them), and so is one the file ends inside (`TruncatedHeader`).
pub fn header(image: &[u8]) -> Option<Result<Header, LoadError>> {
    let end = image.len().min(SEARCH);
    let offset = (0..end.saturating_sub(11)).step_by(4).find(|&o| {
        let (magic, flags, sum) = (u32_at(image, o), u32_at(image, o + 4), u32_at(image, o + 8));
        magic == HEADER_MAGIC && magic.wrapping_add(flags).wrapping_add(sum) == 0
    })?;
    let flags = u32_at(image, offset + 4);
    if flags & ADDRESS == 0 {
        return Some(Ok(Header {
            offset,
            flags,
            address: None,
        }));
    }
    if offset + 32 > SEARCH {
        return Some(Err(LoadError::MultibootAddress(
            "the header's address fields end past the image's first 8192 bytes",
        )));
    }
    if image.len() < offset + 32 {
        return Some(Err(LoadError::TruncatedHeader(image.len())));
    }
    let a = core::array::from_fn(|k| u32_at(image, offset + 12 + 4 * k));
    Some(Ok(Header {
        offset,
        flags,
        address: Some(a),
    }))
}

/// One piece of the image in guest memory: `bytes` of the file at `gpa`, then zeros up to `size` bytes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Segment32 {
    pub gpa: u64,
    pub file: std::ops::Range<usize>,
    pub size: u64,
}
/// What the image says to load and where it starts.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Image {
    pub header: Header,
    pub segments: Vec<Segment32>,
    pub entry: u32,
}

/// The address-field form (3.1.3; QEMU's checks, in its order): the text starts header_addr - load_addr bytes before the
/// header in the file; load_end_addr 0 loads the rest of the file; bss_end_addr 0 means no bss.
fn by_address(image: &[u8], h: Header, a: [u32; 5]) -> Result<Image, LoadError> {
    use LoadError::MultibootAddress as Bad;
    let [header_addr, load_addr, load_end, bss_end, entry] = a.map(u64::from);
    if header_addr < load_addr {
        return Err(Bad("header_addr below load_addr"));
    }
    let text = (h.offset as u64)
        .checked_sub(header_addr - load_addr)
        .ok_or(Bad("header_addr - load_addr past the header's file offset"))?
        as usize;
    let load = if load_end != 0 {
        if load_end < load_addr {
            return Err(Bad("load_end_addr below load_addr"));
        }
        load_end - load_addr
    } else {
        (image.len() - text) as u64
    };
    if (image.len() as u64) < text as u64 + load {
        return Err(LoadError::TruncatedImage {
            have: image.len(),
            need: text as u64 + load,
        });
    }
    let size = if bss_end != 0 {
        if bss_end < load_addr + load {
            return Err(Bad("bss_end_addr below load_end_addr"));
        }
        bss_end - load_addr
    } else {
        load
    };
    if load_addr + size > 1 << 32 {
        return Err(Bad("the image does not fit below 4 GiB"));
    }
    Ok(Image {
        header: h,
        segments: vec![Segment32 {
            gpa: load_addr,
            file: text..text + load as usize,
            size,
        }],
        entry: entry as u32,
    })
}

/// The ELF form: an ELF32, little-endian, i386 executable's PT_LOAD segments (System V ABI, ELF32 header and program
/// headers).
fn by_elf(image: &[u8], h: Header) -> Result<Image, LoadError> {
    use LoadError::MultibootElf as Bad;
    if image.len() < 52 || image[..4] != *b"\x7fELF" {
        return Err(Bad("no address fields and not an ELF image"));
    }
    if image[4] != 1 || image[5] != 1 {
        return Err(Bad(
            "not ELF32 little-endian (an x86-64 ELF64 is refused, as QEMU does)",
        ));
    }
    if u16_at(image, 16) != 2 || u16_at(image, 18) != 3 {
        return Err(Bad(
            "not an i386 executable (e_type ET_EXEC, e_machine EM_386)",
        ));
    }
    let (entry, phoff) = (u32_at(image, 24), u32_at(image, 28) as usize);
    let (phentsize, phnum) = (u16_at(image, 42) as usize, u16_at(image, 44) as usize);
    if phnum == 0 || phentsize < 32 || image.len() < phoff.saturating_add(phentsize * phnum) {
        return Err(Bad("program headers missing or outside the file"));
    }
    let (mut segments, mut physical_entry) = (Vec::new(), None);
    for k in 0..phnum {
        let p = &image[phoff + k * phentsize..];
        let (kind, offset, vaddr, paddr) =
            (u32_at(p, 0), u32_at(p, 4), u32_at(p, 8), u32_at(p, 12));
        let (filesz, memsz) = (u32_at(p, 16), u32_at(p, 20));
        if kind != 1 || memsz == 0 {
            continue;
        }
        if filesz > memsz || image.len() < offset as usize + filesz as usize {
            return Err(Bad(
                "a PT_LOAD segment's file bytes are outside the file or exceed its memory size",
            ));
        }
        if u64::from(paddr) + u64::from(memsz) > 1 << 32 {
            return Err(Bad("a PT_LOAD segment does not fit below 4 GiB"));
        }
        // The segment's virtual range may wrap at 4 GiB; the physical entry lies in [paddr, paddr + memsz), below 4 GiB.
        if physical_entry.is_none() && entry.wrapping_sub(vaddr) < memsz {
            physical_entry = Some(entry.wrapping_sub(vaddr).wrapping_add(paddr));
        }
        segments.push(Segment32 {
            gpa: paddr.into(),
            file: offset as usize..offset as usize + filesz as usize,
            size: memsz.into(),
        });
    }
    if segments.is_empty() {
        return Err(Bad("no PT_LOAD segment"));
    }
    Ok(Image {
        header: h,
        segments,
        entry: physical_entry.unwrap_or(entry),
    })
}

/// Parses `image` as a Multiboot image: its header, what to load, its entry. `BadMagic` when it has no header.
pub fn parse(image: &[u8]) -> Result<Image, LoadError> {
    let h = header(image).ok_or(LoadError::BadMagic)??;
    if h.flags & 0xffff & !(PAGE_ALIGN | MEMORY_INFO) != 0 {
        return Err(LoadError::MultibootFlags(h.flags));
    }
    match h.address {
        Some(a) => by_address(image, h, a),
        None => by_elf(image, h),
    }
}

/// The physical map of a Multiboot boot (module doc): `layout` validated as [`Layout::e820`] does, then RAM [0, A0000H),
/// the legacy hole, RAM [1 MiB, L) and that map's entries from L on.
pub fn map(layout: &Layout) -> Result<Vec<E820>, LoadError> {
    let linux = layout.e820()?;
    let e = |addr: u64, end: u64, kind| E820 {
        addr,
        size: end - addr,
        kind,
    };
    let mut out = vec![
        e(0, LOW_RAM, E820_RAM),
        e(LOW_RAM, MIB, E820_RESERVED),
        e(MIB, layout.low_end, E820_RAM),
    ];
    out.extend(linux.into_iter().filter(|x| x.addr >= layout.low_end));
    Ok(out)
}

/// A module: its bytes and its string (3.3 `mods_addr`: "typically a command line").
pub struct Module<'a> {
    pub data: &'a [u8],
    pub string: &'a str,
}

fn align(v: u64) -> u64 {
    v.next_multiple_of(PAGE)
}

/// Loads the Multiboot image `image` with `cmdline` and `modules` into `ram` (zero where the loader leaves zeros, as
/// [`super::load`]) over the map of `layout` ([`map`]); returns the placement and the entry state: `kernel` and `window`
/// span the loaded image, `zero_page` is the information structure, `cmdline` the command line's address, `initrd` the
/// first module, `setup` `None`.
pub fn load(
    ram: &GuestRam,
    layout: &Layout,
    image: &[u8],
    cmdline: &str,
    modules: &[Module],
) -> Result<Loaded, LoadError> {
    let img = parse(image)?;
    let map = map(layout)?;
    for e in map.iter().filter(|e| e.kind == E820_RAM) {
        ram.range(e.addr, e.size).ok_or(LoadError::RamNotBacked {
            gpa: e.addr,
            bytes: e.size,
        })?;
    }
    if cmdline.len() > CMDLINE_MAX {
        return Err(LoadError::CmdlineTooLong {
            length: cmdline.len(),
            limit: CMDLINE_MAX,
        });
    }
    if cmdline.contains('\0') || modules.iter().any(|m| m.string.contains('\0')) {
        return Err(LoadError::CmdlineNul);
    }
    let in_ram = |gpa: u64, bytes: u64| {
        map.iter().any(|e| {
            e.kind == E820_RAM
                && e.addr <= gpa
                && gpa
                    .checked_add(bytes)
                    .is_some_and(|end| end <= e.addr + e.size && end <= 1 << 32)
        })
    };
    for s in &img.segments {
        if !in_ram(s.gpa, s.size) {
            return Err(LoadError::KernelDoesNotFit {
                start: s.gpa,
                bytes: s.size,
            });
        }
    }
    let kernel = img.segments.iter().map(|s| s.gpa).min().unwrap();
    let kernel_end = img.segments.iter().map(|s| s.gpa + s.size).max().unwrap();
    // The modules, page-aligned after the kernel (at 1 MiB at least), in order.
    let mut next = align(kernel_end.max(MIB));
    let mut placed = Vec::new();
    for m in modules {
        let bytes = m.data.len() as u64;
        if !in_ram(next, bytes) {
            return Err(LoadError::InitrdDoesNotFit { bytes });
        }
        placed.push((next, bytes));
        next = align(next + bytes);
    }
    // The boot data: GDT, information, module list, memory map, strings.
    let gdt = next;
    let mbi = gdt + 8 * GDT_ENTRIES.len() as u64;
    let mods = mbi + info::BYTES.next_multiple_of(8) as u64;
    let mmap = mods + 16 * modules.len() as u64;
    let strings = mmap + 24 * map.len() as u64;
    let mut data = Vec::new();
    for e in GDT_ENTRIES {
        data.extend(e.to_le_bytes());
    }
    let mut mbi_bytes = [0u8; info::BYTES];
    let mut text = Vec::new();
    let mut string = |s: &str| {
        let at = strings + text.len() as u64;
        text.extend(s.as_bytes());
        text.push(0);
        at as u32
    };
    let cmdline_at = string(cmdline);
    let mut list = Vec::new();
    for (m, &(at, bytes)) in modules.iter().zip(&placed) {
        for v in [at as u32, (at + bytes) as u32, string(m.string), 0] {
            list.extend(v.to_le_bytes());
        }
    }
    let name = string(LOADER_NAME);
    let mut entries = Vec::new();
    for e in &map {
        entries.extend(20u32.to_le_bytes());
        entries.extend(e.addr.to_le_bytes());
        entries.extend(e.size.to_le_bytes());
        entries.extend(e.kind.to_le_bytes());
    }
    let upper = map
        .iter()
        .find(|e| e.addr == MIB && e.kind == E820_RAM)
        .map_or(0, |e| e.size / 1024);
    let fields = [
        (
            0,
            info::MEMORY | info::CMDLINE | info::MODS | info::MMAP | info::LOADER_NAME,
        ),
        (4, (LOW_RAM / 1024) as u32),
        (8, upper as u32),
        (16, cmdline_at),
        (20, modules.len() as u32),
        (24, mods as u32),
        (44, entries.len() as u32),
        (48, mmap as u32),
        (64, name),
    ];
    for (o, v) in fields {
        mbi_bytes[o..o + 4].copy_from_slice(&v.to_le_bytes());
    }
    data.extend(mbi_bytes);
    data.resize((mods - gdt) as usize, 0);
    data.extend(list);
    data.extend(entries);
    data.extend(text);
    if !in_ram(gdt, data.len() as u64) {
        return Err(LoadError::BootDataDoesNotFit {
            start: gdt,
            bytes: data.len() as u64,
        });
    }

    let mut w = Vec::new();
    for s in &img.segments {
        place(ram, &mut w, s.gpa, &image[s.file.clone()])?;
        let zeros = s.size - s.file.len() as u64;
        if zeros > 0 {
            place(
                ram,
                &mut w,
                s.gpa + s.file.len() as u64,
                &vec![0; zeros as usize],
            )?;
        }
    }
    for (m, &(at, _)) in modules.iter().zip(&placed) {
        place(ram, &mut w, at, m.data)?;
    }
    place(ram, &mut w, gdt, &data)?;

    let mut gpr = [0; 16];
    (gpr[0], gpr[3]) = (u64::from(BOOT_MAGIC), mbi);
    let code = Segment::loaded(CODE_SELECTOR, GDT_ENTRIES[1]);
    let data_seg = Segment::loaded(DATA_SELECTOR, GDT_ENTRIES[2]);
    let cpu = Cpu {
        gpr,
        rip: img.entry.into(),
        rflags: 2,
        cr0: exec::CR0_PE | exec::CR0_ET,
        cr3: 0,
        cr4: 0,
        efer: 0,
        cpl: 0,
        cr2: 0,
        cr8: 0,
        dr: exec::DR_RESET,
        seg: [data_seg, code, data_seg, data_seg, data_seg, data_seg],
        ldtr: exec::LDTR_RESET,
        tr: exec::TR_RESET,
        gdtr: Table {
            base: gdt,
            limit: (8 * GDT_ENTRIES.len() - 1) as u64,
        },
        idtr: Table {
            base: 0,
            limit: 0x3ff,
        },
        mov_ss: 0,
        xmm: [[0; 2]; 16],
        mxcsr: exec::MXCSR_RESET,
        fcw: exec::FCW_INIT,
        xcr0: natemu_abi::cpu_profile::XCR0_RESET,
        no_afp: 0, // the host sets it in `execution::run`
        accurate_fp: 0,
        stack_traps: 0,
    };
    // The other processors: the INIT state (caches on, as firmware leaves them), waiting for a SIPI.
    let mut ap = cpu;
    natemu_abi::power_on::init(&mut ap);
    debug_assert_eq!(ap.seg[SREG_CS], natemu_abi::power_on::CS_RESET);
    Ok(Loaded {
        setup: None,
        kernel,
        window: kernel_end - kernel,
        zero_page: mbi,
        cmdline: cmdline_at.into(),
        initrd: placed.first().copied(),
        map,
        cpu,
        ap,
        writes: w,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::boot::tests::{SMALL, ram};

    /// A flat address-field image: `pre` bytes, the header (flags `flags` | bit 16) at file offset `pre` naming load
    /// address `load` + `pre` for it, `text` bytes of code after it (`hlt; jmp $-1` at the entry), `bss` bytes of bss.
    fn flat(pre: usize, flags: u32, load: u32, text: usize, bss: u32) -> Vec<u8> {
        let flags = flags | ADDRESS;
        let mut d = vec![0x90u8; pre];
        let header_addr = load + pre as u32;
        let load_end = header_addr + 32 + text as u32;
        let fields = [
            HEADER_MAGIC,
            flags,
            0u32.wrapping_sub(HEADER_MAGIC.wrapping_add(flags)),
            header_addr,
            load,
            load_end,
            if bss == 0 { 0 } else { load_end + bss },
            header_addr + 32,
        ];
        for f in fields {
            d.extend(f.to_le_bytes());
        }
        d.extend([0xf4, 0xeb, 0xfd]);
        d.resize(pre + 32 + text, 0xcc);
        d
    }

    /// A small ELF32 i386 executable with a Multiboot header (flags `flags`) at the start of its first segment: two
    /// PT_LOAD segments (text at `paddr` with `vaddr` as its virtual address, data with a bss) and a PT_NOTE.
    fn elf(flags: u32, vaddr: u32, paddr: u32) -> Vec<u8> {
        let mut d = vec![0u8; 0x1000];
        d[..4].copy_from_slice(b"\x7fELF");
        (d[4], d[5], d[6]) = (1, 1, 1);
        let put16 =
            |d: &mut Vec<u8>, o: usize, v: u16| d[o..o + 2].copy_from_slice(&v.to_le_bytes());
        let put32 =
            |d: &mut Vec<u8>, o: usize, v: u32| d[o..o + 4].copy_from_slice(&v.to_le_bytes());
        put16(&mut d, 16, 2);
        put16(&mut d, 18, 3);
        put32(&mut d, 20, 1);
        put32(&mut d, 24, vaddr.wrapping_add(12)); // e_entry: after the header
        put32(&mut d, 28, 52); // e_phoff
        put16(&mut d, 40, 52);
        put16(&mut d, 42, 32);
        put16(&mut d, 44, 3);
        // text: file 0x1000..0x1100 at paddr; data: file 0x1100..0x1180 at paddr + 0x2000, 0x400 in memory; a note.
        let phdrs = [
            [1, 0x1000, vaddr, paddr, 0x100, 0x100, 5, 0x1000],
            [
                1,
                0x1100,
                vaddr.wrapping_add(0x2000),
                paddr + 0x2000,
                0x80,
                0x400,
                6,
                0x1000,
            ],
            [4, 0x1180, 0, 0, 0x10, 0x10, 4, 4],
        ];
        for (k, ph) in phdrs.iter().enumerate() {
            for (i, v) in ph.iter().enumerate() {
                put32(&mut d, 52 + 32 * k + 4 * i, *v);
            }
        }
        d.resize(0x1190, 0);
        let header = [
            HEADER_MAGIC,
            flags,
            0u32.wrapping_sub(HEADER_MAGIC.wrapping_add(flags)),
        ];
        for (i, f) in header.iter().enumerate() {
            put32(&mut d, 0x1000 + 4 * i, *f);
        }
        d[0x100c..0x100f].copy_from_slice(&[0xf4, 0xeb, 0xfd]);
        for b in &mut d[0x1100..0x1180] {
            *b = 0xda;
        }
        d
    }

    fn read32(ram: &GuestRam, gpa: u64) -> u32 {
        ram.read::<u32>(gpa).unwrap()
    }
    fn bytes(ram: &GuestRam, gpa: u64, n: usize) -> Vec<u8> {
        let mut b = vec![0; n];
        assert!(ram.copy_out(gpa, &mut b));
        b
    }
    fn cstr(ram: &GuestRam, gpa: u64) -> String {
        let b = bytes(ram, gpa, 256);
        String::from_utf8(b[..b.iter().position(|&c| c == 0).unwrap()].to_vec()).unwrap()
    }

    #[test]
    fn header_search() {
        let img = flat(0x40, 0, 0x10_0000, 0x100, 0);
        let h = header(&img).unwrap().unwrap();
        assert_eq!((h.offset, h.flags), (0x40, ADDRESS));
        assert_eq!(h.address.unwrap()[..2], [0x10_0040, 0x10_0000]);
        // A magic with a bad checksum is skipped; an unaligned one is not seen; past 8 KiB none is found.
        let mut bad = vec![0u8; 8];
        bad.extend(HEADER_MAGIC.to_le_bytes());
        bad.extend([0; 8]);
        bad.extend(&img);
        assert_eq!(header(&bad).unwrap().unwrap().offset, 0x40 + 20);
        let mut odd = vec![0u8; 2];
        odd.extend(&img[0x40..0x40 + 12]);
        odd.resize(64, 0);
        assert_eq!(header(&odd), None);
        let far = [vec![0u8; 8192], img.clone()].concat();
        assert_eq!(header(&far), None);
        // The magic inside the first 8 KiB, its address fields past them: refused.
        let at0 = flat(0, 0, 0x10_0000, 0x100, 0);
        let edge = [vec![0u8; 8192 - 20], at0.clone()].concat();
        assert!(matches!(
            header(&edge),
            Some(Err(LoadError::MultibootAddress(_)))
        ));
        let inside = [vec![0u8; 8192 - 32], at0].concat();
        assert_eq!(header(&inside).unwrap().unwrap().offset, 8192 - 32);
        assert!(
            header(&img[..0x40 + 20]).unwrap().is_err(),
            "address fields cut off"
        );
        assert_eq!(parse(&[0; 64]), Err(LoadError::BadMagic));
    }

    #[test]
    fn requirement_flags() {
        for flags in [
            0,
            PAGE_ALIGN,
            MEMORY_INFO,
            PAGE_ALIGN | MEMORY_INFO,
            1 << 17,
        ] {
            assert!(
                parse(&flat(0, flags, 0x10_0000, 16, 0)).is_ok(),
                "{flags:#x}"
            );
        }
        for flags in [1 << 2, 1 << 3, 1 << 15, MEMORY_INFO | 1 << 2] {
            assert_eq!(
                parse(&flat(0, flags, 0x10_0000, 16, 0)),
                Err(LoadError::MultibootFlags(flags | ADDRESS))
            );
        }
    }

    #[test]
    fn address_fields() {
        // load_addr before the header: the bytes before the header are loaded too.
        let img = flat(0x20, 0, 0x20_0000, 0x40, 0x1000);
        let p = parse(&img).unwrap();
        assert_eq!(
            p.segments,
            [Segment32 {
                gpa: 0x20_0000,
                file: 0..img.len(),
                size: img.len() as u64 + 0x1000
            }]
        );
        assert_eq!(p.entry, 0x20_0000 + 0x20 + 32);
        // load_end 0: the rest of the file.
        let mut rest = img.clone();
        rest[0x20 + 20..0x20 + 24].fill(0);
        rest.extend([1, 2, 3]);
        let p = parse(&rest).unwrap();
        assert_eq!(p.segments[0].file, 0..rest.len());
        let bad = |k: usize, v: u32| {
            let mut d = img.clone();
            d[0x20 + 12 + 4 * k..0x20 + 16 + 4 * k].copy_from_slice(&v.to_le_bytes());
            parse(&d)
        };
        assert!(
            matches!(bad(0, 0x1f_ffff), Err(LoadError::MultibootAddress(_))),
            "header_addr < load_addr"
        );
        assert!(
            matches!(bad(1, 0x1f_0000), Err(LoadError::MultibootAddress(_))),
            "the text before the file"
        );
        assert!(
            matches!(bad(2, 0x1f_0000), Err(LoadError::MultibootAddress(_))),
            "load_end < load_addr"
        );
        assert!(
            matches!(bad(2, 0x30_0000), Err(LoadError::TruncatedImage { .. })),
            "load_end past the file"
        );
        assert!(
            matches!(bad(3, 0x20_0010), Err(LoadError::MultibootAddress(_))),
            "bss_end < load_end"
        );
        // header_addr = load_addr = FFFFFF80H with the rest of the 200H-byte file: past 4 GiB.
        let mut top = flat(0, 0, 0x10_0000, 0x1e0, 0);
        for (o, v) in [(12, 0xffff_ff80u32), (16, 0xffff_ff80), (20, 0)] {
            top[o..o + 4].copy_from_slice(&v.to_le_bytes());
        }
        assert!(
            matches!(parse(&top), Err(LoadError::MultibootAddress(_))),
            "past 4 GiB"
        );
    }

    #[test]
    fn elf_segments_and_entry() {
        let p = parse(&elf(0, 0x40_0000, 0x40_0000)).unwrap();
        assert_eq!(
            p.segments,
            [
                Segment32 {
                    gpa: 0x40_0000,
                    file: 0x1000..0x1100,
                    size: 0x100
                },
                Segment32 {
                    gpa: 0x40_2000,
                    file: 0x1100..0x1180,
                    size: 0x400
                }
            ]
        );
        assert_eq!(p.entry, 0x40_000c);
        // A virtual entry is translated through the segment holding it, also one whose virtual range wraps at 4 GiB.
        assert_eq!(
            parse(&elf(0, 0xc040_0000, 0x40_0000)).unwrap().entry,
            0x40_000c
        );
        let mut d = elf(0, 0xffff_f000, 0x40_0000);
        d[52 + 20..52 + 24].copy_from_slice(&0x2000u32.to_le_bytes()); // memsz 2000H: FFFFF000H..1000H
        d[24..28].copy_from_slice(&0x10u32.to_le_bytes()); // e_entry 10H
        assert_eq!(parse(&d).unwrap().entry, 0x40_1010);
        let mut d = elf(0, 0x40_0000, 0x40_0000);
        d[18] = 62; // EM_X86_64
        assert!(matches!(parse(&d), Err(LoadError::MultibootElf(_))));
        let mut d = elf(0, 0x40_0000, 0x40_0000);
        d[4] = 2; // ELFCLASS64
        assert!(matches!(parse(&d), Err(LoadError::MultibootElf(_))));
        let mut d = elf(0, 0x40_0000, 0x40_0000);
        d[52 + 16..52 + 20].copy_from_slice(&0x200u32.to_le_bytes()); // filesz > memsz
        assert!(matches!(parse(&d), Err(LoadError::MultibootElf(_))));
        // The header found, but no ELF and no address fields.
        let mut raw = vec![0u8; 64];
        raw[8..20].copy_from_slice(&elf(0, 0, 0)[0x1000..0x100c]);
        assert!(matches!(parse(&raw), Err(LoadError::MultibootElf(_))));
    }

    #[test]
    fn map_without_the_linux_reservations() {
        let m = map(&SMALL).unwrap();
        let tail = SMALL
            .e820()
            .unwrap()
            .into_iter()
            .filter(|e| e.addr >= SMALL.low_end);
        let want = [
            E820 {
                addr: 0,
                size: 0xa_0000,
                kind: E820_RAM,
            },
            E820 {
                addr: 0xa_0000,
                size: 0x6_0000,
                kind: E820_RESERVED,
            },
            E820 {
                addr: MIB,
                size: SMALL.low_end - MIB,
                kind: E820_RAM,
            },
        ];
        assert_eq!(m, want.into_iter().chain(tail).collect::<Vec<_>>());
        assert!(
            map(&Layout {
                low_end: 3 * MIB,
                ..SMALL
            })
            .is_err(),
            "the layout is validated"
        );
    }

    /// kvm-unit-tests' form (ELF, flags 0, at 4 MiB) with its environment as one module and a command line: the
    /// information structure as `lib/x86/setup.c` and `cstart64.S` read it, and the entry state.
    #[test]
    fn loads_an_elf_image_with_a_module() {
        let (_v, ram) = ram(SMALL.low_end);
        let img = elf(0, 0x40_0000, 0x40_0000);
        let env = b"NR_CPUS=4\nMEMSIZE=64\n";
        let modules = [Module {
            data: env,
            string: "env",
        }];
        let l = load(&ram, &SMALL, &img, "x86/smptest.flat -smp 4", &modules).unwrap();
        assert_eq!(
            (l.kernel, l.window, l.setup.is_none()),
            (0x40_0000, 0x2400, true)
        );
        assert_eq!(bytes(&ram, 0x40_0000, 0x100), img[0x1000..0x1100]);
        assert_eq!(bytes(&ram, 0x40_2000, 0x80), vec![0xda; 0x80]);
        assert_eq!(bytes(&ram, 0x40_2080, 0x380), vec![0; 0x380], "bss");
        // The module after the kernel, page-aligned; the boot data after it.
        assert_eq!(l.initrd, Some((0x40_3000, env.len() as u64)));
        assert_eq!(bytes(&ram, 0x40_3000, env.len()), env);
        let mbi = l.zero_page;
        assert_eq!(
            read32(&ram, mbi),
            0x24d,
            "MEMORY CMDLINE MODS MMAP LOADER_NAME"
        );
        assert_eq!(
            (read32(&ram, mbi + 4), read32(&ram, mbi + 8)),
            (640, 63 * 1024)
        );
        assert_eq!(
            cstr(&ram, read32(&ram, mbi + 16).into()),
            "x86/smptest.flat -smp 4"
        );
        assert_eq!(l.cmdline, u64::from(read32(&ram, mbi + 16)));
        assert_eq!(read32(&ram, mbi + 20), 1);
        let mods = u64::from(read32(&ram, mbi + 24));
        assert_eq!(
            (read32(&ram, mods), read32(&ram, mods + 4)),
            (0x40_3000, 0x40_3000 + env.len() as u32)
        );
        assert_eq!(cstr(&ram, read32(&ram, mods + 8).into()), "env");
        assert_eq!(cstr(&ram, read32(&ram, mbi + 64).into()), "natemu");
        // The memory map: 20-byte entries, the map's.
        let (n, at) = (
            read32(&ram, mbi + 44) as u64,
            u64::from(read32(&ram, mbi + 48)),
        );
        assert_eq!(n, 24 * l.map.len() as u64);
        for (k, e) in l.map.iter().enumerate() {
            let o = at + 24 * k as u64;
            assert_eq!(read32(&ram, o), 20);
            let quad = |at: u64| u64::from_le_bytes(bytes(&ram, at, 8).try_into().unwrap());
            assert_eq!(
                (quad(o + 4), quad(o + 12)),
                (e.addr, e.size),
                "unaligned u64s"
            );
            assert_eq!(read32(&ram, o + 20), e.kind);
        }
        // No boot device, symbols, drives, config, APM or video.
        for o in [12, 28, 32, 36, 40, 52, 56, 60, 68, 72] {
            assert_eq!(read32(&ram, mbi + o), 0, "offset {o}");
        }
        // Everything the loader wrote lies in RAM [1 MiB, L) and nothing overlaps.
        let mut w = l.writes.clone();
        w.sort();
        assert!(w.windows(2).all(|p| p[0].0 + p[0].1 <= p[1].0));
        assert!(w.iter().all(|&(a, n)| a >= MIB && a + n <= SMALL.low_end));

        // Entry (3.2): EAX the magic, EBX the information, flat 32-bit protected mode, paging off, IF = 0.
        let c = l.cpu;
        assert_eq!((c.gpr[0], c.gpr[3], c.rip), (0x2bad_b002, mbi, 0x40_000c));
        assert_eq!(
            c.gpr
                .iter()
                .enumerate()
                .filter(|&(i, &v)| i != 0 && i != 3 && v != 0)
                .count(),
            0
        );
        assert_eq!(
            (c.cr0, c.cr3, c.cr4, c.efer, c.rflags, c.cpl),
            (0x11, 0, 0, 0, 2, 0)
        );
        let cs = c.seg[SREG_CS];
        assert_eq!(
            (cs.selector, cs.base, cs.limit, cs.attributes),
            (8, 0, 0xffff_ffff, 0xc09b)
        );
        for r in [
            exec::SREG_ES,
            exec::SREG_SS,
            exec::SREG_DS,
            exec::SREG_FS,
            exec::SREG_GS,
        ] {
            let s = c.seg[r];
            assert_eq!(
                (s.selector, s.base, s.limit, s.attributes),
                (0x10, 0, 0xffff_ffff, 0xc093)
            );
        }
        // The GDT the selectors name, in RAM.
        assert_eq!(
            (c.gdtr.limit, ram.read::<u64>(c.gdtr.base + 8).unwrap()),
            (23, 0x00cf_9b00_0000_ffff)
        );
        assert_eq!(
            ram.read::<u64>(c.gdtr.base + 16).unwrap(),
            0x00cf_9300_0000_ffff
        );
        // The APs: the INIT state.
        let mut init = c;
        natemu_abi::power_on::init(&mut init);
        assert_eq!(l.ap, init);
        assert_eq!(
            (l.ap.rip, l.ap.seg[SREG_CS].base, l.ap.cr0),
            (0xfff0, 0xffff_0000, 0x10)
        );
    }

    /// The reference rigs' form (address fields, at 1 MiB, no module): loaded byte for byte with its bss zeroed; the
    /// boot data after it.
    #[test]
    fn loads_an_address_field_image() {
        let (_v, ram) = ram(SMALL.low_end);
        let img = flat(0, PAGE_ALIGN | MEMORY_INFO, 0x10_0000, 0x200, 0x3000);
        let l = load(&ram, &SMALL, &img, "", &[]).unwrap();
        assert_eq!(
            (l.kernel, l.window, l.initrd),
            (0x10_0000, img.len() as u64 + 0x3000, None)
        );
        assert_eq!(bytes(&ram, 0x10_0000, img.len()), img);
        assert_eq!(l.cpu.rip, 0x10_0020);
        assert_eq!(l.zero_page, align(0x10_0000 + l.window) + 24);
        assert_eq!(
            (read32(&ram, l.zero_page + 20), cstr(&ram, l.cmdline)),
            (0, String::new())
        );
    }

    #[test]
    fn placement_refusals() {
        let (_v, ram) = ram(SMALL.low_end);
        // In the legacy hole, across L, below 1 MiB but past conventional RAM.
        for load_at in [0xa_0000, SMALL.low_end as u32 - 0x100, 0x9_ff00] {
            assert!(matches!(
                load(&ram, &SMALL, &flat(0, 0, load_at, 0x200, 0), "", &[]),
                Err(LoadError::KernelDoesNotFit { .. })
            ));
        }
        // Low conventional RAM is fine: the module and the boot data then start at 1 MiB.
        let l = load(
            &ram,
            &SMALL,
            &flat(0, 0, 0x8000, 0x100, 0),
            "",
            &[Module {
                data: &[7; 10],
                string: "",
            }],
        )
        .unwrap();
        assert_eq!(l.initrd, Some((MIB, 10)));
        // A module that does not fit below L.
        let big = vec![0u8; (SMALL.low_end - 0x10_0000) as usize];
        assert!(matches!(
            load(
                &ram,
                &SMALL,
                &flat(0, 0, 0x10_0000, 0x100, 0),
                "",
                &[Module {
                    data: &big,
                    string: ""
                }]
            ),
            Err(LoadError::InitrdDoesNotFit { .. })
        ));
        // The boot data at the very end: the strings do not fit.
        let img = flat(
            0,
            0,
            0x10_0000,
            0x100,
            (SMALL.low_end - 0x10_0000 - 0x1000) as u32 - 0x120,
        );
        let long = "a".repeat(0x2000);
        assert!(matches!(
            load(&ram, &SMALL, &img, &long, &[]),
            Err(LoadError::BootDataDoesNotFit { .. })
        ));
        assert_eq!(
            load(&ram, &SMALL, &img, "a\0b", &[]).unwrap_err(),
            LoadError::CmdlineNul
        );
        assert!(matches!(
            load(&ram, &SMALL, &img, &"a".repeat(CMDLINE_MAX + 1), &[]),
            Err(LoadError::CmdlineTooLong { .. })
        ));
    }
}
