"""Where is the user dictating? (Windows UI Automation, read-only)

Wispr Flow sends the app name, the text around the cursor and the selection
with every dictation. This module captures the same, locally:

  {'exe': 'T3 Code (Nightly).exe', 'title': '...', 'before': '...' | None,
   'after': '...' | None, 'selected': '...', 'caret_known': bool,
   'password': bool, 'at': monotonic seconds, 'ms': capture time}

Nothing leaves the machine except to the local cleanup model. Password fields
return no text. All UI Automation calls run on one worker thread with a short
deadline, so a slow or hung target app can never stall a dictation.
"""
from __future__ import annotations

import ctypes
import os
import queue
import threading
import time
from ctypes import wintypes

BEFORE_CHARS = 400
AFTER_CHARS = 200
SELECTED_CHARS = 2000

if os.name == 'nt':
    _user32 = ctypes.WinDLL('user32', use_last_error=True)
    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                     ctypes.POINTER(wintypes.DWORD)]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def foreground_app() -> dict:
    if os.name != 'nt':
        return {}
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return {}
    n = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32.GetWindowTextW(hwnd, buf, n + 1)
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = ''
    handle = _kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if handle:
        try:
            name = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(name))
            if _kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
                exe = os.path.basename(name.value)
        finally:
            _kernel32.CloseHandle(handle)
    return {'hwnd': int(hwnd), 'pid': pid.value, 'exe': exe, 'title': buf.value[:300]}


class _UiaWorker:
    """Owns the COM apartment and the IUIAutomation object.

    UI Automation calls into the target app; a hung app could otherwise block
    for ~20 s per call. Connection and transaction timeouts are set to 300 ms
    where available, and requests whose caller already gave up are skipped,
    so a backlog never builds up."""

    def __init__(self):
        self.jobs: queue.Queue = queue.Queue()
        self.target_hwnd = None
        self.thread = threading.Thread(target=self._run, name='uia-context', daemon=True)
        self.thread.start()

    def _run(self):
        try:
            import sys
            sys.coinit_flags = 0  # COINIT_MULTITHREADED; comtypes initialises COM on import
            import comtypes
            import comtypes.client
            try:
                comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            except OSError:
                pass  # already initialised for this thread
            comtypes.client.GetModule('UIAutomationCore.dll')
            from comtypes.gen import UIAutomationClient as UIA
            self.UIA = UIA
            try:
                self.uia = comtypes.client.CreateObject(UIA.CUIAutomation8, interface=UIA.IUIAutomation2)
                self.uia.ConnectionTimeout = 300
                self.uia.TransactionTimeout = 300
            except Exception:
                self.uia = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
        except Exception as exc:
            failure = {'error': 'uia-unavailable: ' + type(exc).__name__}
            while True:
                self.jobs.get()[0].put(failure)
        while True:
            reply, deadline = self.jobs.get()
            if time.monotonic() > deadline:
                continue  # the caller has already moved on
            try:
                reply.put(self._read())
            except Exception as exc:  # a target app's provider can throw anything
                reply.put({'error': type(exc).__name__})

    def _read(self) -> dict:
        if self.target_hwnd is not None:  # tests: read a given window's text field
            root = self.uia.ElementFromHandle(self.target_hwnd)
            cond = self.uia.CreatePropertyCondition(self.UIA.UIA_IsTextPatternAvailablePropertyId, True)
            found = root.FindFirst(self.UIA.TreeScope_Subtree, cond)
            return self._read_element(found) if found else {}
        return self._read_element(self.uia.GetFocusedElement())

    def _read_element(self, el) -> dict:
        UIA = self.UIA
        if not el:  # comtypes NULL pointers are falsy
            return {}
        out = {'control_pid': el.CurrentProcessId, 'password': bool(el.CurrentIsPassword)}
        if out['password']:
            return out
        try:
            pattern = el.GetCurrentPattern(UIA.UIA_TextPatternId)
            tp = pattern.QueryInterface(UIA.IUIAutomationTextPattern) if pattern else None
        except Exception:
            tp = None
        if tp is not None:
            sel = tp.GetSelection()
            if sel is not None and sel.Length > 0:
                r = sel.GetElement(0)
                start, end, char = 0, 1, 0  # TextPatternRangeEndpoint_Start/End, TextUnit_Character
                b = r.Clone()
                b.MoveEndpointByRange(end, r, start)
                b.MoveEndpointByUnit(start, char, -BEFORE_CHARS)
                a = r.Clone()
                a.MoveEndpointByRange(start, r, end)
                a.MoveEndpointByUnit(end, char, AFTER_CHARS)
                out.update(before=b.GetText(BEFORE_CHARS + 10)[-BEFORE_CHARS:],
                           after=a.GetText(AFTER_CHARS + 10)[:AFTER_CHARS],
                           selected=r.GetText(SELECTED_CHARS), caret_known=True)
                return out
        try:
            pattern = el.GetCurrentPattern(UIA.UIA_ValuePatternId)
            vp = pattern.QueryInterface(UIA.IUIAutomationValuePattern) if pattern else None
            if vp is not None:
                value = vp.CurrentValue or ''
                # Caret position is unknown: useful as context, not for casing.
                out.update(field_text=value[-BEFORE_CHARS:], caret_known=False)
        except Exception:
            pass
        return out

    def read(self, timeout: float) -> dict:
        reply: queue.Queue = queue.Queue(maxsize=1)
        self.jobs.put((reply, time.monotonic() + timeout))
        try:
            return reply.get(timeout=timeout)
        except queue.Empty:
            return {'error': 'timeout'}


_worker: _UiaWorker | None = None
_worker_lock = threading.Lock()


def capture(timeout: float = 0.4) -> dict:
    """Snapshot the foreground app and focused text field."""
    global _worker
    t0 = time.monotonic()
    ctx = foreground_app()
    if os.name == 'nt' and ctx:
        with _worker_lock:
            if _worker is None:
                _worker = _UiaWorker()
        field = _worker.read(timeout)
        # Only trust field text that belongs to the foreground app.
        if field.get('control_pid') in (None, ctx.get('pid')) or field.get('password'):
            ctx.update({k: v for k, v in field.items() if k != 'control_pid'})
        else:
            ctx['other_process_focus'] = True
    ctx.setdefault('caret_known', False)
    ctx['at'] = time.monotonic()
    ctx['ms'] = round((ctx['at'] - t0) * 1000)
    return ctx


class ContextCache:
    """Context is captured when the audio arrives (in parallel with speech
    recognition) and used by the cleanup request a moment later.

    Each capture has a sequence number. Only the newest capture is ever
    returned, and only once it has finished; a capture that is still running
    or was superseded yields None rather than older text."""

    def __init__(self, max_age: float = 60.0):
        self.max_age = max_age
        self.lock = threading.Lock()
        self.seq = 0
        self.done: threading.Event | None = None
        self.value: dict | None = None

    def start_capture(self):
        done = threading.Event()
        with self.lock:
            self.seq += 1
            seq = self.seq
            self.done = done

        def run():
            try:
                value = capture()
            except Exception as exc:
                value = {'error': type(exc).__name__, 'at': time.monotonic()}
            value['seq'] = seq
            with self.lock:
                if seq == self.seq:
                    self.value = value
            done.set()

        threading.Thread(target=run, name='context-capture', daemon=True).start()

    def latest(self, wait: float = 0.5) -> dict | None:
        with self.lock:
            done, seq = self.done, self.seq
        if done is None or not done.wait(wait):
            return None
        with self.lock:
            value = self.value
        if (not value or value.get('seq') != seq or value.get('used')
                or time.monotonic() - value.get('at', 0) > self.max_age):
            return None
        return value

    def take(self, hwnd: int, not_before: float) -> dict | None:
        """The newest snapshot of window hwnd, once, if taken after not_before."""
        value = self.latest(wait=0.2)
        if not value or value.get('hwnd') != hwnd or value.get('at', 0) <= not_before:
            return None
        with self.lock:
            if value.get('used'):
                return None
            value['used'] = True
        return value
