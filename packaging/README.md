# Packaging fmon

fmon is pure Python (3.9+) and runs on Windows, Linux and macOS. The GUI
needs Tk (`tkinter`): included with the python.org installers on Windows
and macOS; on Debian/Ubuntu `apt install python3-tk`, Fedora
`dnf install python3-tkinter`.

## From source (any platform)

    pip install .            # installs the 'fmon' and 'fmon-gui' commands
    pipx install .           # isolated install for one user

## Windows executables and installer

    .\packaging\windows\build.ps1             # PyInstaller -> dist\fmon\fmon.exe, fmon-gui.exe
    .\packaging\windows\build.ps1 -Py2exe     # the same with py2exe
    .\packaging\windows\build.ps1 -NoInstaller

Installer: with Inno Setup 6 installed (free: https://jrsoftware.org/isdl.php,
or `winget install JRSoftware.InnoSetup` - the script offers to do this) you
get `dist\fmon-2.0.0-setup.exe`, a classic wizard with "add to PATH", Start
Menu entries and an optional desktop shortcut. Without it (or with
`-Portable`) the script produces `dist\fmon-2.0.0-windows-portable.zip`:
unzip anywhere and run `install.ps1` for a per-user install (Start Menu,
PATH, `uninstall.ps1`; no administrator rights), or simply run the
executables from the folder.

Both executables must stay in the same folder: the GUI and the scheduler
run every command through `fmon.exe` (so they are audit-logged like
terminal commands).

## Linux

    packaging/linux/fmon.desktop        -> ~/.local/share/applications/ (or /usr/share/applications/)
    packaging/linux/fmon-watch.service  -> systemd unit for headless scheduled checks

A Linux single-file build is possible with the same PyInstaller spec
(`pyinstaller packaging/windows/fmon.spec` on Linux produces `dist/fmon/fmon`
and `dist/fmon/fmon-gui`).
