"""fmon graphical application (Tk).

Tabs: Targets, Run, Schedule, Log, Settings - plus a console at the bottom
showing the output of every command. All actions are executed through the
normal command line (`python -m fmon ...`) in a background thread, so they
are audit-logged exactly like terminal commands, tagged [gui].
"""
import os
import queue
import threading
import time
from datetime import datetime

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
    from tkinter.scrolledtext import ScrolledText
except ImportError as e:  # pragma: no cover - depends on the Python build
    raise SystemExit(
        "fmon gui needs Tk (the 'tkinter' module), which this Python lacks.\n"
        "Windows/macOS: reinstall Python with the Tcl/Tk option; "
        "Debian/Ubuntu: apt install python3-tk"
    ) from e

import tkinter.font as tkfont

import fmon
from fmon import config as fmon_config
from fmon import logview
from fmon import manual
from fmon import schedule as fmon_schedule
from fmon.cli import normalize_argv

INTERVAL_PRESETS = ("off", "1m", "5m", "15m", "30m", "1h", "6h", "12h", "1d")
POLL_MS = 200

COMMANDS_WITH_CONFIG = {"snapshot", "check", "config", "log", "watch"}


# Tabs that do not show the console
TABS_WITHOUT_CONSOLE = {"Log", "Help"}
# Default share of the window height given to the tab area (the rest is console)
DEFAULT_SASH_RATIO = {"Targets": 0.65, "Run": 0.30, "Schedule": 0.65, "Settings": 0.80}

# --- appearance ---------------------------------------------------------------

PALETTES = {
    "Light": dict(bg="#f0f0f0", panel="#e4e4e4", sash="#9aa7b8", field="#ffffff", fg="#1a1a1a", dim="#555555",
                  select="#cce0ff", select_fg="#000000", accent="#1a4d8f", ok="#2e7d32", warn="#9a6700",
                  err="#b00020", row_warn="#fff4d6", row_err="#ffe0e0", console_bg="#ffffff"),
    "Dark": dict(bg="#2b2b2b", panel="#3c3f41", sash="#5d6470", field="#1e1f22", fg="#e6e6e6", dim="#a0a0a0",
                 select="#214283", select_fg="#ffffff", accent="#6ea8fe", ok="#7ccf85", warn="#e5c07b",
                 err="#ff7b72", row_warn="#4a3f1e", row_err="#4d2626", console_bg="#1e1f22"),
    "Solarized Light": dict(bg="#fdf6e3", panel="#eee8d5", sash="#b9b0a0", field="#fffdf5", fg="#586e75", dim="#93a1a1",
                            select="#d3cbb7", select_fg="#002b36", accent="#268bd2", ok="#859900", warn="#b58900",
                            err="#dc322f", row_warn="#f5e6b8", row_err="#f8d2cf", console_bg="#fffdf5"),
    "Solarized Dark": dict(bg="#002b36", panel="#073642", sash="#3d5a66", field="#00212b", fg="#93a1a1", dim="#657b83",
                           select="#586e75", select_fg="#fdf6e3", accent="#268bd2", ok="#859900", warn="#b58900",
                           err="#dc322f", row_warn="#3d3a1a", row_err="#4a1f1f", console_bg="#00212b"),
    "High Contrast": dict(bg="#000000", panel="#1a1a1a", sash="#ffffff", field="#000000", fg="#ffffff", dim="#d0d0d0",
                          select="#ffff00", select_fg="#000000", accent="#00d0ff", ok="#00ff00", warn="#ffd700",
                          err="#ff4040", row_warn="#4d4000", row_err="#5a0000", console_bg="#000000"),
}
DEFAULT_PALETTE = "Light"
FONT_SIZES = tuple(range(8, 21))


class Appearance:
    """Applies a palette + font to every ttk style and registered Text widget."""

    def __init__(self, root):
        self.root = root
        self.style = ttk.Style(root)
        self._native_theme = self.style.theme_use()
        self.texts = []            # tk.Text widgets to recolour
        self.palette_name = DEFAULT_PALETTE
        self.font_family = ""
        self.font_size = 10
        self.colors = PALETTES[DEFAULT_PALETTE]

    @property
    def families(self):
        return sorted(set(tkfont.families(self.root)))

    def register_text(self, widget, mono=True):
        self.texts.append((widget, mono))
        self._style_text(widget, mono)

    def apply(self, palette=None, family=None, size=None):
        self.palette_name = palette if palette in PALETTES else self.palette_name
        self.font_family = family if family is not None else self.font_family
        self.font_size = int(size) if size else self.font_size
        c = self.colors = PALETTES[self.palette_name]
        st = self.style
        light = self.palette_name == DEFAULT_PALETTE
        # the native theme (vista/aqua) ignores colours, so it is only used for the Light palette
        st.theme_use(self._native_theme if light else "clam")
        family = self.font_family or tkfont.nametofont("TkDefaultFont").actual("family")
        font = (family, self.font_size)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkTooltipFont"):
            try:
                tkfont.nametofont(name).configure(family=family, size=self.font_size)
            except tk.TclError:
                pass
        st.configure(".", font=font, background=c["bg"], foreground=c["fg"])
        st.configure("Treeview", font=font, rowheight=int(self.font_size * 2.1),
                     background=c["field"], fieldbackground=c["field"], foreground=c["fg"])
        st.configure("Treeview.Heading", font=(family, self.font_size, "bold"), background=c["panel"], foreground=c["fg"])
        st.map("Treeview", background=[("selected", c["select"])], foreground=[("selected", c["select_fg"])])
        st.configure("TNotebook", background=c["bg"])
        st.configure("TNotebook.Tab", padding=(10, 4), background=c["panel"], foreground=c["fg"])
        st.map("TNotebook.Tab", background=[("selected", c["bg"])], foreground=[("selected", c["accent"])])
        st.configure("TLabelframe", background=c["bg"])
        st.configure("TLabelframe.Label", background=c["bg"], foreground=c["accent"])
        st.configure("TEntry", fieldbackground=c["field"], foreground=c["fg"], insertcolor=c["fg"])
        st.configure("TCombobox", fieldbackground=c["field"], foreground=c["fg"], background=c["panel"],
                     arrowcolor=c["fg"])
        st.map("TCombobox", fieldbackground=[("readonly", c["field"])], foreground=[("readonly", c["fg"])])
        st.configure("TSpinbox", fieldbackground=c["field"], foreground=c["fg"], background=c["panel"],
                     arrowcolor=c["fg"])
        st.configure("TButton", background=c["panel"], foreground=c["fg"])
        st.map("TButton", background=[("active", c["select"]), ("disabled", c["bg"])],
               foreground=[("disabled", c["dim"])])
        st.configure("TCheckbutton", background=c["bg"], foreground=c["fg"])
        st.map("TCheckbutton", background=[("active", c["bg"])])
        st.configure("TScrollbar", background=c["panel"], troughcolor=c["bg"], arrowcolor=c["fg"])
        st.configure("Status.TLabel", background=c["panel"], foreground=c["fg"])
        st.configure("Dim.TLabel", foreground=c["dim"])
        st.configure("Ok.TLabel", foreground=c["ok"])
        st.configure("Warn.TLabel", foreground=c["warn"])
        st.configure("Err.TLabel", foreground=c["err"])
        st.configure("Title.TLabel", foreground=c["accent"], font=(family, self.font_size + 2, "bold"))
        self.root.configure(bg=c["bg"])
        self.root.option_add("*TCombobox*Listbox.background", c["field"])
        self.root.option_add("*TCombobox*Listbox.foreground", c["fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", c["select"])
        self.root.option_add("*Menu.background", c["panel"])
        self.root.option_add("*Menu.foreground", c["fg"])
        for widget, mono in self.texts:
            self._style_text(widget, mono)
        for w in self.root.winfo_children():
            self._style_panes(w)

    def _style_panes(self, widget):
        if isinstance(widget, tk.PanedWindow):
            widget.configure(bg=self.colors["sash"])
        elif isinstance(widget, tk.Canvas):
            widget.configure(bg=self.colors["bg"])
        for child in widget.winfo_children():
            self._style_panes(child)

    def _style_text(self, widget, mono):
        c = self.colors
        family = "Courier" if mono else (self.font_family or tkfont.nametofont("TkTextFont").actual("family"))
        if mono:
            family = tkfont.nametofont("TkFixedFont").actual("family")
        widget.configure(bg=c["console_bg"], fg=c["fg"], insertbackground=c["fg"],
                         selectbackground=c["select"], selectforeground=c["select_fg"],
                         font=(family, self.font_size))
        for tag, color, bold in (("cmd", c["accent"], True), ("err", c["err"], False), ("warn", c["warn"], False),
                                 ("ok", c["ok"], False), ("head", c["accent"], True), ("hit", None, False)):
            if tag == "hit":
                widget.tag_configure("hit", background=c["select"], foreground=c["select_fg"])
            else:
                widget.tag_configure(tag, foreground=color, font=(family, self.font_size, "bold") if bold else (family, self.font_size))


def _fmt_dt(value):
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else ""


def _fmt_countdown(when):
    remaining = int(when - time.time())
    if remaining <= 0:
        return "due"
    if remaining < 60:
        return f"in {remaining}s"
    if remaining < 3600:
        return f"in {remaining // 60}m {remaining % 60:02d}s"
    return f"in {remaining // 3600}h {(remaining % 3600) // 60:02d}m"


# ---------------------------------------------------------------------------
# Small widgets
# ---------------------------------------------------------------------------

class ScrollableFrame(ttk.Frame):
    """A frame with a vertical scrollbar; put content into .inner."""

    def __init__(self, master):
        super().__init__(master)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._window, width=e.width))
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vbar.grid(row=0, column=1, sticky="ns")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        # mouse wheel scrolls while the pointer is over this frame
        self.canvas.bind("<Enter>", lambda e: self._bind_wheel())
        self.canvas.bind("<Leave>", lambda e: self._unbind_wheel())

    def _bind_wheel(self):
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)       # Windows / macOS
        self.canvas.bind_all("<Button-4>", self._on_wheel)         # X11
        self.canvas.bind_all("<Button-5>", self._on_wheel)

    def _unbind_wheel(self):
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.unbind_all(seq)

    def _on_wheel(self, event):
        if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0:
            self.canvas.yview_scroll(-1, "units")
        elif getattr(event, "num", None) == 5 or getattr(event, "delta", 0) < 0:
            self.canvas.yview_scroll(1, "units")


class Console(ttk.Frame):
    """Read-only output area shared by all tabs."""

    def __init__(self, master):
        super().__init__(master)
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Label(bar, text="Console").pack(side="left", padx=4)
        ttk.Label(bar, text="(drag the bar above to resize)", style="Dim.TLabel").pack(side="left", padx=4)
        ttk.Button(bar, text="Clear", command=self.clear).pack(side="right", padx=2, pady=2)
        self.text = ScrolledText(self, height=8, wrap="word", state="disabled")
        self.text.pack(fill="both", expand=True)
        master.winfo_toplevel().appearance.register_text(self.text)

    def append(self, text, tag=None):
        self.text.configure(state="normal")
        self.text.insert("end", text if text.endswith("\n") else text + "\n", tag)
        self.text.see("end")
        self.text.configure(state="disabled")

    def clear(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")


class Table(ttk.Treeview):
    """Treeview with headings, scrollbar and multi-selection helpers."""

    def __init__(self, master, columns, widths=None, **kwargs):
        super().__init__(master, columns=[c for c, _ in columns], show="headings",
                         selectmode=kwargs.pop("selectmode", "extended"), **kwargs)
        widths = widths or {}
        for key, title in columns:
            self.heading(key, text=title)
            self.column(key, width=widths.get(key, 120), anchor="w", stretch=True)
        self.scroll = ttk.Scrollbar(master, orient="vertical", command=self.yview)
        self.configure(yscrollcommand=self.scroll.set)

    def grid_with_scroll(self, row, column, **kwargs):
        self.grid(row=row, column=column, sticky="nsew", **kwargs)
        self.scroll.grid(row=row, column=column + 1, sticky="ns")

    def clear(self):
        self.delete(*self.get_children())

    def selected_values(self, index=0):
        return [self.item(i, "values")[index] for i in self.selection()]


class Dialog(tk.Toplevel):
    """Modal dialog base: subclasses fill body() and validate()/result.
    Uses ttk widgets, so it follows the application's color scheme."""

    def __init__(self, master, title, cancel=True):
        super().__init__(master)
        self.title(title)
        self.transient(master)
        self.resizable(False, False)
        self.result = None
        appearance = getattr(master.winfo_toplevel(), "appearance", None)
        if appearance:
            self.configure(bg=appearance.colors["bg"])
        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)
        self.body(body)
        buttons = ttk.Frame(self, padding=(10, 0, 10, 10))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="OK", command=self.ok).pack(side="right")
        if cancel:
            ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=6)
        self.bind("<Return>", lambda e: self.ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self._center(master)
        self.grab_set()

    def _center(self, master):
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_reqwidth()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_reqheight()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def body(self, frame):
        raise NotImplementedError

    def validate(self):
        return True

    def ok(self):
        if self.validate():
            self.destroy()

    def show(self):
        self.wait_window()
        return self.result


class AboutDialog(Dialog):
    def __init__(self, master):
        super().__init__(master, "About fmon", cancel=False)

    def body(self, frame):
        ttk.Label(frame, text=f"fmon {fmon.__version__}", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frame, text="Folder audit: snapshot folders, detect changes,\n"
                              "scheduled monitoring, audit log.").pack(anchor="w", pady=(4, 10))
        ttk.Label(frame, text="Open source, MIT license").pack(anchor="w")
        ttk.Label(frame, text=f"Author: {fmon.__author__}").pack(anchor="w")


class TargetDialog(Dialog):
    """Add or edit a target."""

    def __init__(self, master, title, target=None):
        self.target = target
        super().__init__(master, title)

    def body(self, frame):
        t = self.target
        self.name = tk.StringVar(value=t.name if t else "")
        self.path = tk.StringVar(value=t.path if t else "")
        self.control = tk.StringVar(value=t.control_file if t else "")
        self.schedule = tk.StringVar(value=fmon_config.format_interval(t.schedule) if t else "off")

        ttk.Label(frame, text="Name").grid(row=0, column=0, sticky="w", pady=2)
        name_entry = ttk.Entry(frame, textvariable=self.name, width=50)
        name_entry.grid(row=0, column=1, sticky="ew", pady=2)
        if t:
            name_entry.configure(state="disabled")
        ttk.Label(frame, text="Folder").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(frame, textvariable=self.path, width=50).grid(row=1, column=1, sticky="ew", pady=2)
        ttk.Button(frame, text="Browse...", command=self._browse_dir).grid(row=1, column=2, padx=4)
        ttk.Label(frame, text="Control file").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(frame, textvariable=self.control, width=50).grid(row=2, column=1, sticky="ew", pady=2)
        ttk.Button(frame, text="Browse...", command=self._browse_ctl).grid(row=2, column=2, padx=4)
        ttk.Label(frame, text="(empty: <name>.ctl in control_dir)").grid(row=3, column=1, sticky="w")
        ttk.Label(frame, text="Schedule").grid(row=4, column=0, sticky="w", pady=2)
        ttk.Combobox(frame, textvariable=self.schedule, values=INTERVAL_PRESETS, width=10).grid(
            row=4, column=1, sticky="w", pady=2)
        ttk.Label(frame, text="e.g. 30s, 15m, 2h, 1d or off").grid(row=5, column=1, sticky="w")

    def _browse_dir(self):
        chosen = filedialog.askdirectory(parent=self, initialdir=self.path.get() or os.getcwd())
        if chosen:
            self.path.set(chosen)

    def _browse_ctl(self):
        chosen = filedialog.asksaveasfilename(parent=self, defaultextension=".ctl",
                                              filetypes=[("control files", "*.ctl"), ("all", "*.*")])
        if chosen:
            self.control.set(chosen)

    def validate(self):
        name = self.name.get().strip()
        if not name:
            messagebox.showerror("fmon", "A name is required.", parent=self)
            return False
        if not self.path.get().strip():
            messagebox.showerror("fmon", "A folder is required.", parent=self)
            return False
        try:
            fmon_config.parse_interval(self.schedule.get())
        except ValueError as e:
            messagebox.showerror("fmon", str(e), parent=self)
            return False
        self.result = {
            "name": name, "path": self.path.get().strip(),
            "control_file": self.control.get().strip(),
            "schedule": self.schedule.get().strip() or "off",
        }
        return True


class IntervalDialog(Dialog):
    def __init__(self, master, names, current="off"):
        self.names, self.current = names, current
        super().__init__(master, "Set schedule")

    def body(self, frame):
        ttk.Label(frame, text=f"Check {', '.join(self.names)} every:").grid(row=0, column=0, sticky="w")
        self.value = tk.StringVar(value=self.current)
        ttk.Combobox(frame, textvariable=self.value, values=INTERVAL_PRESETS, width=12).grid(
            row=1, column=0, sticky="w", pady=6)
        ttk.Label(frame, text="e.g. 30s, 15m, 2h, 1d or off").grid(row=2, column=0, sticky="w")

    def validate(self):
        try:
            fmon_config.parse_interval(self.value.get())
        except ValueError as e:
            messagebox.showerror("fmon", str(e), parent=self)
            return False
        self.result = self.value.get().strip() or "off"
        return True


class DeleteDialog(Dialog):
    def __init__(self, master, names):
        self.names = names
        super().__init__(master, "Delete targets")

    def body(self, frame):
        ttk.Label(frame, text=f"Delete {len(self.names)} target(s) from the config?").pack(anchor="w")
        ttk.Label(frame, text=", ".join(self.names), wraplength=400).pack(anchor="w", pady=4)
        self.ctl = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Also delete their control files (--ctl)", variable=self.ctl).pack(anchor="w")

    def validate(self):
        self.result = {"ctl": self.ctl.get()}
        return True


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

class TargetsTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=6)
        self.app = app
        self.targets = []
        self.filter = tk.StringVar()
        self.filter.trace_add("write", lambda *a: self.render())

        top = ttk.Frame(self)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        ttk.Label(top, text="Filter").pack(side="left")
        ttk.Entry(top, textvariable=self.filter, width=30).pack(side="left", padx=4)
        ttk.Label(top, text="(name, folder or glob like t*)").pack(side="left")
        ttk.Button(top, text="Refresh", command=self.refresh).pack(side="right")
        self.banner = ttk.Label(self, text="", style="Err.TLabel", wraplength=900, justify="left")

        self.table = Table(self, [("name", "Name"), ("path", "Folder"), ("control", "Control file"),
                                  ("status", "Control file status"), ("schedule", "Schedule"),
                                  ("last", "Last scheduled check")],
                           widths={"name": 110, "path": 260, "control": 260, "status": 150,
                                   "schedule": 80, "last": 200})
        self.table.grid_with_scroll(1, 0)
        self.table.bind("<Double-1>", lambda e: self.edit())

        buttons = ttk.Frame(self)
        buttons.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        for text, cmd in (("Snapshot", self.snapshot), ("Check", self.check), ("Add...", self.add),
                          ("Edit...", self.edit), ("Delete...", self.delete)):
            ttk.Button(buttons, text=text, command=cmd).pack(side="left", padx=2)
        ttk.Label(buttons, text="Snapshot/Check act on the selected targets (none selected = all)").pack(
            side="left", padx=12)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

    def refresh(self):
        self.targets = self.app.load_targets()
        self.render()

    def render(self):
        pattern = self.filter.get().strip()
        self.table.clear()
        error = self.app.config_error
        if error:
            self.banner.configure(text=f"Config error - no targets can be shown: {error}\n"
                                       "Fix the file in the Settings tab (raw contents) or with 'fmon config ...'.")
            self.banner.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        elif not os.path.isfile(self.app.config_path):
            self.banner.configure(text="No config file yet: take a snapshot (Add... or 'fmon DIR') to create it.")
            self.banner.configure(style="Warn.TLabel")
            self.banner.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        else:
            self.banner.grid_forget()
        if error:
            self.banner.configure(style="Err.TLabel")
        states = self.app.scheduler.snapshot() if self.app.scheduler else {}
        for t in self.targets:
            if pattern and not self._matches(t, pattern):
                continue
            state = states.get(t.name)
            last = f"{_fmt_dt(state.last_run)}  {state.status_text}" if state and state.last_run else ""
            exists = os.path.isfile(t.control_file)
            status = "exists" if exists else "missing - take a snapshot"
            self.table.insert("", "end", iid=t.name, values=(
                t.name, t.path, t.control_file, status,
                fmon_config.format_interval(t.schedule) if t.schedule else "", last),
                tags=("missing",) if not exists else ())
        self.table.tag_configure("missing", foreground=self.app.appearance.colors["warn"])

    @staticmethod
    def _matches(t, pattern):
        if fmon_config.is_pattern(pattern):
            return bool(fmon_config.match_targets([t], pattern))
        p = pattern.lower()
        return p in t.name.lower() or p in t.path.lower()

    def selected_names(self):
        return list(self.table.selection())

    def _target_args(self):
        args = []
        for name in self.selected_names():
            args += ["-t", name]
        return args

    def snapshot(self):
        self.app.run_command(["snapshot"] + self._target_args(), on_done=lambda p: self.refresh())

    def check(self):
        self.app.run_command(["check"] + self._target_args(), on_done=lambda p: self.app.after_check())

    def add(self):
        values = TargetDialog(self.app, "Add target").show()
        if not values:
            return
        cmds = [["config", "add", values["name"], values["path"]]
                + (["-c", values["control_file"]] if values["control_file"] else [])]
        if values["schedule"] != "off":
            cmds.append(["config", "set", values["name"], "schedule", values["schedule"]])
        self.app.run_commands(cmds, on_done=lambda ok: self.app.after_config_change())

    def edit(self):
        names = self.selected_names()
        if len(names) != 1:
            messagebox.showinfo("fmon", "Select exactly one target to edit.", parent=self.app)
            return
        target = next(t for t in self.targets if t.name == names[0])
        values = TargetDialog(self.app, f"Edit target [{target.name}]", target).show()
        if not values:
            return
        cmds = []
        if values["path"] != target.path:
            cmds.append(["config", "set", target.name, "path", values["path"]])
        if values["control_file"] != target.control_file:
            if values["control_file"]:
                cmds.append(["config", "set", target.name, "control_file", values["control_file"]])
            else:
                cmds.append(["config", "unset", target.name, "control_file"])
        current = fmon_config.format_interval(target.schedule)
        if values["schedule"] != current:
            if values["schedule"] == "off":
                cmds.append(["config", "unset", target.name, "schedule"])
            else:
                cmds.append(["config", "set", target.name, "schedule", values["schedule"]])
        if not cmds:
            self.app.set_status("No changes.")
            return
        self.app.run_commands(cmds, on_done=lambda ok: self.app.after_config_change())

    def delete(self):
        names = self.selected_names()
        if not names:
            messagebox.showinfo("fmon", "Select the target(s) to delete.", parent=self.app)
            return
        values = DeleteDialog(self.app, names).show()
        if not values:
            return
        args = ["config", "delete"] + names + (["--ctl"] if values["ctl"] else [])
        self.app.run_command(args, on_done=lambda p: self.app.after_config_change())


class RunTab(ttk.Frame):
    QUICK = (("snapshot (all)", "snapshot"), ("check (all)", "check"), ("config show", "config show"),
             ("config get", "config get"), ("log -n 20", "log -n 20"), ("log --files", "log --files"),
             ("help", "help"))

    def __init__(self, master, app):
        super().__init__(master, padding=6)
        self.app = app
        self.history = []
        ttk.Label(self, text="Command (without the leading 'fmon'):").grid(row=0, column=0, sticky="w")
        self.command = tk.StringVar(value="check")
        self.entry = ttk.Combobox(self, textvariable=self.command, values=[], width=80)
        self.entry.grid(row=1, column=0, sticky="ew", pady=4)
        self.entry.bind("<Return>", lambda e: self.run())
        ttk.Button(self, text="Run", command=self.run).grid(row=1, column=1, padx=6)
        self.add_config = tk.BooleanVar(value=True)
        ttk.Checkbutton(self, text=f"add '-f {app.config_path}' automatically", variable=self.add_config).grid(
            row=2, column=0, sticky="w")
        quick = ttk.LabelFrame(self, text="Quick commands", padding=6)
        quick.grid(row=3, column=0, columnspan=2, sticky="ew", pady=8)
        for label, cmd in self.QUICK:
            ttk.Button(quick, text=label, command=lambda c=cmd: self.run(c)).pack(side="left", padx=2)
        ttk.Label(self, text="Output appears in the console below. Every command is audit-logged, tagged [gui].",
                  foreground="#555").grid(row=4, column=0, sticky="w")
        self.columnconfigure(0, weight=1)

    def run(self, command=None):
        text = (command or self.command.get()).strip()
        if not text:
            return
        import shlex
        try:
            args = shlex.split(text, posix=(os.name != "nt"))
        except ValueError as e:
            messagebox.showerror("fmon", f"Cannot parse command: {e}", parent=self.app)
            return
        if args and args[0] == "fmon":
            args = args[1:]
        if text not in self.history:
            self.history.insert(0, text)
            self.entry.configure(values=self.history[:30])
        self.command.set(text)
        with_config = self.add_config.get() and normalize_argv(args)[0] in COMMANDS_WITH_CONFIG
        self.app.run_command(args, with_config=with_config, on_done=lambda p: self.app.after_check())


class ScheduleTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=6)
        self.app = app
        top = ttk.Frame(self)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        self.status = ttk.Label(top, text="Scheduler stopped")
        self.status.pack(side="left")
        self.toggle_button = ttk.Button(top, text="Start scheduler", command=self.toggle)
        self.toggle_button.pack(side="right")
        ttk.Button(top, text="Refresh", command=self.refresh).pack(side="right", padx=4)

        self.table = Table(self, [("name", "Target"), ("interval", "Every"), ("last", "Last run"),
                                  ("result", "Result"), ("summary", "Summary"), ("next", "Next run")],
                           widths={"name": 120, "interval": 70, "last": 160, "result": 100,
                                   "summary": 260, "next": 120})
        self.table.grid_with_scroll(1, 0)
        self.table.bind("<<TreeviewSelect>>", lambda e: self.show_output())

        buttons = ttk.Frame(self)
        buttons.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(buttons, text="Set schedule...", command=self.set_interval).pack(side="left", padx=2)
        ttk.Button(buttons, text="Run now", command=self.run_now).pack(side="left", padx=2)
        ttk.Label(buttons, text="Select target(s) and set how often they are checked; 'off' removes the schedule.",
                  style="Dim.TLabel").pack(side="left", padx=12)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self._tick()

    def refresh(self):
        scheduler = self.app.scheduler
        targets = self.app.load_targets()
        states = scheduler.snapshot() if scheduler else {}
        selected = set(self.table.selection())
        self.table.clear()
        for t in targets:
            state = states.get(t.name)
            if not t.schedule:
                next_text = "not scheduled"
            elif state is None or not scheduler.running:
                next_text = "scheduler stopped"
            elif state.running:
                next_text = "running..."
            else:
                next_text = _fmt_countdown(state.next_run)
            self.table.insert("", "end", iid=t.name, values=(
                t.name, fmon_config.format_interval(t.schedule) if t.schedule else "off",
                _fmt_dt(state.last_run) if state else "",
                state.status_text if state else "",
                state.last_summary if state else "",
                next_text,
            ), tags=(state.status_text,) if state else ("unscheduled",))
        c = self.app.appearance.colors
        self.table.tag_configure("CHANGES", foreground=c["err"])
        self.table.tag_configure("error", foreground=c["warn"])
        self.table.tag_configure("unscheduled", foreground=c["dim"])
        for name in selected:
            if self.table.exists(name):
                self.table.selection_add(name)
        running = bool(scheduler and scheduler.running)
        count = len(states)
        self.status.configure(text=f"Scheduler running - {count} scheduled target(s)" if running
                              else f"Scheduler stopped - {count} scheduled target(s)")
        self.toggle_button.configure(text="Stop scheduler" if running else "Start scheduler")

    def _tick(self):
        if self.app.scheduler and self.app.scheduler.running:
            for name, state in self.app.scheduler.snapshot().items():
                if self.table.exists(name):
                    self.table.set(name, "next", "running..." if state.running else _fmt_countdown(state.next_run))
        self.after(1000, self._tick)

    def toggle(self):
        if self.app.scheduler and self.app.scheduler.running:
            self.app.stop_scheduler()
        else:
            self.app.start_scheduler()
        self.refresh()

    def set_interval(self):
        names = list(self.table.selection()) or self.app.targets_tab.selected_names()
        if not names:
            messagebox.showinfo("fmon", "Select the target(s) to schedule in the list.", parent=self.app)
            return
        current = self.table.set(names[0], "interval") if self.table.exists(names[0]) else "off"
        value = IntervalDialog(self.app, names, current).show()
        if value is None:
            return
        cmds = [["config", "unset" if value == "off" else "set", n, "schedule"] + ([] if value == "off" else [value])
                for n in names]
        self.app.run_commands(cmds, on_done=lambda ok: self.app.after_config_change())

    def run_now(self):
        names = list(self.table.selection())
        if not names:
            messagebox.showinfo("fmon", "Select the target(s) to check now.", parent=self.app)
            return
        scheduled = set(self.app.scheduler.snapshot()) if (self.app.scheduler and self.app.scheduler.running) else set()
        for name in [n for n in names if n in scheduled]:
            self.app.scheduler.run_now(name)
        others = [n for n in names if n not in scheduled]
        if others:
            args = ["check"]
            for name in others:
                args += ["-t", name]
            self.app.run_command(args, on_done=lambda p: self.app.after_check())

    def show_output(self):
        names = list(self.table.selection())
        if len(names) == 1 and self.app.scheduler:
            state = self.app.scheduler.snapshot().get(names[0])
            if state and state.last_output:
                self.app.console.append(f"--- last scheduled check of [{names[0]}] at {_fmt_dt(state.last_run)}", "cmd")
                self.app.console.append(state.last_output)

    def on_event(self, event):
        if event.kind == "started":
            self.app.set_status(f"Scheduled check of [{event.name}] running...")
        elif event.kind == "finished":
            st = event.state
            tag = "err" if st.last_status == fmon_schedule.EXIT_CHANGES else (
                "warn" if st.last_status == fmon_schedule.EXIT_ERROR else "ok")
            self.app.console.append(
                f"[scheduled {_fmt_dt(st.last_run)}] [{event.name}] {st.status_text}: {st.last_summary}", tag)
            if st.last_status != fmon_schedule.EXIT_OK:
                self.app.console.append(st.last_output)
            self.app.set_status(f"[{event.name}] {st.status_text}: {st.last_summary}")
            self.app.targets_tab.render()
            self.app.log_tab.refresh(keep_page=True)
        elif event.kind == "error":
            self.app.console.append(f"scheduler: {event.message}", "err")
        self.refresh()


class LogTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=6)
        self.app = app
        self.entries = []
        self.filtered = []
        self.pager = logview.Pager([], 25)

        filters = ttk.LabelFrame(self, text="Filters", padding=6)
        filters.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.level = tk.StringVar(value="All")
        self.user = tk.StringVar(value="All")
        self.command = tk.StringVar()
        self.text = tk.StringVar()
        self.since = tk.StringVar()
        self.until = tk.StringVar()
        self.rotated = tk.BooleanVar(value=False)
        ttk.Label(filters, text="Type").grid(row=0, column=0, sticky="w")
        self.level_box = ttk.Combobox(filters, textvariable=self.level, values=("All", "Info", "Warning", "Error"),
                                      width=9, state="readonly")
        self.level_box.grid(row=0, column=1, padx=(2, 10))
        ttk.Label(filters, text="User").grid(row=0, column=2, sticky="w")
        self.user_box = ttk.Combobox(filters, textvariable=self.user, values=("All",), width=14)
        self.user_box.grid(row=0, column=3, padx=(2, 10))
        ttk.Label(filters, text="Command contains").grid(row=0, column=4, sticky="w")
        ttk.Entry(filters, textvariable=self.command, width=22).grid(row=0, column=5, padx=(2, 10))
        ttk.Label(filters, text="Text (incl. report)").grid(row=0, column=6, sticky="w")
        ttk.Entry(filters, textvariable=self.text, width=22).grid(row=0, column=7, padx=(2, 10))
        ttk.Label(filters, text="From").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(filters, textvariable=self.since, width=18).grid(row=1, column=1, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(filters, text="To").grid(row=1, column=2, sticky="e", pady=(4, 0))
        ttk.Entry(filters, textvariable=self.until, width=18).grid(row=1, column=3, sticky="w", pady=(4, 0))
        ttk.Label(filters, text="(YYYY-MM-DD [HH:MM])").grid(row=1, column=4, sticky="w", pady=(4, 0))
        ttk.Checkbutton(filters, text="include rotated log files", variable=self.rotated,
                        command=self.reload).grid(row=1, column=5, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(filters, text="Apply", command=self.apply).grid(row=1, column=7, sticky="e", pady=(4, 0))
        ttk.Button(filters, text="Reset", command=self.reset).grid(row=1, column=8, sticky="e", padx=4, pady=(4, 0))
        for var in (self.command, self.text, self.since, self.until):
            pass
        for widget in filters.winfo_children():
            if isinstance(widget, ttk.Entry) and not isinstance(widget, ttk.Combobox):
                widget.bind("<Return>", lambda e: self.apply())
        self.level_box.bind("<<ComboboxSelected>>", lambda e: self.apply())
        self.user_box.bind("<<ComboboxSelected>>", lambda e: self.apply())

        paging = ttk.Frame(self)
        paging.grid(row=1, column=0, columnspan=2, sticky="ew", pady=6)
        ttk.Label(paging, text="Per page").pack(side="left")
        self.page_size = tk.StringVar(value="25")
        size_box = ttk.Combobox(paging, textvariable=self.page_size, values=[str(s) for s in logview.PAGE_SIZES],
                                width=5, state="readonly")
        size_box.pack(side="left", padx=(2, 12))
        size_box.bind("<<ComboboxSelected>>", lambda e: self.set_page_size())
        self.first_btn = ttk.Button(paging, text="|<", width=3, command=lambda: self.move("first"))
        self.prev_btn = ttk.Button(paging, text="<", width=3, command=lambda: self.move("prev"))
        self.range_label = ttk.Label(paging, text="0 of 0", width=28, anchor="center")
        self.next_btn = ttk.Button(paging, text=">", width=3, command=lambda: self.move("next"))
        self.last_btn = ttk.Button(paging, text=">|", width=3, command=lambda: self.move("last"))
        for w in (self.first_btn, self.prev_btn, self.range_label, self.next_btn, self.last_btn):
            w.pack(side="left", padx=1)
        ttk.Label(paging, text="Go to page").pack(side="left", padx=(12, 2))
        self.goto_var = tk.StringVar()
        goto = ttk.Entry(paging, textvariable=self.goto_var, width=5)
        goto.pack(side="left")
        goto.bind("<Return>", lambda e: self.goto())
        ttk.Button(paging, text="Refresh", command=self.reload).pack(side="right")
        self.file_label = ttk.Label(paging, text="", style="Dim.TLabel")
        self.file_label.pack(side="right", padx=8)

        self.paned = app.make_paned(self)
        self.paned.grid(row=2, column=0, columnspan=2, sticky="nsew")
        table_frame = ttk.Frame(self.paned)
        self.table = Table(table_frame, [("level", "Type"), ("time", "Time"), ("user", "User"),
                                         ("command", "Command"), ("result", "Result")],
                           widths={"level": 70, "time": 150, "user": 90, "command": 320, "result": 420},
                           selectmode="browse")
        self.table.grid_with_scroll(0, 0)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.table.bind("<<TreeviewSelect>>", lambda e: self.show_details())
        self.paned.add(table_frame, minsize=120, stretch="always")
        details_frame = ttk.Frame(self.paned)
        ttk.Label(details_frame, text="Entry details").pack(anchor="w")
        self.details = ScrolledText(details_frame, height=8, wrap="word", state="disabled")
        self.details.pack(fill="both", expand=True)
        app.appearance.register_text(self.details)
        self.paned.add(details_frame, minsize=80, stretch="never")
        self.paned.bind("<ButtonRelease-1>", lambda e: self.app.save_sash("log_details", self.paned))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self.restyle()

    def restyle(self):
        c = self.app.appearance.colors
        self.table.tag_configure("Warning", background=c["row_warn"])
        self.table.tag_configure("Error", background=c["row_err"])

    # data -----------------------------------------------------------------

    def log_path(self):
        return fmon_config.log_settings(self.app.config_path).path

    def reload(self):
        self.refresh()

    def refresh(self, keep_page=False):
        path = self.log_path()
        self.entries = logview.load_entries(path, include_rotated=self.rotated.get()) if path else []
        self.file_label.configure(text=path or "logging disabled")
        users = ["All"] + logview.users(self.entries)
        self.user_box.configure(values=users)
        if self.user.get() not in users:
            self.user.set("All")
        self.apply(keep_page=keep_page)

    def apply(self, keep_page=False):
        try:
            since = logview.parse_date(self.since.get())
            until = logview.parse_date(self.until.get(), end_of_day=True)
        except ValueError as e:
            messagebox.showerror("fmon", str(e), parent=self.app)
            return
        self.filtered = logview.filter_entries(
            self.entries, level=self.level.get(), user=self.user.get(),
            command=self.command.get().strip() or None, text=self.text.get().strip() or None,
            since=since, until=until)
        page = self.pager.page if keep_page else 1
        self.pager = logview.Pager(self.filtered, int(self.page_size.get()))
        self.pager.goto(page)
        self.render()

    def reset(self):
        self.level.set("All")
        self.user.set("All")
        for var in (self.command, self.text, self.since, self.until):
            var.set("")
        self.apply()

    def set_page_size(self):
        self.pager.set_page_size(int(self.page_size.get()))
        self.render()

    def move(self, where):
        getattr(self.pager, where)()
        self.render()

    def goto(self):
        try:
            self.pager.goto(int(self.goto_var.get()))
        except ValueError:
            return
        self.render()

    def render(self):
        self.table.clear()
        for i, e in enumerate(self.pager.page_entries()):
            self.table.insert("", "end", iid=str(i), values=(
                e.level, _fmt_dt(e.time), e.user, e.command, e.result), tags=(e.level,))
        self.range_label.configure(text=f"{self.pager.label()}   (page {self.pager.page}/{self.pager.pages})")
        self.first_btn.state(["!disabled"] if self.pager.has_prev else ["disabled"])
        self.prev_btn.state(["!disabled"] if self.pager.has_prev else ["disabled"])
        self.next_btn.state(["!disabled"] if self.pager.has_next else ["disabled"])
        self.last_btn.state(["!disabled"] if self.pager.has_next else ["disabled"])
        self.goto_var.set(str(self.pager.page))
        self._set_details("")

    def show_details(self):
        selection = self.table.selection()
        if not selection:
            return
        entry = self.pager.page_entries()[int(selection[0])]
        text = entry.text
        if entry.source:
            text = f"[file: {entry.source}]\n{text}"
        self._set_details(text)

    def _set_details(self, text):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")


class SettingsTab(ttk.Frame):
    FIELDS = (("control_dir", "Control files directory", "dir"),
              ("log_file", "Log file ('off' disables)", "file"),
              ("log_max_size", "Log max size (e.g. 1M, 500K; 0 = never rotate)", None),
              ("log_keep_days", "Days of rotated logs to keep (0 = forever)", None))

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.scroller = ScrollableFrame(self)
        self.scroller.pack(fill="both", expand=True)
        body = self.scroller.inner
        body.configure(padding=6)
        self.current = {}          # key -> value currently shown (effective), to detect changes on Save

        # -- problems banner ---------------------------------------------------
        self.problems = ttk.Frame(body)
        self.problem_label = ttk.Label(self.problems, text="", style="Err.TLabel", wraplength=900, justify="left")
        self.problem_label.pack(side="left", fill="x", expand=True)
        self.fix_button = ttk.Button(self.problems, text="Remove obsolete options", command=self.fix_obsolete)

        # -- config file location ------------------------------------------
        box = ttk.LabelFrame(body, text="Config file", padding=6)
        box.grid(row=1, column=0, sticky="ew")
        ttk.Label(box, text="In use:").grid(row=0, column=0, sticky="w")
        self.path_var = tk.StringVar(value=app.config_path)
        ttk.Entry(box, textvariable=self.path_var, state="readonly").grid(row=0, column=1, columnspan=2, sticky="ew", padx=4)
        ttk.Button(box, text="Open other...", command=self.open_other).grid(row=0, column=3, padx=2)
        self.init_button = ttk.Button(box, text="Create (init)", command=self.init)
        self.init_button.grid(row=0, column=4, padx=2)
        self.writable_label = ttk.Label(box, text="")
        self.writable_label.grid(row=1, column=0, columnspan=5, sticky="w", pady=(2, 6))

        ttk.Label(box, text="My config directory:").grid(row=2, column=0, sticky="w")
        self.ini_dir = tk.StringVar()
        ttk.Entry(box, textvariable=self.ini_dir).grid(row=2, column=1, columnspan=2, sticky="ew", padx=4)
        ttk.Button(box, text="Browse...", command=self._browse_ini_dir).grid(row=2, column=3, padx=2)
        ttk.Label(box, text="My config file name:").grid(row=3, column=0, sticky="w", pady=2)
        self.ini_name = tk.StringVar()
        ttk.Entry(box, textvariable=self.ini_name, width=30).grid(row=3, column=1, sticky="w", padx=4, pady=2)
        ttk.Button(box, text="Apply location", command=self.apply_location).grid(row=3, column=3, padx=2)
        ttk.Button(box, text="Default", command=self._default_location).grid(row=3, column=4, padx=2)
        self.location_note = ttk.Label(box, text="", style="Dim.TLabel", wraplength=900, justify="left")
        self.location_note.grid(row=4, column=0, columnspan=5, sticky="w")
        box.columnconfigure(1, weight=1)

        # -- [fmon] settings --------------------------------------------------
        settings = ttk.LabelFrame(body, text="[fmon] settings", padding=6)
        settings.grid(row=2, column=0, sticky="ew", pady=6)
        self.vars = {}
        self.origin = {}
        for row, (key, label, kind) in enumerate(self.FIELDS):
            ttk.Label(settings, text=label).grid(row=row, column=0, sticky="w", pady=2)
            var = tk.StringVar()
            self.vars[key] = var
            ttk.Entry(settings, textvariable=var, width=40).grid(row=row, column=1, sticky="ew", pady=2, padx=4)
            if kind:
                ttk.Button(settings, text="Browse...", command=lambda k=key, t=kind: self._browse_setting(k, t)).grid(
                    row=row, column=2, padx=2)
            origin = ttk.Label(settings, text="", style="Dim.TLabel", width=15)
            origin.grid(row=row, column=3, sticky="w", padx=(4, 0))
            self.origin[key] = origin
        n = len(self.FIELDS)
        ttk.Label(settings, text="The boxes show the values in effect. Clear a box and Save to return to the default.",
                  style="Dim.TLabel", wraplength=700, justify="left").grid(
            row=n, column=0, columnspan=4, sticky="w", pady=(6, 2))
        buttons = ttk.Frame(settings)
        buttons.grid(row=n + 1, column=0, columnspan=4, sticky="e")
        ttk.Button(buttons, text="Reload", command=self.refresh).pack(side="right", padx=2)
        ttk.Button(buttons, text="Save", command=self.save).pack(side="right", padx=2)
        settings.columnconfigure(1, weight=1)

        # -- appearance -------------------------------------------------------
        look = ttk.LabelFrame(body, text="Appearance (saved as your preference)", padding=6)
        look.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        ap = app.appearance
        ttk.Label(look, text="Color scheme").grid(row=0, column=0, sticky="w")
        self.palette = tk.StringVar(value=ap.palette_name)
        pal = ttk.Combobox(look, textvariable=self.palette, values=list(PALETTES), width=16, state="readonly")
        pal.grid(row=0, column=1, padx=4)
        pal.bind("<<ComboboxSelected>>", lambda e: self.apply_appearance())
        ttk.Label(look, text="Font").grid(row=0, column=2, sticky="w", padx=(12, 0))
        self.font_family = tk.StringVar(value=ap.font_family or "(default)")
        fam = ttk.Combobox(look, textvariable=self.font_family, values=["(default)"] + ap.families, width=24)
        fam.grid(row=0, column=3, padx=4)
        fam.bind("<<ComboboxSelected>>", lambda e: self.apply_appearance())
        fam.bind("<Return>", lambda e: self.apply_appearance())
        ttk.Label(look, text="Size").grid(row=0, column=4, sticky="w", padx=(12, 0))
        self.font_size = tk.StringVar(value=str(ap.font_size))
        size = ttk.Spinbox(look, textvariable=self.font_size, values=[str(v) for v in FONT_SIZES], width=4,
                           command=self.apply_appearance, state="readonly")
        size.grid(row=0, column=5, padx=4)
        ttk.Button(look, text="Apply", command=self.apply_appearance).grid(row=0, column=6, padx=(12, 2))
        ttk.Button(look, text="Reset", command=self.reset_appearance).grid(row=0, column=7, padx=2)

        # -- raw contents -----------------------------------------------------
        raw = ttk.LabelFrame(body, text="Raw contents", padding=6)
        raw.grid(row=4, column=0, sticky="nsew")
        self.raw = ScrolledText(raw, height=14, wrap="none", state="disabled")
        self.raw.pack(fill="both", expand=True)
        app.appearance.register_text(self.raw)
        body.columnconfigure(0, weight=1)

    # -- config location ----------------------------------------------------

    def _browse_ini_dir(self):
        chosen = filedialog.askdirectory(parent=self.app, initialdir=self.ini_dir.get() or os.getcwd())
        if chosen:
            self.ini_dir.set(chosen)

    def _default_location(self):
        default = fmon_config.default_user_config_path()
        self.ini_dir.set(os.path.dirname(default))
        self.ini_name.set(os.path.basename(default))

    def apply_location(self):
        directory = self.ini_dir.get().strip()
        name = self.ini_name.get().strip() or fmon_config.CONFIG_FILENAME
        if not directory:
            messagebox.showerror("fmon", "Choose a directory for the config file.", parent=self.app)
            return
        if os.sep in name or "/" in name:
            messagebox.showerror("fmon", "The file name must not contain a path.", parent=self.app)
            return
        new_path = os.path.join(directory, name)
        current = fmon_config.user_config_path()
        if os.path.abspath(new_path) == current:
            self.app.set_status("Config location unchanged.")
            return
        move = os.path.isfile(current) and not os.path.exists(new_path)
        if move and not messagebox.askyesno(
                "fmon", f"Move your config file\n{current}\nto\n{new_path} ?", parent=self.app):
            return
        try:
            new_path = fmon_config.relocate_user_config(new_path, move=move)
        except fmon_config.ConfigPermissionError as e:
            messagebox.showwarning("fmon - not permitted", str(e), parent=self.app)
            return
        except fmon_config.ConfigError as e:
            messagebox.showerror("fmon", str(e), parent=self.app)
            return
        self.app.console.append(f"config file location: {new_path}", "ok")
        self.app.set_config_path(new_path)

    # -- [fmon] settings ----------------------------------------------------

    def _browse_setting(self, key, kind):
        if kind == "dir":
            chosen = filedialog.askdirectory(parent=self.app, initialdir=self.vars[key].get() or os.getcwd())
        else:
            current = self.vars[key].get()
            chosen = filedialog.asksaveasfilename(
                parent=self.app, defaultextension=".log",
                initialdir=os.path.dirname(current) if current and current != "off" else os.getcwd(),
                initialfile=os.path.basename(current) if current and current != "off" else "fmon.log",
                filetypes=[("log files", "*.log"), ("all", "*.*")], confirmoverwrite=False)
        if chosen:
            self.vars[key].set(chosen)

    def _effective_values(self, path):
        """{key: (value to show, 'set in config' | 'default')} - never raises."""
        ini_dir = os.path.dirname(path)
        raw = {}
        try:
            parser = fmon_config.read_parser(path) if os.path.isfile(path) else None
            if parser is not None and parser.has_section(fmon_config.GLOBAL_SECTION):
                raw = {k: v.strip() for k, v in parser[fmon_config.GLOBAL_SECTION].items()}
        except fmon_config.ConfigError:
            pass
        try:
            logs = fmon_config.log_settings(path)
        except fmon_config.ConfigError:
            logs = None
        values = {}
        for key, _, _ in self.FIELDS:
            if raw.get(key):
                shown = raw[key]
                if key == "control_dir":
                    shown = os.path.abspath(os.path.join(ini_dir, shown))     # as it is used
                elif key == "log_file" and shown.lower() not in ("off", "none", "false"):
                    shown = os.path.abspath(os.path.join(ini_dir, shown))
                values[key] = (shown, "set in config")
                continue
            if key == "control_dir":
                shown = ini_dir
            elif key == "log_file":
                shown = (logs.path if logs else fmon_config.default_log_path(path)) or "off"
            elif key == "log_max_size":
                shown = fmon_config.format_size(fmon_config.DEFAULT_LOG_MAX_SIZE)
            else:
                shown = str(fmon_config.DEFAULT_LOG_KEEP_DAYS)
            values[key] = (shown, "default")
        return values

    def refresh(self):
        path = self.app.config_path
        self.path_var.set(path)
        exists = os.path.isfile(path)
        if exists:
            writable = fmon_config.is_writable(path)
            self.writable_label.configure(
                text="Writable by you: yes" if writable else
                "Writable by you: NO (read-only) - edits will be refused; ask an administrator",
                style="Ok.TLabel" if writable else "Err.TLabel")
            self.init_button.state(["disabled"])
        else:
            self.writable_label.configure(
                text="Config file not found - it is created by the first snapshot, or with Create (init).",
                style="Warn.TLabel")
            self.init_button.state(["!disabled"])

        # problems: a config that does not load, or obsolete options
        problems = []
        if self.app.config_error:
            problems.append(f"The config file cannot be loaded: {self.app.config_error}")
        self.warnings = fmon_config.config_warnings(path)
        for section, option, message in self.warnings:
            problems.append(f"[{section}] {option}: {message}")
        if problems:
            self.problem_label.configure(text="\n".join(problems),
                                         style="Err.TLabel" if self.app.config_error else "Warn.TLabel")
            self.problems.grid(row=0, column=0, sticky="ew", pady=(0, 6))
            if self.warnings and not self.app.config_error:
                self.fix_button.pack(side="right", padx=4)
            else:
                self.fix_button.pack_forget()
        else:
            self.problems.grid_forget()

        mine = fmon_config.user_config_path()
        self.ini_dir.set(os.path.dirname(mine))
        self.ini_name.set(os.path.basename(mine))
        if os.path.abspath(path) != mine:
            self.location_note.configure(text=f"The GUI is showing another file; your personal config is {mine}.")
        elif mine != fmon_config.default_user_config_path():
            self.location_note.configure(text=f"Custom location (default: {fmon_config.default_user_config_path()}).")
        else:
            self.location_note.configure(text="Default location. Change it if you may write to another directory; "
                                              "the file is moved there and the choice is remembered.")

        self.current = {}
        for key, (shown, origin) in self._effective_values(path).items():
            self.vars[key].set(shown)
            self.current[key] = shown
            self.origin[key].configure(text=f"({origin})")

        self.raw.configure(state="normal")
        self.raw.delete("1.0", "end")
        if exists:
            try:
                self.raw.insert("1.0", fmon_config.read_text(path))
            except fmon_config.ConfigError as e:
                self.raw.insert("1.0", str(e))
        self.raw.configure(state="disabled")

    def save(self):
        cmds = []
        for key, _, _ in self.FIELDS:
            new = self.vars[key].get().strip()
            if new == self.current.get(key, ""):
                continue
            cmds.append(["config", "set", key, new] if new else ["config", "unset", key])
        if not cmds:
            self.app.set_status("No changes.")
            return
        self.app.run_commands(cmds, on_done=lambda ok: self.app.after_config_change())

    def fix_obsolete(self):
        cmds = [["config", "unset", option] for _, option, _ in self.warnings]
        if cmds:
            self.app.run_commands(cmds, on_done=lambda ok: self.app.after_config_change())

    def init(self):
        self.app.run_command(["config", "init"], on_done=lambda p: self.app.after_config_change())

    def open_other(self):
        chosen = filedialog.askopenfilename(parent=self.app, filetypes=[("ini files", "*.ini"), ("all", "*.*")])
        if chosen:
            self.app.set_config_path(chosen)

    # -- appearance -----------------------------------------------------------

    def apply_appearance(self):
        family = self.font_family.get().strip()
        self.app.apply_appearance(self.palette.get(), "" if family in ("", "(default)") else family,
                                  int(self.font_size.get() or 10))

    def reset_appearance(self):
        self.palette.set(DEFAULT_PALETTE)
        self.font_family.set("(default)")
        self.font_size.set("10")
        self.apply_appearance()

    def sync_appearance(self):
        ap = self.app.appearance
        self.palette.set(ap.palette_name)
        self.font_family.set(ap.font_family or "(default)")
        self.font_size.set(str(ap.font_size))


class HelpTab(ttk.Frame):
    """The manual, with section navigation and search."""

    def __init__(self, master, app):
        super().__init__(master, padding=6)
        self.app = app
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Label(bar, text="Go to").pack(side="left")
        self.section = tk.StringVar()
        self.sections = {}
        self.section_box = ttk.Combobox(bar, textvariable=self.section, width=24, state="readonly")
        self.section_box.pack(side="left", padx=4)
        self.section_box.bind("<<ComboboxSelected>>", lambda e: self.goto_section())
        ttk.Label(bar, text="Find").pack(side="left", padx=(16, 0))
        self.query = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.query, width=24)
        entry.pack(side="left", padx=4)
        entry.bind("<Return>", lambda e: self.find())
        ttk.Button(bar, text="Find next", command=self.find).pack(side="left")
        self.hits = ttk.Label(bar, text="", style="Dim.TLabel")
        self.hits.pack(side="left", padx=8)
        ttk.Button(bar, text="About", command=app.about).pack(side="right")
        self.text = ScrolledText(self, wrap="word", state="disabled")
        self.text.pack(fill="both", expand=True)
        app.appearance.register_text(self.text, mono=True)
        self.load()

    def load(self):
        text = manual.render_manual()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.sections = {}
        for i, line in enumerate(text.splitlines(), start=1):
            self.text.insert("end", line + "\n")
            if line and not line.startswith(" ") and line.upper() == line and len(line) < 40:
                self.text.tag_add("head", f"{i}.0", f"{i}.end")
                self.sections[line.strip()] = i
        self.text.configure(state="disabled")
        self.section_box.configure(values=list(self.sections))

    def goto_section(self):
        line = self.sections.get(self.section.get())
        if line:
            self.text.see(f"{line}.0")
            self.text.yview(f"{line}.0")

    def find(self):
        query = self.query.get().strip()
        self.text.tag_remove("hit", "1.0", "end")
        if not query:
            self.hits.configure(text="")
            return
        start = self.text.index("insert")
        pos = self.text.search(query, start + "+1c", nocase=True, stopindex="end")
        if not pos:
            pos = self.text.search(query, "1.0", nocase=True, stopindex="end")
        if not pos:
            self.hits.configure(text="not found")
            return
        end = f"{pos}+{len(query)}c"
        self.text.tag_add("hit", pos, end)
        self.text.mark_set("insert", pos)
        self.text.see(pos)
        count = 0
        idx = "1.0"
        while True:
            idx = self.text.search(query, idx, nocase=True, stopindex="end")
            if not idx:
                break
            count += 1
            idx = f"{idx}+{len(query)}c"
        self.hits.configure(text=f"{count} match(es)")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class App(tk.Tk):
    TAB_NAMES = ("Targets", "Run", "Schedule", "Log", "Settings", "Help")

    def __init__(self, config_path=None, auto_start_scheduler=True):
        super().__init__()
        self.config_path = os.path.abspath(config_path or fmon_config.user_config_path())
        self.prefs = fmon_config.load_preferences()
        self.events = queue.Queue()
        self.scheduler = None
        self.pending = 0
        self.config_error = None
        self.title(f"fmon - {self.config_path}")
        self.geometry(self.prefs.get("geometry", "1180x780"))
        self.minsize(900, 600)

        self.appearance = Appearance(self)
        self.appearance.apply(self.prefs.get("palette", DEFAULT_PALETTE), self.prefs.get("font_family", ""),
                              self.prefs.get("font_size", 10))
        self._build_menu()

        top = ttk.Frame(self, padding=(8, 4))
        top.pack(fill="x")
        ttk.Label(top, text="fmon", style="Title.TLabel").pack(side="left")
        ttk.Label(top, text="  folder audit", style="Dim.TLabel").pack(side="left")
        ttk.Button(top, text="About", command=self.about).pack(side="right")
        ttk.Button(top, text="Help", command=lambda: self.show_tab("Help")).pack(side="right", padx=4)

        # status bar first (bottom) so it is never squeezed out by the panes
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(self, textvariable=self.status_var, anchor="w", style="Status.TLabel", padding=(6, 3)).pack(
            side="bottom", fill="x")
        self.paned = self.make_paned(self)
        self.paned.pack(fill="both", expand=True)
        self.notebook = ttk.Notebook(self.paned)
        self.console = Console(self.paned)
        self.targets_tab = TargetsTab(self.notebook, self)
        self.run_tab = RunTab(self.notebook, self)
        self.schedule_tab = ScheduleTab(self.notebook, self)
        self.log_tab = LogTab(self.notebook, self)
        self.settings_tab = SettingsTab(self.notebook, self)
        self.help_tab = HelpTab(self.notebook, self)
        self.tabs = {"Targets": self.targets_tab, "Run": self.run_tab, "Schedule": self.schedule_tab,
                     "Log": self.log_tab, "Settings": self.settings_tab, "Help": self.help_tab}
        for name in self.TAB_NAMES:
            self.notebook.add(self.tabs[name], text=name)
        self.paned.add(self.notebook, minsize=200, stretch="always")
        self.paned.add(self.console, minsize=60, stretch="never")
        self.console_shown = True
        self._current_tab = "Targets"
        self.notebook.bind("<<NotebookTabChanged>>", lambda e: self._tab_changed())
        self.paned.bind("<ButtonRelease-1>", lambda e: self.save_sash(f"sash_{self._current_tab}", self.paned))

        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(POLL_MS, self._poll)
        self.refresh_all()
        self.after(50, self._restore_layout)
        if auto_start_scheduler and any(t.schedule for t in self.load_targets()):
            self.start_scheduler()

    # -- menu / about ----------------------------------------------------------

    def _build_menu(self):
        menubar = tk.Menu(self)
        filemenu = tk.Menu(menubar, tearoff=False)
        filemenu.add_command(label="Open other config...", command=lambda: self.settings_tab.open_other())
        filemenu.add_command(label="Use my config", command=lambda: self.set_config_path(fmon_config.user_config_path()))
        filemenu.add_separator()
        filemenu.add_command(label="Exit", command=self.close)
        menubar.add_cascade(label="File", menu=filemenu)
        view = tk.Menu(menubar, tearoff=False)
        schemes = tk.Menu(view, tearoff=False)
        for name in PALETTES:
            schemes.add_command(label=name, command=lambda n=name: self.apply_appearance(palette=n))
        view.add_cascade(label="Color scheme", menu=schemes)
        view.add_command(label="Larger font", accelerator="Ctrl++", command=lambda: self.bump_font(1))
        view.add_command(label="Smaller font", accelerator="Ctrl+-", command=lambda: self.bump_font(-1))
        view.add_separator()
        view.add_command(label="Reset layout", command=self.reset_layout)
        menubar.add_cascade(label="View", menu=view)
        helpmenu = tk.Menu(menubar, tearoff=False)
        helpmenu.add_command(label="Manual", command=lambda: self.show_tab("Help"))
        helpmenu.add_command(label="About fmon", command=self.about)
        menubar.add_cascade(label="Help", menu=helpmenu)
        self.configure(menu=menubar)
        self.bind("<Control-plus>", lambda e: self.bump_font(1))
        self.bind("<Control-equal>", lambda e: self.bump_font(1))
        self.bind("<Control-minus>", lambda e: self.bump_font(-1))

    def about(self):
        AboutDialog(self).show()

    def show_tab(self, name):
        self.notebook.select(self.tabs[name])

    # -- appearance --------------------------------------------------------------

    def apply_appearance(self, palette=None, family=None, size=None):
        self.appearance.apply(palette, family, size)
        self.log_tab.restyle()
        self.targets_tab.render()
        self.schedule_tab.refresh()
        self.settings_tab.sync_appearance()
        self.prefs.update(palette=self.appearance.palette_name, font_family=self.appearance.font_family,
                          font_size=self.appearance.font_size)
        self.save_prefs()

    def bump_font(self, delta):
        size = min(max(self.appearance.font_size + delta, FONT_SIZES[0]), FONT_SIZES[-1])
        self.apply_appearance(size=size)

    # -- layout (sashes) -----------------------------------------------------------

    def make_paned(self, master):
        return tk.PanedWindow(master, orient="vertical", sashwidth=9, sashrelief="raised", sashpad=1,
                              showhandle=True, handlesize=10, bg=self.appearance.colors["sash"],
                              opaqueresize=True, sashcursor="sb_v_double_arrow")

    def save_sash(self, key, paned):
        try:
            x, y = paned.sash_coord(0)
        except tk.TclError:
            return
        self.prefs[key] = y
        self.save_prefs()

    def _apply_sash(self, key, paned, default_ratio):
        try:
            height = paned.winfo_height()
            if height < 50:
                return
            y = self.prefs.get(key)
            if y is None:
                y = int(height * default_ratio)
            paned.sash_place(0, 0, max(60, min(int(y), height - 60)))
        except tk.TclError:
            pass

    def _restore_layout(self):
        self._tab_changed()
        self._apply_sash("log_details", self.log_tab.paned, 0.6)

    def _tab_changed(self):
        name = self.notebook.tab(self.notebook.select(), "text") if self.notebook.select() else "Targets"
        self._current_tab = name
        if name in TABS_WITHOUT_CONSOLE:
            if self.console_shown:
                self.paned.forget(self.console)
                self.console_shown = False
        else:
            if not self.console_shown:
                self.paned.add(self.console, minsize=60, stretch="never")
                self.console_shown = True
            self.update_idletasks()
            self._apply_sash(f"sash_{name}", self.paned, DEFAULT_SASH_RATIO.get(name, 0.65))

    def reset_layout(self):
        for key in list(self.prefs):
            if key.startswith("sash_") or key in ("log_details", "geometry"):
                del self.prefs[key]
        self.save_prefs()
        self.geometry("1180x780")
        self.after(50, self._restore_layout)

    def save_prefs(self):
        try:
            fmon_config.save_preferences(self.prefs)
        except OSError as e:
            self.set_status(f"could not save preferences: {e}")

    # -- services for the tabs ----------------------------------------------

    def set_status(self, text):
        self.status_var.set(text)

    def load_targets(self):
        if not os.path.isfile(self.config_path):
            self.config_error = None
            return []
        try:
            targets = fmon_config.load_config(self.config_path, allow_empty=True)
        except fmon_config.ConfigError as e:
            if str(e) != self.config_error:
                self.console.append(f"config error: {e}", "err")
            self.config_error = str(e)
            self.set_status(f"Config error: {e}")
            return []
        self.config_error = None
        return targets

    def run_command(self, args, on_done=None, with_config=True):
        """Run one fmon command in the background; output goes to the console."""
        self.run_commands([args], on_done=(lambda ok, procs: on_done(procs[-1])) if on_done else None,
                          with_config=with_config)

    def run_commands(self, commands, on_done=None, with_config=True):
        """Run several commands sequentially (stops at the first failure).
        on_done(ok) or on_done(ok, procs) is called in the UI thread."""
        self.pending += 1
        self.set_status("Running: fmon " + " ".join(commands[0]) + (" ..." if len(commands) > 1 else ""))

        def work():
            procs = []
            ok = True
            for args in commands:
                cmd_text = "fmon " + " ".join(args)
                self.events.put(("echo", cmd_text))
                proc = fmon_schedule.run_fmon(args, self.config_path if with_config else None, origin="gui")
                procs.append(proc)
                self.events.put(("output", proc))
                if proc.returncode == fmon_schedule.EXIT_ERROR:
                    ok = False
                    break
            self.events.put(("done", ok, procs, on_done))

        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                self._handle(event)
        except queue.Empty:
            pass
        self.after(POLL_MS, self._poll)

    def _handle(self, event):
        kind = event[0]
        if kind == "echo":
            self.console.append(f"$ {event[1]}", "cmd")
        elif kind == "output":
            proc = event[1]
            if proc.stdout:
                self.console.append(proc.stdout)
            if proc.stderr:
                tag = "warn" if "not permitted" in proc.stderr else "err"
                self.console.append(proc.stderr, tag)
            if proc.returncode:
                self.console.append(f"[exit {proc.returncode}]", "err" if proc.returncode == 2 else "warn")
        elif kind == "done":
            _, ok, procs, on_done = event
            self.pending -= 1
            last = procs[-1] if procs else None
            if not ok and last is not None and "not permitted" in (last.stderr or ""):
                messagebox.showwarning("fmon - not permitted",
                                       last.stderr.strip() + "\n\nThe configuration is read-only for you.",
                                       parent=self)
            elif not ok and last is not None:
                self.set_status((last.stderr or "command failed").strip().splitlines()[0])
            else:
                self.set_status("Done" if last is None or last.returncode == 0 else
                                "Done - changes detected (see console)")
            if on_done:
                try:
                    on_done(ok, procs)
                except TypeError:
                    on_done(ok)
        elif kind == "scheduler":
            self.schedule_tab.on_event(event[1])

    def after_check(self):
        self.targets_tab.render()
        self.schedule_tab.refresh()
        self.log_tab.refresh(keep_page=True)

    def after_config_change(self):
        self.refresh_all()
        if self.scheduler:
            self.scheduler.reload()

    def refresh_all(self):
        self.targets_tab.refresh()
        self.schedule_tab.refresh()
        self.settings_tab.refresh()
        self.log_tab.refresh()

    # -- scheduler -----------------------------------------------------------

    def start_scheduler(self):
        if self.scheduler and self.scheduler.running:
            return
        self.scheduler = fmon_schedule.Scheduler(
            self.config_path, on_event=lambda ev: self.events.put(("scheduler", ev)))
        self.scheduler.start()
        self.console.append("scheduler started", "ok")
        self.schedule_tab.refresh()

    def stop_scheduler(self):
        if self.scheduler:
            self.scheduler.stop(wait=False)
            self.console.append("scheduler stopped", "warn")
        self.schedule_tab.refresh()
        self._refresh_when_stopped()

    def _refresh_when_stopped(self, tries=50):
        """The scheduler thread may still be finishing a check; refresh the
        Schedule tab once it has really stopped (without blocking the UI)."""
        if self.scheduler and self.scheduler.running and tries > 0:
            self.after(200, lambda: self._refresh_when_stopped(tries - 1))
        else:
            self.schedule_tab.refresh()

    def set_config_path(self, path):
        was_running = bool(self.scheduler and self.scheduler.running)
        self.stop_scheduler()
        self.scheduler = None
        self.config_path = os.path.abspath(path)
        self.title(f"fmon - {self.config_path}")
        self.refresh_all()
        if was_running or any(t.schedule for t in self.load_targets()):
            self.start_scheduler()

    def close(self):
        if self.scheduler:
            self.scheduler.stop(wait=False)
        try:
            if self.console_shown:
                self.save_sash(f"sash_{self._current_tab}", self.paned)
            self.prefs["geometry"] = self.geometry()
            self.save_prefs()
        except tk.TclError:
            pass
        self.destroy()


def main(config_path=None, argv=None):
    import argparse
    if config_path is None:
        parser = argparse.ArgumentParser(prog="fmon-gui", description="fmon graphical application")
        parser.add_argument("-f", "--config", metavar="INI", help="Config file to open")
        args = parser.parse_args(argv)
        config_path = args.config
    app = App(config_path)
    app.mainloop()


if __name__ == "__main__":
    main()
