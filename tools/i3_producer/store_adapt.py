"""Add an independently allocated current-state map at the actual vector store."""
import json
from pathlib import Path
import sys
root=Path(sys.argv[1])
def edit(path,old,new,count=1):
 p=root/path;s=p.read_text();assert s.count(old)==count,(path,old,s.count(old));p.write_text(s.replace(old,new))
front='FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp'
edit(front,'Src, GuestOffset, uint32_t(CFInverted), uint32_t(NativeFPSR));','Src, GuestOffset, 0, uint32_t(CFInverted), uint32_t(NativeFPSR));')
p=root/'FEXCore/Source/Interface/IR/IR.json';schema=json.loads(p.read_text());misc=schema['Ops']['Misc'];key=next(k for k in misc if k.startswith('NatemuRecoveryUse '));value=misc.pop(key);misc[key.replace('u32:$GuestEntryOffset,','u32:$GuestEntryOffset, u32:$StoreSite,')]=value;p.write_text(json.dumps(schema,indent=2)+'\n')
vector='FEXCore/Source/Interface/Core/OpcodeDispatcher/Vector.cpp'
args=', '.join(f'Roots[{i}]' for i in range(35))
hook='''  if (std::getenv("NATEMU_CHECKPOINT_ROOT_PREP")) {
    static constexpr uint8_t Store[] = {0xf3, 0x0f, 0x7f, 0x07};
    const bool NativeFPSR = *reinterpret_cast<const uint8_t*>(Entry) == 0x0f;
    const uint32_t GuestOffset = NativeFPSR ? 12 : 13;
    if (!(Op->PC == Entry + GuestOffset && Op->InstSize == sizeof(Store) &&
          std::memcmp(reinterpret_cast<const void*>(Op->PC), Store, sizeof(Store)) == 0 &&
          GetGPROpSize() == OpSize::i64Bit && !CTX->HostFeatures.SupportsSVE128 &&
          !CTX->HostFeatures.SupportsSVE256 && !CTX->HostFeatures.SupportsRCPC && !CTX->HostFeatures.SupportsTSOImm9)) {
      std::fprintf(stderr, "NATEMU_REJECT store fixture/feature mismatch\\n"); std::abort();
    }
    CalculateDeferredFlags();
    static constexpr uint32_t GPRs[] = {X86State::REG_RAX, X86State::REG_RBX, X86State::REG_RCX, X86State::REG_RDX, X86State::REG_RSI, X86State::REG_RDI, X86State::REG_RBP, X86State::REG_RSP, X86State::REG_R8, X86State::REG_R9, X86State::REG_R10, X86State::REG_R11, X86State::REG_R12, X86State::REG_R13, X86State::REG_R14, X86State::REG_R15};
    std::array<Ref, 35> Roots;
    for (unsigned i = 0; i < 16; ++i) {
      Roots[i] = _NatemuRootCopy(LoadGPRRegister(GPRs[i], OpSize::i64Bit));
      Roots[16 + i] = LoadXMMRegister(i);
    }
    Roots[32] = _NatemuRootCopy(GetRFLAG(X86State::RFLAG_PF_RAW_LOC));
    Roots[33] = _NatemuRootCopy(GetRFLAG(X86State::RFLAG_AF_RAW_LOC));
    Roots[34] = _NatemuRootCopy(GetNZCV());
    Ref Src = LoadSourceFPR(Op, Op->Src[0], Op->Flags, {.Align = OpSize::i8Bit});
    StoreResult_WithAVXInsert(VectorOpType::SSE, RegClass::FPR, Op, Src);
    _NatemuRecoveryUse(ARGS, Roots[5], GuestOffset, 1, uint32_t(CFInverted), uint32_t(NativeFPSR));
    return;
  }
'''.replace('ARGS',args)
edit(vector,'void OpDispatchBuilder::MOVVectorUnalignedOp(OpcodeArgs) {','void OpDispatchBuilder::MOVVectorUnalignedOp(OpcodeArgs) {\n'+hook)
p=root/vector;p.write_text('#include <cstdlib>\n#include <cstdio>\n#include <cstring>\n'+p.read_text())
ra='FEXCore/Source/Interface/IR/Passes/RegisterAllocationPass.cpp'
edit(ra,'Ref Access = IR->GetNode(AccessArg);','Ref Access = Meta->StoreSite ? Previous : IR->GetNode(AccessArg);')
edit(ra,'IR->GetOp<IROp_Header>(Access)->Op == OP_LOADMEMTSO,','IR->GetOp<IROp_Header>(Access)->Op == (Meta->StoreSite ? OP_STOREMEMTSO : OP_LOADMEMTSO),')
edit(ra,'std::getenv("NATEMU_CHECKPOINT_ROOT_PREP") && IROp->Op == OP_LOADMEMTSO','std::getenv("NATEMU_CHECKPOINT_ROOT_PREP") && (IROp->Op == OP_LOADMEMTSO || IROp->Op == OP_STOREMEMTSO)')
mem='FEXCore/Source/Interface/Core/JIT/MemoryOps.cpp';p=root/mem;s=p.read_text();a=s.index('DEF_OP(StoreMemTSO)');b=s.index('DEF_OP(MemSet)',a);part=s[a:b]
old='case IR::OpSize::i128Bit: str(Src.Q(), MemSrc); break;'
new='''case IR::OpSize::i128Bit:
      if (std::getenv("NATEMU_CHECKPOINT_ROOT_PREP")) {
        if (!Op->Offset.IsInvalid()) { std::fprintf(stderr, "NATEMU_REJECT unexpected store offset\\n"); std::abort(); }
        std::fprintf(stderr, "NATEMU_SITE entry=%llx access=%u offset=%zu dst=v%u address=x%u\\n", static_cast<unsigned long long>(Entry), IR->GetID(Node).Value, size_t(GetCursorOffset()), unsigned(Src.Idx()), unsigned(MemReg.Idx()));
      }
      str(Src.Q(), MemSrc); break;'''
assert part.count(old)==1;p.write_text(s[:a]+part.replace(old,new)+s[b:])
