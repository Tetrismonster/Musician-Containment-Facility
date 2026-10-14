"""Hidden child processes that can't outlive MCF.

Every process MCF starts is created without a window and placed in a Windows
job object. Closing the job object's handle, which Windows does automatically
if MCF exits or crashes, ends every process in it.
"""

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Sequence

log = logging.getLogger(__name__)

if sys.platform == "win32":
    import win32api
    import win32con
    import win32job

    _PRIORITY = {
        "idle": subprocess.IDLE_PRIORITY_CLASS,
        "below_normal": subprocess.BELOW_NORMAL_PRIORITY_CLASS,
        "normal": subprocess.NORMAL_PRIORITY_CLASS,
    }
else:  # Allows the pure-logic tests to import this module elsewhere
    _PRIORITY = {}

if sys.platform == "win32" and getattr(sys, "frozen", False):
    # The packaged app points Windows' DLL search at its own _internal folder,
    # and child processes inherit that. The runtime's Python would then load
    # MCF's DLLs instead of its own and crash, so reset it before starting any.
    import ctypes
    ctypes.windll.kernel32.SetDllDirectoryW(None)

# Variables the packaged app's launcher sets for itself; children must not see them
_LAUNCHER_VARIABLES = ("_MEIPASS2", "_PYI_", "TCL_LIBRARY", "TK_LIBRARY", "PYTHONHOME", "PYTHONPATH")


class _Job:
    """One job object for the whole MCF session."""

    def __init__(self):
        self.handle = None
        if sys.platform != "win32":
            return
        self.handle = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation, info)

    def add(self, pid: int) -> None:
        if self.handle is None:
            return
        access = win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE
        process = win32api.OpenProcess(access, False, pid)
        try:
            win32job.AssignProcessToJobObject(self.handle, process)
        finally:
            win32api.CloseHandle(process)


_job: _Job | None = None
_job_lock = threading.Lock()


def _session_job() -> _Job:
    global _job
    with _job_lock:
        if _job is None:
            _job = _Job()
        return _job


class Process:
    """A hidden child process whose output lines are passed to a callback."""

    def __init__(self, args: Sequence[str], env: dict[str, str] | None = None, cwd: Path | None = None,
                 on_line: Callable[[str], None] | None = None, priority: str = "normal",
                 stdin_data: bytes | None = None):
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | _PRIORITY.get(priority, 0)
        log.info("Starting %s", " ".join(str(a) for a in args))
        self.popen = subprocess.Popen(
            [str(a) for a in args], env=env, cwd=str(cwd) if cwd else None,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=flags,
        )
        _session_job().add(self.popen.pid)
        self.on_line = on_line
        self.lines: list[str] = []
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        if stdin_data is not None:
            threading.Thread(target=self._write_stdin, args=(stdin_data,), daemon=True).start()

    def _write_stdin(self, data: bytes) -> None:
        try:
            self.popen.stdin.write(data)
            self.popen.stdin.close()
        except OSError:
            pass  # The process ended or was cancelled

    def _read(self) -> None:
        for raw in self.popen.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            self.lines.append(line)
            del self.lines[:-400]  # Keep only the tail, for error reports
            if self.on_line:
                try:
                    self.on_line(line)
                except Exception:
                    log.exception("Output handler failed")

    def wait(self) -> int:
        code = self.popen.wait()
        self._reader.join(timeout=5)
        return code

    def kill(self) -> None:
        """End the process and everything it started."""
        if self.popen.poll() is not None:
            return
        if sys.platform == "win32":
            # taskkill /T ends the whole tree; the job object is the safety net
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.popen.pid)],
                           creationflags=subprocess.CREATE_NO_WINDOW,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            self.popen.kill()

    def tail(self, count: int = 30) -> str:
        return "\n".join(self.lines[-count:])


def run(args: Sequence[str], env: dict[str, str] | None = None, cwd: Path | None = None,
        on_line: Callable[[str], None] | None = None,
        cancel: threading.Event | None = None) -> tuple[int, str]:
    """Run a hidden process to completion. Returns the exit code and the output tail."""
    process = Process(args, env=env, cwd=cwd, on_line=on_line)
    if cancel is not None:
        def watch():
            while process.popen.poll() is None:
                if cancel.wait(0.3):
                    process.kill()
                    return
        threading.Thread(target=watch, daemon=True).start()
    code = process.wait()
    return code, process.tail()


def base_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in list(env):
        if name.startswith(_LAUNCHER_VARIABLES):
            del env[name]
    if getattr(sys, "frozen", False):
        # Drop the packaged app's own folder from PATH, if the launcher added it
        internal = str(Path(sys.executable).parent).lower()
        env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                      if p and not p.lower().startswith(internal))
    return env
