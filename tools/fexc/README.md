# fexc: native macOS build of the FEXCore compiler subset

Status: **C1a done** (library + link smoke test). **C1b done** (`fexc` CLI and JSON
output, 5-input smoke check). **C1c done in effect**: the 53 pinned old-producer bodies with a recorded
bitness (then `tools/i4v_producer/export.py` CASES; since S3i `tools/fexc/producer_bodies.json`) are byte-identical to the native output, which
`test_fexc.py` asserts; the structural self-check is `test_fexc.py` too. **S1a done**
([slice 1 contract](../../docs/restructure/slice1-contract.md) §3): `natemu-fexc-v2` with access
sites, `spill_bytes`, `cf_inverted` per PC entry, and `--object` (binary record in
`crates/abi/src/code_object.rs`); `check_sites.py` checks the sites against the 64 reference
programs and the emitted code. **S1c done**: profile `slice1` (allowlist block cutting, single-gate exits, see
"Profile slice1"). **S1b1 done**: the precision patches under `--profile slice1` and `verify_precision.py` (see
"Precision (S1b1)"). **S1b2 done**: address guard, split-store check and canonical branch targets under `--profile slice1`
(patch 0007, see "Guards (S1b2)"); **E3**: the address guard removed (series13). **S1f done**: div/idiv admitted with #DE checks and the 128/64 helper call (patch 0008,
see "Division (S1f)").

## Build

```sh
git submodule update --init --depth 1 third_party/FEX      # build.sh also does this
tools/fexc/build.sh BUILD_DIR
```

`BUILD_DIR` must not exist. FEX comes from the submodule `third_party/FEX`, pinned to upstream commit
`48d71752e2a363d5535181f803b0b0e9666c1825` (`.gitmodules`: `shallow = true`). The script initialises the submodule and the
three FEX externals the build uses (`External/fmt`, `xxhash`, `unordered_dense`, each shallow at the revision FEX pins),
checks the pin, creates `BUILD_DIR/src` as a detached git worktree of the submodule at the pin, copies the three externals
into it (`git archive`), applies `patches/*.patch` with `git am`, configures `CMakeLists.txt` against that tree, builds with 4
jobs and runs `fexc_linktest --init` and `smoke.py`. `third_party/FEX` is never modified; `BUILD_DIR/src` is a git checkout
(series applied as thirty commits) and is never committed. Cold build about 30 s. Needs Homebrew `cmake`, `ninja`, Apple clang,
`git`; no Docker, no Linux VM. Disk: submodule checkout about 50 MB plus 10 MB git data, a build about 70 MB.

Current tree: `/Users/liaozping/orca/natemu-raw/fexc-native-20261026-01/series52-adox66` (November 4, owner decision of
November 3: ADX, CLFLUSHOPT/CLWB, XSAVEOPT/XSAVEC; boot-features plan, Built (ADX), Built (CLFLUSHOPT, CLWB), Built (XSAVEOPT,
XSAVEC)): patches 0001-0030 (0030: ADOX with a 66 prefix decoded as ADOX, 66 ignored, as on the reference AMD machine) and
`fexc.cpp` with the shim admitting ADCX/ADOX (66 or F3 0f 38 f6, ADOX also with 66; never with F2) and its cut reasons naming
XSAVEOPT, CLWB, CLFLUSHOPT and XSAVEC as the resident's; `obj/fexc` SHA-256
`bef4081c9444d030413448159e0c02d197d2bbf50c6f53b543a0f5479c7e9fe8`. Before it (intermediate builds of the same work):
`series51-xsave` (`9600ebd1...1892`, no patch 0030), `series50-adx` (`8a25ab6c...196e`), `series50-adx-r0` (refused ADOX by the
REP-prefix rule), and `/Users/liaozping/orca/natemu-raw/fexc-native-20261026-01/series49-sha` (November 3, owner decision: SHA
advertised; boot-features plan, Built (SHA)): series48-lean's patches and `fexc.cpp` with the shim admitting SHA1*/SHA256*
(NP 0f 38 c8..cd, NP 0f 3a cc) as aligned-m128 SSE forms instead of cutting them as #UD; `obj/fexc` SHA-256
`fe1631cbea73342f32a3a3689593c6bf5f9263577e0691e68f3b76d9208e1f61`. Before it, `/Users/liaozping/orca/natemu-raw/fexc-native-20261026-01/series48-lean` (November 3, perf plan "Measured
(chunk 2)": series47-seg's patches and shim with `fexc.cpp`'s server sending only what the host reads, below "Compile
server"; objects, extent and args_read byte-identical to series47-seg's; `obj/fexc` SHA-256
`8c8bd6594a5cbe557d8928c483f2f6c3887f9c68b8797d84e83e1912fda4f592`). Before it, series47-seg (November 3: series46-br66's patches
with the shim and `fexc.cpp` changed for the owner's segment-prefix decisions: CS/DS/ES/SS prefixes ignored, so a site's
class is its operand's default segment (`JoinSites`: SS for an RSP/RBP base without FS/GS; `NullSegment` gone); the last of
several FS/GS prefixes applies; an FS/GS prefix with no overridable memory operand ignored; every segment-prefix refusal of
the allowlist removed; boot-features plan, Built (segment prefixes, November 3); `obj/fexc` SHA-256
`c4de5da07fabda7595a27ce4833961e297f34e4753e80cce3454a32aeb69f91a`). Before it, series46-br66 (November 2: series45-r2fix plus
patch 0029 and the shim admitting 66 on near direct branches, ignored as on Intel (below, "The patch series"); boot-features
plan, Built (66 on near direct branches)). Before it, series45-r2fix (November 2, after the R2 review:
series44-r2 plus the shim admitting segment prefixes on near direct branches, ignored as processors do; boot-features plan,
Built (R2 review fixes), round 2). Before it, series44-r2 (November 2, R2: patches 0001-0028 and
the X1c shim, built by `tools/fexc/build.sh`; its `obj/fexc` is byte-identical to the hand-built series43-x1, so they are the
same compiler: [boot-handoff.md](../../docs/restructure/boot-handoff.md) §6). Between it and series36: series39-i5c (patch
0026), series40-i5c-add (0027), series42-i5c (0028), series43-x1 (the X1c shim changes); boot-features plan, Built (I5c),
Built (I9 addendum), Built (I5c review round), Built (X1b, X1c). Before them, series36 (October 30, I10 review: series34's patches,
patch 0025's FexcStringOp comment fixed, and shim changes: an undecoded #UD encoding is fetched whole first (`FexcUdLength`;
`partial_decode` when its bytes run past the input, `instruction_too_long` over 15 bytes), TPAUSE/UMONITOR/UMWAIT #UD (WAITPKG
not advertised). Boot-features plan, Built (I10) review round.) Before it, series34 (October 30, I10: series33 plus patch 0025
and shim changes. Patch 0025: under slice1 the one-element string pass uses ESI/EDI/ECX with 0x67 (read zero-extended, each
update written zero-extended, the count test `cbz w7`); LOOP/LOOPcc with 0x67 writes ECX zero-extended into RCX (FEX kept
RCX[63:32]); XLAT computes RBX + AL (EBX + AL truncated with 0x67) before the FS/GS base (FEX added the base first). Shim: the
0x67 refusal is gone (FEX's `LoadEffectiveAddress` truncates and zero-extends the 32-bit address before the segment base, RIP-
relative included; 4-byte moffs; JECXZ on ECX; direct branches ignore it); BT/BTS/BTR/BTC m, r with 0x67 stay refused; XLAT is
admitted; encodings the presented CPU raises #UD for (reserved or unadvertised: XOP and 8f /1../7, EVEX, RTM XBEGIN/XABORT and
group 11 /1../6, PadLock 0f a6/a7, 3DNow!, invalid-in-64-bit opcodes, group 4/5/8 reserved members, the reserved VEX L/W/vvvv/
operand-kind encodings and VEX forms FEX does not decode, the MMX/SSE shift groups' reserved encodings, F3 MOVBE, INCSSP) are
system cuts with `cut_ud` (the host answers `system::UD`, no decode), before any gating; 0f 1e /r (ENDBR, RDSSP, the CET hint
space) is a NOP. Boot-features plan, Built (I10).) Before it, series33 (October 30, P3: series32's patches with
`fexc.cpp` and shim changes, no patch change: the compile server runs requests in process and reports what each compile read,
see "Compile server" below; the host needs series33's reply format). Before it, series32 (October 30, I9: series31 plus patch 0024
and shim changes. Patch 0024 (all under slice1): every MMX instruction sets TOP = 0 and the abridged tag byte FFH at its end,
after its memory accesses (`FexcEndMMX`, called after each instruction's handler in Core.cpp), so a fault leaves both as they
were, and every one does (FEX did it once per block); the MMX registers join the per-instruction register-cache flush (none
cached across an instruction, which may be x87 code writing the same slot); PUNPCKLBW/WD/DQ mm, m32 load 4 bytes (FEX loaded 8);
PDEP and PEXT are straight-line code (Hacker's Delight's expand and compress, log2(width) rounds: 102 and 83 instructions at
64 bits; FEX's loops branched backwards); FFREEP ST(i) frees ST(i) and pops (FEX ran it as FINCSTP); and, for every form, before
each IR node and before the block's tail at least 64 KiB must remain in the temporary code buffer, else the compile restarts with
the doubled estimate (FEX's `NeedsLargerJITSpace`, raised before only by its guard-page signal handler, which fexc does not
have: a block of many long lowerings died with SIGBUS). The host translates a block over 32 KiB again with half its
instructions (I9 review). Shim: MOVBE m admitted (a register
operand or F3 is a system cut the host decodes as #UD); MMX (`Mmx()`: every MMX-register form FEX supports but MASKMOVQ, gated
by `--x87-gated` as `cut_gated: "mmx"`, the host's `MMX_GATED`: #UD under CR0.EM, else #NM); AES-NI and PCLMULQDQ, legacy
(16-byte aligned m128) and VEX.128; PDEP/PEXT; FFREEP; ADCX/ADOX, WRSS/WRUSS and SHA (not advertised) system cuts the host
decodes as #UD. Boot-features plan, Built (I9).) Previous: `series31` (October 30, I6b: series30 plus patch 0023
and shim changes. Patch 0023: the x87 transcendental, remainder, scale and extract forms (F2XM1 FYL2X FPTAN FPATAN FXTRACT
FPREM1 FPREM FYL2XP1 FSINCOS FSCALE FSIN FCOS) lower to one IR op `FexcX87Svc`, `movz w0, #<D9 second byte>; svc #5`, run whole
by the resident on the guest-state page's x87 state; VPCMPISTRX/VPCMPESTRX (PCMPxSTRx, legacy and VEX.128) call
`FexcStringSvc` (`v0`, `v1`, `x1` = RAX, `x2` = RDX, `movz w0, #control; svc #6`) instead of FEX's host helper. Shim: the
allowlist admits these forms (PCMPxSTRx memory operands with no alignment check); `FexcSvc` gains `FEXC_SVC_X87` = 5 and
`FEXC_SVC_STRING` = 6. Boot-features plan, Built (I6b).) Previous: `series30` (October 30, I8 review: series29 plus patch
0022 and shim changes. Patch 0022: HADDPS/HADDPD and their VEX forms add each pair high element first (the SDM's order:
UZP2 + UZP1, not FADDP), so two NaNs give the high one; UNORD_Q/ORD_Q (CMPPS/CMPPD/CMPSS/CMPSD and VEX, also I5a's legacy
forms) take the exact-flag path, which now finds unordered lanes by integer compares (no flag) and takes the flags from one
fcmeq/fcmge (x86's DE precedence); the YMM upper halves are written through at each instruction start (one STR each) and stay
cached across register-only vector instructions (at most 6), instead of a store and reload per instruction. Shim: an
encoding x86 makes #UD is refused under `--avx-gated` too (the encoding checks come before a gated refusal; the VEX AES
range is 0xdb-0xdf, no longer BMI's 0xf2-0xf7); VLDMXCSR/VSTMXCSR are system cuts (the host's `VLDMXCSR`/`VSTMXCSR`: the
LDMXCSR/STMXCSR transaction under the AVX gating); a pair access's offset is sign-extended in the split/alignment checks.)
Previous: `series29` (October 30, I8: series28 plus patch 0021
and shim changes: the VEX AVX, AVX2, FMA and F16C forms through FEX's AVX128 lowering (`SupportsAVX` on, no SVE256: each
256-bit operation as two 128-bit halves, YMM upper halves in the guest-state page's `avx_high`, a 32-byte operand as one
LDP/STP of two Q registers). The allowlist's `Avx` table (`shim/fexc_slice1.inc`, VEX fields parsed from the bytes: map, pp,
L, W, vvvv) admits the forms by VEX.L, VEX.W, reserved VEX.vvvv and operand kind and refuses the rest with a reason
(gathers, VMASKMOV/VPMASKMOV/VMASKMOVDQU, VLDMXCSR/VSTMXCSR, VCVTPS2PH to memory, VPCMPxSTRx, VEX AES/PCLMULQDQ, scalar forms
with VEX.L = 1); a 66/F2/F3 prefix or an adjacent REX before VEX is a system cut (the host's `vex_ud`: #UD); `--avx-gated`
(the host's: no CR4.OSXSAVE, XCR0[2:1] != 11b or CR0.TS) cuts every AVX form as gated (`cut_gated`, the host's
`AVX_GATED`); VMOVAPS/VMOVAPD/VMOVDQA/VMOVNT*/VMOVNTDQA get the alignment check `and x0, xA, #15|#31` (`fexc_align16`/
`fexc_align32`). Patch 0021: the YMM upper halves flushed per instruction (`FlushRegisterCache(true)`), FS/GS-based 32-byte
operands as one pair, VEX.128 VMOVAPS/VMOVAPD/VMOVDQA m128 as one aligned X-register LDP/STP, VMOVNT* and broadcasts from
memory as plain accesses, element accesses through `FexcLoadElement`/`FexcStoreElement`, VFMADDSUB/VFMSUBADD's addend negated
by `fneg` (NaN sign kept under FPCR.AH = 1), the AVX compare predicates 8..31 with x86's MXCSR flags. Boot-features plan,
Built (I8).) Previous: `series28` (October 29: series27 plus patch 0020: an
instruction that starts in the supplied bytes and runs past them is `partial_decode`, whatever FEX's zero-filled decode of
the missing bytes gives; before it, `0f`, `0f 3a`, `0f 0f`, `0f c7`, `66 0f`, `f3 0f`, `c4`, `c4 e2`, `c5` at the end of the
input were `not_executable` and the Debian kernel stopped at a `jne rel32` whose `0f` ends a page. An instruction that
needs a 16th byte is `instruction_too_long` (end reason 13; the host answers it as system kind `TOO_LONG`, #GP(0) in the
resident: SDM Vol.3 Interrupt 13 (#GP), "Exceeding the instruction length limit of 15 bytes"), unless an unreadable byte within
its first 15 comes first (`partial_decode`: the fetch fault); the 16th byte is never fetched, so it is #GP(0) even when
unreadable (natemu policy: the SDM is silent; Bochs does the same, QEMU takes the #PF). Before it, a
16- or 17-byte instruction whose bytes were all present was translated and run, and a long 3DNow! form wrote past FEX's
15-byte instruction buffer. Objects that do not reach the end of the input and are not over-long are byte-identical to
series27's.) Previous: `series27` (October 29: series26-i5b's patches and
the tree's shim and `fexc.cpp`. I5b's review fixes (the PTWRITE cut reason) and Intel's rule for CS/DS/ES/SS prefixes in
64-bit mode (SDM Vol. 3A §4.5.1): a single CS/DS/ES prefix is now admitted on an RSP/RBP-based operand and a single SS
prefix on any explicit ModRM operand. They add no base; `JoinSites` takes the class of the explicit operand's sites from
the raw prefix bytes (`NullSegment`): SS with an SS prefix, or with an RSP/RBP base and no prefix, else DS, so a
non-canonical address is #SS(0) or #GP(0) as on Intel. Still cut: SS with CS/DS/ES (mixed classes) and SS on a moffs or
string source operand (series47-seg replaced this rule: every CS/DS/ES/SS prefix ignored, nothing cut). Linux 6.12's `ds btr %rdx, 0x540(%rbp)` (its SMP alternatives turn LOCK into DS on one CPU) now
compiles, its sites DS-class. Previous: `series26-i5b` (I5b: the FXSAVE/XSAVE family cut as system instructions). Previous:
`series25` (I6 after review and M3, October 29: patches
0001-0019 and the tree's shim, which holds I6's hunks and M3's `fexc_mmio.inc` completion records. Previous:
`series24-i6` (I6 before review: series23-i6's sources plus
x87 stack faults with their masked response in patch 0019; built from an I6-only copy of the shim in `series24-i6/fexc-tools`,
without M3's in-progress hunks. Previous: `series23-i6` (I6, October 29: series22 plus patch
0019 and shim changes: x87 in FEX's reduced-precision mode, which `--profile slice1` now sets (`X87REDUCEDPRECISION`). The
allowlist's `X87` table admits the x87 forms whose reduced-precision lowering is native code and cuts the resident's
(FNINIT, FNCLEX, FLDCW, FLDENV/FNSTENV, FRSTOR/FNSAVE, FLD/FSTP m80) as system instructions; transcendental, remainder and
scale forms (host libm helpers), FBLD/FBSTP, FXAM, FXTRACT, the 287 no-ops and undocumented aliases are `unsupported_form`.
`--x87-gated` (CR0.EM or TS) and `--wait-gated` (TS with MP) cut x87 forms and WAIT as gated (`cut_gated`, #NM); `--x87-fpcr V`
runs x87 arithmetic under FPCR V when FCW.RC or MXCSR's FTZ/DAZ differ. The JIT wraps every floating-point op of an x87
instruction (`FexcX87Op`): FPSR cleared before it (MXCSR's flags kept in x3), its flags OR-ed into the x87 exception word at
x28 + 0x4b4 after it. Patch 0019: the x87 stack pass on its slow path, stack faults (underflow/overflow: the indefinite, IE, SF, C1) with a flush after every op (the state is in the
guest-state page at every boundary), TOP masked, ST(i) as context-indexed accesses; FIST/FISTP/FISTTP with x86's integer
indefinite; FNSTSW's exception flags from the exception word. Boot-features plan, Built (I6).) Previous tree:
`series22` (X2, October 29: series21-s3 plus a shim
change, no new patch: `--alignment-gated`, the host's when the vCPU runs under x86 alignment checking (CR0.AM, RFLAGS.AC, CPL
3), cuts BT/BTS/BTR/BTC with a memory operand (`0f a3/ab/b3/bb`, `0f ba /4../7`) as system instructions: FEX touches only the
byte holding the bit, so the resident checks the operand's alignment (#AC) and steps it compiled without the flag). Previous
trees: `series21-s3` (S3: the shim cuts group 9's register forms /6 /7, RDRAND/RDSEED/RDPID); `series20` (I5a after review, October 29: series19 plus
patch 0018: MOVAPS/MOVAPD/MOVDQA m128 as one 16-byte aligned LDP/STP of two X registers (sites of kinds 2/3 `load_pair`/
`store_pair`, alignment-checked; SDM 9.1.1 atomicity with AVX enumerated, FEAT_LSE2), DPPS/DPPD masking their inputs before the
multiply and computing the products under a zero destination mask (their MXCSR flags), a dead FCmp (COMISS/UCOMISS) kept for its
flags. Previous tree: `series19` (I5a, October 29: SSE..SSE4.2. Patch 0017 and
the shims: the allowlist's `Sse` table (mandatory prefix from the bytes; MMX forms, PCMPxSTRx, MASKMOVDQU, AES/PCLMULQDQ and a
zero-masked INSERTPS m32 refused), CRC32; LDMXCSR/STMXCSR cut as system instructions; `--sse-gated` (the host's: CR0.EM/TS or no
CR4.OSFXSR) cuts every SSE form (`cut_gated` in the report); vector loads and stores as FPR sites, FEX's element ops lowered to
plain element accesses, MOVNT* to plain ones; named and indexed vector constants built in their register (no state-frame load);
the alignment check `and x0, xA, #15; cbnz x0` to a stub of kind 14 `align` (`brk #4`) on the first access of a legacy SSE
instruction whose 16-byte operand must be aligned; MXCSR flags as x86: COMISS/COMISD `fcmpe`, ROUND* `frintx` (FPCR.RMode
switched for an explicit mode), RCP*/RSQRT* without flags (FPSR saved in x3 and restored). Boot-features plan, Built (I5a).)
Previous tree: `series18` (I3 after review, October 29: series17 plus
`dmb ish` immediately before the `casal`/`caspal` of LOCK CMPXCHG/CMPXCHG8B/CMPXCHG16B (`fexc_lock_cas`): a failing Arm CAS
writes nothing and is only an acquire, while x86 orders a failing locked compare. Kept for comparison: `series17` (I3, October 29: LOCK forms. Patch 0016
and the shims: every lockable memory form with LOCK (ADD ADC SUB SBB AND OR XOR INC DEC NOT NEG BTS BTR BTC XADD CMPXCHG
XCHG) and CMPXCHG8B/16B, locked or not, compile to FEX's LSE lowering, one acquire-release atomic each (`ldaddal`,
`ldclral`, `ldeoral`, `ldsetal`, `casal`, `caspal`), recorded as a site of kind 13 `atomic` (`data_reg` = [Rt, Rs], write
intent; caspal's width is both elements). LOCK NEG, which FEX lowers to a CAS loop (a native backward branch), is under
slice1 one attempt (`FexcLockNeg`: a write-intent load site, `neg`, one `casal` site, and on a lost race a literal exit to
the instruction's own RIP; the dispatcher completes the flags and puts CF in the exit ABI's form first) and ends its block
like a REP string instruction. Register allocation never prefers a static register for the result of a swap or
fetch-and-op atomic (`FexcPrecision::LoadsInto`: after an abort a load's destination is UNKNOWN; CAS/CASP restore theirs)
and frees an unused one (LOCK NOT); a LOCK instruction neither reuses nor leaves a pooled constant (LOCK forms in long
blocks spilled otherwise). LOCK on any form that is not lockable is a system cut ("LOCK prefix on an instruction that is
not lockable (#UD)"), also when FEX's decoder refused the instruction (judged from its ModRM, else its opcode); the host
names it `LOCK_UD` and the resident raises #UD. Refused (`unsupported_form`): F2/F3 with LOCK (XACQUIRE/XRELEASE, no HLE)
and 66/F2/F3 on cmpxchg8b/16b (no SDM meaning). Objects of the 64 fixtures and the seed-4242, flow, I1, I2 and I7 corpora
are byte-identical to series16 but one I7 program with a memory XCHG (its `swpal` result now goes through a temporary).)
Kept for comparison: `series16` (I2, October 29: string instructions.
Patch 0015: under slice1 MOVS/STOS/LODS/SCAS/CMPS (`FexcStringOp`) translate ONE element: its accesses (movs reads DS:RSI
then writes ES:RDI, cmps reads [RDI] then [RSI], FEX's and the pinned QEMU's order), RSI/RDI stepped by DF from the
registers. With REP/REPE/REPNE: `cbz x7` (RCX = 0) to the next instruction, the element, RCX - 1, then a literal exit to
the instruction's own RIP (REPE/REPNE whose ZF ends the repetition branch forward to the next instruction). The resident
links that exit to the block starting at the REP and its unlinking stop patches it, so a stop, an interrupt window or a
fault is always between elements with RCX/RSI/RDI and RIP exact, and the block has no backward branch. The shim ends the
block after a REP string instruction (`FEXC_CUT_STRING`, reason `block_ending_instruction`: one REP and at most three link
points per block) and admits the five opcodes (REP on all, REPE/REPNE on cmps/scas; REP with REPNE cut (I10: 0x67 admitted), and by
policy, not by an x86 rule, F2 on movs/stos/lods (hardware repeats as with REP; the SDM calls it reserved); FS/GS and the
null prefixes only on the DS:RSI source of movs/cmps/lods, never SS there; FS/GS on stos/scas, which hardware ignores, is
cut by policy); a MOVS's load and
store are not an RMW pair (`fexc_movs`), so its load is read intent. FEX's own lowering (MemSet/MemCpy ops for REP
STOS/MOVS, IR loops with backward branches for CMPS/SCAS/LODS, all without partial progress) is the default profile's.
Patch 0014 (I1 carry-over): the backwards register-allocation pass does not coalesce a value into a static register it is
stored to twice (a constant pooled across instructions: AF = 0 from popcnt and from `cmpxchg r16, r16`); the later store
was a self-move, value-correct, but its instruction had no native x27 write (verify_precision P6). Binary objects of the
64 fixtures and the seed-4242, flow, I1 and I7 corpora: see the I2 note in the boot-features plan.)
Kept for comparison: `series15` (I7, October 28: FS/GS segment overrides
in 64-bit mode. The patches are series14's; the change is in `shim/` and `fexc.cpp`: the allowlist admits an FS or GS
prefix on every admitted form with an explicit memory operand (FEX adds `fs_cached`/`gs_cached`, x28 + 1000/992, to the
effective address; the site's native address is then the x86 linear address, so recovery, the split check and the
canonical check see base + EA), and CS/DS/ES/SS prefixes, which 64-bit mode ignores and FEX's decoder drops, when they
name the operand's default segment or there is no memory operand. Cut (`unsupported_form`): FS/GS with no memory
operand or with a second segment prefix (no SDM rule for two of one group; FEX and QEMU differ), and SS naming a
DS-default operand or CS/DS/ES an RSP/RBP-based one (#SS versus #GP for a non-canonical address differs between
vendors; admitted with Intel's class since series27). The allowlist reads the prefix bytes through `fexc_guest_bytes` (`shim/fexc_hook.h`). An FS/GS-overridden
operand is recorded `segment: ds` (#GP(0)) whatever its base; the site JSON's `x86` object carries `segment_override`.
Binary objects of the 64 fixtures and the seed-4242, flow and I1 corpora (383 programs) are byte-identical to series14.)
Kept for comparison: `series14` (I1, October 28: the small integer and
BMI forms. Patch 0013: the decoder records a VEX instruction's map, pp, L and opcode byte (`FEXC_MAP_VEX`, metadata only);
under slice1, CMPXCHG/XADD with a memory operand and no LOCK are plain read-modify-writes (FEX emits CAS / atomic
fetch-add otherwise) and LEAVE loads [RBP] before writing RSP and RBP (FEX's pop moved RBP into RSP's register first);
fexc records LEAVE's load as a stack (SS) access; SHLD/SHRD to memory by an immediate count masking to 0 writes the value
back (a write-intent read-modify-write like a shift by 0; the pinned QEMU faults W on a read-only page for these too);
CLD/STD store DF in their own instruction (the per-instruction register-cache flush includes DF). The allowlist (`shim/fexc_slice1.inc`) adds the I1 forms listed under
"Allowlist". Objects of the 64 fixtures, the seed-4242 corpus and the flow corpus are byte-identical to series13.)
Kept for comparison: `series13` (E3, October 28: no address guard; the
patches are series12's, the change is in `shim/fexc_sites.inc` and `shim/fexc_precision.h`: `FexcBeforeOp` emits only the
split check, the read-modify-write prescan is gone; generated code runs at EL0, where every non-canonical address faults
in hardware at its site and the runtime raises #GP/#SS; 556 blocks and 4,233 guest instructions on the census corpora
as before, 19,712 words, 17.4% fewer than series12, 0 address-guard words: `experiments/track03/el0-study/e1census.py
--before`). Kept for comparison: `series12` (E1b, October 28: patch 0012, no stop
check at the block entry: the resident stops a vCPU by IRQ-triggered unlinking; otherwise series11; 556 blocks, 23,866
words on the census corpora, none touching emulator data and no `bl`), `series11` (E1: patch 0011, code that
touches no emulator data: literal exits `movz/movk x0, #rip; svc #1`, computed exits `mov x0, xR; b .` relocated by the
resident to its dispatcher (a `dispatch_branch` relocation), the 128/64 division as `svc #3`/`#4`, no block-header store,
the stop check a `bl .` relocated to the vCPU's stop pad, the pre-NZCV save `mrs x25, nzcv` instead of a frame store;
object version 4, header field `pre_nzcv_register` = 25; the same sites, blocks and guest instructions as series10 on the
fixtures, seed 4242, the flow corpus and the benchmarks, 13.5% fewer words: `experiments/track03/el0-study/e1census.py`).
Kept for comparison only: `series10` (S3e: the shims admit
memory xchg as a `swap` site (kind 12: one `swpal`, LSE on as before through the `atomics_lse` feature default; an address
guard, no split check), lfence/mfence/sfence and pause; objects identical to series9 on the 64 fixtures and the 44 seed-4242 programs, which have none of
these forms), `series9` (series8 with `loop/loope/loopne/jrcxz` allowlisted, S2d), `series8` (patches 0001-0010, loop/jrcxz cut), `series7` (patches 0001-0009, literal exits through DispatcherLoopTop), `series6` (patches 0001-0008, before self-loop exits and the stop check), `series5`, `series4`
(patches 0001-0007, before division), `series2` (patches 0001-0006, the "before" of the guard cost), `s1c` (slice1
without the precision patches, the "before" of the precision cost) and `c1b` (the v1 build without sites). The earlier `series1` (flag rule before the cheaper one below) and `s1b1` are gone.

The dependency archives of the old I4-V producer (`upstream.tar.gz`, `dependency-overlay.tar`) are no longer used; the
submodule tree and the three externals are identical to them (the other overlay members, `vixl`, `jemalloc_glibc`,
`range-v3`, `rpmalloc`, `cpp-optparse`, are not used by this build and not initialised). Running
`upstream_tests/run_icount.py` additionally needs `git -C third_party/FEX submodule update --init --depth 1 External/vixl`
(it builds a VIXL disassembler from it into `--work`, and reuses one that is already there; `git -C third_party/FEX
submodule deinit -f External/vixl` afterwards frees the disk). It passes the JSON's `Env` options through `fexc --config`.
Result on the pin: all 7,709 InstructionCountCI tests pass, count and text, `--variant upstream` (6,905 before `--config`;
`--config` alone accounts for 802 of the 804 more, the 2 `rdpid` tests need `SYS_getcpu = 168` in `shim/fexc_compat.h`).

### The patch series

`patches/` is `git format-patch` output on top of the pinned commit (author `natemu`, fixed date), in order:

| patch | purpose |
|---|---|
| 0001 | macOS arm64 portability: SpinWaitLock overloads, no PROT_EXEC for JIT buffers, `posix_memalign`, `memset` for `VirtualDontNeed`, 16 KiB host-page guard pages |
| 0002 | guest-code window seam: `ResolveGuestCodePointer`, integer arithmetic on guest PCs in Frontend/Core |
| 0003 | compile-only export hook (`fexc_compile_hook`, dense PC table) and extra `DebugData` (entry prologues, static register map) |
| 0004 | access-site recording: CF inversion capture, `FexcAfterOp` in the JIT, spill size |
| 0005 | slice1 block cutting (`FexcMap` in the decoder, `FexcSlice1::Cut`) and single-gate exits |
| 0006 | slice1 precision (flag elimination, register allocation, pre-NZCV save) |
| 0007 | slice1 check hooks (JIT hooks, pre-RA scan, `ret` without writeback): split-store check, branch-target check; the address guard until series12 (E3 removed it in `shim/`) |
| 0008 | slice1 #DE for div/idiv (width record in the dispatcher, check after FEX's LUDIV/LDIV call, divide ops as flag fault points) |
| 0009 | slice1 self-loop exits (no `JumpTargets` lookup for the taken edge of jcc/jrcxz/loop) and FEX's pending-interrupt fault check at every block entry |
| 0010 | slice1 literal exits through `Pointers.ExitFunctionLinker` (computed exits keep `DispatcherLoopTop`): FEX's linker/dispatcher split, so the resident can link them |
| 0011 | slice1 code that touches no emulator data (E1): literal exits `movz/movk x0; svc #1`, computed exits `mov x0, xR; b .` (relocated by the resident), the 128/64 division by `svc #3/#4`, no header store, the stop check `bl .` (relocated to the stop pad), the pre-NZCV save `mrs x25, nzcv` (the JIT emits it at the instruction start the flag pass chose, `fexc_pre_nzcv`); FEX's BLOCK_DEBUGGING exit branch back to its upstream form |
| 0012 | slice1 no stop check at block entry (E1b): `EmitSuspendInterruptCheck` emits nothing; the resident's IRQ handler stops a vCPU by patching the interrupted block's exits |
| 0013 | slice1 I1 forms: the decoder's VEX map record, CMPXCHG/XADD without LOCK as plain read-modify-writes, LEAVE's load before RSP/RBP |
| 0014 | slice1 no coalescing of a value stored twice to one static register (the later store was a self-move without a native write) |
| 0015 | slice1 string instructions one element per pass (`FexcStringOp`; REP exits to its own RIP), `fexc_movs` for the site recorder |
| 0016 | slice1 LOCK forms: LOCK NEG as one compare-and-swap attempt (`FexcLockNeg`, an exit to its own RIP on a lost race), `dmb ish` before LOCK CMPXCHG*'s CAS, swap/fetch-and-op results never preferred into a static register, no pooled constant across a LOCK instruction |
| 0017 | slice1 I5a SSE: sites, vector constants in their register, MXCSR flags as x86 raises them |
| 0018 | slice1 I5a after review: MOVAPS/MOVAPD/MOVDQA m128 as one aligned LDP/STP, DPPS/DPPD flags, a dead FCmp kept |
| 0019 | slice1 I6 x87 reduced precision: the x87 stack pass on its slow path, flushed after every op, TOP masked, ST(i) as context-indexed accesses; stack faults with their masked response (the indefinite, IE, SF, C1); C1 on pushes, FCHS/FABS and FCOMI; FCHS/FABS as sign-bit operations; FLD m64 and FST m64 quieting an SNaN (IE; FLD m64's DE); FIST/FISTP/FISTTP with the integer indefinite (flag-free); FXAM's classes; FNSTSW's exception flags from the x87 exception word and FEX's IE, PE and SF bytes |
| 0020 | Decoder: `PARTIAL_DECODE_INST` when an instruction read at least one byte and then needed an unreadable one within 15 bytes (`ReadData`: its field's first unreadable byte), whatever the substituted zero bytes decode to; `TOO_LONG_INST` (new) when it needs a 16th byte, readable or not (`ReadByte` at `>=` 15, a `ReadData` field ending past 15, 15 prefixes); NOEXEC only when its first byte is unreadable |
| 0021 | slice1 I8 AVX128: YMM upper halves flushed per instruction, FS/GS-based 32-byte operands as one LDP/STP, VEX.128 VMOVAPS/VMOVAPD/VMOVDQA m128 as one aligned X-register pair, VMOVNT*/VMOVNTDQA and broadcasts from memory as plain accesses, element loads/stores as plain element accesses, VFMADDSUB/VFMSUBADD's addend by `fneg`, AVX compare predicates 8..31 with exact MXCSR flags |
| 0022 | slice1 I8 review: HADDPS/HADDPD (legacy and VEX) high element first, UNORD_Q/ORD_Q with exact flags (integer unordered test, one flag compare), the YMM upper halves written through per instruction and kept cached across register-only vector instructions |

Anything that belongs to us (hook ABI, recorder, allowlist, predicates) stays in `shim/` and `fexc.cpp`; the patches only
include and call it. To change a patch: `build.sh` into a scratch dir, edit and commit in `BUILD_DIR/src`, then
`git -C BUILD_DIR/src format-patch --no-signature --zero-commit -o tools/fexc/patches 48d7175..HEAD`.

Patches 0023-0028 are described in "Current tree" above and the plan's Built notes. 0030 (series52-adox66, November 4): FEX's
0F 38 table has no slot for 66 with F3; for opcode F6 that prefix pair is decoded as ADOX with the 66 dropped (the reference AMD
machine runs 66 F3 and F3 66 0F 38 F6 as ADOX at the unchanged operand size; boot-features plan, Built (ADX)). 0029 (series46-br66): under slice1 in
64-bit mode, 66 on a near direct branch (Jcc rel8/rel32, JMP rel8/rel32, CALL rel32, LOOP/LOOPcc, JrCXZ) no longer narrows
the decode: the operand size stays 64 bits and the displacement rel8/rel32, as Intel processors do (SDM Vol.2 CALL: "the
operand size for near call (and all near branches) is forced to 64-bits" and "In 64-bit mode the relative offset is always
a 32-bit immediate value"; JMP and Jcc: rel16 "Not supported in 64-bit mode"; Appendix A, superscript f64). FEX decoded rel16
as AMD does; its lowering already used a 64-bit push and target.

### Compile server (`fexc --serve`, S2a; P3)

One persistent process answers compile requests, length-prefixed (u32 little-endian) on stdin/stdout; EOF on stdin ends it.
Request: `len`, `args_len`, the CLI arguments as NUL-terminated strings (without `--hex/--file/--out/--object`), then the
guest bytes. Reply: `len`, `code` (i32: the CLI's exit code), `json_len`, `object_len`, `extent`, `args_read`, JSON
(error text for `code >= 2`), binary object (always produced for `code 0`). The object equals the one-shot CLI's. The JSON
is, by default (the host's server, since series48-lean), only what the host reads: `{"status":"ok"}` for an object; for no
code the status and `guest.end` (reason, RIP, detail, `cut_*`), or, when FEX emitted nothing, the CLI's report without its
header (status, error, `guest`, diagnostics). Building the full report was over half of a request's CPU (perf plan,
"Measured (chunk 1)"; 28.6 to 12.3 us per request on an installer boot's 1.14 M requests, "Measured (chunk 2)").
`fexc --serve --report` (`test_serve.py`) sends the CLI's report instead, less the CLI's hex dumps (`input.bytes_hex`, each
instruction's `bytes_hex`, `code.hex`: the caller has the bytes, the object the code) and the one value that differs
between two CLI runs, the dispatcher's host address `emitted_value`. The report never decides the object, extent or
args_read: the end reason, the zeroed relocation slots and the end marker are computed before it.

`extent` and `args_read` say what the compile depended on, for the caller's cache key (the host's `Compiler`). Every look
at the guest bytes or at the input's size goes through `InWindow` (FEX's decoder learns the size from
`QueryGuestExecutableRange` but uses it only to check a range it then reads), which records the end of each range: the
compile read only bytes `[0, extent)` and, when `extent` is less than their length, gives the same object, end reason and
cut for any input of at least `extent` bytes with that prefix (the JSON differs in `input.size`, the input's length). The gating arguments (`--sse-gated`, `--avx-gated`, `--alignment-gated`, `--x87-gated`,
`--wait-gated`, `--x87-fpcr`) are `FexcArg`s in `shim/fexc_hook.h`: reading one sets its bit (`FEXC_ARG_*`) in `args_read`;
the shim tests the form before the flag, so a block without a gated form reads none of them. Both are 0 for an error.

**Requests run in this process** (`--serve-mode reuse`, the default since P3): one FEX context per configuration (Config
values, host features, profile), kept, and nothing of a request left in it: the compile is FEX's `CompileBlock` without
its caches (the block never enters the context's lookup cache, its relocations are cleared, the code buffer is emptied
after the hook copies the code), FEX's process-global Config is rebuilt from nothing per request (`Config::Shutdown`, then
`Initialize`; a plain `Initialize` kept an earlier request's `--config` values) and the recorders are reset
(`ResetGlobals`). About 0.04 ms per request against 0.45 ms forked (the boot's request set). Server memory is bounded
by the number of configurations, not of requests: about 10 MiB with the host's one (1.34 M requests of a boot), 22-29 MiB
with the many of `test_serve.py` (each kept context holds a thread, its JIT and dispatcher). Contexts are never torn down
(FEX's destructors assume a running emulator thread), so the earlier context per request leaked about 145 KiB each
(`--serve-mode fresh`, removed). `--serve-mode fork` (debugging) runs each request in a forked child of a server that never
compiles: no FEX state crosses requests and a crash costs one request. It is not the same process environment, though:
on this macOS, CNTFRQ_EL0 reads 1 GHz in a normal process and 24 MHz in a forked child (P3 review), so under the default
profile the code FEX emits for RDTSC and CPUID leaf 15H, which reads it, differs between fork mode and the CLI or the
in-process server. Profile slice1 does not use it (both are system instructions the resident runs), so slice1 objects are
the same in every mode. `test_serve.py FEXC [--mode M] [--corpus C.json]` checks either mode against the CLI (requests at one RIP with
other bytes and settings in shuffled orders included), and a second server without `--report` against the first (same
code, object, extent, args_read; only the host's fields in its JSON); the P3 replay of a boot's 76,610 logged requests found no
difference from series32's fork mode, and chunk 2's replay of an installer boot's 1,142,279 recorded requests through
series47-seg and series48-lean none in code, object, extent, args_read or `guest.end`.

## What builds

`libfexcore.a`: decoder/frontend, x86 tables, OpDispatcher + IR, passes,
register allocation, ARM64 JIT backend/emitter, dispatcher generator, plus the
vendored fmt, xxhash, SoftFloat-3e and cephes. `fexc_linktest` creates a
`FEXCore::Context` (which references the whole library) and, with `--init`,
runs `InitCore()` (generates the dispatcher). Both succeed. No guest code has been compiled.

`CMakeLists.txt` is our own project that lists the sources of
`FEXCore/Source/CMakeLists.txt`; FEX's top-level CMake is not used (it assumes
Linux and drags in the application layer).

## Excluded

- Everything under `Source/` (FEXLoader/FEXInterpreter, LinuxEmulation syscalls,
  thunks, signals, FEXServer, config tools, `Source/Common` host-feature probing).
- vixl (disassembler/simulator), Zydis, tracy, jemalloc/rpmalloc, Catch2, range-v3,
  tiny-json. The disassembler is not needed; use `llvm-objdump` on the output.
- FEX's own `Config::Load()` file/env layers (not called; set options with
  `FEXCore::Config::Set`).

## Shimmed / patched (why)

`shim/` (force-included `fexc_compat.h`, plus stub headers):
- Linux headers FEXCore names but a compile-only tool does not use:
  `malloc.h`, `syscall.h`, `sys/prctl.h` (prctl returns -1/EINVAL = "no MDWE"),
  `sys/sendfile.h` (read/write emulation), `sys/vfs.h`, `linux/{limits,futex,magic}.h`.
- Constants: `MADV_HUGEPAGE/NOHUGEPAGE/DONTDUMP`, `MAP_FIXED_NOREPLACE`, `MREMAP_*`
  (inert). `mremap` aborts if reached (disk-cache page finalisation, never used).
- `syscall(...)` is redirected to `fexc_syscall` (`shim/fexc_platform.cpp`): futex
  wait returns immediately (waiters re-check in a loop, so it degrades to polling;
  wake is a no-op), rt_sigprocmask is a no-op, getrandom uses `arc4random_buf`,
  other numbers fail with ENOSYS. Valid because the tool is single-threaded and
  receives no signals.
- `memalign`, `malloc_usable_size`, `sincos` (glibc extensions).
- Generated files use FEX's own generators (`json_ir_generator.py`,
  `config_generator.py`); `git_version.h` is a fixed stub.

Patch 0001 (`patches/`):
- `SpinWaitLock.h`: `size_t` is `unsigned long` on macOS but `uint64_t` is
  `unsigned long long`; add the missing overloads.
- `AllocatorHooks.h`: JIT/dispatcher buffers are mapped without PROT_EXEC (macOS
  arm64 refuses writable+executable anonymous maps; we never execute output).
  **Consequence: generated code cannot be run in-process; it is only exported.**
- `AllocatorHooks.cpp`: macOS `aligned_alloc` rejects small alignments (returned
  NULL and crashed `CreateNewContext`); use `posix_memalign`.

Patches 0002 and 0003 (C1b): the guest-code window seam from `i4v_producer/adapt.py` (deleted at S3i; in git history)
(`ResolveGuestCodePointer`, integer arithmetic on logical PCs in `Frontend.cpp/.h`,
`Core.cpp`); the export hook (`shim/fexc_hook.h`, called from `ContextImpl::CompileCode`);
extra `DebugData` fields (entry prologue end offsets, static register map, filled in
`JIT.cpp`); the dense PC table (`fexc_dense_pc_table`); 16 KiB-page fixes (guard pages sized by
`FEXC_HOST_PAGE_SIZE`, `VirtualDontNeed` zeroes with `memset` because macOS
`MADV_DONTNEED` does not).

Patch 0004 (S1a; see "Access sites" below): `OpcodeDispatcher.h`/`Core.cpp` capture the dispatcher's CF
inversion at each `GuestOpcode`; `JIT.cpp` calls `FexcAfterOp` after every emitted IR op and keeps the spill size;
`JITClass.h` declares it; `MemoryOps.cpp` includes `shim/fexc_sites.inc`, which defines it.

Warnings are suppressed (`-w`) for the vendored sources.

## fexc CLI (C1b)

```
BUILD_DIR/obj/fexc (--hex HEX | --file RAW) --rip N [--bitness 32|64] [--max-insts N] [--profile default|slice1]
                   [--multiblock] [--sparse-pc] [--no-end-marker] [--no-tso]
                   [--feature NAME=0|1]... [--config NAME=VALUE]... [--out FILE] [--object FILE]
```

Input: raw x86 bytes (1..65536), full 64-bit guest RIP (canonical, including
upper half; 32-bit mode needs rip+len <= 4 GiB), mode. The bytes are an immutable
window at that RIP; FEX reads them through `ResolveGuestCodePointer` by logical
address, so nothing is mapped at the guest address. Anything past the window is
"not executable" and ends the block (`end.reason = end_of_input`).
`--max-insts N` stops after N guest instructions (default: FEX's own limit, 5000).
`--config NAME=VALUE` sets any FEX config option after fexc's own settings (NAME: the option's enum name without `CONFIG_`,
e.g. `X87REDUCEDPRECISION`, case-insensitive, a leading `FEX_` is accepted; an unknown name is refused); the options given are
echoed in `settings.config` (absent when none). One fresh FEX context per run (FEX fixes the mode per context). Exit code: 0 ok,
1 no instruction translated (JSON still written), 2 bad arguments, 3 FEX init failed.

### Settings (one place: `FeatureTable()` / `Settings` in `fexc.cpp`)
Recorded in the output under `settings` and `features`. Default profile
`old-producer-compat+m4`: TSO on (also vector and memcpy/memset TSO), multiblock off,
SVE/LRCPC/LRCPC2/RPRES/WFXT off, LSE atomics and preserve_all ABI on (SVE, LRCPC, LRCPC2, LSE and
preserve_all match the old producer's logged `NATEMU_FEATURES`; RPRES and WFXT are pinned off to
match the old profile), FEX's `InstCountCI` end marker on. Every other
bit is Apple M4's ID-register view (sysctl `hw.optional.arm.*`); what the old Linux VM
exposed for those bits (FlagM, FCMA, ...) is **unverified**; the integer code of the 53 pinned
bodies and 64 reference programs does not depend on them. `DCacheLineLog2` is fixed at 4. `--feature
NAME=0|1` overrides one bit. Later this table is to be derived from the VM's vCPU.

### Output (`format: natemu-fexc-v2`, one JSON object; v2 = v1 plus `sites`, `code.layout.spill_bytes`, `cf_inverted` per `pc_table` entry)
- `input`, `settings`, `features`: echo of everything that influenced codegen.
- `status`: `ok` | `no_instructions_translated`. (`no_code` when FEX produced nothing.)
- `code`: `hex` (the whole FEX block: 4-byte header, entry prologue, body,
  end marker, exit/link stubs, tail, encoded RIP table), `size`, `alignment`,
  `layout` (header; per-entry `entry_native_offset` and `body_native_offset` from a
  small patch in `JIT.cpp`; `end_marker_offset` of `udf #0x420f`; `tail` offset/size and
  decoded fields; `code_region_begin/end`), `relocations`.
  Relocation literal slots in `hex` are zeroed; each relocation carries its
  `emitted_value`. Kinds actually seen: `guest_rip_literal` (8-byte guest RIP the block
  exits to / its own entry), `named_symbol_literal` (`exit_function_linker`: host address of
  FEX's block-link routine), `guest_rip_move` (guest-RIP constant materialised by a
  mov/movk sequence; `size` is null because unpadded sequences are 1-4
  instructions). The other FEX kinds (thunk, patchable) are decoded but only arise with
  disk-cache or thunk features.
- `state_abi`: state register (`x28`), static register map actually used for this
  mode (GPRs in x86 order then PF, AF; xmm registers), offsets in `CpuStateFrame` of the
  fields the code touches, and `state_pointer_loads`: every `ldr xN,[x28,#imm]` in the code that
  reads FEX's helper/dispatch pointer table (`Pointers`), with the pointer's name. These
  state-relative loads are how generated code reaches helpers, syscall and the dispatcher;
  they are *not* relocations.
- `guest`: instructions translated and their bytes, per decoded `blocks`
  (status), every decoded instruction (`translated`, FEX's mnemonic, `ends_block`,
  `sets_rip`) and `end` = {rip, reason, detail}. Reasons: `max_instructions`,
  `fex_instruction_limit`, `block_ending_instruction`, `end_of_input`,
  `invalid_instruction`/`partial_decode`/`not_executable`/`unimplemented_instruction`/`instruction_too_long`
  (decoder status), `dispatch_failure` (OpDispatcher cannot translate), `other`
  (e.g. the dispatcher itself requested a block end).
- `pc_table`: FEX's `DebugData.GuestOpcodes` verbatim (guest offset, native offset), plus
  `cf_inverted`: the dispatcher's guest-CF inversion at that instruction's start (the block
  entry is in the ABI state, inverted). Dense by default since v2: one entry per guest
  instruction, which sites need (an instruction FEX does not consider able to fault, e.g. the
  moffs `movabs`, would otherwise get no `GuestOpcode` and its access would be attributed to the
  previous instruction). `--sparse-pc` restores FEX's own sparse table, diagnostics only. The
  setting changes only the tail's encoded table, not the code region (checked on the 64 programs).
- `sites`: see below.
- `subblocks`: FEX's native sub-block extents.
- `recovery`: what is present (per-access sites, flag location, PC table, static register map,
  spill bytes) and what is still absent (the guarantee that guest state at a site equals the
  state before its guest instruction, committed effects, guard and split check).
- `diagnostics`: FEX log messages captured during compilation (for example
  `NoExec instruction in entry block` is expected at the end of the window).

## Known limits / problems seen
- FEX decodes with user-mode semantics: system instructions are not recognised as such
  (`syscall` becomes a 576-byte call-out through `SyscallHandlerFunc`; `mov cr3,eax`
  translates one 2-byte instruction and then reports `partial_decode`). The runtime
  must cut blocks before system instructions itself (vm-restructure step 3); the `default` profile does not, profile `slice1` does.
- Code reaches memory by using the guest address directly (`ldar x4,[x6]`): guest VA is
  assumed to equal the host/VM address in the translated code. Segment bases for 32-bit
  code live in `CpuStateFrame` (`cs_cached` ... `gs_cached`).
- Jump targets and block links go through `exit_function_linker` / `DispatcherLoopTop`
  state pointers; the resident runtime must provide equivalents.
- JIT temp-buffer overflow normally recovers through a SIGSEGV guard-page long jump in
  FEX's signal handler, which this tool does not install. Blocks up to 5000 SSE/x87/ALU
  instructions (about 100 KB of native code) compiled fine; an overflow would crash, not
  corrupt.
- 16 KiB host pages: guard pages and `VirtualDontNeed` are patched (see above); the
  "Failed to mprotect last page" error that appeared before the patch no longer occurs.
- `fexc` ends with `_exit` instead of destroying the context (FEX's destructors assume a
  running emulator thread).

## Smoke check
`smoke.py FEXC OUT_DIR` compiles two sample inputs from `tools/fexc/producer_bodies.json`
(the old producer's pinned cases) (64-bit high RIP, 32-bit) and three from `fixtures/slice1/programs.json`, writes JSON
and disassembles each block with `xcrun objdump` (llvm-based; it wraps the words in a
Mach-O object first). The full comparison with the old pinned bodies and the structural
self-check are in `test_fexc.py`.

The scripts that read `fixtures/slice1/programs.json` (git-ignored; `smoke.py`, `check_sites.py`, `scan_skips.py`,
`verify_precision.py`, `test_slice1.py`) get it through `check_sites.fixture_programs()`, which runs
`tools/reference/build_slice1.py --programs-only` (needs host clang) when the file is missing.

## Profile slice1 (S1c)

`--profile slice1` (put it first: later flags override) compiles for the first connected slice
([contract](../../docs/restructure/slice1-contract.md) §2). The `default` profile is unchanged (the 53 pinned bodies and the
64 programs give the same code, sites, PC table and relocations as the S1a build).

- **Settings** (recorded in `settings`: `profile`, `block_cutting`, `exits`, plus the usual fields): 64-bit only, FEX TSO
  emulation off (hardware TSO, so plain `ldr`/`str`, `ordering: plain`), no end marker, dense PC table, multiblock off
  (`--multiblock` and `--sparse-pc` are refused; `--no-tso` and `--no-end-marker` are no-ops), FlagM required
  (`features.required_by_settings_profile`; `--feature flagm=0` is refused).
- **Exits** (patch 0011; before it, patches 0005/0010 used FEX's `BLOCK_DEBUGGING` branch with a `State.rip` store and a
  frame pointer load). Under `fexc_slice1`, `BranchOps.cpp::ExitFunction` emits an exit to a literal RIP as
  `InsertGuestRIPMove(x0, rip)` (`movz/movn/orr` then `movk`s, a `guest_rip_move` relocation with register 0) and `svc #1`
  (a link point: the resident's linker rewrites its first word into `b <target entry>`); a computed exit as `mov x0, xR`
  and a placeholder `b .` recorded as a `dispatch_branch` relocation, which the resident relocates to its dispatcher when it
  places the block. No literal, no frame access, no `br`; no exit-linker literal, `named_symbol_literal`, FEX jump thunk or
  x25 call-return stack; `state_pointer_loads` is empty. Guest RIP constants for RIP-relative addresses and pushed return
  addresses stay `guest_rip_move` into other registers. The block entry is a placeholder `bl .` (the stop check, relocated
  to the stop pad); there is no header store.
- **Block cutting.** Before each instruction `GenerateIR` asks `shim/fexc_slice1.inc` (`FexcSlice1::Cut`). If the instruction
  is outside the allowlist, the block ends before it: `ExitFunction` to its RIP (or, when it is the first instruction, no
  code: `status: no_code`, rc 1, with `guest.end`). `guest.end.reason` is `system_instruction` or `unsupported_form`, with
  `detail` and `cut_opcode_map/cut_opcode/cut_fex_name`. A decoder end-of-input status (`partial_decode`,
  `not_executable`, `bad_relocation`) on the FIRST instruction also gives no code, with that status as the reason (never FEX's
  NoExec/SIGSEGV block); mid-block, FEX already ends the block before the undecodable bytes without emitting anything for
  them, and the status stays the end reason. Dispatch failures of allowlisted instructions stay `dispatch_failure`.
  `--object` is written only when the status is `ok`.
- **How instructions are classified.** The patched decoder records the opcode map (`DecodedInst::FexcMap`: primary,
  0f plain/F3/F2/66 overlay, 0f38, 0f3a, 3dnow); the key is (map, `OPRaw`, ModRM.reg for group opcodes) plus the decoder's
  prefix flags. FEX's table names are not usable alone (`MOV` is also 0f 20..23 and 8e), FEX does not decode every system
  instruction (0f 20..23 are two bytes without ModRM; lss, clac, invpcid fail to decode), and a decode failure
  keeps the map, opcode byte and ModRM. The system class wins over prefix checks; the allowlist never admits something that
  did not decode.
- **Allowlist.** Primary map: add/or/adc/sbb/and/sub/xor/cmp (all six forms), push/pop r, push imm, mov (88-8b, a0-a3,
  b0-bf, c6/c7 /0), lea, movsxd, imul (69/6b), test, group 1, shifts/rotates (c0/c1/d0-d3 except /6; rcl/rcr only with a register operand: FEX branches around the whole access for a
  static or dynamic zero count, which would lose a guest #PF), group 3 inc/dec-style
  ops (test /0, not, neg, mul, imul, div, idiv), fe/ff inc/dec/push, nop, endbr64 (f3 0f 1e fa, a no-op), pause (f3 90 without
  REX.B, FEX's `yield`), xchg (86/87, 90-97; with a memory operand, LOCK prefix allowed, it is FEX's `swpal` at a `swap` site, S3e),
  cbw/cwde/cdqe and cwd/cdq/cqo; since I1 clc/stc/cmc, cld/std (DF in the guest-state page), lahf/sahf, pushf (no 66),
  leave (no 66), enter with nesting level 0 (no 66). Two-byte map (no mandatory-prefix overlay): nop (0f 1f), cmovcc,
  setcc, bt/bts/btr/btc (a3/ab/b3/bb, ba /4-7), imul, movzx, movsx, bsf/bsr, bswap; lfence/mfence/sfence (exactly 0f ae e8/f0/f8,
  no 66/F2/F3: FEX's `dmb ld`/`dmb sy`/`dmb st`); since I1 shld/shrd (32/64-bit), cmpxchg/xadd without LOCK,
  prefetchnta/t0/t1/t2 with a memory operand (0f 18 /0-3, `prfm`, never faults) and movnti m (a plain store). F3 overlay
  (I1): popcnt, tzcnt, lzcnt (f3 0f b8/bc/bd; the CPU profile advertises POPCNT, BMI1 and LZCNT, and FEX decodes f3 0f
  bc/bd as TZCNT/LZCNT unconditionally). VEX (I1): andn, bextr, blsi/blsmsk/blsr, bzhi, mulx, rorx, sarx/shlx/shrx with
  VEX.L = 0 and no 66/REX/F2/F3/LOCK before the VEX prefix. Still cut: pdep/pext (FEX emits a native loop), prefetchw/0f 0d
  (PREFETCHW not advertised), 16-bit shld/shrd, enter with a nesting level, popf. LOCK (I3): on every lockable memory form
  (add/or/adc/sbb/and/sub/xor r/m,r and group 1 but cmp, inc/dec, not/neg, bts/btr/btc, cmpxchg, xadd, xchg), one LSE
  atomic each (LOCK NEG: one compare-and-swap per pass, ending its block); cmpxchg8b/16b (0f c7 /1 m) with or without LOCK,
  not with 66/F2/F3; F2/F3 with LOCK cut. Strings (I2):
  movs/cmps/stos/lods/scas (a4-a7, aa-af) at every width, alone or with REP (all five) and REPE/REPNE (cmps/scas); a REP
  form ends its block (I10: with 0x67, ESI/EDI/ECX); cut: REP with REPNE, SS on the source, and by policy (hardware repeats or ignores them) REPNE on
  movs/stos/lods and FS/GS on stos/scas.
  Branches (end the block): jcc rel8/rel32,
  jmp rel8/rel32 and near indirect, call rel32/indirect, ret, rep ret (f3 c3), ret imm16. All with no LOCK, REP/REPNE (except
  rep ret, the 0f 1e NOPs, pause and the strings; LOCK only on the lockable forms; I10: 0x67 admitted but on BT* m, r), and (ret and
  indirect branches) no operand-size prefix (on near direct branches 66 is ignored as on Intel: patch 0029); FS/GS
  overrides only on an explicit memory operand and alone, CS/DS/ES/SS only naming the default segment (I7); 66-prefixed bswap
  (undefined result) is cut.
- **System class** (reason `system_instruction`): segment push/pop and moves, lss/lfs/lgs, far call/jmp/ret, iret,
  int/int3/into/icebp, ud2/ud1/ud0, popf/cli/sti, hlt, port I/O, all of 0f 00 and 0f 01 (descriptor tables, invlpg,
  swapgs, rdtscp, xgetbv, ...), lar/lsl, syscall/sysret/sysenter/sysexit, clts, invd/wbinvd, clflush, mov cr/dr, wrmsr/rdmsr/rdpmc,
  rdtsc, cpuid, rd/wr fs/gs base, vmread/vmwrite, invept/invvpid/invpcid; and (I3) LOCK on any instruction that is not a
  lockable memory form, decoded or not (#UD: the host's `LOCK_UD`). Everything else outside the allowlist (the string
  refusals above, x87/SSE/AVX, loop/jrcxz with a 67 prefix, the I1 refusals above, ...) is `unsupported_form`.

### Native control flow inside one guest instruction
`scan_skips.py` (and a test in `test_slice1.py`) looks, in the emitted code, for a forward branch inside one guest
instruction's native range that jumps over one of its recorded sites, and for admitted memory forms with no site at all. It scans
629 single-instruction forms (shifts and rotates by 1, CL and immediates 0/5/31/32/255 at 8/16/32/64 bits, memory and
register; bt family; cmov, bsf/bsr, imul, movzx/movsx, ALU forms, setcc, push/pop, call/jmp [m], div/idiv, ...) and the 64 fixture
programs. Divide stubs are not accesses: the branch around a 64-bit division's helper call (and its #DE test) is FEX's
inline path and skips no access. Only rcl/rcr with a memory operand were found (a `cbz` on the count around the load/store, and no
access at all for a masked-to-zero static count: rcl/rcr by 0 or 32 at 8/16/64 bits); they are cut. All other forms access
memory unconditionally, including shifts and rotates by CL and by an immediate that masks to zero. Since I2 a REP string instruction's
`cbz x7` (RCX = 0) skips its element's sites, which is x86 (no access when the count is 0); `verify_precision.py` P5
exempts exactly that branch of exactly those instructions.

## Access sites (natemu-fexc-v2)

`sites[]`, sorted by `native_offset`, one entry per native instruction FEX emitted for a guest
memory access. Recorded in `Arm64JITCore::FexcAfterOp` (`shim/fexc_sites.inc`, called from the
JIT's IR-op dispatch loop in `JIT.cpp`) right after each IR op is emitted. At that point register
allocation is done, so the registers are final, and the op's emitted native range is known; the
`GuestOpcode` IR op in force (one per guest instruction, a metadata op that emits no code) names
the guest instruction. FEX emits IR ops in order and no later pass moves a memory op across a
`GuestOpcode`, except that `TryPostRAMerge` fuses two adjacent guest push or pop into one
`stp`/`ldp` (`PushTwo`/`PopTwo`); those sites are `push_pair`/`pop_pair` and carry both guest
instructions (`guest_offset`, `guest_offset2`). The dispatcher's CF inversion is captured where the
`GuestOpcode` op is created (after the per-instruction `FlushRegisterCache(true)`). Segment and
the x86 addressing form come from FEX's own decode of the instruction.

| field | meaning |
|---|---|
| `native_offset` | the faulting native instruction (last load/store-class word of the op's range with a base other than x28/sp) |
| `guest_offset`, `guest_offset2` | guest instruction(s), offset from the block RIP; `guest_offset2` only for fused pairs (binary object, since series23-m3: a load/store site's M3 completion record, JSON `mmio`; `shim/fexc_mmio.inc`) |
| `insn_native_offset` | native offset where that guest instruction's translation starts (its PC-table entry) |
| `ir_op` | FEX IR op (diagnostic) |
| `kind` | `load`, `store`, `load_pair`, `store_pair`, `push`, `pop`, `push_pair`, `pop_pair`, `other` (a guest-memory IR op the record cannot describe: a consumer must refuse it); `guard`, `split`: slice1 check stubs (see "Guards (S1b2)") |
| `width` | bytes the native instruction accesses (pairs: both registers) |
| `intent`, `rmw` | `read`/`write`; for a memory-destination read-modify-write the load has `rmw: load`, `intent: write`, the store `rmw: store` (derived at emission: a load followed by a plain store in one guest instruction; valid because admitted instructions have at most one explicit memory operand) |
| `segment` | `ss` for stack ops and RSP/RBP-based operands without an FS/GS override, else `ds` (decides #SS(0) vs #GP(0)) |
| `base_reg`, `index_reg`, `extend`, `shift`, `imm` | native address form: base register number (31 = sp), optional index register with `uxtw`/`sxtw`/`sxtx` and shift, constant offset |
| `data_regs`, `data_class` | transfer register(s) (31 = xzr), `gpr`/`fpr` |
| `writeback`, `writeback_delta` | `pre`/`post` base update performed by the access itself (push: pre, -size; pop: post, +size) |
| `ordering` | `plain`, `acquire` (ldar family, TSO on), `release` |
| `nzcv`, `cf_inverted`, `flags_written_before_site` | guest NZCV is in PSTATE (`frame` is for the precision patches); CF inversion at the guest instruction start; an NZCV-writing IR op of this instruction precedes the site (approximate: only ops that write NZCV by contract, not inline compares) |
| `completed_effects` | `unverified`: nothing yet guarantees that no effect of the instruction is committed before the site |
| `located` | the faulting word was found in the op's range (false would be a recorder bug) |
| `x86` | FEX's decode of the explicit memory operand: `rip_relative`, `base`, `index` (x86 register numbers, -1 none), `scale`, `disp`; null for stack ops |

Example (`mov eax, [rsi]`, TSO on):

```json
{"native_offset":12,"guest_offset":0,"guest_offset2":null,"insn_native_offset":12,"ir_op":"LoadMemTSO","kind":"load","width":4,
 "intent":"read","rmw":"none","segment":"ds","base_reg":10,"index_reg":null,"extend":"none","shift":0,"imm":0,"data_regs":[4],
 "data_class":"gpr","writeback":"none","writeback_delta":0,"ordering":"acquire","nzcv":"pstate","cf_inverted":true,
 "flags_written_before_site":false,"completed_effects":"unverified","located":true,
 "x86":{"rip_relative":false,"base":6,"index":-1,"scale":1,"disp":0}}
```

Known deviations of FEX from x86 in the default profile, fixed under `--profile slice1` by the precision patches (S1b1):
`cmp`/`test`/`bt` with a memory operand whose flags are overwritten later lose the access entirely (flag elimination,
then dead-code elimination; `check_sites.py` reports ELIMINATED without `--slice1`). `push`/`pop` of adjacent guest
instructions are fused. Load and pop destinations can be static registers.

Accepted natemu policy: `bt`/`bts`/`btr`/`btc` with a memory operand access ONE byte (the byte holding the bit), for an
immediate and for a register bit index alike, not the x86 operand width. The fault, CR2 and A/D bits are those of that byte.
`check_sites.py` reports these as NARROWED and does not fail them.

The site's `imm`, `writeback` and `writeback_delta` (address = base register value before the access + `imm`; pre-index: `imm`
equals `writeback_delta`, post-index: `imm` is 0) are read from the emitted instruction word (FEX's `Push` with
`NeedsMoveAfterwards` emits `stur [base,#-n]` and updates the base with a later `sub`, which is no writeback of the access).

`--object FILE` (only when the status is `ok`) writes the binary form, version 4 (v2 plus `pre_nzcv_register` at byte 124, the register of the pre-NZCV save, 25 under slice1 and `NONE32` otherwise; version 3 had the frame offset 1032 there; relocation kind 4 is a `dispatch_branch`): a 128-byte header, the code (relocation
slots zeroed), then site, PC-entry and relocation tables, layout in `crates/abi/src/code_object.rs`. Besides what the JSON
carries per site (including `located`), the header records the profile, the `settings` bits (TSO, multiblock, dense PC, end
marker), the end reason code (the `guest.end.reason` string), the feature profile (`feature_bits`: bit i is the i-th member
of `features.host_features`, plus count and DCacheLineLog2). `check_sites.py` and `test_fexc.py` assert object and JSON agree.
Numeric codes are shared with `shim/fexc_hook.h`.

## Precision (S1b1)

Under `--profile slice1` only (the default profile's output is unchanged: identical JSON on the 64 fixtures, a 264-program
generated corpus and the 613-form matrix, except the host address of FEX's exit-linker literal), patch 0006 applies the
[contract](../../docs/restructure/slice1-contract.md) §1 patches 2-5, with predicates in `shim/fexc_precision.h`:

| patch | FEX file::function | change |
|---|---|---|
| 2 | `RedundantFlagCalculationElimination.cpp::Classify` | every guest-memory op (LoadMem, StoreMem, their TSO and pair forms, Push, Pop) and, since S1f, every divide op (UDiv, Div: `FexcPrecision::FaultPoint`) reads all six flags, so no flag write before it is deleted or narrowed; `InvalidateFlags` stays a write (its value is undefined, so an earlier write of that flag may die, e.g. add's AF across a later `or`), and the `GuestOpcode` of an instruction with an access reads all six flags, so the incoming flags stay live at the instruction start even if the instruction itself invalidates some; the guarantee is unchanged, the cost drops |
| 2b | `...::EliminateDeadCode`, `RegisterAllocationPass.cpp::Run` (forward) | a guest load with a dead value (`cmp`/`test`/`bt [m]`) is kept; its destination register is freed at once |
| 3 | `RegisterAllocationPass.cpp::Run` (backwards) | a guest-memory op and a guest instruction boundary (`GuestOpcode`) count as an access of every static register, so a value is coalesced into its SRA register only if no access (and no instruction boundary) lies between its definition and its store; this covers constants pooled across instructions. A load's own value never gets an SRA register (DDI0487 D1.4.1.4); a pop's writeback stack pointer stays in RSP's register (the access is its only write) |
| 4 | `RegisterAllocationPass.cpp::Run` (forward) | `TryPostRAMerge`'s candidate is reset at `GuestOpcode`: no push/pop pair, no merge across instructions |
| 5 | `RedundantFlagCalculationElimination.cpp::Run` | an instruction with an NZCV writer (FEX's flag table or `ImplicitFlagClobber`) before one of its accesses is recorded in `fexc_pre_nzcv`; the JIT emits `mrs x25, nzcv` right after its `GuestOpcode` (patch 0011; before it, an IR `LoadNZCV; StoreContext` to the frame slot 1032); its sites say `nzcv=saved`. x25 is FEX's call-return stack pointer, unused under slice1, never allocated; reported as `state_abi.state_offsets.natemu_pre_nzcv_register` |

The patch-3 boundary barrier is stricter than the contract (which only names accesses): it keeps every static-register
write inside its own instruction, so each PC-table entry has the exact state and flag writes can be checked per instruction.

Per site (slice1): `nzcv` is `frame` when the instruction saved it, `cf_inverted` is the CF inversion at the instruction
start (valid for PSTATE and the frame copy, since nothing writes NZCV before the site otherwise), and `completed_effects`
is `none`, or `unverified` if the recorder sees an earlier store site or an unsaved NZCV write in the same instruction
(never seen). `flags_written_before_site` also counts `ImplicitFlagClobber` ops under slice1.

`verify_precision.py [--before FEXC0] [CORPUS.json ...]` checks the guarantee on the emitted code (docstring: P1-P7):
between a site's instruction start and the site no static-register (GPR, x26, x27, xmm) write, no store, no frame write
but the pre-NZCV save, no NZCV write unless `nzcv=saved` with the save first; load destinations not static registers; at
most one store site per instruction and last; no fused pair; `completed_effects=none`; no skippable site; for annotated
programs every flag a later site observes has a native write in its defining instruction (flags.py rules; coarse: any NZCV
writer satisfies CF/ZF/SF/OF, any x26/x27 writer PF/AF); load destinations are read from the disassembly at the site; on
every compile (matrix too) each native non-frame load/store is a site and the reverse; plus the rest of `check_sites` for
programs with `asm` (no eliminated access). By construction and unchecked: an instruction's static-register writes land
before the next instruction's start. Corpora: fixtures, the scan_skips matrix, adversarial
programs per patch, and any `gen_slice1.py` corpus given. Against `s1c` it reports every defect class (SRA writes from
pooled constants and RMW flag results, unsaved NZCV writes, load destinations in SRA registers, fused pairs, eliminated
loads and flag writes); `series2` passed P1-P7 and fails only the guard checks P8-P10 added with S1b2 (missing guards,
split checks and branch-target checks; 666 of the 911 compiled programs including the seed-77 corpus); `series4` passes all.

## Guards (S1b2; address guard removed in E3)

**E3 (series13): there is no address guard.** Translated code runs at EL0, where every x86-non-canonical address is
unmapped (both holes and everything outside the two TTBR ranges: translation faults), EL1-only or EL0 execute-only (the
resident's private window: permission faults). A guest access there faults in hardware at its own site; the runtime
(`crates/runtime/src/execution.rs` `aborted`) classifies the abort by site first and raises #GP(0), or #SS(0) for a
stack-segment site, from the site's recorded address (`pages()`: unless both the first and the last byte are canonical).
Kept, as x86 semantics: the split check (Arm may commit part of a page-crossing store), the branch-target check (x86
raises #GP at the branch, before a call's push, a ret's pop or any RIP change), RIP-relative `b S` for a page-crossing
store (canonical or not), the #DE checks, the precision patches and the site map. The rest of this section describes
series4-12; the guard rows no longer apply.

Under `--profile slice1` only (default-profile JSON unchanged on the 64 fixtures, the 613-form matrix and a 264-program
`--seed 77` corpus, except the exit-linker host address), patch 0007 adds three flag-free checks ([contract](../../docs/restructure/slice1-contract.md)
§1 patches 7 and 8; code in `shim/fexc_sites.inc`, the pre-RA scan in `shim/fexc_precision.h`). Each branches with `tbnz` to
an out-of-line `brk` stub placed after the block body (after the last exit `br`); the stub's PC is a site of kind `guard`
(`brk #1`) or `split` (`brk #2`), found by the runtime through ELR like any site. The checks use only x0/x1 (FEX's TMP1/TMP2,
never allocated) and sit right before the access (or right after the branch target is defined), so the state at a stub is
the state of its access site: before the guest instruction, nothing committed. No helper call, no new IR op.

| check | code | when |
|---|---|---|
| address guard | `[add/sub x1, base, index\|imm;] eor x0, A, A, lsl #16; tbnz x0, #63, G` | every non-RIP-relative access (A = base or x1); the store of a read-modify-write whose address is its load's SSA value shares the load's guard (it keeps its split check) |
| split check | `add x0, A, #w-1; eor x0, x0, A; tbnz x0, #12, S` | every store wider than 1 byte (`store`, `store_pair`, `push`, read-modify-write stores too: with both pages already shadowed readable only the store faults, on the second page, and Arm may commit the first part) |
| branch target | `eor x0, T, T, lsl #16; tbnz x0, #63, G` | indirect `jmp`/`call` (register or memory) and `ret`/`ret imm16`, before any state write |
| RIP-relative | `b G` / `b S` or nothing | the address is a compile-time constant: guard only if non-canonical, split only if a store crosses a page |

The RIP-relative elision is sound only because admission requires every guest-RIP relocation to equal the value for the
compile RIP; any future relocation of code to another guest RIP (a code cache reusing objects) must re-check those addresses.
| direct branch | cut at decode | `jmp`/`call`/`jcc` rel with a non-canonical target ends the block before it, `unsupported_form` |

The guard catches A[63] != A[47]: the private half (`PRIVATE` = `0x0000_8000_4000_0000`), the rest of TTBR0's upper half and
TTBR1's lower half. Any other non-canonical A is in neither TTBR range (T0SZ = T1SZ = 16, TBI off) and takes a level-0
translation fault at the access itself. The last byte is not guarded: an access with a canonical A whose end crosses
`0x0000_8000_0000_0000` lands in the unmapped 1 GiB below `PRIVATE` (L1 entry 0 under TTBR0 L0 entry 256 is invalid in the
host's tables) and faults there; a store crossing it is a page-crossing store, caught by the split check. An access
wrapping past `2^64` (A within 15 bytes of the top) reaches guest page 0, never private memory (open: the ABI predicate asks
for checked handling; x86 wraps there too).

`push` (slice1 only) never writes a register between its checks and its store: when the new stack pointer goes to another
register, or the pushed value is the base itself (`push rsp`, `call rsp`), it stores with `stur value, [base, #-n]` and sets
the new stack pointer with a `sub` afterwards (FEX's own path for the value-is-destination case), instead of `mov` before a
pre-indexed store or a TMP copy. A far-branch restart (blocks over 32 KiB) now discards the attempt's PC-table and sub-block
entries; the checks then take FEX's far form `tbz x0, #bit, +8; b stub`, which the verifier accepts. A branch-target check
that Prescan planned but the JIT did not emit kills the compile. Site `data_regs` are the registers the access word encodes
(check_sites compares them with the disassembly).

`ret` (slice1 only) loads its return address with a plain load (site kind `load`, segment `ss`) instead of FEX's
post-index pop, checks it, then adds 8 (+ imm16) to RSP; `call` checks before its push. A target read from a static register
(`jmp`/`call reg`) is checked in that register at the instruction start.

Stub sites (JSON): `access_native_offset` (slice1 only) names the access site a guard or split covers (null for a branch-target
guard, whose `base_reg` is the target register); `base_reg` + `imm` is the address as it is at the stub (x1, the base, or the
RIP-relative base register), no index; width, intent, segment and data registers are the access's. In the binary object
(version 3, unchanged layout) a stub carries that access offset in `guest_offset2`. A split stub's completion: write both
parts (or neither, #PF), apply the access site's writeback to the access site's `base_reg`, resume after the access site;
the words between the split check and the store only build the address or copy the base, which the completion subsumes
(checked).

Cost (`verify_precision.py --before series2`, native words per translated guest instruction, stubs included): fixtures
3.84 -> 5.67, `gen_slice1.py --seed 77 --random 240` 4.06 -> 6.02 (code bytes 17.3 -> 25.1). Of the 1.96 added words on the
seed-77 corpus: guards 0.92, split checks 0.50, branch targets 0.00 (2 per indirect branch; the corpus has none), stubs 0.53
(never executed). Run time is not measured (fexc cannot execute its output; S1d's runtime can).

## Division (S1f)

Under `--profile slice1` only (default-profile JSON unchanged on the 64 fixtures, the `--seed 77` corpus and the 629-form
matrix, 957 compiles), div/idiv at all widths, register and memory divisors, are admitted. FEX raises no #DE (Arm `udiv`/`sdiv`
return 0 for a zero divisor and wrap on overflow), so `FexcBeforeOp` emits a check right before each `UDiv`/`Div` IR op, the
first op of the instruction that writes anything but temporaries (a memory divisor is already loaded, so its #PF comes
first, as on x86). The check is flag-free, uses x0/x1 only, reads FEX's own operand registers of the op at the x86 width the
dispatcher recorded (`fexc_div_width`; FEX widens the 8-bit forms), and branches to an out-of-line `brk #3` stub, a site of
kind `divide` (no access, `base_reg` 255, width 0; one stub per branch). The state at the stub is the state before the
instruction; `verify_precision.py` P1/P2/P11 check it on the code.

| form | check (native words) |
|---|---|
| div 8/16/32 | high >= divisor, unsigned (a zero divisor included): `{ubfx x0, AX, #8, #8 \| uxth w0, wDX \| mov w0, wEDX}; sub x0, x0, wD, uxt{b,h,w}; tbz x0, #63` (3) |
| div 64 | `cbz xD` (1); a nonzero RDX takes FEX's LUDIV helper call, after which `cbnz x2` (1) takes the helper's #DE verdict |
| idiv 8/16/32 | `sxt x1, wD; cbz x1`; dividend in 32 (8, 16) or 64 bits (32); `sdiv x0, x0, x1; sub x0, x0, w0, sxt{b,h,w}; cbnz x0` (6, 7, 7: the quotient does not fit W bits, -2^63 / -1 included) |
| idiv 64 | `cbz xD` + RDX:RAX = -2^63 with divisor -1 (FEX's inline path) in 6 words; a 128-bit dividend takes LDIV, then `cbnz x2` (1) |

FEX's 128/64 call is `mov x0..x2 <- RDX, RAX, divisor` then (patch 0011) `svc #3` (div) or `svc #4` (idiv) instead of its
`ldr x3, [x28, #Pointers.LUDIVHandler|LDIVHandler]; str x30, [sp, #-16]!; blr x3; ldr x30, [sp], #16`. The resident divides
(runtime `division`, abi `execution::divide`): FEX's convention (x0 = high, x1 = low, x2 = divisor in; x0 = quotient, x1 =
remainder out), plus x2 = 1 for #DE; only x0-x2 change, NZCV and all other registers preserved, no guest memory, no native
stack. A divide op is a fault point for flag elimination (`FexcPrecision::FaultPoint`): the flags an earlier
instruction defines stay computed before a div even when a later instruction overwrites them (found by P6 on fixture
random-38: an `add`'s flags were eliminated before an `idiv`).

Cost per guest instruction (native words on the executed path, plus never-executed stubs): FEX's own divide is 6-7 words at
8/16/32 bits; the check adds 3 (div) or 6-7 (idiv, including a second `sdiv`), and 1 or 2 stub words. 64-bit: +1 (div) or +7
(idiv) on FEX's inline path, +1 after the helper call; the helper itself is about 600 instructions (one quotient bit per
step) on the rare path whose dividend needs 128 bits.

## Self-loops and the stop check (S2c0)

Under `--profile slice1` only (default-profile JSON and objects identical to series6 on the 64 fixtures, the 629-form matrix and
six self-loop programs, `emitted_value` aside), patch 0009:

- **Self-loop exits.** `OpcodeDispatcher.cpp::CondJUMPOp`, `CondJUMPRCXOp` and `LoopOp` look the taken target up in `JumpTargets`
  even with multiblock off; the one entry there is the block's own entry, so `1: add rax,1; dec rcx; jnz 1b` became a native
  backward `b.ne` into the block (past the header store, skipping the prologue) with no exit for the taken edge. Under slice1 the
  lookup is skipped (`fexc_slice1 ? JumpTargets.end() : find`), so the taken edge is an ordinary exit: `State.rip` literal and the
  gate, to the block's own RIP. Every branch in a block is now forward (P13), and a conditional block has two exits. `loopne/loope/loop/
  jrcxz` rel8 (no 67 prefix; with it they are `unsupported_form`) are allowlisted since S2d (series9; `Allow(true)` in
  `shim/fexc_slice1.inc`, admission's `branch_target` decodes them): they compile to `cbz/cbnz` forward to a taken exit plus a
  fall-through exit (`loop`: `sub x7,x7,#1; cbnz x7,T; b F`; loope/loopne select rcx = 0 on the wrong ZF first), two exits like a
  jcc, checked by `test_slice1.py` (exit forms, all four as self-loops, P12/P13) and `verify_precision.py`.
- **Stop check.** `JIT.cpp::EmitSuspendInterruptCheck` emits FEX's `str xzr, [x28, #InterruptFaultPage]` only when
  `Config.NeedsPendingInterruptFaultCheck` (gdbserver). Under slice1 it is always emitted, once, as the last word of
  `EmitEntryPoint` (after `adr x0, header; str x0, [x28]`, no TF check and no spill slots here); the second use, before a backward
  branch, never arises. Prologue before/after for `add rax,1; dec rcx; jnz 1b`:

  ```
  before: 4 adr x0,hdr  8 str x0,[x28]                              c adds x4,x4,#1 ... 24 b.ne 0xc  (backward)  28 exit gate
  after:  4 adr x0,hdr  8 str x0,[x28]  c str xzr,[x28,#0xec0]     10 adds x4,x4,#1 ... 28 b.ne 0x44 (forward)  2c exit gate  44 exit gate
  ```

  The field is `offsetof(InternalThreadState, InterruptFaultPage) - offsetof(.., BaseFrameState)` = 3776 = `0xec0`, JSON
  `state_abi.state_offsets.natemu_interrupt_fault_page` (slice1 only). FEX places that page directly after `CpuStateFrame`
  (4 KiB aligned); we only need the 8 bytes at `x28 + 3776` to lie in a region of their own.
- **What the runtime must provide** (before patch 0011, which replaces the store with `bl .` to the resident's stop pad: a
  kick takes execute permission from the pad's stage-2 granule). (1) `x28 + 3776` (8 bytes) is mapped writable in the resident's stage-1 tables and its IPA
  lies in a region that holds nothing else the vCPU or host writes, whose bounds are a multiple of the stage-2 granule (HVF
  protects at host-page granularity, 16 KiB on Apple silicon; `hv_vm_protect` rejects anything else). Simplest layout: the
  frame ends at a 16 KiB boundary S and the stop region is [S, S + 16 KiB), so x28 = S - 3776. The abi's 4096-byte frame
  would then overlap the region's first 320 bytes, which no admitted field uses (highest is the division helper at 3032). (2) Armed,
  the host `hv_vm_protect`s the region read-only; the next block entry's store takes a stage-2 permission data abort to the host
  (EC 0x24, WnR = 1, HPFAR in the region, PC = the store at native offset 12 of a block) with the vCPU exactly at the block-entry
  state: guest registers in the static map and NZCV in PSTATE, `InlineJITBlockHeader` already naming the block (word 8 ran,
  word 12 did not), whose record gives the guest RIP. (State.rip is stale after a linked edge, which skips the exit's store,
  as in FEX: the header, not State.rip, names the block.) Nothing of the block's body ran.
  (3) The host banks that state itself (or resumes the resident at a banking routine), and to continue restores RW and
  re-enters, which retries the store. `hv_vcpus_exit` stays an imprecise watchdog. (4) Built (slice 3, S3b2): x28 = the
  frame page + 0x400 (abi `execution::FRAME_POINTER`), so every admitted field stays in the frame page and the stop store
  lands alone in the next page, mapped to the vCPU's own stop granule (abi `STOP_OFFSET`, 16 KiB of the window holding
  nothing else); the host enters the resident's stop gate, which banks the entry state and drains (host `execution::Stops`,
  runtime `stopped`). No FEX change: every frame offset is x28-relative.
- **Admission** (`structure.rs`, rule "Entry"): the word at offset 12 is exactly `0xf907639f` (`str xzr, [x28, #3776]`) preceded
  by `str x0, [x28]`; any access (load, store, narrower, other register) overlapping those 8 bytes anywhere else is refused. The
  rule has its own constant (3776) and is not a member of `frame::FIELDS`; see the S2c integration note in the file.
- **Checks.** `verify_precision.py` P12/P13 on every compile; `test_slice1.py::Slice1SelfLoopsAndStop` (all 16 condition codes,
  rel8 and rel32, the stop word in every form, mutations of P12/P13); the admission unit tests mutate the stop store (removed,
  next/previous field, wrong register, 32-bit, moved, repeated, loaded).

## Literal exits through the linker (S2c)

Under `--profile slice1` only (default-profile JSON and objects identical to series7 on the 64 fixtures, `emitted_value`
aside), patch 0010 makes `BranchOps.cpp::ExitFunction` load `Pointers.ExitFunctionLinker` (offset 2952, JSON
`state_abi.state_offsets.exit_function_linker`) for an exit to an inline-constant RIP and `Pointers.DispatcherLoopTop` (2920)
for a computed one, as FEX's own linking path does. A literal exit is then exactly `ldr x0, L; str x0, [x28, #0x18]; ldr x1,
[x28, #0xb88]; br x1`. Admission (`structure.rs`, "Exits") accepts it only in that form, with L the block end or the decoded
target of the block's branch and no branch into its words 2-4, and returns it as a link point; the resident's linker
(`crates/runtime/src/execution.rs` `linker`) rewrites the first word into `b` to the target block's entry. Same code size and
layout as series7: only that one load word per literal exit differs.

## Checks

- `python3 -m unittest tools/fexc/test_fexc.py`: structure, exact sites for hand-checked instructions, object file,
  identity of the 53 pinned old-producer bodies.
- `python3 -m unittest tools/fexc/test_slice1.py`: profile slice1. Every system class and unsupported form placed after two
  allowed instructions ends the block exactly at its RIP with the right reason and the same code as the two instructions alone;
  as the first instruction it gives no code; ~100 allowed forms and 11 branch forms translate; every exit in the branch cases
  and the 64 programs is one of the two gates (disassembled: a literal exit loads `ExitFunctionLinker`, a computed one
  `DispatcherLoopTop`), with no named-symbol literal, no other pointer load but the division helpers (used only by FEX's
  helper call), no x25; the 64 programs translate completely.
- `python3 tools/fexc/scan_skips.py`: the native-control-flow scan above.
- `python3 tools/fexc/verify_precision.py [--before FEXC0] [CORPUS.json ...]`: the slice1 precision guarantee (above) and the
  guards (P8-P10: guard on exactly the access address, split check on every store wider than 1 byte, stubs reached only by their checks,
  indirect targets checked before any state write; series2 fails them, series4 passes) and P11 (every div/idiv has exactly
  its per-width #DE checks in their exact form, on its divide's own operands, before its divide; every `blr` is the 128/64
  helper call followed by its #DE branch), P12 (entry prologue and the stop store pinned, no other access to it) and P13 (every branch forward). Since E3 (series13) P8
  requires that no access has an address guard. Admission (`crates/host/src/admission/structure.rs`) re-checks P8-P11 on
  the native words of every block it admits when its code-shape debug check is on (the program driver's `--code-shape`, the test
  suites); its object validation runs always.
- `python3 tools/fexc/check_sites.py [--known-ok] [--slice1]`: the 64 reference programs; expected accesses from the fixture `asm`
  against the sites, sites against the `xcrun objdump` disassembly of the code (every load/store with a base other than
  x28/sp must be a site and the reverse), dense versus sparse code bytes, binary object versus JSON.
