# Pinned I3 allocated recovery-map producer

This adapter exports real allocated roots from FEX commit
`48d71752e2a363d5535181f803b0b0e9666c1825`. Its explicit recovery copies force
allocator pressure and initialized spills. It is a conservative test profile,
not a measurement of natural/minimal recovery overhead.

`producer.json` binds the final binary, immutable Linux ARM64 Docker image,
source archive, dependency overlay, build inputs, and native entry bindings.
`build.sh` applies the retained patches/adapters to a fresh populated source
tree and builds only `CodeSizeValidation` with two build jobs. The binary stays
outside the repository. Compilation does not execute the guest fixture.

`parse(log, raw, object_index, source)` reads allocation/emission diagnostics and
checks them against actual native instructions. A fresh request uses object 1;
object 0 is the exporter's calibration. The returned `body` is bytes; its other
fields are JSON compatible. Locations, spill initialization, access sites,
frame allocation, and extraction boundaries come from the producing trace and
native bytes. The two admitted source shapes and SRA entry bindings are explicit
profile contracts. The parser rejects branches, helpers, canonical-bank accesses,
unsupported native operations, missing roots, and unwitnessed spills/fills.

The 35-root order is the exported `ROOTS` catalog: sixteen GPRs in
RAX/RBX/RCX/RDX/RSI/RDI/RBP/RSP/R8–R15 order, sixteen XMM low-128 values,
raw PF, raw AF, and native NZCV. At both admitted fault sites, native carry is
direct, PF is even parity of the raw PF low byte, and AF is
`((pf_raw ^ af_raw) >> 4) & 1`. Native FPSR is authoritative for the supported
SSE status path. Unchanged guest banks and their lifetime are runtime obligations.

The load map preserves pre-load RAX even though the load overwrites its native
address register. The later store has a fresh map with the loaded RAX and current
SETZ result. Their spill offsets refer to the active native SP after the emitted
stack subtraction. Native guest RSP is a separate root. The emitted body restores
native SP. The runtime uses a direct branch to its capture gate because x30
contains guest RDI in the store map; loading a return address into x30 would
corrupt that live root. Capture uses a separate stack to preserve the spills.
The vector store profile requires an aligned, same-page 16-byte normal-RAM access.

`../i3_worker.py` accepts a fresh directory containing exactly the host's original
128-byte `request.bin`. It validates the final producer hash, creates one isolated
container, compiles one supplied source, reparses the resulting object/maps, and
retains every input/output and lifecycle command. Run it with `--request-dir DIR
--producer PATH`. The request format is sixteen little-endian u64 words:

```
MISS=11, ABI=3, request_id, source_generation, context_generation, VM_cookie,
source_length, source_words[3], profile=2, vCPU=0, RIP=0x102000,
requests, executions, serial
```

Only ALIAS (17 bytes) and FP_ALIAS (16 bytes) are admitted. Source-word padding
must be zero. `reply-request.bin` echoes all 128 original bytes. `export.bin` is
1168 bytes, all integers little endian:

```
u64 body_length, source_length, spill_bytes
u8 source[24]       // zero padded
u8 body[512]        // zero padded
repeat twice:      // load8, then store16
    u64 native_offset, guest_offset, write
    u64 roots[35]   // low byte: 1=GPR, 2=Q, 3=SP spill; remaining bits=index/offset
```

The finite wire profile requires precommit maps, direct carry, captured native
FPSR, and continuation at native offset +4. `maps.json` retains the richer checked
metadata and `body_hex`. `verify_compilations(root, requests)` independently
reparses the retained actual raw/log, checks the original runtime requests,
reconstructs the export, and verifies input/output hashes plus the exact bounded
Docker lifecycle. Compiler failure or cleanup failure prevents successful return.

Run the parser and worker controls without Docker or guest execution:

```
python3 -B -m unittest discover -s tools/i3_producer -p 'test_*.py' -v
```

The parser testdata is actual retained output, with hashes/provenance alongside.
Worker transport tests explicitly use synthetic opaque calibration/relocation
bytes; those test doubles are not FEX evidence. The fresh full-worker compile-only
validation is retained at
`/Users/liaozping/orca/natemu-raw/i3-worker-probe-20261025-01`, with two real requests
and successful verification. The runtime consumes the delivered maps and checks
fault/completion state with an independently authored instruction model. Its exact
finite admission artifacts are `crates/abi/src/pressure_body.arm64` (the generated
308-byte body) and `pressure_sites.bin` (the608-byte two-site section from that
run's `request-000/export.bin[560:1168]`). The FP profile has the same allocations
and adjusted source offsets; changing the producer profile requires rebuilding
these artifacts and checking its consumers together.
