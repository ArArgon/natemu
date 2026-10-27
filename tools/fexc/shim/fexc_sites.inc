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
    // The transfer register(s) actually encoded (e.g. a push of the base register stores a TMP copy in the default profile).
    S.data_reg[0] = uint8_t(w & 31);
    if (((w >> 27) & 7) == 5) S.data_reg[1] = uint8_t((w >> 10) & 31);
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

// ---------------------------------------------------------------------------------------------
// Profile slice1: address guard (contract §1 patch 7), split-store check (patch 8) and branch-target check.
//
// Each check branches with tbnz to an out-of-line `brk` stub emitted after the block body (FexcEmitStubs). The stub's
// PC is a guard or split site: the runtime finds it by ELR like any site. The checks use only TMP1/TMP2 (x0/x1, never
// allocated by the RA and dead between IR ops), do not write NZCV, and sit right before the access op (or right after
// the branch target is defined), so at a stub the guest state is exactly that of the access site: the state before the
// guest instruction, with no effect of it committed. No helper call, no new IR op.
//   guard   eor x0, A, A, lsl #16; tbnz x0, #63, stub       A[63] != A[47]: the private half, the TTBR1 gap and the
//           (A = base, or x1 = base + index|imm first)       non-canonical lower addresses; the rest is in neither
//                                                            TTBR range (level-0 translation fault at the site)
//   split   add x0, A, #w-1; eor x0, x0, A; tbnz x0, #12, stub   store wider than 1 byte crossing a 4 KiB page
//   target  eor x0, T, T, lsl #16; tbnz x0, #63, stub       indirect jmp/call/ret: T = the exit's computed RIP
// A RIP-relative address (base = EntrypointOffset, no index) is a compile-time constant: a non-canonical one or a
// page-crossing store gets an unconditional `b` to its stub instead. Direct branches to non-canonical targets are
// cut at decode (FexcSlice1::Cut).
// ---------------------------------------------------------------------------------------------
namespace {
constexpr bool FexcCanonical(uint64_t A) { return uint64_t(int64_t(A << 16) >> 16) == A; }
constexpr uint32_t FexcBrkGuard = 1, FexcBrkSplit = 2;   // brk immediates (diagnostic; the runtime keys on the PC)
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
  S.ir_op = Kind == FEXC_SITE_SPLIT ? "split" : "guard";
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
  const IR::OrderedNodeWrapper* Addr = nullptr;
  const IR::OrderedNodeWrapper* Offset = nullptr;
  IR::MemOffsetType Type = IR::MemOffsetType::SXTX;
  uint8_t Scale = 1;
  int64_t Imm = 0;
  uint32_t W = IR::OpSizeToSize(IROp->Size);
  bool Store = false;
  switch (IROp->Op) {
#define FEXC_MEM(OP, T, ST) \
  case IROps::OP: { const auto Op = IROp->C<IR::T>(); Addr = &Op->Addr; Offset = &Op->Offset; Type = Op->OffsetType; \
                    Scale = Op->OffsetScale; Store = ST; break; }
  FEXC_MEM(OP_LOADMEM, IROp_LoadMem, false)
  FEXC_MEM(OP_LOADMEMTSO, IROp_LoadMemTSO, false)
  FEXC_MEM(OP_STOREMEM, IROp_StoreMem, true)
  FEXC_MEM(OP_STOREMEMTSO, IROp_StoreMemTSO, true)
#undef FEXC_MEM
  case IROps::OP_LOADMEMPAIR: { const auto Op = IROp->C<IR::IROp_LoadMemPair>(); Addr = &Op->Addr; Imm = Op->Offset; W *= 2; break; }
  case IROps::OP_STOREMEMPAIR: {
    const auto Op = IROp->C<IR::IROp_StoreMemPair>(); Addr = &Op->Addr; Imm = Op->Offset; W *= 2; Store = true; break;
  }
  case IROps::OP_PUSH: {
    const auto Op = IROp->C<IR::IROp_Push>(); Addr = &Op->Addr; W = IR::OpSizeToSize(Op->ValueSize); Imm = -int64_t(W); Store = true;
    break;
  }
  case IROps::OP_POP: { const auto Op = IROp->C<IR::IROp_Pop>(); Addr = &Op->InoutAddr; W = IR::OpSizeToSize(Op->Size); break; }
  default: return;
  }
  bool Index = false;
  uint64_t C;
  if (Offset && !Offset->IsInvalid()) {
    if (IsInlineConstant(*Offset, &C)) Imm += int64_t(C);
    else Index = true;
  }
  // Every store wider than one byte gets the split check, read-modify-write stores included: with both pages already
  // shadowed readable the load does not fault, only the store faults on the second page, and Arm may commit the first
  // part (DDI0487 B2.8.2.1). The store of a read-modify-write (same rule as the recorder) whose address is the very
  // value its load guarded skips only the guard.
  const bool Rmw = (IROp->Op == IROps::OP_STOREMEM || IROp->Op == IROps::OP_STOREMEMTSO) && FexcLoadSite >= 0 &&
                   fexc_sites[FexcLoadSite].width == W;
  const bool Split = Store && W > 1;

  const auto Base = GetReg(*Addr).X();
  // A stub site records the address as it is at the stub: register + imm (the checked register, or the RIP-relative base).
  auto Stub = [&](uint8_t Kind, ARMEmitter::Register R, int64_t Off) {
    auto* L = FexcStub(Kind, true);
    fexc_sites.back().base_reg = uint8_t(R.Idx());
    fexc_sites.back().imm = int32_t(Off);
    return L;
  };
  const auto Rip = fexc_rip_relative.find(IR->GetID(Node).Value);
  if (Rip != fexc_rip_relative.end() && !Index) {   // RIP-relative: a compile-time address
    const uint64_t A = Entry + Rip->second + Imm;
    if (!FexcCanonical(A)) b_OrRestart(Stub(FEXC_SITE_GUARD, Base, Imm));
    else if (Split && ((A ^ (A + W - 1)) & 0x1000)) b_OrRestart(Stub(FEXC_SITE_SPLIT, Base, Imm));
    return;
  }
  const bool Shared = Rmw && fexc_rmw_same_address.count(IR->GetID(Node).Value);
  if (Shared && !Split) return;
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
  if (!Shared) {
    eor(ARMEmitter::Size::i64Bit, TMP1, A, A, ARMEmitter::ShiftType::LSL, 16);
    tbnz_OrRestart(TMP1, 63, Stub(FEXC_SITE_GUARD, A, 0));
  }
  if (Split) {
    add(ARMEmitter::Size::i64Bit, TMP1, A, W - 1);
    eor(ARMEmitter::Size::i64Bit, TMP1, TMP1, A);
    tbnz_OrRestart(TMP1, 12, Stub(FEXC_SITE_SPLIT, A, 0));
  }
}

// The stubs, after the block body (reached only by their checks; the body before them ends in an exit `br`).
void Arm64JITCore::FexcEmitStubs() {
  if (FexcTargetsDone != fexc_target_check.size()) {
    ERROR_AND_DIE_FMT("fexc slice1: {} of {} indirect branch targets were not checked", fexc_target_check.size() - FexcTargetsDone,
                      fexc_target_check.size());
  }
  for (auto& [Index, Label] : FexcStubs) {
    BindOrRestart(&Label);
    fexc_sites[Index].native_offset = uint32_t(GetCursorAddress<uint8_t*>() - CodeData.BlockBegin);
    brk(fexc_sites[Index].kind == FEXC_SITE_SPLIT ? FexcBrkSplit : FexcBrkGuard);
  }
}
