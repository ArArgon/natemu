#!/bin/sh
# run-cpy.sh DIR K[/N]: CPython's regression tests listed in DIR/cpy-tests.txt (lines `TEST PART [TAG...]`) whose PART or a
# TAG is K (every line when K is `all`), run by the build tree PY (default /home/ubuntu/cp/py): `python -m test -j NPROC -u subprocess
# --timeout 3600 -W --junit-xml`. No other resource is enabled (network, urlfetch, gui, audio, curses, largefile,
# extralargefile, cpu, decimal, tzdata, walltime: excluded). As root (the guest) the tests run as the user nobody, as
# natively they run as a normal user. Serial output:
#   CT-LOG | LINE          regrtest's output (its per-test lines and closing summary are the per-test results)
#   CT-CASE ID STATUS      a test case not passed (fail, error, skip), from the JUnit file
#   CT-RESULT cpy TEST cases=C pass=P fail=F error=E skip=S sec=T   per test module that reported cases (T: its cases' time)
D=$1; k=${2%/*}; PY=${PY:-/home/ubuntu/cp/py}; T=${TMPDIR:-/tmp}/cpy.$$; mkdir -p "$T"; chmod 777 "$T"
awk -v k="$k" '{for (i = 2; i <= NF; i++) if (k == "all" || $i == k) {print $1; next}}' "$D/cpy-tests.txt" > "$T/list"
run=; if [ "$(id -u)" = 0 ]; then
    mkdir -p "$T/home"; chown -R 65534:65534 "$PY" "$T"; run="setpriv --reuid=65534 --regid=65534 --clear-groups"; export HOME=$T/home
fi
export LC_ALL=C.UTF-8 TZ=UTC PYTHONHASHSEED=0
cd "$PY" && $run ./python -m test -j "$(nproc)" -u subprocess --timeout 3600 -W --junit-xml "$T/junit.xml" $(cat "$T/list") \
    < /dev/null > "$T/out" 2>&1
echo "CT-LOG rc=$?"
sed 's/^/CT-LOG | /' "$T/out"
[ -f "$T/junit.xml" ] && ./python -I -c '
import sys, xml.etree.ElementTree as ET
from collections import defaultdict
n = defaultdict(lambda: [0, 0, 0, 0, 0]); sec = defaultdict(float)
keys = sorted(((t if t.startswith("test.") else "test." + t) + ".", t) for t in open(sys.argv[2]).read().split())
for c in ET.parse(sys.argv[1]).iter("testcase"):
    i = c.get("name"); p = i.split(".")
    m = max(((len(k), t) for k, t in keys if i.startswith(k)), default=(0, p[1] if p[0] == "test" and len(p) > 1 else p[0]))[1]
    s = "error" if c.find("error") is not None else "fail" if c.find("failure") is not None else "skip" if c.find("skipped") is not None or c.get("status") == "skipped" else "pass"
    sec[m] += float(c.get("time") or 0); r = n[m]; r[0] += 1; r[("pass", "fail", "error", "skip").index(s) + 1] += 1
    if s != "pass": print("CT-CASE", i, s)
for m in sorted(n): print("CT-RESULT cpy %s cases=%d pass=%d fail=%d error=%d skip=%d sec=%.1f" % ((m,) + tuple(n[m]) + (sec[m],)))
' "$T/junit.xml" "$T/list"
rm -rf "$T"
