"""Decodes `probe` output (the Arm ID registers a Hypervisor.framework vCPU presents) into FEAT names and compares
them with this host's `sysctl hw.optional.arm`. The vCPU's view is what natemu's generated code runs on; sysctl
describes the host and can differ (cpu-features plan, appendix).

    sh tools/hvf_id_probe/run.sh
"""
import subprocess
import sys

R = {}
for line in sys.stdin:
    name, cfg, vcpu = line.split()
    R[name] = int(vcpu.split('=')[1], 16)


def f(r, lo):
    return R[r] >> lo & 15


VM = {  # name: (register, low bit, minimum value), per Arm DDI0487 ID_AA64* field definitions
    'FEAT_AES': ('ISAR0', 4, 1), 'FEAT_PMULL': ('ISAR0', 4, 2), 'FEAT_SHA1': ('ISAR0', 8, 1),
    'FEAT_SHA256': ('ISAR0', 12, 1), 'FEAT_SHA512': ('ISAR0', 12, 2), 'FEAT_CRC32': ('ISAR0', 16, 1),
    'FEAT_LSE': ('ISAR0', 20, 2), 'FEAT_RDM': ('ISAR0', 28, 1), 'FEAT_SHA3': ('ISAR0', 32, 1),
    'FEAT_DotProd': ('ISAR0', 44, 1), 'FEAT_FHM': ('ISAR0', 48, 1), 'FEAT_FlagM': ('ISAR0', 52, 1),
    'FEAT_FlagM2': ('ISAR0', 52, 2), 'FEAT_RNG': ('ISAR0', 60, 1),
    'FEAT_DPB': ('ISAR1', 0, 1), 'FEAT_DPB2': ('ISAR1', 0, 2), 'FEAT_JSCVT': ('ISAR1', 12, 1),
    'FEAT_FCMA': ('ISAR1', 16, 1), 'FEAT_LRCPC': ('ISAR1', 20, 1), 'FEAT_LRCPC2': ('ISAR1', 20, 2),
    'FEAT_FRINTTS': ('ISAR1', 32, 1), 'FEAT_SB': ('ISAR1', 36, 1), 'FEAT_SPECRES': ('ISAR1', 40, 1),
    'FEAT_BF16': ('ISAR1', 44, 1), 'FEAT_EBF16': ('ISAR1', 44, 2), 'FEAT_I8MM': ('ISAR1', 52, 1),
    'FEAT_ECV': ('MMFR0', 60, 1), 'FEAT_AFP': ('MMFR1', 44, 1), 'FEAT_LSE2': ('MMFR2', 32, 1),
    'FEAT_DIT': ('PFR0', 48, 1), 'FEAT_CSV2': ('PFR0', 56, 1), 'FEAT_CSV3': ('PFR0', 60, 1),
    'FEAT_BTI': ('PFR1', 0, 1), 'FEAT_SSBS': ('PFR1', 4, 1), 'FEAT_SME': ('PFR1', 24, 1),
}
host = {}
for line in subprocess.run(['sysctl', 'hw.optional.arm'], capture_output=True, text=True).stdout.splitlines():
    k, _, v = line.partition(': ')
    if v.strip().isdigit():
        host[k.removeprefix('hw.optional.arm.')] = int(v)
print(f"ASIDBits {f('MMFR0', 4)} PARange {f('MMFR0', 0)} SVE {f('PFR0', 32)}")
for name, (r, lo, m) in VM.items():
    vm, h = int(f(r, lo) >= m), host.get(name)
    print(f"{name:14} vcpu={vm} host={h}{'   <-- differs' if h is not None and h != vm else ''}")
