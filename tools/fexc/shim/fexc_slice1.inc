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
  case 0xB0: case 0xB1: case 0xC0: case 0xC1: return Allow();       // cmpxchg/xadd, with or without LOCK (I3)
  // cmpxchg8b/16b (I3): FEX's caspal with or without LOCK (an unlocked one is atomic too, which x86 allows); the other
  // group-9 forms (rdrand, rdseed, rdpid, vmptrld...) are not admitted.
  case 0xC7: return has_modrm && mod != 3 && reg == 1 ? Allow() : Uns("group 9 other than cmpxchg8b/16b m");
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
  // drops them (they never set FLAG_SEGMENTS there), so they are admitted as no-ops when they name the operand's default
  // segment (SS for an RSP/RBP base, else DS; CS and ES behave as DS: #GP(0) for a non-canonical address) or the
  // instruction has no explicit memory operand. Refused, where vendors differ and the SDM gives no rule:
  //   - several segment prefixes with an FS/GS one (Vol. 2 §2.1.1 gives no rule for two prefixes of one group; FEX keeps
  //     the last FS/GS prefix and drops CS/DS/ES/SS, QEMU lets the last segment prefix win, a null one included);
  //   - SS naming an operand whose default is DS, or CS/DS/ES one whose default is SS (#SS(0) versus #GP(0) for a
  //     non-canonical address: AMD APM Vol. 3 §1.2.4 makes CS/DS/ES/SS null prefixes in 64-bit mode, so the default
  //     segment decides, while Intel SDM Vol. 1 §3.3.7.1 suggests #SS for an SS override);
  //   - FS/GS on an instruction with no explicit memory operand (nothing to override).
  // String instructions: ES:RDI takes no override (SDM Vol. 2 MOVS/STOS/CMPS/SCAS); the DS:RSI source of movs/cmps/lods
  // takes FS/GS and the DS-default rules above (RSI is no stack base); stos/scas have no overridable operand, so FS/GS
  // there (which hardware ignores) is refused by policy.
  {
    const uint32_t seg = f & DecodeFlags::FLAG_SEGMENTS;
    const bool fsgs = seg == DecodeFlags::FLAG_FS_PREFIX || seg == DecodeFlags::FLAG_GS_PREFIX;
    const bool memory = (has_modrm && mod != 3) || (primary && op >= 0xA0 && op <= 0xA3) || source;
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
    if (!fsgs && memory && ss + data) {
      int base = source ? int(FEXCore::X86State::REG_RSI) : -1;
      for (const auto* o : {&d.Dest, &d.Src[0], &d.Src[1], &d.Src[2]}) {
        if (source) break;
        if (o->IsGPRDirect()) base = o->Data.GPR.GPR;
        else if (o->IsGPRIndirect()) base = o->Data.GPRIndirect.GPR;
        else if (o->IsSIB()) base = o->Data.SIB.Base;
        else if (!o->IsRIPRelative()) continue;
        break;
      }
      const bool stack = base == FEXCore::X86State::REG_RSP || base == FEXCore::X86State::REG_RBP;
      if ((ss && !stack) || (data && stack)) return Uns("CS/DS/ES/SS override naming another segment than the operand's default (#GP versus #SS)");
    }
  }
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
  Verdict v = Classify(d, !failed && d.TableInfo != nullptr);
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
    if (!rel || uint64_t(int64_t(target << 16) >> 16) == target) return false;
    v = Uns("direct branch to a non-canonical target");
  }
  fexc_cut = {v.kind == SYSTEM ? FEXC_CUT_SYSTEM : FEXC_CUT_UNSUPPORTED, rip, d.FexcMap, d.FexcMap == FEXC_MAP_VEX ? d.FexcVexOp : d.OPRaw, d.ModRM,
              bool(d.Flags & DecodeFlags::FLAG_DECODED_MODRM), d.TableInfo && d.TableInfo->Name ? d.TableInfo->Name : "?", v.why};
  return true;
}
} // namespace FexcSlice1
