#!/bin/sh
# run-stress.sh DIR PART: stress-ng (DIR/stress-ng, static) with --verify, every line of DIR/stress-list.txt whose
# first field is the family named by PART (PART = FAMILY[,seed=S][,n=N][,r=R][,scale=X][,timeout=T]; N instances per
# stressor, default nproc; R repeats of the family, default 1; X scales every OPS, round(OPS * X) and at least 1,
# default 1; T is stress-ng's --timeout, default 240; cal=S: calibration, no op limit and --timeout S). List lines:
# FAMILY STRESSOR[:LABEL] OPS [EXTRA-OPTIONS...] (LABEL `user`: run as nobody when root, for stressors that refuse
# root). Per stressor (outer kill at T + 160 s, 400 s by default):
#   stress-ng --STRESSOR N --STRESSOR-ops OPS*X --verify --seed S --timeout T --metrics-brief --temp-path TMP EXTRA...
# with `CT-LOG NAME | run-stress: ops=OPS*X timeout=T` and its output as `CT-LOG NAME | LINE` lines, then
#   CT-RESULT stress NAME.rI rc=EXIT n=N pass=#passed skip=#skipped fail=#fail-lines bogo=OPS-DONE sec=WALL to=T
# and at the end the kernel log as `CT-DMESG` lines (empty natively, where dmesg is restricted). Same script natively.
D=$1; fam=${2%%,*}; cal=; seed=1; n=$(nproc); r=1; scale=1; tmo=240
for kv in $(echo "$2" | tr , ' '); do case $kv in seed=*) seed=${kv#seed=};; n=*) n=${kv#n=};; r=*) r=${kv#r=};; cal=*) cal=${kv#cal=};;
    scale=*) scale=${kv#scale=};; timeout=*) tmo=${kv#timeout=};; esac; done
T=${TMPDIR:-/tmp}/ct-stress; i=0
while [ $i -lt "$r" ]; do
    i=$((i + 1))
    grep -v '^#' "$D/stress-list.txt" | while read -r f sl ops extra; do
        [ "$f" = "$fam" ] || continue
        s=${sl%%:*}   # STRESSOR[:LABEL]: the label names the result when one stressor is listed with several options
        pre=; [ "${sl#*:}" = user ] && [ "$(id -u)" = 0 ] && pre="setpriv --reuid=65534 --regid=65534 --clear-groups"
        ops=$(awk -v o="$ops" -v x="$scale" 'BEGIN { v = int(o * x + 0.5); print (v < 1 ? 1 : v) }')
        o="--$s-ops $ops"; to=$tmo; [ -n "$cal" ] && { o=; to=$cal; }
        echo "CT-LOG $sl | run-stress: ops=${o:+$ops} timeout=$to"
        rm -rf "$T"; mkdir -p "$T"; chmod 1777 "$T"; t0=$(date +%s)
        (cd "$T" && exec timeout -s KILL $((tmo + 160)) $pre "$D/stress-ng" --"$s" "$n" $o --verify --seed "$seed" \
            --timeout "$to" --metrics-brief --temp-path "$T" $extra) > "$T.out" 2>&1 < /dev/null; rc=$?
        sed "s/^/CT-LOG $sl | /" "$T.out"
        bogo=$(awk -v s="$s" '$2 == "metrc:" && $4 == s {print $5}' "$T.out" | head -1)
        pass=$(sed -n 's/.*\] passed: \([0-9]*\).*/\1/p' "$T.out" | head -1); skip=$(sed -n 's/.*\] skipped: \([0-9]*\).*/\1/p' "$T.out" | head -1)
        echo "CT-RESULT stress $sl.r$i rc=$rc n=$n pass=${pass:--} skip=${skip:--} fail=$(grep -c ' fail: ' "$T.out") bogo=${bogo:--} sec=$(($(date +%s) - t0)) to=$to"
    done
done
rm -rf "$T" "$T.out"
dmesg 2>/dev/null | sed 's/^/CT-DMESG /'
