# Apple Silicon Total Store Ordering (TSO) — Technical Dossier

**Compiled:** 2026-08-08
**Test machine:** Apple M2 Max (`hw.cpufamily` = -634136515, 12 cores), macOS 26.5.2 (build 25F84)
**Author's note on evidence tiers:** every claim below is tagged.

| Tag | Meaning |
|---|---|
| **VERIFIED-LOCAL** | I reproduced it on this machine (code run, register read, binary disassembled). Highest confidence. |
| **VERIFIED-PRIMARY** | Stated in a primary source: Apple SDK header, Apple docs, kernel patch, vendor code. |
| **COMMUNITY** | Reverse-engineering / third-party claim from a credible source (marcan, Saagar Jha, Dougall Johnson, FEX devs). |
| **INFERENCE** | My reasoning from the above. Explicitly flagged. |
| **UNVERIFIED** | Repeated claim I could not confirm. Treat as rumor. |

---

## Bottom line / feasibility verdict

**1. TSO is real, it is a single bit — `ACTLR_EL1[1]` (`EnTSO`) — and it is per-thread, software-managed.** Confidence: **High** (VERIFIED-LOCAL — I disassembled the set and clear sequences out of every macOS 26.5.2 kernel on this disk).

**2. CRITICAL Q3 — Can a third-party *native macOS* process enable TSO? NO.** There is no public API, no entitlement you can request, no sysctl, and no Mach call. The bit is applied by the XNU exception-return path *only* when a per-thread flag word at `thread + 0x128` has bit 0 set, and that flag is set by the kernel for Rosetta-translated threads. Getting it on a normal process requires a kernel extension patching thread state (Saagar Jha's `TSOEnabler`), which means disabling SIP and lowering boot security. Confidence: **High** for "no supported path"; **Medium** for the precise gating predicate (see §4.3).

**3. CRITICAL Q4 — Is TSO available inside a VM under Hypervisor.framework? YES — fully, and it is a first-class documented API since macOS 15.** Two independent routes, **both of which I executed successfully on this machine**:
   - **Host route:** `hv_vcpu_set_sys_reg(vcpu, HV_SYS_REG_ACTLR_EL1, 2)`. Added in macOS 15.0. Apple's own header says it "only allows getting / setting of the ACTLR_EL1.EnTSO bit (index 1)."
   - **Guest route:** guest EL1 code executes `msr actlr_el1, x` **directly, with no trap to EL2**. I ran guest code that did exactly this and it returned to me via `hvc` with `ACTLR_EL1 == 0x2`, and the host's `hv_vcpu_get_sys_reg` agreed.

   And it is not merely stored — I measured a **consistent 2–28% slowdown** on a DRAM-bound guest load/store loop with `EnTSO=1`, matching the expected TSO tax. Confidence: **High** (VERIFIED-LOCAL).

   **This is the single most important finding for an emulator project: if you are willing to run your guest inside a VM, you get hardware TSO on macOS today, legitimately, with a public API.** That is the exact trick Apple itself uses for "Rosetta for Linux."

**4. Feasibility verdict for a hypothetical x86 emulator on Apple Silicon:**
   - Native macOS userspace emulator → **no hardware TSO.** You must emulate ordering with barriers/`LDAPR`/`STLR`. (Or ship a kext, which is a non-starter for distribution.)
   - VM-based emulator (Hypervisor.framework or Virtualization.framework) → **hardware TSO available and supported.** Requires macOS 15+ for the host API; guest self-service works too.

---

## 1. The hardware mechanism

### 1.1 The register and bit

**`ACTLR_EL1` bit 1 = `EnTSO`.** Confidence: **High**.

- ARM defines `ACTLR_EL{1,2,3}` as IMPLEMENTATION DEFINED, so Apple is free to put anything there. (VERIFIED-PRIMARY — ARM ARM; also stated in marcan's notes.)
- Linux patch: `#define ACTLR_APPLE_TSO BIT(1)` (VERIFIED-PRIMARY — [marcan's arm64 TSO patch 4/4](https://lkml.rescloud.iu.edu/2404.1/04830.html)).
- Apple's own SDK header calls it `ACTLR_EL1.EnTSO ... (index 1)` (VERIFIED-LOCAL — quoted in §5.1).
- Apple's Virtualization docs call it `ACTLR.TSOEN` (VERIFIED-PRIMARY — [Accelerating the performance of Rosetta](https://developer.apple.com/documentation/virtualization/accelerating-the-performance-of-rosetta)).

Three different names for the same bit: `EnTSO` (Apple SDK), `TSOEN` (Apple docs), `ACTLR_APPLE_TSO` (Linux). All = bit 1.

### 1.2 Capability discovery: `AIDR_EL1` bit 9

**`AIDR_EL1[9] == 1` indicates TSO is supported.** Confidence: **High**.

- marcan: "`ACTLR_ELx<1>` enables TSO mode. `AIDR_EL1<9>` indicates that this is supported." (COMMUNITY, but corroborated — [Apple TSO gist](https://gist.github.com/marcan/9ab73ca0614864bea0eea9e953c074d3))
- Linux patch: `AIDR_APPLE_TSO BIT(9)`, gated behind `read_cpuid_implementor() == ARM_CPU_IMP_APPLE` (0x61). (VERIFIED-PRIMARY — [patch 4/4](https://lkml.rescloud.iu.edu/2404.1/04830.html))
- **VERIFIED-LOCAL:** guest EL1 code on this M2 Max read `AIDR_EL1 = 0x6afd5797`. Bit 9 = **1**. `AIDR_EL1` is *not* trapped by Apple's hypervisor, so guests can do this discovery themselves.

There is **no** `hw.optional.arm.FEAT_*` sysctl for TSO. I dumped all 1823 sysctls on this machine; the only `tso` hits are TCP segmentation offload (`net.inet.tcp.tso`). (VERIFIED-LOCAL) Do not go looking for `hw.optional.arm.FEAT_TSO` — it does not exist.

### 1.3 Granularity: per-thread, software-managed

The bit is **per-core hardware state**, made **per-thread** by the OS saving/restoring it across exception entry/exit and context switch. Confidence: **High** (VERIFIED-LOCAL).

- Apple docs: "Adds support for context-switching the `ACTLR.TSOEN` bit when the thread switches. This optimization allows per-thread setting of the `ACTLR.TSOEN` bit." (VERIFIED-PRIMARY)
- Linux: stored in `t->thread.actlr`, restored on switch. (VERIFIED-PRIMARY)
- macOS: see §4.2 for the exact disassembly.

### 1.4 What the bit actually changes

**TSO = x86-style ordering: no LoadLoad, no LoadStore, no StoreStore reordering. StoreLoad reordering is still permitted** (that is what a store buffer *is*; x86 needs `MFENCE` for the same reason). Confidence: **High** for the model definition.

> **Practical warning from my own testing:** I initially wrote a **Store Buffering (SB)** litmus test to detect TSO and got a null result, because **SB is legal under TSO**. If you are validating a TSO implementation, use **Message Passing (MP)**, **Load Buffering (LB)**, or **IRIW** — not SB. (VERIFIED-LOCAL, learned the hard way.)

What it does **not** obviously change, and where I could not get authoritative answers:

| Aspect | Status |
|---|---|
| Plain loads/stores | Ordered. Core purpose. **High** |
| Store→Load reordering | **Still allowed** (TSO permits it). **High** |
| LSE atomics (`LDADD`/`CAS`/`SWP`) | Already at least as strong as TSO; TSO does not weaken them. **INFERENCE, Medium** |
| Acquire/release (`LDAR`/`STLR`) | Unaffected — still stronger than TSO. **INFERENCE, Medium** |
| `DMB` | Unaffected. **INFERENCE, Medium** |
| Store-buffer forwarding | No evidence it changes. **Unverified** |
| Non-temporal `LDNP`/`STNP` | Widely believed **NOT** covered by TSO — these are explicitly weakly-ordered by architecture. **Unverified** — treat as still needing manual barriers. |
| 128-bit `LDP`/`STP`, vector `LD1`/`ST1` | Atomicity is a *separate* question from ordering (`FEAT_LSE2` governs single-copy atomicity). TSO does not make a 16-byte `STP` single-copy atomic. **INFERENCE, Medium-High** — this is a real correctness trap for emulator authors. |

**The extra bits Apple sets alongside TSO.** This is original finding — see §4.2. Apple does **not** set bit 1 alone; it sets bit 1 **plus** bits 4/5/6 (M1/M2) or bit 4 (M3+). Those additional IMPDEF bits are undocumented. If you enable *only* `EnTSO` via `hv_vcpu_set_sys_reg` (which is all Apple lets you set), **you are getting a strictly smaller behavior change than Rosetta on macOS gets.** What the other bits do is **Unverified** — plausibly x86 FP/denormal semantics or disabling a memory optimization, but I have no evidence. Flagging this as an open risk.

### 1.5 Chip support

**Every Apple Mac SoC from M1 through the M5 generation implements the bit.** Confidence: **High** (VERIFIED-LOCAL).

I scanned all 12 per-SoC kernels shipped in `/System/Library/Kernels/` on macOS 26.5.2 and found the **identical** TSO set/clear sequence in every one:

| Kernel | SoC | Extra bits set with EnTSO |
|---|---|---|
| `kernel.release.t8103` | M1 | `0x70` (bits 4,5,6) |
| `kernel.release.t6000` | M1 Pro / Max / Ultra | `0x70` |
| `kernel.release.t8112` | M2 | `0x70` |
| `kernel.release.t6020` | M2 Pro / Max / Ultra | `0x70` |
| `kernel.release.t8122` | M3 family | `0x10` (bit 4) |
| `kernel.release.t6030` | M3 Pro | `0x10` |
| `kernel.release.t6031` | M3 Max | `0x10` |
| `kernel.release.t8132` | M4 | `0x10` |
| `kernel.release.t6041` | M4 Pro / Max | `0x10` |
| `kernel.release.t6050` | M5-gen | `0x10` |
| `kernel.release.t8140` | M5-gen | `0x10` |
| `kernel.release.t8142` | M5-gen | `0x10` |

(SoC-name mapping for the newest parts is **INFERENCE** from Apple's `t`-code numbering; the *presence of the sequence* is VERIFIED-LOCAL in all 12.)

**A-series:** Saagar Jha's TSOEnabler has a [`t8020` branch](https://github.com/saagarjha/TSOEnabler/tree/t8020) for the **A12Z**, so TSO predates the M1 and existed in A-series silicon by 2018–2020. (VERIFIED-PRIMARY — repo branch exists.) Support in shipping iPhone A-series chips is **Unverified**; there is no reason for Apple to remove it since it is the same core design, but nothing exposes it on iOS.

**Efficiency cores:** the TSOEnabler README claims enabling TSO "will also pin these to the high-performance cores of your processor" (VERIFIED-PRIMARY — [README](https://github.com/saagarjha/TSOEnabler)). The natural reading is that **E-cores do not implement `EnTSO`**, so the scheduler must keep TSO threads on P-cores. I could **not** independently verify this; the kernel sequence I disassembled is not core-type conditional. **Medium confidence / partly Unverified.** If true it has a real consequence: **TSO threads lose access to half your core count**, which is a hidden cost nobody quotes.

### 1.6 Cost of toggling on context switch

**Expensive: a `DSB SY` + `MSR` + `ISB` on *every* kernel entry and *every* kernel exit for a TSO thread.** Confidence: **High** (VERIFIED-LOCAL).

This is the most under-reported cost in the public discussion, and I found it by disassembly rather than from any document. See §4.2 for the exact code. `DSB SY` is a full *system* barrier — it waits for all outstanding memory traffic to complete, not just inner-shareable. Paying that twice per syscall is not free.

**INFERENCE (Medium):** this is presumably why Apple keeps the kernel itself running weakly-ordered rather than just leaving TSO on for the whole process — but it means syscall-heavy emulated workloads pay a toll that CPU-bound ones do not.

---

## 2. How Rosetta 2 uses it on macOS

**Mechanism (VERIFIED-LOCAL, original disassembly — see §4.2):**

1. XNU keeps a per-thread flags word at offset `0x128` from the thread structure (`TPIDR_EL1`).
2. Bit 0 of that word means "this thread runs with Apple's x86-compat CPU mode."
3. On **return to userspace**, if the bit is set: `ACTLR_EL1 |= 0x72` (or `0x12` on M3+), guarded by `DSB SY` / `ISB`.
4. On **kernel entry**, the reverse: `ACTLR_EL1 &= ~0x72` (or `~0x12`), same barriers. **The kernel itself never runs in TSO mode.**
5. Two further Apple IMPDEF registers are switched in the same block — `S3_6_C15_C8_3` (masked with `0x6`) and `S3_6_C15_C2_5`. Purpose **Unverified**; presumably more x86-compat state.

This matches Saagar Jha's 2020-era description exactly: "This is normally done by the kernel through modifications to a special register upon exit from the kernel for programs running under Rosetta 2." (VERIFIED-PRIMARY — [TSOEnabler README](https://github.com/saagarjha/TSOEnabler))

**Why Apple did it:** "X86 follows a stricter memory ordering model... To produce the same behavior as under x86, each access would have to be explicitly synchronized. Instead of paying the accompanying performance costs, Apple built TSO directly into their processors." (Wrenger et al.)

---

## 3. How Linux uses it (Asahi / FEX-Emu / Rosetta-for-Linux)

### 3.1 The `prctl` interface

Both the Asahi and the Apple kernel patch series expose the same userspace API:

```c
prctl(PR_SET_MEM_MODEL, PR_SET_MEM_MODEL_TSO);
prctl(PR_GET_MEM_MODEL, ...);   /* -> PR_SET_MEM_MODEL_DEFAULT or _TSO */
```

- `PR_SET_MEM_MODEL` "will attempt to enable the requested memory model, with the return code indicating whether it was successful. For Apple CPUs, requesting TSO will result in the proper CPU bits being set." (VERIFIED-PRIMARY — [LWN 970907](https://lwn.net/Articles/970907/))
- Apple's doc instructs: "the app needs to call the `prctl` API on each thread that needs TSO support by calling `prctl(PR_SET_MEM_MODEL, PR_SET_MEM_MODEL_TSO)`." (VERIFIED-PRIMARY — [Apple](https://developer.apple.com/documentation/virtualization/accelerating-the-performance-of-rosetta))

### 3.2 Two competing patch series, April 2024

| | **Hector Martin (marcan) / Asahi** | **Zayd Qumsieh / Apple** |
|---|---|---|
| Posted | 2024-04-11 | 2024-04-10 |
| Link | [patchew](https://patchew.org/linux/20240411-tso-v1-0-754f11abfbff@marcan.st/) | [patchew](https://patchew.org/linux/20240410211652.16640-1-zayd._5Fqumsieh@apple.com/) |
| Target | **Bare metal** Asahi | **Virtualized** Linux guests |
| Detection | `AIDR_EL1[9]` + Apple implementer | ACTLR.TSOEN |
| Files | `arch/arm64/kernel/process.c`, cpufeature | `arch/arm64/include/asm/tso.h`, `arch/arm64/kernel/tso.c` (62 lines), `process.c` (+61) |

Apple's series carries a requirement that is **the smoking gun for the VM question**:

> "requires Apple Silicon MacOS Sonoma version **14.4 or later**, since earlier versions do not support modification of the TSOEN bit."

(VERIFIED-PRIMARY.) **INFERENCE (High):** macOS 14.4 is when Apple's EL2 stopped trapping/blocking guest `ACTLR_EL1` writes. macOS 15.0 then added the host-side `HV_SYS_REG_ACTLR_EL1` API. So guest-side self-service came *first* (14.4), host-side control came second (15.0).

### 3.3 Upstream outcome: **rejected**

Confidence: **High** for rejection, **Medium** for current mainline status (I could not run a fresh mainline grep).

- Will Deacon registered a "strong objection," arguing it fragments userspace.
- Catalin Marinas indicated he would block IMPDEF features of this kind.
- Marc Zyngier's suggested alternative was, pointedly, **"using virtual machines with TSO enabled."**
- Options left on the table: keep it downstream, or enable TSO unconditionally and eat ~9%.

(VERIFIED-PRIMARY — [LWN 970907](https://lwn.net/Articles/970907/), [LWN 969484](https://lwn.net/Articles/969484/))

marcan noted the code "has been brewing in the downstream Asahi Linux tree for a while now, and ships to thousands of users." So: **shipping to real users via Asahi's downstream tree; not in mainline.** (VERIFIED-PRIMARY for the quote; mainline absence is **Medium** confidence.)

### 3.4 FEX-Emu

FEX consumes the `prctl` and relaxes its codegen when it succeeds:

> "With recent changes from the Asahi developers, FEX can ask the hardware to enable the TSO emulation bit, and if the kernel reports back that hardware TSO memory is enabled, FEX can take a more lax approach to its memory emulation, getting an automatic speedup for Asahi systems."

(VERIFIED-PRIMARY — [FEX 2306 release notes](https://fex-emu.com/FEX-2306/))

FEX's software fallback is a set of config knobs (`ParanoidTSO`, `HalfBarrierTSOEnabled`, `VectorTSOEnabled`, `MemcpySetTSOEnabled`) that trade correctness for speed when hardware TSO is unavailable. Detailed per-knob costs: see §6.

### 3.5 Rosetta for Linux under Virtualization.framework

Apple ships a downloadable `RosettaPatch.zip` (`RosettaPatch.diff`) from the developer portal, applying cleanly to Linux 6.10, which adds exactly the three things above (context-switch support, read/modify helpers, the `prctl`). (VERIFIED-PRIMARY — [Apple](https://developer.apple.com/documentation/virtualization/accelerating-the-performance-of-rosetta))

Apple's own framing of *why* per-thread control matters is a direct statement of the alternative's cost:

> "Without selective enablement, the system opts all processes into this memory mode, which degrades performance for native ARM processes that don't need it."

This corroborates the widely-repeated community claim that **before the patch, a Virtualization.framework VM with Rosetta enabled ran the *entire VM* in TSO mode all the time**, penalizing even native ARM guest code. (COMMUNITY for the pre-patch behavior; the quote above is VERIFIED-PRIMARY and strongly implies it.)

### 3.6 Other Linux-side projects

- [`cyyself/m1tso-linux`](https://github.com/cyyself/m1tso-linux) — "Apple Silicon TSO Enabler for Linux." (Exists; details **Unverified**.)
- `muvm` / `libkrun` (Asahi/Sergio López) — lightweight VM used to run FEX games; relationship to TSO **Unverified** in this pass.

---

## 4. CRITICAL QUESTION: can a third-party macOS process enable TSO?

### 4.1 Verdict

**No.** Confidence: **High**.

There is no public API, no requestable entitlement, no sysctl, no Mach trap. Specifically, I checked and can rule out:

- **Sysctls** — none. All 1823 sysctls dumped; zero TSO-related. `kern.tso_enable` exists **only** if you load Saagar Jha's kext. (VERIFIED-LOCAL)
- **`hw.optional.arm.FEAT_*`** — no TSO entry. (VERIFIED-LOCAL, full list dumped)
- **`com.apple.security.cs.*`** — these are hardened-runtime entitlements (JIT, unsigned memory, DYLD env). None relates to memory ordering. (**High**)
- **Public `os_*` / `pthread_*` API** — none found in the macOS 26 SDK. (VERIFIED-LOCAL, SDK headers grepped)
- **`ARM64_REG_HID*`** — these are the chicken-bit registers Apple's kernel pokes at boot for errata/tuning. TSO is *not* there; it is in `ACTLR_EL1`. EL0 cannot write either. (**High**)

### 4.2 The enforcement mechanism (original disassembly)

**This is the definitive answer, and I obtained it by disassembling the shipping macOS 26.5.2 kernels.** (VERIFIED-LOCAL)

Enforcement is not an entitlement check at all — **it is structural**. `ACTLR_EL1` is an EL1 register. EL0 code executing `msr actlr_el1, x0` takes an undefined-instruction trap. The only way the bit gets set is if the kernel sets it for you on the way out.

**Set path** — return to userspace (from `kernel.release.t6020`, file offset `0x2de01c`):

```asm
mrs  x3, TPIDR_EL1          ; current thread
strb wzr, [x3, #0x1c6]
ldr  x4, [x3, #0x1b8]
ldr  x0, [x3, #0x128]       ; per-thread flags word
tbz  w0, #0x0, skip         ; bit 0 clear -> plain ARM64 thread, skip entirely
mrs  x0, ACTLR_EL1
orr  x0, x0, #0x2           ; EnTSO
orr  x0, x0, #0x70          ; M1/M2: bits 4,5,6   (M3+: #0x10, bit 4 only)
dsb  sy                     ; full system barrier
msr  ACTLR_EL1, x0
isb
ldr  w0, [x21, #0x114]
and  w0, w0, #0x6
msr  S3_6_C15_C8_3, x0      ; Apple IMPDEF, undocumented
ldr  w0, [x21, #0x348]
msr  S3_6_C15_C2_5, x0      ; Apple IMPDEF, undocumented
skip:
```

**Clear path** — kernel entry (4 occurrences per kernel, one per exception vector):

```asm
mrs  x0, S3_6_C15_C2_5
str  w0, [sp, #0x348]       ; save IMPDEF reg to the saved-state frame
mrs  x0, ACTLR_EL1
and  x0, x0, #0xfffffffffffffffd   ; clear EnTSO
and  x0, x0, #0xffffffffffffff8f   ; clear bits 4,5,6  (M3+: ~0x10)
dsb  sy
msr  ACTLR_EL1, x0
isb
```

So the gate is **bit 0 of the word at `thread + 0x128`**. Whatever sets that bit is the real access-control point.

### 4.3 What sets the gating bit

**COMMUNITY / Medium confidence.** Saagar Jha's kext works precisely by locating this field (it pattern-matches the `ldr` / `cbz` / `mrs actlr_el1` / `orr #2` / `orr #0x70` sequence in `__TEXT_EXEC` to recover the offset) and then writing the flag directly from a sysctl handler:

```c
char *thread_pointer;
__asm__("mrs %0, tpidr_el1" : "=r"(thread_pointer));
...
thread_pointer[tso_offset] = in;     /* set/clear the per-thread TSO flag */
```

(VERIFIED-PRIMARY — [TSOEnabler.c](https://github.com/saagarjha/TSOEnabler/blob/master/TSOEnabler/TSOEnabler.c))

Note his 2020 kernel used `cbz`; **macOS 26 uses `tbz w0, #0`**, i.e. Apple turned a whole-word check into a bitfield. TSOEnabler's matcher as published will therefore **fail on macOS 26** without an update. (VERIFIED-LOCAL by comparing the two sequences; the practical consequence is **INFERENCE, High**.)

In normal operation the flag is set for **Rosetta-translated processes** — i.e. keyed on the process having been exec'd through the OAH/Rosetta path, not on a general-purpose entitlement. (COMMUNITY, corroborated by the TSOEnabler README and by the fact that Apple's docs only ever discuss it for Rosetta.)

**`com.apple.private.oahd`:** a claim surfaced in search results that this entitlement enables TSO from userland. **I could not verify it.** It does not appear in the TSOEnabler README (I fetched the raw file and checked). `com.apple.private.*` entitlements are in any case Apple-internal — they cannot be requested, and binaries carrying them must be signed by Apple. Even if the claim is true it is **not a path available to a third party**. Status: **Unverified, and irrelevant to feasibility either way.**

### 4.4 Practical consequence

To get TSO in a native macOS process you would need a kext that pokes thread state — requiring SIP disabled, boot security lowered to "Reduced Security / allow third-party kexts," and a reboot. Not distributable. And KEXTs are themselves deprecated in favor of DriverKit, which has no such access.

**If your emulator must be a plain macOS app, plan for software-emulated ordering (§6). If you can use a VM, use the VM (§5).**

### 4.5 Rosetta's future

Apple has signalled Rosetta 2 will be wound down after a transition period (macOS 26 Tahoe announcements). **Unverified** — I could not confirm exact wording (web search budget exhausted).

**INFERENCE (Medium):** this does *not* obviously endanger the TSO bit itself. The bit is hardware; and Apple's *documented, public* `HV_SYS_REG_ACTLR_EL1` API plus the Rosetta-for-Linux kernel patch are aimed at third-party VMs, which is a commitment surface independent of macOS-side Rosetta. But a project betting on this should track it.

---

## 5. CRITICAL QUESTION: TSO inside a VM (Hypervisor.framework)

### 5.1 Verdict: YES — documented, public, and I ran it

**Confidence: High (VERIFIED-LOCAL).**

Apple's SDK header, verbatim from this machine (`.../MacOSX.sdk/System/Library/Frameworks/Hypervisor.framework/Headers/hv_vcpu_types.h:339-346`):

```c
/*!
 @abstract The ACTLR_EL1 register
 @discussion
    This only allows getting / setting of the ACTLR_EL1.EnTSO bit (index 1). Setting this bit to 1
    will cause the vcpu to use a TSO memory model, whereas clearing it will cause the vcpu to use
    the default ARM64 memory model (weakly ordered loads / stores).
*/
HV_SYS_REG_ACTLR_EL1 API_AVAILABLE(macos(15.0)) = 0xc081,
```

`ACTLR_EL1` is one of **152** registers in `hv_sys_reg_t`. `AIDR_EL1` is **not** in the set (it does not need to be — guests read it directly, see below).

### 5.2 Experiment 1 — host-side set/get

Signed with `com.apple.security.hypervisor`, on M2 Max / macOS 26.5.2:

```
hv_vm_create=0x0
hv_vcpu_create=0x0
get ACTLR_EL1 -> r=0x0 val=0x0
set ACTLR_EL1=2 -> r=0x0
re-get ACTLR_EL1 -> r=0x0 val=0x2 (EnTSO=1)
set ACTLR_EL1=0xffffffff -> r=0xfae94003        <-- HV_BAD_ARGUMENT
re-get -> val=0x2
```

**Findings:** setting `EnTSO` succeeds and reads back. Setting **any other bit is rejected** with `HV_BAD_ARGUMENT` — Apple enforces the header's "only the EnTSO bit" contract at runtime. So you **cannot** reach the extra IMPDEF bits (4/5/6) that Apple's own Rosetta path sets. (VERIFIED-LOCAL)

### 5.3 Experiment 2 — guest EL1 writes `ACTLR_EL1` itself: **no trap**

I mapped guest memory, started a vCPU at EL1 (`CPSR=0x3c5`), and ran:

```asm
mrs x0, actlr_el1
mov x1, #2
msr actlr_el1, x1        ; <-- does this trap to EL2?
isb
mrs x2, actlr_el1
mrs x3, s3_1_c0_c0_7     ; AIDR_EL1
mrs x4, midr_el1
hvc #0
```

Result:

```
EXCEPTION pc=0x40000020 ESR=0x5a000000 EC=0x16       (EC 0x16 = HVC, i.e. it ran to completion)
  GUEST RAN OK (no trap on ACTLR_EL1 access)
  guest read ACTLR_EL1 before write = 0x0
  guest read ACTLR_EL1 after  write = 0x2  (EnTSO=1)
  guest read AIDR_EL1 = 0x6afd5797  (bit9 TSO-capable=1)
  guest read MIDR_EL1 = 0x610f0000
  host  hv_vcpu_get_sys_reg(ACTLR_EL1) = 0x2
```

**Findings (all VERIFIED-LOCAL):**
- Guest EL1 `msr actlr_el1` executes natively. **No `EC=0x18` sysreg trap.** Apple's EL2 leaves `HCR_EL2.TACR` and `HACR_EL2[0]` clear for `ACTLR_EL1`.
- Guest EL1 `mrs aidr_el1` also does not trap — so `HCR_EL2.TID1` / `HACR_EL2[4]` are clear too. **Guests can do their own TSO capability detection.**
- Host and guest views are coherent.
- Guest `MIDR_EL1 = 0x610f0000` — a synthetic "VirtualApple" MIDR. Implementer is `0x61` (Apple), so marcan's `read_cpuid_implementor() == ARM_CPU_IMP_APPLE` check works in a guest, but there is **no part number**. A guest kernel must detect via `AIDR_EL1[9]`, not by part number.

**This directly contradicts the widely-cited [Parallels gist](https://gist.github.com/Skryptonyte/7eed38259850a0126a7a56353f780fc7)**, which concluded `ACTLR_EL1` "is trapped by the hypervisor" and that negotiating with Virtualization.framework would be hopeless. That conclusion was **correct for its time and is now obsolete** — Apple opened this up in macOS 14.4 (per Apple's own patch requirement) and added the host API in 15.0. **Do not trust that gist for current macOS.**

### 5.4 Experiment 3 — does it actually *do* anything?

Register state is not behavior. I ran a DRAM-bound guest loop (32 MB working set, 4160-byte stride to defeat prefetch, interleaved store + load), single vCPU, best-of-5, 2M iterations:

| Run | TSO OFF | TSO ON | Delta |
|---|---|---|---|
| 1 | 7.26 ns/iter | 7.70 ns/iter | +6.1% |
| 2 | 7.27 ns/iter | 7.43 ns/iter | +2.2% |
| 3 | 7.29 ns/iter | 9.30 ns/iter | +27.6% |

And on an L2-resident set (8 MB):

| Run | TSO OFF | TSO ON | Delta |
|---|---|---|---|
| 1 | 1.63 ns/iter | 1.80 ns/iter | +10.4% |
| 2 | 1.70 ns/iter | 1.77 ns/iter | +4.1% |
| 3 | 1.63 ns/iter | 1.67 ns/iter | +2.5% |

**TSO ON was slower in 6/6 runs.** (VERIFIED-LOCAL) The bit is genuinely reconfiguring the memory pipeline inside the VM, not being silently swallowed. The magnitude brackets the widely-quoted ~9% figure.

*Caveat:* this is one microbenchmark on one machine, run counts are small, and the 27.6% outlier suggests noise. Treat the *direction and existence* of the effect as High confidence and the *magnitude* as Low.

### 5.5 Experiment 4 — litmus test (inconclusive, reported for honesty)

I built an MP (message-passing) litmus test across two vCPUs with a barrier, 100k iterations. **Zero violations with TSO both on and off.** This does **not** show TSO is absent — it shows my test lacked sensitivity. Apple cores implement load-load hazard detection that replays speculative loads on invalidation, making MP violations hard to trigger without a purpose-built harness (`litmus7`/`herd7`-class). **Inconclusive; superseded by §5.4.** Anyone reproducing this should use a real litmus tool rather than a hand-rolled loop.

### 5.6 How Rosetta-for-Linux gets TSO — resolved

The picture is now complete and consistent:

1. Apple's EL2 does **not** trap guest `ACTLR_EL1` (macOS ≥ 14.4). (VERIFIED-LOCAL)
2. The guest Linux kernel, patched with Apple's `RosettaPatch.diff`, treats `ACTLR.TSOEN` as ordinary per-thread state and context-switches it. (VERIFIED-PRIMARY)
3. Guest userspace (the `rosetta` binary in `/Library/Apple/usr/libexec/oah/RosettaLinux/`) calls `prctl(PR_SET_MEM_MODEL, PR_SET_MEM_MODEL_TSO)` per thread. (VERIFIED-PRIMARY)
4. On an **unpatched** guest kernel, there is no per-thread mechanism, so the VM runs TSO-on globally — hence the documented penalty to native ARM guest code. (COMMUNITY + Apple's own "without selective enablement" wording)

There is no secret channel. **It is the same public mechanism available to any third-party VMM.**

### 5.7 Third-party VMMs

QEMU/UTM, Parallels, VMware can all use `HV_SYS_REG_ACTLR_EL1` on macOS 15+, and their guests can set it themselves on macOS 14.4+. Whether any of them currently *do* is **Unverified**. **INFERENCE (High):** nothing prevents them; it is a matter of plumbing.

---

## 6. Measured performance impact

### 6.1 The TSO tax on native ARM code — and a correction to the folklore

**The single most important correction in this dossier: "~9%" and "8.94%" are the *same measurement*, not two independent ones — and a newer, much broader study puts the real figure far lower.**

Hector Martin's widely-quoted "about 9%" is him citing Wrenger et al., verbatim:

> "To add some numbers to this (I was just made aware of this paper): [TOSTING PDF]. **Using TSO globally has, on average, a 9% performance hit**, so that is clearly off the table as a general solution."

(VERIFIED-PRIMARY — [linux-arm-kernel, 2024-04-11](https://lists.infradead.org/pipermail/linux-arm-kernel/2024-April/919487.html)) Confidence: **High**. Anyone presenting these as corroborating datapoints is double-counting.

| Study | Setup | Result | Confidence |
|---|---|---|---|
| **Wrenger et al.** (ARCS'23 / JSA'24) | M1 **Ultra**, 20 threads, SPECspeed 2017 **FP only**, TSO toggled **globally** | geomean 86.59 (WO) → 78.85 (TSO) = **8.94% score decrease** (= **+9.8% runtime**) | **High** |
| **Khayet & Morrison** (ASPLOS'26) | M1 **and M4**, **49 applications** (SPEC 2017, PARSEC, SPLASH-2x, OpenBenchmarking), per-process TSO | "**typically within 3%**"; harmonic mean **4.1% single-core / 4.3% multi-core** excluding 7 outliers; **1.9%** after trivial allocator fixes | **High** |
| **My measurement** (§5.4) | M2 Max, memory-bound loop **inside a VM** | +2.2% to +27.6%, always slower | **Medium** (direction High, magnitude Low) |

**The 8.94% figure is unrepresentative and the reasons are now understood.** ASPLOS'26 traced every large slowdown to *implementation artifacts*, not to TSO ordering:

- **L1 self-conflict squashes** — `bwaves`, `cactuBSSN`, `wrf`, `roms` show 19–70% slowdowns. TSO load throughput collapses "once the load burst accesses **more than 8 addresses — exactly the associativity of the L1 data cache**." Randomizing malloc intra-page offsets *eliminated* the outliers and also sped up ARM mode by ~40% on cactuBSSN.
- **Partial store-to-load forwarding** — "STL forwarding in TSO mode is as fast as Arm's if the load obtains all its data from the store, but is **about 5× slower otherwise**."
- **M1 SIMD loads >16 B are >10× slower in TSO mode** — **already fixed on M4** ("on the M4, all load variants have equal throughput in both modes").

**Practical read (INFERENCE, Medium-High):** budget ~2–5% for the TSO tax on modern Apple silicon, not 9%, and expect it to be near zero if you control your allocator's alignment behavior. Note also that Wrenger measured *FP-only, multithreaded* SPEC; SPEC **integer** showed no measurable TSO effect at all.

**E-cores are different.** ASPLOS'26: "for all patterns, **Arm mode throughput is consistently 2× better than TSO mode's**" on E cores. This corroborates §1.5's P-core-pinning claim — TSO on E-cores is real but bad.

### 6.2 Emulation speedup from hardware TSO

FEX-Emu's position is unambiguous and repeated across releases:

> "Emulating the x86 memory model is the **number one thing that slows down FEX emulation today**. Apple Silicon supports this memory model in hardware which is why Rosetta on MacOS can get amazing performance." ([FEX-2306](https://fex-emu.com/FEX-2306/))

> "**This is why Apple has hardware x86-TSO support and their performance is the best in the industry, side-stepping the problem entirely.**" ([FEX-2504](https://fex-emu.com/FEX-2504/))

**However — and this is a real gap — nobody has published a same-machine A/B of FEX with hardware TSO vs FEX with barrier TSO.** Every FEX blog post from 2301 through 2608 was checked, and the Asahi Linux blog's 27 posts mention TSO **zero** times. The kernel patch series' claim of a "massive performance boost" cites only the qualitative FEX-2306 post. **Any specific percentage attributed to "hardware TSO makes FEX N% faster" is unsourced.** (Confidence in the *absence*: **High**.)

The best available proxy is FEX's estimate of the *alternative's* cost: **"Even with this set of extensions, emulating x86's memory model can have near a 10x performance hit."** ([FEX-2404](https://fex-emu.com/FEX-2404/)) Confidence: **Medium-High** (vendor claim, no raw data).

### 6.3 Cost of the alternative — measured directly on this machine

This is the number that decides the architecture. Public sources do not contain LDAR/LDAPR timings for *any* Apple core (Dougall Johnson's tables omit them entirely). **So I measured them.** All figures below are **VERIFIED-LOCAL**, M2 Max P-core, best-of-3, 8 independent ops per iteration to isolate throughput.

**Instruction throughput, L1-resident (reciprocal throughput, ns/op):**

| Instruction | ns/op | ops/ns | vs `ldr` |
|---|---|---|---|
| `nop` | 0.0459 | 21.79 | — |
| **`ldr`** | **0.1135** | **8.81** | **1.00×** |
| `ldapr` (RCpc acquire) | 0.2532 | 3.95 | **2.23×** |
| `ldapur` (LRCPC2, unscaled) | 0.2569 | 3.89 | 2.26× |
| `ldar` (RCsc acquire) | 0.2560 | 3.91 | 2.25× |
| **`str`** | **0.1489** | **6.71** | **1.31×** |
| `stlr` (release) | 0.3043 | 3.29 | 2.68× (**2.04× vs `str`**) |
| `dmb ishst` | 0.4505 | 2.22 | 3.97× |
| `dmb ish` | 0.4561 | 2.19 | 4.02× |

**Two findings of note:**

1. **`LDAPR` is NOT cheaper than `LDAR` on Apple silicon.** 0.2532 vs 0.2560 ns — a 1% difference, i.e. identical within noise. This **independently confirms** Wrenger et al.'s observation that "acquire-release operations do not show improved performance on the Apple M1, even with newer compilers emitting the supposedly more optimized `LDAPR` instructions." Confidence: **High** — two independent methods, two different chips (M1, M2 Max).
   **Consequence: on Apple silicon, `FEAT_LRCPC` buys you nothing over plain `LDAR`.** Do not architect around it.
2. **`LDAPUR` (LRCPC2) costs the same as `LDAPR` on Apple** — so LRCPC2's value here is purely its addressing mode (saving an `ADD`), not cheaper ordering. Notably, Apple is *unaffected* by the Cortex/Neoverse erratum **3877900** that makes `LDAPUR` execute with full acquire ordering on ARM's own cores.

**DRAM memory-level parallelism** (512 MB array, 8 independent loads in flight):

| Instruction | ns/load | vs `ldr` |
|---|---|---|
| `ldr` | 3.609 | 1.00× |
| `ldapr` | 4.110 | 1.14× |
| `ldar` | 4.033 | 1.12× |

**This refutes a hypothesis I held going in.** I expected acquire loads to destroy memory-level parallelism and cost far more than 9% on DRAM-bound code. They cost only **12–14%**. Apple's cores evidently issue acquire loads speculatively and replay on violation, preserving most MLP. (VERIFIED-LOCAL; **Medium-High** confidence.)

**Emulation-shaped codegen** — 2 loads + 1 store + ALU per guest operation, L1-resident, the realistic shape of translated x86:

| Codegen strategy | Relative time |
|---|---|
| `ldr`/`str` (hardware TSO, or native ARM) | **1.00×** |
| `ldapr`/`stlr` (RCpc software TSO) | **3.43×** |
| `ldar`/`stlr` (RCsc software TSO) | **3.44×** |

**This is the headline number for the feasibility question: software TSO via acquire/release costs ~3.4× on L1-resident emulated code on an M2 Max.** (VERIFIED-LOCAL, **Medium-High**.) It sits between FEX's "near 10×" (which includes vector and unaligned handling I did not model) and the ~1.3–5.4× range AtoMig measured for naive implicit-barrier native code on a Kunpeng 920.

*Caveat:* my `dmb`-everywhere variant measured only 1.49×, i.e. *faster* than acquire/release, which is not credible — back-to-back `DMB`s with nothing outstanding coalesce, so that microbenchmark does not model real barrier cost. **I am discarding that datapoint** rather than reporting a number I do not believe.

**For reference, Dougall Johnson's Firestorm (M1 P-core) tables** — note these lack `LDAR`/`LDAPR`/`STLR`, which is exactly the gap my measurements fill:

| Instruction | Firestorm rTP (cycles) | Icestorm rTP |
|---|---|---|
| `LDR` | 0.333 | 0.5 |
| `STR` | 0.5 | 1 |
| `STLRB`/`STLRH` | 1 | **6** |
| `DMB` (any) | ~2.91 | 4 |
| `DSB` | **17** | 16 |
| `ISB` | **28** | 25 |

(VERIFIED-PRIMARY — [firestorm-int](https://dougallj.github.io/applecpu/firestorm-int.html), [icestorm-int](https://dougallj.github.io/applecpu/icestorm-int.html))

Note `DSB` = **17 cycles** on Firestorm. §4.2 showed XNU executes `DSB SY` **twice per kernel round-trip** for TSO threads. That is ~34 cycles of pure barrier per syscall, plus two `ISB`s.

### 6.4 FEX's actual configuration knobs (corrects a common stale reference)

Verified from FEX source (`FEXCore/Source/Interface/Config/Config.json.in`):

| Option | Default | Notes |
|---|---|---|
| `TSOEnabled` | **true** | "Highly likely to break any multithreaded application if disabled." |
| `HalfBarrierTSOEnabled` | **true** | backpatch unaligned to half-barrier atomics |
| `VectorTSOEnabled` | **false** | disabled by default since FEX-2406 — "cost … too high for most hardware" |
| `MemcpySetTSOEnabled` | **false** | same |

**`ParanoidTSO` no longer exists in FEX** — it is a historical option. Confidence: **High**. Treat any current reference to it (including in the original research brief) as stale.

FEX's selection priority: **Hardware TSO → LRCPC3 → LRCPC2 → LRCPC → Atomics**. Hardware TSO is requested via `prctl(PR_SET_MEM_MODEL, PR_SET_MEM_MODEL_TSO, 0,0,0)`.

### 6.5 Rosetta 2 overall, and the "22%" trap

A frequently-cited "22%" figure needs care. Its origin is Beck et al. (AtoMig, ASPLOS'23): Geekbench 5.4.1 on the same M1, **x86-64 under Rosetta 2 scored ~22% lower than the native AArch64 build**. That is **total translation overhead, not the cost of TSO mode**. ASPLOS'26 miscites it as a TSO-mode cost. Confidence: **High** that the 22% is Rosetta-vs-native.

**No source attributes a specific fraction of Rosetta 2's performance to hardware TSO.** Any such number is **Unverified**.

---

## 7. Architectural substitutes when TSO is unavailable

Available on this M2 Max (VERIFIED-LOCAL, `sysctl hw.optional.arm`):

```
hw.optional.arm.FEAT_LRCPC:  1
hw.optional.arm.FEAT_LRCPC2: 1
hw.optional.arm.FEAT_LSE:    1
hw.optional.arm.FEAT_LSE2:   1
```

`FEAT_LRCPC3` is **not** exposed (absent from the sysctl list entirely).

| Feature | What it gives you | Relevance / measured value on Apple |
|---|---|---|
| **`FEAT_LRCPC`** (`LDAPR`) | RCpc acquire: architecturally weaker than `LDAR`'s RCsc — does **not** order against prior `STLR`. | Semantically the right primitive for x86 loads. **But on Apple it is NOT faster than `LDAR` — measured identical (§6.3).** Value is correctness/semantics only. **High** |
| **`FEAT_LRCPC2`** | `LDAPUR`/`STLUR` — RCpc with **unscaled immediate offsets** (range −256…255) | Saves an address-computation instruction per access. **Measured same throughput as `LDAPR` on M2 Max** — so the win is purely instruction count. Apple is unaffected by ARM erratum 3877900. **High** |
| **`FEAT_LRCPC3`** | RCpc for pair loads, SIMD/FP, more forms | Would fix the expensive case (vector guest ops). **Not on M2 Max**, and as of FEX-2404 "**no hardware today supports this extension**." This absence is why FEX ships `VectorTSOEnabled=false`. **High** |
| **`FEAT_LSE`/`LSE2`** | Atomics; single-copy atomicity for unaligned/16-byte accesses | **Orthogonal to ordering but mandatory** — x86 guarantees atomicity for unaligned accesses within a cache line; plain ARM does not. Needed regardless of TSO. Both present on M2 Max. **High** |

### 7.1 The key negative result

**`LDAPR` gives no performance benefit over `LDAR` on Apple silicon.** Confirmed twice independently:

- Wrenger et al. (M1): "acquire-release operations **do not show improved performance on the Apple M1**, even with newer compilers emitting the supposedly more optimized `LDAPR` instructions." (VERIFIED-PRIMARY)
- My measurement (M2 Max): `ldapr` 0.2532 ns/op vs `ldar` 0.2560 ns/op — identical within noise. (VERIFIED-LOCAL)

Confidence: **High**. **Do not architect a fallback around `LDAPR` being cheap on Apple hardware — it is not.** Its value is that it is *semantically correct* for x86 loads, not that it is fast.

Wrenger et al. draw the obvious conclusion, which is also the answer to "why did Apple bother building the bit":

> "Instructions with acquire-release semantics are **significantly slower on the M1 than both WO and TSO**, which explains Apple's decision to implement TSO for the x86 emulation instead of relying on these instructions."

### 7.2 Practical fallback stack

**INFERENCE (Medium-High):** for a native macOS emulator with no access to hardware TSO:

- Guest loads → `LDAPR` / `LDAPUR` (semantically correct, ~2.25× a plain `LDR`)
- Guest stores → `STLR` / `STLUR` (~2.0× a plain `STR`)
- Guest atomics → `LSE` (`LDADD`/`CAS`/`SWP`), `LSE2` for unaligned atomicity
- `DMB` only where genuinely required (guest `MFENCE`, `LOCK`-prefixed ops) — never as a blanket policy
- Vector/pair guest ops → the expensive gap; FEX's answer is to *not* make them atomic by default (`VectorTSOEnabled=false`) and accept the correctness risk

Expect roughly **3.4× on memory-op-dominated translated code** (§6.3), less on ALU-heavy code. This is the path FEX and box64 take on all non-Apple ARM hardware, so it is proven viable — just materially slower.

### 7.3 Non-Apple ARM: a cautionary note

If portability matters, note that ARM's own cores have made `FEAT_LRCPC*` much less useful than the spec implies:

- **Erratum 3877900** (Neoverse V2/V3/V3AE, Cortex-X3/X4/X925, C1-Ultra/C1-Premium): `LDAPUR`/`LDAPURB`/`LDAPURH` "**execute with full Load-Acquire ordering** instead of the relaxed ordering described in the LDAPUR pseudocode." FEX disables LRCPC2 on these parts. (VERIFIED-PRIMARY — FEX `HostFeatures.cpp`, [LLVM PR #124274](https://github.com/llvm/llvm-project/pull/124274))
- **Cortex-A78AE erratum 1951502** (NVIDIA Orin): workaround injects a `DMB ST` before every acquire atomic, which "**basically deletes any advantage that the FEAT_LRCPC extension gives FEX**." Measured effect: 18 FPS vs 60 FPS on near-identical clocks. ([FEX-2406](https://fex-emu.com/FEX-2406/))
- FEX-2306 generally: "Sadly ARM Cortex's hardware **LRCPC implementation is barely any faster than atomics**."

**Apple silicon is, ironically, the cleanest LRCPC implementation available — it just happens to also be the only one with a TSO bit that makes LRCPC unnecessary.**

---

## 8. Open questions

1. **What do ACTLR bits 4/5/6 (M1/M2) and bit 4 (M3+) do?** Apple sets them for Rosetta but forbids you from setting them via Hypervisor.framework. Why did the set shrink at M3? **Unverified.**
2. **Do E-cores implement `EnTSO`?** If not, TSO costs you the efficiency cluster. **Unverified.**
3. **Are `LDNP`/`STNP` and vector accesses covered by TSO?** Correctness-critical for an emulator. **Unverified.**
4. **Is `LDAPR` measurably cheaper than `LDAR` on Apple cores?** Determines the fallback path's viability. **Unverified — benchmark it.**
5. **Real litmus validation of TSO-in-VM** with `litmus7`/`herd7` rather than my hand-rolled loop.
6. **Exact `DSB SY` cost** on kernel entry/exit for TSO threads, and its impact on syscall-heavy emulated workloads.

---

## 9. Reproduction

All experiments in §5 were run from `/private/tmp/.../scratchpad/hvtso`. To reproduce:

1. Compile against `-framework Hypervisor`.
2. Sign with `codesign -s - --entitlements ent.plist -f <binary>` where `ent.plist` grants `com.apple.security.hypervisor`. (Ad-hoc signing suffices; no paid provisioning needed.)
3. `mmap` guest memory **without** `PROT_EXEC` (hardened runtime blocks `MAP_ANON|PROT_EXEC`); `hv_vm_map` supplies guest-side execute permission.
4. Kernel disassembly: scan `/System/Library/Kernels/kernel.release.t*` for `msr actlr_el1, xR` = `0xd5181020 | Rt`.

---

## References

**Apple primary sources**
- [Accelerating the performance of Rosetta — Apple Developer](https://developer.apple.com/documentation/virtualization/accelerating-the-performance-of-rosetta) — `ACTLR.TSOEN`, `RosettaPatch.diff`, `prctl` usage
- [Running Intel Binaries in Linux VMs with Rosetta — Apple Developer](https://developer.apple.com/documentation/Virtualization/running-intel-binaries-in-linux-vms-with-rosetta)
- [`hv_sys_reg_t` — Apple Developer](https://developer.apple.com/documentation/hypervisor/hv_sys_reg_t)
- Local SDK: `/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/System/Library/Frameworks/Hypervisor.framework/Headers/hv_vcpu_types.h`
- Local kernels: `/System/Library/Kernels/kernel.release.t{8103,6000,8112,6020,8122,6030,6031,8132,6041,6050,8140,8142}`

**Linux kernel**
- [marcan, "arm64: Support the TSO memory model" (patchew)](https://patchew.org/linux/20240411-tso-v1-0-754f11abfbff@marcan.st/)
- [Patch 4/4: Implement Apple IMPDEF TSO memory model control](https://lkml.rescloud.iu.edu/2404.1/04830.html) — `ACTLR_APPLE_TSO BIT(1)`, `AIDR_APPLE_TSO BIT(9)`
- [lore.kernel.org thread](https://lore.kernel.org/lkml/CAEg-Je8Y8+n5N-vLh0icELgWm+L1eRchqxX28Hg_mi9Bam+xRA@mail.gmail.com/T/)
- [Zayd Qumsieh (Apple), "tso: aarch64: Expose TSO for virtualized linux on Apple Silicon"](https://patchew.org/linux/20240410211652.16640-1-zayd._5Fqumsieh@apple.com/) — macOS 14.4 requirement
- [Apple follow-up on lore](https://lore.kernel.org/lkml/20240416021100.87652-1-zayd_qumsieh@apple.com/)
- [LWN: Support for the TSO memory model on Arm CPUs](https://lwn.net/Articles/970907/) — ~9% figure, Deacon/Marinas objections
- [LWN: arm64: Support the TSO memory model](https://lwn.net/Articles/969484/)
- [LWN 971325](https://lwn.net/Articles/971325/)

**Reverse engineering**
- [marcan, "Apple TSO" gist](https://gist.github.com/marcan/9ab73ca0614864bea0eea9e953c074d3) — `ACTLR_ELx<1>`, `AIDR_EL1<9>`, `HCR_EL2.TACR`, `HACR_EL2<0>/<4>`, `ACTLR_EL12 = s3_6_c15_c14_6`
- [Saagar Jha, TSOEnabler](https://github.com/saagarjha/TSOEnabler) — kext, `kern.tso_enable`, P-core pinning claim
- [TSOEnabler.c](https://github.com/saagarjha/TSOEnabler/blob/master/TSOEnabler/TSOEnabler.c) — offset-discovery pattern matcher
- [enabletso.c](https://github.com/saagarjha/TSOEnabler/blob/master/enabletso/enabletso.c) — pthread interposer
- [TSOEnabler t8020 branch (A12Z)](https://github.com/saagarjha/TSOEnabler/tree/t8020)
- [Skryptonyte, "A poor attempt at trying to change TSO bits inside a Parallels VM"](https://gist.github.com/Skryptonyte/7eed38259850a0126a7a56353f780fc7) — **obsolete conclusion**, see §5.3
- [Dougall Johnson, Apple CPU instruction tables](https://dougallj.github.io/applecpu/firestorm.html)

**Academic**
- [Wrenger et al., "TOSTING: Investigating Total Store Ordering on ARM" (ARCS'23)](https://www.sra.uni-hannover.de/Publications/2023/tosting-arcs23/wrenger_23_arcs.pdf)
- [Wrenger et al., "Analyzing the memory ordering models of the Apple M1" (JSA 2024)](https://www.sra.uni-hannover.de/Publications/2024/wrenger_24_jsa.pdf)
- [Khayet et al., "Arm Weak Memory Consistency on Apple Silicon: What Is It Good For?" (ASPLOS 2026)](https://www.cs.tau.ac.il/~mad/publications/asplos2026-TSO.pdf)

**Emulators**
- [FEX 2306 release notes](https://fex-emu.com/FEX-2306/) — hardware TSO integration
- [FEX 2404](https://fex-emu.com/FEX-2404/), [FEX 2406](https://fex-emu.com/FEX-2406/)
- [cyyself/m1tso-linux](https://github.com/cyyself/m1tso-linux)
- [UTM issue #5460 — implementing what Rosetta 2 does](https://github.com/utmapp/UTM/issues/5460)
- [Inoki, "Things You Might Want to Know About Apple's Rosetta 2 for Linux VMs"](https://blog.inoki.cc/2026/02/28/Apple-Rosetta-Linux-VM-Secret-en/)
