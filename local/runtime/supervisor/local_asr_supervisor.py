"""Own local ASR children for exactly one OpenWhispr process lifetime."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request


class WindowsLifetime:
    """A retained parent handle avoids PID reuse; the Job prevents orphans."""
    def __init__(self, parent_pid, parent_exe):
        if os.name != "nt":
            raise RuntimeError("This worker is only for native Windows OpenWhispr")
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        api = self.api
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        api.SetInformationJobObject.restype = wintypes.BOOL
        api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        api.AssignProcessToJobObject.restype = wintypes.BOOL
        api.GetCurrentProcess.argtypes = []
        api.GetCurrentProcess.restype = wintypes.HANDLE
        api.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        api.IsProcessInJob.restype = wintypes.BOOL
        api.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        api.QueryFullProcessImageNameW.restype = wintypes.BOOL
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        self.parent = self.job = None
        try:
            self.parent = api.OpenProcess(0x00100000 | 0x1000, False, parent_pid)
            if not self.parent:
                raise ctypes.WinError(ctypes.get_last_error())
            name = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(name))
            if not api.QueryFullProcessImageNameW(self.parent, 0, name, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            if Path(name.value).name.lower() != "openwhispr.exe" or os.path.normcase(name.value) != os.path.normcase(parent_exe):
                raise RuntimeError("Parent is not the expected OpenWhispr executable")

            class BasicLimits(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

            class IoCounters(ctypes.Structure):
                _fields_ = [(key, ctypes.c_uint64) for key in ("ReadOperationCount", "WriteOperationCount",
                    "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class ExtendedLimits(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

            self.job = api.CreateJobObjectW(None, None)
            if not self.job:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not api.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            # Join before any Popen: every descendant inherits membership at
            # creation, including during the spawn/verification window. Closing
            # this last noninherited Job handle also terminates this worker.
            if not api.AssignProcessToJobObject(self.job, api.GetCurrentProcess()):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def alive(self):
        result = self.api.WaitForSingleObject(self.parent, 0)
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 258  # WAIT_TIMEOUT: parent still running

    def assign(self, child):
        member = wintypes.BOOL()
        if not self.api.IsProcessInJob(wintypes.HANDLE(int(child._handle)), self.job, ctypes.byref(member)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not member.value:
            raise RuntimeError("ASR child did not inherit its worker's Windows Job")

    def close(self):
        for attr in ("job", "parent"):
            handle = getattr(self, attr, None)
            if handle:
                self.api.CloseHandle(handle)
                setattr(self, attr, None)


REQUIRED = ("nemo", "adapter")


def port_busy(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def health(service):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(service["healthUrl"], timeout=1) as response:
            data = json.load(response)
        if service["name"] == "nemo":
            return data.get("ready") is True and "asr" in data.get("capabilities", [])
        if service["name"] == "cleanup-llm":
            return data.get("status") == "ok"  # llama-server /health
        return data.get("ready") is True
    except (OSError, ValueError):
        return False


def validate(config):
    if config.get("enabled") is not True:
        raise ValueError("Runtime is disabled")
    services = config.get("services", [])
    names = [s["name"] for s in services]
    # The optional cleanup-llm (2026-10-12) is the small local model used for
    # context-aware cleanup. ASR readiness never waits for it, and a broken
    # cleanup-llm entry is dropped rather than stopping speech recognition.
    if sorted(names) not in (["adapter", "nemo"], ["adapter", "cleanup-llm", "nemo"]):
        raise ValueError("Expected the local NeMo backend and format adapter")
    required = [s for s in services if s["name"] in REQUIRED]
    _check_services(required)
    optional = [s for s in services if s["name"] not in REQUIRED]
    for service in optional:
        try:
            _check_services([*required, service])
        except (ValueError, KeyError, TypeError):
            continue
        required.append(service)
    return required


def _check_services(services):
    if len({s["port"] for s in services}) != len(services):
        raise ValueError("Services must use distinct ports")
    for service in services:
        if not isinstance(service["port"], int) or not 1024 <= service["port"] <= 65535:
            raise ValueError("Invalid service port")
        suffix = "/ready" if service["name"] == "nemo" else "/health"
        if service["healthUrl"] != f'http://127.0.0.1:{service["port"]}{suffix}':
            raise ValueError("Health checks must stay on the configured loopback port")
        if not Path(service["executable"]).is_absolute() or not Path(service["executable"]).is_file():
            raise ValueError("Missing executable")
        if not isinstance(service["args"], list) or not all(isinstance(a, str) for a in service["args"]):
            raise ValueError("Arguments must be a string array")
        if not Path(service["cwd"]).is_dir():
            raise ValueError("Missing working directory")
    return services


class Supervisor:
    def __init__(self, services, logs, lifetime, *, launch=subprocess.Popen, busy=port_busy,
            probe=health, clock=time.monotonic):
        self.services, self.logs, self.lifetime = services, Path(logs), lifetime
        self.launch, self.busy, self.probe, self.clock = launch, busy, probe, clock
        self.logs.mkdir(parents=True, exist_ok=True)
        self.states = {s["name"]: {"child": None, "failures": 0, "retryAt": 0, "checkAt": 0,
            "ready": False, "status": "starting", "error": None} for s in services}
        self.last_status = None

    def step(self):
        now = self.clock()
        for service in self.services:
            state = self.states[service["name"]]
            child = state["child"]
            if child and child.poll() is not None:
                state.update(child=None, ready=False, error=f"Exited with code {child.returncode}")
                state["failures"] += 1
                state["retryAt"] = now + min(30, 2 ** min(state["failures"], 5))
                state["status"] = "restarting"
            if state["child"] is None and now >= state["retryAt"]:
                if self.busy(service["port"]):
                    state.update(status="blocked", ready=False, error="Port already owned by another process", retryAt=now + 3)
                else:
                    child = None
                    try:
                        with open(self.logs / f'{service["name"]}.log', "ab", buffering=0) as output:
                            child = self.launch([service["executable"], *service["args"]],
                                cwd=service["cwd"], stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                        self.lifetime.assign(child)
                        state.update(child=child, status="loading", error=None, checkAt=now)
                    except Exception as exc:
                        if child is not None:
                            child.kill()
                            child.wait(timeout=3)
                        state["failures"] += 1
                        state.update(status="restarting", error=str(exc),
                            retryAt=now + min(30, 2 ** min(state["failures"], 5)))
            if state["child"] and now >= state["checkAt"]:
                state["ready"] = self.probe(service)
                state["checkAt"] = now + (30 if state["ready"] else 3)
                state["status"] = "ready" if state["ready"] else "loading"
                # A health failure never kills an in-flight dictation. Only
                # exited children restart; missing readiness remains visible.
                if state["ready"]:
                    state["failures"] = 0
        self.write_status()

    def write_status(self, stopped=False):
        status = {"parentPid": os.getppid(), "supervisorPid": os.getpid(),
            "ready": not stopped and all(s["ready"] for name, s in self.states.items() if name in REQUIRED),
            "services": {name: {"pid": s["child"].pid if s["child"] and not stopped else None,
                "status": "stopped" if stopped else s["status"], "error": s["error"]}
                for name, s in self.states.items()}}
        if status != self.last_status:
            target = self.logs / "status.json"
            pending = target.with_suffix(".json.next")
            try:
                pending.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
                os.replace(pending, target)
            except OSError:
                # A status reader, antivirus scan or temporary disk problem
                # must never stop an active dictation. Retry on the next step.
                return
            self.last_status = status

    def close(self):
        try:
            self.write_status(stopped=True)
        finally:
            # Closing our own Job deliberately terminates this worker too.
            self.lifetime.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--parent-exe", required=True)
    args = parser.parse_args()
    config_path = Path(args.config)
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    services = validate(config)
    lifetime = WindowsLifetime(args.parent_pid, args.parent_exe)
    try:
        runner = Supervisor(services, config_path.parent / "local-asr-logs", lifetime)
        try:
            while lifetime.alive():
                runner.step()
                time.sleep(0.5)
        finally:
            runner.close()
    finally:
        lifetime.close()


if __name__ == "__main__":
    main()
