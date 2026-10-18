"""Put dictated text on the Windows clipboard without adding it to Win+V
clipboard history or cloud clipboard sync (what Wispr Flow does for pastes).

Windows honours three registered formats set alongside the text:
  ExcludeClipboardContentFromMonitorProcessing  (any data)
  CanIncludeInClipboardHistory                  (DWORD 0)
  CanUploadToCloudClipboard                     (DWORD 0)
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

_user32 = ctypes.WinDLL('user32', use_last_error=True)
_kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_user32.SetClipboardData.restype = wintypes.HANDLE
_user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
_user32.RegisterClipboardFormatW.restype = wintypes.UINT
_user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
_user32.CreateWindowExW.restype = wintypes.HWND
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalLock.restype = wintypes.LPVOID
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]

_user32.DestroyWindow.argtypes = [wintypes.HWND]

_lock = threading.Lock()


def _owner_window():
    # A clipboard owner window is required (with a NULL owner, EmptyClipboard
    # leaves no owner and SetClipboardData can fail). It is created and
    # destroyed on the calling thread around each write, like pyperclip, so no
    # window without a message loop is ever left owning the clipboard (other
    # apps would wait on its WM_DESTROYCLIPBOARD). HWND_MESSAGE = -3.
    hwnd = _user32.CreateWindowExW(0, 'STATIC', 'openwhispr-clipboard', 0, 0, 0, 0, 0,
                                   wintypes.HWND(-3), None, None, None)
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())
    return hwnd


def _global(data: bytes):
    handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    ptr = _kernel32.GlobalLock(handle)
    if not ptr:
        _kernel32.GlobalFree(handle)
        raise ctypes.WinError(ctypes.get_last_error())
    ctypes.memmove(ptr, data, len(data))
    _kernel32.GlobalUnlock(handle)
    return handle


def set_private_text(text: str, attempts: int = 10, deadline: float | None = None) -> None:
    """deadline (time.monotonic) - give up rather than write late."""
    payload = [(CF_UNICODETEXT, (text + '\0').encode('utf-16-le'))]
    for name, data in (('ExcludeClipboardContentFromMonitorProcessing', b'\0\0\0\0'),
                       ('CanIncludeInClipboardHistory', b'\0\0\0\0'),
                       ('CanUploadToCloudClipboard', b'\0\0\0\0')):
        payload.append((_user32.RegisterClipboardFormatW(name), data))
    with _lock:
        hwnd = _owner_window()
        try:
            _write(hwnd, payload, attempts, deadline)
        finally:
            _user32.DestroyWindow(hwnd)


def _write(hwnd, payload, attempts, deadline):
    for i in range(attempts):
        if deadline is not None and time.monotonic() > deadline:
            raise OSError('clipboard busy past the deadline')
        if _user32.OpenClipboard(hwnd):
            break
        time.sleep(0.01 * (i + 1))  # another app holds the clipboard briefly
    else:
        raise OSError('clipboard busy')
    try:
        if not _user32.EmptyClipboard():
            raise ctypes.WinError(ctypes.get_last_error())
        for fmt, data in payload:
            handle = _global(data)
            if not _user32.SetClipboardData(fmt, handle):
                _kernel32.GlobalFree(handle)
                raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _user32.CloseClipboard()
