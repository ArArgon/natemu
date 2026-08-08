#include <Hypervisor/hypervisor.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/mman.h>
#include <dlfcn.h>

// Helpers for Decode ID_AA64MMFR0 TGran fields per ARM ARM DDI0487
// ID_AA64MMFR0_EL1 layout (bits):
// [31:28] TGran4, [27:24] TGran64, [23:20] TGran16, [19:16] BigEnd etc
// Values for TGran* : 0b0000 = supported, 0b1111 = not supported (or 0b0001 for TGran4 not supported variant)
// For definitive decode we print raw field + interpretation.
static const char* tgran_decode(uint8_t v, int is_tgran4) {
    if (is_tgran4) {
        // TGran4: 0x0=supported, 0x1= not supported? Actually ARM defines 0x0=4K supported, 0xF= not supported? Check Linux
        // Linux: ID_AA64MMFR0_TGRAN4_SUPPORTED 0x0, NOT_SUPPORTED 0xF
        if (v == 0x0) return "SUPPORTED (4K granule supported)";
        if (v == 0xF) return "NOT SUPPORTED";
        if (v == 0x1) return "NOT SUPPORTED (alt encoding)";
        return "RESERVED/unknown";
    } else {
        if (v == 0x0) return "SUPPORTED";
        if (v == 0x1) return "NOT SUPPORTED"; // for TGran16/64 some encodings differ
        if (v == 0xF) return "NOT SUPPORTED";
        return "RESERVED";
    }
}

static void decode_mmfr0(uint64_t v) {
    uint8_t tgran4  = (v >> 28) & 0xF;
    uint8_t tgran64 = (v >> 24) & 0xF;
    uint8_t tgran16 = (v >> 20) & 0xF;
    // also check TGran4_2, TGran64_2? No on this reg
    printf("  ID_AA64MMFR0_EL1 = 0x%016llx\n", (unsigned long long)v);
    printf("    TGran4  [31:28] = 0x%x (%s)\n", tgran4, tgran_decode(tgran4,1));
    printf("    TGran64 [27:24] = 0x%x (%s)\n", tgran64, tgran_decode(tgran64,0));
    printf("    TGran16 [23:20] = 0x%x (%s)\n", tgran16, tgran_decode(tgran16,0));
    printf("    PARange [3:0]   = 0x%x (%d bits, %d GB)\n", (int)(v & 0xF), ((int)(v & 0xF) ? 32+(int)(v & 0xF)*0 : 0), 0);
    // PARange decode per ARM: 0=32, 1=36, 2=40, 3=42, 4=44, 5=48, 6=52, 7=56? but we just print raw
}

int main(){
    printf("=== G-1 Hypervisor.framework guest EL1 probe ===\n");
    printf("Host: macOS 26.5.2, SDK Hypervisor 259.5.15, hw.pagesize=%d\n", (int)getpagesize());
    // SDK granule API check
    printf("\n--- SDK header / IPA granule config ---\n");
    // Use dlsym to avoid hard failure if symbol missing on older OS
    void *handle = dlopen("/System/Library/Frameworks/Hypervisor.framework/Hypervisor", RTLD_NOW);
    hv_return_t (*p_get_default_granule)(hv_ipa_granule_t *) = NULL;
    hv_return_t (*p_set_granule)(hv_vm_config_t, hv_ipa_granule_t) = NULL;
    hv_return_t (*p_get_granule)(hv_vm_config_t, hv_ipa_granule_t *) = NULL;
    if (handle) {
        p_get_default_granule = dlsym(handle, "hv_vm_config_get_default_ipa_granule");
        p_set_granule = dlsym(handle, "hv_vm_config_set_ipa_granule");
        p_get_granule = dlsym(handle, "hv_vm_config_get_ipa_granule");
    }
    printf("hv_vm_config_get_default_ipa_granule symbol: %s\n", p_get_default_granule ? "FOUND (macOS 26+)" : "NOT FOUND (pre-26 SDK/OS)");
    printf("hv_vm_config_set_ipa_granule symbol: %s\n", p_set_granule ? "FOUND" : "NOT FOUND");
    if (p_get_default_granule) {
        hv_ipa_granule_t g;
        hv_return_t r = p_get_default_granule(&g);
        printf(" hv_vm_config_get_default_ipa_granule() -> 0x%x ret=0x%x\n", (unsigned)g, r);
        if (r==0) printf("   default granule = %s (%d)\n", g==0?"HV_IPA_GRANULE_4KB":"HV_IPA_GRANULE_16KB", (int)g);
    }
    uint32_t max_ipa=0, def_ipa=0;
    hv_return_t r;
    r = hv_vm_config_get_max_ipa_size(&max_ipa);
    printf(" hv_vm_config_get_max_ipa_size -> %u bits ret=0x%x\n", max_ipa, r);
    r = hv_vm_config_get_default_ipa_size(&def_ipa);
    printf(" hv_vm_config_get_default_ipa_size -> %u bits ret=0x%x\n", def_ipa, r);

    // Test config creation with 4K vs 16K
    if (p_set_granule && p_get_granule) {
        hv_vm_config_t cfg = hv_vm_config_create();
        if (cfg) {
            hv_ipa_granule_t cur;
            r = p_get_granule(cfg, &cur);
            printf("  new config default granule -> %d ret=0x%x\n",(int)cur,r);
            r = p_set_granule(cfg, HV_IPA_GRANULE_4KB);
            printf("  set 4K granule -> ret=0x%x %s\n", r, r==0?"OK":"FAIL");
            if (r==0) {
                r = p_get_granule(cfg, &cur);
                printf("   verify -> %d ret=0x%x\n",(int)cur,r);
            }
            r = p_set_granule(cfg, HV_IPA_GRANULE_16KB);
            printf("  set 16K granule -> ret=0x%x %s\n", r, r==0?"OK":"FAIL");
            // Try creating VM with 4K granule to see if hv_vm_create accepts it
            // Note: must destroy any existing VM first; we haven't created one yet
            hv_vm_config_t cfg4 = hv_vm_config_create();
            p_set_granule(cfg4, HV_IPA_GRANULE_4KB);
            r = hv_vm_create(cfg4);
            printf("  hv_vm_create with 4K granule -> ret=0x%x %s\n", r, r==0?"OK":"FAIL");
            if (r==0) hv_vm_destroy();
            // restore default
            // need to release config? os_release
            // Use os_release if available
        }
    }

    printf("\n--- Host ACTLR_EL1 path ---\n");
    hv_vm_config_t cfg = NULL; // default
    // Try with default config
    r = hv_vm_create(cfg);
    printf("hv_vm_create(NULL) -> 0x%x %s\n", r, r==0?"HV_SUCCESS": (r==0xfae94007?"HV_DENIED (missing entitlement)": "FAIL"));
    if (r != 0) {
        printf("Cannot proceed without VM. This is expected if not signed with com.apple.security.hypervisor.\n");
        printf("Entitlement check: codesign -d --entitlements - ./probe 2>&1 | grep hypervisor\n");
        // Still try to explain expected values
        goto expect;
    }

    hv_vcpu_t vcpu;
    hv_vcpu_exit_t *exitp;
    r = hv_vcpu_create(&vcpu, &exitp, NULL);
    printf("hv_vcpu_create -> 0x%x\n", r);
    if (r!=0) { hv_vm_destroy(); goto expect; }

    uint64_t val=0;
    r = hv_vcpu_get_sys_reg(vcpu, HV_SYS_REG_ACTLR_EL1, &val);
    printf(" get ACTLR_EL1 -> r=0x%x val=0x%llx (EnTSO=%llu)\n", r, (unsigned long long)val, (unsigned long long)(val & 2)>>1);
    r = hv_vcpu_set_sys_reg(vcpu, HV_SYS_REG_ACTLR_EL1, 2);
    printf(" set ACTLR_EL1=2 -> r=0x%x %s\n", r, r==0?"OK":"FAIL");
    r = hv_vcpu_get_sys_reg(vcpu, HV_SYS_REG_ACTLR_EL1, &val);
    printf(" re-get -> r=0x%x val=0x%llx (EnTSO=%llu)\n", r, (unsigned long long)val, (unsigned long long)(val & 2)>>1);
    r = hv_vcpu_set_sys_reg(vcpu, HV_SYS_REG_ACTLR_EL1, 0xffffffffULL);
    printf(" set ACTLR_EL1=0xffffffff -> r=0x%x %s (expected HV_BAD_ARGUMENT 0xfae94003)\n", r, r==0xfae94003?"HV_BAD_ARGUMENT as expected":"");
    r = hv_vcpu_get_sys_reg(vcpu, HV_SYS_REG_ACTLR_EL1, &val);
    printf(" re-get after bad -> val=0x%llx\n", (unsigned long long)val);
    // reset to 0 for guest test
    hv_vcpu_set_sys_reg(vcpu, HV_SYS_REG_ACTLR_EL1, 0);
    r = hv_vcpu_get_sys_reg(vcpu, HV_SYS_REG_ACTLR_EL1, &val);
    printf(" reset to 0 -> val=0x%llx\n", (unsigned long long)val);

    printf("\n--- Guest EL1 self-service path ---\n");
    // Map guest memory
    size_t pagesz = (size_t)getpagesize(); // 16384
    size_t memsz = pagesz;
    void *host_mem = mmap(NULL, memsz, PROT_READ|PROT_WRITE, MAP_ANON|MAP_PRIVATE, -1, 0);
    if (host_mem==MAP_FAILED) { perror("mmap"); hv_vcpu_destroy(vcpu); hv_vm_destroy(); return 1; }
    printf(" mmap host_mem=%p size=%zu pagesz=%zu\n", host_mem, memsz, pagesz);
    // Build guest code at IPA 0x40000000 (like docs) or 0x0? Use 0x0 for simplicity with MMU off
    // But to avoid null IPA issues, use 0x400000
    hv_ipa_t guest_ipa = 0x400000;
    // Must be page aligned - it is
    r = hv_vm_map(host_mem, guest_ipa, memsz, HV_MEMORY_READ|HV_MEMORY_WRITE|HV_MEMORY_EXEC);
    printf(" hv_vm_map -> 0x%x\n", r);
    if (r!=0) { munmap(host_mem, memsz); hv_vcpu_destroy(vcpu); hv_vm_destroy(); return 1; }

    // Guest code: see encoding dump above
    // We will place code at offset 0 in host_mem, and set PC to guest_ipa
    uint32_t *code = (uint32_t*)host_mem;
    int idx=0;
    // mrs x0, ID_AA64MMFR0_EL1 -> d5380700
    code[idx++]=0xd5380700; // x0 = MMFR0
    // mrs x1, ID_AA64MMFR1_EL1 -> d5380721
    code[idx++]=0xd5380721; // x1 = MMFR1
    // mrs x2, ACTLR_EL1 before -> d5381022
    code[idx++]=0xd5381022; // x2 = ACTLR before
    // mov x3, #2 -> d2800043? Wait mov x3 #2 is d2800043 (rd=3, imm 2)
    code[idx++]=0xd2800043; // x3 = 2
    // msr ACTLR_EL1, x3 -> d5181023? Check encoding: msr ACTLR_EL1, x3 = d5181023 (rt=3)
    code[idx++]=0xd5181023;
    // isb -> d5033fdf
    code[idx++]=0xd5033fdf;
    // mrs x3, ACTLR_EL1 after -> d5381023 (overwrites x3 with readback)
    code[idx++]=0xd5381023;
    // mrs x4, AIDR_EL1 -> d53900e4 (rt=4)
    code[idx++]=0xd53900e4;
    // mrs x5, MIDR_EL1 -> d5380005 (rt=5)
    code[idx++]=0xd5380005;
    // mrs x6, MPIDR_EL1 -> d53800a6 (rt=6)
    code[idx++]=0xd53800a6;
    // mov x7, #0 -> d2800007
    code[idx++]=0xd2800007;
    // msr ACTLR_EL1, x7 -> d5181027
    code[idx++]=0xd5181027;
    // isb
    code[idx++]=0xd5033fdf;
    // mrs x7, ACTLR_EL1 -> d5381027 (read after clear)
    code[idx++]=0xd5381027;
    // hvc #0 -> d4000002
    code[idx++]=0xd4000002;
    // padding
    printf(" guest code %d insns at IPA 0x%llx\n", idx, (unsigned long long)guest_ipa);
    for(int i=0;i<idx;i++) printf("  [%d] 0x%08x\n", i, code[i]);
    // Ensure visible
    __builtin___clear_cache((char*)host_mem, (char*)host_mem+memsz);

    hv_vcpu_set_reg(vcpu, HV_REG_PC, guest_ipa);
    hv_vcpu_set_reg(vcpu, HV_REG_CPSR, 0x3c5); // EL1h DAIF all masked

    printf(" Running guest at EL1...\n");
    r = hv_vcpu_run(vcpu);
    printf(" hv_vcpu_run -> 0x%x\n", r);
    printf(" exit reason=%u syndrome=0x%llx FAR=0x%llx IPA=0x%llx\n",
        exitp->reason, (unsigned long long)exitp->exception.syndrome,
        (unsigned long long)exitp->exception.virtual_address,
        (unsigned long long)exitp->exception.physical_address);
    uint32_t ec = (exitp->exception.syndrome >> 26) & 0x3f;
    printf("  EC=0x%x (%s) ISS=0x%x\n", ec, ec==0x16?"HVC": ec==0x18?"MSR/MRS trap": ec==0x24?"Data abort":"other", (unsigned)(exitp->exception.syndrome & 0x1ffffff));
    if (exitp->reason==1 && ec==0x16) printf("  GUEST RAN OK (no trap on ACTLR/AIDR, reached HVC)\n");
    else printf("  GUEST TRAPPED UNEXPECTEDLY - indicates hypervisor traps ACTLR/AIDR\n");

    uint64_t regs[8];
    for(int i=0;i<8;i++){
        hv_vcpu_get_reg(vcpu, HV_REG_X0+i, &regs[i]);
        printf("  X%d = 0x%016llx\n", i, (unsigned long long)regs[i]);
    }
    uint64_t pc,cpsr;
    hv_vcpu_get_reg(vcpu, HV_REG_PC, &pc);
    hv_vcpu_get_reg(vcpu, HV_REG_CPSR, &cpsr);
    printf("  PC=0x%llx CPSR=0x%llx\n", (unsigned long long)pc, (unsigned long long)cpsr);

    // Decode
    printf("\n--- Decode ---\n");
    decode_mmfr0(regs[0]);
    printf("  ID_AA64MMFR1_EL1 = 0x%016llx (X1)\n", (unsigned long long)regs[1]);
    printf("  ACTLR_EL1 before guest write (X2) = 0x%llx EnTSO=%llu\n", (unsigned long long)regs[2], (unsigned long long)(regs[2]&2)>>1);
    printf("  ACTLR_EL1 after guest msr 2 (X3) = 0x%llx EnTSO=%llu %s\n", (unsigned long long)regs[3], (unsigned long long)(regs[3]&2)>>1, (regs[3]==2?"PASS guest self-service works":"FAIL"));
    printf("  AIDR_EL1 (X4) = 0x%016llx bit9 TSO-capable=%llu\n", (unsigned long long)regs[4], (unsigned long long)(regs[4]>>9)&1);
    printf("  MIDR_EL1 (X5) = 0x%016llx implementer=0x%x part=0x%x\n", (unsigned long long)regs[5], (unsigned)((regs[5]>>24)&0xff), (unsigned)((regs[5]>>4)&0xfff));
    printf("  MPIDR_EL1 (X6)= 0x%016llx\n", (unsigned long long)regs[6]);
    printf("  ACTLR_EL1 after guest clear (X7)= 0x%llx EnTSO=%llu\n", (unsigned long long)regs[7], (unsigned long long)(regs[7]&2)>>1);

    // Host coherence check
    uint64_t host_actlr=0;
    hv_vcpu_get_sys_reg(vcpu, HV_SYS_REG_ACTLR_EL1, &host_actlr);
    printf("  host hv_vcpu_get_sys_reg(ACTLR_EL1) after guest = 0x%llx (should match X7)\n", (unsigned long long)host_actlr);

    // Second test: host set to 2, guest reads it
    printf("\n--- Coherence: host set -> guest read ---\n");
    hv_vcpu_set_sys_reg(vcpu, HV_SYS_REG_ACTLR_EL1, 2);
    // New guest snippet: just read ACTLR and HVC
    // Reuse same memory but overwrite with simpler code
    idx=0;
    code[idx++]=0xd5381020; // mrs x0, actlr_el1
    code[idx++]=0xd4000002; // hvc #0
    __builtin___clear_cache((char*)host_mem, (char*)host_mem+memsz);
    hv_vcpu_set_reg(vcpu, HV_REG_PC, guest_ipa);
    hv_vcpu_set_reg(vcpu, HV_REG_CPSR, 0x3c5);
    r = hv_vcpu_run(vcpu);
    printf(" hv_vcpu_run -> 0x%x reason %u EC 0x%x\n", r, exitp->reason, (unsigned)((exitp->exception.syndrome>>26)&0x3f));
    uint64_t x0;
    hv_vcpu_get_reg(vcpu, HV_REG_X0, &x0);
    printf(" guest read ACTLR after host set 2 -> X0=0x%llx %s\n", (unsigned long long)x0, x0==2?"PASS coherent":"FAIL");

    hv_vcpu_destroy(vcpu);
    hv_vm_destroy();
    munmap(host_mem, memsz);
    printf("\n=== Probe complete ===\n");
    return 0;

expect:
    printf("\n--- Expected values (from arm-cpusysregs + existing measurements) ---\n");
    printf(" If run succeeds you would see (M2 Pro/M2 Max example from 01-apple-tso.md):\n");
    printf("  ID_AA64MMFR0: TGran4=0x0 SUPPORTED, TGran16=0x1 ? TGran64=0x0 etc - but need real measurement\n");
    printf("  AIDR_EL1 = 0x6afd5797 (bit9=1 TSO capable) on M2 Max guest sample in 01 §5.3\n");
    printf("  ACTLR before 0x0, after guest msr 2 -> 0x2 EnTSO=1 (no trap)\n");
    printf("  MIDR_EL1 = 0x610f0000 (VirtualApple, implementer 0x61)\n");
    printf(" Summary for G-1 requires live run to confirm TGran4 inside VM.\n");
    // Still query dlsym results already printed
    return 0;
}
