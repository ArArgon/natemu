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
// SYSTEM wins over prefix checks (a rep insb is a system instruction); the allowlist never admits an
// instruction that did not decode.
namespace FexcSlice1 {
using namespace FEXCore::X86Tables;

enum Kind : uint8_t { ALLOW, SYSTEM, UNSUPPORTED };
struct Verdict { Kind kind; bool branch; const char* why; };

inline Verdict Allow(bool branch = false) { return {ALLOW, branch, ""}; }
inline Verdict Sys(const char* why) { return {SYSTEM, false, why}; }
inline Verdict Uns(const char* why) { return {UNSUPPORTED, false, why}; }

// Primary opcode map (also reached through FEX's arch-dispatch entries for 63 and a0..a3).
inline Verdict Primary(uint8_t op, bool has_modrm, uint8_t mod, uint8_t reg) {
  if (op < 0x40 && (op & 7) < 6) return Allow();                    // add/or/adc/sbb/and/sub/xor/cmp, all 6 forms each
  switch (op) {
  case 0x06: case 0x07: case 0x0E: case 0x16: case 0x17: case 0x1E: case 0x1F: return Sys("push/pop of a segment register");
  case 0x8C: case 0x8E: return Sys("segment register move");
  case 0x9A: case 0xEA: case 0xCA: case 0xCB: case 0xCF: return Sys("far transfer or iret");
  case 0xCC: case 0xCD: case 0xCE: case 0xF1: return Sys("software interrupt");
  case 0x9C: case 0x9D: case 0xFA: case 0xFB: return Sys("flag control (pushf/popf/cli/sti)");
  case 0xF4: return Sys("hlt");
  case 0xE4: case 0xE5: case 0xE6: case 0xE7: case 0xEC: case 0xED: case 0xEE: case 0xEF:
  case 0x6C: case 0x6D: case 0x6E: case 0x6F: return Sys("port I/O");
  case 0x63: case 0x68: case 0x6A: case 0x69: case 0x6B: case 0x84: case 0x85: case 0x88: case 0x89: case 0x8A: case 0x8B:
  case 0x8D: case 0x98: case 0x99: case 0xA0: case 0xA1: case 0xA2: case 0xA3: case 0xA8: case 0xA9: return Allow();
  case 0x80: case 0x81: case 0x83: return Allow();                  // group 1 (82 is invalid in 64-bit mode)
  case 0x86: case 0x87: return mod == 3 ? Allow() : Uns("memory xchg (implicit LOCK)");
  case 0x8F: return has_modrm && reg == 0 ? Allow() : Uns("pop r/m with reg != 0");
  case 0xC0: case 0xC1: case 0xD0: case 0xD1: case 0xD2: case 0xD3:
    if (!has_modrm || reg == 6) return Uns("shift group /6 alias");
    // FEX branches around the whole access for a static or dynamic zero count of rcl/rcr, losing a guest #PF.
    if ((reg == 2 || reg == 3) && mod != 3) return Uns("rcl/rcr with a memory operand (FEX skips the access for a zero count)");
    return Allow();
  case 0xC6: case 0xC7: return has_modrm && reg == 0 ? Allow() : Uns("group 11 other than mov");
  case 0xF6: case 0xF7:
    if (!has_modrm) return Uns("group 3 without ModRM");
    if (reg == 6 || reg == 7) return Uns("div/idiv (admitted with S1f)");
    return reg == 1 ? Uns("test /1 alias") : Allow();
  case 0xFE: return has_modrm && reg <= 1 ? Allow() : Uns("group 4 other than inc/dec");
  case 0xFF:
    if (!has_modrm) return Uns("group 5 without ModRM");
    if (reg == 3 || reg == 5) return Sys("far call/jmp");
    if (reg == 2 || reg == 4) return Allow(true);
    return reg <= 1 || reg == 6 ? Allow() : Uns("group 5 /7");
  case 0xC2: case 0xC3: case 0xE8: case 0xE9: case 0xEB: return Allow(true);
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
    return Uns("fence, fxsave/xsave, ldmxcsr/stmxcsr");
  default: break;
  }
  if (!plain) return Uns("mandatory-prefix (F3/F2/66) overlay opcode");
  switch (op) {
  case 0x1F: return Allow();                                        // multi-byte nop
  case 0xA3: case 0xAB: case 0xB3: case 0xBB: case 0xAF: case 0xB6: case 0xB7: case 0xBE: case 0xBF:
  case 0xBC: case 0xBD: return Allow();                             // bt/bts/btr/btc, imul, movzx/movsx, bsf/bsr
  case 0xBA: return has_modrm && reg >= 4 ? Allow() : Uns("group 8 below /4");
  default: break;
  }
  if (op >= 0x40 && op <= 0x4F) return Allow();                     // cmovcc
  if (op >= 0x80 && op <= 0x8F) return Allow(true);                 // jcc rel32
  if (op >= 0x90 && op <= 0x9F) return Allow();                     // setcc
  if (op >= 0xC8 && op <= 0xCF) return Allow();                     // bswap
  return Uns("not in the slice-1 allowlist");
}

inline Verdict Classify(const DecodedInst& d, bool decoded) {
  const bool has_modrm = d.Flags & DecodeFlags::FLAG_DECODED_MODRM;
  const uint8_t mod = d.ModRM >> 6, reg = (d.ModRM >> 3) & 7;
  const uint8_t op = d.OPRaw;
  Verdict v;
  switch (d.FexcMap) {
  case FEXC_MAP_PRIMARY: v = Primary(op, has_modrm, mod, reg); break;
  case FEXC_MAP_TWO: case FEXC_MAP_TWO_F3: case FEXC_MAP_TWO_F2: case FEXC_MAP_TWO_66:
    v = Two(op, d.FexcMap == FEXC_MAP_TWO, has_modrm, mod, reg); break;
  case FEXC_MAP_0F38:
    v = (op >= 0x80 && op <= 0x82) ? Sys("invept/invvpid/invpcid") : Uns("0f38 map"); break;
  default: v = Uns("0f3a/3dnow map"); break;
  }
  if (v.kind != ALLOW) {
    // endbr64 (f3 0f 1e fa) is a no-op. 0f 1e is a NO_OVERLAY opcode, so FEX decodes it from the base two-byte table
    // with the REP flag still set; nothing else may be set.
    const uint32_t other = DecodeFlags::FLAG_LOCK | DecodeFlags::FLAG_REPNE_PREFIX | DecodeFlags::FLAG_OPERAND_SIZE |
                           DecodeFlags::FLAG_ADDRESS_SIZE | DecodeFlags::FLAG_SEGMENTS;
    if (decoded && d.FexcMap == FEXC_MAP_TWO && op == 0x1E && has_modrm && d.ModRM == 0xFA && (d.Flags & DecodeFlags::FLAG_REP_PREFIX) &&
        !(d.Flags & other))
      return Allow();
    return v;
  }
  if (!decoded) return Uns("did not decode");
  const uint32_t f = d.Flags;
  if (f & DecodeFlags::FLAG_LOCK) return Uns("LOCK prefix");
  if (f & DecodeFlags::FLAG_REPNE_PREFIX) return Uns("REPNE prefix");
  if ((f & DecodeFlags::FLAG_REP_PREFIX) && !(d.FexcMap == FEXC_MAP_PRIMARY && op == 0xC3)) return Uns("REP prefix");   // rep ret is a ret
  const uint32_t seg = f & DecodeFlags::FLAG_SEGMENTS;
  if (seg == DecodeFlags::FLAG_FS_PREFIX || seg == DecodeFlags::FLAG_GS_PREFIX) return Uns("FS/GS override");
  if (f & DecodeFlags::FLAG_ADDRESS_SIZE) return Uns("address-size override");
  if (v.branch && (f & DecodeFlags::FLAG_OPERAND_SIZE)) return Uns("operand-size prefix on a branch");
  if (d.FexcMap == FEXC_MAP_TWO && op >= 0xC8 && op <= 0xCF && (f & DecodeFlags::FLAG_OPERAND_SIZE) && !(f & DecodeFlags::FLAG_REX_WIDENING))
    return Uns("16-bit bswap (undefined result)");
  return v;
}

// The one place the cut is decided. `last` = this is the last decoded instruction of its block, whose decode
// status is `status`; `first` = no instruction of the block was translated yet. Returns false to translate the
// instruction (and, mid-block, to let FEX end the block at a decoder end-of-input status with its own reason).
inline bool Cut(const Frontend::Decoder::DecodedBlockStatus status, bool last, bool first, const DecodedInst& d, uint64_t rip) {
  using S = Frontend::Decoder::DecodedBlockStatus;
  const bool failed = last && status != S::SUCCESS;
  if (failed && status != S::INVALID_INST && status != S::UNIMPLEMENTED_INST) {
    // Mid-block, FEX already ends the block before such an instruction (HadInvalidInst) without emitting anything for it.
    // As the first instruction FEX would emit its NoExec/SIGSEGV block; slice 1 produces no code instead.
    if (!first) return false;
    fexc_cut = {FEXC_CUT_DECODER, rip, d.FexcMap, d.OPRaw, d.ModRM, false, "?", "", uint8_t(status)};
    return true;
  }
  Verdict v = Classify(d, !failed && d.TableInfo != nullptr);
  if (v.kind == ALLOW) {
    // A direct branch to a non-canonical target raises #GP(0) at the branch (Intel), before a call pushes. Its target is
    // known here, so it is cut instead of compiled; slice 1 does not deliver that #GP.
    const uint8_t op = d.OPRaw;
    const bool rel = (d.FexcMap == FEXC_MAP_PRIMARY && ((op >= 0x70 && op <= 0x7F) || op == 0xE8 || op == 0xE9 || op == 0xEB)) ||
                     (d.FexcMap == FEXC_MAP_TWO && op >= 0x80 && op <= 0x8F);
    const uint64_t target = rip + d.InstSize + d.Src[0].Literal();
    if (!rel || uint64_t(int64_t(target << 16) >> 16) == target) return false;
    v = Uns("direct branch to a non-canonical target");
  }
  fexc_cut = {v.kind == SYSTEM ? FEXC_CUT_SYSTEM : FEXC_CUT_UNSUPPORTED, rip, d.FexcMap, d.OPRaw, d.ModRM,
              bool(d.Flags & DecodeFlags::FLAG_DECODED_MODRM), d.TableInfo && d.TableInfo->Name ? d.TableInfo->Name : "?", v.why};
  return true;
}
} // namespace FexcSlice1
