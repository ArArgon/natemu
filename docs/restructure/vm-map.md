# VM code map by responsibility (restructure step 2)

Status: October 26, 2026; §7 implemented (V1b), §1–5 describe the code before it. Maps code to the
[restructure plan](../vm-restructure.md)'s responsibilities and proposes the migration
order. "Scenario-bound": code that knows a test program, expected result, case name
or fixed workload shape.

## 1. Responsibilities and where the code lives today

| Responsibility | crates/abi | crates/host | crates/runtime |
| --- | --- | --- | --- |
| VM lifecycle | — | `main.rs::Vm` (one vCPU, owns `Memory`), `concurrent.rs::Machine` (VM-only guard), `concurrent/containment.rs::destroy`; `hv_vm_create`+`hv_vm_map` copied in `concurrent.rs::run` and six `concurrent/*::run` | — |
| CPU state (entry/boot) | `NativeFrame`, `Shared` (`lib.rs`) | boot registers written inline in `main.rs::run`, `translation.rs::run`, `i4v.rs`, `i4v_off.rs`, `i4v_off_steps.rs`, `i4v_stack.rs`, `i4v_controls.rs` and `concurrent.rs::prepare_cpu`; `concurrent.rs::Cpu` duplicates `Vm`'s register accessors | `gates.S::_start`, `main.rs::runtime_main` (profile capture) |
| Guest/private memory | layout constants (`lib.rs`), `resident.rs`, `shadow.rs`; walkers `paging.rs`, `pae.rs`, `paging32.rs`, `modes.rs` | `main.rs::Memory`, `load_elf`, `read_input`, `page_tables`; `concurrent.rs::tables`; `i4v.rs::prepare_shadow`; `recovery.rs::prepare` (guest tables) | `main.rs::{copy_record,read_record,write_record}`; per-family `Tables` adapters (see §3) |
| Execution | `Object::validate`, `translation.rs` wire packets | `Vm::run_any`, `concurrent.rs::Cpu::run` (HVC loop + compiler service), `translation.rs::Session` | `gates.S` enter/capture/resume, `translation.rs`, `helper.rs`, `pressure.rs`, `recovery.rs` |
| Coordination | `concurrency.rs` (`Coordinator`, `Round`), `schedule.rs` | `concurrent.rs::run_owners` (owner threads, start barrier, supervision, cancellation) | `coordinator.rs` (lock, FIFO grants, trace), `schedule.rs`, `concurrent.rs` |
| Diagnostics | — | per-family JSON writers (`main.rs::frame_json`, `concurrent/*::owner_json`, `*::report`) | trace in `coordinator.rs` |
| Producer transport | `translation.rs::Lifecycle` packet | `translation.rs::{Producer,start_worker}`, `translation/process_owner.rs`, `concurrent/*producer.rs`, `i4v_controls/producer.rs` | service gates `hvc #0x4a` |

## 2. Duplicated setup and low-level operations (host)

- **VM create/map/destroy:** open-coded in `concurrent.rs::run` and
  `concurrent/{fence,coherence,ordering,address,controls,partial_progress}::run`;
  `main.rs::Vm::new` repeats it for one vCPU. `hv_vm_get_max_vcpu_count` limit
  checks are copied 7 times.
- **vCPU operations:** `main.rs::Vm::{reg,sys,read_reg,read_sys,run_any}` and
  `concurrent.rs::Cpu::{new,reg,sys,destroy,run}` duplicate each other, including
  HVC exit decoding.
- **Resident boot registers:** the same nine writes appear eight times. Real
  differences: owner window (`id * RAM_SIZE`) and TCR (single-root for I1–I4,
  `shadow::TCR_BOTH` for I4-V). Only three places check FlagM.
- **Resident image:** `load_elf(&memory, &read_input(path, 2 MiB))` 14 times;
  per-owner text copy (`resident::executable_offset` loop) 7 times; VM nonce
  (`getentropy` + nonzero) 12 times; hand-written `ptr::write(memory.0.add(..))`
  record transfer everywhere (`concurrent/*` have local `record()` helpers).
- **Private page tables:** `main.rs::page_tables`, `concurrent.rs::tables` and
  `i4v.rs::prepare_shadow` build the same resident address space with different
  span lists; one builder over an explicit span list should replace them (§6 step 3).

## 3. Duplicated low-level operations (runtime)

- `copy_frame` (`fence.rs`, `ordering.rs`, `coherence.rs`,
  `concurrent/stack_boundary.rs`), `pause`, `id/all/owner/common/config` accessors
  are repeated per family.
- Guest-table adapters: `recovery.rs::OwnedTables`, `ordering.rs::GuestTables`,
  `fence.rs::GuestTables`, `coherence.rs::Tables`, `partial_progress.rs::Tables`,
  `atomic_progress.rs::WalkTables`, `controls.rs::{PaeTables,Tables}`,
  `controls/long_mode.rs::Tables`, `concurrent/address_concurrent.rs::Tables`.
  Each maps a fixed guest-physical window (e.g. `0x1000..0x5000`) onto one fixed
  private table page and counts reads in its own record. These are the
  "fixed-page memory adapters": one guest-physical memory accessor over explicit
  RAM bounds should replace them.
- Guest memory read/write/read32/write32 helpers in `controls.rs`, `coherence.rs`,
  `ordering.rs`, `fence.rs`, `partial_progress.rs`, `concurrent/address_concurrent.rs`.

## 4. Scenario-bound pieces to move into test drivers

- Program bytes and expected-body matching: `main.rs::run` (`FEX_BODY`, `NOP`,
  `UDF`), `i4v.rs::BODY`, `concurrent.rs::run` (`FEX_BODY`, `stack::SOURCE`,
  `mixed32::SOURCE`, `wire::XOR`), `recovery.rs::prepare` source bytes.
- Expected values: `concurrent.rs::Cpu::run` checks each compiler request against a
  per-family `expected` packet chosen by `compiler.is_coherence()/is_fence()/…`;
  `main.rs::run` "resident modified seeded entry state"; the `Owner::expected`
  fields in abi `coherence/fence/ordering/address_concurrent/controls/mixed32/
  stack_concurrent`.
- Case dispatch by name: `main.rs::run` (argument count + family string),
  `recovery.rs::CASES`/`prepare(name)`, `i4v.rs` scenario names that set operands,
  `concurrent.rs::profiles`, mutations `"version"|"profile"|…` in `main.rs::run`.
- Scripted mutations/progress: reply-word flips (`i4v*.rs` `reply_mutation`),
  `containment.rs` failure injection (withhold ack, panic at source), runtime
  `*::mutate`, `coherence.rs::mutate`, `partial_progress.rs::try_mutate`.
- Fixed-count queues/shapes: `run_owners`' nine optional family configs and
  per-family `producer::Compiler::new_*`; `i4v_off_steps.rs` 9-step loop,
  `i4v_off.rs`/`i4v_stack.rs` 3-chunk loops; abi `maintenance::State::new(n)`,
  `schedule.rs` fixed pools; seeds `main.rs::seed(index)` (4 vectors).

## 5. Reusable mechanisms to keep (with limits)

- abi walkers `paging::walk`, `pae::walk`, `paging32::walk`, `modes.rs` resolver:
  generic over a `Tables` trait; limit: callers supply fixed-window adapters.
- Capture/enter gates (`gates.S`): full GPR/Q/NZCV/FPCR/FPSR/SP_EL0 save, DSB SY +
  ISB completion, ERET restore (contract 61). Keep unchanged until step 3.
- `coordinator.rs` lock with FIFO grants and trace; `concurrency.rs::Round`
  publication; `schedule.rs` permits. Limit: sized for N ≤ 4 owners.
- `run_owners` start barrier (no entry before all owners prepare), cancellation,
  join-before-read and vCPU-destroy-before-VM-destroy ordering (contracts 57–59).
  Limit: family configs are passed as nine optional tuples.
- `process_owner.rs` child ownership/reaping (contract 60, producer transport).
- `load_elf` resident-image admission and its unit tests.

## 6. Migration order

Ordered for the critical path to the first connected slice (one vCPU, 64-bit,
paging on), which needs guest memory and tables before multi-owner services.

Status (Oct 27): steps 1–4 done; step 5 host side (`host/src/drivers/`) and runtime/abi side (`families/` behind the `families` cargo feature; the default resident is the general path only, ~18.6 KB text) done; drivers dissolve as the general path covers them (`host/src/{vm,memory,owners}.rs`, abi
`guest_memory.rs`/`shadow.rs`, runtime `tables.rs`/`record.rs`).
Sections 1–5 describe the code before these steps.

1. **Host lifecycle, CPU state, memory.** See §7.
2. **Runtime guest memory.** One guest-physical accessor and one `Tables`
   implementation over explicit bounds; one `copy_frame`/record module; families
   keep only their semantics.
3. **Private address space.** One resident-table builder over an explicit span list
   (replaces `page_tables`, `tables`, `prepare_shadow`).
4. **Host owner execution.** A caller-supplied per-owner service replaces
   `run_owners`' optional family tuples; `expected` packet selection leaves `Cpu::run`.
5. **Test drivers.** Move program bytes, `seed`, expected packets and name dispatch
   into drivers; then recovery/native-register/dispatch with the plan's step 3.

## 7. Proposal for the first consolidation (host lifecycle, CPU state, memory)
Two new modules; behaviour, layouts and JSON output stay unchanged.

- `host/src/memory.rs` (from `main.rs`): `Memory(base, len)` with `new`, `with_size`,
  `copy`, `word`, `read`, bounds-checked `write_record<T>`/`read_record<T>`, `flush_instruction_cache()`,
  `replicate_resident_text(window)`; free functions `read_input`, `load_elf`,
  `load_resident(&Memory, path)`, `page_tables` (as-is until §6 step 3); ELF and
  mapping unit tests move with them.
- `host/src/vm.rs`: Hypervisor FFI, `hv()`, `HvExit`; `Machine::create(&Memory)`
  (flush I-cache, `hv_vm_create`, map whole backing RWX at `IPA`; destroy on drop;
  `destroy(self) -> status` for containment's explicit teardown record);
  `Vcpu` (`create` with the FlagM check, `id`, `set_reg`/`reg`, `set_sys`/`sys`,
  `run_to_hvc() -> immediate`, `destroy() -> status`, destroy-on-drop);
  `Vcpu::enter_resident(window, tcr)` writing the architectural boot state
  `_start` consumes (PC/CPSR, x0 = window root, x1 = TCR, x2 = MAIR, SCTLR,
  ACTLR TSO bit, CPACR, CNTV_CTL) with named register constants and
  `SINGLE_ROOT_TCR`; `require_vcpus(n)`, `exit_vcpus(&mut [u64])`,
  `new_vm_nonce()`; `Vm { cpu: Vcpu, _machine: Machine, memory: Memory }` (field
  order = teardown order) with `new`, `shared`, `run(expected)`.
- Separation: `enter_resident` is architectural state only. Seeding `Shared`,
  banks, data, `Coordinator` and family records stays in the callers as fixture
  initialisation (moved to drivers in §6 step 5); owner windows remain explicit
  `id * RAM_SIZE` arguments, not family identities.
- Callers that migrate: `main.rs::run` (I1), `translation.rs::run`, `i4v.rs`,
  `i4v_off.rs`, `i4v_off_steps.rs`, `i4v_stack.rs`, `i4v_controls.rs`;
  `concurrent.rs::{run, prepare_cpu, Cpu, Machine}` (`Cpu` keeps only its service
  loop over a `Vcpu`); `concurrent/{fence,coherence,ordering,address,controls,
  partial_progress}::run`; `concurrent/containment.rs::destroy`;
  `recovery.rs` readers; record stores use `write_record`. Removed: ~8 boot
  sequences (~80 lines), 7 VM create/map blocks (~70), 14 load calls, 7 text
  replication loops (~40), 12 nonce blocks (~40), 7 limit checks (~50), the second
  vCPU wrapper (~45).
- FlagM becomes an emulator requirement checked once in `Vcpu::create`
  (FEX output uses CFINV); previously only I1, I2/I3 and concurrent owners checked.
- Kept per caller: window and TCR choice; backing size per family.
- Risks: error texts change (no checker matches them; the checked `vm-destroy`
  JSON line, `check_i4v_containment.py:380`, stays).
- Verification: `cargo test --offline --locked` (ELF/mapping/FFI tests),
  release runtime build, `python3 -B -m unittest discover -s tools -p 'test_*.py'`,
  and focused native runs: `run_i1.py` control + live_load cells, I2 live,
  `run_i4v.py --focus` cells for address (high/cold), stack, paging-off, paging-off
  steps, controls, concurrent controls N2, coherence N2, ordering (xchg N2),
  fence N2, address-forms N2, partial-progress N2, I4 entry N4 and one containment
  cell with its fresh control (contracts 57–59 teardown ordering).
