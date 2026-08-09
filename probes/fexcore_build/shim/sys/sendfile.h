#pragma once
#include <sys/types.h>
static inline ssize_t sendfile(int a,int b,off_t*c,size_t d){(void)a;(void)b;(void)c;(void)d;return -1;}
