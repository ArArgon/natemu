// Force-included into every FEXCore translation unit (-include).
// Linux-only constants/calls that FEXCore headers name but a compile-only host
// tool never needs. Everything is a no-op or a fail-closed stub; see
// fexc_platform.cpp for the out-of-line parts.
#pragma once
// Host page size for guard pages / protections (guest pages stay FEX_PAGE_SIZE = 4 KiB).
#ifdef __APPLE__
#define FEXC_HOST_PAGE_SIZE 16384
#else
#define FEXC_HOST_PAGE_SIZE (FEXCore::Utils::FEX_PAGE_SIZE)
#endif
#ifdef __APPLE__
#include <string.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <math.h>
#ifndef MADV_HUGEPAGE
#define MADV_HUGEPAGE MADV_NORMAL
#define MADV_NOHUGEPAGE MADV_NORMAL
#endif
#ifndef MADV_DONTDUMP
#define MADV_DONTDUMP MADV_NORMAL
#endif
#ifndef MAP_FIXED_NOREPLACE
#define MAP_FIXED_NOREPLACE 0  /* FEXCore's 64-bit allocator hooks are never installed by this tool */
#endif
// Linux syscall numbers named by FEXHeaderUtils/Syscalls.h and friends. The
// numbers are only labels for fexc_syscall; they never reach the kernel.
// mremap(MREMAP_DONTUNMAP) is used only to finalize disk-cache pages, which the
// compiler tool never loads; fail closed if it is ever reached.
#define MREMAP_MAYMOVE 1
#define MREMAP_FIXED 2
#define MREMAP_DONTUNMAP 4
#ifdef __cplusplus
#include <stdlib.h>
static inline void* mremap(void*, size_t, size_t, int, ...) { abort(); }
#endif
// The compiler tool never executes generated code, so JIT buffers need not be
// PROT_EXEC (macOS arm64 refuses writable+executable anonymous mappings).
#define FEXC_EXEC_PROT(execute) 0
#define SYS_futex 1001
#define SYS_getcpu 168   // the arm64 Linux number: rdpid/rdtscp code embeds it (`mov x8, #168; svc`), InstructionCountCI checks it
#define SYS_tgkill 1003
#define SYS_statx 1004
#define SYS_renameat2 1005
#define SYS_getrandom 1006
#define SYS_pidfd_open 1007
#define SYS_rt_sigprocmask 1008
#ifdef __cplusplus
extern "C" long fexc_syscall(long number, ...);
// gettid exists as a Linux-only helper in FEXHeaderUtils (SYS_gettid is defined by macOS).
#define syscall fexc_syscall
#include <stdlib.h>
#include <malloc/malloc.h>
// glibc allocator extensions used by the host-tool allocator wrappers (Utils/AllocatorHooks.cpp).
static inline void* memalign(size_t a, size_t s) { void* p = nullptr; return posix_memalign(&p, a, s) ? nullptr : p; }
static inline size_t malloc_usable_size(void* p) { return malloc_size(p); }
static inline void sincos(double x, double* s, double* c) { __sincos(x, s, c); }
#endif
#endif
