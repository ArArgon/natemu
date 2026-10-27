#!/usr/bin/env python3
"""Apply the macOS compile-only patches to a FRESH extracted pinned FEX tree.

usage: patch.py TREE      (asserts every anchor matches exactly once; not idempotent)
"""
import sys
from pathlib import Path

root = Path(sys.argv[1])

def edit(name, old, new, count=1, first=False):
    # first=True: the anchor may occur several times (arch #ifdef branches); patch the first.
    p = root / name
    t = p.read_text()
    assert t.count(old) == count, (name, old, t.count(old))
    p.write_text(t.replace(old, new, 1 if first else -1))

# size_t is `unsigned long` on macOS but uint64_t is `unsigned long long`; the
# SpinWaitLock helpers are only overloaded for uint64_t (Linux aarch64: same type).
edit('FEXCore/include/FEXCore/Utils/SpinWaitLock.h',
     'template<typename Pred, typename T>\nstatic inline void WaitPred(',
     '''#ifdef __APPLE__
static inline unsigned long LoadExclusive(unsigned long* Futex) { return LoadExclusive(reinterpret_cast<uint64_t*>(Futex)); }
static inline unsigned long WFELoadAtomic(unsigned long* Futex) { return WFELoadAtomic(reinterpret_cast<uint64_t*>(Futex)); }
#endif

template<typename Pred, typename T>
static inline void WaitPred(''', count=2, first=True)

# JIT/dispatcher buffers: no PROT_EXEC (see shim/fexc_compat.h).
edit('FEXCore/include/FEXCore/Utils/AllocatorHooks.h',
     '(Execute ? PROT_EXEC : 0)', 'FEXC_EXEC_PROT(Execute)', count=2)

# macOS aligned_alloc rejects small alignments (e.g. alignof(char)); use posix_memalign
# with a pointer-size minimum. fextl containers allocate through this on every use.
edit('FEXCore/Source/Utils/AllocatorHooks.cpp',
     '  return ::aligned_alloc(a, s);',
     '''#ifdef __APPLE__
  void* p = nullptr;
  return ::posix_memalign(&p, a < sizeof(void*) ? sizeof(void*) : a, s ? s : 1) ? nullptr : p;
#else
  return ::aligned_alloc(a, s);
#endif''')

# ---------------------------------------------------------------------------
# C1b: guest-code window seam (ported from tools/i4v_producer/adapt.py).
# Guest instruction bytes live in a bounded host buffer that the driver resolves
# by *logical guest address*; every pointer arithmetic on guest PCs becomes
# integer arithmetic so upper-canonical RIPs never dereference host memory.
# ---------------------------------------------------------------------------
api = 'FEXCore/include/FEXCore/HLE/SyscallHandler.h'
edit(api, '  virtual void PreCompile() {}', '''  // Compile-only clients keep guest instruction bytes outside the guest VA:
  // return the host bytes for [Address, Address+Size), or nullptr if not available.
  virtual const uint8_t* ResolveGuestCodePointer(uint64_t Address, uint64_t Size, const uint8_t* Fallback) {
    return Fallback;
  }

  virtual void PreCompile() {}''')
front = 'FEXCore/Source/Interface/Core/Frontend.cpp'
edit(front, 'return InstStream.AdjustedInstStream[InstructionSize + Offset];', '''const auto* Source = CTX->SyscallHandler->ResolveGuestCodePointer(ByteAddress, 1, InstStream.AdjustedInstStream + InstructionSize + Offset);
    if (!Source) return std::nullopt;
    return *Source;''')
edit(front, '    std::memcpy(&Res, &InstStream.AdjustedInstStream[InstructionSize], Size);', '''    const auto* Source = CTX->SyscallHandler->ResolveGuestCodePointer(Address, Size, InstStream.AdjustedInstStream + InstructionSize);
    if (!Source) { HitNonExecutableRange = true; return {0, false}; }
    std::memcpy(&Res, Source, Size);''')
edit(front, 'reinterpret_cast<uint64_t>(InstStream.InstStream + InstructionSize + Offset)',
     'reinterpret_cast<uint64_t>(InstStream.InstStream) + InstructionSize + Offset')
edit(front, 'reinterpret_cast<uint64_t>(InstStream.InstStream + InstructionSize)',
     'reinterpret_cast<uint64_t>(InstStream.InstStream) + InstructionSize')
edit(front, 'InstStream.AdjustedInstStream + InstructionSize + Offset',
     'reinterpret_cast<const uint8_t*>(reinterpret_cast<uint64_t>(InstStream.AdjustedInstStream) + InstructionSize + Offset)')
edit(front, 'InstStream.AdjustedInstStream + InstructionSize',
     'reinterpret_cast<const uint8_t*>(reinterpret_cast<uint64_t>(InstStream.AdjustedInstStream) + InstructionSize)')
edit(front, '_InstStream - EntryPoint + RIP',
     'reinterpret_cast<const uint8_t*>(reinterpret_cast<uint64_t>(_InstStream) - EntryPoint + RIP)', count=3)
for member in ('InstStream', 'AdjustedInstStream'):
    for op in ('+', '-'):
        edit('FEXCore/Source/Interface/Core/Frontend.h', f'      {member} {op}= offset;',
             f'      {member} = reinterpret_cast<const uint8_t*>(reinterpret_cast<uint64_t>({member}) {op} offset);')
core = 'FEXCore/Source/Interface/Core/Core.cpp'
edit(core, 'const uint8_t* InstBytes = reinterpret_cast<const uint8_t*>(InstAddress);',
     'const uint8_t* InstBytes = SyscallHandler->ResolveGuestCodePointer(InstAddress, DecodedInfo->InstSize, reinterpret_cast<const uint8_t*>(InstAddress));')
edit(core, 'auto ExistingCodePtr = reinterpret_cast<uint8_t*>(Block.Entry + BlockInstructionsLength);',
     'auto ExistingCodePtr = SyscallHandler->ResolveGuestCodePointer(Block.Entry + BlockInstructionsLength, DecodedInfo->InstSize, reinterpret_cast<const uint8_t*>(Block.Entry + BlockInstructionsLength));')
edit(core, '          ZydisDisassembledInstruction ZydisInst;', '          if (!InstBytes) std::abort();\n          ZydisDisassembledInstruction ZydisInst;')
edit(core, '          auto InstAddressReg = Thread->OpDispatcher->_EntrypointOffset(GPRSize, InstAddress - GuestRIP);',
     '          if (!ExistingCodePtr) std::abort();\n          auto InstAddressReg = Thread->OpDispatcher->_EntrypointOffset(GPRSize, InstAddress - GuestRIP);')

# ---------------------------------------------------------------------------
# C1b: export hook (see shim/fexc_hook.h). Called once per compiled unit with the
# finished native code and FEX's debug data, before FEX installs the block.
# ---------------------------------------------------------------------------
p = root / core
t = p.read_text()
t = '#include <fexc_hook.h>\n' + t
p.write_text(t)
edit(core, '    GenerateIR(Thread, GuestRIP, Config.GDBSymbols(), MaxInst);',
     '    GenerateIR(Thread, GuestRIP, Config.GDBSymbols() || fexc_dense_pc_table, MaxInst);')
edit(core, '''  auto CompiledCode = Thread->CPUBackend->CompileCode(GuestRIP, Length, TotalInstructions == 1, &*IRView, DebugData.get(), TFSet);
''', '''  auto CompiledCode = Thread->CPUBackend->CompileCode(GuestRIP, Length, TotalInstructions == 1, &*IRView, DebugData.get(), TFSet);
  if (fexc_compile_hook) {
    fexc_compile_hook(Thread, FexcCompileInfo {GuestRIP, StartAddr, Length, TotalInstructions, TotalInstructionsLength},
                      CompiledCode.BlockBegin, CompiledCode.Size, *DebugData);
  }
''')

# Extra DebugData the JIT knows but does not otherwise keep.
edit('FEXCore/Source/Interface/Core/JIT/DebugData.h',
     '  fextl::vector<FEXCore::CPU::Relocation>* Relocations;\n};',
     '''  fextl::vector<FEXCore::CPU::Relocation>* Relocations;

  // natemu additions: {guest entry offset, native offset where the entry prologue ends}
  // for every FEX entry point, and the static register map used for this compile.
  fextl::vector<std::pair<uint64_t, uint64_t>> FexcEntryPrologues;
  uint8_t FexcSRAGPR[18] {};
  uint8_t FexcSRAGPRCount {};
  uint8_t FexcSRAFPR[16] {};
  uint8_t FexcSRAFPRCount {};
};''')
edit('FEXCore/Source/Interface/Core/JIT/DebugData.h', '#include <cstdint>', '#include <cstdint>\n#include <utility>')
jit = 'FEXCore/Source/Interface/Core/JIT/JIT.cpp'
edit(jit, '        EmitEntryPoint(JITCodeHeaderLabel, CheckTF);\n',
     '''        EmitEntryPoint(JITCodeHeaderLabel, CheckTF);
        DebugData->FexcEntryPrologues.push_back({BlockIROp->GuestEntryOffset, uint64_t(GetCursorAddress<uint8_t*>() - CodeData.BlockBegin)});
''')
edit(jit, '  DebugData->HostCodeSize = CodeData.Size;\n',
     '''  DebugData->HostCodeSize = CodeData.Size;
  DebugData->FexcSRAGPRCount = uint8_t(StaticRegisters.size());
  for (size_t i = 0; i < StaticRegisters.size(); ++i) DebugData->FexcSRAGPR[i] = uint8_t(StaticRegisters[i].Idx());
  DebugData->FexcSRAFPRCount = uint8_t(StaticFPRegisters.size());
  for (size_t i = 0; i < StaticFPRegisters.size(); ++i) DebugData->FexcSRAFPR[i] = uint8_t(StaticFPRegisters[i].Idx());
''')

# ---------------------------------------------------------------------------
# 16 KiB host pages. FEX protects/decommits at FEX_PAGE_SIZE (4 KiB, also the
# *guest* page size), which fails or is wrong on macOS arm64. Only host-side
# uses are switched to FEXC_HOST_PAGE_SIZE (shim/fexc_compat.h); guest-page uses
# keep 4 KiB.
# ---------------------------------------------------------------------------
edit('FEXCore/Source/Interface/Core/SharedCodeBufferManager.cpp',
     'AlignDown(reinterpret_cast<uintptr_t>(Ptr) + Size - 1, FEXCore::Utils::FEX_PAGE_SIZE);',
     'AlignDown(reinterpret_cast<uintptr_t>(Ptr) + Size - 1, FEXC_HOST_PAGE_SIZE);')
edit('FEXCore/Source/Interface/Core/SharedCodeBufferManager.cpp',
     'reinterpret_cast<void*>(LastPageAddr), FEXCore::Utils::FEX_PAGE_SIZE,',
     'reinterpret_cast<void*>(LastPageAddr), FEXC_HOST_PAGE_SIZE,')
edit('FEXCore/include/FEXCore/Utils/ThreadPoolAllocator.h',
     'AlignDown(reinterpret_cast<uintptr_t>(Ptr) + Size - 1, FEXCore::Utils::FEX_PAGE_SIZE);',
     'AlignDown(reinterpret_cast<uintptr_t>(Ptr) + Size - 1, FEXC_HOST_PAGE_SIZE);')
edit('FEXCore/include/FEXCore/Utils/ThreadPoolAllocator.h',
     'reinterpret_cast<void*>(LastPageAddr), FEXCore::Utils::FEX_PAGE_SIZE,',
     'reinterpret_cast<void*>(LastPageAddr), FEXC_HOST_PAGE_SIZE,')
edit(jit, 'AlignUp(FEXCore::Utils::FEX_PAGE_SIZE * 2 + SSACount * SSANodeMultiplier, FEXCore::Utils::FEX_PAGE_SIZE);',
     'AlignUp(FEXC_HOST_PAGE_SIZE * 2 + SSACount * SSANodeMultiplier, FEXC_HOST_PAGE_SIZE);')
edit(jit, 'TempCodeBufferInfo.Size - FEXCore::Utils::FEX_PAGE_SIZE;', 'TempCodeBufferInfo.Size - FEXC_HOST_PAGE_SIZE;')
# macOS madvise(MADV_DONTNEED) does not zero pages; FEXCore relies on that.
edit('FEXCore/include/FEXCore/Utils/AllocatorHooks.h',
     '  ::madvise(reinterpret_cast<void*>(Ptr), Size, MADV_DONTNEED);',
     '''#ifdef __APPLE__
  memset(Ptr, 0, Size);
#else
  ::madvise(reinterpret_cast<void*>(Ptr), Size, MADV_DONTNEED);
#endif''')

# ---------------------------------------------------------------------------
# S1a: guest-memory access sites (natemu-fexc-v2). See shim/fexc_sites.inc for the recording rules.
# ---------------------------------------------------------------------------
# (1) The dispatcher's carry-inversion state at each guest instruction start, captured where the
#     GuestOpcode op is created (after the per-instruction FlushRegisterCache(true)).
edit('FEXCore/Source/Interface/Core/OpcodeDispatcher.h', '  void StartNewBlock() {',
     '  bool FexcCFInverted() const { return CFInverted; }\n\n  void StartNewBlock() {')
edit(core, '          Thread->OpDispatcher->_GuestOpcode(InstAddress - GuestRIP);',
     '''          fexc_cf_inverted[InstAddress - GuestRIP] = Thread->OpDispatcher->FexcCFInverted();
          Thread->OpDispatcher->_GuestOpcode(InstAddress - GuestRIP);''')

# (2) The recorder is a member of the JIT class, defined in the included shim file.
edit('FEXCore/Source/Interface/Core/JIT/JITClass.h', '  void ResetStack();',
     '''  void ResetStack();

  // natemu (fexc): access-site recording, defined in shim/fexc_sites.inc (included from MemoryOps.cpp).
  void FexcAfterOp(const IR::IROp_Header* IROp, IR::Ref Node, uint32_t Begin);
  uint32_t FexcGuest {}, FexcInsnNative {};
  bool FexcCf {true}, FexcNzcvWritten {}, FexcNzcvSaved {}, FexcStoreSeen {};
  int FexcLoadSite {-1};
  fextl::vector<size_t> FexcPendingFused;''')
edit('FEXCore/Source/Interface/Core/JIT/MemoryOps.cpp', 'DEF_OP(LoadContext) {',
     '#include <fexc_sites.inc>\n\nDEF_OP(LoadContext) {')
p = root / 'FEXCore/Source/Interface/Core/JIT/MemoryOps.cpp'
p.write_text('#include <fexc_hook.h>\n#include <cstring>\n' + p.read_text())

# (3) JIT.cpp: reset per attempt (a restart re-emits everything), observe every emitted IR op, keep spill size.
p = root / jit
p.write_text('#include <fexc_hook.h>\n' + p.read_text())
edit(jit, '  JumpTargets.clear();\n  CallReturnTargets.clear();',
     '''  fexc_sites.clear();
  fexc_insns.clear();
  DebugData->FexcEntryPrologues.clear();
  FexcGuest = FexcInsnNative = 0; FexcCf = true; FexcNzcvWritten = FexcNzcvSaved = FexcStoreSeen = false; FexcLoadSite = -1; FexcPendingFused.clear();
  JumpTargets.clear();
  CallReturnTargets.clear();''')
edit(jit, '    for (auto [CodeNode, IROp] : IR->GetCode(BlockNode)) {\n      switch (IROp->Op) {',
     '''    for (auto [CodeNode, IROp] : IR->GetCode(BlockNode)) {
      const uint32_t FexcBegin = uint32_t(GetCursorAddress<uint8_t*>() - CodeData.BlockBegin);
      switch (IROp->Op) {''')
edit(jit, '      default: Op_Unhandled(IROp, CodeNode); break;\n      }\n',
     '''      default: Op_Unhandled(IROp, CodeNode); break;
      }
      FexcAfterOp(IROp, CodeNode, FexcBegin);
''')
edit(jit, '  DebugData->HostCodeSize = CodeData.Size;\n',
     '  DebugData->HostCodeSize = CodeData.Size;\n  fexc_spill_bytes = uint32_t(SpillSlots * MaxSpillSlotSize);\n')

# ---------------------------------------------------------------------------
# S1c: profile slice1. Allowlist block cutting, and the single-gate exit (BLOCK_DEBUGGING's ExitFunction branch,
# gated by the runtime flag fexc_slice1 so Core.cpp::CompileBlock's compile-time one-instruction-per-block rule stays off).
# ---------------------------------------------------------------------------
# (1) The decoder records which opcode map produced each instruction. Arch-dispatch entries (63, a0..a3, 0f 05, ...)
#     and decode failures lose the table identity, but the map, DecodedInst::OPRaw and ModRM survive both.
tables = 'FEXCore/Source/Interface/Core/X86Tables/X86Tables.h'
edit(tables, '  int8_t REXIndex;\n};', '  int8_t REXIndex;\n  uint8_t FexcMap; // natemu: FexcMap (shim/fexc_hook.h) that produced this instruction\n};')
edit(front, 'return NormalOpHeader(&FEXCore::X86Tables::DDDNowOps[LocalOp], LocalOp);',
     '{ DecodeInst->FexcMap = 7; return NormalOpHeader(&FEXCore::X86Tables::DDDNowOps[LocalOp], LocalOp); }')
edit(front, 'return NormalOpHeader(&FEXCore::X86Tables::H0F38TableOps[LocalOp], LocalOp);',
     '{ DecodeInst->FexcMap = 5; return NormalOpHeader(&FEXCore::X86Tables::H0F38TableOps[LocalOp], LocalOp); }')
edit(front, 'return NormalOpHeader(&FEXCore::X86Tables::H0F3ATableOps[LocalOp], LocalOp);',
     '{ DecodeInst->FexcMap = 6; return NormalOpHeader(&FEXCore::X86Tables::H0F3ATableOps[LocalOp], LocalOp); }')
for old, m in (('SecondBaseOps[EscapeOp], EscapeOp)', 1), ('RepModOps[EscapeOp], EscapeOp)', 2),
               ('RepNEModOps[EscapeOp], EscapeOp)', 3), ('OpSizeModOps[EscapeOp], EscapeOp)', 4)):
    # SecondBaseOps appears in the NoOverlay and in the plain branch: both are map 1.
    cnt = 2 if m == 1 else 1
    edit(front, 'return NormalOpHeader(&FEXCore::X86Tables::' + old + ';',
         '{ DecodeInst->FexcMap = %d; return NormalOpHeader(&FEXCore::X86Tables::%s; }' % (m, old), count=cnt)
edit(front, '          DecodeREXIfValid();\n          return NormalOpHeader(Info, Op);',
     '          DecodeREXIfValid();\n          DecodeInst->FexcMap = 0;\n          return NormalOpHeader(Info, Op);')
p = root / front
p.write_text('#include <fexc_hook.h>\n' + p.read_text())

# (2) Cut before an instruction outside the allowlist: ExitFunction to its RIP, or no code at all when it is first.
p = root / core
p.write_text(p.read_text().replace('ContextImpl::GenerateIRResult\nContextImpl::GenerateIR(',
                                   '#include <fexc_slice1.inc>\n\nContextImpl::GenerateIRResult\nContextImpl::GenerateIR(', 1))
assert '#include <fexc_slice1.inc>' in p.read_text()
edit(core, '  Thread->OpDispatcher->ResetWorkingList();\n\n  uint64_t TotalInstructions {0};',
     '  Thread->OpDispatcher->ResetWorkingList();\n  fexc_cut = {};\n\n  uint64_t TotalInstructions {0};')
edit(core, '        DecodedInfo = &Block.DecodedInstructions[i];\n',
     '''        DecodedInfo = &Block.DecodedInstructions[i];

        if (fexc_slice1 && FexcSlice1::Cut(Block.BlockStatus, i + 1 == InstsInBlock, BlockInstructionsLength == 0, *DecodedInfo, InstAddress)) {
          if (!BlockInstructionsLength) {
            Thread->OpDispatcher->DelayedDisownBuffer();
            return {std::nullopt, 0, 0, 0, 0};
          }
          Thread->OpDispatcher->ExitFunction(Thread->OpDispatcher->_InlineEntrypointOffset(GPRSize, InstAddress - GuestRIP));
          break;
        }
''')

# (3) Exits: BLOCK_DEBUGGING's branch under the runtime flag. The RIP literal is registered as a guest-RIP relocation.
branch = 'FEXCore/Source/Interface/Core/JIT/BranchOps.cpp'
edit(branch, '  if constexpr (Context::BLOCK_DEBUGGING) {\n    // Skip block linking',
     '  if (fexc_slice1 || Context::BLOCK_DEBUGGING) {\n    // Skip block linking')
edit(branch, '      BindOrRestart(&l_ExitLink);\n      dc64(NewRIP);',
     '      BindOrRestart(&l_ExitLink);\n      PlaceNamedSymbolLiteral(InsertGuestRIPLiteral(NewRIP));')
p = root / branch
p.write_text('#include <fexc_hook.h>\n' + p.read_text())
# No exit linker literal (nothing is linked) when every exit goes through the gate.
edit(jit, '  BindOrRestart(&l_ExitLink);\n  PlaceNamedSymbolLiteral(InsertNamedSymbolLiteral(RelocNamedSymbolLiteral::NamedSymbol::SYMBOL_LITERAL_EXITFUNCTION_LINKER));',
     '  BindOrRestart(&l_ExitLink);\n  if (!fexc_slice1) PlaceNamedSymbolLiteral(InsertNamedSymbolLiteral(RelocNamedSymbolLiteral::NamedSymbol::SYMBOL_LITERAL_EXITFUNCTION_LINKER));')

# ---------------------------------------------------------------------------
# S1b1: precision patches (slice-1 contract §1, patches 2-5), every one guarded by the runtime flag fexc_slice1, so the
# default profile compiles exactly as before. Goal: at every site the guest state is the state before the site's guest
# instruction and none of its effects is committed. Shared predicates: shim/fexc_precision.h.
# ---------------------------------------------------------------------------
flagpass = 'FEXCore/Source/Interface/IR/Passes/RedundantFlagCalculationElimination.cpp'
edit(flagpass, '#include <FEXCore/fextl/vector.h>\n\n// Flag bit flags', '#include <FEXCore/fextl/vector.h>\n#include <fexc_precision.h>\n\n// Flag bit flags')
# (2) Every guest-memory op reads all six flags, so no flag write that precedes it (in this or an earlier instruction) is
#     deleted or narrowed. InvalidateFlags emits no code; as a "write of undefined values" just before an access it would
#     make the previous instruction's flag writes look dead, so under slice1 it is neither a read nor a write.
edit(flagpass, '''FlagInfo DeadFlagCalculationEliminination::Classify(IROp_Header* IROp) {
  FlagInfo Info = FlagInfos[IROp->Op];
''', '''FlagInfo DeadFlagCalculationEliminination::Classify(IROp_Header* IROp) {
  if (fexc_slice1 && FexcPrecision::GuestMemOp(IROp->Op)) {
    return FlagInfo::Pack({.Read = FLAG_ALL});
  }
  if (fexc_slice1 && IROp->Op == OP_INVALIDATEFLAGS) {
    return FlagInfo::Pack({});
  }
  FlagInfo Info = FlagInfos[IROp->Op];
''')
# (2b) A guest load whose value is dead (cmp/test/bt [m] after flag elimination) is still an access that can fault: keep it.
edit(flagpass, '  if (CodeNode->GetUses() > 0 || IR::HasSideEffects(IROp->Op)) {',
     '  if (CodeNode->GetUses() > 0 || IR::HasSideEffects(IROp->Op) || (fexc_slice1 && FexcPrecision::GuestMemOp(IROp->Op))) {')
# (5) Pre-NZCV: an instruction that writes NZCV before one of its accesses saves its incoming NZCV at its start
#     (`mrs xT, nzcv; str wT, [x28, #PreNzcvOffset]`, existing IR ops LoadNZCV + StoreContext inserted right after its
#     GuestOpcode); its sites then say nzcv=frame. Done before the elimination proper, which then sees the save as an NZCV
#     read and keeps the previous instruction's flags alive. NZCV writers: FEX's own flag table plus ImplicitFlagClobber.
edit(flagpass, '  // Initialize CFG\n  ControlFlowGraph CFG {.IR = CurrentIR};',
     '''  if (fexc_slice1) {
    Ref Guest = nullptr;
    bool Writer = false, Saved = false;
    for (auto [BlockNode, BlockHeader] : CurrentIR.GetBlocks()) {
      for (auto [CodeNode, IROp] : CurrentIR.GetCode(BlockNode)) {
        if (IROp->Op == OP_GUESTOPCODE) {
          Guest = CodeNode;
          Writer = Saved = false;
        } else if (FexcPrecision::GuestMemOp(IROp->Op)) {
          if (Writer && !Saved && Guest) {
            auto Cursor = IREmit->GetWriteCursor();
            IREmit->SetWriteCursor(Guest);
            IREmit->_StoreContext(OpSize::i32Bit, RegClass::GPR, IREmit->_LoadNZCV(), FexcPrecision::PreNzcvOffset);
            IREmit->SetWriteCursor(Cursor);
            Saved = true;
          }
        } else if ((Classify(IROp).Write() & FLAG_NZCV) || IR::ImplicitFlagClobber(IROp->Op)) {
          Writer = true;
        }
      }
    }
  }

  // Initialize CFG
  ControlFlowGraph CFG {.IR = CurrentIR};''')

ra = 'FEXCore/Source/Interface/IR/Passes/RegisterAllocationPass.cpp'
edit(ra, '#include <FEXCore/fextl/vector.h>\n#include <bit>', '#include <FEXCore/fextl/vector.h>\n#include <fexc_precision.h>\n#include <bit>')
# (3) No guest-memory op between a value's definition and its store to a static register. The backwards pass coalesces a
#     value into its SRA register when the first SRA load/store after the definition is that store; a guest-memory op now
#     counts as an access of every static register (GPRs, PF/AF, FPRs), so a value defined before it is computed in a
#     temporary and moved after it. This covers constants pooled across instructions (IREmitter::Constant). A load's own
#     result is never allocated to a static register (DDI0487 D1.4.1.4: the destination is UNKNOWN after an abort).
#     Exception: the stack pointer a Pop updates by writeback stays in RSP's register, because the access itself is the
#     only write (it does not happen if the access faults).
#     Stricter than the contract: a guest instruction boundary (GuestOpcode) is a barrier too, so a constant pooled from
#     an earlier instruction is never coalesced there; every static-register write stays in its own instruction's range
#     (each PC-table entry then holds the exact state, and verify_precision.py can check flag writes per instruction).
edit(ra, '''          if (CodeNode != DecodeSRANode(Header, Node)) {
            PreferredReg[IR->GetID(CodeNode).Value] = PhysicalRegister::Invalid();
          }
        }
''', '''          if (CodeNode != DecodeSRANode(Header, Node)) {
            PreferredReg[IR->GetID(CodeNode).Value] = PhysicalRegister::Invalid();
          }
        }

        if (fexc_slice1 && (FexcPrecision::GuestMemOp(IROp->Op) || IROp->Op == OP_GUESTOPCODE)) {
          if (IROp->Op == OP_LOADMEM || IROp->Op == OP_LOADMEMTSO) {
            PreferredReg[IR->GetID(CodeNode).Value] = PhysicalRegister::Invalid();
          }
          PhysicalRegister Keep = PhysicalRegister::Invalid();
          if (IROp->Op == OP_POP) {
            Keep = PreferredReg[IROp->C<IR::IROp_Pop>()->InoutAddr.ID().Value];
          }
          for (auto C : {RegClass::GPRFixed, RegClass::FPRFixed}) {
            for (uint8_t r = 0; r < Classes[FEXCore::ToUnderlying(C)].Count; ++r) {
              if (!(Keep == PhysicalRegister(C, r))) {
                Classes[FEXCore::ToUnderlying(C)].RegToSSA[r] = CodeNode;
              }
            }
          }
        }
''')
# (4) No merge across a guest instruction boundary (TryPostRAMerge fused adjacent guest push/pop into stp/ldp).
edit(ra, '''      if (IROp->Op == OP_GUESTOPCODE || IROp->Op == OP_INLINECONSTANT) {
        continue;''', '''      if (IROp->Op == OP_GUESTOPCODE || IROp->Op == OP_INLINECONSTANT) {
        if (fexc_slice1 && IROp->Op == OP_GUESTOPCODE) {
          LastNode = nullptr;
        }
        continue;''')
# (2b, RA side) A kept guest load with no uses still needs a destination; free it at once instead of holding it to block end.
edit(ra, '''      if (GetHasDest(IROp->Op) && PhysicalRegister(CodeNode).IsInvalid()) {
        AssignReg(IROp, BlockIROp, CodeNode, IROp);
      }''', '''      if (GetHasDest(IROp->Op) && PhysicalRegister(CodeNode).IsInvalid()) {
        AssignReg(IROp, BlockIROp, CodeNode, IROp);
        if (fexc_slice1 && (IROp->Op == OP_LOADMEM || IROp->Op == OP_LOADMEMTSO) && CodeNode->GetUses() == 0) {
          FreeReg(PhysicalRegister(CodeNode));
        }
      }''')
