//! The PCI test function (`[debug] pci_testdev`, a test option): QEMU's `pci-testdev`, as its specification (QEMU's
//! `docs/specs/pci-testdev.rst`) describes it, with QEMU's device (`hw/misc/pci-testdev.c`) read as the behaviour
//! reference for the test table and the BAR sizes (no code copied). The launch puts two such functions in one slot
//! (functions 0 and 1); guests' PCI tests (kvm-unit-tests' `pci-testdev`) drive them.
//!
//! Identity: vendor 1B36H (Red Hat), device 0005H, revision 0, class FFH subclass 0, header type 0, subsystem
//! 1AF4H:1100H (what QEMU gives a device that names none), no interrupt pin, no capabilities.
//!
//! BARs: BAR0 4 KiB of 32-bit memory and BAR1 256 bytes of I/O (QEMU's 2 x 2048 and 2 x 128 bytes), both
//! non-prefetchable. Function 1 also has BAR2-3: 64 KiB of 64-bit prefetchable memory (QEMU's optional `membar`
//! BAR2, which has no backing). Natemu defines its contents: the aligned dword at offset d reads d (32 bits,
//! little-endian), so a read of any width at any offset says where it landed; writes to it are dropped.
//!
//! BAR0 and BAR1 each begin with the header of the selected test, its fields little-endian:
//!
//! | Offset | Bytes | Field |
//! | --- | --- | --- |
//! | 0 | 1 | test: a write of N selects test N of this BAR (N < 3; any other value selects none); reads the test's index |
//! | 1 | 1 | width of the test's write: 1 (a byte) for every test |
//! | 2 | 2 | zero |
//! | 4 | 4 | offset of the test's write in its BAR |
//! | 8 | 4 | the value it must write: FAH; CEH for a wildcard test, which takes any value and width |
//! | 12 | 4 | count of the writes that matched since the test was selected |
//! | 16 | .. | the test's name, NUL-terminated |
//!
//! The tests (index, name, offset of the write): on BAR0 0 `mmio-no-eventfd` 800H, 1 `mmio-wildcard-eventfd` 801H, 2
//! `mmio-datamatch-eventfd` 802H; on BAR1 3 `portio-no-eventfd` 83H, 4 `portio-wildcard-eventfd` 84H, 5
//! `portio-datamatch-eventfd` 85H (the second half of the BAR, at its index). Selecting a test zeroes its count. A
//! write at the offset counts when it is one byte of the value (or any write, for a wildcard test). Natemu has no
//! eventfds: every write reaches the function, so the count is the number of writes made (on QEMU an eventfd KVM
//! catches leaves it 0; the specification allows both and kvm-unit-tests accepts both).
//!
//! As QEMU's device: one selection per function, shared by both BARs (a test selected on BAR1 is read on BAR0 too, and
//! its write counts on either BAR); with no test selected everything reads 0; a function starts with none. Bytes past
//! the name read 0. Deviation: a read returns the bytes of the fields it covers, as the specification's little-endian
//! fields read; QEMU's device returns only the byte at the address, sign-extended, and 0 for a read reaching the name's
//! last byte.
use super::configuration::{
    PciBarConfiguration, PciBarPrefetchable, PciClassCode, PciConfiguration, PciHeaderType,
    PciSubclass,
};
use super::{BARS, Bar, BarKind, Bars, Function};
use crate::devices::Access;
use std::sync::{Arc, Mutex};

const VENDOR: u16 = 0x1b36;
const DEVICE: u16 = 0x0005;
/// QEMU's subsystem IDs for a device that sets none (Red Hat / Qumranet, QEMU).
const SUBSYSTEM: (u16, u16) = (0x1af4, 0x1100);
/// BAR0 (memory), BAR1 (I/O) and function 1's BAR2-3 (64-bit memory).
const MEMORY: u64 = 0x1000;
const IO: u64 = 0x100;
const OFFSETS: u64 = 0x1_0000;
/// The tests of each BAR, in index order (BAR0's are 0-2, BAR1's 3-5).
const TESTS: [&str; 3] = ["no-eventfd", "wildcard-eventfd", "datamatch-eventfd"];
const WILDCARD: u8 = 1;
/// The value a data-match test's write must carry, and what a wildcard test's header shows instead.
const DATAMATCH: u8 = 0xfa;
const NOMATCH: u8 = 0xce;

struct Subclass(u8);
impl PciSubclass for Subclass {
    fn get_register_value(&self) -> u8 {
        self.0
    }
}

/// The selected test (an index 0-5) and the count of its matching writes.
#[derive(Default)]
struct Selected {
    test: Option<u8>,
    count: u32,
}

/// One test function: function 0 or 1 of its slot (only function 1 has BAR2-3).
pub struct TestDev {
    config: Mutex<PciConfiguration>,
    requests: Vec<(u8, BarKind, u64)>,
    bars: Bars,
    selected: Mutex<Selected>,
}

fn lock<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

impl TestDev {
    /// Function `function` of the test slot, its BARs at 0 until the bus places them.
    pub fn new(function: u8) -> Arc<Self> {
        let mut config = PciConfiguration::new(
            VENDOR,
            DEVICE,
            0,
            PciClassCode::Other,
            &Subclass(0),
            None,
            PciHeaderType::Device,
            SUBSYSTEM.0,
            SUBSYSTEM.1,
            None,
        );
        let mut requests = vec![(0, BarKind::Mem32, MEMORY), (1, BarKind::Io, IO)];
        if function == 1 {
            requests.push((2, BarKind::Mem64, OFFSETS));
        }
        for &(index, kind, len) in &requests {
            let prefetch = if kind == BarKind::Mem64 {
                PciBarPrefetchable::Prefetchable
            } else {
                PciBarPrefetchable::NotPrefetchable
            };
            let bar = PciBarConfiguration::new(index.into(), len, kind.into(), prefetch);
            config.add_pci_bar(&bar).expect("test function BAR");
        }
        let bars = Bars::new(&requests);
        bars.publish(&config);
        Arc::new(TestDev {
            config: Mutex::new(config),
            requests,
            bars,
            selected: Mutex::new(Selected::default()),
        })
    }
}

/// Test `t`'s header: the 16 bytes of fields, then its name and a NUL.
fn header(t: u8, count: u32) -> Vec<u8> {
    let (bar, half) = if t < 3 {
        ("mmio", MEMORY / 2)
    } else {
        ("portio", IO / 2)
    };
    let data = if t % 3 == WILDCARD { NOMATCH } else { DATAMATCH };
    let mut h = vec![t, 1, 0, 0];
    h.extend((half as u32 + u32::from(t)).to_le_bytes());
    h.extend(u32::from(data).to_le_bytes());
    h.extend(count.to_le_bytes());
    h.extend(format!("{bar}-{}", TESTS[usize::from(t % 3)]).bytes());
    h.push(0);
    h
}

/// `size` bytes at `offset` of BAR2-3: each aligned dword holds its own offset.
fn offsets(offset: u64, size: u64) -> u64 {
    (0..size).fold(0, |v, k| {
        let at = offset + k;
        let dword = (at & !3) as u32;
        v | u64::from(dword.to_le_bytes()[(at & 3) as usize]) << (8 * k)
    })
}

impl Function for TestDev {
    fn config_read(&self, reg_idx: usize) -> u32 {
        lock(&self.config).read_reg(reg_idx)
    }
    fn config_write(&self, reg_idx: usize, offset: u64, data: &[u8]) {
        let mut config = lock(&self.config);
        let _ = config.write_config_register(reg_idx, offset, data);
        self.bars.publish(&config);
    }
    fn bars(&self) -> [Option<Bar>; BARS] {
        self.bars.get()
    }
    fn bar_requests(&self) -> Vec<(u8, BarKind, u64)> {
        self.requests.clone()
    }
    fn assign(&self, index: u8, base: u64) {
        self.bars.assign(&mut lock(&self.config), index, base);
    }
    fn bar_access(&self, _: usize, bar: u8, offset: u64, size: u64, value: Option<u64>) -> Access {
        if bar == 2 {
            return Access::Completed(value.map_or_else(|| offsets(offset, size), |_| 0));
        }
        let mut s = lock(&self.selected);
        match value {
            None => {
                let h = s.test.map(|t| header(t, s.count)).unwrap_or_default();
                let byte = |at: u64| usize::try_from(at).ok().and_then(|at| h.get(at)).copied();
                Access::Completed((0..size).fold(0, |v, k| {
                    v | u64::from(byte(offset + k).unwrap_or(0)) << (8 * k)
                }))
            }
            Some(v) if offset == 0 => {
                // BAR0's tests are 0-2, BAR1's 3-5.
                *s = Selected {
                    test: (v < TESTS.len() as u64).then(|| bar * 3 + v as u8),
                    count: 0,
                };
                Access::Completed(0)
            }
            Some(v) => {
                if let Some(t) = s.test {
                    let h = header(t, 0);
                    let at = u32::from_le_bytes(h[4..8].try_into().unwrap());
                    let matches = t % 3 == WILDCARD || size == 1 && v & 0xff == u64::from(h[8]);
                    if offset == u64::from(at) && matches {
                        s.count = s.count.wrapping_add(1);
                    }
                }
                Access::Completed(0)
            }
        }
    }
    fn bar_poll(&self, _: usize, _: u8, _: u64) -> Access {
        Access::Refused
    }
    fn quiesce(&self) -> crate::Result<bool> {
        Ok(true)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::devices::Devices;
    use crate::pci::Bus;

    /// The test slot of the launch's choice in these tests.
    const SLOT: u8 = 4;

    /// The two functions installed at `SLOT`, decoding memory and I/O (the guest's Command register write).
    fn installed() -> Devices {
        let mut bus = Bus::new();
        bus.add(SLOT, 0, TestDev::new(0)).unwrap();
        bus.add(SLOT, 1, TestDev::new(1)).unwrap();
        let mut d = Devices::new(1 << 30);
        bus.install(&mut d).unwrap();
        for f in 0..2 {
            cfg_write(&d, f, 1, 0b11);
        }
        d
    }
    fn address(function: u8, reg: usize) -> u64 {
        1 << 31 | u64::from(SLOT) << 11 | u64::from(function) << 8 | (reg as u64) << 2
    }
    fn cfg_read(d: &Devices, function: u8, reg: usize) -> u32 {
        d.port(0, 0xcf8, 4, Some(address(function, reg))).unwrap();
        d.port(0, 0xcfc, 4, None).unwrap() as u32
    }
    fn cfg_write(d: &Devices, function: u8, reg: usize, value: u32) {
        d.port(0, 0xcf8, 4, Some(address(function, reg))).unwrap();
        d.port(0, 0xcfc, 4, Some(value.into())).unwrap();
    }
    /// One BAR of one function as the guest reaches it: memory through the registry's window, I/O through the ports.
    struct Reach<'a> {
        d: &'a Devices,
        io: bool,
        base: u64,
    }
    impl Reach<'_> {
        fn of(d: &Devices, function: u8, bar: usize) -> Reach<'_> {
            let reg = cfg_read(d, function, 4 + bar);
            let io = reg & 1 != 0;
            let base = u64::from(reg & if io { !3 } else { !0xf });
            Reach { d, io, base }
        }
        fn access(&self, offset: u64, size: u64, value: Option<u64>) -> u64 {
            if self.io {
                return self.d.port(0, (self.base + offset) as u16, size, value).unwrap();
            }
            let (dev, off) = self.d.find(self.base + offset).unwrap();
            let direct = dev.lock().unwrap().direct().unwrap();
            match direct.access(0, off, size, value) {
                Access::Completed(v) => v,
                other => panic!("{other:?}"),
            }
        }
        fn read(&self, offset: u64, size: u64) -> u64 {
            self.access(offset, size, None)
        }
        fn write(&self, offset: u64, size: u64, value: u64) {
            self.access(offset, size, Some(value));
        }
    }

    #[test]
    fn identity_and_bars_as_a_guest_reads_them() {
        let d = installed();
        for f in 0..2 {
            assert_eq!(cfg_read(&d, f, 0), 0x0005_1b36, "vendor, device");
            assert_eq!(cfg_read(&d, f, 2), 0xff00_0000, "class FFH, subclass 0, revision 0");
            assert_eq!(cfg_read(&d, f, 11), 0x1100_1af4, "subsystem");
            assert_eq!(cfg_read(&d, f, 15) >> 8 & 0xff, 0, "no interrupt pin");
            // BAR0 32-bit memory, BAR1 I/O, placed in their windows
            let (b0, b1) = (cfg_read(&d, f, 4), cfg_read(&d, f, 5));
            assert_eq!(b0 & 0xf, 0);
            assert!((0xe000_0000..0xf000_0000).contains(&b0) && b0 % 0x1000 == 0, "{b0:#x}");
            assert_eq!(b1 & 3, 1);
            assert!((0xc000..0x1_0000).contains(&b1) && (b1 & !3) % 0x100 == 0, "{b1:#x}");
        }
        assert_eq!(cfg_read(&d, 0, 3) >> 16 & 0xff, 0x80, "function 0 says multi-function");
        assert_eq!(cfg_read(&d, 1, 3) >> 16 & 0xff, 0x00);
        // only function 1 has BAR2-3: 64-bit prefetchable memory below 4 GiB
        assert_eq!((cfg_read(&d, 0, 6), cfg_read(&d, 0, 7)), (0, 0));
        let b2 = cfg_read(&d, 1, 6);
        assert_eq!((b2 & 0xf, cfg_read(&d, 1, 7)), (0xc, 0));
        assert!((0xe000_0000..0xf000_0000).contains(&b2) && (b2 & !0xf) % 0x1_0000 == 0, "{b2:#x}");
        // sizing reads back the lengths: 4 KiB, 256 ports, 64 KiB
        for (f, reg, size) in [(0, 4, 0xffff_f000u32), (1, 5, 0xffff_ff01), (1, 6, 0xffff_000c)] {
            let keep = cfg_read(&d, f, reg);
            cfg_write(&d, f, reg, !0);
            assert_eq!(cfg_read(&d, f, reg) | if reg == 5 { 0xffff_0000 } else { 0 }, size);
            cfg_write(&d, f, reg, keep);
        }
    }

    #[test]
    fn header_and_test_table_on_both_bars_of_both_functions() {
        let d = installed();
        for f in 0..2u8 {
            for bar in 0..2u8 {
                let r = Reach::of(&d, f, bar.into());
                assert_eq!(r.io, bar == 1);
                // nothing selected: everything reads 0
                assert_eq!((r.read(0, 4), r.read(12, 4)), (0, 0), "{f}.{bar} unselected");
                let half = if bar == 0 { 0x800 } else { 0x80 };
                for n in 0..3u8 {
                    let t = bar * 3 + n;
                    r.write(0, 1, n.into());
                    let name = format!(
                        "{}-{}",
                        ["mmio", "portio"][usize::from(bar)],
                        ["no-eventfd", "wildcard-eventfd", "datamatch-eventfd"][usize::from(n)]
                    );
                    let data = if n == 1 { 0xce } else { 0xfa };
                    let why = format!("{f}.{bar} test {t} {name}");
                    assert_eq!(r.read(0, 1), u64::from(t), "{why}: test");
                    assert_eq!(r.read(1, 1), 1, "{why}: width");
                    assert_eq!(r.read(0, 4), u64::from(t) | 1 << 8, "{why}: test, width, padding");
                    assert_eq!(r.read(4, 4), half + u64::from(t), "{why}: offset");
                    assert_eq!(r.read(8, 4), data, "{why}: data");
                    assert_eq!(r.read(12, 4), 0, "{why}: count after selection");
                    let text: Vec<u8> = (0..=name.len() as u64).map(|k| r.read(16 + k, 1) as u8).collect();
                    assert_eq!(text, [name.as_bytes(), &[0]].concat(), "{why}: name");
                    assert_eq!(r.read(16 + name.len() as u64 + 1, 4), 0, "{why}: past the name");
                    if bar == 0 {
                        assert_eq!(r.read(4, 8), data << 32 | (half + u64::from(t)), "{why}: qword");
                    }
                    // the test's write: 16 of them count
                    let at = half + u64::from(t);
                    for _ in 0..16 {
                        r.write(at, 1, data);
                    }
                    assert_eq!(r.read(12, 4), 16, "{why}: count");
                    // other offsets never count; a data-match test refuses another value or width
                    r.write(at + 1, 1, data);
                    r.write(4, 4, at);
                    r.write(at, 1, 0x11);
                    r.write(at, 2, data);
                    let more = if n == 1 { 2 } else { 0 };
                    assert_eq!(r.read(12, 4), 16 + more, "{why}: mismatched writes");
                    assert_eq!(r.read(8, 4), data, "{why}: header writes are dropped");
                }
                // an index past the table selects none
                r.write(0, 1, 3);
                assert_eq!((r.read(0, 4), r.read(4, 4)), (0, 0), "{f}.{bar} none");
            }
            // one selection per function: BAR1's test reads on BAR0 and its write counts there too
            let (m, p) = (Reach::of(&d, f, 0), Reach::of(&d, f, 1));
            p.write(0, 4, 2);
            assert_eq!(m.read(0, 1), 5);
            m.write(0x85, 1, 0xfa);
            assert_eq!(p.read(12, 4), 1);
            // reselecting zeroes the count
            p.write(0, 1, 2);
            assert_eq!(m.read(12, 4), 0);
        }
        // the two functions select independently
        let (a, b) = (Reach::of(&d, 0, 0), Reach::of(&d, 1, 0));
        a.write(0, 1, 0);
        b.write(0, 1, 2);
        assert_eq!((a.read(0, 1), b.read(0, 1)), (0, 2));
    }

    #[test]
    fn function_1_bar2_reads_its_offsets() {
        let d = installed();
        let r = Reach::of(&d, 1, 2);
        assert!(!r.io);
        for at in [0u64, 4, 0x800, 0x1234 & !3, 0xfffc] {
            assert_eq!(r.read(at, 4), at, "dword at {at:#x}");
        }
        assert_eq!(r.read(0x10, 8), 0x14 << 32 | 0x10, "qword");
        assert_eq!(r.read(0x1236, 2), 0x0000, "the upper half of dword 1234H");
        assert_eq!(r.read(0x1235, 1), 0x12, "byte 1 of dword 1234H");
        assert_eq!(r.read(0xabce, 2), 0x0000);
        assert_eq!(r.read(0xabcd, 1), 0xab);
        assert_eq!(r.read(0xfffe, 2), 0x0000);
        assert_eq!(r.read(0xfffd, 1), 0xff);
        assert_eq!(r.read(0x6, 4), 0x0008_0000, "unaligned: bytes of dwords 4 and 8");
        r.write(0x40, 4, 0xdead_beef);
        assert_eq!(r.read(0x40, 4), 0x40, "writes are dropped");
        // function 0 has no BAR2
        assert!(TestDev::new(0).bar_requests().iter().all(|&(i, _, _)| i < 2));
    }
}
