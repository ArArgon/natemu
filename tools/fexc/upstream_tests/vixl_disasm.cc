// Disassemble 32-bit words with FEX's own vendored VIXL disassembler, exactly as JIT.cpp's
// Disassemble::BLOCKS prints them. stdin: one hex word per line (little-endian value);
// a line "#" separates groups and is echoed. Output: one line per word (raw vixl output, no sanitising).
#include "aarch64/decoder-aarch64.h"
#include "aarch64/disasm-aarch64.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
using namespace vixl::aarch64;
int main() {
  static char buf[1024];
  Disassembler dis(buf, sizeof buf);
  Decoder dec;
  dec.AppendVisitor(&dis);
  char line[64];
  // Instructions must stay at a stable address: decode from a heap block, one word at a time.
  alignas(16) static uint32_t word[4];
  while (fgets(line, sizeof line, stdin)) {
    if (line[0] == '#') { puts("#"); continue; }
    word[0] = (uint32_t)strtoul(line, nullptr, 16);
    dec.Decode(reinterpret_cast<const Instruction*>(word));
    puts(dis.GetOutput());
  }
}
