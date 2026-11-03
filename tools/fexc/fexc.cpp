// fexc: compile x86 guest bytes with FEXCore (native macOS) and describe the
// resulting ARM64 block as one JSON document. Nothing is executed.
//
//   fexc (--hex HEX | --file PATH) --rip N [--bitness 32|64] [--max-insts N]
//        [--profile default|slice1] [--multiblock] [--sparse-pc] [--no-end-marker] [--feature NAME=0|1]... [--config NAME=VALUE]... [--out FILE] [--object FILE]
//
// General by construction: input bytes + RIP + mode + feature profile in, code +
// FEX metadata out. Nothing here is keyed by program name.
#include "FEXCore/Debug/InternalThreadState.h"
#include "Interface/Context/Context.h"
#include "Interface/Core/CPUBackend.h"
#include "Interface/Core/Frontend.h"
#include "Interface/Core/JIT/DebugData.h"
#include "Interface/Core/JIT/Relocations.h"
#include <FEXCore/Config/Config.h>
#include <FEXCore/Core/Context.h>
#include <FEXCore/Core/CoreState.h>
#include <FEXCore/Core/HostFeatures.h>
#include <FEXCore/Core/SignalDelegator.h>
#include <FEXCore/HLE/SyscallHandler.h>
#include <FEXCore/Utils/LogManager.h>
#include <FEXCore/fextl/memory.h>
#include <fexc_hook.h>

#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <map>
#include <stdexcept>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <functional>
#include <iterator>
#include <string>
#include <type_traits>
#include <vector>

using namespace FEXCore;

// ---------------------------------------------------------------- features
// The ONE place that defines the target CPU feature set handed to FEX. Today a
// fixed profile matching the old Linux producer (see README); later this table
// is to be filled from what the VM's vCPU exposes.
// Profile "old-producer-compat+m4": the bits the old Linux producer logged (SVE, LRCPC, LRCPC2, RPRES, WFXT off;
// LSE, preserve_all on) are pinned to the old values; every other bit is Apple M4's ID-register view.
struct FeatureDef {
  const char* name;
  std::function<void(HostFeatures&, bool)> set;
  std::function<bool(const HostFeatures&)> get;
  bool value; // profile default
};
#define FEAT(field, json, dflt) {json, [](HostFeatures& h, bool v) { h.field = v; }, [](const HostFeatures& h) { return bool(h.field); }, dflt}
static std::vector<FeatureDef> FeatureTable() {
  return {
    // Needed to match the old producer exactly (rpres and wfxt off as well).
    FEAT(SupportsAtomics, "atomics_lse", true), FEAT(SupportsRCPC, "lrcpc", false), FEAT(SupportsTSOImm9, "lrcpc2", false),
    FEAT(SupportsSVE128, "sve128", false), FEAT(SupportsSVE256, "sve256", false), FEAT(SupportsPreserveAllABI, "preserve_all_abi", true),
    FEAT(SupportsRPRES, "rpres", false), FEAT(SupportsWFXT, "wfxt", false),
    // Everything below is the Apple M4 ID-register view (sysctl hw.optional.arm.*); the old Linux VM's view is unverified.
    FEAT(SupportsFlagM, "flagm", true), FEAT(SupportsFlagM2, "flagm2", true), FEAT(SupportsFCMA, "fcma", true),
    FEAT(SupportsCRC, "crc32", true), FEAT(SupportsAES, "aes", true), FEAT(SupportsSHA, "sha", true),
    FEAT(SupportsPMULL_128Bit, "pmull128", true), FEAT(SupportsRAND, "rng", false), FEAT(SupportsCSSC, "cssc", false),
    FEAT(SupportsAFP, "afp", true), FEAT(SupportsFRINTTS, "frintts", true),
    FEAT(SupportsMOPS, "mops", false), FEAT(SupportsI8MM, "i8mm", true), FEAT(SupportsDotProd, "dotprod", true),
    FEAT(SupportsECV, "ecv", true), FEAT(SupportsCLZERO, "clzero", false),
    FEAT(SupportsSVEBitPerm, "svebitperm", false), FEAT(SupportsFloatExceptions, "float_exceptions", false),
    FEAT(SupportsCacheMaintenanceOps, "cache_maintenance_ops", true), FEAT(PreferZVAForVZero, "prefer_zva_for_vzero", false),
    // What FEX itself force-enables for the guest-visible x86 feature set.
    FEAT(SupportsAVX, "x86_avx", true), FEAT(SupportsAES256, "x86_aes256", true), FEAT(Supports3DNow, "x86_3dnow", true),
    FEAT(SupportsSSE4a, "x86_sse4a", false),
  };
}
// Profile "slice1" (slice-1 contract §2): 64-bit only, hardware TSO (FEX TSO emulation off), no end marker, dense PC table,
// multiblock off, FlagM required; block cutting at an allowlist (fexc_slice1.inc) and every exit through the resident
// dispatcher gate (no block linking, L1 lookup or call-return stack). "default" keeps FEX's own behaviour.
struct Settings {
  std::string profile = "default";
  bool tso = true;             // x86-TSO memory ordering (acquire/release loads and stores)
  bool multiblock = false;     // follow branches into further blocks inside the input window
  bool dense_pc = true;        // guest-PC table entry for every guest instruction (sites need it: see --sparse-pc)
  bool end_marker = true;      // FEX's InstCount marker (udf #0x420f) after the block body
  int64_t max_insts = 0;       // 0 = FEX default
  uint32_t dcache_line_log2 = 4;
};

// ---------------------------------------------------------------- JSON
struct Json {
  std::string s;
  std::vector<bool> first {true};
  void sep() { if (!first.back()) s += ','; first.back() = false; }
  void open(char c) { s += c; first.push_back(true); }
  void close(char c) { s += c; first.pop_back(); }
  static std::string esc(const std::string& v) {
    std::string o;
    for (unsigned char c : v) {
      if (c == '"' || c == '\\') { o += '\\'; o += char(c); }
      else if (c < 0x20) { char b[8]; snprintf(b, sizeof b, "\\u%04x", c); o += b; }
      else o += char(c);
    }
    return o;
  }
  // key + value helpers; values never need separators of their own
  Json& kv(const char* k, const std::string& v) { sep(); s += '"'; s += k; s += "\":\""; s += esc(v); s += '"'; return *this; }
  Json& kv(const char* k, const char* v) { return kv(k, std::string(v)); }
  template<class T, std::enable_if_t<std::is_integral_v<T> && !std::is_same_v<T, bool>, int> = 0>
  Json& kv(const char* k, T v) {
    sep(); s += '"'; s += k; s += "\":";
    s += std::is_signed_v<T> ? std::to_string((long long)v) : std::to_string((unsigned long long)v);
    return *this;
  }
  Json& kv(const char* k, bool v) { sep(); s += '"'; s += k; s += "\":"; s += v ? "true" : "false"; return *this; }
  Json& null(const char* k) { sep(); s += '"'; s += k; s += "\":null"; return *this; }
  Json& obj(const char* k) { sep(); s += '"'; s += k; s += "\":{"; first.push_back(true); return *this; }
  Json& arr(const char* k) { sep(); s += '"'; s += k; s += "\":["; first.push_back(true); return *this; }
  Json& elem_obj() { sep(); s += '{'; first.push_back(true); return *this; }
  Json& end_obj() { s += '}'; first.pop_back(); return *this; }
  Json& end_arr() { s += ']'; first.pop_back(); return *this; }
};
static std::string Hex(const uint8_t* p, size_t n) {
  static const char* d = "0123456789abcdef";
  std::string o;
  for (size_t i = 0; i < n; ++i) { o += d[p[i] >> 4]; o += d[p[i] & 15]; }
  return o;
}
static std::string Hex64(uint64_t v) { char b[24]; snprintf(b, sizeof b, "0x%llx", (unsigned long long)v); return b; }

// ---------------------------------------------------------------- guest window
namespace {
struct Window { uint64_t base = 0, size = 0; const uint8_t* bytes = nullptr; } W;
// The end (offset from the window base) of every guest range the compile checked or read, saturating (an address below the
// base counts as the whole address space). Every look at the guest bytes or at the window's size goes through InWindow: FEX's
// decoder learns the size from QueryGuestExecutableRange but only checks a range with it before reading that range. So the
// compile depends on bytes[0, Extent) alone and, when Extent < size, not on the size: the host keys its cache on that prefix.
uint64_t Extent = 0;
bool InWindow(uint64_t a, uint64_t n) {
  const uint64_t off = a - W.base;
  Extent = std::max(Extent, a < W.base || off + n < off ? UINT64_MAX : off + n);
  return a >= W.base && off <= W.size && n <= W.size - off;
}

struct Handler final : HLE::SyscallHandler {
  void HandleSyscall(Core::CpuStateFrame*) override {}
  // Only the supplied bytes are executable code; anything else ends the block (NOEXEC).
  HLE::ExecutableRangeInfo QueryGuestExecutableRange(Core::InternalThreadState*, uint64_t a) override {
    if (!InWindow(a, 1)) return {a, 0, false};
    return {W.base, W.size, false};
  }
  std::optional<ExecutableFileSectionInfo> LookupExecutableFileSection(Core::InternalThreadState*, uint64_t) override { return std::nullopt; }
  const uint8_t* ResolveGuestCodePointer(uint64_t a, uint64_t n, const uint8_t*) override {
    return InWindow(a, n) ? W.bytes + (a - W.base) : nullptr;
  }
};
struct Signals final : SignalDelegator {};

std::vector<std::string> Diagnostics;
void MsgHandler(LogMan::DebugLevels level, const char* msg) { Diagnostics.push_back(std::string(LogMan::DebugLevelStr(level)) + ": " + msg); }
void AssertHandler(const char* msg) { Diagnostics.push_back(std::string("ASSERT: ") + msg); }

// ---------------------------------------------------------------- hook capture
struct Capture {
  bool fired = false;
  FexcCompileInfo info {};
  std::vector<uint8_t> code;
  std::vector<std::pair<uint64_t, uint64_t>> pcs, subblocks, prologues;
  std::vector<CPU::Relocation> relocs;
  std::vector<uint32_t> dispatch_exits;   // slice1: computed exits' `b` to the resident dispatcher (relocated at placement)
  std::vector<uint8_t> sra_gpr, sra_fpr;
  // The explicit x86 memory operand FEX decoded (base/index are x86 register numbers, -1 = none).
  struct X86Mem { bool present = false, rip_relative = false; int base = -1, index = -1; unsigned scale = 1; int64_t disp = 0; };
  // seg: the FS/GS override FEX applies (4 fs, 5 gs, 0 none: the last FS/GS prefix; CS/DS/ES/SS are ignored in 64-bit mode).
  struct Inst { uint64_t pc; unsigned size; std::string name; bool block_end; bool sets_rip; X86Mem mem; int seg; };
  struct Block { uint64_t entry, size, count; int status; bool entry_point; };
  std::vector<Inst> insts;
  std::vector<Block> blocks;
  std::vector<FexcSiteInfo> sites;
  std::vector<FexcInsnInfo> insn_info;
  uint32_t spill_bytes = 0;
} Cap;

Capture::X86Mem MemOperand(const X86Tables::DecodedInst& d) {
  Capture::X86Mem m;
  for (const auto* o : {&d.Dest, &d.Src[0], &d.Src[1], &d.Src[2]}) {
    if (o->IsGPRDirect()) { m = {true, false, o->Data.GPR.GPR, -1, 1, 0}; }
    else if (o->IsGPRIndirect()) { m = {true, false, o->Data.GPRIndirect.GPR, -1, 1, o->Data.GPRIndirect.Displacement}; }
    else if (o->IsRIPRelative()) { m = {true, true, -1, -1, 1, o->Data.RIPLiteral.Value}; }
    else if (o->IsSIB()) {
      const auto& s = o->Data.SIB;
      m = {true, false, s.Base == 0xff ? -1 : s.Base, s.Index == 0xff ? -1 : s.Index, s.Scale, s.Offset};
    } else continue;
    break; // the admitted instructions have at most one explicit memory operand
  }
  return m;
}

// CF inversion at the start of the guest instruction whose PC-table entry is (guest, native): 1/0, or -1 if the entry
// is not an instruction start. The block entry (prologue) is in the ABI state, inverted.
int CfInverted(uint64_t guest, uint64_t native) {
  for (auto& i : Cap.insn_info) if (i.guest_offset == guest && i.native_offset == native) return i.cf_inverted;
  for (auto& p : Cap.prologues) if (p.first == guest) {
    for (auto& g : Cap.pcs) if (g.first == guest && g.second == native) return 1;
  }
  return -1;
}

int SegmentOverride(uint32_t flags) {
  const uint32_t s = flags & X86Tables::DecodeFlags::FLAG_SEGMENTS;
  return s == X86Tables::DecodeFlags::FLAG_FS_PREFIX ? 4 : s == X86Tables::DecodeFlags::FLAG_GS_PREFIX ? 5 : 0;
}
// Join each site with FEX's own decode of its guest instruction: segment (stack ops use SS, which decides #SS(0) vs #GP(0)
// for a non-canonical address; an explicit operand uses SS with an RSP/RBP base, else DS: CS/DS/ES/SS prefixes are ignored,
// owner decision of November 3, as on the reference AMD machine; an FS/GS-overridden operand is FS/GS, #GP(0), whatever its
// base) and the x86 addressing form.
// A load of a RIP-setting instruction without an explicit memory operand is the implicit stack access of slice1's ret;
// leave's load of [RBP] (slice1: a plain load, then RSP and RBP) is a stack access too.
// Guard/split/alignment stubs (slice1) take the width, intent, segment and data registers of the access they cover (their own
// base_reg + imm is the address as it is at the stub); a branch-target guard (no access) has its target register in
// base_reg and is DS (#GP(0)).
void JoinSites(uint64_t rip) {
  for (auto& s : Cap.sites) {
    s.segment = (s.kind == FEXC_SITE_PUSH || s.kind == FEXC_SITE_POP || s.kind == FEXC_SITE_PUSH_PAIR || s.kind == FEXC_SITE_POP_PAIR) ? 1 : 0;
    if (s.kind == FEXC_SITE_LOAD || s.kind == FEXC_SITE_STORE || s.kind == FEXC_SITE_SWAP || s.kind == FEXC_SITE_ATOMIC ||
        s.kind == FEXC_SITE_LOAD_PAIR || s.kind == FEXC_SITE_STORE_PAIR)   // (I5a: MOVAPS m128's X pair: its operand's segment)
      for (auto& i : Cap.insts) if (i.pc - rip == s.guest_offset) {
        const bool stack_base = i.mem.base == 4 || i.mem.base == 5;
        if (!i.mem.present ? i.sets_rip || i.name == "LEAVE" : !i.seg && stack_base) s.segment = 1;
        break;
      }
  }
  for (auto& s : Cap.sites) {
    const auto stub = [](uint8_t k) { return k == FEXC_SITE_GUARD || k == FEXC_SITE_SPLIT || k == FEXC_SITE_ALIGN; };
    if (!stub(s.kind) || s.access_native_offset == FEXC_NONE32) continue;
    for (auto& a : Cap.sites) {
      if (a.native_offset != s.access_native_offset || stub(a.kind)) continue;
      s.width = a.width; s.intent = a.intent; s.rmw = a.rmw; s.segment = a.segment; s.data_reg[0] = a.data_reg[0];
      s.data_reg[1] = a.data_reg[1]; s.data_class = a.data_class; s.ordering = a.ordering;
      break;
    }
  }
}
// M3: the completion record of a load or store site (shim/fexc_mmio.inc), or 0: its instruction's record when the site is
// that instruction's only access, a plain one (not half of a read-modify-write, a general register) of the recorded width
// and direction. The binary object carries it in the site's guest_offset2 slot, the JSON as "mmio".
uint32_t MmioRecord(const FexcSiteInfo& s, uint64_t rip) {
  if ((s.kind != FEXC_SITE_LOAD && s.kind != FEXC_SITE_STORE) || s.rmw != 0 || s.data_class != 0) return 0;
  const auto it = fexc_mmio.find(rip + s.guest_offset);
  if (it == fexc_mmio.end()) return 0;
  int accesses = 0;
  for (auto& a : Cap.sites) accesses += a.guest_offset == s.guest_offset && a.kind != FEXC_SITE_GUARD && a.kind != FEXC_SITE_SPLIT && a.kind != FEXC_SITE_ALIGN && a.kind != FEXC_SITE_DIVIDE &&
                                                       a.kind < FEXC_SITE_SIMD;
  const uint32_t r = it->second;
  return accesses == 1 && (1u << ((r >> 12) & 3)) == s.width && ((r >> 4) & 1) == uint32_t(s.kind == FEXC_SITE_STORE) ? r : 0;
}
const char* const KindName[] = {"load", "store", "load_pair", "store_pair", "push", "pop", "guard", "split", "push_pair", "pop_pair", "other", "divide", "swap", "atomic", "align",
                                "simd", "x87", "pending"};
const char* const ExtendName[] = {"none", "uxtw", "sxtw", "sxtx"};
const char* const RmwName[] = {"none", "load", "store"};
const char* const OrderingName[] = {"plain", "acquire", "release"};
const char* const WritebackName[] = {"none", "pre", "post"};

void Hook(Core::InternalThreadState* t, const FexcCompileInfo& info, const uint8_t* code, size_t size, const Core::DebugData& dd) {
  Cap.fired = true;
  Cap.info = info;
  Cap.code.assign(code, code + size);
  for (auto& g : dd.GuestOpcodes) Cap.pcs.push_back({g.GuestEntryOffset, uint64_t(g.HostEntryOffset)});
  for (auto& s : dd.Subblocks) Cap.subblocks.push_back({s.HostCodeOffset, s.HostCodeSize});
  Cap.prologues = {dd.FexcEntryPrologues.begin(), dd.FexcEntryPrologues.end()};
  if (dd.Relocations) Cap.relocs.assign(dd.Relocations->begin(), dd.Relocations->end());
  Cap.dispatch_exits = fexc_dispatch_exits;
  Cap.sra_gpr.assign(dd.FexcSRAGPR, dd.FexcSRAGPR + dd.FexcSRAGPRCount);
  Cap.sra_fpr.assign(dd.FexcSRAFPR, dd.FexcSRAFPR + dd.FexcSRAFPRCount);
  Cap.sites = fexc_sites;
  Cap.insn_info = fexc_insns;
  Cap.spill_bytes = fexc_spill_bytes;
  const auto* bi = t->FrontendDecoder->GetDecodedBlockInfo();
  for (auto& b : bi->Blocks) {
    Cap.blocks.push_back({b.Entry, b.Size, b.NumInstructions, int(b.BlockStatus), b.IsEntryPoint});
    for (uint64_t i = 0; i < b.NumInstructions; ++i) {
      auto& d = b.DecodedInstructions[i];
      Cap.insts.push_back({d.PC, d.InstSize, d.TableInfo && d.TableInfo->Name ? d.TableInfo->Name : "?",
                           d.TableInfo && (d.TableInfo->Flags & X86Tables::InstFlags::FLAGS_BLOCK_END),
                           d.TableInfo && (d.TableInfo->Flags & X86Tables::InstFlags::FLAGS_SETS_RIP), MemOperand(d),
                           SegmentOverride(d.Flags)});
    }
  }
}
} // namespace

static const char* StatusName(int s) {
  using S = Frontend::Decoder::DecodedBlockStatus;
  switch (S(s)) {
  case S::SUCCESS: return "success";
  case S::INVALID_INST: return "invalid_instruction";
  case S::NOEXEC_INST: return "not_executable";
  case S::PARTIAL_DECODE_INST: return "partial_decode";
  case S::BAD_RELOCATION: return "bad_relocation";
  case S::UNIMPLEMENTED_INST: return "unimplemented_instruction";
  case S::TOO_LONG_INST: return "instruction_too_long";
  }
  return "unknown";
}

// A served request's result (--serve): exit code as the CLI would return it, the JSON report and the binary object.
struct Reply { int code = 0; std::string json; std::vector<uint8_t> object; uint32_t extent = 0, args_read = 0; };
static Reply* Served = nullptr;   // set while a --serve request is compiled
static bool ServeReport = false;  // --serve --report: each reply carries the full JSON report (test_serve.py)

static int Fail(const std::string& msg, int code = 2) {
  if (Served) Served->json = msg; else fprintf(stderr, "fexc: %s\n", msg.c_str());
  return code;
}

// Codes shared with crates/abi/src/code_object.rs (end_reason).
static uint8_t EndReasonCode(const std::string& r) {
  static const char* const names[] = {"", "end_of_input", "block_ending_instruction", "max_instructions", "fex_instruction_limit", "system_instruction",
    "unsupported_form", "dispatch_failure", "invalid_instruction", "partial_decode", "not_executable", "bad_relocation", "unimplemented_instruction", "instruction_too_long"};
  for (size_t i = 1; i < sizeof names / sizeof *names; ++i) if (r == names[i]) return uint8_t(i);
  return 255;
}
static const char* CutReasonName() {
  if (fexc_cut.reason == FEXC_CUT_STRING) return "block_ending_instruction";
  return fexc_cut.reason == FEXC_CUT_SYSTEM ? "system_instruction" : fexc_cut.reason == FEXC_CUT_DECODER ? StatusName(fexc_cut.status) : "unsupported_form";
}
static std::string CutDetail() {
  if (fexc_cut.reason == FEXC_CUT_DECODER) return "at " + Hex64(fexc_cut.rip) + " (decoder status on the first instruction; no code, no NoExec block)";
  if (fexc_cut.reason == FEXC_CUT_STRING) return "an instruction that exits to its own RIP (a REP string instruction until it is done, a LOCK NEG until its compare-and-swap succeeds)";
  char b[64]; snprintf(b, sizeof b, "opcode map %d opcode 0x%02x", int(fexc_cut.map), fexc_cut.opcode);
  return std::string(fexc_cut.why) + " (" + b + (fexc_cut.has_modrm ? (std::string(" /") + std::to_string((fexc_cut.modrm >> 3) & 7)) : "") +
         ", FEX decoded it as '" + fexc_cut.name + "')";
}
static void CutJson(Json& j) {
  j.obj("end").kv("rip", Hex64(fexc_cut.rip)).kv("reason", CutReasonName()).kv("detail", CutDetail());
  if (fexc_cut.reason != FEXC_CUT_DECODER && fexc_cut.reason != FEXC_CUT_STRING) j.kv("cut_opcode_map", int(fexc_cut.map)).kv("cut_opcode", int(fexc_cut.opcode)).kv("cut_fex_name", fexc_cut.name);
  if (fexc_cut.gated_mmx) j.kv("cut_gated", "mmx");
  else if (fexc_cut.gated) j.kv("cut_gated", true);
  if (fexc_cut.ud) j.kv("cut_ud", true);
  j.end_obj();
}

// FEX config option by name (the enum name without CONFIG_, e.g. X87REDUCEDPRECISION; a leading FEX_ is accepted, case ignored).
static bool ConfigOptionByName(std::string n, Config::ConfigOption& out) {
  static const std::pair<const char*, Config::ConfigOption> opts[] = {
#define OPT_BASE(type, group, enum, json, default) {#enum, Config::CONFIG_##enum},
#include <FEXCore/Config/ConfigValues.inl>
  };
  for (auto& c : n) c = char(toupper(c));
  if (n.rfind("FEX_", 0) == 0) n = n.substr(4);
  for (auto& o : opts) if (n == o.first) { out = o.second; return true; }
  return false;
}

// Everything the compile reads from process-wide state is reset here, so a request never sees an earlier one's.
static void ResetGlobals() {
  Cap = {};
  Diagnostics.clear();
  W = {};
  fexc_sites.clear(); fexc_insns.clear(); fexc_cf_inverted.clear(); fexc_spill_bytes = 0; fexc_cut = {}; fexc_after_rep = 0;
  fexc_rip_relative.clear(); fexc_target_check.clear(); fexc_div_width.clear(); fexc_movs.clear(); fexc_lock_cas.clear();
  fexc_align16.clear(); fexc_align32.clear(); fexc_avx_gated = false; fexc_sse_gated = false; fexc_alignment_gated = false; fexc_fp_signal.clear(); fexc_fp_quiet.clear(); fexc_round_inexact.clear(); fexc_mmio.clear();
  fexc_x87_gated = false; fexc_wait_gated = false; fexc_x87_fpcr = 0; fexc_x87.clear();
  fexc_simd_unmasked = 0; fexc_x87_unmasked = 0; fexc_x87_waiting.clear(); fexc_x87_store.clear(); fexc_x87_length.clear(); fexc_simd_checked.clear(); fexc_x87_extra.clear();
  fexc_args_read = 0; Extent = 0;
}

// The CLI's body. `args` are the command-line arguments (args[0] is the program name); `given` replaces --hex/--file with the
// bytes themselves and `rep` collects the outputs instead of stdout/--out/--object (both set only by --serve).
// One FEX context per configuration (everything fixed at its creation: the Config values set below, the host features, the
// profile), created at its first request and kept: a context is never torn down (FEX's destructors assume a running emulator
// thread). A request leaves nothing behind in it (see the compile below), so a kept context compiles exactly as a new one.
struct Ctx { fextl::unique_ptr<Context::Context> ctx; Core::InternalThreadState* thread = nullptr; Handler handler; Signals signals; Core::CPUState::gdt_segment gdt[32] {}; };
static int CompileRequest(const std::vector<std::string>& args, const std::vector<uint8_t>* given, Reply* rep) {
  Served = rep;
  ResetGlobals();
  const int argc = int(args.size());
  std::string hex, file, out, object;
  bool have_rip = false;
  uint64_t rip = 0, bitness = 64;
  Settings st;
  bool sparse_flag = false;
  std::vector<std::pair<std::string, bool>> overrides;
  std::vector<std::pair<std::string, std::string>> configs;   // --config NAME=VALUE, applied after fexc's own Config::Set calls
  std::string missing;
  for (int i = 1; i < argc && missing.empty(); ++i) {
    std::string a = args[i];
    auto next = [&]() -> const char* { if (i + 1 >= argc) { missing = a; return ""; } return args[++i].c_str(); };
    if (a == "--hex") hex = next();
    else if (a == "--file") file = next();
    else if (a == "--rip") { rip = strtoull(next(), nullptr, 0); have_rip = true; }
    else if (a == "--bitness") bitness = strtoull(next(), nullptr, 0);
    else if (a == "--max-insts") st.max_insts = strtoll(next(), nullptr, 0);
    else if (a == "--profile") {
      st.profile = next();
      if (st.profile == "slice1") { st.tso = false; st.end_marker = false; st.dense_pc = true; st.multiblock = false; }
      else if (st.profile != "default") return Fail("--profile must be default or slice1");
    }
    else if (a == "--multiblock") st.multiblock = true;
    else if (a == "--dense-pc") st.dense_pc = true; // default since v2
    else if (a == "--sparse-pc") { st.dense_pc = false; sparse_flag = true; } // diagnostic only: an instruction without a GuestOpcode (e.g. moffs movabs) is then attributed to the previous one
    else if (a == "--no-end-marker") st.end_marker = false;
    else if (a == "--no-tso") st.tso = false;
    else if (a == "--sse-gated") fexc_sse_gated = true;   // slice1 (I5a): CR0.EM/TS or no CR4.OSFXSR: SSE forms are system cuts
    else if (a == "--avx-gated") fexc_avx_gated = true;   // slice1 (I8): no OSXSAVE, XCR0[2:1] != 11b or CR0.TS: VEX AVX forms are system cuts
    else if (a == "--alignment-gated") fexc_alignment_gated = true;   // slice1 (X2): alignment checking on: BT* memory forms are system cuts
    else if (a == "--x87-gated") fexc_x87_gated = true;   // slice1 (I6): CR0.EM or CR0.TS: x87 forms are system cuts (#NM)
    else if (a == "--wait-gated") fexc_wait_gated = true; // slice1 (I6): CR0.TS and CR0.MP: WAIT/FWAIT is a system cut (#NM)
    else if (a == "--x87-fpcr") fexc_x87_fpcr = strtoull(next(), nullptr, 0);   // slice1 (I6): FPCR of x87 arithmetic when MXCSR's differs
    else if (a == "--simd-unmasked") fexc_simd_unmasked = strtoull(next(), nullptr, 0) & 0x3f;   // slice1 (I5c): MXCSR's unmasked exceptions
    else if (a == "--x87-unmasked") fexc_x87_unmasked = strtoull(next(), nullptr, 0) & 0x3f;     // slice1 (I5c): FCW's unmasked exceptions
    else if (a == "--feature") { std::string v = next(); auto eq = v.find('='); if (eq == std::string::npos) return Fail("--feature NAME=0|1"); overrides.push_back({v.substr(0, eq), v.substr(eq + 1) != "0"}); }
    else if (a == "--config") { std::string v = next(); auto eq = v.find('='); Config::ConfigOption o; if (eq == std::string::npos || !ConfigOptionByName(v.substr(0, eq), o)) return Fail("--config NAME=VALUE (NAME: a FEX config option, e.g. X87REDUCEDPRECISION)"); configs.push_back({v.substr(0, eq), v.substr(eq + 1)}); }
    else if (a == "--out") out = next();
    else if (a == "--object") object = next();
    else return Fail("unknown argument " + a);
  }
  if (!missing.empty()) return Fail(missing + " needs a value");
  if (!have_rip || (!given && hex.empty() == file.empty())) return Fail("usage: fexc (--hex HEX | --file PATH) --rip N [--bitness 32|64] [--max-insts N] [--profile default|slice1] [--multiblock] [--sparse-pc] [--no-end-marker] [--no-tso] [--sse-gated] [--avx-gated] [--alignment-gated] [--x87-gated] [--wait-gated] [--x87-fpcr V] [--simd-unmasked M] [--x87-unmasked M] [--feature NAME=0|1] [--config NAME=VALUE] [--out FILE] [--object FILE]");
  if (bitness != 32 && bitness != 64) return Fail("--bitness must be 32 or 64");
  const bool slice1 = st.profile == "slice1";
  if (slice1 && bitness != 64) return Fail("profile slice1 is 64-bit only");
  if (slice1 && sparse_flag) return Fail("profile slice1 needs the dense PC table (sites would be attributed to the previous instruction)");
  if (slice1 && (st.tso || st.multiblock || st.end_marker)) return Fail("profile slice1 fixes TSO emulation off, multiblock off and no end marker");
  for (auto& c : configs) {   // slice1 (I6): x87 runs in reduced precision only (the resident's state model is doubles)
    Config::ConfigOption o;
    if (slice1 && ConfigOptionByName(c.first, o) && o == Config::CONFIG_X87REDUCEDPRECISION && c.second != "1")
      return Fail("profile slice1 fixes X87REDUCEDPRECISION=1");
  }

  std::vector<uint8_t> bytes;
  if (given) bytes = *given;
  else if (!file.empty()) {
    std::ifstream f(file, std::ios::binary);
    if (!f) return Fail("cannot read " + file);
    bytes.assign(std::istreambuf_iterator<char>(f), {});
  } else {
    if (hex.size() % 2) return Fail("odd-length hex");
    for (size_t i = 0; i < hex.size(); i += 2) {
      char b[3] = {hex[i], hex[i + 1], 0}; char* e;
      unsigned v = strtoul(b, &e, 16);
      if (*e) return Fail("bad hex");
      bytes.push_back(uint8_t(v));
    }
  }
  if (bytes.empty() || bytes.size() > 65536) return Fail("input must be 1..65536 bytes");
  // Window must lie inside the guest address space of the chosen mode.
  if (bitness == 32 ? (rip > 0xffffffffull || bytes.size() > 0x100000000ull - rip)
                    : (bytes.size() > UINT64_MAX - rip || (rip >> 47 != 0 && rip >> 47 != 0x1ffff) ||
                       ((rip + bytes.size() - 1) >> 47 != 0 && (rip + bytes.size() - 1) >> 47 != 0x1ffff) || ((rip >> 47) != ((rip + bytes.size() - 1) >> 47))))
    return Fail("RIP window is outside the guest address space (non-canonical, wraps, or exceeds 32 bits)");
  W = {rip, bytes.size(), bytes.data()};

  // ---- configure FEX. Config is process-global: rebuilt from nothing for every request, so no earlier request's values
  // (e.g. a --config) remain; a context reads it at its creation, the compile reads some options again.
  std::vector<std::pair<Config::ConfigOption, std::string>> sets = {
    {Config::CONFIG_IS64BIT_MODE, bitness == 64 ? "1" : "0"}, {Config::CONFIG_DISABLETELEMETRY, "1"},
    {Config::CONFIG_MULTIBLOCK, st.multiblock ? "1" : "0"}, {Config::CONFIG_TSOENABLED, st.tso ? "1" : "0"},
    {Config::CONFIG_VECTORTSOENABLED, st.tso ? "1" : "0"}, {Config::CONFIG_MEMCPYSETTSOENABLED, st.tso ? "1" : "0"}};
  // slice1 (I6): x87 in FEX's reduced-precision mode (doubles; owner direction, boot-features plan I6).
  if (slice1) sets.push_back({Config::CONFIG_X87REDUCEDPRECISION, "1"});
  for (auto& c : configs) { Config::ConfigOption o; ConfigOptionByName(c.first, o); sets.push_back({o, c.second}); }
  LogMan::Throw::InstallHandler(AssertHandler);
  LogMan::Msg::InstallHandler(MsgHandler);
  Config::Shutdown();
  Config::Initialize();
  for (auto& c : sets) Config::Set(c.first, c.second);

  HostFeatures hf {};
  auto table = FeatureTable();
  for (auto& o : overrides) {
    bool found = false;
    for (auto& f : table) if (o.first == f.name) { f.value = o.second; found = true; }
    if (!found) return Fail("unknown feature " + o.first);
  }
  if (slice1) for (auto& f : table) if (std::string(f.name) == "flagm" && !f.value) return Fail("profile slice1 requires flagm");
  for (auto& f : table) f.set(hf, f.value);
  hf.DCacheLineLog2 = st.dcache_line_log2;
  hf.IsInstCountCI = st.end_marker;
  hf.HostType = HostFeatures::HostTypeEnum::Linux;

  fexc_compile_hook = Hook;
  fexc_guest_bytes = [](uint64_t a, uint64_t n) -> const uint8_t* { return InWindow(a, n) ? W.bytes + (a - W.base) : nullptr; };
  fexc_dense_pc_table = st.dense_pc;
  fexc_slice1 = slice1;

  static std::map<std::string, Ctx*> kept;
  std::string key = st.profile + (st.dense_pc ? "|d" : "|s") + (st.end_marker ? "e|" : "n|") + std::to_string(st.dcache_line_log2) + "|";
  for (auto& f : table) key += f.get(hf) ? '1' : '0';
  for (auto& c : sets) key += "|" + std::to_string(int(c.first)) + "=" + c.second;
  Ctx*& kc = kept[key];
  if (!kc) {
    kc = new Ctx;
    kc->ctx = Context::Context::CreateNewContext(hf);
    kc->ctx->SetSignalDelegator(&kc->signals);
    kc->ctx->SetSyscallHandler(&kc->handler);
    if (!kc->ctx->InitCore()) { kept.erase(key); return Fail("InitCore failed", 3); }
    kc->thread = kc->ctx->CreateThread();
  }
  auto* thread = kc->thread;
  {
    // Code segment as a flat user CS (same setup FEX's CodeSizeValidation uses).
    auto* gdt = kc->gdt;
    auto& state = thread->CurrentFrame->State;
    state.segment_arrays[Core::CPUState::SEGMENT_ARRAY_INDEX_GDT] = &gdt[0];
    state.segment_arrays[Core::CPUState::SEGMENT_ARRAY_INDEX_LDT] = &gdt[0];
    state.cs_idx = Core::CPUState::DEFAULT_USER_CS << 3;
    auto* g = Core::CPUState::GetSegmentFromIndex(state, state.cs_idx);
    Core::CPUState::SetGDTBase(g, 0);
    Core::CPUState::SetGDTLimit(g, 0xFFFFF);
    state.cs_cached = Core::CPUState::CalculateGDTBase(*g);
    g->L = bitness == 64; g->D = bitness != 64;
  }
  {
    // FEX's CompileBlock without its caches: the block is never entered into the context's lookup cache (which would answer
    // a later request at this RIP with this block), its relocations are cleared and its code buffer is emptied (the hook
    // has copied the code), so the context holds nothing of this request afterwards.
    auto* impl = static_cast<Context::ContextImpl*>(kc->ctx.get());
    const uint64_t max = st.max_insts > 0 ? uint64_t(st.max_insts) : 0;
    thread->FrontendDecoder->SetupDecodeInstructionsAtEntry(thread, rip, max);
    (void)impl->CompileCode(thread, rip, max);
    thread->CPUBackend->ClearRelocations();
    impl->GetLatest()->Reset();
  }

  // ---- describe
  // The report: always for the CLI; from --serve only with --report. Otherwise the server sends what the host reads: nothing
  // for an object, `guest.end` (and the status) for no code. The object, extent and args_read do not depend on it.
  const bool report = !rep || ServeReport;
  // --serve leaves the hex dumps out (input bytes, each instruction's, the code): its caller has the bytes, the object has the
  // code, and dumping them was most of a request's fixed cost.
  const bool dumps = !rep;
  Json j;
  j.open('{');
  if (report) {
    j.kv("format", "natemu-fexc-v2");
    j.obj("producer").kv("tool", "fexc").kv("fex_commit", "48d71752e2a363d5535181f803b0b0e9666c1825").kv("host", "macos-arm64-native").end_obj();
    j.obj("input");
    if (dumps) j.kv("bytes_hex", Hex(bytes.data(), bytes.size()));
    j.kv("size", bytes.size()).kv("rip", Hex64(rip)).kv("bitness", bitness);
    if (st.max_insts > 0) j.kv("max_instructions", st.max_insts); else j.null("max_instructions");
    j.end_obj();
    j.obj("settings").kv("profile", st.profile).kv("block_cutting", slice1 ? "allowlist" : "none")
     .kv("exits", slice1 ? "dispatcher_gate" : "linked").kv("tso", st.tso).kv("multiblock", st.multiblock).kv("dense_pc_table", st.dense_pc).kv("end_marker", st.end_marker)
     .kv("fresh_context_per_request", true);   // (a kept context holds nothing of earlier requests: see the compile above)
    if (!configs.empty()) {   // only when given: output without --config is unchanged
      j.obj("config");
      for (auto& c : configs) j.kv(c.first.c_str(), c.second);
      j.end_obj();
    }
    j.end_obj();
    j.obj("features").kv("profile", "old-producer-compat+m4").kv("required_by_settings_profile", slice1 ? "flagm" : "none").kv("dcache_line_log2", uint64_t(st.dcache_line_log2));
    j.obj("host_features");
    for (auto& f : table) j.kv(f.name, f.get(hf));
    j.end_obj().end_obj();
  }

  if (!Cap.fired) {
    j.kv("status", "no_code").kv("error", fexc_cut.reason ? "no native code: the first instruction is outside the profile's allowlist or could not be fetched"
                                                          : "FEX produced no native code (first instruction could not be dispatched)");
    if (fexc_cut.reason) {
      j.obj("guest").kv("instructions_translated", 0).kv("bytes_translated", 0);
      CutJson(j);
      j.end_obj();
    }
    j.arr("diagnostics"); for (auto& d : Diagnostics) { j.sep(); j.s += '"' + Json::esc(d) + '"'; } j.end_arr();
    j.end_obj();
    std::string o = j.s + "\n";
    if (rep) rep->json = o; else if (out.empty()) fwrite(o.data(), 1, o.size(), stdout); else std::ofstream(out) << o;
    return 1;
  }

  JoinSites(rip);
  std::string end_reason_str;   // guest.end.reason, also recorded in the binary object
  std::stable_sort(Cap.sites.begin(), Cap.sites.end(), [](const FexcSiteInfo& a, const FexcSiteInfo& b) { return a.native_offset < b.native_offset; });
  const auto& c = Cap.code;
  uint32_t tail_off = 0; std::memcpy(&tail_off, c.data(), 4);
  CPU::CPUBackend::JITCodeTail tail {};
  const bool layout_ok = c.size() >= sizeof(uint32_t) && tail_off + sizeof(tail) <= c.size();
  if (layout_ok) std::memcpy(&tail, c.data() + tail_off, sizeof tail);

  // Relocation slots hold process-specific host pointers (dispatcher entry) or guest RIPs.
  // The code is exported with literal slots zeroed; the emitted values are listed per relocation.
  std::vector<uint8_t> code = c;
  for (auto& r : Cap.relocs) {
    using T = CPU::RelocationTypes;
    const uint64_t off = r.Header.Offset;
    if ((r.Header.Type == T::RELOC_NAMED_SYMBOL_LITERAL || r.Header.Type == T::RELOC_GUEST_RIP_LITERAL ||
         r.Header.Type == T::RELOC_GUEST_PATCHABLE_RIP_LITERAL) && off + 8 <= code.size())
      std::memset(code.data() + off, 0, 8);
  }
  // layout
  uint64_t marker = UINT64_MAX;
  for (size_t o = 4; o + 4 <= (layout_ok ? tail_off : c.size()); o += 4) {
    uint32_t w; std::memcpy(&w, c.data() + o, 4);
    if (w == 0x0000420f) { marker = o; break; } // udf #0x420f
  }
  // Why translation stopped (guest.end; its reason is also in the object).
  std::string end_detail;
  const uint64_t end_rip = rip + Cap.info.TotalLength;
  {
    const auto& lb = Cap.blocks.back();
    std::string& reason = end_reason_str;
    std::string& detail = end_detail;
    // FEX appends a placeholder "instruction" (no table entry) for whatever stopped the decode.
    if (fexc_cut.reason) {
      reason = CutReasonName(); detail = CutDetail();
    }
    else if (Cap.info.TotalInstructions < Cap.insts.size()) {
      const auto& stop = Cap.insts[Cap.info.TotalInstructions];
      const bool no_table = stop.name == "?";
      if (no_table && lb.status == int(Frontend::Decoder::DecodedBlockStatus::NOEXEC_INST) && !InWindow(end_rip, 1)) {
        reason = "end_of_input"; detail = "the next instruction lies outside the supplied bytes";
      } else if (no_table && lb.status != int(Frontend::Decoder::DecodedBlockStatus::SUCCESS)) {
        reason = StatusName(lb.status); detail = "at " + Hex64(stop.pc);
      } else {
        reason = "dispatch_failure"; detail = "instruction " + std::to_string(Cap.info.TotalInstructions) + " (" + stop.name + ") could not be translated by FEX's OpDispatcher";
      }
    }
    else if (lb.status != int(Frontend::Decoder::DecodedBlockStatus::SUCCESS)) { reason = StatusName(lb.status); }
    else if (!Cap.insts.empty() && (Cap.insts.back().block_end || Cap.insts.back().sets_rip)) { reason = "block_ending_instruction"; detail = Cap.insts.back().name; }
    else if (st.max_insts > 0 && Cap.info.TotalInstructions >= uint64_t(st.max_insts)) reason = "max_instructions";
    else if (Cap.info.TotalInstructions >= uint64_t(Config::Get_MAXINST())) { reason = "fex_instruction_limit"; detail = "FEX's per-block default (MAXINST)"; }
    else if (!InWindow(end_rip, 1)) { reason = "end_of_input"; detail = "last instruction ends at the end of the supplied bytes"; }
    else reason = "other"; // e.g. dispatcher-requested block end (system/exception instruction) or FEX's own limit
  }
  const auto end_json = [&] {
    j.obj("end").kv("rip", Hex64(end_rip)).kv("reason", end_reason_str).kv("detail", end_detail);
    if (fexc_cut.reason && fexc_cut.reason != FEXC_CUT_STRING) j.kv("cut_opcode_map", int(fexc_cut.map)).kv("cut_opcode", int(fexc_cut.opcode)).kv("cut_fex_name", fexc_cut.name);
    j.end_obj();
  };

  j.kv("status", Cap.info.TotalInstructions ? "ok" : "no_instructions_translated");
  if (!report) {
    if (!Cap.info.TotalInstructions) { j.obj("guest"); end_json(); j.end_obj(); }
  } else {
    j.obj("code");
    j.arr("relocations");
    for (auto& r : Cap.relocs) {
      using T = CPU::RelocationTypes;
      uint64_t off = r.Header.Offset;
      j.elem_obj().kv("offset", off);
      auto literal = [&](const char* kind) {
        uint64_t v = 0; if (off + 8 <= c.size()) std::memcpy(&v, c.data() + off, 8);
        j.kv("kind", kind).kv("size", 8).kv("emitted_value", Hex64(v));
      };
      switch (r.Header.Type) {
      case T::RELOC_NAMED_SYMBOL_LITERAL:
        literal("named_symbol_literal");
        j.kv("symbol", r.NamedSymbolLiteral.Symbol == CPU::RelocNamedSymbolLiteral::NamedSymbol::SYMBOL_LITERAL_EXITFUNCTION_LINKER
                         ? "exit_function_linker" : "unknown");
        j.kv("meaning", "host address of FEX's dispatcher block-linker; the resident runtime supplies its own");
        break;
      case T::RELOC_GUEST_RIP_LITERAL: literal("guest_rip_literal"); j.kv("guest_rip", Hex64(r.GuestRIP.GuestRIP)); break;
      case T::RELOC_GUEST_PATCHABLE_RIP_LITERAL:
        literal("guest_patchable_rip_literal"); j.kv("site_address", Hex64(r.GuestPatchableData.SiteAddress)).kv("value_size", int(r.GuestPatchableData.ValueSize)); break;
      case T::RELOC_GUEST_RIP_MOVE:
        j.kv("kind", "guest_rip_move").kv("register", int(r.GuestRIP.RegisterIndex)).kv("guest_rip", Hex64(r.GuestRIP.GuestRIP)).null("size");
        break;
      case T::RELOC_NAMED_THUNK_MOVE: j.kv("kind", "named_thunk_move").kv("register", int(r.NamedThunkMove.RegisterIndex)).null("size"); break;
      case T::RELOC_GUEST_PATCHABLE_DATA_MOVE: j.kv("kind", "guest_patchable_data_move").kv("register", int(r.GuestPatchableData.RegisterIndex)).null("size"); break;
      case T::RELOC_GUEST_PATCHABLE_RIP_MOVE: j.kv("kind", "guest_patchable_rip_move").kv("register", int(r.GuestPatchableData.RegisterIndex)).null("size"); break;
      case T::RELOC_GUEST_PATCHABLE_CRC_MOVE: j.kv("kind", "guest_patchable_crc_move").kv("register", int(r.GuestPatchableData.RegisterIndex)).null("size"); break;
      default: j.kv("kind", "unknown").kv("type", uint64_t(r.Header.Type)); break;
      }
      j.end_obj();
    }
    for (auto o : Cap.dispatch_exits)
      j.elem_obj().kv("offset", uint64_t(o)).kv("kind", "dispatch_branch").null("size")
       .kv("meaning", "placeholder `b .`: the resident relocates it to its dispatcher when it places the block").end_obj();
    j.end_arr();
    // (relocations written inside "code" object after we know zeroed bytes; hex goes last)
    j.kv("size", code.size());
    j.kv("alignment", 16);
    j.obj("layout");
    j.obj("header").kv("offset", 0).kv("size", 4).kv("note", "JITCodeHeader: u32 offset to tail");
    j.end_obj();
    j.arr("entries");
    for (auto& p : Cap.prologues) {
      uint64_t native = 0;
      for (auto& g : Cap.pcs) if (g.first == p.first) { native = g.second; break; }
      j.elem_obj().kv("guest_offset", p.first).kv("entry_native_offset", native).kv("body_native_offset", p.second)
       .kv("note", "[entry, body) is the entry prologue (slice1: the stop `bl` to the pad)").end_obj();
    }
    j.end_arr();
    j.kv("spill_bytes", Cap.spill_bytes); // the entry prologue subtracts this from sp; ResetStack restores it
    if (marker != UINT64_MAX) j.kv("end_marker_offset", marker).kv("end_marker", "udf #0x420f"); else j.null("end_marker_offset");
    if (layout_ok) {
      j.obj("tail").kv("offset", tail_off).kv("size", sizeof(tail)).kv("rip", Hex64(tail.RIP)).kv("guest_size", tail.GuestSize)
       .kv("single_inst", bool(tail.SingleInst)).kv("rip_entry_count", uint64_t(tail.NumberOfRIPEntries))
       .kv("rip_entries_offset", tail_off + tail.OffsetToRIPEntries).kv("note", "JITCodeTail then FEX-encoded (vl64) PC entries; both are runtime-recovery data of FEX's own format").end_obj();
      j.kv("code_region_begin", 4).kv("code_region_end", tail_off);
    } else {
      j.null("tail");
    }
    j.end_obj(); // layout
    if (dumps) {
      j.kv("hex", Hex(code.data(), code.size()));
      j.kv("hex_note", "relocation literal slots are zeroed; emitted values are in relocations[]");
    }
    j.end_obj(); // code

    j.obj("state_abi").kv("state_register", "x28").kv("state_struct", "FEXCore::Core::CpuStateFrame");
    j.arr("static_registers_gpr_order_rax_rcx_rdx_rbx_rsp_rbp_rsi_rdi_r8_to_r15_then_pf_af");
    for (auto g : Cap.sra_gpr) { j.sep(); j.s += std::to_string(g); }
    j.end_arr();
    j.arr("static_registers_xmm0_up");
    for (auto g : Cap.sra_fpr) { j.sep(); j.s += std::to_string(g); }
    j.end_arr();
  #define OFF(n, expr) j.kv(n, uint64_t(offsetof(Core::CpuStateFrame, expr)))
    j.obj("state_offsets");
    OFF("rip", State.rip); OFF("gregs", State.gregs); OFF("xmm", State.xmm); OFF("flags", State.flags); OFF("pf_raw", State.pf_raw);
    OFF("af_raw", State.af_raw); OFF("mxcsr", State.mxcsr); OFF("fs_cached", State.fs_cached); OFF("gs_cached", State.gs_cached);
    OFF("pointers", Pointers); OFF("exit_function_linker", Pointers.ExitFunctionLinker); OFF("dispatcher_loop_top", Pointers.DispatcherLoopTop);
    if (slice1) j.kv("natemu_pre_nzcv_register", 25); // x25: incoming NZCV for nzcv=saved sites (`mrs x25, nzcv` at the instruction start)
    j.end_obj();
    // External references that are not relocations: helper/dispatch pointers loaded from the state frame.
    j.arr("state_pointer_loads");
    {
      const uint64_t pb = offsetof(Core::CpuStateFrame, Pointers), pe = pb + sizeof(Core::JITPointers);
      struct Named { const char* n; uint64_t o; };
  #define P(f) {#f, offsetof(Core::CpuStateFrame, Pointers.f)}
      const Named named[] = {P(PrintValue), P(PrintVectorValue), P(PrintMsgValue), P(ThreadRemoveCodeEntryFromJIT), P(CPUIDObj), P(CPUIDFunction),
        P(XCRFunction), P(SyscallHandlerObj), P(SyscallHandlerFunc), P(ExitFunctionLink), P(MonoBackpatcherWrite), P(LUDIV), P(LDIV),
        P(RDRANDFallback), P(ThunkCallbackRet), P(ExitFunctionEC), P(DispatcherLoopTop), P(DispatcherLoopTopFillSRA), P(ExitFunctionLinker),
        P(ThreadStopHandlerSpillSRA), P(ThreadPauseHandlerSpillSRA), P(GuestSignal_SIGILL), P(GuestSignal_SIGTRAP), P(GuestSignal_SIGSEGV),
        P(SignalReturnHandler), P(SignalReturnHandlerRT), P(L2Pointer), P(LUDIVHandler), P(LDIVHandler), P(F64SinHandler), P(F64CosHandler),
        P(F64TanHandler), P(F64F2XM1Handler), P(F64ScaleHandler), P(F64AtanHandler), P(F64FYL2XHandler), P(F64FYL2XP1Handler), P(F64FPREMHandler),
        P(F64FPREM1Handler)};
      const size_t end = layout_ok ? tail_off : c.size();
      for (size_t o = 4; o + 4 <= end; o += 4) {
        uint32_t w; std::memcpy(&w, c.data() + o, 4);
        if ((w & 0xFFC00000u) != 0xF9400000u || ((w >> 5) & 31) != 28) continue; // ldr Xt, [x28, #imm]
        uint64_t off = uint64_t((w >> 10) & 0xFFF) * 8;
        if (off < pb || off >= pe) continue;
        const char* nm = nullptr;
        for (auto& n : named) if (n.o == off) nm = n.n;
        j.elem_obj().kv("native_offset", o).kv("state_offset", off).kv("dest_register", int(w & 31));
        if (nm) j.kv("pointer", nm); else j.kv("pointer", "indexed_table_entry (fallback handlers / vector constants / telemetry)");
        j.end_obj();
      }
    }
    j.end_arr().end_obj();

    j.obj("guest");
    j.kv("instructions_translated", Cap.info.TotalInstructions).kv("bytes_translated", Cap.info.TotalLength)
     .kv("decoded_start", Hex64(Cap.info.DecodedStart)).kv("decoded_length", Cap.info.DecodedLength);
    j.arr("blocks");
    for (auto& b : Cap.blocks)
      j.elem_obj().kv("entry", Hex64(b.entry)).kv("size", b.size).kv("instructions", b.count).kv("status", StatusName(b.status)).kv("is_entry_point", b.entry_point).end_obj();
    j.end_arr();
    j.arr("instructions");
    uint64_t idx = 0;
    for (auto& i : Cap.insts) {
      j.elem_obj().kv("translated", idx++ < Cap.info.TotalInstructions).kv("rip", Hex64(i.pc)).kv("offset", i.pc - rip).kv("size", i.size).kv("name", i.name).kv("ends_block", i.block_end).kv("sets_rip", i.sets_rip);
      if (dumps && InWindow(i.pc, i.size)) j.kv("bytes_hex", Hex(W.bytes + (i.pc - rip), i.size));
      j.end_obj();
    }
    j.end_arr();
    end_json();
    j.end_obj(); // guest

    j.obj("pc_table").kv("density", st.dense_pc ? "every_guest_instruction" : "fex_default_instructions_that_can_fault_or_have_side_effects")
     .kv("note", "FEX's DebugData.GuestOpcodes: guest offset from block RIP -> native offset from code start, in emission order; an entry marks the native instruction where the guest instruction's translation begins");
    j.arr("entries");
    for (auto& p : Cap.pcs) {
      j.elem_obj().kv("guest_offset", p.first).kv("native_offset", p.second);
      int cf = CfInverted(p.first, p.second);
      if (cf < 0) j.null("cf_inverted"); else j.kv("cf_inverted", cf != 0);
      j.end_obj();
    }
    j.end_arr().end_obj();
    j.arr("subblocks");
    for (auto& s : Cap.subblocks) j.elem_obj().kv("native_offset", s.first).kv("size", s.second).end_obj();
    j.end_arr();

    // Access sites: every native instruction FEX emitted for a guest memory access, recorded at emission.
    j.arr("sites");
    for (auto& s : Cap.sites) {
      j.elem_obj().kv("native_offset", s.native_offset).kv("guest_offset", s.guest_offset);
      if (s.guest_offset2 != FEXC_NONE32) j.kv("guest_offset2", s.guest_offset2); else j.null("guest_offset2");
      if (const uint32_t m = MmioRecord(s, rip)) j.kv("mmio", m);   // M3
      j.kv("insn_native_offset", s.insn_native_offset).kv("ir_op", s.ir_op).kv("kind", KindName[s.kind]).kv("width", int(s.width))
       .kv("intent", s.intent ? "write" : "read").kv("rmw", RmwName[s.rmw]).kv("segment", s.segment ? "ss" : "ds")
       .kv("base_reg", int(s.base_reg));
      if (s.index_reg != 0xff) j.kv("index_reg", int(s.index_reg)); else j.null("index_reg");
      j.kv("extend", ExtendName[s.extend]).kv("shift", int(s.shift)).kv("imm", s.imm);
      j.arr("data_regs"); j.sep(); j.s += std::to_string(s.data_reg[0]);
      if (s.kind == FEXC_SITE_LOAD_PAIR || s.kind == FEXC_SITE_STORE_PAIR || s.kind == FEXC_SITE_PUSH_PAIR || s.kind == FEXC_SITE_POP_PAIR ||
          s.kind == FEXC_SITE_SWAP || s.kind == FEXC_SITE_ATOMIC) { j.sep(); j.s += std::to_string(s.data_reg[1]); }
      j.end_arr();
      j.kv("data_class", s.data_class ? "fpr" : "gpr").kv("writeback", WritebackName[s.writeback]).kv("writeback_delta", int(s.writeback_delta))
       .kv("ordering", OrderingName[s.ordering]).kv("nzcv", s.nzcv ? "saved" : "pstate").kv("cf_inverted", bool(s.cf_inverted))
       .kv("flags_written_before_site", bool(s.flags_written_before))
       .kv("completed_effects", s.completed_effects ? "unverified" : "none").kv("located", bool(s.located));
      if (slice1) {
        if (s.access_native_offset != FEXC_NONE32) j.kv("access_native_offset", s.access_native_offset); else j.null("access_native_offset");
      }
      const Capture::Inst* in = nullptr;
      for (auto& i : Cap.insts) if (i.pc - rip == s.guest_offset) { if (i.mem.present) in = &i; break; }
      if (in && (s.kind == FEXC_SITE_LOAD || s.kind == FEXC_SITE_STORE || s.kind == FEXC_SITE_SWAP || s.kind == FEXC_SITE_ATOMIC)) {
        const auto* m = &in->mem;
        j.obj("x86").kv("rip_relative", m->rip_relative).kv("base", m->base).kv("index", m->index).kv("scale", m->scale).kv("disp", m->disp);
        if (in->seg) j.kv("segment_override", in->seg == 4 ? "fs" : "gs"); else j.null("segment_override");
        j.end_obj();
      } else j.null("x86");
      j.end_obj();
    }
    j.end_arr();

    j.obj("recovery");
    j.kv("per_access", "present: sites[] (native offset, guest instruction, kind, width, intent, address registers and offset form, segment, writeback, data registers)");
    if (slice1) {
      j.kv("flag_location_at_site", "nzcv=pstate (PSTATE) or nzcv=saved (in register state_abi.state_offsets.natemu_pre_nzcv_register, saved at the instruction start), with cf_inverted at the guest instruction start");
      j.kv("guest_state_at_fault", "present (precision patches 2-5): at each site with completed_effects=none, static registers, PF/AF and NZCV (per nzcv) hold the state before the site's guest instruction; checked on the code by verify_precision.py. Guard sites (brk stubs): the check of every indirect branch target (base_reg = target register, #GP(0)); no address guard (E3: a non-canonical access faults in hardware at its site); split sites: a store wider than 1 byte (read-modify-write included) crossing a 4 KiB page (the runtime completes it all-or-nothing and resumes after access_native_offset). Same state at a stub as at its access; checked by verify_precision.py");
      j.kv("committed_effects", "completed_effects=none per site (unverified if the recorder saw an earlier store site or an unsaved NZCV write in the instruction)");
    } else {
      j.kv("flag_location_at_site", "nzcv=pstate with cf_inverted at the guest instruction start, plus flags_written_before_site; the saved copy (nzcv=saved) is absent until the precision patches");
      j.kv("guest_state_at_fault", "absent: not guaranteed (no precision patches yet: flag elimination, static-register hoisting, push/pop fusion and the address guard are unmodified)");
      j.kv("committed_effects", "unverified: completed_effects=unverified on every site");
    }
    j.kv("pc_table", st.dense_pc ? "present_dense" : "present_sparse");
    j.kv("static_register_map", "present").kv("flag_representation", "NZCV in PSTATE with carry inverted per cf_inverted; PF/AF in x26/x27 (see state_abi)");
    j.kv("spill_bytes", "present: code.layout.spill_bytes");
    j.kv("note", slice1 ? "Sites are recorded from the final IR after register allocation, so each is exact for the emitted code. Profile slice1 adds the precision patches, block cutting, single-gate exits, branch-target and split stubs."
                         : "Sites are recorded from the final IR after register allocation, so each is exact for the emitted code. What is still absent is the guarantee that guest state at a site equals the state before its guest instruction (slice-1 chunk S1b), the address guard and split-store check, and block cutting/exits (S1c).");
    j.end_obj();
    j.arr("diagnostics");
    for (auto& d : Diagnostics) { j.sep(); j.s += '"' + Json::esc(d) + '"'; }
    j.end_arr();
  }
  j.end_obj();

  std::string o = j.s + "\n";
  if (rep) rep->json = o; else if (out.empty()) fwrite(o.data(), 1, o.size(), stdout); else std::ofstream(out) << o;

  // Binary object (crates/abi/src/code_object.rs): header, code with zeroed relocation slots, then the tables.
  // Only for a successful compile (status ok): nothing is written for a block without translated instructions.
  if ((rep || !object.empty()) && layout_ok && !Cap.prologues.empty() && Cap.info.TotalInstructions) {
    std::vector<uint8_t> f(128, 0);
    auto put = [&](size_t at, uint64_t v, int n) { for (int i = 0; i < n; ++i) f[at + i] = uint8_t(v >> (8 * i)); };
    auto pad = [&](size_t a) { while (f.size() % a) f.push_back(0); };
    auto push = [&](uint64_t v, int n) { for (int i = 0; i < n; ++i) f.push_back(uint8_t(v >> (8 * i))); };
    memcpy(f.data(), "NFXOBJ\0\x01", 8);
    put(8, 4, 4); put(12, 128, 4); put(20, bitness, 4); put(24, rip, 8);
    put(32, Cap.info.TotalLength, 4); put(36, Cap.info.TotalInstructions, 4);
    put(40, f.size(), 4); put(44, code.size(), 4);
    put(48, Cap.prologues[0].second, 4); put(52, marker == UINT64_MAX ? 0xFFFFFFFFull : marker, 4);
    put(56, tail_off, 4); put(60, Cap.spill_bytes, 4);
    f.insert(f.end(), code.begin(), code.end());
    pad(8);
    put(64, f.size(), 4); put(68, Cap.sites.size(), 4);
    for (auto& s : Cap.sites) {
      const bool stub = s.kind == FEXC_SITE_GUARD || s.kind == FEXC_SITE_SPLIT || s.kind == FEXC_SITE_ALIGN;   // a stub's guest_offset2 is its access site
      push(s.native_offset, 4); push(s.guest_offset, 4); push(stub ? s.access_native_offset : MmioRecord(s, rip) ? MmioRecord(s, rip) : s.guest_offset2, 4); push(s.insn_native_offset, 4); push(uint32_t(s.imm), 4);
      for (uint8_t b : {s.kind, s.width, s.intent, s.rmw, s.segment, s.base_reg, s.index_reg, s.extend, s.shift, s.writeback,
                        uint8_t(s.writeback_delta), s.data_reg[0], s.data_reg[1], s.data_class, s.nzcv, s.cf_inverted,
                        s.flags_written_before, s.ordering, s.completed_effects, s.located}) f.push_back(b);
    }
    pad(8);
    put(72, f.size(), 4); put(76, Cap.pcs.size(), 4);
    for (auto& p : Cap.pcs) {
      int cf = CfInverted(p.first, p.second);
      push(p.second, 4); push(p.first, 4); push(cf < 0 ? 2 : cf, 1); push(0, 3);
    }
    pad(8);
    put(80, f.size(), 4); put(84, Cap.relocs.size() + Cap.dispatch_exits.size(), 4);
    for (auto& r : Cap.relocs) {
      using T = CPU::RelocationTypes;
      uint8_t kind = 255, reg = 0, size = 0; uint64_t value = 0;
      switch (r.Header.Type) {
      case T::RELOC_NAMED_SYMBOL_LITERAL: kind = 2; size = 8; break;
      case T::RELOC_GUEST_RIP_LITERAL: kind = 1; size = 8; value = r.GuestRIP.GuestRIP; break;
      case T::RELOC_GUEST_RIP_MOVE: kind = 3; reg = r.GuestRIP.RegisterIndex; value = r.GuestRIP.GuestRIP; break;
      default: break;
      }
      push(r.Header.Offset, 4); push(kind, 1); push(reg, 1); push(size, 1); push(0, 1); push(value, 8);
    }
    for (auto o : Cap.dispatch_exits) { push(o, 4); push(4, 1); push(0, 1); push(0, 1); push(0, 1); push(0, 8); }   // dispatch_branch
    for (size_t i = 0; i < Cap.sra_gpr.size() && i < 20; ++i) f[88 + i] = Cap.sra_gpr[i];
    f[108] = uint8_t(Cap.sra_gpr.size());
    // Profile, end reason, settings and the feature profile (same values as the JSON `settings`, `guest.end` and `features`).
    f[109] = slice1 ? 1 : 0;
    f[110] = EndReasonCode(end_reason_str);
    f[111] = uint8_t((st.tso ? 1 : 0) | (st.multiblock ? 2 : 0) | (st.dense_pc ? 4 : 0) | (st.end_marker ? 8 : 0));
    uint64_t fbits = 0;   // bit i = FeatureTable() entry i = the i-th member of JSON features.host_features
    for (size_t i = 0; i < table.size() && i < 64; ++i) if (table[i].get(hf)) fbits |= 1ull << i;
    put(112, fbits, 8); f[120] = uint8_t(table.size()); f[121] = uint8_t(st.dcache_line_log2);
    put(124, slice1 ? 25u : 0xFFFFFFFFu, 4);   // = JSON natemu_pre_nzcv_register
    put(16, f.size(), 4);
    if (rep) rep->object = f; else std::ofstream(object, std::ios::binary).write(reinterpret_cast<const char*>(f.data()), std::streamsize(f.size()));
  }
  return Cap.info.TotalInstructions ? 0 : 1;
}

// ---------------------------------------------------------------- --serve
// Framing, all integers little-endian u32. Request: len, then payload = args_len, args (NUL-terminated strings, the CLI's
// arguments without --hex/--file/--out/--object), then the guest bytes. Reply: len, then payload = code (i32, the CLI's exit
// code: 0 object, 1 no code (json: below), >=2 error (json = message)), json_len, object_len, extent, args_read, json, object.
// extent: the compile read or checked only guest bytes [0, extent) and, if extent < the bytes' length, would be the same for
// any longer input with that prefix (saturating). args_read: bit i set if the compile read the gating argument FEXC_ARG_* i;
// it is the same under any value of the others. Both are 0 for an error. json: with --report, the CLI's report less its hex
// dumps; without (the host's server), only what the host reads: {"status":"ok"} for an object, the status and guest.end for
// no code (with the CLI's status, error and diagnostics when FEX emitted nothing). The object, extent and args_read are the same.
// EOF on stdin ends the server. One request at a time, in order.
static bool ReadAll(int fd, void* p, size_t n) {
  for (auto* c = static_cast<char*>(p); n;) { ssize_t r = read(fd, c, n); if (r <= 0) return false; c += r; n -= size_t(r); }
  return true;
}
static void WriteAll(int fd, const void* p, size_t n) {
  for (auto* c = static_cast<const char*>(p); n;) { ssize_t r = write(fd, c, n); if (r <= 0) _exit(3); c += r; n -= size_t(r); }
}
static void SendReply(const Reply& r) {
  std::vector<uint8_t> f(24);
  auto put = [&](size_t at, uint32_t v) { for (int i = 0; i < 4; ++i) f[at + i] = uint8_t(v >> (8 * i)); };
  put(4, uint32_t(r.code)); put(8, uint32_t(r.json.size())); put(12, uint32_t(r.object.size()));
  if (r.code < 2) { put(16, r.extent); put(20, r.args_read); }
  f.insert(f.end(), r.json.begin(), r.json.end());
  f.insert(f.end(), r.object.begin(), r.object.end());
  put(0, uint32_t(f.size() - 4));
  WriteAll(1, f.data(), f.size());
}
static void Compile(const std::vector<std::string>& args, const std::vector<uint8_t>& bytes, Reply& r) {
  r.code = CompileRequest(args, &bytes, &r);
  r.extent = uint32_t(std::min<uint64_t>(Extent, UINT32_MAX));
  r.args_read = fexc_args_read;
}
// mode "reuse" (the default): every request in this process, in the kept context of its configuration (CompileRequest);
// "fork" (debugging): each request in a forked child of a server that never compiles, so nothing at all crosses requests and
// a crash costs one request (about 0.5 ms more per request).
static int Serve(const std::string& mode) {
  for (;;) {
    uint32_t len;
    if (!ReadAll(0, &len, 4)) return 0;
    std::vector<uint8_t> req(len);
    if (!ReadAll(0, req.data(), len) || len < 4) return 3;
    uint32_t alen = req[0] | req[1] << 8 | req[2] << 16 | uint32_t(req[3]) << 24;
    if (alen > len - 4 || (alen && req[3 + alen]) ) return 3;   // the arguments end with a NUL
    std::vector<std::string> args {"fexc"};
    for (size_t i = 4; i < 4 + alen; i += args.back().size() + 1) args.push_back(reinterpret_cast<const char*>(&req[i]));
    std::vector<uint8_t> bytes(req.begin() + 4 + alen, req.end());
    Reply r;
    if (mode == "fork") {
      pid_t pid = fork();
      if (pid == 0) { Compile(args, bytes, r); SendReply(r); _exit(0); }
      int status = 0;
      waitpid(pid, &status, 0);
      if (!WIFEXITED(status) || WEXITSTATUS(status)) { r = {}; r.code = 4; r.json = "fexc child died, status " + std::to_string(status); SendReply(r); }
    } else { Compile(args, bytes, r); SendReply(r); }
  }
}

int main(int argc, char** argv) {
  if (argc >= 2 && std::string(argv[1]) == "--serve") {   // fexc --serve [--serve-mode reuse|fork] [--report]
    std::string mode = "reuse";
    for (int i = 2; i < argc; ++i) {
      const std::string a = argv[i];
      if (a == "--serve-mode" && i + 1 < argc) mode = argv[++i];
      else if (a == "--report") ServeReport = true;
      else return Fail("--serve [--serve-mode reuse|fork] [--report]");
    }
    if (mode != "fork" && mode != "reuse") return Fail("--serve-mode reuse|fork");
    return Serve(mode);   // stdin EOF; contexts are never torn down (see Ctx), so leave without destructors
  }
  int code = CompileRequest(std::vector<std::string>(argv, argv + argc), nullptr, nullptr);
  // Do not tear the context down: FEX's destructors assume a running emulator thread.
  fflush(nullptr);
  _exit(code);
}
