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
    "LoadMemX87SVEOptPredicate", "MemSet", "MemCpy", "VStoreNonTemporal", "VStoreNonTemporalPair", "VLoadNonTemporal", "CAS",
    "CASPair", "AtomicSwap", "AtomicFetchAdd", "AtomicFetchSub", "AtomicFetchAnd", "AtomicFetchCLR", "AtomicFetchOr",
    "AtomicFetchXor", "AtomicFetchNeg", "CacheLineClear", "CacheLineClean", "CacheLineZero"};
  for (auto s : names) if (s == n) return true;
  return false;
}
} // namespace

void Arm64JITCore::FexcAfterOp(const IR::IROp_Header* IROp, IR::Ref Node, uint32_t Begin) {
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
    return;
  }

  FexcSiteInfo S {};
  S.guest_offset = FexcGuest;
  S.guest_offset2 = FEXC_NONE32;
  S.insn_native_offset = FexcInsnNative;
  S.index_reg = 0xff;
  S.cf_inverted = FexcCf;
  S.flags_written_before = FexcNzcvWritten;
  // Profile slice1 (precision patches): the incoming NZCV is in the frame if the instruction saved it (patch 5), and the
  // site is precise unless the recorder sees an earlier store site or an unsaved NZCV write in the same instruction
  // (verify_precision.py checks the emitted code itself).
  S.nzcv = fexc_slice1 && FexcNzcvSaved;
  S.completed_effects = fexc_slice1 && !FexcStoreSeen && (!FexcNzcvWritten || FexcNzcvSaved) ? 0 : 1;
  S.ir_op = IR::GetName(IROp->Op).data();

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
  default:
    Record = false;
    if (FexcIsNzcvWriter(Name) || (fexc_slice1 && IR::ImplicitFlagClobber(IROp->Op))) FexcNzcvWritten = true;
    if (fexc_slice1 && IROp->Op == IROps::OP_STORECONTEXT && IROp->C<IR::IROp_StoreContext>()->Offset == FexcPrecision::PreNzcvOffset &&
        !FexcNzcvWritten) FexcNzcvSaved = true;
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
    const FexcAddr A = FexcDecodeAddr(w);
    if (A.imm_form) { S.imm = A.imm; S.index_reg = 0xff; S.extend = 0; S.shift = 0; S.writeback = A.wb; S.writeback_delta = A.delta; }
    break;
  }
  if (S.kind == FEXC_SITE_OTHER) { S.native_offset = Begin; S.completed_effects = 1; }

  // A memory-destination read-modify-write loads then stores the one explicit memory operand, so the load of
  // an instruction that also has a plain store site is the RMW load and must be walked with write intent
  // (x86 reports such a fault as a write). Valid because the admitted instructions have at most one explicit
  // memory operand; implicit stack accesses are push/pop kinds and never match.
  const int Index = int(fexc_sites.size());
  if (S.kind == FEXC_SITE_LOAD) { FexcLoadSite = Index; }
  else if (S.kind == FEXC_SITE_STORE && FexcLoadSite >= 0 && fexc_sites[FexcLoadSite].width == S.width) {
    fexc_sites[FexcLoadSite].rmw = 1; fexc_sites[FexcLoadSite].intent = 1; S.rmw = 2;
  }
  if (S.kind == FEXC_SITE_PUSH_PAIR || S.kind == FEXC_SITE_POP_PAIR) FexcPendingFused.push_back(size_t(Index));
  if (S.kind == FEXC_SITE_STORE || S.kind == FEXC_SITE_STORE_PAIR || S.kind == FEXC_SITE_PUSH || S.kind == FEXC_SITE_PUSH_PAIR ||
      S.kind == FEXC_SITE_OTHER) FexcStoreSeen = true;
  fexc_sites.push_back(S);
}
