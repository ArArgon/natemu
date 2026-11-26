#!/bin/sh
# de-setup.service (the image's unit, before the autologin on tty1): the user's settings from the harness
# (tests/bench/desktop/home-config -> ~/.config, the demarker KWin script -> ~/.local/share/kwin/scripts).
H=/run/de-harness
mkdir -p /home/de/.config /home/de/.local/share/kwin/scripts
cp -a $H/home-config/. /home/de/.config/
cp -a $H/kwin-demarker /home/de/.local/share/kwin/scripts/demarker
chown -R de:de /home/de
echo "DE-SETUP t=$(cut -d' ' -f1 /proc/uptime)"
