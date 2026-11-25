# The control API

natemu can listen on a Unix socket for a program on the Mac to drive the VM: type keys, move and click the mouse,
quit, ask how the run is going and, with a display (`[display]`, [configuration](configuration.md)), take pictures
of the screen or map the framebuffer. A test, a debugger or a window that shows the guest can use it. This page is the
reference for writing such a client in any language. The protocol is JSON-RPC 2.0 (jsonrpc.org/specification), one
JSON value per line.

To turn it on, name the socket in the configuration file, or with `--set control.socket=PATH`
([configuration](configuration.md)):

```toml
[control]
socket = "vm.sock"
```

Without that key natemu opens no socket and the launch is the same as before.

## The socket

- The socket is made when the VM is ready to start, before the guest runs, and removed when natemu exits.
- Its mode is 0600, so only your user can connect. A path is at most 103 bytes. natemu makes the socket in a
  temporary folder beside the path (named `.c` and a few characters) and then moves it into place, so a path within
  about 10 bytes of the limit can be refused when the socket's own name is short.
- A socket left behind by an earlier run is replaced. If another program is listening on the path, natemu refuses to
  start (`control socket vm.sock: another program listens on this socket`), and it also refuses a path where
  something other than a socket exists.
- While the socket exists, natemu does not end a run because all its processors are halted (a guest idle in HLT may
  be waiting for a key from a client); end such a run with [`quit`](#quit).
- Up to 8 programs can be connected at once. A ninth connection is closed at once. A client's place frees only once
  natemu has noticed it left: usually at once, up to about 1 second while its last answers are written, and up to 5
  seconds if it left during a `send_input` the guest is not reading.

## Messages

Each message is one line: compact JSON (no newline inside it) followed by a newline (`\n`). Lines go both ways on
the same connection. A line a client sends may be at most 1 MiB (1,048,576 bytes, without its newline). A longer
line is answered with a parse error (-32700) and the connection is closed. The bound is on what clients send only:
natemu's own lines have none (a `snapshot` answer is several MiB, see [Limits](#limits)). Blank lines are ignored. A line that is not valid UTF-8 is
a parse error. When a client closes its sending side, a last line without its newline still counts, and the client
gets the answers to everything it sent before natemu closes the connection.

A request:

```json
{"jsonrpc":"2.0","id":1,"method":"query_status"}
```

- `jsonrpc` must be `"2.0"`.
- `id` is a number, a string or `null`, and comes back in the response unchanged. Use any value you like; numbers
  counting up are easiest.
- `method` names what to do.
- `params` is an object or an array. For a method that takes no parameters it may be left out, or be `{}` or `[]`.
  `send_input` takes an object only.
- No other members are allowed.

The response to a request carries either `result` or `error`:

```json
{"jsonrpc":"2.0","id":1,"result":{"protocol":1,"state":"running","vcpus":2,"uptime_ms":5120,"devices":{"ps2":true}}}
{"jsonrpc":"2.0","id":2,"error":{"code":-32601,"message":"Method not found"}}
```

Every request gets exactly one response while the connection lasts, and the responses come in the order the requests
arrived on that connection. When the run ends (the `stopped` notification) or natemu closes the connection, requests
still being carried out or not yet read may go unanswered.
You may send several requests without waiting for answers. An error can have a `data` member with details: a string
explaining it, or for -32001 an object naming the device.

**Notifications.** A request without `id` is a notification: natemu carries it out and never answers it, not even
with an error. A program that streams input (a window passing on every mouse movement) can send `send_input` this
way. A `quit` notification quits. Only a parse error or a request that is not a valid request at all is still
answered (with `"id": null`), because natemu cannot tell whether it was meant as a notification.

**Batches.** A line that is a JSON array of requests is a batch. natemu carries the requests out in order and answers
with one line: an array of the responses, in the same order, without entries for the notifications. A batch of only
notifications gets no answer. An empty array `[]` is answered with one invalid-request error (not an array).

```json
[{"jsonrpc":"2.0","id":1,"method":"query_status"},{"jsonrpc":"2.0","method":"quit"}]
```

answered with

```json
[{"jsonrpc":"2.0","id":1,"result":{"protocol":1,"state":"running","vcpus":1,"uptime_ms":900,"devices":{"ps2":true}}}]
```

## Methods

### `send_input`

Sends keyboard and mouse events to the guest's PS/2 keyboard and mouse, in order.

```json
{"jsonrpc":"2.0","id":3,"method":"send_input","params":{"events":[{"type":"key","code":"KeyA","pressed":true},{"type":"key","code":"KeyA","pressed":false}]}}
```

Result: `{}`, once every event is in the keyboard controller, ready for the guest to read. An empty `events` list is
allowed and answered `{}` at once. The events are described under [Input events](#input-events). Errors: -32602 for
an unknown event or key, a missing field, or a motion beyond the limit; -32001 when the VM has no PS/2 devices
(`machine.ps2 = false`); -32003 when the guest stopped reading; -32002 when the run is stopping.

### `quit`

Ends the run, as Ctrl-A x does at the terminal: natemu stops every processor and exits with status 2. Result: `{}`.
Then every client gets the [`stopped` notification](#the-stopped-notification) with reason `cancelled`, and the
connections close. No parameters.

### `query_status`

No parameters. Result:

```json
{"protocol":1,"state":"running","vcpus":2,"uptime_ms":5120,"devices":{"ps2":true}}
```

- `protocol`: the protocol version, 1 (see [Stability](#stability)).
- `state`: `"running"`, or `"stopping"` once the run is ending (after a `quit`, Ctrl-A x or Ctrl-C).
- `vcpus`: the number of processors the VM has.
- `uptime_ms`: milliseconds since the socket was made.
- `devices.ps2`: whether the VM has the PS/2 keyboard and mouse that `send_input` needs.
- `devices.display`: only when the VM has a display: `null` while the guest shows no picture, else the mode, as
  [`framebuffer`](#framebuffer) reports it without `size`
  (`{"width":1024,"height":768,"stride":4096,"offset":0,"format":"x8r8g8b8"}`). A VM without a display has no
  `display` member.

### `power_button`

No parameters. Result: `{}`. It presses the VM's fixed ACPI power button once: the button's status bit (PWRBTN_STS in
PM1_STS) is set and, if the guest has enabled it (PWRBTN_EN), the guest gets its ACPI interrupt. A Linux guest with the
ACPI `button` driver reports that as a KEY_POWER press on its "Power Button" input device, which a login manager or
`acpid` normally turns into a shutdown; natemu itself does nothing more, and a guest that ignores it keeps running.
Pressing again before the guest has handled the first press changes nothing. If the VM has no power button the answer
is error -32001 with `data` `{"device": "power_button"}`; every VM natemu builds has one. While the run is stopping
the answer is -32002 and nothing is pressed.

### `snapshot`

The picture the guest shows now, as a PNG. Parameters: `{"format": "png"}`, or none (PNG is the default and the only
format). Result:

```json
{"width":640,"height":480,"png":"iVBORw0KGgoAAAANSUhEUgAAAoAAAAHgCAIAAAD..."}
```

`png` is the PNG file in base64 (RFC 4648, with padding): 8-bit RGB, as wide and tall as the guest's mode. natemu
reads the mode once and copies the visible part of the framebuffer row by row, so a picture never mixes two modes;
a row the guest rewrites during the copy may show part of each version, as a real screen would. Errors: -32001 with
`data` `{"device": "display"}` when the VM has no display; -32004 when the guest has not turned the display on (no
driver loaded yet, or it turned it off); -32005 when the picture is wider than 2560 or taller than 1600 pixels
(`data` names the size and the limits); -32602 for another format.

### `framebuffer`

No parameters. The mode and the framebuffer itself, for a program that shows the screen without a copy per frame:

```json
{"width":640,"height":480,"stride":3200,"offset":51220,"format":"x8r8g8b8","size":16777216}
```

The answer's line comes with one file descriptor, passed as `SCM_RIGHTS` ancillary data on the message that carries
the line's first bytes: the framebuffer's shared-memory object, `size` bytes, which the client maps read-only
(`mmap(NULL, size, PROT_READ, MAP_SHARED, fd, 0)`; a writable mapping is refused). Read it with `recvmsg` (Python:
`socket.recv_fds`); a client that reads with a buffered `readline`, or `socat`, loses the descriptor. The picture's
row `y` starts at byte `offset + y * stride` of the mapping; each pixel is 4 bytes in `format`: `x8r8g8b8` is B, G,
R and an unused byte in memory (the usual order), `b8g8r8x8` the unused byte, R, G, B (a guest that selected the
big-endian order). The mapping follows the guest: it is the memory the guest draws into, so it changes as the guest
draws and stays valid if the guest moves the framebuffer's PCI address. When the mode changes (see
[`display_changed`](#the-display_changed-notification)) ask again for the new geometry; the descriptor stays the
same object. In a batch, the descriptors of its `framebuffer` answers come with the batch's line, in the order of
the answers; a `framebuffer` notification sends none. Errors: -32001 and -32004 as for `snapshot`.

## Input events

Each event is an object with a `type` and that type's fields; nothing else is allowed. The vertical axis follows the
screen: a positive `dy` is downward, for the pointer and for the wheel.

| Type | Fields | Example |
| --- | --- | --- |
| `key` | `code` (a key name, below), `pressed` (true: down, false: up) | `{"type":"key","code":"ShiftLeft","pressed":true}` |
| `pointer_motion` | `dx`, `dy`: integers, how far to move right and down | `{"type":"pointer_motion","dx":10,"dy":-3}` |
| `button` | `button`: `"left"`, `"right"` or `"middle"`; `pressed` | `{"type":"button","button":"left","pressed":true}` |
| `scroll` | `dy`: integer, wheel steps; positive scrolls down (the content moves up) | `{"type":"scroll","dy":1}` |

A key press and its release are two events. To type an uppercase A, send `ShiftLeft` down, `KeyA` down, `KeyA` up,
`ShiftLeft` up. A key you hold down stays down until you release it; natemu does not repeat it (the guest's own
autorepeat may).

`pointer_motion` is relative: the PS/2 mouse reports movement, not positions. A large motion is delivered whole: dx
1000 moves the guest's pointer 1000 units (natemu splits it into several mouse reports; on a diagonal the path bends,
the end point is exact). How far that moves the guest's cursor on screen depends on the guest's mouse acceleration.
Each `dx` and `dy` of a `pointer_motion`, and each `dy` of a `scroll`, may be at most 65535 in size (-65535 to 65535).
A larger one makes the whole request fail with -32602, whose `data` names the field (for example
`events[1].dx: at most 65535 in magnitude`), and none of its events is applied: natemu checks the whole list before it
sends anything.

The wheel works once the guest has switched the mouse to its wheel mode (Linux does so when it finds the mouse).
Before that, a `scroll` is accepted and has no effect.

### Key names

Keys are named by where they sit on a US keyboard, as the W3C's KeyboardEvent `code` values (w3.org/TR/uievents-code),
not by the character they type: `KeyQ` is the key left of `KeyW` whatever layout the guest uses. The names are case
sensitive.

- Letters: `KeyA` to `KeyZ`.
- Digits on the main block: `Digit0` to `Digit9`.
- `Enter`, `Escape`, `Backspace`, `Tab`, `Space`, `Minus`, `Equal`, `BracketLeft`, `BracketRight`, `Backslash`,
  `Semicolon`, `Quote`, `Backquote`, `Comma`, `Period`, `Slash`, `CapsLock`.
- Modifiers: `ShiftLeft`, `ShiftRight`, `ControlLeft`, `ControlRight`, `AltLeft`, `AltRight`, `MetaLeft`,
  `MetaRight` (the Windows or Command keys), `ContextMenu`.
- Function row: `F1` to `F12`, `PrintScreen`, `ScrollLock`, `Pause`.
- `Insert`, `Home`, `PageUp`, `Delete`, `End`, `PageDown`, `ArrowRight`, `ArrowLeft`, `ArrowDown`, `ArrowUp`.
- Numeric keypad: `NumLock`, `NumpadDivide`, `NumpadMultiply`, `NumpadSubtract`, `NumpadAdd`, `NumpadEnter`,
  `Numpad0` to `Numpad9`, `NumpadDecimal`.
- Keys of other layouts: `IntlBackslash` (the extra key beside the left Shift on European keyboards), `IntlRo` and
  `IntlYen` (Japanese keyboards).

`Pause` sends its press only: a real keyboard sends nothing when it is released, so its release event does nothing
and it never counts as held.

## Held keys and buttons

The VM has one keyboard and one mouse, shared by every connected client, so natemu keeps track of what is held down
over all of them:

- A key is down in the guest while at least one client holds it. Each client either holds a key or not: a client that
  presses a key it already holds changes nothing, and one release from that client lets go of it. The guest sees the
  press when the first client takes the key and the release when the last one lets go.
- A mouse button is down while any client holds it.
- When a client disconnects, for any reason, everything it held is let go: the guest sees the releases of the keys
  and buttons nobody else holds. A client that crashes never leaves a key stuck, and never releases what another
  client holds.

## Flow control

The guest reads input from the keyboard controller, which holds only a few bytes. natemu keeps the rest and passes it
on as the guest reads, so nothing is lost or overwritten however fast a client sends: 200 keys in one `send_input` all
arrive, in order. `send_input` is answered when the last of its events has been passed on, so a client that waits for
each answer knows the guest can see what it sent.

natemu passes the bytes on as fast as the guest reads them, much faster than a real keyboard, so a guest program can
fall behind even though its kernel got everything. A Linux program that reads the keyboard's `/dev/input/event*`
device (a display server, for example) is kept at most 64 events, about 20 key transitions (10 keys pressed and
released), and loses what had piled up when it lets more arrive before reading. To be sure such a program sees every
key, send a few keys per `send_input` and let it read them before sending more (the guest cases in
`tests/devices/run_control.py` send 6 and wait until the guest reports them).

If the guest takes nothing for 5 seconds (it hangs, or does not read the keyboard or mouse at all), `send_input` fails
with -32003. The events not yet passed on are dropped, and what the guest already got stands: a key whose press
reached the guest stays held by that client until the client releases it or disconnects, and a press that did not
reach it is forgotten. Events from different clients are interleaved one event at a time; a key's bytes are never
mixed with another's.

Requests from one client are carried out one after another, so a long `send_input` delays that client's later
requests. Other clients are still answered meanwhile, though their input waits in the same keyboard and mouse queue.

## The `stopped` notification

When the run ends, every connected client gets one notification, and then natemu closes the connection:

```json
{"jsonrpc":"2.0","method":"stopped","params":{"reason":"power_off"}}
```

| `reason` | The run ended because |
| --- | --- |
| `power_off` | the guest powered off |
| `reset` | the guest reset the machine (natemu ends the run at a reset) |
| `panic` | the guest kernel panicked (reported through the pvpanic device) |
| `cancelled` | it was cancelled: `quit`, Ctrl-A x, Ctrl-C or SIGTERM |
| `error` | anything else: a processor stopped on something natemu does not support, the watchdog, a host error |

natemu waits at most 1 second for the notification to be written; a client that is not reading may not get it. A
client that closed its sending side gets the answers to what it sent and then the connection closes, so it never
gets `stopped`.

## The `display_changed` notification

With a display, every connected client is told when the guest's mode changes (a mode set, a page flip that moves the
offset, the display turned off):

```json
{"jsonrpc":"2.0","method":"display_changed","params":{"enabled":true,"width":640,"height":480,"stride":3200,"offset":51220,"format":"x8r8g8b8"}}
{"jsonrpc":"2.0","method":"display_changed","params":{"enabled":false,"width":0,"height":0,"stride":0,"offset":0}}
```

At most one is sent every 50 ms: changes within that time after one are announced together at its end, with the
mode as it is then, so the last notification always gives the current state. A guest that flips pages quickly
therefore costs a client at most 20 lines a second. Only changes after a client connected are announced; ask
`query_status` for the state when you connect.

## Errors

| Code | Meaning |
| --- | --- |
| -32700 | Parse error: the line is not JSON or not UTF-8, or it is longer than 1 MiB (only then is the connection closed) |
| -32600 | Invalid request: not a request object, `jsonrpc` not `"2.0"`, `method` not a string, `params` neither object nor array, an id that is not a number, string or null, an unknown member, an empty batch |
| -32601 | Method not found |
| -32602 | Invalid params: wrong or missing parameters, an unknown event type or key name, a motion beyond 65535; `data` says what |
| -32001 | The device the request needs is absent: `data` is `{"device": "ps2"}`, `{"device": "power_button"}` or `{"device": "display"}` |
| -32002 | The run is stopping: the request was not carried out |
| -32003 | The guest took no input for 5 seconds; the events not yet delivered were dropped |
| -32004 | The display shows no picture: the guest has not turned it on (`snapshot`, `framebuffer`) |
| -32005 | The picture is larger than a snapshot takes (2560 x 1600); `data` names the sizes |

An error's `message` is a short English phrase; programs should look at `code`.

## Limits

- 8 connections at once; a ninth is closed at once.
- A line a client sends is at most 1 MiB. That is a protocol bound on requests only; natemu's lines have none (a
  `snapshot` of 2560 x 1600 can be over 16 MiB of base64).
- natemu never waits for a slow client. Responses wait in a queue for each client; a client that lets more than 1 MiB
  pile up, or whose socket takes nothing for 1 second, is disconnected (and what it held is let go). The run goes on.
  One answer larger than 1 MiB (a snapshot, or a large batch) is queued when nothing else waits for the client, so
  a client that reads its answers gets it whole; anything queued behind it then counts against the 1 MiB. A client
  that sends many requests without reading the answers should read them as it goes.
- A `pointer_motion`'s `dx` and `dy` and a `scroll`'s `dy` are at most 65535 in size.

## Stability

This is protocol 1, reported by `query_status`. Names on the wire do not change: renaming a method, a field, an event
type or a key name would be a new protocol version. New methods, new event types, new key names, new members in a
result and new notifications may be added within protocol 1, so a client should ignore members and notifications it
does not know. The display's methods, error codes and notification were added this way.

## Examples

With `socat`, interactively (type a request and press Enter; each answer appears as a line):

```sh
socat - UNIX-CONNECT:vm.sock
{"jsonrpc":"2.0","id":1,"method":"query_status"}
{"jsonrpc":"2.0","id":1,"result":{"protocol":1,"state":"running","vcpus":1,"uptime_ms":4210,"devices":{"ps2":true}}}
```

A program that closes its sending side still gets the answers to what it sent, so a one-shot request works too
(`socat` waits half a second for them by default):

```sh
echo '{"jsonrpc":"2.0","id":1,"method":"quit"}' | socat - UNIX-CONNECT:vm.sock
```

In Python, with only the standard library:

```python
import json, socket

class Control:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.lines = self.sock.makefile("r", encoding="utf-8")
        self.next_id = 0

    def call(self, method, params=None):
        self.next_id += 1
        request = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            request["params"] = params
        self.sock.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode())
        while True:
            line = self.lines.readline()
            if not line:  # natemu closed the connection (the run ended): no answer is coming
                raise ConnectionError("natemu closed the control connection")
            message = json.loads(line)
            if isinstance(message, list):
                continue  # a batch's answer (this client sends none)
            if message.get("id") == self.next_id:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message["result"]
            # A notification (such as "stopped"): handle or ignore it.

    def tap(self, code):
        self.call("send_input", {"events": [
            {"type": "key", "code": code, "pressed": True},
            {"type": "key", "code": code, "pressed": False},
        ]})

vm = Control("vm.sock")
print(vm.call("query_status"))
for code in ["KeyL", "KeyS", "Enter"]:
    vm.tap(code)
vm.call("send_input", {"events": [{"type": "pointer_motion", "dx": 100, "dy": 50}]})
```
