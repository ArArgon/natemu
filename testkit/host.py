"""Facts about this machine, detected, never written down (spec R5.1): identity, chip, cores, macOS build, CPU features."""
import dataclasses
import functools
import re
import subprocess


@dataclasses.dataclass(frozen=True)
class Host:
    machine_id: str
    chip: str
    p_cores: int
    e_cores: int
    os_build: str
    features: dict


def _out(*cmd: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def parse_sysctl(text: str) -> dict:
    """`sysctl hw.optional.arm` lines → {'afp': False, ...}: the FEAT_ keys only, lowercased."""
    out = {}
    for line in text.splitlines():
        m = re.match(r'hw\.optional\.arm\.FEAT_(\w+):\s*(\d+)', line)
        if m: out[m[1].lower()] = m[2] != '0'
    return out


def detect() -> Host:
    """This machine. NATEMU_TEST_HOST_FEATURES="afp=0,sb=1" overrides detected features (the framework's own tests)."""
    h = _detect()
    if v := __import__('os').environ.get('NATEMU_TEST_HOST_FEATURES'):
        f = dict(h.features)
        for kv in v.split(','):
            k, _, x = kv.partition('='); f[k] = x not in ('0', 'false', '')
        h = dataclasses.replace(h, features=f)
    return h


@functools.cache
def _detect() -> Host:
    p, e, chip = _out('sysctl', '-n', 'hw.perflevel0.physicalcpu', 'hw.perflevel1.physicalcpu',
                      'machdep.cpu.brand_string').splitlines()[:3]
    uuid = re.search(r'"IOPlatformUUID" = "([0-9A-F-]{36})"', _out('ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'))[1]
    return Host(machine_id=uuid, chip=chip.strip(), p_cores=int(p), e_cores=int(e),
                os_build=_out('sw_vers', '-buildVersion').strip(), features=parse_sysctl(_out('sysctl', 'hw.optional.arm')))
