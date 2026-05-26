# Testing the Windows install in a VirtualBox VM

This is the **developer-facing** recipe for validating the Windows install end-to-end on the Linux dev box, without needing a physical Windows machine. The lab operator never sees this document — they get [README.md § Quick install (Windows)](../README.md#quick-install-windows) instead.

**Goal:** simulate the full operator pipeline (download ZIP → double-click `install.bat` → play a 30 s Mario run → see LSL markers) inside a clean Windows 11 VM, so we catch packaging / install regressions before tagging a release.

---

## Prereqs on the host

- Linux (this repo lives at `~/GitHub/mario_task`).
- VirtualBox 7+ with the extension pack (we already installed via `sudo apt install virtualbox virtualbox-ext-pack`).
- Membership in the `vboxusers` group (`groups | grep vboxusers` should show it; log out + back in if not).
- ~80 GB free disk and ~8 GB free RAM.
- A **Windows 11 ISO**, downloadable for free from <https://www.microsoft.com/software-download/windows11> (pick "Download Windows 11 Disk Image (ISO) for x64 devices"). No license key required for dev use — there's a small activation watermark in the corner, which is fine.

---

## One-time: create the VM

1. Open VirtualBox (`virtualbox` from a terminal, or your launcher).
2. **New** → name `mario-win11`, type `Microsoft Windows`, version `Windows 11 (64-bit)`.
3. RAM: **8192 MB** (8 GB). Anything less makes Windows 11 painful.
4. Disk: **80 GB**, VDI, dynamically allocated.
5. Before starting the VM, open **Settings**:
   - **System → Motherboard**: enable **Enable EFI (special OSes only)** — required by the Win 11 installer.
   - **System → Motherboard**: ensure **Enable I/O APIC** is on (default).
   - **Display → Screen**: set **Video Memory** to 128 MB. Enable **3D acceleration** (PsychoPy's GL window benefits even if it falls back to software).
   - **Storage**: attach the Windows 11 ISO to the IDE optical drive.
   - **Network → Adapter 1**: NAT (default) — works for everything in this recipe.
   - **Audio**: leave defaults (PulseAudio / PipeWire host driver, AC97 or HDA audio controller).
6. **Important — add a TPM**: at the host shell, run
   ```bash
   VBoxManage modifyvm mario-win11 --tpm-type 2.0
   VBoxManage modifyvm mario-win11 --secure-boot on
   ```
   (Windows 11 refuses to install without a TPM 2.0 and Secure Boot. The GUI exposes these on VirtualBox 7.1+; if your version doesn't, the CLI does.)
7. **Start** the VM. The Windows 11 installer boots from the ISO.

## Installing Windows 11

1. Pick language / region / keyboard → Next.
2. **Install Windows 11**.
3. **I don't have a product key** (skip activation; activation watermark is fine for dev).
4. Pick **Windows 11 Pro** (or Home; doesn't matter for this test).
5. Accept the license. **Custom install**. Pick the only disk (the 80 GB VDI), Next.
6. Wait ~10 minutes while files copy. VM reboots a few times.
7. **OOBE (Out-of-Box Experience)**:
   - Pick region (e.g. Canada / United States).
   - Pick keyboard layout.
   - **Skip the Microsoft Account requirement**: when the "Sign in to your Microsoft account" page appears, press **Shift+F10** to open a command prompt and type `oobe\BypassNRO` — the VM reboots and now you can pick "Sign-in options → Domain join instead", which lets you create a local account.
   - Username: `mario` (or whatever). Skip password (or set "1234"; we don't care).
   - Privacy settings: turn everything off (don't matter for dev).
8. You're at the desktop.

## Optional but recommended: install Guest Additions

1. From the VirtualBox menu bar: **Devices → Insert Guest Additions CD image**.
2. Inside the VM, open File Explorer → DVD Drive → run `VBoxWindowsAdditions.exe`. Reboot when prompted.
3. Now you get: dynamic resolution scaling, mouse pointer integration, shared clipboard (under **Devices → Shared Clipboard → Bidirectional**), and shared folders.

## Snapshot the clean install

In VirtualBox: **Machine → Take Snapshot**, name `clean-win11`. After every test you can roll back to this in seconds.

---

## Running the actual install test

### Building the release ZIP on the host

```bash
cd ~/GitHub/mario_task
PACKAGE_ALLOW_DIRTY=1 bash scripts/package-windows-release.sh
# → dist/mario_task-windows-v0.1.0.zip
```

(`PACKAGE_ALLOW_DIRTY=1` lets you test packaging without committing first. In CI the workflow runs the same script on a clean checkout and the dirty-tree check passes naturally.)

### Getting the ZIP into the VM

Easiest: VirtualBox **Devices → Shared Clipboard → Bidirectional**, then drag the ZIP from the host File Manager into the VM. If shared clipboard isn't working (Guest Additions not yet installed), use a shared folder:

```bash
VBoxManage sharedfolder add mario-win11 --name=host_dist --hostpath=$HOME/GitHub/mario_task/dist --automount
```

Then inside Windows it appears as a network drive `\\VBOXSVR\host_dist`.

### Inside the VM

1. Copy the ZIP from `\\VBOXSVR\host_dist` to `%USERPROFILE%\Documents\` (drag in File Explorer).
2. Right-click the ZIP → **Extract All** → accept the suggested folder.
3. Open the extracted folder, double-click **install.bat**.
4. SmartScreen will warn — **More info → Run anyway**.
5. Watch the install. Should take ~5 minutes:
   - winget installs Python 3.10 (~30 s)
   - winget installs Git (~30 s)
   - winget installs git-annex (~30 s)
   - `uv sync --extra dev` resolves and installs ~311 packages (~2 min, ~1 GB on disk)
   - `datalad install` clones the mario.stimuli metadata (~30 s)
   - `datalad get` pulls the ROM + level states from `conp-ria-storage-http` (~30 s, ~40 KB ROM + 22 × 1 KB states)
6. **"Setup complete!"** A "Run Mario Task" shortcut appears on the desktop.
7. Double-click **Run Mario Task**. The config wizard opens.
8. For VM testing, pick **`null`** trigger backend (no LSL recipient needed in this round). Leave other defaults. Save.
9. Subject picker opens. Type `vmtest`. Press **Start session**.
10. Instructions screen: press **X** to start.
11. Play ~30 s of Level 1-1 (arrow keys + Z + X).
12. End-of-run prompt appears. Press **Z** to end the session.

### Verifying the output

Open PowerShell in the install folder (Shift+right-click the folder background → "Open in Terminal"). Then:

```powershell
# Walk the BIDS tree
dir output\sourcedata\sub-vmtest\ses-001
# Expect: a .log, an _events.tsv, a _Level1-1_rep-01.bk2

# Verify bk2 frame count matches log
.venv\Scripts\python.exe -m mario_task.verify output\sourcedata\sub-vmtest\ses-001\sub-vmtest_ses-001_*.log
# Expect: [ok] line, exit 0
```

### In-VM LSL test (separate session)

1. Double-click `Run Mario Task` and use `--reconfigure` (or `rm config.json` and let the wizard reopen) to switch backend to **lsl**. Save.
2. Open a second PowerShell, `cd` to the install folder, run:
   ```powershell
   .venv\Scripts\python.exe -m mario_task.monitor --quiet
   ```
3. Run the experiment in the first window. The monitor prints `TASK_START`, `GAME_RESET`, gameplay frame markers, `TASK_STOP`.

**Both pylsl processes run inside the same VM.** VirtualBox NAT blocks LSL's multicast discovery, so cross-VM/host LSL isn't tested here — we'll do that at the real deploy site with the actual EEG amp on the same LAN.

---

## Rolling back

After each test, **VirtualBox → Machine → Close → Power off**, then right-click `mario-win11` → **Snapshots** → restore `clean-win11`. Five seconds and you're back to a fresh install ready for the next ZIP version.

---

## Known issues in the VM

| Symptom | Cause | Workaround |
| --- | --- | --- |
| Frame rate <60 Hz in PsychoPy | VirtualBox's 3D acceleration is limited | Acceptable for install validation. For real performance use real Windows hardware. |
| Audio crackling | Audio buffer underruns in the VM | Increase VM RAM, or use Audio Controller = HDA in VM settings. |
| LSL stream visible inside VM but not from host | VirtualBox NAT blocks multicast | Switch the VM's network adapter to **Bridged** for cross-machine LSL. Defer to real-hardware test. |
| `install.bat` hangs at winget step | Microsoft Store backend slow / offline | Wait ~5 min. If still stuck, restart winget: `wsreset.exe`. |
| `datalad install` fails with TLS error | Outdated VM time / cert store | Ensure VM has internet and the system clock is current. |
