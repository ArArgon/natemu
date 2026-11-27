#!/bin/bash
# de-bench.service (the image's unit; root, its output on the console, ttyS0): the desktop profile's workload, as
# tests/bench/run_desktop.py describes. It waits for the Plasma session the autologin on tty1 starts, then runs the
# phases the kernel command line's de.phases names, in this order:
#   idle      20 s of the idle desktop
#   ui        the app launcher opened and closed, the Overview opened and closed, the next virtual desktop and back
#   apps      Konsole and Dolphin: each started (first start), closed, started again (second start); with both open the
#             front window moved, grown, maximized and restored, and the windows switched; then both closed
#   click     Konsole and Dolphin started as a user does, through real input (natemu's PS/2 keyboard, driven by the runner
#             from DE-INPUT lines): the Meta key opens the launcher, the app's name is typed, Enter starts it (Plasma's
#             launcher, KIO's ApplicationLauncherJob, systemd's transient app unit); each first and second start, with the
#             marks between: the launcher's D-Bus call to systemd (StartTransientUnit), the unit's start in the journal,
#             the process's creation, its first window
#   browsers  Chromium and Firefox ESR on about:blank: each first start, closed, second start, closed
#   speedometer  Speedometer 3.1 in Chromium (de.spiter iterations), its results printed by serve.py ("SP /done ...")
# Every line it prints starts with DE- and ends with the guest's uptime (t=SECONDS): BEGIN NAME / END NAME around each
# measured window, MAPPED (an app's first window, at KWin's own time of the window's arrival, at=), SETTLED (the CPUs
# quiet again after a start), SNAP NAME (the runner takes a PNG of the display then), STAT (the guest's own counters:
# CPU jiffies, page faults, the disk's I/O, available memory), PS (each process's CPU jiffies and faults) and INFO
# (what KWin and the browsers render with). de.guestperf=1: the guest's perf samples every CPU at 199 Hz from the
# first phase after idle to the last, and its reports (by process and object, by symbol) are printed at the end.
[ -n "$BASH_VERSION" ] || exec /bin/bash "$0" "$@"     # the unit starts it with /bin/sh
H=/run/de-harness
RUN=/run/user/1000
WINLOG=/run/de-win.log
arg() { tr ' ' '\n' < /proc/cmdline | sed -n "s/^$1=//p" | head -1; }
PHASES=",$(arg de.phases),"
GPERF=$(arg de.guestperf)
SPITER=$(arg de.spiter); SPITER=${SPITER:-5}
has() { case $PHASES in *,$1,*) return 0;; esac; return 1; }
up() { local u; read -r u _ < /proc/uptime; echo "$u"; }
UP0=$(up); RT0=$(date +%s.%N)
ms2up() { awk "BEGIN{printf \"%.3f\", $1 / 1000 - ($RT0 - $UP0)}"; }
say() { echo "DE-$* t=$(up)"; }
busy() { local c u n s i w q sq st; read -r c u n s i w q sq st _ < /proc/stat; echo $((u + n + s + q + sq + st)); }
counters() {
  local c u n s i w q sq st vm dk ma
  read -r c u n s i w q sq st _ < /proc/stat
  vm=$(awk '/^(pgfault|pgmajfault|pswpin|pswpout|pgpgin|pgpgout) /{printf "%s=%s ", $1, $2}' /proc/vmstat)
  dk=$(awk '$3=="vda"{printf "rd=%s rsect=%s rms=%s wr=%s wsect=%s wms=%s ioms=%s", $4, $6, $7, $8, $10, $11, $13}' /proc/diskstats)
  ma=$(awk '/^MemAvailable/{print $2}' /proc/meminfo)
  echo "DE-STAT $1 user=$u nice=$n system=$s idle=$i iowait=$w irq=$q softirq=$sq steal=$st $vm$dk memavail=$ma t=$(up)"
}
ps_cpu() {   # every process with CPU time: pid, name (Chromium's and Firefox's with their kind), jiffies, faults, and its
             # threads' times on a CPU (schedstat's third field: each one a switch to it)
  local label=$1 f s pid c r k sw t n
  echo "DE-PS-BEGIN $label t=$(up)"
  for f in /proc/[0-9]*/stat; do
    read -r s 2>/dev/null < "$f" || continue
    pid=${s%% *}; c=${s#*(}; c=${c%)*}; r=${s##*) }
    set -- $r
    [ $((${12} + ${13})) -gt 0 ] || continue
    k=
    # Chromium rewrites its children's command lines into one string: the kind is its --type=; Firefox's children
    # carry theirs as the last argument (tab, rdd, socket, utility, gpu).
    case $c in chromium*) k=$(grep -ao -- '--type=[a-z-]*' /proc/$pid/cmdline 2>/dev/null | head -1); k=${k#--type=};;
      firefox*|Web*|Isolated*|RDD*|Socket*|Utility*|Privileged*) k=$(tr '\0' '\n' < /proc/$pid/cmdline 2>/dev/null | tail -1 | grep -xE 'tab|rdd|socket|utility|gpu|forkserver');; esac
    sw=0
    for t in /proc/$pid/task/*/schedstat; do read -r _ _ n 2>/dev/null < "$t" && sw=$((sw + n)); done
    echo "DE-PS $pid ${c// /_}${k:+:$k} cpu=$((${12} + ${13})) minflt=${8} majflt=${10} rss=${22} sw=$sw"
  done
  echo "DE-PS-END $label t=$(up)"
}
maps() {   # every process's executable mappings of its own program and its anonymous ones (JIT code): DE-MAP lines
  local label=$1 f pid exe
  echo "DE-MAPS-BEGIN $label t=$(up)"
  for f in /proc/[0-9]*; do
    pid=${f#/proc/}
    exe=$(readlink $f/exe 2>/dev/null) || continue
    awk -v pid=$pid -v exe="$exe" '$2 ~ /x/ && ($6 == exe || $6 == "" || $6 ~ /^\[anon/ || $6 ~ /memfd/) {print "DE-MAP " pid " " $1 " " $2 " " ($6 == "" ? "[anon]" : $6)}' $f/maps 2>/dev/null
  done
  echo "DE-MAPS-END $label t=$(up)"
}
asuser() {
  runuser -u de -- env HOME=/home/de USER=de LOGNAME=de XDG_RUNTIME_DIR=$RUN DBUS_SESSION_BUS_ADDRESS=unix:path=$RUN/bus \
    WAYLAND_DISPLAY=${WAYLAND:-wayland-0} XDG_SESSION_TYPE=wayland XDG_CURRENT_DESKTOP=KDE KDE_FULL_SESSION=true \
    KDE_SESSION_VERSION=6 QT_QPA_PLATFORM=wayland XDG_DATA_DIRS=/home/de/.local/share/flatpak/exports/share:/usr/local/share:/usr/share "$@"
}
shortcut() {
  asuser dbus-send --session --print-reply=literal --dest=org.kde.kglobalaccel /component/kwin \
    org.kde.kglobalaccel.Component.invokeShortcut "string:$1" > /dev/null
}
lines() { wc -l < $WINLOG; }
# wait_win CLASS SECONDS FROM [any]: KWin's first "add" of a normal window (any window with `any`: the shell's are the
# desktop's and the panel's) whose class contains CLASS after line FROM of the window log: prints "MS PID" (KWin's wall
# clock in ms), or fails after SECONDS.
wait_win() {
  local end=$(( $(date +%s) + $2 )) l n=1
  [ "$4" = any ] && n='[01]'
  while [ "$(date +%s)" -lt $end ]; do
    l=$(tail -n +$(($3 + 1)) $WINLOG | grep -m1 -i "^DEWIN add [^ ]*$1[^ ]* pid=[0-9]* normal=$n")
    if [ -n "$l" ]; then l=${l#*pid=}; echo "${l##*ms=} ${l%% *}"; return 0; fi
    sleep 0.2
  done
  return 1
}
# quiet SECONDS MAX: until the CPUs' busy time stays under a quarter of one CPU for SECONDS (USER_HZ 100), at most MAX s.
quiet() {
  local need=$(($1 * 2)) end=$(( $(date +%s) + $2 )) ok=0 a b
  a=$(busy)
  while :; do
    sleep 0.5; b=$(busy)
    if [ $((b - a)) -le 12 ]; then ok=$((ok + 1)); else ok=0; fi
    a=$b
    [ $ok -ge $need ] && return 0
    [ "$(date +%s)" -ge $end ] && return 1
  done
}
gone() {   # gone PID SECONDS: wait until PID has exited, else kill it
  local end=$(( $(date +%s) + $2 ))
  while [ -d /proc/$1 ] && [ "$(date +%s)" -lt $end ]; do sleep 0.2; done
  [ -d /proc/$1 ] && { say KILLED pid=$1; kill -9 $1; sleep 1; }
  return 0
}
snap() { say SNAP $1; sleep 1.5; }
get_state() {
  local from end l
  from=$(lines)
  shortcut "DE State"
  end=$(( $(date +%s) + 5 ))
  while [ "$(date +%s)" -lt $end ]; do
    l=$(tail -n +$(($from + 1)) $WINLOG | grep -m1 '^DEWIN state ')
    if [ -n "$l" ]; then echo "$l"; return 0; fi
    sleep 0.05
  done
  return 1
}
state_val() {
  local w
  for w in $2; do case $w in "$1"=*) echo "${w#*=}"; return 0;; esac; done
  echo none
}
check_ui_effect() {
  local name=$1 before=$2 after=$3 from=$4 to=$5 end_ms=$6
  local bd ad bdi adi bov aov bw aw bc ac bp ap bn an bg ag bm am sw dist seq opid ocls eff=noop
  local bxy axy bwh awh bx by bw_ bh_ ax ay aw_ ah_
  bd=$(state_val desk "$before"); ad=$(state_val desk "$after")
  bdi=$(state_val deskid "$before"); adi=$(state_val deskid "$after")
  bov=$(state_val ov "$before"); aov=$(state_val ov "$after")
  bw=$(state_val win "$before"); aw=$(state_val win "$after")
  bc=$(state_val cls "$before"); ac=$(state_val cls "$after")
  bp=$(state_val pid "$before"); ap=$(state_val pid "$after")
  bn=$(state_val normal "$before"); an=$(state_val normal "$after")
  bg=$(state_val geom "$before"); ag=$(state_val geom "$after")
  bm=$(state_val max "$before"); am=$(state_val max "$after")
  case $name in
    overview-open)
      if [ "$bov" = 0 ] && [ "$aov" = 1 ]; then eff=ok; fi
      say UI-EFFECT ui-$name before_ov=$bov after_ov=$aov effect=$eff
      ;;
    overview-close)
      if [ "$bov" = 1 ] && [ "$aov" = 0 ]; then eff=ok; fi
      say UI-EFFECT ui-$name before_ov=$bov after_ov=$aov effect=$eff
      ;;
    desktop-next)
      if [ "$bd" != none ] && [ "$ad" != none ] && [ "$bdi" != none ] && [ "$adi" != none ] \
         && [ "$ad" -gt "$bd" ] 2>/dev/null && [ "$bdi" != "$adi" ]; then eff=ok; fi
      say UI-EFFECT ui-$name before_desk=$bd after_desk=$ad before_deskid=$bdi after_deskid=$adi effect=$eff
      ;;
    desktop-prev)
      if [ "$bd" != none ] && [ "$ad" != none ] && [ "$bdi" != none ] && [ "$adi" != none ] \
         && [ "$ad" -lt "$bd" ] 2>/dev/null && [ "$bdi" != "$adi" ]; then eff=ok; fi
      say UI-EFFECT ui-$name before_desk=$bd after_desk=$ad before_deskid=$bdi after_deskid=$adi effect=$eff
      ;;
    window-place)
      if [ "$bw" != none ] && [ "$aw" != none ] && [ "$bw" = "$aw" ] && [ "$bn" = 1 ] && [ "$an" = 1 ] \
         && [ "$bg" != none ] && [ "$ag" = "80,60,900,600" ] && [ "$bg" != "$ag" ]; then eff=ok; fi
      say UI-EFFECT ui-$name before_win=$bw after_win=$aw before_cls=$bc after_cls=$ac before_pid=$bp after_pid=$ap before_normal=$bn after_normal=$an before_geom=$bg after_geom=$ag effect=$eff
      ;;
    window-move)
      if [ "$bw" != none ] && [ "$aw" != none ] && [ "$bw" = "$aw" ] && [ "$bn" = 1 ] && [ "$an" = 1 ] \
         && [ "$bg" != none ] && [ "$ag" != none ]; then
        bxy=${bg%,*,*}; axy=${ag%,*,*}; bwh=${bg#*,*,}; awh=${ag#*,*,}
        bx=${bxy%,*}; by=${bxy#*,}; ax=${axy%,*}; ay=${axy#*,}
        if [ "$bwh" = "$awh" ] && [ $((ax - bx)) -eq 480 ] 2>/dev/null && [ $((ay - by)) -eq 240 ] 2>/dev/null; then eff=ok; fi
      fi
      say UI-EFFECT ui-$name before_win=$bw after_win=$aw before_normal=$bn after_normal=$an before_geom=$bg after_geom=$ag effect=$eff
      ;;
    window-grow)
      if [ "$bw" != none ] && [ "$aw" != none ] && [ "$bw" = "$aw" ] && [ "$bn" = 1 ] && [ "$an" = 1 ] \
         && [ "$bg" != none ] && [ "$ag" != none ]; then
        bwh=${bg#*,*,}; awh=${ag#*,*,}
        bw_=${bwh%,*}; bh_=${bwh#*,}; aw_=${awh%,*}; ah_=${awh#*,}
        if [ $((aw_ - bw_)) -eq 360 ] 2>/dev/null && [ "$ah_" -gt "$bh_" ] 2>/dev/null; then eff=ok; fi
      fi
      say UI-EFFECT ui-$name before_win=$bw after_win=$aw before_normal=$bn after_normal=$an before_geom=$bg after_geom=$ag effect=$eff
      ;;
    window-maximize)
      if [ "$bw" != none ] && [ "$aw" != none ] && [ "$bw" = "$aw" ] && [ "$bn" = 1 ] && [ "$an" = 1 ] \
         && [ "$bm" = 0 ] && [ "$am" = 3 ] && [ "$bg" != none ] && [ "$ag" != none ]; then
        bwh=${bg#*,*,}; awh=${ag#*,*,}
        bw_=${bwh%,*}; bh_=${bwh#*,}; aw_=${awh%,*}; ah_=${awh#*,}
        if [ "$aw_" -gt "$bw_" ] 2>/dev/null && [ "$ah_" -gt "$bh_" ] 2>/dev/null; then eff=ok; fi
      fi
      say UI-EFFECT ui-$name before_win=$bw after_win=$aw before_normal=$bn after_normal=$an before_geom=$bg after_geom=$ag before_max=$bm after_max=$am effect=$eff
      ;;
    window-restore)
      if [ "$bw" != none ] && [ "$aw" != none ] && [ "$bw" = "$aw" ] && [ "$bn" = 1 ] && [ "$an" = 1 ] \
         && [ "$bm" = 3 ] && [ "$am" = 0 ] && [ "$bg" != none ] && [ "$ag" != none ]; then
        bwh=${bg#*,*,}; awh=${ag#*,*,}
        bw_=${bwh%,*}; bh_=${bwh#*,}; aw_=${awh%,*}; ah_=${awh#*,}
        if [ "$aw_" -lt "$bw_" ] 2>/dev/null && [ "$ah_" -lt "$bh_" ] 2>/dev/null; then eff=ok; fi
      fi
      say UI-EFFECT ui-$name before_win=$bw after_win=$aw before_normal=$bn after_normal=$an before_geom=$bg after_geom=$ag before_max=$bm after_max=$am effect=$eff
      ;;
    window-switch)
      read -r sw dist seq opid ocls <<EOF
$(sed -n "$((from + 1)),${to}p" $WINLOG | awk -v init="$bw" -v bpid="$bp" -v endms="$end_ms" '
  BEGIN { prev = init; sw = 0; seq = ""; opid = "none"; ocls = "none"; if (init != "none" && init != "") seen[init] = 1 }
  $1 == "DEWIN" && $2 == "active" {
    cls = $3; norm = ""; id = ""; pid = ""; ms = 0
    for (i = 4; i <= NF; i++) {
      if ($i ~ /^normal=/) norm = substr($i, 8)
      else if ($i ~ /^id=/) id = substr($i, 4)
      else if ($i ~ /^pid=/) pid = substr($i, 5)
      else if ($i ~ /^ms=/) ms = substr($i, 4) + 0
    }
    if (norm == "1" && id != "" && id != "none" && (endms == 0 || ms <= endms) && id != prev) {
      sw++; prev = id; seen[id] = 1
      seq = (seq == "" ? id : seq "," id)
      if (id != init && opid == "none" && pid != "" && pid != bpid) { opid = pid; ocls = cls }
    }
  }
  END { n = 0; for (k in seen) n++; print sw, n, (seq == "" ? "none" : seq), opid, ocls }')
EOF
      if [ "$bw" != none ] && [ "$aw" != none ] && [ "$bw" = "$aw" ] && [ "$bn" = 1 ] && [ "$an" = 1 ] \
         && [ "$bc" != none ] && [ "$ac" = "$bc" ] && [ "${sw:-0}" = 4 ] && [ "${dist:-0}" = 2 ] \
         && [ "$opid" != none ] && { [ -z "$K" ] || [ "$opid" = "$K" ]; }; then
        eff=ok
      fi
      say UI-EFFECT ui-$name before_win=$bw after_win=$aw before_cls=$bc after_cls=$ac before_pid=$bp after_pid=$ap before_normal=$bn after_normal=$an switches=${sw:-0} distinct=${dist:-0} seq=${seq:-none} other_pid=${opid:-none} expected_other_pid=${K:-none} other_cls=${ocls:-none} effect=$eff
      ;;
  esac
}
# ui NAME COMMAND...: one interaction, from its command to the CPUs quiet again.
ui() {
  local name=$1 r=ok track=0 before="" after="" from=0 to=0 end_ms=0; shift
  case $name in overview-open|overview-close|desktop-next|desktop-prev|window-place|window-move|window-grow|window-maximize|window-restore|window-switch) track=1;; esac
  if [ $track = 1 ]; then
    before=$(get_state || true)
    from=$(lines)
  fi
  say BEGIN ui-$name; counters ui-$name-begin
  "$@"
  quiet 1 30 || r=timeout
  if [ $track = 1 ]; then
    [ "$name" = window-switch ] && end_ms=$(date +%s%3N)
    after=$(get_state || true)
    to=$(lines)
  fi
  counters ui-$name-end; say END ui-$name quiet=$r
  if [ $track = 1 ]; then
    check_ui_effect "$name" "$before" "$after" "$from" "$to" "$end_ms"
  fi
  snap ui-$name
}
steps() { local i; for i in $(seq $2); do shortcut "DE $1"; sleep 0.1; done; }
# start NAME N CLASS COMMAND...: an app's start, from its command to its first window (MAPPED) and to the CPUs quiet
# again (SETTLED); START_PID is the window's process.
start() {
  local name=$1 n=$2 cls=$3 from r ms t0 q=ok; shift 3
  from=$(lines)
  say BEGIN app-$name-$n; counters app-$name-$n-begin
  t0=$(up)
  asuser "$@" > /tmp/de-$name-$n.log 2>&1 &
  if r=$(wait_win "$cls" 240 "$from"); then
    ms=${r% *}; START_PID=${r#* }
    say MAPPED app-$name-$n at=$(ms2up $ms) started=$t0 pid=$START_PID
  else
    START_PID=; say MAPPED app-$name-$n timeout started=$t0
  fi
  counters app-$name-$n-mapped
  quiet 2 90 || q=timeout
  counters app-$name-$n-settled; say SETTLED app-$name-$n quiet=$q
  ps_cpu app-$name-$n
  maps app-$name-$n
  say END app-$name-$n
  snap app-$name-$n
}
INPUT=$(arg de.input)
if [ "$INPUT" = uinput ]; then        # no runner to type (the KVM reference): a uinput keyboard in the guest
  modprobe uinput; mkfifo /run/de-uinput.fifo; python3 $H/uinput.py /run/de-uinput.fifo &
fi
# input key CODE | input text STRING: natemu's runner types it on the PS/2 keyboard when the line arrives (de.input=uinput:
# the guest's uinput keyboard does)
input() {
  echo "DE-INPUT $* t=$(up)"
  [ "$INPUT" = uinput ] && echo "$*" > /run/de-uinput.fifo
  sleep 0.3
}
# click NAME N CLASS QUERY: the launcher (Meta), QUERY typed, then Enter (the click: the DE-INPUT line's t=), to the app's
# first window (MAPPED) and the CPUs quiet (SETTLED); then DE-CLICKMARKS with the stretch's marks (guest uptimes): the
# StartTransientUnit call on the session bus (dbus-monitor), the unit's "Started" line (the user's journal), the
# process's creation (/proc/PID/stat's start time), KWin's arrival of the window.
click() {
  local name=$1 n=$2 cls=$3 query=$4 from r ms t0 q=ok dm dbus unit exec at
  dm=/tmp/de-dbus-$name-$n.log
  asuser dbus-monitor --session "type='method_call',member='StartTransientUnit'" > $dm 2>&1 & DMON=$!
  input key MetaLeft; quiet 1 30 || say QUIET-TIMEOUT click-$name-$n-meta
  input text $query; quiet 1 30 || say QUIET-TIMEOUT click-$name-$n-query
  from=$(lines)
  say BEGIN click-$name-$n; counters click-$name-$n-begin
  input key Enter
  t0=$(up)
  if r=$(wait_win "$cls" 240 "$from"); then
    ms=${r% *}; START_PID=${r#* }; at=$(ms2up $ms)
    say MAPPED click-$name-$n at=$at started=$t0 pid=$START_PID
  else
    START_PID=; at=; say MAPPED click-$name-$n timeout started=$t0
  fi
  counters click-$name-$n-mapped
  quiet 2 90 || q=timeout
  counters click-$name-$n-settled; say SETTLED click-$name-$n quiet=$q
  ps_cpu click-$name-$n
  say END click-$name-$n
  kill $DMON 2>/dev/null
  dbus=$(grep -m1 'member=StartTransientUnit' $dm | sed -n 's/.*time=\([0-9.]*\).*/\1/p')
  [ -n "$dbus" ] && dbus=$(awk "BEGIN{printf \"%.3f\", $dbus - ($RT0 - $UP0)}")
  unit=$(journalctl -b _UID=1000 -o short-monotonic --no-hostname 2>/dev/null | grep -i "Started app-[^ ]*$cls" | tail -1 | sed -n 's/^\[ *\([0-9.]*\)\].*/\1/p')
  [ -n "$START_PID" ] && exec=$(awk '{print $22 / 100}' /proc/$START_PID/stat 2>/dev/null)
  echo "DE-CLICKMARKS click-$name-$n dbus=${dbus:-none} unit=${unit:-none} exec=${exec:-none} at=${at:-none} unitline=$(grep -m1 -o 'app-[^ "]*' $dm | head -1) t=$(up)"
  snap click-$name-$n
}
close() {   # close NAME: the front window (KWin's "DE Close Top"), then its process gone and the CPUs quiet
  local pid=$START_PID
  say CLOSE $1 pid=$pid
  shortcut "DE Close Top"
  [ -n "$pid" ] && gone $pid 60
  quiet 2 30 || say QUIET-TIMEOUT close-$1
}

say DRIVER v=2 phases=$PHASES guestperf=${GPERF:-0} spiter=$SPITER rt=$RT0 up0=$UP0
echo "DE-KTEXT $(grep ' _text$' /proc/kallsyms | cut -d' ' -f1)"
: > $WINLOG
( journalctl -b -f -o cat _UID=1000 2>/dev/null & while [ ! -f /tmp/de-session.log ]; do sleep 1; done; tail -n +1 -F /tmp/de-session.log 2>/dev/null ) \
  | grep --line-buffered 'DEWIN ' | sed -u 's/.*DEWIN /DEWIN /' >> $WINLOG &

# The session: KWin's socket, plasmashell's first window (the desktop or the panel), then the CPUs quiet: an idle desktop.
end=$(( $(date +%s) + 900 ))
while [ ! -S $RUN/wayland-0 ] && [ "$(date +%s)" -lt $end ]; do sleep 0.5; done
WAYLAND=$(cd $RUN 2>/dev/null && ls wayland-? 2>/dev/null | head -1)
say WAYLAND socket=${WAYLAND:-none}
if r=$(wait_win plasmashell 900 0 any); then say SHELL-MAPPED at=$(ms2up ${r% *}); else say SHELL-MAPPED timeout; fi
grep -m1 'DEWIN loaded' $WINLOG > /dev/null && say KWIN-SCRIPT loaded || say KWIN-SCRIPT missing
q=ok; quiet 3 240 || q=timeout
say IDLE-READY quiet=$q
counters ready; ps_cpu ready; maps ready
snap desktop

if has idle; then
  say BEGIN idle; counters idle-begin
  sleep 20
  counters idle-end; say END idle
  ps_cpu idle
  snap idle
fi

if [ "$GPERF" = 1 ]; then
  perf record -a -F 199 -e cpu-clock -o /var/tmp/de.perf.data -q > /tmp/de-perf.log 2>&1 & PERF=$!
  sleep 2; say GPERF-START pid=$PERF
fi

if has ui; then
  D=org.kde.plasmashell
  launcher() { asuser dbus-send --session --print-reply=literal --dest=$D /PlasmaShell org.kde.PlasmaShell.activateLauncherMenu > /dev/null; }
  ui launcher-open launcher
  ui launcher-close launcher
  ui overview-open shortcut Overview
  ui overview-close shortcut Overview
  ui desktop-next shortcut "Switch to Next Desktop"
  ui desktop-prev shortcut "Switch to Previous Desktop"
  ps_cpu ui
fi

if has apps; then
  start konsole 1 konsole konsole; close konsole
  start konsole 2 konsole konsole; K=$START_PID
  start dolphin 1 dolphin dolphin; close dolphin
  start dolphin 2 dolphin dolphin; DP=$START_PID
  ui window-place steps Place 1
  ui window-move steps Move 20
  ui window-grow steps Grow 15
  ui window-maximize shortcut "Window Maximize"
  ui window-restore shortcut "Window Maximize"
  ui window-switch steps "Next Window" 4
  START_PID=$DP; close dolphin
  START_PID=$K; close konsole
  ps_cpu apps
fi

CH="chromium --ozone-platform=wayland --no-first-run --no-default-browser-check --password-store=basic"
if has click; then
  click konsole 1 konsole konsole; close konsole
  click konsole 2 konsole konsole; close konsole
  click dolphin 1 dolphin dolphin; close dolphin
  click dolphin 2 dolphin dolphin; close dolphin
  ps_cpu click
fi

if has browsers; then
  start chromium 1 chromium $CH about:blank; close chromium
  start chromium 2 chromium $CH about:blank; close chromium
  start firefox 1 firefox firefox-esr about:blank; close firefox
  start firefox 2 firefox firefox-esr about:blank; close firefox
  ps_cpu browsers
fi

if has speedometer; then
  sed -i 's|<script src="resources/main.mjs" type="module"></script>|<script src="de-gpu.js"></script><script src="hook.js"></script>&|' /sp/index.html
  cp $H/hook.js $H/de-gpu.js /sp/
  grep -q '<script src="hook.js"></script><script src="resources/main.mjs"' /sp/index.html || say SP-NO-HOOK
  python3 $H/serve.py & SRV=$!
  sleep 1
  say SP-BEGIN iter=$SPITER
  start chromium 3 chromium $CH "http://127.0.0.1:8080/index.html?startAutomatically&iterationCount=$SPITER"
  CP=$START_PID
  end=$(( $(date +%s) + ${SPCAP:-3000} ))
  k=0; next=$(( $(date +%s) + 20 ))
  while [ -d /proc/$SRV ] && [ "$(date +%s)" -lt $end ]; do
    sleep 2
    if [ "$(date +%s)" -ge $next ]; then k=$((k + 1)); say SNAP speedometer-$k; next=$(( $(date +%s) + 30 )); fi
  done
  [ -d /proc/$SRV ] && { say SP-TIMEOUT; kill $SRV; }
  say SP-END
  # What Chromium renders with: its GPU process's command line and the GL and Vulkan libraries it mapped.
  for p in $(pgrep -u de -f -- '--type=gpu-process'); do
    echo "DE-INFO chromium-gpu cmdline $(tr '\0' ' ' < /proc/$p/cmdline | grep -o -- '--[a-z-]*\(gl\|angle\|gpu\|vulkan\|swiftshader\)[a-z-]*\(=[^ ]*\)\?' | tr '\n' ' ')"
    echo "DE-INFO chromium-gpu maps $(grep -oE '[^/ ]*(swiftshader|GLESv2|EGL|gallium|vulkan|LLVM|dri)[^/ ]*$' /proc/$p/maps | sort -u | tr '\n' ' ')"
  done
  ps_cpu speedometer
  START_PID=$CP; close chromium
fi

if [ -n "$PERF" ]; then
  kill -INT $PERF; wait $PERF
  say GPERF-STOP
fi
# What KWin renders with.
KW=$(pgrep -u de -x kwin_wayland | head -1)
[ -n "$KW" ] && echo "DE-INFO kwin maps $(grep -oE '[^/ ]*(gallium|GLESv2|EGL|LLVM|dri|gbm)[^/ ]*$' /proc/$KW/maps | sort -u | tr '\n' ' ')"
journalctl -b -o cat _UID=1000 2>/dev/null | cat - /tmp/de-session.log 2>/dev/null | grep -iE 'opengl|llvmpipe|renderer|qpainter|software rendering|egl' \
  | sort -u | head -20 | sed 's/^/DE-INFO kwin log /'
counters end; ps_cpu end; maps end
say END-ALL
if [ -n "$PERF" ]; then
  say GPERF-REPORT-BEGIN
  # percentages are of all samples, idle (swapper) included; the idle lines are left out
  perf report -i /var/tmp/de.perf.data --stdio -q --sort comm,dso --percent-limit 0.02 2>/dev/null | grep -v ' swapper ' | head -150 | sed 's/^/DE-GPERF comm,dso /'
  perf report -i /var/tmp/de.perf.data --stdio -q --sort dso,sym --percent-limit 0.01 2>/dev/null | grep -v 'pv_native_safe_halt' | head -250 | sed 's/^/DE-GPERF dso,sym /'
  perf report -i /var/tmp/de.perf.data --stdio -q --sort comm,dso,sym --percent-limit 0.02 2>/dev/null | grep -v ' swapper ' | head -200 | sed 's/^/DE-GPERF comm,dso,sym /'
  say GPERF-REPORT-END
fi
journalctl -b -p err -o short-monotonic 2>/dev/null | tail -40 | sed 's/^/DE-JOURNAL /'
say POWEROFF
sync
systemctl poweroff -ff
