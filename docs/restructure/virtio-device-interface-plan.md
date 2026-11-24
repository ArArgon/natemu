# Virtio device interface and virtio-console: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

Status: revised November 23, 2026, after an independent review (accept with required changes: five blocking findings
on test selection, locking, backpressure, power-off drain and oversize transmits, all folded in below); a second check
confirmed those and found that a busy request could block reset and a stalled socket client could make power-off
fatal: folded in. Owner
direction (same day): plan the device-memory track (proposed as "zero-copy DMA") separately and have it reviewed.
Research for this plan changed its scope; the Decision section says how and why, and the owner may reject that change.

As built (November 23, 2026): implemented on branch `claude/virtio-model-d26cc7` (commits `e4701a6e`..`afada82e` and
the fix wave on top); this page now states what was built. It deviates from the reviewed plan in: the 64 KiB bounds on
a model's messages and replies (the interface), the socket backend's busy guard held for its whole life, the PCI
console's slot (after the disks'), `boot/acpi.rs` left unchanged, the guest modules (`virtio_mmio` is a module), and
both guest cases' inputs and method (Task 2 and Task 3 Step 5). The integration gate (Task 3 Step 6) is still to run.

**Goal:** one small interface through which a new virtio device (virtio-console now, vsock next, later sound, input
and GPU) plugs into natemu's existing virtio owner and its windowless guest-memory copies, plus virtio-console as its
first consumer: a second guest console on a host file or a Unix socket.

**Architecture:** today the device set is a closed `enum Profile { Entropy, Block, Network }` matched in 27 places in
`crates/host/src/virtio`. A fourth variant, `Profile::Model(Arc<dyn Model>)`, carries any device that works on owned
buffers: the owner captures chains, hands the device the readable bytes of a request, and copies its reply and any
host-originated data back through `Virtio::copy` (windowless, with code-page invalidation and the full-window
fallbacks). Block and network keep their tuned paths; entropy stays as it is.

**Tech stack:** Rust, `crates/host`. New dependencies are allowed when judicious (owner, November 24): name each, with its reason, in the commit that adds it; never a project's internal workspace crates.

**Spec:** the Design section below; virtio 1.2 §5.3 (console), §4.1 and §4.2 (PCI and MMIO transports);
`docs/restructure/io-plan.md` "Windowless copies with targeted invalidation" (the copy contract this plan keeps).

## Decision: no zero-copy DMA now

The track was proposed as direct (zero-copy) guest-memory access for devices, on the premise that memory windows
pausing every vCPU were the dominant I/O cost. That premise is out of date:

- Since November 10 every virtio copy of a running VM is windowless (`Backing::copy_guest_windowless`,
  `crates/host/src/execution/io.rs`; called from `virtio/mod.rs` `copy_while`): it checks page marks, copies, posts
  code-page invalidations to the vCPUs holding translations and waits only for them before publishing. A full window
  runs only when a written page carries a page-table mark, or when that wait outlasts `DRAIN_DEADLINE` (50 ms). In the
  windowless measurement (experiment, `io-benchmarks.md` "Windowless copies — November 10", four vCPUs) windows fell
  from about 5 M per run to 7-12, about 0.003% of wall time.
- The per-request profile after that change (experiment, `io-benchmarks.md` "Per-request profile after windowless
  copies — November 10", 4 KiB random reads) splits each request into translated guest code, host exits, block
  dispatch, the notify-to-admission hand-off and interrupt delivery. It has no row for host copies: the copy was not
  measured separately (it is inside "block dispatch"), and 4 KiB reads are the least copy-heavy load. Large
  transfers may weigh more (the reviewer estimates 3-6 µs of memcpy in a 128 KiB request: an estimate, not a
  measurement).
- What zero-copy would add and windowless copies avoid: a system call writing into guest RAM cannot honour a
  `CopyPermit` cancel or a device reset while it runs; it can leave partial data on error (a revision of INV-03-04
  claims 27-29); and the code-mark pre-check, recheck and page-table fallback would have to bracket a kernel write
  that natemu does not perform itself. (Split locks do not separate the designs: windowless copies do not consult
  the split lock either, under io-plan's accepted rule that a guest atomic on a buffer it gave the device is a guest
  bug with no effect on natemu's own state; that rule applies to `Model` devices unchanged.)

So this plan keeps owned buffers and windowless copies, and gives new devices a clean interface over them. Adapted
upstream device logic (OpenVMM's vsock connections, cloud-hypervisor's console) sits above it on owned buffers, the
reuse boundary the OpenVMM review recommended. The costs the windowless path still has are known and counted
(`table_fallbacks` from permanent table marks, `drain_expiries`, batches split at `MAX_BYTES`/`MAX_SPANS`; the `io`
perf-log line).

**Revisit zero-copy** with its own plan when a framework bench case that attributes per-request time to host copies
(to be built; it does not exist: `tests/bench/family.toml` has whole-workload cases only), run on the machine that
holds the control group, shows copies at 10% or more of per-request time for one of 4 KiB random reads, 128 KiB
sequential reads, or network TX/RX; or when a device's payload volume (display) makes copying its bottleneck.

## Global constraints

- Block, network and entropy behaviour unchanged: their register traces, features, queue handling and every existing
  test are byte-for-byte as at this plan's base commit.
- A `Model` never touches guest RAM or a guest address: it sees owned byte slices and returns owned buffers; all guest
  memory access stays in the owner (`Virtio::copy`), with today's validation (`check_rings`, `descriptors_in`).
- **Locking.** `Model::started`, `Model::reset` and the `on_ready` callback run under the device's state lock: they
  only record state and signal the model's own thread; they never call `ModelPort` and never block. `Model::request`
  and `Model::drain` run without the state lock, return promptly and never wait for the guest or a backend.
  `ModelPort` holds a `Weak<Virtio>` (as `PacketPort` does), so the `Arc<dyn Model>` inside `Virtio` forms no cycle.
- Backend I/O runs on the model's own thread, which holds an `irqs.busy()` guard for its whole life (as `PacketPort`
  does; the console's socket backend holds it also while no client is connected, since one may connect and send), so
  a guest waiting for input is not ended as all-halted.
- Guest-visible behaviour follows virtio 1.2; where it leaves a choice, QEMU's `virtio-serial`/`virtconsole`, except
  as stated (the console offers no MULTIPORT and no EMERG_WRITE, which QEMU offers by default).
- Rust unit tests run in the framework as one unit (`tools/natemu-test run tests/unit -k rust`, item `rust/all`); a
  step's named tests are read from that run's `.natemu/runs/<id>/junit.xml`. Runner cases are selected with `-k`.
- Each new check is shown to fail against a deliberate break of the behaviour it pins (named in its step), not only at
  a commit where the feature is absent.
- Tests only through `tools/natemu-test`; the gate passes before merge.
- Branch `virtio-model` off `m2-peripherals`, the integration branch it merges back into after its gate. Commit with pathspecs. Landing order with the sibling plans: platform devices,
  then multi-BAR PCI, then this one (it edits `virtio/pci.rs`, which multi-BAR PCI also edits); rebase onto whatever
  has landed, and "byte-for-byte" is measured against that base.

## Review Focus

- A guest that resets the device (status 0) while host data is queued for it: queued bytes of the old incarnation are
  dropped, never written into the new incarnation's buffers (Task 1 `reset_drops_queued_host_data`; break:
  `retire_reset` not clearing the per-queue backlog).
- Host data arriving faster than the guest posts buffers: `deliver` takes only what fits in posted buffers plus a
  bounded backlog and says how much it took; the producer keeps the rest (Task 1
  `deliver_takes_what_fits_and_reports_it`; break: an unbounded backlog).
- A 1 MiB console write through `splice` (one chain of up to 256 descriptors): delivered to the model whole, in
  pieces of at most `MAX_BYTES`, never NEEDS_RESET (Task 1 `large_transmit_chain_is_delivered_in_pieces`; break: a
  per-request byte cap).
- The host end of the console socket closing mid-write: guest writes keep completing (discarded, counted), and a new
  client receives later output (Task 3 `client_reconnect`).
- A console on the MMIO transport when all MMIO interrupt lines are taken: a launch error naming the device (Task 2
  `mmio_without_free_gsi_is_refused`).

## Design

### The interface

```rust
pub(crate) enum Direction { DeviceReads, DeviceWrites }
pub(crate) struct QueueSpec { pub max: u16, pub direction: Direction, pub stream: bool }
pub(crate) enum Reply { Done(Option<Vec<u8>>), Busy }
pub(super) const BACKLOG: usize = 64 * 1024;            // a DeviceWrites queue's backlog; also the largest message
pub(super) const REPLY: usize = 64 * 1024;              // the largest reply
pub(crate) trait Model: Send + Sync {
    fn device_id(&self) -> u32;                         // virtio 1.2 §5
    fn features(&self) -> u64;                          // offered, besides VERSION_1
    fn accepts(&self, accepted: u64) -> bool;           // the device's feature dependencies
    fn config(&self) -> Vec<u8>;                        // device configuration space, read-only to the guest
    fn queues(&self) -> &[QueueSpec];
    fn pci_class(&self) -> (PciClassCode, u8);          // class and subclass for the PCI transport
    /// A request on a DeviceReads queue: `readable` is one piece (at most MAX_BYTES) of the chain's readable bytes,
    /// `last` true for the final piece. `Busy` leaves this piece unconsumed: the request leaves the owner's in-flight
    /// set and is offered again after `ModelPort::resume(queue)`; a device reset or stop drops it (its chain is never
    /// completed, as a reset abandons any unfinished request), and no piece of an old incarnation reaches a new one.
    /// `Done(reply)`: the reply (at most `writable` bytes) goes to the chain's writable part with the last piece;
    /// earlier pieces return `Done(None)`. `writable` is at most REPLY (64 KiB); a longer reply is cut and counted.
    fn request(&self, queue: u16, readable: &[u8], last: bool, writable: usize) -> Reply;
    fn started(&self, port: ModelPort);                 // DRIVER_OK; under the state lock: record and signal only
    fn reset(&self);                                    // device reset; under the state lock: record and signal only
    /// Power-off drain: true once every accepted transmit byte has reached its backend. Polled, never waits.
    fn drain(&self) -> bool;
}
pub struct ModelPort { /* Weak<Virtio> and the incarnation */ }
impl ModelPort {
    /// For a DeviceWrites queue: takes as many bytes as fit in posted buffers and the queue's bounded backlog
    /// (stream queues split across chains; a message queue takes a message whole or not at all) and returns the
    /// count taken; 0 after the device was reset. A message over 64 KiB (BACKLOG) is taken, dropped and counted.
    pub fn deliver(&self, queue: u16, bytes: &[u8]) -> usize;
    pub fn on_ready(&self, ready: impl Fn() + Send + Sync + 'static);   // buffers were posted (under the lock)
    pub fn resume(&self, queue: u16);                   // a Busy request may be offered again
}
```

**Owner changes** (`virtio/mod.rs`): the receive-side state hard-wired to queue 0 today (`received`, `rx_starved`,
`rx_kicked`, the `i == 0` branches near lines 1020, 1646 and 1697, `accepting(0)`) becomes per queue for `Model`
devices; net keeps its queue-0 behaviour through the same arrays. `queue_max` becomes per queue (`Snapshot::new`,
`check_rings`, the queue-ready write). Stream delivery adds one `Request` outcome: commit the filled chains and keep
the remainder at the front of that queue's backlog under the same identity. A message that does not fit a posted
chain is dropped and counted, as net drops a frame (`lost`); so is a message over `BACKLOG` (64 KiB: one copy must
hold a message and its publication), at `deliver`, and a reply over `REPLY` (64 KiB) is cut to it and counted. The
power-off drain (`drain`, near line 1566) polls `Model::drain` until true before it reports the device quiet. A
model's `drain` must turn true within its own stated grace (the console: 2 s), discarding and counting what its
backend did not take by then, so a stalled backend never turns a guest power-off into a fatal error (net's 10 s
backend bound, which ends the process, does not apply).

### virtio-console

- virtio 1.2 §5.3, device ID 3, single port: queue 0 receiveq (DeviceWrites, stream, max 256), queue 1 transmitq
  (DeviceReads, max 256). Features offered: none. Config space 12 bytes of zero (cols, rows, max_nr_ports, emerg_wr),
  valid without F_SIZE and F_MULTIPORT.
- Backends, one per device: `output = FILE` (transmit appended to the file; nothing to receive) or `socket = PATH` (a
  Unix-domain socket natemu listens on; one client at a time; client bytes are delivered to receiveq with
  backpressure from `deliver`'s count; transmit goes to the client, or is discarded and counted while none is
  connected). The transmit side buffers at most 1 MiB in the model; beyond it `request` returns `Busy` until the
  backend catches up (QEMU throttles its chardev the same way).
- Configuration: `[[console]]` with `id` and exactly one of `output`, `socket`; transport key
  `machine.transport.console` (added to `Transports`, `Wanted` and the transport key list in `config.rs`). Placement
  in `linux.rs`: MMIO takes the next MMIO page and uid after the disks' and the first free GSI in 16-23 (none free:
  launch error); PCI takes the slot after the disks' (the k-th PCI console slot 3 + max(p, 1) + k, p the PCI disks; not
  the first free slot, so a console keeps its slot when a NIC is added), and none left (for example 29 PCI disks) is a
  launch error. The ACPI entries of an MMIO console come from the generic MMIO manifest placement, so
  `boot/acpi.rs` is unchanged.
- PCI class 07H (communication), subclass 80H (other), as QEMU's virtio-serial-pci.
- The guest kernel (Debian 6.12.107+deb13-amd64) has `CONFIG_VIRTIO_CONSOLE=y` and `CONFIG_VIRTIO_PCI=y`, but
  `CONFIG_VIRTIO_MMIO=m`: the guest cases' initramfs carries the stock `virtio_mmio` module and the init runs
  `modprobe virtio_mmio`.

## File structure

| File | Responsibility |
| --- | --- |
| `crates/host/src/virtio/model.rs` (new) | `Model`, `QueueSpec`, `Reply`, `ModelPort` |
| `crates/host/src/virtio/mod.rs` | `Profile::Model`, per-queue receive state, pieces, drain |
| `crates/host/src/virtio/pci.rs` | class codes from `Model::pci_class` |
| `crates/host/src/virtio/console.rs` (new) | the console `Model` and its two backends |
| `crates/host/src/linux.rs` | placement; MMIO ACPI entries through the generic manifest (`boot/acpi.rs` unchanged) |
| `crates/cli/src/config.rs`, `crates/cli/src/config/tests.rs`, `docs/configuration.md` | `[[console]]`, transport key |
| `tests/devices/run_console.py`, `tests/devices/family.toml` | guest cases `console-file`, `console-socket` (the family the platform-devices plan creates; this plan creates it if it lands first) |

## Tasks

### Task 1: `Model` and `Profile::Model`

**Files:** create `virtio/model.rs`; modify `virtio/mod.rs`, `virtio/pci.rs`.

**Interfaces:** produces the items above and `Virtio::model(irqs: Irqs, gsi: usize, model: Arc<dyn Model>) ->
Arc<Virtio>`.

- [x] **Step 1: unit tests** with a test model (an echo device: transmit on queue 1 is delivered back on queue 0
  through its port), on the virtio test rig (`virtio/testing.rs`): `features_and_config_reach_the_driver`,
  `request_reply_lands_in_writable_part`, `deliver_fills_posted_buffers_in_order`,
  `stream_delivery_splits_across_chains`, `message_too_large_is_dropped_and_counted`,
  `deliver_takes_what_fits_and_reports_it`, `busy_request_is_offered_again_after_resume`,
  `large_transmit_chain_is_delivered_in_pieces`, `reset_drops_queued_host_data`, `reset_during_busy_completes` (a
  request held `Busy` with no `resume` does not delay a device reset; break: keep it in the in-flight set),
  `drain_waits_for_model` (S5 does not report quiet while `drain` is false), `pci_class_from_model`. The existing
  entropy, block and net unit tests stay unchanged.
- [x] **Step 2: run** `tools/natemu-test run tests/unit -k rust`; the new tests FAIL in junit.xml (the rest pass).
- [x] **Step 3: implement**; the receive path is parameterized by queue, not copied.
- [x] **Step 4: run** the same (all PASS), then `tools/natemu-test run tests/io` (PASS, behaviour unchanged). Show
  `reset_drops_queued_host_data`, `deliver_takes_what_fits_and_reports_it` and
  `large_transmit_chain_is_delivered_in_pieces` fail against the breaks named in Review Focus.
- [x] **Step 5: commit** `virtio: a Model interface for devices on owned buffers`.

### Task 2: virtio-console with a file backend

**Files:** create `virtio/console.rs`, `tests/devices/run_console.py`; modify `linux.rs` (as built: not
`boot/acpi.rs`), `config.rs`, `config/tests.rs`, `docs/configuration.md`, `tests/devices/family.toml`.

**Interfaces:** `run_console.py prepare` builds a BusyBox initramfs from the framework's inputs as
`tests/io/run_virtio_rng.py prepare` does (as built: with the `virtio_mmio` module); `run_console.py run --case
console-file|console-socket --transport mmio|pci --cpus N`, judged from the host's output file or socket and its exit
line; `self-check`.

- [x] **Step 1: unit tests** `console_offers_no_features_and_zero_config`, `transmit_appends_to_output_file`,
  `transmit_backlog_turns_busy_at_1_mib`, `mmio_without_free_gsi_is_refused`, and in `config/tests.rs`
  `console_needs_exactly_one_of_output_socket`, `console_transport_key`.
- [x] **Step 2: run** `tools/natemu-test run tests/unit -k rust`; FAIL. **Step 3: implement.** **Step 4: run**; PASS.
- [x] **Step 5: guest case `console-file`** (MMIO and PCI, 1 and 2 vCPUs). As built, two sinks: by default the
  console's output is a fifo the runner holds open and reads only 0.5 s after the guest's `reboot: Power down` line on
  COM1, with a 256 KiB pattern: the backend can put only the pipe's buffer (8 KiB) into it before then, so the rest
  reaches the runner only through the power-off drain (2 s grace), and a drain that returns at once loses the tail;
  `--sink file` writes the whole 1 MiB (the device's `TX_BUFFER`) to a regular file natemu creates. The init opens
  `/dev/hvc0` on descriptor 3 and keeps it open (hvc resets its termios when the last descriptor closes), sets it
  `stty raw -echo`, writes the pattern with `dd bs=4096`, drains it (`stty -g` and back, TCSADRAIN) and powers off
  with nothing waiting in between. `dd`'s exit proves delivery: hvc_write hands at most a page to virtio_console's
  `put_chars`, which spins until the device returns the buffer, so `dd` returns only once the model holds every byte
  (`hvc_close`'s 10 ms never applies, the descriptor staying open). The method and the driver-source findings are
  pinned in `tests/devices/run_console.py`'s docstring. The runner checks the bytes are exactly the pattern (no CR
  added), nothing was reported discarded, and the run ends with `Power(1)`; `result.json` records the drain's timing
  margin. Shown to fail with `Model::drain` returning true at once (run 20261123-230735-d383) and pass here.
- [x] **Step 6: commit** `virtio-console: a second console on a host file`.

### Task 3: the socket backend

**Files:** modify `virtio/console.rs`, `config.rs`, `tests/devices/run_console.py`.

- [x] **Step 1: unit tests** `socket_client_bytes_reach_receiveq`, `socket_input_respects_deliver_count`,
  `transmit_without_client_is_counted`, `client_reconnect`, `one_client_at_a_time` (a second connection is refused
  while one is open), `poweroff_with_stalled_client` (a connected client that stops reading: S5 still ends with
  `Power(1)` within the 2 s grace, the discarded bytes counted).
- [x] **Step 2: run** `tools/natemu-test run tests/unit -k rust`; FAIL. **Step 3: implement.** **Step 4: run**; PASS.
- [x] **Step 5: guest case `console-socket`**: the runner makes the socket in a short private directory
  (`tempfile.mkdtemp(prefix='nm-')` under the system temporary directory, removed afterwards, because run folders can
  exceed `sun_path`'s 104 bytes), connects after the guest's ready marker on the serial console, sends
  `ping <nonce>\n` followed by 4 MiB of pattern and never half-closes; the init reads the line, then pauses 3 s without
  reading (so the input fills the guest's receive buffers, about 1.7 MiB, and `deliver` must take less than it is
  offered), reads exactly the pattern from `/dev/hvc0` and writes back the nonce reversed and the pattern's SHA-256;
  the runner checks both and that the socket was removed. As built, not 256 KiB with no pause: that input passed
  against the break (run 20261123-231432-ce74), because the guest took it as fast as it came. Shown to fail with
  `deliver` ignoring its count (input bytes lost under backpressure; run 20261123-230905-9014) and pass here.
- [ ] **Step 6: run** `tools/natemu-test run` then `--gate`; PASS. (As built: the unit and devices runs passed; the
  gate is still to run, once for the integration branch, on the M4.)
- [x] **Step 7: commit** `virtio-console: an interactive console on a Unix socket`.

## Follow-up (own plan)

virtio-vsock on `Model` (message queues, the event queue, guest CID in config), with OpenVMM's connection logic
(`vm/devices/virtio/virtio_vsock/src/{connections.rs,ring.rs,spec.rs,unix_relay.rs}` at `5d6ab1cf`, MIT) adapted onto
`ModelPort` and host Unix sockets; then systemd's `vmm.notify_socket` for test readiness.

What the vsock plan inherits from this interface as built:

- A message queue's `deliver` takes a message whole or not at all, and the owner drops (and counts) a message that
  does not fit the chain it lands in; the model cannot learn the next posted chain's size beforehand. vsock has no
  retransmit, so one dropped RW packet corrupts its connection's stream. The vsock plan needs a way to size packets
  to the chain: a "room of the next posted chain" query on `ModelPort`, or a message mode that fits a message to the
  chain (splitting an RW payload, as stream delivery splits bytes).
- A `Busy` answer parks the request and stops the whole transmit queue until `resume`, not only the connection it
  belongs to. Under vsock's credit flow control a peer sends only what the other side has room for, so this should
  not stall unrelated connections; the vsock plan states it and checks it. The bench case that would
trigger a zero-copy plan (per-request copy share) belongs with the next I/O performance work.
