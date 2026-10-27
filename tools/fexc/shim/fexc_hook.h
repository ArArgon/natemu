// natemu export hook: the only interface between the patched FEXCore and the fexc driver.
#pragma once
#include <cstddef>
#include <cstdint>
#include <map>
#include <string_view>
#include <vector>

namespace FEXCore::Core {
struct InternalThreadState;
struct DebugData;
} // namespace FEXCore::Core

struct FexcCompileInfo {
  uint64_t GuestRIP;          // entry RIP of the compiled unit
  uint64_t DecodedStart;      // lowest / highest guest address FEX decoded (end - start = length)
  uint64_t DecodedLength;
  uint64_t TotalInstructions; // guest instructions translated (not just decoded)
  uint64_t TotalLength;       // bytes of those instructions
};

// Called after CPUBackend::CompileCode with the finished block (still in FEX's code buffer).
inline void (*fexc_compile_hook)(FEXCore::Core::InternalThreadState*, const FexcCompileInfo&, const uint8_t* Code, size_t Size,
                                 const FEXCore::Core::DebugData&) = nullptr;
// Make FEX emit a guest-PC entry for every guest instruction (it normally records only those that can fault).
inline bool fexc_dense_pc_table = false;

// ---------------------------------------------------------------------------------------------
// Access sites (natemu-fexc-v2). Recorded by Arm64JITCore::FexcAfterOp (shim/fexc_sites.inc) right
// after each IR op is emitted, so the guest instruction identity is the GuestOpcode op in force and
// the IR op supplies kind, width and operands. The numeric codes are shared with the binary record
// in crates/abi/src/code_object.rs; keep them in sync.
// ---------------------------------------------------------------------------------------------
enum FexcSiteKind : uint8_t {
  FEXC_SITE_LOAD = 0, FEXC_SITE_STORE = 1, FEXC_SITE_LOAD_PAIR = 2, FEXC_SITE_STORE_PAIR = 3,
  FEXC_SITE_PUSH = 4, FEXC_SITE_POP = 5,
  FEXC_SITE_GUARD = 6, FEXC_SITE_SPLIT = 7, // reserved for the precision patches (S1b); never produced here
  FEXC_SITE_PUSH_PAIR = 8, FEXC_SITE_POP_PAIR = 9, // FEX fused two guest push/pop into one stp/ldp
  FEXC_SITE_OTHER = 10,                     // guest-memory IR op the site record cannot describe
};
inline constexpr uint32_t FEXC_NONE32 = 0xFFFFFFFFu;
struct FexcSiteInfo {
  uint32_t native_offset;        // the faulting native instruction, from the start of the FEX block
  uint32_t guest_offset;         // guest instruction (offset from the block RIP) the access belongs to
  uint32_t guest_offset2;        // push_pair/pop_pair: the second guest instruction, else FEXC_NONE32
  uint32_t insn_native_offset;   // native offset where that guest instruction's translation starts (PC table entry)
  int32_t imm;                   // constant address offset (0 with an index register)
  uint8_t kind;                  // FexcSiteKind
  uint8_t width;                 // bytes accessed by the native instruction (pairs: both registers)
  uint8_t intent;                // 0 read, 1 write
  uint8_t rmw;                   // 0 none, 1 load half of a read-modify-write, 2 store half
  uint8_t segment;               // 0 ds, 1 ss (filled by fexc from FEX's decode)
  uint8_t base_reg;              // native register number of the base (x31 = sp)
  uint8_t index_reg;             // 0xff = none
  uint8_t extend;                // index extend: 0 none, 1 uxtw, 2 sxtw, 3 sxtx
  uint8_t shift;                 // index shift
  uint8_t writeback;             // 0 none, 1 pre-index, 2 post-index (base register updated by the access)
  int8_t writeback_delta;
  uint8_t data_reg[2];           // transfer register(s), 31 = xzr; second only for pairs
  uint8_t data_class;            // 0 gpr, 1 fpr
  uint8_t nzcv;                  // 0 = guest NZCV in PSTATE at the site, 1 = saved in the frame (precision patch only)
  uint8_t cf_inverted;           // guest CF is inverted in PSTATE at the instruction start
  uint8_t flags_written_before;  // an NZCV-writing IR op of this guest instruction precedes the site
  uint8_t ordering;              // 0 plain, 1 acquire (ldar/ldapr family), 2 release (stlr family)
  uint8_t located;               // 1 if the faulting word was found in the op's emitted range
  uint8_t completed_effects;     // 0 none (slice1: state at the site = state before the instruction), 1 unverified
  const char* ir_op;             // IR op name (diagnostic, not exported to the binary record)
};
struct FexcInsnInfo { uint32_t guest_offset; uint32_t native_offset; bool cf_inverted; };
inline std::vector<FexcSiteInfo> fexc_sites;
inline std::vector<FexcInsnInfo> fexc_insns;
inline std::map<uint64_t, bool> fexc_cf_inverted; // guest offset -> CF inversion at that instruction's start (dispatcher state)
inline uint32_t fexc_spill_bytes = 0;             // bytes the entry prologue subtracts from sp (ResetStack restores them)

// ---------------------------------------------------------------------------------------------
// Profile slice1 (slice-1 contract §2): block cutting before instructions outside the allowlist, and the
// single-gate exit (FEX's BLOCK_DEBUGGING branch of ExitFunction, enabled by this runtime flag instead of the
// global constant, which would also force one instruction per block).
// ---------------------------------------------------------------------------------------------
inline bool fexc_slice1 = false;
// Which opcode map produced a decoded instruction (DecodedInst::FexcMap, set by the patched decoder).
enum FexcMap : uint8_t { FEXC_MAP_PRIMARY = 0, FEXC_MAP_TWO = 1, FEXC_MAP_TWO_F3 = 2, FEXC_MAP_TWO_F2 = 3, FEXC_MAP_TWO_66 = 4,
                         FEXC_MAP_0F38 = 5, FEXC_MAP_0F3A = 6, FEXC_MAP_3DNOW = 7 };
// FEXC_CUT_DECODER: the FIRST instruction hit a decoder end-of-input status (partial_decode, not_executable, bad_relocation):
// no code, never FEX's NoExec/SIGSEGV block. `status` is the Frontend::Decoder::DecodedBlockStatus value.
enum FexcCutReason : uint8_t { FEXC_CUT_NONE = 0, FEXC_CUT_SYSTEM = 1, FEXC_CUT_UNSUPPORTED = 2, FEXC_CUT_DECODER = 3 };
struct FexcCut {
  uint8_t reason = FEXC_CUT_NONE; // set when the block ended before an instruction outside the allowlist
  uint64_t rip = 0;               // that instruction
  uint8_t map = 0, opcode = 0, modrm = 0;
  bool has_modrm = false;
  const char* name = "";          // FEX's table name for the instruction ("?" when it did not decode)
  const char* why = "";
  uint8_t status = 0;
};
inline FexcCut fexc_cut {};
