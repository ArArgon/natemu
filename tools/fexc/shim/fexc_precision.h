// natemu fexc, profile slice1: shared predicates of the precision patches (slice-1 contract §1, patches 2-5).
// Included by patches/0006 into FEX's flag-elimination and register-allocation passes and by shim/fexc_sites.inc, after the
// FEX IR headers. Every use is guarded by fexc_slice1; the default profile never reaches this code.
#pragma once
#include <fexc_hook.h>
#include <FEXCore/Core/CoreState.h>
#include <FEXCore/Core/X86Enums.h>
#include <cstddef>

namespace FexcPrecision {
// IR ops that access guest memory and are recorded as sites (PushTwo/PopTwo only arise post-RA, and not under slice1).
inline bool GuestMemOp(FEXCore::IR::IROps Op) {
  using namespace FEXCore::IR;
  switch (Op) {
  case OP_LOADMEM: case OP_LOADMEMTSO: case OP_STOREMEM: case OP_STOREMEMTSO: case OP_LOADMEMPAIR: case OP_STOREMEMPAIR:
  case OP_PUSH: case OP_PUSHTWO: case OP_POP: case OP_POPTWO: return true;
  default: return false;
  }
}
// Frame slot of the instruction's incoming NZCV (patch 5): FEX's own home for guest NZCV outside the JIT,
// CPUState.flags[RFLAG_NZCV_LOC] (32 bits, the PSTATE bit layout; see Core.cpp::ReconstructCompactedEFLAGS).
inline constexpr uint32_t PreNzcvOffset =
  uint32_t(offsetof(FEXCore::Core::CpuStateFrame, State) + offsetof(FEXCore::Core::CPUState, flags) + FEXCore::X86State::RFLAG_NZCV_LOC);
} // namespace FexcPrecision
