# Libslirp usability implementation plan

> **For agentic workers:** use `superpowers:subagent-driven-development` or
> `superpowers:executing-plans`, following the repository's build-first workflow.
> Keep implementation authors and consequential reviewers separate. Completed
> task checks below are qualified by the final result and limits in section 5.

**Goal:** make the existing unprivileged network useful for host–guest work with
TCP/UDP forwarding, optional host-loopback access and a short startup summary.

**Architecture:** retain the existing virtio device, `PacketPort` and single
libslirp owner thread. Configure all forwarding before guest execution and let
the existing stack lifetime own its listeners. No new backend or control thread.

**Tech stack:** Rust host/CLI, clap, existing handwritten libslirp 4.9 bindings,
and the current Python/stock-Linux network runner.

**Status:** implemented, independently reviewed and accepted within the scope
below. Twelve final primary and six independent replay VMs pass. Work started on
`codex/slirp-usability`, November 15, 2026, 19:03 PST (03:03 UTC November 16).
Owner request:
the three features above, kept simple, with host-loopback access disabled by default. The owning
[investigation](../research/investigations/INV-03-01.md#current-io-state--november-11)
records the next action. Both reviewers' verdicts and the correction return are
in the existing [findings thread](../research/reviews/INV-03-01-findings.md#libslirp-usability-implementation--november-15-2026-revised-udp-contract).

**Implementation finding (November 15):** the first retained MMIO acceptance
case passes both TCP transfers across reset but fails both UDP handshakes.
Exact libslirp 4.9.7 source and independent inspection show that
`disable_host_loopback=true` also rejects replies on UDP host-forward sockets.
The owner selected explicit `--net-host-loopback` as a requirement for UDP
forwarding. TCP forwarding remains independent of that flag; the default is
unchanged and no rule enables loopback implicitly. The revised contract below
supersedes the original UDP-with-loopback-off premise. Evidence:
`natemu-raw/slirp-usability-20261115-01/candidate-01/mmio-forward`.

**Runner correction:** candidate-02 passes 389 host/CLI tests, but its first
MMIO case remains failed: a routine ACPI shutdown printk splits the final guest
JSON marker despite positive TCP receive counts. Suppress informational console
printk in the guest fixture while preserving warning/error output, the complete
kernel log and strict JSON parsing. Candidate-03 may reuse candidate-02 binaries
only after verifying all producing source/build inputs are identical; the
changed runner and guest images must be freshly checked/prepared and executed.

## 1. Behavior contract

An interface-based network does not inherently require vmnet: TAP/bridge
backends are another design. Vmnet remains the planned native macOS route;
this slice adds no host interface, helper privilege or host route.

| Interface | Exact behavior |
| --- | --- |
| `--net` | Existing IPv4 NAT/DHCP/DNS; no forwarding and host-loopback access off by default. |
| `--net-forward [tcp\|udp:]HOST_PORT:GUEST_PORT` | Repeatable. Omitted protocol means TCP. Bind only `127.0.0.1:HOST_PORT`, targeting only `10.0.2.15:GUEST_PORT`. Both ports are decimal 1–65535; UDP host port 53 is unsupported. UDP requires explicit `--net-host-loopback`. |
| `--net-host-loopback` | Explicit per-launch opt-in: guest traffic to `10.0.2.2:PORT` can reach the Mac's `127.0.0.1:PORT`. It does not merge the two loopback interfaces. |
| Startup summary | With `--net`, print expected DHCP address, gateway/DNS, loopback policy and each successfully configured mapping to stderr once, after preparation succeeds and before guest execution. |

Both new options require `--net`; reject them without it. No new environment
variables. Reject unknown protocols, missing/extra fields, zero/out-of-range
ports and duplicate `(protocol, host_port)` listeners before binding any rule.
TCP and UDP may use the same host port. An OS bind failure refuses the launch
and names the offending mapping; never silently skip it or choose another port.
Reject UDP rules without the loopback flag before VM creation or binding. Reject
UDP host port 53: libslirp 4.9.7's gateway-DNS translation corrupts that forward.
UDP clients using source ports 53, 67 or 69 are unsupported because replies hit
libslirp's reserved DNS/BOOTP/TFTP handling; guest port 53 itself is allowed.
Reject duplicate UDP guest ports too: libslirp matches UDP replies by the guest
source endpoint, so two host listeners cannot reliably share one guest UDP port.
TCP guest-port reuse and TCP/UDP sharing a port remain allowed.

Example (new options proposed by this plan):

```sh
--net --net-forward 2222:22 --net-forward 8080:80

# UDP also permits guest access to host localhost services:
--net --net-host-loopback --net-forward udp:19000:9000
```

The Mac can then use `ssh -p 2222 user@127.0.0.1` or
`curl http://127.0.0.1:8080`. SSH/SFTP/rsync require the corresponding guest
software; guest listeners must bind the guest NIC address or all interfaces,
not only guest localhost. TCP forwarding works with host-loopback access off.
Different VMs need different host ports, even though each private guest can
keep `10.0.2.15`.

The opt-in loopback flag permits access to host localhost services broadly,
including services relying on a local connection instead of authentication.
It is not a per-port permission or a host-isolation guarantee. Default-off
does not block existing guest access to the Mac's LAN address.

Out of scope: configurable bind/guest addresses or subnets, automatic port
allocation, live add/remove, a monitor/API, DNS aliases, PCAP, IPv6, guestfwd,
file-sharing services, guest provisioning and vmnet implementation. No virtio,
resident, ABI, offload, packet scheduling or performance changes.

## 2. Source basis and implementation seams

These are source-backed integration premises, not new measured behavior:

- `crates/host/src/virtio/slirp.rs`: `Slirp::new` creates the stack;
  `disable_host_loopback` is currently hardcoded true. `Drop` already invokes
  `slirp_cleanup`, which owns socket/timer cleanup. The creator configures the
  stack before `Slirp::run` takes sole ownership on its existing thread.
- `crates/host/src/linux.rs`: `Boot::net` enables the device; `Boot::prepare`
  calls `Slirp::new` before guest execution. Keep `Boot::net: bool` and its
  default so existing callers remain unchanged.
- Libslirp's installed 4.9.7 `slirp/libslirp.h` declares
  `slirp_add_hostfwd(Slirp *, int is_udp, struct in_addr host_addr,
  int host_port, struct in_addr guest_addr, int guest_port)`.
  Addresses are network order; ports are host-order integers. Confirm the
  installed header when implementing; reuse the existing `In4`/`ipv4` helper.
  [Upstream header](https://qemu.googlesource.com/libslirp/+/refs/heads/master/src/libslirp.h),
  [forwarding implementation](https://qemu.googlesource.com/libslirp/+/dfe1229fc8f707f76b3f4d09078ab5e9b5817469/src/slirp.c)
  (host-order port conversion and negative failure return),
  [QEMU forwarding documentation](https://www.qemu.org/docs/master/system/devices/net.html#using-the-user-mode-network-stack).
- Upstream `sotranslate_out4` maps the virtual host address to host loopback
  when allowed. The linked revision is older than the installed library;
  the installed header confirms the switch and the paired runtime checks below
  must qualify this behavior in our build.
  [Address translation source](https://qemu.googlesource.com/libslirp/+/dfe1229fc8f707f76b3f4d09078ab5e9b5817469/src/socket.c).
- `tools/run_net_linux.py` already uses the production CLI, curl, iperf3,
  a known HTTP blob, serial phase markers, host-port locking and bounded VMs.
  Its stop/error oracles recognize `natemu: ...`; informational output must
  use the separate **`natemu-net:`** prefix so it cannot masquerade as a stop
  or libslirp guest error. Preserve those existing error checks.

## 3. Implementation tasks

### Task 1 — Typed configuration and command line

**Files:** `crates/host/src/virtio/slirp.rs`, `virtio/mod.rs`,
`crates/host/src/linux.rs`, `crates/cli/src/main.rs`.

- [x] Add the small public host-only types below beside `Slirp`, re-exported
  from `virtio`; the private FFI `Config` remains unchanged.

```rust
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ForwardProtocol { Tcp, Udp }
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PortForward {
    pub protocol: ForwardProtocol,
    pub host_port: std::num::NonZeroU16,
    pub guest_port: std::num::NonZeroU16,
}
#[derive(Clone, Debug, Default)]
pub struct SlirpOptions {
    pub forwards: Vec<PortForward>,
    pub host_loopback: bool,
}
```

- [x] Add `Boot::net_options: SlirpOptions`, initialized with `Default`.
  Reject nondefault options with `net == false` at the start of `prepare`,
  before VM/RAM creation. Validate duplicate listeners for library callers too.
- [x] Add repeatable `--net-forward` parsed by a small CLI function into
  `PortForward`, plus boolean `--net-host-loopback`; both use clap's
  `requires = "net"`. Pass the typed options into `Boot`.
- [x] Add focused tests: implicit/explicit TCP, UDP, repeated rules, valid
  boundary ports; reject malformed fields, signs/whitespace, zero/overflow,
  unknown protocol, duplicate listeners and options without `--net`.
  Reject UDP without loopback opt-in, UDP host port 53 and duplicate UDP guest
  ports; allow TCP guest-port reuse and cross-protocol sharing. Assert default
  options leave loopback disabled. No new parser crate.

### Task 2 — Configure the existing stack once

**Files:** `crates/host/src/virtio/slirp.rs`, `crates/host/src/linux.rs`.

- [x] Change construction to `Slirp::new(port, &self.net_options)` and add
  the exact C declaration for `slirp_add_hostfwd` above. Validate the complete
  list first; set FFI `disable_host_loopback = !options.host_loopback`.
- [x] Construct the owning `Slirp` value immediately after `slirp_new`, as
  today. Once non-null, install rules in order with `is_udp` 0/1,
  `ipv4(127, 0, 0, 1)`, `ipv4(10, 0, 2, 15)` and host-order integer ports.
  A negative return is an error naming protocol and both endpoints. Do not
  promise an OS error string unless the API preserves a meaningful errno.
- [x] On any rule failure, return through the existing `Drop`; previously
  installed listeners and the stack must all be released. Keep rules through
  virtio driver resets, since the stack already survives them. Existing stop,
  join and cleanup remain responsible for terminal teardown; no remove API,
  separate socket ownership or cross-thread calls are needed.
- [x] Add no-VM socket tests using the existing `virtio::tests::device()`
  IRQ fixture and `Virtio::network`, as sibling network tests do. Occupy a real
  TCP/UDP port B, install free rule A then B, and verify failure names B and A
  can immediately be bound again. Cover both protocols, TCP/UDP sharing a
  numerical port, and successful construction/drop/rebind without traffic.
  Bind only loopback; hold collision sockets throughout the check.

### Task 3 — Startup summary and usage

**Files:** `crates/cli/src/main.rs`, `crates/host/src/virtio/README.md`,
`tools/play/README.md`.

- [x] Prepare the summary from the immutable options before `Boot` is consumed,
  then emit it only after `boot.prepare()` succeeds. Use `natemu-net:` on
  stderr; emit nothing when networking is off or preparation fails. Example:

```text
natemu-net: libslirp IPv4; DHCP expected 10.0.2.15; gateway 10.0.2.2; DNS 10.0.2.3
natemu-net: host loopback disabled
natemu-net: tcp 127.0.0.1:2222 -> 10.0.2.15:22
```

- [x] Enabled wording states `10.0.2.2:PORT -> host 127.0.0.1:PORT`.
  A successful listener is not proof that DHCP or a guest service is ready.
  Keep SSH/curl/file-copy recipes in help/docs rather than inferring services
  from arbitrary port numbers. State the fixed guest target and localhost bind.
- [x] Check CLI defaults, dependency errors, successful mapping output,
  enabled/disabled wording and absence of success output on bind failure.
  Extend runner self-checks with the new informational lines while retaining
  rejection of genuine errors and unexpected stop records.

### Task 4 — Bounded integration, review and handoff

**Files:** `tools/run_net_linux.py`, `tools/run_suite.py` only if needed to
include the cases (prefer extending the runner's default case list), and
the current investigation/findings thread.

- [x] Extend the existing runner with the three cases below. Reuse its staged
  curl/iperf3 and Python host server; no new guest packages or test service.
  Serialize fixed fixture ports with its existing `.net-ports.lock`, detect
  occupied ports as fixture failures, bound client/readiness waits, and reap
  every child on timeout/failure. New loopback-only cases must not require a
  host LAN address or Internet access.

| Case | Required oracle |
| --- | --- |
| `forward` | Host-loopback flag absent. Forward TCP on host port 15201 to guest 5201. Run a one-shot guest iperf3 server and bounded host client; require a successful session and positive receiver bytes. Reset/rebind virtio-net, renew DHCP, and repeat without recreating the stack. |
| `loopback-off` | Keep a known host HTTP server bound specifically to `127.0.0.1:8000`; host fetches verify it is alive before/after. Guest curl to `10.0.2.2:8000` must fail within its deadline. Require DHCP and a successful forwarded TCP session in this same VM so missing network progress cannot count as denial. |
| `loopback-on` | Same HTTP server plus the opt-in flag and TCP/UDP forwards on host 15201 to guest 5201. Guest HTTP fetch through `10.0.2.2` must match the retained blob hash. Run bounded TCP then UDP iperf3 sessions; require positive receiver bytes, and received datagrams for UDP. Reset/rebind virtio-net, renew DHCP, and repeat both sessions. UDP iperf3 needs both mappings. |

- [x] Guest phase markers and server readiness drive host iperf3 clients from
  the existing launch polling loop. Retain host client output/exit status as
  well as serial/stderr, and require the expected phases exactly once. A
  timeout, absent result or missing UDP receive is failure. Add judge
  self-check mutations removing each required observation, and positive and
  negative startup-summary cases.
- [x] Exercise the existing `cancel` case with a forward installed and
  strengthen its traffic oracle: record the RX counter before starting the
  background iperf3 stream, then require at least a 1 MiB increase and a live
  iperf3 child (`kill -0` on its PID) before emitting a progress/readiness
  record. Only then may the host send SIGINT; replace the fixed two-second
  sleep. Retain the observed delta/live-child record and require it in the
  judge, with mutations for missing/zero progress. Preserve the existing
  10-second exit limit. The current readiness-before-iperf marker alone
  does not establish traffic under cancellation.
- [x] After normal shutdown and cancel, check listener release without another
  VM: TCP bind **and listen** on the same address/port with `SO_REUSEADDR`
  (TIME_WAIT must not produce a false leak); UDP plain bind with no address or
  port reuse (permissive reuse must not hide a stale listener). The no-traffic
  construction/drop tests in Task 2 continue to use ordinary binds.
- [x] Run the commands below from a retained source/build snapshot for accepted
  evidence. Use the existing raw-root/build retention process, not a new runner
  framework. `FEXC` must name the compiler built from that snapshot, `NET_OUT`
  an unused path below an external `NATEMU_RAW_ROOT`; never overwrite a run.

```sh
./tools/with-env cargo test --offline --locked -p natemu -p natemu-cli
./tools/with-env cargo build --offline --locked --release -p natemu-cli
./tools/with-env cargo build --offline --locked --release -p natemu-runtime --target aarch64-unknown-none
codesign --force --sign - --entitlements crates/host/entitlements.plist target/release/natemu
./tools/with-env python3 tools/run_net_linux.py self-check
./tools/with-env python3 tools/run_net_linux.py run --offline --fexc "$FEXC" --out "$NET_OUT/mmio" --vcpus 2 --cases forward loopback-off loopback-on basic rebind cancel
./tools/with-env python3 tools/run_net_linux.py run --offline --transport pci --fexc "$FEXC" --out "$NET_OUT/pci" --vcpus 2 --cases forward loopback-off loopback-on basic rebind cancel
```

- [x] Obtain independent code/oracle review before acceptance runs and an
  independent fresh-build replay of the three new cases on both transports,
  plus the no-VM listener/default-policy tests. Record evidence validity and
  decision relevance separately in the existing findings thread. Update the
  network README and investigation with passed, failed and untested behavior.
  Full suite remains required before push/merge under `docs/implementation.md`;
  this targeted plan does not waive that rule or initiate a push/merge.

## 4. Bounds and stopping condition

One small implementation slice; no performance campaign. Proposed execution
budget: up to four engineering hours including preparation, review and
corrections; 14 primary VMs (the two preserved failed cases plus the 12 revised
cases) and six independent-replay VMs, each at two vCPUs,
1 GiB RAM and the existing 180-second deadline. Client/readiness waits must
fit that deadline. The existing 10-second cancellation limit remains. Retain
sources, build inputs, commands, tool/library versions and successful/failed
outputs outside Git under the workflow; report snapshot/run storage use.

Stop when the table and lifecycle checks pass and independent review/replay
accept the scoped behavior. If the budget or a substantive failure prevents
that, report the exact incomplete case and smallest next fix; do not expand
into another backend, guest provisioning or a general networking API. This
planning task ended at the reviewed plan; the owner's subsequent November 15
instruction authorizes implementation on the new branch. Record actual results
and review/replay status before marking tasks complete.

## 5. Final result — November 15, 2026

Implemented on `codex/slirp-usability`; source/spec, code/method and final
findings reviews accept the revised contract with no substantive blockers.
The default remains host-loopback off; UDP never enables it implicitly.

- Both primary and independently built host/CLI suites pass **389 tests**
  (382 host, seven CLI; four existing host tests ignored). Release host/runtime
  builds, signing, pinned compiler checks and runner self-checks pass.
- **12 final primary VMs pass:** forward, loopback-off, loopback-on, basic,
  rebind and cancel on each of MMIO and PCI. **Six independent replay VMs
  pass:** the three new cases on both transports, with separately built
  production binaries and freshly prepared guest images.
- Scope: M2 Max, macOS 26.6.2, libslirp 4.9.7, stock Linux, two vCPUs and
  1 GiB RAM. TCP/UDP receive data, reset/DHCP ordering, loopback deny/allow
  controls, startup summaries and listener release were independently checked.
  Cancellation with a TCP forward passed the live-traffic and ten-second bound.
- Retained root: `natemu-raw/slirp-usability-20261115-01`, about **3.1 GiB**.
  Candidate-03 and replica-03 contain final sources/run artifacts and explicit
  provenance to unchanged producing builds. Candidate-01's UDP-policy failure
  and candidate-02's interrupted-JSON failure remain failed and preserved.
  Exact libslirp release source/provenance is in `udp-policy-source`.
- Actual bound: **14 primary VMs including two failures, six replay VMs**;
  about one hour of the four-hour allowance. No new backend or performance
  claim. IPv6, broader VM-size/host coverage, full-suite push/merge verification
  and the rest of the network contract are outside this accepted slice.
