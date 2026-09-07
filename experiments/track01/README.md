# Track 01 reproduction snapshot

Frozen source for reproducing the five accepted Track 01 mechanism checks.
The final program exercises paging, precise faults, interrupts, host-produced
code and secondary-CPU startup together at two or four vCPUs.

Retained in the repository at the owner's request after track closure. This
preservation changes no accepted result or production-architecture decision.
Raw results, RAM images and compiled binaries remain outside Git.

## Contents and provenance

- `source/`: all 27 files from retained `q34-startup-build-05/source`, unchanged,
  including six audited Linux source excerpts, the host/guest program, build
  runner and signing entitlements. 398,452 bytes; 8,867 text lines.
- `oracle/`: the seven unchanged runtime checker modules from
  `q34-startup-oracle-01`.
- `run_check.py`: the resource supervisor from `q34-startup-supervisor-01`,
  adapted to read the Git-versioned oracle without a separate source checksum
  file. Required modules are copied into each run; symlink rejection, copied-source
  mutation detection and raw-result integrity checks remain.

Git records source versions. No source checksum files are stored in this
snapshot. Generated run artifacts retain their own inventories for evidence
integrity and producing-source association.

The full original bundles, synthetic preparation controls, producing snapshots,
failed attempts and accepted results remain under the recorded external raw
root. They are not needed to build a fresh run and check its output using this
snapshot. Historical manifests retain their actual original paths and identities.
See the [investigation](../../docs/research/investigations/INV-01-34.md) and
[accepted findings](../../docs/research/reviews/INV-01-34-findings.md) for evidence
and limits; a new run does not retroactively reproduce a historical run identity.

## Requirements

Run from the repository root on an Apple Silicon Mac with macOS 26, Python 3.9+
and Apple's command-line build/signing tools. Select **SDK 26.4**: the unchanged
checker explicitly requires that version. The accepted host had 16 GiB RAM.
Allow 28 GiB free storage before a two/four-vCPU campaign and at least 14 GiB
before each VM. Run VM/check pairs serially, with no other full-memory checker.
The host process requires an unlimited inherited CPU-time limit (`ulimit -t`).

The current native watchdog is 20 seconds, its outer host-child limit is
30 seconds, and the full checker has a 1,800-second wall limit and 12 GiB
sampled process-group RSS ceiling. Native files are limited to 2 GiB each;
checker files to 256 MiB. A slower host or changed SDK may need separately
reviewed preparation changes. Preserve failed attempts and stop on failure.

[Research workflow](../../docs/research/README.md) applies to new findings:
record the new host, review its preparation and bounded campaign, then perform
independent reproduction/findings review. Old execution budgets are closed.
The commands below are individual build/run/check operations; they do not
supply a campaign-wide deadline or automatically enforce the disk admission
floors above.

## Verify and prepare without launching a VM

From the repository root:

```bash
(
  set -e
  python3 -B experiments/track01/source/run.py --self-check
  python3 -B experiments/track01/run_check.py --self-check
  for q in 30 31 32 33 34; do
    python3 -B "experiments/track01/oracle/check_q${q}.py" --self-check
  done
)
```

Use `-B` to keep generated Python bytecode out of the source directories.

A build-only check uses a fresh output name and retains the build outside Git:

```bash
export NATEMU_RAW_ROOT="$PWD/../natemu-raw"
track01_build="track01-build-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
python3 -B experiments/track01/source/run.py \
  --snapshot "$PWD/experiments/track01/source" \
  --build-only --name "$track01_build"
```

## Run and check one reviewed pair

After host/campaign preparation, use a fresh name for each attempt. This example
runs two vCPUs; repeat with `--vcpus 4` and a new name for the four-vCPU pair.
The final snapshot includes all five mechanisms in each run.

```bash
export NATEMU_RAW_ROOT="$PWD/../natemu-raw"
track01_run="track01-run-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
python3 -B experiments/track01/source/run.py \
  --snapshot "$PWD/experiments/track01/source" \
  --vcpus 2 --name "$track01_run" &&
python3 -B experiments/track01/run_check.py --q34 \
  --run "$NATEMU_RAW_ROOT/$track01_run" \
  --oracle "$PWD/experiments/track01/oracle" \
  --name "$track01_run-check"
```

A producer success is preliminary. The checker output directory must contain
`supervision.json` with `status: passed` and a JSON `stdout` with all five nested
mechanism results `PASS` and no unsupported replay. Interpret that result only
within the declared finite fixture and new host's reviewed applicability.
