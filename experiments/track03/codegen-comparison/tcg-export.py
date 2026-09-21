#!/usr/bin/env python3
"""Patch pinned QEMU for CPL3 linux-user emission only, never guest execution."""
from pathlib import Path
import sys

root = Path(sys.argv[1])
p = root / 'accel/tcg/cpu-exec.c'
s = p.read_text()
marker = 'cpu_tb_exec(CPUState *cpu, TranslationBlock *itb, int *tb_exit)\n{'
assert s.count(marker) == 1
# Both normal and atomic-step callers must fail if the generation export is missed.
s = s.replace(marker, marker + '\n    _exit(94); /* Probe build: no translated execution. */', 1)
marker = '            if (check_for_breakpoints(cpu, s.pc, &s.cflags)) {'
assert s.count(marker) == 1
s = s.replace(marker, r'''
            const char *count_arg = getenv("NATEMU_COUNT");
            char *end;
            long count = count_arg ? strtol(count_arg, &end, 10) : 0;
            if (!count_arg || *end || count < 1 || count > 5) _exit(90);
            s.cflags = (s.cflags & ~CF_COUNT_MASK) | count |
                       CF_PARALLEL | CF_NO_GOTO_TB | CF_NO_GOTO_PTR;
''' + marker, 1)
p.write_text(s)

p = root / 'accel/tcg/translate-all.c'
s = p.read_text()
marker = '    assert_memory_lock();\n    qemu_thread_jit_write();'
assert s.count(marker) == 1
s = s.replace(marker, '''    struct timespec natemu_compile_begin, natemu_compile_end;
    if (clock_gettime(CLOCK_MONOTONIC, &natemu_compile_begin)) _exit(96);
''' + marker, 1)
marker = '    qatomic_set(&tcg_ctx->code_gen_ptr, (void *)\n'
assert s.count(marker) == 1
s = s.replace(marker, r'''
    /* First generated fixture exits here, before publication or execution. */
    {
        if (clock_gettime(CLOCK_MONOTONIC, &natemu_compile_end)) _exit(96);
        int64_t compile_ns = (int64_t)(natemu_compile_end.tv_sec - natemu_compile_begin.tv_sec) * 1000000000 +
                             natemu_compile_end.tv_nsec - natemu_compile_begin.tv_nsec;
        if (compile_ns < 0) _exit(96);
        const char *path = getenv("NATEMU_EXPORT");
        const char *bytes_arg = getenv("NATEMU_BYTES");
        const char *count_arg = getenv("NATEMU_COUNT");
        char *end;
        long expected = bytes_arg ? strtol(bytes_arg, &end, 10) : 0;
        if (!path || !bytes_arg || *end || expected < 1 || expected > 32 ||
            !count_arg || tb->icount != atoi(count_arg) ||
            s.pc != 0x401000 || tb->size != expected ||
            gen_code_size <= 0 || gen_code_size > 65536 ||
            search_size <= 0 || search_size > 65536) _exit(91);
        printf("NATEMU_COMPILE ns=%" PRId64 " clock=monotonic scope=tb_gen_code-prepublication diagnostics=included export_io=excluded policy=first-compile-in-fresh-initialized-process cache=empty\n", compile_ns);
        FILE *code = fopen(path, "wbx");
        if (!code || fwrite(gen_code_buf, 1, gen_code_size, code) != gen_code_size ||
            fclose(code)) _exit(92);
        char *search_path = g_strdup_printf("%s.search", path);
        FILE *search = fopen(search_path, "wbx");
        if (!search || fwrite((uint8_t *)gen_code_buf + gen_code_size, 1,
                              search_size, search) != search_size ||
            fclose(search)) _exit(93);
        printf("NATEMU_EXPORT mode=linux-user-direct-memory guest_pc=%" PRIx64
               " guest_bytes=%u instructions=%u native_bytes=%d search_bytes=%d"
               " cflags=%x flags=%" PRIx64 " cs_base=%" PRIx64 " native=%p\n",
               s.pc, (unsigned)tb->size, (unsigned)tb->icount, gen_code_size,
               search_size, s.cflags, (uint64_t)s.flags, s.cs_base, tb->tc.ptr);
        for (int i = 0; i < tb->icount; i++) {
            printf("NATEMU_INSN index=%d end_offset=%u state=", i,
                   (unsigned)tcg_ctx->gen_insn_end_off[i]);
            for (int j = 0; j < INSN_START_WORDS; j++) {
                printf("%s%" PRIx64, j ? "," : "",
                       tcg_ctx->gen_insn_data[i * INSN_START_WORDS + j]);
            }
            putchar('\n');
        }
        fflush(NULL);
        _exit(0);
    }
''' + marker, 1)
p.write_text(s)

p = root / 'tcg/aarch64/tcg-target.c.inc'
s = p.read_text()
marker = 'static void tcg_target_init(TCGContext *s)\n{'
assert s.count(marker) == 1
p.write_text(s.replace(marker, marker + '''
    fprintf(stderr, "NATEMU_HOST cpuinfo=%lx lse2=%d profile=native-aarch64-software-ordering\\n",
            (unsigned long)cpuinfo, (int)have_lse2);
''', 1))

# Runnable static check: source pin drift or an export past execution fails above.
assert '_exit(94);' in (root / 'accel/tcg/cpu-exec.c').read_text()
assert s.count(marker) == 1
