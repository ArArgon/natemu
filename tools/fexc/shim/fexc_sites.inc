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

void Arm64JITCore::FexcAfterOp(const IR::IROp_Header* IROp, IR::Ref Node, uint32_t Begin) {
  if (FexcQuietOp(IROp)) msr(ARMEmitter::SystemRegister::FPSR, TMP4);
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
  if (S.kind == FEXC_SITE_STORE || S.kind == FEXC_SITE_STORE_PAIR || S.kind == FEXC_SITE_PUSH || S.kind == FEXC_SITE_PUSH_PAIR ||
      S.kind == FEXC_SITE_OTHER || S.kind == FEXC_SITE_SWAP || S.kind == FEXC_SITE_ATOMIC) FexcStoreSeen = true;
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
constexpr uint32_t FexcBrkGuard = 1, FexcBrkSplit = 2, FexcBrkDivide = 3, FexcBrkAlign = 4;   // brk immediates (the runtime keys on the PC); 1: branch target
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
  S.ir_op = Kind == FEXC_SITE_SPLIT ? "split" : Kind == FEXC_SITE_DIVIDE ? "divide" : Kind == FEXC_SITE_ALIGN ? "align" : "guard";
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
  case IROps::OP_LOADMEMPAIR: { const auto Op = IROp->C<IR::IROp_LoadMemPair>(); Addr = &Op->Addr; Imm = Op->Offset; W *= 2; Pair = true; break; }
  case IROps::OP_STOREMEMPAIR: {
    const auto Op = IROp->C<IR::IROp_StoreMemPair>(); Addr = &Op->Addr; Imm = Op->Offset; W *= 2; Store = true; Pair = true; break;
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
  const bool Align = fexc_slice1 && (Offset || Pair) && FexcLoadSite < 0 && !FexcStoreSeen && fexc_align16.count(Entry + FexcGuest);
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
    if (Align && (A & 15)) { b_OrRestart(Stub(Base, Imm, FEXC_SITE_ALIGN)); return; }
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
    and_(ARMEmitter::Size::i64Bit, TMP1, A, 15);
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
    brk(K == FEXC_SITE_SPLIT ? FexcBrkSplit : K == FEXC_SITE_DIVIDE ? FexcBrkDivide : K == FEXC_SITE_ALIGN ? FexcBrkAlign : FexcBrkGuard);
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
