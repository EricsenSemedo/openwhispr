"""Tests for the usage-audit fixes of 2026-10-14 (flow_text, agent mode).

    python -I tests\\test_audit_fixes.py

The fixtures are made-up sentences shaped like typical dictation problems.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ADAPTER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADAPTER))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import flow_llm  # noqa: E402
import flow_style  # noqa: E402
import local_cleanup_shim as shim  # noqa: E402
from flow_text import fix_vocabulary, numbers_to_digits, scratch_that  # noqa: E402
from test_flow import FakeModel  # noqa: E402

VOCAB = ['T3 Code', 'ToonSync', 'CogniFillz', 'Robux', 'AGENTS.md', 'ChatGPT', 'PCPartPicker',
         'Proton Pass', "Hero's Quest", 'Shure', 'Blender', 'GitHub', 'robux']


class VocabularyTests(unittest.TestCase):
    def test_terms_get_exact_spelling(self):
        self.assertEqual(fix_vocabulary('Also in T3 code can I open a second tab?', VOCAB),
                         'Also in T3 Code can I open a second tab?')
        self.assertEqual(fix_vocabulary('Does the demo use Toon Sync?', VOCAB), 'Does the demo use ToonSync?')
        self.assertEqual(fix_vocabulary('Dana likes robux. chat GPT and the shure mic', VOCAB),
                         'Dana likes Robux. ChatGPT and the Shure mic')
        self.assertEqual(fix_vocabulary('the pcpartpicker site and Proton pass', VOCAB),
                         'the PCPartPicker site and Proton Pass')

    def test_ordinary_words_links_and_paths_untouched(self):
        # "blender" and "github" are ordinary words: left to the model.
        self.assertEqual(fix_vocabulary('a blender on github', VOCAB), 'a blender on github')
        self.assertEqual(fix_vocabulary('example.github.io/portfolio', VOCAB),
                         'example.github.io/portfolio')
        self.assertEqual(fix_vocabulary('see docs/robux.md', VOCAB), 'see docs/robux.md')
        self.assertEqual(fix_vocabulary('Dana edited the agents.md.', VOCAB), 'Dana edited the AGENTS.md.')


class NumberTests(unittest.TestCase):
    def test_numbers_become_digits(self):
        self.assertEqual(numbers_to_digits("the forecast says ninety percent or ninety-five percent rain"),
                         "the forecast says 90% or 95% rain")
        self.assertEqual(numbers_to_digits('a thirty percent chance of a sixty five K race or maybe fifty two thousand steps'),
                         'a 30% chance of a 65K race or maybe 52,000 steps')
        self.assertEqual(numbers_to_digits('In PR three eighty-four the docs look fine'), 'In PR 384 the docs look fine')
        self.assertEqual(numbers_to_digits('a hat for three twenty-nine Robux or ninety-nine Robux'),
                         'a hat for 329 Robux or 99 Robux')
        self.assertEqual(numbers_to_digits('Seventy nine sounds fine.'), '79 sounds fine.')
        self.assertEqual(numbers_to_digits('a garden shed for sixty-five thousand dollars'), 'a garden shed for $65,000')
        self.assertEqual(numbers_to_digits('two hundred and fifty, version two point five, a hundred percent'),
                         '250, version 2.5, 100%')

    def test_words_kept(self):
        for text in ('Count along, kids: one, two, three.',
                     'the recipe got zero out of five stars',
                     'let the pan cool for like five seconds',
                     'at two thirty and seven eleven', 'a couple hundred times',
                     'one of the twenty first users', 'one-on-one, twenty-something, hundreds of them',
                     'And Dana picked door number one.', 'Dana enjoys the one free refill per day.'):
            self.assertEqual(numbers_to_digits(text), text)


class ScratchThatTests(unittest.TestCase):
    def test_deletes_what_came_before(self):
        self.assertEqual(scratch_that("Hey, how's the garden? Big tomatoes. Oh, okay scratch that."),
                         "Hey, how's the garden?")
        self.assertEqual(scratch_that('Testing, testing, whoa, hey, oh scratch it. Dana likes pancakes.'),
                         'Dana likes pancakes.')
        self.assertEqual(scratch_that('The soup is too salty maybe. Actually scrap that. Don\'t even add anything.'),
                         "Don't even add anything.")
        self.assertEqual(scratch_that('I want the blue one, the big one, scratch that, the red one.'),
                         'I want the blue one, the red one.')
        self.assertEqual(scratch_that('We should ship today. No wait, scratch that. Ship tomorrow.'),
                         'Ship tomorrow.')

    def test_not_a_command(self):
        for text in ("There's a scuff on the car, don't scratch it.", 'Scratch that idea, use Postgres.',
                     'Scratch that.', 'Big tomatoes. Scratch that.', 'I had to scratch that itch.'):
            self.assertEqual(scratch_that(text), text)


class AgentGuardTests(unittest.TestCase):
    def test_agent_apps(self):
        for exe in ('T3 Code (Nightly).exe', 'claude.exe', 'Code.exe', 'WindowsTerminal.exe', 'Cursor.exe'):
            self.assertEqual(flow_style.app_category(exe, ''), 'agent')
        self.assertEqual(flow_style.app_category('zen.exe', 'Claude — Zen Browser'), 'agent')
        self.assertIs(flow_llm.prompt_for('agent'), flow_llm.AGENT_PROMPT)
        self.assertIs(flow_llm.prompt_for('other'), flow_llm.SYSTEM_PROMPT)

    def test_strict_rejects_the_audit_meaning_changes(self):
        # A six-word clause dropped: "if the lights are turned down".
        self.assertEqual(flow_llm.strict_reason(
            'Dana says the renders look sharper if the lights are turned down by using Blender.',
            'Dana says the renders look sharper by using Blender.'), 'dropped')
        # A hedge reworded: "something like that" became "in roughly the same way".
        self.assertIn(flow_llm.strict_reason(
            'so that the class can have several cooks something like that, maybe a shared kitchen',
            'so that the class can have several cooks in roughly the same way, maybe a shared kitchen'),
            ('dropped', 'added'))
        self.assertEqual(flow_llm.strict_reason('we should ship the update today and check the logs',
                                                'we should ship the big new update today and check all logs'),
                         'added')
        # Numbers changed: a time split up, a currency sign added.
        self.assertEqual(flow_llm.strict_reason('I fed the cat at 615am thinking', 'I fed the cat at 6:15 AM, thinking'),
                         'digits')
        self.assertEqual(flow_llm.strict_reason('Is 275 enough for lunch', 'Is $275 enough for lunch?'), 'digits')

    def test_strict_allows_punctuation_and_spelling(self):
        self.assertIsNone(flow_llm.strict_reason(
            'Dana says we can buy the big bag of 5kg and honestly it look it looks like the drip hoses watering system works',
            'Dana says we can buy the big bag of 5kg. And honestly, it looks it looks like the drip hose watering system works.'))
        self.assertIsNone(flow_llm.strict_reason('so Dana thinks they are they\'re ripe', "So Dana thinks they're they're ripe."))
        self.assertIsNone(flow_llm.strict_reason('either model the vase in Bundary or download one',
                                                 'Either model the vase in Blender or download one.', {'Blender'}))

    def test_vocab_swap(self):
        self.assertEqual(flow_llm.vocab_swap('a tiny crack in tile B7.', 'a tiny crack in tile T3 Code.',
                                             {'T3 Code'}, []), 'vocab-swap')
        self.assertEqual(flow_llm.vocab_swap('A quick garden pass.', "A quick FUTO's pass.", {'FUTO'}, []),
                         'vocab-swap')
        self.assertIsNone(flow_llm.vocab_swap("Dana likes the food's keyboard", "Dana likes the FUTO's keyboard",
                                              {'FUTO'}, []))
        self.assertIsNone(flow_llm.vocab_swap('paid with robots?', 'paid with Robux?', {'Robux'}, [('robots', 'Robux')]))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.model = FakeModel()
        self.addCleanup(self.model.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        shim.OPTIONS = Path(self.tmp.name) / 'local-dictation-options.json'
        (Path(self.tmp.name) / 'local-vocabulary.json').write_text(
            json.dumps({'words': ['Robux', 'T3 Code']}), encoding='utf-8')
        self.settings = {'conservativeCleanup': True, 'applyStyle': True, 'scratchThat': True,
                         'numbersAsDigits': True,
                         'llmCleanup': {'enabled': True, 'baseUrl': self.model.url, 'timeoutMs': 800,
                                        'maxTimeoutMs': 900}}

    def test_short_dictation_gets_rule_fixes(self):
        out, info = shim.flow_cleanup('uh ninety percent robux', ctx=None, category='agent',
                                      vocabulary=shim.merge_terms(shim.local_vocabulary(), ['robux']),
                                      settings=self.settings)
        self.assertEqual(out, '90% Robux')
        self.assertEqual(info.get('skipped'), 'short')
        self.assertEqual(info['fixes'], ['vocabulary', 'digits'])
        self.assertEqual(self.model.requests, [])

    def test_agent_uses_agent_prompt_and_strict_guard(self):
        self.model.reply = 'Dana says the renders look sharper by using Blender.'
        out, info = shim.flow_cleanup('Dana says the renders look sharper if the lights are turned down by '
                                      'using Blender', ctx={'exe': 'T3 Code (Nightly).exe'}, category='agent',
                                      vocabulary=[], settings=self.settings)
        self.assertEqual(self.model.requests[0]['messages'][0]['content'], flow_llm.AGENT_PROMPT)
        self.assertEqual(info['fallback'], 'guard:dropped')
        self.assertIn('if the lights are turned down', out)

    def test_model_output_gets_vocabulary_spelling(self):
        self.model.reply = 'Also in T3 code, can I open a second tab?'
        out, _ = shim.flow_cleanup('also in T3 code can I open a second tab', ctx=None, category='agent',
                                   vocabulary=shim.local_vocabulary(), settings=self.settings)
        self.assertEqual(out, 'Also in T3 Code, can I open a second tab?')

    def test_off_switches(self):
        self.settings.update(scratchThat=False, numbersAsDigits=False)
        self.settings['llmCleanup']['enabled'] = False
        out, _ = shim.flow_cleanup('Big tomatoes. Oh scratch that. ninety percent', ctx=None, category='other',
                                   vocabulary=[], settings=self.settings)
        self.assertEqual(out, 'Big tomatoes. Oh scratch that. ninety percent.')

    def test_dictionary_payload_includes_local_vocabulary(self):
        self.assertEqual(shim.dictionary_payload()['words'][:2], ['Robux', 'T3 Code'])


if __name__ == '__main__':
    unittest.main(verbosity=1)
