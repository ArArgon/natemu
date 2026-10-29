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
