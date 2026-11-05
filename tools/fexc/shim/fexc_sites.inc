// natemu fexc: guest-memory access-site recording. Included by patches/0004 into FEXCore's JIT/MemoryOps.cpp,
// i.e. inside namespace FEXCore::CPU, so it can use the Arm64JITCore members that emit the ops.
//
// Why here: after RA every IR op has its final physical registers and its emitted native range
// [Begin, End), and the GuestOpcode op (emitted per guest instruction when the PC table is dense, and
// always for an instruction with a memory operand) says which guest instruction is being translated.
// FEX emits IR ops in order and no later pass moves an access across a GuestOpcode, so the "current
// GuestOpcode" is the owning guest instruction. The one exception is a fused push/pop pair (TryPostRAMerge
// skips GuestOpcode), which is recorded as such and carries both guest offsets.
// (fexc_hook.h and <cstring> are included at the top of MemoryOps.cpp by patches/0004.)
#include <fexc_precision.h>

namespace {
// Load/store class of the A64 encoding, excluding PC-relative literal loads (patch-free: one fixed rule).
bool FexcIsMemWord(uint32_t w) {
  return ((w >> 25) & 5) == 4 && (w & 0x3B000000u) != 0x18000000u;
}
// Address form of an A64 load/store word: constant offset and base update, read from the emitted instruction because
// the IR does not know which handler path produced it (Push's NeedsMoveAfterwards path is `stur [base, #-n]` with no
// writeback; the base is updated by a later `sub`). `imm_form` is false for register-offset forms (index from the IR).
struct FexcAddr { bool imm_form; int32_t imm; uint8_t wb; int8_t delta; };
FexcAddr FexcDecodeAddr(uint32_t w) {
  auto sext = [](uint32_t v, int bits) { return int32_t(v << (32 - bits)) >> (32 - bits); };
  const uint32_t kind = (w >> 27) & 7, v = (w >> 26) & 1;
  FexcAddr a {false, 0, 0, 0};
  if (kind == 5) {                                              // ldp/stp/ldpsw/ldnp/stnp
    const uint32_t opc = w >> 30, scale = v ? 2 + opc : (opc == 2 ? 3 : 2), type = (w >> 23) & 3;
    const int32_t imm = sext((w >> 15) & 0x7F, 7) * (1 << scale);
    a.imm_form = true;
    if (type == 3) { a.imm = imm; a.wb = 1; a.delta = int8_t(imm); }
    else if (type == 1) { a.wb = 2; a.delta = int8_t(imm); }
    else a.imm = imm;
  } else if (kind == 7) {                                       // ldr/str/ldur/stur and sized/signed variants
    const uint32_t size = w >> 30, scale = (v && ((w >> 23) & 1) && size == 0) ? 4 : size;
    if ((w >> 24) & 1) { a.imm_form = true; a.imm = int32_t(((w >> 10) & 0xFFF) << scale); }
    else if (!((w >> 21) & 1)) {
      const int32_t imm = sext((w >> 12) & 0x1FF, 9), type = (w >> 10) & 3;
      a.imm_form = true;
      if (type == 3) { a.imm = imm; a.wb = 1; a.delta = int8_t(imm); }
      else if (type == 1) { a.wb = 2; a.delta = int8_t(imm); }
      else a.imm = imm;                                         // ldur/stur (00) and ldtr/sttr (10)
    }
  }
  return a;
}
bool FexcIsNzcvWriter(std::string_view n) {
  // IR ops that write PSTATE NZCV by contract (IR.json). Ops that clobber it only inside their expansion
  // (e.g. a Select with an inline compare) are not listed; the precision verifier (S1b) must cover those.
  static constexpr std::string_view names[] = {"AddWithFlags", "AddNZCV", "CarryInvert", "AXFlag", "RmifNZCV", "CondAddNZCV",
    "CondSubNZCV", "AdcWithFlags", "AdcZeroWithFlags", "SbbWithFlags", "AdcNZCV", "SbbNZCV", "SubWithFlags", "SubNZCV", "TestNZ",
    "TestZ", "AndWithFlags", "ShiftFlags", "RotateFlags", "FCmp", "StoreNZCV"};
  for (auto s : names) if (s == n) return true;
  return false;
}
bool FexcIsOtherMemoryOp(std::string_view n) {
  // Guest-memory IR ops with no site description; the producer must refuse them (contract: unsupported_form).
  static constexpr std::string_view names[] = {"VLoadVectorMasked", "VStoreVectorMasked", "VLoadVectorGatherMasked",
    "VLoadVectorGatherMaskedQPS", "VLoadVectorElement", "VStoreVectorElement", "VBroadcastFromMem", "StoreMemX87SVEOptPredicate",
    "LoadMemX87SVEOptPredicate", "MemSet", "MemCpy", "VStoreNonTemporal", "VStoreNonTemporalPair", "VLoadNonTemporal",
    "AtomicFetchNeg", "CacheLineClear", "CacheLineClean", "CacheLineZero"};   // AtomicFetchNeg: a CAS loop (slice1: patch 0016)
  for (auto s : names) if (s == n) return true;
  return false;
}
} // namespace

// I5a: RCPPS/RCPSS/RSQRTPS/RSQRTSS raise no SIMD exception on x86; FEX's exact division would (ZE, PE, IE): FPSR around it.
bool Arm64JITCore::FexcQuietOp(const IR::IROp_Header* IROp) {
  using IR::IROps;
  return fexc_slice1 && fexc_fp_quiet.count(Entry + FexcGuest) &&
         (IROp->Op == IROps::OP_VFRECP || IROp->Op == IROps::OP_VFRSQRT || IROp->Op == IROps::OP_VFRECPSCALARINSERT ||
          IROp->Op == IROps::OP_VFRSQRTSCALARINSERT);
}
// I5a: ROUND* with imm8 bit 3 clear (PE when inexact): `frintx`, under the instruction's own rounding mode when it names one
// (FPCR.RMode switched around it: x2 keeps FPCR, x3 the temporary value). Returns whether to emit frintx.
bool Arm64JITCore::FexcRoundBegin(IR::RoundMode Mode) {
  if (!fexc_slice1 || !fexc_round_inexact.count(Entry + FexcGuest)) return false;
  // x2/x3 hold the x87 wrapper's saved FPCR/FPSR around an x87 instruction's op (FexcX87Begin): x87 rounding ops (FRNDINT,
  // FISTP m64) use the host mode only, which switches nothing here.
  if (Mode != IR::RoundMode::Host && fexc_x87.count(Entry + FexcGuest)) std::abort();   // a hard check (LOGMAN asserts compile out)
  if (Mode != IR::RoundMode::Host) {
    const uint64_t RMode = Mode == IR::RoundMode::Nearest ? 0 : Mode == IR::RoundMode::PosInfinity ? 1 : Mode == IR::RoundMode::NegInfinity ? 2 : 3;
    mrs(TMP3, ARMEmitter::SystemRegister::FPCR);
    and_(ARMEmitter::Size::i64Bit, TMP4, TMP3, ~(uint64_t(3) << 22));
    if (RMode) orr(ARMEmitter::Size::i64Bit, TMP4, TMP4, RMode << 22);
    msr(ARMEmitter::SystemRegister::FPCR, TMP4);
  }
  return true;
}
void Arm64JITCore::FexcRoundEnd(IR::RoundMode Mode) {
  if (Mode != IR::RoundMode::Host) msr(ARMEmitter::SystemRegister::FPCR, TMP3);
}

// I6: the floating-point ops of an admitted x87 instruction (reduced precision: scalar doubles). Each runs with FPSR cleared
// (x3 keeps MXCSR's flags) and, when the host gave one, under the x87 FPCR (x2 keeps MXCSR's); after it, its FPSR flags go into
// the x87 exception word (x0, x1 are free between ops). None of these ops uses x0..x3 itself (FEX's TMP1..TMP4).
bool Arm64JITCore::FexcX87Op(const IR::IROp_Header* IROp) {
  using IR::IROps;
  if (!fexc_slice1 || !fexc_x87.count(Entry + FexcGuest)) return false;
  switch (IROp->Op) {
  case IROps::OP_VFADD: case IROps::OP_VFSUB: case IROps::OP_VFMUL: case IROps::OP_VFDIV: case IROps::OP_VFSQRT:
  case IROps::OP_FLOAT_FTOF: case IROps::OP_FLOAT_FROMGPR_S: case IROps::OP_FLOAT_TOGPR_S: case IROps::OP_FLOAT_TOGPR_ZS:
  case IROps::OP_VECTOR_FTOZS: case IROps::OP_VECTOR_FTOS: case IROps::OP_VECTOR_FTOI: case IROps::OP_FCMP: return true;
  default: return false;
  }
}
void Arm64JITCore::FexcX87Begin() {
  mrs(TMP4, ARMEmitter::SystemRegister::FPSR);
  msr(ARMEmitter::SystemRegister::FPSR, ARMEmitter::XReg::zr);
  if (fexc_x87_fpcr) {
    mrs(TMP3, ARMEmitter::SystemRegister::FPCR);
    LoadConstant(ARMEmitter::Size::i64Bit, TMP1, fexc_x87_fpcr);
    msr(ARMEmitter::SystemRegister::FPCR, TMP1);
  }
}
// I6b: an x87 transcendental, remainder, scale or extract instruction (patch 0023's FexcX87Svc), run whole by the resident on the
// x87 state in the guest-state page: `movz w0, #fn; svc #X87`, fn the D9 opcode's second byte. No register changes (x0 is FEX's
// TMP1); the resident leaves NZCV and every register as they were, and admission accepts the svc only right after that movz in
// that very instruction.
DEF_OP(FexcX87Svc) {
  const auto Op = IROp->C<IR::IROp_FexcX87Svc>();
  movz(ARMEmitter::Size::i32Bit, TMP1, Op->Function);
  svc(FEXC_SVC_X87);
}
// I6b (I5c): PCMPxSTRx's intermediate result (FEX's VPCMPISTRX/VPCMPESTRX, a host helper otherwise) by the resident: v0 = xmm1,
// v1 = xmm2/m128 (FEX's VTMP1/VTMP2), x1 = RAX, x2 = RDX for the explicit forms, then `movz w0, #control; svc #STRING` with
// control = imm8 | REX.W << 8 (FEX's own bit 8) | explicit << 9; w0 out (IntRes2 and the flags in bits 31:28, as FEX's helper).
// The resident changes only x0; temporaries x0-x2 and v0/v1 hold no allocated value.
void Arm64JITCore::FexcStringSvc(const IR::IROp_Header* IROp, IR::Ref Node) {
  uint32_t Control;
  if (IROp->Op == IR::OP_VPCMPESTRX) {
    const auto Op = IROp->C<IR::IROp_VPCMPESTRX>();
    mov(VTMP1.Q(), GetVReg(Op->LHS).Q());
    mov(VTMP2.Q(), GetVReg(Op->RHS).Q());
    mov(TMP2, GetReg(Op->RAX).X());
    mov(TMP3, GetReg(Op->RDX).X());
    Control = uint32_t(Op->Control) | 1u << 9;
  } else {
    const auto Op = IROp->C<IR::IROp_VPCMPISTRX>();
    mov(VTMP1.Q(), GetVReg(Op->LHS).Q());
    mov(VTMP2.Q(), GetVReg(Op->RHS).Q());
    Control = Op->Control;
  }
  movz(ARMEmitter::Size::i32Bit, TMP1, Control);
  svc(FEXC_SVC_STRING);
  mov(GetReg(Node).W(), TMP1.W());
}

void Arm64JITCore::FexcX87End() {
  if (fexc_x87_fpcr) msr(ARMEmitter::SystemRegister::FPCR, TMP3);
  mrs(TMP1, ARMEmitter::SystemRegister::FPSR);
  msr(ARMEmitter::SystemRegister::FPSR, TMP4);
  if (fexc_x87_unmasked) {
    // I5c: under unmasked x87 exceptions the instruction's flags gather in FEXC_X87_PENDING, which its check reads and the x87
    // stack pass folds into the exception word at its end (so a fault or check stub inside it leaves FSW unchanged).
    ldrb(TMP2.W(), STATE, FEXC_X87_PENDING);
    orr(ARMEmitter::Size::i32Bit, TMP2, TMP2, TMP1);
    strb(TMP2.W(), STATE, FEXC_X87_PENDING);
    return;
  }
  ldr(TMP2.W(), STATE, FEXC_X87_EXCEPTIONS);
  orr(ARMEmitter::Size::i32Bit, TMP2, TMP2, TMP1);
  str(TMP2.W(), STATE, FEXC_X87_EXCEPTIONS);
}

// ---------------------------------------------------------------------------------------------
// I5c: unmasked floating-point exceptions (FexcPrecision::PlaceFPChecks places the check ops). Each check is a fault point:
// at its stub the guest state is the state before the instruction (no static register written, the incoming NZCV in PSTATE
// or x25), so the resident delivers or completes from the bank exactly as at an access site. Only x0/x1 are written.
//   SIMD   mrs x0, fpsr; movz w1, #M; and w1, w0, w1; cbnz w1, S      (M: the FPSR bits of the unmasked MXCSR exceptions)
//          ldrb w1, [x28, #simd_flags]; orr w1, w1, w0; strb w1, [x28, #simd_flags]; msr fpsr, xzr
//   x87    ldrb w0, [x28, #x87_pending]; sub w1, wF, #1; and w0, w0, w1 (a stack fault drops the operation's flags: it has
//          precedence, SDM Vol.1 4.9.2); orr w0, w0, wF, lsl #8; orr w0, w0, wO, lsl #9; orr w0, w0, wI, lsl #10;
//          orr w0, w0, wT, lsl #11; movz w1, #M; and w1, w0, w1; cbnz w1, X
//   #MF    (at a waiting instruction's start) ldrb w0, [x28, #ie]; ldrb w1, [x28, #pe]; orr w0, w0, w1, lsl #4;
//          ldr w1, [x28, #exceptions]; orr w0, w0, w1; movz w1, #M; and w0, w0, w1; cbnz w0, P
// ---------------------------------------------------------------------------------------------
void Arm64JITCore::FexcSimdCheckWords(uint32_t Mask) {
  mrs(TMP1, ARMEmitter::SystemRegister::FPSR);
  movz(ARMEmitter::Size::i32Bit, TMP2, Mask);
  and_(ARMEmitter::Size::i32Bit, TMP2, TMP1, TMP2);
  cbnz_OrRestart(ARMEmitter::Size::i32Bit, TMP2, FexcStub(FEXC_SITE_SIMD, false));
  ldrb(TMP2.W(), STATE, FEXC_SIMD_FLAGS);
  orr(ARMEmitter::Size::i32Bit, TMP2, TMP2, TMP1);
  strb(TMP2.W(), STATE, FEXC_SIMD_FLAGS);
  msr(ARMEmitter::SystemRegister::FPSR, ARMEmitter::XReg::zr);
}
DEF_OP(FexcSimdCheck) {
  FexcSimdCheckWords(IROp->C<IR::IROp_FexcSimdCheck>()->Mask);
}
// Perf chunk 25: STMXCSR/VSTMXCSR's value (FEX's `FexcMxcsr`, fexc_slice1.inc `Classify`): MXCSR as the resident wrote it at the
// block's entry (FEX's CPUState::mxcsr, frame::MXCSR on the host: the controls and the flags banked so far) with the flags
// generated code raised since, exactly as the resident's bank computes them (`sse::bank`, `mxcsr_flags`): FPSR's IOC, DZC,
// OFC, UFC, IXC as IE, ZE, OE, UE, PE and IDC as DE unless DAZ, FPSR or-ed with the SIMD flag byte (the flags checked
// instructions moved there under unmasked exceptions). Only x0/x1 and the destination are written.
//   ldr wD, [x28, #mxcsr]; mrs x0, fpsr; ldrb w1, [x28, #simd_flags]; orr w0, w0, w1; and w1, w0, #1; orr wD, wD, w1;
//   ubfx w1, w0, #1, #4; orr wD, wD, w1, lsl #2; ubfx w0, w0, #7, #1; bic w0, w0, wD, lsr #6; orr wD, wD, w0, lsl #1
DEF_OP(FexcMxcsr) {
  const auto Dst = GetReg(Node);
  ldr(Dst.W(), STATE, offsetof(FEXCore::Core::CPUState, mxcsr));
  mrs(TMP1, ARMEmitter::SystemRegister::FPSR);
  ldrb(TMP2.W(), STATE, FEXC_SIMD_FLAGS);
  orr(ARMEmitter::Size::i32Bit, TMP1, TMP1, TMP2);
  and_(ARMEmitter::Size::i32Bit, TMP2, TMP1, 1);
  orr(ARMEmitter::Size::i32Bit, Dst, Dst, TMP2);
  ubfx(ARMEmitter::Size::i32Bit, TMP2, TMP1, 1, 4);
  orr(ARMEmitter::Size::i32Bit, Dst, Dst, TMP2, ARMEmitter::ShiftType::LSL, 2);
  ubfx(ARMEmitter::Size::i32Bit, TMP1, TMP1, 7, 1);
  bic(ARMEmitter::Size::i32Bit, TMP1, TMP1, Dst, ARMEmitter::ShiftType::LSR, 6);
  orr(ARMEmitter::Size::i32Bit, Dst, Dst, TMP1, ARMEmitter::ShiftType::LSL, 1);
}
// Perf chunk 26: REP STOS in bulk inside RDI's page (FEX's `FexcRepStos`, before the one-element pass of FexcStringOp; only with
// `--bulk`, in 64-bit addressing). With IA32_MISC_ENABLE's fast-string bit set (its flag byte, which the resident writes at
// every entry: fast strings group iterations only then, SDM Vol.1 7.3.9.3), DF = 0, RDI 8-aligned (any for bytes: every 8-byte half of a store then holds whole,
// naturally aligned elements, each written single-copy atomically) and RCX holding n >= 1 whole 16-byte chunks: min(n, the
// chunks that fit before RDI's page end, <= 256) stores of AL/AX/EAX/RAX repeated to 16 bytes, RDI + 16 and RCX - 16 / w after
// each, so at every store (the site) RCX/RDI are those of an iteration boundary and no element after it is written. No flags
// change (no flag-setting word); only x0-x2, RCX and RDI are written. Admission pins these words (structure.rs, `bulk_loop`).
//   ldrb w0, [x28, #fast_strings]; cbz w0, S; ldrb w0, [x28, #df]; tbnz w0, #7, S; [w > 1: and x0, x11, #7; cbnz x0, S;] lsr x1, x7, #(4 - log2 w);
//   and x0, x11, #0xfff; movz w2, #0x1000; sub x0, x2, x0; lsr x0, x0, #4; sub x2, x1, x0; and x2, x2, x2, asr #63;
//   add x1, x0, x2; cbz x1, S; [the pattern into x2: w 4 `mov w2, w4; orr x2, x2, x2, lsl #32`, w 2 `and x2, x4, #0xffff`
//   then lsl #16 and #32, w 1 `and x2, x4, #0xff` then lsl #8, #16, #32;]
//   L: stp xD, xD, [x11]; add x11, x11, #16; sub x7, x7, #16/w; sub x1, x1, #1; cbnz x1, L; S:
DEF_OP(FexcRepStos) {
  const auto Op = IROp->C<IR::IROp_FexcRepStos>();
  const uint32_t W = Op->Width;
  const auto RCX = StaticRegisters[X86State::REG_RCX].X(), RDI = StaticRegisters[X86State::REG_RDI].X(), RAX = StaticRegisters[X86State::REG_RAX].X();
  constexpr auto X = ARMEmitter::Size::i64Bit;
  ARMEmitter::ForwardLabel Skip;
  ARMEmitter::BackwardLabel Loop;
  ldrb(TMP1.W(), STATE, offsetof(FEXCore::Core::CPUState, flags[FEXC_FAST_STRINGS_LOC]));
  (void)cbz(ARMEmitter::Size::i32Bit, TMP1, &Skip);
  ldrb(TMP1.W(), STATE, offsetof(FEXCore::Core::CPUState, flags[X86State::RFLAG_DF_RAW_LOC]));
  (void)tbnz(TMP1, 7, &Skip);
  if (W > 1) {
    and_(X, TMP1, RDI, 7);
    (void)cbnz(X, TMP1, &Skip);
  }
  lsr(X, TMP2, RCX, 4 - FEXCore::ilog2(W));
  and_(X, TMP1, RDI, 0xfff);
  movz(ARMEmitter::Size::i32Bit, TMP3, 0x1000);
  sub(X, TMP1, TMP3, TMP1);
  lsr(X, TMP1, TMP1, 4);
  sub(X, TMP3, TMP2, TMP1);
  and_(X, TMP3, TMP3, TMP3, ARMEmitter::ShiftType::ASR, 63);
  add(X, TMP2, TMP1, TMP3);
  (void)cbz(X, TMP2, &Skip);
  auto D = RAX;
  if (W < 8) {
    if (W == 4) mov(ARMEmitter::Size::i32Bit, TMP3, RAX);
    else and_(X, TMP3, RAX, W == 2 ? 0xffff : 0xff);
    for (uint32_t b = 8 * W; b < 64; b *= 2) orr(X, TMP3, TMP3, TMP3, ARMEmitter::ShiftType::LSL, b);
    D = TMP3;
  }
  (void)Bind(&Loop);
  stp<ARMEmitter::IndexType::OFFSET>(D, D, RDI);
  add(X, RDI, RDI, 16);
  sub(X, RCX, RCX, 16 / W);
  sub(X, TMP2, TMP2, 1);
  (void)cbnz(X, TMP2, &Loop);
  (void)Bind(&Skip);
}
// DPPS's first add level, between VFAddV's two `faddp` (SDM DPPS: "Exceptions are determined separately for each add and
// multiply operation, in the order of their execution"; the multiply has its own check op, PlaceFPChecks): the two sums'
// exact-tiny test and their check. Nothing of the instruction is committed yet, so the state before it holds here too.
void Arm64JITCore::FexcSimdStep(ARMEmitter::VRegister R) {
  if (!fexc_simd_checked.count(Entry + FexcGuest)) return;
  FexcTinyLanes(R, 4, 2, uint64_t(fexc_simd_unmasked));
  uint32_t Mask = 0;
  constexpr uint32_t Arm[6] = {1, 1u << 7, 2, 4, 8, 16};
  for (int b = 0; b < 6; ++b) if (fexc_simd_unmasked >> b & 1) Mask |= Arm[b];
  FexcSimdCheckWords(Mask);
}
DEF_OP(FexcX87Check) {
  const auto Op = IROp->C<IR::IROp_FexcX87Check>();
  ldrb(TMP1.W(), STATE, FEXC_X87_PENDING);
  sub(ARMEmitter::Size::i32Bit, TMP2, GetReg(Op->Fault), 1);
  and_(ARMEmitter::Size::i32Bit, TMP1, TMP1, TMP2);
  orr(ARMEmitter::Size::i32Bit, TMP1, TMP1, GetReg(Op->Fault), ARMEmitter::ShiftType::LSL, 8);
  orr(ARMEmitter::Size::i32Bit, TMP1, TMP1, GetReg(Op->Overflow), ARMEmitter::ShiftType::LSL, 9);
  orr(ARMEmitter::Size::i32Bit, TMP1, TMP1, GetReg(Op->Invalid), ARMEmitter::ShiftType::LSL, 10);
  orr(ARMEmitter::Size::i32Bit, TMP1, TMP1, GetReg(Op->Tiny), ARMEmitter::ShiftType::LSL, 11);
  movz(ARMEmitter::Size::i32Bit, TMP2, Op->Mask);
  and_(ARMEmitter::Size::i32Bit, TMP2, TMP1, TMP2);
  const auto it = fexc_x87_length.find(Entry + FexcGuest);
  if (it == fexc_x87_length.end()) ERROR_AND_DIE_FMT("fexc slice1 (I5c): an x87 check outside an x87 instruction");
  auto* L = FexcStub(FEXC_SITE_X87, false);
  fexc_sites.back().width = it->second;
  cbnz_OrRestart(ARMEmitter::Size::i32Bit, TMP2, L);
}
// The #MF check at the start of a waiting x87, WAIT or MMX instruction (SDM Vol.1 8.7.1): an exception flag whose mask is clear
// is pending (FSW.ES). FSW's flags: FEX's IE byte, its PE byte and the exception word (FPSR order).
void Arm64JITCore::FexcPendingCheck() {
  const uint64_t M = fexc_x87_unmasked;
  if (!M) return;
  constexpr uint32_t Arm[6] = {1, 1u << 7, 2, 4, 8, 16};
  uint32_t Mask = 0;
  for (int b = 0; b < 6; ++b) if (M >> b & 1) Mask |= Arm[b];
  ldrb(TMP1.W(), STATE, offsetof(FEXCore::Core::CPUState, flags) + FEXCore::X86State::X87FLAG_IE_LOC);
  ldrb(TMP2.W(), STATE, offsetof(FEXCore::Core::CPUState, flags) + FEXCore::X86State::X87FLAG_PE_LOC);
  orr(ARMEmitter::Size::i32Bit, TMP1, TMP1, TMP2, ARMEmitter::ShiftType::LSL, 4);
  ldr(TMP2.W(), STATE, FEXC_X87_EXCEPTIONS);
  orr(ARMEmitter::Size::i32Bit, TMP1, TMP1, TMP2);
  movz(ARMEmitter::Size::i32Bit, TMP2, Mask);
  and_(ARMEmitter::Size::i32Bit, TMP1, TMP1, TMP2);
  cbnz_OrRestart(ARMEmitter::Size::i32Bit, TMP1, FexcStub(FEXC_SITE_PENDING, false));
}
// I5c: x86 reports an unmasked underflow for a tiny result whether or not it is exact (SDM Vol.1 4.9.1.5, 11.5.2.5); Arm's FPSR.UFC
// (untrapped) only for a tiny inexact one. Under an unmasked UE (FPCR.FZ is then clear: MXCSR.FTZ is ignored) an exact tiny
// result is a nonzero denormal: after each op of a checked instruction that can underflow, every result lane is tested
// (flag-free: t = lane << (65 - bits) drops the sign; t != 0 and t's exponent field zero) and a hit sets FPSR.UFC. SSE/AVX arithmetic, FMA and narrowing
// conversions; x87: FST m32's narrowing (FST m64's test is the stack pass's, register results have the double's range only).
void Arm64JITCore::FexcTinyCheck(const IR::IROp_Header* IROp, IR::Ref Node) {
  using IR::IROps;
  const uint64_t Rip = Entry + FexcGuest;
  const bool X87 = fexc_x87.count(Rip) != 0;
  if (X87 ? !fexc_x87_store.count(Rip) : !fexc_simd_checked.count(Rip)) return;
  uint32_t Elem = IR::OpSizeToSize(IROp->ElementSize), Lanes = 0, First = 0;
  switch (IROp->Op) {
  case IROps::OP_VFADD: case IROps::OP_VFSUB: case IROps::OP_VFMUL: case IROps::OP_VFDIV: case IROps::OP_VFADDP: case IROps::OP_VFCADD:
  case IROps::OP_VFMLA: case IROps::OP_VFMLS: case IROps::OP_VFNMLA: case IROps::OP_VFNMLS:
    Lanes = X87 ? 0 : IR::OpSizeToSize(IROp->Size) / Elem; break;
  case IROps::OP_VFADDV: case IROps::OP_VFADDSCALARINSERT: case IROps::OP_VFSUBSCALARINSERT: case IROps::OP_VFMULSCALARINSERT:
  case IROps::OP_VFDIVSCALARINSERT: case IROps::OP_VFMLASCALARINSERT: case IROps::OP_VFMLSSCALARINSERT: case IROps::OP_VFNMLASCALARINSERT:
  case IROps::OP_VFNMLSSCALARINSERT:
    Lanes = X87 ? 0 : 1; break;
  case IROps::OP_VFTOFSCALARINSERT: {
    const auto Op = IROp->C<IR::IROp_VFToFScalarInsert>();
    Lanes = !X87 && Elem < IR::OpSizeToSize(Op->SrcElementSize) ? 1 : 0; break;
  }
  case IROps::OP_FLOAT_FTOF: {
    const auto Op = IROp->C<IR::IROp_Float_FToF>();
    Elem = IR::OpSizeToSize(IROp->Size);
    Lanes = Elem < IR::OpSizeToSize(Op->SrcElementSize) ? 1 : 0; break;
  }
  case IROps::OP_VECTOR_FTOF: {
    const auto Op = IROp->C<IR::IROp_Vector_FToF>();
    const uint32_t Src = IR::OpSizeToSize(Op->SrcElementSize);
    Lanes = !X87 && Elem < Src ? IR::OpSizeToSize(IROp->Size) / Src : 0; break;
  }
  case IROps::OP_VFCVTN2:   // narrowing into the upper half (VCVTPS2PH ymm's upper four; ElementSize the result's)
    First = IR::OpSizeToSize(IROp->Size) / Elem / 2;
    Lanes = X87 ? 0 : First; break;
  default: return;
  }
  if (Lanes) FexcTinyLanes(GetVReg(Node), Elem, Lanes, X87 ? uint64_t(fexc_x87_unmasked) : uint64_t(fexc_simd_unmasked), First);
}
// The exact-tiny test of `Lanes` lanes of `Elem` bytes in R from lane `First` under unmasked set `Unmasked` (x86 flag order): nothing unless UE
// is unmasked; a nonzero lane with a zero exponent field sets FPSR.UFC.
void Arm64JITCore::FexcTinyLanes(ARMEmitter::VRegister R, uint32_t Elem, uint32_t Lanes, uint64_t Unmasked, uint32_t First) {
  if (!(Unmasked & 16)) return;
  const uint32_t Bits = 8 * Elem, Exp = Elem == 2 ? 5 : Elem == 4 ? 8 : 11;
  ARMEmitter::ForwardLabel Tiny, Done;
  for (uint32_t i = First; i < First + Lanes; ++i) {
    ARMEmitter::ForwardLabel Next;
    if (Elem == 8) umov<ARMEmitter::SubRegSize::i64Bit>(TMP1, R, i);
    else if (Elem == 4) umov<ARMEmitter::SubRegSize::i32Bit>(TMP1, R, i);
    else umov<ARMEmitter::SubRegSize::i16Bit>(TMP1, R, i);
    lsl(ARMEmitter::Size::i64Bit, TMP1, TMP1, 65 - Bits);
    (void)cbz(ARMEmitter::Size::i64Bit, TMP1, &Next);   // a zero
    lsr(ARMEmitter::Size::i64Bit, TMP1, TMP1, 64 - Exp);
    (void)cbz(ARMEmitter::Size::i64Bit, TMP1, &Tiny);   // exponent field zero: a denormal
    (void)Bind(&Next);
  }
  (void)b(&Done);
  (void)Bind(&Tiny);
  mrs(TMP1, ARMEmitter::SystemRegister::FPSR);
  orr(ARMEmitter::Size::i64Bit, TMP1, TMP1, 8);
  msr(ARMEmitter::SystemRegister::FPSR, TMP1);
  (void)Bind(&Done);
}

void Arm64JITCore::FexcAfterOp(const IR::IROp_Header* IROp, IR::Ref Node, uint32_t Begin) {
  if (FexcQuietOp(IROp)) msr(ARMEmitter::SystemRegister::FPSR, TMP4);
  if (fexc_slice1) FexcTinyCheck(IROp, Node);
  if (FexcX87Op(IROp)) FexcX87End();
  FexcRecordOp(IROp, Node, Begin);
  if (fexc_slice1) FexcCheckTarget(Node);
}

void Arm64JITCore::FexcRecordOp(const IR::IROp_Header* IROp, IR::Ref Node, uint32_t Begin) {
  const uint32_t End = uint32_t(GetCursorAddress<uint8_t*>() - CodeData.BlockBegin);
  const std::string_view Name = IR::GetName(IROp->Op);
  using IR::IROps;

  if (IROp->Op == IROps::OP_GUESTOPCODE) {
    const auto Op = IROp->C<IR::IROp_GuestOpcode>();
    FexcGuest = uint32_t(Op->GuestEntryOffset);
    FexcInsnNative = Begin;
    FexcNzcvWritten = FexcNzcvSaved = FexcStoreSeen = false;
    FexcLoadSite = -1;
    const auto it = fexc_cf_inverted.find(Op->GuestEntryOffset);
    FexcCf = it == fexc_cf_inverted.end() ? true : it->second;
    for (auto i : FexcPendingFused) fexc_sites[i].guest_offset2 = FexcGuest;
    FexcPendingFused.clear();
    fexc_insns.push_back({FexcGuest, Begin, FexcCf});
    if (fexc_slice1 && fexc_x87_waiting.count(Op->GuestEntryOffset + Entry)) FexcPendingCheck();   // I5c: #MF before it runs
    if (fexc_slice1 && fexc_pre_nzcv.count(Op->GuestEntryOffset)) {
      static_assert(FexcPrecision::PreNzcvReg == 25);
      mrs(ARMEmitter::XReg::x25, ARMEmitter::SystemRegister::NZCV);   // the incoming NZCV (patch 5)
      FexcNzcvSaved = true;
    }
    return;
  }

  FexcSiteInfo S {};
  S.guest_offset = FexcGuest;
  S.guest_offset2 = FEXC_NONE32;
  S.insn_native_offset = FexcInsnNative;
  S.index_reg = 0xff;
  S.cf_inverted = FexcCf;
  S.flags_written_before = FexcNzcvWritten;
  // Profile slice1 (precision patches): the incoming NZCV is in x25 if the instruction saved it (patch 5), and the
  // site is precise unless the recorder sees an earlier store site or an unsaved NZCV write in the same instruction
  // (verify_precision.py checks the emitted code itself).
  S.nzcv = fexc_slice1 && FexcNzcvSaved;
  S.completed_effects = fexc_slice1 && !FexcStoreSeen && (!FexcNzcvWritten || FexcNzcvSaved) ? 0 : 1;
  S.ir_op = IR::GetName(IROp->Op).data();
  S.access_native_offset = FEXC_NONE32;

  // Index/offset form of LoadMem/StoreMem(TSO): none, constant, or register with extend and shift.
  auto Form = [&](IR::OrderedNodeWrapper Offset, IR::MemOffsetType Type, uint8_t Scale) {
    if (Offset.IsInvalid()) return;
    uint64_t Const;
    if (IsInlineConstant(Offset, &Const)) { S.imm = int32_t(int64_t(Const)); return; }
    S.index_reg = GetReg(Offset).Idx();
    S.shift = uint8_t(FEXCore::ilog2(Scale));
    S.extend = Type == IR::MemOffsetType::UXTW ? 1 : Type == IR::MemOffsetType::SXTW ? 2 : 3;
  };
  auto DataReg = [&](int i, IR::OrderedNodeWrapper N, IR::RegClass C, bool Zeroable) {
    S.data_class = C == IR::RegClass::GPR ? 0 : 1;
    S.data_reg[i] = C == IR::RegClass::GPR ? (Zeroable ? GetZeroableReg(N).Idx() : GetReg(N).Idx()) : GetVReg(N).Idx();
  };
  const uint8_t Size = uint8_t(IR::OpSizeToSize(IROp->Size));
  bool Record = true;

  switch (IROp->Op) {
  case IROps::OP_LOADMEM: {
    const auto Op = IROp->C<IR::IROp_LoadMem>();
    S.kind = FEXC_SITE_LOAD; S.width = Size; Form(Op->Offset, Op->OffsetType, Op->OffsetScale);
    S.data_class = Op->Class == IR::RegClass::GPR ? 0 : 1;
    S.data_reg[0] = Op->Class == IR::RegClass::GPR ? GetReg(Node).Idx() : GetVReg(Node).Idx();
    break;
  }
  case IROps::OP_LOADMEMTSO: {
    const auto Op = IROp->C<IR::IROp_LoadMemTSO>();
    S.kind = FEXC_SITE_LOAD; S.width = Size; Form(Op->Offset, Op->OffsetType, Op->OffsetScale);
    S.data_class = Op->Class == IR::RegClass::GPR ? 0 : 1;
    S.data_reg[0] = Op->Class == IR::RegClass::GPR ? GetReg(Node).Idx() : GetVReg(Node).Idx();
    S.ordering = Op->Class == IR::RegClass::GPR ? 1 : 0;
    break;
  }
  case IROps::OP_STOREMEM: {
    const auto Op = IROp->C<IR::IROp_StoreMem>();
    S.kind = FEXC_SITE_STORE; S.width = Size; S.intent = 1; Form(Op->Offset, Op->OffsetType, Op->OffsetScale);
    DataReg(0, Op->Value, Op->Class, true);
    break;
  }
  case IROps::OP_STOREMEMTSO: {
    const auto Op = IROp->C<IR::IROp_StoreMemTSO>();
    S.kind = FEXC_SITE_STORE; S.width = Size; S.intent = 1; Form(Op->Offset, Op->OffsetType, Op->OffsetScale);
    DataReg(0, Op->Value, Op->Class, true);
    S.ordering = Op->Class == IR::RegClass::GPR ? 2 : 0;
    break;
  }
  case IROps::OP_LOADMEMPAIR: {
    const auto Op = IROp->C<IR::IROp_LoadMemPair>();
    S.kind = FEXC_SITE_LOAD_PAIR; S.width = uint8_t(2 * Size); S.imm = int32_t(Op->Offset);
    DataReg(0, Op->OutValue1, Op->Class, false); DataReg(1, Op->OutValue2, Op->Class, false);
    break;
  }
  case IROps::OP_STOREMEMPAIR: {
    const auto Op = IROp->C<IR::IROp_StoreMemPair>();
    S.kind = FEXC_SITE_STORE_PAIR; S.width = uint8_t(2 * Size); S.intent = 1; S.imm = int32_t(Op->Offset);
    DataReg(0, Op->Value1, Op->Class, true); DataReg(1, Op->Value2, Op->Class, true);
    break;
  }
  case IROps::OP_PUSH: {
    const auto Op = IROp->C<IR::IROp_Push>();
    S.kind = FEXC_SITE_PUSH; S.width = uint8_t(IR::OpSizeToSize(Op->ValueSize)); S.intent = 1;
    S.data_reg[0] = GetReg(Op->Value).Idx();
    break;
  }
  case IROps::OP_PUSHTWO: {
    const auto Op = IROp->C<IR::IROp_PushTwo>();
    S.kind = FEXC_SITE_PUSH_PAIR; S.width = uint8_t(2 * IR::OpSizeToSize(Op->ValueSize)); S.intent = 1;
    S.data_reg[0] = GetReg(Op->Value1).Idx(); S.data_reg[1] = GetReg(Op->Value2).Idx();
    break;
  }
  case IROps::OP_POP: {
    const auto Op = IROp->C<IR::IROp_Pop>();
    S.kind = FEXC_SITE_POP; S.width = uint8_t(IR::OpSizeToSize(Op->Size));
    S.data_reg[0] = GetReg(Op->OutValue).Idx();
    break;
  }
  case IROps::OP_POPTWO: {
    const auto Op = IROp->C<IR::IROp_PopTwo>();
    S.kind = FEXC_SITE_POP_PAIR; S.width = uint8_t(2 * IR::OpSizeToSize(Op->Size));
    auto D1 = GetReg(Op->OutValue1); const auto D2 = GetReg(Op->OutValue2);
    S.data_reg[0] = D1 == D2 ? 31 : D1.Idx(); S.data_reg[1] = D2.Idx(); // the handler encodes a duplicate as xzr
    break;
  }
  case IROps::OP_ATOMICSWAP: {   // memory xchg: `swpal Rs, Rt, [Rn]` (SupportsAtomics), nothing written if it faults
    S.kind = FEXC_SITE_SWAP; S.width = Size; S.intent = 1;
    break;
  }
  // LOCK read-modify-writes (I3), one LSE word each with SupportsAtomics: `ld{add,clr,eor,set}al Rs, Rt, [Rn]`, `casal Rs,
  // Rt, [Rn]`, `caspal Rs, Rs+1, Rt, Rt+1, [Rn]` (CMPXCHG8B/16B: both elements). A write access that commits nothing if it
  // aborts; an operand crossing 16 bytes takes an alignment fault there (LSE2; caspal of two X registers: unless 16-aligned).
  case IROps::OP_ATOMICFETCHADD: case IROps::OP_ATOMICFETCHSUB: case IROps::OP_ATOMICFETCHAND: case IROps::OP_ATOMICFETCHCLR:
  case IROps::OP_ATOMICFETCHOR: case IROps::OP_ATOMICFETCHXOR: case IROps::OP_CAS:
    S.kind = FEXC_SITE_ATOMIC; S.width = Size; S.intent = 1;
    break;
  case IROps::OP_CASPAIR:
    S.kind = FEXC_SITE_ATOMIC; S.width = uint8_t(2 * IR::OpSizeToSize(IROp->ElementSize)); S.intent = 1;
    break;
  case IROps::OP_FEXCREPSTOS:   // perf chunk 26: the bulk loop's `stp` (found below), at an iteration boundary
    S.kind = FEXC_SITE_STORE_PAIR; S.width = 16; S.intent = 1; S.data_class = 0;
    break;
  case IROps::OP_ATOMICFETCHNEG: {
    if (!fexc_slice1) { S.kind = FEXC_SITE_OTHER; break; }   // FEX's CAS loop
    // FexcLockNeg: `ldr` (the read-modify-write load, recorded here) then `casal` (the atomic site, found below).
    FexcSiteInfo L = S;
    uint32_t w;
    std::memcpy(&w, CodeData.BlockBegin + FexcNegLoad, 4);
    L.kind = FEXC_SITE_LOAD; L.width = Size; L.native_offset = FexcNegLoad; L.located = 1;
    L.base_reg = uint8_t((w >> 5) & 31); L.data_reg[0] = uint8_t(w & 31); L.imm = 0;
    FexcLoadSite = int(fexc_sites.size());
    fexc_sites.push_back(L);
    S.kind = FEXC_SITE_ATOMIC; S.width = Size; S.intent = 1;
    break;
  }
  default:
    Record = false;
    if (FexcIsNzcvWriter(Name) || (fexc_slice1 && IR::ImplicitFlagClobber(IROp->Op))) FexcNzcvWritten = true;
    if (FexcIsOtherMemoryOp(Name)) { S.kind = FEXC_SITE_OTHER; Record = true; }
    break;
  }
  if (!Record) return;

  // The faulting word is the last load/store-class word of the op's own range whose base is not the state
  // frame (x28) or sp: the handlers emit the access last, followed at most by a nop/dmb/sub. The base register
  // and any writeback come from that word, because for push/pop the base is the register the handler picked.
  S.native_offset = End ? End - 4 : 0;
  S.base_reg = 0xff;
  for (uint32_t off = End; off >= Begin + 4 && S.kind != FEXC_SITE_OTHER; off -= 4) {
    uint32_t w;
    std::memcpy(&w, CodeData.BlockBegin + off - 4, 4);
    const uint32_t rn = (w >> 5) & 31;
    if (!FexcIsMemWord(w) || rn == 28 || rn == 31) continue;
    S.native_offset = off - 4; S.base_reg = uint8_t(rn); S.located = 1;
    // The transfer register(s) actually encoded (e.g. a push of the base register stores a TMP copy in the default profile).
    S.data_reg[0] = uint8_t(w & 31);
    if (((w >> 27) & 7) == 5) S.data_reg[1] = uint8_t((w >> 10) & 31);
    if (S.kind == FEXC_SITE_SWAP || S.kind == FEXC_SITE_ATOMIC) S.data_reg[1] = uint8_t((w >> 16) & 31);   // Rs
    const FexcAddr A = FexcDecodeAddr(w);
    if (A.imm_form) { S.imm = A.imm; S.index_reg = 0xff; S.extend = 0; S.shift = 0; S.writeback = A.wb; S.writeback_delta = A.delta; }
    break;
  }
  if (S.kind == FEXC_SITE_OTHER) { S.native_offset = Begin; S.completed_effects = 1; }
  for (auto i : FexcPendingLink) fexc_sites[i].access_native_offset = S.native_offset;   // this op's guard/split stubs
  FexcPendingLink.clear();

  // A memory-destination read-modify-write loads then stores the one explicit memory operand, so the load of
  // an instruction that also has a plain store site is the RMW load and must be walked with write intent
  // (x86 reports such a fault as a write). Valid because the admitted instructions have at most one explicit
  // memory operand, but MOVS (I2: its load reads DS:RSI, its store writes ES:RDI); implicit stack accesses are
  // push/pop kinds and never match. LOCK NEG (I3) loads, then compare-and-swaps the same operand: the same pair.
  const int Index = int(fexc_sites.size());
  if (S.kind == FEXC_SITE_LOAD) { FexcLoadSite = Index; }
  else if ((S.kind == FEXC_SITE_STORE || S.kind == FEXC_SITE_ATOMIC) && FexcLoadSite >= 0 && fexc_sites[FexcLoadSite].width == S.width &&
           !fexc_movs.count(Entry + FexcGuest)) {
    fexc_sites[FexcLoadSite].rmw = 1; fexc_sites[FexcLoadSite].intent = 1; S.rmw = 2;
  }
  if (S.kind == FEXC_SITE_PUSH_PAIR || S.kind == FEXC_SITE_POP_PAIR) FexcPendingFused.push_back(size_t(Index));
  // (Perf chunk 26: a bulk REP STOS's stores leave RCX/RDI at an iteration boundary, so the one-element pass after them still
  // starts from the state at the instruction: they do not count as committed effects.)
  if ((S.kind == FEXC_SITE_STORE || S.kind == FEXC_SITE_STORE_PAIR || S.kind == FEXC_SITE_PUSH || S.kind == FEXC_SITE_PUSH_PAIR ||
       S.kind == FEXC_SITE_OTHER || S.kind == FEXC_SITE_SWAP || S.kind == FEXC_SITE_ATOMIC) && IROp->Op != IROps::OP_FEXCREPSTOS) FexcStoreSeen = true;
  fexc_sites.push_back(S);
}

// ---------------------------------------------------------------------------------------------
// Profile slice1: split-store check (contract §1 patch 8) and branch-target check.
//
// Each check branches with tbnz to an out-of-line `brk` stub emitted after the block body (FexcEmitStubs). The stub's
// PC is a split or guard site: the runtime finds it by ELR like any site. The checks use only TMP1/TMP2 (x0/x1, never
// allocated by the RA and dead between IR ops), do not write NZCV, and sit right before the access op (or right after
// the branch target is defined), so at a stub the guest state is exactly that of the access site: the state before the
// guest instruction, with no effect of it committed. No helper call, no new IR op.
//   split   [add/sub x1, base, index|imm;] add x0, A, #w-1; eor x0, x0, A; tbnz x0, #12, stub
//                                                            store wider than 1 byte crossing a 4 KiB page (A = base or x1)
//   target  eor x0, T, T, lsl #16; tbnz x0, #63, stub       indirect jmp/call/ret: T = the exit's computed RIP (x86 raises
//                                                            #GP at the branch, before its push, pop or RIP change)
// There is no address guard (E3): generated code runs at EL0, where every non-canonical address is unmapped, EL1-only or
// execute-only, so a data access to it faults in hardware at its site and the runtime raises #GP/#SS from the site.
// A RIP-relative address (base = EntrypointOffset, no index) is a compile-time constant: a page-crossing store gets an
// unconditional `b` to its split stub instead. Direct branches to non-canonical targets are cut at decode (FexcSlice1::Cut).
// ---------------------------------------------------------------------------------------------
namespace {
constexpr bool FexcCanonical(uint64_t A) { return uint64_t(int64_t(A << 16) >> 16) == A; }
constexpr uint32_t FexcBrkGuard = 1, FexcBrkSplit = 2, FexcBrkDivide = 3, FexcBrkAlign = 4, FexcBrkSimd = 5, FexcBrkX87 = 7, FexcBrkPending = 8;   // brk immediates (the runtime keys on the PC); 1: branch target (6 is the resident's stop word)
} // namespace

// A new guard/split site for the current guest instruction, recorded like an access site at this point (width, intent,
// segment and data registers are copied from the linked access by fexc; the caller sets the address). Returns its label.
ARMEmitter::ForwardLabel* Arm64JITCore::FexcStub(uint8_t Kind, bool Link) {
  FexcSiteInfo S {};
  S.kind = Kind;
  S.guest_offset = FexcGuest;
  S.guest_offset2 = FEXC_NONE32;
  S.insn_native_offset = FexcInsnNative;
  S.base_reg = S.index_reg = 0xff;
  S.cf_inverted = FexcCf;
  S.flags_written_before = FexcNzcvWritten;
  S.nzcv = FexcNzcvSaved;
  S.completed_effects = !FexcStoreSeen && (!FexcNzcvWritten || FexcNzcvSaved) ? 0 : 1;
  S.located = 1;
  S.access_native_offset = FEXC_NONE32;
  S.ir_op = Kind == FEXC_SITE_SPLIT ? "split" : Kind == FEXC_SITE_DIVIDE ? "divide" : Kind == FEXC_SITE_ALIGN ? "align" :
            Kind == FEXC_SITE_SIMD ? "simd" : Kind == FEXC_SITE_X87 ? "x87" : Kind == FEXC_SITE_PENDING ? "pending" : "guard";
  const size_t Index = fexc_sites.size();
  fexc_sites.push_back(S);
  if (Link) FexcPendingLink.push_back(Index);
  return &FexcStubs[Index];
}

// After the node FexcPrecision::Prescan chose (the target's definition, or the instruction's GuestOpcode), check the indirect exit's
// target: in its register (a static register for jmp/call reg), or at compile time for a materialised constant.
void Arm64JITCore::FexcCheckTarget(IR::Ref Node) {
  const auto it = fexc_target_check.find(IR->GetID(Node).Value);
  if (it == fexc_target_check.end()) return;
  ++FexcTargetsDone;
  const auto Def = IR->GetNode(IR::OrderedNodeWrapper::WrapOffset(it->second.def * sizeof(IR::OrderedNode)));
  const auto DefOp = IR->GetOp<IR::IROp_Header>(Def);
  if (it->second.sra < 0 && DefOp->Op == IR::IROps::OP_CONSTANT) {
    if (!FexcCanonical(DefOp->C<IR::IROp_Constant>()->Constant)) b_OrRestart(FexcStub(FEXC_SITE_GUARD, false));
    return;
  }
  const auto T = it->second.sra >= 0 ? ARMEmitter::Register(StaticRegisters[it->second.sra]) : GetReg(Def);
  auto* L = FexcStub(FEXC_SITE_GUARD, false);
  fexc_sites.back().base_reg = uint8_t(T.Idx());
  eor(ARMEmitter::Size::i64Bit, TMP1, T.X(), T.X(), ARMEmitter::ShiftType::LSL, 16);
  tbnz_OrRestart(TMP1, 63, L);
}

void Arm64JITCore::FexcBeforeOp(const IR::IROp_Header* IROp, IR::Ref Node) {
  using IR::IROps;
  if (FexcQuietOp(IROp)) mrs(TMP4, ARMEmitter::SystemRegister::FPSR);
  if (FexcX87Op(IROp)) FexcX87Begin();
  const IR::OrderedNodeWrapper* Addr = nullptr;
  const IR::OrderedNodeWrapper* Offset = nullptr;
  IR::MemOffsetType Type = IR::MemOffsetType::SXTX;
  uint8_t Scale = 1;
  int64_t Imm = 0;
  uint32_t W = IR::OpSizeToSize(IROp->Size);
  bool Store = false, Pair = false;
  switch (IROp->Op) {
#define FEXC_MEM(OP, T, ST) \
  case IROps::OP: { const auto Op = IROp->C<IR::T>(); Addr = &Op->Addr; Offset = &Op->Offset; Type = Op->OffsetType; \
                    Scale = Op->OffsetScale; Store = ST; break; }
  FEXC_MEM(OP_LOADMEM, IROp_LoadMem, false)
  FEXC_MEM(OP_LOADMEMTSO, IROp_LoadMemTSO, false)
  FEXC_MEM(OP_STOREMEM, IROp_StoreMem, true)
  FEXC_MEM(OP_STOREMEMTSO, IROp_StoreMemTSO, true)
#undef FEXC_MEM
  // (I5a: MOVAPS/MOVAPD/MOVDQA m128 are an X-register pair, patch 0018: `Pair`, alignment-checked like a plain access.)
  // (I8: the pair's offset is a signed imm7 multiple, held unsigned in the IR: a VEX.256 operand at [base - 32] is -32.)
  case IROps::OP_LOADMEMPAIR: {
    const auto Op = IROp->C<IR::IROp_LoadMemPair>(); Addr = &Op->Addr; Imm = int32_t(Op->Offset); W *= 2; Pair = true; break;
  }
  case IROps::OP_STOREMEMPAIR: {
    const auto Op = IROp->C<IR::IROp_StoreMemPair>(); Addr = &Op->Addr; Imm = int32_t(Op->Offset); W *= 2; Store = true; Pair = true;
    break;
  }
  case IROps::OP_PUSH: {
    const auto Op = IROp->C<IR::IROp_Push>(); Addr = &Op->Addr; W = IR::OpSizeToSize(Op->ValueSize); Imm = -int64_t(W); Store = true;
    break;
  }
  case IROps::OP_POP: { const auto Op = IROp->C<IR::IROp_Pop>(); Addr = &Op->InoutAddr; W = IR::OpSizeToSize(Op->Size); break; }
  // Memory xchg: no split check: an operand crossing 16 bytes (a page included) takes an alignment fault at the swap
  // itself, before any effect (LSE2), which the runtime stops as unsupported.
  case IROps::OP_ATOMICSWAP: return;   // likewise every LOCK atomic (I3; the default case)
  case IROps::OP_UDIV: case IROps::OP_DIV: FexcDivCheck(IROp); return;
  default: return;
  }
  // Every store wider than one byte gets the split check, read-modify-write stores included: with both pages already
  // shadowed readable the load does not fault, only the store faults on the second page, and Arm may commit the first
  // part (DDI0487 B2.8.2.1). Loads, byte stores and swaps need no check: a fault anywhere commits nothing.
  // I5a: the first access of a legacy SSE instruction whose 16-byte operand must be 16-byte aligned (FexcSlice1::Sse) gets
  // the alignment check first: `and x0, A, #15; cbnz x0, stub` (#GP(0) at the stub, before any walk: QEMU's order; an
  // aligned 16-byte operand never crosses a page). FEX may load fewer bytes than the x86 operand (e.g. punpcklbw's low
  // half); the check is on the operand's address all the same.
  // I8: VMOVAPS/VMOVAPD/VMOVDQA/VMOVNT* ymm: 32-byte alignment (`and x0, A, #31`); their VEX.128 forms are fexc_align16's.
  const bool Align32 = fexc_slice1 && fexc_align32.count(Entry + FexcGuest);
  const bool Align = fexc_slice1 && (Offset || Pair) && FexcLoadSite < 0 && !FexcStoreSeen && (Align32 || fexc_align16.count(Entry + FexcGuest));
  const uint64_t AlignMask = Align32 ? 31 : 15;
  const bool Split = Store && W >= 2;
  if (!Align && !Split) return;
  bool Index = false;
  uint64_t C;
  if (Offset && !Offset->IsInvalid()) {
    if (IsInlineConstant(*Offset, &C)) Imm += int64_t(C);
    else Index = true;
  }
  const auto Base = GetReg(*Addr).X();
  // A stub site records the address as it is at the stub: register + imm (the checked register, or the RIP-relative base).
  auto Stub = [&](ARMEmitter::Register R, int64_t Off, uint8_t Kind = FEXC_SITE_SPLIT) {
    auto* L = FexcStub(Kind, true);
    fexc_sites.back().base_reg = uint8_t(R.Idx());
    fexc_sites.back().imm = int32_t(Off);
    return L;
  };
  const auto Rip = fexc_rip_relative.find(IR->GetID(Node).Value);
  if (Rip != fexc_rip_relative.end() && !Index) {   // RIP-relative: a compile-time address
    const uint64_t A = Entry + Rip->second + Imm;
    if (Align && (A & AlignMask)) { b_OrRestart(Stub(Base, Imm, FEXC_SITE_ALIGN)); return; }
    if (Split && ((A ^ (A + W - 1)) & 0x1000)) b_OrRestart(Stub(Base, Imm));
    return;
  }
  ARMEmitter::Register A = Base;
  if (Index) {
    const auto Ext = Type == IR::MemOffsetType::UXTW ? ARMEmitter::ExtendedType::UXTW :
                     Type == IR::MemOffsetType::SXTW ? ARMEmitter::ExtendedType::SXTW : ARMEmitter::ExtendedType::SXTX;
    add(ARMEmitter::Size::i64Bit, TMP2, Base, GetReg(*Offset).X(), Ext, FEXCore::ilog2(Scale));
    A = TMP2;
  } else if (Imm) {
    if (Imm > 0 && Imm < 4096) add(ARMEmitter::Size::i64Bit, TMP2, Base, uint32_t(Imm));
    else if (Imm < 0 && Imm > -4096) sub(ARMEmitter::Size::i64Bit, TMP2, Base, uint32_t(-Imm));
    else { LoadConstant(ARMEmitter::Size::i64Bit, TMP2, uint64_t(Imm)); add(ARMEmitter::Size::i64Bit, TMP2, Base, TMP2); }
    A = TMP2;
  }
  if (Align) {
    and_(ARMEmitter::Size::i64Bit, TMP1, A, AlignMask);
    cbnz_OrRestart(ARMEmitter::Size::i64Bit, TMP1, Stub(A, 0, FEXC_SITE_ALIGN));
  }
  if (!Split) return;
  add(ARMEmitter::Size::i64Bit, TMP1, A, W - 1);
  eor(ARMEmitter::Size::i64Bit, TMP1, TMP1, A);
  tbnz_OrRestart(TMP1, 12, Stub(A, 0));
}

// The stubs, after the block body (reached only by their checks; the body before them ends in an exit: `svc #1` or the
// dispatch `b`).
void Arm64JITCore::FexcEmitStubs() {
  if (FexcTargetsDone != fexc_target_check.size()) {
    ERROR_AND_DIE_FMT("fexc slice1: {} of {} indirect branch targets were not checked", fexc_target_check.size() - FexcTargetsDone,
                      fexc_target_check.size());
  }
  for (auto& [Index, Label] : FexcStubs) {
    BindOrRestart(&Label);
    fexc_sites[Index].native_offset = uint32_t(GetCursorAddress<uint8_t*>() - CodeData.BlockBegin);
    const uint8_t K = fexc_sites[Index].kind;
    brk(K == FEXC_SITE_SPLIT ? FexcBrkSplit : K == FEXC_SITE_DIVIDE ? FexcBrkDivide : K == FEXC_SITE_ALIGN ? FexcBrkAlign :
        K == FEXC_SITE_SIMD ? FexcBrkSimd : K == FEXC_SITE_X87 ? FexcBrkX87 : K == FEXC_SITE_PENDING ? FexcBrkPending : FexcBrkGuard);
  }
}

// ---------------------------------------------------------------------------------------------
// Profile slice1: #DE for div/idiv (contract S1f). FEX raises none (Arm udiv/sdiv return 0 for a zero divisor and wrap on
// overflow). Right before the divide op, which is the first op of the instruction to write anything but temporaries, a
// flag-free check on FEX's own operand registers of the op branches to a `brk #3` divide stub (one stub per branch), so
// the state at the stub is the state before the instruction, its memory divisor already loaded (a #PF there comes first,
// as on x86). Only x0/x1 are written. W is the x86 width the dispatcher recorded (FEX widens the 8-bit forms).
//   DIV  W<64  #DE iff high >= divisor (a zero divisor included), high = AH | DX | EDX:
//              {ubfx x0, lo, #8, #8 | uxth w0, wHi | mov w0, wHi}; sub x0, x0, wD, uxt{b,h,w}; tbz x0, #63, stub
//   DIV  64    cbz xD, stub; a nonzero RDX takes FEX's LUDIV helper, which reports high >= divisor (FexcDivAfterCall)
//   IDIV W<64  sxt{b,h,w} x1, wD; cbz x1, stub; x0 = dividend (sign-correct in 32 or 64 bits); sdiv x0, x0, x1;
//              sub x0, x0, w0, sxt{b,h,w}; cbnz x0, stub     (the quotient does not fit W bits; -2^63 / -1 included)
//   IDIV 64    cbz xD, stub; RDX:RAX = -2^63 with divisor -1 (FEX's inline path, whose sdiv would wrap):
//              eor x0, xLo, #1<<63; add x1, xHi, #1; orr x0, x0, x1; add x1, xD, #1; orr x0, x0, x1; cbz x0, stub;
//              a dividend that needs 128 bits takes FEX's LDIV helper, which reports any other overflow.
// ---------------------------------------------------------------------------------------------
void Arm64JITCore::FexcDivCheck(const IR::IROp_Header* IROp) {
  using ARMEmitter::ExtendedType;
  using Sz = ARMEmitter::Size;
  const auto it = fexc_div_width.find(Entry + FexcGuest);
  if (it == fexc_div_width.end()) ERROR_AND_DIE_FMT("fexc slice1: a divide op outside a guest div/idiv");
  const bool Signed = IROp->Op == IR::IROps::OP_DIV;
  IR::OrderedNodeWrapper Lower, Upper, Divisor;
  if (Signed) { const auto Op = IROp->C<IR::IROp_Div>(); Lower = Op->Lower; Upper = Op->Upper; Divisor = Op->Divisor; }
  else { const auto Op = IROp->C<IR::IROp_UDiv>(); Lower = Op->Lower; Upper = Op->Upper; Divisor = Op->Divisor; }
  const unsigned W = 8 * it->second;
  if ((W == 8) != Upper.IsInvalid()) ERROR_AND_DIE_FMT("fexc slice1: unexpected divide form for a {}-bit div/idiv", W);
  const auto L = GetReg(Lower), D = GetReg(Divisor);
  const auto H = W == 8 ? L : GetReg(Upper);
  auto Stub = [&] { return FexcStub(FEXC_SITE_DIVIDE, false); };
  const ExtendedType Uxt = W == 8 ? ExtendedType::UXTB : W == 16 ? ExtendedType::UXTH : ExtendedType::UXTW;
  const ExtendedType Sxt = W == 8 ? ExtendedType::SXTB : W == 16 ? ExtendedType::SXTH : ExtendedType::SXTW;
  if (W == 64) {
    cbz_OrRestart(Sz::i64Bit, D.X(), Stub());
    if (!Signed) return;
    eor(Sz::i64Bit, TMP1, L.X(), uint64_t(1) << 63);
    add(Sz::i64Bit, TMP2, H.X(), 1);
    orr(Sz::i64Bit, TMP1, TMP1, TMP2);
    add(Sz::i64Bit, TMP2, D.X(), 1);
    orr(Sz::i64Bit, TMP1, TMP1, TMP2);
    cbz_OrRestart(Sz::i64Bit, TMP1, Stub());
  } else if (!Signed) {
    if (W == 8) ubfx(Sz::i64Bit, TMP1, L.X(), 8, 8);
    else if (W == 16) uxth(Sz::i32Bit, TMP1, H);
    else mov(Sz::i32Bit, TMP1, H);
    sub(Sz::i64Bit, TMP1, TMP1, D, Uxt);
    tbz_OrRestart(TMP1, 63, Stub());
  } else {
    const Sz S = W == 32 ? Sz::i64Bit : Sz::i32Bit;   // the dividend: 16 bits (W = 8), 32 (16) or 64 (32)
    if (W == 8) sxtb(Sz::i32Bit, TMP2, D);
    else if (W == 16) sxth(Sz::i32Bit, TMP2, D);
    else sxtw(TMP2, D.W());
    cbz_OrRestart(S, TMP2, Stub());
    if (W == 8) sxth(Sz::i32Bit, TMP1, L);
    else if (W == 16) { uxth(Sz::i32Bit, TMP1, L); bfi(Sz::i32Bit, TMP1, H, 16, 16); }
    else { mov(Sz::i32Bit, TMP1, L); bfi(Sz::i64Bit, TMP1, H, 32, 32); }
    sdiv(S, TMP1, TMP1, TMP2);
    sub(S, TMP1, TMP1, TMP1, Sxt);
    cbnz_OrRestart(S, TMP1, Stub());
  }
}

// LOCK NEG m (I3, patch 0016). FEX lowers AtomicFetchNeg to a compare-and-swap loop, a native backward branch that admission
// refuses and the unlinking stop could not interrupt. Under slice1: ONE attempt; a lost race leaves through a literal exit to
// the instruction's own RIP, where the next pass starts (the instruction ends its block, FexcSlice1::Cut). Only x0-x3 are
// written before that exit and the dispatcher completed the flags and put CF in the exit ABI's form first, so the exit holds
// exactly the state before the instruction, as its two sites do:
//   ldr   x1, [A]            load site, the read-modify-write load (write intent)
//   neg   x2, x1; mov x3, x1
//   casal x1, x2, [A]        atomic site: x1 := memory; -x1 stored iff memory still held x1 (acquire + release)
//   sub   x2, x1, x3; cbz x2, done
//   movz/movk x0, #RIP; svc #LINK                lost the race: retry
// done: mov Dst, x1          the old value (the dispatcher computes NEG's flags from it)
void Arm64JITCore::FexcLockNeg(const IR::IROp_Header* IROp, IR::Ref Node) {
  const auto Op = IROp->C<IR::IROp_AtomicFetchNeg>();
  const auto EmitSize = ConvertSize(IROp);
  const auto SubEmitSize = ConvertSubRegSize8(IROp->Size);
  const auto MemSrc = GetReg(Op->Addr);
  FexcNegLoad = uint32_t(GetCursorAddress<uint8_t*>() - CodeData.BlockBegin);
  ldr(SubEmitSize, TMP2, MemSrc);
  neg(EmitSize, TMP3, TMP2);
  mov(EmitSize, TMP4, TMP2);
  casal(SubEmitSize, TMP2, TMP3, MemSrc);
  sub(EmitSize, TMP3, TMP2, TMP4);
  ARMEmitter::ForwardLabel Done;
  (void)cbz(EmitSize, TMP3, &Done);
  InsertGuestRIPMove(TMP1, Entry + FexcGuest);
  svc(FEXC_SVC_LINK);
  (void)Bind(&Done);
  mov(EmitSize, GetReg(Node), TMP2.R());
}

// A computed exit (its RIP already in x0) branches to the resident's dispatcher: a placeholder `b .` the resident
// relocates when it places the block (its offset is exported as a `dispatch_branch` relocation).
void Arm64JITCore::FexcDispatchExit() {
  fexc_dispatch_exits.push_back(uint32_t(GetCursorAddress<uint8_t*>() - CodeData.BlockBegin));
  dc32(0x14000000);
}

// After the resident's 128/64 division (`svc #FEXC_SVC_DIV_U/S`, which replaces FEX's LUDIV/LDIV helper call for a 64-bit
// div/idiv whose dividend needs 128 bits): x2 != 0 reports #DE (quotient does not fit 64 bits, or a zero divisor). The
// resident changes only x0-x2 and leaves NZCV and every other register as they were, so the state at the stub is still
// the state before the instruction (no write to the destination registers has happened yet).
void Arm64JITCore::FexcDivAfterCall() {
  cbnz_OrRestart(ARMEmitter::Size::i64Bit, TMP3, FexcStub(FEXC_SITE_DIVIDE, false));
}
