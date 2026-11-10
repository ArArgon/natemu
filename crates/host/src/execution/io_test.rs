//! Opt-in private scheduling controls, absent from ordinary host builds. No guest RAM access.
use super::*;
pub use cache::io_test::*;
fn word(memory: &Memory, v: usize, at: usize) -> &AtomicU64 {
    global_u64(memory, BASE + v * STRIDE + at)
}
/// One-shot tagged hold; one driver controller owns arming/release. Drop releases every hold.
pub struct Hooks<'a> {
    backing: &'a Backing,
    sequence: std::cell::Cell<u64>,
}
impl<'a> Hooks<'a> {
    pub fn new(backing: &'a Backing) -> Self {
        Self {
            backing,
            sequence: std::cell::Cell::new(
                (0..=HOST)
                    .map(|v| word(&backing.memory, v, RELEASE).load(SeqCst) >> 8)
                    .max()
                    .unwrap_or(0),
            ),
        }
    }
    pub fn arm(&self, v: usize, point: u64) -> Result<u64> {
        if v > HOST
            || !matches!(
                point,
                BEFORE_QUIET
                    | AFTER_WITHDRAW
                    | AFTER_ACK
                    | BEFORE_SPLIT
                    | BEFORE_GRANT
                    | AFTER_GRANT
                    | AFTER_PRECHECK
                    | BEFORE_RECHECK
                    | BEFORE_PUBLISH
            )
        {
            return Err("invalid I/O test hook".into());
        }
        let sequence = self.sequence.get() + 1;
        self.sequence.set(sequence);
        let tag = sequence * 256 + point;
        if word(&self.backing.memory, v, ARM)
            .compare_exchange(0, tag, SeqCst, SeqCst)
            .is_err()
        {
            return Err("I/O test hook already armed".into());
        }
        Ok(tag)
    }
    pub fn get(&self, v: usize, at: usize) -> u64 {
        assert!(v <= HOST && at < STRIDE && at % 8 == 0);
        word(&self.backing.memory, v, at).load(SeqCst)
    }
    pub fn release(&self, v: usize, tag: u64) {
        word(&self.backing.memory, v, RELEASE).fetch_max(tag, SeqCst);
    }
    pub fn shadows(&self, v: usize) -> bool {
        record_word(&self.backing.memory, v, None).load(SeqCst) & cache::SHADOWS != 0
    }
    pub fn resets(&self, v: usize) -> u64 {
        global_u64(&self.backing.memory, cache::RESETS + v * 8).load(SeqCst)
    }
    pub fn holding(&self, v: usize) -> u64 {
        global_u64(&self.backing.memory, cache::SPLIT + (3 + v) * 8).load(SeqCst)
    }
    pub fn abort(&self) {
        for v in 0..=HOST {
            word(&self.backing.memory, v, ARM).store(0, SeqCst);
            word(&self.backing.memory, v, RELEASE)
                .fetch_max(self.sequence.get() * 256 + 255, SeqCst);
        }
    }
}
impl Drop for Hooks<'_> {
    fn drop(&mut self) {
        self.abort();
    }
}
pub(super) fn point(memory: &Memory, point: u64) {
    let tag = word(memory, HOST, ARM).load(SeqCst);
    if tag & 255 != point
        || word(memory, HOST, ARM)
            .compare_exchange(tag, 0, SeqCst, SeqCst)
            .is_err()
    {
        return;
    }
    word(memory, HOST, HITS).fetch_add(1, SeqCst);
    word(memory, HOST, REACHED).store(tag, SeqCst);
    let began = Instant::now();
    while word(memory, HOST, RELEASE).load(SeqCst) < tag {
        if began.elapsed() > Duration::from_secs(5) {
            word(memory, HOST, TIMEOUT).store(tag, SeqCst);
            break;
        }
        std::thread::yield_now();
    }
}
