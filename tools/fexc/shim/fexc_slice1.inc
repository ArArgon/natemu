// natemu (fexc) profile slice1: the allowlist of slice-1 instruction forms, applied to FEX's own decode.
// Included from Core.cpp (patches/0005) just before GenerateIR; FEXCore::X86Tables is in scope there.
//
// The key is (opcode map, opcode byte, ModRM.reg where the opcode is a group) taken from the decoder's result
// (DecodedInst::FexcMap/OPRaw/ModRM, which survive a decode failure) plus the decoder's prefix flags. This is
// deliberate: FEX's table names are ambiguous ("MOV" is also 0f 20..23 and 8e) and FEX does not decode every
// system instruction (0f 20..23 are two bytes without ModRM there; lss, clac and others do not decode at all).
//
// Verdicts:  ALLOW         admitted, translate it
//            SYSTEM        a system instruction: privilege, descriptor, MSR, segment, interrupt, I/O, TLB or cache
//                          maintenance, flag-control (IF/IOPL/TF), or one whose value belongs to the VM (cpuid, tsc)
//            UNSUPPORTED   anything else outside slice 1 (including admitted opcodes with a prefix or form that is not)
// A SYSTEM verdict with `ud` (I10) is an encoding the presented CPU raises #UD for whatever its operands and gating: reserved
// or unadvertised (SDM Vol.3 6.15 "#UD: ... an invalid or reserved opcode"; Vol.2 per instruction "#UD If CPUID ... = 0",
// "#UD If VEX.L = 1" and the like). fexc reports it as `cut_ud`, the host answers `system::UD` without decoding it.
// SYSTEM wins over prefix checks (a rep insb is a system instruction); the allowlist never admits an
// instruction that did not decode.
namespace FexcSlice1 {
using namespace FEXCore::X86Tables;

enum Kind : uint8_t { ALLOW, SYSTEM, UNSUPPORTED };
// `simd`: an SSE form, gated by CR0.EM/TS and CR4.OSFXSR (I5a); `align`: its 16-byte memory operand must be 16-byte aligned.
// `x87` (I6): 1 = a documented x87 form (d8..df: #NM under CR0.EM or CR0.TS whatever else it is), 2 = WAIT/FWAIT (9b: #NM
// under CR0.TS with CR0.MP). `gated`: cut because of that gating (the host's `cut_gated`). `avx` (I8): a VEX-encoded AVX/AVX2/
// FMA/F16C form, gated by CR4.OSXSAVE, XCR0[2:1] and CR0.TS; `align32`: its 32-byte memory operand must be 32-byte aligned.
// `mmx` (I9): an MMX form on MM registers only (gated by CR0.EM: #UD, and CR0.TS: #NM; not by CR4.OSFXSR).
struct Verdict { Kind kind; bool branch; const char* why; bool simd = false; bool align = false; uint8_t x87 = 0; bool gated = false;
                 bool avx = false; bool align32 = false; bool mmx = false; bool ud = false; };

inline Verdict Allow(bool branch = false) { return {ALLOW, branch, ""}; }
inline Verdict Sys(const char* why) { return {SYSTEM, false, why}; }
inline Verdict Uns(const char* why) { return {UNSUPPORTED, false, why}; }
inline Verdict Ud(const char* why) { Verdict v {SYSTEM, false, why}; v.ud = true; return v; }

// Primary opcode map (also reached through FEX's arch-dispatch entries for 63 and a0..a3).
inline Verdict Primary(uint8_t op, bool has_modrm, uint8_t mod, uint8_t reg) {
  if (op < 0x40 && (op & 7) < 6) return Allow();                    // add/or/adc/sbb/and/sub/xor/cmp, all 6 forms each
  switch (op) {
  case 0x06: case 0x07: case 0x0E: case 0x16: case 0x17: case 0x1E: case 0x1F: return Sys("push/pop of a segment register");
  case 0x8C: case 0x8E: return Sys("segment register move");
  case 0x9A: case 0xEA: case 0xCA: case 0xCB: case 0xCF: return Sys("far transfer or iret");
  case 0xCC: case 0xCD: case 0xCE: case 0xF1: return Sys("software interrupt");
  case 0x9D: case 0xFA: case 0xFB: return Sys("flag control (popf/cli/sti: IF, IOPL, AC, TF are the resident's)");
  // pushf reads TF/IF/IOPL/NT/AC/VIF/VIP/ID from the guest-state page (the resident writes them at every entry, RF/VM
  // as 0: the image clears them); DF lives there too (cld/std write it). clc/stc/cmc, lahf/sahf: arithmetic flags only.
  case 0x9C: case 0xFC: case 0xFD: case 0xF5: case 0xF8: case 0xF9: case 0x9E: case 0x9F: return Allow();
  case 0xC9: return Allow();                                       // leave
  case 0xD7: return Allow();                                       // xlat (I10: DS:[RBX + AL], or [EBX + AL] with 0x67; patch 0025)
  // movs/cmps/stos/lods/scas (I2): one element per pass, REP through an exit to the instruction's own RIP (patch 0015)
  case 0xA4: case 0xA5: case 0xA6: case 0xA7: case 0xAA: case 0xAB: case 0xAC: case 0xAD: case 0xAE: case 0xAF: return Allow();
  case 0xC8: return Allow();                                       // enter (nesting level 0 only, checked below)
  case 0xF4: return Sys("hlt");
  case 0xE4: case 0xE5: case 0xE6: case 0xE7: case 0xEC: case 0xED: case 0xEE: case 0xEF:
  case 0x6C: case 0x6D: case 0x6E: case 0x6F: return Sys("port I/O");
  case 0x63: case 0x68: case 0x6A: case 0x69: case 0x6B: case 0x84: case 0x85: case 0x88: case 0x89: case 0x8A: case 0x8B:
  case 0x8D: case 0x98: case 0x99: case 0xA0: case 0xA1: case 0xA2: case 0xA3: case 0xA8: case 0xA9: return Allow();
  case 0x80: case 0x81: case 0x83: return Allow();                  // group 1 (82 is invalid in 64-bit mode)
  case 0x86: case 0x87: return Allow();                             // xchg; with memory: implicit LOCK, FEX's swpal (S3e)
  // 8f /1../7: reserved in Intel's map (group 1A has /0 only); AMD's XOP prefix (8f with map_select >= 8, so ModRM.reg != 0)
  // is not advertised (GenuineIntel, CPUID.80000001H:ECX.XOP[11] = 0): #UD (I10).
  case 0x8F: return has_modrm && reg == 0 ? Allow() : Ud("8f /1../7: XOP (not advertised) or reserved (#UD)");
  case 0xC0: case 0xC1: case 0xD0: case 0xD1: case 0xD2: case 0xD3:
    if (!has_modrm || reg == 6) return Uns("shift group /6 alias");
    // FEX branches around the whole access for a static or dynamic zero count of rcl/rcr, losing a guest #PF.
    if ((reg == 2 || reg == 3) && mod != 3) return Uns("rcl/rcr with a memory operand (FEX skips the access for a zero count)");
    return Allow();
  // Group 11 /1../6 is reserved; /7 is XABORT (c6 f8 ib) / XBEGIN (c7 f8 rel), #UD with CPUID.7.0:EBX.RTM[11] = 0 (I10).
  case 0xC6: case 0xC7: return has_modrm && reg == 0 ? Allow() : Ud("group 11 other than mov: RTM xabort/xbegin (not advertised) or reserved (#UD)");
  case 0xF6: case 0xF7:
    if (!has_modrm) return Uns("group 3 without ModRM");
    return reg == 1 ? Uns("test /1 alias") : Allow();
  case 0xFE: return has_modrm && reg <= 1 ? Allow() : Ud("group 4 /2../7: reserved (#UD)");
  case 0xFF:
    if (!has_modrm) return Uns("group 5 without ModRM");
    if (reg == 3 || reg == 5) return Sys("far call/jmp");
    if (reg == 2 || reg == 4) return Allow(true);
    return reg <= 1 || reg == 6 ? Allow() : Ud("group 5 /7: reserved (#UD)");
  case 0xC2: case 0xC3: case 0xE8: case 0xE9: case 0xEB: return Allow(true);
  case 0xE0: case 0xE1: case 0xE2: case 0xE3: return Allow(true);  // loopne/loope/loop/jrcxz rel8 (two exits, both forward; 67: ECX)
  // Invalid in 64-bit mode (SDM Vol.2 each: "#UD If in 64-bit mode"; opcode map "i64"): DAA, DAS, AAA, AAS, PUSHA, POPA, AAM, AAD,
  // 82 (group 1 alias); 62 is the EVEX prefix there (AVX-512 not advertised: #UD) (I10).
  case 0x27: case 0x2F: case 0x37: case 0x3F: case 0x60: case 0x61: case 0xD4: case 0xD5: case 0x82:
    return Ud("invalid in 64-bit mode (#UD)");
  case 0x62: return Ud("EVEX (AVX-512 not advertised: #UD)");
  default: break;
  }
  if (op >= 0x50 && op <= 0x5F) return Allow();                     // push/pop r
  if (op >= 0x70 && op <= 0x7F) return Allow(true);                 // jcc rel8
  if (op >= 0x90 && op <= 0x97) return Allow();                     // nop, xchg rax,r
  if (op >= 0xB0 && op <= 0xBF) return Allow();                     // mov r, imm
  return Uns("not in the slice-1 allowlist");
}

// 0f xx. `plain` = the entry came from the base two-byte table (no mandatory-prefix overlay).
inline Verdict Two(uint8_t op, bool plain, bool has_modrm, uint8_t mod, uint8_t reg) {
  switch (op) {
  case 0x00: case 0x01: return Sys("descriptor/system group (sldt..ltr, sgdt..invlpg, swapgs, rdtscp, xgetbv, ...)");
  case 0x02: case 0x03: return Sys("lar/lsl");
  case 0x05: case 0x07: case 0x34: case 0x35: return Sys("syscall/sysret/sysenter/sysexit");
  case 0x06: return Sys("clts");
  case 0x08: case 0x09: return Sys("invd/wbinvd");
  case 0x0B: case 0xB9: case 0xFF: return Sys("ud2/ud1/ud0 (raises #UD)");
  case 0x20: case 0x21: case 0x22: case 0x23: return Sys("mov to/from control/debug register");
  case 0x30: case 0x32: case 0x33: return Sys("wrmsr/rdmsr/rdpmc");
  case 0x31: return Sys("rdtsc");
  case 0x78: case 0x79: return Sys("vmread/vmwrite");
  case 0xA0: case 0xA1: case 0xA8: case 0xA9: return Sys("push/pop fs/gs");
  case 0xA2: return Sys("cpuid");
  case 0xB2: case 0xB4: case 0xB5: return Sys("lss/lfs/lgs");
  case 0xAE:
    if (has_modrm && mod != 3 && reg == 7) return Sys("clflush/clflushopt (cache maintenance)");
    if (has_modrm && mod == 3 && reg < 4) return Sys("rd/wr fs/gs base");
    // I5b: fxsave/fxrstor and xsave/xrstor m (the resident's transactions); /6 m is xsaveopt, (66) clwb or (F3) clrssbsy,
    // none advertised (#UD). With F3, /4 m is ptwrite (not advertised either), which the host does not decode (a stop).
    if (has_modrm && mod != 3 && (reg <= 1 || (reg >= 4 && reg <= 6)))
      return Sys("fxsave/fxrstor/xsave/xrstor m, xsaveopt/clwb/clrssbsy m (#UD), or (F3 /4) ptwrite (not decoded)");
    return Uns("ldmxcsr/stmxcsr with a prefix, a fence with another ModRM");
  // rdrand (/6), rdseed and rdpid (/7, F3): the entropy service and TSC_AUX are the resident's (S3).
  case 0xC7:
    if (has_modrm && mod == 3 && reg >= 6) return Sys("rdrand/rdseed/rdpid");
    if (has_modrm && mod != 3 && reg >= 3 && reg <= 5) return Sys("xrstors/xsavec/xsaves (not advertised: #UD)");
    break;
  default: break;
  }
  if (!plain) return Uns("mandatory-prefix (F3/F2/66) overlay opcode");
  switch (op) {
  case 0x1F: return Allow();                                        // multi-byte nop
  case 0xA4: case 0xA5: case 0xAC: case 0xAD: return Allow();       // shld/shrd (16-bit refused below)
  case 0xB0: case 0xB1: case 0xC0: case 0xC1: return Allow();       // cmpxchg/xadd, with or without LOCK (I3)
  // cmpxchg8b/16b (I3): FEX's caspal with or without LOCK (an unlocked one is atomic too, which x86 allows); the other
  // group-9 forms (rdrand, rdseed, rdpid, vmptrld...) are not admitted.
  case 0xC7: return has_modrm && mod != 3 && reg == 1 ? Allow() : Uns("group 9 other than cmpxchg8b/16b m");
  case 0x18: return has_modrm && mod != 3 && reg <= 3 ? Allow() : Uns("0f 18 other than prefetchnta/t0/t1/t2 m8");
  case 0xC3: return has_modrm && mod != 3 ? Allow() : Uns("movnti with a register operand");
  case 0x0D: return Uns("prefetchw/0f 0d: PREFETCHW is not advertised (cpu_profile 0x80000001.ecx.8)");
  case 0xA6: case 0xA7: return Ud("0f a6/a7: VIA PadLock (not advertised; reserved in Intel's map: #UD)");
  case 0xA3: case 0xAB: case 0xB3: case 0xBB: case 0xAF: case 0xB6: case 0xB7: case 0xBE: case 0xBF:
  case 0xBC: case 0xBD: return Allow();                             // bt/bts/btr/btc, imul, movzx/movsx, bsf/bsr
  case 0xBA: return has_modrm && reg >= 4 ? Allow() : Ud("group 8 /0../3: reserved (#UD)");
  default: break;
  }
  if (op >= 0x40 && op <= 0x4F) return Allow();                     // cmovcc
  if (op >= 0x80 && op <= 0x8F) return Allow(true);                 // jcc rel32
  if (op >= 0x90 && op <= 0x9F) return Allow();                     // setcc
  if (op >= 0xC8 && op <= 0xCF) return Allow();                     // bswap
  return Uns("not in the slice-1 allowlist");
}

// VEX map (FEXC_MAP_VEX): only the BMI1/BMI2 general-register forms the profile advertises. `vex` = map_select | pp << 2 |
// L << 4. VEX.L = 1 is #UD for all of them.
inline Verdict Vex(uint8_t vex, uint8_t op, bool has_modrm, uint8_t reg) {
  const uint8_t map = vex & 3, pp = vex >> 2 & 3;
  if (vex & 16) return Uns("VEX.L = 1 (BMI forms #UD)");
  if (map == 2) {
    switch (op) {
    case 0xF2: if (pp == 0) return Allow(); break;                                     // andn
    case 0xF3: if (pp == 0 && has_modrm && reg >= 1 && reg <= 3) return Allow(); break; // blsr/blsmsk/blsi
    case 0xF5:
      if (pp == 0) return Allow();                                                     // bzhi
      if (pp >= 2) return Allow();                                                     // pdep, pext (I9: patch 0024's straight-line lowering)
      break;
    case 0xF6: if (pp == 3) return Allow(); break;                                     // mulx
    case 0xF7: return Allow();                                                         // bextr/shlx/sarx/shrx
    default: break;
    }
  }
  if (map == 3 && op == 0xF0 && pp == 3) return Allow();                               // rorx
  return Uns("VEX form other than BMI1/BMI2 (AVX: chunk I8)");
}

// The VEX prefix as encoded (SDM Vol.2 2.3.5): C5 (map 0F, W 0) or C4 (map_select, W); vvvv and the register-extension bits
// inverted. `legacy`: a 66, F2 or F3 prefix before it, or a REX immediately before it (#UD, SDM Vol.2 2.3.2); `rex_far`: a
// REX before it with another prefix in between (no SDM text: refused).
struct VexBytes { bool ok = false, legacy = false, rex_far = false; uint8_t map = 0, pp = 0, L = 0, W = 0, vvvv = 0, R = 0, X = 0; unsigned op = 0; };
inline VexBytes ParseVex(const uint8_t* b, unsigned n) {
  VexBytes v;
  unsigned i = 0;
  bool rex = false;
  for (; i < n; ++i) {
    const uint8_t x = b[i];
    if (x >= 0x40 && x <= 0x4F) { rex = true; continue; }
    if (x == 0x66 || x == 0xF2 || x == 0xF3) v.legacy = true;
    else if (!(x == 0x26 || x == 0x2E || x == 0x36 || x == 0x3E || x == 0x64 || x == 0x65 || x == 0x67 || x == 0xF0)) break;
    if (rex) { v.rex_far = true; rex = false; }
  }
  if (rex) v.legacy = true;
  if (i + 2 >= n) return v;
  if (b[i] == 0xC5) {
    v.map = 1; v.W = 0; v.vvvv = (~b[i + 1] >> 3) & 15; v.L = (b[i + 1] >> 2) & 1; v.pp = b[i + 1] & 3; v.ok = true;
    v.R = (~b[i + 1] >> 7) & 1; v.op = i + 2;
  } else if (b[i] == 0xC4 && i + 3 < n) {
    v.map = b[i + 1] & 31; v.W = b[i + 2] >> 7; v.vvvv = (~b[i + 2] >> 3) & 15; v.L = (b[i + 2] >> 2) & 1; v.pp = b[i + 2] & 3; v.ok = true;
    v.R = (~b[i + 1] >> 7) & 1; v.X = (~b[i + 1] >> 6) & 1; v.op = i + 3;
  }
  return v;
}

// AVX, AVX2, FMA and F16C (I8; the profile advertises all four: CPUID.1:ECX 28, 12, 29 and CPUID.7.0:EBX 5) on XMM/YMM
// registers, VEX-encoded (SDM Vol.2 chapters 3-5 per instruction, 2.3, 2.5). FEX's AVX128 lowering (no SVE256 on the M4):
// each 256-bit operation as two 128-bit halves, the upper halves in the guest-state page (`avx_high`, frame::AVX_HIGH), a
// 32-byte memory operand as one LDP/STP of two Q registers. Rows: the VEX.L values the form has (L0: 128-bit only, L1:
// 256-bit only; a scalar "LIG" form is admitted with L = 0 only, by policy), a VEX.W requirement (W0/W1, else WIG), NOV:
// VEX.vvvv is reserved (1111b, else #UD: refused), MEM/REG: memory or register operand only, ALIGN: VMOVAPS/VMOVAPD/
// VMOVDQA/VMOVNTPS/VMOVNTPD/VMOVNTDQ/VMOVNTDQA, whose operand must be aligned to its size (#GP(0); every other VEX form
// has no alignment requirement, Vol.2 2.5 exception class 4). Anything else is refused (`unsupported_form`) with its reason;
// `avx` is also set on the refused forms x86 defines, so their gating (#UD/#NM) is delivered first.
inline Verdict Avx(uint8_t map, uint8_t pp, uint8_t L, uint8_t W, uint8_t vvvv, uint8_t op, bool has_modrm, uint8_t mod, uint8_t reg) {
  enum : uint16_t { L0 = 1, L1 = 2, LL = 3, W0 = 4, W1 = 8, NOV = 16, MEM = 32, REG = 64, ALIGN = 128, NONE = 0x8000 };
  auto in = [](uint8_t v, std::initializer_list<int> l) { for (int x : l) if (x == v) return true; return false; };
  // A form x86 defines but I8 refuses: its own row first, so an encoding x86 makes #UD stays an encoding refusal (never a
  // gated #UD/#NM); `refuse` is returned (avx set, so a valid encoding is gated first) only after the encoding checks.
  const char* refuse = nullptr;
  bool mxcsr = false;
  uint16_t r = NONE;
  if (map == 1 && pp == 0) {
    if (in(op, {0x10, 0x11, 0x51, 0x52, 0x53, 0x5a, 0x5b})) r = LL | NOV;
    else if (op == 0x12 || op == 0x16) r = L0;                                   // vmovlps/vmovhlps, vmovhps/vmovlhps
    else if (op == 0x13 || op == 0x17) r = L0 | NOV | MEM;
    else if (in(op, {0x14, 0x15, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59, 0x5c, 0x5d, 0x5e, 0x5f, 0xc2, 0xc6})) r = LL;
    else if (op == 0x28 || op == 0x29) r = LL | NOV | ALIGN;
    else if (op == 0x2b) r = LL | NOV | MEM | ALIGN;
    else if (op == 0x2e || op == 0x2f) r = L0 | NOV;
    else if (op == 0x50) r = LL | NOV | REG;
    else if (op == 0x77) r = LL | NOV;                                           // vzeroupper (L0), vzeroall (L1)
    else if (op == 0xae && has_modrm && (reg == 2 || reg == 3)) { r = L0 | NOV | MEM; mxcsr = true; }   // vldmxcsr/vstmxcsr m32
  } else if (map == 1 && pp == 1) {
    if (in(op, {0x10, 0x11, 0x51, 0x5a, 0x5b, 0x70, 0xe6})) r = LL | NOV;
    else if (op == 0x12 || op == 0x16) r = L0 | MEM;                             // vmovlpd/vmovhpd xmm, xmm, m64
    else if (op == 0x13 || op == 0x17) r = L0 | NOV | MEM;
    else if (in(op, {0x28, 0x29, 0x6f, 0x7f})) r = LL | NOV | ALIGN;
    else if (op == 0x2b || op == 0xe7) r = LL | NOV | MEM | ALIGN;
    else if (op == 0x2e || op == 0x2f || op == 0x6e || op == 0x7e || op == 0xd6) r = L0 | NOV;
    else if (op == 0x50 || op == 0xd7) r = LL | NOV | REG;
    else if (op == 0xc4) r = L0;
    else if (op == 0xc5) r = L0 | NOV | REG;
    else if (op == 0x71 || op == 0x72 || op == 0x73) {                          // shifts by imm8: VEX.vvvv is the destination
      if (has_modrm && mod == 3 && (reg == 2 || reg == 6 || (op != 0x73 && reg == 4) || (op == 0x73 && (reg == 3 || reg == 7)))) r = LL;
    } else if (op == 0xf7) { r = L0 | NOV | REG; refuse = "vmaskmovdqu: a byte-masked store (FEX stores all 16 bytes)"; }
    else if (in(op, {0x14, 0x15, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59, 0x5c, 0x5d, 0x5e, 0x5f, 0x74, 0x75, 0x76, 0x7c, 0x7d, 0xc2, 0xc6, 0xd0}) ||
             (op >= 0x60 && op <= 0x6d) || (op >= 0xd1 && op <= 0xd5) || (op >= 0xd8 && op <= 0xe5) || (op >= 0xe8 && op <= 0xef) ||
             (op >= 0xf1 && op <= 0xf6) || (op >= 0xf8 && op <= 0xfe))
      r = LL;
  } else if (map == 1 && pp == 2) {
    if (op == 0x10 || op == 0x11) r = mod == 3 ? L0 : L0 | NOV;                  // vmovss: NDS for registers only
    else if (in(op, {0x12, 0x16, 0x5b, 0x6f, 0x70, 0x7f, 0xe6})) r = LL | NOV;
    else if (in(op, {0x2a, 0x51, 0x52, 0x53, 0x58, 0x59, 0x5a, 0x5c, 0x5d, 0x5e, 0x5f, 0xc2})) r = L0;
    else if (op == 0x2c || op == 0x2d || op == 0x7e) r = L0 | NOV;
  } else if (map == 1 && pp == 3) {
    if (op == 0x10 || op == 0x11) r = mod == 3 ? L0 : L0 | NOV;                  // vmovsd
    else if (in(op, {0x12, 0x70, 0xe6})) r = LL | NOV;
    else if (in(op, {0x2a, 0x51, 0x58, 0x59, 0x5a, 0x5c, 0x5d, 0x5e, 0x5f, 0xc2})) r = L0;
    else if (op == 0x2c || op == 0x2d) r = L0 | NOV;
    else if (in(op, {0x7c, 0x7d, 0xd0})) r = LL;
    else if (op == 0xf0) r = LL | NOV | MEM;                                     // vlddqu
  } else if (map == 2 && pp == 1) {
    if (op <= 0x0b || in(op, {0x28, 0x29, 0x2b, 0x37, 0x38, 0x39, 0x3a, 0x3b, 0x3c, 0x3d, 0x3e, 0x3f, 0x40, 0x45, 0x47})) r = LL;
    else if (op == 0x0c || op == 0x0d || op == 0x46) r = LL | W0;
    else if (in(op, {0x0e, 0x0f, 0x13, 0x18, 0x58, 0x59, 0x78, 0x79})) r = LL | W0 | NOV;
    else if (op == 0x16 || op == 0x36) r = L1 | W0;                              // vpermps, vpermd
    else if (op == 0x17 || (op >= 0x1c && op <= 0x1e) || (op >= 0x20 && op <= 0x25) || (op >= 0x30 && op <= 0x35)) r = LL | NOV;
    else if (op == 0x19) r = L1 | W0 | NOV;                                      // vbroadcastsd
    else if (op == 0x1a || op == 0x5a) r = L1 | W0 | NOV | MEM;                  // vbroadcastf128, vbroadcasti128
    else if (op == 0x2a) r = LL | NOV | MEM | ALIGN;                             // vmovntdqa
    else if (op == 0x41) r = L0 | NOV;                                           // vphminposuw
    else if (in(op, {0x96, 0x97, 0x98, 0x9a, 0x9c, 0x9e, 0xa6, 0xa7, 0xa8, 0xaa, 0xac, 0xae, 0xb6, 0xb7, 0xb8, 0xba, 0xbc, 0xbe})) r = LL;
    else if (in(op, {0x99, 0x9b, 0x9d, 0x9f, 0xa9, 0xab, 0xad, 0xaf, 0xb9, 0xbb, 0xbd, 0xbf})) r = L0;   // FMA scalar
    else if (in(op, {0x2c, 0x2d, 0x2e, 0x2f, 0x8c, 0x8e})) {
      r = LL | MEM | (op <= 0x2f ? W0 : 0);
      refuse = "vmaskmovps/pd, vpmaskmovd/q: FEX's masked load and store are element loops (branches over accesses)";
    } else if (op >= 0x90 && op <= 0x93) { r = LL | MEM; refuse = "vgather*/vpgather*: FEX's gather is an element loop (one site per element)"; }
    else if (op == 0xdb) r = L0 | NOV;                                           // vaesimc (I9)
    else if (op >= 0xdc && op <= 0xdf) r = L0;                                   // vaesenc/last, vaesdec/last (I9; VAES's L1 not advertised)
  } else if (map == 3 && pp == 1) {
    if (op == 0x00 || op == 0x01) r = L1 | W1 | NOV;                             // vpermq, vpermpd
    else if (op == 0x02 || op == 0x4a || op == 0x4b || op == 0x4c) r = LL | W0;  // vpblendd, vblendvps/pd, vpblendvb
    else if (op == 0x04 || op == 0x05 || op == 0x1d) r = LL | W0 | NOV;          // vpermilps/pd imm8, vcvtps2ph
    else if (op == 0x06 || op == 0x18 || op == 0x38 || op == 0x46) r = L1 | W0;  // vperm2f128, vinsertf128, vinserti128, vperm2i128
    else if (op == 0x19 || op == 0x39) r = L1 | W0 | NOV;                        // vextractf128, vextracti128
    else if (op == 0x08 || op == 0x09) r = LL | NOV;
    else if (op == 0x0a || op == 0x0b || op == 0x20 || op == 0x21 || op == 0x22 || op == 0x41) r = L0;
    else if (in(op, {0x0c, 0x0d, 0x0e, 0x0f, 0x40, 0x42})) r = LL;
    else if (op >= 0x14 && op <= 0x17) r = L0 | NOV;
    else if (op >= 0x60 && op <= 0x63) r = L0 | NOV;                             // vpcmpestrm/i, vpcmpistrm/i (I6b: svc #STRING)
    else if (op == 0x44) r = L0;                                                 // vpclmulqdq xmm (I9; the ymm form not advertised)
    else if (op == 0xdf) r = L0 | NOV;                                           // vaeskeygenassist (I9)
  }
  // I10: the encodings x86 makes #UD are delivered (`Ud`, before any gating: decode faults come first, SDM Vol.3 Table 6-2):
  // a VEX form the four advertised features do not define (AVX-VNNI, VAES/VPCLMULQDQ ymm, GFNI, AVX-512 mask forms, AMX...),
  // reserved VEX.L, VEX.W, VEX.vvvv or operand kind. Scalar LIG forms with VEX.L = 1 stay refused (policy: hardware ignores L).
  if (r == NONE) return Ud("VEX form the advertised AVX/AVX2/FMA/F16C/BMI1/BMI2/AES/PCLMULQDQ do not define (#UD)");
  if (!(r & (L ? L1 : L0))) {
    const bool lig = L && in(op, {0x10, 0x11, 0x2a, 0x2c, 0x2d, 0x2e, 0x2f, 0x51, 0x52, 0x53, 0x58, 0x59, 0x5a, 0x5c, 0x5d, 0x5e, 0x5f, 0xc2}) &&
                     map == 1 && (pp >= 2 || ((op == 0x2e || op == 0x2f) && pp <= 1));
    const bool fma_scalar = L && map == 2 && pp == 1 && in(op, {0x99, 0x9b, 0x9d, 0x9f, 0xa9, 0xab, 0xad, 0xaf, 0xb9, 0xbb, 0xbd, 0xbf});
    const bool rmwlig = L && map == 3 && pp == 1 && (op == 0x0a || op == 0x0b);   // vroundss/sd: LIG
    if (lig || fma_scalar || rmwlig) return Uns("VEX.L = 1 on a scalar LIG form (hardware ignores VEX.L: refused by policy)");
    return Ud(L ? "VEX.L = 1 on a 128-bit-only form (#UD)" : "VEX.L = 0 on a 256-bit-only form (#UD)");
  }
  if (((r & W0) && W) || ((r & W1) && !W)) return Ud("VEX.W the form does not define (#UD)");
  if ((r & NOV) && vvvv != 0) return Ud("VEX.vvvv not 1111b on a form without a VEX.vvvv operand (#UD)");
  if (op == 0x77 && map == 1) { Verdict v = Allow(); v.avx = true; return v; }
  if (!has_modrm || ((r & MEM) && mod == 3) || ((r & REG) && mod != 3)) return Ud("AVX form with an operand x86 makes #UD");
  if (refuse) { Verdict v = Uns(refuse); v.avx = true; return v; }
  // VLDMXCSR/VSTMXCSR m32: the resident's LDMXCSR/STMXCSR transaction (the host's VLDMXCSR/VSTMXCSR: no CR0.EM/CR4.OSFXSR
  // gating, the AVX gating being this cut's).
  if (mxcsr) { Verdict v = Sys("vldmxcsr/vstmxcsr (the resident's MXCSR transaction)"); v.avx = true; return v; }
  Verdict v = Allow();
  v.avx = true;
  v.align = mod != 3 && (r & ALIGN);
  v.align32 = v.align && L;
  return v;
}

// MMX (I9; CPUID.1:EDX.MMX[23], with SSE's, SSE2's and SSSE3's MMX-register forms) on MM0..MM7, which are the x87 registers:
// FEX keeps MMn as the low 64 bits of the x87 slot mm[n] (the slot of physical register n), the reduced-precision x87 a double
// there (plan I6), so an MMX value and an x87 value share the slot's bits without conversion (plan I9: the aliasing deviation).
// Every MMX instruction but EMMS sets TOP = 0 and every tag valid (patch 0024: at the instruction's end, after its accesses);
// EMMS empties the tags. Memory operands are m64, m32 for PUNPCKL* (patch 0024: FEX loaded 8 bytes) and MOVD, m16 for PINSRW;
// no alignment check. `mmx`: gated by CR0.EM (#UD) and CR0.TS (#NM) only (SDM Vol.3 Table 12-1; Vol.2 MMX instructions'
// exception tables), cut under the host's `--x87-gated`; the forms with an XMM operand (CVTPI2PS/PD, CVT(T)PS2PI, CVT(T)PD2PI,
// MOVQ2DQ, MOVDQ2Q) are SSE/SSE2 instructions with SSE's gating (`simd`). Refused: MASKMOVQ (a byte-masked store), the shift
// groups' encodings x86 makes #UD, 3DNow! (not advertised). Returns {UNSUPPORTED, .., nullptr} for an opcode that is no MMX form.
inline Verdict Mmx(uint8_t map, uint8_t pfx, uint8_t op, bool has_modrm, uint8_t mod, uint8_t reg) {
  const Verdict none {UNSUPPORTED, false, nullptr};
  auto in = [](uint8_t v, std::initializer_list<int> l) { for (int x : l) if (x == v) return true; return false; };
  bool pure = false, mixed = false, reg_only = false, mem_only = false, align = false;
  if (map == 1 && pfx == 0) {
    if ((op >= 0x60 && op <= 0x6b) || in(op, {0x6e, 0x6f, 0x70, 0x74, 0x75, 0x76, 0x7e, 0x7f, 0xc4}) || (op >= 0xd1 && op <= 0xd5) ||
        (op >= 0xd8 && op <= 0xe5) || (op >= 0xe8 && op <= 0xef) || (op >= 0xf1 && op <= 0xf6) || (op >= 0xf8 && op <= 0xfe))
      pure = true;
    else if (op == 0xc5 || op == 0xd7) pure = reg_only = true;                  // pextrw, pmovmskb
    else if (op == 0xe7) pure = mem_only = true;                                // movntq
    else if (op == 0x77) return [] { Verdict v = Allow(); v.mmx = true; return v; }();   // emms
    else if (op >= 0x71 && op <= 0x73) {                                        // psrl/psra/psll mm, imm8 (0f 73: /2 and /6 only)
      if (!(has_modrm && mod == 3 && (reg == 2 || reg == 6 || (op != 0x73 && reg == 4)))) return Ud("MMX shift group encoding x86 makes #UD");
      pure = reg_only = true;
    } else if (op == 0xf7) {   // refused, but gated first (#UD/#NM come before anything else: I8's rule)
      Verdict v = Uns("maskmovq: a byte-masked store (FEX stores all 8 bytes)");
      v.mmx = true;
      return v;
    }
    else if (in(op, {0x2a, 0x2c, 0x2d})) mixed = true;                          // cvtpi2ps, cvttps2pi, cvtps2pi (SSE)
  } else if (map == 1 && pfx == 0x66 && in(op, {0x2a, 0x2c, 0x2d})) {
    mixed = true;                                                               // cvtpi2pd; cvttpd2pi, cvtpd2pi xmm/m128 (aligned)
    align = op != 0x2a;
  } else if (map == 1 && (pfx == 0xf3 || pfx == 0xf2) && op == 0xd6) mixed = reg_only = true;   // movq2dq, movdq2q
  else if (map == 5 && pfx == 0 && (op <= 0x0b || (op >= 0x1c && op <= 0x1e))) pure = true;     // SSSE3 on MMX registers
  else if (map == 6 && pfx == 0 && op == 0x0f) pure = true;                                    // palignr mm
  if (!pure && !mixed) return none;
  if (!has_modrm || (reg_only && mod != 3) || (mem_only && mod == 3)) return Ud("MMX form with an operand x86 makes #UD");
  Verdict v = Allow();
  v.mmx = pure;
  v.simd = mixed;
  v.align = align && mod != 3;
  return v;
}

// SSE, SSE2, SSE3, SSSE3, SSE4.1, SSE4.2 (I5a; the profile advertises all six: CPUID.1 EDX 25/26, ECX 0/9/19/20) on XMM
// registers. `map` 1 = 0f, 5 = 0f 38, 6 = 0f 3a; `pfx` the mandatory prefix (0, 0x66, 0xf3, 0xf2) read from the bytes.
// Returns nullopt-like {UNSUPPORTED, .., why = nullptr} when the opcode is no SSE form here (the integer allowlist decides).
// Refused: MMX-register forms (x87/MMX state is I6's: cvtpi2ps/pd, cvt(t)ps2pi/pd2pi, movq2dq, movdq2q, the no-prefix 0f 38/
// 0f 3a and 0f 60..7f/c4/c5/d0..ff forms), maskmovdqu (a byte-masked store FEX lowers as a load, select and store of all
// 16 bytes: it would fault and write where x86 does not), SSE4a (not advertised). I9: AES and PCLMULQDQ (CPUID.1:ECX 25, 1)
// through FEX's Arm crypto lowering (AESE/AESD/AESMC/AESIMC with a zero round key and an EOR; PMULL/PMULL2). Memory-operand sizes and alignment: SDM Vol.2 per instruction and
// 2.5 (exception classes 1, 2, 4: a 16-byte operand must be 16-byte aligned, except movups/movupd/movdqu/lddqu).
inline Verdict Sse(uint8_t map, uint8_t pfx, uint8_t op, bool has_modrm, uint8_t mod, uint8_t reg) {
  const Verdict none {UNSUPPORTED, false, nullptr};
  enum : uint8_t { NO = 0, M128 = 1, SMALL = 2, UNALIGNED = 3 };   // NO: not an SSE form here
  auto in = [](uint8_t v, std::initializer_list<int> l) { for (int x : l) if (x == v) return true; return false; };
  uint8_t form = NO;
  bool reg_only = false, mem_only = false;
  if (map == 1 && pfx == 0) {
    if (in(op, {0x10, 0x11})) form = UNALIGNED;
    else if (in(op, {0x12, 0x13, 0x16, 0x17, 0x2e, 0x2f, 0x5a})) form = SMALL;
    else if (in(op, {0x14, 0x15, 0x28, 0x29, 0x2b, 0x50, 0xc2, 0xc6}) || (op >= 0x51 && op <= 0x5f)) form = M128;
    mem_only = in(op, {0x13, 0x17, 0x2b}); reg_only = op == 0x50;
  } else if (map == 1 && pfx == 0x66) {
    if (in(op, {0x10, 0x11})) form = UNALIGNED;
    else if (in(op, {0x12, 0x13, 0x16, 0x17, 0x2e, 0x2f, 0x6e, 0x7e, 0xd6, 0xc4})) form = SMALL;
    else if (in(op, {0x14, 0x15, 0x28, 0x29, 0x2b, 0x50, 0x51, 0x70, 0x74, 0x75, 0x76, 0x7c, 0x7d, 0x7f, 0xc2, 0xc5, 0xc6, 0xd7, 0xe7}) ||
             (op >= 0x54 && op <= 0x6f) || (op >= 0xd0 && op <= 0xd5) || (op >= 0xd8 && op <= 0xe6) || (op >= 0xe8 && op <= 0xef) ||
             (op >= 0xf1 && op <= 0xf6) || (op >= 0xf8 && op <= 0xfe))
      form = M128;
    else if (in(op, {0x71, 0x72, 0x73}) && has_modrm && mod == 3 && (reg == 2 || reg == 4 || reg == 6 || (op == 0x73 && (reg == 3 || reg == 7))) &&
             !(op == 0x73 && reg == 4))
      form = M128;
    mem_only = in(op, {0x13, 0x17, 0x2b, 0xe7}); reg_only = in(op, {0x50, 0xc5, 0xd7});
  } else if (map == 1 && pfx == 0xf3) {
    if (in(op, {0x6f, 0x7f})) form = UNALIGNED;
    else if (in(op, {0x12, 0x16, 0x5b, 0x70})) form = M128;
    else if (in(op, {0x10, 0x11, 0x2a, 0x2c, 0x2d, 0x51, 0x52, 0x53, 0x58, 0x59, 0x5a, 0x5c, 0x5d, 0x5e, 0x5f, 0x7e, 0xc2, 0xe6})) form = SMALL;
  } else if (map == 1 && pfx == 0xf2) {
    if (op == 0xf0) form = UNALIGNED;
    else if (in(op, {0x70, 0x7c, 0x7d, 0xd0, 0xe6})) form = M128;
    else if (in(op, {0x10, 0x11, 0x12, 0x2a, 0x2c, 0x2d, 0x51, 0x58, 0x59, 0x5a, 0x5c, 0x5d, 0x5e, 0x5f, 0xc2})) form = SMALL;
    mem_only = op == 0xf0;
  } else if (map == 5 && pfx == 0x66) {
    if ((op >= 0x20 && op <= 0x25) || (op >= 0x30 && op <= 0x35)) form = SMALL;
    else if (op <= 0x0b || in(op, {0x10, 0x14, 0x15, 0x17, 0x1c, 0x1d, 0x1e, 0x28, 0x29, 0x2a, 0x2b}) || (op >= 0x37 && op <= 0x41)) form = M128;
    else if (op >= 0xdb && op <= 0xdf) form = M128;        // aesimc, aesenc/last, aesdec/last (I9: class 4, aligned)
    mem_only = op == 0x2a;
  } else if (map == 6 && pfx == 0x66) {
    if (in(op, {0x0a, 0x0b, 0x14, 0x15, 0x16, 0x17, 0x20, 0x21, 0x22})) form = SMALL;
    else if ((op >= 0x08 && op <= 0x0f && op != 0x0a && op != 0x0b) || (op >= 0x40 && op <= 0x42)) form = M128;
    else if (op == 0x44 || op == 0xdf) form = M128;        // pclmulqdq, aeskeygenassist (I9: exception class 4, aligned)
    else if (op >= 0x60 && op <= 0x63) form = UNALIGNED;   // pcmpestrm/i, pcmpistrm/i: no alignment (SDM); I6b: svc #STRING
  }
  if (form == NO) {
    if (map == 1 && pfx == 0x66 && op == 0xf7) return Uns("maskmovdqu: FEX stores all 16 bytes (a masked store needs its own lowering)");
    if ((map == 5 || map == 6) && pfx == 0) return Ud("0f 38 / 0f 3a form without a prefix that is no MMX form: none advertised (#UD)");
    return none;
  }
  if (!has_modrm || (reg_only && mod != 3) || (mem_only && mod == 3)) return Ud("SSE form with an operand x86 makes #UD");
  Verdict v = Allow();
  v.simd = true;
  v.align = mod != 3 && form == M128;
  return v;
}

// x87 (I6), FEX's reduced-precision mode (doubles). `modrm` is the byte after d8..df. Admitted: loads, stores and integer
// conversions at every width but m80, the arithmetic and compares (FCOM forms compare signalling, as COMISS), FXCH, FCMOV,
// constants, FCHS/FABS/FSQRT/FRNDINT, FXAM (patch 0019: x86's classes of a double), FINCSTP/FDECSTP/FFREE/FNOP, FNSTSW and FNSTCW. The resident's (system cuts): FNINIT,
// FNCLEX, FLDCW, FLDENV/FNSTENV, FRSTOR/FNSAVE and FLD/FSTP m80 (FCW, the environment, 80-bit images). I6b: the transcendental,
// remainder, scale and extract forms run whole in the resident by `svc #X87` (patch 0023). Refused: FBLD/FBSTP (a host helper),
// the 287 no-ops FENI/FDISI/FSETPM, and the undocumented aliases (DC D0/D8, DD C8, DE D0, DF C8..DF) and reserved encodings.
// I9: FFREEP (DF C0+i) is admitted.
inline Verdict X87(uint8_t op, uint8_t modrm) {
  const uint8_t mod = modrm >> 6, reg = (modrm >> 3) & 7;
  const Verdict ok = Allow(), res = Sys("x87 control or state instruction (the resident's: FCW, environment, 80-bit image)");
  const Verdict alias = Uns("undocumented or reserved x87 encoding");
  if (mod != 3) {
    switch (op) {
    case 0xD8: case 0xDA: case 0xDC: case 0xDE: return ok;                       // arithmetic/compare with m32fp, m32int, m64fp, m16int
    case 0xD9: return reg == 1 ? alias : (reg >= 4 && reg <= 6) ? res : ok;      // fld/fst/fstp m32, fnstcw; fldenv, fldcw, fnstenv
    case 0xDB: return (reg == 4 || reg == 6) ? alias : (reg == 5 || reg == 7) ? res : ok;   // fild/fisttp/fist/fistp m32; fld/fstp m80
    case 0xDD: return reg == 5 ? alias : (reg == 4 || reg == 6) ? res : ok;      // fld/fisttp/fst/fstp m64, fnstsw m16; frstor, fnsave
    default: return (reg == 4 || reg == 6) ? Uns("fbld/fbstp (FEX calls a host helper for BCD)") : ok;   // df: fild/fisttp/fist/fistp m16, fild/fistp m64
    }
  }
  switch (op) {
  case 0xD8: return ok;                                                        // fadd..fdivr st0, st(i); fcom/fcomp st(i)
  case 0xD9:
    if (reg <= 1 || modrm == 0xD0) return ok;                                    // fld st(i), fxch st(i), fnop
    switch (modrm) {
    case 0xE0: case 0xE1: case 0xE4: case 0xE5: case 0xF6: case 0xF7: case 0xFA: case 0xFC: return ok;   // fchs fabs ftst fxam fdecstp fincstp fsqrt frndint
    case 0xE8: case 0xE9: case 0xEA: case 0xEB: case 0xEC: case 0xED: case 0xEE: return ok;   // fld1 fldl2t fldl2e fldpi fldlg2 fldln2 fldz
    case 0xF0: case 0xF1: case 0xF2: case 0xF3: case 0xF4: case 0xF5: case 0xF8: case 0xF9: case 0xFB: case 0xFD: case 0xFE: case 0xFF:
      return ok;   // f2xm1 fyl2x fptan fpatan fxtract fprem1 fprem fyl2xp1 fsincos fscale fsin fcos (I6b: the resident's, svc #X87)
    default: return alias;
    }
  case 0xDA: return reg <= 3 || modrm == 0xE9 ? ok : alias;                    // fcmovb/e/be/u, fucompp
  case 0xDB:
    if (reg <= 3 || reg == 5 || reg == 6) return ok;                             // fcmovnb/ne/nbe/nu, fucomi, fcomi
    if (modrm == 0xE2 || modrm == 0xE3) return res;                              // fnclex, fninit
    if (modrm == 0xE0 || modrm == 0xE1 || modrm == 0xE4) return Uns("feni/fdisi/fsetpm (8087/287 no-ops)");
    return alias;
  case 0xDC: return reg == 2 || reg == 3 ? alias : ok;                         // fadd..fdiv st(i), st0
  case 0xDD: return reg == 1 || reg >= 6 ? alias : ok;                         // ffree, fst/fstp st(i), fucom/fucomp
  case 0xDE: return reg == 2 || (reg == 3 && modrm != 0xD9) ? alias : ok;      // faddp fmulp fcompp fsubrp fsubp fdivrp fdivp
  // df: fnstsw ax, fucomip, fcomip; I9: FFREEP ST(i) (df c0+i, free ST(i) then pop; patch 0024): undocumented by Intel but executed
  // by Intel and AMD x87 units (the sources FEX's table cites: AMD Athlon x86 Code Optimization Guide, "Use FFREEP Macro to Pop One
  // Register from the FPU Stack"; pagetable.com/?p=16); QEMU and Bochs implement it (free, then pop).
  default: return modrm == 0xE0 || reg == 0 || reg == 5 || reg == 6 ? ok : alias;
  }
}

// LOCK (SDM Vol.2 "LOCK"): allowed only on the memory-destination forms of ADD, ADC, AND, BTC, BTR, BTS, CMPXCHG,
// CMPXCHG8B/16B, DEC, INC, NEG, NOT, OR, SBB, SUB, XOR, XADD and XCHG; on any other instruction, or one of these with a
// register destination, it raises #UD.
// Without a ModRM (`opcode_only`: FEX's decoder failed before it), whether some lockable form has this opcode.
inline bool Lockable(const DecodedInst& d, bool has_modrm, uint8_t mod, uint8_t reg, bool opcode_only = false) {
  const uint8_t op = d.OPRaw;
  if (opcode_only)
    return d.FexcMap == FEXC_MAP_PRIMARY ? (op < 0x38 && (op & 7) <= 1) || op == 0x80 || op == 0x81 || op == 0x83 || op == 0x86 ||
                                             op == 0x87 || op == 0xF6 || op == 0xF7 || op == 0xFE || op == 0xFF
         : d.FexcMap == FEXC_MAP_TWO && (op == 0xAB || op == 0xB3 || op == 0xBB || op == 0xBA || op == 0xB0 || op == 0xB1 ||
                                         op == 0xC0 || op == 0xC1 || op == 0xC7);
  if (!has_modrm || mod == 3) return false;
  if (d.FexcMap == FEXC_MAP_PRIMARY)
    return (op < 0x38 && (op & 7) <= 1) ||                                    // add/or/adc/sbb/and/sub/xor r/m, r
           ((op == 0x80 || op == 0x81 || op == 0x83) && reg != 7) ||          // group 1 but cmp
           op == 0x86 || op == 0x87 ||                                       // xchg
           ((op == 0xF6 || op == 0xF7) && (reg == 2 || reg == 3)) ||         // not, neg
           ((op == 0xFE || op == 0xFF) && reg <= 1);                         // inc, dec
  if (d.FexcMap == FEXC_MAP_TWO)
    return op == 0xAB || op == 0xB3 || op == 0xBB || (op == 0xBA && reg >= 5) ||   // bts/btr/btc r/m, r and /5../7 imm8
           op == 0xB0 || op == 0xB1 || op == 0xC0 || op == 0xC1 || (op == 0xC7 && reg == 1);   // cmpxchg, xadd, cmpxchg8b/16b
  return false;
}

// I9: 0f 38 / 0f 3a forms the presented CPU raises #UD for whatever their operands (cpu_profile.json): ADCX (66 0f 38 f6) and
// ADOX (F3 0f 38 f6; CPUID.7.0:EBX.ADX[19] = 0), WRSS (NP 0f 38 f6) and WRUSS (66 0f 38 f5; CPUID.7.0:ECX.CET_SS[7] = 0) and the
// rest of 0f 38 f5/f6, SHA (NP 0f 38 c8..cd, NP 0f 3a cc; CPUID.7.0:EBX.SHA[29] = 0), and MOVBE with a register operand (SDM
// MOVBE: #UD). The prefixes from the decoder's flags (set before a decode failure too). The host decodes each as `system::UD`;
// I10: F3 0f 38 f0/f1 (MOVBE with REP: #UD) is a `ud` cut (FEX's decode fails before its ModRM).
inline Verdict Undefined0F38(const DecodedInst& d, bool has_modrm, uint8_t mod) {
  const uint8_t op = d.OPRaw;
  const uint32_t f = d.Flags;
  const bool p66 = f & DecodeFlags::FLAG_OPERAND_SIZE, pf2 = f & DecodeFlags::FLAG_REPNE_PREFIX, pf3 = f & DecodeFlags::FLAG_REP_PREFIX;
  if (d.FexcMap == FEXC_MAP_0F38) {
    if ((op == 0xF0 || op == 0xF1) && !pf2 && !pf3 && has_modrm && mod == 3) return Sys("movbe with a register operand (#UD)");
    // I10: SDM MOVBE "#UD ... If REP (F3H) prefix is used" (FEX's decoder fails before the ModRM: any operand).
    if ((op == 0xF0 || op == 0xF1) && pf3) return Ud("movbe with an F3 prefix (#UD)");
    if (op == 0xF5 || op == 0xF6) return Sys("adcx/adox, wrss/wruss (ADX, CET_SS not advertised: #UD)");
    if (op >= 0xC8 && op <= 0xCD && !p66 && !pf2 && !pf3) return Sys("sha1*/sha256* (SHA not advertised: #UD)");
  }
  if (d.FexcMap == FEXC_MAP_0F3A && op == 0xCC && !p66 && !pf2 && !pf3) return Sys("sha1rnds4 (SHA not advertised: #UD)");
  return {UNSUPPORTED, false, nullptr};
}

inline Verdict Classify(const DecodedInst& d, bool decoded) {
  const bool has_modrm = d.Flags & DecodeFlags::FLAG_DECODED_MODRM;
  const uint8_t mod = d.ModRM >> 6, reg = (d.ModRM >> 3) & 7;
  const uint8_t op = d.OPRaw;
  const uint32_t f = d.Flags;
  // LOCK on a form that is not lockable: #UD, whatever the instruction (I3). A system cut, which the host decodes as a LOCK
  // #UD and the resident raises (a guest fault until delivery is built). FEX's decoder refuses many such forms itself (it
  // fails on LOCK with a register operand or a non-lockable opcode); after a failure the form is known not lockable from
  // its ModRM when that was decoded, else only from an opcode no lockable form has.
  if ((f & DecodeFlags::FLAG_LOCK) && (decoded || has_modrm ? !Lockable(d, has_modrm, mod, reg) : !Lockable(d, false, 0, 0, true)))
    return Sys("LOCK prefix on an instruction that is not lockable (#UD)");
  Verdict v = Undefined0F38(d, has_modrm, mod);
  if (v.why) return v;
  {
    // I10, #UD whatever the gating (decode faults come first, SDM Vol.3 Table 6-2):
    const bool two = d.FexcMap >= FEXC_MAP_TWO && d.FexcMap <= FEXC_MAP_TWO_66;
    const bool np66 = !(f & (DecodeFlags::FLAG_REP_PREFIX | DecodeFlags::FLAG_REPNE_PREFIX));
    // TPAUSE (66), UMONITOR (F3), UMWAIT (F2) 0f ae /6 register: "#UD If CPUID.7.0:ECX.WAITPKG[bit 5] = 0" (not advertised).
    // (Without a prefix, 0f ae /6 register is MFENCE.)
    if (two && op == 0xAE && has_modrm && mod == 3 && reg == 6 &&
        (f & (DecodeFlags::FLAG_OPERAND_SIZE | DecodeFlags::FLAG_REP_PREFIX | DecodeFlags::FLAG_REPNE_PREFIX)))
      return Ud("tpause/umonitor/umwait (WAITPKG not advertised: #UD)");
    // INCSSPD/INCSSPQ (F3 0f ae /5, register): SDM INCSSP "#UD If CR4.CET = 0" (CET is not advertised, so CR4.CET stays 0).
    if (two && op == 0xAE && has_modrm && mod == 3 && reg == 5 && (f & DecodeFlags::FLAG_REP_PREFIX) && !(f & DecodeFlags::FLAG_REPNE_PREFIX))
      return Ud("incssp (#UD with CR4.CET = 0; CET not advertised)");
    // The MMX/SSE shift groups 12-14 (0f 71..73, none or 66): only the register forms of /2 /4 /6 (0f 73: /2 /6, 66 0f 73 also
    // /3 /7) exist; FEX's decoder fails on the others, which are reserved (#UD).
    if (two && op >= 0x71 && op <= 0x73 && has_modrm && np66) {
      const bool p66 = f & DecodeFlags::FLAG_OPERAND_SIZE;
      const bool ok = mod == 3 && (reg == 2 || reg == 6 || (op != 0x73 && reg == 4) || (op == 0x73 && p66 && (reg == 3 || reg == 7)));
      if (!ok) return Ud("MMX/SSE shift group encoding x86 makes #UD");
    }
    // 3DNow! (0f 0f; CPUID.80000001H:EDX[31] = 0, not in Intel's map): #UD.
    if (d.FexcMap == FEXC_MAP_3DNOW) return Ud("3DNow! (not advertised: #UD)");
    // A VEX prefix FEX does not decode (an opcode no VEX feature FEX implements defines: AVX-VNNI, AMX, AVX-512 mask forms...):
    // none advertised, #UD; a 66/F2/F3/REX before it is #UD too (I8's vex_ud).
    if (!decoded && d.FexcMap == FEXC_MAP_VEX) return Ud("VEX form FEX does not decode: not advertised (#UD)");
  }
  // SSE (I5a): the mandatory prefix from the bytes (FEX takes the last of 66/F2/F3, x86 lets F2/F3 win over 66).
  // (Only a decoded instruction: after a decoder failure its size, so its prefix bytes, are unknown.)
  if (decoded && d.FexcMap >= FEXC_MAP_TWO && d.FexcMap <= FEXC_MAP_0F3A) {
    const uint8_t* b = fexc_guest_bytes ? fexc_guest_bytes(d.PC, d.InstSize) : nullptr;
    if (!b) return Uns("SSE prefixes: instruction bytes unavailable");
    bool p66 = false, pf2 = false, pf3 = false;
    for (unsigned i = 0; i < d.InstSize && b[i] != 0x0F; ++i) { p66 |= b[i] == 0x66; pf2 |= b[i] == 0xF2; pf3 |= b[i] == 0xF3; }
    const uint8_t pfx = pf3 ? 0xF3 : pf2 ? 0xF2 : p66 ? 0x66 : 0;
    const uint8_t map = d.FexcMap == FEXC_MAP_0F38 ? 5 : d.FexcMap == FEXC_MAP_0F3A ? 6 : 1;
    const uint8_t fexpfx = d.FexcMap == FEXC_MAP_TWO_66 ? 0x66 : d.FexcMap == FEXC_MAP_TWO_F3 ? 0xF3 : d.FexcMap == FEXC_MAP_TWO_F2 ? 0xF2 : 0;
    // CRC32 (SSE4.2, F2 0f 38 f0/f1; 66 for a 16-bit source): a general-register instruction, not gated by CR0/CR4.
    if (map == 5 && pf2 && !pf3 && (op == 0xF0 || op == 0xF1) && has_modrm) {
      if (!decoded) return Uns("did not decode");
      return Allow();
    }
    // I9: MOVBE m (CPUID.1:ECX.MOVBE[22]; 0f 38 f0 load, f1 store, 66 for 16 bits): FEX's plain load or store plus REV, so the
    // site, #PF/#GP/#SS and (natively, SCTLR.A) #AC are an ordinary access's. Not gated by CR0/CR4 (a general-register form).
    if (map == 5 && !pf2 && !pf3 && (op == 0xF0 || op == 0xF1) && has_modrm && mod != 3) v = Allow();
    else if (v = Mmx(map, pfx, op, has_modrm, mod, reg); v.why == nullptr) v = Sse(map, pfx, op, has_modrm, mod, reg);
    else if (v.mmx && v.kind != ALLOW && fexc_x87_gated) { v = Sys("MMX gated by CR0.EM/TS (#UD/#NM)"); v.mmx = true; v.gated = true; return v; }
    if (v.why == nullptr) v = {UNSUPPORTED, false, nullptr};
    // (FEX decodes the 0f 71..73 shift groups through its group table, keyed by the prefix itself, and reports map 1.)
    else if (v.kind == ALLOW && ((pf2 && pf3) || ((pf2 || pf3) && p66)))
      v = Uns("SSE form with several mandatory prefixes (66 with F2/F3, or F2 with F3)");
    else if (v.kind == ALLOW && map == 1 && fexpfx != pfx && !(op >= 0x71 && op <= 0x73))
      v = Uns("SSE form FEX decoded under another mandatory prefix");
    // INSERTPS m32 whose zero mask clears the inserted element: FEX drops the load, and with it the access's #PF.
    else if (v.kind == ALLOW && map == 6 && op == 0x21 && mod != 3 && (b[d.InstSize - 1] & 0xF) & (1 << (b[d.InstSize - 1] >> 4 & 3)))
      v = Uns("insertps m32 whose zero mask clears the inserted element (FEX drops the load and its #PF)");
    // LDMXCSR/STMXCSR m32 (0f ae /2, /3): MXCSR is the resident's (its gating, #GP for reserved bits, the exception flags).
    if (d.FexcMap == FEXC_MAP_TWO && op == 0xAE && has_modrm && mod != 3 && (reg == 2 || reg == 3)) return Sys("ldmxcsr/stmxcsr");
  }
  // x87 (I6): d8..df with their ModRM, WAIT/FWAIT. A documented form is cut as gated under CR0.EM/TS (#NM comes first,
  // whatever else the form is), WAIT under CR0.TS with CR0.MP; the prefixes are checked below as for any admitted form.
  if (d.FexcMap == FEXC_MAP_PRIMARY && ((op >= 0xD8 && op <= 0xDF && has_modrm) || op == 0x9B)) {
    v = op == 0x9B ? Allow() : X87(op, d.ModRM);
    v.x87 = op == 0x9B ? 2 : v.why && std::string_view(v.why).starts_with("undocumented") ? 0 : 1;
    if ((v.x87 == 1 && fexc_x87_gated) || (v.x87 == 2 && fexc_wait_gated)) {
      v = Sys(v.x87 == 1 ? "x87 instruction under CR0.EM or CR0.TS (#NM)" : "wait under CR0.TS and CR0.MP (#NM)");
      v.gated = true;
      return v;
    }
    if (v.kind == ALLOW && (f & (DecodeFlags::FLAG_OPERAND_SIZE | DecodeFlags::FLAG_REP_PREFIX | DecodeFlags::FLAG_REPNE_PREFIX)))
      v = Uns("66/F2/F3 prefix on an x87 form");
  }
  if (v.why == nullptr) switch (d.FexcMap) {
  case FEXC_MAP_PRIMARY: v = Primary(op, has_modrm, mod, reg); break;
  case FEXC_MAP_TWO: case FEXC_MAP_TWO_F2: case FEXC_MAP_TWO_66:
    v = Two(op, d.FexcMap == FEXC_MAP_TWO, has_modrm, mod, reg); break;
  case FEXC_MAP_TWO_F3:
    // popcnt, tzcnt, lzcnt: the profile advertises POPCNT, BMI1 and LZCNT, so f3 0f bc/bd are TZCNT/LZCNT (FEX
    // decodes them so unconditionally: X86Tables/SecondaryTables.cpp RepModOps).
    v = (op == 0xB8 || op == 0xBC || op == 0xBD) ? Allow() : Two(op, false, has_modrm, mod, reg); break;
  case FEXC_MAP_VEX: {
    // I8: a VEX form after a 66, F2 or F3 prefix, or right after a REX, is #UD (SDM Vol.2 2.3.2), whatever the gating: cut as a
    // system instruction (the host's `vex_ud`). Then the BMI forms (general registers, not gated), else the AVX table, whose
    // forms are gated by CR4.OSXSAVE, XCR0[2:1] and CR0.TS (the host's `--avx-gated`; the resident raises #UD or #NM).
    const uint8_t* b = decoded && fexc_guest_bytes ? fexc_guest_bytes(d.PC, d.InstSize) : nullptr;
    if (!b) return Uns("VEX: instruction bytes unavailable");
    const VexBytes x = ParseVex(b, d.InstSize);
    if (!x.ok) return Uns("VEX prefix not found in the bytes");
    if (x.legacy) return Sys("66, F2, F3 or REX prefix before VEX (#UD)");
    if (x.rex_far) return Uns("REX before VEX with another prefix between them (no SDM rule)");
    // I8 re-review: a gather (VEX.66.0F38 90..93) is #UD unless ModRM.rm = 100b (a SIB byte: VSIB) and its destination
    // (ModRM.reg, VEX.R), mask (VEX.vvvv) and index (SIB.index, VEX.X) registers are three different ones (SDM VPGATHERDD
    // and the like): an encoding refusal, so never a gated #NM.
    if (x.map == 2 && x.pp == 1 && d.FexcVexOp >= 0x90 && d.FexcVexOp <= 0x93 && has_modrm && mod != 3) {
      const unsigned at = x.op + 1;
      const uint8_t rm = at < d.InstSize ? b[at] & 7 : 0;
      const uint8_t dst = reg | x.R << 3, index = rm == 4 && at + 1 < d.InstSize ? ((b[at + 1] >> 3) & 7) | x.X << 3 : 0xFF;
      if (rm != 4) return Ud("gather without a SIB byte (VSIB; #UD)");
      if (dst == x.vvvv || dst == index || x.vvvv == index) return Ud("gather whose destination, index and mask registers are not all different (#UD)");
    }
    v = Vex(d.FexcVex, d.FexcVexOp, has_modrm, reg);
    if (v.kind != ALLOW) {
      v = Avx(x.map, x.pp, x.L, x.W, x.vvvv, d.FexcVexOp, has_modrm, mod, reg);
      if (v.avx && fexc_avx_gated) {
        v = Sys("AVX form gated by CR4.OSXSAVE, XCR0[2:1] or CR0.TS (#UD/#NM)");
        v.gated = true;
        return v;
      }
    }
    break;
  }
  case FEXC_MAP_0F38:
    v = (op >= 0x80 && op <= 0x82) ? Sys("invept/invvpid/invpcid") : Uns("0f38 map"); break;
  default: v = Uns("0f3a/3dnow map"); break;
  }
  // LFENCE/MFENCE/SFENCE (0f ae e8/f0/f8, no 66/F2/F3: those are other instructions): FEX emits dmb ld/sy/st (S3e).
  if (d.FexcMap == FEXC_MAP_TWO && op == 0xAE && has_modrm && (d.ModRM == 0xE8 || d.ModRM == 0xF0 || d.ModRM == 0xF8) &&
      !(f & (DecodeFlags::FLAG_OPERAND_SIZE | DecodeFlags::FLAG_REP_PREFIX | DecodeFlags::FLAG_REPNE_PREFIX)))
    v = Allow();
  if (v.kind != ALLOW) {
    // I10: 0f 1e /r is the CET hint space (ENDBR32/ENDBR64 f3 0f 1e fb/fa, RDSSPD/RDSSPQ f3 0f 1e /1 register, the other
    // encodings reserved NOPs): without CET (not advertised; CR4.CET stays 0) every one executes as a NOP: SDM ENDBR64 "a NOP on
    // processors that do not support CET", RDSSP "a NOP when CET shadow stacks are not enabled and on processors that do not
    // support CET" (its destination unchanged), with no memory access. FEX decodes it as NOP from the base two-byte table
    // (NO_OVERLAY) whatever the 66/F2/F3 prefix and emits nothing. LOCK is cut above (#UD).
    if (decoded && d.FexcMap == FEXC_MAP_TWO && op == 0x1E && has_modrm && !(d.Flags & DecodeFlags::FLAG_LOCK))
      return Allow();
    return v;
  }
  if (!decoded) return Uns("did not decode");
  // CR0.EM, CR0.TS or no CR4.OSFXSR (the host's --sse-gated): the resident raises #UD or #NM before the instruction.
  if (v.simd && fexc_sse_gated) { v = Sys("SSE gated by CR0.EM/TS or CR4.OSFXSR (#UD/#NM)"); v.simd = true; return v; }
  // I9: an MMX form (EMMS included) under CR0.EM or CR0.TS (the host's --x87-gated): #UD or #NM (the host's MMX_GATED).
  if (v.mmx && fexc_x87_gated) { v = Sys("MMX gated by CR0.EM/TS (#UD/#NM)"); v.mmx = true; v.gated = true; return v; }
  // X2: BT/BTS/BTR/BTC m, r and m, imm8 (0f a3/ab/b3/bb, 0f ba /4../7) under alignment checking (the host's --alignment-gated):
  // the resident checks the operand-size alignment (#AC), then steps the instruction compiled without this flag.
  // (The form first: fexc_alignment_gated is read only for the forms it gates; see FexcArg.)
  if ((f & DecodeFlags::FLAG_ADDRESS_SIZE) && d.FexcMap >= FEXC_MAP_TWO && d.FexcMap <= FEXC_MAP_TWO_66 && has_modrm && mod != 3 &&
      (op == 0xA3 || op == 0xAB || op == 0xB3 || op == 0xBB))
    return Uns("address-size override on bt/bts/btr/btc m, r (no SDM rule for the bit offset with a 32-bit address)");
  if (d.FexcMap >= FEXC_MAP_TWO && d.FexcMap <= FEXC_MAP_TWO_66 && has_modrm && mod != 3 &&
      (op == 0xA3 || op == 0xAB || op == 0xB3 || op == 0xBB || (op == 0xBA && reg >= 4)) && fexc_alignment_gated)
    return Sys("bt/bts/btr/btc with a memory operand under alignment checking (#AC: the resident's check)");
  const bool primary = d.FexcMap == FEXC_MAP_PRIMARY;
  // LOCK (I3): admitted on every lockable form (the others are cut above as #UD). FEX lowers each to one LSE atomic (LOCK NEG:
  // one compare-and-swap per pass, patch 0016). 66 on cmpxchg8b/16b is refused by policy (the SDM gives it no meaning).
  if (d.FexcMap == FEXC_MAP_TWO && op == 0xC7 && (f & DecodeFlags::FLAG_OPERAND_SIZE)) return Uns("operand-size prefix on cmpxchg8b/16b");
  // String instructions (I2): REP (F3) on all five, REPE (F3) and REPNE (F2) on cmps/scas. Refused: both prefixes together,
  // and by policy F2 on movs/stos/lods (hardware repeats as with REP; the SDM calls it reserved).
  const bool string = primary && ((op >= 0xA4 && op <= 0xA7) || (op >= 0xAA && op <= 0xAF));
  const bool compare = string && (op == 0xA6 || op == 0xA7 || op == 0xAE || op == 0xAF);
  const bool source = string && (op <= 0xA7 || op == 0xAC || op == 0xAD);   // movs, cmps, lods: DS:RSI
  if ((f & DecodeFlags::FLAG_REP_PREFIX) && (f & DecodeFlags::FLAG_REPNE_PREFIX)) return Uns("REP and REPNE prefixes together");
  if ((f & DecodeFlags::FLAG_REPNE_PREFIX) && !compare) return Uns(string ? "REPNE prefix on movs/stos/lods" : "REPNE prefix");
  // rep ret is a ret; F3 90 (no REX.B: that would be xchg r8, rax) is PAUSE, FEX's `yield`.
  if ((f & DecodeFlags::FLAG_REP_PREFIX) && !string && !(primary && (op == 0xC3 || (op == 0x90 && !(f & DecodeFlags::FLAG_REX_XGPR_B)))))
    return Uns("REP prefix");
  // Segment prefixes in 64-bit mode (I7). FS/GS: FEX adds fs_cached/gs_cached (guest-state page loads) to the effective
  // address of the explicit memory operand, as x86 adds FS.base/GS.base; never to the implicit stack accesses of push/
  // pop/call/ret/enter/leave/pushf (SS, which no prefix overrides). CS/DS/ES/SS: base 0 in 64-bit mode, and FEX's decoder
  // drops them (they never set FLAG_SEGMENTS there), so they only choose the exception for a non-canonical address. The
  // presented CPU is GenuineIntel, so Intel's rule (SDM Vol. 3A §4.5.1; Vol. 1 §3.3.7.1): #SS(0) only for a reference
  // that uses SS, implicitly (an RSP/RBP base, no override) or explicitly (an SS prefix, whatever the base); CS/DS/ES
  // name a DS-class reference, #GP(0) even on an RSP/RBP base. fexc's JoinSites gives the explicit ModRM operand's sites
  // that class from these bytes (Linux's SMP alternatives turn LOCK into DS on one CPU: `ds btr [rbp+...]`). Refused:
  //   - several segment prefixes with an FS/GS one (Vol. 2 §2.1.1 gives no rule for two prefixes of one group; FEX keeps
  //     the last FS/GS prefix and drops CS/DS/ES/SS, QEMU lets the last segment prefix win, a null one included), and SS
  //     with CS/DS/ES (mixed classes, the same);
  //   - SS on a moffs or string source operand (JoinSites classes only an explicit ModRM operand);
  //   - FS/GS on an instruction with no explicit memory operand (nothing to override).
  // String instructions: ES:RDI takes no override (SDM Vol. 2 MOVS/STOS/CMPS/SCAS); the DS:RSI source of movs/cmps/lods
  // takes FS/GS and CS/DS/ES (DS-class either way); stos/scas have no overridable operand, so FS/GS there (which hardware
  // ignores) is refused by policy.
  {
    const uint32_t seg = f & DecodeFlags::FLAG_SEGMENTS;
    const bool fsgs = seg == DecodeFlags::FLAG_FS_PREFIX || seg == DecodeFlags::FLAG_GS_PREFIX;
    const bool xlat = primary && op == 0xD7;
    const bool memory = (has_modrm && mod != 3) || (primary && op >= 0xA0 && op <= 0xA3) || source || xlat;
    const uint8_t* b = fexc_guest_bytes ? fexc_guest_bytes(d.PC, d.InstSize) : nullptr;
    if (!b) return Uns("segment prefixes: instruction bytes unavailable");
    int ss = 0, data = 0, high = 0;   // 36; 26/2E/3E; 64/65
    for (unsigned i = 0; i < d.InstSize; ++i) {
      const uint8_t x = b[i];
      if (x == 0x36) ++ss;
      else if (x == 0x26 || x == 0x2E || x == 0x3E) ++data;
      else if (x == 0x64 || x == 0x65) ++high;
      else if (!(x == 0xF0 || x == 0xF2 || x == 0xF3 || x == 0x66 || x == 0x67 || (x >= 0x40 && x <= 0x4F))) break;
    }
    if (fsgs && !memory) return Uns("FS/GS override on an instruction without a memory operand");
    if (fsgs && ss + data + high != 1) return Uns("FS/GS override with another segment prefix (no SDM rule for several)");
    if (memory && ss && data) return Uns("SS with a CS/DS/ES prefix (mixed segment prefixes)");
    if (ss && memory && !(has_modrm && mod != 3)) return Uns("SS override on a moffs, xlat or string source operand");
  }
  // 0x67 (I10): 32-bit address size. FEX computes the effective address in 64 bits, truncates it to 32 bits and zero-extends
  // it before the FS/GS base (Addressing.cpp LoadEffectiveAddress: `Bfe`), RIP-relative included (EIP-relative: RIP + disp
  // truncated); moffs is 4 bytes; strings use ESI/EDI/ECX, LOOP/JECXZ ECX, XLAT EBX + AL (patch 0025); a direct branch, RET and
  // the stack's own accesses ignore it. The sites see the truncated address (BT* m, r: refused above).
  if (v.branch && (f & DecodeFlags::FLAG_OPERAND_SIZE)) return Uns("operand-size prefix on a branch");
  if (d.FexcMap == FEXC_MAP_TWO && op >= 0xC8 && op <= 0xCF && (f & DecodeFlags::FLAG_OPERAND_SIZE) && !(f & DecodeFlags::FLAG_REX_WIDENING))
    return Uns("16-bit bswap (undefined result)");
  if (d.FexcMap == FEXC_MAP_TWO && (op == 0xA4 || op == 0xA5 || op == 0xAC || op == 0xAD) && (f & DecodeFlags::FLAG_OPERAND_SIZE))
    return Uns("16-bit shld/shrd (result undefined for counts above 16)");
  if (primary && (op == 0x9C || op == 0xC8 || op == 0xC9) && (f & DecodeFlags::FLAG_OPERAND_SIZE))
    return Uns("operand-size prefix on pushf/enter/leave (16-bit stack forms)");
  if (primary && op == 0xC8 && ((d.Src[0].Literal() >> 16) & 0x1F) != 0)
    return Uns("enter with a nesting level (several stores)");
  // I8: VCVTPS2PH m: the conversion raises its MXCSR flags (FPSR) before the store, which may fault; x86 commits neither.
  if (d.FexcMap == FEXC_MAP_VEX && v.avx && (d.FexcVex & 3) == 3 && d.FexcVexOp == 0x1D && has_modrm && mod != 3)
    return Uns("vcvtps2ph m: its MXCSR flags would be raised before the store, which may fault");
  // I8: VINSERTPS m32 whose zero mask clears the inserted element: FEX drops the load and its #PF (as INSERTPS, I5a).
  if (d.FexcMap == FEXC_MAP_VEX && v.avx && (d.FexcVex & 3) == 3 && d.FexcVexOp == 0x21 && has_modrm && mod != 3) {
    const uint8_t* b = fexc_guest_bytes(d.PC, d.InstSize);
    const uint8_t imm = b[d.InstSize - 1];
    if ((imm & 0xF) & (1 << (imm >> 4 & 3))) return Uns("vinsertps m32 whose zero mask clears the inserted element (FEX drops the load and its #PF)");
  }
  return v;
}

// The one place the cut is decided. `last` = this is the last decoded instruction of its block, whose decode
// status is `status`; `first` = no instruction of the block was translated yet. Returns false to translate the
// instruction (and, mid-block, to let FEX end the block at a decoder end-of-input status with its own reason).
#include <fexc_mmio.inc>

// I10 review: the length of an undecoded #UD encoding, the whole instruction (fetched before its decode, Machine.fetch's
// policy, and subject to the 15-byte limit): legacy prefixes and REX, the opcode bytes (EVEX 62 P0 P1 P2 op, VEX C4 P1 P2 op /
// C5 P1 op, XOP 8F with map_select >= 8: 8F P1 P2 op, 0f 38/0f 3a op, 0f op, a primary opcode), then the ModRM with its SIB and
// displacement, then an immediate: imm8 for 0f 3a, VEX/EVEX map 3, XOP map 8, 0f 71..73, 0f ba; C6 imm8, C7 imm32 (imm16
// with 66). The opcodes invalid in 64-bit mode (27 2F 37 3F 60 61 82 D4 D5) are their opcode byte. A byte past the supplied
// ones counts as needed (the caller then answers `partial_decode`).
inline unsigned FexcUdLength(uint64_t rip) {
  auto byte = [&](unsigned i) -> int { const uint8_t* p = fexc_guest_bytes ? fexc_guest_bytes(rip + i, 1) : nullptr; return p ? *p : -1; };
  auto prefix = [](int b) { return b == 0x26 || b == 0x2E || b == 0x36 || b == 0x3E || b == 0x64 || b == 0x65 || b == 0x66 || b == 0x67 ||
                                   b == 0xF0 || b == 0xF2 || b == 0xF3 || (b >= 0x40 && b <= 0x4F); };
  unsigned i = 0;
  bool o16 = false;
  while (i < 15 && prefix(byte(i))) o16 |= byte(i++) == 0x66;
  const int b = byte(i);
  unsigned m, imm = 0;   // the ModRM's offset
  switch (b) {
  case -1: return i + 1;
  case 0x27: case 0x2F: case 0x37: case 0x3F: case 0x60: case 0x61: case 0x82: case 0xD4: case 0xD5: return i + 1;
  case 0x62: m = i + 5; imm = (byte(i + 1) & 3) == 3; break;
  case 0xC4: m = i + 4; imm = (byte(i + 1) & 0x1F) == 3; break;
  case 0xC5: m = i + 3; break;
  case 0x8F: { const int p1 = byte(i + 1); if (p1 >= 0 && (p1 & 0x1F) >= 8) { m = i + 4; imm = (p1 & 0x1F) == 8; } else m = i + 1; break; }
  case 0x0F: {
    const int o = byte(i + 1);
    if (o == 0x38 || o == 0x3A) { m = i + 3; imm = o == 0x3A; }
    else { m = i + 2; imm = (o >= 0x71 && o <= 0x73) || o == 0xBA; }
    break;
  }
  default: m = i + 1; imm = b == 0xC6 ? 1 : b == 0xC7 ? (o16 ? 2 : 4) : 0; break;
  }
  const int modrm = byte(m);
  if (modrm < 0) return m + 1;
  const unsigned mod = modrm >> 6, rm = modrm & 7;
  unsigned n = m + 1;
  if (mod != 3 && rm == 4) {
    const int sib = byte(n++);
    if (sib < 0) return n;
    if (mod == 0 && (sib & 7) == 5) n += 4;
  } else if (mod == 0 && rm == 5) n += 4;
  return n + (mod == 1 ? 1 : mod == 2 ? 4 : 0) + imm;
}

inline bool Cut(const Frontend::Decoder::DecodedBlockStatus status, bool last, bool first, const DecodedInst& d, uint64_t rip) {
  using S = Frontend::Decoder::DecodedBlockStatus;
  // A REP string instruction (I2) or a LOCK NEG (I3) ends its block: the next instruction starts the next block.
  if (first) fexc_after_rep = 0;
  if (!first && rip == fexc_after_rep) {
    fexc_cut = {FEXC_CUT_STRING, rip, d.FexcMap, d.OPRaw, d.ModRM, false, "?", "after a self-exiting instruction (REP string, LOCK NEG)"};
    return true;
  }
  const bool failed = last && status != S::SUCCESS;
  if (failed && status != S::INVALID_INST && status != S::UNIMPLEMENTED_INST) {
    // Mid-block, FEX already ends the block before such an instruction (HadInvalidInst) without emitting anything for it.
    // As the first instruction FEX would emit its NoExec/SIGSEGV block; slice 1 produces no code instead.
    if (!first) return false;
    fexc_cut = {FEXC_CUT_DECODER, rip, d.FexcMap, d.OPRaw, d.ModRM, false, "?", "", uint8_t(status)};
    return true;
  }
  const bool decoded = !failed && d.TableInfo != nullptr;
  Verdict v = Classify(d, decoded);
  // I10 review: a #UD encoding FEX did not decode (it stops at the byte it does not know) is #UD once the instruction is
  // fetched: its bytes through the ModRM (FexcUdLength) must be in the supplied bytes, else `partial_decode` (the host fetches
  // the next page, whose fault comes first: SDM Vol.3 Table 6-2, fetch faults before decode faults), and one longer than 15
  // bytes is `instruction_too_long` (#GP(0), as for every other instruction; natemu fetches no 16th byte, Machine.fetch).
  if (first && !decoded && v.kind == SYSTEM && v.ud) {
    const unsigned n = FexcUdLength(rip);
    const uint8_t st = !fexc_guest_bytes(rip, n < 15 ? n : 15) ? uint8_t(S::PARTIAL_DECODE_INST) : n > 15 ? uint8_t(S::TOO_LONG_INST) : 0;
    if (st) {
      fexc_cut = {FEXC_CUT_DECODER, rip, d.FexcMap, d.OPRaw, d.ModRM, false, "?", "", st};
      return true;
    }
  }
  if (v.kind == ALLOW) {
    // A direct branch to a non-canonical target raises #GP(0) at the branch (Intel), before a call pushes. Its target is
    // known here, so it is cut instead of compiled; slice 1 does not deliver that #GP.
    const uint8_t op = d.OPRaw;
    const bool rel = (d.FexcMap == FEXC_MAP_PRIMARY && ((op >= 0x70 && op <= 0x7F) || op == 0xE8 || op == 0xE9 || op == 0xEB)) ||
                     (d.FexcMap == FEXC_MAP_TWO && op >= 0x80 && op <= 0x8F);
    const uint64_t target = rip + d.InstSize + d.Src[0].Literal();
    const bool string = d.FexcMap == FEXC_MAP_PRIMARY && ((op >= 0xA4 && op <= 0xA7) || (op >= 0xAA && op <= 0xAF));
    if (string && (d.Flags & (DecodeFlags::FLAG_REP_PREFIX | DecodeFlags::FLAG_REPNE_PREFIX))) fexc_after_rep = rip + d.InstSize;
    // LOCK NEG m (I3, patch 0016) exits to its own RIP when its compare-and-swap loses a race: it ends its block too.
    const bool lock_neg = d.FexcMap == FEXC_MAP_PRIMARY && (op == 0xF6 || op == 0xF7) && ((d.ModRM >> 3) & 7) == 3 &&
                          (d.Flags & DecodeFlags::FLAG_LOCK);
    if (lock_neg) fexc_after_rep = rip + d.InstSize;
    if (v.align) (v.align32 ? fexc_align32 : fexc_align16).insert(rip);   // I5a/I8: its first access gets the alignment check (#GP(0))
    FexcMmioRecord(d, rip);                  // M3: completion record of a MOV-family device access
    if (v.x87 == 1) {   // I6: FSW exception flags from FPSR, the x87 FPCR; FCOM forms and FTST signal, FIST/FRNDINT raise PE
      const uint8_t m = d.ModRM, r = (m >> 3) & 7;
      fexc_x87.insert(rip);
      const bool memory = (m >> 6) != 3;
      const bool fcom_m = memory && (op == 0xD8 || op == 0xDA || op == 0xDC || op == 0xDE) && (r == 2 || r == 3);   // fcom(p)/ficom(p) m
      const bool fcom_r = !memory && ((op == 0xD8 && (r == 2 || r == 3)) || (op == 0xDE && m == 0xD9) || ((op == 0xDB || op == 0xDF) && r == 6));
      if (fcom_m || fcom_r || (op == 0xD9 && m == 0xE4))   // + fcompp, fcomi(p), ftst
        fexc_fp_signal.insert(rip);
      // fistp m64 and frndint: frintx (PE); m16/m32 FIST computes PE itself (patch 0019: none beside an out-of-range IE)
      if ((memory && op == 0xDF && r == 7) || (op == 0xD9 && m == 0xFC))
        fexc_round_inexact.insert(rip);
    }
    if (v.avx) {   // I8: the same for the VEX forms (FEX's AVX128 lowering uses the same IR ops)
      const uint8_t* b = fexc_guest_bytes(d.PC, d.InstSize);
      const VexBytes x = ParseVex(b, d.InstSize);
      const uint8_t vop = d.FexcVexOp;
      if (x.map == 1 && x.pp <= 1 && vop == 0x2F) fexc_fp_signal.insert(rip);
      if (x.map == 1 && (x.pp == 0 || x.pp == 2) && (vop == 0x52 || vop == 0x53)) fexc_fp_quiet.insert(rip);
      if (x.map == 3 && vop >= 0x08 && vop <= 0x0B && !(b[d.InstSize - 1] & 8)) fexc_round_inexact.insert(rip);
    }
    if (v.simd) {   // I5a: the MXCSR flags FEX's lowering would get wrong (fexc_hook.h)
      const uint8_t* b = fexc_guest_bytes(d.PC, d.InstSize);
      if (d.FexcMap == FEXC_MAP_TWO || d.FexcMap == FEXC_MAP_TWO_66) { if (op == 0x2F) fexc_fp_signal.insert(rip); }
      if ((d.FexcMap == FEXC_MAP_TWO || d.FexcMap == FEXC_MAP_TWO_F3) && (op == 0x52 || op == 0x53)) fexc_fp_quiet.insert(rip);
      if (d.FexcMap == FEXC_MAP_0F3A && op >= 0x08 && op <= 0x0B && !(b[d.InstSize - 1] & 8)) fexc_round_inexact.insert(rip);
    }
    if (!rel || uint64_t(int64_t(target << 16) >> 16) == target) return false;
    v = Uns("direct branch to a non-canonical target");
  }
  fexc_cut = {v.kind == SYSTEM ? FEXC_CUT_SYSTEM : FEXC_CUT_UNSUPPORTED, rip, d.FexcMap, d.FexcMap == FEXC_MAP_VEX ? d.FexcVexOp : d.OPRaw, d.ModRM,
              bool(d.Flags & DecodeFlags::FLAG_DECODED_MODRM), d.TableInfo && d.TableInfo->Name ? d.TableInfo->Name : "?", v.why};
  fexc_cut.gated = v.kind == SYSTEM && (v.simd || v.gated);
  fexc_cut.gated_mmx = fexc_cut.gated && v.mmx;
  fexc_cut.ud = v.kind == SYSTEM && v.ud;
  return true;
}
} // namespace FexcSlice1
