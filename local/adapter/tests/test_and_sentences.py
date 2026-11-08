"""Tests for joining a sentence that starts with "And" (2026-11-08).

    python -I tests\\test_and_sentences.py

The model is never called. The fixtures are made-up sentences.
"""
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ADAPTER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADAPTER))

import flow_llm  # noqa: E402
import flow_text  # noqa: E402
import local_cleanup_shim as shim  # noqa: E402

join = flow_text.join_and_sentences


def words(text):
    return re.findall(r"[\w'’]+", text.lower())


class JoinTests(unittest.TestCase):
    def test_joined(self):
        self.assertEqual(join('We shipped the build. And it works.'), 'We shipped the build, and it works.')
        self.assertEqual(join('Paint the fence. And water the roses. And call Dana.'),
                         'Paint the fence, and water the roses, and call Dana.')

    def test_keeps_the_next_word_as_said(self):
        self.assertEqual(join('Call the shop. And Dana said yes.'), 'Call the shop, and Dana said yes.')

    def test_left_alone(self):
        for text in ('And it works.',                        # starts with it
                     'Did it work? And is it live?',         # a question stays a question
                     'It works! And it is fast.',
                     'Line one.\nAnd line two.',             # a new line was typed or said
                     'She said "we are done. And it works." today.',
                     'We met Dr. And then left.',            # abbreviations
                     'We met the Prof. And then left.',
                     'We called Acme Co. And then left.',
                     'Ask the U.S. And then Canada.',
                     'Fix the sidebar etc. And the footer.',
                     'We paid 5. And it works.',             # after a digit
                     'We shipped it. And.',                  # no clause after it
                     'We shipped it. Andrew called.',        # another word
                     'We shipped it. and it works.'):        # lower case: not a new sentence
            self.assertEqual(join(text), text, text)

    def test_only_punctuation_and_case_change(self):
        for text in ('We shipped the build. And it works. And the logs are clean.',
                     'One. And two.'):
            self.assertEqual(words(join(text)), words(text))

    def test_empty(self):
        self.assertEqual(join(''), '')


class PipelineTests(unittest.TestCase):
    SETTINGS = {'conservativeCleanup': True, 'applyStyle': True}
    TEXT = 'We shipped the build. And it works now.'

    def run_cleanup(self, settings=None, reply=None):
        s = {**self.SETTINGS, **(settings or {})}
        if reply is None:
            return shim.flow_cleanup(self.TEXT, ctx=None, category='agent', vocabulary=[], settings=s)
        s['llmCleanup'] = {'enabled': True, 'baseUrl': 'http://127.0.0.1:1/v1', 'minWords': 1}
        with patch.object(flow_llm, 'call_model', return_value=reply):
            return shim.flow_cleanup(self.TEXT, ctx=None, category='agent', vocabulary=[], settings=s)

    def test_on_by_default_and_reported(self):
        text, info = self.run_cleanup()
        self.assertEqual(text, 'We shipped the build, and it works now.')
        self.assertIn('and', info['fixes'])

    def test_can_be_turned_off(self):
        text, info = self.run_cleanup({'joinAndSentences': False})
        self.assertEqual(text, self.TEXT)
        self.assertNotIn('fixes', info)

    def test_applies_to_the_model_output_too(self):
        text, info = self.run_cleanup(reply='We shipped the build. And it works now.')
        self.assertEqual(info['engine'], 'llm')
        self.assertEqual(text, 'We shipped the build, and it works now.')


if __name__ == '__main__':
    unittest.main()
