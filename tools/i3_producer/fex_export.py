#!/usr/bin/env python3
"""Patch the pinned compile-only tool, or validate its retained input/output."""
import json
import re
from pathlib import Path
import struct
import sys


def check_input(binary, fixtures):
    data = binary.read_bytes()
    expected = json.loads(fixtures.read_text())['Instructions']
    bitness, _, count, enabled, disabled, envs = struct.unpack_from('<6Q', data)
    assert (bitness, count, envs) == (64, 6, 0)
    assert enabled == 1 << 13 and disabled == ((1 << 0) | (1 << 1) | (1 << 14) | (1 << 15))
    offset = 48
    for name, fixture in expected.items():
        label, _, size, instructions, cookie = struct.unpack_from('<128sqQQI', data, offset)
        offset += 156
        assert label.rstrip(b'\0').decode() == name and cookie == 0x41424344
        assert instructions == len(fixture['x86Insts'])
        assert data[offset:offset+size] == bytes.fromhex(fixture['ExpectedBytes'])
        offset += size
    assert offset == len(data)


def check_output(out, fixtures):
    code = int((out/'exit-code.txt').read_text())
    log = (out/'fex.log').read_text()
    assert code in (0, 1) and f'NATEMU_COMPLETED tests=6 result={code}' in log
    if code:
        assert 'Fail: Test took ' in log or 'Fail: TestHarness binary cache version is ' in log
    entries = [json.loads(line.removeprefix('NATEMU_OBJECT ')) for line in log.splitlines() if line.startswith('NATEMU_OBJECT ')]
    expected = [1] + [len(x['x86Insts']) for x in json.loads(fixtures.read_text())['Instructions'].values()]
    assert len(entries) == len(expected) == 7
    timings = re.findall(r'^NATEMU_COMPILE index=(\d+) ns=(\d+) ', log, re.MULTILINE)
    assert len(timings) == 7 and [int(i) for i, _ in timings] == list(range(7))
    for i, (entry, count) in enumerate(zip(entries, expected)):
        assert entry['index'] == i and entry['guest_instructions'] == count
        assert 0 < entry['bytes'] <= 65536 and 0 < entry['code_bytes'] <= entry['bytes']
        assert 0 <= entry['code_offset'] <= entry['bytes'] - entry['code_bytes']
        assert (out/f'{i:03}.native').stat().st_size == entry['bytes']
        assert (out/f'{i:03}.relocations').stat().st_size == entry['relocation_bytes']
    assert 'NATEMU_FEATURES sve128=0 sve256=0 rcpc=0 imm9=0' in log
    (out/'objects.json').write_text(json.dumps(entries, indent=2)+'\n')
    print('PASS: seven raw objects; finite exact input counts; no fixture execution; count-test status retained')


if sys.argv[1] == '--check-input':
    check_input(Path(sys.argv[2]), Path(sys.argv[3]))
    sys.exit(0)
if sys.argv[1] == '--check-output':
    check_output(Path(sys.argv[2]), Path(sys.argv[3]))
    sys.exit(0)

root = Path(sys.argv[1])
def replace(path, marker, addition):
    p = root/path
    text = p.read_text()
    assert text.count(marker) == 1, (path, marker)
    p.write_text(text.replace(marker, addition, 1))

tool = 'Source/Tools/CodeSizeValidation/Main.cpp'
replace(tool, '    if (MaxInst == -1) {', '''    struct timespec NatemuCompileBegin, NatemuCompileEnd;
    if (clock_gettime(CLOCK_MONOTONIC, &NatemuCompileBegin)) std::abort();
    if (MaxInst == -1) {''')
replace(tool, '    return CurrentStats;', '''    if (clock_gettime(CLOCK_MONOTONIC, &NatemuCompileEnd)) std::abort();
    int64_t NatemuCompileNS = int64_t(NatemuCompileEnd.tv_sec - NatemuCompileBegin.tv_sec) * 1000000000 +
                             NatemuCompileEnd.tv_nsec - NatemuCompileBegin.tv_nsec;
    if (NatemuCompileNS < 0) std::abort();
    static unsigned NatemuCompileIndex = 0;
    printf("NATEMU_COMPILE index=%u ns=%lld clock=monotonic scope=CompileRIP-or-CompileRIPCount diagnostics=included export_io=included policy=NOP-then-six-sequential-compiles cache=range-invalidated-before-each-compile\\n",
           NatemuCompileIndex++, (long long)NatemuCompileNS);
    return CurrentStats;''')
replace(tool, '  if (Level == LogMan::INFO) {', '  if (Level == LogMan::INFO) {\n    if (getenv("NATEMU_EXPORT")) fextl::fmt::print("RAW {}\\n", Message);')
replace(tool, '    HostFeatures.IsInstCountCI = true;', '''    HostFeatures.IsInstCountCI = true;
    printf("NATEMU_FEATURES sve128=%d sve256=%d rcpc=%d imm9=%d atomics=%d preserve_all=%d\\n",
      HostFeatures.SupportsSVE128, HostFeatures.SupportsSVE256, HostFeatures.SupportsRCPC,
      HostFeatures.SupportsTSOImm9, HostFeatures.SupportsAtomics, HostFeatures.SupportsPreserveAllABI);''')
replace(tool, '  FEXCore::Allocator::VirtualFree(TestData, TestDataSize);', '  printf("NATEMU_COMPLETED tests=%llu result=%d\\n", (unsigned long long)TestHeaderData->NumTests, Result);\n  FEXCore::Allocator::VirtualFree(TestData, TestDataSize);')
# Existing tool is compile-only; additionally fail on both ordinary and callback execution.
core = 'FEXCore/Source/Interface/Core/Core.cpp'
for signature in ('void ContextImpl::ExecuteThread(FEXCore::Core::InternalThreadState* Thread) {',
                  'void ContextImpl::HandleCallback(FEXCore::Core::InternalThreadState* Thread, uint64_t RIP) {'):
    replace(core, signature, signature+'\n  if (getenv("NATEMU_EXPORT")) std::abort();')

jit = 'FEXCore/Source/Interface/Core/JIT/JIT.cpp'
replace(jit, '  return std::move(CodeData);', r'''  if (const char* Directory = getenv("NATEMU_EXPORT")) {
    static unsigned Index = 0;
    if (Index >= 7 || !CodeData.Size || CodeData.Size > 65536) std::abort();
    char Path[4096];
    auto Write = [&](const char* Suffix, const void* Data, size_t Bytes) {
      int Length = snprintf(Path, sizeof(Path), "%s/%03u.%s", Directory, Index, Suffix);
      if (Length < 0 || size_t(Length) >= sizeof(Path)) std::abort();
      FILE* File = fopen(Path, "wbx");
      if (!File || (Bytes && fwrite(Data, 1, Bytes, File) != Bytes) || fclose(File)) std::abort();
    };
    Write("native", CodeData.BlockBegin, CodeData.Size);
    const size_t RelocationBytes = (Relocations.size() - PrevNumAllocations) * sizeof(Relocations[0]);
    Write("relocations", RelocationBytes ? Relocations.data() + PrevNumAllocations : nullptr, RelocationBytes);
    printf("NATEMU_OBJECT {\"index\":%u,\"bytes\":%zu,\"code_bytes\":%zu,\"code_offset\":%zu,\"guest_instructions\":%llu,\"relocation_bytes\":%zu,\"block_address\":%llu}\n",
      Index, size_t(CodeData.Size), size_t(CodeOnlySize), size_t(CodeBegin - CodeData.BlockBegin),
      (unsigned long long)IR->GetHeader()->NumHostInstructions, RelocationBytes,
      (unsigned long long)reinterpret_cast<uintptr_t>(CodeData.BlockBegin));
    ++Index;
    fflush(nullptr);
  }
  return std::move(CodeData);''')
for name in (tool, core, jit):
    p = root/name
    p.write_text('#include <cstdio>\n#include <cstdlib>\n#include <time.h>\n'+p.read_text())
