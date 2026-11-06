#ifndef TRACK01_X86_H
#define TRACK01_X86_H
#include "shared.h"
#define X86_RAM_SIZE 0x400000UL
#define X86_RAM_IPA 0x40a00000UL
#define X86_PA_MASK 0xffffff000UL
enum { X86_READ, X86_WRITE, X86_EXECUTE };
enum { X86_OK, X86_GP = 13, X86_PF = 14, X86_RAM_UNSUPPORTED = 256,
       X86_ATTRIBUTES_UNSUPPORTED, X86_CONTEXT_INVALID };
enum { X86_RIGHT_READ = 1, X86_RIGHT_WRITE = 2, X86_RIGHT_USER = 4,
       X86_RIGHT_EXECUTE = 8 };
struct x86_context { u64 cr3, cpl, wp, nxe, pge; };
struct x86_walk {
    u64 status, error, gpa, page_size, rights, count;
    u64 entry_gpa[4], entry_value[4];
};
int x86_canonical(u64 va);
void x86_walk(const unsigned char *memory, const struct x86_context *context,
              u64 va, unsigned access, struct x86_walk *result);
u64 x86_cmp_flags(u64 accumulator, u64 destination, u64 previous_flags);
#endif
