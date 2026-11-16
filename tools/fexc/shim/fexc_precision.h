// natemu fexc, profile slice1: shared predicates of the precision patches (slice-1 contract §1, patches 2-5).
// Included by patches/0006 into FEX's flag-elimination and register-allocation passes and by shim/fexc_sites.inc, after the
// FEX IR headers. Every use is guarded by fexc_slice1; the default profile never reaches this code.
#pragma once
#include <fexc_hook.h>
#include <FEXCore/Core/CoreState.h>
#include <FEXCore/Core/X86Enums.h>
#include "Interface/IR/RegisterAllocationData.h"
#include <algorithm>
#include <cstddef>
#include <initializer_list>
#include <set>
#include <type_traits>
#include <utility>
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
  return Op == FEXCore::IR::OP_FEXCSIMDCHECK || Op == FEXCore::IR::OP_FEXCX87CHECK || Op == FEXCore::IR::OP_FEXCSIMDCHECKDE ||
         Op == FEXCore::IR::OP_FEXCX87CHECKDE;
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
// ---------------------------------------------------------------------------------------------
// AFP fallback chunk 3b (plan rows 7, 8 and 10): without FEAT_AFP the host never sets IDC, and an inf*0 + QNaN FMA raises IOC. Only
// for the exceptions the guest has unmasked (`--simd-unmasked`, `--x87-unmasked`: DE for rows 7 and 8, IE for row 10) and only without
// AFP (`--feature afp=0`); with AFP, with the exception masked, or under DAZ (which drops DE from the unmasked set) nothing is placed.
// The tests are integer and quiet-compare vector ops on the operands of the Arm op, placed before it, so they cost the allocator
// nothing but its registers; none writes FPSR but a `fcmeq` of an SNaN lane, whose op raises IOC for it too.
//
// Row 7 (SSE/AVX): per op, a lane is a DE lane when an operand it examines is a denormal and none of them is a NaN (SDM Vol.1 11.5.2:
// no DE beside IE for a NaN, nor for a negative SQRT operand, an inf*0 product, ZE); the lanes' bits go to a GPR, 0 or nonzero, which the
// check takes as FPSR bit 7 (`FexcSimdCheckDe`). Several ops of one instruction (DPPS' multiplies, VCVTPS2PH ymm's halves) or'd. DPPS/DPPD's add
// levels (inputs: the products, then the sums, an exact denormal product has no UE) are tested in the JIT, inside VFAddV (FexcDenormalStep).
// Row 8 (x87): the memory operand of an m32/m64 FLD, arithmetic or FCOM/FCOMP is tested on its bits (a register holding a double
// denormal is an 80-bit normal), none when ST0 is a NaN, and for a memory dividend (FDIVR) none when ST0 is zero. A stack fault drops it
// in the check as it drops every flag (`FexcX87CheckDe`). Integer forms (FI*), FLD m80 and the register forms have no such operand.
// Nodes PlaceFmaSanitize made (its quiet compares are not the guest's compares: no DE test of their own). A global set, cleared at
// the start of each compile: it assumes compiles do not run concurrently (fexc compiles one request at a time, in one thread).
inline std::set<const void*> FexcMade;
struct DeRule {
  enum Kind : uint8_t { None, Bin, Div, Sqrt, Unary, Fma, Pair, Cadd } K = None;
  uint8_t Arg[3] = {0, 0, 0};   // the operands: Bin and Div 0 and 1, Unary and Sqrt 0, Fma the multiplicands 0 and 1 and the addend 2
  uint32_t E = 0;               // bytes of an operand element
  uint32_t B = 0;               // bytes of each operand the op examines: 4, 8 or 16
  bool Scalar = false;          // element 0 only (B == E)
  bool Upper = false;           // VFCVTL2: the upper 8 bytes of the operand
  bool Flip = false;            // Fma: the product and the addend are subtracted (a*b - c, -(a*b) + c)
};
inline bool DeRuleOf(const FEXCore::IR::IROp_Header* Op, DeRule& R) {
  using namespace FEXCore::IR;
  using std::initializer_list;
  const uint32_t E = OpSizeToSize(Op->ElementSize), S = OpSizeToSize(Op->Size);
  auto Set = [&](DeRule::Kind K, initializer_list<uint8_t> A, uint32_t Elem, uint32_t Bytes, bool Scalar) {
    size_t n = 0;
    for (auto a : A) R.Arg[n++] = a;
    R.K = K; R.E = Elem; R.B = Bytes; R.Scalar = Scalar;
  };
  switch (Op->Op) {
  case OP_VFADD: case OP_VFSUB: case OP_VFMUL: case OP_VFMIN: case OP_VFMAX:
  case OP_VFCMPEQ: case OP_VFCMPNEQ: case OP_VFCMPLT: case OP_VFCMPGT: case OP_VFCMPLE: case OP_VFCMPORD: case OP_VFCMPUNO:
    Set(DeRule::Bin, {0, 1}, E, S, S == E); break;
  case OP_VFDIV: Set(DeRule::Div, {0, 1}, E, S, S == E); break;
  case OP_VFADDP: Set(DeRule::Pair, {0, 1}, E, S, false); break;
  case OP_VFCADD: Set(DeRule::Cadd, {0, 1}, E, S, false); break;
  case OP_VFSQRT: Set(DeRule::Sqrt, {0}, E, S, S == E); break;
  case OP_VFMLA: case OP_VFMLS: case OP_VFNMLA: case OP_VFNMLS:
    Set(DeRule::Fma, {0, 1, 2}, E, S, S == E); R.Flip = Op->Op == OP_VFMLS || Op->Op == OP_VFNMLA; break;
  case OP_VFADDSCALARINSERT: case OP_VFSUBSCALARINSERT: case OP_VFMULSCALARINSERT: case OP_VFMINSCALARINSERT: case OP_VFMAXSCALARINSERT:
  case OP_VFCMPSCALARINSERT: Set(DeRule::Bin, {0, 1}, E, E, true); break;
  case OP_VFDIVSCALARINSERT: Set(DeRule::Div, {0, 1}, E, E, true); break;
  case OP_VFSQRTSCALARINSERT: Set(DeRule::Sqrt, {1}, E, E, true); break;
  case OP_VFMLASCALARINSERT: case OP_VFMLSSCALARINSERT: case OP_VFNMLASCALARINSERT: case OP_VFNMLSSCALARINSERT:
    Set(DeRule::Fma, {1, 2, 3}, E, E, true); R.Flip = Op->Op == OP_VFMLSSCALARINSERT || Op->Op == OP_VFNMLASCALARINSERT; break;
  case OP_VFTOFSCALARINSERT: {
    const uint32_t Src = OpSizeToSize(Op->C<IROp_VFToFScalarInsert>()->SrcElementSize);
    Set(DeRule::Unary, {1}, Src, Src, true); break;
  }
  case OP_FLOAT_FTOF: {
    const uint32_t Src = OpSizeToSize(Op->C<IROp_Float_FToF>()->SrcElementSize);
    Set(DeRule::Unary, {0}, Src, Src, true); break;
  }
  case OP_FCMP: {
    const uint32_t Elem = OpSizeToSize(Op->C<IROp_FCmp>()->ElementSize);
    Set(DeRule::Bin, {0, 1}, Elem, Elem, true); break;
  }
  case OP_VECTOR_FTOF: {   // widening reads the low half of the source (cvtps2pd), narrowing all of it (cvtpd2ps, vcvtps2ph)
    const uint32_t Src = OpSizeToSize(Op->C<IROp_Vector_FToF>()->SrcElementSize);
    const uint32_t Bytes = Src > E ? 16 : S / E * Src;
    if (Bytes != 8 && Bytes != 16) ERROR_AND_DIE_FMT("fexc slice1 (AFP fallback 3b): a vector convert of {} bytes", Bytes);
    Set(DeRule::Unary, {0}, Src, Bytes, false);
    break;
  }
  case OP_VFCVTL2: Set(DeRule::Unary, {0}, E >> 1, 8, false); R.Upper = true; break;
  case OP_VFCVTN2: Set(DeRule::Unary, {1}, E << 1, 16, false); break;
  default: return false;
  }
  if (R.E < 4) R.K = DeRule::None;   // a half-precision source (VCVTPH2PS) has no DE: x86 does not raise it
  return R.K != DeRule::None;
}
inline FEXCore::IR::OpSize OpSz(uint32_t Bytes) {
  using FEXCore::IR::OpSize;
  return Bytes == 4 ? OpSize::i32Bit : Bytes == 8 ? OpSize::i64Bit : OpSize::i128Bit;
}
// The vector ops of one test; the operands' ops are placed at the emitter's write cursor. `Wide`: a 128-bit register (all lanes
// examined; a 64-bit operand has its upper half zeroed first by `Canon`), else one scalar element (lane 0; the rest is not looked at).
template<class Emitter> struct SoftDe {
  using Ref = FEXCore::IR::Ref;
  using OpSize = FEXCore::IR::OpSize;
  Emitter* IR;
  uint32_t E, B;
  bool Scalar;
  OpSize Es, Int, Cmp;   // element size; size of the integer ops (a scalar one is a 64-bit op); size of the compare (exact: no other lane is read)
  SoftDe(Emitter* IREmit, uint32_t Elem, uint32_t Bytes, bool IsScalar)
    : IR(IREmit), E(Elem), B(Bytes), Scalar(IsScalar), Es(OpSz(Elem)), Int(IsScalar ? OpSize::i64Bit : OpSize::i128Bit), Cmp(IsScalar ? OpSz(Elem) : OpSize::i128Bit) {}
  Ref Canon(Ref X) { return Scalar || B == 16 ? X : IR->_VMov(OpSize::i64Bit, X); }
  struct Class { Ref Den, Zero, Exp; };
  // Denormal (exponent field zero, mantissa not), zero (either sign) and exponent field lanes of an operand
  Class Of(Ref X) {
    Ref T = IR->_VShlI(Int, Es, X, 1);   // the sign out
    Ref Zero = IR->_VCMPEQZ(Int, Es, T);
    Ref Exp = IR->_VUShrI(Int, Es, T, E == 4 ? 24 : 53);
    return {IR->_VAndn(Int, IR->_VCMPEQZ(Int, Es, Exp), Zero), Zero, Exp};
  }
  Ref NotNaN(Ref X) { return IR->_VFCMPEQ(Cmp, Es, X, X); }   // quiet: an SNaN lane raises IOC, as the op itself does
  Ref Or(Ref A, Ref C) { return IR->_VOr(Int, A, C); }
  Ref And(Ref A, Ref C) { return IR->_VAnd(Int, A, C); }
  Ref Andn(Ref A, Ref C) { return IR->_VAndn(Int, A, C); }   // A & ~C
  Ref Bin(Ref A, Ref C, bool Div = false) {
    const Class Ca = Of(A), Cb = Of(C);
    Ref D = Div ? Or(Andn(Ca.Den, Cb.Zero), Cb.Den) : Or(Ca.Den, Cb.Den);   // a denormal divided by zero is ZE
    return And(D, And(NotNaN(A), NotNaN(C)));
  }
  Ref Sqrt(Ref A) { return Andn(Of(A).Den, IR->_VCMPLTZ(Int, Es, A)); }   // a negative operand is IE
  Ref Fma(Ref A, Ref C, Ref Add, bool Flip) {
    // No DE beside IE: inf * 0, and an infinite product with an infinite addend of the opposite effective sign (inf - inf). A NaN lane
    // is out already (Nn), so an exponent field of all ones is an infinity here. Built operand by operand to keep few registers live
    // (an ymm FMADDSUB with IE and DE both unmasked spilled with all three classes at once).
    auto Ones = [&] { return E == 4 ? IR->_VectorImm(Int, Es, 0xff) : IR->_VDupFromGPR(Int, Es, IR->_Constant(0x7ff)); };   // (made where used: short-lived)
    const Class Ca = Of(A), Cb = Of(C);
    Ref Ia = IR->_VCMPEQ(Int, Es, Ca.Exp, Ones()), Ib = IR->_VCMPEQ(Int, Es, Cb.Exp, Ones());
    Ref Bad = Or(And(Ia, Cb.Zero), And(Ca.Zero, Ib));
    Ref Inf = Or(Ia, Ib);
    Ref Dn = Or(Ca.Den, Cb.Den);
    Ref Nn = And(NotNaN(A), NotNaN(C));
    const Class Cc = Of(Add);
    Dn = Or(Dn, Cc.Den);
    Nn = And(Nn, NotNaN(Add));
    Ref Ic = IR->_VCMPEQ(Int, Es, Cc.Exp, Ones());
    Ref Odd = IR->_VCMPLTZ(Int, Es, IR->_VXor(Int, IR->_VXor(Int, A, C), Add));   // an odd number of the three operands is negative
    if (Flip) Odd = IR->_VNot(Int, Odd);   // (the signs of product and addend differ: odd, or even for the subtracting forms)
    Ref Product = Andn(Inf, Bad);          // an infinite product: an infinity and a nonzero factor
    return Andn(And(Dn, Nn), Or(Bad, And(And(Product, Ic), Odd)));
  }
  // The lanes' mask as a GPR, nonzero (the low 32 bits) when some lane is set
  Ref Flag(Ref M) {
    if (!Scalar) M = IR->_VUMaxV(OpSize::i128Bit, OpSize::i32Bit, M);
    return IR->_VExtractToGPR(OpSize::i128Bit, OpSize::i32Bit, M, 0);
  }
};
// The test of the SSE/AVX op at `Node` (rule `R`): a GPR, 0 or nonzero, placed before the op.
template<class Emitter> inline FEXCore::IR::Ref BuildSimdDe(Emitter* IREmit, FEXCore::IR::Ref Node, const FEXCore::IR::IROp_Header* Op, const DeRule& R) {
  using namespace FEXCore::IR;
  auto View = IREmit->ViewIR();
  auto Arg = [&](int i) { return View.GetNode(Op->Args[R.Arg[i]]); };
  IREmit->SetWriteCursorBefore(Node);
  SoftDe<Emitter> D(IREmit, R.E, R.B, R.Scalar);
  Ref M = nullptr;
  switch (R.K) {
  case DeRule::Bin: M = D.Bin(D.Canon(Arg(0)), D.Canon(Arg(1))); break;
  case DeRule::Div: M = D.Bin(D.Canon(Arg(0)), D.Canon(Arg(1)), true); break;
  case DeRule::Sqrt: M = D.Sqrt(D.Canon(Arg(0))); break;
  case DeRule::Unary: {
    Ref X = Arg(0);
    if (R.Upper) X = IREmit->_VDupElement(OpSize::i128Bit, OpSize::i64Bit, X, 1);   // the upper half, down
    M = D.Of(D.Canon(X)).Den;
    break;
  }
  case DeRule::Fma: M = D.Fma(D.Canon(Arg(0)), D.Canon(Arg(1)), D.Canon(Arg(2)), R.Flip); break;
  case DeRule::Pair: {   // HADD/HSUB: the even and odd elements of the two sources are one op's operands
    if (R.B != 16) ERROR_AND_DIE_FMT("fexc slice1 (AFP fallback 3b): a pairwise op of {} bytes", R.B);
    Ref X = Arg(0), Y = Arg(1);
    M = D.Bin(IREmit->_VUnZip(OpSize::i128Bit, D.Es, X, Y), IREmit->_VUnZip2(OpSize::i128Bit, D.Es, X, Y));
    break;
  }
  case DeRule::Cadd: {   // ADDSUB: Arg(1) is the swapped operand (VRev64 or VExtr, both involutions): swap it back
    if (R.B != 16) ERROR_AND_DIE_FMT("fexc slice1 (AFP fallback 3b): an ADDSUB op of {} bytes", R.B);
    Ref Y = Arg(1);
    Y = R.E == 4 ? IREmit->_VRev64(OpSize::i128Bit, OpSize::i32Bit, Y) : IREmit->_VExtr(OpSize::i128Bit, OpSize::i8Bit, Y, Y, 8);
    M = D.Bin(Arg(0), Y);
    break;
  }
  default: return nullptr;
  }
  return D.Flag(M);
}
// x87 (row 8): the memory operand of an op is the load itself (FLD m64, arithmetic and FCOM m64: `LoadMem` into a vector register) or a
// single converted to a double from one (m32): its element size, or 0.
inline uint32_t X87MemBits(const FEXCore::IR::IRListView& View, FEXCore::IR::OrderedNodeWrapper Arg, FEXCore::IR::Ref& Bits) {
  using namespace FEXCore::IR;
  const auto Op = View.GetOp<IROp_Header>(Arg);
  auto Load = [&](OrderedNodeWrapper W, uint32_t& Size) {
    const auto L = View.GetOp<IROp_Header>(W);
    if (L->Op != OP_LOADMEM && L->Op != OP_LOADMEMTSO) return false;
    const auto Class = L->Op == OP_LOADMEM ? L->C<IROp_LoadMem>()->Class : L->C<IROp_LoadMemTSO>()->Class;
    Size = OpSizeToSize(L->Size);
    return Class == RegClass::FPR && (Size == 4 || Size == 8);
  };
  uint32_t Size = 0;
  if (Load(Arg, Size)) { Bits = View.GetNode(Arg); return Size; }
  if (Op->Op == OP_FLOAT_FTOF) {
    const auto F = Op->C<IROp_Float_FToF>();
    if (OpSizeToSize(F->SrcElementSize) == 4 && Load(F->Scalar, Size) && Size == 4) { Bits = View.GetNode(F->Scalar); return 4; }
  }
  return 0;
}
// The test of the x87 instruction's floating-point ops among `Ops` (all before its check): a GPR, 0 or nonzero, or null.
template<class Emitter> inline FEXCore::IR::Ref BuildX87De(Emitter* IREmit, const std::vector<std::pair<FEXCore::IR::Ref, FEXCore::IR::IROps>>& Ops, size_t Count) {
  using namespace FEXCore::IR;
  using Ref = FEXCore::IR::Ref;
  auto View = IREmit->ViewIR();
  struct Hit { Ref Node, Bits, Other; uint32_t Elem; bool Div; };
  std::vector<Hit> Hits;
  bool Arith = false;   // an arithmetic or compare op with a memory operand: the converting FLOAT_FTOF of an m32 is its operand, not an FLD
  for (size_t i = 0; i < Count; ++i) {
    const auto Op = View.template GetOp<IROp_Header>(Ops[i].first);
    switch (Op->Op) {
    case OP_VFADD: case OP_VFSUB: case OP_VFMUL: case OP_VFDIV: case OP_FCMP: {
      for (int m = 0; m < 2; ++m) {
        Ref Bits = nullptr;
        const uint32_t Elem = X87MemBits(View, Op->Args[m], Bits);
        if (!Elem) continue;
        const bool Const = View.template GetOp<IROp_Header>(Op->Args[1 - m])->Op == OP_VCASTFROMGPR;   // FLD m64's multiply by 1.0
        Hits.push_back({Ops[i].first, Bits, Const ? nullptr : View.GetNode(Op->Args[1 - m]), Elem, Op->Op == OP_VFDIV && m == 0});
        Arith |= !Const;
        break;
      }
      break;
    }
    default: break;
    }
  }
  for (size_t i = 0; i < Count; ++i) {   // FLD m32: the conversion alone
    const auto Op = View.template GetOp<IROp_Header>(Ops[i].first);
    Ref Bits = nullptr;
    if (!Arith && Op->Op == OP_FLOAT_FTOF && X87MemBits(View, Op->Args[0], Bits) == 4 && Hits.empty())
      Hits.push_back({Ops[i].first, Bits, nullptr, 4, false});
  }
  Ref Flag = nullptr;
  for (const auto& H : Hits) {
    IREmit->SetWriteCursorBefore(H.Node);
    SoftDe<Emitter> D(IREmit, H.Elem, H.Elem, true);
    Ref M = D.Of(H.Bits).Den;
    if (H.Other) {   // a NaN in ST0 (an integer test: this op's FPSR flags are the x87 wrapper's): no DE; a divide of a memory dividend by zero is ZE
      SoftDe<Emitter> Q(IREmit, 8, 8, true);
      Ref U = IREmit->_VUShrI(OpSize::i64Bit, OpSize::i64Bit, IREmit->_VShlI(OpSize::i64Bit, OpSize::i64Bit, H.Other, 1), 1);
      Ref NaN = IREmit->_VCMPGT(OpSize::i64Bit, OpSize::i64Bit, U, IREmit->_VCastFromGPR(OpSize::i64Bit, OpSize::i64Bit, IREmit->_Constant(0x7ff0'0000'0000'0000ULL)));
      M = D.Andn(M, NaN);
      if (H.Div) M = D.Andn(M, Q.Of(H.Other).Zero);
    }
    Ref F = D.Flag(M);
    Flag = Flag ? IREmit->_Or(OpSize::i32Bit, Flag, F) : F;
  }
  return Flag;
}
// The ops that commit an instruction's effect: everything with a side effect but the NZCV writers (the incoming NZCV is saved
// for a fault point after one, patch 0006), instruction markers, loads, prefetches and FEX's rounding-mode bookkeeping.
inline bool Commits(FEXCore::IR::IROps Op) {
  using namespace FEXCore::IR;
  switch (Op) {
  case OP_GUESTOPCODE: case OP_INVALIDATEFLAGS: case OP_INLINECONSTANT: case OP_INLINEENTRYPOINTOFFSET: case OP_PREFETCH:
  case OP_LOADCONTEXTPAIR: case OP_LOADMEMPAIR: case OP_VLOADNONTEMPORAL: case OP_DIV: case OP_UDIV: case OP_FCMP: case OP_STORENZCV:
  case OP_SETROUNDINGMODE: case OP_PUSHROUNDINGMODE: case OP_POPROUNDINGMODE: case OP_FEXCSIMDCHECK: case OP_FEXCX87CHECK:
  case OP_FEXCSIMDCHECKDE: case OP_FEXCX87CHECKDE: case OP_FEXCKEEP:
  case OP_ADDWITHFLAGS: case OP_ADDNZCV: case OP_SETSMALLNZV: case OP_CARRYINVERT: case OP_AXFLAG: case OP_RMIFNZCV: case OP_CONDADDNZCV:
  case OP_CONDSUBNZCV: case OP_ADCWITHFLAGS: case OP_ADCZEROWITHFLAGS: case OP_SBBWITHFLAGS: case OP_ADCNZCV: case OP_SBBNZCV:
  case OP_SUBWITHFLAGS: case OP_CMPPAIRZ: case OP_SUBNZCV: case OP_ANDWITHFLAGS: case OP_TESTNZ: case OP_TESTZ: case OP_SHIFTFLAGS:
  case OP_ROTATEFLAGS: case OP_FEXCFLUSH: return false;
  default: return HasSideEffects(Op);
  }
}
// AFP fallback, decision C (the unmasked case): without FEAT_AFP Arm judges tininess before rounding, x86 after (inventory 1.8), so a
// result that rounds up to +-MIN_NORMAL raises UFC and a spurious #XM. The resident decides exactly from the operands (crates/abi
// `tiny`), which it reads from the registers at the check's stub; FexcKeep ops before the check keep them live. `TinyDescOf` says for one
// op whether it can raise a UFC the resident could not judge (returns false: an add, a subtract, a half-precision narrowing... the
// instruction then keeps today's behaviour) and describes the ones it can: meta (crates/abi `tiny`) and the operands by position.
struct TinyDesc { uint8_t Meta = 0; uint8_t Arg[3] {}; const FEXCore::IR::IROp_Header* Op = nullptr; };   // Arg: indices into Op->Args
inline bool TinyDescOf(const FEXCore::IR::IROp_Header* Op, TinyDesc& D) {
  using namespace FEXCore::IR;
  D.Op = Op;
  constexpr uint8_t Mul = 1, Div = 2, Fma = 3, Cvt = 4, Elem8 = 1 << 3, Scalar = 1 << 4, NegP = 1 << 6, NegA = 1 << 7;
  const uint32_t Size = OpSizeToSize(Op->Size), E = OpSizeToSize(Op->ElementSize);
  auto Bin = [&](uint8_t Kind, bool Sc, uint8_t Neg = 0, size_t First = 0) {
    if (E != 4 && E != 8) return false;
    if (!Sc && Size != 16) return false;
    D.Meta = Kind | (E == 8 ? Elem8 : 0) | (Sc ? Scalar : 0) | Neg;
    const size_t N = Kind == Fma ? 3 : 2;
    for (size_t i = 0; i < N; ++i) D.Arg[i] = uint8_t(First + i);
    return true;
  };
  switch (Op->Op) {
  case OP_VFMUL: return Bin(Mul, false);
  case OP_VFDIV: return Bin(Div, false);
  case OP_VFMLA: return Bin(Fma, false);
  case OP_VFMLS: return Bin(Fma, false, NegA);
  case OP_VFNMLA: return Bin(Fma, false, NegP);
  case OP_VFNMLS: return Bin(Fma, false, NegP | NegA);
  case OP_VFMULSCALARINSERT: return Bin(Mul, true);
  case OP_VFDIVSCALARINSERT: return Bin(Div, true);
  case OP_VFMLASCALARINSERT: return Bin(Fma, true, 0, 1);
  case OP_VFMLSSCALARINSERT: return Bin(Fma, true, NegA, 1);
  case OP_VFNMLASCALARINSERT: return Bin(Fma, true, NegP, 1);
  case OP_VFNMLSSCALARINSERT: return Bin(Fma, true, NegP | NegA, 1);
  case OP_VFTOFSCALARINSERT: {   // Vector1 holds the upper elements, Vector2 the source
    const uint32_t Src = OpSizeToSize(Op->C<IROp_VFToFScalarInsert>()->SrcElementSize);
    if (E >= Src) { D.Meta = 0; return true; }   // widening or same size: no underflow
    if (!(E == 4 && Src == 8)) return false;
    D.Meta = Cvt | Elem8 | Scalar; D.Arg[0] = 1;
    return true;
  }
  case OP_FLOAT_FTOF: {
    const uint32_t Src = OpSizeToSize(Op->C<IROp_Float_FToF>()->SrcElementSize), Dst = Size;
    if (Dst >= Src) { D.Meta = 0; return true; }
    if (!(Dst == 4 && Src == 8)) return false;
    D.Meta = Cvt | Elem8 | Scalar; D.Arg[0] = 0;
    return true;
  }
  case OP_VECTOR_FTOF: {
    const uint32_t Src = OpSizeToSize(Op->C<IROp_Vector_FToF>()->SrcElementSize);
    if (E >= Src) { D.Meta = 0; return true; }
    if (!(E == 4 && Src == 8 && Size == 16)) return false;
    D.Meta = Cvt | Elem8; D.Arg[0] = 0;
    return true;
  }
  case OP_VFADD: case OP_VFSUB: case OP_VFADDP: case OP_VFCADD: case OP_VFADDV: case OP_VFADDSCALARINSERT: case OP_VFSUBSCALARINSERT:
  case OP_VFCVTN2: return false;   // exact sums (a tiny sum is a denormal: the shim's exact-tiny test) and VCVTPS2PH's halves
  default: D.Meta = 0; return true;
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
      // AFP fallback chunk 3b (rows 7 and 8): without AFP and with DE unmasked, the software denormal tests of the instruction's ops,
      // placed before each op (SIMD: `Des`, the op's index and its flag) or before the x87 check (`X87De`).
      std::vector<std::pair<size_t, Ref>> Des;
      Ref X87De = nullptr;
      if (fexc_no_afp) {
        if (!X87 && (uint64_t(fexc_simd_unmasked) >> 1 & 1)) {
          for (size_t i = 0; i < First; ++i) {
            const auto* Op = IR.template GetOp<IROp_Header>(Insn[i].first);
            DeRule R;
            if (!FexcMade.count(Insn[i].first) && DeRuleOf(Op, R))
              if (Ref F = BuildSimdDe(IREmit, Insn[i].first, Op, R)) Des.push_back({i, F});
          }
        } else if (X87 && (uint64_t(fexc_x87_unmasked) >> 1 & 1)) {
          X87De = BuildX87De(IREmit, Insn, First);
        }
      }
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
        if (X87De) IREmit->_FexcX87CheckDe(F, O, I, T, X87De, Mask, Store);
        else IREmit->_FexcX87Check(F, O, I, T, Mask, Store);
      } else {
        fexc_simd_checked.insert(Rip);
        std::vector<size_t> Steps;   // DPPS/DPPD: every multiply and add step is checked in its order (VDPPS ymm: each half in turn)
        if (Dpp) {
          size_t Last = First;
          for (size_t i = 0; i < First; ++i) if (SimdFPOp(Insn[i].second)) Last = i;
          for (size_t i = 0; i < Last; ++i) if (SimdFPOp(Insn[i].second)) Steps.push_back(i);
        }
        // a check takes the software flags of the ops after the previous check up to its own point
        auto Check = [&](long After, long Upto) {
          // AFP fallback, decision C: the operands of the multiplies, divides, FMAs and narrowing converts after the previous check, kept live
          // for the resident (at most two ops, the halves of a 256-bit one, all alike: else the instruction is not described)
          if (fexc_no_afp && (uint64_t(fexc_simd_unmasked) >> 4 & 1)) {
            TinyDesc Ds[2];
            size_t N = 0;
            bool Ok = true;
            for (long i = After + 1; i <= Upto && i < long(First) && Ok; ++i) {
              TinyDesc D;
              Ok = TinyDescOf(IR.template GetOp<IROp_Header>(Insn[i].first), D);
              if (Ok && D.Meta) { if (N == 2) Ok = false; else Ds[N++] = D; }
            }
            if (Ok && N == 2 && Ds[0].Meta != Ds[1].Meta) Ok = false;
            if (Ok && N)
              for (size_t h = 0; h < N; ++h)
                for (size_t k = 0; k < ((Ds[h].Meta & 7) == 3 ? 3u : (Ds[h].Meta & 7) == 4 ? 1u : 2u); ++k)
                  IREmit->_FexcKeep(IR.GetNode(Ds[h].Op->Args[Ds[h].Arg[k]]), uint32_t(Ds[h].Meta) | uint32_t(k) << 8 | uint32_t(h) << 10);
          }
          Ref De = nullptr;
          for (const auto& D : Des)
            if (long(D.first) > After && long(D.first) <= Upto) De = De ? IREmit->_Or(OpSize::i32Bit, De, D.second) : D.second;
          if (De) IREmit->_FexcSimdCheckDe(Mask, De);
          else IREmit->_FexcSimdCheck(Mask);
        };
        Check(Steps.empty() ? -1 : long(Steps.back()), long(Insn.size()));
        long Prev = -1;
        for (size_t i : Steps) {
          // a step's check follows its flush (PlaceFlushes ran first): the flush's UFC/IXC belong to this step, not the next
          Ref At = Insn[i].first;
          for (size_t j = i + 1; j < Insn.size() && Insn[j].second == OP_FEXCFLUSH &&
                                 IR.GetNode(IR.template GetOp<IROp_Header>(Insn[j].first)->Args[0]) == At; ++j)
            At = Insn[j].first;
          IREmit->SetWriteCursor(At);
          Check(Prev, long(i));
          Prev = long(i);
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
// AFP fallback chunk 3b, row 10: with IE unmasked and no AFP, an FMA whose multiplicands are inf and 0 and whose addend is a NaN raises a
// spurious IOC (x86 returns the QNaN, no IE: SDM Vol.1 Table 14-17), so a #XM. Under the unmasked IE (`--simd-unmasked` bit 0) the
// multiplicands of each lane whose addend is a NaN are replaced by zero unless they are NaNs themselves (an SNaN multiplicand still
// raises IOC): per multiplicand a quiet `fcmeq` of it and of the addend, `bic`, `bic`; the product is then 0, and the result NaN
// is the addend's or a multiplicand's as before. 7 vector ops per FMA; the compares' nodes are `FexcMade` (no DE test of their own).
template<class Emitter> inline void PlaceFmaSanitize(Emitter* IREmit) {
  using namespace FEXCore::IR;
  using Ref = FEXCore::IR::Ref;
  if (!fexc_no_afp) return;
  FexcMade.clear();   // (node addresses are reused from one compile to the next)
  auto IR = IREmit->ViewIR();
  const uint64_t Rip0 = IR.GetHeader()->OriginalRIP;
  std::vector<Ref> Ops;
  uint64_t Rip = ~0ull;
  for (auto [BlockNode, BlockHeader] : IR.GetBlocks()) {
    for (auto [CodeNode, IROp] : IR.GetCode(BlockNode)) {
      if (IROp->Op == OP_GUESTOPCODE) { Rip = Rip0 + IROp->template C<IROp_GuestOpcode>()->GuestEntryOffset; continue; }
      switch (IROp->Op) {
      case OP_VFMLA: case OP_VFMLS: case OP_VFNMLA: case OP_VFNMLS:
      case OP_VFMLASCALARINSERT: case OP_VFMLSSCALARINSERT: case OP_VFNMLASCALARINSERT: case OP_VFNMLSSCALARINSERT:
        if (Rip != ~0ull && !fexc_x87.count(Rip)) Ops.push_back(CodeNode);
        break;
      default: break;
      }
    }
  }
  if (Ops.empty() || !(uint64_t(fexc_simd_unmasked) & 1)) return;
  for (Ref Node : Ops) {
    auto View = IREmit->ViewIR();
    const auto* Op = View.template GetOp<IROp_Header>(Node);
    const bool Insert = Op->Op == OP_VFMLASCALARINSERT || Op->Op == OP_VFMLSSCALARINSERT || Op->Op == OP_VFNMLASCALARINSERT || Op->Op == OP_VFNMLSSCALARINSERT;
    const uint32_t E = OpSizeToSize(Op->ElementSize), S = OpSizeToSize(Op->Size);
    const bool Scalar = Insert || S == E;
    if (!Scalar && S != 16) ERROR_AND_DIE_FMT("fexc slice1 (AFP fallback 3b): an FMA of {} bytes", S);
    const uint8_t First = Insert ? 1 : 0;   // the multiplicands' arguments (the scalar forms' argument 0 only supplies the upper elements)
    const OpSize Es = OpSz(E), Int = Scalar ? OpSize::i64Bit : OpSize::i128Bit, Cmp = Scalar ? Es : OpSize::i128Bit;
    IREmit->SetWriteCursorBefore(Node);
    Ref A = View.GetNode(Op->Args[First]), B = View.GetNode(Op->Args[First + 1]), C = View.GetNode(Op->Args[First + 2]);
    Ref Cok = IREmit->_VFCMPEQ(Cmp, Es, C, C), Aok = IREmit->_VFCMPEQ(Cmp, Es, A, A), Bok = IREmit->_VFCMPEQ(Cmp, Es, B, B);
    FexcMade.insert(Cok); FexcMade.insert(Aok); FexcMade.insert(Bok);
    Ref A2 = IREmit->_VAndn(Int, A, IREmit->_VAndn(Int, Aok, Cok));
    Ref B2 = IREmit->_VAndn(Int, B, IREmit->_VAndn(Int, Bok, Cok));
    IREmit->ReplaceNodeArgument(Node, First, A2);
    IREmit->ReplaceNodeArgument(Node, First + 1, B2);
  }
}
// ---------------------------------------------------------------------------------------------
// Without FEAT_AFP (FPCR.NEP) an Arm scalar FP op zeroes its register above the element, so FEX computes x86's scalar op (which
// keeps the destination's other bits: ADDSD/ADDSS and the other scalar SSE/AVX forms) into a temporary and inserts it (`ins`), an
// extra FP/SIMD op per scalar op. When the bits above the element of the value that supplies them (argument 0: Vector1, an FMA's
// Upper, VSToFGPRInsert's Vector) are known zero, the scalar op writes its register directly: the same 128 bits. `UpperZeroOps`
// holds those ops (the JIT's VFScalarOperation, VFScalarUnaryOperation and VFScalarFMAOperation read it). Known zero is decided on
// the IR before register allocation (the JIT sees registers only), conservatively: per value, the lowest byte from which its
// register's bits are known zero (16: none), from its definition (UpperZeroFrom); a static register read (LoadRegister) has the
// value stored to it earlier in the same code block, and nothing known otherwise (a value from before the block). The resident's
// x87 service inside a block (FexcX87Svc) changes no XMM register; FexcStringSvc writes scratch registers only and needs no entry.
// Cleared at each compile, as FexcMade; keyed by node pointer, so it assumes one compile at a time (fexc is single-threaded) and
// that each run of this pass is followed by its own JIT.
inline std::set<const void*> UpperZeroOps;
// `Z(arg)`: that byte for an argument already seen; `Stored`: the values the code block stored to the XMM static registers so far.
template<class ZFn> inline uint32_t UpperZeroFrom(const FEXCore::IR::IROp_Header* Op, ZFn Z, FEXCore::IR::Ref const* Stored) {
  using namespace FEXCore::IR;
  const uint32_t Size = OpSizeToSize(Op->Size), E = OpSizeToSize(Op->ElementSize);
  // a 128-bit op replacing the low `Elem` bytes of argument 0 and keeping the rest (256-bit ops are split without SVE256)
  auto Through = [&](uint32_t Elem) { return Size == 16 ? std::max(Elem, Z(Op->Args[0])) : 16u; };
  auto Vec = [&](uint32_t F) { return Size == 16 ? F : Size < 16 ? std::min(8u, F) : 16u; };   // (under 16 bytes: `.8b`, the rest zeroed)
  switch (Op->Op) {
  // ldr b/h/s/d: MOVSS/MOVSD/MOVD/MOVQ xmm, m (FEX stores the node as the whole register: it relies on the zero bits already)
  case OP_LOADMEM: return Op->C<IROp_LoadMem>()->Class == RegClass::FPR ? Size : 16;
  case OP_LOADMEMTSO: return Op->C<IROp_LoadMemTSO>()->Class == RegClass::FPR ? Size : 16;
  case OP_LOADNAMEDVECTORCONSTANT: return Op->C<IROp_LoadNamedVectorConstant>()->Constant == NamedVectorConstant::NAMED_VECTOR_ZERO ? 0 : 16;   // XORPS x, x
  case OP_VCASTFROMGPR: return E;   // fmov s/d from a GPR (uxtb/uxth first): MOVD/MOVQ xmm, r
  case OP_VMOV: return Size < 16 ? Size : Size == 16 ? Z(Op->Args[0]) : 16;   // under 16 bytes zero-extending (MOVQ xmm, xmm), else a copy
  case OP_VINSELEMENT: return Op->C<IROp_VInsElement>()->DestIdx == 0 ? Through(E) : 16;   // MOVSS/MOVSD xmm, xmm (argument 0: DestVector)
  case OP_FEXCFLUSH: return Z(Op->Args[0]);   // (it only clears bits: `bic`)
  // bitwise (XORPD/ANDPD/ANDNPD with a mask: negation, fabs)
  case OP_VAND: return Vec(std::min(Z(Op->Args[0]), Z(Op->Args[1])));
  case OP_VOR: case OP_VXOR: return Vec(std::max(Z(Op->Args[0]), Z(Op->Args[1])));
  case OP_VANDN: return Vec(Z(Op->Args[0]));   // Vector1 & ~Vector2
  case OP_LOADREGISTER: {
    const auto* L = Op->C<IROp_LoadRegister>();
    return L->Class == RegClass::FPR && L->Reg < 16 && Stored[L->Reg] ? Z(Stored[L->Reg]) : 16;
  }
  case OP_VFADDSCALARINSERT: case OP_VFSUBSCALARINSERT: case OP_VFMULSCALARINSERT: case OP_VFDIVSCALARINSERT:
  case OP_VFMINSCALARINSERT: case OP_VFMAXSCALARINSERT: case OP_VFSQRTSCALARINSERT: case OP_VFRSQRTSCALARINSERT:
  case OP_VFRECPSCALARINSERT: case OP_VFTOFSCALARINSERT: case OP_VFTOISCALARINSERT: case OP_VFCMPSCALARINSERT:
  case OP_VSTOFGPRINSERT: case OP_VFMLASCALARINSERT: case OP_VFMLSSCALARINSERT: case OP_VFNMLASCALARINSERT:
  case OP_VFNMLSSCALARINSERT:
    return Through(E);
  case OP_VSTOFVECTORINSERT: return Through(Op->C<IROp_VSToFVectorInsert>()->HasTwoElements ? 8 : E);   // (CVTPI2PS: two elements)
  default: return 16;
  }
}
// Ops with side effects that write no XMM static register (any other, a call or a service, ends what MarkUpperZero knows of them).
inline bool KeepsStaticFPRs(FEXCore::IR::IROps Op) {
  using namespace FEXCore::IR;
  switch (Op) {
  case OP_DUMMY: case OP_INVALIDATEFLAGS: case OP_GUESTOPCODE: case OP_SETROUNDINGMODE: case OP_PUSHROUNDINGMODE:
  case OP_POPROUNDINGMODE: case OP_STOREPF: case OP_STOREAF: case OP_LOADCONTEXTPAIR: case OP_STORECONTEXT: case OP_STORECONTEXTPAIR:
  case OP_STORECONTEXTINDEXED: case OP_STORENZCV: case OP_LOADMEMPAIR: case OP_STOREMEM: case OP_STOREMEMPAIR: case OP_STOREMEMTSO:
  case OP_VSTOREVECTORMASKED: case OP_VSTOREVECTORELEMENT: case OP_PUSH: case OP_PUSHTWO: case OP_POP: case OP_POPTWO:
  case OP_FENCE: case OP_PREFETCH: case OP_VSTORENONTEMPORAL: case OP_VSTORENONTEMPORALPAIR: case OP_VLOADNONTEMPORAL:
  case OP_CAS: case OP_CASPAIR: case OP_ATOMICSWAP: case OP_ATOMICFETCHADD: case OP_ATOMICFETCHSUB: case OP_ATOMICFETCHAND:
  case OP_ATOMICFETCHCLR: case OP_ATOMICFETCHOR: case OP_ATOMICFETCHXOR: case OP_ATOMICFETCHNEG: case OP_INLINEENTRYPOINTOFFSET:
  case OP_INLINECONSTANT: case OP_ADDWITHFLAGS: case OP_ADDNZCV: case OP_SETSMALLNZV: case OP_CARRYINVERT: case OP_AXFLAG:
  case OP_RMIFNZCV: case OP_CONDADDNZCV: case OP_CONDSUBNZCV: case OP_ADCWITHFLAGS: case OP_ADCZEROWITHFLAGS: case OP_SBBWITHFLAGS:
  case OP_ADCNZCV: case OP_SBBNZCV: case OP_SUBWITHFLAGS: case OP_CMPPAIRZ: case OP_SUBNZCV: case OP_ANDWITHFLAGS: case OP_TESTNZ:
  case OP_TESTZ: case OP_SHIFTFLAGS: case OP_ROTATEFLAGS: case OP_FCMP: case OP_FEXCSIMDCHECK: case OP_FEXCX87CHECK:
  case OP_FEXCSIMDCHECKDE: case OP_FEXCX87CHECKDE: case OP_FEXCKEEP: case OP_FEXCFLUSH: case OP_FEXCMXCSR: case OP_FEXCREPSTOS: case OP_FEXCX87SVC:
    return true;
  default: return false;
  }
}
// The scalar ops the JIT may write directly (run last before register allocation: after the flushes and the FMA sanitizing).
template<class Emitter> inline void MarkUpperZero(Emitter* IREmit) {
  using namespace FEXCore::IR;
  UpperZeroOps.clear();
  if (!fexc_no_afp) return;
  auto View = IREmit->ViewIR();
  std::vector<uint8_t> From(View.GetSSACount(), 16);
  auto Z = [&](auto Node) -> uint32_t {
    const uint32_t Id = [&] { if constexpr (std::is_same_v<decltype(Node), Ref>) return View.GetID(Node).Value; else return Node.ID().Value; }();
    return Id < From.size() ? From[Id] : 16;
  };
  for (auto [BlockNode, BlockHeader] : View.GetBlocks()) {
    Ref Stored[16] = {};
    for (auto [CodeNode, IROp] : View.GetCode(BlockNode)) {
      if (IROp->Op == OP_STOREREGISTER) {
        const PhysicalRegister R(CodeNode);
        if (R.AsRegClass() == RegClass::FPRFixed && R.Reg < 16) {
          Stored[R.Reg] = OpSizeToSize(IROp->Size) == 16 ? View.GetNode(IROp->Args[0]) : nullptr;
        }
        continue;
      }
      if (HasSideEffects(IROp->Op) && !KeepsStaticFPRs(IROp->Op)) std::fill(std::begin(Stored), std::end(Stored), nullptr);
      const uint32_t F = UpperZeroFrom(IROp, Z, Stored);
      From[View.GetID(CodeNode).Value] = uint8_t(F);
      switch (IROp->Op) {
      case OP_VFCMPSCALARINSERT: {   // UNO, NEQ and ORD build their result in VTMP1 and insert it into Dst themselves: never direct
        const auto C = IROp->template C<IROp_VFCMPScalarInsert>()->Op;
        if (C != FloatCompareOp::EQ && C != FloatCompareOp::LT && C != FloatCompareOp::LE) continue;
        break;
      }
      case OP_VSTOFVECTORINSERT: if (IROp->template C<IROp_VSToFVectorInsert>()->HasTwoElements) continue; break;   // (not a scalar op)
      case OP_VFADDSCALARINSERT: case OP_VFSUBSCALARINSERT: case OP_VFMULSCALARINSERT: case OP_VFDIVSCALARINSERT:
      case OP_VFMINSCALARINSERT: case OP_VFMAXSCALARINSERT: case OP_VFSQRTSCALARINSERT: case OP_VFRSQRTSCALARINSERT:
      case OP_VFRECPSCALARINSERT: case OP_VFTOFSCALARINSERT: case OP_VFTOISCALARINSERT: case OP_VSTOFGPRINSERT:
      case OP_VFMLASCALARINSERT: case OP_VFMLSSCALARINSERT: case OP_VFNMLASCALARINSERT: case OP_VFNMLSSCALARINSERT: break;
      default: continue;
      }
      if (OpSizeToSize(IROp->Size) == 16 && Z(IROp->Args[0]) <= OpSizeToSize(IROp->ElementSize)) UpperZeroOps.insert(CodeNode);
    }
  }
}
} // namespace FexcPrecision
