"""Speak through UI Automation notifications.

Narrator has no direct speech API, but it reads UIA notification events
raised by the foreground window. Requires Windows 10 1709 or later.
"""

import ctypes
import logging
from ctypes import wintypes

log = logging.getLogger(__name__)

NOTIFICATION_KIND_OTHER = 4
NOTIFICATION_PROCESSING_ALL = 2
NOTIFICATION_PROCESSING_MOST_RECENT = 3

_ACTIVITY_ID = "MCF.Announcement"

try:
    _uia = ctypes.windll.uiautomationcore
    _oleaut = ctypes.windll.oleaut32
    _uia.UiaHostProviderFromHwnd.argtypes = [wintypes.HWND, ctypes.POINTER(ctypes.c_void_p)]
    _uia.UiaHostProviderFromHwnd.restype = ctypes.c_long
    _uia.UiaRaiseNotificationEvent.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
    ]
    _uia.UiaRaiseNotificationEvent.restype = ctypes.c_long
    _oleaut.SysAllocString.argtypes = [ctypes.c_wchar_p]
    _oleaut.SysAllocString.restype = ctypes.c_void_p
    _oleaut.SysFreeString.argtypes = [ctypes.c_void_p]
    _AVAILABLE = True
except (AttributeError, OSError):
    _AVAILABLE = False

_Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)


def available() -> bool:
    return _AVAILABLE


def notify(hwnd: int, text: str, interrupt: bool = False) -> bool:
    """Raise a notification on the window. Returns True on success."""
    if not _AVAILABLE or not hwnd:
        return False
    provider = ctypes.c_void_p()
    if _uia.UiaHostProviderFromHwnd(hwnd, ctypes.byref(provider)) != 0 or not provider:
        return False
    text_bstr = _oleaut.SysAllocString(text)
    activity_bstr = _oleaut.SysAllocString(_ACTIVITY_ID)
    try:
        processing = NOTIFICATION_PROCESSING_MOST_RECENT if interrupt else NOTIFICATION_PROCESSING_ALL
        result = _uia.UiaRaiseNotificationEvent(
            provider, NOTIFICATION_KIND_OTHER, processing, text_bstr, activity_bstr
        )
        return result == 0
    except OSError:
        log.exception("UIA notification failed")
        return False
    finally:
        _oleaut.SysFreeString(text_bstr)
        _oleaut.SysFreeString(activity_bstr)
        vtable = ctypes.cast(provider, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        _Release(vtable[2])(provider)


def narrator_running() -> bool:
    """True when Windows Narrator is running."""
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = wintypes.HWND
    return bool(user32.FindWindowW("Narrator Helper Window", None)) or _process_running("Narrator.exe")


def _process_running(name: str) -> bool:
    TH32CS_SNAPPROCESS = 0x2

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return False
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == name.lower():
                return True
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return False
    finally:
        kernel32.CloseHandle(snapshot)
