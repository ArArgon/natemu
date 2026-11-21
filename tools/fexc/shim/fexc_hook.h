// natemu export hook: the only interface between the patched FEXCore and the fexc driver.
#pragma once
#include <cstddef>
#include <cstdint>
#include <map>
#include <memory_resource>
#include <set>
#include <string_view>
#include <vector>

namespace FEXCore::Core {
struct InternalThreadState;
struct DebugData;
} // namespace FEXCore::Core
namespace FEXCore::IR {
class IRListView;
} // namespace FEXCore::IR

struct FexcCompileInfo {
  uint64_t GuestRIP;          // entry RIP of the compiled unit
  uint64_t DecodedStart;      // lowest / highest guest address FEX decoded (end - start = length)
  uint64_t DecodedLength;
  uint64_t TotalInstructions; // guest instructions translated (not just decoded)
  uint64_t TotalLength;       // bytes of those instructions
};

// The per-request tables below (every one ResetGlobals clears, fexc.cpp) take their nodes from one arena that ResetGlobals releases
// after clearing them all: a request's inserts are pointer bumps and its reset frees nothing node by node (allocation was a fifth
// of a compile). Only tables cleared there may use it; a table kept across requests would point into released memory.
alignas(64) inline std::byte fexc_request_buffer[256 << 10];
inline std::pmr::monotonic_buffer_resource fexc_request_arena {fexc_request_buffer, sizeof fexc_request_buffer, std::pmr::new_delete_resource()};
template<class K> struct FexcSetT : std::pmr::set<K> { FexcSetT() : std::pmr::set<K>(&fexc_request_arena) {} };
template<class K, class V> struct FexcMapT : std::pmr::map<K, V> { FexcMapT() : std::pmr::map<K, V>(&fexc_request_arena) {} };
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
  FEXC_SITE_ALIGN = 14,                     // slice1 stub (I5a): a legacy SSE 16-byte memory operand not 16-byte aligned (#GP(0))
  FEXC_SITE_SIMD = 15,                      // slice1 stub (I5c): an SSE/AVX instruction raised an unmasked SIMD exception (#XM/#UD)
  FEXC_SITE_X87 = 16,                       // slice1 stub (I5c): an x87 instruction raised an unmasked exception that leaves its
                                            // operands unaltered (width: the instruction's length; w0: its check word)
  FEXC_SITE_PENDING = 17,                   // slice1 stub (I5c): a waiting x87, WAIT or MMX instruction with an x87 exception pending (#MF)
  FEXC_SITE_FMA = 18,                       // slice1 (AFP accurate plan, chunk 4): the NaN check of a fused multiply-add, in the body (FexcFmaCheck)
  FEXC_SITE_DENORM = 19,                    // slice1 stub (in-block DE screen skip): the resident's FP trap handler left a denormal result since the
                                            // last check; exit to the dispatcher at this instruction (no access, no address, the state before it)
  FEXC_SITE_STACK = 20,                     // slice1 stub (--push-run): the guest RSP is not 8-byte aligned (32-bit code: ESP not 4-byte aligned) at a store it would cover; the
                                            // resident steps the instruction alone (its step unit has the split check)
  FEXC_SITE_X87TAG = 22,                    // slice1 stub (M2 task 4): an x87 instruction's stack check (FexcX87Tag), at its start: a register
                                            // it reads is empty or the one it pushes onto is not; the resident steps the instruction alone
                                            // (its step unit has the masked response)
  FEXC_SITE_X87GUESS = 23,                  // slice1 stub (M2 task 4): the unit was compiled for another x87 stack top or tag byte than the
                                            // guest state holds (FexcX87Guess, at an IR block's first x87 instruction): the resident retires
                                            // the unit and goes on at this instruction
  FEXC_SITE_FLDCW = 21,                     // slice1 stub (M2 task 4): an inline FLDCW's control word has other exception masks than the one
                                            // in the guest state: the resident runs the instruction (it steps it alone; in a step unit FLDCW is cut)
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
inline FexcMapT<uint64_t, bool> fexc_cf_inverted; // guest offset -> CF inversion at that instruction's start (dispatcher state)
inline uint32_t fexc_spill_bytes = 0;             // bytes the entry prologue subtracts from sp (ResetStack restores them)

// ---------------------------------------------------------------------------------------------
// Profile slice1 (slice-1 contract §2): block cutting before instructions outside the allowlist, and the
// single-gate exit (FEX's BLOCK_DEBUGGING branch of ExitFunction, enabled by this runtime flag instead of the
// global constant, which would also force one instruction per block).
// ---------------------------------------------------------------------------------------------
inline bool fexc_slice1 = false;
// M2: the request is for 32-bit code (--bitness 32; its own FEX context). Not a FexcArg: the host's code identity holds the mode.
inline bool fexc_bits32 = false;
// M2 task 3a, 32-bit code: FS (bit 0), GS (bit 1), DS (bit 2), ES (bit 3) hold a null selector (--fs-null, --gs-null, --ds-null,
// --es-null, from the host's code identity):
// an access through that segment is cut as a system instruction, which the resident raises as #GP(0) (SDM Vol.3 3.4.4). Not
// FexcArgs: the host keeps no answer of a 32-bit request.
inline uint8_t fexc_null_segments = 0;
// Extended blocks (--ext-conds N): after a conditional branch (jcc, loop, jrcxz) the decoder goes on with the
// fall-through instructions in the same unit while the unit has fewer than N conditional branches and the fall-through
// starts in the unit's first page; the taken edge stays an ordinary exit. 0: off (FEX's single basic block).
// fexc_ext_stop: set by GenerateIR to the last decoded block a retry may keep (a cut or early end inside a non-final one).
inline int fexc_ext_conds = 0;
inline int fexc_ext_stop = -1;
// Asked by GenerateIR with the dispatcher's IR, before the passes (fexc.cpp ExtAbandon): true when the extended-block loop would
// compile the unit again anyway; GenerateIR then returns no IR and sets fexc_ext_abandoned (the caller resets it).
inline bool (*fexc_ext_abandon)(FEXCore::IR::IRListView&) = nullptr;
inline bool fexc_ext_abandoned = false;
// Request arguments (the host always sends them; fexc's own default is off): --push-run drops the split check of 2/4/8-byte stores at an 8-byte aligned base +
// 8k, the guest RSP's alignment checked once (shim/fexc_sites.inc FexcBeforeOp, FexcAlignedAfter; in 32-bit code the slot is 4 bytes: 2/4-byte stores at a 4-byte aligned base); --narrow-cmp compares 8/16-bit
// operands with a constant without materialising it (FEX Flags.cpp CalculateFlags_SUB, JIT ALUOps.cpp SubNZCV). fexc_step_unit:
// the request is a step (--max-insts 1), whose split checks stay (the resident steps an instruction whose alignment check fails).
inline bool fexc_narrow_cmp = false, fexc_step_unit = false;
// Profile slice1: the guest bytes [Address, Address + Size) the compile was given, or nullptr (set by fexc). The allowlist
// reads an instruction's prefix bytes with it (FEX's decode keeps only the last segment prefix).
inline const uint8_t* (*fexc_guest_bytes)(uint64_t Address, uint64_t Size) = nullptr;
// Profile slice1: `svc` immediates of the resident entries generated code makes (crates/abi/src/execution.rs `svc`):
// a literal exit (RIP in x0), the dispatcher's L1 miss (resident code, listed for completeness) and the 128/64
// unsigned / signed division (FEX's helper registers: x0 high, x1 low, x2 divisor in; quotient, remainder, #DE out).
enum FexcSvc : uint16_t { FEXC_SVC_LINK = 1, FEXC_SVC_MISS = 2, FEXC_SVC_DIV_U = 3, FEXC_SVC_DIV_S = 4, FEXC_SVC_X87 = 5, FEXC_SVC_STRING = 6 };
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
  bool gated = false;             // I5a: an SSE instruction cut because the vCPU's CR0/CR4 gate it (#UD or #NM; --sse-gated)
  bool gated_mmx = false;         // I9: an MMX instruction cut by CR0.EM/TS (#UD or #NM; --x87-gated): reported as cut_gated "mmx"
  bool gated_null = false;        // M2 task 3a: an access through a null segment in 32-bit code (#GP): cut_gated "null_segment"
  bool ud = false;                // I10: an encoding the presented CPU raises #UD for (reserved or unadvertised): reported as cut_ud
};
inline FexcCut fexc_cut {};
inline uint64_t fexc_after_rep = 0;   // slice1: the RIP after the block's self-exiting instruction (REP string, LOCK NEG; 0: none yet)
// Perf P2: the inline instructions with an exit to their own RIP translated so far in the block (bit 0 an STI, bit 1 a VERW):
// a block has at most four literal exits (abi MAX_LINK_POINTS), its end and a branch target among them, so a VERW is
// translated only in a block with no other such instruction, and no STI after it.
inline uint8_t fexc_self_exits = 0;
// Profile slice1, filled by FexcPrecision::Prescan before register allocation (post-RA the JIT sees physical registers,
// not the SSA nodes behind them); keys are IR node IDs, which RA does not change.
inline FexcMapT<uint32_t, int64_t> fexc_rip_relative;   // guest-memory op -> its base EntrypointOffset (offset from the block RIP)
struct FexcTarget { uint32_t def; int sra; };            // the target's node, or (sra >= 0) the static register it reads
inline FexcMapT<uint32_t, FexcTarget> fexc_target_check; // node after which to check an indirect exit's target
// Profile slice1, recorded by the dispatcher's DIVOp/IDIVOp: guest RIP of each div/idiv -> its x86 operand width in bytes
// (the #DE check needs it; FEX's IR widens the 8-bit forms).
inline FexcMapT<uint64_t, uint8_t> fexc_div_width;
// Profile slice1 (I2), recorded by the dispatcher's string instructions: guest RIP of each MOVS, whose load and store are two
// memory operands (DS:RSI read, ES:RDI written), not a read-modify-write of one.
inline FexcSetT<uint64_t> fexc_movs;
// The request arguments that gate forms (`--sse-gated` ... `--x87-fpcr`): reading one through its conversion sets its bit in
// `fexc_args_read` (bit = FexcArg's Bit), so fexc can tell its caller which of them this compile depended on (fexc never read
// the others: the compile is the same under any value of them, which lets the host's cache leave them out of its key).
inline uint32_t fexc_args_read = 0;
template<class T, int Bit>
struct FexcArg {
  T value {};
  operator T() const { fexc_args_read |= 1u << Bit; return value; }
  FexcArg& operator=(T v) { value = v; return *this; }
};
enum : int { FEXC_ARG_SSE_GATED, FEXC_ARG_AVX_GATED, FEXC_ARG_ALIGNMENT_GATED, FEXC_ARG_X87_GATED, FEXC_ARG_WAIT_GATED, FEXC_ARG_X87_FPCR,
             FEXC_ARG_SIMD_UNMASKED, FEXC_ARG_X87_UNMASKED, FEXC_ARG_CPL0, FEXC_ARG_TSC, FEXC_ARG_BULK, FEXC_ARG_DAZ, FEXC_ARG_FTZ,
             FEXC_ARG_ACCURATE, FEXC_ARG_PUSH_RUN, FEXC_ARG_X87_GUESS };
// `--push-run` (the host asks for it unless the vCPU's stack pointer keeps tripping the run check): a gating argument, so an answer
// compiled with it is not reused for a request without it.
inline FexcArg<bool, FEXC_ARG_PUSH_RUN> fexc_push_run;
// Profile slice1 (perf chunk 26): the host asks for bulk REP STOS (`--bulk`: never for a single-instruction step, nor while
// alignment checking or a data breakpoint gates the block): FexcStringOp stores the part of the string inside RDI's page
// first (FEX's `FexcRepStos`, JIT form in fexc_sites.inc).
inline FexcArg<bool, FEXC_ARG_BULK> fexc_bulk;
// Profile slice1 (perf chunk 15): the request is at CPL 0 (`--cpl0`, from the host; never for a single-instruction step):
// the one-byte CLI and STI are translated inline (patch 0031, `FexcInterruptFlagOp`; the rules are `Cut`'s).
inline FexcArg<bool, FEXC_ARG_CPL0> fexc_cpl0;
// The flag byte (FEX's flags[22], no FEX code uses it) the resident sets when an interrupt would be taken were IF set: an
// inline STI reads it (frame::EVENT_PENDING on the host).
constexpr unsigned FEXC_EVENT_PENDING_LOC = 22;
// Perf chunk 26: the flag byte (FEX's flags[23], no FEX code uses it) holding IA32_MISC_ENABLE's fast-string bit, which the
// resident writes at every entry (frame::FAST_STRINGS on the host); a bulk REP STOS stores in bulk only while it is 1.
constexpr unsigned FEXC_FAST_STRINGS_LOC = 23;
// Profile slice1 (perf chunk 15): a CPL-0 request on a host whose counter runs at 24 MHz (`--tsc`): the two-byte RDTSC is
// translated inline (patch 0032, the resident's exact formula), reading the vCPU's TSC offset at FEXC_TSC_OFFSET (FEX's
// `_pad1`, which no FEX code uses; the resident writes it at every entry: frame::TSC_OFFSET on the host).
inline FexcArg<bool, FEXC_ARG_TSC> fexc_tsc;
constexpr uint32_t FEXC_TSC_OFFSET = 0xb8;
// Profile slice1 (perf chunk 17): with `--cpl0` the plain SWAPGS (0f 01 f8) is translated inline (patch 0033), exchanging FEX's
// gs_cached with IA32_KERNEL_GS_BASE, which the resident keeps at FEXC_KERNEL_GS_BASE (FEX's `L1Pointer`, which no generated
// code uses) while a CPL-0 block runs (frame::KERNEL_GS_BASE on the host).
constexpr uint32_t FEXC_KERNEL_GS_BASE = 0xa0;
// Profile slice1 (perf chunk 26): with `--cpl0` MOV r64, CR2 (0f 20 /r, reg 2, mod 11, no prefix but a REX without R) is
// translated inline, a load of CR2 from FEXC_CR2 (FEX's `L1Mask`, which no generated code uses), which the resident writes
// at every entry while a CPL-0 block runs (frame::CR2 on the host).
constexpr uint32_t FEXC_CR2 = 0xa8;
// Profile slice1 (perf P2): with `--cpl0` VERW m16 (0f 00 /5, no prefix but a REX; never first in its block) is translated
// inline (FEX's FexcVerwOp): ZF = 1 for a writable data segment of the GDT with DPL >= RPL, read through the shadow; anything
// else exits to the VERW's RIP, which the resident runs. The resident writes the GDT base and its limit less 7 (0 when the
// inline form may not read the table) at every entry while a CPL-0 block runs, in FEX's `segment_arrays` (which no 64-bit
// code uses): frame::VERW_GDT_BASE and frame::VERW_GDT_LIMIT on the host.
constexpr uint32_t FEXC_VERW_GDT_BASE = 0x4a0;
constexpr uint32_t FEXC_VERW_GDT_LIMIT = 0x4a8;
// Profile slice1 (I3), recorded by the dispatcher: guest RIP of each LOCK CMPXCHG / CMPXCHG8B / CMPXCHG16B, whose casal/caspal
// the JIT precedes with `dmb ish` (x86 orders a failing locked compare; a failing Arm CAS writes nothing, acquire only).
inline FexcSetT<uint64_t> fexc_lock_cas;
// Profile slice1 (I5a): the vCPU runs with CR0.EM or CR0.TS set or CR4.OSFXSR clear (`--sse-gated`, from the host): every SSE
// form is cut as a system instruction (the resident raises #UD or #NM, SDM Vol.3 Table 2-2 and the SSE exception tables).
inline FexcArg<bool, FEXC_ARG_SSE_GATED> fexc_sse_gated;
// Profile slice1 (X2): the vCPU runs under x86 alignment checking (CR0.AM, RFLAGS.AC, CPL 3; `--alignment-gated`, from the
// host): BT/BTS/BTR/BTC with a memory operand are cut as system instructions. FEX accesses the one byte holding the bit, so the
// operand-size access x86 checks (#AC) never happens natively; the resident checks the operand's alignment, then steps it.
inline FexcArg<bool, FEXC_ARG_ALIGNMENT_GATED> fexc_alignment_gated;
// Profile slice1 (I5a), recorded by the allowlist: guest RIP of each legacy SSE instruction whose 16-byte memory operand must be
// 16-byte aligned (#GP(0) otherwise: SDM Vol.2 2.5, exception classes 1, 2 and 4); its first access gets an alignment check.
inline FexcSetT<uint64_t> fexc_align16;
// Profile slice1 (I8): the same for VMOVAPS/VMOVAPD/VMOVDQA/VMOVNT* ymm, whose 32-byte memory operand must be 32-byte aligned.
inline FexcSetT<uint64_t> fexc_align32;
// Profile slice1 (I8): the vCPU's controls gate the VEX AVX forms (`--avx-gated`, from the host: CR4.OSXSAVE clear, XCR0[2:1]
// not 11b, or CR0.TS): every one is cut as a system instruction (the resident raises #UD, else #NM).
inline FexcArg<bool, FEXC_ARG_AVX_GATED> fexc_avx_gated;
// Profile slice1 (I5a), recorded by the allowlist so the JIT raises the MXCSR exception flags x86 does (FPSR is MXCSR's flags):
// COMISS/COMISD compare signalling (`fcmpe`: IE for a QNaN too; FEX emits UCOMIS's quiet `fcmp` for both); RCPPS/RCPSS/
// RSQRTPS/RSQRTSS raise none (FEX divides exactly: FPSR is saved and restored around the op) and get the SDM's fixed results for
// denormal sources and RCP's tiny results (integer words around the op, shim/fexc_sites.inc FexcApproxScalar); ROUNDPS/PD/SS/SD with imm8
// bit 3 clear raise PE when inexact (`frintx`, under the instruction's rounding mode when imm8 bit 2 is clear).
inline FexcSetT<uint64_t> fexc_fp_signal, fexc_fp_quiet, fexc_round_inexact;
// Profile slice1 (M3), recorded by the allowlist: guest RIP of each MOV-family instruction -> its completion record for a
// device access (shim/fexc_mmio.inc); fexc.cpp attaches it to the instruction's one load or store site.
inline FexcMapT<uint64_t, uint32_t> fexc_mmio;
// Profile slice1 (I6): x87 in FEX's reduced-precision mode (X87REDUCEDPRECISION, set by fexc for slice1): ST(i) as doubles in
// the guest-state page (`mm[i]`, low 8 bytes), TOP/C0..C3 in FEX's flag bytes, FCW and the abridged tag word in their FEX
// fields. From the host: `--x87-gated` (CR0.EM or CR0.TS) cuts every x87 form d8..df as a system instruction (#NM),
// `--wait-gated` (CR0.TS and CR0.MP) cuts WAIT/FWAIT (9b) likewise; `--x87-fpcr V` (V nonzero) is the FPCR x87 arithmetic
// runs under (FCW.RC, no flush to zero) when it differs from MXCSR's (the resident enters blocks with MXCSR's FPCR). V's low 32 bits
// are the FPCR and bit 32 only makes V nonzero for an all-zero FPCR (AFP fallback chunk 1: without AFP, nearest rounding with no
// flush is FPCR 0, which must still switch; the host sets bit 32 then, `X87_FPCR_EXPLICIT`).
inline FexcArg<bool, FEXC_ARG_X87_GATED> fexc_x87_gated;
inline FexcArg<bool, FEXC_ARG_WAIT_GATED> fexc_wait_gated;
inline FexcArg<uint64_t, FEXC_ARG_X87_FPCR> fexc_x87_fpcr;
// Profile slice1 (I6), recorded by the allowlist: guest RIP of each admitted x87 instruction. Its floating-point ops run
// with FPSR cleared (MXCSR's flags saved around them) and, with `fexc_x87_fpcr`, under that FPCR; the cumulative FPSR flags
// they raise are or-ed into the x87 exception word at FEXC_X87_EXCEPTIONS (a guest-state field in FEX's CPUState padding
// after AbridgedFTW: FPSR bit order, IOC IE, DZC ZE, OFC OE, UFC UE, IXC PE, IDC DE; bit 6 is the resident's stack fault SF).
inline FexcSetT<uint64_t> fexc_x87;
constexpr uint32_t FEXC_X87_EXCEPTIONS = 0x4b4;
// Profile slice1 (I5c): unmasked floating-point exceptions. From the host, in x86 flag order (bit 0 IE, 1 DE, 2 ZE, 3 OE, 4 UE,
// 5 PE): `--simd-unmasked M`, the MXCSR exceptions whose mask is clear (DE left out under DAZ, which never raises it), and
// `--x87-unmasked M`, the FCW exceptions whose mask is clear. 0 (both, as Linux runs): every exception masked, no check emitted.
// Under them the compiled code checks each instruction's exception flags before it commits anything (shim/fexc_sites.inc).
inline FexcArg<uint64_t, FEXC_ARG_SIMD_UNMASKED> fexc_simd_unmasked;
inline FexcArg<uint64_t, FEXC_ARG_X87_UNMASKED> fexc_x87_unmasked;
// AFP fallback chunk 3a (plan rows 4-6 and row 3's unmasked-PE part). The host without FEAT_AFP (`--feature afp=0`: `fexc_no_afp`, set
// per request from the features) keys its code on MXCSR.DAZ (`--daz`) and on effective FTZ, FTZ with UM masked (`--ftz`; the same
// condition abi `fpcr` uses), and neither is ever sent otherwise. Both are read only where a SIMD floating-point op needs them
// (FexcPrecision::PlaceFlushes and the PE check mask), and only when `fexc_no_afp`, so no answer of a host with AFP reads them.
// FPCR.FZ is then 1 with DAZ and FTZ both (hardware flush, as the M4's FIZ and FZ), else 0: DAZ alone is a software input flush
// per FP source (behind an exact exponent screen, `FexcFlush` mode bit 2: AFP accurate plan chunk 6b), FTZ alone a software output flush with UFC and IXC for each flushed lane (FexcFlush); DAZ and FTZ both flush
// only the raw result MIN/MAX select. x87 is left alone; RCP/RSQRT fix their SDM results themselves, whatever DAZ and FTZ say.
inline bool fexc_no_afp = false;
inline FexcArg<bool, FEXC_ARG_DAZ> fexc_daz;
inline FexcArg<bool, FEXC_ARG_FTZ> fexc_ftz;
// AFP accurate plan, chunk 1 (the frame): `--accurate` (accurate floating-point mode, host without AFP). Sent only without AFP; fexc refuses
// it with AFP. Read only where a SIMD floating-point op needs it (chunks 2 and 4: the masked-DE test, the FMA NaN check). (Chunk 1's
// `--exact-step` is gone: traps replaced the exact steps, plan section 5.)
inline FexcArg<bool, FEXC_ARG_ACCURATE> fexc_accurate;
// AFP fallback, decision C (the unmasked case): FexcKeep ops (placed right before a SIMD check, only without AFP and with MXCSR.UM clear)
// name the operands of the instruction's multiplies, divides, FMAs and narrowing converts, each with a tag (crates/abi `tiny`: the
// descriptor's meta byte | position << 8 | half << 10); the JIT collects (tag, vector register) here and the check that follows takes
// them into its stub's site: `width` = meta, `imm` = five bits per register by slot (3 * half + position), `guest_offset2` = the native
// offset after the check's `cbnz` (the resident resumes there when it finds no operand lane tiny after rounding).
inline std::vector<std::pair<uint32_t, uint8_t>> FexcKeepList;
// Two bytes of FEX's flag array no FEX code uses (X87FLAG_DE_LOC, X87FLAG_ZE_LOC), guest-state fields for I5c, both in FPSR's
// flag order: the MXCSR flags SIMD-checked instructions raised since the last resident entry (folded there into MXCSR), and the
// flags the x87 ops of the current x87 instruction raised so far (folded into the x87 exception word at its end).
constexpr uint32_t FEXC_SIMD_FLAGS = 0x3f0 + 33, FEXC_X87_PENDING = 0x3f0 + 34;
// M2 task 4: x87 runs. Writing FPSR costs about 11 ns on an M4 and the wrapper above wrote it twice per x87 op (and FPCR twice
// more under `fexc_x87_fpcr`), which was 86% of an x87-heavy program's time. With every x87 exception masked on a host with AFP,
// FPSR (and the x87 FPCR) is switched once per run instead: from the first x87 floating-point op to the next op that is not
// x87's and reads or writes FPSR or FPCR (an SSE/AVX floating-point op, a check, STMXCSR) or the block's next jump or exit.
// At the run's start FPSR's flags (MXCSR's) move into the SIMD flag byte, which the resident ORs into MXCSR at every entry,
// and FPSR is cleared; each x87 op then only reads FPSR into the pending byte (flags are sticky and are committed to the x87
// exception word at each instruction's end, so reading the run's flags again commits nothing new); the run's end clears FPSR.
// FEXC_X87_RUN (FEX's X87FLAG_UE_LOC byte, which no FEX code uses) is 1 during a run: at a fault inside one the resident does
// not take FPSR for MXCSR's flags. FEXC_X87_FPCR (FEX's callret_sp, unused in slice1): the FPCR the run started with.
// A stack fault drops its instruction's own flags; inside a run they stay in FPSR, but with AFP the only flag such an
// instruction's ops can raise is IOC (an SNaN beside the substituted QNaN, a compare or integer conversion of it), and the
// stack fault itself sets IE, so nothing changes. An instruction converting an m32 operand has a DE fix-up that reads its
// own ops' DZC and IDC: it clears FPSR first when the run left one of them set. Kept per op, as before: hosts without AFP
// (their NaN and DE fix-ups read each op's flags) and unmasked exceptions.
// 64-bit code: a run ends at its IR block's end (every block of SSE code is entered with FPSR holding MXCSR's flags, as
// before, and no SSE code changes). 32-bit code, where x87 is the usual floating point: a run stays open across blocks (links
// and the dispatcher's hits keep FPSR, FPCR and the byte), each IR block's first x87 op asking the byte whether to start one
// and its first FPSR or FPCR user that is not x87's whether to end one: a load and a branch per block. Every full entry from
// the resident starts closed (FPSR clear, MXCSR's FPCR, the byte 0), and every change of mode or identity is one.
// ponytail: 64-bit code pays two FPSR writes per block with x87; the 32-bit scheme there would put the load and branch in
// every block of SSE code.
// The cast fix (M2 task 4): a run's FPCR is made from FCW.RC when the run starts (AH and NEP, RMode from RC, no flush to
// zero), so x87 rounding is honoured per run whatever MXCSR says and the rounding control is no part of the code identity;
// FLDCW is translated when it keeps the exception masks (`FexcFldcwInline`): it ends the run and stores the word.
constexpr uint32_t FEXC_X87_RUN = 0x3f0 + 36, FEXC_X87_FPCR = 0xb0;
// M2 task 4, lazy x87 flags: where x87 ops run in runs, FPSR keeps the flags of the run's x87 instructions and nothing copies
// them per op or per instruction. They are or-ed into the exception word where it is read or FPSR is taken back: a run's end,
// FNSTSW (FexcX87Fold), and the resident's bank inside a run. An instruction that may have to drop its own flags saves FPSR's
// flag byte, with bit 5 set, in the pending byte before its first floating-point op and clears the byte at its end (a "hold"):
// one whose floating-point op comes before a fault site of its own (the narrowing stores: fexc_x87_hold, checked when a site
// is recorded), and one with a stack check (FexcX87Hold, FexcX87Release: after a stack fault FPSR's flags are the saved ones
// again). At a bank inside a run the resident takes the saved byte when bit 5 is set, else FPSR.
inline bool FexcX87Lazy() { return fexc_slice1 && !fexc_no_afp && uint64_t(fexc_x87_unmasked) == 0; }
// M2 task 4, `--x87-guess V` (32-bit code; V = 0x1000 | TOP << 8 | the tag byte, as the guest state held them when the unit
// was requested; 0: none): the x87 stack pass compiles the unit for that pair (FEX's x87StackOptimizationPass, TopAbs).
inline FexcArg<uint64_t, FEXC_ARG_X87_GUESS> fexc_x87_guess;
inline std::set<uint64_t> fexc_x87_hold;       // guest RIP of each x87 store to memory that converts first (FST m32, FIST*, FISTTP)
inline bool fexc_x87_hold_wanted = false, fexc_x87_held = false, fexc_x87_fp_seen = false;   // the instruction at the emit cursor
// A fault point after a floating-point op of its own x87 instruction that fexc_x87_hold does not list (none known): the unit is
// compiled again with every x87 instruction held (fexc.cpp), which is exact for any form.
inline bool fexc_x87_hold_missing = false, fexc_x87_hold_all = false;
inline bool FexcFldcwInline() { return fexc_slice1 && !fexc_step_unit && !fexc_no_afp && uint64_t(fexc_x87_unmasked) == 0; }
enum : int { FEXC_RUN_CLOSED, FEXC_RUN_OPEN, FEXC_RUN_UNKNOWN };
inline int fexc_x87_run = FEXC_RUN_CLOSED;     // whether an x87 run is open at the emit cursor (unknown: 32-bit code, at an IR block's start)
inline bool fexc_x87_m32_convert = false;      // the op being emitted is an x87 instruction's m32 convert (FexcX87Begin)
// FEX's X87FLAG_OE_LOC byte (no FEX code uses it): set by the resident's FP trap handler when it writes a result that may be a denormal
// (DAZ and FTZ clear), cleared by the resident at a DENORM stub; the in-block DE screen skip's check reads it (`frame::DE_PENDING`).
constexpr uint32_t FEXC_DE_PENDING = 0x3f0 + 35;
// Recorded by the allowlist (I5c): guest RIP of each waiting x87 instruction, WAIT/FWAIT and MMX instruction (it delivers a
// pending x87 exception, #MF, before it runs: SDM Vol.1 8.7.1), of each FST/FSTP m32/m64 (an unmasked OE/UE stores nothing)
// and each x87 instruction's length (the resident resumes after one whose unmasked exception left its operands unaltered).
inline FexcSetT<uint64_t> fexc_x87_waiting, fexc_x87_store, fexc_x87_m32;   // fexc_x87_m32: x87 instructions with an m32 convert (AFP hosts' DE fix-up)
inline FexcMapT<uint64_t, uint8_t> fexc_x87_length;
// M2 task 4: guest RIP of each admitted x87 instruction with a register operand only (ModRM.mod = 3): it has no access site,
// and with every x87 exception masked no stub. The x87 stack pass keeps what such instructions change in registers and writes
// it to the guest-state page before the next instruction that is not one of them (FEX's x87StackOptimizationPass, FexcCache).
inline std::set<uint64_t> fexc_x87_quiet;
// The x87 lowering's check operands per guest RIP (I5c; IR nodes, FEX's Ref): stack fault, stack overflow, invalid operand
// (FST m64's SNaN, FIST's out-of-range value) and exact tiny (FST m64), each a 0/1 value or null.
struct FexcX87Extras { void* fault {}; void* overflow {}; void* invalid {}; void* tiny {}; };
inline FexcMapT<uint64_t, FexcX87Extras> fexc_x87_extra;
// Filled when the checks are placed (I5c): guest RIP of each instruction given a SIMD exception check.
inline FexcSetT<uint64_t> fexc_simd_checked;
// AFP accurate plan, chunk 2 (B): the instructions (RIPs) whose software DE flag is set with DE masked (accurate mode, no AFP, MXCSR.DM set, no
// DAZ): PlaceFPChecks fills it, FexcDenormalStep (DPPS/DPPD's add levels, inside the JIT) reads it.
inline FexcSetT<uint64_t> fexc_simd_de_masked;
