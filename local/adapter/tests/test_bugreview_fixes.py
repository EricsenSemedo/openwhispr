"""Tests for the bug-review fixes of 2026-10-17.

    python -I tests\\test_bugreview_fixes.py

1. The strict guard (strict_reason) checks the cleanup model in every app, and
   a rejected light cleanup is retried once with the minimal agent prompt.
2. An empty transcript for a clip that has sound is retried once, 6 dB louder.

No model or speech server is called: both are replaced by fakes. The fixtures
are made-up sentences.
"""
import array
import math
import os
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path

ADAPTER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADAPTER))

import flow_llm  # noqa: E402
import nemo_openwhispr_shim as asr  # noqa: E402

SAID = ('so I think we should repaint the fence this weekend and if the shop has the green paint '
        'we can also fix the gate before Dana gets back')
# A light cleanup that loses a whole clause (the kind of edit replay found).
DROPS_CLAUSE = 'So I think we should repaint the fence this weekend before Dana gets back.'
# The minimal fix: punctuation only.
MINIMAL = ('So I think we should repaint the fence this weekend, and if the shop has the green paint, '
           'we can also fix the gate before Dana gets back.')


class FakeCalls:
    """Stands in for flow_llm.call_model: a reply per system prompt."""

    def __init__(self, replies, delay=0.0):
        self.replies, self.delay, self.calls = replies, delay, []

    def __call__(self, base_url, model, system, user, max_tokens, timeout, api_key=None):
        self.calls.append({'system': system, 'timeout': timeout})
        if self.delay:
            time.sleep(self.delay)
        reply = self.replies[system]
        if isinstance(reply, Exception):
            raise reply
        return reply


class StrictGuardTests(unittest.TestCase):
    CFG = {'baseUrl': 'http://127.0.0.1:9/v1', 'timeoutMs': 2500}

    def setUp(self):
        self._real = flow_llm.call_model
        self.addCleanup(setattr, flow_llm, 'call_model', self._real)

    def run_clean(self, fake, category, text=SAID, cfg=None):
        flow_llm.call_model = fake
        return flow_llm.clean_with_model(text, ctx=None, style='formal', category=category,
                                         vocabulary=[], hints=[], cfg=cfg or self.CFG)

    def test_dropped_clause_is_rejected_in_every_app(self):
        # drift_reason alone accepted this; strict_reason does not.
        self.assertIsNone(flow_llm.drift_reason(SAID, DROPS_CLAUSE))
        self.assertEqual(flow_llm.strict_reason(SAID, DROPS_CLAUSE), 'dropped')
        for category in ('other', 'personal', 'email', 'work'):
            fake = FakeCalls({flow_llm.SYSTEM_PROMPT: DROPS_CLAUSE, flow_llm.AGENT_PROMPT: MINIMAL})
            out, info = self.run_clean(fake, category)
            self.assertEqual(out, MINIMAL, category)
            self.assertEqual(info['engine'], 'llm')
            self.assertEqual(info['retry'], 'ok')
            self.assertEqual([c['system'] for c in fake.calls], [flow_llm.SYSTEM_PROMPT, flow_llm.AGENT_PROMPT])

    def test_both_attempts_rejected_falls_back_to_rules(self):
        fake = FakeCalls({flow_llm.SYSTEM_PROMPT: DROPS_CLAUSE, flow_llm.AGENT_PROMPT: DROPS_CLAUSE})
        out, info = self.run_clean(fake, 'other')
        self.assertIsNone(out)
        self.assertEqual(info['engine'], 'rules')
        self.assertEqual(info['fallback'], 'guard:dropped')
        self.assertEqual(info['retry'], 'guard:dropped')

    def test_good_light_cleanup_needs_no_retry(self):
        # Fillers, a repeat and punctuation are fine edits in any app.
        said = 'um so I think we should uh repaint the the fence this weekend'
        fake = FakeCalls({flow_llm.SYSTEM_PROMPT: 'So I think we should repaint the fence this weekend.'})
        out, info = self.run_clean(fake, 'personal', text=said)
        self.assertEqual(out, 'So I think we should repaint the fence this weekend.')
        self.assertNotIn('retry', info)
        self.assertEqual(len(fake.calls), 1)

    def test_agent_apps_are_not_retried(self):
        fake = FakeCalls({flow_llm.AGENT_PROMPT: DROPS_CLAUSE})
        out, info = self.run_clean(fake, 'agent')
        self.assertIsNone(out)
        self.assertEqual(info['fallback'], 'guard:dropped')
        self.assertNotIn('retry', info)
        self.assertEqual(len(fake.calls), 1)

    def test_retry_stays_inside_the_time_budget(self):
        fake = FakeCalls({flow_llm.SYSTEM_PROMPT: DROPS_CLAUSE, flow_llm.AGENT_PROMPT: MINIMAL}, delay=0.1)
        out, info = self.run_clean(fake, 'other', cfg={'baseUrl': 'http://127.0.0.1:9/v1', 'timeoutMs': 3000})
        self.assertEqual(out, MINIMAL)
        # The retry gets only what is left of the same budget.
        self.assertLessEqual(fake.calls[1]['timeout'], 3.0 - 0.09)
        # Too little time left: no retry, rules-only text.
        slow = FakeCalls({flow_llm.SYSTEM_PROMPT: DROPS_CLAUSE, flow_llm.AGENT_PROMPT: MINIMAL}, delay=0.3)
        out, info = self.run_clean(slow, 'other', cfg={'baseUrl': 'http://127.0.0.1:9/v1', 'timeoutMs': 1000})
        self.assertIsNone(out)
        self.assertEqual(len(slow.calls), 1)
        self.assertEqual(info['fallback'], 'guard:dropped')

    def test_retry_error_keeps_rules_text(self):
        fake = FakeCalls({flow_llm.SYSTEM_PROMPT: DROPS_CLAUSE, flow_llm.AGENT_PROMPT: TimeoutError()})
        out, info = self.run_clean(fake, 'email')
        self.assertIsNone(out)
        self.assertEqual(info['fallback'], 'guard:dropped')
        self.assertEqual(info['retry'], 'TimeoutError')

    def test_first_call_error_is_unchanged(self):
        fake = FakeCalls({flow_llm.SYSTEM_PROMPT: ConnectionError()})
        out, info = self.run_clean(fake, 'other')
        self.assertIsNone(out)
        self.assertEqual(info['fallback'], 'ConnectionError')
        self.assertEqual(len(fake.calls), 1)


def write_wav(path, samples, rate=16000, channels=1, width=2):
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(array.array('h', samples).tobytes() if width == 2 else bytes(samples))


def tone(peak, seconds=1.0, rate=16000):
    return [round(peak * math.sin(2 * math.pi * 220 * i / rate)) for i in range(int(seconds * rate))]


class AsrRetryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self._real = asr.transcribe
        self.addCleanup(setattr, asr, 'transcribe', self._real)

    def wav(self, name, samples, **kw):
        path = Path(self.dir.name) / name
        write_wav(path, samples, **kw)
        return str(path)

    def fake_transcribe(self, replies):
        calls = []

        def transcribe(path, model, language, prompt):
            with wave.open(path, 'rb') as w:
                frames = array.array('h', w.readframes(w.getnframes()))
            calls.append({'path': path, 'peak': max(map(abs, frames)) if frames else 0,
                          'model': model, 'prompt': prompt})
            return replies[len(calls) - 1]
        asr.transcribe = transcribe
        return calls

    def test_louder_copy_doubles_and_clips(self):
        src = self.wav('a.wav', [0, 1000, -1000, 20000, -20000])
        out = asr.louder_copy(src)
        self.addCleanup(os.remove, out)
        with wave.open(out, 'rb') as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (1, 2, 16000))
            self.assertEqual(list(array.array('h', w.readframes(5))), [0, 1995, -1995, 32767, -32768])

    def test_no_retry_for_silence_or_very_quiet_audio(self):
        self.assertIsNone(asr.louder_copy(self.wav('zero.wav', [0] * 16000)))
        self.assertIsNone(asr.louder_copy(self.wav('quiet.wav', tone(300))))       # below -40 dBFS
        self.assertIsNone(asr.louder_copy(self.wav('empty.wav', [])))
        self.assertIsNotNone(asr.louder_copy(self.wav('ok.wav', tone(5000))))

    def test_no_retry_for_unexpected_formats_or_long_clips(self):
        self.assertIsNone(asr.louder_copy(self.wav('stereo.wav', tone(5000), channels=2)))
        self.assertIsNone(asr.louder_copy(self.wav('8bit.wav', [200] * 1000, width=1)))
        long_clip = self.wav('long.wav', tone(5000, seconds=asr.RETRY_MAX_SECONDS + 1))
        self.assertIsNone(asr.louder_copy(long_clip))

    def test_empty_transcript_with_sound_is_retried_louder(self):
        src = self.wav('speech.wav', tone(6000))
        calls = self.fake_transcribe(['', 'Water the tomatoes at six.'])
        self.assertEqual(asr.transcribe_with_retry(src, 'parakeet-v3-q8', None, 'Dana'),
                         'Water the tomatoes at six.')
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]['path'], src)
        self.assertGreater(calls[1]['peak'], calls[0]['peak'] * 1.9)
        self.assertEqual((calls[1]['model'], calls[1]['prompt']), ('parakeet-v3-q8', 'Dana'))
        self.assertFalse(os.path.exists(calls[1]['path']))  # the louder copy is removed

    def test_silent_clip_stays_empty_without_retry(self):
        calls = self.fake_transcribe([''])
        self.assertEqual(asr.transcribe_with_retry(self.wav('zero.wav', [0] * 32000), 'm', None, None), '')
        self.assertEqual(len(calls), 1)

    def test_text_is_never_retried(self):
        calls = self.fake_transcribe(['Hello there.'])
        self.assertEqual(asr.transcribe_with_retry(self.wav('s.wav', tone(6000)), 'm', None, None),
                         'Hello there.')
        self.assertEqual(len(calls), 1)

    def test_retry_that_finds_nothing_returns_empty(self):
        calls = self.fake_transcribe(['', '   '])
        self.assertEqual(asr.transcribe_with_retry(self.wav('n.wav', tone(6000)), 'm', None, None), '')
        self.assertEqual(len(calls), 2)
        self.assertFalse(os.path.exists(calls[1]['path']))


if __name__ == '__main__':
    unittest.main()
