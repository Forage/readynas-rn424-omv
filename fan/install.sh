#!/bin/sh
# install.sh - install / remove ReadyNAS RN424 fan control on OMV / Debian.
#   sh install.sh              install (or update), calibrate if needed, start
#   sh install.sh recalibrate  measure the fan again, then restart the service
#   sh install.sh uninstall    stop (fan returns to the BIOS setting), remove
set -eu

DEST=/opt/rn424-fan
UNIT=/etc/systemd/system/rn424-fan.service
MODS=/etc/modules-load.d/rn424-fan.conf
CONF=/etc/default/rn424-fan
SRC="$(cd "$(dirname "$0")" && pwd)/rn424_fan.py"
PY="/usr/bin/python3 -I $DEST/rn424_fan.py"

[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

MODE="${1:-install}"
case "$MODE" in
    install|recalibrate) ;;
    uninstall)
        systemctl disable --now rn424-fan.service 2>/dev/null || true
        rm -f "$UNIT"
        rm -rf "$DEST"
        systemctl daemon-reload
        echo "Removed; the fan is back on its BIOS setting."
        echo "($CONF and $MODS kept; delete them by hand if you like.)"
        exit 0 ;;
    *) echo "usage: sh install.sh [install|recalibrate|uninstall]"; exit 1 ;;
esac

if [ "$MODE" != "recalibrate" ] || [ ! -f "$DEST/rn424_fan.py" ]; then
    [ -f "$SRC" ] || { echo "rn424_fan.py must sit next to install.sh"; exit 1; }
fi

echo "== Loading the sensor drivers"
printf 'it87\ndrivetemp\n' > "$MODS"
modprobe drivetemp 2>/dev/null || true
modprobe it87 2>/dev/null || true

# The it87 driver is rebuilt by DKMS on kernel updates only if the headers
# for the new kernel get installed along with it.
if dpkg-query -W -f='${Status}' linux-image-amd64 2>/dev/null | grep -q 'ok installed' &&
   ! dpkg-query -W -f='${Status}' linux-headers-amd64 2>/dev/null | grep -q 'ok installed'; then
    echo "WARNING: linux-headers-amd64 is not installed, so the fan driver will"
    echo "NOT be rebuilt after the next kernel update. Fix:"
    echo "  apt-get install linux-headers-amd64=\$(dpkg-query -W -f='\${Version}' linux-image-amd64)"
fi

systemctl stop rn424-fan.service 2>/dev/null || true

if [ -f "$SRC" ]; then
    # copy, dropping Windows line endings if the file passed through Windows
    mkdir -p "$DEST"
    tr -d '\r' < "$SRC" > "$DEST/rn424_fan.py.new"
    chmod 0755 "$DEST/rn424_fan.py.new"
    mv "$DEST/rn424_fan.py.new" "$DEST/rn424_fan.py"
fi

echo "== Read-only check"
if ! $PY probe; then
    echo
    echo "Check failed - NOT enabling fan control. If 'Fan chip' says NOT FOUND,"
    echo "install the community it87 driver first (see fan/README.md)."
    exit 3
fi

if [ "$MODE" = "recalibrate" ] || ! grep -qs '^RN_FAN_MIN=[0-9]' "$CONF"; then
    echo "== Calibrating (about 3 minutes; the fan speeds up and slows down)"
    if ! $PY calibrate; then
        echo "Calibration failed - NOT enabling fan control. Send the output above."
        exit 3
    fi
fi

cat > "$UNIT" <<EOF
[Unit]
Description=ReadyNAS RN424 fan control
After=local-fs.target systemd-modules-load.service

[Service]
Type=notify
ExecStartPre=-/sbin/modprobe it87
ExecStartPre=-/sbin/modprobe drivetemp
ExecStart=$PY run
# runs after every stop or crash: fan back to the setting it had before
ExecStopPost=$PY restore
Restart=always
RestartSec=10
# exit code 3 = not calibrated / chip missing: do not retry blindly
RestartPreventExitStatus=3
WatchdogSec=90
Nice=-5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable rn424-fan.service
systemctl restart rn424-fan.service
sleep 3
systemctl --no-pager --lines=5 status rn424-fan.service || true
echo "== Done. Logs: journalctl -u rn424-fan -f"
