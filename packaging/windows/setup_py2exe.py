"""py2exe build of fmon (alternative to the PyInstaller spec).

    pip install py2exe
    python packaging\\windows\\setup_py2exe.py py2exe

Produces dist\\fmon.exe (console) and dist\\fmon-gui.exe (windowed) in the
same folder, plus the Tcl/Tk runtime needed by the GUI. Check that your
py2exe release supports your Python version (3.13 needs py2exe >= 0.13.0.2).
"""
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
root = os.path.abspath(os.path.join(here, "..", ".."))
sys.path.insert(0, os.path.join(root, "src"))

from setuptools import setup  # noqa: E402
import py2exe  # noqa: E402,F401

import fmon  # noqa: E402

setup(
    name="fmon",
    version=fmon.__version__,
    author=fmon.__author__,
    description="fmon - folder audit",
    console=[{"script": os.path.join(here, "entry_cli.py"), "dest_base": "fmon"}],
    windows=[{"script": os.path.join(here, "entry_gui.py"), "dest_base": "fmon-gui"}],
    data_files=[("", [os.path.join(root, "fmon.ini.example"), os.path.join(root, "LICENSE")])],
    options={"py2exe": {
        "packages": ["fmon", "tkinter"],
        "includes": ["tkinter.ttk", "tkinter.scrolledtext", "tkinter.font", "tkinter.filedialog",
                     "tkinter.messagebox", "pydoc"],
        "excludes": ["pytest"],
        "bundle_files": 3,      # keep DLLs as files: required for tkinter
        "compressed": True,
        "dist_dir": os.path.join(root, "dist", "fmon"),
    }},
    zipfile="lib/library.zip",
)
