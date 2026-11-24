# Virtio device interface and virtio-console: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

Status: revised November 23, 2026, after an independent review (accept with required changes: five blocking findings
on test selection, locking, backpressure, power-off drain and oversize transmits, all folded in below); a second check
confirmed those and found that a busy request could block reset and a stalled socket client could make power-off
fatal: folded in. Owner
direction (same day): plan the device-memory track (proposed as "zero-copy DMA") separately and have it reviewed.
Research for this plan changed its scope; the Decision section says how and why, and the owner may reject that change.

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
- Backend I/O runs on the model's own thread; while it may deliver data it holds an `irqs.busy()` guard (as
  `PacketPort` does), so a guest waiting for input is not ended as all-halted.
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
    /// earlier pieces return `Done(None)`.
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
    /// count taken; 0 after the device was reset.
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
chain is dropped and counted, as net drops a frame (`lost`). The power-off drain (`drain`, near line 1566) polls
`Model::drain` until true before it reports the device quiet. A model's `drain` must turn true within its own stated
grace (the console: 2 s), discarding and counting what its backend did not take by then, so a stalled backend never
turns a guest power-off into a fatal error (net's 10 s backend bound, which ends the process, does not apply).

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
  launch error); PCI takes the first free slot (none free, for example 29 PCI disks: launch error).
- PCI class 07H (communication), subclass 80H (other), as QEMU's virtio-serial-pci.
- The guest kernel (Debian 6.12.107+deb13-amd64) has `CONFIG_VIRTIO_CONSOLE=y`: no module to load.

## File structure

| File | Responsibility |
| --- | --- |
| `crates/host/src/virtio/model.rs` (new) | `Model`, `QueueSpec`, `Reply`, `ModelPort` |
| `crates/host/src/virtio/mod.rs` | `Profile::Model`, per-queue receive state, pieces, drain |
| `crates/host/src/virtio/pci.rs` | class codes from `Model::pci_class` |
| `crates/host/src/virtio/console.rs` (new) | the console `Model` and its two backends |
| `crates/host/src/linux.rs`, `crates/host/src/boot/acpi.rs` | placement, MMIO ACPI entries |
| `crates/cli/src/config.rs`, `crates/cli/src/config/tests.rs`, `docs/configuration.md` | `[[console]]`, transport key |
| `tests/devices/run_console.py`, `tests/devices/family.toml` | guest cases `console-file`, `console-socket` (the family the platform-devices plan creates; this plan creates it if it lands first) |

## Tasks

### Task 1: `Model` and `Profile::Model`

**Files:** create `virtio/model.rs`; modify `virtio/mod.rs`, `virtio/pci.rs`.

**Interfaces:** produces the items above and `Virtio::model(irqs: Irqs, gsi: usize, model: Arc<dyn Model>) ->
Arc<Virtio>`.

- [ ] **Step 1: unit tests** with a test model (an echo device: transmit on queue 1 is delivered back on queue 0
  through its port), on the virtio test rig (`virtio/testing.rs`): `features_and_config_reach_the_driver`,
  `request_reply_lands_in_writable_part`, `deliver_fills_posted_buffers_in_order`,
  `stream_delivery_splits_across_chains`, `message_too_large_is_dropped_and_counted`,
  `deliver_takes_what_fits_and_reports_it`, `busy_request_is_offered_again_after_resume`,
  `large_transmit_chain_is_delivered_in_pieces`, `reset_drops_queued_host_data`, `reset_during_busy_completes` (a
  request held `Busy` with no `resume` does not delay a device reset; break: keep it in the in-flight set),
  `drain_waits_for_model` (S5 does not report quiet while `drain` is false), `pci_class_from_model`. The existing
  entropy, block and net unit tests stay unchanged.
- [ ] **Step 2: run** `tools/natemu-test run tests/unit -k rust`; the new tests FAIL in junit.xml (the rest pass).
- [ ] **Step 3: implement**; the receive path is parameterized by queue, not copied.
- [ ] **Step 4: run** the same (all PASS), then `tools/natemu-test run tests/io` (PASS, behaviour unchanged). Show
  `reset_drops_queued_host_data`, `deliver_takes_what_fits_and_reports_it` and
  `large_transmit_chain_is_delivered_in_pieces` fail against the breaks named in Review Focus.
- [ ] **Step 5: commit** `virtio: a Model interface for devices on owned buffers`.

### Task 2: virtio-console with a file backend

**Files:** create `virtio/console.rs`, `tests/devices/run_console.py`; modify `linux.rs`, `boot/acpi.rs`,
`config.rs`, `config/tests.rs`, `docs/configuration.md`, `tests/devices/family.toml`.

**Interfaces:** `run_console.py prepare` builds a BusyBox initramfs from the framework's inputs as
`tests/io/run_virtio_rng.py prepare` does (no module needed); `run_console.py run --case console-file|console-socket
--transport mmio|pci --cpus N`, judged from the host's output file or socket and its exit line; `self-check`.

- [ ] **Step 1: unit tests** `console_offers_no_features_and_zero_config`, `transmit_appends_to_output_file`,
  `transmit_backlog_turns_busy_at_1_mib`, `mmio_without_free_gsi_is_refused`, and in `config/tests.rs`
  `console_needs_exactly_one_of_output_socket`, `console_transport_key`.
- [ ] **Step 2: run** `tools/natemu-test run tests/unit -k rust`; FAIL. **Step 3: implement.** **Step 4: run**; PASS.
- [ ] **Step 5: guest case `console-file`** (MMIO and PCI, 1 and 2 vCPUs): the init writes a 1 MiB pattern to
  `/dev/hvc0` with `dd`, then powers off. Whether `dd`'s return means the device has consumed every byte depends on
  the guest driver (virtio_console's `put_chars` appears to wait for the device to return each buffer; `hvc_close`
  may wait only about 10 ms): the implementer confirms it in `drivers/char/virtio_console.c` and
  `drivers/tty/hvc/hvc_console.c` and, if it does not hold, adds an explicit drain wait to the init; the method is
  pinned in the runner's docstring; the runner checks the output
  file holds exactly the pattern and the run ends with `Power(1)`. Show it fails with `Model::drain` returning true
  at once (the tail is lost) and passes here.
- [ ] **Step 6: commit** `virtio-console: a second console on a host file`.

### Task 3: the socket backend

**Files:** modify `virtio/console.rs`, `config.rs`, `tests/devices/run_console.py`.

- [ ] **Step 1: unit tests** `socket_client_bytes_reach_receiveq`, `socket_input_respects_deliver_count`,
  `transmit_without_client_is_counted`, `client_reconnect`, `one_client_at_a_time` (a second connection is refused
  while one is open), `poweroff_with_stalled_client` (a connected client that stops reading: S5 still ends with
  `Power(1)` within the 2 s grace, the discarded bytes counted).
- [ ] **Step 2: run** `tools/natemu-test run tests/unit -k rust`; FAIL. **Step 3: implement.** **Step 4: run**; PASS.
- [ ] **Step 5: guest case `console-socket`**: the runner makes the socket in a short private directory
  (`tempfile.mkdtemp(prefix='nm-')` under the system temporary directory, removed afterwards, because run folders can
  exceed `sun_path`'s 104 bytes), connects after the guest's ready marker on the serial console, sends
  `ping <nonce>\n` followed by 256 KiB of pattern; the init reads them from `/dev/hvc0` and writes back the nonce
  reversed and the pattern's SHA-256; the runner checks both. Show it fails with `deliver` ignoring its count (input
  bytes lost under backpressure) and passes here.
- [ ] **Step 6: run** `tools/natemu-test run` then `--gate`; PASS.
- [ ] **Step 7: commit** `virtio-console: an interactive console on a Unix socket`.

## Follow-up (own plan)

virtio-vsock on `Model` (message queues, the event queue, guest CID in config), with OpenVMM's connection logic
(`vm/devices/virtio/virtio_vsock/src/{connections.rs,ring.rs,spec.rs,unix_relay.rs}` at `5d6ab1cf`, MIT) adapted onto
`ModelPort` and host Unix sockets; then systemd's `vmm.notify_socket` for test readiness. The bench case that would
trigger a zero-copy plan (per-request copy share) belongs with the next I/O performance work.
