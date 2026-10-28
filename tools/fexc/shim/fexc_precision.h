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
  default: return false;
  }
}
// Ops at which a slice1 guest fault is raised with the state before the instruction: guest memory accesses, and (S1f) the
// divide ops, right before which the #DE check branches to its stub. The incoming flags must be live there.
inline bool FaultPoint(FEXCore::IR::IROps Op) {
  return GuestMemOp(Op) || Op == FEXCore::IR::OP_DIV || Op == FEXCore::IR::OP_UDIV;
}
// Frame slot of the instruction's incoming NZCV (patch 5): FEX's own home for guest NZCV outside the JIT,
// CPUState.flags[RFLAG_NZCV_LOC] (32 bits, the PSTATE bit layout; see Core.cpp::ReconstructCompactedEFLAGS).
inline constexpr uint32_t PreNzcvOffset =
  uint32_t(offsetof(FEXCore::Core::CpuStateFrame, State) + offsetof(FEXCore::Core::CPUState, flags) + FEXCore::X86State::RFLAG_NZCV_LOC);

// Profile slice1 (address guard, branch-target check): called at the start of register allocation, while operands are
// still SSA nodes. Records each guest load/store whose base is an EntrypointOffset (a RIP-relative operand: its address
// is a compile-time constant), each store that uses its instruction's load address (the same SSA values: the store of a
// read-modify-write needs no second guard), and for each ExitFunction with a computed RIP the node after which the JIT checks it: the
// target's definition when that lies inside the exit's guest instruction (after its GuestOpcode), else the GuestOpcode.
inline void Prescan(const FEXCore::IR::IRListView& IR) {
  using namespace FEXCore::IR;
  fexc_rip_relative.clear();
  fexc_target_check.clear();
  fexc_rmw_same_address.clear();
  const IROp_LoadMem* Load = nullptr;   // the instruction's last plain load
  auto SameOffset = [&](OrderedNodeWrapper A, OrderedNodeWrapper B) {
    if (A == B) return true;
    if (A.IsInvalid() || B.IsInvalid()) return false;
    const auto a = IR.GetOp<IROp_Header>(A), b = IR.GetOp<IROp_Header>(B);
    return a->Op == OP_INLINECONSTANT && b->Op == OP_INLINECONSTANT &&
           a->C<IROp_InlineConstant>()->Constant == b->C<IROp_InlineConstant>()->Constant;
  };
  std::vector<uint32_t> Owner(IR.GetSSACount(), ~0u);   // node ID -> GuestOpcode node ID in force
  uint32_t Guest = ~0u;
  for (auto [BlockNode, BlockHeader] : IR.GetBlocks()) {
    for (auto [CodeNode, IROp] : IR.GetCode(BlockNode)) {
      const uint32_t ID = IR.GetID(CodeNode).Value;
      if (IROp->Op == OP_GUESTOPCODE) {
        Guest = ID;
        Load = nullptr;
      }
      Owner[ID] = Guest;
      if (IROp->Op == OP_LOADMEM) Load = IROp->C<IROp_LoadMem>();
      if (IROp->Op == OP_STOREMEM && Load) {
        const auto St = IROp->C<IROp_StoreMem>();
        if (St->Addr == Load->Addr && SameOffset(St->Offset, Load->Offset) && St->OffsetType == Load->OffsetType &&
            St->OffsetScale == Load->OffsetScale) {
          fexc_rmw_same_address.insert(ID);
        }
      }
      OrderedNodeWrapper Addr = OrderedNodeWrapper::WrapOffset(0);
      switch (IROp->Op) {
      case OP_LOADMEM: Addr = IROp->C<IROp_LoadMem>()->Addr; break;
      case OP_LOADMEMTSO: Addr = IROp->C<IROp_LoadMemTSO>()->Addr; break;
      case OP_STOREMEM: Addr = IROp->C<IROp_StoreMem>()->Addr; break;
      case OP_STOREMEMTSO: Addr = IROp->C<IROp_StoreMemTSO>()->Addr; break;
      case OP_LOADMEMPAIR: Addr = IROp->C<IROp_LoadMemPair>()->Addr; break;
      case OP_STOREMEMPAIR: Addr = IROp->C<IROp_StoreMemPair>()->Addr; break;
      case OP_ATOMICSWAP: Addr = IROp->C<IROp_AtomicSwap>()->Addr; break;
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
} // namespace FexcPrecision
