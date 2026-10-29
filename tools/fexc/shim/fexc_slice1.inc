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
  case 0x9D: case 0xFA: case 0xFB: return Sys("flag control (popf/cli/sti: IF, IOPL, AC, TF are the resident's)");
  // pushf reads TF/IF/IOPL/NT/AC/VIF/VIP/ID from the guest-state page (the resident writes them at every entry, RF/VM
  // as 0: the image clears them); DF lives there too (cld/std write it). clc/stc/cmc, lahf/sahf: arithmetic flags only.
  case 0x9C: case 0xFC: case 0xFD: case 0xF5: case 0xF8: case 0xF9: case 0x9E: case 0x9F: return Allow();
  case 0xC9: return Allow();                                       // leave
  case 0xC8: return Allow();                                       // enter (nesting level 0 only, checked below)
  case 0xF4: return Sys("hlt");
  case 0xE4: case 0xE5: case 0xE6: case 0xE7: case 0xEC: case 0xED: case 0xEE: case 0xEF:
  case 0x6C: case 0x6D: case 0x6E: case 0x6F: return Sys("port I/O");
  case 0x63: case 0x68: case 0x6A: case 0x69: case 0x6B: case 0x84: case 0x85: case 0x88: case 0x89: case 0x8A: case 0x8B:
  case 0x8D: case 0x98: case 0x99: case 0xA0: case 0xA1: case 0xA2: case 0xA3: case 0xA8: case 0xA9: return Allow();
  case 0x80: case 0x81: case 0x83: return Allow();                  // group 1 (82 is invalid in 64-bit mode)
  case 0x86: case 0x87: return Allow();                             // xchg; with memory: implicit LOCK, FEX's swpal (S3e)
  case 0x8F: return has_modrm && reg == 0 ? Allow() : Uns("pop r/m with reg != 0");
  case 0xC0: case 0xC1: case 0xD0: case 0xD1: case 0xD2: case 0xD3:
    if (!has_modrm || reg == 6) return Uns("shift group /6 alias");
    // FEX branches around the whole access for a static or dynamic zero count of rcl/rcr, losing a guest #PF.
    if ((reg == 2 || reg == 3) && mod != 3) return Uns("rcl/rcr with a memory operand (FEX skips the access for a zero count)");
    return Allow();
  case 0xC6: case 0xC7: return has_modrm && reg == 0 ? Allow() : Uns("group 11 other than mov");
  case 0xF6: case 0xF7:
    if (!has_modrm) return Uns("group 3 without ModRM");
    return reg == 1 ? Uns("test /1 alias") : Allow();
  case 0xFE: return has_modrm && reg <= 1 ? Allow() : Uns("group 4 other than inc/dec");
  case 0xFF:
    if (!has_modrm) return Uns("group 5 without ModRM");
    if (reg == 3 || reg == 5) return Sys("far call/jmp");
    if (reg == 2 || reg == 4) return Allow(true);
    return reg <= 1 || reg == 6 ? Allow() : Uns("group 5 /7");
  case 0xC2: case 0xC3: case 0xE8: case 0xE9: case 0xEB: return Allow(true);
  case 0xE0: case 0xE1: case 0xE2: case 0xE3: return Allow(true);  // loopne/loope/loop/jrcxz rel8 (two exits, both forward; 67 prefix refused below)
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
    return Uns("fxsave/xsave, ldmxcsr/stmxcsr, a fence with another ModRM");
  default: break;
  }
  if (!plain) return Uns("mandatory-prefix (F3/F2/66) overlay opcode");
  switch (op) {
  case 0x1F: return Allow();                                        // multi-byte nop
  case 0xA4: case 0xA5: case 0xAC: case 0xAD: return Allow();       // shld/shrd (16-bit refused below)
  case 0xB0: case 0xB1: case 0xC0: case 0xC1: return Allow();       // cmpxchg/xadd (LOCK refused below: chunk I3)
  case 0x18: return has_modrm && mod != 3 && reg <= 3 ? Allow() : Uns("0f 18 other than prefetchnta/t0/t1/t2 m8");
  case 0xC3: return has_modrm && mod != 3 ? Allow() : Uns("movnti with a register operand");
  case 0x0D: return Uns("prefetchw/0f 0d: PREFETCHW is not advertised (cpu_profile 0x80000001.ecx.8)");
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
      if (pp >= 2) return Uns("pdep/pext: FEX emits a native loop (backward branch)");
      break;
    case 0xF6: if (pp == 3) return Allow(); break;                                     // mulx
    case 0xF7: return Allow();                                                         // bextr/shlx/sarx/shrx
    default: break;
    }
  }
  if (map == 3 && op == 0xF0 && pp == 3) return Allow();                               // rorx
  return Uns("VEX form other than BMI1/BMI2 (AVX: chunk I8)");
}

inline Verdict Classify(const DecodedInst& d, bool decoded) {
  const bool has_modrm = d.Flags & DecodeFlags::FLAG_DECODED_MODRM;
  const uint8_t mod = d.ModRM >> 6, reg = (d.ModRM >> 3) & 7;
  const uint8_t op = d.OPRaw;
  const uint32_t f = d.Flags;
  Verdict v;
  switch (d.FexcMap) {
  case FEXC_MAP_PRIMARY: v = Primary(op, has_modrm, mod, reg); break;
  case FEXC_MAP_TWO: case FEXC_MAP_TWO_F2: case FEXC_MAP_TWO_66:
    v = Two(op, d.FexcMap == FEXC_MAP_TWO, has_modrm, mod, reg); break;
  case FEXC_MAP_TWO_F3:
    // popcnt, tzcnt, lzcnt: the profile advertises POPCNT, BMI1 and LZCNT, so f3 0f bc/bd are TZCNT/LZCNT (FEX
    // decodes them so unconditionally: X86Tables/SecondaryTables.cpp RepModOps).
    v = (op == 0xB8 || op == 0xBC || op == 0xBD) ? Allow() : Two(op, false, has_modrm, mod, reg); break;
  case FEXC_MAP_VEX: v = Vex(d.FexcVex, d.FexcVexOp, has_modrm, reg); break;
  case FEXC_MAP_0F38:
    v = (op >= 0x80 && op <= 0x82) ? Sys("invept/invvpid/invpcid") : Uns("0f38 map"); break;
  default: v = Uns("0f3a/3dnow map"); break;
  }
  // LFENCE/MFENCE/SFENCE (0f ae e8/f0/f8, no 66/F2/F3: those are other instructions): FEX emits dmb ld/sy/st (S3e).
  if (d.FexcMap == FEXC_MAP_TWO && op == 0xAE && has_modrm && (d.ModRM == 0xE8 || d.ModRM == 0xF0 || d.ModRM == 0xF8) &&
      !(f & (DecodeFlags::FLAG_OPERAND_SIZE | DecodeFlags::FLAG_REP_PREFIX | DecodeFlags::FLAG_REPNE_PREFIX)))
    v = Allow();
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
  const bool primary = d.FexcMap == FEXC_MAP_PRIMARY;
  // LOCK only on memory xchg (implicitly locked anyway; on a register form LOCK is #UD).
  if ((f & DecodeFlags::FLAG_LOCK) && !(primary && (op == 0x86 || op == 0x87) && mod != 3)) return Uns("LOCK prefix");
  if (f & DecodeFlags::FLAG_REPNE_PREFIX) return Uns("REPNE prefix");
  // rep ret is a ret; F3 90 (no REX.B: that would be xchg r8, rax) is PAUSE, FEX's `yield`.
  if ((f & DecodeFlags::FLAG_REP_PREFIX) && !(primary && (op == 0xC3 || (op == 0x90 && !(f & DecodeFlags::FLAG_REX_XGPR_B)))))
    return Uns("REP prefix");
  const uint32_t seg = f & DecodeFlags::FLAG_SEGMENTS;
  if (seg == DecodeFlags::FLAG_FS_PREFIX || seg == DecodeFlags::FLAG_GS_PREFIX) return Uns("FS/GS override");
  if (f & DecodeFlags::FLAG_ADDRESS_SIZE) return Uns("address-size override");
  if (v.branch && (f & DecodeFlags::FLAG_OPERAND_SIZE)) return Uns("operand-size prefix on a branch");
  if (d.FexcMap == FEXC_MAP_TWO && op >= 0xC8 && op <= 0xCF && (f & DecodeFlags::FLAG_OPERAND_SIZE) && !(f & DecodeFlags::FLAG_REX_WIDENING))
    return Uns("16-bit bswap (undefined result)");
  if (d.FexcMap == FEXC_MAP_TWO && (op == 0xA4 || op == 0xA5 || op == 0xAC || op == 0xAD) && (f & DecodeFlags::FLAG_OPERAND_SIZE))
    return Uns("16-bit shld/shrd (result undefined for counts above 16)");
  if (primary && (op == 0x9C || op == 0xC8 || op == 0xC9) && (f & DecodeFlags::FLAG_OPERAND_SIZE))
    return Uns("operand-size prefix on pushf/enter/leave (16-bit stack forms)");
  if (primary && op == 0xC8 && ((d.Src[0].Literal() >> 16) & 0x1F) != 0)
    return Uns("enter with a nesting level (several stores)");
  if (d.FexcMap == FEXC_MAP_VEX && (f & (DecodeFlags::FLAG_OPERAND_SIZE | DecodeFlags::FLAG_REX_PREFIX)))
    return Uns("66 or REX before VEX (#UD)");
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
  fexc_cut = {v.kind == SYSTEM ? FEXC_CUT_SYSTEM : FEXC_CUT_UNSUPPORTED, rip, d.FexcMap, d.FexcMap == FEXC_MAP_VEX ? d.FexcVexOp : d.OPRaw, d.ModRM,
              bool(d.Flags & DecodeFlags::FLAG_DECODED_MODRM), d.TableInfo && d.TableInfo->Name ? d.TableInfo->Name : "?", v.why};
  return true;
}
} // namespace FexcSlice1
