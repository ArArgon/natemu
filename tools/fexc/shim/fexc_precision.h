// natemu fexc, profile slice1: shared predicates of the precision patches (slice-1 contract §1, patches 2-5).
// Included by patches/0006 into FEX's flag-elimination and register-allocation passes and by shim/fexc_sites.inc, after the
// FEX IR headers. Every use is guarded by fexc_slice1; the default profile never reaches this code.
#pragma once
#include <fexc_hook.h>
#include <FEXCore/Core/CoreState.h>
#include <FEXCore/Core/X86Enums.h>
#include <cstddef>
#include <initializer_list>
#include <vector>

namespace FexcPrecision {
// IR ops that access guest memory and are recorded as sites (PushTwo/PopTwo only arise post-RA, and not under slice1).
inline bool GuestMemOp(FEXCore::IR::IROps Op) {
  using namespace FEXCore::IR;
  switch (Op) {
  case OP_LOADMEM: case OP_LOADMEMTSO: case OP_STOREMEM: case OP_STOREMEMTSO: case OP_LOADMEMPAIR: case OP_STOREMEMPAIR:
  case OP_PUSH: case OP_PUSHTWO: case OP_POP: case OP_POPTWO: case OP_ATOMICSWAP: return true;
  // Perf chunk 26: a bulk REP STOS's stores (a site); it also writes RCX's and RDI's static registers itself, so as for every
  // access no value is coalesced into a static register across it, and the incoming flags stay live there.
  case OP_FEXCREPSTOS: return true;
  // LOCK read-modify-writes (I3)
  case OP_ATOMICFETCHADD: case OP_ATOMICFETCHSUB: case OP_ATOMICFETCHAND: case OP_ATOMICFETCHCLR: case OP_ATOMICFETCHOR:
  case OP_ATOMICFETCHXOR: case OP_ATOMICFETCHNEG: case OP_CAS: case OP_CASPAIR: return true;
  default: return false;
  }
}
// Ops whose result is loaded from guest memory by their faulting word: plain loads, and the swap and fetch-and-op atomics
// (`swpal`, `ld<op>al`). After a synchronous abort a load's destination is UNKNOWN (DDI0487 D1.4.1.4), so register
// allocation never prefers a static register for it (the result reaches the guest register by a move after the site).
// CAS/CASP are not listed: their compared-and-loaded register is restored on an abort (DDI0487 C6.2 CAS, CASP).
inline bool LoadsInto(FEXCore::IR::IROps Op) {
  using namespace FEXCore::IR;
  switch (Op) {
  case OP_LOADMEM: case OP_LOADMEMTSO: case OP_ATOMICSWAP: case OP_ATOMICFETCHADD: case OP_ATOMICFETCHSUB: case OP_ATOMICFETCHAND:
  case OP_ATOMICFETCHCLR: case OP_ATOMICFETCHOR: case OP_ATOMICFETCHXOR: return true;
  default: return false;
  }
}
// Ops at which a slice1 guest fault is raised with the state before the instruction: guest memory accesses, and (S1f) the
// divide ops, right before which the #DE check branches to its stub. The incoming flags must be live there.
// I5c: the floating-point exception checks (PlaceFPChecks below): a fault point too, and, for register allocation, an access of
// every static register (no value is coalesced into its static register across one), so at their stub the guest state is the
// state before the instruction.
inline bool CheckOp(FEXCore::IR::IROps Op) {
  return Op == FEXCore::IR::OP_FEXCSIMDCHECK || Op == FEXCore::IR::OP_FEXCX87CHECK;
}
inline bool FaultPoint(FEXCore::IR::IROps Op) {
  return GuestMemOp(Op) || Op == FEXCore::IR::OP_DIV || Op == FEXCore::IR::OP_UDIV || CheckOp(Op);
}
// Register holding the instruction's incoming NZCV (patch 5): `mrs x25, nzcv` at the instruction start. x25 is FEX's
// call-return stack pointer, which profile slice1 never uses (exits go through the resident), so the register
// allocator never hands it out and no other word writes it (admission checks this).
inline constexpr uint32_t PreNzcvReg = 25;

// Profile slice1 (split-store check, branch-target check): called at the start of register allocation, while operands are
// still SSA nodes. Records each guest load/store whose base is an EntrypointOffset (a RIP-relative operand: its address
// is a compile-time constant), and for each ExitFunction with a computed RIP the node after which the JIT checks it: the
// target's definition when that lies inside the exit's guest instruction (after its GuestOpcode), else the GuestOpcode.
inline void Prescan(const FEXCore::IR::IRListView& IR) {
  using namespace FEXCore::IR;
  fexc_rip_relative.clear();
  fexc_target_check.clear();
  std::vector<uint32_t> Owner(IR.GetSSACount(), ~0u);   // node ID -> GuestOpcode node ID in force
  uint32_t Guest = ~0u;
  for (auto [BlockNode, BlockHeader] : IR.GetBlocks()) {
    for (auto [CodeNode, IROp] : IR.GetCode(BlockNode)) {
      const uint32_t ID = IR.GetID(CodeNode).Value;
      if (IROp->Op == OP_GUESTOPCODE) Guest = ID;
      Owner[ID] = Guest;
      OrderedNodeWrapper Addr = OrderedNodeWrapper::WrapOffset(0);
      switch (IROp->Op) {
      case OP_LOADMEM: Addr = IROp->C<IROp_LoadMem>()->Addr; break;
      case OP_LOADMEMTSO: Addr = IROp->C<IROp_LoadMemTSO>()->Addr; break;
      case OP_STOREMEM: Addr = IROp->C<IROp_StoreMem>()->Addr; break;
      case OP_STOREMEMTSO: Addr = IROp->C<IROp_StoreMemTSO>()->Addr; break;
      case OP_LOADMEMPAIR: Addr = IROp->C<IROp_LoadMemPair>()->Addr; break;
      case OP_STOREMEMPAIR: Addr = IROp->C<IROp_StoreMemPair>()->Addr; break;
      case OP_ATOMICSWAP: Addr = IROp->C<IROp_AtomicSwap>()->Addr; break;
      case OP_CAS: Addr = IROp->C<IROp_CAS>()->Addr; break;
      case OP_CASPAIR: Addr = IROp->C<IROp_CASPair>()->Addr; break;
      case OP_ATOMICFETCHADD: Addr = IROp->C<IROp_AtomicFetchAdd>()->Addr; break;
      case OP_ATOMICFETCHSUB: Addr = IROp->C<IROp_AtomicFetchSub>()->Addr; break;
      case OP_ATOMICFETCHAND: Addr = IROp->C<IROp_AtomicFetchAnd>()->Addr; break;
      case OP_ATOMICFETCHCLR: Addr = IROp->C<IROp_AtomicFetchCLR>()->Addr; break;
      case OP_ATOMICFETCHOR: Addr = IROp->C<IROp_AtomicFetchOr>()->Addr; break;
      case OP_ATOMICFETCHXOR: Addr = IROp->C<IROp_AtomicFetchXor>()->Addr; break;
      case OP_ATOMICFETCHNEG: Addr = IROp->C<IROp_AtomicFetchNeg>()->Addr; break;
      default: break;
      }
      if (!Addr.IsInvalid()) {
        const auto A = IR.GetOp<IROp_Header>(Addr);
        if (A->Op == OP_ENTRYPOINTOFFSET) fexc_rip_relative[ID] = A->C<IROp_EntrypointOffset>()->Offset;
      }
      if (IROp->Op != OP_EXITFUNCTION || Guest == ~0u) continue;
      const auto T = IROp->C<IROp_ExitFunction>()->NewRIP;
      const auto TOp = IR.GetOp<IROp_Header>(T)->Op;
      if (TOp == OP_INLINECONSTANT || TOp == OP_INLINEENTRYPOINTOFFSET) continue;   // direct: checked by FexcSlice1::Cut
      // A target read from a static register (jmp/call reg) is checked in that register at the instruction start: the
      // read comes first in the instruction and FEX may drop the trivial read itself after register allocation.
      const uint32_t Def = T.ID().Value;
      const auto DefOp = IR.GetOp<IROp_Header>(T);
      if (DefOp->Op == OP_LOADREGISTER && DefOp->C<IROp_LoadRegister>()->Class == RegClass::GPR) {
        fexc_target_check[Guest] = {Def, int(DefOp->C<IROp_LoadRegister>()->Reg)};
      } else {
        fexc_target_check[Owner[Def] == Guest ? Def : Guest] = {Def, -1};
      }
    }
  }
}

// ---------------------------------------------------------------------------------------------
// I5c: unmasked floating-point exceptions (SDM Vol.1 4.9, 8.5, 11.5; Vol.3 events 16 and 19). Hardware FP traps are not used: a
// trap reports one Arm op, x86 an instruction. Under `fexc_simd_unmasked` / `fexc_x87_unmasked` (0 as Linux runs: nothing here
// happens) each instruction that may raise one of the unmasked exceptions gets a check op right before the first op that commits
// anything of it (a store to a guest register, the guest-state page or memory, an exit), after every floating-point op of it:
//   FexcSimdCheck: the instruction's FPSR flags (FPSR holds only this instruction's: every flag-raising SSE/AVX instruction is
//     checked and folds its flags into FEXC_SIMD_FLAGS once its check passes) against the unmasked ones -> #XM/#UD stub.
//     DPPS/DPPD also get one right after each of their floating-point ops but the last (SDM DPPS: "exceptions are determined
//     separately for each add and multiply operation, in the order of their execution"; VFAddV checks its first level itself).
//   FexcX87Check: the x87 instruction's flags (FEXC_X87_PENDING) and its stack fault, overflow, invalid-operand (FST m64 SNaN,
//     FIST out of range) and exact-tiny (FST m64) bits against the exceptions that leave the operands unaltered when unmasked:
//     IE, DE, ZE, and OE/UE for a store to memory (SDM Vol.1 8.5.1-8.5.5) -> the x87 stub (the resident sets the flags, nothing
//     else, and resumes after the instruction; #MF follows at the next waiting instruction).
// The x87 check's operands are the lowering's fexc_x87_extra (fexc_hook.h).
// SSE/AVX ops that may raise an MXCSR flag (the I5a/I8 lowerings; x87 instructions' ops are the x87 wrapper's).
inline bool SimdFPOp(FEXCore::IR::IROps Op) {
  using namespace FEXCore::IR;
  switch (Op) {
  case OP_VFADD: case OP_VFSUB: case OP_VFMUL: case OP_VFDIV: case OP_VFMIN: case OP_VFMAX: case OP_VFSQRT: case OP_VFADDP: case OP_VFADDV:
  case OP_VFCMPEQ: case OP_VFCMPNEQ: case OP_VFCMPLT: case OP_VFCMPGT: case OP_VFCMPLE: case OP_VFCMPORD: case OP_VFCMPUNO: case OP_VFCADD:
  case OP_VFMLA: case OP_VFMLS: case OP_VFNMLA: case OP_VFNMLS: case OP_VFRECP: case OP_VFRSQRT: case OP_VFRECPPRECISION: case OP_VFRSQRTPRECISION:
  case OP_VFADDSCALARINSERT: case OP_VFSUBSCALARINSERT: case OP_VFMULSCALARINSERT: case OP_VFDIVSCALARINSERT: case OP_VFMINSCALARINSERT:
  case OP_VFMAXSCALARINSERT: case OP_VFSQRTSCALARINSERT: case OP_VFRSQRTSCALARINSERT: case OP_VFRECPSCALARINSERT: case OP_VFTOFSCALARINSERT:
  case OP_VSTOFVECTORINSERT: case OP_VSTOFGPRINSERT: case OP_VFTOISCALARINSERT: case OP_VFCMPSCALARINSERT: case OP_VFMLASCALARINSERT:
  case OP_VFMLSSCALARINSERT: case OP_VFNMLASCALARINSERT: case OP_VFNMLSSCALARINSERT: case OP_FLOAT_FROMGPR_S: case OP_FLOAT_FTOF:
  case OP_FLOAT_TOGPR_S: case OP_FLOAT_TOGPR_ZS: case OP_VECTOR_STOF: case OP_VECTOR_FTOS: case OP_VECTOR_FTOZS: case OP_VECTOR_FTOF:
  case OP_VFCVTL2: case OP_VFCVTN2: case OP_VECTOR_FTOI: case OP_VECTOR_FTOISIZED: case OP_VECTOR_F64TOI32: case OP_FCMP: return true;
  default: return false;
  }
}
// The FPSR bits a SIMD check tests: those of the unmasked MXCSR exceptions (x86 flag order IE DE ZE OE UE PE -> IOC IDC DZC OFC UFC
// IXC). AFP fallback chunk 3a, row 3: without AFP an FZ flush (DAZ and FTZ both set) raises UFC and no IXC, but x86 sets PE for a
// flush: under effective FTZ (`--ftz`) an unmasked PE is tested through UFC too (UE is masked then, so every UFC is a flush).
inline uint32_t SimdCheckMask() {
  const uint64_t M = fexc_simd_unmasked;
  constexpr uint32_t Arm[6] = {1, 1u << 7, 2, 4, 8, 16};
  uint32_t Mask = 0;
  for (int b = 0; b < 6; ++b) if (M >> b & 1) Mask |= Arm[b];
  if (fexc_no_afp && (M >> 5 & 1) && fexc_ftz) Mask |= 8;
  return Mask;
}
// The ops that commit an instruction's effect: everything with a side effect but the NZCV writers (the incoming NZCV is saved
// for a fault point after one, patch 0006), instruction markers, loads, prefetches and FEX's rounding-mode bookkeeping.
inline bool Commits(FEXCore::IR::IROps Op) {
  using namespace FEXCore::IR;
  switch (Op) {
  case OP_GUESTOPCODE: case OP_INVALIDATEFLAGS: case OP_INLINECONSTANT: case OP_INLINEENTRYPOINTOFFSET: case OP_PREFETCH:
  case OP_LOADCONTEXTPAIR: case OP_LOADMEMPAIR: case OP_VLOADNONTEMPORAL: case OP_DIV: case OP_UDIV: case OP_FCMP: case OP_STORENZCV:
  case OP_SETROUNDINGMODE: case OP_PUSHROUNDINGMODE: case OP_POPROUNDINGMODE: case OP_FEXCSIMDCHECK: case OP_FEXCX87CHECK:
  case OP_ADDWITHFLAGS: case OP_ADDNZCV: case OP_SETSMALLNZV: case OP_CARRYINVERT: case OP_AXFLAG: case OP_RMIFNZCV: case OP_CONDADDNZCV:
  case OP_CONDSUBNZCV: case OP_ADCWITHFLAGS: case OP_ADCZEROWITHFLAGS: case OP_SBBWITHFLAGS: case OP_ADCNZCV: case OP_SBBNZCV:
  case OP_SUBWITHFLAGS: case OP_CMPPAIRZ: case OP_SUBNZCV: case OP_ANDWITHFLAGS: case OP_TESTNZ: case OP_TESTZ: case OP_SHIFTFLAGS:
  case OP_ROTATEFLAGS: case OP_FEXCFLUSH: return false;
  default: return HasSideEffects(Op);
  }
}
// Places the checks (run at the start of flag elimination, after the x87 stack pass, before register allocation). An
// instruction whose floating-point op follows its first commit has no exact check point: the compile fails (a producer bug).
template<class Emitter> inline void PlaceFPChecks(Emitter* IREmit) {   // (a template: IREmitter is complete only in its caller)
  using namespace FEXCore::IR;
  auto IR = IREmit->ViewIR();
  const uint64_t Rip0 = IR.GetHeader()->OriginalRIP;
  for (auto [BlockNode, BlockHeader] : IR.GetBlocks()) {
    std::vector<std::pair<Ref, IROps>> Insn;
    uint64_t Rip = ~0ull;
    auto Finish = [&]() {
      if (Rip == ~0ull || Insn.empty()) return;
      const bool X87 = fexc_x87.count(Rip) != 0;
      bool Fp = false, Dpp = false;
      for (size_t i = 0; i < Insn.size(); ++i) {
        Fp |= SimdFPOp(Insn[i].second);
        if (Insn[i].second == OP_VFMUL) for (size_t j = i + 1; j < Insn.size(); ++j) Dpp |= Insn[j].second == OP_VFADDV;
      }
      uint32_t Mask = 0;
      bool Store = false;
      if (X87) {
        if (!Fp && !fexc_x87_extra.count(Rip)) return;   // FXAM, FNSTSW, FINCSTP, FFREE...: nothing to raise (stack faults are extras)
        const uint64_t M = fexc_x87_unmasked;
        Store = fexc_x87_store.count(Rip) != 0;
        // x86 order IE DE ZE OE UE PE -> FPSR IOC IDC DZC OFC UFC IXC; extras in bits 8.. (stack fault, overflow, invalid, tiny).
        if (M & 1) Mask |= 1 | 1u << 8 | 1u << 10;
        if (M & 2) Mask |= 1u << 7;
        if (M & 4) Mask |= 2;
        if (Store && (M & 8)) Mask |= 4;
        if (Store && (M & 16)) Mask |= 8 | 1u << 11;
        if (!Fp) Mask &= 0xf00;   // no floating-point op: only its stack fault can be raised
      } else {
        if (!Fp || fexc_fp_quiet.count(Rip)) return;
        Mask = SimdCheckMask();
      }
      if (!Mask) return;
      size_t First = Insn.size();
      for (size_t i = 0; i < Insn.size(); ++i) if (Commits(Insn[i].second)) { First = i; break; }
      for (size_t i = First; i < Insn.size(); ++i)
        if (SimdFPOp(Insn[i].second)) ERROR_AND_DIE_FMT("fexc slice1 (I5c): a floating-point op after the first commit at {:#x}", Rip);
      if (First == Insn.size()) ERROR_AND_DIE_FMT("fexc slice1 (I5c): no commit to check before at {:#x}", Rip);
      IREmit->SetWriteCursorBefore(Insn[First].first);
      if (X87) {
        const auto It = fexc_x87_extra.find(Rip);
        const FexcX87Extras E = It == fexc_x87_extra.end() ? FexcX87Extras {} : It->second;
        Ref Zero = nullptr;
        auto Arg = [&](void* P) {
          Ref R = static_cast<Ref>(P);
          if (R) {
            bool Seen = false;
            for (size_t i = 0; i < First && !Seen; ++i) Seen = Insn[i].first == R;
            if (!Seen) ERROR_AND_DIE_FMT("fexc slice1 (I5c): an x87 check operand defined after the check at {:#x}", Rip);
            return R;
          }
          return Zero ? Zero : Zero = IREmit->_Constant(0);
        };
        Ref F = Arg(E.fault), O = Arg(E.overflow), I = Arg(E.invalid), T = Arg(E.tiny);
        IREmit->_FexcX87Check(F, O, I, T, Mask, Store);
      } else {
        fexc_simd_checked.insert(Rip);
        IREmit->_FexcSimdCheck(Mask);
        if (Dpp) {   // DPPS/DPPD: every multiply and add step is checked in its order (VDPPS ymm: each half in turn)
          size_t Last = First;
          for (size_t i = 0; i < First; ++i) if (SimdFPOp(Insn[i].second)) Last = i;
          for (size_t i = 0; i < Last; ++i) if (SimdFPOp(Insn[i].second)) {
            // a step's check follows its flush (PlaceFlushes ran first): the flush's UFC/IXC belong to this step, not the next
            Ref At = Insn[i].first;
            for (size_t j = i + 1; j < Insn.size() && Insn[j].second == OP_FEXCFLUSH &&
                                   IR.GetNode(IR.template GetOp<IROp_Header>(Insn[j].first)->Args[0]) == At; ++j)
              At = Insn[j].first;
            IREmit->SetWriteCursor(At);
            IREmit->_FexcSimdCheck(Mask);
          }
        }
      }
    };
    for (auto [CodeNode, IROp] : IR.GetCode(BlockNode)) {
      if (IROp->Op == OP_GUESTOPCODE) {
        Finish();
        Insn.clear();
        Rip = Rip0 + IROp->template C<IROp_GuestOpcode>()->GuestEntryOffset;
        continue;
      }
      Insn.push_back({CodeNode, IROp->Op});
    }
    Finish();
  }
}
// ---------------------------------------------------------------------------------------------
// AFP fallback chunk 3a (plan rows 4-6): MXCSR.DAZ and effective FTZ on a host without FEAT_AFP, where FPCR.FIZ is RES0 and FZ also
// flushes inputs (inventory 1.7). The host keys the code on both (`--daz`, `--ftz`) and runs it with FPCR.FZ = 1 only when both are
// set; otherwise FZ = 0 and each Arm FP op of an SSE/AVX instruction gets what the M4's FIZ and FZ do to it, as `FexcFlush` ops (an
// element whose exponent field is zero becomes a zero of its sign) around it, on the op's own operands and result:
//   DAZ alone       every FP source operand of the ops in the table below is flushed first (no flag: DE is never set without AFP).
//   FTZ alone       every FP result of the ops where x86 flushes (arithmetic, FMA, HADD/HSUB/ADDSUB, the narrowing converts to
//                   single; not conversions to integer, ROUND, VCVTPS2PH, the compares, MIN/MAX, SQRT, RSQRT) is flushed after,
//                   raising UFC and IXC for each lane flushed (x86 sets UE and PE; an op that rounded a tiny result raised both).
//                   RCP's fdiv result is flushed too (as FZ does it on the M4) but raises nothing: its flags are restored after it.
//   DAZ and FTZ     FZ = 1 flushes inputs and results in hardware (UFC alone: PE comes from UFC in `mxcsr_flags`), but MIN/MAX
//                   return a raw operand, so their result is flushed (no flag).
// x87 instructions (their own FPCR) are left alone. DPPS's first
// add level inside VFAddV is flushed by the JIT (FexcFlushStep). The tininess band (an exact value just under MIN_NORMAL that
// rounds up to it) stays the declared deviation of decision C.
struct FlushRule {
  uint8_t In[4] = {0xff, 0xff, 0xff, 0xff};   // the arguments flushed under DAZ, 0xff ends
  uint8_t Out = 0;                            // the result is flushed: 1 under FTZ alone, with flags; 2 under DAZ and FTZ (MIN/MAX), 3 under FTZ alone (RCP), without
  bool InScalar = false, OutScalar = false;   // element 0 only (the other bits are kept)
  uint32_t InElem = 0, OutElem = 0;           // bytes of the operands' and the result's elements
};
inline bool FlushRuleOf(const FEXCore::IR::IROp_Header* Op, FlushRule& R) {
  using namespace FEXCore::IR;
  using std::initializer_list;
  const uint32_t E = OpSizeToSize(Op->ElementSize), S = OpSizeToSize(Op->Size);
  auto In = [&](initializer_list<uint8_t> A, uint32_t Elem, bool Scalar) {
    size_t n = 0;
    for (auto a : A) R.In[n++] = a;
    R.InElem = Elem; R.InScalar = Scalar;
  };
  auto Out = [&](uint8_t Kind, uint32_t Elem, bool Scalar) { R.Out = Kind; R.OutElem = Elem; R.OutScalar = Scalar; };
  switch (Op->Op) {
  case OP_VFADD: case OP_VFSUB: case OP_VFMUL: case OP_VFDIV: case OP_VFADDP: case OP_VFCADD:
    In({0, 1}, E, S == E); Out(1, E, S == E); break;
  case OP_VFMIN: case OP_VFMAX: In({0, 1}, E, S == E); Out(2, E, S == E); break;
  case OP_VFMLA: case OP_VFMLS: case OP_VFNMLA: case OP_VFNMLS: In({0, 1, 2}, E, S == E); Out(1, E, S == E); break;
  case OP_VFRECP: In({0}, E, S == E); Out(3, E, S == E); break;
  case OP_VFSQRT: case OP_VFRSQRT: case OP_VECTOR_FTOS: case OP_VECTOR_FTOZS: case OP_VECTOR_FTOI:
  case OP_VECTOR_FTOISIZED: In({0}, E, S == E); break;
  case OP_VFCMPEQ: case OP_VFCMPNEQ: case OP_VFCMPLT: case OP_VFCMPGT: case OP_VFCMPLE: case OP_VFCMPORD: case OP_VFCMPUNO:
    In({0, 1}, E, S == E); break;
  case OP_VFADDV: In({0}, E, false); Out(1, E, true); break;   // (the result is element 0; the first add level: FexcFlushStep)
  case OP_VFADDSCALARINSERT: case OP_VFSUBSCALARINSERT: case OP_VFMULSCALARINSERT: case OP_VFDIVSCALARINSERT:
    In({0, 1}, E, true); Out(1, E, true); break;
  case OP_VFMINSCALARINSERT: case OP_VFMAXSCALARINSERT: In({0, 1}, E, true); Out(2, E, true); break;
  case OP_VFMLASCALARINSERT: case OP_VFMLSSCALARINSERT: case OP_VFNMLASCALARINSERT: case OP_VFNMLSSCALARINSERT:
    In({1, 2, 3}, E, true); Out(1, E, true); break;   // (argument 0 only supplies the upper elements)
  case OP_VFRECPSCALARINSERT: In({1}, E, true); Out(3, E, true); break;
  case OP_VFSQRTSCALARINSERT: case OP_VFRSQRTSCALARINSERT: case OP_VFTOISCALARINSERT: In({1}, E, true); break;
  case OP_VFCMPSCALARINSERT: In({0, 1}, E, true); break;
  case OP_VFTOFSCALARINSERT: {
    const uint32_t Src = OpSizeToSize(Op->C<IROp_VFToFScalarInsert>()->SrcElementSize);
    In({1}, Src, true);
    if (E == 4 && Src == 8) Out(1, E, true);
    break;
  }
  case OP_FLOAT_FTOF: {
    const uint32_t Src = OpSizeToSize(Op->C<IROp_Float_FToF>()->SrcElementSize);
    In({0}, Src, true);
    if (S == 4 && Src == 8) Out(1, S, true);
    break;
  }
  case OP_FLOAT_TOGPR_S: In({0}, OpSizeToSize(Op->C<IROp_Float_ToGPR_S>()->SrcElementSize), true); break;
  case OP_FLOAT_TOGPR_ZS: In({0}, OpSizeToSize(Op->C<IROp_Float_ToGPR_ZS>()->SrcElementSize), true); break;
  case OP_FCMP: In({0, 1}, OpSizeToSize(Op->C<IROp_FCmp>()->ElementSize), true); break;
  case OP_VECTOR_F64TOI32: In({0}, 8, false); break;
  case OP_VECTOR_FTOF: {
    const uint32_t Src = OpSizeToSize(Op->C<IROp_Vector_FToF>()->SrcElementSize);
    In({0}, Src, false);
    if (E == 4 && Src == 8) Out(1, E, false);
    break;
  }
  case OP_VFCVTL2: In({0}, E >> 1, false); break;
  case OP_VFCVTN2: In({1}, E << 1, false); if (E == 4) Out(1, E, false); break;   // (argument 0 holds the lower half, converted before)
  default: return false;
  }
  return R.In[0] != 0xff;
}
// Places the flushes (run at the start of flag elimination, before PlaceFPChecks: the flush ops come before an instruction's first
// commit, so before its check, and a flushed result's flags are checked). Nothing on a host with AFP, in a block without such an op,
// or with neither key set; `--daz` and `--ftz` are read only once such an op is found.
template<class Emitter> inline void PlaceFlushes(Emitter* IREmit) {
  using namespace FEXCore::IR;
  if (!fexc_no_afp) return;
  auto IR = IREmit->ViewIR();
  const uint64_t Rip0 = IR.GetHeader()->OriginalRIP;
  struct Found { Ref Node; uint64_t Rip; };
  std::vector<Found> Ops;
  uint64_t Rip = ~0ull;
  for (auto [BlockNode, BlockHeader] : IR.GetBlocks()) {
    for (auto [CodeNode, IROp] : IR.GetCode(BlockNode)) {
      if (IROp->Op == OP_GUESTOPCODE) { Rip = Rip0 + IROp->template C<IROp_GuestOpcode>()->GuestEntryOffset; continue; }
      FlushRule R;
      if (Rip != ~0ull && !fexc_x87.count(Rip) && FlushRuleOf(IROp, R)) Ops.push_back({CodeNode, Rip});
    }
  }
  if (Ops.empty()) return;
  const bool Daz = fexc_daz, Ftz = fexc_ftz;
  if (!Daz && !Ftz) return;
  for (const auto& F : Ops) {
    auto View = IREmit->ViewIR();
    const auto* Op = View.template GetOp<IROp_Header>(F.Node);
    FlushRule R;
    FlushRuleOf(Op, R);
    if (Daz && !Ftz) {   // the sources
      if (R.InElem != 4 && R.InElem != 8) continue;   // (a half-precision source is not flushed)
      IREmit->SetWriteCursorBefore(F.Node);
      Ref Seen[4] = {}, Made[4] = {};   // a value read twice by the op (`addps xmm0, xmm0`) is flushed once
      for (size_t i = 0; i < 4 && R.In[i] != 0xff; ++i) {
        Ref Src = View.GetNode(Op->Args[R.In[i]]), Flushed = nullptr;
        const auto SrcOp = View.template GetOp<IROp_Header>(Op->Args[R.In[i]])->Op;
        if (SrcOp == OP_VECTOR_FTOI || SrcOp == OP_VECTOR_FTOISIZED) continue;   // integral values (CVTPS2DQ's FRINT before its FCVTZS): no denormal
        for (size_t j = 0; j < i; ++j) if (Seen[j] == Src) Flushed = Made[j];
        if (!Flushed) {
          const auto Size = View.template GetOp<IROp_Header>(Op->Args[R.In[i]])->Size;
          Flushed = IREmit->_FexcFlush(Size, R.InElem == 4 ? OpSize::i32Bit : OpSize::i64Bit, Src, R.InScalar ? 1 : 0);
        }
        IREmit->ReplaceNodeArgument(F.Node, R.In[i], Flushed);
        Seen[i] = Src; Made[i] = Flushed;
      }
    } else if (R.Out == (Daz && Ftz ? 2 : Ftz ? 1 : 0) || (R.Out == 3 && Ftz && !Daz)) {   // the result (3: RCP's, flushed without a flag under FTZ alone)
      IREmit->SetWriteCursor(F.Node);
      Ref Flushed = IREmit->_FexcFlush(Op->Size, R.OutElem == 4 ? OpSize::i32Bit : OpSize::i64Bit, F.Node, (R.OutScalar ? 1 : 0) | (R.Out == 1 ? 2 : 0));
      IREmit->ReplaceUsesWithAfter(F.Node, Flushed, Flushed);
    }
  }
}
} // namespace FexcPrecision
