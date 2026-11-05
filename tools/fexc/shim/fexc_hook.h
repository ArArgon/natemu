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
  FEXC_SITE_ALIGN = 14,                     // slice1 stub (I5a): a legacy SSE 16-byte memory operand not 16-byte aligned (#GP(0))
  FEXC_SITE_SIMD = 15,                      // slice1 stub (I5c): an SSE/AVX instruction raised an unmasked SIMD exception (#XM/#UD)
  FEXC_SITE_X87 = 16,                       // slice1 stub (I5c): an x87 instruction raised an unmasked exception that leaves its
                                            // operands unaltered (width: the instruction's length; w0: its check word)
  FEXC_SITE_PENDING = 17,                   // slice1 stub (I5c): a waiting x87, WAIT or MMX instruction with an x87 exception pending (#MF)
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
  bool ud = false;                // I10: an encoding the presented CPU raises #UD for (reserved or unadvertised): reported as cut_ud
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
             FEXC_ARG_SIMD_UNMASKED, FEXC_ARG_X87_UNMASKED, FEXC_ARG_CPL0, FEXC_ARG_TSC, FEXC_ARG_BULK };
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
// Profile slice1 (I3), recorded by the dispatcher: guest RIP of each LOCK CMPXCHG / CMPXCHG8B / CMPXCHG16B, whose casal/caspal
// the JIT precedes with `dmb ish` (x86 orders a failing locked compare; a failing Arm CAS writes nothing, acquire only).
inline std::set<uint64_t> fexc_lock_cas;
// Profile slice1 (I5a): the vCPU runs with CR0.EM or CR0.TS set or CR4.OSFXSR clear (`--sse-gated`, from the host): every SSE
// form is cut as a system instruction (the resident raises #UD or #NM, SDM Vol.3 Table 2-2 and the SSE exception tables).
inline FexcArg<bool, FEXC_ARG_SSE_GATED> fexc_sse_gated;
// Profile slice1 (X2): the vCPU runs under x86 alignment checking (CR0.AM, RFLAGS.AC, CPL 3; `--alignment-gated`, from the
// host): BT/BTS/BTR/BTC with a memory operand are cut as system instructions. FEX accesses the one byte holding the bit, so the
// operand-size access x86 checks (#AC) never happens natively; the resident checks the operand's alignment, then steps it.
inline FexcArg<bool, FEXC_ARG_ALIGNMENT_GATED> fexc_alignment_gated;
// Profile slice1 (I5a), recorded by the allowlist: guest RIP of each legacy SSE instruction whose 16-byte memory operand must be
// 16-byte aligned (#GP(0) otherwise: SDM Vol.2 2.5, exception classes 1, 2 and 4); its first access gets an alignment check.
inline std::set<uint64_t> fexc_align16;
// Profile slice1 (I8): the same for VMOVAPS/VMOVAPD/VMOVDQA/VMOVNT* ymm, whose 32-byte memory operand must be 32-byte aligned.
inline std::set<uint64_t> fexc_align32;
// Profile slice1 (I8): the vCPU's controls gate the VEX AVX forms (`--avx-gated`, from the host: CR4.OSXSAVE clear, XCR0[2:1]
// not 11b, or CR0.TS): every one is cut as a system instruction (the resident raises #UD, else #NM).
inline FexcArg<bool, FEXC_ARG_AVX_GATED> fexc_avx_gated;
// Profile slice1 (I5a), recorded by the allowlist so the JIT raises the MXCSR exception flags x86 does (FPSR is MXCSR's flags):
// COMISS/COMISD compare signalling (`fcmpe`: IE for a QNaN too; FEX emits UCOMIS's quiet `fcmp` for both); RCPPS/RCPSS/
// RSQRTPS/RSQRTSS raise none (FEX divides exactly: FPSR is saved and restored around the op); ROUNDPS/PD/SS/SD with imm8
// bit 3 clear raise PE when inexact (`frintx`, under the instruction's rounding mode when imm8 bit 2 is clear).
inline std::set<uint64_t> fexc_fp_signal, fexc_fp_quiet, fexc_round_inexact;
// Profile slice1 (M3), recorded by the allowlist: guest RIP of each MOV-family instruction -> its completion record for a
// device access (shim/fexc_mmio.inc); fexc.cpp attaches it to the instruction's one load or store site.
inline std::map<uint64_t, uint32_t> fexc_mmio;
// Profile slice1 (I6): x87 in FEX's reduced-precision mode (X87REDUCEDPRECISION, set by fexc for slice1): ST(i) as doubles in
// the guest-state page (`mm[i]`, low 8 bytes), TOP/C0..C3 in FEX's flag bytes, FCW and the abridged tag word in their FEX
// fields. From the host: `--x87-gated` (CR0.EM or CR0.TS) cuts every x87 form d8..df as a system instruction (#NM),
// `--wait-gated` (CR0.TS and CR0.MP) cuts WAIT/FWAIT (9b) likewise; `--x87-fpcr V` (V nonzero) is the FPCR x87 arithmetic
// runs under (FCW.RC, no flush to zero) when it differs from MXCSR's (the resident enters blocks with MXCSR's FPCR).
inline FexcArg<bool, FEXC_ARG_X87_GATED> fexc_x87_gated;
inline FexcArg<bool, FEXC_ARG_WAIT_GATED> fexc_wait_gated;
inline FexcArg<uint64_t, FEXC_ARG_X87_FPCR> fexc_x87_fpcr;
// Profile slice1 (I6), recorded by the allowlist: guest RIP of each admitted x87 instruction. Its floating-point ops run
// with FPSR cleared (MXCSR's flags saved around them) and, with `fexc_x87_fpcr`, under that FPCR; the cumulative FPSR flags
// they raise are or-ed into the x87 exception word at FEXC_X87_EXCEPTIONS (a guest-state field in FEX's CPUState padding
// after AbridgedFTW: FPSR bit order, IOC IE, DZC ZE, OFC OE, UFC UE, IXC PE, IDC DE; bit 6 is the resident's stack fault SF).
inline std::set<uint64_t> fexc_x87;
constexpr uint32_t FEXC_X87_EXCEPTIONS = 0x4b4;
// Profile slice1 (I5c): unmasked floating-point exceptions. From the host, in x86 flag order (bit 0 IE, 1 DE, 2 ZE, 3 OE, 4 UE,
// 5 PE): `--simd-unmasked M`, the MXCSR exceptions whose mask is clear (DE left out under DAZ, which never raises it), and
// `--x87-unmasked M`, the FCW exceptions whose mask is clear. 0 (both, as Linux runs): every exception masked, no check emitted.
// Under them the compiled code checks each instruction's exception flags before it commits anything (shim/fexc_sites.inc).
inline FexcArg<uint64_t, FEXC_ARG_SIMD_UNMASKED> fexc_simd_unmasked;
inline FexcArg<uint64_t, FEXC_ARG_X87_UNMASKED> fexc_x87_unmasked;
// Two bytes of FEX's flag array no FEX code uses (X87FLAG_DE_LOC, X87FLAG_ZE_LOC), guest-state fields for I5c, both in FPSR's
// flag order: the MXCSR flags SIMD-checked instructions raised since the last resident entry (folded there into MXCSR), and the
// flags the x87 ops of the current x87 instruction raised so far (folded into the x87 exception word at its end).
constexpr uint32_t FEXC_SIMD_FLAGS = 0x3f0 + 33, FEXC_X87_PENDING = 0x3f0 + 34;
// Recorded by the allowlist (I5c): guest RIP of each waiting x87 instruction, WAIT/FWAIT and MMX instruction (it delivers a
// pending x87 exception, #MF, before it runs: SDM Vol.1 8.7.1), of each FST/FSTP m32/m64 (an unmasked OE/UE stores nothing)
// and each x87 instruction's length (the resident resumes after one whose unmasked exception left its operands unaltered).
inline std::set<uint64_t> fexc_x87_waiting, fexc_x87_store;
inline std::map<uint64_t, uint8_t> fexc_x87_length;
// The x87 lowering's check operands per guest RIP (I5c; IR nodes, FEX's Ref): stack fault, stack overflow, invalid operand
// (FST m64's SNaN, FIST's out-of-range value) and exact tiny (FST m64), each a 0/1 value or null.
struct FexcX87Extras { void* fault {}; void* overflow {}; void* invalid {}; void* tiny {}; };
inline std::map<uint64_t, FexcX87Extras> fexc_x87_extra;
// Filled when the checks are placed (I5c): guest RIP of each instruction given a SIMD exception check.
inline std::set<uint64_t> fexc_simd_checked;
