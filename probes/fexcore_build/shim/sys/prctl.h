#pragma once
static inline int prctl(int a, ...) { (void)a; return -1; }
#define PR_SET_NAME 15
#define PR_GET_NAME 16
#define PR_SET_VMA 0x53564d41
#define PR_SET_VMA_ANON_NAME 0
#define PR_GET_DUMPABLE 3
#define PR_SET_DUMPABLE 4
