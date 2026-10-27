//! Bounded repeated maintenance requests; the resident coordinator owns access.
use crate::{
    NativeFrame, PRIVATE,
    concurrency::{MAX_CPUS, NO_CPU},
};
pub const OFFSET: usize = 0x92000;
pub const VA: u64 = PRIVATE + OFFSET as u64;
pub const RECORD_OFFSET: usize = 0x2a000;
pub const RECORD_VA: u64 = PRIVATE + RECORD_OFFSET as u64;
pub const BASELINE_OFFSET: usize = 0x2e000;
pub const BASELINE_VA: u64 = PRIVATE + BASELINE_OFFSET as u64;
pub const REQUESTS: u64 = 9;
pub const MAX_INTERVALS: usize = 4;
pub const DUPLICATE: u64 = 130;
pub const FULL: u64 = 131;
pub const BAD: u64 = 132;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(C)]
pub struct Request {
    pub cpu: u64,
    pub serial: u64,
}
impl Request {
    pub const EMPTY: Self = Self {
        cpu: NO_CPU,
        serial: 0,
    };
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(C)]
pub struct State {
    pub n: u64,
    pub ready: u64,
    pub serial: u64,
    pub head: u64,
    pub length: u64,
    pub queue: [Request; MAX_CPUS],
    pub current: Request,
    pub pending: [u64; MAX_CPUS],
    pub submitted: [u64; MAX_CPUS],
    pub completed: [u64; MAX_CPUS],
    pub epoch: u64,
    pub acknowledgements: u64,
    pub total: u64,
    pub batch: u64,
    pub next_ordinary: u64,
    pub ordinary: u64,
    pub intervals: [u64; MAX_CPUS],
    pub ordinary_total: u64,
    pub closing: u64,
    pub finished: u64,
    pub duplicate_rejections: u64,
    pub maximum_depth: u64,
    pub leaf_history: [u64; 36],
    pub request_history: [Request; 36],
    pub compose_leaf: u64,
}
impl State {
    pub fn new(n: usize) -> Option<Self> {
        if n == 0 || n > MAX_CPUS {
            return None;
        }
        Some(Self {
            n: n as u64,
            ready: 0,
            serial: 0,
            head: 0,
            length: 0,
            queue: [Request::EMPTY; MAX_CPUS],
            current: Request::EMPTY,
            pending: [0; MAX_CPUS],
            submitted: [0; MAX_CPUS],
            completed: [0; MAX_CPUS],
            epoch: 2,
            acknowledgements: 0,
            total: 0,
            batch: 0,
            next_ordinary: 0,
            ordinary: NO_CPU,
            intervals: [0; MAX_CPUS],
            ordinary_total: 0,
            closing: 0,
            finished: 0,
            duplicate_rejections: 0,
            maximum_depth: 0,
            leaf_history: [0; 36],
            request_history: [Request::EMPTY; 36],
            compose_leaf: 0,
        })
    }
    pub fn enqueue(&mut self, id: usize) -> Result<Option<Request>, u64> {
        self.enqueue_limit(id, self.n)
    }
    /// The fixture may lower the real outstanding-request capacity. Readiness
    /// remains an N-owner rendezvous; it is not an admission count.
    pub fn enqueue_limit(&mut self, id: usize, capacity: u64) -> Result<Option<Request>, u64> {
        if capacity == 0 || capacity > self.n {
            return Err(BAD);
        }
        if id >= self.n as usize || self.finished != 0 {
            return Err(BAD);
        }
        if self.pending[id] != 0 {
            return Err(DUPLICATE);
        }
        if self.submitted[id] >= REQUESTS {
            return Ok(None);
        }
        // Reserve the next round-robin waiter soon enough for its already queued
        // request to finish within the remaining batch, without passing it.
        if id as u64 == self.next_ordinary && self.batch >= 8 - self.n {
            return Ok(None);
        }
        if self.length + u64::from(self.current.serial != 0) >= capacity {
            return Err(FULL);
        }
        self.serial = self.serial.checked_add(1).ok_or(BAD)?;
        let r = Request {
            cpu: id as u64,
            serial: self.serial,
        };
        self.queue[((self.head + self.length) % self.n) as usize] = r;
        self.length += 1;
        self.pending[id] = r.serial;
        self.submitted[id] += 1;
        self.maximum_depth = self
            .maximum_depth
            .max(self.length + u64::from(self.current.serial != 0));
        Ok(Some(r))
    }
    pub fn ordinary_due(&self) -> bool {
        self.current.serial == 0
            && self.ordinary == NO_CPU
            && self.batch != 0
            && self.pending[self.next_ordinary as usize] == 0
            && (self.batch >= 8
                || self.total == self.n * REQUESTS
                || (self.length == 0
                    && (0..self.n as usize).all(|id| {
                        id as u64 == self.next_ordinary || self.completed[id] == REQUESTS
                    })))
    }
    pub fn start_head(&mut self) -> Result<Option<Request>, u64> {
        if self.current.serial != 0
            || self.ordinary != NO_CPU
            || self.length == 0
            || self.ordinary_due()
        {
            return Ok(None);
        }
        if self.batch >= 8 {
            return Err(BAD);
        }
        let r = self.queue[self.head as usize];
        if r.cpu >= self.n || self.pending[r.cpu as usize] != r.serial || r.serial == 0 {
            return Err(BAD);
        }
        self.head = (self.head + 1) % self.n;
        self.length -= 1;
        self.current = r;
        self.acknowledgements = 0;
        self.epoch = self.epoch.checked_add(1).ok_or(BAD)?;
        Ok(Some(r))
    }
    pub fn acknowledge(&mut self, id: usize) -> Result<(), u64> {
        if id >= self.n as usize
            || self.current.serial == 0
            || self.acknowledgements & (1 << id) != 0
        {
            return Err(BAD);
        }
        self.acknowledgements |= 1 << id;
        Ok(())
    }
    pub fn complete_head(&mut self) -> Result<Request, u64> {
        let r = self.current;
        if r.serial == 0 || self.acknowledgements != (1 << self.n) - 1 {
            return Err(BAD);
        }
        self.pending[r.cpu as usize] = 0;
        self.completed[r.cpu as usize] += 1;
        self.request_history[self.total as usize] = r;
        self.total += 1;
        self.batch += 1;
        self.current = Request::EMPTY;
        Ok(r)
    }
    pub fn begin_ordinary(&mut self) -> Result<u64, u64> {
        if !self.ordinary_due()
            || self.intervals[self.next_ordinary as usize] >= MAX_INTERVALS as u64
        {
            return Err(BAD);
        }
        self.ordinary = self.next_ordinary;
        self.closing = 0;
        Ok(self.ordinary)
    }
    pub fn finish_ordinary(&mut self, id: u64) -> Result<(), u64> {
        if self.ordinary != id || self.closing == 0 {
            return Err(BAD);
        }
        self.intervals[id as usize] += 1;
        self.ordinary_total += 1;
        self.batch = 0;
        self.next_ordinary = (id + 1) % self.n;
        self.ordinary = NO_CPU;
        if self.total == self.n * REQUESTS {
            self.finished = 1;
        }
        Ok(())
    }
}

#[derive(Clone, Copy)]
#[repr(C)]
pub struct Interval {
    pub ordinal: u64,
    pub heads: u64,
    pub epoch: u64,
    pub gpa: u64,
    pub initial: NativeFrame,
    pub captured: NativeFrame,
    pub dirty_frame: NativeFrame,
    pub before: [u8; 256],
    pub after: [u8; 256],
    pub faults: u64,
    pub esr: u64,
    pub far: u64,
    pub pc: u64,
    pub spsr: u64,
}
impl Interval {
    pub const ZERO: Self = Self {
        ordinal: 0,
        heads: 0,
        epoch: 0,
        gpa: 0,
        initial: NativeFrame::ZERO,
        captured: NativeFrame::ZERO,
        dirty_frame: NativeFrame::ZERO,
        before: [0; 256],
        after: [0; 256],
        faults: 0,
        esr: 0,
        far: 0,
        pc: 0,
        spsr: 0,
    };
}
const _: () = assert!(core::mem::size_of::<Interval>() <= 4096);
const _: () = assert!(core::mem::size_of::<State>() <= 4096);

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn pressure_preserves_slots_and_requires_each_head_ack() {
        for n in 1..=4 {
            let mut s = State::new(n).unwrap();
            for id in 0..n {
                s.enqueue(id).unwrap().unwrap();
            }
            assert_eq!(s.maximum_depth, n as u64);
            let old = s;
            assert_eq!(s.enqueue(0), Err(DUPLICATE));
            assert_eq!(old, s);
            for _ in 0..1000 {
                if s.finished != 0 {
                    break;
                }
                for id in 0..n {
                    if s.pending[id] == 0 {
                        s.enqueue(id).unwrap();
                    }
                }
                if s.ordinary_due() {
                    let id = s.begin_ordinary().unwrap();
                    assert_eq!(s.pending[id as usize], 0);
                    let old = s;
                    assert_eq!(s.finish_ordinary(id), Err(BAD));
                    assert_eq!(old, s);
                    s.closing = 1;
                    s.finish_ordinary(id).unwrap();
                } else if s.start_head().unwrap().is_some() {
                    for id in 0..n {
                        assert_eq!(s.complete_head(), Err(BAD));
                        s.acknowledge(id).unwrap();
                    }
                    s.complete_head().unwrap();
                }
            }
            assert_eq!(s.finished, 1);
            assert_eq!(s.total, 9 * n as u64);
            assert!(s.ordinary_total >= n as u64 + 1);
            assert!(s.intervals[..n].iter().all(|v| *v > 0));
        }
    }
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct Baseline {
    pub frame: NativeFrame,
    pub old_memory: [u8; 256],
    pub new_memory: [u8; 256],
}
