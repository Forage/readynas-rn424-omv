# Fan control

Out of the box, OMV leaves the RN424's fan at the fixed speed the BIOS sets
(50 % duty, about 1900 rpm, which is loud). ReadyNAS OS 6 slowed it down
with NETGEAR's own software. This service does that job: it sets the fan speed
from the hottest disk and the CPU temperature.

| Situation | Fan |
|---|---|
| Disks ≤ 35 °C and CPU ≤ 55 °C | Minimum (measured for your fan, see *Calibration*) |
| Disk 35 → 45 °C or CPU 55 → 80 °C | Rises in a straight line |
| Disk ≥ 45 °C or CPU ≥ 80 °C | Full speed |
| A temperature or the fan speed can't be read | Full speed |
| Fan stops spinning | Full speed, and the minimum is raised by 6 % |
| Service stops or crashes | Back to the BIOS setting it had before |

Speed-ups take effect at once. Slow-downs are gradual, by at most 3 % every
15 s, so the fan doesn't keep changing pitch. The front panel's *System* page
shows the fan speed in rpm once the driver is loaded.

## 1. Fan chip driver

The fan is on an **ITE IT8613E** chip. Debian's built-in `it87` driver doesn't
support that chip (`modprobe it87` says *No such device*, and
`modprobe it87 dyndbg=+p` logs `Unsupported chip (DEVID=0x8613)`). The
community driver [frankcrawford/it87](https://github.com/frankcrawford/it87)
supports it. NETGEAR's own OS 6 kernel had a version of the same driver built
in. Install it with DKMS so it's rebuilt automatically on kernel updates:

```
apt-get update
apt-get install -y dkms build-essential git "linux-headers-$(uname -r)"
V=$(dpkg-query -W -f='${Version}' linux-image-amd64 2>/dev/null); [ -n "$V" ] && apt-get install -y "linux-headers-amd64=$V"
git clone https://github.com/frankcrawford/it87 /root/it87
cd /root/it87 && git checkout -q bd7a566c8a60fe0308d67e26761e16260368191a
make dkms
cd /root
```

The third line installs the headers package that follows your kernel
(including a backports kernel), so DKMS can rebuild the driver when the
kernel updates. That commit is the tested one: it builds without warnings
against Linux 7.2 (`W=1`) and runs on the RN424 with Debian's 7.2.6 kernel.

Check: `dmesg | grep it87` should show `Found IT8613E chip at 0xa30`.
`module verification failed … tainting kernel` is harmless; it only means the
module isn't signed with a key the kernel knows. (With Secure Boot on, the
module wouldn't load at all.)

## 2. Install

```
git clone https://github.com/dgalue/readynas-rn424-omv /root/readynas-rn424-omv
sh /root/readynas-rn424-omv/fan/install.sh
```

`install.sh`:

1. loads `it87` and `drivetemp` now and at every boot;
2. warns if kernel updates would leave the fan driver behind;
3. runs a read-only check (`probe`);
4. **calibrates** the fan the first time (about 3 minutes, the fan speeds up
   and slows down);
5. installs and starts the `rn424-fan` service.

To update later: `git -C /root/readynas-rn424-omv pull`, then run `install.sh`
again. To remove it: `sh install.sh uninstall`, which puts the fan back on
its BIOS setting.

## Calibration

`calibrate` first finds which PWM output drives the fan. It sets each output
to 100 % and then 25 % and watches the fan's rpm, starting with the output
whose number matches the fan's. Then it steps the duty down from 100 % and
records the lowest speed that still spins. The minimum is that value plus a
safety margin. Everything the calibration changed is put back afterwards,
including if you press Ctrl-C.

On an RN424 the fan is `fan2`, driven by `pwm2`; the IT8613E has no `fan1`.

Measured on the author's RN424 (stock fan):

| Duty | 100 % | 75 % | 50 % (BIOS) | 31 % | 15 % | 9 % |
|---|---|---|---|---|---|---|
| rpm | 3000 | 2445 | 1875 | 1448 | 1090 | 949 |

It never stopped, even at 9 %, so calibration chose the lowest allowed
minimum, 40/255 (15 %).

Run it again with `sh install.sh recalibrate`, for example after swapping the
fan.

## Settings

`/etc/default/rn424-fan`, then `systemctl restart rn424-fan`:

```
RN_FAN_PWM=2        # set by calibrate
RN_FAN_TACH=2       # set by calibrate
RN_FAN_MIN=72       # set by calibrate: lowest duty, 0-255
RN_FAN_MAX=255      # highest duty
RN_DISK_LOW=35      # hottest disk, °C: minimum at or below
RN_DISK_HIGH=45     #                    full speed at or above
RN_CPU_LOW=55       # CPU, °C
RN_CPU_HIGH=80
RN_INTERVAL=15      # seconds between adjustments
```

The defaults keep drives in the 35–45 °C range that's comfortable for 3.5"
disks. For a quieter fan with warmer disks, raise `RN_DISK_LOW` and
`RN_DISK_HIGH` a few degrees, but keep `RN_DISK_HIGH` well under your drives'
rated maximum (usually 60–65 °C).

Logs: `journalctl -u rn424-fan -f`. A line is written when the speed changes
noticeably, on any fault, and every 30 minutes.

## Commands

| Command | Does |
|---|---|
| `probe` | Read-only: fan chip, driver version, fans, PWM outputs, temperatures, settings. |
| `calibrate` | Finds the PWM output and minimum speed, saves them. Refuses to run while the service is running. |
| `run` | The service loop. |
| `restore` | Puts back the fan setting saved when the service started (systemd runs this after every stop). |

## How it stays safe

* It only writes the fan chip's `pwmN` and `pwmN_enable` files, through the
  kernel driver. No direct register access.
* The BIOS setting is saved to `/run/rn424-fan.state` before the first write.
  A restart after a crash keeps the original.
* systemd runs `restore` after any stop, crash or watchdog timeout, so a dead
  service leaves the fan at the BIOS speed, not at a low one.
* Disk temperatures come from `drivetemp`. A disk that stops answering keeps
  its last reading for 2 minutes, then the fan goes to full speed. A device
  that never reports a temperature (some USB sticks) is ignored, as long as
  another disk reports.

Note: the kernel's drivetemp documentation warns that reading a drive's
temperature can reset the spin-down timer on some drives. If you use disk
spin-down in OMV, check that your disks still go to sleep.
