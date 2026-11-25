//! The display (`[display] device = "bochs"`): a Bochs display PCI function (QEMU's `bochs-display`, [`bochs`]) whose
//! framebuffer is mapped device memory the guest writes natively, and whose registers (BAR2) publish the mode the
//! guest selects. The host reads the published [`Mode`] and the framebuffer to show or capture the picture.
//!
//! The published mode and its change counter live under one lock, so a reader takes both, or the mode alone, whole:
//! never one geometry's width with another's height. The counter counts changes of the published value: a mode
//! published, unpublished, or replaced by a different one. A re-evaluation that yields the same value does not count.
//!
//! A launch describes its display with a [`Spec`] (`[display]`: the framebuffer's size and the EDID's preferred mode),
//! checked by [`Spec::check`] before anything is built.
//!
//! **Pictures.** [`Display::snapshot`] copies the visible picture into an RGB [`Frame`] ([`Frame::png`] encodes it):
//! the mode is read once under the lock, so a snapshot never mixes two modes' geometry, and every row is read through
//! aligned word loads clipped to the framebuffer, so a snapshot never reads past it whatever the guest does meanwhile
//! (a row the guest rewrites during the copy may show part old, part new, as a scan-out would).
//! [`Display::framebuffer`] hands out the mode with the framebuffer's read-only descriptor, for a client to map.
//! [`Display::wait_change`] lets the control server follow the change counter without polling.
use crate::devices::{Access, mask};
use crate::pci::configuration::PciConfiguration;
use crate::pci::{BARS, Bar, BarKind, Bars, DeviceMemory, Function};
use std::os::fd::BorrowedFd;
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

pub mod bochs;
pub mod edid;

/// A picture's geometry in the framebuffer: `width` x `height` pixels of 32 bits, rows `stride` bytes apart from byte
/// `offset`; each pixel x8r8g8b8 in little-endian byte order (B, G, R, X in memory) or, when `big_endian`, in
/// big-endian byte order (X, R, G, B).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Mode {
    pub width: u32,
    pub height: u32,
    pub stride: u32,
    pub offset: u64,
    pub big_endian: bool,
}

/// The framebuffer's least and greatest size, in MiB (`[display] vram_mib`, a power of two between them). The greatest is
/// what launch-time BAR placement admits beside BAR2 and the other functions (`crate::pci::place`): memory BARs go
/// from the PCI window's start + 2 MiB upward, largest first, naturally aligned, so a 128 MiB BAR0 lands at E8000000H
/// and fills the window to its end, leaving no room for anything after it; 64 MiB lands at E4000000H with 128 MiB above
/// it free (`vram_max_is_what_placement_admits`). 64 MiB holds two frames of 2560 x 1600, the largest snapshot.
pub const VRAM_MIB_MIN: u64 = 4;
pub const VRAM_MIB_MAX: u64 = 64;

/// A launch's display (`[display]`): `vram_mib` MiB of framebuffer and the EDID's preferred mode, `width` x `height`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Spec {
    pub vram_mib: u64,
    pub width: u32,
    pub height: u32,
}
impl Default for Spec {
    /// 16 MiB (QEMU's `bochs-display` default) and 1024 x 768.
    fn default() -> Self {
        Spec { vram_mib: 16, width: 1024, height: 768 }
    }
}
impl Spec {
    /// `Err` naming the `display.` key when the framebuffer size is not a power of two from [`VRAM_MIB_MIN`] to
    /// [`VRAM_MIB_MAX`], when the EDID cannot describe the preferred mode ([`edid::fits`]), or when the framebuffer holds
    /// fewer than two frames of it (Linux's `drm_vram_helper_mode_valid` drops a mode larger than half of video memory).
    pub fn check(&self) -> Result<(), String> {
        let Spec { vram_mib: v, width: w, height: h } = *self;
        if !v.is_power_of_two() || !(VRAM_MIB_MIN..=VRAM_MIB_MAX).contains(&v) {
            return Err(format!(
                "display.vram_mib: {v}: a power of two from {VRAM_MIB_MIN} to {VRAM_MIB_MAX} (MiB)"
            ));
        }
        if !edid::fits(w, h) {
            return Err(format!(
                "display.width, display.height: {w} x {h}: not a mode the EDID can describe (each at least 64, at most \
                 4095, and a 60 Hz pixel clock of at most 655.25 MHz)"
            ));
        }
        let two = 2 * 4 * u64::from(w) * u64::from(h);
        if v << 20 < two {
            let need = two.div_ceil(1 << 20).next_power_of_two();
            return Err(format!(
                "display.vram_mib: {v} MiB holds fewer than two {w} x {h} frames; {w} x {h} needs at least {need}"
            ));
        }
        Ok(())
    }
}

/// The registers and what they publish, under one lock.
#[derive(Default)]
struct State {
    registers: bochs::Registers,
    mode: Option<Mode>,
    changes: u64,
}

/// The display function.
pub struct Display {
    config: Mutex<PciConfiguration>,
    requests: Vec<(u8, BarKind, u64)>,
    bars: Bars,
    vram: Arc<DeviceMemory>,
    edid: [u8; 128],
    state: Mutex<State>,
    /// Notified when the change counter moves.
    changed: Condvar,
}

fn lock<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

impl Display {
    /// The function with `vram` as its framebuffer (BAR0) and an EDID whose preferred mode is `preferred` (width,
    /// height); its BARs at 0 until the bus places them, no mode published.
    pub fn new(vram: Arc<DeviceMemory>, preferred: (u32, u32)) -> Arc<Self> {
        let requests = bochs::requests(vram.len());
        let config = bochs::configuration(&requests);
        let bars = Bars::new(&requests);
        bars.publish(&config);
        Arc::new(Display {
            config: Mutex::new(config),
            requests,
            bars,
            edid: edid::block(preferred.0, preferred.1),
            vram,
            state: Mutex::new(State::default()),
            changed: Condvar::new(),
        })
    }
    /// The framebuffer.
    pub fn vram(&self) -> &Arc<DeviceMemory> {
        &self.vram
    }
    /// The published mode, if any.
    pub fn mode(&self) -> Option<Mode> {
        lock(&self.state).mode
    }
    /// How many times the published mode has changed (module documentation).
    pub fn changes(&self) -> u64 {
        lock(&self.state).changes
    }
    /// The published mode and the change counter, taken together.
    pub fn published(&self) -> (Option<Mode>, u64) {
        let s = lock(&self.state);
        (s.mode, s.changes)
    }
}

/// The largest picture a snapshot takes (`snapshot` refuses a larger one rather than encode it).
pub const SNAPSHOT_MAX: (u32, u32) = (2560, 1600);

/// Why there is no snapshot.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SnapshotError {
    /// No mode is published: the guest has not enabled the display (or is changing its mode).
    NotEnabled,
    /// The picture is larger than [`SNAPSHOT_MAX`].
    TooLarge { width: u32, height: u32 },
}

/// A picture: `width` x `height` pixels, 3 bytes each (R, G, B), row after row from the top.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Frame {
    pub width: u32,
    pub height: u32,
    pub rgb: Vec<u8>,
}

impl Frame {
    /// The picture as a PNG: 8-bit RGB, default compression (the `png` crate's encoder).
    pub fn png(&self) -> Vec<u8> {
        let mut out = Vec::new();
        let mut encoder = png::Encoder::new(&mut out, self.width, self.height);
        encoder.set_color(png::ColorType::Rgb);
        encoder.set_depth(png::BitDepth::Eight);
        // Writing to a Vec fails only on a size the frame cannot have (its buffer holds exactly width x height pixels).
        let mut writer = encoder.write_header().expect("a PNG header for the frame's size");
        writer.write_image_data(&self.rgb).expect("the frame's pixels");
        writer.finish().expect("the PNG's end");
        out
    }
}

/// The picture `mode` shows in a framebuffer of `len` bytes that `read(offset, words)` reads (8-aligned word loads):
/// each row's covering aligned words, sliced to the row; a row, or part of one, past the framebuffer reads black.
pub fn frame(mode: &Mode, len: u64, mut read: impl FnMut(u64, &mut [u64])) -> Frame {
    let (w, h) = (mode.width as usize, mode.height as usize);
    let mut rgb = vec![0u8; w * h * 3];
    let mut words = vec![0u64; w / 2 + 2];
    let mut bytes = vec![0u8; 8 * words.len()];
    for y in 0..h {
        let start = mode.offset + y as u64 * u64::from(mode.stride);
        let end = (start + 4 * w as u64).min(len);
        if start >= end {
            continue;
        }
        let first = start & !7;
        let n = (end - first).div_ceil(8) as usize;
        read(first, &mut words[..n]);
        for (k, word) in words[..n].iter().enumerate() {
            bytes[8 * k..8 * k + 8].copy_from_slice(&word.to_le_bytes());
        }
        let row = &bytes[(start - first) as usize..(end - first) as usize];
        let out = &mut rgb[3 * w * y..3 * w * (y + 1)];
        for (px, rgb) in row.chunks_exact(4).zip(out.chunks_exact_mut(3)) {
            // Little-endian x8r8g8b8 is B, G, R, X in memory; big-endian is X, R, G, B.
            let (r, g, b) = if mode.big_endian { (px[1], px[2], px[3]) } else { (px[2], px[1], px[0]) };
            rgb.copy_from_slice(&[r, g, b]);
        }
    }
    Frame { width: mode.width, height: mode.height, rgb }
}

impl Display {
    /// The picture the guest shows now (module documentation), or why there is none.
    pub fn snapshot(&self) -> Result<Frame, SnapshotError> {
        let mode = self.mode().ok_or(SnapshotError::NotEnabled)?;
        if mode.width > SNAPSHOT_MAX.0 || mode.height > SNAPSHOT_MAX.1 {
            return Err(SnapshotError::TooLarge { width: mode.width, height: mode.height });
        }
        Ok(frame(&mode, self.vram.len(), |at, words| self.vram.read_words(at, words)))
    }
    /// The published mode and the framebuffer's read-only descriptor (`DeviceMemory::fd`), none while no mode is
    /// published.
    pub fn framebuffer(&self) -> Option<(Mode, BorrowedFd<'_>)> {
        self.mode().map(|m| (m, self.vram.fd()))
    }
    /// Waits until the change counter differs from `seen`, at most `timeout`; the counter then.
    pub fn wait_change(&self, seen: u64, timeout: Duration) -> u64 {
        let s = lock(&self.state);
        let (s, _) = self
            .changed
            .wait_timeout_while(s, timeout, |s| s.changes == seen)
            .unwrap_or_else(|e| e.into_inner());
        s.changes
    }
}

impl Function for Display {
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
    /// BAR0 maps the framebuffer.
    fn mapped(&self, bar: u8) -> Option<Arc<DeviceMemory>> {
        (bar == 0).then(|| self.vram.clone())
    }
    fn assign(&self, index: u8, base: u64) {
        self.bars.assign(&mut lock(&self.config), index, base);
    }
    fn bar_access(&self, _: usize, bar: u8, offset: u64, size: u64, value: Option<u64>) -> Access {
        match bar {
            2 => {
                let mut s = lock(&self.state);
                let Some(value) = value else {
                    return Access::Completed(s.registers.read(&self.edid, self.vram.len(), offset, size));
                };
                s.registers.write(offset, size, value);
                // Evaluated once per access, under the lock: a reader sees the mode before or after the whole
                // access, never between the registers of a wide one.
                let mode = s.registers.mode(self.vram.len());
                if mode != s.mode {
                    s.mode = mode;
                    s.changes += 1;
                    self.changed.notify_all();
                }
                Access::Completed(0)
            }
            // BAR0 is mapped (`mapped`): guest accesses run natively on `vram`, and one that still reaches the bus
            // reads all-ones and its write is dropped (`Function::mapped`'s rule).
            0 => Access::Completed(if value.is_none() { mask(size) } else { 0 }),
            _ => Access::Refused,
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
    use super::bochs::*;
    use super::*;

    const MIB: u64 = 1 << 20;

    fn display(vram_mib: u64) -> Arc<Display> {
        Display::new(DeviceMemory::new(vram_mib * MIB).unwrap(), (1024, 768))
    }
    fn completed(a: Access) -> u64 {
        match a {
            Access::Completed(v) => v,
            other => panic!("{other:?}"),
        }
    }
    /// BAR2 as the guest reaches it.
    fn read(d: &Display, offset: u64, size: u64) -> u64 {
        completed(d.bar_access(0, 2, offset, size, None))
    }
    fn write(d: &Display, offset: u64, size: u64, value: u64) {
        completed(d.bar_access(0, 2, offset, size, Some(value)));
    }
    fn reg(d: &Display, index: usize) -> u16 {
        read(d, DISPI + 2 * index as u64, 2) as u16
    }
    fn set(d: &Display, index: usize, value: u16) {
        write(d, DISPI + 2 * index as u64, 2, value.into());
    }
    /// A mode as Linux's `bochs` driver sets it: geometry, then BPP, then ENABLE.
    fn modeset(d: &Display, w: u16, h: u16) {
        set(d, XRES, w);
        set(d, YRES, h);
        set(d, VIRT_WIDTH, w);
        set(d, VIRT_HEIGHT, h);
        set(d, BPP, 32);
        set(d, ENABLE, ENABLED | 0x40);
    }
    fn mode(w: u32, h: u32) -> Mode {
        Mode {
            width: w,
            height: h,
            stride: w * 4,
            offset: 0,
            big_endian: false,
        }
    }

    #[test]
    fn identity_and_bars() {
        let d = display(16);
        assert_eq!(d.config_read(0), 0x1111_1234, "vendor, device");
        assert_eq!(d.config_read(2), 0x0380_0002, "class 03H, subclass 80H, revision 2");
        assert_eq!(d.config_read(3) >> 16 & 0xff, 0, "header type 0");
        assert_eq!(d.config_read(11), 0x1100_1af4, "subsystem");
        assert_eq!(d.config_read(15) >> 8 & 0xff, 0, "no interrupt pin");
        assert_eq!(
            d.bar_requests(),
            [(0, BarKind::Mem32, 16 * MIB), (2, BarKind::Mem32, 0x1000)],
            "BAR0 the framebuffer, BAR2 4 KiB of registers"
        );
        assert_eq!(d.bar_requests()[0].2, d.vram().len());
        assert!(d.mapped(0).is_some_and(|m| Arc::ptr_eq(&m, d.vram())), "BAR0 maps the framebuffer");
        assert!(d.mapped(2).is_none(), "BAR2 is emulated");
        // BAR0 through the bus: all-ones, the write dropped
        assert_eq!(completed(d.bar_access(0, 0, 16, 4, None)), 0xffff_ffff);
        d.bar_access(0, 0, 16, 8, Some(5));
        let mut w = [1u64];
        d.vram().read_words(16, &mut w);
        assert_eq!(w, [0], "the framebuffer unchanged");
        let other = DeviceMemory::new(16 * MIB).unwrap();
        assert!(!Arc::ptr_eq(d.vram(), &other));
        // sizing reads back the lengths and kinds: 16 MiB prefetchable, 4 KiB not; no BAR1, 3-5
        for (reg, size) in [(4, 0xff00_0008u32), (5, 0), (6, 0xffff_f000), (7, 0), (8, 0), (9, 0)] {
            d.config_write(reg, 0, &(!0u32).to_le_bytes());
            assert_eq!(d.config_read(reg), size, "BAR{}", reg - 4);
        }
        // the framebuffer is the one given
        let vram = DeviceMemory::new(4 * MIB).unwrap();
        let d = Display::new(vram.clone(), (1024, 768));
        assert!(Arc::ptr_eq(d.vram(), &vram));
        assert_eq!(d.bar_requests()[0], (0, BarKind::Mem32, 4 * MIB));
        assert_eq!(d.mode(), None, "nothing published at first");
    }

    #[test]
    fn id_reads_b0c5() {
        let d = display(16);
        assert_eq!(reg(&d, ID), 0xb0c5);
        for v in [0xb0c0, 0xb0c4, 0x1234] {
            set(&d, ID, v);
            assert_eq!(reg(&d, ID), 0xb0c5, "after writing {v:#x}");
        }
        assert_eq!(read(&d, DISPI, 1), 0xc5);
        assert_eq!(read(&d, DISPI + 1, 1), 0xb0);
    }

    #[test]
    fn registers_read_back_as_written() {
        let d = display(16);
        for i in [XRES, YRES, BPP, ENABLE, BANK, VIRT_WIDTH, VIRT_HEIGHT, X_OFFSET, Y_OFFSET] {
            let v = 0x1100 + i as u16 * 0x111;
            set(&d, i, v);
            assert_eq!(reg(&d, i), v, "register {i}");
        }
        // nothing is validated at write time: an odd depth, ENABLE's every bit
        set(&d, BPP, 24);
        set(&d, ENABLE, 0xffff);
        assert_eq!((reg(&d, BPP), reg(&d, ENABLE)), (24, 0xffff));
        // a 32-bit access is two registers, the lower index in the low half
        write(&d, DISPI + 2 * XRES as u64, 4, 0x0300_0400);
        assert_eq!((reg(&d, XRES), reg(&d, YRES)), (0x400, 0x300));
        assert_eq!(read(&d, DISPI + 2 * XRES as u64, 4), 0x0300_0400);
        // a byte write changes its byte only
        write(&d, DISPI + 2 * BANK as u64 + 1, 1, 0xab);
        assert_eq!(reg(&d, BANK), 0xab00 | (0x1100 + BANK as u16 * 0x111) & 0xff);
        assert_eq!(read(&d, DISPI + 2 * BANK as u64 + 1, 1), 0xab);
        // past index 10, and around the register blocks: reads 0, writes dropped
        for at in [0x516, 0x518, 0x5fe, 0x080, 0x400, 0x41e, 0x608, 0xffc] {
            write(&d, at, 2, 0x5555);
            assert_eq!(read(&d, at, 2), 0, "{at:#x}");
        }
        // the EDID block, read-only
        let edid = edid::block(1024, 768);
        let got: Vec<u8> = (0..128).map(|k| read(&d, EDID + k, 1) as u8).collect();
        assert_eq!(got, edid);
        assert_eq!(read(&d, 0, 8), u64::from_le_bytes(edid[..8].try_into().unwrap()));
        write(&d, 8, 4, 0);
        assert_eq!(read(&d, 8, 4), u64::from(u32::from_le_bytes(edid[8..12].try_into().unwrap())));
    }

    #[test]
    fn evaluation_rule() {
        let d = display(16);
        modeset(&d, 1024, 768);
        assert_eq!(d.mode(), Some(mode(1024, 768)));
        // BPP 24 (or 16, or 8) unpublishes
        for bpp in [24, 16, 8, 0] {
            set(&d, BPP, bpp);
            assert_eq!(d.mode(), None, "BPP {bpp}");
        }
        set(&d, BPP, 32);
        assert_eq!(d.mode(), Some(mode(1024, 768)));
        // width or height under 64
        set(&d, XRES, 63);
        set(&d, VIRT_WIDTH, 63);
        assert_eq!(d.mode(), None, "width 63");
        set(&d, XRES, 64);
        set(&d, VIRT_WIDTH, 64);
        assert_eq!(d.mode(), Some(mode(64, 768)), "width 64");
        set(&d, YRES, 63);
        assert_eq!(d.mode(), None, "height 63");
        modeset(&d, 1024, 768);
        // ENABLED clear
        set(&d, ENABLE, 0x40);
        assert_eq!(d.mode(), None, "not enabled");
        set(&d, ENABLE, ENABLED);
        assert_eq!(d.mode(), Some(mode(1024, 768)));
        // a mode past the framebuffer: 16 MiB is 4096 rows of 4096 bytes; the last 768 start at row 3328
        set(&d, Y_OFFSET, 3328);
        assert_eq!(d.mode().map(|m| m.offset), Some(3328 * 4096), "ends at the framebuffer's end");
        set(&d, Y_OFFSET, 3329);
        assert_eq!(d.mode(), None, "one row past the framebuffer");
        set(&d, Y_OFFSET, 0);
        set(&d, X_OFFSET, 1);
        set(&d, VIRT_HEIGHT, 4096);
        set(&d, YRES, 4096);
        assert_eq!(d.mode(), None, "one pixel past the framebuffer");
        set(&d, X_OFFSET, 0);
        assert_eq!(d.mode(), Some(mode(1024, 4096)), "the whole framebuffer");
        // 2048 x 2048 x 4 is 16 MiB; 2048 x 2049 is past it
        modeset(&d, 2048, 2048);
        assert_eq!(d.mode(), Some(mode(2048, 2048)));
        modeset(&d, 2048, 2049);
        assert_eq!(d.mode(), None, "2048 x 2049 in 16 MiB");
        // rows narrower than the picture are not a mode
        modeset(&d, 1024, 768);
        set(&d, VIRT_WIDTH, 1023);
        assert_eq!(d.mode(), None, "VIRT_WIDTH under XRES");
        // VIRT_WIDTH 0 means XRES
        set(&d, VIRT_WIDTH, 0);
        assert_eq!(d.mode(), Some(mode(1024, 768)), "VIRT_WIDTH 0");
        // 1366 x 768 and 1024 x 768 at 32 bits
        modeset(&d, 1366, 768);
        assert_eq!(d.mode(), Some(mode(1366, 768)));
        assert_eq!(d.mode().unwrap().stride, 5464);
        modeset(&d, 1024, 768);
        assert_eq!(d.mode(), Some(mode(1024, 768)));
        // ENABLE is never cleared by the device
        assert_eq!(reg(&d, ENABLE), ENABLED | 0x40);
    }

    #[test]
    fn registers_apply_in_any_order() {
        let writes = [(XRES, 1366), (YRES, 768), (BPP, 32), (ENABLE, ENABLED)];
        // every order of the four writes ends in the same mode, and publishes only on the last
        let mut order = [0, 1, 2, 3];
        let mut seen = 0;
        loop {
            let d = display(16);
            for (n, &k) in order.iter().enumerate() {
                assert_eq!(d.mode(), None, "{order:?} before write {n}");
                set(&d, writes[k].0, writes[k].1);
            }
            assert_eq!(d.mode(), Some(mode(1366, 768)), "{order:?}");
            assert_eq!(d.changes(), 1, "{order:?}");
            seen += 1;
            // next permutation
            let Some(i) = (0..3).rev().find(|&i| order[i] < order[i + 1]) else { break };
            let j = (i + 1..4).rev().find(|&j| order[j] > order[i]).unwrap();
            order.swap(i, j);
            order[i + 1..].reverse();
        }
        assert_eq!(seen, 24);
    }

    #[test]
    fn flip_through_invalid_states() {
        let d = display(16);
        // a pattern in the framebuffer, which nothing the registers do may clear
        let pattern: Vec<u64> = (0..1024).map(|k| 0x0123_4567_89ab_cdef ^ k).collect();
        d.vram().write_words(0, &pattern);
        d.vram().write_words(15 * MIB, &pattern);
        modeset(&d, 1024, 768);
        let (first, n) = d.published();
        assert_eq!((first, n), (Some(mode(1024, 768)), 1));
        // the driver pans to the second frame: Y_OFFSET first (still in range), then a wider pitch it has not
        // yet matched with XRES passes through a geometry past the framebuffer
        set(&d, Y_OFFSET, 768);
        let second = Some(Mode { offset: 768 * 4096, ..mode(1024, 768) });
        assert_eq!(d.published(), (second, 2));
        set(&d, VIRT_WIDTH, 8192);
        assert_eq!(d.published(), (None, 3), "768 rows of 32 KiB from row 768: past 16 MiB");
        set(&d, VIRT_WIDTH, 1024);
        assert_eq!(d.published(), (second, 4), "valid again");
        // a write that leaves the mode as it is does not count
        set(&d, BANK, 3);
        set(&d, VIRT_HEIGHT, 1536);
        set(&d, Y_OFFSET, 768);
        assert_eq!(d.published(), (second, 4));
        // a modeset through 0 x 0 (XRES and YRES cleared first, as a driver resetting does)
        set(&d, XRES, 0);
        assert_eq!(d.published(), (None, 5));
        set(&d, YRES, 0);
        set(&d, Y_OFFSET, 0);
        set(&d, XRES, 800);
        set(&d, VIRT_WIDTH, 800);
        set(&d, YRES, 600);
        assert_eq!(d.published(), (Some(mode(800, 600)), 6));
        assert_eq!(reg(&d, ENABLE) & ENABLED, ENABLED, "ENABLE stays set throughout");
        // the framebuffer was never cleared
        let mut back = vec![0; 1024];
        d.vram().read_words(0, &mut back);
        assert_eq!(back, pattern);
        d.vram().read_words(15 * MIB, &mut back);
        assert_eq!(back, pattern);
    }

    #[test]
    fn x_offset_moves_the_base() {
        let d = display(16);
        modeset(&d, 1024, 768);
        set(&d, X_OFFSET, 16);
        assert_eq!(d.mode().map(|m| m.offset), Some(64), "16 pixels of 4 bytes");
        set(&d, Y_OFFSET, 10);
        assert_eq!(d.mode().map(|m| m.offset), Some(10 * 4096 + 64));
        set(&d, VIRT_WIDTH, 2048);
        assert_eq!(
            d.mode().map(|m| (m.stride, m.offset)),
            Some((8192, 10 * 8192 + 64)),
            "Y_OFFSET counts rows of VIRT_WIDTH"
        );
        set(&d, X_OFFSET, 1024);
        assert_eq!(d.mode().map(|m| m.offset), Some(10 * 8192 + 4096));
        set(&d, X_OFFSET, 0);
        assert_eq!(d.mode().map(|m| m.offset), Some(10 * 8192));
    }

    #[test]
    fn video_memory_reads_vram() {
        for mib in [4, 16, 64] {
            let d = display(mib);
            assert_eq!(u64::from(reg(&d, VIDEO_MEMORY_64K)), mib * 16, "{mib} MiB");
            set(&d, VIDEO_MEMORY_64K, 1);
            assert_eq!(u64::from(reg(&d, VIDEO_MEMORY_64K)), mib * 16, "{mib} MiB: read-only");
        }
    }

    #[test]
    fn endianness_register() {
        let d = display(16);
        assert_eq!(read(&d, EXTENDED, 4), 8, "the extended region's size");
        write(&d, EXTENDED, 4, 0x100);
        assert_eq!(read(&d, EXTENDED, 4), 8, "read-only");
        assert_eq!(read(&d, BYTE_ORDER, 4), 0x1e1e_1e1e, "little-endian at first");
        modeset(&d, 1024, 768);
        assert_eq!(d.published(), (Some(mode(1024, 768)), 1));
        write(&d, BYTE_ORDER, 4, 0xbebe_bebe);
        assert_eq!(read(&d, BYTE_ORDER, 4), 0xbebe_bebe);
        let big = Some(Mode { big_endian: true, ..mode(1024, 768) });
        assert_eq!(d.published(), (big, 2), "big-endian in the published mode");
        // other values, and partial writes, leave it
        for (at, size, v) in [(BYTE_ORDER, 4, 0x1234_5678), (BYTE_ORDER, 2, 0x1e1e), (BYTE_ORDER, 1, 0x1e)] {
            write(&d, at, size, v);
            assert_eq!(read(&d, BYTE_ORDER, 4), 0xbebe_bebe, "{v:#x} of {size} bytes");
        }
        assert_eq!(d.published(), (big, 2));
        write(&d, BYTE_ORDER, 4, 0x1e1e_1e1e);
        assert_eq!(read(&d, BYTE_ORDER, 4), 0x1e1e_1e1e);
        assert_eq!(d.published(), (Some(mode(1024, 768)), 3), "little-endian again");
        // an 8-byte access covers both extended registers
        assert_eq!(read(&d, EXTENDED, 8), 0x1e1e_1e1e << 32 | 8);
        write(&d, EXTENDED, 8, 0xbebe_bebe << 32 | 0x77);
        assert_eq!(read(&d, EXTENDED, 8), 0xbebe_bebe << 32 | 8);
        // the byte order is kept while no mode is published, and applies when one is
        set(&d, ENABLE, 0);
        assert_eq!(d.mode(), None);
        set(&d, ENABLE, ENABLED);
        assert_eq!(d.mode(), big);
    }

    #[test]
    fn mode_change_is_atomic_for_readers() {
        // A writer alternates two geometries, register by register, through an unpublished state (ENABLE cleared
        // while it changes them), so only the two whole modes are ever published; a reader taking modes meanwhile
        // must never see a mix (one's width with the other's height, stride or offset). Three accesses a geometry
        // (8-byte writes of four registers each), so publishes come often and a torn read has many chances.
        let d = display(16);
        let a = Mode { offset: 4096 * 4 + 64, ..mode(1024, 768) };
        let b = Mode { offset: 3 * 1600 * 4 + 32, ..mode(1600, 900) };
        let flips = 50_000;
        let reader = {
            let d = d.clone();
            std::thread::spawn(move || {
                let (mut seen_a, mut seen_b, mut mixed) = (0u64, 0u64, Vec::new());
                let mut last = 0;
                // A deadline, so a writer that stops early fails the test instead of hanging it.
                let deadline = std::time::Instant::now() + std::time::Duration::from_secs(60);
                loop {
                    let changes = d.changes();
                    assert!(changes >= last, "the counter never goes back");
                    last = changes;
                    match d.mode() {
                        Some(m) if m == a => seen_a += 1,
                        Some(m) if m == b => seen_b += 1,
                        Some(m) => mixed.push(m),
                        None => {}
                    }
                    if changes >= 4 * flips - 1 || std::time::Instant::now() > deadline {
                        return (seen_a, seen_b, mixed);
                    }
                }
            })
        };
        let geometry = |m: Mode, x_offset: u64, y_offset: u64| {
            // XRES, YRES, BPP 32 and ENABLE 0; then VIRT_WIDTH 0 (XRES), VIRT_HEIGHT 0 and the offsets; then ENABLE
            let (w, h) = (u64::from(m.width), u64::from(m.height));
            write(&d, DISPI + 2 * XRES as u64, 8, w | h << 16 | 32 << 32);
            write(&d, DISPI + 2 * VIRT_WIDTH as u64, 8, x_offset << 32 | y_offset << 48);
            set(&d, ENABLE, ENABLED);
        };
        for _ in 0..flips {
            geometry(a, 16, 4);
            geometry(b, 8, 3);
        }
        let (seen_a, seen_b, mixed) = reader.join().unwrap();
        // the first publish, then an unpublish and a publish per geometry after it
        assert_eq!(d.changes(), 4 * flips - 1);
        assert!(mixed.is_empty(), "{} mixed modes, first {:?}", mixed.len(), mixed.first());
        assert!(seen_a > 0 && seen_b > 0, "the reader saw both ({seen_a}, {seen_b})");
    }

    /// `[display]`'s rules: the framebuffer a power of two from 4 to 64 MiB, a preferred mode the EDID describes, two
    /// frames of it in the framebuffer; each refusal names its key.
    #[test]
    fn spec_check() {
        let spec = |vram_mib, width, height| Spec { vram_mib, width, height };
        assert_eq!(Spec::default(), spec(16, 1024, 768));
        let fine = [spec(16, 1024, 768), spec(4, 640, 480), spec(64, 2560, 1600), spec(32, 2560, 1600), spec(4, 64, 64)];
        for ok in [&fine[..], &[spec(64, 4095, 2048)]].concat() {
            assert_eq!(ok.check(), Ok(()), "{ok:?}");
        }
        for (bad, key, what) in [
            (spec(3, 640, 480), "display.vram_mib: 3:", "not a power of two"),
            (spec(2, 640, 480), "display.vram_mib: 2:", "below 4"),
            (spec(128, 1024, 768), "display.vram_mib: 128:", "above what placement admits"),
            (spec(0, 1024, 768), "display.vram_mib: 0:", "zero"),
            (spec(16, 63, 480), "display.width, display.height: 63 x 480", "too narrow"),
            (spec(16, 640, 4096), "display.width, display.height: 640 x 4096", "too tall"),
            (spec(64, 4095, 2497), "display.width, display.height: 4095 x 2497", "pixel clock"),
            (spec(16, 1920, 1200), "display.vram_mib: 16 MiB holds fewer than two 1920 x 1200 frames", "two frames"),
            (spec(4, 1024, 768), "display.vram_mib: 4 MiB", "two frames: 6 MiB"),
        ] {
            let e = bad.check().expect_err(what);
            assert!(e.starts_with(key), "{what}: {e}");
        }
        assert!(spec(16, 1920, 1200).check().unwrap_err().ends_with("needs at least 32"));
        // exactly two frames fit: 1024 x 1024 in 8 MiB
        assert_eq!(spec(8, 1024, 1024).check(), Ok(()));
        assert!(spec(8, 1024, 1025).check().is_err());
    }

    /// [`VRAM_MIB_MAX`] is what launch-time placement admits: at 64 MiB the display's BARs place beside every other
    /// function a launch can have (the virtio functions, the PCI test function, the mapped-memory test function); at
    /// 128 MiB BAR0 alone fills the window from E8000000H and the next BAR placed is refused (alone, its own BAR2).
    #[test]
    fn vram_max_is_what_placement_admits() {
        use crate::pci::place::{Request, place};
        let at = |slot: u8, function: u8, (bar, kind, len): (u8, BarKind, u64)| Request { slot, function, bar, kind, len };
        let others = |slot: u8| {
            let mut r: Vec<Request> = (1..slot - 2).map(|s| at(s, 0, (0, BarKind::Mem32, 0x8000))).collect();
            let testdev = slot - 2;
            r.push(at(testdev, 0, (0, BarKind::Mem32, 0x1000)));
            r.push(at(testdev, 0, (1, BarKind::Io, 0x100)));
            r.push(at(testdev, 1, (0, BarKind::Mem32, 0x1000)));
            r.push(at(testdev, 1, (1, BarKind::Io, 0x100)));
            r.push(at(testdev, 1, (2, BarKind::Mem64, 0x1_0000)));
            r.push(at(slot - 1, 0, (0, BarKind::Mem64, 2 * MIB)));
            r.push(at(slot - 1, 0, (2, BarKind::Mem32, 0x1000)));
            r
        };
        let with_display = |vram_mib: u64| {
            let mut r = others(31);
            r.extend(bochs::requests(vram_mib * MIB).into_iter().map(|q| at(31, 0, q)));
            place(&r)
        };
        let placed = with_display(VRAM_MIB_MAX).unwrap();
        let bar0 = placed.iter().find(|p| p.request.slot == 31 && p.request.bar == 0).unwrap();
        assert_eq!(bar0.base, 0xe400_0000);
        // Largest first: the 128 MiB BAR0 fills the window from E8000000H, so the next BAR placed (the 2 MiB 64-bit BAR0
        // of slot 30, the largest left) finds no room.
        let e = with_display(VRAM_MIB_MAX * 2).unwrap_err();
        assert!(e.starts_with("PCI 1e.0 BAR0 (Mem64, 0x200000 bytes): no room left in the memory window"), "{e}");
        let alone: Vec<_> = bochs::requests(VRAM_MIB_MAX * 2 * MIB).into_iter().map(|q| at(1, 0, q)).collect();
        assert!(place(&alone).unwrap_err().contains("PCI 01.0 BAR2"), "128 MiB leaves no room even for BAR2");
    }

    /// Writes 32-bit pixels at byte `at` (4-aligned) of the framebuffer through its word stores.
    fn poke(d: &Display, at: u64, pixels: &[u32]) {
        let first = at & !7;
        let n = (at + 4 * pixels.len() as u64 - first).div_ceil(8) as usize;
        let mut words = vec![0u64; n];
        d.vram().read_words(first, &mut words);
        let mut bytes: Vec<u8> = words.iter().flat_map(|w| w.to_le_bytes()).collect();
        for (i, p) in pixels.iter().enumerate() {
            let k = (at - first) as usize + 4 * i;
            bytes[k..k + 4].copy_from_slice(&p.to_le_bytes());
        }
        let words: Vec<u64> = bytes.chunks_exact(8).map(|c| u64::from_le_bytes(c.try_into().unwrap())).collect();
        d.vram().write_words(first, &words);
    }
    /// A pixel of the test pattern: a function of x and y in each channel.
    fn colour(x: u32, y: u32) -> [u8; 3] {
        [(x * 7 + y) as u8, (y * 5 + 3) as u8, (x ^ y.wrapping_mul(3)) as u8]
    }
    /// `colour` as little-endian x8r8g8b8 (the u32 0xXXRRGGBB, with junk in X).
    fn xrgb(x: u32, y: u32) -> u32 {
        let [r, g, b] = colour(x, y);
        0xa5 << 24 | u32::from(r) << 16 | u32::from(g) << 8 | u32::from(b)
    }
    fn expected(w: u32, h: u32) -> Vec<u8> {
        (0..h).flat_map(|y| (0..w).flat_map(move |x| colour(x, y))).collect()
    }

    /// Little-endian x8r8g8b8 (B, G, R, X in memory) becomes R, G, B; X is dropped.
    #[test]
    fn snapshot_converts_bgrx_to_rgb() {
        let d = display(16);
        let (w, h) = (64u32, 64u32);
        for y in 0..h {
            poke(&d, u64::from(y * w * 4), &(0..w).map(|x| xrgb(x, y)).collect::<Vec<_>>());
        }
        modeset(&d, w as u16, h as u16);
        let f = d.snapshot().unwrap();
        assert_eq!((f.width, f.height), (w, h));
        assert_eq!(f.rgb, expected(w, h));
        // one pixel by hand: 0x00112233 is R 11H, G 22H, B 33H
        poke(&d, 0, &[0xff11_2233]);
        assert_eq!(d.snapshot().unwrap().rgb[..3], [0x11, 0x22, 0x33]);
    }

    /// With the big-endian byte order selected (X, R, G, B in memory) the same picture comes out.
    #[test]
    fn snapshot_converts_big_endian() {
        let d = display(16);
        let (w, h) = (64u32, 70u32);
        for y in 0..h {
            poke(&d, u64::from(y * w * 4), &(0..w).map(|x| xrgb(x, y).swap_bytes()).collect::<Vec<_>>());
        }
        modeset(&d, w as u16, h as u16);
        write(&d, BYTE_ORDER, 4, 0xbebe_bebe);
        assert!(d.mode().unwrap().big_endian);
        assert_eq!(d.snapshot().unwrap().rgb, expected(w, h));
        // the same memory read as little-endian is not the picture
        write(&d, BYTE_ORDER, 4, 0x1e1e_1e1e);
        assert_ne!(d.snapshot().unwrap().rgb, expected(w, h));
    }

    /// Rows `stride` apart from `offset`: the picture is the visible part of a wider buffer at a 4-aligned (not
    /// 8-aligned) offset, and nothing outside it shows. A mode past the framebuffer is clipped, never read past.
    #[test]
    fn snapshot_honours_stride_and_offset() {
        let d = display(16);
        // a 100 x 80 buffer of pattern at Y_OFFSET 3, X_OFFSET 5 (offset 3 x 400 + 20: 4 mod 8), pitch 100 pixels
        let (bw, bh, w, h, xo, yo) = (100u32, 80u32, 64u32, 64u32, 5u32, 3u32);
        for y in 0..bh {
            poke(&d, u64::from(y * bw * 4), &(0..bw).map(|x| if x >= xo && y >= yo { xrgb(x - xo, y - yo) } else { 0xffff_ffff }).collect::<Vec<_>>());
        }
        set(&d, XRES, w as u16);
        set(&d, YRES, h as u16);
        set(&d, VIRT_WIDTH, bw as u16);
        set(&d, X_OFFSET, xo as u16);
        set(&d, Y_OFFSET, yo as u16);
        set(&d, BPP, 32);
        set(&d, ENABLE, ENABLED);
        let m = d.mode().unwrap();
        assert_eq!((m.stride, m.offset), (400, 3 * 400 + 20));
        assert_eq!(m.offset % 8, 4, "a 4-aligned offset");
        assert_eq!(d.snapshot().unwrap().rgb, expected(w, h));
        // an odd width: the row's last pixel ends mid-word
        set(&d, XRES, 65);
        assert_eq!(d.snapshot().unwrap().rgb, expected(65, h));
        // clipped: a mode whose last rows lie past a 4 KiB framebuffer reads them black, and reads nothing past it
        let len = 4096u64;
        let mode = Mode { width: 8, height: 4, stride: 1024, offset: 2020, big_endian: false };
        let mut reads = Vec::new();
        let f = frame(&mode, len, |at, words| {
            reads.push((at, words.len()));
            words.fill(0x0000_0001_0000_0001);
        });
        assert!(reads.iter().all(|&(at, n)| at % 8 == 0 && at + 8 * n as u64 <= len), "{reads:?}");
        let row = |y: usize| &f.rgb[24 * y..24 * (y + 1)];
        assert_eq!(row(0), [0, 0, 1].repeat(8), "row 0 whole");
        assert_eq!(row(1), [0, 0, 1].repeat(8), "row 1 whole");
        assert_eq!(&row(2)[..21], [0, 0, 1].repeat(7), "row 2: seven pixels before the end");
        assert_eq!(&row(2)[21..], [0, 0, 0], "the eighth past the end, black");
        assert_eq!(row(3), [0; 24], "row 3 past the end, black");
    }

    /// No published mode: no snapshot and no framebuffer; a mode past the snapshot bound is refused, not encoded.
    #[test]
    fn snapshot_while_disabled() {
        let d = display(64);
        assert_eq!(d.snapshot(), Err(SnapshotError::NotEnabled), "no driver yet");
        assert!(d.framebuffer().is_none());
        modeset(&d, 640, 480);
        assert!(d.snapshot().is_ok());
        assert_eq!(d.framebuffer().map(|(m, _)| m), Some(mode(640, 480)));
        set(&d, ENABLE, 0);
        assert_eq!(d.snapshot(), Err(SnapshotError::NotEnabled), "disabled");
        modeset(&d, 2560, 1600);
        assert_eq!(d.snapshot().map(|f| (f.width, f.height)), Ok((2560, 1600)), "the largest");
        modeset(&d, 2561, 1600);
        assert_eq!(d.snapshot(), Err(SnapshotError::TooLarge { width: 2561, height: 1600 }));
        modeset(&d, 1024, 1601);
        assert_eq!(d.snapshot(), Err(SnapshotError::TooLarge { width: 1024, height: 1601 }));
    }

    /// The PNG decodes (with the `png` crate's decoder) to the frame: 8-bit RGB, the same pixels.
    #[test]
    fn png_round_trips() {
        for (w, h) in [(64u32, 64u32), (640, 480), (65, 1)] {
            let f = Frame { width: w, height: h, rgb: expected(w, h) };
            let png = f.png();
            assert_eq!(&png[..8], b"\x89PNG\r\n\x1a\n");
            let mut reader = png::Decoder::new(std::io::Cursor::new(&png)).read_info().unwrap();
            let mut out = vec![0; reader.output_buffer_size().unwrap()];
            let info = reader.next_frame(&mut out).unwrap();
            assert_eq!((info.width, info.height), (w, h));
            assert_eq!((info.color_type, info.bit_depth), (png::ColorType::Rgb, png::BitDepth::Eight));
            assert_eq!(&out[..info.buffer_size()], &f.rgb[..], "{w} x {h}");
        }
    }

    /// `wait_change` returns at once when the counter has moved, at a change, or at its timeout.
    #[test]
    fn wait_change_follows_the_counter() {
        let d = display(16);
        let t = std::time::Instant::now();
        assert_eq!(d.wait_change(0, Duration::from_millis(30)), 0);
        assert!(t.elapsed() >= Duration::from_millis(30));
        let waiter = {
            let d = d.clone();
            std::thread::spawn(move || d.wait_change(0, Duration::from_secs(30)))
        };
        std::thread::sleep(Duration::from_millis(20));
        modeset(&d, 640, 480);
        assert_eq!(waiter.join().unwrap(), 1);
        assert_eq!(d.wait_change(0, Duration::from_secs(30)), 1, "already moved");
    }
}
