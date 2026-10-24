"""Tests for the fixes of 2026-10-23 (benchmark work in work\\llm-bench-20261023).

    python -I tests\\test_fixes_20261023.py

  * added-word guard (flow_llm.added_words) on the light and agent paths;
  * prompt rule against added words and a leading "So", casual "?" kept;
  * stable prompt parts first, and warm() caching the same prefix;
  * auto-learn junk filter (flow_learn.review);
  * PC filler rules on phone-link requests;
  * pause ellipses removed (flow_text.drop_pause_ellipses), model output too;
  * ISO date in adapter log lines and size-based rotation of adapter.log.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

ADAPTER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADAPTER))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import flow_learn  # noqa: E402
import flow_llm  # noqa: E402
import local_cleanup_shim as shim  # noqa: E402
from flow_text import drop_pause_ellipses  # noqa: E402
from test_flow import FakeModel  # noqa: E402

VOCAB = {'Cloudflare', 'T3 Code', 'Tailscale', 'OpenWhispr', 'Syncthing'}

# (source given to the model, model output) that must be REJECTED
PHANTOM = [
    ('I think we should ship the update today.', 'So I think we should ship the update today.'),
    ('I think we should get pizza tonight', 'So I think we should get pizza tonight'),
    ('the build is broken so we should roll back', 'So the build is broken, so we should roll back.'),
    ('we can review the numbers on Friday', 'Okay, we can review the numbers on Friday.'),
    ('I finished the report.', 'Well, I finished the report.'),
    ('it works on my machine', 'And it works on my machine.'),
    ('send me the file when you can', 'So, send me the file when you can.'),
    ('the header should stay fixed', 'So the header should stay fixed.'),
    ('call me when you land', 'Just call me when you land.'),
    ('can you fix the login page', 'Can you please fix the login page?'),
]
# (source, output) that must stay ACCEPTED: legitimate light edits
GOOD = [
    ('So I think we should ship the the update today', 'So I think we should ship the update today.'),
    ("We're you putting the recipes on cloud flare so they're not on visible to anyone.",
     "We're putting the recipes on Cloudflare so they're not visible to anyone."),
    ("Let's meet at 2, no wait, at 3 in the main room.", "Let's meet at 3 in the main room."),
    ('can you check what time the store closes', 'Can you check what time the store closes?'),
    ('push the changes to tail scale and check cloud flare', 'Push the changes to Tailscale and check Cloudflare.'),
    ('my friend uses sync thing on his phone', 'My friend uses Syncthing on his phone.'),
    ('I do not think it is ready', "I don't think it's ready."),
    ('okay so I looked at the PR and the main issue is the cache key',
     'Okay, so I looked at the PR, and the main issue is the cache key.'),
    ('the new design is like way cleaner you know', 'The new design is way cleaner.'),
    ('we need to fix login page before demo', 'We need to fix the login page before the demo.'),
    ('it gets above eighty percent', 'It gets above 80 percent.'),
    ('open the t3 code settings', 'Open the T3 Code settings.'),
    ('their going to be late', "They're going to be late."),
]


class AddedWordGuardTests(unittest.TestCase):
    def test_phantom_words_rejected_on_both_paths(self):
        for s, o in PHANTOM:
            with self.subTest(o=o):
                self.assertEqual(flow_llm.drift_reason(s, o, VOCAB), 'added')
                self.assertIn(flow_llm.strict_reason(s, o, VOCAB), ('added', 'dropped'))
                self.assertTrue(flow_llm.added_words(s, o, VOCAB))

    def test_legit_light_edits_kept(self):
        for s, o in GOOD:
            with self.subTest(o=o):
                self.assertEqual(flow_llm.added_words(s, o, VOCAB), [])
                self.assertIsNone(flow_llm.drift_reason(s, o, VOCAB))

    def test_article_never_allowed_first(self):
        self.assertEqual(flow_llm.added_words('build failed again', 'The build failed again.'), ['the'])
        self.assertEqual(flow_llm.added_words('fix login page', 'Fix the login page.'), [])

    def test_pipeline_falls_back_on_added_word(self):
        model = FakeModel()
        self.addCleanup(model.close)
        settings = {'conservativeCleanup': True, 'applyStyle': True,
                    'llmCleanup': {'enabled': True, 'baseUrl': model.url, 'timeoutMs': 800, 'maxTimeoutMs': 900}}
        with tempfile.TemporaryDirectory() as tmp:
            shim.OPTIONS = Path(tmp) / 'local-dictation-options.json'
            for category in ('other', 'agent'):
                model.reply = 'So I think we should ship the update today.'
                out, info = shim.flow_cleanup('um I think we should ship the update today', ctx=None,
                                              category=category, vocabulary=[], settings=settings)
                self.assertEqual(out, 'I think we should ship the update today.')
                self.assertEqual(info['fallback'], 'guard:added')


class PromptTests(unittest.TestCase):
    def test_rule_and_examples(self):
        self.assertIn('Never add a word the speaker did not say', flow_llm.SYSTEM_PROMPT)
        self.assertNotIn('um so I think', flow_llm.SYSTEM_PROMPT)
        self.assertNotIn('\nSo I think', flow_llm.SYSTEM_PROMPT)
        self.assertIn('keep question marks', flow_llm.STYLE_TEXT['casual'])
        self.assertEqual(flow_llm.prompt_for('agent'), flow_llm.AGENT_PROMPT)

    def test_stable_parts_first_and_warm_prefix_matches(self):
        vocab, hints = ['Roblox', 'Tailscale'], [('roadblocks', 'Roblox')]
        ctx = {'exe': 'Discord.exe', 'title': '#general', 'before': 'Hey '}
        real = flow_llm.build_user_message('see you at 3', ctx, 'formal', 'other', vocab, hints)
        lines = real.split('\n')
        self.assertTrue(lines[1].startswith('Vocabulary'))
        self.assertTrue(lines[2].startswith('Often misheard'))
        self.assertTrue(lines[3].startswith('Style'))
        self.assertTrue(lines[4].startswith('App: Discord'))
        warm = flow_llm.build_user_message('ok', None, 'formal', 'other', vocab, hints)
        stable = '\n'.join(lines[:4]) + '\n'
        self.assertTrue(real.startswith(stable) and warm.startswith(stable))

    def test_warm_sends_vocabulary_and_hints(self):
        model = FakeModel()
        self.addCleanup(model.close)
        with tempfile.TemporaryDirectory() as tmp:
            shim.OPTIONS = Path(tmp) / 'local-dictation-options.json'
            flow_learn.remember(Path(tmp), [{'from': 'roadblocks', 'to': 'Roblox'}], 'test')
            shim._last_vocabulary = ['Tailscale']
            vocabulary, hints = shim.warm_prefix()
            self.assertEqual((vocabulary, hints), (['Tailscale', 'Roblox'], [('roadblocks', 'Roblox')]))
            flow_llm.warm({'baseUrl': model.url}, 'other', vocabulary, hints, 'formal')
            user = model.requests[0]['messages'][1]['content']
            self.assertIn('Vocabulary (exact spellings): Tailscale, Roblox', user)
            self.assertIn('"roadblocks" may mean "Roblox"', user)
            self.assertEqual(model.requests[0]['max_tokens'], 1)
            # flow_cleanup builds the same vocabulary for the real request.
            model.reply = 'Set up Tailscale on the new laptop today.'
            settings = {'conservativeCleanup': True,
                        'llmCleanup': {'enabled': True, 'baseUrl': model.url, 'timeoutMs': 800}}
            shim.flow_cleanup('set up tailscale on the new laptop today', ctx=None, category='other',
                              vocabulary=['Tailscale'], settings=settings)
            real = model.requests[1]['messages'][1]['content']
            self.assertEqual(real.split('\nStyle')[0], user.split('\nStyle')[0])
        shim._last_vocabulary = []


class LearnFilterTests(unittest.TestCase):
    def review(self, pairs, sentence='x', answer='yes'):
        asked = []

        def model(system, user):
            asked.append(user)
            if answer is None:
                raise ConnectionError
            return answer
        accepted, rejected = flow_learn.review(pairs, sentence, model)
        return accepted, rejected, asked

    def test_function_word_glued_to_a_neighbour_rejected(self):
        accepted, rejected, asked = self.review([{'from': 'the', 'to': 'boxthe'}],
                                                'open the red boxthe lid')
        self.assertEqual(accepted, [])
        self.assertEqual(rejected[0]['why'], 'common-source')
        self.assertEqual(asked, [])

    def test_joined_word(self):
        self.assertTrue(flow_learn.is_joined('the', 'boxthe'))
        self.assertTrue(flow_learn.is_joined('flare', 'Cloudflare'))
        self.assertFalse(flow_learn.is_joined('TealScale', 'Tailscale'))
        self.assertFalse(flow_learn.is_joined('robot', 'robots'))  # rest too short
        accepted, rejected, asked = self.review([{'from': 'flare', 'to': 'Cloudflare'}])
        self.assertEqual((accepted, rejected[0]['why'], asked), ([], 'joined', []))

    def test_common_sources(self):
        for src in ('the', 'and', 'a', 'which', 'time'):
            with self.subTest(src=src):
                _, rejected, asked = self.review([{'from': src, 'to': 'Tailscale'}])
                self.assertEqual(rejected[0]['why'], 'common-source')
        # Less frequent words are still learned.
        accepted, _, _ = self.review([{'from': 'fiber', 'to': 'Fiverr'}, {'from': 'Jessa', 'to': 'Jezza'}])
        self.assertEqual([a['why'] for a in accepted], ['rare-word', 'rare-word'])

    def test_stored_pairs_are_never_filtered(self):
        """The filter only reviews new candidates. Pairs already in the store
        (even ones it would now reject) are loaded, synced and applied."""
        stored = [{'from': 'the', 'to': 'boxthe', 'source': 'auto-learn'},
                  {'from': 'Gavrel', 'to': 'QZNT', 'source': 'auto-learn'},
                  {'from': 'flare', 'to': 'Cloudflare', 'source': 'auto-learn'}]
        with tempfile.TemporaryDirectory() as tmp:
            flow_learn.save_pairs(Path(tmp), stored)
            self.assertEqual(flow_learn.load_pairs(Path(tmp)), stored)
            direct, hints = flow_learn.split_pairs(flow_learn.load_pairs(Path(tmp)))
            self.assertEqual(len(direct) + len(hints), 3)
            shim.OPTIONS = Path(tmp) / 'local-dictation-options.json'
            self.assertEqual([p['to'] for p in shim.dictionary_payload()['replacements']],
                             ['boxthe', 'QZNT', 'Cloudflare'])
            out, _ = shim.flow_cleanup('ask Gavrel about it today', ctx=None, category='other', vocabulary=[],
                                       settings={'conservativeCleanup': True})
            self.assertEqual(out, 'ask QZNT about it today')

    def test_low_similarity_needs_the_model(self):
        accepted, _, asked = self.review([{'from': 'Gavrel', 'to': 'QZNT'}], answer='yes')
        self.assertEqual((accepted[0]['why'], len(asked)), ('model-approved', 1))
        accepted, rejected, _ = self.review([{'from': 'Gavrel', 'to': 'QZNT'}], answer='no')
        self.assertEqual((accepted, rejected[0]['why']), ([], 'model-declined'))
        accepted, rejected, _ = self.review([{'from': 'Gavrel', 'to': 'QZNT'}], answer=None)
        self.assertTrue(rejected[0]['why'].startswith('model-unavailable'))
        # Similar rare words need no model, as before.
        accepted, _, asked = self.review([{'from': 'TealScale', 'to': 'Tailscale'}], answer=None)
        self.assertEqual((accepted[0]['why'], asked), ('rare-word', []))


class EllipsisTests(unittest.TestCase):
    def test_rules(self):
        cases = [
            ('I was thinking... Maybe we could go', 'formal', 'I was thinking maybe we could go'),
            ('I was thinking...maybe we could go', 'formal', 'I was thinking maybe we could go'),
            ('So… I think we should', 'formal', 'So I think we should'),
            ('Talk to… Monday works', 'formal', 'Talk to Monday works'),
            ('we need... Blender for this', 'formal', 'we need Blender for this'),
            ("I don't know...", 'formal', "I don't know."),
            ("I don't know...", 'casual', "I don't know"),
            ('are you there?...', 'formal', 'are you there?'),
            ('...so anyway we left', 'formal', 'so anyway we left'),
            ('wait..., okay', 'formal', 'wait, okay'),
            ('Done. ... Next we go', 'formal', 'Done. Next we go'),
            ('one  two   three', 'formal', 'one two three'),
            ('line one...\nline two', 'formal', 'line one.\nline two'),
            ('pages 1...5', 'formal', 'pages 1...5'),
            ('He said "wait... what" to me', 'formal', 'He said "wait... what" to me'),
            ('no dots here.', 'formal', 'no dots here.'),
        ]
        for text, style, expected in cases:
            with self.subTest(text=text, style=style):
                self.assertEqual(drop_pause_ellipses(text, style, {'Blender'}), expected)

    def test_pipeline_rules_and_model_output(self):
        model = FakeModel()
        self.addCleanup(model.close)
        settings = {'conservativeCleanup': True, 'applyStyle': True, 'scratchThat': True,
                    'llmCleanup': {'enabled': True, 'baseUrl': model.url, 'timeoutMs': 800, 'maxTimeoutMs': 900}}
        with tempfile.TemporaryDirectory() as tmp:
            shim.OPTIONS = Path(tmp) / 'local-dictation-options.json'
            model.reply = 'I was thinking maybe we could... go tomorrow...'
            out, info = shim.flow_cleanup('um I was thinking... Maybe we could go tomorrow...', ctx=None,
                                          category='other', vocabulary=[], settings=settings)
            sent = model.requests[-1]['messages'][1]['content']
            self.assertIn('\nI was thinking maybe we could go tomorrow.\n', sent)
            self.assertEqual(out, 'I was thinking maybe we could go tomorrow.')
            self.assertIn('ellipsis', info['fixes'])
            model.reply = 'Hey are you free later...'
            out, _ = shim.flow_cleanup('hey are you free later', ctx=None, category='personal',
                                       vocabulary=[], settings=settings)
            self.assertEqual(out, 'Hey are you free later')
            # "scratch that" still sees the sentence end before it.
            settings['llmCleanup']['enabled'] = False
            out, _ = shim.flow_cleanup('Send it to Mark... Oh, scratch that. Send it to Ann.', ctx=None,
                                       category='other', vocabulary=[], settings=settings)
            self.assertEqual(out, 'Send it to Ann.')


class PhoneRulesTests(unittest.TestCase):
    def test_pc_filler_rules_run_on_phone_requests(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        shim.OPTIONS = Path(tmp.name) / 'local-dictation-options.json'
        shim.OPTIONS.write_text(json.dumps({'conservativeCleanup': True, 'applyStyle': True,
                                            'removeRepeatedFunctionWords': True,
                                            'phoneLink': {'enabled': True, 'owner': 'me@github'}}))
        server = ThreadingHTTPServer(('127.0.0.1', 0), shim.PhoneLinkHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        body = {'text': 'um can you check the uh build logs... I think the the deploy failed',
                'app': {'package': 'com.termux', 'category': 'agent'}}
        req = urllib.request.Request(f'http://127.0.0.1:{server.server_address[1]}/v1/cleanup',
                                     data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'Tailscale-User-Login': 'me@github'})
        with urllib.request.urlopen(req, timeout=5) as r:
            out = json.load(r)
        self.assertEqual(out['text'], 'Can you check the build logs I think the deploy failed.')
        self.assertEqual(out['engine'], 'rules')


CHILD = textwrap.dedent('''
    import sys
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    import local_cleanup_shim as shim
    shim.OPTIONS = Path(sys.argv[2]) / 'local-dictation-options.json'
    shim.LOG_MAX_BYTES = 400
    for i in range(60):
        shim.log('test', i=i)
        if i == 57:
            print('stderr line', file=sys.stderr, flush=True)
''')


class LogTests(unittest.TestCase):
    def test_iso_timestamp(self):
        line = []
        orig = shim.print if hasattr(shim, 'print') else None
        shim.print = lambda s, **kw: line.append(s)
        try:
            shim.log('probe', a=1)
        finally:
            if orig is None:
                del shim.print
        entry = json.loads(line[0])
        self.assertRegex(entry['t'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$')
        self.assertLess(abs((datetime.fromisoformat(entry['t']) - datetime.now().astimezone()).total_seconds()), 5)

    def test_rotation_like_the_supervisor_launch(self):
        """The supervisor opens adapter.log ("ab", unbuffered) and passes it as
        stdout and stderr. Rotation must keep every recent line, write no
        NUL gap after the truncation, and keep at most LOG_KEEP copies."""
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / 'local-asr-logs'
            logs.mkdir()
            (logs / 'adapter.log').write_bytes(b'old line\n' * 10)
            script = Path(tmp) / 'child.py'
            script.write_text(CHILD, encoding='utf-8')
            with open(logs / 'adapter.log', 'ab', buffering=0) as output:
                child = subprocess.Popen([sys.executable, '-I', '-X', 'utf8', '-u', str(script), str(ADAPTER), tmp],
                                         stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(child.wait(timeout=60), 0)
            names = sorted(p.name for p in logs.iterdir())
            self.assertEqual(names, ['adapter.log', 'adapter.log.1', 'adapter.log.2', 'adapter.log.3'])
            seen = []
            for name in ('adapter.log.3', 'adapter.log.2', 'adapter.log.1', 'adapter.log'):
                data = (logs / name).read_bytes()
                self.assertNotIn(b'\x00', data, name)
                for raw in data.decode('utf-8').splitlines():
                    if raw.startswith('{'):
                        seen.append(json.loads(raw)['i'])
            self.assertLessEqual((logs / 'adapter.log').stat().st_size, 400 + 200)
            # The newest lines are all there, in order, without gaps.
            self.assertEqual(seen, list(range(seen[0], 60)))
            self.assertIn(b'stderr line', b''.join((logs / n).read_bytes() for n in names))

    def test_rotation_ignores_other_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            shim.OPTIONS = Path(tmp) / 'local-dictation-options.json'
            other = Path(tmp) / 'local-asr-logs' / 'adapter.log'
            other.parent.mkdir()
            other.write_bytes(b'x' * 10)
            old = shim.LOG_MAX_BYTES
            shim.LOG_MAX_BYTES = 0
            try:
                self.assertFalse(shim.rotate_own_log())  # stdout here is not that file
            finally:
                shim.LOG_MAX_BYTES = old
            self.assertEqual(other.read_bytes(), b'x' * 10)


class SweepTests(unittest.TestCase):
    def test_model_call_ignores_proxies(self):
        model = FakeModel()
        self.addCleanup(model.close)
        model.reply = 'fine'
        saved = {k: os.environ.get(k) for k in ('HTTP_PROXY', 'http_proxy', 'NO_PROXY', 'no_proxy')}
        os.environ['HTTP_PROXY'] = os.environ['http_proxy'] = 'http://127.0.0.1:9'  # nothing listens
        for k in ('NO_PROXY', 'no_proxy'):
            os.environ.pop(k, None)
        try:
            self.assertEqual(flow_llm.call_model(model.url, 'm', 's', 'u', 5, 3), 'fine')
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_phone_dictionary_must_be_a_list(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        shim.OPTIONS = Path(tmp.name) / 'local-dictation-options.json'
        shim.OPTIONS.write_text(json.dumps({'conservativeCleanup': True, 'applyStyle': True,
                                            'phoneLink': {'enabled': True, 'owner': 'me@github'}}))
        seen = []
        orig = shim.flow_cleanup
        shim.flow_cleanup = lambda text, **kw: (seen.append(kw['vocabulary']), orig(text, **kw))[1]
        self.addCleanup(setattr, shim, 'flow_cleanup', orig)
        server = ThreadingHTTPServer(('127.0.0.1', 0), shim.PhoneLinkHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for dictionary, expected in (('abc', []), (['Roblox', 3], ['Roblox'])):
            req = urllib.request.Request(
                f'http://127.0.0.1:{server.server_address[1]}/v1/cleanup',
                data=json.dumps({'text': 'see you at the game', 'dictionary': dictionary}).encode(),
                headers={'Content-Type': 'application/json', 'Tailscale-User-Login': 'me@github'})
            with urllib.request.urlopen(req, timeout=5) as r:
                self.assertEqual(r.status, 200)
            self.assertEqual(seen[-1], expected)

    def test_bad_phone_port_does_not_stop_the_adapter(self):
        for port in ('x', None, 99999):
            shim.start_phone_link({'phoneLink': {'enabled': True, 'owner': 'me@github', 'port': port}})


if __name__ == '__main__':
    unittest.main(verbosity=1)
