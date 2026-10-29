// natemu export hook: the only interface between the patched FEXCore and the fexc driver.
#pragma once
#include <cstddef>
#include <cstdint>
#include <map>
#include <set>
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
  FEXC_SITE_GUARD = 6, FEXC_SITE_SPLIT = 7, // slice1 stubs (branch-target check; series4-12 also the address guard, split-store check)
  FEXC_SITE_PUSH_PAIR = 8, FEXC_SITE_POP_PAIR = 9, // FEX fused two guest push/pop into one stp/ldp
  FEXC_SITE_OTHER = 10,                     // guest-memory IR op the site record cannot describe
  FEXC_SITE_DIVIDE = 11,                    // slice1 stub: div/idiv #DE (zero divisor or quotient overflow)
  FEXC_SITE_SWAP = 12,                      // memory xchg: swpal (data_reg[0] = Rt, the old value; data_reg[1] = Rs, the new)
  FEXC_SITE_ATOMIC = 13,                    // slice1 (I3): a LOCK read-modify-write, one ld{add,clr,eor,set}al / casal / caspal
                                            // (data_reg[0] = Rt, data_reg[1] = Rs; caspal: the even registers of both pairs)
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
  uint32_t access_native_offset; // guard/split: native offset of the access site the stub covers (FEXC_NONE32 for a
                                 // branch-target guard, whose base_reg is the target register); FEXC_NONE32 otherwise.
                                 // The binary record carries it in guest_offset2 (pairs never occur with stubs)
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
// Profile slice1: the guest bytes [Address, Address + Size) the compile was given, or nullptr (set by fexc). The allowlist
// reads an instruction's prefix bytes with it (FEX's decode keeps only the last segment prefix).
inline const uint8_t* (*fexc_guest_bytes)(uint64_t Address, uint64_t Size) = nullptr;
// Profile slice1: `svc` immediates of the resident entries generated code makes (crates/abi/src/execution.rs `svc`):
// a literal exit (RIP in x0), the dispatcher's L1 miss (resident code, listed for completeness) and the 128/64
// unsigned / signed division (FEX's helper registers: x0 high, x1 low, x2 divisor in; quotient, remainder, #DE out).
enum FexcSvc : uint16_t { FEXC_SVC_LINK = 1, FEXC_SVC_MISS = 2, FEXC_SVC_DIV_U = 3, FEXC_SVC_DIV_S = 4 };
// Profile slice1: native offsets of the computed exits' `b` to the resident dispatcher (placeholder `b .`, relocated by
// the resident when it places the block), per compile attempt.
inline std::vector<uint32_t> fexc_dispatch_exits;
// Profile slice1: guest offsets of the instructions whose incoming NZCV the JIT saves in FexcPrecision::PreNzcvReg at
// their start (an NZCV write precedes one of their fault points), chosen by the flag-elimination pass.
inline std::set<uint64_t> fexc_pre_nzcv;
// Which opcode map produced a decoded instruction (DecodedInst::FexcMap, set by the patched decoder).
enum FexcMap : uint8_t { FEXC_MAP_PRIMARY = 0, FEXC_MAP_TWO = 1, FEXC_MAP_TWO_F3 = 2, FEXC_MAP_TWO_F2 = 3, FEXC_MAP_TWO_66 = 4,
                         FEXC_MAP_0F38 = 5, FEXC_MAP_0F3A = 6, FEXC_MAP_3DNOW = 7, FEXC_MAP_VEX = 8 };
// FEXC_CUT_DECODER: the FIRST instruction hit a decoder end-of-input status (partial_decode, not_executable, bad_relocation):
// no code, never FEX's NoExec/SIGSEGV block. `status` is the Frontend::Decoder::DecodedBlockStatus value.
// FEXC_CUT_STRING (I2, I3): the instruction after one that exits to its own RIP (a REP string instruction while RCX is not 0,
// a LOCK NEG whose compare-and-swap lost a race), which ends its block like a branch: end reason block_ending_instruction,
// so a block has at most one such instruction and its link points.
enum FexcCutReason : uint8_t { FEXC_CUT_NONE = 0, FEXC_CUT_SYSTEM = 1, FEXC_CUT_UNSUPPORTED = 2, FEXC_CUT_DECODER = 3, FEXC_CUT_STRING = 4 };
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
inline uint64_t fexc_after_rep = 0;   // slice1: the RIP after the block's self-exiting instruction (REP string, LOCK NEG; 0: none yet)
// Profile slice1, filled by FexcPrecision::Prescan before register allocation (post-RA the JIT sees physical registers,
// not the SSA nodes behind them); keys are IR node IDs, which RA does not change.
inline std::map<uint32_t, int64_t> fexc_rip_relative;   // guest-memory op -> its base EntrypointOffset (offset from the block RIP)
struct FexcTarget { uint32_t def; int sra; };            // the target's node, or (sra >= 0) the static register it reads
inline std::map<uint32_t, FexcTarget> fexc_target_check; // node after which to check an indirect exit's target
// Profile slice1, recorded by the dispatcher's DIVOp/IDIVOp: guest RIP of each div/idiv -> its x86 operand width in bytes
// (the #DE check needs it; FEX's IR widens the 8-bit forms).
inline std::map<uint64_t, uint8_t> fexc_div_width;
// Profile slice1 (I2), recorded by the dispatcher's string instructions: guest RIP of each MOVS, whose load and store are two
// memory operands (DS:RSI read, ES:RDI written), not a read-modify-write of one.
inline std::set<uint64_t> fexc_movs;
// Profile slice1 (I3), recorded by the dispatcher: guest RIP of each LOCK CMPXCHG / CMPXCHG8B / CMPXCHG16B, whose casal/caspal
// the JIT precedes with `dmb ish` (x86 orders a failing locked compare; a failing Arm CAS writes nothing, acquire only).
inline std::set<uint64_t> fexc_lock_cas;
