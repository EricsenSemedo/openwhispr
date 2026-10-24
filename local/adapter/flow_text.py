"""Deterministic text fixes from the usage audit of 2026-10-14.

All three run on every dictation, short ones included, before and (for the
vocabulary) after the local model:

  * fix_vocabulary()    - your terms in their exact spelling: "t3 code" ->
                          "T3 Code", "Toon Sync" -> "ToonSync", "robux" -> "Robux".
  * numbers_to_digits() - "ninety-five percent" -> "95%", "PR one forty-two"
                          -> "PR 142", "sixty five K" -> "65K". Counting ("one, two,
                          three"), small amounts ("five seconds") and times stay words.
  * scratch_that()      - "Blue walls. Oh, scratch that." deletes "Blue walls.".
  * drop_pause_ellipses() - "I was thinking... Maybe we" -> "I was thinking maybe
                          we" (2026-10-23; also applied to the model's output).
"""
from __future__ import annotations

import re

from flow_style import _first_plain_word, common_words, lowerable

# ---------------------------------------------------------------- vocabulary

_TERM_WORD = re.compile(r"[\w'’]+")
_CAMEL = re.compile(r'(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])')


def _eligible(term: str) -> bool:
    """Multi-word terms are specific enough to fix anywhere. A single word is
    fixed only when it isn't an ordinary English word ("Blender" and "Dot"
    are left to the model, which sees the context)."""
    words = _TERM_WORD.findall(term)
    if not words or len(term) < 3:
        return False
    return len(words) >= 2 or term.lower() not in common_words()


def _pattern(term: str) -> str:
    # "ToonSync" also matches "Toon Sync" / "toon-sync"; "T3 Code" also "t3code".
    parts = [p for p in re.split(r'[\s\-]+', _CAMEL.sub(' ', term)) if p]
    return r'[\s\-]*'.join(re.escape(p) for p in parts)


def fix_vocabulary(text: str, terms: list[str]) -> str:
    """Whole-word, case-insensitive; the first spelling of a term wins."""
    seen, usable = set(), []
    for term in terms:
        key = re.sub(r'[\s\-]+', '', term.lower())
        if term and key not in seen and _eligible(term):
            seen.add(key)
            usable.append(term)
    if not usable or not text:
        return text
    usable.sort(key=len, reverse=True)
    table = {re.sub(r'[\s\-]+', '', t.lower()): t for t in usable}
    rx = re.compile(r"(?<![\w'’./@\\-])(" + '|'.join(_pattern(t) for t in usable)
                    + r")(?![\w\-]|[./@\\]\w)", re.IGNORECASE)
    return rx.sub(lambda m: table.get(re.sub(r'[\s\-]+', '', m.group(1).lower()), m.group(1)), text)


# ------------------------------------------------------------------- numbers

_UNITS = {'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7,
          'eight': 8, 'nine': 9}
_TEENS = {'ten': 10, 'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14, 'fifteen': 15,
          'sixteen': 16, 'seventeen': 17, 'eighteen': 18, 'nineteen': 19}
_TENS = {'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50, 'sixty': 60, 'seventy': 70,
         'eighty': 80, 'ninety': 90}
_SCALES = {'hundred': 100, 'thousand': 1000, 'million': 1000000, 'billion': 1000000000}
_ALL = {**_UNITS, **_TEENS, **_TENS, **_SCALES}
_WORD_ALT = '|'.join(sorted(_ALL, key=len, reverse=True))
_UNIT_ALT = '|'.join(k for k in _UNITS if k != 'zero')
# One number word; "ninety-five" is one token. "twenty-something", "one-on-one"
# and "hundreds" are not numbers here.
_NUM = rf"(?:{_WORD_ALT})(?:-(?:{_UNIT_ALT}))?(?![\w'’]|-\w)"
# "and" joins only after a scale word: "two hundred and fifty", not "thirty and seven".
_AND = r"(?:(?<=hundred)|(?<=thousand)|(?<=million)|(?<=billion))\s+and"
_RUN = re.compile(rf"(?<![\w'’\-])(?:a\s+)?{_NUM}(?:(?:{_AND})?\s+{_NUM})*", re.IGNORECASE)
_TOKEN = re.compile(rf"\b(?:and|a)\b|{_NUM}", re.IGNORECASE)
_POINT = re.compile(rf"\s+point\s+((?:{'|'.join(_UNITS)})(?:\s+(?:{'|'.join(_UNITS)}))*)(?![\w\-])",
                    re.IGNORECASE)
_ORDINAL_NEXT = re.compile(
    r"\s+(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|\w+th)\b", re.IGNORECASE)
# Words before a number that make it an identifier: "PR one forty-two".
_ID_BEFORE = {'pr', 'prs', 'issue', 'issues', 'ticket', 'port', 'version', 'build', '#'}
_PREV_WORD = re.compile(r"(#|[\w'’]+)\W{0,2}$")
_UNIT_AFTER = re.compile(
    r"\s*(percent\b|%|[kKmM]\b|dollars?\b|bucks\b|robux\b|gb\b|mb\b|tb\b|ms\b|px\b|fps\b)", re.IGNORECASE)


def _value(token: str) -> tuple[int, str]:
    token = token.lower()
    if '-' in token:
        tens, unit = token.split('-')
        return _TENS.get(tens, 0) + _UNITS[unit], 'tens'
    if token in _SCALES:
        return _SCALES[token], 'scale'
    if token in _TENS:
        return _TENS[token], 'tens'
    if token in _TEENS:
        return _TEENS[token], 'teen'
    return _UNITS[token], 'unit'


def _segments(tokens: list[str]) -> list[tuple[int, bool]]:
    """Split spoken number words into numbers: [(value, has_scale)].
    "two forty-nine" is two numbers (2, 49); "two hundred forty-nine" is one."""
    out, total, current, last, scaled = [], 0, 0, None, False
    for tok in tokens:
        if tok.lower() == 'and':
            continue
        value, kind = _value(tok)
        if kind == 'scale':
            if last in (None, 'scale') and current == 0:
                current = 1  # "hundred" alone: "a hundred"
            if value == 100:
                current *= 100
            else:
                total += current * value
                current = 0
            scaled, last = True, 'scale'
            continue
        ok = (last in (None, 'scale')
              or (last == 'tens' and kind == 'unit' and current % 10 == 0 and current % 100 != 0))
        if not ok:
            out.append((total + current, scaled))
            total, current, scaled = 0, 0, False
        current += value
        last = kind
    out.append((total + current, scaled))
    return out


def _fmt(n: int) -> str:
    return f'{n:,}' if n >= 1000 else str(n)


def numbers_to_digits(text: str) -> str:
    if not text:
        return text
    out, pos = [], 0
    for m in _RUN.finditer(text):
        tokens = _TOKEN.findall(m.group(0))
        lead_a = tokens[0].lower() == 'a'
        if lead_a:
            tokens = tokens[1:]
        if tokens and tokens[-1].lower() == 'and':
            continue
        end = m.end()
        before = text[:m.start()]
        prev = _PREV_WORD.search(before)
        prev = prev.group(1).lower() if prev else ''
        unit = _UNIT_AFTER.match(text, end)
        if _ORDINAL_NEXT.match(text, end):
            continue  # "twenty first"
        segs = _segments(tokens)
        ident = prev in _ID_BEFORE
        if len(segs) == 2 and (ident or unit) and 1 <= segs[0][0] <= 9 and 10 <= segs[1][0] <= 99 \
                and not segs[1][1] and not segs[0][1]:
            value = segs[0][0] * 100 + segs[1][0]  # "two seventy-five" -> 275
        elif len(segs) == 1:
            value, scaled = segs[0]
            starts_with_scale = _value(tokens[0])[1] == 'scale'
            if starts_with_scale and not (lead_a and unit):
                continue  # "a couple hundred", "a thousand times"
            if value < 10 and not (ident or unit):
                continue  # "one", "five seconds", "zero out of five"
        else:
            continue  # "one two three", "two thirty", "seven eleven"
        if lead_a and not _value(tokens[0])[1] == 'scale':
            start = m.start() + len(re.match(r'a\s+', m.group(0), re.IGNORECASE).group(0))
        else:
            start = m.start()
        digits = _fmt(value)
        point = _POINT.match(text, end)
        if point and not unit:
            digits += '.' + ''.join(str(_UNITS[w.lower()]) for w in point.group(1).split())
            end = point.end()
            unit = _UNIT_AFTER.match(text, end)
        if unit:
            word = unit.group(1).lower()
            if word in ('percent', '%'):
                digits, end = digits + '%', unit.end()
            elif word in ('k', 'm'):
                digits, end = digits + word.upper(), unit.end()
            elif word.startswith('dollar'):
                digits, end = '$' + digits, unit.end()
        out.append(text[pos:start])
        out.append(digits)
        pos = end
    if not out:
        return text
    out.append(text[pos:])
    return ''.join(out)


# ------------------------------------------------------------- scratch that

_SCRATCH = re.compile(r"\b(?:scratch|scrap)\s+(?:that|it)\b(?=\s*(?:[.!?,;:…—-]|$))", re.IGNORECASE)
# Words that may lead into the command: "Oh, wait, scratch that", "Actually scrap that".
_LEAD = re.compile(r"(?:(?:oh|ok|okay|no|nope|actually|wait|sorry|um|uh|hmm|please|yeah|so|nah)"
                   r"[\s,.!…]*)+$", re.IGNORECASE)
_SENT_END = re.compile(r"[.!?…]")


def _cap(text: str) -> str:
    m = re.match(r"(\s*[\"'“‘(]*)([a-z])", text)
    return text[:m.start(2)] + m.group(2).upper() + text[m.end(2):] if m else text


def _scratch_once(text: str) -> str | None:
    for m in _SCRATCH.finditer(text):
        head = text[:m.start()]
        lead = _LEAD.search(head)
        # Lead-in words count only as whole words ("oh", not the end of "Rio").
        cut = lead.start() if lead and (lead.start() == 0 or not head[lead.start() - 1].isalnum()) \
            else len(head)
        before = text[:cut].rstrip()
        if not re.search(r'\w', before) or before[-1] not in '.!?…,;:—-':
            continue  # nothing to delete, or "don't scratch it"
        after = text[m.end():].lstrip(' .!?,;:…—-')
        if before[-1] in '.!?…':
            # Starts a sentence: drop the previous sentence.
            body = before.rstrip('.!?… ')
            ends = list(_SENT_END.finditer(body))
            keep, joiner = (body[:ends[-1].end()], ' ') if ends else ('', '')
        else:
            # Mid-sentence: drop the clause back to the previous comma, or the
            # whole sentence when the command ends it ("and red, oh scratch it.").
            body = before.rstrip(',;:—- ')
            ends_sentence = re.match(r"\s*(?:[.!?…]|$)", text[m.end():]) is not None
            stops = list(re.finditer(r"[.!?…]" if ends_sentence else r"[.!?…,;:—]", body))
            if not stops:
                keep, joiner = '', ''
            elif body[stops[-1].start()] in '.!?…':
                keep, joiner = body[:stops[-1].end()], ' '
            else:
                keep, joiner = body[:stops[-1].start()], ', '
        if not keep and not after:
            return None  # everything would go; leave it for the user to see
        if not keep:
            return _cap(after)
        if not after:
            return keep if keep[-1] in '.!?…' else keep + '.'
        return keep + joiner + (_cap(after) if joiner == ' ' else after)
    return None


def scratch_that(text: str) -> str:
    """"scratch that" / "scratch it" / "scrap that", said as its own phrase,
    deletes what came before it: the previous sentence when the command starts
    a sentence ("Blue walls. Oh, scratch that."), otherwise the clause back to the
    previous comma ("the big one, scratch that, the red one"). With nothing
    before it in this dictation the text is left as it is (an earlier paste
    can't be undone). "Scratch that idea" and "don't scratch it" are not
    commands."""
    for _ in range(5):
        result = _scratch_once(text)
        if result is None:
            break
        text = result
    return text


# ------------------------------------------------------------ pause ellipses

# Speech recognisers (whisper.cpp on the phone in particular) type "..." where
# the speaker only paused. The speaker did not ask for that punctuation.
_ELLIPSIS = re.compile(r"[ \t]*(?:\.{3,}|…)[ \t]*")
_QUOTED = re.compile(r'"[^"\n]*"|“[^”\n]*”')
_SPACES = re.compile(r"(?<=\S)[ \t]{2,}(?=\S)")


def drop_pause_ellipses(text: str, style: str = 'formal', dictionary=frozenset()) -> str:
    """Pause ellipses go and double spaces collapse to one.

    * Mid-text: "thinking... Maybe we" -> "thinking maybe we". The next word is
      lower-cased only under the cursor casing rules (a common word; not I, a
      name or a dictionary term) and only when no sentence ended before it.
    * At the end of a line or of the text: "." in formal style, nothing in
      casual, and nothing after other punctuation.
    * At the start of a line or before other punctuation: removed.
    Quoted spans and number ranges ("1...5") are left alone. Run it after
    scratch_that(), which treats an ellipsis as a sentence end."""
    if not text or ('...' not in text and '…' not in text and '  ' not in text and '\t' not in text):
        return text
    quoted = [m.span() for m in _QUOTED.finditer(text)]
    out, pos = [], 0
    for m in _ELLIPSIS.finditer(text):
        a, b = m.span()
        if any(qa <= a < qb or qa < b <= qb for qa, qb in quoted):
            continue
        prev, nxt = text[:a], text[b:b + 1]
        if prev[-1:].isdigit() and nxt.isdigit():
            continue
        out.append(text[pos:a])
        pos = b
        if not nxt or nxt in '\r\n':
            tail = prev.rstrip()
            if style != 'casual' and tail and tail[-1] not in '.!?,;:':
                out.append('.')
            continue
        if not prev.strip(' \t') or prev[-1:] in '\r\n' or nxt in '.!?,;:)':
            continue
        out.append(' ')
        rest = text[b:]
        _, word = _first_plain_word(rest)
        if word and rest.startswith(word) and prev.rstrip()[-1:] not in '.!?' and lowerable(word, dictionary):
            out.append(word[0].lower())
            pos = b + 1
    out.append(text[pos:])
    return _SPACES.sub(' ', ''.join(out))
