// Linux sendfile(2) is only named by FHU::Filesystem::CopyFile; emulate with read/write.
#pragma once
#include <sys/types.h>
#include <unistd.h>
static inline ssize_t sendfile(int out_fd, int in_fd, off_t* offset, size_t count) {
  char buf[8192]; ssize_t total = 0;
  while (count) {
    ssize_t n = offset ? pread(in_fd, buf, count < sizeof(buf) ? count : sizeof(buf), *offset)
                       : read(in_fd, buf, count < sizeof(buf) ? count : sizeof(buf));
    if (n <= 0) return total ? total : n;
    if (write(out_fd, buf, n) != n) return -1;
    if (offset) *offset += n;
    total += n; count -= n;
  }
  return total;
}
