#!/bin/sh
# install.sh - install / remove the ReadyNAS RN424 front-panel service on OMV.
#   sh install.sh            install (or update) and start
#   sh install.sh uninstall  stop and remove everything
set -eu

DEST=/opt/rn424-panel
UNIT=/etc/systemd/system/rn424-panel.service
SPLASH=/etc/systemd/system/rn424-panel-splash.service
MODS=/etc/modules-load.d/rn424-panel.conf
CONF=/etc/default/rn424-panel
SRC="$(cd "$(dirname "$0")" && pwd)/rn424_panel.py"

[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

case "${1:-install}" in
    install) ;;
    uninstall)
        systemctl disable --now rn424-panel.service 2>/dev/null || true
        systemctl disable rn424-panel-splash.service 2>/dev/null || true
        rm -f "$UNIT" "$SPLASH" "$MODS"
        rm -rf "$DEST"
        systemctl daemon-reload
        echo "Removed. ($CONF kept; delete it by hand if you like.)"
        exit 0 ;;
    *) echo "usage: sh install.sh [install|uninstall]"; exit 1 ;;
esac

[ -f "$SRC" ] || { echo "rn424_panel.py must sit next to install.sh"; exit 1; }

echo "== Installing dependencies"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    python3 python3-pil fonts-dejavu-core >/dev/null

echo "== Loading SMBus modules (buttons)"
printf 'i2c-dev\ni2c-i801\n' > "$MODS"
modprobe i2c-dev 2>/dev/null || true
modprobe i2c-i801 2>/dev/null || true

# copy, dropping Windows line endings if the file passed through Windows
mkdir -p "$DEST"
tr -d '\r' < "$SRC" > "$DEST/rn424_panel.py.new"
chmod 0755 "$DEST/rn424_panel.py.new"
mv "$DEST/rn424_panel.py.new" "$DEST/rn424_panel.py"

echo "== Read-only hardware check"
if ! python3 -I "$DEST/rn424_panel.py" probe; then
    echo "Hardware check failed - NOT enabling the service. Send the output above."
    exit 3
fi

[ -f "$CONF" ] || cat > "$CONF" <<'EOF'
# Seconds of no button presses before the display blanks (0 = never).
RN_SLEEP=120
# Seconds between automatic page changes (0 = only with the buttons).
RN_ROTATE=0
EOF

cat > "$UNIT" <<EOF
[Unit]
Description=ReadyNAS RN424 front panel (display + buttons)
After=local-fs.target network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=-$CONF
ExecStartPre=-/sbin/modprobe i2c-dev
ExecStartPre=-/sbin/modprobe i2c-i801
ExecStart=/usr/bin/python3 -I $DEST/rn424_panel.py run
Restart=on-failure
RestartSec=10
# exit code 3 = a safety check failed: do not retry blindly
# (exit code 4 = panel briefly busy: normal restart applies)
RestartPreventExitStatus=3
Nice=10

[Install]
WantedBy=multi-user.target
EOF

# Early-boot splash: replaces the BIOS "NETGEAR Storage / Welcome" frame a
# few seconds into boot, long before the main service (which waits for the
# network) starts.
cat > "$SPLASH" <<EOF
[Unit]
Description=ReadyNAS RN424 front panel boot message
DefaultDependencies=no
After=local-fs.target
Before=sysinit.target rn424-panel.service
ConditionPathExists=$DEST/rn424_panel.py

[Service]
Type=oneshot
ExecStart=-/usr/bin/python3 -I $DEST/rn424_panel.py splash
TimeoutStartSec=20

[Install]
WantedBy=sysinit.target
EOF

systemctl daemon-reload
systemctl enable rn424-panel-splash.service
systemctl enable rn424-panel.service
systemctl restart rn424-panel.service
sleep 3
systemctl --no-pager --lines=5 status rn424-panel.service || true
echo "== Done. Logs: journalctl -u rn424-panel -f"
