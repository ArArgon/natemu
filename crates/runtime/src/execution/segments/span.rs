//! Retained translations for explicit transactions that preflight several accesses before committing any of them.
use super::*;

#[derive(Clone, Copy, PartialEq)]
enum Backing {
    Ram,
    Rom,
    Device,
}

#[derive(Clone, Copy)]
pub(crate) struct Span {
    va: u64,
    len: usize,
    first: usize,
    gpa: [u64; 2],
    backing: [Backing; 2],
}

impl Span {
    /// Caller already checked the segment and alignment. All page permissions and A/D updates use the original walk.
    pub(crate) fn walk(
        s: &mut State,
        va: u64,
        len: usize,
        write: bool,
        range: OperandRange,
    ) -> Result<Self, Exception> {
        let gpa = pages(s, va, len as u64, write, false, range)?;
        let first = len.min(4096 - (va & 4095) as usize);
        let mut backing = [Backing::Ram; 2];
        for j in 0..1 + usize::from(first < len) {
            backing[j] = if mmio::ram(gpa[j]) {
                Backing::Ram
            } else if mmio::rom(gpa[j]) {
                Backing::Rom
            } else {
                Backing::Device
            };
        }
        Ok(Self {
            va,
            len,
            first,
            gpa,
            backing,
        })
    }

    fn chunks(&self, offset: usize, len: usize) -> impl Iterator<Item = (u64, Backing, usize)> {
        debug_assert!(offset + len <= self.len);
        let low = self.first.saturating_sub(offset).min(len);
        [
            (
                self.gpa[0].wrapping_add(offset as u64),
                self.backing[0],
                low,
            ),
            (
                self.gpa[1] + offset.saturating_sub(self.first) as u64,
                self.backing[1],
                len - low,
            ),
        ]
        .into_iter()
        .filter(|chunk| chunk.2 != 0)
    }

    /// Check routes only after all walks have succeeded. MASKMOV accepts ROM; XSAVE's image must be entirely RAM.
    pub(crate) fn check_backing(&self, s: &State, offset: usize, len: usize, allow_rom: bool) {
        for (_, backing, _) in self.chunks(offset, len) {
            if backing != Backing::Ram && !(allow_rom && backing == Backing::Rom) {
                mmio::system_access(s)
            }
        }
    }

    /// Commit a subrange of a checked span. Split locks and UC ordering still apply at the actual access.
    pub(crate) fn copy(&self, s: &mut State, offset: usize, buf: &mut [u8], write: bool) {
        split_wait(s, &self.gpa[..1 + usize::from(self.first < self.len)]);
        watch::access(
            s,
            self.va.wrapping_add(offset as u64),
            buf.len() as u64,
            write,
        );
        let mut done = 0;
        for (gpa, backing, len) in self.chunks(offset, buf.len()) {
            let chunk = &mut buf[done..done + len];
            let c = cache::control();
            if c.uncached_count | c.uncached_fence != 0 {
                // SAFETY: order earlier accesses before each transfer after a UC walk.
                unsafe { asm!("dmb ish") };
            }
            match backing {
                Backing::Ram => copy_ram(s, [gpa, 0], len, chunk, write),
                Backing::Rom if write => {}
                Backing::Rom => {
                    if !mmio::roms().copy_out(gpa, chunk) {
                        halt(s, stop::UNSUPPORTED_SYSTEM)
                    }
                }
                Backing::Device => mmio::system_access(s),
            }
            done += len;
        }
    }
}
