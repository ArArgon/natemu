//! The guest's picture: the framebuffer mapped read-only, the mode the guest shows in it, and the copy of that picture
//! into the window's pixels, scaled to fit with its aspect kept (nearest pixel, so integer scales stay sharp).
use serde_json::Value;
use std::io;
use std::os::fd::{AsRawFd, OwnedFd};
use std::sync::atomic::{AtomicU32, Ordering};

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

/// The framebuffer, mapped read-only. The guest writes it while we read, so it is read as atomic words: a pixel is
/// never torn, a row may show part of two frames (as a real scan-out would).
pub struct Framebuffer {
    base: *const AtomicU32,
    words: usize,
}

// The mapping is shared memory read through atomics only.
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

    fn word(&self, i: usize) -> u32 {
        if i < self.words { unsafe { (*self.base.add(i)).load(Ordering::Relaxed) } } else { 0 }
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

/// Draws `mode`'s picture from `fb` into `out` (`dw` x `dh` pixels, 0RGB words), black around it. A part of the picture
/// that lies outside the framebuffer is black.
pub fn draw(fb: &Framebuffer, mode: &Mode, out: &mut [u32], dw: u32, dh: u32) {
    out.fill(0);
    let (ox, oy, ow, oh) = fit(mode.width, mode.height, dw, dh);
    if ow == 0 {
        return;
    }
    let xs: Vec<usize> = (0..ow).map(|x| (x as u64 * mode.width as u64 / ow as u64) as usize).collect();
    let first = (mode.offset / 4) as usize;
    let row_words = (mode.stride / 4) as usize;
    for y in 0..oh {
        let sy = (y as u64 * mode.height as u64 / oh as u64) as usize;
        let src = first.saturating_add(sy.saturating_mul(row_words));
        let start = (oy + y) as usize * dw as usize + ox as usize;
        let dst = &mut out[start..start + ow as usize];
        for (d, &sx) in dst.iter_mut().zip(&xs) {
            let w = fb.word(src.saturating_add(sx));
            *d = if mode.big_endian { w.swap_bytes() } else { w } & 0x00ff_ffff;
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
}
