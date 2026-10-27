//! Opt-in, bounded scheduling evidence for the progress fixtures.
//!
//! `eligible` describes protocol-eligible owners, not OS thread readiness. A
//! chosen owner may not have reached the cut yet. Other callers must release
//! their coordinator guard before retrying; the scheduler never waits itself.
use crate::{PRIVATE, concurrency::MAX_CPUS, families::maintenance};

pub const MAGIC: u64 = 0x5343_4845_4400_0001;
pub const VERSION: u64 = 1;
pub const OFFSET: usize = 0x1e0000;
pub const VA: u64 = PRIVATE + OFFSET as u64;
pub const BACKING_OFFSET: usize = 0xe0000;
pub const BYTES: usize = 0x20000;
pub const SEEDS: [u64; 4] = [1, 37, 0x12345678, 0x9e3779b9];
pub const STACK: u64 = 0;
pub const CONTROLS: u64 = 1;
pub const COHERENCE: u64 = 2;
pub const ATOMIC: u64 = 3;
pub const PARTIAL: u64 = 4;
pub const MAX_CUTS: usize = 256;
pub const MAX_DECISIONS: usize = 768;
pub const MAX_POOL_EVENTS: usize = 128;
pub const NONE: u64 = u64::MAX;
pub const BAD: u64 = 810;
pub const EXHAUSTED: u64 = 811;
// Stable cut namespaces. The low 32 bits contain the visit/epoch supplied by
// the caller; singleton/early/late cohorts use distinct low values.
pub const ADMIT: u64 = 1 << 32;
pub const DETACH: u64 = 2 << 32;
pub const ACK: u64 = 3 << 32;
pub const RELEASE: u64 = 4 << 32;
pub const MAINTENANCE_ACK: u64 = 5 << 32;
pub const SOURCE_ACK: u64 = 6 << 32;
pub const SOURCE_RELEASE: u64 = 7 << 32;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(C)]
pub struct Config {
    pub family: u64,
    pub n: u64,
    pub actor: u64,
    pub seed_ordinal: u64,
    pub seed: u64,
    /// Actual maintenance capacity for stack/controls; retained-route capacity
    /// N for the other families. Exhaustion is only claimed for the former.
    pub pool_limit: u64,
}
impl Config {
    pub fn new(family: u64, n: u64, ordinal: u64) -> Option<Self> {
        if family > PARTIAL
            || n == 0
            || n > MAX_CPUS as u64
            || ordinal >= if family == PARTIAL { 2 } else { 4 }
        {
            return None;
        }
        let pool_limit = if family <= CONTROLS && n > 1 {
            1 + ordinal % (n - 1).min(2)
        } else {
            n
        };
        Some(Self {
            family,
            n,
            actor: (family + ordinal) % n,
            seed_ordinal: ordinal,
            seed: SEEDS[ordinal as usize],
            pool_limit,
        })
    }
    pub fn valid(self) -> bool {
        Self::new(self.family, self.n, self.seed_ordinal) == Some(self)
    }
    /// Ordering data tokens reserve 16 bits for the seed; scheduling keeps all 32.
    pub fn token_seed(self) -> u64 {
        if self.family == ATOMIC {
            (self.seed & 0xffff).max(1)
        } else {
            0
        }
    }
    pub fn all(self) -> u64 {
        (1 << self.n) - 1
    }
    pub fn pool_applicable(self) -> bool {
        self.family <= CONTROLS && self.n > 1
    }
    pub fn profile(self) -> u64 {
        match self.family {
            STACK => 1,
            CONTROLS => {
                if self.seed_ordinal & 1 == 0 {
                    21
                } else {
                    24
                }
            }
            COHERENCE => match self.seed_ordinal {
                0 | 2 => 0,
                1 => 4,
                _ => 5,
            },
            _ => 0,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(C)]
pub struct Cut {
    pub key: u64,
    pub cohort: u64,
    pub arrived: u64,
    pub completed: u64,
    pub chosen: u64,
    pub decision: u64,
}
impl Cut {
    pub const ZERO: Self = Self {
        key: 0,
        cohort: 0,
        arrived: 0,
        completed: 0,
        chosen: NONE,
        decision: NONE,
    };
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(C)]
pub struct Decision {
    pub cut: u64,
    pub phase: u64,
    pub epoch: u64,
    pub cohort: u64,
    pub eligible: u64,
    pub arrived: u64,
    pub done_before: u64,
    pub prng_before: u64,
    pub prng_after: u64,
    pub draw: u64,
    pub chosen: u64,
    pub claimed: u64,
    pub trace_before: u64,
    pub claim_trace_before: u64,
}
impl Decision {
    pub const ZERO: Self = Self {
        cut: 0,
        phase: 0,
        epoch: 0,
        cohort: 0,
        eligible: 0,
        arrived: 0,
        done_before: 0,
        prng_before: 0,
        prng_after: 0,
        draw: 0,
        chosen: 0,
        claimed: 0,
        trace_before: 0,
        claim_trace_before: 0,
    };
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(C)]
pub struct PoolEvent {
    pub owner: u64,
    pub ordinal: u64,
    pub capacity: u64,
    pub depth: u64,
    pub epoch: u64,
    pub result: u64,
    pub serial: u64,
    pub before_digest: u64,
    pub after_digest: u64,
    pub trace_before: u64,
}
impl PoolEvent {
    pub const ZERO: Self = Self {
        owner: 0,
        ordinal: 0,
        capacity: 0,
        depth: 0,
        epoch: 0,
        result: 0,
        serial: 0,
        before_digest: 0,
        after_digest: 0,
        trace_before: 0,
    };
}

#[repr(C)]
pub struct State {
    pub magic: u64,
    pub version: u64,
    pub config: Config,
    pub prng: u64,
    pub draws: u64,
    pub cut_count: u64,
    pub decision_count: u64,
    pub pool_event_count: u64,
    pub full_count: u64,
    pub readmitted: u64,
    pub full: [u64; MAX_CPUS],
    pub retried: [u64; MAX_CPUS],
    pub cuts: [Cut; MAX_CUTS],
    pub decisions: [Decision; MAX_DECISIONS],
    pub pool_events: [PoolEvent; MAX_POOL_EVENTS],
    /// Zero for healthy schedules; containment::MAGIC opts into failure hooks.
    pub containment: u64,
}
impl State {
    pub fn new(config: Config) -> Option<Self> {
        if !config.valid() {
            return None;
        }
        Some(Self {
            magic: MAGIC,
            version: VERSION,
            config,
            prng: config.seed,
            draws: 0,
            cut_count: 0,
            decision_count: 0,
            pool_event_count: 0,
            full_count: 0,
            readmitted: 0,
            full: [0; MAX_CPUS],
            retried: [0; MAX_CPUS],
            cuts: [Cut::ZERO; MAX_CUTS],
            decisions: [Decision::ZERO; MAX_DECISIONS],
            pool_events: [PoolEvent::ZERO; MAX_POOL_EVENTS],
            containment: 0,
        })
    }
    /// Call under the same authority that protects the following action.
    /// A true result consumes this owner's grant exactly once.
    pub fn permit(
        &mut self,
        id: u64,
        key: u64,
        cohort: u64,
        eligible: u64,
        phase: u64,
        epoch: u64,
    ) -> Result<bool, u64> {
        if self.magic != MAGIC
            || self.version != VERSION
            || !self.config.valid()
            || id >= self.config.n
            || key == 0
            || cohort == 0
            || self.cut_count as usize > MAX_CUTS
            || self.decision_count as usize > MAX_DECISIONS
            || cohort & !self.config.all() != 0
            || eligible & !cohort != 0
            || cohort & (1 << id) == 0
        {
            return Err(BAD);
        }
        let at = match self.cuts[..self.cut_count as usize]
            .iter()
            .position(|c| c.key == key)
        {
            Some(at) => at,
            None => {
                if self.cut_count as usize >= MAX_CUTS {
                    return Err(EXHAUSTED);
                }
                let at = self.cut_count as usize;
                self.cut_count += 1;
                self.cuts[at] = Cut {
                    key,
                    cohort,
                    ..Cut::ZERO
                };
                at
            }
        };
        let c = &mut self.cuts[at];
        if c.cohort != cohort || c.completed & (1 << id) != 0 {
            return Err(BAD);
        }
        c.arrived |= 1 << id;
        let available = eligible & !c.completed;
        if c.chosen == NONE {
            if available == 0 {
                return Ok(false);
            }
            if self.decision_count as usize >= MAX_DECISIONS {
                return Err(EXHAUSTED);
            }
            let before = self.prng;
            self.prng = xorshift32(before);
            let rank = self.prng % available.count_ones() as u64;
            let chosen = nth_owner(available, rank).ok_or(BAD)?;
            let d = self.decision_count as usize;
            self.decisions[d] = Decision {
                cut: key,
                phase,
                epoch,
                cohort,
                eligible: available,
                arrived: c.arrived,
                done_before: c.completed,
                prng_before: before,
                prng_after: self.prng,
                draw: self.draws,
                chosen,
                claimed: 0,
                trace_before: 0,
                claim_trace_before: 0,
            };
            self.draws += 1;
            self.decision_count += 1;
            c.chosen = chosen;
            c.decision = d as u64;
        }
        // Eligibility cannot disappear while a pending grant is protected by
        // the protocol. Treat such a fixture bug as failure, not resampling.
        if available & (1 << c.chosen) == 0 {
            return Err(BAD);
        }
        if id != c.chosen {
            return Ok(false);
        }
        self.decisions[c.decision as usize].claimed = 1;
        c.completed |= 1 << id;
        c.chosen = NONE;
        c.decision = NONE;
        Ok(true)
    }
    /// The real maintenance queue enforces the limit, including the in-flight
    /// head. A FULL result is nonmutating and is retried with (owner,ordinal).
    #[inline(never)]
    pub fn enqueue(
        &mut self,
        queue: &mut maintenance::State,
        id: usize,
    ) -> Result<Option<maintenance::Request>, u64> {
        if id >= self.config.n as usize || queue.n != self.config.n || self.config.family > CONTROLS
        {
            return Err(BAD);
        }
        // Do not clone the large maintenance record on the resident exception
        // stack. The queue method rejects FULL before any write; tests compare
        // the complete record, and the journal binds its field-wise digest.
        let before_digest = queue_digest(queue);
        let depth = queue.length + u64::from(queue.current.serial != 0);
        let epoch = queue.epoch;
        let ordinal = queue.submitted[id] + 1;
        let result = queue.enqueue_limit(id, self.config.pool_limit);
        let record = match result {
            Err(maintenance::FULL) => {
                if queue_digest(queue) != before_digest || ordinal > maintenance::REQUESTS {
                    return Err(BAD);
                }
                let bit = 1 << (ordinal - 1);
                if self.full[id] & bit != 0 {
                    false
                } else {
                    self.full[id] |= bit;
                    self.full_count += 1;
                    true
                }
            }
            Ok(Some(_)) => {
                let bit = 1 << (ordinal - 1);
                if self.full[id] & bit != 0 {
                    self.retried[id] |= bit;
                    self.readmitted += 1;
                }
                true
            }
            _ => false,
        };
        if record {
            if self.pool_event_count as usize >= MAX_POOL_EVENTS {
                return Err(EXHAUSTED);
            }
            self.pool_events[self.pool_event_count as usize] = PoolEvent {
                owner: id as u64,
                ordinal,
                capacity: self.config.pool_limit,
                depth,
                epoch,
                result: if result.is_err() {
                    maintenance::FULL
                } else {
                    0
                },
                serial: match result {
                    Ok(Some(r)) => r.serial,
                    _ => 0,
                },
                before_digest,
                after_digest: queue_digest(queue),
                trace_before: 0,
            };
            self.pool_event_count += 1;
        }
        result
    }
}
pub fn xorshift32(value: u64) -> u64 {
    let mut x = value as u32;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    x as u64
}
pub fn nth_owner(mask: u64, mut rank: u64) -> Option<u64> {
    for id in 0..MAX_CPUS {
        if mask & (1 << id) != 0 {
            if rank == 0 {
                return Some(id as u64);
            }
            rank -= 1;
        }
    }
    None
}
pub fn queue_digest(s: &maintenance::State) -> u64 {
    let mut h = 0xcbf29ce484222325u64;
    let mut add = |v: u64| {
        h = (h ^ v).wrapping_mul(0x100000001b3);
    };
    for v in [
        s.n,
        s.ready,
        s.serial,
        s.head,
        s.length,
        s.current.cpu,
        s.current.serial,
        s.epoch,
        s.acknowledgements,
        s.total,
        s.batch,
        s.next_ordinary,
        s.ordinary,
        s.ordinary_total,
        s.closing,
        s.finished,
        s.duplicate_rejections,
        s.maximum_depth,
        s.compose_leaf,
    ] {
        add(v);
    }
    for r in s.queue.iter().chain(s.request_history.iter()) {
        add(r.cpu);
        add(r.serial);
    }
    for a in [
        &s.pending[..],
        &s.submitted[..],
        &s.completed[..],
        &s.intervals[..],
        &s.leaf_history[..],
    ] {
        for &v in a {
            add(v);
        }
    }
    h
}
const _: () = assert!(core::mem::size_of::<State>() <= BYTES);

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn seeds_actors_and_capacity_are_explicit() {
        for family in 0..=PARTIAL {
            for n in 1..=4 {
                for ordinal in 0..4 {
                    let c = Config::new(family, n, ordinal);
                    if family == PARTIAL && ordinal >= 2 {
                        assert!(c.is_none());
                        continue;
                    }
                    let c = c.unwrap();
                    assert!(c.valid());
                    assert_eq!(c.actor, (family + ordinal) % n);
                    assert_eq!(c.seed, SEEDS[ordinal as usize]);
                    if family == ATOMIC {
                        assert_eq!(c.token_seed(), [1, 37, 0x5678, 0x79b9][ordinal as usize]);
                        assert!(
                            crate::families::ordering::Config::new(n, 9, c.actor, c.token_seed()).is_some()
                        );
                    } else {
                        assert_eq!(c.token_seed(), 0);
                    }
                    assert_eq!(c.pool_limit < n, c.pool_applicable());
                }
            }
        }
        let mut bad = Config::new(STACK, 3, 1).unwrap();
        bad.actor = 2;
        assert!(!bad.valid());
    }
    #[test]
    fn grants_are_replayable_and_not_caller_selected() {
        let mut a = State::new(Config::new(STACK, 4, 0).unwrap()).unwrap();
        assert_eq!(xorshift32(1), 270369);
        // First draw chooses owner1; owner0 cannot consume that grant.
        assert!(!a.permit(0, ADMIT, 15, 15, 0, 1).unwrap());
        assert_eq!(a.decisions[0].chosen, 1);
        assert_eq!(a.draws, 1);
        assert!(a.permit(1, ADMIT, 15, 15, 0, 1).unwrap());
        assert_eq!(a.permit(1, ADMIT, 15, 15, 0, 1), Err(BAD));
        while a.cuts[0].completed != 15 {
            for id in 0..4 {
                if a.cuts[0].completed & (1 << id) == 0 {
                    let _ = a.permit(id, ADMIT, 15, 15, 0, 1).unwrap();
                }
            }
        }
        let mut p = 1;
        let mut done = 0;
        for d in &a.decisions[..4] {
            assert_eq!(d.prng_before, p);
            p = xorshift32(p);
            assert_eq!(d.prng_after, p);
            assert_eq!(d.eligible, 15 ^ done);
            assert_eq!(
                nth_owner(d.eligible, p % d.eligible.count_ones() as u64),
                Some(d.chosen)
            );
            assert_eq!(d.claimed, 1);
            done |= 1 << d.chosen;
        }
    }
    #[test]
    fn pending_grant_cannot_be_silently_resampled() {
        let mut s = State::new(Config::new(STACK, 4, 0).unwrap()).unwrap();
        assert!(!s.permit(0, ACK, 15, 15, 2, 3).unwrap());
        assert_eq!(s.permit(0, ACK, 15, 13, 2, 3), Err(BAD));
    }
    #[test]
    fn bounded_queue_full_preserves_identity_then_readmits() {
        let mut s = State::new(Config::new(STACK, 3, 0).unwrap()).unwrap();
        let mut q = maintenance::State::new(3).unwrap();
        q.ready = 7;
        assert_eq!(s.enqueue(&mut q, 0).unwrap().unwrap().serial, 1);
        let before = q;
        assert_eq!(s.enqueue(&mut q, 1), Err(maintenance::FULL));
        assert_eq!(q, before);
        let rejected = s.pool_events[1];
        assert_eq!(rejected.before_digest, rejected.after_digest);
        q.start_head().unwrap();
        for id in 0..3 {
            q.acknowledge(id).unwrap();
        }
        q.complete_head().unwrap();
        assert_eq!(s.enqueue(&mut q, 1).unwrap().unwrap().serial, 2);
        assert_eq!(s.pool_events[2].ordinal, rejected.ordinal);
        assert_eq!(s.full_count, 1);
        assert_eq!(s.readmitted, 1);
        assert_eq!(q.maximum_depth, 1);
    }
    #[test]
    fn every_bounded_pool_drains_without_an_all_admitted_barrier() {
        for family in [STACK, CONTROLS] {
            for n in 1..=4 {
                for ordinal in 0..4 {
                    let config = Config::new(family, n, ordinal).unwrap();
                    let mut s = State::new(config).unwrap();
                    let mut q = maintenance::State::new(n as usize).unwrap();
                    // Readiness includes owners which have no queue slot yet.
                    q.ready = config.all();
                    for _ in 0..2000 {
                        if q.finished != 0 {
                            break;
                        }
                        for id in 0..n as usize {
                            if q.pending[id] == 0 && q.ordinary != id as u64 {
                                match s.enqueue(&mut q, id) {
                                    Ok(_) | Err(maintenance::FULL) => (),
                                    other => panic!("unexpected enqueue {other:?}"),
                                }
                            }
                        }
                        let started = q.maximum_depth >= config.pool_limit
                            && (!config.pool_applicable() || s.full_count != 0);
                        if started {
                            if q.ordinary_due() {
                                let id = q.begin_ordinary().unwrap();
                                q.closing = 1;
                                q.finish_ordinary(id).unwrap();
                            } else if q.start_head().unwrap().is_some() {
                                for id in 0..n as usize {
                                    q.acknowledge(id).unwrap();
                                }
                                q.complete_head().unwrap();
                            }
                        }
                        assert!(q.length + u64::from(q.current.serial != 0) <= config.pool_limit);
                        assert!(q.batch <= 8);
                    }
                    assert_eq!(q.finished, 1, "family={family} n={n} ordinal={ordinal}");
                    assert_eq!(q.total, 9 * n);
                    assert_eq!(q.completed, expected_completed(n as usize));
                    assert_eq!(q.maximum_depth, config.pool_limit);
                    assert_eq!(s.full, s.retried);
                    assert_eq!(s.full_count, s.readmitted);
                    assert_eq!(s.full_count != 0, config.pool_applicable());
                    assert!(
                        q.intervals
                            .iter()
                            .all(|&v| v <= maintenance::MAX_INTERVALS as u64)
                    );
                }
            }
        }
    }
    fn expected_completed(n: usize) -> [u64; 4] {
        // Returned fixed storage avoids pulling an allocator into the ABI.
        let mut expected = [0; 4];
        for item in &mut expected[..n] {
            *item = maintenance::REQUESTS;
        }
        expected
    }
}
