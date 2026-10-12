# Front panel driver (display + buttons)

Brings the RN424's front display and 5-way button pad back to life under
OpenMediaVault or plain Debian. Pure Python, no kernel module. Also contains
the RN426/RN428 pin map (selected automatically from the DMI model name).

## Pages

| Page | Line 1 | Line 2 |
|---|---|---|
| Network | hostname | IP address |
| Data | `Data 37% used` | `10.3T free/16.4T` (largest volume under `/srv`) |
| RAID | `md127 raid1` | `OK [U]`, `DEGRADED [U_]` or rebuild progress |
| System | CPU temperature | fan speed if a sensor exists, else RAM use |
| Uptime | uptime | load average |

* **Up/Down or Left/Right** change the page.
* The display turns off after `RN_SLEEP` seconds without a button press; any
  button wakes it.
* A degraded RAID or a volume over 90 % full wakes the display and shows a
  warning page until the condition clears.
* The text shifts by a pixel every minute to limit burn-in.
* If the buttons aren't available, pages rotate automatically and the display
  never sleeps.
* At power-on the BIOS writes "NETGEAR Storage / Welcome" to the display.
  That can't be changed without modifying the BIOS. About 10 s into the
  Linux boot, a small early service (`rn424-panel-splash`) replaces it with
  "Booting… / <hostname>" until the main service starts. On a normal boot
  the status pages follow 1–2 s later. The message mainly helps when the
  network is slow to come up (no cable, for example): the main service waits
  for the network, up to two minutes, and without the message the display
  would show "Welcome" all that time.
* Most of the "Welcome" time is the BIOS and the GRUB menu. To cut GRUB's
  5 s wait to 1 s: `sed -i 's/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT=1/' /etc/default/grub && update-grub`.

## Install

Copy `rn424_panel.py` and `install.sh` to the NAS (e.g. from Windows
PowerShell: `scp rn424_panel.py install.sh root@<NAS-IP>:/root/`), then as
root:

```
apt-get install -y python3-pil fonts-dejavu-core
modprobe i2c-dev; modprobe i2c-i801
python3 rn424_panel.py probe     # read-only, must end with "Result: READY"
python3 rn424_panel.py test      # shows "Hello! / Front panel OK"
sh install.sh                    # installs the rn424-panel systemd service
```

`install.sh` copies the driver to `/opt/rn424-panel`, loads the SMBus modules
at boot, re-runs the probe and only enables the service if it passes.
Re-running it updates an existing install; `sh install.sh uninstall` removes
everything.

## Settings

`/etc/default/rn424-panel`, then `systemctl restart rn424-panel`:

```
RN_SLEEP=120   # idle seconds before the display turns off (0 = never)
RN_ROTATE=0    # seconds between automatic page changes (0 = buttons only)
```

Logs: `journalctl -u rn424-panel -f`

## Commands

| Command | Does |
|---|---|
| `probe` | Read-only. Prints the model, both GPIO PADBARs, every pad register and the SMBus adapter. |
| `test` | Initialises the display and draws a test message. |
| `run` | The service loop. |
| `sleep` / `wake` | Turn the display off / on. |
| `splash [line1] [line2]` | Show a message (used by the early-boot service). |

`test`, `sleep` and `wake` refuse to run while the service is running (stop it
first with `systemctl stop rn424-panel`).

## Safety

Before writing anything, the driver checks:

1. DMI says RN422/424 (or RN426/428).
2. Both GPIO communities' PADBAR read `0x400` at `0xFDC2000C` / `0xFDC5000C`.
3. Every display pad is a plain GPIO, **already an output**, with **no
   interrupt routing**.

If any check fails it exits with code 3 and writes nothing (the service won't
retry). When it runs it only ever changes the output-level bit of those pads,
never drives EN or RST low, never writes to the button controller, and doesn't
touch P2SB or I/O ports. See [`../docs/PINMAP.md`](../docs/PINMAP.md) for why.

## If the buttons stop responding

Unplug the NAS for 10 seconds. The button controller runs on standby power,
so a normal reboot doesn't reset it.

## Credits

Based on [riplatt/truenas-rn426-panel](https://github.com/riplatt/truenas-rn426-panel)
(MIT), which did the original reverse engineering for the RN426. Changes in
this version: RN422/RN424 pin map, OMV/btrfs/mdadm pages, no P2SB/`/dev/port`
access, corrected PADCFG TX/RX bits, stricter pre-flight checks, single-instance
lock.
