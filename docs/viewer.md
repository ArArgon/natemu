# The viewer

`natemu-viewer` opens a window on the Mac that shows a running natemu's screen and passes your keyboard and mouse to
the guest. It is a separate program: it connects to natemu's control socket ([control-api.md](control-api.md)), so
natemu itself has no window code, and you can start and close the viewer while the VM keeps running.

## Using it

Build it once:

```sh
cargo build --release -p natemu-viewer
```

Start natemu with a display and a control socket, then point the viewer at the socket:

```sh
target/release/natemu ... --set display.device=bochs --set control.socket=vm.sock
target/release/natemu-viewer vm.sock
```

The window opens at the size of the guest's screen and follows it when the guest changes resolution; resizing the
window scales the picture, keeping its shape. Until the guest's display driver loads, the window is black.

- **Keyboard:** while the window has focus, keys go to the guest, named by their place on a US keyboard (the Command
  keys are the guest's Windows/Meta keys). The guest's keyboard layout decides what a key types. Command-Q still quits
  the viewer. Caps Lock does not reach the guest yet: macOS reports it only as a lock state, which winit does not pass on.
- **Mouse:** click in the window to capture the mouse: the Mac pointer disappears and its movement, buttons and wheel go
  to the guest. **Control-Option**, pressed together and let go with no other key, lets it go (as in QEMU's and UTM's
  windows); Control-Alt-F2 or Control-Alt-Delete with the mouse captured still go to the guest. Switching to another
  window also lets go of the mouse and of every key the viewer held down, so nothing stays stuck in the guest. If the
  guest has no mouse driver (Debian's `psmouse` module not loaded), the mouse movement it never reads would hold up the
  keyboard behind it: the viewer notices after natemu's 5-second limit, lets the mouse go and says so.
- **Closing the window** closes the viewer only; the VM keeps running. When the VM stops, the viewer closes and says
  why. It exits with status 0 then and when you close it, and 1 when it cannot go on (no such socket, a VM without a
  display, a lost connection).

## How it works

The viewer opens two connections to the socket. On the first it asks for `framebuffer`, which hands it the
framebuffer's shared memory; it maps that read-only and redraws the window from it about 60 times a second (the Bochs
display does not say which parts changed, and copying a 1920 x 1080 picture costs well under a millisecond). It follows
`display_changed` for the mode and closes on `stopped`. On the second connection it sends `send_input`, one request at
a time; input that arrives meanwhile waits in the viewer, where consecutive mouse movements merge into one. Both
connections are read all the time, since natemu disconnects a client that leaves its notifications unread. When a
request fails partway (the guest stopped reading input), natemu has dropped its remaining events, so the viewer sends
that request's key and button releases again: a key let go meanwhile never stays down in the guest.

The guest's mouse is a PS/2 mouse, which reports movement, not positions, so the Mac pointer cannot simply be placed
over the guest's: the viewer captures it instead and sends relative motion. How far the guest's pointer moves then
depends on the guest's mouse acceleration.

It is built on [winit](https://github.com/rust-windowing/winit) (the window and its events) and
[softbuffer](https://github.com/rust-windowing/softbuffer) (putting pixels in it), both MIT or Apache-2.0.

## Not yet

- Caps Lock (see above).
- Absolute pointing (a tablet device in the guest), which would let the Mac pointer move straight into the guest
  without capture.
- Clipboard, audio, a menu (snapshot, reset, quit the VM).
- virtio-gpu: with its 2D path the same viewer can redraw only what the guest says changed; with 3D the picture is a
  GPU surface shared through Metal, which the viewer would add beside today's shared-memory path.
