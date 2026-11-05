// natemu fexc, profile slice1: shared predicates of the precision patches (slice-1 contract §1, patches 2-5).
// Included by patches/0006 into FEX's flag-elimination and register-allocation passes and by shim/fexc_sites.inc, after the
// FEX IR headers. Every use is guarded by fexc_slice1; the default profile never reaches this code.
#pragma once
#include <fexc_hook.h>
#include <FEXCore/Core/CoreState.h>
#include <FEXCore/Core/X86Enums.h>
#include <cstddef>
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
  case OP_ROTATEFLAGS: return false;
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
        const uint64_t M = fexc_simd_unmasked;
        constexpr uint32_t Arm[6] = {1, 1u << 7, 2, 4, 8, 16};
        for (int b = 0; b < 6; ++b) if (M >> b & 1) Mask |= Arm[b];
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
            IREmit->SetWriteCursor(Insn[i].first);
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
} // namespace FexcPrecision
