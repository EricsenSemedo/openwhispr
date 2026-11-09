import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("supervisor", Path(__file__).resolve().parents[1] / "local_asr_supervisor.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Child:
    pid = 123
    returncode = None
    killed = False
    def poll(self): return self.returncode
    def kill(self): self.killed = True; self.returncode = 1
    def wait(self, timeout=None): return self.returncode


class Lifetime:
    def __init__(self): self.children = []; self.fail_assign = False
    def assign(self, child):
        if self.fail_assign: raise OSError("job assignment failed")
        self.children.append(child)
    def close(self):
        for child in self.children: child.kill()


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.services = [{"name": name, "port": port, "healthUrl": f"http://127.0.0.1:{port}/{suffix}",
            "executable": sys.executable, "args": ["-V"], "cwd": self.tmp.name}
            for name, port, suffix in [("nemo", 8080, "ready"), ("adapter", 18765, "health")]]
        self.now = 0
        self.created = []
        self.lifetime = Lifetime()
        self.busy_ports = set()
        def launch(*args, **kwargs):
            child = Child(); self.created.append((child, args, kwargs)); return child
        self.runner = module.Supervisor(self.services, self.tmp.name, self.lifetime,
            launch=launch, busy=lambda port: port in self.busy_ports,
            probe=lambda service: True, clock=lambda: self.now)

    def test_starts_each_child_once_and_stays_warm_when_idle(self):
        for self.now in range(20): self.runner.step()
        self.assertEqual(len(self.created), 2)
        self.assertTrue(json.loads((Path(self.tmp.name) / "status.json").read_text())["ready"])

    def test_stopped_model_recovers_without_restarting_adapter(self):
        self.runner.step()
        first = self.created[0][0]
        adapter = self.created[1][0]
        first.returncode = 1
        self.runner.step()
        self.assertEqual(self.runner.states["nemo"]["status"], "restarting")
        self.assertEqual(len(self.created), 2)
        self.now = 2
        self.runner.step()
        self.assertEqual(len(self.created), 3)
        self.assertIs(self.runner.states["adapter"]["child"], adapter)

    def test_port_collision_does_not_adopt_or_kill_unrelated_listener(self):
        self.busy_ports.add(8080)
        self.runner.step()
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.runner.states["nemo"]["status"], "blocked")
        self.busy_ports.clear(); self.now = 3; self.runner.step()
        self.assertEqual(len(self.created), 2)

    def test_assignment_failure_cleans_up_unowned_child(self):
        self.lifetime.fail_assign = True
        self.runner.step()
        self.assertTrue(all(item[0].killed for item in self.created))
        self.assertTrue(all(state["child"] is None for state in self.runner.states.values()))

    def test_failed_health_does_not_interrupt_transcription(self):
        self.runner.step()
        self.runner.probe = lambda _: False
        self.now = 30; self.runner.step()
        self.assertEqual(len(self.created), 2)
        self.assertFalse(any(item[0].killed for item in self.created))

    def test_shutdown_releases_job_owned_children(self):
        self.runner.step(); self.runner.close()
        self.assertTrue(all(item[0].killed for item in self.created))
        status = json.loads((Path(self.tmp.name) / "status.json").read_text())
        self.assertFalse(status["ready"])
        self.assertTrue(all(s["status"] == "stopped" for s in status["services"].values()))

    def test_validate_rejects_external_health_endpoint(self):
        self.services[0]["healthUrl"] = "https://example.com/ready"
        with self.assertRaises(ValueError): module.validate({"enabled": True, "services": self.services})

    def test_optional_cleanup_model_is_accepted_but_never_blocks_readiness(self):
        llm = {"name": "cleanup-llm", "port": 18767, "healthUrl": "http://127.0.0.1:18767/health",
            "executable": sys.executable, "args": ["-V"], "cwd": self.tmp.name}
        self.assertEqual(len(module.validate({"enabled": True, "services": [*self.services, llm]})), 3)
        with self.assertRaises(ValueError):
            module.validate({"enabled": True, "services": [*self.services, dict(llm, name="other")]})
        # A broken optional model is dropped; speech recognition still starts.
        for broken in (dict(llm, port=18765), dict(llm, executable=self.tmp.name + "/missing.exe"),
                       dict(llm, cwd=self.tmp.name + "/missing")):
            kept = module.validate({"enabled": True, "services": [*self.services, broken]})
            self.assertEqual(sorted(s["name"] for s in kept), ["adapter", "nemo"])
        runner = module.Supervisor([*self.services, llm], self.tmp.name, self.lifetime,
            launch=lambda *a, **k: Child(), busy=lambda port: False,
            probe=lambda service: service["name"] != "cleanup-llm", clock=lambda: self.now)
        runner.step()
        status = json.loads((Path(self.tmp.name) / "status.json").read_text(encoding="utf-8"))
        self.assertTrue(status["ready"])
        self.assertEqual(status["services"]["cleanup-llm"]["status"], "loading")

    def test_status_publication_failure_cannot_interrupt_dictation(self):
        with patch.object(module.os, "replace", side_effect=PermissionError("reader holds status file")):
            self.runner.step()
        self.assertFalse(any(item[0].killed for item in self.created))
        self.assertIsNone(self.runner.last_status)
        self.runner.step()
        self.assertTrue(json.loads((Path(self.tmp.name) / "status.json").read_text())["ready"])

    def test_real_subprocesses_are_replaced_and_stopped(self):
        children = []
        class RealLifetime:
            def assign(self, child): children.append(child)
            def close(self):
                for child in children:
                    if child.poll() is None: child.kill()
                    child.wait(timeout=3)
        for service in self.services:
            service["args"] = ["-c", "import time; time.sleep(60)"]
        runner = module.Supervisor(self.services, self.tmp.name, RealLifetime(),
            busy=lambda _: False, probe=lambda _: True, clock=lambda: self.now)
        self.addCleanup(runner.close)
        runner.step(); original = children[0]; adapter = children[1]
        original.kill(); original.wait(timeout=3)
        runner.step(); self.now = 2; runner.step()
        self.assertEqual(len(children), 3)
        self.assertIsNone(adapter.poll())
        runner.close()
        self.assertTrue(all(c.poll() is not None for c in children))


if __name__ == "__main__": unittest.main()
