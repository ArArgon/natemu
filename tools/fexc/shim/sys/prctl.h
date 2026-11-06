// Linux prctl is unavailable; report "unsupported" (-1/EINVAL), which FEXCore treats as the no-feature case.
#pragma once
#include <errno.h>
#define PR_SET_NAME 15
#define PR_GET_MDWE 66
#ifdef __cplusplus
static inline int prctl(int, ...) { errno = EINVAL; return -1; }
#endif
