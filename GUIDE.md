# Converting a ReadyNAS RN424 to OpenMediaVault

A complete, tested walkthrough: OpenMediaVault 8 on a replacement internal USB
DOM, set up from a Windows PC with no monitor or keyboard on the NAS, keeping
the data that is already on a ReadyNAS-formatted drive.

**Tested on:** ReadyNAS 424 (BIOS "ReadyNAS 424 V3.11"), OpenMediaVault 8.3.1
(Debian 13), kernels 6.12 and 7.2, Windows 11 as the helper PC.

> ⚠️ **Back up anything you can't lose before you start.** Nothing here
> formats your data drive, but you are changing the boot device of the box
> that holds it.

---

## Contents

1. [What you need](#1-what-you-need)
2. [Why it's done this way](#2-why-its-done-this-way)
3. [Install OMV into a VirtualBox VM](#3-install-omv-into-a-virtualbox-vm)
4. [Prepare the image for a headless ReadyNAS](#4-prepare-the-image-for-a-headless-readynas)
5. [Convert and flash the image to the DOM](#5-convert-and-flash-the-image-to-the-dom)
6. [Swap the DOM and boot](#6-swap-the-dom-and-boot)
7. [Finish the network setup](#7-finish-the-network-setup)
8. [Reduce writes to the DOM](#8-reduce-writes-to-the-dom)
9. [Bring in an existing ReadyNAS data drive](#9-bring-in-an-existing-readynas-data-drive)
10. [Share it to Windows](#10-share-it-to-windows)
11. [Front panel display and buttons](#11-front-panel-display-and-buttons)
12. [Fan control](#12-fan-control)
13. [Troubleshooting](#13-troubleshooting)
14. [Going back to ReadyNAS OS](#14-going-back-to-readynas-os)

---

## 1. What you need

| Item | Notes |
|---|---|
| **9-pin USB DOM, 8 GB or larger** | Replaces the internal 256 MB DOM. A used industrial one (e.g. HGST/STEC `SLUFM8GU2AUI-A`, 8 GB) costs a few euros. 16 GB gives more headroom. |
| **9-pin header ↔ USB-A adapter** | To connect the DOM to your PC. Check the gender: the plug must fit your DOM (pins vs. holes). |
| **Micro-USB *data* cable** | For the NAS's serial console (rear port labelled **UART**). Charge-only cables won't work. |
| **Windows PC** | With [VirtualBox](https://www.virtualbox.org/), [balenaEtcher](https://etcher.balena.io/), [PuTTY](https://www.chiark.greenend.org.uk/~sgtatham/putty/) and the [Silicon Labs CP210x driver](https://www.silabs.com/developer-tools/usb-to-uart-bridge-vcp-drivers). |
| **OMV ISO** | From [openmediavault.org](https://www.openmediavault.org/download.html). This guide used `openmediavault_8.3.1-amd64.iso`. |
| **Ethernet cable** | The NAS must be wired to your router. |

About the RN424 hardware: Intel Atom C3338 (Denverton), 2 GB RAM, 4 bays, two
gigabit ports. The stock 256 MB DOM sits on an internal 9-pin USB header and
only holds Netgear's boot/recovery image; ReadyNAS OS itself lives on the data
disks. There's also an unused M.2 slot.

## 2. Why it's done this way

* **OMV is installed into a VM disk file, then written to the DOM in one go.**
  Installing directly onto the DOM through VirtualBox raw-disk passthrough
  failed repeatedly (red partitioner screens, `VERR_ACCESS_DENIED`) over a
  9-pin USB adapter. Building a file first and flashing it with Etcher just
  works.
* **The image is adjusted before flashing**, because inside the VM the network
  card is VirtualBox's (`enp0s3`), not the ReadyNAS's (`enp1s0f0`/`enp1s0f1`).
  Without the fix the NAS boots fine and then never appears on your network.
* **The serial console is turned on**, so you can watch the boot and log in
  without a screen.

## 3. Install OMV into a VirtualBox VM

1. VirtualBox → **New**:
   - Name `OMV-Internal`, ISO = the OMV ISO, type Linux / Debian (64-bit).
   - **Untick "Proceed with Unattended Installation"** (older versions: tick
     "Skip Unattended Installation").
   - 2048 MB RAM, 1 CPU, **"Enable EFI" unticked** (the image must boot in
     legacy BIOS mode).
   - Hard disk: create a new one of **6.8 GB** – it must end up smaller than
     your DOM. (A 6.8 GB VDI became a 7,301,444,096-byte image; an "8 GB" DOM
     is 8,011,120,640 bytes.)
2. **Start** the VM → **Install**.
3. Language, location, keyboard as you like. Set a **root password** and write
   it down – you'll need it over SSH and the serial console.
4. Partitioning: **Guided – use entire disk** is fine.
5. If asked about a package mirror, either is fine.
6. Install GRUB to **`/dev/sda`**.
7. At "Installation complete", let it reboot. You'll get a console showing
   `openmediavault login:` and an IP like `10.0.2.15` (VirtualBox's internal
   network).

## 4. Prepare the image for a headless ReadyNAS

### 4.1 Connect to the VM over SSH

Copy-pasting beats typing into the VM window.

1. With the VM **powered off**: Settings → Network → Adapter 1 (NAT) →
   **Port Forwarding** → **+**: Name `ssh`, TCP, Host IP `127.0.0.1`, Host
   Port `2222`, Guest Port `22`.
2. Start the VM and wait for the login prompt.
3. In **PowerShell**:
   ```
   ssh -p 2222 root@127.0.0.1
   ```
   Answer `yes`, enter the root password (OMV allows root SSH by default).

### 4.2 Apply the four fixes

Paste this whole block at the `root@openmediavault:~#` prompt:

```sh
# 1) DHCP on whatever Ethernet port the real hardware has
cat > /etc/netplan/99-readynas-catchall.yaml <<'EOF'
network:
  version: 2
  renderer: networkd
  ethernets:
    anyport:
      match:
        name: "en*"
      dhcp4: true
      optional: true
EOF
chmod 600 /etc/netplan/99-readynas-catchall.yaml
netplan generate && echo ">>> NETWORK OK"

# 2) GRUB, kernel messages and a login prompt on the serial console
grep -q 'console=ttyS0' /etc/default/grub || sed -i 's/^GRUB_CMDLINE_LINUX="\(.*\)"/GRUB_CMDLINE_LINUX="\1 console=tty0 console=ttyS0,115200n8"/' /etc/default/grub
sed -i '/^GRUB_TERMINAL=/d;/^GRUB_SERIAL_COMMAND=/d' /etc/default/grub
echo 'GRUB_TERMINAL="console serial"' >> /etc/default/grub
echo 'GRUB_SERIAL_COMMAND="serial --speed=115200 --unit=0 --word=8 --parity=no --stop=1"' >> /etc/default/grub
update-grub && echo ">>> SERIAL OK"

# 3) Put all storage drivers in the initramfs, not just the VM's
sed -i 's/^MODULES=.*/MODULES=most/' /etc/initramfs-tools/initramfs.conf
update-initramfs -u -k all && echo ">>> INITRAMFS OK"

# 4) No swap on the flash DOM
sed -i '/^[^#].*\sswap\s/s/^/#/' /etc/fstab && echo ">>> SWAP OK"
```

You should see `NETWORK OK`, `SERIAL OK`, `INITRAMFS OK` and `SWAP OK`. Then:

```
poweroff
```

Finally check VM Settings → System → Motherboard: **Enable EFI must be
unticked.**

## 5. Convert and flash the image to the DOM

1. Connect the DOM to the PC through the 9-pin adapter. **Line up the blocked
   hole with the missing pin** – reversed polarity can kill the DOM.
2. In **PowerShell**, convert the VM disk to a raw image and compare sizes:
   ```powershell
   $vdi = (Get-ChildItem "$env:USERPROFILE\VirtualBox VMs\OMV-Internal\*.vdi").FullName
   $img = "$env:USERPROFILE\omv_readynas.img"
   & "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe" clonemedium disk "$vdi" "$img" --format RAW
   "IMG bytes: " + (Get-Item $img).Length
   Get-Disk | Select-Object Number, FriendlyName, Size
   ```
   The image must be **no larger** than the DOM's `Size`. (An HGST DOM may
   show up as `STEC USB 2.0` – STEC was HGST's flash division.)
3. Disk Management: make sure the DOM is **Online**.
4. balenaEtcher → **Flash from file** → `omv_readynas.img` → **Select target**
   → *only* the DOM (double-check the size; never pick another disk) →
   **Flash**, with validation.
5. If Windows offers to format the disk afterwards: **Cancel**.

If validation fails, suspect the DOM or the adapter, not the procedure.

## 6. Swap the DOM and boot

1. Unplug the NAS. Open it and pull the original 256 MB DOM off the 9-pin
   header. **Keep it safe and unmodified** – it's your way back (see §14). Copy
   its files to your PC as an extra backup.
2. Fit the new DOM, blocked hole over the missing pin.
3. **Leave all drive bays empty** for the first boot. Plug in Ethernet.
4. Serial console: the rear micro-USB port labelled **UART** (it may be hidden
   under a black plug – pull it off). Connect it to the PC with a data cable.
   Install the CP210x driver if Device Manager shows *CP2102 USB to UART Bridge
   Controller* with a warning sign; it then appears under **Ports (COM & LPT)**.
5. PuTTY: **Serial**, your COM port, **115200**; under Connection → Serial set
   8 data bits, 1 stop bit, parity None, **flow control None**. Save the
   session, click **Open**, *then* power on the NAS. The CP2102 is powered by
   the PC, so the port works even while the NAS is off.
6. You should see GRUB, the kernel, then the OMV banner and a login prompt.
7. Log in as **`root`** (not `admin` – that's the web UI account) and run:
   ```
   ip -br addr
   ```
   One port (e.g. `enp1s0f0`) should be `UP` with an address from your router.
   No address? Check that the cable is in a LAN port, then run `networkctl`.

> The banner may still list `enp0s3: 10.0.2.15`. That's stale text from the
> VM; trust `ip -br addr`.

## 7. Finish the network setup

1. Browse to `http://<NAS-IP>`, log in as `admin` / `openmediavault`, and
   **change the password** (user icon, top right).
2. **Network → Interfaces → + → Ethernet**: device `enp1s0f0` (whatever port
   you used), IPv4 **DHCP** → Save → **Apply** (yellow banner).
3. Delete the stale **`enp0s3`** entry → **Apply**.
4. In your router, reserve the NAS's current IP for its MAC address
   (`ip link show enp1s0f0`).
5. On the **serial console** (the network drops briefly):
   ```
   rm /etc/netplan/99-readynas-catchall.yaml && netplan apply && ip -br addr
   reboot
   ```
   It should come back on the same IP.

> OMV only applies changes when you click **Apply** in the yellow banner. If
> something "doesn't work", that's the first thing to check.

## 8. Reduce writes to the DOM

From SSH (`ssh root@<NAS-IP>`) or the serial console, install **omv-extras**:

```
wget -O - https://github.com/OpenMediaVault-Plugin-Developers/packages/raw/master/install | bash
```

Refresh the web UI → **System → Plugins** → install
**openmediavault-writecache** (the successor of *flashmemory*). Check its page
under **Services → WriteCache**; if it has an Enable switch, turn it on → Save
→ Apply. Then reboot. It keeps frequently
written files (mostly logs) in RAM and writes them to the DOM occasionally; a
sudden power cut can lose recent logs, not data.

Also install pending updates (**System → Update Management**). A newer kernel
from Debian backports is fine.

## 9. Bring in an existing ReadyNAS data drive

1. `poweroff`, insert the drive, power on with PuTTY open. If the boot now
   stops before GRUB, the BIOS is trying the hard disk first: press **ESC**
   during the BIOS banner and put the DOM first.
2. See what's on it (read-only):
   ```
   lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT
   cat /proc/mdstat
   ```
   A ReadyNAS OS 6 disk looks like this – three mdadm arrays:

   | Array | Size | What it is |
   |---|---|---|
   | `md126` (`<id>:root`) | 4 G | old ReadyNAS OS – leave it |
   | `md125` (`swap`) | 512 M | old swap – leave it |
   | `md127` (`<id>:data`) | rest of disk, **btrfs** | **your data** |

3. Web UI → **Storage → File Systems** → the **Mount** button (hover to check
   the tooltip) → pick the big btrfs `/dev/md127` → Save → Apply.
   **Never use Create (+) or Disks → Wipe** – both destroy data.

> A single-disk ReadyNAS volume is a RAID1 with one member (`[1/1] [U]`):
> **no redundancy**. Add a second disk to the array, or keep a backup.

## 10. Share it to Windows

1. **Users → Users → +**: create your own user (the `admin` account is only for
   the web UI).
2. **Storage → Shared Folders → +**: name e.g. `data`, file system = the btrfs
   volume, **Relative path `/`** to expose the whole volume with all your old
   ReadyNAS share folders inside (don't keep the auto-filled `data/`, which
   would create a new empty folder). Save → Apply.
3. Select it → **Permissions** (folder-with-key icon) → your user
   **Read/Write** → Save.
4. **Services → SMB/CIFS → Settings**: tick **Enabled** → Save.
5. **Services → SMB/CIFS → Shares → +**: shared folder `data`, **Public: No**
   → Save → **Apply**.
6. On Windows: `\\<NAS-IP>\data`, log in with that user. Check
   `Test-NetConnection <NAS-IP> -Port 445` if it doesn't connect.

### "You do not have permission" on old folders

The files still belong to the old ReadyNAS accounts. OMV's share permissions
don't change file ownership, so take ownership once (replace `<user>` and the
path, which is shown as *Absolute Path* in Shared Folders):

```
nohup sh -c 'chown -R <user>:users /srv/dev-disk-by-uuid-XXXX; chmod -R u+rwX /srv/dev-disk-by-uuid-XXXX' > /root/fixperms.log 2>&1 &
pgrep -a 'chown|chmod'     # repeat until it prints nothing
```

"Read-only file system" lines in the log come from old ReadyNAS snapshots and
are harmless.

## 11. Front panel display and buttons

See [`panel/README.md`](panel/README.md). In short:

```
apt-get install -y python3-pil fonts-dejavu-core
modprobe i2c-dev; modprobe i2c-i801
python3 rn424_panel.py probe     # read-only check, must end with READY
python3 rn424_panel.py test      # display shows "Hello! / Front panel OK"
sh install.sh                    # systemd service
```

## 12. Fan control

Under OMV the fan stays at the BIOS's fixed, loud speed. The fan chip is an
ITE IT8613E, which Debian's `it87` driver doesn't support, so the community
driver is installed first (built by DKMS, so it survives kernel updates).
Then a small service sets the fan from the disk and CPU temperatures. See
[`fan/README.md`](fan/README.md) for the driver commands. After that:

```
git clone -b fan-control https://github.com/dgalue/readynas-rn424-omv /root/readynas-rn424-omv
sh /root/readynas-rn424-omv/fan/install.sh     # checks, calibrates (~3 min), starts
```

## 13. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| PuTTY stays blank | Wrong COM port, CP210x driver missing, or charge-only cable. |
| BIOS shows, then no boot device | Put the DOM first in the BIOS boot order (ESC over serial). ReadyNAS units are picky about USB devices. |
| "Gave up waiting for root device" | `MODULES=most` step was skipped (§4.2). |
| Login prompt but no IP | Cable in the wrong port, or the netplan catch-all is missing. Run `networkctl`. |
| Serial login keeps failing | Use `root` and the root password from the installer, not `admin`. |
| `\\NAS` says *network path not found* | SMB not enabled or not applied (yellow banner). |
| `scp`/`ssh` errors with `$env:` | That syntax is PowerShell; in Command Prompt use `%USERPROFILE%`. |

These boot messages are **normal** on the RN424 and can be ignored:
`DMAR: [Firmware Bug]: No firmware reserved region can cover this RMRR`,
`denverton-pinctrl ... probe ... failed with error -61`,
`EDAC pnd2: Failed to register device with error -22`,
`mdadm: No arrays found` (when no data disks are present).

## 14. Going back to ReadyNAS OS

ReadyNAS OS 6 itself lives on the data disks (the `md126` root array above);
the original DOM only boots it. Nothing in this guide touches `md126`, so
putting the original 256 MB DOM back should bring ReadyNAS OS back – provided
you never modified that DOM. (Not tested by the author. Files re-owned in §10
keep their new owner.)
