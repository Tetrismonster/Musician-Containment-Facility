"""Messages from the worker to MCF.

Each message is one line on stdout: a marker, then JSON. Anything else on
stdout or stderr is library output, which MCF only writes to the log.
"""

import json
import sys
import threading

MARKER = "\x1eMCF "
_lock = threading.Lock()
_out = sys.__stdout__


def send(kind: str, **data) -> None:
    data["type"] = kind
    line = MARKER + json.dumps(data, ensure_ascii=True)
    with _lock:
        _out.write(line + "\n")
        _out.flush()


def stage(name: str) -> None:
    """name: load, analyse, plan, generate, decode, save or train."""
    send("stage", name=name)


def progress(fraction: float, text: str = "") -> None:
    send("progress", fraction=max(0.0, min(1.0, float(fraction))), text=text)


def status(text: str) -> None:
    send("status", text=text)


def result(**data) -> None:
    send("result", **data)


def error(code: str, message: str, detail: str = "") -> None:
    send("error", code=code, message=message, detail=detail[-4000:])
