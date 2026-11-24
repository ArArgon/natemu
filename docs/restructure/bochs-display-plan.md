# Bochs display: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

Status: draft, November 24, 2026, revised the same day after an independent review against Linux 6.12's `bochs`
and DRM fbdev code and QEMU's `bochs-display` (accept with required changes: the guest observes and draws through a
KMS program, not `/dev/fb0`; X_OFFSET; invalid geometry while enabled; register semantics as `bochs-display`; folded in
below). Owner direction (same day): mapped device memory first, then a Bochs-style display on
it; no VNC; frames for tests and debugging are PNG snapshots made with an existing crate; virtio-gpu is deferred to a
later milestone; input and frames go through the control API. This plan stands on two branches that land first:
mapped device memory (`docs/restructure/mapped-device-memory-plan.md`: `DeviceMemory`, mapped BARs, the read-only
descriptor) and the control API (`docs/restructure/control-api-plan.md`: its extension rules for `snapshot`,
`framebuffer` and `display_changed`).

**Goal:** a guest Linux kernel's `bochs` DRM driver binds a natemu display, sets a mode and draws into a framebuffer
the guest writes natively; a program on the Mac gets PNG snapshots of the visible picture, or maps the framebuffer
itself, through the control socket.

**Architecture:** a PCI function with the identity of QEMU's `bochs-display` (1234:1111, class 0380H: a display that
is not VGA): BAR0 the framebuffer, a mapped region (`DeviceMemory`); BAR2 a 4 KiB MMIO page holding the EDID block, the
Bochs DISPI registers at 500H and the extended registers at 600H. No legacy VGA (no A0000H window, no 3C0H ports, no
VGA BIOS): natemu boots kernels directly and the console stays on serial, and Linux's `bochs` driver drives the class
0380H function through BAR2 alone. The host keeps the current mode (width, height, stride, offset) under the
function's lock; the control server reads the framebuffer through `DeviceMemory::view()` for snapshots and hands out
its read-only descriptor for `framebuffer`.

**Tech stack:** Rust: `crates/host` (new module `display`), `crates/cli`; new crates `png` (image-rs, MIT/Apache-2.0:
the encoder the owner asked for) and `base64` (MIT/Apache-2.0: the snapshot's JSON encoding). Python for the guest
cases, with a small PNG reader in `tests/lib` on the standard library's `zlib`.

**Spec:** the Design section; QEMU's `docs/specs/standard-vga.rst` (the DISPI and MMIO register layout, as a
specification); VESA E-EDID 1.4 (the 128-byte base block). Behaviour references only, no code adapted (GPL): QEMU's
`hw/display/bochs-display.c` and Linux's `drivers/gpu/drm/tiny/bochs.c`.

## Global constraints

- Default launch unchanged: no display unless `[display] device = "bochs"`; every benchmark keeps it off, and every
  existing test is byte-for-byte as at the base.
- Guest framebuffer writes run natively (a mapped region): no exit per pixel; register accesses (BAR2) exit as any
  MMIO. A bench case shows it (Task 4).
- 32 bits per pixel, the only depth Linux's `bochs` driver sets, in either byte order the extended register selects
  (little-endian x8r8g8b8 by default; big-endian when the guest writes BEBEBEBEH to 604H, as the driver does for
  `DRM_FORMAT_BGRX8888`); snapshots convert from the selected order and `framebuffer` reports it.
- Register writes only store values (as QEMU's `bochs-display`; not std-VGA's validating writes): nothing is refused
  at write time and nothing in VRAM is cleared. Whether a picture is shown is decided when the mode is evaluated,
  after every register write, by `bochs-display`'s rule: ENABLED set, BPP 32 (16 bpp is not offered), width and
  height at least 64, and `offset + stride x height` within the framebuffer. A geometry that fails it (including the
  intermediate states the driver passes through while it pans) unpublishes the mode until a later write makes it valid
  again; ENABLE is never cleared by the device.
- The framebuffer size is fixed at launch (`vram_mib`, a power of two from 4 to 256, default 16), and must hold two
  frames of the preferred mode (`vram_mib` MiB at least 2 x width x height x 4: Linux's `drm_vram_helper_mode_valid`
  filters out a mode larger than half of VRAM); the mapped-memory limits apply.
- New crates are named with their reason in the commit that adds them; nothing GPL or LGPL is adapted.
- Rust unit tests run in the framework as one unit (`tests/unit/family.toml`'s `rust/all`); each new check is shown to
  fail against a deliberate break named in its step. Tests only through `tools/natemu-test`; the gate passes before
  merge.
- Branch `bochs-display` off `m2-peripherals` after mapped memory and the control API are merged there. Commit with
  pathspecs.

## Review Focus

- **The picture the guest drew is the picture the snapshot shows:** the guest's KMS program (`kms_pattern`, Task 3)
  scans out a 640 x 480 framebuffer whose pitch is 3,200 bytes (a dumb buffer 800 pixels wide) at a nonzero offset,
  written through the buffer's own mapping (the mapped BAR, not fbdev's shadow); the PNG's pixels equal the pattern's
  visible part in the right byte order (Task 3 `snapshot_matches_guest_pattern`; break: read B and R swapped, or
  ignore X_OFFSET). The expected offset comes from the device's registers through `framebuffer`, never assumed:
  the device offset is the framebuffer's `offsets[0]` plus the buffer object's place in VRAM.
- **A snapshot while the guest changes mode, flips or disables the display:** a snapshot never reads past the framebuffer and
  never mixes two modes' geometry: it copies the mode under the lock and clips to the framebuffer; with the display
  disabled it answers an error (Task 1 `mode_change_is_atomic_for_readers`, Task 3 `snapshot_while_disabled`; break:
  read the registers without the lock).
- **A guest that never loads a driver:** `snapshot` answers -32004 "display not enabled", never an empty image, and
  `query_status` says so (Task 3 `snapshot_while_disabled`).
- **Mode registers written in any order:** Linux writes ENABLE 0, then BPP, XRES, YRES, BANK, VIRT_WIDTH, VIRT_HEIGHT,
  X/Y_OFFSET, then ENABLE with LFB; on a page flip it writes VIRT_WIDTH, X_OFFSET, Y_OFFSET with ENABLE set, passing
  through invalid states; each write re-evaluates the mode (Task 1 `registers_apply_in_any_order`,
  `flip_through_invalid_states`: the published mode is valid or absent after every write and correct after the last;
  `x_offset_moves_the_base`).
- **The framebuffer moves:** Linux may reassign BAR0; a mapped client keeps a valid mapping (the object, not the
  address, is shared) and snapshots follow the new base (Task 3 case `display` writes after a BAR move done by the
  mapped-memory case's method; break: none needed beyond mapped memory's own).

## Design

### The function (`crates/host/src/display/bochs.rs`)

- Identity: vendor 1234H, device 1111H, revision 2, class 0380H, subsystem 1AF4H:1100H, no interrupt pin, header type
  0. BAR0: `vram_mib` of 32-bit memory, mapped (`DeviceMemory`); BAR2: 4 KiB of 32-bit memory.
- BAR2 layout (as `standard-vga.rst`): 000H-07FH the EDID block (128 bytes, read-only; 080H-3FFH read 0); 400H-41FH
  reserved (VGA ports on a VGA-class device; here read 0, writes dropped); 500H-515H the DISPI registers, 16 bits
  each at 500H + 2 x index; 600H-607H the extended registers: 600H the extended region's size (8), 604H the framebuffer
  byte order (reads 1E1E1E1EH, little-endian; a write of BEBEBEBEH, big-endian, is ignored).
- DISPI registers: 0 ID (reads B0C5H; writes stored and ignored), 1 XRES, 2 YRES, 3 BPP, 4 ENABLE (bits ENABLED 01H,
  LFB_ENABLED 40H and the rest stored, none acted on beyond ENABLED), 5 BANK, 6 VIRT_WIDTH, 7 VIRT_HEIGHT, 8 X_OFFSET,
  9 Y_OFFSET (all read back as written, as `bochs-display`), 10 VIDEO_MEMORY_64K (read-only, `vram_mib` x 16, which the
  driver checks against BAR0's size). No GETCAPS maxima and no VRAM clearing: `bochs-display` has neither, and
  clearing on Linux's every modeset would zero memory TTM may have given another buffer.
- Evaluation after every write, `bochs-display`'s `get_mode`: stride = VIRT_WIDTH x 4 (VIRT_WIDTH 0 means XRES),
  offset = Y_OFFSET x stride + X_OFFSET x 4, valid when the constraint above holds. A valid result is published as
  `Mode { width, height, stride, offset, big_endian }` with a change counter; an invalid one unpublishes.
- The EDID block (`display/edid.rs`): manufacturer "NTM", product 1, a detailed timing for the preferred mode
  (`[display] width`/`height`, default 1024 x 768, 60 Hz, CVT reduced blanking), standard timings for 800 x 600 and
  640 x 480, a monitor name descriptor "natemu", checksum. Linux's driver reads it when the extended region is present
  and builds its mode list from it.

### Host side and the control API

- `Display` (`display/mod.rs`): the function, its `DeviceMemory`, the published mode. `Display::snapshot() ->
  Result<Frame>` copies the visible area row by row through `view()` into an RGB buffer (dropping X, reordering B, G,
  R), with the mode read once under the lock; `Display::framebuffer() -> (Mode, BorrowedFd)`.
- PNG: `png` crate, 8-bit RGB, default compression; at most 2560 x 1600.
- Control API (by its extension rules): `snapshot {"format":"png"}` -> `{"width","height","png": base64}`; errors
  -32001 (no display) and -32004 (display not enabled). `framebuffer {}` -> `{"width","height","stride","offset",
  "format":"x8r8g8b8","size"}` plus the read-only descriptor as `SCM_RIGHTS` (the control plan's rule: the client reads
  with `recvmsg`). `display_changed` notification `{"width","height","stride","offset","enabled"}` on every published
  mode change, coalesced to at most one per 50 ms per client. `query_status.devices.display`: `null` or the mode.
- A snapshot response may exceed the control plan's 1 MiB request-line limit; the limit is on requests only (the
  control branch's documentation fix).
- `crates/cli`: `[display] device = "bochs"` (default none), `vram_mib` (16), `width`, `height` (1024, 768: the EDID's
  preferred mode); refused when `vram_mib` cannot hold two preferred frames.
- BAR0 is prefetchable in QEMU; natemu's `BarKind` has no prefetchable flag, and Linux's driver does not need one, so
  BAR0 is plain 32-bit memory (a stated difference, not worth a `BarKind` change). The display is a PCI function, so it gives the guest a PCI bus even when every virtio
  device is on MMIO, as `pci_testdev` does.

## File structure

| File | Responsibility |
| --- | --- |
| `crates/host/src/display/mod.rs` (new) | `Display`, `Mode`, `Frame`, snapshot and framebuffer access |
| `crates/host/src/display/bochs.rs` (new) | the PCI function: configuration, BAR2 registers, mode validation |
| `crates/host/src/display/edid.rs` (new) | the EDID block |
| `crates/host/src/control/*` | `snapshot`, `framebuffer`, `display_changed`, status |
| `crates/host/src/linux.rs` | the display on the bus; `Prepared::display()` |
| `crates/cli/src/config.rs`, `crates/cli/src/config/tests.rs`, `docs/configuration.md`, `docs/control-api.md` | `[display]`; the methods |
| `tests/lib/png_read.py` (new), `tests/lib/control_client.py` | a PNG reader (zlib, filters 0-4); `snapshot` and `framebuffer` |
| `tests/devices/run_display.py`, `tests/devices/kms_pattern.c` (new), `testkit/inputs.py`, `tests/devices/family.toml` | the guest cases; the guest's KMS program and its container build |
| `tests/bench/...` (the bench family the framework uses for guest benchmarks) | `display-fill` |
| `README.md`, `docs/benchmarks.md` | one sentence that a display exists and how to look at it; the bench row |

## Tasks

### Task 1: the function and its registers

**Files:** `crates/host/src/display/{mod.rs,bochs.rs,edid.rs}`, `crates/host/src/lib.rs`.

**Interfaces:** `Display::new(vram: Arc<DeviceMemory>, preferred: (u32, u32)) -> Arc<Display>` implementing
`pci::Function` with BAR0 mapped; `Display::mode() -> Option<Mode>`, `Display::changes() -> u64`; `edid::block(w, h)
-> [u8; 128]`.

- [ ] **Step 1: unit tests**: `identity_and_bars` (IDs, class, BAR0 mapped of `vram_mib`, BAR2 4 KiB),
  `id_reads_b0c5`, `registers_read_back_as_written`, `evaluation_rule` (BPP 24 unpublished, width 63 unpublished, a mode
  past the framebuffer unpublished, 1366 x 768 x 32 published, 1024 x 768 x 32 published), `registers_apply_in_any_order`,
  `flip_through_invalid_states`, `x_offset_moves_the_base`, `video_memory_reads_vram`, `endianness_register` (reads
  1E1E1E1EH; BEBEBEBEH selects big-endian in the published mode; 1E1E1E1EH back), `mode_change_is_atomic_for_readers` (a reader thread taking modes while a
  writer alternates two geometries never sees a mixed one), `edid_checksum_and_preferred_mode` (the block's sum is 0
  mod 256; the first detailed timing decodes to the preferred size).
- [ ] **Step 2: run**; FAIL. **Step 3: implement.** **Step 4: run**; PASS. Show `evaluation_rule` fails with the
  size check removed, `x_offset_moves_the_base` with X_OFFSET ignored, and `mode_change_is_atomic_for_readers` with an unlocked read.
- [ ] **Step 5: commit** `display: the Bochs display function and its registers`.

### Task 2: launch and configuration

**Files:** `crates/host/src/linux.rs`, `crates/cli/src/*`, `docs/configuration.md`.

- [ ] **Step 1: tests**: config tests for `[display]` (keys, defaults, `vram_mib` limits, unknown device refused);
  a `tests/devices` case `display-probe`: the guest loads `bochs` (a module in Debian's kernel; the family's initramfs
  recipe adds it and `drm_kms_helper`, and the self-check fails without them), and `/sys/class/drm/card0` exists,
  `card0-Virtual-1/modes` lists 1024x768 first and 800x600 and 640x480, dmesg names `bochs-drm` with
  `Found bochs VGA, ID 0xb0c5.` and `Found EDID data blob.`, and `/dev/dri/card0` exists.
- [ ] **Step 2: run**; FAIL. **Step 3: implement.** **Step 4: run**; PASS.
- [ ] **Step 5: commit** `display: a Bochs display on the PCI bus from [display]`.

### Task 3: snapshots and the framebuffer through the control API

**Files:** `crates/host/src/display/mod.rs`, `crates/host/src/control/*`, `docs/control-api.md`,
`tests/lib/{png_read.py,control_client.py}`, `tests/devices/run_display.py`, `tests/devices/kms_pattern.c` (new),
`testkit/inputs.py` (the display inputs recipe), `tests/devices/family.toml`.

The guest draws with `kms_pattern`, a static x86-64 C program of raw DRM ioctls (kernel UAPI headers only, no libdrm),
built by the display inputs recipe with `gcc -static` in the amd64 Debian container `testkit/inputs.py`'s
`_in_container` already provides (the declared `container` prerequisite), and put in the case's initramfs. It opens
`/dev/dri/card0`, creates an 800 x 480 dumb buffer (pitch 3,200), adds a 640 x 480 framebuffer on it with
`pitches[0]` 3,200 and `offsets[0]` 16 x 3,200, maps it, writes the pattern (each pixel's colour a function of x and y,
known to the runner), sets the CRTC to the 640 x 480 mode from the connector's list, prints `READY` and sleeps holding
the mode (fbdev restores its own mode when the program exits). Its `bench` mode instead copies full frames into the
mapped buffer for a byte count and prints the elapsed time. `/dev/fb0` is not used for checks: DRM's fbdev
emulation draws into a shadow in guest RAM and copies it to VRAM about every 50 ms, and cannot set a pitch or offset.

- [ ] **Step 1: unit tests**: `snapshot_converts_bgrx_to_rgb`, `snapshot_converts_big_endian`, `snapshot_honours_stride_and_offset`,
  `snapshot_while_disabled` (-32004), `png_round_trips` (decode with the `png` crate's decoder in the test),
  `display_changed_is_coalesced`; `tests/lib` unit tests for `png_read.py` against PNGs the `png` crate wrote (filters
  0-4, checked in as small fixtures generated by a Rust test, or generated by the case at run time).
- [ ] **Step 2: guest cases**: `display` (1 vCPU, smoke): after the probe the init runs `kms_pattern`; at `READY` the
  runner takes a `framebuffer` (stride 3,200, width 640, height 480, an offset whose X part is nonzero unless the buffer
  happened to land aligned: the judge uses the reported offset), maps it read-only and compares every visible pixel with
  the pattern, takes a `snapshot` and compares the same pixels (`snapshot_matches_guest_pattern`), and checks a
  `display_changed` arrived for the mode set; `display-boot-console` (the fbcon text console: the init writes a line of
  text to `/dev/tty1`, waits 200 ms for fbdev's deferred copy, and the snapshot is not uniform);
  `display-off` (no `[display]`: `snapshot` gives -32001, no 1234:1111 in `lspci -n`).
- [ ] **Step 3: run**; FAIL. **Step 4: implement.** **Step 5: run**; PASS. Show `display` fails with B and R swapped
  and with the offset ignored.
- [ ] **Step 6: commit** `display: PNG snapshots and the framebuffer over the control socket`.

### Task 4: cost, documentation, gate

**Files:** the guest bench family, `docs/benchmarks.md`, `README.md`, `docs/configuration.md`.

- [ ] **Step 1: bench case `display-fill`**: `kms_pattern bench` copies 1 GiB of full 1024 x 768 frames into the mapped
  dumb buffer and reports the elapsed time, with the same program writing the same amount into a guest-RAM buffer as
  the in-guest reference (the ratio shows the framebuffer runs at RAM speed: the claim this plan makes); the
  control-socket snapshot cost in the same case (100 snapshots, mean time per PNG). The control group: QEMU's
  `bochs-display` on the same Mac running the same guest program (`-device bochs-display`), as AGENTS.md's benchmark
  rules ask. Recorded on the M4 in `docs/benchmarks.md` with case, machine, commit and date.
- [ ] **Step 2:** README: one plain sentence that natemu can show a simple screen to programs that ask for pictures
  through the control socket (no window yet), linking `docs/control-api.md`.
- [ ] **Step 3: run** `tools/natemu-test run` then `--gate`; PASS. **Commit** `display: bench case and documentation`.

## Cost

Nothing without `[display]`. With it: guest pixel stores are native; each mode set or pan is a few MMIO exits; a
snapshot copies the visible area once (3 MiB at 1024 x 768) and encodes it (measured by `display-fill`). No dirty
tracking: a front end that wants motion maps the framebuffer and reads it at its own rate.

## Out of scope

Legacy VGA (text mode, A0000H, the VGA BIOS) and a boot console on the display before Linux's driver loads; a native
window; dirty-rectangle tracking; hardware cursor; virtio-gpu and 3D; audio; resolution changes requested by the host
(EDID is fixed at launch); multiple displays.
