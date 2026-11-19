#!/bin/sh
# run-cpy.sh DIR K[/N]: CPython's regression tests listed in DIR/cpy-tests.txt (lines `TEST PART [TAG...]`) whose PART or a
# TAG is K (every line when K is `all`), run by the build tree PY (default /home/ubuntu/cp/py): `python -m test -j NPROC -u subprocess
# --timeout 3600 -W --junit-xml`. No other resource is enabled (network, urlfetch, gui, audio, curses, largefile,
# extralargefile, cpu, decimal, tzdata, walltime: excluded). As root (the guest) the tests run as the user nobody, as
# natively they run as a normal user. Serial output:
#   CT-LOG | LINE          regrtest's output (its per-test lines and closing summary are the per-test results)
#   CT-CASE ID STATUS      a test case not passed (fail, error, skip), from the JUnit file
#   CT-RESULT cpy TEST cases=C pass=P fail=F error=E skip=S sec=T   per test module that reported cases (T: its cases' time;
#                          DIR/cpy-junit.py says which module a case belongs to)
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
[ -f "$T/junit.xml" ] && ./python -I "$D/cpy-junit.py" "$T/junit.xml" "$T/list" "$T/out"   # CT-CASE, CT-RESULT lines
rm -rf "$T"
