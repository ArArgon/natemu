# I4-V reused x86 cases

`cases.json` pins four adapted FEX cases to revision
`48d71752e2a363d5535181f803b0b0e9666c1825`. Exact upstream files and the MIT
license are retained under `upstream/FEX/`. Each case records the original
expectation, adaptation rationale, source bytes/hash, instruction boundaries,
and memory effects. The `.s` files assemble with Clang's integrated assembler;
the explicit 32-bit moffs bytes preserve the upstream encodings.

The 64-bit cases accept data/stack addresses through initial registers, allowing
the same instructions to execute with low or upper-canonical addresses and a
separately selected guest RIP. The 32-bit cases use owned RAM at `0x100000`, flat
segments and A20 enabled. They test partial loads/stores with real memory access;
they do not establish segmentation, A20 switching or mode-transition coverage.

`expectations.py` computes architectural state after a successful instruction
prefix without importing the emulator or producer. A recovery check supplies
the number of instructions committed before the faulting instruction. It does
not decide which address should fault or which concurrent outcomes are legal.
Those checks belong to the I4-V runner. Flags and untouched registers/memory
must remain unchanged; compare only the low 32 GPR bits for legacy-mode cases.

Run the fixture checks with:

```sh
python3 -m unittest discover -s fixtures/i4v -p 'test_*.py'
```

Boundary addresses in these host-side checks exercise the expectation helpers,
not the runtime's MMU. A fixture definition or passing expectation self-check is
not a translated-execution result. FEX output is the system under test; FEX
execution is not an independent oracle for these cases.
