These are finite 64-bit-decoded, 32-bit-data ordering/atomic participants. RBX,
RDX and RSI contain full-width native-mapped guest addresses; memory operands do
not truncate those addresses. Runtime setup and repetition remain outside the
participant instruction sequence.

`cases.json` distinguishes newly authored litmus blocks from the reduced FEX
fence/XCHG cases and retains upstream paths, hashes, revision and license. The
upstream fence tests only check execution. Their original tests are not ordering
oracles. The XCHG reduction drops the original unaligned offsets and does not
inherit their expected register values. Independent architectural expectations
belong to the ordering checker, not the FEX producer.

Actual checked emission and independent LLVM disassembly are retained in
`i4v-ordering-producer-20261026-01`; finite checked exports and relevant emitter
source copies are in `i4v-ordering-export-20261026-01`. Both are compile-only
artifacts under the durable raw root. The exporter admits exact bodies and keeps
the producer's sparse PC map separate from inspected access/commit metadata.

Observed SWPAL is an LSE acquire/release DWORD exchange, not evidence by itself
for the complete x86 locked-operation ordering contract. Observed LFENCE,
MFENCE and SFENCE bodies are DMB LD, DMB SY and DMB ST respectively. In particular,
DMB LD alone does not implement the selected resident LFENCE live-result sink
and completion boundary. Runtime adaptation and independent native validation
remain separate obligations.
