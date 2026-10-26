//! Resident ownership transitions. All non-rendezvous updates hold this lock;
//! callers drop it before waiting, entering generated code or servicing a fault.
use core::sync::atomic::Ordering::{Acquire, Relaxed, Release};
use natemu_abi::concurrency::*;

pub struct Guard<'a>(&'a Coordinator);
impl Drop for Guard<'_> {
    fn drop(&mut self) {
        self.0.lock.fetch_add(1, Release);
    }
}
pub fn lock(c: &Coordinator) -> Guard<'_> {
    // FIFO handoff prevents a hot polling owner from repeatedly overtaking the
    // peer whose acknowledgement or ordinary interval it is waiting for.
    let ticket = c.next_ticket.fetch_add(1, Relaxed);
    while c.lock.load(Acquire) != ticket {
        core::hint::spin_loop();
    }
    Guard(c)
}
impl Guard<'_> {
    pub fn trace_length(&self) -> u64 {
        self.0.trace_length.load(Relaxed)
    }
    pub fn trace(&self, event: u64, id: u64) -> Result<(), u64> {
        let c = self.0;
        let at = c.trace_length.load(Relaxed) as usize;
        if at >= TRACE_CAPACITY {
            return Err(90);
        }
        c.trace[at].store((event << 8) | id, Relaxed);
        c.trace_length.store((at + 1) as u64, Relaxed);
        Ok(())
    }
    pub fn admit(&self, id: u64) -> Result<(), u64> {
        let c = self.0;
        if id >= c.participants {
            return Err(91);
        }
        let bit = 1 << id;
        if id >= c.participants
            || c.phase.load(Relaxed) != OPEN
            || c.active.load(Relaxed) & bit != 0
        {
            return Err(91);
        }
        c.active.fetch_or(bit, Relaxed);
        c.retained.fetch_or(bit, Relaxed);
        self.trace(1, id)
    }
    pub fn detach_fault(&self, id: u64) -> Result<(), u64> {
        let c = self.0;
        if id >= c.participants {
            return Err(92);
        }
        let bit = 1 << id;
        let len = c.length.load(Relaxed) as usize;
        if id >= c.participants
            || c.active.load(Relaxed) & bit == 0
            || c.queued.load(Relaxed) & bit != 0
            || len >= c.participants as usize
            || c.phase.load(Relaxed) > CLOSING
        {
            return Err(92);
        }
        c.queue[len].store(id, Relaxed);
        c.length.store((len + 1) as u64, Relaxed);
        c.queued.fetch_or(bit, Relaxed);
        c.active.fetch_and(!bit, Relaxed);
        c.phase.store(CLOSING, Relaxed);
        self.trace(2, id)
    }
    pub fn select(&self) -> Result<u64, u64> {
        let c = self.0;
        if c.active.load(Relaxed) != 0 {
            return Ok(NO_CPU);
        }
        if c.phase.load(Relaxed) == CLOSING {
            c.phase.store(EXCLUSIVE, Relaxed);
            self.trace(3, 0)?;
        }
        if c.phase.load(Relaxed) != EXCLUSIVE {
            return Ok(NO_CPU);
        }
        let turn = c.servicing.load(Relaxed);
        if turn != NO_CPU {
            return Ok(turn);
        }
        let head = c.head.load(Relaxed) as usize;
        if head >= c.length.load(Relaxed) as usize {
            return Ok(NO_CPU);
        }
        let id = c.queue[head].load(Relaxed);
        c.servicing.store(id, Relaxed);
        self.trace(4, id)?;
        Ok(id)
    }
    pub fn complete(&self, id: u64) -> Result<(), u64> {
        let c = self.0;
        if c.phase.load(Relaxed) != EXCLUSIVE
            || c.servicing.load(Relaxed) != id
            || c.completed.load(Relaxed) & (1 << id) != 0
        {
            return Err(93);
        }
        c.completed.fetch_or(1 << id, Relaxed);
        c.servicing.store(NO_CPU, Relaxed);
        c.head.fetch_add(1, Relaxed);
        self.trace(5, id)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn contended_waiters_receive_ordered_grants() {
        let c = Coordinator::new(4).unwrap();
        std::thread::scope(|scope| {
            let held = lock(&c);
            for id in 0..4 {
                let shared = &c;
                scope.spawn(move || {
                    let g = lock(shared);
                    g.trace(99, id).unwrap();
                });
                while c.next_ticket.load(Acquire) != id + 2 {
                    std::thread::yield_now();
                }
            }
            drop(held);
        });
        assert_eq!(c.trace_length.load(Relaxed), 4);
        for id in 0..4 {
            assert_eq!(c.trace[id].load(Relaxed), (99 << 8) | id as u64);
        }
    }

    #[test]
    fn delayed_grant_blocks_service_and_fifo_detaches_once() {
        let c = Coordinator::new(4).unwrap();
        {
            let g = lock(&c);
            for id in 0..4 {
                g.admit(id).unwrap();
            }
        }
        {
            let g = lock(&c);
            for id in [2, 0, 3] {
                g.detach_fault(id).unwrap();
            }
            assert_eq!(g.select(), Ok(NO_CPU));
            assert!(g.detach_fault(2).is_err());
            assert!(g.admit(2).is_err());
        }
        {
            let g = lock(&c);
            g.detach_fault(1).unwrap();
            for id in [2, 0, 3, 1] {
                assert_eq!(g.select(), Ok(id));
                assert!(g.complete((id + 1) % 4).is_err());
                g.complete(id).unwrap();
                assert!(g.complete(id).is_err());
            }
            assert_eq!(g.select(), Ok(NO_CPU));
        }
        assert_eq!(c.active.load(Relaxed), 0);
        assert_eq!(c.completed.load(Relaxed), 15);
        assert_eq!(c.retained.load(Relaxed), 15);
    }
}
