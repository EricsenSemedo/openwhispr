"""Restart a local model server that the GPU took down.

When the graphics driver resets, Vulkan reports "device lost" to nemo-speech and
llama-server. They stay alive and their health checks stay green, but every
request fails until the process is replaced. The supervisor only restarts
children that exit (a health failure must never kill a dictation in flight), so
nothing recovers on its own: on 2026-11-07 every dictation failed until the app
was restarted by hand.

This module ends the broken process and leaves the rest to the supervisor, which
starts it again about 2 s later. It is called only after a real request failed,
and it is careful about what it ends:

  * only the PID the supervisor reports for that service (status.json);
  * only if that PID is running the executable configured for that service
    (local-asr-runtime.json), so another llama-server on this PC is never touched;
  * at most once per RESTART_COOLDOWN_S for each service.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

RESTART_COOLDOWN_S = 120.0
READY_WAIT_S = 25.0
READY_POLL_S = 0.5


def _image_path(pid: int) -> str | None:
    """Full executable path of a running process, or None."""
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)]
    api.QueryFullProcessImageNameW.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        name = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(name))
        return name.value if api.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)) else None
    finally:
        api.CloseHandle(handle)


def _terminate(pid: int) -> bool:
    if os.name != 'nt':
        return False
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.TerminateProcess.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
    if not handle:
        return False
    try:
        return bool(api.TerminateProcess(handle, 1))
    finally:
        api.CloseHandle(handle)


class Restarter:
    def __init__(self, profile_dir, log=lambda *a, **k: None, *, clock=time.monotonic, sleep=time.sleep,
                 image_path=_image_path, terminate=_terminate):
        self.profile_dir = profile_dir  # a callable: the options path can change at startup
        self.log, self.clock, self.sleep = log, clock, sleep
        self.image_path, self.terminate = image_path, terminate
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def _target(self, service: str) -> tuple[int, str] | None:
        profile = Path(self.profile_dir())
        try:
            status = json.loads((profile / 'local-asr-logs' / 'status.json').read_text(encoding='utf-8-sig'))
            runtime = json.loads((profile / 'local-asr-runtime.json').read_text(encoding='utf-8-sig'))
            pid = status['services'][service]['pid']
            exe = next(s['executable'] for s in runtime['services'] if s['name'] == service)
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            return None
        return (pid, exe) if isinstance(pid, int) and isinstance(exe, str) else None

    def restart(self, service: str, why: str) -> bool:
        """End the service's process. True only if it was ended now."""
        with self._lock:
            now = self.clock()
            last = self._last.get(service)
            if last is not None and now - last < RESTART_COOLDOWN_S:
                return False
            target = self._target(service)
            if target is None:
                self.log('service-restart', service=service, why=why, result='no-target')
                return False
            pid, exe = target
            actual = self.image_path(pid)
            if not actual or os.path.normcase(actual) != os.path.normcase(exe):
                self.log('service-restart', service=service, why=why, result='refused-not-the-service')
                return False
            self._last[service] = now
            ok = self.terminate(pid)
            self.log('service-restart', service=service, why=why, result='ended' if ok else 'failed')
            return ok

    def restart_soon(self, service: str, why: str) -> None:
        """For the cleanup model: rules-only text is pasted meanwhile, so never wait."""
        threading.Thread(target=self.restart, args=(service, why), daemon=True).start()

    def recover_speech(self, probe) -> bool:
        """After a failed decode. probe() decodes a tiny clip and returns
        True when the server answers. True here means "decode again".

        A server that answers the probe is healthy and the failure was about
        that one clip, so it is left alone. Otherwise it is replaced (or it is
        already being replaced) and this waits until the probe passes."""
        if probe():
            return True
        self.restart('nemo', 'decode-failed')
        deadline = self.clock() + READY_WAIT_S
        while self.clock() < deadline:
            self.sleep(READY_POLL_S)
            if probe():
                self.log('service-recovered', service='nemo')
                return True
        self.log('service-not-recovered', service='nemo')
        return False
