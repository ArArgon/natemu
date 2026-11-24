# Control API: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

Status: draft, November 24, 2026, revised the same day after an independent review (accept with required changes:
input flow control, server-wide held state, two-stage decoding, bounded writes; folded in below). Owner direction (same day): the display work needs a way to send keyboard and mouse
input in and to see the guest's screen; the control channel is to be a real API, not a debugging placeholder; input
events follow Rust idiom; no VNC (frames are PNG snapshots and, for a later native front end, a shared framebuffer).
This plan builds the API with what exists today (the PS/2 keyboard and mouse, power and status) and fixes the
extension rules a display plan follows to add `snapshot` and `framebuffer`; it builds no display. It stands on the
platform-devices plan's 8042 (`crates/host/src/ps2.rs`, `Ps2Input`) and is independent of mapped device memory.

**Goal:** a program on the Mac (a test, a debugger, later a native window) connects to a Unix socket natemu listens on
and drives the VM through typed requests: input events, a power-button press, quit, status; with a stable wire format
documented for clients in any language.

**Architecture:** JSON-RPC 2.0, one compact JSON value per line, over a Unix-domain socket named by `[control]
socket`. A listener thread accepts up to 8 clients, one thread each; each request is decoded into a Rust enum
(`serde`, adjacently tagged by `method`) and dispatched to a `Target` that holds the handles the host library already
exposes (`Ps2Input`, `Canceller`, a new power-button handle, run counters). Server-to-client notifications carry
events (the run stopped). File descriptors (a later framebuffer) travel with a response as `SCM_RIGHTS` ancillary data
on the same socket.

**Tech stack:** Rust: `crates/host` (new module `control`), `crates/cli`; `serde`, `serde_json`, `libc` (already
dependencies). Python client for tests in `tests/lib`. New dependencies are allowed when judicious (owner, November
24): name each, with its reason, in the commit that adds it.

**Spec:** the Design section below; JSON-RPC 2.0 (jsonrpc.org/specification); W3C UI Events KeyboardEvent code values
(w3.org/TR/uievents-code) for key names; Microsoft's "USB HID to PS/2 Scan Code Translation Table"
(the set 2 column) for their codes; ACPI 6.5 4.8.2.2.1.1 (fixed power button) for the button.

## Why this protocol

- **JSON-RPC 2.0** is a published standard with request ids, errors with codes, notifications and batches; clients
  exist for every language, and a test can speak it with `socat` or ten lines of Python. QEMU's QMP is QEMU's own
  dialect (a greeting and capability negotiation first); cloud-hypervisor and Firecracker use REST over HTTP on a Unix
  socket, which needs an HTTP server and has no server-to-client events. gRPC needs code generation and HTTP/2.
- **Newline-delimited framing** (as `jq -c` writes, and as LSP's alternative transports and many JSON-RPC daemons use):
  compact JSON never contains a raw newline, so a client reads lines. A line longer than 1 MiB is a parse error and
  closes the connection.
- **A Unix socket** (as the second console already uses): same-user access through the file's mode (0600), no network
  exposure, file-descriptor passing for the framebuffer.

## Global constraints

- Default launch unchanged: without `[control] socket` no listener, no thread, no busy guard; every existing test
  byte-for-byte as at the base.
- The socket follows the console's rules (`docs/configuration.md`, A second console): a path of at most 103 bytes, a
  stale socket replaced, a live one refused (`another program listens on this socket`), removed when natemu exits.
  New here (the console does not set a mode): the socket is mode 0600, made by binding it inside a fresh 0700
  directory beside the path and renaming it into place, which is safe with threads running (never a process-wide
  umask).
- The server never blocks a vCPU: device handles take only the locks the devices already take (`Ps2Input`'s mutex).
  Input is never dropped: the 8042's queues are small (mouse FIFO 16 bytes, `ps2/mouse.rs` `PS2MOUSE_FIFOSZ`; keyboard
  19 bytes with OVERRUN, `ps2/keyboard.rs` `key`), so the server keeps its own queue per device and moves a key's or a
  packet's bytes in only when they all fit (`Ps2Input::try_keyboard`, `try_mouse`, which consume nothing on refusal),
  waiting on a drain signal the 8042 gives when the guest reads port 60H. The client thread waits, never a vCPU;
  `send_input` is answered once all its events are in the 8042, or with -32003 after 5 s without progress (the guest
  is not reading: the unsent events are discarded and the held state stays consistent). `quit` uses
  `Canceller::cancel`; the power button sets status bits and raises the SCI.
- While the listener exists the server holds an `Irqs::busy` guard (`crates/host/src/ps2.rs`'s producer rule), so a
  guest idle in HLT waiting for a key is not ended as idle.
- Every request gets exactly one response, in the order requests arrived on that connection; notifications are never
  answered. A client that stops reading is disconnected when its output buffer exceeds 1 MiB (the run goes on).
- Held state is server-wide, since the 8042 has one keyboard and one button mask: each key has the set of clients
  holding it (a client's repeated press is idempotent; a make is sent when the first client takes the key, a break
  when the last lets go) and the button mask sent is the OR of every client's buttons. A client's disconnect drops it
  from every key's set and drops its buttons, so a crashed client never leaves a key stuck and never releases what
  another client holds.
- Writes never block the server: each client has one writer with `SO_SNDTIMEO` of 1 s and an output cap of 1 MiB; a
  client over either is disconnected (the run goes on). `Server::stopped` writes the notification with that deadline,
  `shutdown`s every client socket and joins the threads, so the end of the run never waits on a client.
- Wire names are stable: renaming a method, field or key code is a breaking change; additions are not. The protocol
  version is reported by `query_status` (`protocol: 1`).
- Rust unit tests run in the framework as one unit (the `rust/all` item of `tests/unit/family.toml`); named tests are
  read from that run's `junit.xml`. Each new check is shown to fail against a deliberate break named in its step.
- Tests only through `tools/natemu-test`; the gate passes before merge (this changes the host and the ACPI device).
- Branch `control-api` off `m2-peripherals`. Commit with pathspecs.

## Review Focus

- **A held key outlives its client:** a client sends `ShiftLeft` down and disconnects; the guest must see the break
  (Task 2 `disconnect_releases_held_keys`, Task 4 `control-keyboard` checks the evdev release; break: no release on
  disconnect). With two clients, A holds the left button and B moves the pointer: the button stays down; A holds
  `KeyA`, B presses and releases it, then A disconnects: one break, after A's (Task 1 `held_state_is_server_wide`;
  break: per-client state).
- **The guest falls behind:** 200 keys in one `send_input` and a dx of 10,000 all arrive at the 8042, none overwritten
  by OVERRUN or dropped by a full FIFO (Task 1 `queue_waits_for_room`; break: call `keyboard`/`mouse` without waiting
  for room). A guest's evdev reader cannot see that many at once: natemu passes bytes on as fast as the guest reads
  60H, so one `send_input`'s keys arrive as one run of interrupts, and evdev keeps 64 events per keyboard reader and
  drops them all beyond that (SYN_DROPPED). Task 4 `control-keyboard` therefore sends 6 keys per `send_input` (found
  in Task 4: one `send_input` of all 208 transitions lost all but the last few, at 1 and 2 vCPUs).
- **Large pointer motion:** a `pointer_motion` of dx 1000 must move the guest pointer 1000 units, not 255 with the
  overflow bit (the mouse clamps beyond 9 bits, `ps2/mouse.rs` `movement_get`): the server splits it into packets of at
  most 255 (Task 1 `motion_splits_into_nine_bit_packets`, Task 4 `control-mouse`; break: one packet).
- **Vertical direction:** the API's dy is positive downward (screen convention, as every windowing system); PS/2's is
  upward (Task 1 `dy_is_negated_for_ps2`, Task 4 `control-mouse` checks REL_Y's sign; break: no negation). `Scroll`'s
  dy is positive downward too (content moves up), which Linux reports as negative REL_WHEEL (Task 4 checks the sign).
- **No PS/2 device:** `send_input` with `machine.ps2=false` answers error `-32001` naming the missing device and the
  run goes on (Task 2 `input_without_device_is_an_error`).
- **Malformed and hostile input:** a non-JSON line, an unknown method, wrong params, a 2 MiB line and a client that
  pipelines requests and never reads each give the specified error or disconnect, and other clients keep working
  (Task 2 `protocol_errors`, `slow_reader_is_dropped`).

## Design

### Wire format

Request: `{"jsonrpc":"2.0","id":1,"method":"send_input","params":{"events":[...]}}`. A request without `id` is a
notification: it is carried out and never answered, errors included (a front end streams `send_input` this way; a
`quit` notification quits). Methods without parameters accept `params` absent, `{}` or `[]`. A JSON
array is a batch (JSON-RPC 2.0 section 6), answered with an array. Errors use the JSON-RPC codes (-32700 parse error,
-32600 invalid request, -32601 method not found, -32602 invalid params) plus natemu's:

| Code | Meaning |
| --- | --- |
| -32001 | The device the request needs is absent (`data: {"device": "ps2"}`) |
| -32002 | The run is stopping: the request was not applied |
| -32003 | The guest stopped taking input for 5 s; the events not yet delivered were discarded |

### Methods (protocol 1)

| Method | Params | Result |
| --- | --- | --- |
| `send_input` | `{"events": [InputEvent, ...]}`, applied in order | `{}` |
| `power_button` | none | `{}`: the guest sees a fixed power-button press (ACPI PWRBTN_STS and the SCI) |
| `quit` | none | `{}`, then the run ends as Ctrl-A x does (`Stopped::Cancelled`) |
| `query_status` | none | `{"protocol": 1, "state": "running" \| "stopping", "vcpus": N, "uptime_ms": T, "devices": {"ps2": bool}}` |

Notification from the server: `{"jsonrpc":"2.0","method":"stopped","params":{"reason": "power_off" \| "reset" \|
"panic" \| "cancelled" \| "error"}}`, sent to every client before the listener closes.

### Rust types (`crates/host/src/control/protocol.rs`)

Decoding is two-stage, so each failure keeps the request's `id` and gets its own code: first an `Envelope {
jsonrpc: String, id: Option<Value>, method: String, params: Option<Value> }` (a failure: -32700 or -32600), then a
match on `method` (unknown: -32601) that decodes `params` into that method's struct (`SendInput { events }`; the
no-argument methods take `params` as absent, `{}` or `[]`; a failure: -32602 with serde's message in `data`).

```rust
pub enum Call { SendInput(SendInput), PowerButton, Quit, QueryStatus }
#[derive(Deserialize)] #[serde(deny_unknown_fields)]
pub struct SendInput { pub events: Vec<InputEvent> }

#[derive(Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum InputEvent {
    Key { code: KeyCode, pressed: bool },
    PointerMotion { dx: i32, dy: i32 },
    Button { button: MouseButton, pressed: bool },
    Scroll { dy: i32 },
}
```

`KeyCode`'s variants are the W3C code names verbatim (`KeyA`, `Digit1`, `Enter`, `ShiftLeft`, `ArrowUp`, `F12`,
`NumpadEnter`, ...), so serde's default variant names are the wire names; the set is the 104-key US layout plus the
keys with a set 2 code (`IntlBackslash`, `IntlRo`, `IntlYen`, `ContextMenu`, `Pause`, `PrintScreen`). `MouseButton` is
`Left`, `Right`, `Middle` (wire `"left"`, ...). Wire examples: `{"type":"key","code":"KeyA","pressed":true}`,
`{"type":"pointer_motion","dx":10,"dy":-3}`, `{"type":"button","button":"left","pressed":true}`. An absolute
pointer (`pointer_position`) is not defined: PS/2 has none, and an absolute device (a tablet) adds the variant.
Each event's |dx| and |dy| (`pointer_motion`) and |dy| (`scroll`) is at most 65535: beyond it the request gets -32602
naming the field (`data`: `events[1].dx: at most 65535 in magnitude`) and none of its events is applied (the server
checks the whole list first). The cap bounds the packets one event becomes (at most 257 per axis).

### Translation to PS/2 (`crates/host/src/control/ps2.rs`)

- `KeyCode::set2(self) -> &'static [u8]` the make bytes (E0-prefixed keys with E0); a break is F0 before the last
  byte. `Pause` has no break and its make is the 8-byte sequence; `PrintScreen` is the table's E0 7C (break E0 F0 7C),
  without the modifier-dependent codes of the table's Note 1 (a real keyboard's fake-shift E0 12, which Linux ignores).
- `pointer_motion`, `button` and `scroll` update the server's held buttons and become packets, each axis split into
  steps of at most 255 in magnitude (the mouse clamps beyond 9 bits). `Scroll` needs a wheel byte: the mouse gains
  `dz: i8`, written as the fourth byte (a signed byte, as Linux's psmouse reads ID 3's: scroll dy +1 is byte +1, which
  psmouse reports as REL_WHEEL -1) when the mouse is in IntelliMouse mode and dropped otherwise.
- `Ps2Input` gains `try_keyboard(&self, set2: &[u8]) -> bool` and `try_mouse(&self, dx, dy, dz, buttons) -> bool`, each
  queuing all of its bytes or none (the keyboard keeps its two reply places and refuses while it does not scan; the
  mouse its FIFO room, refusing while it does not stream), and their waiting forms `wait_keyboard(set2, deadline,
  cancel)` and `wait_mouse(dx, dy, dz, buttons, deadline, cancel)`, which retry under the controller's lock on a
  condvar notified when the guest accesses 60H (a separate `wait_room` after a refused `try_*` would miss a read
  between the two), with `wake()` for a cancel.
- The server-wide `Held` (per key the set of clients holding it, per-client button masks, and what the guest was sent)
  decides what to send; a client's disconnect sends only the transitions its leaving causes. `control::ps2::Input`
  holds it with a queue per device and that device's delivery thread (Task 1); after a stall a press whose make was
  not delivered is undone, a release kept with its break owed and sent once the guest reads again.

### Server (`crates/host/src/control/mod.rs`)

- `pub fn serve(path: &Path, target: Target) -> Result<Server>`; `Target { ps2: Option<Ps2Input>, cancel: Canceller,
  status: StatusSource, irqs: Irqs }` (Task 3 adds `power_button: Option<PowerButton>`; until then `power_button` is
  -32601); `StatusSource { vcpus, started: Instant }` (`uptime_ms` counts from `started`; `state` is `stopping` once
  `quit` was asked, the canceller is set or `stopped` began); `Server::stopped(&self, reason)` sends the notification
  and closes; dropping it closes without the notification and removes the socket (only if it is still this server's
  file). The stale/live/non-socket/length checks are the console's, shared as `crate::virtio::claim_socket_path`.
- One listener thread (`poll` on the listener and a wake socket), at most 8 clients (a ninth connection is closed at
  once), each with a reader thread (reads lines, decodes, dispatches, waits for input room) and a writer thread
  (responses and notifications from a queue, `SO_SNDTIMEO` 1 s, `SO_NOSIGPIPE`, 1 MiB cap on the queued bytes; over
  either the client is shut down). A blank line is ignored. A client's end of file releases what it held, then its
  queued responses are written before the connection closes (so `echo REQUEST | socat` gets its answer).
  `Server::stopped` stops the listener, stops `Input` (pending `send_input` gets -32002), queues the notification,
  waits for the writers until one shared 1 s deadline, shuts every socket down and joins every thread. One input
  thread per device (`control::ps2::Input`, Task 1) drains the server's queues into the 8042, so clients' events
  interleave per event, never inside a key's bytes.
- `crates/cli`: `[control] socket = "PATH"` (relative to the file's folder; `--set control.socket=PATH` relative to the
  working directory, as every path key); `serve` after `prepare`, `stopped` when the run returns.

### Power button (`crates/host/src/pm.rs`)

`PowerButton::press()` sets PWRBTN_STS (bit 8 of PM1_STS, `pm.rs` `PWRBTN`) and updates the SCI line, which is already
gated by PWRBTN_EN (`pm.rs` near 106). The FADT already clears PWR_BUTTON (a fixed button is present,
`boot/acpi.rs` near 157). Linux's ACPI button driver turns the press into KEY_POWER on its "Power Button" input device
(and, with systemd-logind or acpid, a shutdown).

### Extension rules (for the display plan)

A display adds methods; it does not change these. `snapshot { "format": "png" }` returns `{"png": BASE64, "width",
"height"}` (the PNG from the encoder crate the display plan names; base64 so the response stays JSON).
`framebuffer {}` returns `{"width", "height", "stride", "format": "x8r8g8b8", "size"}` and one file descriptor as
`SCM_RIGHTS` on the same `sendmsg` as the response line (a client that asks for it must read with `recvmsg`, Python
`socket.recv_fds`; a buffered `readline` or `socat` loses the descriptor): the mapped device memory's shared-memory object
(`docs/restructure/mapped-device-memory-plan.md`, `DeviceMemory::fd`), for the client to `mmap` read-only. A
`display_changed` notification announces a mode change; the client asks for a new `framebuffer`. An IOSurface for a
Metal front end needs a Mach port, which a Unix socket cannot carry; the native front end decides that (out of scope).

## File structure

| File | Responsibility |
| --- | --- |
| `crates/host/src/control/mod.rs` (new) | listener, clients, dispatch, `Target`, `Server` |
| `crates/host/src/control/protocol.rs` (new) | `Call`, `InputEvent`, `KeyCode`, `MouseButton`, envelopes, errors |
| `crates/host/src/control/ps2.rs` (new) | key codes to set 2, motion splitting, held-state release; delivery (`Input`: the per-device queues and threads) |
| `crates/host/src/ps2.rs`, `crates/host/src/ps2/{keyboard.rs,mouse.rs}` | `try_keyboard`, `try_mouse` (with `dz`), `wait_keyboard`, `wait_mouse`; a crate-visible test constructor of the 8042 without a VM |
| `crates/host/src/pm.rs`, `crates/host/src/clock.rs`, `crates/host/src/linux.rs` | `PowerButton` (the PM device sits type-erased inside a `Timer` in `clock.rs`, so the handle is made there and carried on `Board`); `Prepared::power_button()` (always `Some`: the PM block is on every board), `Prepared::irqs()` |
| `crates/cli/src/config.rs`, `crates/cli/src/config/tests.rs`, `crates/cli/src/main.rs` | `[control] socket` |
| `docs/control-api.md` (new), `docs/configuration.md` | the client reference (wire format, methods, codes, examples); the key |
| `tests/lib/control_client.py` (new) | a small client for cases |
| `tests/devices/run_control.py` (new), `tests/devices/family.toml` | the guest cases |

## Tasks

### Task 1: protocol types and PS/2 translation

**Files:** `crates/host/src/control/{mod.rs,protocol.rs,ps2.rs}`, `crates/host/src/ps2.rs`,
`crates/host/src/ps2/{keyboard.rs,mouse.rs}`.

**Interfaces:** `protocol::{Call, Request, Response, InputEvent, KeyCode, MouseButton, ErrorCode}`;
`KeyCode::set2(self) -> &'static [u8]`; `ps2::Held` (server-wide, keyed by `ClientId`); `ps2::Input::start(Ps2Input,
stall)` (the per-device queues and delivery threads) with `send(&self, ClientId, &[InputEvent]) -> Result<(),
SendError>`, `release(&self, ClientId)` and `stop(&self)`; `Ps2Input::mouse(dx, dy, dz, buttons)`, `try_keyboard`,
`try_mouse`, `wait_keyboard`, `wait_mouse`, `wake`.

- [ ] **Step 1: unit tests**: `wire_names_match_examples` (the three examples above round-trip exactly),
  `params_may_be_omitted` (`quit` with `params` absent, `{}` and `[]` decodes), `decode_errors_keep_the_id` (an unknown
  method gives -32601 and bad `send_input` params -32602, each with the request's `id`), `held_state_is_server_wide`
  (the two-client cases of Review Focus), `queue_waits_for_room` (200 keys and dx 10,000 against a test 8042 whose
  output a reader thread drains slowly: every byte arrives in order, no OVERRUN, no lost packet; with no reader,
  -32003 after the timeout and nothing half-sent), `every_keycode_has_a_set2_code` (and
  the table matches the set 2 column of Microsoft's translation table, transcribed into the test as data; QEMU's
  GPL table is not used), `break_inserts_f0_before_last_byte`, `pause_has_no_break`,
  `motion_splits_into_nine_bit_packets`, `dy_is_negated_for_ps2`, `scroll_needs_intellimouse` (no fourth byte, wheel
  dropped, before the 200-100-80 sequence; after it, dz in the fourth byte),
  `release_sends_breaks_for_held_keys_and_buttons`.
- [ ] **Step 2: run** the Rust unit tests; FAIL. **Step 3: implement.** **Step 4: run**; PASS. Show
  `motion_splits_into_nine_bit_packets` fails with one packet per event and `dy_is_negated_for_ps2` without the
  negation.
- [ ] **Step 5: commit** `control: protocol types and their PS/2 translation`.

### Task 2: the server and its configuration

**Files:** `crates/host/src/control/{mod.rs,tests.rs}`, `crates/host/src/linux.rs` (`Prepared::irqs`,
`Canceller::cancelled`), `crates/host/src/virtio/{console.rs,mod.rs}` (the shared socket-path checks),
`crates/cli/src/*`, `docs/configuration.md`, `docs/control-api.md`.

**Interfaces:** `control::serve`, `Target`, `Server::stopped`; `[control] socket`.

- [ ] **Step 1: unit tests** against a `Target` with a test `Ps2Input` (the 8042 built without a VM, as `ps2.rs`'s
  tests do) and a test canceller: `request_gets_one_response_in_order`, `batch_is_answered_as_array`,
  `notification_gets_no_response`, `protocol_errors` (parse, invalid request, unknown method, bad params, a 2 MiB line:
  each code, the last disconnecting), `input_without_device_is_an_error`, `quit_cancels`, `disconnect_releases_held_keys`,
  `ninth_client_is_refused`, `slow_reader_is_dropped` (a client that pipelines `query_status` requests and never
  reads is disconnected by the send timeout or the cap; the other client is still answered), `stopped_never_waits`
  (`stopped` returns within 2 s with a client that never reads), `stopped_reaches_every_client`, `socket_rules` (0600, stale replaced, live refused,
  removed on drop); config tests for the key and its errors.
- [ ] **Step 2: run**; FAIL. **Step 3: implement**, with `docs/control-api.md` (wire format, every method and code, a
  `socat` and a Python example) and the `[control]` row in `docs/configuration.md`. **Step 4: run**; PASS. Show
  `disconnect_releases_held_keys` fails without the release, and `slow_reader_is_dropped` and `stopped_never_waits`
  fail with blocking writes.
- [ ] **Step 5: commit** `control: a JSON-RPC control socket for input, quit and status`.

### Task 3: the power button

**Files:** `crates/host/src/pm.rs`, `crates/host/src/linux.rs`, `crates/host/src/control/mod.rs`.

- [ ] **Step 1: unit tests** in `pm.rs`: `press_sets_pwrbtn_sts`, `sci_follows_pwrbtn_en` (no line while disabled; the
  line once enabled with the status set), `write_one_clears_pwrbtn_sts`.
- [ ] **Step 2: run**; FAIL. **Step 3: implement** `PowerButton` and the `power_button` method. **Step 4: run**; PASS.
- [ ] **Step 5: commit** `pm: a fixed power button the control socket presses`.

### Task 4: guest cases

**Files:** `tests/lib/control_client.py`, `tests/devices/run_control.py`, `tests/devices/family.toml`.

The cases boot the `tests/devices` platform guest (Debian kernel, BusyBox init) with `[control] socket`; the init loads `evdev`, `psmouse` and the ACPI `button`
driver (modules in Debian's kernel: the family's initramfs recipe includes them, and the runner's self-check fails if
one is missing), finds the keyboard's, mouse's and power button's `/dev/input/event*` through
`/proc/bus/input/devices` and prints each 24-byte `input_event` on the serial console with BusyBox `hexdump -v -e`
(no compiled reader, so no new toolchain); the runner sends requests and compares the printed events. The expected
`KEY_*` and `REL_*` numbers are Linux's UAPI interface constants (`input-event-codes.h`), cited as such. Reading evdev in the guest is the test's observer, not a
front end.

- [ ] **Step 1: cases**: `control-keyboard` (every `KeyCode`, press and release, a few keys per `send_input` (the
  guest's evdev holds 64 events per reader, Review Focus): the guest's EV_KEY codes equal the
  Linux `KEY_*` each code names, from a table in the runner; then `ShiftLeft` down and a disconnect: the release
  arrives); `control-mouse` (after `modprobe psmouse`: dx 1000 sums to 1000 in REL_X, dy 40 gives REL_Y +40, each
  button down and up, scroll dy 3 gives REL_WHEEL -3 and dy -3 gives +3); `control-power-button` (KEY_POWER on the "Power Button" device,
  then the init powers off: `stopped` with `power_off`, exit status 0); `control-status` (`query_status` while running;
  `send_input` with `machine.ps2=false` gives -32001; `quit` ends the run with status 2 and `stopped: cancelled`).
  Each at 1 vCPU; `control-keyboard` also at 2. `control-keyboard` and `control-power-button` are smoke cases.
- [ ] **Step 2: run**; FAIL. **Step 3: implement.** **Step 4: run**; PASS. Show `control-mouse` fails with the dy
  negation removed and `control-keyboard` with one table entry changed.
- [ ] **Step 5:** add `crates/host/src/control/**` to the family's `covers`; run `tools/natemu-test run` then
  `--gate`; PASS. **Commit** `tests: control socket guest cases`.

## Cost

Nothing on the guest's paths: the server runs on its own threads and touches the 8042 and PM device through their
existing locks. With no `[control]` key nothing starts.

## Out of scope

The display methods (`snapshot`, `framebuffer`, `display_changed`: the display plan, by the rules above); an absolute
pointer device; virtio-input; keyboard layouts and text typing (a client maps characters to codes; the Python client
may offer `type_text` for US layout); authentication beyond the socket's file mode; TCP; save, restore and pause;
the native front end and its IOSurface handoff.
