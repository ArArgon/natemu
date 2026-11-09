//! Modern virtio-MMIO, bounded devices: one active guest request, owned snapshots and payloads.
//! Register the device before boot, run its owner alongside the vCPU owners, call `stop`, and
//! join it before detaching IRQs or destroying the VM. No backend call holds a RAM grant.
mod block;
mod net;
mod queue;
pub use net::PacketPort;
#[cfg(feature = "testing")]
pub mod testing;
use crate::{
    Result,
    devices::{self, Access},
    execution::{
        Backing, Stops,
        io::{CopyPermit, Transfer},
    },
    irq::{Busy, Irqs},
    vm::Machine,
};
use queue::{Queue, Snapshot};
use std::{
    collections::VecDeque,
    sync::{
        Arc, Condvar, Mutex, MutexGuard,
        atomic::{AtomicBool, Ordering::SeqCst},
    },
    time::Duration,
};

const VERSION_1: u64 = 1 << 32;
const RNG_MAX_QUEUE: u16 = 8;
const MAX_PAYLOAD: usize = 256;
const DRIVER_OK: u32 = 4;
const FEATURES_OK: u32 = 8;
const NEEDS_RESET: u32 = 64;
/// One resource declaration shared by discovery and the MMIO registry.
pub const RNG_RESOURCE: crate::boot::acpi::VirtioMmio = crate::boot::acpi::VirtioMmio {
    uid: 2,
    base: 0xf000_2000,
    gsi: 18,
};

#[derive(Clone, Copy)]
enum Reset {
    Queue(usize),
    Device,
}
impl Reset {
    fn affects(self, id: Identity) -> bool {
        match self {
            Self::Device => true,
            Self::Queue(q) => q == id.queue,
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Identity {
    queue: usize,
    device: u64,
    generation: u64,
}
struct Control {
    token: u64,
    vcpu: usize,
    offset: u64,
    value: Option<u64>,
    applied: bool,
}
struct State {
    status: u32,
    device_sel: u32,
    driver_sel: u32,
    features: u64,
    queue_sel: u32,
    queues: [Queue; 2],
    generations: [u64; 2],
    interrupt: u32,
    generation: u64,
    notified: [bool; 2],
    active: Option<Identity>,
    cursor: usize,
    quiescing: bool,
    drained: bool,
    closing: Option<Reset>,
    stopped: bool,
    permit: Option<Arc<CopyPermit>>,
    controls: VecDeque<Control>,
    token: u64,
    busy: Option<Busy>,
    received: Option<(Identity, net::Packet)>,
    backend_error: Option<String>,
}
impl Default for State {
    fn default() -> Self {
        Self {
            status: 0,
            device_sel: 0,
            driver_sel: 0,
            features: 0,
            queue_sel: 0,
            queues: [Queue::default(); 2],
            generations: [0; 2],
            interrupt: 0,
            generation: 0,
            notified: [false; 2],
            active: None,
            cursor: 0,
            quiescing: false,
            drained: false,
            closing: None,
            stopped: false,
            permit: None,
            controls: VecDeque::new(),
            token: 0,
            busy: None,
            received: None,
            backend_error: None,
        }
    }
}

impl State {
    fn identity(&self, queue: usize) -> Identity {
        Identity {
            queue,
            device: self.generation,
            generation: self.generations[queue],
        }
    }
    fn current(&self, id: Identity) -> bool {
        !self.stopped
            && self.identity(id.queue) == id
            && !self.closing.is_some_and(|r| r.affects(id))
    }
    fn pending(&self) -> bool {
        self.notified.iter().any(|v| *v)
    }
}

/// A production virtio device with up to two queues. Clones share one register/IRQ ordering domain; exactly one
/// independent owner calls `run`. The MMIO adapter retains no guest-memory authority.
pub struct Virtio {
    profile: Profile,
    state: Mutex<State>,
    wake: Condvar,
    irqs: Irqs,
    gsi: usize,
    running: AtomicBool,
}
enum Profile {
    Entropy,
    Block(block::Disk),
    Network(net::Network),
}
impl Profile {
    fn queue_count(&self) -> usize {
        if matches!(self, Self::Network(_)) {
            2
        } else {
            1
        }
    }
    fn queue_max(&self) -> u16 {
        match self {
            Self::Entropy => RNG_MAX_QUEUE,
            Self::Block(_) => 128,
            Self::Network(_) => 256,
        }
    }
    fn features(&self) -> u64 {
        match self {
            Self::Entropy => VERSION_1,
            Self::Block(_) => block::FEATURES,
            Self::Network(_) => net::FEATURES,
        }
    }
    fn required(&self) -> u64 {
        match self {
            Self::Entropy | Self::Network(_) => VERSION_1,
            Self::Block(_) => block::REQUIRED,
        }
    }
}
pub const BLOCK_RESOURCE: crate::boot::acpi::VirtioMmio = crate::boot::acpi::VirtioMmio {
    uid: 0,
    base: 0xf000_0000,
    gsi: 16,
};
pub const NET_RESOURCE: crate::boot::acpi::VirtioMmio = crate::boot::acpi::VirtioMmio {
    uid: 1,
    base: 0xf000_1000,
    gsi: 17,
};
impl Virtio {
    pub fn rng(irqs: Irqs, gsi: usize) -> Arc<Self> {
        Self::new(irqs, gsi, Profile::Entropy)
    }
    pub fn block(irqs: Irqs, gsi: usize, path: &std::path::Path) -> Result<Arc<Self>> {
        Ok(Self::new(
            irqs,
            gsi,
            Profile::Block(block::Disk::open(path)?),
        ))
    }
    fn new(irqs: Irqs, gsi: usize, profile: Profile) -> Arc<Self> {
        assert!((16..24).contains(&gsi));
        Arc::new(Self {
            profile,
            state: Mutex::new(State::default()),
            wake: Condvar::new(),
            irqs,
            gsi,
            running: AtomicBool::new(false),
        })
    }
    /// Install a finite native-call schedule before the test driver starts the VM.
    #[cfg(feature = "testing")]
    pub fn test_control(&self, control: Arc<testing::Control>) -> Result<()> {
        if self.running.load(SeqCst) {
            return Err("test schedule installed after owner start".into());
        }
        let Profile::Block(disk) = &self.profile else {
            return Err("test schedule requires disk".into());
        };
        *disk.control.lock().unwrap_or_else(|e| e.into_inner()) = Some(control);
        Ok(())
    }
    #[cfg(feature = "testing")]
    fn observe(&self, name: &str) {
        if let Profile::Block(disk) = &self.profile {
            if let Some(control) = &*disk.control.lock().unwrap_or_else(|e| e.into_inner()) {
                control.event(name);
            }
        }
    }
    pub fn device(self: &Arc<Self>) -> devices::Shared {
        Arc::new(Mutex::new(Mmio(self.clone())))
    }
    fn lock(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }
    fn irq(&self, s: &State) {
        self.irqs.line(self.gsi, s.interrupt != 0);
    }
    fn busy(&self, s: &mut State) {
        if s.busy.is_none() {
            s.busy = Some(self.irqs.busy());
        }
    }
    /// Stop admission and cancel ungranted windows. A granted copy/backend call retains its
    /// storage until the owner returns. The caller must join `run` before destroying the VM.
    pub fn stop(&self) {
        let mut s = self.lock();
        s.stopped = true;
        s.notified = [false; 2];
        s.received = None;
        if let Some(p) = &s.permit {
            p.cancel();
        }
        self.wake.notify_all();
    }
    fn quiesce(&self) -> Result<bool> {
        let mut s = self.lock();
        s.quiescing = true;
        s.notified = [false; 2];
        s.received = None;
        self.wake.notify_all();
        Ok(s.drained)
    }
    fn retire_reset(&self, s: &mut State) {
        let Some(reset) = s.closing else {
            return;
        };
        if s.active.is_some_and(|id| reset.affects(id)) {
            return;
        }
        s.closing = None;
        match reset {
            Reset::Device => {
                s.generation = s
                    .generation
                    .checked_add(1)
                    .expect("virtio generation exhausted");
                s.queues = [Queue::default(); 2];
                s.notified = [false; 2];
                s.cursor = 0;
                s.received = None;
                s.status = 0;
                s.features = 0;
                s.device_sel = 0;
                s.driver_sel = 0;
                s.queue_sel = 0;
                s.interrupt = 0;
            }
            Reset::Queue(i) => {
                s.generations[i] = s.generations[i]
                    .checked_add(1)
                    .expect("virtio queue generation exhausted");
                s.queues[i] = Queue {
                    ready: false,
                    next_avail: 0,
                    next_used: 0,
                    ..s.queues[i]
                };
                s.notified[i] = false;
                if i == 0 {
                    s.received = None;
                }
            }
        }
        self.irq(s);
        if !s.pending() && s.active.is_none() {
            s.busy = None;
        }
    }
    fn reset(&self, s: &mut State, reset: Reset) -> Access {
        s.closing = Some(reset);
        match reset {
            Reset::Device => s.notified = [false; 2],
            Reset::Queue(i) => s.notified[i] = false,
        }
        if s.active.is_some_and(|id| reset.affects(id)) {
            if let Some(p) = &s.permit {
                p.cancel();
            }
        }
        self.retire_reset(s);
        self.wake.notify_all();
        if s.closing.is_some() {
            Access::Deferred(0)
        } else {
            Access::Completed(0)
        }
    }
    fn fail(&self, s: &mut State) {
        s.status |= NEEDS_RESET;
        s.notified = [false; 2];
        if s.status & DRIVER_OK != 0 {
            s.interrupt |= 2;
        }
        self.irq(s);
    }
    /// One register access under the same lock as physical IRQ publication. Only reset/disable
    /// can defer here; the outer control queue orders conflicting accesses behind retirement.
    fn register(&self, s: &mut State, offset: u64, value: Option<u64>) -> Access {
        let selected = s.queue_sel as usize;
        let valid_queue = selected < self.profile.queue_count();
        let Some(value) = value else {
            let value = match offset {
                0x00 => 0x74726976,
                0x04 => 2,
                0x08 => match self.profile {
                    Profile::Entropy => 4,
                    Profile::Block(_) => 2,
                    Profile::Network(_) => 1,
                },
                0x0c => 0x4e415445,
                0x10 => {
                    if s.device_sel < 2 {
                        (self.profile.features() >> (s.device_sel * 32)) as u32
                    } else {
                        0
                    }
                }
                0x34 => {
                    if valid_queue {
                        self.profile.queue_max() as u32
                    } else {
                        0
                    }
                }
                0x44 => u32::from(valid_queue && s.queues[selected].ready),
                0x60 => s.interrupt,
                0x70 => s.status,
                0xfc => 0,
                _ => 0,
            };
            return Access::Completed(value.into());
        };
        let value = value as u32; // caller validated the full access width
        // Poweroff already owns lifecycle ordering. Peer writes cannot cancel an accepted drain.
        if s.quiescing && offset != 0x64 {
            return Access::Completed(0);
        }
        match offset {
            0x14 => s.device_sel = value,
            0x24 => s.driver_sel = value,
            0x20 if s.status & FEATURES_OK == 0 => {
                if s.driver_sel < 2 {
                    let shift = s.driver_sel * 32;
                    s.features = (s.features & !(u64::from(u32::MAX) << shift))
                        | (u64::from(value) << shift);
                }
            }
            0x30 => s.queue_sel = value,
            0x38 | 0x80 | 0x84 | 0x90 | 0x94 | 0xa0 | 0xa4 if valid_queue => {
                if s.queues[selected].ready || s.active.is_some_and(|id| id.queue == selected) {
                    return Access::Refused;
                }
                if offset == 0x38 {
                    if value == 0
                        || value > self.profile.queue_max().into()
                        || !value.is_power_of_two()
                    {
                        self.fail(s);
                    } else {
                        s.queues[selected].size = value as u16;
                    }
                } else {
                    let field = match offset & !4 {
                        0x80 => &mut s.queues[selected].desc,
                        0x90 => &mut s.queues[selected].avail,
                        _ => &mut s.queues[selected].used,
                    };
                    let shift = if offset & 4 == 0 { 0 } else { 32 };
                    *field =
                        (*field & !(u64::from(u32::MAX) << shift)) | (u64::from(value) << shift);
                }
            }
            0x44 if valid_queue => {
                if value == 0 {
                    return self.reset(s, Reset::Queue(selected));
                }
                if value != 1
                    || s.status & FEATURES_OK == 0
                    || !s.queues[selected].valid(self.profile.queue_max())
                {
                    self.fail(s);
                } else if !s.queues[selected].ready {
                    s.queues[selected].ready = true;
                }
            }
            0x50 if !s.quiescing
                && (value as usize) < self.profile.queue_count()
                && s.queues[value as usize].ready
                && s.status & (DRIVER_OK | NEEDS_RESET | 128) == DRIVER_OK =>
            {
                // RX buffers alone are not work. Packet arrival schedules the scan, even without a new kick.
                if value != 0
                    || !matches!(self.profile, Profile::Network(_))
                    || s.received.is_some()
                {
                    s.notified[value as usize] = true;
                    self.busy(s);
                    self.wake.notify_one();
                }
            }
            0x64 => {
                s.interrupt &= !(value & 3);
                self.irq(s);
            }
            0x70 => {
                if value == 0 {
                    return self.reset(s, Reset::Device);
                }
                if value & !0x8f != 0
                    || value & s.status != s.status
                    || value & 2 != 0 && value & 1 == 0
                    || value & FEATURES_OK != 0 && value & 3 != 3
                    || value & DRIVER_OK != 0
                        && (value & FEATURES_OK == 0 || s.status & FEATURES_OK == 0)
                {
                    self.fail(s);
                } else {
                    s.status = value;
                    if value & FEATURES_OK != 0
                        && (s.features & !self.profile.features() != 0
                            || s.features & self.profile.required() != self.profile.required())
                    {
                        s.status &= !FEATURES_OK;
                    }
                    if value & 128 != 0 {
                        s.notified = [false; 2];
                    }
                }
            }
            _ => {}
        }
        Access::Completed(0)
    }
    fn access(&self, vcpu: usize, offset: u64, size: u64, value: Option<u64>) -> Access {
        if let Profile::Network(network) = &self.profile {
            if (0x100..0x10c).contains(&offset) {
                if value.is_some()
                    || !matches!(size, 1 | 2 | 4)
                    || offset % size != 0
                    || offset + size > 0x10c
                {
                    return Access::Refused;
                }
                let bytes = network.config();
                let mut word = [0; 8];
                word[..size as usize].copy_from_slice(
                    &bytes[(offset - 0x100) as usize..(offset - 0x100 + size) as usize],
                );
                return Access::Completed(u64::from_le_bytes(word));
            }
        }
        if let Profile::Block(disk) = &self.profile {
            if (0x100..0x118).contains(&offset) {
                if value.is_some()
                    || !matches!(size, 1 | 2 | 4)
                    || offset % size != 0
                    || offset + size > 0x118
                {
                    return Access::Refused;
                }
                let bytes = disk.config();
                let mut word = [0; 8];
                word[..size as usize].copy_from_slice(
                    &bytes[(offset - 0x100) as usize..(offset - 0x100 + size) as usize],
                );
                return Access::Completed(u64::from_le_bytes(word));
            }
        }
        if size != 4 || offset % 4 != 0 || value.is_some_and(|v| v > u32::MAX as u64) {
            return Access::Refused;
        }
        let mut s = self.lock();
        if s.stopped {
            return Access::Refused;
        }
        // There can be only one outstanding access per vCPU. Reserve its control slot before
        // applying a deferred lifecycle effect; resource refusal must leave the device unchanged.
        if s.controls.len() >= natemu_abi::execution::cache::MAX_VCPUS
            || s.controls.iter().any(|c| c.vcpu == vcpu)
            || s.token == u64::MAX
        {
            return Access::Refused;
        }
        let result = if s.closing.is_some() || !s.controls.is_empty() {
            Access::Deferred(0)
        } else {
            self.register(&mut s, offset, value)
        };
        if matches!(result, Access::Deferred(_)) {
            s.token += 1;
            let token = s.token;
            let applied = s.controls.is_empty() && s.closing.is_some();
            s.controls.push_back(Control {
                token,
                vcpu,
                offset,
                value,
                applied,
            });
            #[cfg(feature = "testing")]
            if applied && value == Some(0) {
                if offset == 0x70 {
                    self.observe("reset-deferred");
                }
                if offset == 0x44 {
                    self.observe("disable-deferred");
                }
            }
            Access::Deferred(token)
        } else {
            result
        }
    }
    fn poll(&self, vcpu: usize, token: u64) -> Access {
        let mut s = self.lock();
        let Some(c) = s
            .controls
            .iter()
            .find(|c| c.token == token && c.vcpu == vcpu)
        else {
            return Access::Refused;
        };
        if s.closing.is_some() || s.controls.front().unwrap().token != token {
            return Access::Deferred(token);
        }
        let (offset, value, applied) = (c.offset, c.value, c.applied);
        let result = if applied {
            Access::Completed(0)
        } else {
            self.register(&mut s, offset, value)
        };
        if matches!(result, Access::Deferred(_)) {
            s.controls.front_mut().unwrap().applied = true;
            Access::Deferred(token)
        } else {
            s.controls.pop_front();
            self.wake.notify_one();
            result
        }
    }
    fn current(&self, generation: Identity) -> bool {
        self.lock().current(generation)
    }
    fn copy(
        &self,
        backing: &Backing,
        machine: &Machine,
        stops: &Stops,
        generation: Identity,
        spans: &mut [Transfer<'_>],
    ) -> Result<bool> {
        let permit = Arc::new(CopyPermit::new());
        {
            let mut s = self.lock();
            if !s.current(generation) {
                return Ok(false);
            }
            s.permit = Some(permit.clone());
        }
        let copied =
            backing.copy_guest_with_permit(machine, stops, Duration::from_secs(2), spans, &permit);
        let mut s = self.lock();
        s.permit = None;
        match copied {
            Ok(()) => Ok(true), // even a reset that lost the grant must retire this committed effect
            Err(_) if !s.current(generation) => Ok(false),
            Err(e) => Err(e),
        }
    }
    /// Run once on an independent scoped host thread. Cancellation/stop closes admission; the
    /// method joins normal disk shutdown before returning. Fatal disk timeout/cancellation may
    /// retain an owned-only lane until its syscall retires or the VM worker process exits.
    /// No returning path retains guest RAM authority. Errors cancel VM owners.
    pub fn run(
        &self,
        backing: &Backing,
        machine: &Machine,
        stops: &Stops,
        cancel: &AtomicBool,
    ) -> Result<()> {
        if self.running.swap(true, SeqCst) {
            return Err("virtio owner already started".into());
        }
        let result = self.owner(backing, machine, stops, cancel);
        if result.is_err() {
            crate::owners::cancel_owners(cancel);
        }
        let mut s = self.lock();
        s.stopped = !s.drained;
        s.active = None;
        s.permit = None;
        s.notified = [false; 2];
        s.busy = None;
        s.interrupt = 0;
        self.irq(&s);
        result
    }
    fn drain(&self, lane: Option<block::Lane>, cancel: &AtomicBool) -> Result<()> {
        if let Some(mut disk) = lane {
            disk.submit(block::Job::Flush)?;
            let done = disk.wait(|| cancel.load(SeqCst) || self.lock().stopped)?;
            if let Some(error) = done.error {
                return Err(format!("disk shutdown full-sync: {error}"));
            }
            disk.finish()?;
            #[cfg(feature = "testing")]
            self.observe("lane-joined");
        }
        let mut s = self.lock();
        if let Some(error) = &s.backend_error {
            return Err(format!("network shutdown: {error}"));
        }
        s.notified = [false; 2];
        s.busy = None;
        s.interrupt = 0;
        self.irq(&s);
        s.drained = true;
        Ok(())
    }
    fn owner(
        &self,
        backing: &Backing,
        machine: &Machine,
        stops: &Stops,
        cancel: &AtomicBool,
    ) -> Result<()> {
        let mut snapshot = Snapshot::new(self.profile.queue_max())?;
        let mut lane = match &self.profile {
            Profile::Entropy | Profile::Network(_) => None,
            Profile::Block(disk) => Some(disk.start()?),
        };
        loop {
            let (q, generation, received) = {
                let mut s = self.lock();
                loop {
                    if s.stopped || cancel.load(SeqCst) {
                        drop(s);
                        return lane.take().map_or(Ok(()), |lane| lane.finish());
                    }
                    if let Some(error) = &s.backend_error {
                        return Err(format!("network backend: {error}"));
                    }
                    if s.quiescing {
                        drop(s);
                        return self.drain(lane.take(), cancel);
                    }
                    if s.closing.is_none() && s.controls.is_empty() {
                        let count = self.profile.queue_count();
                        if let Some(i) = (0..count)
                            .map(|n| (s.cursor + n) % count)
                            .find(|i| s.notified[*i])
                        {
                            s.notified[i] = false;
                            let id = s.identity(i);
                            s.active = Some(id);
                            s.cursor = (i + 1) % count;
                            let received = if i == 0 {
                                s.received
                                    .take()
                                    .filter(|(tag, _)| *tag == id)
                                    .map(|(_, p)| p)
                            } else {
                                None
                            };
                            break (s.queues[i], id, received);
                        }
                    }
                    s = self
                        .wake
                        .wait_timeout(s, Duration::from_millis(10))
                        .unwrap_or_else(|e| e.into_inner())
                        .0;
                }
            };
            let result = self.request(
                backing,
                machine,
                stops,
                &mut snapshot,
                q,
                generation,
                &mut lane,
                received,
                cancel,
            );
            let mut s = self.lock();
            s.active = None;
            match result {
                Ok(Request::Committed { pending }) => {
                    self.retire_request(&mut s, &snapshot, q, generation, pending)
                }
                Ok(Request::Invalid(reason)) => {
                    snapshot.discard();
                    eprintln!("virtio: {reason}");
                    if s.current(generation) {
                        self.fail(&mut s);
                    }
                }
                Ok(Request::Empty | Request::Canceled) => {
                    snapshot.discard();
                }
                Err(e) => return Err(e),
            }
            self.retire_reset(&mut s);
            if !s.pending() {
                s.busy = None;
            }
        }
    }
    fn retire_request(
        &self,
        s: &mut State,
        snapshot: &Snapshot,
        q: Queue,
        id: Identity,
        pending: bool,
    ) {
        // A granted completion retires once even if reset arrived; reset clears its old queue afterward.
        s.queues[id.queue].next_avail = q.next_avail.wrapping_add(1);
        s.queues[id.queue].next_used = q.next_used.wrapping_add(1);
        if !(id.queue == 0 && matches!(self.profile, Profile::Network(_)))
            && s.current(id)
            && !s.quiescing
            && s.queues[id.queue].ready
            && s.status & (DRIVER_OK | NEEDS_RESET | 128) == DRIVER_OK
        {
            // Keep both notifications arriving after admission and already-owned heads even if
            // completion writes aliased the live available index. This does not admit another request.
            s.notified[id.queue] |= pending || snapshot.reusable(s.queues[id.queue], id);
        }
    }
    fn request(
        &self,
        backing: &Backing,
        machine: &Machine,
        stops: &Stops,
        snapshot: &mut Snapshot,
        q: Queue,
        generation: Identity,
        lane: &mut Option<block::Lane>,
        received: Option<net::Packet>,
        cancel: &AtomicBool,
    ) -> Result<Request> {
        if matches!(self.profile, Profile::Network(_))
            && generation.queue == 0
            && received.is_none()
        {
            return Ok(Request::Empty);
        }
        if !q.valid(self.profile.queue_max()) {
            return Ok(Request::Invalid("invalid queue geometry".into()));
        }
        let table_len = usize::from(q.size) * 16;
        let avail_len = 4 + usize::from(q.size) * 2;
        // Validation only: no raw pointer is dereferenced or retained by this device.
        if backing
            .ram
            .range(q.used, 4 + u64::from(q.size) * 8)
            .is_none()
            || backing.ram.range(q.desc, table_len as u64).is_none()
            || backing.ram.range(q.avail, avail_len as u64).is_none()
        {
            return Ok(Request::Invalid("queue metadata outside RAM".into()));
        }
        if !snapshot.reusable(q, generation) {
            if !self.copy(
                backing,
                machine,
                stops,
                generation,
                &mut [
                    Transfer::Read {
                        gpa: q.desc,
                        bytes: &mut snapshot.table[..table_len],
                    },
                    Transfer::Read {
                        gpa: q.avail,
                        bytes: &mut snapshot.avail[..avail_len],
                    },
                ],
            )? {
                return Ok(Request::Canceled);
            }
            snapshot.captured(generation);
        }
        if !self.current(generation) {
            return Ok(Request::Canceled);
        }
        let (head, chain) = match snapshot.chain(q) {
            Ok(Some(c)) => c,
            Ok(None) => return Ok(Request::Empty),
            Err(e) => return Ok(Request::Invalid(e)),
        };
        if chain
            .iter()
            .any(|d| d.len() != 0 && backing.ram.range(d.addr().0, d.len().into()).is_none())
        {
            return Ok(Request::Invalid("descriptor outside RAM".into()));
        }
        if let Profile::Block(disk) = &self.profile {
            return self.block_request(
                backing,
                machine,
                stops,
                q,
                generation,
                head,
                &chain,
                disk,
                lane.as_mut().unwrap(),
                cancel,
            );
        }
        if let Profile::Network(network) = &self.profile {
            return self.net_request(
                backing, machine, stops, q, generation, head, &chain, network, received, cancel,
            );
        }
        if chain.iter().any(|d| !d.is_write_only()) {
            return Ok(Request::Invalid("entropy chain direction".into()));
        }
        let len = chain
            .iter()
            .map(|d| d.len() as usize)
            .sum::<usize>()
            .min(MAX_PAYLOAD);
        if len == 0 {
            return Ok(Request::Invalid("empty entropy chain".into()));
        }
        let mut payload = [0u8; MAX_PAYLOAD];
        if !self.current(generation) {
            return Ok(Request::Canceled);
        }
        // Native host entropy fills only owned storage, outside the memory window and every lock.
        unsafe extern "C" {
            fn getentropy(buffer: *mut u8, size: usize) -> i32;
        }
        // SAFETY: the owned writable array contains `len` bytes, at most getentropy's 256-byte limit.
        if unsafe { getentropy(payload.as_mut_ptr(), len) } != 0 {
            return Ok(Request::Invalid(format!(
                "getentropy: {}",
                std::io::Error::last_os_error()
            )));
        }
        let mut used = [0u8; 8];
        used[..4].copy_from_slice(&u32::from(head).to_le_bytes());
        used[4..].copy_from_slice(&(len as u32).to_le_bytes());
        let index = q.next_used.wrapping_add(1).to_le_bytes();
        let mut flags = [0u8; 2];
        let mut writes = Vec::with_capacity(chain.len() + 3);
        let mut offset = 0;
        for d in &chain {
            let count = (d.len() as usize).min(len - offset);
            if count != 0 {
                writes.push(Transfer::Write {
                    gpa: d.addr().0,
                    bytes: &payload[offset..offset + count],
                });
                offset += count;
            }
        }
        writes.push(Transfer::Write {
            gpa: q.used + 4 + u64::from(q.next_used % q.size) * 8,
            bytes: &used,
        });
        writes.push(Transfer::Write {
            gpa: q.used + 2,
            bytes: &index,
        });
        // Read suppression after publication, including when guest buffers alias ring metadata.
        writes.push(Transfer::Read {
            gpa: q.avail,
            bytes: &mut flags,
        });
        if !self.copy(backing, machine, stops, generation, &mut writes)? {
            return Ok(Request::Canceled);
        }
        // ACK, reset and level publication share this lock. An old granted commit is retired but
        // cannot offer an interrupt after reset closed its incarnation.
        let mut s = self.lock();
        if s.current(generation) && u16::from_le_bytes(flags) & 1 == 0 {
            s.interrupt |= 1;
            self.irq(&s);
        }
        Ok(Request::Committed { pending: true })
    }
}
enum Request {
    Empty,
    Canceled,
    Committed { pending: bool },
    Invalid(String),
}
struct Mmio(Arc<Virtio>);
impl devices::Device for Mmio {
    fn quiesce(&mut self) -> Result<bool> {
        self.0.quiesce()
    }
    fn read(&mut self, _: usize, _: u64, _: u64) -> Option<u64> {
        None
    }
    fn write(&mut self, _: usize, _: u64, _: u64, _: u64) -> bool {
        false
    }
    fn access(&mut self, v: usize, o: u64, n: u64, value: Option<u64>) -> Access {
        self.0.access(v, o, n, value)
    }
    fn poll(&mut self, v: usize, token: u64) -> Access {
        self.0.poll(v, token)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    pub(super) fn device() -> Arc<Virtio> {
        let memory = Arc::new(crate::memory::Memory::with_size(4096).unwrap());
        let mut devices = devices::Devices::new(64 << 20);
        let uart = Arc::new(Mutex::new(crate::uart::Uart::new()));
        let irqs = crate::irq::install(&mut devices, &memory, 0, &uart).unwrap();
        Virtio::rng(irqs, RNG_RESOURCE.gsi as usize)
    }
    fn write(r: &Virtio, offset: u64, value: u64) {
        assert!(matches!(
            r.access(0, offset, 4, Some(value)),
            Access::Completed(0)
        ));
    }
    fn read(r: &Virtio, offset: u64) -> u64 {
        match r.access(0, offset, 4, None) {
            Access::Completed(v) => v,
            _ => panic!("register read not complete"),
        }
    }
    fn negotiated(r: &Virtio) {
        write(r, 0x70, 1);
        write(r, 0x70, 3);
        write(r, 0x24, 1);
        write(r, 0x20, 1);
        write(r, 0x70, 11);
        assert_eq!(read(r, 0x70), 11);
    }
    #[test]
    fn completion_preserves_later_notifications_and_captured_work_across_wrap() {
        for wrap in [false, true] {
            for pending in [false, true] {
                for notify in [false, true] {
                    for captured in [false, true] {
                        let r = device();
                        let q = Queue {
                            size: 8,
                            ready: true,
                            next_avail: if wrap { u16::MAX } else { 3 },
                            next_used: 17,
                            ..Default::default()
                        };
                        let id = {
                            let mut s = r.lock();
                            s.status = DRIVER_OK;
                            s.queues[0] = q;
                            s.notified[1] = true;
                            s.identity(0)
                        };
                        let mut snapshot = Snapshot::new(8).unwrap();
                        snapshot.avail[2..4].copy_from_slice(
                            &q.next_avail
                                .wrapping_add(1 + u16::from(captured))
                                .to_le_bytes(),
                        );
                        snapshot.captured(id);
                        // A real QueueNotify after completion's observation must survive owner retirement.
                        if notify {
                            write(&r, 0x50, 0);
                        }
                        let mut s = r.lock();
                        r.retire_request(&mut s, &snapshot, q, id, pending);
                        assert_eq!(s.notified, [pending || notify || captured, true]);
                        assert_eq!(s.queues[0].next_avail, q.next_avail.wrapping_add(1));
                        assert_eq!(s.queues[0].next_used, 18);
                    }
                }
            }
        }
    }
    #[test]
    fn completion_rearm_obeys_lifecycle_and_snapshot_identity() {
        for gate in 0..7 {
            let r = device();
            let q = Queue {
                size: 8,
                ready: true,
                ..Default::default()
            };
            let mut s = r.lock();
            s.status = DRIVER_OK;
            s.queues[0] = q;
            let id = s.identity(0);
            let mut snapshot = Snapshot::new(8).unwrap();
            snapshot.avail[2] = 2;
            snapshot.captured(id);
            match gate {
                0 => s.quiescing = true,
                1 => s.closing = Some(Reset::Device),
                2 => s.closing = Some(Reset::Queue(0)),
                3 => s.stopped = true,
                4 => s.queues[0].ready = false,
                5 => s.status |= NEEDS_RESET,
                _ => s.generation += 1,
            }
            r.retire_request(&mut s, &snapshot, q, id, true);
            assert!(!s.notified[0]);
            assert_eq!((s.queues[0].next_avail, s.queues[0].next_used), (1, 1));
        }
        let r = device();
        let q = Queue {
            size: 8,
            ready: true,
            ..Default::default()
        };
        let mut s = r.lock();
        s.status = DRIVER_OK;
        s.queues[0] = q;
        let id = s.identity(0);
        let mut snapshot = Snapshot::new(8).unwrap();
        snapshot.avail[2] = 2;
        snapshot.captured(Identity {
            device: id.device + 1,
            ..id
        });
        r.retire_request(&mut s, &snapshot, q, id, false);
        assert!(!s.notified[0]);
    }
    #[test]
    fn power_quiesce_preserves_accepted_work_and_ignores_later_reset_notify() {
        let r = device();
        negotiated(&r);
        {
            let mut s = r.lock();
            s.active = Some(s.identity(0));
            s.notified[0] = true;
            s.queues[0].ready = true;
            s.status |= DRIVER_OK;
        }
        assert!(!r.quiesce().unwrap());
        write(&r, 0x50, 0);
        write(&r, 0x70, 0);
        write(&r, 0x44, 0);
        let s = r.lock();
        assert!(s.active.is_some() && s.queues[0].ready && s.quiescing);
        assert!(!s.pending() && s.closing.is_none() && !s.drained && !s.stopped);
        assert_ne!(s.status, 0);
    }
    #[test]
    fn full_selectors_and_feature_negotiation_do_not_truncate() {
        let r = device();
        write(&r, 0x14, 0x1_0000);
        assert_eq!(read(&r, 0x10), 0);
        write(&r, 0x14, 1);
        assert_eq!(read(&r, 0x10), 1);
        write(&r, 0x30, 0x1_0000);
        assert_eq!(read(&r, 0x34), 0);
        write(&r, 0x70, 3);
        write(&r, 0x70, 11);
        assert_eq!(read(&r, 0x70), 3); // missing mandatory VERSION_1
        negotiated(&device());
        assert!(matches!(r.access(0, 0x70, 8, Some(0)), Access::Refused));
        assert_eq!(read(&r, 0x70), 3); // refusal made no reset effect
    }
    #[test]
    fn queue_geometry_fails_device_without_scheduling_work() {
        let r = device();
        negotiated(&r);
        write(&r, 0x38, 65544); // would truncate to an apparently valid eight-entry queue
        assert_ne!(read(&r, 0x70) & NEEDS_RESET as u64, 0);
        assert!(!r.lock().pending());
        write(&r, 0x70, 0);
        negotiated(&r);
        write(&r, 0x38, 8);
        write(&r, 0x80, 1);
        write(&r, 0x44, 1);
        assert_eq!(read(&r, 0x44), 0);
        assert_ne!(read(&r, 0x70) & NEEDS_RESET as u64, 0);
    }
    #[test]
    fn reset_cancels_pending_copy_and_orders_conflicting_read() {
        let r = device();
        negotiated(&r);
        let permit = Arc::new(CopyPermit::new());
        {
            let mut s = r.lock();
            s.active = Some(s.identity(0));
            s.queues[0].ready = true;
            s.permit = Some(permit.clone());
        }
        let token = match r.access(0, 0x70, 4, Some(0)) {
            Access::Deferred(t) => t,
            _ => panic!("reset did not defer"),
        };
        assert!(permit.cancel()); // reset already won the pending permit
        let read_token = match r.access(1, 0x70, 4, None) {
            Access::Deferred(t) => t,
            _ => panic!("conflicting read passed reset"),
        };
        assert!(matches!(r.poll(0, token), Access::Deferred(t) if t == token));
        {
            let mut s = r.lock();
            s.active = None;
            s.permit = None;
            r.retire_reset(&mut s);
        }
        assert!(matches!(r.poll(1, read_token), Access::Deferred(_))); // FIFO: original access retires first
        assert!(matches!(r.poll(0, token), Access::Completed(0)));
        assert!(matches!(r.poll(1, read_token), Access::Completed(0)));
        assert!(matches!(r.poll(0, token), Access::Refused)); // no replay
        assert_eq!(r.lock().generation, 1);
    }
    #[test]
    fn queue_disable_readback_and_ack_share_serialized_state() {
        let r = device();
        negotiated(&r);
        write(&r, 0x38, 8);
        write(&r, 0x80, 0x1000);
        write(&r, 0x90, 0x2000);
        write(&r, 0xa0, 0x3000);
        write(&r, 0x44, 1);
        assert_eq!(read(&r, 0x44), 1);
        write(&r, 0x44, 0);
        assert_eq!(read(&r, 0x44), 0);
        assert_eq!(r.lock().queues[0].desc, 0x1000);
        {
            let mut s = r.lock();
            s.interrupt = 3;
            r.irq(&s);
        }
        write(&r, 0x64, 1);
        assert_eq!(read(&r, 0x60), 2);
        write(&r, 0x64, 2);
        assert_eq!(read(&r, 0x60), 0);
    }
}
