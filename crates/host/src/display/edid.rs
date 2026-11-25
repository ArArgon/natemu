//! The display's EDID: a VESA E-EDID 1.4 base block (VESA E-EDID Standard, Release A, Revision 2), written from the
//! standard; no code is adapted from another implementation.
//!
//! The block describes a monitor whose preferred (native) mode is `width` x `height` at 60 Hz:
//!
//! - manufacturer "NTM", product 1, no serial number, made in 2026 (week unspecified);
//! - a digital input, 8 bits per colour, RGB 4:4:4, sRGB as the default colour space (with sRGB's chromaticity), and
//!   the preferred timing mode its native one;
//! - the physical size at 96 pixels per inch, so a guest computes 96 DPI;
//! - standard timings for 800 x 600 and 640 x 480, both 60 Hz, 4:3 (the other six slots unused);
//! - descriptor 1: the detailed timing of the preferred mode, CVT reduced blanking ([`cvt_rb`]); descriptor 2: the
//!   monitor name "natemu"; descriptors 3 and 4: dummy descriptors;
//! - no extension blocks, and the checksum.
//!
//! Limits: the preferred mode is at most 3935 x 4095 (a detailed timing's 12-bit fields: active plus blanking below
//! 4096), at least 64 x 64; [`block`] panics outside them (the launch validates the configuration first).

/// Horizontal blanking, front porch and sync width of CVT reduced blanking, in pixels (CVT 1.2, Table 3-2).
const RB_H_BLANK: u32 = 160;
const RB_H_FRONT: u32 = 48;
const RB_H_SYNC: u32 = 32;
/// Vertical front porch and the least back porch, in lines; the least vertical blanking, in microseconds.
const RB_V_FRONT: u32 = 3;
const RB_MIN_V_BACK: u32 = 6;
const RB_MIN_V_BLANK_US: u64 = 460;
/// The pixel clock's step: 0.25 MHz, in the detailed timing's units of 10 kHz.
const CLOCK_STEP_10KHZ: u64 = 25;
const REFRESH_HZ: u64 = 60;

/// One mode's CVT reduced-blanking timing (all in pixels or lines; the clock in units of 10 kHz).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Timing {
    pub clock_10khz: u16,
    pub h_active: u32,
    pub h_blank: u32,
    pub h_front: u32,
    pub h_sync: u32,
    pub v_active: u32,
    pub v_blank: u32,
    pub v_front: u32,
    pub v_sync: u32,
}

/// CVT 1.2 reduced blanking (version 1) for `width` x `height` at 60 Hz, progressive, no margins. The computation,
/// from CVT 1.2 section 5.4 with every quantity kept exact in integers:
///
/// - vertical sync width by aspect ratio: 4:3 4 lines, 16:9 5, 16:10 6, 5:4 and 15:9 7, any other 10;
/// - the estimated line period is (1/60 s - 460 us) / height, so the lines of vertical blanking are
///   floor(460 us / line period) + 1 = floor(460 x 60 x height / (10^6 - 460 x 60)) + 1, and at least front porch (3) +
///   sync + the least back porch (6);
/// - horizontal blanking 160 pixels (front porch 48, sync 32, back porch 80);
/// - the pixel clock is 60 x total lines x total pixels, rounded down to a multiple of 0.25 MHz.
///
/// One departure: CVT rounds the active width down to its cell of 8 pixels; here it stays as given (1366 stays 1366),
/// so the timing's active size is the preferred size exactly. The horizontal totals stay what CVT gives for any width
/// that is a multiple of 8.
pub fn cvt_rb(width: u32, height: u32) -> Timing {
    let (w, h) = (u64::from(width), u64::from(height));
    let v_sync = if w * 3 == h * 4 {
        4
    } else if w * 9 == h * 16 {
        5
    } else if w * 10 == h * 16 {
        6
    } else if w * 4 == h * 5 || w * 9 == h * 15 {
        7
    } else {
        10
    };
    let vbi = (RB_MIN_V_BLANK_US * REFRESH_HZ * h / (1_000_000 - RB_MIN_V_BLANK_US * REFRESH_HZ) + 1)
        .max(u64::from(RB_V_FRONT + v_sync + RB_MIN_V_BACK));
    let total_v = h + vbi;
    let total_h = w + u64::from(RB_H_BLANK);
    // pixels per second / 10^4 is the clock in 10 kHz; round down to the step.
    let clock = REFRESH_HZ * total_v * total_h / (10_000 * CLOCK_STEP_10KHZ) * CLOCK_STEP_10KHZ;
    Timing {
        clock_10khz: u16::try_from(clock).expect("pixel clock within a detailed timing's 655.35 MHz"),
        h_active: width,
        h_blank: RB_H_BLANK,
        h_front: RB_H_FRONT,
        h_sync: RB_H_SYNC,
        v_active: height,
        v_blank: vbi as u32,
        v_front: RB_V_FRONT,
        v_sync,
    }
}

/// The EDID base block for a monitor whose preferred mode is `width` x `height` (module documentation).
pub fn block(width: u32, height: u32) -> [u8; 128] {
    assert!(
        (64..=4095 - RB_H_BLANK).contains(&width) && (64..=4095).contains(&height),
        "EDID preferred mode {width} x {height}: from 64 x 64 to 3935 x 4095"
    );
    let mut b = [0u8; 128];
    b[..8].copy_from_slice(&[0, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0]);
    // Manufacturer: three letters of 5 bits (A = 1), big-endian, bit 15 clear.
    let letter = |c: u8| u16::from(c - b'A' + 1);
    let id = letter(b'N') << 10 | letter(b'T') << 5 | letter(b'M');
    b[8..10].copy_from_slice(&id.to_be_bytes());
    b[10..12].copy_from_slice(&1u16.to_le_bytes()); // product 1
    // 12-15 serial number 0; 16 week unspecified; 17 year - 1990.
    b[17] = (2026 - 1990) as u8;
    (b[18], b[19]) = (1, 4);
    // Digital input, 8 bits per primary colour, interface not specified.
    b[20] = 0x80 | 0b010 << 4;
    // The screen size in cm, at 96 pixels per inch.
    let (w_mm, h_mm) = (mm(width), mm(height));
    (b[21], b[22]) = (((w_mm + 5) / 10) as u8, ((h_mm + 5) / 10) as u8);
    b[23] = 120; // gamma 2.2: (2.2 x 100) - 100
    // No DPMS, RGB 4:4:4, sRGB the default colour space, the preferred timing native, not continuous frequency.
    b[24] = 0x04 | 0x02;
    // sRGB's chromaticity (IEC 61966-2-1): red, green, blue, white (D65), as 10-bit binary fractions.
    let xy: Vec<u16> = [0.640, 0.330, 0.300, 0.600, 0.150, 0.060, 0.3127, 0.3290]
        .iter()
        .map(|&c: &f64| (c * 1024.0).round() as u16)
        .collect();
    let low = |i: usize| (xy[i] & 3) as u8;
    b[25] = low(0) << 6 | low(1) << 4 | low(2) << 2 | low(3);
    b[26] = low(4) << 6 | low(5) << 4 | low(6) << 2 | low(7);
    for (i, &c) in xy.iter().enumerate() {
        b[27 + i] = (c >> 2) as u8;
    }
    // 35-37: no established timings. Standard timings: (width / 8 - 31), aspect 4:3 (01b) and refresh - 60.
    b[38..54].fill(1);
    b[38..42].copy_from_slice(&[(800 / 8 - 31) as u8, 0x40, (640 / 8 - 31) as u8, 0x40]);
    b[54..72].copy_from_slice(&detailed(cvt_rb(width, height), w_mm, h_mm));
    // Display product name: tag FCH, up to 13 bytes, a line feed after the name and spaces after that.
    b[72..77].copy_from_slice(&[0, 0, 0, 0xfc, 0]);
    let mut name = [b' '; 13];
    name[..7].copy_from_slice(b"natemu\n");
    b[77..90].copy_from_slice(&name);
    // Descriptors 3 and 4: dummy descriptors (tag 10H).
    for d in [90, 108] {
        b[d + 3] = 0x10;
    }
    // 126: no extensions. 127: the checksum makes the block's sum 0 mod 256.
    b[127] = b[..127].iter().fold(0u8, |s, &x| s.wrapping_sub(x));
    b
}

/// `pixels` at 96 per inch, in millimetres.
fn mm(pixels: u32) -> u32 {
    (pixels * 254 + 480) / 960
}

/// A detailed timing descriptor (18 bytes) of `t`, the image `w_mm` x `h_mm`: digital separate sync, horizontal sync
/// positive and vertical negative (as CVT reduced blanking signals itself), progressive, no borders.
fn detailed(t: Timing, w_mm: u32, h_mm: u32) -> [u8; 18] {
    let lo = |v: u32| v as u8;
    let nibbles = |hi: u32, lo: u32| ((hi >> 8 & 0xf) << 4 | (lo >> 8 & 0xf)) as u8;
    let mut d = [0u8; 18];
    d[..2].copy_from_slice(&t.clock_10khz.to_le_bytes());
    (d[2], d[3], d[4]) = (lo(t.h_active), lo(t.h_blank), nibbles(t.h_active, t.h_blank));
    (d[5], d[6], d[7]) = (lo(t.v_active), lo(t.v_blank), nibbles(t.v_active, t.v_blank));
    (d[8], d[9]) = (lo(t.h_front), lo(t.h_sync));
    d[10] = ((t.v_front & 0xf) << 4 | (t.v_sync & 0xf)) as u8;
    d[11] = ((t.h_front >> 8 & 3) << 6 | (t.h_sync >> 8 & 3) << 4 | (t.v_front >> 4 & 3) << 2 | (t.v_sync >> 4 & 3))
        as u8;
    (d[12], d[13], d[14]) = (lo(w_mm), lo(h_mm), nibbles(w_mm, h_mm));
    d[17] = 0x18 | 0x02;
    d
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The first detailed timing descriptor (bytes 54-71), decoded.
    fn first_timing(b: &[u8; 128]) -> Timing {
        let d = &b[54..72];
        let hi = |byte: u8, shift: u32| u32::from(byte >> shift & 0xf) << 8;
        Timing {
            clock_10khz: u16::from_le_bytes([d[0], d[1]]),
            h_active: u32::from(d[2]) | hi(d[4], 4),
            h_blank: u32::from(d[3]) | hi(d[4], 0),
            v_active: u32::from(d[5]) | hi(d[7], 4),
            v_blank: u32::from(d[6]) | hi(d[7], 0),
            h_front: u32::from(d[8]) | u32::from(d[11] >> 6 & 3) << 8,
            h_sync: u32::from(d[9]) | u32::from(d[11] >> 4 & 3) << 8,
            v_front: u32::from(d[10] >> 4) | u32::from(d[11] >> 2 & 3) << 4,
            v_sync: u32::from(d[10] & 0xf) | u32::from(d[11] & 3) << 4,
        }
    }

    #[test]
    fn edid_checksum_and_preferred_mode() {
        for (w, h) in [(1024, 768), (1366, 768), (1920, 1080), (640, 480), (2560, 1600), (3840, 2160)] {
            let b = block(w, h);
            let why = format!("{w}x{h}");
            assert_eq!(b[..8], [0, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0], "{why}: header");
            assert_eq!(b.iter().fold(0u8, |s, &x| s.wrapping_add(x)), 0, "{why}: checksum");
            assert_eq!((b[18], b[19]), (1, 4), "{why}: version 1.4");
            // "NTM": three 5-bit letters (A = 1), big-endian
            let id = u16::from_be_bytes([b[8], b[9]]);
            let letters: String =
                [10, 5, 0].iter().map(|s| char::from(b'A' - 1 + (id >> s & 0x1f) as u8)).collect();
            assert_eq!(letters, "NTM", "{why}: manufacturer");
            assert_eq!(b[126], 0, "{why}: no extensions");
            let t = first_timing(&b);
            assert_ne!(t.clock_10khz, 0, "{why}: descriptor 1 is a detailed timing");
            assert_eq!((t.h_active, t.v_active), (w, h), "{why}: the preferred size");
            assert_eq!((t.h_blank, t.h_front, t.h_sync), (160, 48, 32), "{why}: CVT-RB horizontal");
            assert_eq!(t.v_front, 3, "{why}: CVT-RB vertical front porch");
            assert_eq!(t, cvt_rb(w, h), "{why}: the descriptor holds the computed timing");
            assert_eq!(b[71] & 0x1e, 0x1a, "{why}: digital separate sync, H+ V-");
            // the monitor name, then the standard timings 800x600@60 and 640x480@60 (4:3)
            assert_eq!(b[72..77], [0, 0, 0, 0xfc, 0], "{why}: name descriptor");
            assert_eq!(&b[77..90], b"natemu\n      ", "{why}: name");
            assert_eq!(b[38..42], [0x45, 0x40, 0x31, 0x40], "{why}: standard timings");
            assert!(b[42..54].chunks(2).all(|s| s == [1, 1]), "{why}: unused standard timings");
        }
        // CVT-RB as VESA's published timings give it: 1024x768R 56.00 MHz, 1184 x 790, V sync 4; 1920x1080R 138.50
        // MHz, 2080 x 1111, V sync 5; 1280x800R 71.00 MHz, 1440 x 823, V sync 6.
        for ((w, h), clock, vblank, vsync) in [
            ((1024, 768), 5600, 22, 4),
            ((1920, 1080), 13850, 31, 5),
            ((1280, 800), 7100, 23, 6),
        ] {
            let t = cvt_rb(w, h);
            assert_eq!((t.clock_10khz, t.v_blank, t.v_sync), (clock, vblank, vsync), "{w}x{h}");
        }
    }
}
