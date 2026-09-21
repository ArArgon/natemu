#!/usr/bin/env python3
"""Apply the finite compile-only branch to the exact pinned Box64 snapshot."""
from pathlib import Path
import sys

root=Path(sys.argv[1])
p=root/'src/core.c'; s=p.read_text(); marker='    thread_set_emu(emu);'
assert s.count(marker)==1
s=s.replace('#include "hostext.h"','#include "hostext.h"\n#include <time.h>\n#include "dynarec/dynablock_private.h"\n#include "dynablock.h"',1)
branch=r'''
    if (getenv("NATEMU_EXPORT")) {
        if (box64_hasinterp || !box64_nolibs || box64_is32bits) _exit(90);
        uintptr_t entry = GetEntryPoint(my_context->maplib, elf_header);
        size_t expected = strtoul(getenv("NATEMU_BYTES"), NULL, 10);
        int count = atoi(getenv("NATEMU_COUNT"));
        if (!expected || expected > 32 || count < 1 || count > 5) _exit(91);
        struct timespec compile_begin, compile_end;
        if (clock_gettime(CLOCK_MONOTONIC, &compile_begin)) _exit(96);
        dynablock_t *db = CreateDBnoAlt(emu, entry, 0);
        if (clock_gettime(CLOCK_MONOTONIC, &compile_end)) _exit(96);
        int64_t compile_ns = (int64_t)(compile_end.tv_sec - compile_begin.tv_sec) * 1000000000 +
                             compile_end.tv_nsec - compile_begin.tv_nsec;
        if (compile_ns < 0) _exit(96);
        if (!db || !db->block || db->isize != count || db->x64_size != expected
            || !db->native_size || db->native_size > 65536) _exit(92);
        printf("NATEMU_COMPILE ns=%lld clock=monotonic scope=CreateDBnoAlt diagnostics=included export_io=excluded policy=first-compile-in-fresh-initialized-process cache=empty\n",
               (long long)compile_ns);
        FILE *code = fopen(getenv("NATEMU_EXPORT"), "wbx");
        if (!code || fwrite(db->block, 1, db->native_size, code) != db->native_size
            || fclose(code)) _exit(93);
        printf("NATEMU_EXPORT guest_bytes=%zu instructions=%d native_bytes=%zu prefix_bytes=%u table64_words=%zu arch_bytes=%d reloc_bytes=%zu entry=%p native=%p\n",
            expected, db->isize, db->native_size, db->prefixsize, db->table64size,
            db->arch_size, db->relocsize, (void*)entry, db->block);
        fflush(NULL);
        _exit(0);
    }
'''
s=s.replace(marker,marker+branch,1); p.write_text(s)
p=root/'src/dynarec/dynablock.c'; s=p.read_text(); old='FillBlock64(addr, is32bits, MAX_INSTS, 0, 1)'; assert s.count(old)==1
s=s.replace(old,'FillBlock64(addr, is32bits, getenv("NATEMU_EXPORT") ? atoi(getenv("NATEMU_COUNT")) : MAX_INSTS, 0, 1)',1); p.write_text(s)
# Refuse entry into the application's execution path even if initialization changes.
p=root/'src/core.c'; s=p.read_text(); marker='int emulate(x64emu_t* emu, elfheader_t* elf_header)\n{'; assert s.count(marker)==1
p.write_text(s.replace(marker,marker+'\n    if (getenv("NATEMU_EXPORT")) _exit(94);',1))
for name, markers in {
    'src/dynarec/dynarec.c': [
        'void DynaCall(x64emu_t* emu, uintptr_t addr, int no_alt)\n{',
        'void DynaRun(x64emu_t *emu)\n{',
        'void EmuRun(x64emu_t* emu, int use_dynarec, int no_alt)\n{'],
    'src/emu/x64run.c': ['int Run(x64emu_t *emu, int step)\n#endif\n{'],
}.items():
    p=root/name; s=p.read_text()
    s='#include <stdlib.h>\n#include <unistd.h>\n'+s
    for marker in markers:
        assert s.count(marker)==1, (name, marker)
        s=s.replace(marker,marker+'\n    if (getenv("NATEMU_EXPORT")) _exit(95);',1)
    p.write_text(s)
# GetEntryPoint does not require exported symbols at this pin.
p=root/'src/elfs/elfloader.c'
assert 'uintptr_t ep = h->entrypoint + h->delta;' in p.read_text()
