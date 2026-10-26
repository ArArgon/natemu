#!/usr/bin/env python3
"""Apply the bounded guest-code window seam after the existing raw exporter."""
from pathlib import Path
import sys

root = Path(sys.argv[1])

def edit(name, old, new, count=1):
    path = root / name
    text = path.read_text()
    assert text.count(old) == count, (name, old, text.count(old))
    path.write_text(text.replace(old, new))

api = 'FEXCore/include/FEXCore/HLE/SyscallHandler.h'
edit(api, '  virtual void PreCompile() {}', '''  // Compile-only clients may keep guest instruction bytes outside the guest VA.
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
# Logical guest PCs need integer arithmetic: upper guest addresses are not C++
# host arrays, even when a later resolver supplies the actual byte storage.
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
# The adapter only admits immutable bounded code; retain coherent pointer use in
# diagnostic/SMC paths as well, rather than leaving a hidden host dereference.
core = 'FEXCore/Source/Interface/Core/Core.cpp'
edit(core, 'const uint8_t* InstBytes = reinterpret_cast<const uint8_t*>(InstAddress);', 'const uint8_t* InstBytes = SyscallHandler->ResolveGuestCodePointer(InstAddress, DecodedInfo->InstSize, reinterpret_cast<const uint8_t*>(InstAddress));')
edit(core, 'auto ExistingCodePtr = reinterpret_cast<uint8_t*>(Block.Entry + BlockInstructionsLength);', 'auto ExistingCodePtr = SyscallHandler->ResolveGuestCodePointer(Block.Entry + BlockInstructionsLength, DecodedInfo->InstSize, reinterpret_cast<const uint8_t*>(Block.Entry + BlockInstructionsLength));')
edit(core, '          ZydisDisassembledInstruction ZydisInst;', '          if (!InstBytes) std::abort();\n          ZydisDisassembledInstruction ZydisInst;')
edit(core, '          auto InstAddressReg = Thread->OpDispatcher->_EntrypointOffset(GPRSize, InstAddress - GuestRIP);',
     '          if (!ExistingCodePtr) std::abort();\n          auto InstAddressReg = Thread->OpDispatcher->_EntrypointOffset(GPRSize, InstAddress - GuestRIP);')

main = 'Source/Tools/CodeSizeValidation/Main.cpp'
edit(main, 'namespace CodeSize {', '''namespace NatemuWindow {
static uint64_t Base, Size;
static const uint8_t* Source;
static bool Contains(uint64_t Address, uint64_t Bytes) {
  return Address >= Base && Address - Base <= Size && Bytes <= Size - (Address - Base);
}
}
namespace CodeSize {''')
edit(main, '    ClearStats();\n    memcpy(CodeStart, Data, SizeBytes);', '''    ClearStats();
    memcpy(CodeStart, Data, SizeBytes);
    static unsigned WindowIndex = 0;
    uint64_t GuestRIP = reinterpret_cast<uint64_t>(CodeStart);
    if (WindowIndex && getenv("NATEMU_GUEST_RIP")) {
      char* End = nullptr;
      GuestRIP = strtoull(getenv("NATEMU_GUEST_RIP"), &End, 0);
      if (!End || *End || SizeBytes > 64 || !SizeBytes || MaxInst < 1 || MaxInst > 32 ||
          GuestRIP > UINT64_MAX - SizeBytes) std::abort();
    }
    NatemuWindow::Base = GuestRIP;
    NatemuWindow::Size = SizeBytes;
    NatemuWindow::Source = reinterpret_cast<const uint8_t*>(CodeStart);
    printf("NATEMU_WINDOW index=%u guest_rip=%llu source_address=%llu bytes=%zu instructions=%d\\n",
      WindowIndex++, (unsigned long long)GuestRIP,
      (unsigned long long)reinterpret_cast<uint64_t>(CodeStart), SizeBytes, MaxInst);''')
edit(main, 'CTX->CompileRIP(Thread, reinterpret_cast<uint64_t>(CodeStart));', 'CTX->CompileRIP(Thread, GuestRIP);')
edit(main, 'CTX->CompileRIPCount(Thread, reinterpret_cast<uint64_t>(CodeStart), MaxInst);', 'CTX->CompileRIPCount(Thread, GuestRIP, MaxInst);')
edit(main, '    return {0, UINT64_MAX, true};', '''    if (!NatemuWindow::Contains(Address, 1)) return {Address, 0, false};
    return {NatemuWindow::Base, NatemuWindow::Size, false};
  }
  const uint8_t* ResolveGuestCodePointer(uint64_t Address, uint64_t Size, const uint8_t*) override {
    if (!NatemuWindow::Contains(Address, Size)) return nullptr;
    return NatemuWindow::Source + (Address - NatemuWindow::Base);''')
edit(main, '  // Choose bitness.', '''  if (TestHeaderData->Bitness != 32 && TestHeaderData->Bitness != 64) std::abort();
  printf("NATEMU_MODE bitness=%llu\\n", (unsigned long long)TestHeaderData->Bitness);
  if (TestHeaderData->Bitness == 32 && getenv("NATEMU_GUEST_RIP") &&
      strtoull(getenv("NATEMU_GUEST_RIP"), nullptr, 0) > UINT32_MAX) std::abort();
  // Choose bitness.''')
jit = 'FEXCore/Source/Interface/Core/JIT/JIT.cpp'
edit(jit, '    ++Index;\n    fflush(nullptr);', '''    printf("NATEMU_ENTRY index=%u guest_rip=%llu\\n", Index, (unsigned long long)Entry);
    for (const auto& Opcode : DebugData->GuestOpcodes) {
      printf("NATEMU_GUEST_PC index=%u guest_offset=%llu native_offset=%llu\\n", Index,
        (unsigned long long)Opcode.GuestEntryOffset, (unsigned long long)Opcode.HostEntryOffset);
    }
    ++Index;
    fflush(nullptr);''')
