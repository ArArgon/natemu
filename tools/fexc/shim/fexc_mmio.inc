// natemu fexc (M3): completion records for device accesses. Included by fexc_slice1.inc inside namespace FexcSlice1
// (FEXCore::X86Tables in scope), called from Cut for every instruction slice 1 translates.
//
// A guest access to a device page (guest-physical, not RAM) aborts at its site; the resident completes it once through
// the host's device and then must commit the instruction without running it again. It does so from this record, made
// from FEX's own decode of the instruction it translated (never from the guest bytes at run time, which a peer may have
// rewritten since): MOV loads into a general register (x86 register, AH..BH, destination size, zero- or sign-extension
// from the access width) and MOV stores (the data is the site's native transfer register: what the translated
// instruction stores, an immediate or AH..BH included), and the instruction length. Only these forms get a record:
// 88/89/8A/8B with a memory operand, C6/C7 /0, A0..A3, 63 (MOVSXD), 0F B6/B7/BE/BF; with LOCK or REP/REPNE none. (I10: with
// 0x67 too: the device address is the site's, already the truncated one; the record holds no address.) fexc.cpp attaches it to the instruction's one load or store site when the site's width and
// direction agree (and the site is not half of a read-modify-write); every other device access stays a typed refusal.
//
// Record (u32, the binary site's `guest_offset2` slot of a load/store site; crates/abi code_object::completion):
//   bits 0-3 length, 4 store, 5-8 register, 9 high byte (AH..BH), 10-11 log2 destination size, 12-13 log2 width,
//   14 sign-extend, 31 set (30 clear: never FEXC_NONE32).
inline void FexcMmioRecord(const DecodedInst& d, uint64_t rip) {
  using namespace DecodeFlags;
  const uint32_t f = d.Flags;
  if (d.FexcMap != FEXC_MAP_PRIMARY && d.FexcMap != FEXC_MAP_TWO) return;
  if (f & (FLAG_LOCK | FLAG_REP_PREFIX | FLAG_REPNE_PREFIX)) return;
  const uint8_t op = d.OPRaw;
  const bool primary = d.FexcMap == FEXC_MAP_PRIMARY, modrm = f & FLAG_DECODED_MODRM;
  const bool memory = modrm && (d.ModRM >> 6) != 3;
  const auto bytes = [](uint32_t code) -> uint32_t { return code == SIZE_8BIT ? 1 : code == SIZE_16BIT ? 2 : code == SIZE_32BIT ? 4 : code == SIZE_64BIT ? 8 : 0; };
  const uint32_t dst = bytes((f >> FLAG_SIZE_DST_OFF) & SIZE_MASK), src = bytes((f >> FLAG_SIZE_SRC_OFF) & SIZE_MASK);
  uint32_t width = 0, dest = 0, reg = 0, store = 0, sign = 0;
  bool has_reg = false;
  if (primary && op >= 0x88 && op <= 0x8B && memory) {          // MOV r/m, r and MOV r, r/m (FEX's sizes: the operand's)
    store = !(op & 2);
    width = store ? dst : src;
    dest = width;
    has_reg = !store;
  } else if (primary && (op == 0xC6 || op == 0xC7) && memory && ((d.ModRM >> 3) & 7) == 0) {
    store = 1;
    width = dst;
  } else if (primary && op >= 0xA0 && op <= 0xA3) {             // moffs64: AL/rAX
    store = (op & 2) != 0;
    width = store ? dst : src;
    dest = width;
  } else if (primary && op == 0x63 && memory) {                 // MOVSXD as FEX lowers it (MOVSXDOp): 66 without REX.W a
    const bool w = f & FLAG_REX_WIDENING;                       // 16-bit MOV, REX.W sign-extends m32, else a 32-bit MOV
    width = (f & FLAG_OPERAND_SIZE) && !w ? 2 : 4; dest = w ? 8 : width; sign = w; has_reg = true;
  } else if (!primary && (op == 0xB6 || op == 0xB7 || op == 0xBE || op == 0xBF) && memory) {
    width = src; dest = dst; sign = op >= 0xBE; has_reg = true;
  } else {
    return;
  }
  const auto log2 = [](uint32_t n) -> int { return n == 1 ? 0 : n == 2 ? 1 : n == 4 ? 2 : n == 8 ? 3 : -1; };
  if (log2(width) < 0 || (!store && (log2(dest) < 0 || dest < width)) || d.InstSize == 0 || d.InstSize > 15) return;
  uint32_t high = 0;
  if (has_reg) {
    reg = ((d.ModRM >> 3) & 7) | ((f & FLAG_REX_XGPR_R) ? 8 : 0);
    if (dest == 1 && !(f & FLAG_REX_PREFIX) && reg >= 4 && reg < 8) { reg -= 4; high = 1; }   // AH, CH, DH, BH
  }
  fexc_mmio[rip] = uint32_t(d.InstSize) | store << 4 | reg << 5 | high << 9 | uint32_t(store ? 0 : log2(dest)) << 10 |
                   uint32_t(log2(width)) << 12 | sign << 14 | 1u << 31;
}
