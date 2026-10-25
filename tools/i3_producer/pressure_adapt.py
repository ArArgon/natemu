"""Extend the existing real allocator metadata seam with a bounded pressure profile."""
import json
from pathlib import Path
import sys
root=Path(sys.argv[1])
def edit(path,old,new,count=1):
 p=root/path;s=p.read_text();assert s.count(old)==count,(path,old,s.count(old));p.write_text(s.replace(old,new))
front='FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp'
edit(front,'static constexpr uint8_t Load[] = {0x48, 0x8b, 0x03};','static constexpr uint8_t Load[] = {0x48, 0x8b, 0x00};\n    const bool NativeFPSR = *reinterpret_cast<const uint8_t*>(Entry) == 0x0f;\n    const uint32_t GuestOffset = NativeFPSR ? 6 : 7;\n    CalculateDeferredFlags();')
edit(front,'Op->PC == Entry + 7','Op->PC == Entry + GuestOffset')
edit(front,'std::array<Ref, 32> Roots;','std::array<Ref, 35> Roots;')
edit(front,'Roots[i] = LoadGPRRegister(GPRs[i], OpSize::i64Bit);','Roots[i] = _NatemuRootCopy(LoadGPRRegister(GPRs[i], OpSize::i64Bit));')
edit(front,'    Ref Src = LoadSourceGPR(Op, Op->Src[SrcIndex], Op->Flags, {.Align = OpSize::i8Bit, .AllowUpperGarbage = true});\n    const auto Header', '    Roots[32] = _NatemuRootCopy(GetRFLAG(X86State::RFLAG_PF_RAW_LOC));\n    Roots[33] = _NatemuRootCopy(GetRFLAG(X86State::RFLAG_AF_RAW_LOC));\n    Roots[34] = _NatemuRootCopy(GetNZCV());\n    Ref Src = LoadSourceGPR(Op, Op->Src[SrcIndex], Op->Flags, {.Align = OpSize::i8Bit, .AllowUpperGarbage = true});\n    const auto Header')
edit(front,'Roots[30], Roots[31], Src, 7);','Roots[30], Roots[31], Roots[32], Roots[33], Roots[34], Src, GuestOffset, uint32_t(CFInverted), uint32_t(NativeFPSR));')
p=root/'FEXCore/Source/Interface/IR/IR.json';schema=json.loads(p.read_text())
schema['Ops']['Moves']['GPR = NatemuRootCopy GPR:$Source']={'Desc':['Conservative I3 pressure-profile recovery copy, allocated by real FEX RA'],'DestSize':'OpSize::i64Bit','HasSideEffects':True,'JITDispatchOverride':'Copy'}
misc=schema['Ops']['Misc'];key=next(k for k in misc if k.startswith('NatemuRecoveryUse '));value=misc.pop(key)
key=key.replace('GPR:$AccessValue,','GPR:$Root32, GPR:$Root33, GPR:$Root34, GPR:$AccessValue,').replace('u32:$GuestEntryOffset,','u32:$GuestEntryOffset, u32:$CarryInverted, u32:$NativeFPSR,').replace('u64:$LocationKinds{0},','u64:$LocationKinds{0}, u64:$LocationKindsHi{0},')
key+=', u64:$Location32{0}, u64:$Location33{0}, u64:$Location34{0}'
value['Desc']=['I3 allocated 35-root map with explicit flag recipe; metadata-only uses do not force fills']
misc[key]=value;p.write_text(json.dumps(schema,indent=2)+'\n')
ra='FEXCore/Source/Interface/IR/Passes/RegisterAllocationPass.cpp'
edit(ra,'uint64_t Locations[32] {};','uint64_t Locations[35] {};')
edit(ra,'s < 32; ++s','s < 35; ++s')
edit(ra,'(s >= 16 ? OpSize::i128Bit : OpSize::i64Bit)','(s >= 16 && s < 32 ? OpSize::i128Bit : OpSize::i64Bit)')
edit(ra,'Meta->LocationKinds |= uint64_t{1} << (2 * s);','(s < 32 ? Meta->LocationKinds : Meta->LocationKindsHi) |= uint64_t{1} << (2 * (s % 32));')
edit(ra,'Meta->LocationKinds |= uint64_t{2} << (2 * s);','(s < 32 ? Meta->LocationKinds : Meta->LocationKindsHi) |= uint64_t{2} << (2 * (s % 32));')
edit(ra,'Meta->Location31 = Locations[31];','Meta->Location31 = Locations[31];\n        Meta->Location32 = Locations[32];\n        Meta->Location33 = Locations[33];\n        Meta->Location34 = Locations[34];')
jit='FEXCore/Source/Interface/Core/JIT/JIT.cpp'
edit(jit,'format=working32-v2','format=allocated35-pressure-v1')
edit(jit,'    std::fprintf(stderr, "NATEMU_AUTHORITY entry=%llx status=missing\\n", static_cast<unsigned long long>(Entry));','    std::fprintf(stderr, "NATEMU_STACK entry=%llx slots=%u bytes=%u\\n", static_cast<unsigned long long>(Entry), IR->GetHeader()->SpillSlots, IR->GetHeader()->SpillSlots * MaxSpillSlotSize);')
edit(jit,'Meta->Location30, Meta->Location31};','Meta->Location30, Meta->Location31, Meta->Location32, Meta->Location33, Meta->Location34};')
edit(jit,'"XMM14.low", "XMM15.low"};','"XMM14.low", "XMM15.low", "pf_raw", "af_raw", "nzcv"};')
edit(jit,'s < 32; ++s','s < 35; ++s')
edit(jit,'(Meta->LocationKinds >> (2 * s)) & 3','((s < 32 ? Meta->LocationKinds : Meta->LocationKindsHi) >> (2 * (s % 32))) & 3')
edit(jit,"s >= 16 ? GetVReg(Reg).Idx() : GetReg(Reg).Idx()","s >= 16 && s < 32 ? GetVReg(Reg).Idx() : GetReg(Reg).Idx()")
edit(jit,"s >= 16 ? 'v' : 'x'","s >= 16 && s < 32 ? 'v' : 'x'")
edit(jit,'        continue; // metadata contributes no native instruction','        std::fprintf(stderr, "NATEMU_FLAGS entry=%llx access=%u carry_inverted=%u native_fpsr=%u\\n", static_cast<unsigned long long>(Entry), Meta->AccessNode, Meta->CarryInverted, Meta->NativeFPSR);\n        continue; // metadata contributes no native instruction')
mem='FEXCore/Source/Interface/Core/JIT/MemoryOps.cpp'
anchor='  const uint32_t SlotOffset = Op->Slot * MaxSpillSlotSize;'
edit(mem,anchor,anchor+'\n  if (std::getenv("NATEMU_CHECKPOINT_ROOT_PREP") && Op->Class == IR::RegClass::GPR) {\n    std::fprintf(stderr, "NATEMU_SPILL entry=%llx offset=%zu slot=%u bytes=%u src=x%u\\n", static_cast<unsigned long long>(Entry), size_t(GetCursorOffset()), Op->Slot, unsigned(IR::OpSizeToSize(OpSize)), unsigned(GetReg(Op->Value).Idx()));\n  }',count=2)
# Only the actual SpillRegister branch is an initialization witness; FillRegister logs are separately named.
p=root/mem;s=p.read_text();pos=s.index('DEF_OP(FillRegister)');s=s[:pos]+s[pos:].replace('NATEMU_SPILL','NATEMU_FILL').replace('unsigned(GetReg(Op->Value).Idx()));', 'unsigned(GetReg(Node).Idx()));', 1);p.write_text(s)
main='Source/Tools/CodeSizeValidation/Main.cpp'
edit(main,'0x48, 0x8b, 0x03,','0x48, 0x8b, 0x00,')
edit(main,'      const bool SelectedFixture = SizeBytes == sizeof(Fixture) && MaxInst == 5 &&\n                                   std::memcmp(Data, Fixture, sizeof(Fixture)) == 0;','      static constexpr uint8_t FPFixture[] = {0x0f,0x58,0xc1,0x48,0x01,0xd1,0x48,0x8b,0x00,0x0f,0x94,0xc2,0xf3,0x0f,0x7f,0x07};\n      const bool SelectedFixture = MaxInst == 5 && ((SizeBytes == sizeof(Fixture) && std::memcmp(Data, Fixture, sizeof(Fixture)) == 0) || (SizeBytes == sizeof(FPFixture) && std::memcmp(Data, FPFixture, sizeof(FPFixture)) == 0));')
