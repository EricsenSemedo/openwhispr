"""Tests for restarting a model server after the GPU device is lost (2026-11-08).

    python -I tests\\test_recovery.py

No real speech or model server is called and no running process of the app is
touched. The end-to-end test starts its own small fake speech server (hidden, on
a free port) and stands in for the supervisor. The fixtures are made up.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import wave
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch

ADAPTER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADAPTER))

import flow_llm  # noqa: E402
import flow_recovery  # noqa: E402
import local_cleanup_shim as shim  # noqa: E402

asr = shim.asr
EXE = r'C:\apps\runtime\nemo\nemo-speech.exe'


def http_error(code, body=b'{}'):
    return urllib.error.HTTPError('http://127.0.0.1/', code, 'failed', {}, BytesIO(body))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Profile:
    """A temp profile folder holding status.json and local-asr-runtime.json."""

    def __init__(self, test, pid=4242, exe=EXE):
        self.dir = tempfile.TemporaryDirectory()
        test.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name)
        (self.path / 'local-asr-logs').mkdir()
        (self.path / 'local-asr-logs' / 'status.json').write_text(json.dumps(
            {'services': {'nemo': {'pid': pid, 'status': 'ready'}, 'cleanup-llm': {'pid': 77, 'status': 'ready'}}}))
        (self.path / 'local-asr-runtime.json').write_text(json.dumps(
            {'services': [{'name': 'nemo', 'executable': exe}, {'name': 'cleanup-llm', 'executable': r'C:\apps\llama-server.exe'}]}))


class RestartTests(unittest.TestCase):
    def make(self, image=EXE, **kw):
        self.ended, self.logged, self.clock = [], [], Clock()
        profile = Profile(self, **kw)
        return flow_recovery.Restarter(lambda: profile.path, lambda e, **f: self.logged.append((e, f)),
                                       clock=self.clock, sleep=self.clock.sleep,
                                       image_path=lambda pid: image,
                                       terminate=lambda pid: self.ended.append(pid) or True)

    def test_ends_the_reported_process(self):
        self.assertTrue(self.make().restart('nemo', 'test'))
        self.assertEqual(self.ended, [4242])

    def test_refuses_a_process_running_another_program(self):
        # Another llama-server on this PC, or a PID reused after a stale status.
        r = self.make(image=r'C:\other\hermes\llama-server.exe')
        self.assertFalse(r.restart('nemo', 'test'))
        self.assertEqual(self.ended, [])
        self.assertEqual(self.logged[-1][1]['result'], 'refused-not-the-service')

    def test_refuses_when_the_process_is_gone_or_unreadable(self):
        r = self.make(image=None)
        self.assertFalse(r.restart('nemo', 'test'))
        self.assertEqual(self.ended, [])

    def test_path_case_does_not_matter_on_windows(self):
        r = self.make(image=EXE.upper() if os.name == 'nt' else EXE)
        self.assertTrue(r.restart('nemo', 'test'))

    def test_cooldown_per_service(self):
        r = self.make()
        self.assertTrue(r.restart('nemo', 'test'))
        self.assertFalse(r.restart('nemo', 'test'))
        self.clock.now += flow_recovery.RESTART_COOLDOWN_S + 1
        self.assertTrue(r.restart('nemo', 'test'))
        self.assertEqual(len(self.ended), 2)

    def test_missing_or_bad_files_end_nothing(self):
        for broken in ('status', 'runtime', 'pid'):
            r = self.make()
            profile = r.profile_dir()
            if broken == 'status':
                (profile / 'local-asr-logs' / 'status.json').write_text('{not json')
            elif broken == 'runtime':
                (profile / 'local-asr-runtime.json').unlink()
            else:
                (profile / 'local-asr-logs' / 'status.json').write_text(
                    json.dumps({'services': {'nemo': {'pid': None}}}))
            self.assertFalse(r.restart('nemo', 'test'), broken)
            self.assertEqual(self.ended, [], broken)

    def test_healthy_server_is_left_alone(self):
        r = self.make()
        self.assertTrue(r.recover_speech(lambda: True))
        self.assertEqual(self.ended, [])

    def test_broken_server_is_replaced_and_waited_for(self):
        r = self.make()
        answers = iter([False, False, False, True])
        self.assertTrue(r.recover_speech(lambda: next(answers)))
        self.assertEqual(self.ended, [4242])
        self.assertEqual(self.logged[-1][0], 'service-recovered')

    def test_a_server_still_loading_is_not_ended(self):
        r = self.make()
        status = r.profile_dir() / 'local-asr-logs' / 'status.json'
        data = json.loads(status.read_text())
        data['services']['nemo']['status'] = 'loading'
        status.write_text(json.dumps(data))
        self.assertFalse(r.restart('nemo', 'test'))
        self.assertEqual(self.ended, [])
        answers = iter([False, False, True])
        self.assertTrue(r.recover_speech(lambda: next(answers)))  # it waits instead
        self.assertEqual(self.ended, [])

    def test_a_load_that_finishes_broken_is_ended_during_the_wait(self):
        r = self.make()
        status = r.profile_dir() / 'local-asr-logs' / 'status.json'
        ready = status.read_text()
        data = json.loads(ready)
        data['services']['nemo']['status'] = 'loading'
        status.write_text(json.dumps(data))
        answers = iter([False, False, False, False, True])

        def probe():
            answer = next(answers)
            status.write_text(ready)
            return answer
        self.assertTrue(r.recover_speech(probe))
        self.assertEqual(self.ended, [4242])

    def test_a_status_file_being_replaced_is_tried_again(self):
        r = self.make()
        status = r.profile_dir() / 'local-asr-logs' / 'status.json'
        good = status.read_text()
        status.write_text('{not json')
        answers = iter([False, False, False, True])

        def probe():
            answer = next(answers)
            status.write_text(good)
            return answer
        self.assertTrue(r.recover_speech(probe))
        self.assertEqual(self.ended, [4242])

    def test_a_failed_end_does_not_start_the_cooldown(self):
        r = self.make()
        r.terminate = lambda pid: False
        self.assertFalse(r.restart('nemo', 'test'))
        self.assertEqual(self.logged[-1][1]['result'], 'failed')
        r.terminate = lambda pid: self.ended.append(pid) or True
        self.assertTrue(r.restart('nemo', 'test'))

    def test_gives_up_after_the_wait(self):
        r = self.make()
        self.assertFalse(r.recover_speech(lambda: False))
        self.assertEqual(self.logged[-1][0], 'service-not-recovered')
        self.assertLessEqual(self.clock.now - 1000.0, flow_recovery.READY_WAIT_S + flow_recovery.READY_POLL_S)

    @unittest.skipUnless(os.name == 'nt', 'Windows process API')
    def test_real_process(self):
        # A hidden child of this test, ended through the real Windows calls.
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(child.wait, 10)
        self.addCleanup(child.kill)
        profile = Profile(self, pid=child.pid, exe=sys.executable)
        r = flow_recovery.Restarter(lambda: profile.path)
        self.assertTrue(r.restart('nemo', 'test'))
        self.assertEqual(child.wait(10), 1)


class DecodeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for name in ('transcribe', 'recover_backend'):
            self.addCleanup(setattr, asr, name, getattr(asr, name))
        self.wav = str(Path(self.dir.name) / 'a.wav')
        with open(self.wav, 'wb') as out:
            out.write(asr.probe_wav_bytes())

    def fake(self, replies):
        calls = []

        def transcribe(path, model, language, prompt):
            calls.append(path)
            reply = replies[len(calls) - 1]
            if isinstance(reply, Exception):
                raise reply
            return reply
        asr.transcribe = transcribe
        return calls

    def test_classification(self):
        self.assertTrue(asr.backend_failed(http_error(500)))
        self.assertTrue(asr.backend_failed(http_error(503)))
        self.assertTrue(asr.backend_failed(urllib.error.URLError('refused')))
        self.assertTrue(asr.backend_failed(ConnectionResetError()))
        self.assertFalse(asr.backend_failed(TimeoutError()))  # maybe only slow
        self.assertFalse(asr.backend_failed(urllib.error.URLError(TimeoutError())))
        self.assertFalse(asr.backend_failed(http_error(400)))
        self.assertFalse(asr.backend_failed(ValueError('bad reply')))

    def test_decodes_again_after_recovery(self):
        calls = self.fake([http_error(500), 'Water the tomatoes.'])
        asked = []
        asr.recover_backend = lambda probe: asked.append(probe) or True
        self.assertEqual(asr.transcribe_with_retry(self.wav, 'm', None, None), 'Water the tomatoes.')
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(asked), 1)

    def test_error_reaches_the_app_when_recovery_fails(self):
        self.fake([http_error(500)])
        asr.recover_backend = lambda probe: False
        with self.assertRaises(urllib.error.HTTPError):
            asr.transcribe_with_retry(self.wav, 'm', None, None)

    def test_only_one_more_decode(self):
        calls = self.fake([http_error(500), http_error(500)])
        asr.recover_backend = lambda probe: True
        with self.assertRaises(urllib.error.HTTPError):
            asr.transcribe_with_retry(self.wav, 'm', None, None)
        self.assertEqual(len(calls), 2)

    def test_request_errors_never_restart(self):
        self.fake([http_error(400)])
        asked = []
        asr.recover_backend = lambda probe: asked.append(1) or True
        with self.assertRaises(urllib.error.HTTPError):
            asr.transcribe_with_retry(self.wav, 'm', None, None)
        self.assertEqual(asked, [])

    def test_a_locked_temp_file_does_not_lose_the_result(self):
        self.fake(['', 'Water the tomatoes.'])
        with patch.object(asr.os, 'remove', side_effect=PermissionError()):
            self.assertEqual(asr.transcribe_with_retry(self.wav, 'm', None, None), 'Water the tomatoes.')

    def test_no_hook_keeps_the_old_behaviour(self):
        self.fake([http_error(500)])
        asr.recover_backend = None
        with self.assertRaises(urllib.error.HTTPError):
            asr.transcribe_with_retry(self.wav, 'm', None, None)


class CleanupModelTests(unittest.TestCase):
    CFG = {'baseUrl': 'http://127.0.0.1:1/v1', 'enabled': True, 'minWords': 1, 'timeoutMs': 500, 'maxTimeoutMs': 500}
    SETTINGS = {'conservativeCleanup': True, 'llmCleanup': CFG, 'applyStyle': True}

    def run_with(self, error):
        restarts = []
        with patch.object(flow_llm, 'call_model', side_effect=error), \
                patch.object(shim.RESTARTER, 'restart_soon', lambda s, w: restarts.append((s, w))):
            text, info = shim.flow_cleanup('please water the tomatoes at six', ctx=None, category='other',
                                           vocabulary=[], settings=self.SETTINGS)
        self.assertIn('water the tomatoes', text)  # the rules text is pasted either way
        self.assertEqual(info['engine'], 'rules')
        return restarts

    def test_device_lost_restarts_the_model(self):
        lost = http_error(500, b'{"error":{"message":"decode() failed: vk::Queue::submit: ErrorDeviceLost"}}')
        self.assertEqual(self.run_with(lost), [('cleanup-llm', 'device-lost')])

    def test_another_server_error_does_not(self):
        self.assertEqual(self.run_with(http_error(500, b'{"error":"bad template"}')), [])

    def test_busy_gpu_or_loading_model_does_not(self):
        self.assertEqual(self.run_with(TimeoutError()), [])
        self.assertEqual(self.run_with(http_error(503)), [])
        self.assertEqual(self.run_with(urllib.error.URLError('refused')), [])


class QuietLogTests(unittest.TestCase):
    def printed(self, exc):
        server = shim.QuietServer.__new__(shim.QuietServer)
        err = StringIO()
        with patch('sys.stderr', err):
            try:
                raise exc
            except Exception:
                server.handle_error(None, ('127.0.0.1', 1))
        return err.getvalue()

    def test_a_client_hanging_up_prints_nothing(self):
        for exc in (ConnectionAbortedError(10053, 'aborted'), ConnectionResetError(), BrokenPipeError(), TimeoutError()):
            self.assertEqual(self.printed(exc), '')

    def test_real_errors_still_print(self):
        self.assertIn('KeyError', self.printed(KeyError('oops')))


FAKE_NEMO = r'''
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
port, lost = int(sys.argv[1]), sys.argv[2]
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        broken = os.path.exists(lost)
        body = json.dumps({'error': 'device lost'} if broken else {'text': 'Water the tomatoes.'}).encode()
        self.send_response(500 if broken else 200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
HTTPServer(('127.0.0.1', port), H).serve_forever()
'''


@unittest.skipUnless(os.name == 'nt', 'Windows process API')
class EndToEndTest(unittest.TestCase):
    """The 2026-11-07 failure, with a fake server: it stays up and answers 500
    to every decode until the process is replaced. A small loop stands in for
    the supervisor: it starts the server again when the process exits."""

    def test_dictation_succeeds_across_a_lost_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            script, lost = tmp / 'fake_nemo.py', tmp / 'device-lost'
            script.write_text(FAKE_NEMO)
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
            sock.close()
            (tmp / 'local-asr-logs').mkdir()
            (tmp / 'local-asr-runtime.json').write_text(json.dumps(
                {'services': [{'name': 'nemo', 'executable': sys.executable}]}))
            state = {'child': None, 'starts': 0, 'stop': False}

            def write_status():
                (tmp / 'local-asr-logs' / 'status.json').write_text(json.dumps(
                    {'services': {'nemo': {'pid': state['child'].pid, 'status': 'ready'}}}))

            def launch():
                state['child'] = subprocess.Popen(
                    [sys.executable, '-I', str(script), str(port), str(lost)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                state['starts'] += 1
                write_status()

            def supervise():
                while not state['stop']:
                    if state['child'].poll() is not None:
                        lost.unlink(missing_ok=True)  # a new process gets a working device
                        launch()
                    time.sleep(0.1)

            launch()
            for name in ('NEMO_BASE_URL', 'recover_backend'):
                self.addCleanup(setattr, asr, name, getattr(asr, name))
            asr.NEMO_BASE_URL = f'http://127.0.0.1:{port}'
            events = []
            restarter = flow_recovery.Restarter(lambda: tmp, lambda e, **f: events.append((e, f)))
            asr.recover_backend = restarter.recover_speech
            watcher = threading.Thread(target=supervise, daemon=True)
            watcher.start()
            try:
                wav = tmp / 'a.wav'
                wav.write_bytes(asr.probe_wav_bytes())
                deadline = time.monotonic() + 10
                while not asr.backend_answers('m'):  # let the fake server come up
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.1)
                self.assertEqual(asr.transcribe_with_retry(str(wav), 'm', None, None), 'Water the tomatoes.')
                self.assertEqual(state['starts'], 1)  # healthy: nothing restarted

                lost.write_text('x')
                self.assertEqual(asr.transcribe_with_retry(str(wav), 'm', None, None), 'Water the tomatoes.')
                self.assertEqual(state['starts'], 2)
                self.assertEqual(events[0][0], 'service-restart')
                self.assertEqual(events[0][1]['result'], 'ended')
                self.assertEqual(events[-1][0], 'service-recovered')
            finally:
                state['stop'] = True
                watcher.join(5)
                state['child'].kill()
                state['child'].wait(5)


if __name__ == '__main__':
    unittest.main()
