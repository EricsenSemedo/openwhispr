"""Wispr-style text rules shared by the PC adapter and (as a spec) the phone.

Everything here is deterministic and fast:
  * app_category()   - personal / work / email / agent / other, from the target app
  * apply_style()    - formal or casual punctuation (the user's Wispr styles)
  * cursor_adjust()  - casing and spacing that fit the text around the cursor
  * apply_replacements() - learned "misheard -> correct" pairs

The phone app implements the same rules in StyleRules.java / CursorText.java;
keep the two in step (see FLOW-PARITY.md).
"""
from __future__ import annotations

import re
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / 'data'

# Copied from the user's Wispr Flow settings (2026-10-12): cleanup level
# "light"; personal messages casual; work, email and everything else formal.
DEFAULT_STYLES = {'personal': 'casual', 'work': 'formal', 'email': 'formal', 'agent': 'formal',
                  'other': 'formal'}
DEFAULT_LEVEL = 'light'
# Usage audit 2026-10-14: about 80% of dictation goes to coding agents. There
# the model only fixes punctuation and spelling and never rewords (flow_llm).

# Lower-case executable names (without .exe) and window-title fragments.
APP_RULES = {
    'personal': {
        'exes': {'beeper', 'whatsapp', 'discord', 'telegram', 'signal', 'messenger', 'instagram'},
        'titles': ('whatsapp', 'messenger', 'instagram', 'discord', 'beeper', 'telegram', 'signal',
                   'google messages'),
    },
    'email': {
        'exes': {'outlook', 'olk', 'proton mail', 'thunderbird', 'superhuman'},
        'titles': ('gmail', 'proton mail', 'outlook', 'superhuman'),
    },
    'work': {
        'exes': {'slack', 'ms-teams', 'teams'},
        'titles': ('slack', 'microsoft teams'),
    },
    'agent': {
        'exes': {'t3 code (nightly)', 't3 code', 't3code', 'claude', 'codex', 'chatgpt', 'code',
                 'code - insiders', 'cursor', 'windsurf', 'zed', 'kiro', 'antigravity',
                 'windowsterminal', 'wezterm-gui', 'alacritty', 'warp', 'ghostty', 'mintty',
                 'cmd', 'powershell', 'pwsh', 'conhost', 'openconsole'},
        'titles': ('claude', 'chatgpt', 'codex', 't3 code'),
    },
}
BROWSERS = {'zen', 'firefox', 'chrome', 'msedge', 'brave', 'opera', 'vivaldi', 'arc'}

_common: set[str] | None = None


def common_words() -> set[str]:
    global _common
    if _common is None:
        try:
            _common = set((DATA_DIR / 'common-en-30k.txt').read_text(encoding='utf-8').split())
        except OSError:
            _common = set()
    return _common


def app_category(exe: str | None, title: str | None) -> str:
    exe = (exe or '').lower()
    if exe.endswith('.exe'):
        exe = exe[:-4]
    title = (title or '').lower()
    for category, rule in APP_RULES.items():
        if exe in rule['exes']:
            return category
    if exe in BROWSERS or not exe:
        for category, rule in APP_RULES.items():
            if any(t in title for t in rule['titles']):
                return category
    return 'other'


_GREETING_COMMA = re.compile(r"^(Hey|Hi|Hello|Yo|Thanks|Thank you|Okay|OK|So|Yeah|Yes|No|Well),\s+")
_WORD = re.compile(r"[\w'’]+")


# A plain first word: letters only (no capitals inside), followed by a space,
# the end, or sentence punctuation - never by "." + letters, "/", "@" or ":".
# URLs, file names, snippets and brands (iPhone, npm, github.io) never match.
_FIRST_WORD = re.compile(r"""^(\s*["'“‘(\[]*)([A-Za-z][a-z'’]*)(?=$|[\s,;!?)\]"'”’]|\.(?:\s|$))""")
_URLISH = re.compile(r"\w[./@:]\w")


def _first_plain_word(text: str):
    m = _FIRST_WORD.match(text)
    return (len(m.group(1)), m.group(2)) if m else (None, None)


def apply_style(text: str, style: str) -> str:
    """Both styles start with a capital (Wispr: "Caps + ..."). formal: a
    sentence-length message ends with punctuation. casual: no full stop at
    the very end, no comma after a greeting. Casing for the cursor position
    is decided later, at paste time (cursor_adjust). Only plain common words
    are capitalised, and text ending in a link or address gets no period."""
    if not text:
        return text
    pos, word = _first_plain_word(text)
    if word and word[0].islower() and word.lower() in common_words():
        text = text[:pos] + word[0].upper() + text[pos + 1:]
    if style == 'casual':
        if '\n' not in text and text.endswith('.') and not text.endswith('..'):
            text = text[:-1]
        text = _GREETING_COMMA.sub(lambda m: m.group(1) + ' ', text, count=1)
    elif style == 'formal':
        words = text.split()
        if (len(_WORD.findall(text)) >= 3 and text[-1].isalnum()
                and not (words and _URLISH.search(words[-1]))):
            text += '.'
    return text


_CLOSERS = ')"\'”’]'
_I_FORMS = {"I", "I'm", "I'll", "I've", "I'd", "I’m", "I’ll", "I’ve", "I’d"}
# Common words that are usually names; never lower-cased (same list as the
# phone's CursorText.PROPER).
_PROPER = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
           "january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december",
           "english", "spanish", "french", "german", "chinese", "japanese", "american", "america",
           "christmas", "easter", "god", "google", "android", "windows", "linux", "mac"}
_SPACE_AFTER = set('.,!?;:…)]}”’')


def lowerable(word: str, dictionary=frozenset()) -> bool:
    """A Titlecase word that may be lower-cased mid-sentence: a common
    English word, not I/I'm/..., not a day, month or similar name, and not
    one of the user's dictionary terms."""
    return (bool(re.fullmatch(r"[A-Z][a-z'’]+", word)) and word not in _I_FORMS
            and word.lower() not in _PROPER and word.lower() in common_words() and word not in dictionary)


def cursor_adjust(text: str, before: str | None, after: str | None,
                  dictionary: set[str] | frozenset[str] = frozenset()) -> str:
    """Fit pasted text to the text around the cursor (None = unknown)."""
    if not text or before is None:
        return text
    # Continuing a token ("github." + "com", "me@" + "host"): leave it alone.
    if len(before) > 1 and before[-1] in '.@/:' and before[-2].isalnum() and text[:1].islower():
        return text
    pb = before.rstrip(' \t')
    tail = pb.rstrip(_CLOSERS)
    sentence_start = not pb or pb.endswith('\n') or (tail[-1:] in ('.', '!', '?', '…'))
    pos, word = _first_plain_word(text)
    if word:
        if sentence_start:
            if word[0].islower() and word.lower() in common_words():
                text = text[:pos] + word[0].upper() + text[pos + 1:]
        elif (pb[-1:].isalnum() or pb[-1:] == ',') and lowerable(word, dictionary):
            text = text[:pos] + word[0].lower() + text[pos + 1:]
    if not sentence_start and after is not None:
        nxt = after.lstrip(' \t')
        if (text.endswith('.') and not text.endswith('..') and nxt
                and (nxt[0].islower() or nxt[0].isdigit())):
            text = text[:-1]
    if before and not before[-1].isspace() and not text[0].isspace():
        if before[-1].isalnum() or before[-1] in _SPACE_AFTER:
            text = ' ' + text
    return text


def apply_replacements(text: str, pairs: list[tuple[str, str]]) -> str:
    """Learned "misheard -> correct" pairs: whole words, case-insensitive,
    longest first, one pass (Wispr's dictionary replacement)."""
    usable = sorted(((f, t) for f, t in pairs if f and t and f.lower() != t.lower()),
                    key=lambda p: len(p[0]), reverse=True)
    if not usable:
        return text
    table = {f.lower(): t for f, t in usable}
    pattern = re.compile(r"(?<![\w'’])(" + '|'.join(re.escape(f) for f, _ in usable) + r")(?![\w'’])",
                         re.IGNORECASE)
    return pattern.sub(lambda m: table[m.group(1).lower()], text)
