#!/bin/sh
# The user's login on tty1 (the image's ~/.bash_profile execs this): the Plasma Wayland session, its own output kept in
# /tmp/de-session.log (the harness's driver reads KWin's lines from there and from the journal).
echo "DE-SESSION t=$(cut -d' ' -f1 /proc/uptime) rt=$(date +%s.%N)" > /dev/ttyS0
exec startplasma-wayland > /tmp/de-session.log 2>&1
