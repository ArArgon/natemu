#include "x86.h"

int x86_canonical(u64 va) {
    return (va >> 47) == 0 || (va >> 47) == 0x1ffff;
}

// Pure, bounded four-level decoder. The caller owns synchronization and A/D
// mutation; this function never repairs memory or substitutes a data access.
void x86_walk(const unsigned char *memory, const struct x86_context *c,
              u64 va, unsigned access, struct x86_walk *r) {
    r->status = X86_OK; r->error = 0; r->gpa = 0; r->page_size = 0;
    r->rights = X86_RIGHT_READ | X86_RIGHT_WRITE | X86_RIGHT_USER | X86_RIGHT_EXECUTE;
    r->count = 0;
    for (unsigned i = 0; i < 4; ++i) r->entry_gpa[i] = r->entry_value[i] = 0;
    if ((c->cpl != 0 && c->cpl != 3) || c->wp > 1 || c->nxe > 1 || c->pge > 1 ||
        access > X86_EXECUTE || (c->cr3 & ~(X86_PA_MASK | 0xfffUL))) {
        r->status = X86_CONTEXT_INVALID;
        return;
    }
    if (!x86_canonical(va)) { r->status = X86_GP; return; }
    if (c->cr3 & 0x18) { r->status = X86_ATTRIBUTES_UNSUPPORTED; return; }
    u64 table = c->cr3 & X86_PA_MASK;
    int attributes_supported = 1;
    u64 error = (access == X86_WRITE ? 2UL : 0) | (c->cpl == 3 ? 4UL : 0) |
                (access == X86_EXECUTE && c->nxe ? 16UL : 0);
    for (unsigned level = 4; level; --level) {
        u64 location = table + ((va >> (12 + 9 * (level - 1))) & 511) * 8;
        if (location > X86_RAM_SIZE - 8) { r->status = X86_RAM_UNSUPPORTED; return; }
        u64 value = *(const u64 *)(memory + location);
        unsigned index = r->count++;
        r->entry_gpa[index] = location; r->entry_value[index] = value;
        if (!(value & 1)) { r->status = X86_PF; r->error = error; return; }
        int large = level == 2 && (value & 128);
        // MAXPHYADDR=36, PDPE1GB=0, LA57=0. Software/ignored bits are not RSVD.
        if ((value & 0x000ffff000000000UL) || (!c->nxe && (value >> 63)) ||
            (level >= 3 && (value & 128)) || (large && (value & 0x1fe000))) {
            r->status = X86_PF; r->error = error | 9; return;
        }
        if ((value & 0x18) || (level == 1 && (value & 128)) || (large && (value & 4096)))
            attributes_supported = 0;
        if (!(value & 2)) r->rights &= ~X86_RIGHT_WRITE;
        if (!(value & 4)) r->rights &= ~X86_RIGHT_USER;
        if (value >> 63) r->rights &= ~X86_RIGHT_EXECUTE;
        if (level == 1 || large) {
            r->page_size = large ? 0x200000 : 4096;
            if ((c->cpl == 3 && !(r->rights & X86_RIGHT_USER)) ||
                (access == X86_WRITE && !(r->rights & X86_RIGHT_WRITE) && (c->cpl == 3 || c->wp)) ||
                (access == X86_EXECUTE && !(r->rights & X86_RIGHT_EXECUTE))) {
                r->status = X86_PF; r->error = error | 1; return;
            }
            r->gpa = ((value & X86_PA_MASK) & ~(r->page_size - 1)) | (va & (r->page_size - 1));
            if (!attributes_supported) { r->status = X86_ATTRIBUTES_UNSUPPORTED; return; }
            if (r->gpa >= X86_RAM_SIZE) r->status = X86_RAM_UNSUPPORTED;
            return;
        }
        table = value & X86_PA_MASK;
    }
}

// CMPXCHG compares RAX minus the destination. Preserve all other x86 flags.
u64 x86_cmp_flags(u64 accumulator, u64 destination, u64 previous_flags) {
    u64 difference = accumulator - destination;
    unsigned byte = (unsigned)difference & 255, parity = 0;
    for (unsigned i = 0; i < 8; ++i) parity ^= (byte >> i) & 1;
    u64 flags = (accumulator < destination ? 1UL : 0) |
                (!parity ? 4UL : 0) | ((accumulator ^ destination ^ difference) & 16) |
                (!difference ? 64UL : 0) | ((difference >> 63) << 7) |
                ((((accumulator ^ destination) & (accumulator ^ difference)) >> 63) << 11);
    return (previous_flags & ~0x8d5UL) | flags;
}
