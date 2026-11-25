//! The window (macOS): see the crate root.
use crate::{control, keys, picture};
use control::{Connection, describe_error};
use picture::{Framebuffer, Mode};
use serde_json::{Value, json};
use std::collections::HashSet;
use std::num::NonZeroU32;
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::sync::mpsc::{Receiver, Sender, channel};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use winit::application::ApplicationHandler;
use winit::dpi::LogicalSize;
use winit::event::{DeviceEvent, DeviceId, ElementState, KeyEvent, MouseButton, MouseScrollDelta, WindowEvent};
use winit::event_loop::{ActiveEventLoop, ControlFlow, EventLoop, EventLoopProxy};
use winit::keyboard::PhysicalKey;
use winit::window::{CursorGrabMode, Window, WindowId};

const FRAME: Duration = Duration::from_micros(16_667);
/// The size of one `pointer_motion` or `scroll` field (docs/control-api.md: at most 65535 in magnitude).
const MAX_STEP: i64 = 65535;
/// Trackpad scrolling: logical pixels per wheel step.
const PIXELS_PER_STEP: f64 = 24.0;
const TITLE: &str = "natemu";

/// What the display connection publishes: the mapped framebuffer and the mode the guest shows in it.
#[derive(Default)]
struct Picture {
    framebuffer: Option<Framebuffer>,
    mode: Option<Mode>,
}

enum Note {
    /// The mode changed (or the framebuffer was mapped): resize the window to it.
    Mode(Option<Mode>),
    /// The run ended (`Ok`: natemu stopped, the reason) or the viewer cannot go on (`Err`): say so and exit.
    Gone(Result<String, String>),
    /// The guest stopped taking mouse input (no mouse driver?): let the mouse go, or its bytes hold up the keyboard.
    MouseUnread,
}

pub fn main() {
    let mut args = std::env::args_os().skip(1);
    let socket = match (args.next(), args.next()) {
        (Some(a), None) if a != "-h" && a != "--help" => PathBuf::from(a),
        _ => {
            eprintln!(
                "usage: natemu-viewer SOCKET\n\nShows the display of the natemu that listens on SOCKET (its \
                 `control.socket`) and passes on keyboard and mouse input. Click to capture the mouse; \
                 Control-Option releases it."
            );
            std::process::exit(2);
        }
    };
    match run(&socket) {
        Ok(Some(stopped)) => eprintln!("natemu-viewer: {stopped}"),
        Ok(None) => {}
        Err(e) => {
            eprintln!("natemu-viewer: {e}");
            std::process::exit(1);
        }
    }
}

/// `Ok(Some(why))` when the VM stopped, `Ok(None)` when the window was closed, `Err` when the viewer could not go on.
fn run(socket: &Path) -> Result<Option<String>, String> {
    let what = |e: std::io::Error| format!("control socket {}: {e}", socket.display());
    let display = Connection::open(socket).map_err(what)?;
    let input = Connection::open(socket).map_err(what)?;

    let event_loop = EventLoop::<Note>::with_user_event().build().map_err(|e| e.to_string())?;
    let picture = Arc::new(Mutex::new(Picture::default()));
    let proxy = event_loop.create_proxy();
    {
        let picture = picture.clone();
        std::thread::spawn(move || {
            let end = follow_display(display, &picture, &proxy);
            let _ = proxy.send_event(Note::Gone(end));
        });
    }
    let (tx, rx) = channel();
    let input_proxy = event_loop.create_proxy();
    std::thread::spawn(move || {
        if let Err(e) = send_input(input, rx, input_proxy) {
            eprintln!("natemu-viewer: input connection: {e}; keyboard and mouse no longer reach the guest");
        }
    });

    let mut app = App::new(picture, tx);
    event_loop.run_app(&mut app).map_err(|e| e.to_string())?;
    app.gone.transpose()
}

/// The display connection: map the framebuffer once the guest shows a picture, then follow its mode. Returns the
/// reason natemu stopped (`Ok`), or why the viewer cannot go on.
///
/// natemu computes an answer on the client's reader thread and announces mode changes from another, so a
/// `display_changed` can arrive before the answer to a `framebuffer` asked earlier, with the answer's mode (or its
/// -32004) already stale. A change seen while the request is pending therefore asks again once the answer is in.
fn follow_display(mut c: Connection, picture: &Mutex<Picture>, proxy: &EventLoopProxy<Note>) -> Result<String, String> {
    let io = |e: std::io::Error| format!("display connection: {e}");
    let mut pending = Some(c.request("framebuffer", None).map_err(io)?);
    let mut changed_while_pending = false;
    while let Some(msg) = c.next().map_err(io)? {
        if let Some(id) = msg.get("id") {
            if pending.is_none() || id.as_u64() != pending {
                continue;
            }
            pending = None;
            if let Some(result) = msg.get("result") {
                let fd = c.take_fd().ok_or("natemu answered `framebuffer` without its descriptor")?;
                let size = result.get("size").and_then(Value::as_u64).ok_or("`framebuffer` answer without size")?;
                let fb = Framebuffer::map(&fd, size).map_err(|e| format!("mapping the framebuffer: {e}"))?;
                let mode = Mode::parse(result);
                let mut p = picture.lock().unwrap();
                p.framebuffer = Some(fb);
                p.mode = mode;
                drop(p);
                let _ = proxy.send_event(Note::Mode(mode));
            } else if let Some(err) = msg.get("error") {
                match err.get("code").and_then(Value::as_i64) {
                    // The guest shows no picture yet: wait for display_changed.
                    Some(-32004) => {}
                    Some(-32001) => return Err("this VM has no display (`[display]` in its configuration)".into()),
                    _ => return Err(format!("framebuffer: {}", describe_error(err))),
                }
            }
            if std::mem::take(&mut changed_while_pending) {
                pending = Some(c.request("framebuffer", None).map_err(io)?);
            }
            continue;
        }
        match msg.get("method").and_then(Value::as_str) {
            Some("display_changed") => {
                if pending.is_some() {
                    changed_while_pending = true;
                    continue;
                }
                let params = msg.get("params").cloned().unwrap_or(Value::Null);
                let mode = Mode::parse(&params);
                let mut p = picture.lock().unwrap();
                if p.framebuffer.is_some() {
                    p.mode = mode;
                    drop(p);
                    let _ = proxy.send_event(Note::Mode(mode));
                } else if mode.is_some() {
                    drop(p);
                    pending = Some(c.request("framebuffer", None).map_err(io)?);
                }
            }
            Some("stopped") => {
                let reason = msg.pointer("/params/reason").and_then(Value::as_str).unwrap_or("stopped");
                return Ok(format!("the VM stopped ({reason})"));
            }
            _ => {}
        }
    }
    Err("natemu closed the connection".into())
}

/// The most events one `send_input` carries (a line of them stays far below natemu's 1 MiB bound).
const EVENTS_PER_REQUEST: usize = 512;
/// After the guest stopped taking mouse input, mouse motion and wheel steps the window queued before it let the mouse
/// go are dropped for this long, so they do not stall the keyboard again.
const MOUSE_QUIET: Duration = Duration::from_secs(1);

/// The input connection: send what the window queued, one `send_input` at a time, waiting for each answer, so input
/// piles up here (where pointer motion merges) rather than in natemu. A reader thread drains the connection all the
/// time: natemu sends every client its notifications and disconnects one that does not read them within a second.
///
/// When a request fails, natemu has dropped its events from the first one the guest did not take, so a release among
/// them never reached the guest and the key would stay held: the releases of that request are sent again on their own
/// (a release of something not held changes nothing). A failure with mouse events in it (-32003: the guest stopped
/// taking input) tells the window to let the mouse go, since the keyboard and mouse share one queue (as on a real
/// 8042) and mouse bytes a guest never reads (no mouse driver) would hold up every key after them.
fn send_input(c: Connection, rx: Receiver<Value>, proxy: EventLoopProxy<Note>) -> std::io::Result<()> {
    let (answers_tx, answers) = channel();
    let mut reader = c.try_clone()?;
    std::thread::spawn(move || {
        while let Ok(Some(msg)) = reader.next() {
            if let Some(id) = msg.get("id").and_then(Value::as_u64) {
                if answers_tx.send((id, msg.get("error").cloned())).is_err() {
                    return;
                }
            }
        }
    });
    let mut c = c;
    let mut call = |events: &[Value]| -> std::io::Result<Option<Value>> {
        let id = c.request("send_input", Some(json!({ "events": events })))?;
        loop {
            match answers.recv() {
                Ok((got, err)) if got == id => return Ok(err),
                Ok(_) => {}
                Err(_) => return Err(std::io::Error::other("natemu closed the connection")),
            }
        }
    };
    let mut quiet_until = Instant::now();
    while let Ok(first) = rx.recv() {
        let mut events: Vec<Value> = std::iter::once(first).chain(rx.try_iter()).collect();
        if Instant::now() < quiet_until {
            events.retain(|e| !is_mouse_motion(e));
        }
        for batch in merge_motion(events).chunks(EVENTS_PER_REQUEST) {
            let batch: Vec<Value> = if Instant::now() < quiet_until {
                batch.iter().filter(|e| !is_mouse_motion(e)).cloned().collect()
            } else {
                batch.to_vec()
            };
            if batch.is_empty() {
                continue;
            }
            let Some(err) = call(&batch)? else { continue };
            eprintln!("natemu-viewer: send_input: {}", describe_error(&err));
            if err.get("code").and_then(Value::as_i64) == Some(-32003) && batch.iter().any(|e| e["type"] != "key") {
                quiet_until = Instant::now() + MOUSE_QUIET;
                let _ = proxy.send_event(Note::MouseUnread);
            }
            let releases = final_releases(&batch);
            if !releases.is_empty() {
                if let Some(err) = call(&releases)? {
                    eprintln!("natemu-viewer: send_input (releases): {}", describe_error(&err));
                }
            }
        }
    }
    Ok(())
}

fn is_mouse_motion(e: &Value) -> bool {
    e["type"] == "pointer_motion" || e["type"] == "scroll"
}

/// The keys and buttons whose last event in `events` is a release, as release events (in the order of each one's first event).
fn final_releases(events: &[Value]) -> Vec<Value> {
    let mut last: Vec<(String, &Value)> = Vec::new();
    for e in events {
        let name = match e["type"].as_str() {
            Some("key") => format!("key {}", e["code"]),
            Some("button") => format!("button {}", e["button"]),
            _ => continue,
        };
        match last.iter_mut().find(|(n, _)| *n == name) {
            Some(slot) => slot.1 = e,
            None => last.push((name, e)),
        }
    }
    last.into_iter().filter(|(_, e)| e["pressed"] == false).map(|(_, e)| e.clone()).collect()
}

/// Adjacent `pointer_motion` events as one (within the per-event bound).
fn merge_motion(events: Vec<Value>) -> Vec<Value> {
    let mut out: Vec<Value> = Vec::with_capacity(events.len());
    for e in events {
        if e["type"] == "pointer_motion" {
            if let Some(last) = out.last_mut().filter(|l| l["type"] == "pointer_motion") {
                let dx = last["dx"].as_i64().unwrap_or(0) + e["dx"].as_i64().unwrap_or(0);
                let dy = last["dy"].as_i64().unwrap_or(0) + e["dy"].as_i64().unwrap_or(0);
                if dx.abs() <= MAX_STEP && dy.abs() <= MAX_STEP {
                    *last = json!({"type": "pointer_motion", "dx": dx, "dy": dy});
                    continue;
                }
            }
        }
        out.push(e);
    }
    out
}

struct App {
    picture: Arc<Mutex<Picture>>,
    input: Sender<Value>,
    window: Option<Rc<Window>>,
    surface: Option<softbuffer::Surface<Rc<Window>, Rc<Window>>>,
    shown: Option<(u32, u32)>,
    captured: bool,
    /// Control and Option are down together with nothing else since (a release of either lets the mouse go).
    release_armed: bool,
    keys: HashSet<&'static str>,
    buttons: HashSet<&'static str>,
    motion: (f64, f64),
    scroll: f64,
    next_frame: Instant,
    gone: Option<Result<String, String>>,
}

impl App {
    fn new(picture: Arc<Mutex<Picture>>, input: Sender<Value>) -> App {
        App {
            picture,
            input,
            window: None,
            surface: None,
            shown: None,
            captured: false,
            release_armed: false,
            keys: HashSet::new(),
            buttons: HashSet::new(),
            motion: (0.0, 0.0),
            scroll: 0.0,
            next_frame: Instant::now(),
            gone: None,
        }
    }

    fn send(&self, event: Value) {
        let _ = self.input.send(event);
    }

    fn title(&self) -> String {
        let size = match self.shown {
            Some((w, h)) => format!("{w} x {h}"),
            None => "no picture".into(),
        };
        let hint = if self.captured { "Control-Option releases the mouse" } else { "click to capture the mouse" };
        format!("{TITLE} — {size} — {hint}")
    }

    fn set_captured(&mut self, on: bool) {
        let Some(w) = self.window.clone() else { return };
        if on {
            if w.set_cursor_grab(CursorGrabMode::Locked).or_else(|_| w.set_cursor_grab(CursorGrabMode::Confined)).is_err() {
                eprintln!("natemu-viewer: cannot capture the mouse on this system");
                return;
            }
            w.set_cursor_visible(false);
        } else {
            let _ = w.set_cursor_grab(CursorGrabMode::None);
            w.set_cursor_visible(true);
            for b in std::mem::take(&mut self.buttons) {
                self.send(json!({"type": "button", "button": b, "pressed": false}));
            }
        }
        self.captured = on;
        self.motion = (0.0, 0.0);
        w.set_title(&self.title());
    }

    /// Lets go of every key and button this window holds in the guest (focus lost: the releases would go elsewhere).
    fn release_all(&mut self) {
        self.release_armed = false;
        for k in std::mem::take(&mut self.keys) {
            self.send(json!({"type": "key", "code": k, "pressed": false}));
        }
        if self.captured {
            self.set_captured(false);
        }
    }

    fn key(&mut self, event: &KeyEvent) {
        let PhysicalKey::Code(code) = event.physical_key else { return };
        let pressed = event.state == ElementState::Pressed;
        let Some(name) = keys::name(code) else {
            self.release_armed = false;
            return;
        };
        if pressed {
            // A held key's auto-repeat is the guest's to make; send each press once.
            if !self.keys.insert(name) {
                return;
            }
        } else if !self.keys.remove(name) {
            return;
        }
        self.send(json!({"type": "key", "code": name, "pressed": pressed}));
        // Control-Option on its own lets the mouse go, when the pair is let go with no other key pressed meanwhile, so
        // the guest still gets Control-Alt-F2 or Control-Alt-Delete with the mouse captured.
        let pair = |k: &str| matches!(k, "ControlLeft" | "ControlRight" | "AltLeft" | "AltRight");
        if pressed {
            let control = self.keys.contains("ControlLeft") || self.keys.contains("ControlRight");
            let option = self.keys.contains("AltLeft") || self.keys.contains("AltRight");
            self.release_armed = pair(name) && control && option && self.keys.iter().all(|k| pair(k));
        } else if pair(name) && std::mem::take(&mut self.release_armed) && self.captured {
            self.set_captured(false);
        }
    }

    fn redraw(&mut self) {
        let (Some(window), Some(surface)) = (self.window.as_ref(), self.surface.as_mut()) else { return };
        let size = window.inner_size();
        let (Some(w), Some(h)) = (NonZeroU32::new(size.width), NonZeroU32::new(size.height)) else { return };
        if surface.resize(w, h).is_err() {
            return;
        }
        let Ok(mut buffer) = surface.buffer_mut() else { return };
        let p = self.picture.lock().unwrap();
        match (&p.framebuffer, &p.mode) {
            (Some(fb), Some(mode)) => picture::draw(fb, mode, &mut buffer, w.get(), h.get()),
            _ => buffer.fill(0),
        }
        drop(p);
        let _ = buffer.present();
    }
}

impl ApplicationHandler<Note> for App {
    fn resumed(&mut self, event_loop: &ActiveEventLoop) {
        if self.window.is_some() {
            return;
        }
        let attrs = Window::default_attributes().with_title(self.title()).with_inner_size(LogicalSize::new(1024.0, 768.0));
        let window = match event_loop.create_window(attrs) {
            Ok(w) => Rc::new(w),
            Err(e) => {
                self.gone = Some(Err(format!("cannot open a window: {e}")));
                return event_loop.exit();
            }
        };
        let surface = softbuffer::Context::new(window.clone()).and_then(|cx| softbuffer::Surface::new(&cx, window.clone()));
        match surface {
            Ok(s) => self.surface = Some(s),
            Err(e) => {
                self.gone = Some(Err(format!("cannot draw in the window: {e}")));
                return event_loop.exit();
            }
        }
        self.window = Some(window);
    }

    fn user_event(&mut self, event_loop: &ActiveEventLoop, note: Note) {
        match note {
            Note::Mode(mode) => {
                let shown = mode.map(|m| (m.width, m.height));
                if shown != self.shown {
                    self.shown = shown;
                    if let (Some(w), Some((mw, mh))) = (&self.window, shown) {
                        let _ = w.request_inner_size(LogicalSize::new(mw as f64, mh as f64));
                    }
                    if let Some(w) = &self.window {
                        w.set_title(&self.title());
                    }
                }
            }
            Note::MouseUnread => {
                if self.captured {
                    eprintln!("natemu-viewer: the guest is not reading the mouse (no mouse driver?); mouse released");
                    self.set_captured(false);
                }
            }
            Note::Gone(end) => {
                self.gone = Some(end);
                event_loop.exit();
            }
        }
    }

    fn window_event(&mut self, event_loop: &ActiveEventLoop, _: WindowId, event: WindowEvent) {
        match event {
            WindowEvent::CloseRequested => event_loop.exit(),
            WindowEvent::RedrawRequested => self.redraw(),
            WindowEvent::Focused(false) => self.release_all(),
            WindowEvent::KeyboardInput { event, is_synthetic: false, .. } => self.key(&event),
            WindowEvent::MouseInput { state, button, .. } => {
                if !self.captured {
                    if state == ElementState::Pressed && button == MouseButton::Left {
                        self.set_captured(true);
                    }
                    return;
                }
                let name = match button {
                    MouseButton::Left => "left",
                    MouseButton::Right => "right",
                    MouseButton::Middle => "middle",
                    _ => return,
                };
                let pressed = state == ElementState::Pressed;
                let changed = if pressed { self.buttons.insert(name) } else { self.buttons.remove(name) };
                if changed {
                    self.send(json!({"type": "button", "button": name, "pressed": pressed}));
                }
            }
            WindowEvent::MouseWheel { delta, .. } if self.captured => {
                // winit: positive y scrolls up; send_input: positive dy scrolls down.
                self.scroll += match delta {
                    MouseScrollDelta::LineDelta(_, y) => -y as f64,
                    MouseScrollDelta::PixelDelta(p) => -p.y / PIXELS_PER_STEP,
                };
                let steps = (self.scroll.trunc() as i64).clamp(-MAX_STEP, MAX_STEP);
                if steps != 0 {
                    self.scroll -= steps as f64;
                    self.send(json!({"type": "scroll", "dy": steps}));
                }
            }
            _ => {}
        }
    }

    fn device_event(&mut self, _: &ActiveEventLoop, _: DeviceId, event: DeviceEvent) {
        if let DeviceEvent::MouseMotion { delta: (dx, dy) } = event {
            if !self.captured {
                return;
            }
            self.motion.0 += dx;
            self.motion.1 += dy;
            let (ix, iy) = (self.motion.0.trunc() as i64, self.motion.1.trunc() as i64);
            if ix != 0 || iy != 0 {
                self.motion.0 -= ix as f64;
                self.motion.1 -= iy as f64;
                let (ix, iy) = (ix.clamp(-MAX_STEP, MAX_STEP), iy.clamp(-MAX_STEP, MAX_STEP));
                self.send(json!({"type": "pointer_motion", "dx": ix, "dy": iy}));
            }
        }
    }

    fn about_to_wait(&mut self, event_loop: &ActiveEventLoop) {
        let now = Instant::now();
        if now >= self.next_frame {
            if let Some(w) = &self.window {
                w.request_redraw();
            }
            self.next_frame = now + FRAME;
        }
        event_loop.set_control_flow(ControlFlow::WaitUntil(self.next_frame));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn motion_merges_until_another_event_or_the_bound() {
        let m = |dx: i64, dy: i64| json!({"type": "pointer_motion", "dx": dx, "dy": dy});
        let click = json!({"type": "button", "button": "left", "pressed": true});
        let out = merge_motion(vec![m(1, 2), m(3, -1), click.clone(), m(5, 5), m(MAX_STEP, 0)]);
        assert_eq!(out, vec![m(4, 1), click, m(5, 5), m(MAX_STEP, 0)]);
    }

    #[test]
    fn a_failed_request_resends_its_final_releases() {
        let k = |code: &str, pressed: bool| json!({"type": "key", "code": code, "pressed": pressed});
        let b = |pressed: bool| json!({"type": "button", "button": "left", "pressed": pressed});
        let motion = json!({"type": "pointer_motion", "dx": 1, "dy": 0});
        let batch = [k("KeyB", true), motion, k("ShiftLeft", false), b(true), k("KeyA", false), b(false), k("KeyB", false), k("KeyA", true)];
        assert_eq!(final_releases(&batch), vec![k("KeyB", false), k("ShiftLeft", false), b(false)]);
    }
}
