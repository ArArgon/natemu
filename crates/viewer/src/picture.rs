//! The guest's picture: the framebuffer mapped read-only, the mode the guest shows in it, a copy of that picture (which
//! the window compares with the last one, to draw only when something changed), and the drawing of a copy into the
//! window's pixels, scaled to fit with its shape kept.
use serde_json::Value;
use std::io;
use std::os::fd::{AsRawFd, OwnedFd};

/// A picture's geometry in the framebuffer (`framebuffer`'s answer and `display_changed`, docs/control-api.md).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Mode {
    pub width: u32,
    pub height: u32,
    pub stride: u32,
    pub offset: u64,
    /// `b8g8r8x8`: the unused byte, R, G, B in memory (else `x8r8g8b8`: B, G, R, unused).
    pub big_endian: bool,
}

impl Mode {
    /// The mode a `framebuffer` answer or a `display_changed` notification describes; `None` when the display is off
    /// or the description is not one this viewer can show (an unknown format, or rows not 4-byte aligned).
    pub fn parse(v: &Value) -> Option<Mode> {
        if v.get("enabled").and_then(Value::as_bool) == Some(false) {
            return None;
        }
        let num = |k: &str| v.get(k).and_then(Value::as_u64);
        let big_endian = match v.get("format").and_then(Value::as_str)? {
            "x8r8g8b8" => false,
            "b8g8r8x8" => true,
            _ => return None,
        };
        let mode = Mode {
            width: u32::try_from(num("width")?).ok()?,
            height: u32::try_from(num("height")?).ok()?,
            stride: u32::try_from(num("stride")?).ok()?,
            offset: num("offset")?,
            big_endian,
        };
        (mode.width > 0 && mode.height > 0 && mode.stride % 4 == 0 && mode.offset % 4 == 0).then_some(mode)
    }
}

/// The framebuffer, mapped read-only. The guest writes it while we read: a copy may show part of two frames (as a real
/// scan-out would), which the next copy corrects.
pub struct Framebuffer {
    base: *const u32,
    words: usize,
}

// The mapping is shared memory this process only reads.
unsafe impl Send for Framebuffer {}
unsafe impl Sync for Framebuffer {}

impl Framebuffer {
    pub fn map(fd: &OwnedFd, size: u64) -> io::Result<Framebuffer> {
        let size = usize::try_from(size).map_err(|_| io::Error::other("framebuffer size does not fit"))?;
        if size == 0 || size % 4 != 0 {
            return Err(io::Error::other(format!("framebuffer size {size} is not a positive multiple of 4")));
        }
        let p = unsafe { libc::mmap(std::ptr::null_mut(), size, libc::PROT_READ, libc::MAP_SHARED, fd.as_raw_fd(), 0) };
        if p == libc::MAP_FAILED {
            return Err(io::Error::last_os_error());
        }
        Ok(Framebuffer { base: p.cast(), words: size / 4 })
    }

    /// Whether `mode`'s visible picture is word for word `last` (a [`Framebuffer::copy`] of the same mode): compared in
    /// place, so an unchanged picture costs one read of it and no copy.
    pub fn same(&self, mode: &Mode, last: &[u32]) -> bool {
        let (w, h) = (mode.width as usize, mode.height as usize);
        if last.len() != w * h {
            return false;
        }
        let first = (mode.offset / 4) as usize;
        let row_words = (mode.stride / 4) as usize;
        last.chunks_exact(w).enumerate().all(|(y, row)| {
            let start = first.saturating_add(y.saturating_mul(row_words));
            let n = w.min(self.words.saturating_sub(start));
            // Raw memcmp, not a slice: the guest may write the mapping meanwhile.
            (n == 0 || unsafe { libc::memcmp(self.base.add(start).cast(), row.as_ptr().cast(), n * 4) } == 0)
                && row[n..].iter().all(|&v| v == 0)
        })
    }

    /// Copies `mode`'s visible picture into `out` (`width` x `height` words, rows packed), as raw framebuffer words; a
    /// part that lies outside the framebuffer is 0. One plain copy per row: the words are read once, so another
    /// process writing them meanwhile cannot make this process see two values for one word.
    pub fn copy(&self, mode: &Mode, out: &mut Vec<u32>) {
        let (w, h) = (mode.width as usize, mode.height as usize);
        out.clear();
        out.resize(w * h, 0);
        let first = (mode.offset / 4) as usize;
        let row_words = (mode.stride / 4) as usize;
        for (y, row) in out.chunks_exact_mut(w).enumerate() {
            let start = first.saturating_add(y.saturating_mul(row_words));
            let n = w.min(self.words.saturating_sub(start));
            if n > 0 {
                unsafe { std::ptr::copy_nonoverlapping(self.base.add(start), row.as_mut_ptr(), n) };
            }
        }
    }
}

impl Drop for Framebuffer {
    fn drop(&mut self) {
        unsafe { libc::munmap(self.base as *mut _, self.words * 4) };
    }
}

/// Where the picture lands in a `dw` x `dh` window: (x, y, width, height), the largest scale that fits, centred.
pub fn fit(sw: u32, sh: u32, dw: u32, dh: u32) -> (u32, u32, u32, u32) {
    if sw == 0 || sh == 0 || dw == 0 || dh == 0 {
        return (0, 0, 0, 0);
    }
    let scale = f64::min(dw as f64 / sw as f64, dh as f64 / sh as f64);
    let ow = ((sw as f64 * scale).floor() as u32).clamp(1, dw);
    let oh = ((sh as f64 * scale).floor() as u32).clamp(1, dh);
    ((dw - ow) / 2, (dh - oh) / 2, ow, oh)
}

/// Draws a picture copied by [`Framebuffer::copy`] (`mode`'s width x height raw words) into `out` (`dw` x `dh` pixels,
/// 0RGB words), scaled to fit with its shape kept (nearest pixel, so integer scales stay sharp), black around it.
pub fn draw(src: &[u32], mode: &Mode, out: &mut [u32], dw: u32, dh: u32) {
    out.fill(0);
    let (sw, sh) = (mode.width as usize, mode.height as usize);
    let (ox, oy, ow, oh) = fit(mode.width, mode.height, dw, dh);
    if ow == 0 || src.len() < sw * sh {
        return;
    }
    let xs: Vec<usize> = (0..ow as usize).map(|x| x * sw / ow as usize).collect();
    for y in 0..oh as usize {
        let row = &src[(y * sh / oh as usize) * sw..][..sw];
        let start = (oy as usize + y) * dw as usize + ox as usize;
        let dst = &mut out[start..start + ow as usize];
        if mode.big_endian {
            for (d, &sx) in dst.iter_mut().zip(&xs) {
                *d = row[sx].swap_bytes() & 0x00ff_ffff;
            }
        } else {
            for (d, &sx) in dst.iter_mut().zip(&xs) {
                *d = row[sx] & 0x00ff_ffff;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parses_the_documented_modes() {
        let on = json!({"enabled":true,"width":640,"height":480,"stride":3200,"offset":51220,"format":"x8r8g8b8"});
        assert_eq!(
            Mode::parse(&on),
            Some(Mode { width: 640, height: 480, stride: 3200, offset: 51220, big_endian: false })
        );
        let answer = json!({"width":640,"height":480,"stride":3200,"offset":0,"format":"b8g8r8x8","size":16777216});
        assert!(Mode::parse(&answer).unwrap().big_endian);
        let off = json!({"enabled":false,"width":0,"height":0,"stride":0,"offset":0});
        assert_eq!(Mode::parse(&off), None);
        assert_eq!(Mode::parse(&json!({"width":640,"height":480,"stride":3200,"offset":0,"format":"rgb565"})), None);
    }

    #[test]
    fn fit_keeps_the_aspect_and_centres() {
        assert_eq!(fit(1024, 768, 2048, 1536), (0, 0, 2048, 1536));
        assert_eq!(fit(1024, 768, 2048, 1636), (0, 50, 2048, 1536));
        assert_eq!(fit(640, 480, 100, 480), (0, 202, 100, 75));
    }

    #[test]
    fn copy_and_same_read_the_picture_where_the_mode_says() {
        use std::os::unix::fs::FileExt;
        // A 40-byte framebuffer (10 words); a 2 x 2 picture at word 1, rows 4 words apart: words 1, 2 and 5, 6.
        let path = std::env::temp_dir().join(format!("natemu-viewer-test-{}", std::process::id()));
        let file = std::fs::File::options().read(true).write(true).create(true).truncate(true).open(&path).unwrap();
        let words: Vec<u8> = (0u32..10).flat_map(|w| (w * 0x11).to_le_bytes()).collect();
        file.write_all_at(&words, 0).unwrap();
        let fb = Framebuffer::map(&OwnedFd::from(file.try_clone().unwrap()), 40).unwrap();
        let mode = Mode { width: 2, height: 2, stride: 16, offset: 4, big_endian: false };
        let mut copy = Vec::new();
        fb.copy(&mode, &mut copy);
        assert_eq!(copy, vec![0x11, 0x22, 0x55, 0x66]);
        assert!(fb.same(&mode, &copy));
        // A write inside the picture is a change; one outside it (word 3) is not.
        file.write_all_at(&0x33u32.to_le_bytes(), 12).unwrap();
        file.write_all_at(&0xEEu32.to_le_bytes(), 12).unwrap();
        assert!(fb.same(&mode, &copy));
        file.write_all_at(&0x77u32.to_le_bytes(), 20).unwrap();
        assert!(!fb.same(&mode, &copy));
        // Rows past the framebuffer's end are black (0) in a copy, and compare as such.
        let tall = Mode { height: 3, ..mode };
        fb.copy(&tall, &mut copy);
        assert_eq!(copy, vec![0x11, 0x22, 0x77, 0x66, 0x99, 0]);
        assert!(fb.same(&tall, &copy));
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn draw_scales_by_nearest_pixel_and_keeps_the_colour_order() {
        // A 2 x 1 picture, x8r8g8b8 (red, then blue), drawn into 4 x 3: scaled 2x, centred, the last row black.
        let le = Mode { width: 2, height: 1, stride: 8, offset: 0, big_endian: false };
        let mut out = vec![7u32; 4 * 3];
        draw(&[0xAA_FF0000, 0xAA_0000FF], &le, &mut out, 4, 3);
        let (r, b) = (0xFF0000, 0x0000FF);
        assert_eq!(out, vec![r, r, b, b, r, r, b, b, 0, 0, 0, 0]);
        // b8g8r8x8 holds X, R, G, B in memory: the same red read as a little-endian word is 0x0000FFXX.
        let be = Mode { big_endian: true, ..le };
        let mut out = vec![0u32; 2];
        draw(&[0x0000_FFAA, 0xFF00_00AA], &be, &mut out, 2, 1);
        assert_eq!(out, vec![r, b]);
    }
}
