#pragma once
#include <stdlib.h>
#include <malloc/malloc.h>
static inline size_t malloc_usable_size(void* p) { return malloc_size(p); }
