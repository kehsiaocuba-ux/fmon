"""Scheduled checks.

A target with `schedule = 15m` in the config is checked every 15 minutes
while a Scheduler is running - inside the GUI, or headless with
`fmon watch`. Checks are executed through the normal command line
(`python -m fmon check -f INI -t NAME`), so they are audit-logged exactly
like manual runs, tagged "[scheduled]".
"""
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from fmon import config as fmon_config

ORIGIN_ENV = "FMON_ORIGIN"   # tag added to the logged command: [gui], [scheduled]

EXIT_OK, EXIT_CHANGES, EXIT_ERROR = 0, 1, 2
STATUS_TEXT = {EXIT_OK: "no changes", EXIT_CHANGES: "CHANGES", EXIT_ERROR: "error"}


def fmon_command(args, config_path=None):
    """Command line that runs fmon with the current interpreter."""
    cmd = [sys.executable, "-m", "fmon"] + list(args)
    if config_path and "-f" not in args and "--config" not in args:
        cmd += ["-f", config_path]
    return cmd


def run_fmon(args, config_path=None, origin=None, timeout=None):
    """Run an fmon command as a subprocess; returns CompletedProcess."""
    env = dict(os.environ)
    if origin:
        env[ORIGIN_ENV] = origin
    kwargs = {}
    if os.name == "nt":  # no console window flashing up from the GUI
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        fmon_command(args, config_path), capture_output=True, text=True,
        env=env, timeout=timeout, **kwargs,
    )


@dataclass
class JobState:
    name: str
    interval: int
    next_run: float                 # time.time() of the next check
    last_run: datetime = None
    last_status: int = None         # exit code of the last check
    last_summary: str = ""
    last_output: str = ""
    running: bool = False

    @property
    def status_text(self):
        if self.running:
            return "running..."
        if self.last_status is None:
            return "not run yet"
        return STATUS_TEXT.get(self.last_status, f"exit {self.last_status}")


@dataclass
class Event:
    kind: str               # "started" | "finished" | "reloaded" | "error"
    name: str = None
    state: JobState = None
    message: str = ""
    targets: list = field(default_factory=list)


class Scheduler:
    """Runs the scheduled checks of a config file in a background thread.

    on_event(Event) is called from the scheduler thread (GUI code must hand
    it over to the UI thread, e.g. through a queue).
    """

    def __init__(self, config_path, on_event=None, runner=None, reload_every=10):
        self.config_path = os.path.abspath(config_path)
        self.on_event = on_event or (lambda event: None)
        self.runner = runner or self._default_runner
        self.reload_every = reload_every
        self.jobs = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._last_reload = 0

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self.reload()
        self._thread = threading.Thread(target=self._loop, name="fmon-scheduler", daemon=True)
        self._thread.start()

    def stop(self, wait=True):
        self._stop.set()
        self._wake.set()
        if wait and self._thread is not None:
            self._thread.join(timeout=5)

    def reload(self):
        """Re-read the config: add/remove/re-time jobs. Safe to call any time."""
        try:
            targets = fmon_config.load_config(self.config_path, allow_empty=True)
        except fmon_config.ConfigError as e:
            self.on_event(Event("error", message=str(e)))
            return
        now = time.time()
        with self._lock:
            wanted = {t.name: t for t in targets if t.schedule}
            for name in list(self.jobs):
                if name not in wanted:
                    del self.jobs[name]
            for name, t in wanted.items():
                job = self.jobs.get(name)
                if job is None:
                    self.jobs[name] = JobState(name=name, interval=t.schedule, next_run=now + t.schedule)
                elif job.interval != t.schedule:
                    base = job.last_run.timestamp() if job.last_run else now
                    job.interval = t.schedule
                    job.next_run = max(now, base + t.schedule)
        self._last_reload = now
        self.on_event(Event("reloaded", targets=targets))
        self._wake.set()

    def snapshot(self):
        """Copy of the job states, for display."""
        with self._lock:
            return {name: JobState(**vars(job)) for name, job in self.jobs.items()}

    def run_now(self, name):
        """Schedule an immediate check of one target."""
        with self._lock:
            job = self.jobs.get(name)
            if job:
                job.next_run = 0
        self._wake.set()

    # -- internals ---------------------------------------------------------

    def _loop(self):
        while not self._stop.is_set():
            now = time.time()
            if now - self._last_reload >= self.reload_every:
                self.reload()
            due = None
            with self._lock:
                for job in self.jobs.values():
                    if not job.running and job.next_run <= now:
                        due = job
                        job.running = True
                        break
            if due is not None:
                self._run_job(due)
                continue
            with self._lock:
                upcoming = [j.next_run for j in self.jobs.values() if not j.running]
            wait = min(upcoming) - time.time() if upcoming else self.reload_every
            self._wake.clear()
            self._wake.wait(timeout=max(0.2, min(wait, self.reload_every)))

    def _run_job(self, job):
        self.on_event(Event("started", name=job.name, state=JobState(**vars(job))))
        try:
            status, output = self.runner(job.name)
        except Exception as e:  # never let one job kill the scheduler
            status, output = EXIT_ERROR, f"{type(e).__name__}: {e}"
        with self._lock:
            job.running = False
            job.last_run = datetime.now()
            job.last_status = status
            job.last_output = output
            job.last_summary = _summarize(output, status)
            job.next_run = time.time() + job.interval
            state = JobState(**vars(job))
        self.on_event(Event("finished", name=job.name, state=state))

    def _default_runner(self, name):
        proc = run_fmon(["check", "-t", name], self.config_path, origin="scheduled")
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _summarize(output, status):
    """One-line summary of a check's output for the schedule table."""
    for line in output.splitlines():
        if line.startswith("Added:") or line.startswith("error:"):
            counts = {}
            for l in output.splitlines():
                for key in ("Added", "Removed", "Modified"):
                    if l.startswith(key + ":"):
                        counts[key] = l.split(":", 1)[1].strip()
            if counts:
                return ", ".join(f"{v} {k.lower()}" for k, v in counts.items())
            return line[len("error:"):].strip()
    return STATUS_TEXT.get(status, "")
