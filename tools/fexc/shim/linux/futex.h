// Constants only; waits/wakes route through fexc_syscall (shim/fexc_platform.cpp).
#pragma once
#define FUTEX_WAIT 0
#define FUTEX_WAKE 1
#define FUTEX_WAIT_BITSET 9
#define FUTEX_WAKE_BITSET 10
#define FUTEX_PRIVATE_FLAG 128
#define FUTEX_BITSET_MATCH_ANY 0xffffffff
