# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build of fmon: two executables in ONE folder (dist/fmon/):
#   fmon.exe      console command line
#   fmon-gui.exe  windowed application (runs commands through fmon.exe next to it)
#
#   pip install pyinstaller
#   pyinstaller packaging\windows\fmon.spec
#
# Tk (tkinter) and its Tcl/Tk libraries are collected automatically.
import os

here = os.path.dirname(os.path.abspath(SPEC))
root = os.path.abspath(os.path.join(here, "..", ".."))
src = os.path.join(root, "src")

block_cipher = None
common = dict(
    pathex=[src],
    binaries=[],
    datas=[(os.path.join(root, "fmon.ini.example"), "."), (os.path.join(root, "LICENSE"), ".")],
    hiddenimports=["fmon.gui", "fmon.cli", "fmon.schedule", "fmon.logview", "fmon.manual"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest"],
    cipher=block_cipher,
)

cli_a = Analysis([os.path.join(here, "entry_cli.py")], **common)
gui_a = Analysis([os.path.join(here, "entry_gui.py")], **common)
MERGE((cli_a, "fmon", "fmon"), (gui_a, "fmon-gui", "fmon-gui"))

cli_pyz = PYZ(cli_a.pure, cli_a.zipped_data, cipher=block_cipher)
cli_exe = EXE(cli_pyz, cli_a.scripts, [], exclude_binaries=True, name="fmon",
              console=True, icon=None, version=os.path.join(here, "version_info.txt"))

gui_pyz = PYZ(gui_a.pure, gui_a.zipped_data, cipher=block_cipher)
gui_exe = EXE(gui_pyz, gui_a.scripts, [], exclude_binaries=True, name="fmon-gui",
              console=False, icon=None, version=os.path.join(here, "version_info.txt"))

COLLECT(cli_exe, cli_a.binaries, cli_a.zipfiles, cli_a.datas,
        gui_exe, gui_a.binaries, gui_a.zipfiles, gui_a.datas,
        strip=False, upx=False, name="fmon")
