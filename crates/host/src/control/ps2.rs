//! Input events to the 8042's PS/2 keyboard and mouse (`docs/restructure/control-api-plan.md`, Translation to PS/2),
//! delivered without loss.
//!
//! **Translation.** A key is its set 2 make code ([`KeyCode::set2`], Microsoft's "USB HID to PS/2 Scan Code
//! Translation Table") and its break, F0 before the make's last byte (`Pause` has none). A pointer motion is split into
//! packets of at most 255 per axis (the mouse clamps beyond its 9 bits; each axis is clamped on its own, so the path
//! bends while the final position is exact) with dy negated (the API's dy is positive
//! downward, PS/2's upward); a scroll is packets whose wheel byte is the scroll's dy (at most 127 each), which the mouse
//! sends only in IntelliMouse mode. Every packet carries the buttons the guest holds.
//!
//! **Held state** ([`Held`]) is the server's, since the 8042 has one keyboard and one button mask: each key has the
//! set of clients holding it (a make when the first takes it, a break when the last lets go) and each client a button
//! mask, whose OR the guest sees. Beside it, what the guest was sent: the keys whose make reached the 8042 and the mask
//! of the last packet. Whatever differs between the two is owed to the guest and sent before anything else.
//!
//! **Delivery** ([`Input`]): a queue per device, drained by its own thread (`control-keyboard`, `control-mouse`) into
//! the 8042 one whole key or packet at a time through `Ps2Input::wait_keyboard` and `wait_mouse`, which wait for room
//! as the guest reads 60H. A client's `send` queues one event at a time and waits for it, so its events reach the
//! guest in order across both devices, and clients interleave per event, never inside a key or a packet. When a
//! device takes nothing for the stall time (5 s, [`STALL`]), the event in progress and every event queued on that
//! device fail with [`SendError::Stalled`] (-32003) and are discarded, and the held state stays consistent: a press
//! whose make was not delivered is undone (the guest never saw it), a release is kept and its break stays owed, sent
//! once the guest reads again (the thread retries while nothing else is queued). So no key is left down in the guest
//! that no client holds, and a client's leaving ([`Input::release`]) sends only the transitions its leaving causes.
use super::protocol::{Error, ErrorCode, InputEvent, KeyCode, MouseButton};
use crate::ps2::{Ps2Input, LEFT, MIDDLE, RIGHT};
use std::collections::{BTreeMap, BTreeSet, HashMap, VecDeque};
use std::sync::atomic::{AtomicBool, Ordering::SeqCst};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

/// How long a device may take nothing before `send_input` fails with -32003.
pub const STALL: Duration = Duration::from_secs(5);
/// While the guest is owed a transition and no event waits, how long each retry waits for room.
const RETRY: Duration = Duration::from_millis(50);

/// A client of the server (its connection), for the held state.
pub type ClientId = u64;

impl KeyCode {
    /// The key's set 2 make code (E0-prefixed keys with their E0), from the set 2 column of Microsoft's "USB HID to
    /// PS/2 Scan Code Translation Table" (PrintScreen is the table's E0 7C, without the modifier-dependent codes of
    /// its Note 1).
    pub fn set2(self) -> &'static [u8] {
        use KeyCode::*;
        match self {
            KeyA => &[0x1c],
            KeyB => &[0x32],
            KeyC => &[0x21],
            KeyD => &[0x23],
            KeyE => &[0x24],
            KeyF => &[0x2b],
            KeyG => &[0x34],
            KeyH => &[0x33],
            KeyI => &[0x43],
            KeyJ => &[0x3b],
            KeyK => &[0x42],
            KeyL => &[0x4b],
            KeyM => &[0x3a],
            KeyN => &[0x31],
            KeyO => &[0x44],
            KeyP => &[0x4d],
            KeyQ => &[0x15],
            KeyR => &[0x2d],
            KeyS => &[0x1b],
            KeyT => &[0x2c],
            KeyU => &[0x3c],
            KeyV => &[0x2a],
            KeyW => &[0x1d],
            KeyX => &[0x22],
            KeyY => &[0x35],
            KeyZ => &[0x1a],
            Digit1 => &[0x16],
            Digit2 => &[0x1e],
            Digit3 => &[0x26],
            Digit4 => &[0x25],
            Digit5 => &[0x2e],
            Digit6 => &[0x36],
            Digit7 => &[0x3d],
            Digit8 => &[0x3e],
            Digit9 => &[0x46],
            Digit0 => &[0x45],
            Enter => &[0x5a],
            Escape => &[0x76],
            Backspace => &[0x66],
            Tab => &[0x0d],
            Space => &[0x29],
            Minus => &[0x4e],
            Equal => &[0x55],
            BracketLeft => &[0x54],
            BracketRight => &[0x5b],
            Backslash => &[0x5d],
            Semicolon => &[0x4c],
            Quote => &[0x52],
            Backquote => &[0x0e],
            Comma => &[0x41],
            Period => &[0x49],
            Slash => &[0x4a],
            CapsLock => &[0x58],
            ShiftLeft => &[0x12],
            ShiftRight => &[0x59],
            ControlLeft => &[0x14],
            ControlRight => &[0xe0, 0x14],
            AltLeft => &[0x11],
            AltRight => &[0xe0, 0x11],
            MetaLeft => &[0xe0, 0x1f],
            MetaRight => &[0xe0, 0x27],
            ContextMenu => &[0xe0, 0x2f],
            F1 => &[0x05],
            F2 => &[0x06],
            F3 => &[0x04],
            F4 => &[0x0c],
            F5 => &[0x03],
            F6 => &[0x0b],
            F7 => &[0x83],
            F8 => &[0x0a],
            F9 => &[0x01],
            F10 => &[0x09],
            F11 => &[0x78],
            F12 => &[0x07],
            PrintScreen => &[0xe0, 0x7c],
            ScrollLock => &[0x7e],
            Pause => &[0xe1, 0x14, 0x77, 0xe1, 0xf0, 0x14, 0xf0, 0x77],
            Insert => &[0xe0, 0x70],
            Home => &[0xe0, 0x6c],
            PageUp => &[0xe0, 0x7d],
            Delete => &[0xe0, 0x71],
            End => &[0xe0, 0x69],
            PageDown => &[0xe0, 0x7a],
            ArrowRight => &[0xe0, 0x74],
            ArrowLeft => &[0xe0, 0x6b],
            ArrowDown => &[0xe0, 0x72],
            ArrowUp => &[0xe0, 0x75],
            NumLock => &[0x77],
            NumpadDivide => &[0xe0, 0x4a],
            NumpadMultiply => &[0x7c],
            NumpadSubtract => &[0x7b],
            NumpadAdd => &[0x79],
            NumpadEnter => &[0xe0, 0x5a],
            Numpad1 => &[0x69],
            Numpad2 => &[0x72],
            Numpad3 => &[0x7a],
            Numpad4 => &[0x6b],
            Numpad5 => &[0x73],
            Numpad6 => &[0x74],
            Numpad7 => &[0x6c],
            Numpad8 => &[0x75],
            Numpad9 => &[0x7d],
            Numpad0 => &[0x70],
            NumpadDecimal => &[0x71],
            IntlBackslash => &[0x61],
            IntlRo => &[0x51],
            IntlYen => &[0x6a],
        }
    }

    /// The key's set 2 break: F0 before the make's last byte; none for `Pause`.
    pub fn set2_break(self) -> Option<Vec<u8>> {
        if self == KeyCode::Pause {
            return None;
        }
        let make = self.set2();
        let (last, prefix) = make.split_last().expect("every make has a byte");
        Some(prefix.iter().copied().chain([0xf0, *last]).collect())
    }
}

/// A pointer motion in the API's directions (dy downward) as PS/2 steps (dy upward), each axis at most 255 in
/// magnitude. Each axis is clamped on its own, so the path bends (the shorter axis is done first, the longer one goes
/// on alone) while the final position is exact. No motion is no step.
pub fn motion_steps(dx: i32, dy: i32) -> impl Iterator<Item = (i16, i16)> {
    let mut rest = (i64::from(dx), -i64::from(dy));
    std::iter::from_fn(move || {
        if rest == (0, 0) {
            return None;
        }
        let step = (rest.0.clamp(-255, 255), rest.1.clamp(-255, 255));
        rest = (rest.0 - step.0, rest.1 - step.1);
        Some((step.0 as i16, step.1 as i16))
    })
}

/// A wheel movement (dy positive downward) as PS/2 wheel bytes, each at most 127 in magnitude: the byte is the
/// scroll's dy (+1 is 01, which Linux reports as REL_WHEEL -1).
pub fn scroll_steps(dy: i32) -> impl Iterator<Item = i8> {
    let mut rest = i64::from(dy);
    std::iter::from_fn(move || {
        if rest == 0 {
            return None;
        }
        let step = rest.clamp(-127, 127);
        rest -= step;
        Some(step as i8)
    })
}

/// A button as the packet's bit.
fn bit(button: MouseButton) -> u8 {
    match button {
        MouseButton::Left => LEFT,
        MouseButton::Right => RIGHT,
        MouseButton::Middle => MIDDLE,
    }
}

/// The server-wide held state: what the clients hold, and what the guest was sent (see the module documentation).
#[derive(Default)]
pub struct Held {
    /// The clients holding each key down (`Pause` never: it has no break).
    keys: BTreeMap<KeyCode, BTreeSet<ClientId>>,
    /// The keys whose make reached the 8042 and whose break has not.
    guest_keys: BTreeSet<KeyCode>,
    /// Each client's buttons.
    buttons: BTreeMap<ClientId, u8>,
    /// The buttons of the last packet sent.
    guest_buttons: u8,
}

impl Held {
    /// A client's press or release of a key: whether what it holds changed (a second press, or a release of a key it
    /// does not hold, changes nothing).
    pub fn key(&mut self, client: ClientId, code: KeyCode, pressed: bool) -> bool {
        if code == KeyCode::Pause {
            return false;
        }
        let holders = self.keys.entry(code).or_default();
        let changed = if pressed { holders.insert(client) } else { holders.remove(&client) };
        if holders.is_empty() {
            self.keys.remove(&code);
        }
        changed
    }
    /// A client's press or release of a button: whether its mask changed.
    pub fn button(&mut self, client: ClientId, button: MouseButton, pressed: bool) -> bool {
        let mask = self.buttons.entry(client).or_default();
        let old = *mask;
        if pressed {
            *mask |= bit(button);
        } else {
            *mask &= !bit(button);
        }
        let changed = *mask != old;
        if *mask == 0 {
            self.buttons.remove(&client);
        }
        changed
    }
    /// A client leaves: it holds no key.
    pub fn release_keys(&mut self, client: ClientId) {
        self.keys.retain(|_, holders| {
            holders.remove(&client);
            !holders.is_empty()
        });
    }
    /// A client leaves: it holds no button.
    pub fn release_buttons(&mut self, client: ClientId) {
        self.buttons.remove(&client);
    }
    /// The next key transition the guest is owed: a break for a key it has down that nobody holds, or a make for a key
    /// held that it does not have down (true), in key code order.
    pub fn owed_key(&self) -> Option<(KeyCode, bool)> {
        let brk = self.guest_keys.iter().find(|k| !self.keys.contains_key(k)).map(|&k| (k, false));
        brk.or_else(|| self.keys.keys().find(|k| !self.guest_keys.contains(k)).map(|&k| (k, true)))
    }
    /// A key transition reached the 8042.
    pub fn key_sent(&mut self, code: KeyCode, down: bool) {
        if down {
            self.guest_keys.insert(code);
        } else {
            self.guest_keys.remove(&code);
        }
    }
    /// The buttons the guest should see: every client's, ORed.
    pub fn mask(&self) -> u8 {
        self.buttons.values().fold(0, |m, b| m | b)
    }
    /// The mask the guest is owed, if its last packet's differs.
    pub fn owed_buttons(&self) -> Option<u8> {
        Some(self.mask()).filter(|&m| m != self.guest_buttons)
    }
    /// A packet with `mask` reached the 8042.
    pub fn buttons_sent(&mut self, mask: u8) {
        self.guest_buttons = mask;
    }
}

/// Why `Input::send` did not deliver everything.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SendError {
    /// The device took nothing for the stall time (-32003): this event and the rest were discarded.
    Stalled,
    /// The run is stopping (-32002).
    Stopping,
}

impl From<SendError> for Error {
    fn from(e: SendError) -> Error {
        Error::new(match e {
            SendError::Stalled => ErrorCode::InputStalled,
            SendError::Stopping => ErrorCode::Stopping,
        })
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Device {
    Keyboard = 0,
    Mouse = 1,
}

impl Device {
    fn of(event: &InputEvent) -> Device {
        match event {
            InputEvent::Key { .. } => Device::Keyboard,
            _ => Device::Mouse,
        }
    }
}

enum Work {
    Event(InputEvent),
    /// The client left.
    Release,
}

struct Job {
    /// Where the result goes; none for a release, which nobody waits for.
    ticket: Option<u64>,
    client: ClientId,
    work: Work,
}

#[derive(Default)]
struct State {
    held: Held,
    queues: [VecDeque<Job>; 2],
    /// Results of finished events, by ticket.
    done: HashMap<u64, Result<(), SendError>>,
    next: u64,
}

struct Inner {
    ps2: Ps2Input,
    stall: Duration,
    /// Set (under `state`'s lock) once the delivery stops.
    stop: AtomicBool,
    state: Mutex<State>,
    /// Notified when a job is queued, an event finishes or the delivery stops.
    changed: Condvar,
}

/// The server's input delivery into the 8042 (see the module documentation): one per server, shared by its clients.
/// Dropping it stops it.
pub struct Input {
    inner: Arc<Inner>,
    threads: Mutex<Vec<JoinHandle<()>>>,
}

impl Input {
    /// Starts the keyboard's and the mouse's delivery threads; `stall` is how long a device may take nothing before
    /// events fail ([`STALL`] in the server; shorter in tests).
    pub fn start(ps2: Ps2Input, stall: Duration) -> Input {
        let inner = Arc::new(Inner {
            ps2,
            stall,
            stop: AtomicBool::new(false),
            state: Mutex::new(State::default()),
            changed: Condvar::new(),
        });
        let threads = [(Device::Keyboard, "control-keyboard"), (Device::Mouse, "control-mouse")]
            .map(|(device, name)| {
                let inner = inner.clone();
                std::thread::Builder::new()
                    .name(name.into())
                    .spawn(move || inner.run(device))
                    .expect("spawn an input thread")
            })
            .into();
        Input { inner, threads: Mutex::new(threads) }
    }

    /// Delivers `client`'s events in order, each once the previous one is wholly in the 8042, and returns once the
    /// last is: the calling (client) thread waits, never a vCPU. On an error the events not yet delivered are
    /// discarded.
    pub fn send(&self, client: ClientId, events: &[InputEvent]) -> Result<(), SendError> {
        let inner = &*self.inner;
        for &event in events {
            let mut st = inner.lock();
            if inner.stop.load(SeqCst) {
                return Err(SendError::Stopping);
            }
            let ticket = st.next;
            st.next += 1;
            st.queues[Device::of(&event) as usize].push_back(Job {
                ticket: Some(ticket),
                client,
                work: Work::Event(event),
            });
            inner.changed.notify_all();
            loop {
                if let Some(result) = st.done.remove(&ticket) {
                    result?;
                    break;
                }
                st = inner.changed.wait(st).unwrap_or_else(|e| e.into_inner());
            }
        }
        Ok(())
    }

    /// `client` left: the keys and buttons it held are let go, and the guest is sent the breaks and the button mask
    /// its leaving causes (after any event of its still queued). Returns at once.
    pub fn release(&self, client: ClientId) {
        let inner = &*self.inner;
        let mut st = inner.lock();
        if inner.stop.load(SeqCst) {
            return;
        }
        for queue in &mut st.queues {
            queue.push_back(Job { ticket: None, client, work: Work::Release });
        }
        inner.changed.notify_all();
    }

    /// Stops the delivery: events in progress or queued fail with [`SendError::Stopping`], later ones too; the
    /// threads are joined.
    pub fn stop(&self) {
        {
            let _st = self.inner.lock();
            self.inner.stop.store(true, SeqCst);
            self.inner.changed.notify_all();
        }
        self.inner.ps2.wake();
        let threads = std::mem::take(&mut *self.threads.lock().unwrap_or_else(|e| e.into_inner()));
        for t in threads {
            let _ = t.join();
        }
    }
}

impl Drop for Input {
    fn drop(&mut self) {
        self.stop();
    }
}

impl Inner {
    fn lock(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// A device's thread: takes its queue's jobs in order; while none waits and the guest is owed a transition,
    /// retries it.
    fn run(&self, device: Device) {
        let mut st = self.lock();
        loop {
            if self.stop.load(SeqCst) {
                let queue = std::mem::take(&mut st.queues[device as usize]);
                for t in queue.into_iter().filter_map(|j| j.ticket) {
                    st.done.insert(t, Err(SendError::Stopping));
                }
                self.changed.notify_all();
                return;
            }
            if let Some(job) = st.queues[device as usize].pop_front() {
                drop(st);
                let result = self.process(device, &job);
                st = self.lock();
                if let Some(t) = job.ticket {
                    st.done.insert(t, result);
                }
                if result == Err(SendError::Stalled) {
                    // The device took nothing for the stall time: every event waiting on it fails too; a leaving
                    // client still lets go (its transitions stay owed).
                    let queue = std::mem::take(&mut st.queues[device as usize]);
                    for j in queue {
                        match (j.ticket, j.work) {
                            (Some(t), _) => {
                                st.done.insert(t, Err(SendError::Stalled));
                            }
                            (None, _) => Self::let_go(&mut st.held, device, j.client),
                        }
                    }
                }
                self.changed.notify_all();
                continue;
            }
            if Self::owed(&st.held, device) {
                drop(st);
                let _ = self.settle(device, &mut (Instant::now() + RETRY));
                st = self.lock();
                continue;
            }
            st = self.changed.wait(st).unwrap_or_else(|e| e.into_inner());
        }
    }

    fn let_go(held: &mut Held, device: Device, client: ClientId) {
        match device {
            Device::Keyboard => held.release_keys(client),
            Device::Mouse => held.release_buttons(client),
        }
    }

    fn owed(held: &Held, device: Device) -> bool {
        match device {
            Device::Keyboard => held.owed_key().is_some(),
            Device::Mouse => held.owed_buttons().is_some(),
        }
    }

    /// One job: first what the guest is owed, then the job's own bytes. Each item delivered moves the deadline to the
    /// stall time from then.
    fn process(&self, device: Device, job: &Job) -> Result<(), SendError> {
        let deadline = &mut (Instant::now() + self.stall);
        self.settle(device, deadline)?;
        match job.work {
            Work::Release => {
                Self::let_go(&mut self.lock().held, device, job.client);
                self.settle(device, deadline)
            }
            Work::Event(InputEvent::Key { code: KeyCode::Pause, pressed }) => match pressed {
                true => self.keyboard(KeyCode::Pause.set2(), deadline),
                false => Ok(()),
            },
            Work::Event(InputEvent::Key { code, pressed }) => {
                let changed = self.lock().held.key(job.client, code, pressed);
                let result = self.settle(device, deadline);
                if result.is_err() && pressed && changed {
                    // The make did not reach the guest: the press is discarded.
                    self.lock().held.key(job.client, code, false);
                }
                result
            }
            Work::Event(InputEvent::Button { button, pressed }) => {
                let changed = self.lock().held.button(job.client, button, pressed);
                let result = self.settle(device, deadline);
                if result.is_err() && pressed && changed {
                    self.lock().held.button(job.client, button, false);
                }
                result
            }
            Work::Event(InputEvent::PointerMotion { dx, dy }) => {
                let mask = self.lock().held.guest_buttons;
                motion_steps(dx, dy).try_for_each(|(x, y)| self.mouse(x, y, 0, mask, deadline))
            }
            Work::Event(InputEvent::Scroll { dy }) => {
                let mask = self.lock().held.guest_buttons;
                scroll_steps(dy).try_for_each(|z| self.mouse(0, 0, z, mask, deadline))
            }
        }
    }

    /// Sends the guest every transition it is owed on `device`.
    fn settle(&self, device: Device, deadline: &mut Instant) -> Result<(), SendError> {
        loop {
            match device {
                Device::Keyboard => {
                    let Some((code, down)) = self.lock().held.owed_key() else { return Ok(()) };
                    match down {
                        true => self.keyboard(code.set2(), deadline)?,
                        false => self.keyboard(&code.set2_break().expect("a held key has a break"), deadline)?,
                    }
                    self.lock().held.key_sent(code, down);
                }
                Device::Mouse => {
                    let Some(mask) = self.lock().held.owed_buttons() else { return Ok(()) };
                    self.mouse(0, 0, 0, mask, deadline)?;
                    self.lock().held.buttons_sent(mask);
                }
            }
        }
    }

    fn outcome(&self, delivered: bool, deadline: &mut Instant) -> Result<(), SendError> {
        if delivered {
            *deadline = Instant::now() + self.stall;
            Ok(())
        } else if self.stop.load(SeqCst) {
            Err(SendError::Stopping)
        } else {
            Err(SendError::Stalled)
        }
    }

    fn keyboard(&self, set2: &[u8], deadline: &mut Instant) -> Result<(), SendError> {
        let delivered = self.ps2.wait_keyboard(set2, *deadline, &self.stop);
        self.outcome(delivered, deadline)
    }

    fn mouse(&self, dx: i16, dy: i16, dz: i8, mask: u8, deadline: &mut Instant) -> Result<(), SendError> {
        let delivered = self.ps2.wait_mouse(dx, dy, dz, mask, *deadline, &self.stop);
        self.outcome(delivered, deadline)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ps2::testing::{board, Board};
    use std::sync::atomic::{AtomicBool, Ordering::SeqCst};
    use std::time::Instant;

    const A: ClientId = 1;
    const B: ClientId = 2;

    fn key(code: KeyCode, pressed: bool) -> InputEvent {
        InputEvent::Key { code, pressed }
    }
    fn button(button: MouseButton, pressed: bool) -> InputEvent {
        InputEvent::Button { button, pressed }
    }
    fn motion(dx: i32, dy: i32) -> InputEvent {
        InputEvent::PointerMotion { dx, dy }
    }

    /// A test 8042 with XLAT off (the set 2 bytes as sent) and the mouse reporting, and the delivery started on it.
    fn setup(stall: Duration) -> (Board, Input) {
        let b = board();
        b.command_with(0x60, 0x07);
        assert_eq!(b.mouse(&[0xf4]), [0xfa]);
        let input = Input::start(b.input.clone(), stall);
        (b, input)
    }

    /// Reads the 8042 until `kbd` keyboard and `mouse` mouse bytes arrived (2 s at most), then 20 ms more for any
    /// further byte: (keyboard, mouse).
    fn collect(b: &Board, kbd: usize, mouse: usize) -> (Vec<u8>, Vec<u8>) {
        let (mut k, mut m) = (Vec::new(), Vec::new());
        let end = Instant::now() + Duration::from_secs(2);
        let mut quiet = None;
        loop {
            match b.read() {
                Some((byte, true)) => m.push(byte),
                Some((byte, false)) => k.push(byte),
                None => {
                    if k.len() >= kbd && m.len() >= mouse {
                        let q = *quiet.get_or_insert_with(|| Instant::now() + Duration::from_millis(20));
                        if Instant::now() >= q {
                            return (k, m);
                        }
                    } else if Instant::now() >= end {
                        panic!("timed out: keyboard {k:x?}, mouse {m:x?}");
                    }
                    std::thread::sleep(Duration::from_micros(200));
                }
            }
        }
    }

    /// Microsoft's "USB HID to PS/2 Scan Code Translation Table" (revised 4/2/2004), the set 2 make and break columns,
    /// transcribed by hand for each key's W3C code name (the table's key name in the comment where they differ).
    /// PrintScreen is the table's E0 7C (Note 1: the keyboard may add codes depending on modifiers; natemu sends none).
    const MICROSOFT_SET2: &[(&str, &str, &str)] = &[
        ("KeyA", "1C", "F0 1C"), ("KeyB", "32", "F0 32"), ("KeyC", "21", "F0 21"), ("KeyD", "23", "F0 23"),
        ("KeyE", "24", "F0 24"), ("KeyF", "2B", "F0 2B"), ("KeyG", "34", "F0 34"), ("KeyH", "33", "F0 33"),
        ("KeyI", "43", "F0 43"), ("KeyJ", "3B", "F0 3B"), ("KeyK", "42", "F0 42"), ("KeyL", "4B", "F0 4B"),
        ("KeyM", "3A", "F0 3A"), ("KeyN", "31", "F0 31"), ("KeyO", "44", "F0 44"), ("KeyP", "4D", "F0 4D"),
        ("KeyQ", "15", "F0 15"), ("KeyR", "2D", "F0 2D"), ("KeyS", "1B", "F0 1B"), ("KeyT", "2C", "F0 2C"),
        ("KeyU", "3C", "F0 3C"), ("KeyV", "2A", "F0 2A"), ("KeyW", "1D", "F0 1D"), ("KeyX", "22", "F0 22"),
        ("KeyY", "35", "F0 35"), ("KeyZ", "1A", "F0 1A"),
        ("Digit1", "16", "F0 16"), ("Digit2", "1E", "F0 1E"), ("Digit3", "26", "F0 26"), ("Digit4", "25", "F0 25"),
        ("Digit5", "2E", "F0 2E"), ("Digit6", "36", "F0 36"), ("Digit7", "3D", "F0 3D"), ("Digit8", "3E", "F0 3E"),
        ("Digit9", "46", "F0 46"), ("Digit0", "45", "F0 45"),
        ("Enter", "5A", "F0 5A"),        // Return
        ("Escape", "76", "F0 76"),
        ("Backspace", "66", "F0 66"),
        ("Tab", "0D", "F0 0D"),
        ("Space", "29", "F0 29"),
        ("Minus", "4E", "F0 4E"),        // -_
        ("Equal", "55", "F0 55"),        // =+
        ("BracketLeft", "54", "F0 54"),  // [{
        ("BracketRight", "5B", "F0 5B"), // ]}
        ("Backslash", "5D", "F0 5D"),    // \| (and Europe 1)
        ("Semicolon", "4C", "F0 4C"),    // ;:
        ("Quote", "52", "F0 52"),        // '"
        ("Backquote", "0E", "F0 0E"),    // `~
        ("Comma", "41", "F0 41"),        // ,<
        ("Period", "49", "F0 49"),       // .>
        ("Slash", "4A", "F0 4A"),        // /?
        ("CapsLock", "58", "F0 58"),
        ("ShiftLeft", "12", "F0 12"),
        ("ShiftRight", "59", "F0 59"),
        ("ControlLeft", "14", "F0 14"),
        ("ControlRight", "E0 14", "E0 F0 14"),
        ("AltLeft", "11", "F0 11"),
        ("AltRight", "E0 11", "E0 F0 11"),
        ("MetaLeft", "E0 1F", "E0 F0 1F"),  // Left GUI
        ("MetaRight", "E0 27", "E0 F0 27"), // Right GUI
        ("ContextMenu", "E0 2F", "E0 F0 2F"), // App
        ("F1", "05", "F0 05"), ("F2", "06", "F0 06"), ("F3", "04", "F0 04"), ("F4", "0C", "F0 0C"),
        ("F5", "03", "F0 03"), ("F6", "0B", "F0 0B"), ("F7", "83", "F0 83"), ("F8", "0A", "F0 0A"),
        ("F9", "01", "F0 01"), ("F10", "09", "F0 09"), ("F11", "78", "F0 78"), ("F12", "07", "F0 07"),
        ("PrintScreen", "E0 7C", "E0 F0 7C"),
        ("ScrollLock", "7E", "F0 7E"),
        ("Pause", "E1 14 77 E1 F0 14 F0 77", ""), // break: None
        ("Insert", "E0 70", "E0 F0 70"),
        ("Home", "E0 6C", "E0 F0 6C"),
        ("PageUp", "E0 7D", "E0 F0 7D"),
        ("Delete", "E0 71", "E0 F0 71"),
        ("End", "E0 69", "E0 F0 69"),
        ("PageDown", "E0 7A", "E0 F0 7A"),
        ("ArrowRight", "E0 74", "E0 F0 74"),
        ("ArrowLeft", "E0 6B", "E0 F0 6B"),
        ("ArrowDown", "E0 72", "E0 F0 72"),
        ("ArrowUp", "E0 75", "E0 F0 75"),
        ("NumLock", "77", "F0 77"),
        ("NumpadDivide", "E0 4A", "E0 F0 4A"),
        ("NumpadMultiply", "7C", "F0 7C"),
        ("NumpadSubtract", "7B", "F0 7B"),
        ("NumpadAdd", "79", "F0 79"),
        ("NumpadEnter", "E0 5A", "E0 F0 5A"),
        ("Numpad1", "69", "F0 69"), ("Numpad2", "72", "F0 72"), ("Numpad3", "7A", "F0 7A"),
        ("Numpad4", "6B", "F0 6B"), ("Numpad5", "73", "F0 73"), ("Numpad6", "74", "F0 74"),
        ("Numpad7", "6C", "F0 6C"), ("Numpad8", "75", "F0 75"), ("Numpad9", "7D", "F0 7D"),
        ("Numpad0", "70", "F0 70"),
        ("NumpadDecimal", "71", "F0 71"),
        ("IntlBackslash", "61", "F0 61"), // Europe 2
        ("IntlRo", "51", "F0 51"),        // Keyboard Int'l 1 (Ro)
        ("IntlYen", "6A", "F0 6A"),       // Keyboard Int'l 3 (Yen)
    ];

    fn hex(s: &str) -> Vec<u8> {
        s.split_whitespace().map(|b| u8::from_str_radix(b, 16).unwrap()).collect()
    }

    /// Every key code has a set 2 make code and break, and they are Microsoft's table's (transcribed above as data);
    /// every name in the table is a key code's wire name.
    #[test]
    fn every_keycode_has_a_set2_code() {
        assert_eq!(KeyCode::ALL.len(), MICROSOFT_SET2.len());
        assert!(KeyCode::ALL.len() >= 104 + 3, "the 104-key US layout and the Intl keys");
        for &(name, make, brk) in MICROSOFT_SET2 {
            let code: KeyCode = serde_json::from_value(serde_json::json!(name)).unwrap();
            assert_eq!(serde_json::to_value(code).unwrap(), name);
            assert_eq!(code.set2(), hex(make), "{name}'s make");
            let expected = (!brk.is_empty()).then(|| hex(brk));
            assert_eq!(code.set2_break(), expected, "{name}'s break");
        }
    }

    /// A break is F0 before the make's last byte, the E0 prefix kept: A's F0 1C, the up arrow's E0 F0 75.
    #[test]
    fn break_inserts_f0_before_last_byte() {
        assert_eq!(KeyCode::KeyA.set2_break().unwrap(), [0xf0, 0x1c]);
        assert_eq!(KeyCode::ArrowUp.set2_break().unwrap(), [0xe0, 0xf0, 0x75]);
        assert_eq!(KeyCode::PrintScreen.set2_break().unwrap(), [0xe0, 0xf0, 0x7c]);
        for &code in KeyCode::ALL.iter().filter(|&&c| c != KeyCode::Pause) {
            let make = code.set2();
            let mut expected = make[..make.len() - 1].to_vec();
            expected.extend([0xf0, make[make.len() - 1]]);
            assert_eq!(code.set2_break().unwrap(), expected, "{code:?}");
        }
    }

    /// Pause has no break: a press sends its 8-byte make every time, a release nothing.
    #[test]
    fn pause_has_no_break() {
        assert_eq!(KeyCode::Pause.set2_break(), None);
        let (b, input) = setup(STALL);
        let make = [0xe1, 0x14, 0x77, 0xe1, 0xf0, 0x14, 0xf0, 0x77];
        input.send(A, &[key(KeyCode::Pause, true), key(KeyCode::Pause, false)]).unwrap();
        assert_eq!(collect(&b, 8, 0), (make.to_vec(), vec![]));
        input.send(A, &[key(KeyCode::Pause, true)]).unwrap();
        input.release(A);
        assert_eq!(collect(&b, 8, 0), (make.to_vec(), vec![]), "no break on release either");
    }

    /// A motion beyond the 9-bit range becomes packets of at most 255 per axis (the mouse would clamp a bigger delta
    /// to 255 with the overflow bit), all of it delivered and no overflow bit set.
    #[test]
    fn motion_splits_into_nine_bit_packets() {
        let steps: Vec<_> = motion_steps(1000, 600).collect();
        assert_eq!(steps, [(255, -255), (255, -255), (255, -90), (235, 0)]);
        assert_eq!(motion_steps(-256, 0).collect::<Vec<_>>(), [(-255, 0), (-1, 0)]);
        assert_eq!(motion_steps(0, 0).count(), 0);
        let (b, input) = setup(STALL);
        input.send(A, &[motion(1000, -600)]).unwrap();
        let (k, m) = collect(&b, 0, 12);
        assert!(k.is_empty());
        assert_eq!(m, [0x08, 255, 255, 0x08, 255, 255, 0x08, 255, 90, 0x08, 235, 0]);
        input.send(A, &[motion(-1000, 0)]).unwrap();
        let (_, m) = collect(&b, 0, 12);
        // -255 is 0x101 in 9 bits: the X sign bit and 01; the rest -235 (0x115).
        assert_eq!(m, [0x18, 1, 0, 0x18, 1, 0, 0x18, 1, 0, 0x18, 0x15, 0]);
    }

    /// The API's dy is positive downward, PS/2's upward: dy 3 is a packet with -3 (Y sign bit, FD).
    #[test]
    fn dy_is_negated_for_ps2() {
        assert_eq!(motion_steps(0, 3).collect::<Vec<_>>(), [(0, -3)]);
        let (b, input) = setup(STALL);
        input.send(A, &[motion(0, 3)]).unwrap();
        assert_eq!(collect(&b, 0, 3).1, [0x08 | 0x20, 0, 0xfd]);
        input.send(A, &[motion(2, -3)]).unwrap();
        assert_eq!(collect(&b, 0, 3).1, [0x08, 2, 3]);
    }

    /// The wheel needs IntelliMouse mode: before the 200-100-80 sample-rate sequence a scroll sends nothing and packets
    /// have three bytes; after it a scroll's dy is the fourth byte as a signed byte (+1 is 01: Linux reports REL_WHEEL
    /// -1, content moving up), split into steps of at most 127.
    #[test]
    fn scroll_needs_intellimouse() {
        assert_eq!(scroll_steps(300).collect::<Vec<_>>(), [127, 127, 46]);
        assert_eq!(scroll_steps(-2).collect::<Vec<_>>(), [-2]);
        let (b, input) = setup(STALL);
        input.send(A, &[InputEvent::Scroll { dy: 1 }, motion(1, 0)]).unwrap();
        assert_eq!(collect(&b, 0, 3).1, [0x08, 1, 0], "the wheel dropped, three bytes");
        assert_eq!(b.mouse(&[0xf3, 200, 0xf3, 100, 0xf3, 80, 0xf4]), [0xfa; 7]);
        input.send(A, &[InputEvent::Scroll { dy: 1 }, InputEvent::Scroll { dy: -2 }, motion(1, 0)]).unwrap();
        assert_eq!(collect(&b, 0, 12).1, [0x08, 0, 0, 1, 0x08, 0, 0, 0xfe, 0x08, 1, 0, 0]);
        input.send(A, &[InputEvent::Scroll { dy: 300 }]).unwrap();
        assert_eq!(collect(&b, 0, 12).1, [0x08, 0, 0, 127, 0x08, 0, 0, 127, 0x08, 0, 0, 46]);
    }

    /// The held state is the server's, over all clients: A holds the left button and B moves the pointer, the button
    /// stays down; A holds KeyA, B presses and releases it, nothing is sent; A leaves, one break. B's leaving sends
    /// nothing for what B no longer holds; A's leaving lifts the button.
    #[test]
    fn held_state_is_server_wide() {
        let (b, input) = setup(STALL);
        input.send(A, &[button(MouseButton::Left, true)]).unwrap();
        input.send(B, &[motion(5, 0)]).unwrap();
        assert_eq!(collect(&b, 0, 6).1, [0x09, 0, 0, 0x09, 5, 0], "the button stays down under B's motion");
        input.send(A, &[key(KeyCode::KeyA, true)]).unwrap();
        assert_eq!(collect(&b, 1, 0).0, [0x1c]);
        input.send(B, &[key(KeyCode::KeyA, true), key(KeyCode::KeyA, false)]).unwrap();
        assert_eq!(collect(&b, 0, 0), (vec![], vec![]), "KeyA is down already and A still holds it");
        input.send(B, &[button(MouseButton::Left, true), button(MouseButton::Left, false)]).unwrap();
        assert_eq!(collect(&b, 0, 0), (vec![], vec![]), "A holds the left button");
        input.release(B);
        assert_eq!(collect(&b, 0, 0), (vec![], vec![]));
        input.release(A);
        assert_eq!(collect(&b, 2, 3), (vec![0xf0, 0x1c], vec![0x08, 0, 0]), "one break, after A's");
        // A second press by the same client is not a second hold: one release lifts it.
        input.send(A, &[key(KeyCode::KeyB, true), key(KeyCode::KeyB, true), key(KeyCode::KeyB, false)]).unwrap();
        assert_eq!(collect(&b, 3, 0).0, [0x32, 0xf0, 0x32]);
    }

    /// A client's leaving breaks every key it held and lifts its buttons.
    #[test]
    fn release_sends_breaks_for_held_keys_and_buttons() {
        let (b, input) = setup(STALL);
        input
            .send(A, &[
                key(KeyCode::ShiftLeft, true),
                key(KeyCode::ArrowUp, true),
                button(MouseButton::Left, true),
                button(MouseButton::Right, true),
            ])
            .unwrap();
        assert_eq!(collect(&b, 3, 6), (vec![0x12, 0xe0, 0x75], vec![0x09, 0, 0, 0x0b, 0, 0]));
        input.release(A);
        assert_eq!(collect(&b, 5, 3), (vec![0xf0, 0x12, 0xe0, 0xf0, 0x75], vec![0x08, 0, 0]));
    }

    /// The guest falls behind: 200 keys (pressed and released) and a dx of 10,000 in one `send_input`, against an
    /// 8042 a reader drains slowly, all arrive in order, whole, with no OVERRUN (00) and no lost packet. With no
    /// reader the call fails with -32003 after the (shortened) timeout, nothing half-sent: the 8042 holds whole keys
    /// and packets only, and the held state is what reached it: the client's leaving breaks exactly the keys whose
    /// make was delivered.
    #[test]
    fn queue_waits_for_room() {
        let letters = &KeyCode::ALL[..26];
        let (b, input) = setup(STALL);
        let mut events = Vec::new();
        let mut expected = Vec::new();
        for i in 0..200 {
            let code = letters[i % 26];
            events.extend([key(code, true), key(code, false)]);
            expected.extend(code.set2());
            expected.extend(code.set2_break().unwrap());
        }
        events.push(motion(10_000, 0));
        let done = AtomicBool::new(false);
        let (k, m) = std::thread::scope(|s| {
            let reader = s.spawn(|| {
                let (mut k, mut m) = (Vec::new(), Vec::new());
                while !done.load(SeqCst) || b.status() & crate::ps2::testing::OBF != 0 {
                    match b.read() {
                        Some((byte, true)) => m.push(byte),
                        Some((byte, false)) => k.push(byte),
                        None => {}
                    }
                    std::thread::sleep(Duration::from_micros(20));
                }
                (k, m)
            });
            input.send(A, &events).unwrap();
            done.store(true, SeqCst);
            reader.join().unwrap()
        });
        assert_eq!(k.len(), expected.len());
        assert!(k == expected, "the keys' bytes in order, no OVERRUN");
        assert_eq!(m.len() % 3, 0, "whole packets: {m:x?}");
        let mut dx = 0;
        for p in m.chunks(3) {
            assert_eq!(p[0] & !0x10, 0x08, "no overflow, no button: {p:x?}");
            dx += if p[0] & 0x10 != 0 { i32::from(p[1]) - 256 } else { i32::from(p[1]) };
            assert_eq!(p[2], 0);
        }
        assert_eq!(dx, 10_000);
        drop(input);

        // No reader: a stall.
        let stall = Duration::from_millis(100);
        let (b, input) = setup(stall);
        // 19 one-byte makes leave one of the keyboard's 19 host places (the output buffer holds the first byte): the
        // up arrow's two bytes (E0 75) do not fit, so none of them is queued (a partial push would leave E0 alone).
        let presses: Vec<_> = letters[..19]
            .iter()
            .chain([&KeyCode::ArrowUp])
            .chain(&letters[19..])
            .map(|&c| key(c, true))
            .collect();
        let start = Instant::now();
        let stalled = input.send(A, &presses);
        assert_eq!(stalled, Err(SendError::Stalled));
        assert!(start.elapsed() >= stall);
        assert_eq!(Error::from(stalled.unwrap_err()).code.code(), -32003, "a stall is -32003");
        assert_eq!(Error::from(SendError::Stopping).code.code(), -32002);
        let delivered: Vec<u8> = letters[..19].iter().flat_map(|c| c.set2().iter().copied()).collect();
        assert_eq!(collect(&b, 19, 0), (delivered, vec![]), "the first 19 makes, whole, no E0");
        assert_eq!(input.send(A, &[motion(10_000, 0)]), Err(SendError::Stalled));
        let (k, m) = collect(&b, 0, 15);
        assert!(k.is_empty());
        // The output buffer took the first byte; four more packets fill the FIFO to 14 of its 16 bytes.
        assert_eq!(m, [0x08, 255, 0].repeat(5), "whole packets only");
        // The undelivered presses were discarded: the client's leaving breaks the 19 delivered keys, no more (no up
        // arrow).
        input.release(A);
        let breaks: Vec<u8> = letters[..19].iter().flat_map(|c| c.set2_break().unwrap()).collect();
        assert_eq!(collect(&b, 38, 0), (breaks, vec![]));
    }
}
