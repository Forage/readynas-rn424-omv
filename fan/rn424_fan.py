#!/usr/bin/env python3
"""rn424_fan.py - temperature-controlled fan for a ReadyNAS RN422/RN424 on
OpenMediaVault / Debian.

The fan hangs off the board's ITE IT8613E Super I/O chip. The BIOS leaves it
at a fixed, loud speed; under ReadyNAS OS 6 NETGEAR's own software slowed it
down. This replaces that: it reads the disk temperatures (drivetemp) and the
CPU temperature (coretemp) and sets the fan speed from them.

Needs the community it87 driver (Debian's own it87 doesn't know the IT8613E),
see README.md next to this file.

Commands (run as root):
  probe      read-only: fan chip, fans, PWM outputs, temperatures, settings
  calibrate  find which PWM output drives the fan and how slow it can safely
             run (about 3 minutes; the fan speeds up and slows down), then
             save the result to /etc/default/rn424-fan
  run        the service loop (normally started by systemd)
  restore    put the fan back the way it was before the service took over

Safety:
  * Any problem reading a temperature or the fan speed -> full speed.
  * Fan stops spinning -> full speed, and the minimum speed is raised.
  * When the service stops (or crashes), systemd runs "restore", which puts
    back the BIOS setting the fan had before.
  * Only the fan chip's PWM registers are written, through the kernel driver.
"""

import fcntl
import glob
import json
import os
import re
import signal
import socket
import sys
import time

SYS = os.environ.get("RN_SYSFS", "/sys")            # overridable for testing
HWMON = os.path.join(SYS, "class", "hwmon")
CONF = os.environ.get("RN_FAN_CONF", "/etc/default/rn424-fan")
STATE = os.environ.get("RN_FAN_STATE", "/run/rn424-fan.state")
LOCKFILE = os.environ.get("RN_FAN_LOCK", "/run/rn424-fan.lock")
SETTLE = float(os.environ.get("RN_SETTLE", "8"))    # s for the fan to settle

EXIT_UNSAFE = 3     # don't let systemd retry blindly
EXIT_BUSY = 4
FULL = 255
CHIP_RE = re.compile(r"^it8[0-9]{3}$|^it87")
STEP_DOWN = 8       # max duty decrease per interval (slow, quiet ramp-down)
DEADBAND = 4        # ignore decreases smaller than this
STALL_BUMP = 16     # raise the minimum by this much after a stall
DISK_CACHE_S = 120  # tolerate a disk not answering for this long
SWEEP = (255, 192, 160, 128, 112, 96, 80, 64, 56, 48, 40, 32, 24)

KEYS = ("RN_FAN_PWM", "RN_FAN_TACH", "RN_FAN_MIN", "RN_FAN_MAX",
        "RN_DISK_LOW", "RN_DISK_HIGH", "RN_CPU_LOW", "RN_CPU_HIGH",
        "RN_INTERVAL")
DEFAULTS = {"RN_FAN_MAX": 255, "RN_DISK_LOW": 35, "RN_DISK_HIGH": 45,
            "RN_CPU_LOW": 55, "RN_CPU_HIGH": 80, "RN_INTERVAL": 15}

CONF_TEMPLATE = """\
# ReadyNAS RN424 fan control.  After editing: systemctl restart rn424-fan
#
# Written by "rn424_fan.py calibrate": the PWM output and tachometer the fan
# is on, and the lowest duty (0-255) at which it still spins reliably.
RN_FAN_PWM=
RN_FAN_TACH=
RN_FAN_MIN=
# Highest duty the service will use (255 = full speed).
RN_FAN_MAX=255
# Hottest disk, degrees C: fan at minimum at or below LOW, full speed at HIGH.
RN_DISK_LOW=35
RN_DISK_HIGH=45
# CPU, degrees C, same idea. The fan follows whichever needs more airflow.
RN_CPU_LOW=55
RN_CPU_HIGH=80
# Seconds between adjustments.
RN_INTERVAL=15
"""


class Unsafe(Exception):
    pass


class Busy(Exception):
    pass


class Stop(Exception):
    pass


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- sysfs ----

def rd(path):
    with open(path) as f:
        return f.read().strip()


def rd_int(path):
    return int(rd(path))


def wr(path, value):
    with open(path, "w") as f:
        f.write("%d" % int(value))


def _num(path):
    m = re.search(r"(\d+)", os.path.basename(path))
    return int(m.group(1)) if m else 0


def hwmons():
    out = []
    for d in sorted(glob.glob(os.path.join(HWMON, "hwmon*")), key=_num):
        try:
            out.append((rd(os.path.join(d, "name")), d))
        except OSError:
            pass
    return out


def fan_chip():
    for name, d in hwmons():
        if CHIP_RE.match(name):
            return name, d
    return None, None


def channels(d, kind):
    """Channel numbers of fanN_input (kind='fan') or pwmN (kind='pwm')."""
    if kind == "fan":
        files = glob.glob(os.path.join(d, "fan*_input"))
        pat = re.compile(r"^fan(\d+)_input$")
    else:
        files = glob.glob(os.path.join(d, "pwm*"))
        pat = re.compile(r"^pwm(\d+)$")
    out = []
    for f in files:
        m = pat.match(os.path.basename(f))
        if m and (kind == "fan" or os.path.exists(f + "_enable")):
            out.append(int(m.group(1)))
    return sorted(out)


def pwm_path(d, ch):
    return os.path.join(d, "pwm%d" % ch)


def enable_path(d, ch):
    return os.path.join(d, "pwm%d_enable" % ch)


def tach_path(d, ch):
    return os.path.join(d, "fan%d_input" % ch)


def disk_name(d):
    try:
        return sorted(os.listdir(os.path.join(d, "device", "block")))[0]
    except (OSError, IndexError):
        return os.path.basename(d)


def disks():
    return [(disk_name(d), d) for name, d in hwmons() if name == "drivetemp"]


def cpu_temp():
    vals = []
    for name, d in hwmons():
        if name != "coretemp":
            continue
        for f in glob.glob(os.path.join(d, "temp*_input")):
            try:
                vals.append(rd_int(f) / 1000.0)
            except (OSError, ValueError):
                pass
    return max(vals) if vals else None


# --------------------------------------------------------------- config ----

def load_config():
    raw = dict.fromkeys(KEYS)
    raw.update(DEFAULTS)
    try:
        with open(CONF) as f:
            for line in f:
                m = re.match(r'\s*(RN_[A-Z_]+)\s*=\s*"?([^"#\s]*)', line)
                if m and m.group(1) in raw and m.group(2):
                    raw[m.group(1)] = m.group(2)
    except FileNotFoundError:
        pass
    for k in KEYS:
        if os.environ.get(k):
            raw[k] = os.environ[k]
    cfg = {}
    for k, v in raw.items():
        try:
            cfg[k] = None if v is None else int(v)
        except ValueError:
            raise Unsafe("%s=%r in %s is not a whole number" % (k, v, CONF))
    return cfg


def checked_config():
    cfg = load_config()
    missing = [k for k in ("RN_FAN_PWM", "RN_FAN_TACH", "RN_FAN_MIN")
               if cfg[k] is None]
    if missing:
        raise Unsafe("not calibrated yet (%s missing in %s); run: "
                     "rn424_fan.py calibrate" % (", ".join(missing), CONF))
    if not 0 < cfg["RN_FAN_MIN"] <= cfg["RN_FAN_MAX"] <= FULL:
        raise Unsafe("need 0 < RN_FAN_MIN <= RN_FAN_MAX <= 255")
    if cfg["RN_DISK_LOW"] >= cfg["RN_DISK_HIGH"]:
        raise Unsafe("need RN_DISK_LOW < RN_DISK_HIGH")
    if cfg["RN_CPU_LOW"] >= cfg["RN_CPU_HIGH"]:
        raise Unsafe("need RN_CPU_LOW < RN_CPU_HIGH")
    if not 2 <= cfg["RN_INTERVAL"] <= 60:
        raise Unsafe("need 2 <= RN_INTERVAL <= 60")
    return cfg


def save_config(updates):
    try:
        with open(CONF) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = CONF_TEMPLATE.splitlines()
    for k, v in updates.items():
        pat = re.compile(r"^\s*#?\s*%s\s*=" % k)
        for i, line in enumerate(lines):
            if pat.match(line):
                lines[i] = "%s=%s" % (k, v)
                break
        else:
            lines.append("%s=%s" % (k, v))
    tmp = CONF + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, CONF)


# ----------------------------------------------------- lock and signals ----

def take_lock(wait=0.0):
    f = open(LOCKFILE, "w")
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except BlockingIOError:
            if time.monotonic() >= deadline:
                f.close()
                raise Busy()
            time.sleep(0.5)


def _raise_stop(signum, frame):
    raise Stop()


def catch_signals():
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, _raise_stop)


def ignore_signals():
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, signal.SIG_IGN)


def sd_notify(msg):
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr[0] == "@":
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
    except OSError:
        pass


# ------------------------------------------------------ BIOS state save ----

def save_state(chip, d, ch):
    """Remember the fan's pre-service setting, once per boot (a restart
    after a crash must not overwrite it with our own values)."""
    if os.path.exists(STATE):
        return
    st = {"chip": chip, "channel": ch,
          "enable": rd_int(enable_path(d, ch)), "pwm": rd_int(pwm_path(d, ch))}
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE)


def restore_state():
    """Put back what save_state() recorded. No state file = we never
    touched the fan, so there is nothing to undo."""
    try:
        with open(STATE) as f:
            st = json.load(f)
        ch, enable, duty = int(st["channel"]), int(st["enable"]), int(st["pwm"])
    except FileNotFoundError:
        return False
    except (OSError, ValueError, KeyError, TypeError):
        st = None
    chip, d = fan_chip()
    if d is None:
        log("restore: fan chip not found, cannot restore")
        return False
    if st is None:
        ch = load_config().get("RN_FAN_PWM")
        if ch is None:
            log("restore: state file unreadable and no RN_FAN_PWM set")
            return False
        log("restore: state file unreadable, setting pwm%d to full speed" % ch)
        wr(enable_path(d, ch), 1)
        wr(pwm_path(d, ch), FULL)
    else:
        wr(enable_path(d, ch), 1)
        wr(pwm_path(d, ch), duty)
        if enable != 1:
            wr(enable_path(d, ch), enable)
        log("restored pwm%d to its previous setting (mode %d, duty %d/255)"
            % (ch, enable, duty))
    try:
        os.remove(STATE)
    except FileNotFoundError:
        pass
    return True


# ---------------------------------------------------------------- probe ----

def probe():
    ok = True
    chip, d = fan_chip()
    try:
        drv = rd(os.path.join(SYS, "module", "it87", "version"))
    except OSError:
        drv = "no version (Debian's built-in driver?)"
    log("Fan chip:     %s" % ("%s  (%s)" % (chip, d) if chip
                              else "NOT FOUND - is the it87 driver loaded?"))
    log("it87 driver:  %s" % drv)
    if chip:
        spinning = 0
        for n in channels(d, "fan"):
            try:
                rpm = rd_int(tach_path(d, n))
            except (OSError, ValueError):
                rpm = -1
            spinning += rpm > 0
            log("  fan%d   %5d rpm" % (n, rpm))
        for n in channels(d, "pwm"):
            try:
                v, e = rd_int(pwm_path(d, n)), rd_int(enable_path(d, n))
                log("  pwm%d   %3d/255 (%d%%)  mode %d" % (n, v, v * 100 // 255, e))
            except (OSError, ValueError):
                log("  pwm%d   unreadable" % n)
        if not spinning:
            log("  ! no fan reports a speed")
            ok = False
    else:
        ok = False
    dl = disks()
    for name, dd in dl:
        try:
            log("Disk %-8s %d C" % (name, rd_int(os.path.join(dd, "temp1_input")) // 1000))
        except (OSError, ValueError):
            log("Disk %-8s unreadable" % name)
    if not dl:
        log("Disks:        NO drivetemp sensors - run: modprobe drivetemp")
        ok = False
    c = cpu_temp()
    log("CPU:          %s" % ("%d C" % c if c is not None else "NOT FOUND (coretemp)"))
    ok = ok and c is not None
    try:
        cfg = checked_config()
        log("Settings:     pwm%d / fan%d, duty %d-%d, disk %d-%d C, CPU %d-%d C"
            % (cfg["RN_FAN_PWM"], cfg["RN_FAN_TACH"], cfg["RN_FAN_MIN"],
               cfg["RN_FAN_MAX"], cfg["RN_DISK_LOW"], cfg["RN_DISK_HIGH"],
               cfg["RN_CPU_LOW"], cfg["RN_CPU_HIGH"]))
    except Unsafe as e:
        log("Settings:     %s" % e)
    log("Result: %s" % ("READY" if ok else "NOT READY"))
    return 0 if ok else EXIT_UNSAFE


# ------------------------------------------------------------ calibrate ----

def calibrate():
    chip, d = fan_chip()
    if d is None:
        raise Unsafe("fan chip not found - is the it87 driver loaded?")
    fans = []
    for n in channels(d, "fan"):
        try:
            if rd_int(tach_path(d, n)) > 0:
                fans.append(n)
        except (OSError, ValueError):
            pass
    pwms = channels(d, "pwm")
    if not fans or not pwms:
        raise Unsafe("need a spinning fan and a PWM output (fans %s, pwms %s)"
                     % (fans, pwms))
    saved = {n: (rd_int(enable_path(d, n)), rd_int(pwm_path(d, n))) for n in pwms}

    def setp(n, v):
        wr(enable_path(d, n), 1)
        wr(pwm_path(d, n), v)

    def put_back(n):
        e, v = saved[n]
        wr(enable_path(d, n), 1)
        wr(pwm_path(d, n), v)
        if e != 1:
            wr(enable_path(d, n), e)

    def rpm(n):
        return rd_int(tach_path(d, n))

    try:
        found = None
        for f in fans:
            for p in [f] + [x for x in pwms if x != f]:
                if p not in pwms:
                    continue
                log("Testing pwm%d against fan%d ..." % (p, f))
                setp(p, FULL)
                time.sleep(SETTLE)
                hi = rpm(f)
                setp(p, 64)
                time.sleep(SETTLE)
                lo = rpm(f)
                put_back(p)
                log("  pwm%d at 100%%: %d rpm, at 25%%: %d rpm" % (p, hi, lo))
                if lo > hi * 1.2 and lo - hi > 150:
                    raise Unsafe("the fan runs FASTER at low PWM (inverted "
                                 "polarity) - not supported, nothing changed")
                if hi > lo * 1.2 and hi - lo > 150:
                    found = (p, f)
                    break
                time.sleep(SETTLE)
            if found:
                break
        if not found:
            raise Unsafe("no PWM output changes the fan's speed (3-pin fan "
                         "on a PWM-only header?) - nothing changed")
        p, f = found
        log("Fan is on pwm%d / fan%d. Finding the lowest safe speed:" % (p, f))
        last_ok, top, stalled_at = None, None, None
        for duty in SWEEP:
            setp(p, duty)
            time.sleep(SETTLE)
            r = rpm(f)
            top = top or r
            log("  duty %3d/255 (%3d%%): %5d rpm" % (duty, duty * 100 // 255, r))
            if r < max(150, top * 0.08):
                stalled_at = duty
                break
            last_ok = duty
        if stalled_at is not None:
            setp(p, FULL)               # restart a stopped fan
            time.sleep(3)
        if last_ok is None:
            raise Unsafe("fan doesn't spin properly even at full speed")
        fmin = max(40, min(160, last_ok + STALL_BUMP))
        save_config({"RN_FAN_PWM": p, "RN_FAN_TACH": f, "RN_FAN_MIN": fmin})
        if stalled_at is not None:
            log("Fan stopped at %d/255; lowest speed that still spun: %d/255."
                % (stalled_at, last_ok))
        else:
            log("Fan still spinning at %d/255." % last_ok)
        log("Minimum set to %d/255 (%d%%) for a safety margin. Saved to %s"
            % (fmin, fmin * 100 // 255, CONF))
        return 0
    finally:
        ignore_signals()
        for n in pwms:
            try:
                put_back(n)
            except OSError as e:
                log("! could not restore pwm%d: %s" % (n, e))


# ------------------------------------------------------------------ run ----

def curve(t, lo, hi, fmin, fmax):
    if t <= lo:
        return fmin
    if t >= hi:
        return fmax
    return int(round(fmin + (t - lo) * (fmax - fmin) / float(hi - lo)))


_never_read = set()


def read_temps(cache, now):
    """Returns ({disk: C}, cpu C), or raises Unsafe with the reason.

    A disk that stops answering keeps its last reading for DISK_CACHE_S,
    then forces full speed. A device that has never given a reading since
    the service started (e.g. a USB stick that drivetemp attached to) is
    skipped, as long as at least one other disk reports."""
    dl = disks()
    if not dl:
        raise Unsafe("no disk temperature sensors (is drivetemp loaded?)")
    vals = {}
    for name, d in dl:
        try:
            t = rd_int(os.path.join(d, "temp1_input")) / 1000.0
            cache[name] = (t, now)
            _never_read.discard(name)
        except (OSError, ValueError):
            c = cache.get(name)
            if c is None:
                if name not in _never_read:
                    _never_read.add(name)
                    log("note: %s has a temperature sensor but gives no "
                        "reading; ignoring it" % name)
                continue
            if now - c[1] > DISK_CACHE_S:
                raise Unsafe("cannot read the temperature of %s" % name)
            t = c[0]
        vals[name] = t
    if not vals:
        raise Unsafe("no disk gives a temperature reading")
    cpu = cpu_temp()
    if cpu is None:
        raise Unsafe("no CPU temperature (coretemp)")
    return vals, cpu


def run():
    cfg = checked_config()
    ch, tach = cfg["RN_FAN_PWM"], cfg["RN_FAN_TACH"]
    fmin, fmax, interval = cfg["RN_FAN_MIN"], cfg["RN_FAN_MAX"], cfg["RN_INTERVAL"]

    deadline = time.monotonic() + 60       # the driver may still be loading
    chip, d = fan_chip()
    while d is None and time.monotonic() < deadline:
        time.sleep(2)
        chip, d = fan_chip()
    if d is None:
        raise Unsafe("fan chip not found - is the it87 driver loaded?")
    if not (os.path.exists(pwm_path(d, ch)) and os.path.exists(tach_path(d, tach))):
        raise Unsafe("%s has no pwm%d/fan%d - recalibrate" % (chip, ch, tach))

    save_state(chip, d, ch)
    wr(enable_path(d, ch), 1)
    duty = rd_int(pwm_path(d, ch))          # start from the current speed
    log("controlling %s pwm%d (fan%d), duty %d-%d, disk %d-%d C, CPU %d-%d C"
        % (chip, ch, tach, fmin, fmax, cfg["RN_DISK_LOW"], cfg["RN_DISK_HIGH"],
           cfg["RN_CPU_LOW"], cfg["RN_CPU_HIGH"]))
    sd_notify("READY=1")

    cache = {}
    stalls = 0
    boost_until = 0.0
    logged = (None, None, 0.0)      # (duty, problem, when)
    while True:
        now = time.monotonic()
        chip, d = fan_chip()
        if d is None:
            raise RuntimeError("fan chip disappeared")
        problem = None
        try:
            dt, cpu = read_temps(cache, now)
            target = max(curve(max(dt.values()), cfg["RN_DISK_LOW"],
                               cfg["RN_DISK_HIGH"], fmin, fmax),
                         curve(cpu, cfg["RN_CPU_LOW"], cfg["RN_CPU_HIGH"],
                               fmin, fmax))
        except Unsafe as e:
            problem, dt, cpu, target = str(e), {}, None, FULL
        if now < boost_until:
            target = FULL
        if target >= duty:
            duty = target
        elif duty - target >= DEADBAND:
            duty = max(target, duty - STEP_DOWN)
        wr(pwm_path(d, ch), duty)

        try:
            rpm = rd_int(tach_path(d, tach))
        except (OSError, ValueError):
            rpm = None
            problem = problem or "cannot read the fan speed"
        stalls = stalls + 1 if rpm == 0 else 0
        if stalls >= 2:                     # 0 rpm on two readings in a row
            if now >= boost_until and duty < FULL:
                fmin = min(fmax, 192, fmin + STALL_BUMP)
                log("! fan stopped at %d/255 - minimum raised to %d/255"
                    % (duty, fmin))
            boost_until = now + 30          # full speed until 30 s after it spins
            problem = problem or "fan not spinning (0 rpm)"
        if problem and duty < FULL:
            duty = FULL
            wr(pwm_path(d, ch), duty)

        last_duty, last_problem, last_when = logged
        if (problem != last_problem or last_duty is None
                or abs(duty - last_duty) >= 16 or now - last_when >= 1800):
            if problem:
                log("! %s -> full speed" % problem)
            else:
                log("disk %s, CPU %d C -> fan %d%% (%d/255), %s rpm"
                    % (", ".join("%s %d C" % kv for kv in sorted(dt.items())),
                       cpu, duty * 100 // 255, duty,
                       rpm if rpm is not None else "?"))
            logged = (duty, problem, now)

        sd_notify("WATCHDOG=1")
        time.sleep(interval)


# ----------------------------------------------------------------- main ----

USAGE = "usage: rn424_fan.py probe|calibrate|run|restore"


def main(argv):
    mode = argv[1] if len(argv) > 1 else ""
    if mode not in ("probe", "calibrate", "run", "restore"):
        print(USAGE, file=sys.stderr)
        return 2
    if mode == "probe":
        return probe()
    try:
        lock = take_lock({"run": 30, "restore": 10}.get(mode, 0))
    except Busy:
        log("busy: the fan service (or another copy) is running; stop it "
            "first: systemctl stop rn424-fan")
        return EXIT_BUSY
    catch_signals()
    try:
        if mode == "calibrate":
            try:
                return calibrate()
            except (OSError, ValueError) as e:
                log("error: %s - fan settings put back, nothing saved" % e)
                return 1
        if mode == "restore":
            ignore_signals()
            restore_state()
            return 0
        try:
            run()
        finally:
            ignore_signals()
            restore_state()
    except Stop:
        if mode == "calibrate":
            log("interrupted - fan settings put back, nothing saved")
            return 1
        return 0
    except Unsafe as e:
        log("stopped: %s" % e)
        return EXIT_UNSAFE
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
