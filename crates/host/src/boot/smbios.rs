// Copyright © 2020 Intel Corporation
//
// Copyright 2019 The Chromium OS Authors. All rights reserved.
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.
//
// SPDX-License-Identifier: Apache-2.0 AND BSD-3-Clause
//
// Adapted from cloud-hypervisor arch/src/smbios.rs at f84dae558c8b9518e9d9bf8c0f1b64cd4dc8e546 (licence texts in
// third_party/cloud-hypervisor/). Upstream's are the packed structure layouts of the entry point and types 0, 1, 3, 11 and
// 127, the checksum, the string-index allocation and the string-set encoding (DSP0134 6.1.3: each string NUL-terminated,
// the set ended by one more NUL, an empty set by two). Changed: the table is built into a byte vector for a given
// guest-physical address (no vm-memory, uuid or thiserror: a local `ByteValued`, the nil UUID, no error type, since nothing
// here can fail), the entry point is returned for a ROM page instead of written to guest memory, and the version is 3.0.0;
// types 0, 1 and 3 carry natemu's values (type 3 always written); types 4, 16, 17, 19 and 32 are natemu's. Upstream's OEM
// strings (type 11) stay, unused, for a later systemd credential path.

//! SMBIOS 3.0 tables of the virtual firmware (platform-devices plan, "SMBIOS"): the structure table, built at load time into
//! the ACPI NVS range after the ACPI tables and placed by the loader with them ([`super::Inputs::smbios`]), and the 24-byte
//! SMBIOS 3.0 entry point, which the host writes into a read-only legacy ROM page at [`ENTRY_POINT`] (F0000H, where a PC's
//! system BIOS lies and where Linux's DMI scan of F0000H-FFFFFH finds it: `Backing::write_legacy_rom`).
//!
//! Values per DMTF DSP0134 3.0.0 and, where it leaves a choice, QEMU's `pc` (`hw/smbios/smbios.c`, a behaviour reference):
//! type 0 (BIOS: vendor `natemu`, version `natemu <crate version>`, release date [`RELEASE_DATE`], characteristics
//! "not supported", a virtual machine), 1 (system: `natemu` / `natemu x86-64`, the crate version, UUID not set, woken by the
//! power switch, family `natemu`), 3 (enclosure: Other, safe), 4 (one processor per vCPU: central, family Other, the CPU
//! profile's CPUID leaf 1 EAX and EDX as its ID, 2000 MHz, populated and enabled, one core and one thread), 16 (one
//! physical memory array: system memory, multi-bit ECC as QEMU's, its capacity the RAM), 17 (one memory device per 16 GiB of
//! RAM, as QEMU's DIMMs), 19 (one mapped address range per RAM extent of the boot layout: [0, low end) and [4 GiB, 4 GiB +
//! high)), 32 (boot status: no errors) and 127 (end). Handles are consecutive from 1 (upstream's).

use super::Layout;

/// The entry point's guest-physical address: the first paragraph of the legacy ROM page F0000H-F0FFFH.
pub const ENTRY_POINT: u64 = 0xF_0000;
/// Type 0's BIOS release date (a constant: the table does not change with the build date).
pub const RELEASE_DATE: &str = "11/23/2026";

const SM3_MAGIC_IDENT: &[u8; 5usize] = b"_SM3_";
const BIOS_INFORMATION: u8 = 0;
const SYSTEM_INFORMATION: u8 = 1;
const SYSTEM_ENCLOSURE: u8 = 3;
const PROCESSOR_INFORMATION: u8 = 4;
const OEM_STRINGS: u8 = 11;
const PHYSICAL_MEMORY_ARRAY: u8 = 16;
const MEMORY_DEVICE: u8 = 17;
const MEMORY_ARRAY_MAPPED_ADDRESS: u8 = 19;
const SYSTEM_BOOT_INFORMATION: u8 = 32;
const END_OF_TABLE: u8 = 127;
/// Type 0 characteristics bit 3: "BIOS Characteristics are not supported" (DSP0134 7.1.1).
const CHARACTERISTICS_NOT_SUPPORTED: u64 = 1 << 3;
/// Type 0 characteristics extension byte 2 bit 4: "SMBIOS table describes a virtual machine" (7.1.2.2).
const IS_VIRTUAL_MACHINE: u8 = 1 << 4;
const SYSTEM_WAKE_UP_TYPE_POWER_SWITCH: u8 = 0x06;
const CHASSIS_TYPE_OTHER: u8 = 0x01;
const CHASSIS_STATE_SAFE: u8 = 0x03;
/// Security status "Unknown" (7.4.3), as QEMU's.
const CHASSIS_SECURITY_STATUS_UNKNOWN: u8 = 0x02;
const PROCESSOR_TYPE_CENTRAL: u8 = 0x03;
const PROCESSOR_FAMILY_OTHER: u8 = 0x01;
const PROCESSOR_UPGRADE_OTHER: u8 = 0x01;
/// Socket populated (bit 6), CPU enabled (bits 2:0 = 1).
const PROCESSOR_STATUS_ENABLED: u8 = 0x41;
/// Processor characteristics: bit 1 "Unknown".
const PROCESSOR_CHARACTERISTICS_UNKNOWN: u16 = 0x02;
const SPEED_MHZ: u16 = 2000;
/// No cache structure (the L1-L3 cache handles).
const NO_HANDLE: u16 = 0xFFFF;
/// "Not provided": no memory error information structure (7.17.4).
const NO_ERROR_INFORMATION: u16 = 0xFFFE;
const MEMORY_LOCATION_OTHER: u8 = 0x01;
const MEMORY_USE_SYSTEM: u8 = 0x03;
const MEMORY_ERROR_CORRECTION_MULTI_BIT_ECC: u8 = 0x06;
const MEMORY_FORM_FACTOR_DIMM: u8 = 0x09;
const MEMORY_TYPE_RAM: u8 = 0x07;
/// Type detail bit 1, "Other" (QEMU's).
const MEMORY_TYPE_DETAIL_OTHER: u16 = 0x02;
const UNKNOWN_WIDTH: u16 = 0xFFFF;
/// QEMU's DIMM size: one type 17 per 16 GiB of RAM.
const DIMM_BYTES: u64 = 16 << 30;
const KIB: u64 = 1 << 10;
const MIB: u64 = 1 << 20;

/// What the tables describe.
#[derive(Clone, Copy, Debug)]
pub struct SmbiosInput {
    /// vCPUs: one type 4 structure each.
    pub vcpus: usize,
    /// The CPU profile's CPUID leaf 1 EAX and EDX (`natemu_abi::cpu::cpuid` at the boot processor's start): type 4's ID.
    pub cpuid_1: (u32, u32),
    /// The boot memory map: its RAM extents (types 16, 17, 19).
    pub layout: Layout,
    /// natemu's version (the crate version): types 0 and 1.
    pub version: &'static str,
}

/// The structure table (for the guest-physical address [`build`] was given) and its entry point.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Smbios {
    pub table: Vec<u8>,
    pub entry: [u8; 24],
}
impl Smbios {
    /// The table's guest-physical address (the entry point's structure table address).
    pub fn table_at(&self) -> u64 {
        u64::from_le_bytes(self.entry[16..24].try_into().unwrap())
    }
}

/// Plain integers only (upstream's `vm_memory::ByteValued`, reduced to what the builder uses).
///
/// # Safety
/// The type is `repr(C, packed)` of integers and integer arrays: every byte is initialised, no padding.
unsafe trait ByteValued: Copy {
    fn as_slice(&self) -> &[u8] {
        // SAFETY: the trait's contract: `size_of::<Self>()` initialised bytes, alignment 1.
        unsafe { std::slice::from_raw_parts((self as *const Self).cast::<u8>(), size_of::<Self>()) }
    }
}

fn compute_checksum<T: Copy + ByteValued>(v: &T) -> u8 {
    let mut checksum: u8 = 0;
    for i in v.as_slice().iter() {
        checksum = checksum.wrapping_add(*i);
    }
    (!checksum).wrapping_add(1)
}

#[repr(C, packed)]
#[derive(Default, Copy, Clone)]
struct Smbios30Entrypoint {
    signature: [u8; 5usize],
    checksum: u8,
    length: u8,
    majorver: u8,
    minorver: u8,
    docrev: u8,
    revision: u8,
    reserved: u8,
    max_size: u32,
    physptr: u64,
}

#[repr(C, packed)]
#[derive(Default, Copy, Clone)]
struct SmbiosBiosInfo {
    r#type: u8,
    length: u8,
    handle: u16,
    vendor: u8,
    version: u8,
    start_addr: u16,
    release_date: u8,
    rom_size: u8,
    characteristics: u64,
    characteristics_ext1: u8,
    characteristics_ext2: u8,
}

#[repr(C, packed)]
#[derive(Default, Copy, Clone)]
struct SmbiosSysInfo {
    r#type: u8,
    length: u8,
    handle: u16,
    manufacturer: u8,
    product_name: u8,
    version: u8,
    serial_number: u8,
    uuid: [u8; 16usize],
    wake_up_type: u8,
    sku: u8,
    family: u8,
}

#[repr(C, packed)]
#[derive(Default, Copy, Clone)]
struct SmbiosOemStrings {
    r#type: u8,
    length: u8,
    handle: u16,
    count: u8,
}

/// SMBIOS Chassis Table (Type 3) as defined in DMTF SMBIOS 3.9.0:
/// https://www.dmtf.org/sites/default/files/standards/documents/DSP0134_3.9.0.pdf
/// Note: trailing fields are omitted, so this structure is not complete.
#[repr(C, packed)]
#[derive(Default, Copy, Clone)]
struct SmbiosChassis {
    r#type: u8,
    length: u8,
    handle: u16,
    manufacturer: u8,
    chassis_type: u8,
    version: u8,
    serial_number: u8,
    asset_tag: u8,
    bootup_state: u8,
    power_supply_state: u8,
    thermal_state: u8,
    security_status: u8,
    oem_defined: u32,
    height: u8,
    number_of_power_cords: u8,
    contained_element_count: u8,
    contained_element_record_length: u8,
    // followed by contained element records (optional, variable-length)
    // followed by sku_number: u8, rack_type: u8, rack_height: u8
}

/// Processor Information (Type 4), DSP0134 3.0.0 7.5: its 3.0 length (0x30, Core Count 2 to Thread Count 2 included).
#[repr(C, packed)]
#[derive(Default, Copy, Clone)]
struct SmbiosProcessor {
    r#type: u8,
    length: u8,
    handle: u16,
    socket: u8,
    processor_type: u8,
    family: u8,
    manufacturer: u8,
    processor_id: [u32; 2usize],
    version: u8,
    voltage: u8,
    external_clock: u16,
    max_speed: u16,
    current_speed: u16,
    status: u8,
    upgrade: u8,
    l1_cache: u16,
    l2_cache: u16,
    l3_cache: u16,
    serial_number: u8,
    asset_tag: u8,
    part_number: u8,
    core_count: u8,
    core_enabled: u8,
    thread_count: u8,
    characteristics: u16,
    family2: u16,
    core_count2: u16,
    core_enabled2: u16,
    thread_count2: u16,
}

/// Physical Memory Array (Type 16), 7.17: the 2.7 length (0x17, Extended Maximum Capacity included).
#[repr(C, packed)]
#[derive(Default, Copy, Clone)]
struct SmbiosMemoryArray {
    r#type: u8,
    length: u8,
    handle: u16,
    location: u8,
    r#use: u8,
    error_correction: u8,
    maximum_capacity: u32,
    error_information: u16,
    devices: u16,
    extended_maximum_capacity: u64,
}

/// Memory Device (Type 17), 7.18: the 2.8 length (0x28, the voltages included), unchanged in 3.0.
#[repr(C, packed)]
#[derive(Default, Copy, Clone)]
struct SmbiosMemoryDevice {
    r#type: u8,
    length: u8,
    handle: u16,
    array: u16,
    error_information: u16,
    total_width: u16,
    data_width: u16,
    size: u16,
    form_factor: u8,
    device_set: u8,
    device_locator: u8,
    bank_locator: u8,
    memory_type: u8,
    type_detail: u16,
    speed: u16,
    manufacturer: u8,
    serial_number: u8,
    asset_tag: u8,
    part_number: u8,
    attributes: u8,
    extended_size: u32,
    configured_speed: u16,
    minimum_voltage: u16,
    maximum_voltage: u16,
    configured_voltage: u16,
}

/// Memory Array Mapped Address (Type 19), 7.20: the 2.7 length (0x1F, the extended addresses included).
#[repr(C, packed)]
#[derive(Default, Copy, Clone)]
struct SmbiosMappedAddress {
    r#type: u8,
    length: u8,
    handle: u16,
    starting_address: u32,
    ending_address: u32,
    array: u16,
    partition_width: u8,
    extended_starting_address: u64,
    extended_ending_address: u64,
}

/// System Boot Information (Type 32), 7.33: the minimal length (0x0B, one status byte).
#[repr(C, packed)]
#[derive(Default, Copy, Clone)]
struct SmbiosBootInfo {
    r#type: u8,
    length: u8,
    handle: u16,
    reserved: [u8; 6usize],
    boot_status: u8,
}

#[repr(C, packed)]
#[derive(Default, Copy, Clone)]
struct SmbiosEndOfTable {
    r#type: u8,
    length: u8,
    handle: u16,
}

// SAFETY: data structure only contain a series of integers
unsafe impl ByteValued for Smbios30Entrypoint {}
// SAFETY: data structure only contain a series of integers
unsafe impl ByteValued for SmbiosBiosInfo {}
// SAFETY: data structure only contain a series of integers
unsafe impl ByteValued for SmbiosSysInfo {}
// SAFETY: data structure only contain a series of integers
unsafe impl ByteValued for SmbiosOemStrings {}
// SAFETY: data structure only contain a series of integers
unsafe impl ByteValued for SmbiosChassis {}
// SAFETY: data structure only contain a series of integers
unsafe impl ByteValued for SmbiosProcessor {}
// SAFETY: data structure only contain a series of integers
unsafe impl ByteValued for SmbiosMemoryArray {}
// SAFETY: data structure only contain a series of integers
unsafe impl ByteValued for SmbiosMemoryDevice {}
// SAFETY: data structure only contain a series of integers
unsafe impl ByteValued for SmbiosMappedAddress {}
// SAFETY: data structure only contain a series of integers
unsafe impl ByteValued for SmbiosBootInfo {}
// SAFETY: data structure only contain a series of integers
unsafe impl ByteValued for SmbiosEndOfTable {}
const _: () = assert!(
    size_of::<Smbios30Entrypoint>() == 0x18
        && size_of::<SmbiosBiosInfo>() == 0x14
        && size_of::<SmbiosSysInfo>() == 0x1b
        && size_of::<SmbiosChassis>() == 0x15
        && size_of::<SmbiosProcessor>() == 0x30
        && size_of::<SmbiosMemoryArray>() == 0x17
        && size_of::<SmbiosMemoryDevice>() == 0x28
        && size_of::<SmbiosMappedAddress>() == 0x1f
        && size_of::<SmbiosBootInfo>() == 0x0b
);

/// The table being written (upstream's guest-memory cursor, here a byte vector).
struct Writer(Vec<u8>);
impl Writer {
    fn write_and_incr<T: ByteValued>(&mut self, val: T) {
        self.0.extend_from_slice(val.as_slice());
    }
    fn write_string(&mut self, val: &str) {
        debug_assert!(!val.is_empty() && !val.contains('\0'), "a string of a string set: nonempty, no NUL");
        self.0.extend_from_slice(val.as_bytes());
        self.0.push(0);
    }
    fn write_opt_string(&mut self, s: Option<&str>) {
        if let Some(v) = s {
            self.write_string(v)
        }
    }
    fn write_string_terminator(&mut self, has_strings: bool) {
        // SMBIOS DSP0134 §6.1.3: if all string-reference fields are 0, follow the
        // formatted section with two null bytes (empty string-set).
        self.0.push(0);
        if !has_strings {
            self.0.push(0);
        }
    }
    /// A structure: its formatted section, then `strings` (in index order, 1 first) as its string set.
    fn structure<T: ByteValued>(&mut self, val: T, strings: &[&str]) {
        self.write_and_incr(val);
        for s in strings {
            self.write_string(s);
        }
        self.write_string_terminator(!strings.is_empty());
    }
}

/// Allocate the next string index for an SMBIOS string-set.
///
/// Per SMBIOS DSP0134, index `0` means "no string", so valid indices run from
/// `1` to `255`. Returns `0` when `present` is `false`. Otherwise returns the
/// current value of `*next` and advances it by one. (Upstream fails once all 255
/// indices are used; no structure here has more than four strings.)
fn alloc_index(next: &mut u8, present: bool) -> u8 {
    if !present {
        return 0;
    }
    let idx = *next;
    assert!(idx != 0, "SMBIOS string index overflow (u8 limit reached: {})", u8::MAX);
    *next = next.wrapping_add(1);
    idx
}

/// Upstream's OEM strings (type 11), not written today: kept for a later systemd credential path.
#[allow(dead_code)]
fn write_type11_oem_strings(w: &mut Writer, handle: &mut u16, oem_strings: &[&str]) {
    if oem_strings.is_empty() {
        return;
    }
    *handle += 1;
    let smbios_oemstrings = SmbiosOemStrings {
        r#type: OEM_STRINGS,
        length: size_of::<SmbiosOemStrings>() as u8,
        handle: *handle,
        count: oem_strings.len() as u8,
    };
    w.structure(smbios_oemstrings, oem_strings);
}

/// The tables of a direct boot with `vcpus` processors over `layout`, after the ACPI tables `acpi` (16-byte aligned, as
/// both load paths place them: `linux::Boot::prepare` and the test driver's load-only dump). Type 4's processor ID is the
/// CPU profile's CPUID leaf 1 EAX and EDX as the boot processor reads them at its start (`natemu_abi::cpu::cpuid` with the
/// virtual firmware's MSRs, `cpu::reset_msrs`: the local APIC enabled; CR4 and XCR0 at their start values).
pub fn for_board(vcpus: usize, layout: Layout, acpi: &super::acpi::Tables) -> Smbios {
    use natemu_abi::cpu;
    let msrs = cpu::reset_msrs(true);
    let d = cpu::Dynamic {
        vcpus: vcpus as u64,
        vcpu: 0,
        cr4: 0,
        apic_base: msrs[cpu::msr(cpu::MSR_APIC_BASE).unwrap()],
        misc_enable: msrs[cpu::msr(cpu::MSR_MISC_ENABLE).unwrap()],
        xcr0: natemu_abi::cpu_profile::XCR0_RESET,
    };
    let [eax, _, _, edx] = cpu::cpuid(1, 0, &d);
    let input = SmbiosInput { vcpus, cpuid_1: (eax, edx), layout, version: env!("CARGO_PKG_VERSION") };
    build(&input, acpi.end().next_multiple_of(16))
}

/// The SMBIOS 3.0 structure table for `input`, to lie at guest-physical `table_at`, and its entry point (DSP0134 5.2.2:
/// anchor `_SM3_`, length 18H, version 3.0.0, entry point revision 1, the table's maximum size and address, checksummed
/// to zero).
pub fn build(input: &SmbiosInput, table_at: u64) -> Smbios {
    let mut w = Writer(Vec::new());
    let mut handle = 0u16;
    let ram = input.layout.low_end + input.layout.high;

    {
        handle += 1;
        let bios_version = format!("natemu {}", input.version);
        let mut next = 1u8;
        let smbios_biosinfo = SmbiosBiosInfo {
            r#type: BIOS_INFORMATION,
            length: size_of::<SmbiosBiosInfo>() as u8,
            handle,
            vendor: alloc_index(&mut next, true),
            version: alloc_index(&mut next, true),
            release_date: alloc_index(&mut next, true),
            characteristics: CHARACTERISTICS_NOT_SUPPORTED,
            characteristics_ext2: IS_VIRTUAL_MACHINE,
            ..Default::default()
        };
        w.structure(smbios_biosinfo, &["natemu", &bios_version, RELEASE_DATE]);
    }

    {
        handle += 1;
        let (manufacturer, product, version, family) = ("natemu", "natemu x86-64", input.version, "natemu");
        let mut next = 1u8;
        let sys = SmbiosSysInfo {
            r#type: SYSTEM_INFORMATION,
            length: size_of::<SmbiosSysInfo>() as u8,
            handle,
            manufacturer: alloc_index(&mut next, true),
            product_name: alloc_index(&mut next, true),
            version: alloc_index(&mut next, true),
            serial_number: alloc_index(&mut next, false),
            uuid: [0; 16], // the nil UUID: not set (7.2.1)
            wake_up_type: SYSTEM_WAKE_UP_TYPE_POWER_SWITCH,
            sku: alloc_index(&mut next, false),
            family: alloc_index(&mut next, true),
        };
        w.write_and_incr(sys);
        w.write_string(manufacturer);
        w.write_string(product);
        w.write_opt_string(Some(version));
        w.write_string(family);
        w.write_string_terminator(true);
    }

    {
        handle += 1;
        let mut next = 1u8;
        let ch = SmbiosChassis {
            r#type: SYSTEM_ENCLOSURE,
            length: size_of::<SmbiosChassis>() as u8,
            handle,
            manufacturer: alloc_index(&mut next, true),
            chassis_type: CHASSIS_TYPE_OTHER,
            bootup_state: CHASSIS_STATE_SAFE,
            power_supply_state: CHASSIS_STATE_SAFE,
            thermal_state: CHASSIS_STATE_SAFE,
            security_status: CHASSIS_SECURITY_STATUS_UNKNOWN,
            ..Default::default()
        };
        w.structure(ch, &["natemu"]);
    }

    for n in 0..input.vcpus {
        handle += 1;
        let socket = format!("CPU {n}");
        let mut next = 1u8;
        let cpu = SmbiosProcessor {
            r#type: PROCESSOR_INFORMATION,
            length: size_of::<SmbiosProcessor>() as u8,
            handle,
            socket: alloc_index(&mut next, true),
            processor_type: PROCESSOR_TYPE_CENTRAL,
            family: PROCESSOR_FAMILY_OTHER,
            manufacturer: alloc_index(&mut next, true),
            processor_id: [input.cpuid_1.0, input.cpuid_1.1],
            max_speed: SPEED_MHZ,
            current_speed: SPEED_MHZ,
            status: PROCESSOR_STATUS_ENABLED,
            upgrade: PROCESSOR_UPGRADE_OTHER,
            l1_cache: NO_HANDLE,
            l2_cache: NO_HANDLE,
            l3_cache: NO_HANDLE,
            core_count: 1,
            core_enabled: 1,
            thread_count: 1,
            characteristics: PROCESSOR_CHARACTERISTICS_UNKNOWN,
            family2: u16::from(PROCESSOR_FAMILY_OTHER),
            core_count2: 1,
            core_enabled2: 1,
            thread_count2: 1,
            ..Default::default()
        };
        w.structure(cpu, &[&socket, "natemu"]);
    }

    handle += 1;
    let array = handle;
    let devices = ram.div_ceil(DIMM_BYTES);
    {
        // Maximum Capacity in KiB; from 2 TiB (8000 0000H KiB) that value and the capacity in bytes in the extended field.
        let (maximum_capacity, extended) = match ram / KIB {
            kib if kib < 0x8000_0000 => (kib as u32, 0),
            _ => (0x8000_0000, ram),
        };
        let a = SmbiosMemoryArray {
            r#type: PHYSICAL_MEMORY_ARRAY,
            length: size_of::<SmbiosMemoryArray>() as u8,
            handle,
            location: MEMORY_LOCATION_OTHER,
            r#use: MEMORY_USE_SYSTEM,
            error_correction: MEMORY_ERROR_CORRECTION_MULTI_BIT_ECC,
            maximum_capacity,
            error_information: NO_ERROR_INFORMATION,
            devices: devices as u16,
            extended_maximum_capacity: extended,
        };
        w.structure(a, &[]);
    }

    for n in 0..devices {
        handle += 1;
        let locator = format!("DIMM {n}");
        let mib = (ram - n * DIMM_BYTES).min(DIMM_BYTES) / MIB;
        // Size in MiB (bit 15 clear); from 32 GiB - 1 MiB, 7FFFH and the size in MiB in Extended Size (7.18.5).
        let (size, extended_size) = if mib < 0x7FFF { (mib as u16, 0) } else { (0x7FFF, mib as u32) };
        let mut next = 1u8;
        let d = SmbiosMemoryDevice {
            r#type: MEMORY_DEVICE,
            length: size_of::<SmbiosMemoryDevice>() as u8,
            handle,
            array,
            error_information: NO_ERROR_INFORMATION,
            total_width: UNKNOWN_WIDTH,
            data_width: UNKNOWN_WIDTH,
            size,
            form_factor: MEMORY_FORM_FACTOR_DIMM,
            device_locator: alloc_index(&mut next, true),
            memory_type: MEMORY_TYPE_RAM,
            type_detail: MEMORY_TYPE_DETAIL_OTHER,
            manufacturer: alloc_index(&mut next, true),
            extended_size,
            ..Default::default()
        };
        w.structure(d, &[&locator, "natemu"]);
    }

    let mut extents = vec![(0, input.layout.low_end)];
    if input.layout.high > 0 {
        extents.push((4 << 30, (4 << 30) + input.layout.high));
    }
    for (start, end) in extents {
        handle += 1;
        // Addresses in KiB (the ending one its last KiB); from 4 TiB, FFFF FFFFH and the byte addresses (7.20.1).
        let ((s, e), (xs, xe)) = match (end - 1) / KIB {
            last if last < 0xFFFF_FFFF => (((start / KIB) as u32, last as u32), (0, 0)),
            _ => ((0xFFFF_FFFF, 0xFFFF_FFFF), (start, end - 1)),
        };
        let m = SmbiosMappedAddress {
            r#type: MEMORY_ARRAY_MAPPED_ADDRESS,
            length: size_of::<SmbiosMappedAddress>() as u8,
            handle,
            starting_address: s,
            ending_address: e,
            array,
            partition_width: 1, // one device per row, as QEMU's
            extended_starting_address: xs,
            extended_ending_address: xe,
        };
        w.structure(m, &[]);
    }

    {
        handle += 1;
        let b = SmbiosBootInfo {
            r#type: SYSTEM_BOOT_INFORMATION,
            length: size_of::<SmbiosBootInfo>() as u8,
            handle,
            boot_status: 0, // no errors detected
            ..Default::default()
        };
        w.structure(b, &[]);
    }

    {
        handle += 1;
        let smbios_end = SmbiosEndOfTable {
            r#type: END_OF_TABLE,
            length: size_of::<SmbiosEndOfTable>() as u8,
            handle,
        };
        w.structure(smbios_end, &[]);
    }

    let table = w.0;
    let mut smbios_ep = Smbios30Entrypoint {
        signature: *SM3_MAGIC_IDENT,
        length: size_of::<Smbios30Entrypoint>() as u8,
        // SMBIOS 3.0.0
        majorver: 0x03,
        minorver: 0x00,
        docrev: 0x00,
        revision: 0x01, // SMBIOS 3.0
        max_size: u32::try_from(table.len()).unwrap(),
        physptr: table_at,
        ..Default::default()
    };
    smbios_ep.checksum = compute_checksum(&smbios_ep);
    Smbios {
        entry: smbios_ep.as_slice().try_into().unwrap(),
        table,
    }
}

#[cfg(test)]
mod tests {
    use super::super::{ACPI_NVS, acpi};
    use super::*;
    use natemu_abi::execution as exec;

    const AT: u64 = 0x60_8000;

    fn input(vcpus: usize, ram: u64) -> SmbiosInput {
        SmbiosInput {
            vcpus,
            cpuid_1: (0x0009_06a3, 0x178b_fbff),
            layout: Layout {
                low_end: ram.min(exec::LOW_RAM_END),
                high: ram.saturating_sub(exec::LOW_RAM_END),
                pci: false,
            },
            version: "0.1.0",
        }
    }
    fn u16_at(b: &[u8], o: usize) -> u16 {
        u16::from_le_bytes(b[o..o + 2].try_into().unwrap())
    }
    fn u32_at(b: &[u8], o: usize) -> u32 {
        u32::from_le_bytes(b[o..o + 4].try_into().unwrap())
    }
    fn u64_at(b: &[u8], o: usize) -> u64 {
        u64::from_le_bytes(b[o..o + 8].try_into().unwrap())
    }

    /// One structure: its type, formatted section and strings.
    struct Structure {
        kind: u8,
        formatted: Vec<u8>,
        strings: Vec<String>,
    }
    impl Structure {
        /// String `k` of the set (1 first; 0 is none).
        fn string(&self, k: u8) -> Option<&str> {
            k.checked_sub(1).and_then(|k| self.strings.get(k as usize)).map(String::as_str)
        }
    }
    /// The table's structures in order, each checked well formed: a formatted section of at least 4 bytes inside the table,
    /// a string set of nonempty NUL-terminated strings ended by one more NUL (an empty set by two), and nothing after type
    /// 127.
    fn parse(table: &[u8]) -> Vec<Structure> {
        let mut out = Vec::new();
        let mut o = 0;
        while o < table.len() {
            assert!(out.last().is_none_or(|s: &Structure| s.kind != END_OF_TABLE), "bytes after type 127 at {o:#x}");
            let len = table[o + 1] as usize;
            assert!(len >= 4 && o + len <= table.len(), "structure at {o:#x}: length {len:#x}");
            let formatted = table[o..o + len].to_vec();
            let mut p = o + len;
            let mut strings = Vec::new();
            if table[p] == 0 {
                assert_eq!(table[p + 1], 0, "empty string set at {p:#x}: a double NUL");
                p += 2;
            } else {
                while table[p] != 0 {
                    let end = p + table[p..].iter().position(|&b| b == 0).expect("a NUL-terminated string");
                    strings.push(String::from_utf8(table[p..end].to_vec()).unwrap());
                    p = end + 1;
                }
                p += 1;
            }
            out.push(Structure { kind: formatted[0], formatted, strings });
            o = p;
        }
        assert_eq!(o, table.len());
        out
    }

    #[test]
    fn entry_point_checksums_to_zero() {
        for vcpus in [1, 2, 64] {
            let s = build(&input(vcpus, 2 << 30), AT);
            assert_eq!(s.entry.iter().fold(0u8, |a, &b| a.wrapping_add(b)), 0);
            assert_ne!(s.entry[5], 0, "a checksum byte that does something");
        }
    }

    #[test]
    fn entry_point_names_table() {
        let s = build(&input(2, 2 << 30), AT);
        let e = &s.entry;
        assert_eq!(&e[0..5], b"_SM3_");
        assert_eq!(e[6], 0x18, "entry point length");
        assert_eq!((e[7], e[8], e[9]), (3, 0, 0), "SMBIOS 3.0.0");
        assert_eq!(e[10], 1, "entry point revision: SMBIOS 3.0");
        assert_eq!(e[11], 0);
        assert_eq!(u32_at(e, 12) as usize, s.table.len(), "structure table maximum size");
        assert_eq!(u64_at(e, 16), AT, "structure table address");
        assert_eq!(s.table_at(), AT);
    }

    #[test]
    fn structures_are_well_formed() {
        let s = build(&input(4, (4 << 30) + (2 << 20)), AT);
        let all = parse(&s.table);
        let handles: std::collections::BTreeSet<u16> = all.iter().map(|x| u16_at(&x.formatted, 2)).collect();
        assert_eq!(handles.len(), all.len(), "handles unique");
        assert!(handles.iter().all(|&h| h < 0xFEFF), "no reserved handle");
        assert_eq!(all.last().unwrap().kind, END_OF_TABLE, "type 127 last");
        assert_eq!(all.iter().filter(|x| x.kind == END_OF_TABLE).count(), 1);
        let kinds: Vec<u8> = all.iter().map(|x| x.kind).collect();
        assert_eq!(kinds, [0, 1, 3, 4, 4, 4, 4, 16, 17, 19, 19, 32, 127]);
        // Every string index in a formatted section names a string of its set.
        let index = |x: &Structure, offsets: &[usize]| offsets.iter().map(|&o| x.formatted[o]).collect::<Vec<u8>>();
        let t0 = &all[0];
        assert_eq!(index(t0, &[4, 5, 8]), [1, 2, 3]);
        assert_eq!(t0.strings, ["natemu", "natemu 0.1.0", RELEASE_DATE]);
        assert_eq!(u64_at(&t0.formatted, 0x0a), 0x08, "characteristics: not supported");
        assert_eq!(t0.formatted[0x13], 1 << 4, "a virtual machine");
        let t1 = &all[1];
        assert_eq!(index(t1, &[4, 5, 6, 7, 0x19, 0x1a]), [1, 2, 3, 0, 0, 4]);
        assert_eq!(t1.strings, ["natemu", "natemu x86-64", "0.1.0", "natemu"]);
        assert_eq!(&t1.formatted[8..24], &[0; 16], "UUID not set");
        assert_eq!(t1.formatted[0x18], 6, "wake-up: power switch");
        let t3 = &all[2];
        assert_eq!((t3.string(t3.formatted[4]), t3.formatted[5]), (Some("natemu"), 0x01));
        assert_eq!(&t3.formatted[9..12], &[3, 3, 3], "states: safe");
        for x in &all {
            if matches!(x.kind, 16 | 19 | 32 | 127) {
                assert!(x.strings.is_empty(), "type {} has no strings", x.kind);
            }
        }
        // Types 16, 17 and 19 refer to the one array.
        let array = u16_at(&all[7].formatted, 2);
        assert_eq!(u16_at(&all[8].formatted, 4), array);
        assert_eq!((u16_at(&all[9].formatted, 0x0c), u16_at(&all[10].formatted, 0x0c)), (array, array));
    }

    #[test]
    fn one_processor_per_vcpu() {
        for vcpus in [1, 2, 3, 64] {
            let s = build(&input(vcpus, 2 << 30), AT);
            let cpus: Vec<Structure> = parse(&s.table).into_iter().filter(|x| x.kind == PROCESSOR_INFORMATION).collect();
            assert_eq!(cpus.len(), vcpus);
            for (n, c) in cpus.iter().enumerate() {
                let f = &c.formatted;
                assert_eq!(f[1], 0x30, "the 3.0 length");
                assert_eq!(c.string(f[4]), Some(format!("CPU {n}").as_str()), "socket");
                assert_eq!((f[5], f[6]), (0x03, 0x01), "central, family Other");
                assert_eq!(c.string(f[7]), Some("natemu"));
                assert_eq!((u32_at(f, 8), u32_at(f, 12)), (0x0009_06a3, 0x178b_fbff), "CPUID.1 EAX, EDX");
                assert_eq!((u16_at(f, 0x14), u16_at(f, 0x16)), (2000, 2000), "max and current speed");
                assert_eq!(f[0x18], 0x41, "populated, enabled");
                assert_eq!((f[0x23], f[0x24], f[0x25]), (1, 1, 1), "one core, one thread");
                assert_eq!((u16_at(f, 0x2a), u16_at(f, 0x2c), u16_at(f, 0x2e)), (1, 1, 1));
                assert_eq!(u16_at(f, 0x28), 0x01, "family 2");
            }
        }
    }

    #[test]
    fn memory_matches_layout() {
        const GIB: u64 = 1 << 30;
        for ram in [32 << 20, 2 * GIB, 3 * GIB, 3 * GIB + (2 << 20), 16 * GIB, 40 * GIB, exec::MAX_GUEST_RAM_BYTES as u64] {
            let i = input(1, ram);
            let all = parse(&build(&i, AT).table);
            let array: Vec<&Structure> = all.iter().filter(|x| x.kind == PHYSICAL_MEMORY_ARRAY).collect();
            assert_eq!(array.len(), 1);
            let a = &array[0].formatted;
            assert_eq!((a[4], a[5], a[6]), (0x01, 0x03, 0x06), "other, system memory, multi-bit ECC");
            let capacity = match u32_at(a, 7) {
                0x8000_0000 => u64_at(a, 0x0f),
                kib => u64::from(kib) * 1024,
            };
            assert_eq!(capacity, ram, "{ram:#x}: the array's capacity");
            let devices: Vec<&Structure> = all.iter().filter(|x| x.kind == MEMORY_DEVICE).collect();
            assert_eq!(u16_at(a, 0x0d) as usize, devices.len());
            assert_eq!(devices.len() as u64, ram.div_ceil(16 * GIB));
            let mut sum = 0;
            for (n, d) in devices.iter().enumerate() {
                let f = &d.formatted;
                let mib = match u16_at(f, 0x0c) {
                    0x7fff => u64::from(u32_at(f, 0x1c)),
                    s => {
                        assert_eq!(s & 0x8000, 0, "MiB granularity");
                        u64::from(s)
                    }
                };
                assert!(mib > 0 && mib <= 16 * 1024);
                sum += mib << 20;
                assert_eq!((f[0x0e], f[0x12]), (0x09, 0x07), "DIMM, RAM");
                assert_eq!(d.string(f[0x10]), Some(format!("DIMM {n}").as_str()));
            }
            assert_eq!(sum, ram, "{ram:#x}: type 17 sizes sum to the RAM");
            let ranges: Vec<(u64, u64)> = all
                .iter()
                .filter(|x| x.kind == MEMORY_ARRAY_MAPPED_ADDRESS)
                .map(|x| {
                    let f = &x.formatted;
                    match (u32_at(f, 4), u32_at(f, 8)) {
                        (0xffff_ffff, 0xffff_ffff) => (u64_at(f, 0x0f), u64_at(f, 0x17) + 1),
                        (s, e) => (u64::from(s) * 1024, (u64::from(e) + 1) * 1024),
                    }
                })
                .collect();
            let mut want = vec![(0, i.layout.low_end)];
            if i.layout.high > 0 {
                want.push((4 * GIB, 4 * GIB + i.layout.high));
            }
            assert_eq!(ranges, want, "{ram:#x}: type 19 ranges are the layout's RAM extents");
            // Those are the boot map's RAM extents' span: the e820 RAM entries lie inside them.
            if let Ok(map) = i.layout.e820() {
                for e in map.iter().filter(|e| e.kind == super::super::E820_RAM) {
                    assert!(want.iter().any(|&(s, x)| s <= e.addr && e.addr + e.size <= x), "{e:?}");
                }
            }
        }
    }

    /// 64 vCPUs (`MAX_VCPUS`) and the largest RAM: the table still fits the ACPI NVS range after 64 processors' ACPI tables
    /// (with the wakeup mailbox), where the loader places it.
    #[test]
    fn table_fits_for_64_vcpus() {
        assert_eq!(exec::cache::MAX_VCPUS, 64);
        let tables = acpi::build_tables(64, ACPI_NVS.0, &[], &[(1, 11), (2, 10)], true, acpi::Board { pvpanic: true, ps2: true });
        let at = tables.end().next_multiple_of(16);
        let s = build(&input(64, exec::MAX_GUEST_RAM_BYTES as u64), at);
        assert!(at >= ACPI_NVS.0 && at + s.table.len() as u64 <= ACPI_NVS.1, "{at:#x} + {:#x}", s.table.len());
        assert_eq!(parse(&s.table).iter().filter(|x| x.kind == PROCESSOR_INFORMATION).count(), 64);
        assert_eq!(s.table_at(), at);
    }
}
