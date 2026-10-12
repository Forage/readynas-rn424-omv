#!/usr/bin/env python3
"""
rn424_panel.py - front-panel display + buttons for NETGEAR ReadyNAS RN422/RN424
(also RN426/RN428) running OpenMediaVault or plain Debian.

https://github.com/dgalue/readynas-rn424-omv  -  MIT licence

Port of riplatt/truenas-rn426-panel (MIT licence) to the RN422/RN424 board and
to OMV. Everything model-specific below was decoded from NETGEAR's own
ReadyNAS OS 6 kernel (4.4.218 and 4.4.178 agree byte-for-byte):

  * rn_oled config table, entry 'rn422_4' (= RN422 + RN424):
        gpio_dnv.0 pins MOSI=34 CLK=35 DC=0 CS=122 EN=17 RST=31
    entry 'rn426_8' (= RN426 + RN428): 30 28 7 8 17 31
  * gpio_dnv's reg_map_offset_dnv()/table5 turns those into PADCFG_DW0
    offsets (PADBAR 0x400; pins 3-13, 53-140 and 146-152 live in the South
    community at +0x30000).
  * The stock kernel hard-codes the GPIO MMIO window 0xFDC20000-0xFDC5FFFF
    and never touches P2SB, so neither does this driver. (The upstream RN426
    script "unhides" P2SB with byte writes to I/O ports 0xCF8-0xCFB; 0xCF9 is
    the chipset reset-control register, so that step is deliberately dropped.)
  * SSD1305 init sequence (33 bytes) and the 128x32 / 4-page geometry come
    from init_oled()/clear_oled().
  * Buttons: rn42x front-board table = MSP430 at SMBus 0x1C on the I801 bus,
    reg 0x04 bitmap (left 0, right 1, up 2, down 3, ok 4); its interrupt line
    is gpio_dnv pin 5 = South PADCFG 0x570, active low.

  * PADCFG_DW0 bits, confirmed from the stock dnv_gpio_direction_output/
    _input: bit 0 TX level, bit 1 RX level, bit 8 TX disable, bit 9 RX
    disable, bits 10-12 pad mode, bits 17-20 interrupt routing. (The upstream
    RN426 script has bits 8 and 9 swapped.)

Safety rules:
  * The MSP430 is only ever READ. Never write its reg 0x02.
  * EN and RST are shared with the MSP430: this driver never drives them low.
  * Only bit 0 (output level) of a pad register is ever changed. Pad
    direction, input buffers and interrupt routing are left exactly as the
    BIOS set them.
  * Before touching anything it checks the DMI model, both PADBAR registers,
    and that every display pad is a plain GPIO, already an output, with no
    interrupt routing. Any mismatch = abort with exit code 3, nothing written.
  * One instance at a time (lock in /run).

Usage:  rn424_panel.py probe | test | run | sleep | wake | splash [l1 [l2]]
Config: /etc/default/rn424-panel (see install.sh), or environment:
        RN_SLEEP=120   idle seconds before the display blanks (0 = never)
        RN_ROTATE=0    seconds between automatic page changes (0 = manual)
"""
import ctypes
import fcntl
import glob
import mmap
import os
import re
import signal
import socket
import subprocess
import sys
import time

DEVMEM = os.environ.get("RN_DEVMEM", "/dev/mem")              # test hook
DMI_DIR = os.environ.get("RN_DMI_DIR", "/sys/class/dmi/id")   # test hook

NORTH_BASE, SOUTH_BASE = 0xFDC20000, 0xFDC50000
PADBAR = 0x400
N, S = "N", "S"
EXIT_UNSAFE = 3
EXIT_BUSY = 4

PROFILES = {
    "rn422_4": {
        "models": "RN422/RN424",
        "match": re.compile(r"(ReadyNAS 42[24]|RN42[24])(?=$|[\sXS])"),
        "pads": {"MOSI": (N, 0x4A0), "CLK": (N, 0x4A8), "DC": (N, 0x4D8),
                 "CS": (S, 0x560), "EN": (N, 0x418), "RST": (N, 0x488)},
    },
    "rn426_8": {
        "models": "RN426/RN428",
        "match": re.compile(r"(ReadyNAS 42[68]|RN42[68])(?=$|[\sXS])"),
        "pads": {"MOSI": (N, 0x480), "CLK": (N, 0x470), "DC": (S, 0x580),
                 "CS": (S, 0x5C8), "EN": (N, 0x418), "RST": (N, 0x488)},
    },
}
BTN_INT = (S, 0x570)   # MSP430 interrupt line, RXSTATE bit 1, active low

INIT_SEQ = bytes.fromhex(
    "aed571a81fd9222002a1c8da12d80081cfb0d300210483220003100040a6a4db18")

TXSTATE, RXSTATE, TXDIS, RXDIS = 1 << 0, 1 << 1, 1 << 8, 1 << 9
IRQ_ROUTE = 0xF << 17          # GPIROUTNMI / SMI / SCI / IOxAPIC
LOCKFILE = os.environ.get("RN_LOCK", "/run/rn424-panel.lock")


class Unsafe(Exception):
    """A pre-flight check failed; nothing has been written to the hardware."""


class Busy(Exception):
    """Another instance holds the panel."""


# ---------------------------------------------------------------- model check
def _dmi(field):
    try:
        with open(os.path.join(DMI_DIR, field)) as f:
            return f.read().strip()
    except OSError:
        return ""


def detect_profile():
    ident = "%s | %s" % (_dmi("product_name"), _dmi("product_version"))
    for key, prof in PROFILES.items():
        if prof["match"].search(ident):
            return key, prof, ident
    return None, None, ident


# ---------------------------------------------------------------- SoC GPIO pads
class GpioWindow:
    """32-bit MMIO access to the Denverton North/South GPIO communities."""

    def __init__(self):
        try:
            fd = os.open(DEVMEM, os.O_RDWR | os.O_SYNC)
        except OSError as e:
            raise Unsafe("cannot open %s: %s" % (DEVMEM, e))
        try:
            self._maps = {}
            for name, base in ((N, NORTH_BASE), (S, SOUTH_BASE)):
                self._maps[name] = mmap.mmap(
                    fd, 0x1000, mmap.MAP_SHARED,
                    mmap.PROT_READ | mmap.PROT_WRITE, offset=base)
        except OSError as e:
            raise Unsafe("mmap of the GPIO window failed (%s). A kernel driver "
                         "probably owns it - check 'grep -i fdc /proc/iomem' "
                         "and do not force access with iomem=relaxed." % e)
        finally:
            os.close(fd)
        # memoryview cast to 'I' gives real 32-bit loads/stores
        self.mv = {k: memoryview(m).cast("I") for k, m in self._maps.items()}

    def read(self, loc):
        return self.mv[loc[0]][loc[1] // 4]

    def write(self, loc, value):
        self.mv[loc[0]][loc[1] // 4] = value & 0xFFFFFFFF


class Pad:
    def __init__(self, win, name, loc):
        self.win, self.name, self.loc = win, name, loc
        self.dw0 = win.read(loc)
        base = self.dw0 & ~TXSTATE               # only the level bit changes
        self._lo, self._hi = base, base | TXSTATE
        self._idx, self._mv = loc[1] // 4, win.mv[loc[0]]

    @property
    def mode(self):
        return (self.dw0 >> 10) & 0x7            # PMODE, 0 = GPIO

    @property
    def is_output(self):
        return not self.dw0 & TXDIS

    def describe(self):
        d = self.dw0
        return "%-4s %s+0x%03x DW0=0x%08x mode=%d %s tx=%d rx=%d route=%x" % (
            self.name, self.loc[0], self.loc[1], d, self.mode,
            "output" if self.is_output else "input ",
            d & TXSTATE, (d & RXSTATE) >> 1, (d & IRQ_ROUTE) >> 17)

    def set(self, level):
        self._mv[self._idx] = self._hi if level else self._lo

    def ensure_high(self):
        """For the shared EN/RST lines: only ever drive high, never low."""
        if not self.win.read(self.loc) & TXSTATE:
            self.set(1)


def preflight(verbose=False):
    """Read-only checks. Returns (profile_key, window, pads) or raises Unsafe."""
    key, prof, ident = detect_profile()
    if verbose:
        print("DMI           : %s" % ident)
    if not key:
        raise Unsafe("this is not a ReadyNAS RN422/424/426/428 (DMI: %s)"
                     % ident)
    if verbose:
        print("Profile       : %s (%s)" % (key, prof["models"]))
    win = GpioWindow()
    for c, base in ((N, NORTH_BASE), (S, SOUTH_BASE)):
        padbar = win.read((c, 0x0C))
        if verbose:
            print("PADBAR %s     : 0x%08x @ 0x%08x (expect 0x400)"
                  % (c, padbar, base + 0x0C))
        if padbar != PADBAR:
            raise Unsafe("PADBAR of the %s GPIO community reads 0x%x, expected "
                         "0x400 - wrong address window, refusing to write"
                         % ("North" if c == N else "South", padbar))
    pads = {n: Pad(win, n, loc) for n, loc in prof["pads"].items()}
    pads["INT"] = Pad(win, "INT", BTN_INT)
    problems = []
    for p in pads.values():
        if verbose:
            print("Pad           : " + p.describe())
        if p.dw0 == 0xFFFFFFFF or p.mode != 0:
            problems.append("%s is not a plain GPIO" % p.name)
        elif p.name != "INT" and not p.is_output:
            problems.append("%s is not set up as an output" % p.name)
        elif p.name != "INT" and p.dw0 & IRQ_ROUTE:
            problems.append("%s has interrupt routing enabled" % p.name)
    if problems:
        raise Unsafe("; ".join(problems) + " - refusing to drive the pads")
    return key, win, pads


class InstanceLock:
    """Two bit-bangers at once would garble the display; allow only one."""

    def __init__(self, wait=0.0):
        self.wait = wait

    def __enter__(self):
        self.fd = os.open(LOCKFILE, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.wait
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(self.fd)
                    raise Busy("the panel is already in use (is the service "
                               "running? stop it with: systemctl stop "
                               "rn424-panel)")
                time.sleep(0.5)

    def __exit__(self, *exc):
        os.close(self.fd)


# ---------------------------------------------------------------- display
class LCD:
    """SSD1305 128x32 on bit-banged SPI. Visible columns are 4..131."""
    W, H = 132, 32

    def __init__(self, pads):
        self.cs, self.dc = pads["CS"], pads["DC"]
        self.clk, self.mosi = pads["CLK"], pads["MOSI"]
        self.en, self.rst = pads["EN"], pads["RST"]
        self.f1 = self.f2 = None

    def _byte(self, b, is_data):
        self.cs.set(0)
        self.dc.set(is_data)
        for k in range(7, -1, -1):               # MSB first, latch on rising CLK
            self.clk.set(0)
            self.mosi.set((b >> k) & 1)
            self.clk.set(1)
        self.dc.set(1)
        self.cs.set(1)

    def cmd(self, b):
        self._byte(b, 0)

    def init(self):
        self.cs.set(1)
        self.clk.set(0)
        self.mosi.set(0)
        self.dc.set(0)
        # NetGear's cold-boot path pulses RST and toggles EN; both lines also
        # reset the MSP430, so here they are only ever held/driven high.
        self.rst.ensure_high()
        self.en.ensure_high()
        for b in INIT_SEQ:
            self.cmd(b)
        self.cmd(0xAF)                           # display on

    def off(self):
        self.cmd(0xAE)

    def on(self):
        self.cmd(0xAF)

    def show(self, img):
        px = img.load()
        for page in range(4):
            self.cmd(0xB0 | page)
            self.cmd(0x00)
            self.cmd(0x10)
            for c in range(self.W):
                byte = 0
                for r in range(8):
                    if px[c, page * 8 + r]:
                        byte |= 1 << r
                self._byte(byte, 1)

    def _fonts(self):
        if self.f1 is None:
            from PIL import ImageFont
            d = "/usr/share/fonts/truetype/dejavu/"
            try:
                self.f1 = ImageFont.truetype(d + "DejaVuSansMono-Bold.ttf", 16)
                self.f2 = ImageFont.truetype(d + "DejaVuSansMono.ttf", 13)
            except OSError:
                self.f1 = self.f2 = ImageFont.load_default()
        return self.f1, self.f2

    def lines(self, l1, l2, shift=0):
        from PIL import Image, ImageDraw
        f1, f2 = self._fonts()
        img = Image.new("1", (self.W, self.H), 0)
        d = ImageDraw.Draw(img)
        d.text((4 + shift, -2), l1[:13], font=f1, fill=1)
        d.text((4 + shift, 17), l2[:16], font=f2, fill=1)
        self.show(img)


# ---------------------------------------------------------------- buttons
I2C_SLAVE, I2C_SMBUS = 0x0703, 0x0720
I2C_SMBUS_READ, I2C_SMBUS_BYTE_DATA = 1, 2


class _SmbusIoctl(ctypes.Structure):
    _fields_ = [("read_write", ctypes.c_ubyte), ("command", ctypes.c_ubyte),
                ("size", ctypes.c_uint), ("data", ctypes.c_void_p)]


def find_i801_bus():
    """i2c-N numbers swap between boots (i801 vs iSMT), so match by name."""
    for d in sorted(glob.glob("/sys/class/i2c-dev/i2c-*")):
        try:
            with open(os.path.join(d, "name")) as f:
                if "I801" in f.read():
                    return int(d.rsplit("-", 1)[1])
        except (OSError, ValueError):
            pass
    return None


class Buttons:
    LEFT, RIGHT, UP, DOWN, OK = 0x01, 0x02, 0x04, 0x08, 0x10
    ADDR, REG = 0x1C, 0x04                       # READ ONLY - never write the MCU

    def __init__(self, bus):
        self.fd = os.open("/dev/i2c-%d" % bus, os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, self.ADDR)

    def read(self):
        buf = (ctypes.c_ubyte * 34)()
        req = _SmbusIoctl(I2C_SMBUS_READ, self.REG, I2C_SMBUS_BYTE_DATA,
                          ctypes.cast(buf, ctypes.c_void_p))
        fcntl.ioctl(self.fd, I2C_SMBUS, req)
        return buf[0]


def open_buttons():
    bus = find_i801_bus()
    if bus is None:
        return None, "no I801 SMBus adapter (modprobe i2c-dev i2c-i801)"
    try:
        return Buttons(bus), "i2c-%d" % bus
    except OSError as e:
        return None, "i2c-%d: %s" % (bus, e)


# ---------------------------------------------------------------- info pages
def _human(n):
    """Binary units, same as OMV and lsblk (16.4T for an 18 TB drive)."""
    for unit in ("B", "K", "M", "G", "T", "P"):
        if n < 1000 or unit == "P":
            return ("%.1f%s" % (n, unit)) if n < 100 else ("%d%s" % (n, unit))
        n /= 1024.0


def _ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))              # UDP connect sends nothing
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        pass
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True,
                             text=True, timeout=2).stdout.split()
        return out[0] if out else "no network"
    except (OSError, subprocess.SubprocessError):
        return "no network"


def data_volumes():
    """OMV mounts data file systems under /srv/dev-disk-by-*."""
    vols, seen = [], set()
    try:
        with open("/proc/self/mounts") as f:
            for line in f:
                dev, mnt, fstype = line.split()[:3]
                if not mnt.startswith("/srv/") or dev in seen:
                    continue
                if fstype not in ("btrfs", "ext4", "xfs", "f2fs", "zfs"):
                    continue
                seen.add(dev)
                st = os.statvfs(mnt.replace("\\040", " "))
                total = st.f_blocks * st.f_frsize
                free = st.f_bavail * st.f_frsize
                if total:
                    vols.append((total, free, mnt))
    except OSError:
        pass
    return sorted(vols, reverse=True)


def md_arrays(path="/proc/mdstat"):
    """[(name, level, status_brackets, progress_or_None, blocks)]"""
    out = []
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for i, line in enumerate(lines):
        m = re.match(r"^(md\d+) : (\S+)", line)
        if not m:
            continue
        name, state = m.groups()
        level = re.search(r"\b(raid\d+|linear)\b", line)
        detail = " ".join(lines[i + 1:i + 3])
        blocks = re.search(r"(\d+) blocks", detail)
        status = re.search(r"\[([U_]+)\]", detail)
        prog = re.search(r"(resync|recovery|reshape|check)\s*=\s*([\d.]+)%",
                         detail)
        out.append((name, level.group(1) if level else state,
                    status.group(1) if status else "",
                    "%s %s%%" % prog.groups() if prog else None,
                    int(blocks.group(1)) if blocks else 0))
    return sorted(out, key=lambda a: -a[4])


def page_host():
    return socket.gethostname()[:13], _ip()


def page_volume():
    vols = data_volumes()
    if not vols:
        return "Data", "not mounted"
    total, free, _ = vols[0]
    used = 100 - int(round(100.0 * free / total))
    return "Data %d%% used" % used, "%s free/%s" % (_human(free), _human(total))


def page_raid():
    arrays = md_arrays()
    if not arrays:
        return None
    name, level, status, prog, _ = arrays[0]
    if prog:
        return "%s %s" % (name, level), prog
    if "_" in status:
        return "%s %s" % (name, level), "DEGRADED [%s]" % status
    return "%s %s" % (name, level), "OK [%s]" % status


def _hwmon(name):
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            if open(hw + "/name").read().strip() == name:
                yield hw
        except OSError:
            pass


def page_system():
    temp = None
    for hw in _hwmon("coretemp"):
        vals = []
        for f in glob.glob(hw + "/temp*_input"):
            try:
                vals.append(int(open(f).read()) // 1000)
            except (OSError, ValueError):
                pass
        if vals:
            temp = max(vals)
    l1 = "CPU %dC" % temp if temp is not None else "CPU temp n/a"
    for f in sorted(glob.glob("/sys/class/hwmon/hwmon*/fan*_input")):
        try:
            rpm = int(open(f).read())
        except (OSError, ValueError):
            continue
        if rpm > 0:
            return l1, "Fan %d rpm" % rpm
    try:
        mem = dict(l.split(":", 1) for l in open("/proc/meminfo"))
        tot = int(mem["MemTotal"].split()[0])
        avail = int(mem["MemAvailable"].split()[0])
        return l1, "RAM %d%% used" % (100 - 100 * avail // tot)
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        return l1, ""


def page_uptime():
    up = float(open("/proc/uptime").read().split()[0])
    d, h, m = int(up // 86400), int(up % 86400 // 3600), int(up % 3600 // 60)
    load = open("/proc/loadavg").read().split()[0]
    return ("Up %dd %dh" % (d, h) if d else "Up %dh %dm" % (h, m),
            "Load " + load)


PAGES = [page_host, page_volume, page_raid, page_system, page_uptime]


def alerts():
    out = []
    for name, _, status, _, _ in md_arrays():
        if "_" in status:
            out.append(("! RAID DEGRADED", "%s [%s]" % (name, status)))
    for total, free, _ in data_volumes():
        if free < total * 0.10:
            out.append(("! DISK >90%", "%s free" % _human(free)))
    return out


# ---------------------------------------------------------------- main loop
def _env_int(name, default):
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        print("rn424-panel: ignoring bad %s=%r" % (name, os.environ[name]),
              flush=True)
        return default


def run():
    sleep_after = _env_int("RN_SLEEP", 120)
    rotate = _env_int("RN_ROTATE", 0)
    _, _, pads = preflight()
    lcd = LCD(pads)
    lcd.init()
    btn, btn_info = open_buttons()
    print("rn424-panel: display ready, buttons: %s"
          % ("on " + btn_info if btn else "off (" + btn_info + ")"), flush=True)
    if btn is None:
        sleep_after = 0                          # nothing could wake it again
        rotate = rotate or 8

    stop = {"flag": False}

    def _term(*_):
        stop["flag"] = True
    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)

    int_pad = pads["INT"]
    clock = time.monotonic                       # immune to NTP clock steps
    start = clock()
    idx, last_show, last_rot = 0, -1e9, start
    activity, asleep, armed, high_since = start, False, True, start
    last_alert_check, active_alerts = -1e9, []

    while not stop["flag"]:
        now = clock()

        # --- buttons: read the MCU only when its interrupt line says so
        if btn is not None:
            if not int_pad.win.read(int_pad.loc) & RXSTATE:      # active low
                high_since = None
                if armed:
                    armed = False
                    try:
                        v = btn.read()
                    except OSError:
                        v = 0
                    if v:
                        activity = now
                        if asleep:
                            lcd.on()
                            asleep, last_show = False, -1e9
                        else:
                            if v & (Buttons.UP | Buttons.LEFT):
                                idx -= 1
                            if v & (Buttons.DOWN | Buttons.RIGHT):
                                idx += 1
                            last_show = -1e9
            else:
                if high_since is None:
                    high_since = now
                elif now - high_since >= 0.05:
                    armed = True

        # --- alerts wake the display and jump to the alert page
        if now - last_alert_check >= 30:
            last_alert_check = now
            new = alerts()
            if new and not active_alerts:
                activity, idx, last_show = now, 0, -1e9
                if asleep:
                    lcd.on()
                    asleep = False
            active_alerts = new

        pages = [(lambda a=a: a) for a in active_alerts] + PAGES

        if not asleep:
            if sleep_after and not active_alerts and now - activity > sleep_after:
                lcd.off()
                asleep = True
            else:
                if rotate and now - last_rot >= rotate:
                    idx, last_rot, last_show = idx + 1, now, -1e9
                if now - last_show >= 5:
                    content = None
                    for _ in range(len(pages)):          # skip empty pages
                        try:
                            content = pages[idx % len(pages)]()
                        except Exception as e:           # never die on a page
                            content = ("ERR", str(e)[:16])
                        if content:
                            break
                        idx += 1
                    shift = int(now // 60) % 3           # slow anti burn-in
                    lcd.lines(content[0], content[1], shift)
                    last_show = now
        time.sleep(0.005)

    # On shutdown leave a message; on a plain stop/uninstall blank the panel
    # so nothing stays lit forever.
    try:
        state = subprocess.run(["systemctl", "is-system-running"],
                               capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        state = ""
    if "stopping" in state:
        if asleep:
            lcd.on()
        lcd.lines("Shutting down", socket.gethostname()[:16])
    else:
        lcd.off()


# ---------------------------------------------------------------- CLI
def probe():
    """Read-only. Prints everything the driver would rely on."""
    ok = True
    try:
        preflight(verbose=True)
        print("GPIO checks   : PASS")
    except Unsafe as e:
        print("GPIO checks   : FAIL - %s" % e)
        ok = False
    bus = find_i801_bus()
    print("I801 SMBus    : %s" % ("i2c-%d" % bus if bus is not None else
                                  "not found (buttons disabled)"))
    if bus is None:
        try:
            dm = subprocess.run(["dmesg"], capture_output=True, text=True,
                                timeout=5).stdout
            for line in dm.splitlines():
                if "i801" in line.lower() or "smbus" in line.lower():
                    print("  dmesg: " + line.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        from PIL import Image  # noqa: F401
        print("Pillow        : ok")
    except ImportError:
        print("Pillow        : MISSING (apt install python3-pil)")
        ok = False
    print("Result        : %s" % ("READY" if ok else "NOT READY"))
    return 0 if ok else EXIT_UNSAFE


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "probe":
        return probe()
    if os.geteuid() != 0 and DEVMEM == "/dev/mem":
        print("run as root", file=sys.stderr)
        return 1
    if mode not in ("run", "test", "sleep", "wake", "splash"):
        print(__doc__)
        return 1
    wait = {"run": 30, "splash": 10}.get(mode, 0)
    try:
        with InstanceLock(wait):
            if mode == "run":
                run()
            elif mode == "splash":
                # Early-boot message, replacing the BIOS "NETGEAR Storage /
                # Welcome" frame until the main service takes over.
                l1 = sys.argv[2] if len(sys.argv) > 2 else "Booting..."
                l2 = sys.argv[3] if len(sys.argv) > 3 else \
                    socket.gethostname()[:16]
                _, _, pads = preflight()
                lcd = LCD(pads)
                lcd.init()
                lcd.lines(l1, l2)
            elif mode == "test":
                _, _, pads = preflight(verbose=True)
                lcd = LCD(pads)
                lcd.init()
                lcd.lines("Hello!", "Front panel OK")
                print("Look at the front display: it should say "
                      "'Hello! / Front panel OK'")
            else:
                _, _, pads = preflight()
                lcd = LCD(pads)
                for p, v in (("CS", 1), ("CLK", 0), ("MOSI", 0), ("DC", 0)):
                    pads[p].set(v)
                if mode == "sleep":
                    lcd.off()
                else:
                    lcd.on()
    except Unsafe as e:
        print("ABORTED, nothing written: %s" % e, file=sys.stderr)
        return EXIT_UNSAFE
    except Busy as e:
        print("BUSY: %s" % e, file=sys.stderr)
        return EXIT_BUSY
    return 0


if __name__ == "__main__":
    sys.exit(main())
