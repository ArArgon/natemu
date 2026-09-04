# BP-01-10-start — toolchain breadth pass

## Metadata

- Breadth-pass ID: `BP-01-10-start`
- Question: `Q-01-10`
- Date: 2026-09-03
- Executor/session: Claude Code authoring subagent `t01_platform_author`

This pass organizes questions and establishes no evidence or conclusion.

## Coverage

- **Product assumptions:** "the required toolchain" means whatever Track 01's
  own experiment-code rules demand — build (C via Apple clang, or Python 3
  standard library; a `Makefile`/`build.sh` and `run.sh`), debug (`lldb`),
  disassembly/inspection (`llvm-objdump`), version control (`git`), and
  code-signing (`codesign`, for the Hypervisor.framework entitlement) — plus
  whatever profiling later tracks need, from public sources, sustainably across
  the two required macOS releases. It assumes Command Line Tools (CLT) alone
  may or may not suffice; that is this question's job to determine, not an
  assumption this pass makes.
- **Adjacent systems:** Homebrew is the candidate package source for tools CLT
  does not ship — `cmake`, `ninja`, and QEMU itself as the Track 05 baseline
  (subject to `Q-05-05`'s version-freeze policy, not this question). Apple
  clang (`clang-2100.0.123.102`, reporting version `21.0.0`) is a downstream
  fork of upstream LLVM/clang, not an identical build; where a claim needs
  upstream LLVM behavior specifically, Apple clang's documented divergence (if
  any) is a distinct sub-question from "does a compiler exist." Xcode.app
  (not installed on this host) is the only documented source for Instruments
  and `xctrace`; CLT and Xcode.app are separate Apple distributions with
  different install/update mechanisms and different license terms.
- **Alternatives:** installing Xcode.app trades a larger footprint and an
  App-Store/developer-portal download for GUI/timeline profiling; staying
  CLT-only keeps the experiment-code rule's "no new dependencies" posture but
  may leave a documented capability gap (profiling) that a CLI-only
  alternative (e.g., `sample`(1), `dtrace`-based tooling, or `os_signpost`
  consumers) would need to cover instead — this pass records the fork, it does
  not choose a side. Homebrew LLVM/clang is a possible alternative to Apple
  clang for building non-Apple-specific code, but experiment code under this
  track's rules must use Apple clang or Python 3 standard library only, so
  that alternative is out of scope for `experiments/` even if useful for a
  Track 05 QEMU build.
- **Negative and boundary cases:** this host has no `cmake`, no `ninja`, no
  `xctrace`/Instruments, and no QEMU (per the brief's host-facts table); a
  dependency that only builds via CMake (some decoder or emulation libraries
  do) cannot be built without Homebrew's `cmake`, which itself is a toolchain
  admissibility fact, not an experiment-code dependency, since experiment code
  itself must stay dependency-free. `llvm-objcopy` is also absent; any future
  method that assumed it needs a documented substitute or a Homebrew LLVM
  install.
- **Host and guest matrix:** toolchain composition varies by SDK — this host's
  CLT carries `MacOSX26.4.sdk` (default), `MacOSX26.sdk`, `MacOSX15.4.sdk`, and
  `MacOSX15.sdk` side by side under
  `/Library/Developer/CommandLineTools/SDKs`, which is itself evidence of
  same-CLT-install SDK coexistence across the two required macOS release
  lines, but is only Observed on `P2`'s generation/release line
  (`Mac16,10`/macOS 26.6.2/`25G83`); `P1` (M1 Pro/Sequoia 15) toolchain
  replication is owed before any platform-wide sustainability claim, per the
  executor guide's incremental-replication rule.
- **Multicore interactions:** the toolchain itself carries no vCPU-topology
  dependency, but downstream resource-guard instruments (`Q-05-08`) and
  per-core placement measurement (owned by `Q-05-06`) depend on profiling tools
  this question inventories; if `xctrace`/Instruments turns out to be the only
  documented per-core attribution path and Xcode.app is absent, that is a
  toolchain-sustainability fact this question must record, not silently defer.
- **Falsifiers and stop paths:** this question tests `KA-09` ("a build/test/
  debug/profiling toolchain exists from public sources on the supported hosts
  and is sustainable across the two macOS releases"); if false, `INV-00-06`'s
  family-kill item 1 treats an unsupported toolchain as a family-level kill
  condition, and — per that item's own text — could also be an input to a
  project-level stop if it holds for every mandatory host/OS cell. A
  falsifier here is: a tool this track's experiment rules require is available
  only from a private/internal Apple channel, or only via a paid/Apple-ID-gated
  Xcode.app download whose long-term availability across both required macOS
  releases is not documented.
- **Measurement failure modes:** an observed tool inventory on this one host
  and this one CLT/SDK snapshot cannot be generalized to the full host/OS
  matrix without `P1` replication; Homebrew formula versions drift between the
  capture date and any later reproduction, so an inventory claim needs a
  pinned formula/version, not just "Homebrew has it"; and CLT can silently
  auto-update its default SDK between sessions, so a claim that cites
  `MacOSX26.4.sdk` as default must record the CLT build/version at capture
  time, not merely assume it is stable.

## Backlog updates

Surfaced concerns map to existing questions: Homebrew-sourced QEMU version
pinning to `Q-05-05`; per-core profiling instrument validity to `Q-05-06` and
`Q-05-08`; and Xcode.app-versus-CLT distribution/entitlement questions to
`Q-01-01`. No new question is required.

`no new issues found`
