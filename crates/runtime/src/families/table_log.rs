//! Observers of guest page-table accesses whose records the family checkers read.
use super::*;
use natemu_abi::families::coherence::Walk;

/// Counts table reads and writes. `require_warm` refuses A/D writes: such fixtures
/// supply tables whose A/D bits are already set. A refusal happens before any
/// compare-exchange, so it never causes a re-walk; without it the counts cover
/// every attempt of a re-walked access.
pub(crate) struct Counts {
    pub reads: *mut u64,
    pub writes: *mut u64,
    pub require_warm: bool,
}
impl Observer for Counts {
    fn read(&mut self, _: u64, _: u64) -> bool {
        unsafe { *self.reads += 1 };
        true
    }
    fn write(&mut self, _: u64, _: u64, _: u64) -> bool {
        unsafe { *self.writes += 1 };
        !self.require_warm
    }
}

/// Records the entries one walk reads and writes; refuses a fifth of either.
/// A walk that lost an A/D race restarts at its root entry: re-reading the
/// first recorded entry starts a fresh record, so the log holds exactly the
/// attempt that completed.
// ponytail: a guest table whose deeper level re-reads the root entry (a
// recursive self-map at the same index) would be mistaken for a restart; an
// explicit attempt signal from the walker removes that ceiling.
pub(crate) struct WalkLog(pub *mut Walk);
impl Observer for WalkLog {
    fn read(&mut self, gpa: u64, value: u64) -> bool {
        let w = unsafe { &mut *self.0 };
        if w.read_count != 0 && w.reads[0][0] == gpa {
            (w.read_count, w.reads) = (0, [[0; 2]; 4]);
            (w.write_count, w.writes) = (0, [[0; 3]; 4]);
        }
        let i = w.read_count as usize;
        if i >= w.reads.len() {
            return false;
        }
        w.reads[i] = [gpa, value];
        w.read_count += 1;
        true
    }
    fn write(&mut self, gpa: u64, old: u64, new: u64) -> bool {
        let w = unsafe { &mut *self.0 };
        let i = w.write_count as usize;
        if i >= w.writes.len() {
            return false;
        }
        w.writes[i] = [gpa, old, new];
        w.write_count += 1;
        true
    }
}
