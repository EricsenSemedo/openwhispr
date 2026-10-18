"""Tests for the Wispr-style flow modules and their adapter wiring.

    python -I tests\\test_flow.py
"""
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ADAPTER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADAPTER))

import flow_context  # noqa: E402
import flow_learn  # noqa: E402
import flow_llm  # noqa: E402
import flow_style  # noqa: E402
import local_cleanup_shim as shim  # noqa: E402


class FakeModel:
    """Minimal OpenAI-style server: replies with `reply`, optionally slowly."""

    def __init__(self):
        self.reply, self.delay, self.requests = '', 0.0, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.requests.append(body)
                time.sleep(outer.delay)
                data = json.dumps({'choices': [{'message': {'content': outer.reply}}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}/v1'

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class StyleTests(unittest.TestCase):
    def test_app_category(self):
        self.assertEqual(flow_style.app_category('Discord.exe', '#general'), 'personal')
        self.assertEqual(flow_style.app_category('zen.exe', 'Inbox - Gmail — Zen Browser'), 'email')
        self.assertEqual(flow_style.app_category('slack.exe', ''), 'work')
        self.assertEqual(flow_style.app_category('T3 Code (Nightly).exe', 'Gmail draft'), 'agent')
        self.assertEqual(flow_style.app_category('notepad.exe', 'Gmail draft'), 'other')
        self.assertEqual(flow_style.app_category(None, None), 'other')

    def test_styles(self):
        self.assertEqual(flow_style.apply_style('Hey, are you free at 3.', 'casual'), 'Hey are you free at 3')
        self.assertEqual(flow_style.apply_style('Wait...', 'casual'), 'Wait...')
        self.assertEqual(flow_style.apply_style('Are you free?', 'casual'), 'Are you free?')
        self.assertEqual(flow_style.apply_style('Ship the update today', 'formal'), 'Ship the update today.')
        self.assertEqual(flow_style.apply_style('Ship it', 'formal'), 'Ship it')
        self.assertEqual(flow_style.apply_style('Ship it today!', 'formal'), 'Ship it today!')
        self.assertEqual(flow_style.apply_style('example.github.io/portfolio', 'formal'),
                         'example.github.io/portfolio')
        self.assertEqual(flow_style.apply_style('npm install the deps', 'formal'), 'npm install the deps.')
        self.assertEqual(flow_style.apply_style('so see example.com/docs', 'formal'), 'So see example.com/docs')

    def test_cursor_sentence_start_and_unknown(self):
        adj = flow_style.cursor_adjust
        self.assertEqual(adj('then we ship.', None, None), 'then we ship.')
        self.assertEqual(adj('then we ship.', '', ''), 'Then we ship.')
        self.assertEqual(adj('then we ship.', 'Done. ', ''), 'Then we ship.')
        self.assertEqual(adj('Then we ship.', 'Done.', ''), ' Then we ship.')
        # Lower-case text glued to "word." is a continued token (github. + com).
        self.assertEqual(adj('com', 'github.', ''), 'com')
        self.assertEqual(adj('then we ship.', 'Line one\n', ''), 'Then we ship.')
        self.assertEqual(adj('then we ship.', 'He said "go." ', ''), 'Then we ship.')

    def test_cursor_mid_sentence(self):
        adj = flow_style.cursor_adjust
        self.assertEqual(adj('Then we ship.', 'If tests pass, ', ''), 'then we ship.')
        self.assertEqual(adj('Then we ship.', 'If tests pass,', ''), ' then we ship.')
        self.assertEqual(adj("I'm in.", 'and ', ''), "I'm in.")
        self.assertEqual(adj('Roblox map.', 'the ', ''), 'Roblox map.')
        self.assertEqual(adj('Tailscale works.', 'so ', '', {'Tailscale'}), 'Tailscale works.')
        self.assertEqual(adj('Then we ship.', 'word', ' and more'), ' then we ship')
        self.assertEqual(adj('Then we ship.', 'word ', ' And more'), 'then we ship.')
        self.assertEqual(adj('Then.', '(', ''), 'Then.')  # only after a letter or comma
        self.assertEqual(adj('The build passed.', '- ', ''), 'The build passed.')
        self.assertEqual(adj('example.github.io/portfolio', '', ''), 'example.github.io/portfolio')
        self.assertEqual(adj('iPhone works.', '', ''), 'iPhone works.')
        self.assertEqual(adj('Monday works.', 'and ', ''), 'Monday works.')

    def test_replacements(self):
        pairs = [('TealScale', 'Tailscale'), ('context the MDP', 'context.md')]
        self.assertEqual(flow_style.apply_replacements('set up tealscale and read context the MDP', pairs),
                         'set up Tailscale and read context.md')
        self.assertEqual(flow_style.apply_replacements('TealScales', pairs), 'TealScales')


class LearnTests(unittest.TestCase):
    def test_review_rules(self):
        asked = []

        def model(system, user):
            asked.append(user)
            return 'no'
        accepted, rejected = flow_learn.review([
            {'from': 'TealScale', 'to': 'Tailscale'},
            {'from': 'sent', 'to': 'Send'},
            {'from': 'PR 274', 'to': 'PR274'},
            {'from': 'thing', 'to': 'thing'},
            {'from': 'a', 'to': 'b'},
        ], 'set up TealScale and sent it', model)
        self.assertEqual([a['to'] for a in accepted], ['Tailscale'])
        self.assertEqual({r['why'] for r in rejected}, {'model-declined', 'digits', 'unchanged', 'over-cap'})
        self.assertEqual(len(asked), 1)

    def test_review_without_model_learns_only_rare_words(self):
        def down(system, user):
            raise ConnectionError
        accepted, rejected = flow_learn.review([{'from': 'Astro', 'to': 'Astra'}, {'from': 'fiber', 'to': 'Fiverr'}],
                                               'x', down)
        self.assertEqual([a['to'] for a in accepted], ['Fiverr'])
        self.assertTrue(rejected[0]['why'].startswith('model-unavailable'))

    def test_direct_vs_hint(self):
        direct, hints = flow_learn.split_pairs([
            {'from': 'TealScale', 'to': 'Tailscale'}, {'from': 'roadblocks', 'to': 'Roblox'},
            {'from': 'context the MDP', 'to': 'context.md'}, {'from': 'B2B', 'to': 'BtB'}])
        self.assertEqual(direct, [('TealScale', 'Tailscale'), ('context the MDP', 'context.md')])
        self.assertEqual(hints, [('roadblocks', 'Roblox'), ('B2B', 'BtB')])

    def test_budget_stops_model_questions(self):
        def slow(system, user):
            time.sleep(0.3)
            return 'yes'
        accepted, rejected = flow_learn.review([{'from': 'sent', 'to': 'Send'}, {'from': 'brake', 'to': 'break'}],
                                               'x', slow, budget=0.1)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(rejected[0]['why'], 'budget')

    def test_forget_only_drops_auto_learned(self):
        with tempfile.TemporaryDirectory() as tmp:
            flow_learn.remember(Path(tmp), [{'from': 'Astro', 'to': 'Astra'}], 'wispr-import')
            flow_learn.remember(Path(tmp), [{'from': 'TealScale', 'to': 'Tailscale'}], 'auto-learn')
            self.assertEqual(flow_learn.forget(Path(tmp), ['tailscale', 'Astra']), 1)
            self.assertEqual([p['to'] for p in flow_learn.load_pairs(Path(tmp))], ['Astra'])

    def test_store_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            flow_learn.remember(Path(tmp), [{'from': 'TealScale', 'to': 'Tailscale'}], 'test')
            flow_learn.remember(Path(tmp), [{'from': 'tealscale', 'to': 'tailscale'}], 'test')
            self.assertEqual(len(flow_learn.load_pairs(Path(tmp))), 1)


class GuardTests(unittest.TestCase):
    def test_drift(self):
        src = "so we should set up teal scale on the pc and then connect it to the laptop"
        self.assertIsNone(flow_llm.drift_reason(src, 'So we should set up Tailscale on the PC and then connect it to the laptop.', {'Tailscale'}))
        self.assertEqual(flow_llm.drift_reason('what is the weather in Boston today', 'It is sunny and 70 degrees in Boston today with light wind.'), 'longer')
        self.assertEqual(flow_llm.drift_reason('a b c d e f g h i j', 'a b c'), 'shorter')
        self.assertEqual(flow_llm.drift_reason('deploy it there and make sure it works',
                                               'ship it now and verify it functions'), 'reworded')
        self.assertEqual(flow_llm.drift_reason('we should ship the update today',
                                               'The update should ship today, we think.'), 'rewritten')
        # Answers, negation flips (reviewer cases) are rejected...
        self.assertEqual(flow_llm.drift_reason('what is the capital of France', 'Paris is the capital of France.'),
                         'answered')
        self.assertEqual(flow_llm.drift_reason('I do not want to go there today', 'I do want to go there today.'),
                         'negation')
        # ...while self-corrections, joined words and real questions pass.
        for src, out in [("Let's meet at 2, no wait, at 3 in the main room.", "Let's meet at 3 in the main room."),
                         ('meet at 2 no wait at 3', 'Meet at 3.'),
                         ('cloud flare and tail scale', 'Cloudflare and Tailscale'),
                         ('what we need is more tests', 'What we need is more tests.'),
                         ('can you check what time it closes', 'Can you check what time it closes?')]:
            self.assertIsNone(flow_llm.drift_reason(src, out, {'Tailscale'}), out)
        self.assertEqual(flow_llm.drift_reason('x', 'Sure, here is the cleaned text: x'), 'preamble')
        self.assertEqual(flow_llm.strip_wrapping('<transcript>\nHi there\n</transcript>'), 'Hi there')

    def test_prompt_contains_context_not_in_system(self):
        msg = flow_llm.build_user_message('hi', {'exe': 'Discord.exe', 'title': '#general', 'before': 'Hey '},
                                          'casual', 'personal', ['Roblox'], [('roadblocks', 'Roblox')])
        self.assertIn('App: Discord (personal messaging)', msg)
        self.assertIn('Text before the cursor: "Hey "', msg)
        self.assertIn('"roadblocks" may mean "Roblox"', msg)
        self.assertNotIn('Discord', flow_llm.SYSTEM_PROMPT)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.model = FakeModel()
        self.addCleanup(self.model.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        shim.OPTIONS = Path(self.tmp.name) / 'local-dictation-options.json'
        self.settings = {'conservativeCleanup': True, 'applyStyle': True,
                         'llmCleanup': {'enabled': True, 'baseUrl': self.model.url, 'timeoutMs': 800,
                                        'maxTimeoutMs': 900}}

    def run_cleanup(self, text, category='other', ctx=None):
        return shim.flow_cleanup(text, ctx=ctx, category=category, vocabulary=[], settings=self.settings)

    def test_model_output_used_and_styled(self):
        self.model.reply = 'Hey, can we bake at 3 for the picnic?'
        out, info = self.run_cleanup('um hey can we bake at 2 no at 3 for the picnic', 'personal')
        self.assertEqual(out, 'Hey can we bake at 3 for the picnic?')
        self.assertEqual((info['engine'], info['style']), ('llm', 'casual'))

    def test_answering_model_falls_back_to_rules(self):
        self.model.reply = 'The weather in Boston is sunny today with a high of seventy degrees and light wind.'
        out, info = self.run_cleanup('um what is the weather in Boston today')
        self.assertEqual(out, 'What is the weather in Boston today.')
        self.assertEqual(info['fallback'], 'guard:longer')

    def test_slow_model_falls_back(self):
        self.model.delay, self.model.reply = 1.5, 'late'
        t0 = time.monotonic()
        out, info = self.run_cleanup('we should ship the update today for sure')
        self.assertLess(time.monotonic() - t0, 1.4)
        self.assertEqual(info['engine'], 'rules')
        self.assertEqual(out, 'We should ship the update today for sure.')

    def test_short_and_protected_skip_model(self):
        out, info = self.run_cleanup('ship it')
        self.assertEqual(info.get('skipped'), 'short')
        out, info = self.run_cleanup('set the value to x = 5 in the config file')
        self.assertEqual(info.get('skipped'), 'protected')
        self.assertEqual(self.model.requests, [])

    def test_learned_pairs_applied_and_hinted(self):
        flow_learn.remember(Path(self.tmp.name), [{'from': 'TealScale', 'to': 'Tailscale'},
                                                  {'from': 'roadblocks', 'to': 'Roblox'}], 'test')
        self.model.reply = 'Set up Tailscale for the Roblox map.'
        self.run_cleanup('set up tealscale for the roadblocks map')
        user = self.model.requests[0]['messages'][1]['content']
        self.assertIn('set up Tailscale for the roadblocks map', user)
        self.assertIn('"roadblocks" may mean "Roblox"', user)

    def test_completion_envelope_and_disabled_model(self):
        self.settings['llmCleanup']['enabled'] = False
        body = {'model': shim.MODEL, 'messages': [
            {'role': 'system', 'content': 'Clean.\n\n' + shim.DICTIONARY_MARKER + 'Tailscale, my LinkedIn'},
            {'role': 'user', 'content': shim.APP_TRANSCRIPT_PREFIX + 'um ship the update today'
             + shim.APP_TRANSCRIPT_SUFFIX}]}
        result = shim.cleanup_completion(body, settings=self.settings)
        self.assertEqual(result['choices'][0]['message']['content'], 'Ship the update today.')
        self.assertEqual(shim._last_vocabulary, ['Tailscale', 'my LinkedIn'])


class ContextCacheTests(unittest.TestCase):
    def setUp(self):
        self.real = flow_context.capture
        self.addCleanup(setattr, flow_context, 'capture', self.real)

    def test_unfinished_or_superseded_capture_is_never_returned(self):
        flow_context.capture = lambda timeout=0.4: {'hwnd': 1, 'before': 'old', 'at': time.monotonic()}
        cache = flow_context.ContextCache()
        cache.start_capture()
        self.assertEqual(cache.latest(wait=1)['before'], 'old')

        def slow(timeout=0.4):
            time.sleep(0.5)
            return {'hwnd': 1, 'before': 'new', 'at': time.monotonic()}
        flow_context.capture = slow
        cache.start_capture()
        self.assertIsNone(cache.latest(wait=0.05))  # still running: no stale 'old'
        self.assertEqual(cache.latest(wait=1)['before'], 'new')

    def test_take_once_and_only_after_last_paste(self):
        flow_context.capture = lambda timeout=0.4: {'hwnd': 7, 'before': 'x', 'at': time.monotonic()}
        cache = flow_context.ContextCache()
        cache.start_capture()
        cache.latest(wait=1)
        self.assertIsNone(cache.take(8, 0.0))                 # other window
        self.assertIsNone(cache.take(7, time.monotonic()))    # taken before the last paste
        self.assertIsNotNone(cache.take(7, 0.0))
        self.assertIsNone(cache.take(7, 0.0))                 # used once


class LocalHostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        shim.OPTIONS = Path(self.tmp.name) / 'local-dictation-options.json'
        shim.OPTIONS.write_text(json.dumps({'conservativeCleanup': True}))
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), shim.LocalCleanupShim)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def request(self, path, body=None, host=None):
        import http.client
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        headers = {'Host': host or f'127.0.0.1:{self.port}'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        conn.request('POST' if body is not None else 'GET', path,
                     body=json.dumps(body) if body is not None else None, headers=headers)
        r = conn.getresponse()
        return r.status, json.loads(r.read() or b'{}')

    def test_rebinding_host_is_refused(self):
        self.assertEqual(self.request('/v1/flow/dictionary', host='evil.example:18765')[0], 403)
        self.assertEqual(self.request('/v1/flow/dictionary')[0], 200)

    def test_learn_reviews_without_storing_then_remember_stores(self):
        status, out = self.request('/v1/flow/learn', {'pairs': [{'from': 'TealScale', 'to': 'Tailscale'}],
                                                      'sentence': 'set up TealScale'})
        self.assertEqual((status, [a['to'] for a in out['accepted']]), (200, ['Tailscale']))
        self.assertEqual(flow_learn.load_pairs(Path(self.tmp.name)), [])
        self.assertEqual(self.request('/v1/flow/remember', {'pairs': [{'from': 'TealScale', 'to': 'Tailscale'}]})[0],
                         200)
        self.assertEqual(len(flow_learn.load_pairs(Path(self.tmp.name))), 1)
        self.assertEqual(self.request('/v1/flow/forget', {'words': ['Tailscale']})[1], {'removed': 1})


class PhoneLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        shim.OPTIONS = Path(self.tmp.name) / 'local-dictation-options.json'
        shim.OPTIONS.write_text(json.dumps({'conservativeCleanup': True, 'applyStyle': True,
                                            'phoneLink': {'enabled': True, 'owner': 'me@github'}}))
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), shim.PhoneLinkHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'

    def post(self, path, body, login):
        headers = {'Content-Type': 'application/json'}
        if login:
            headers['Tailscale-User-Login'] = login
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_host_and_origin_checks(self):
        opts = json.loads(shim.OPTIONS.read_text())
        opts['phoneLink']['hosts'] = ['desktop.example.ts.net:8443']
        shim.OPTIONS.write_text(json.dumps(opts))
        body = {'text': 'hello there my friend'}
        self.assertEqual(self.post('/v1/cleanup', body, 'me@github')[0], 403)  # Host is 127.0.0.1:port
        opts['phoneLink']['hosts'] = []
        opts['phoneLink']['enabled'] = False
        shim.OPTIONS.write_text(json.dumps(opts))
        self.assertEqual(self.post('/v1/cleanup', body, 'me@github')[0], 404)

    def test_bad_bodies(self):
        self.assertEqual(self.post('/v1/cleanup', ['not', 'an', 'object'], 'me@github')[0], 400)

    def test_owner_only(self):
        body = {'schemaVersion': 1, 'text': 'hey are you free at 3.', 'app': {'package': 'com.whatsapp',
                                                                              'category': 'personal'}}
        self.assertEqual(self.post('/v1/cleanup', body, None)[0], 401)
        self.assertEqual(self.post('/v1/cleanup', body, 'someone@else')[0], 401)
        status, out = self.post('/openwhispr-link/v1/cleanup', body, 'me@github')
        self.assertEqual(status, 200)
        self.assertEqual((out['text'], out['style'], out['engine']), ('Hey are you free at 3', 'casual', 'rules'))


if __name__ == '__main__':
    unittest.main(verbosity=1)
