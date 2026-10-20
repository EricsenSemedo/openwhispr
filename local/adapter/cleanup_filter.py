"""Deterministic dictation cleanup (served as conservative-filler-filter-v1).

RECONSTRUCTED 2026-10-08 after the original source was lost with the Temp
staging folder; see tests/history_parity.py for the Oct 3-6 live outputs it
was rebuilt from.

REVISED 2026-10-12 (filter rules v2). v1 kept most real fillers: Parakeet
writes sentence-initial fillers capitalised and without a comma ('Um I
think'), which v1 treated as a possible name; any line containing words such
as 'code', 'say' or 'saying' skipped cleanup for the whole line; and a lone
apostrophe ('em, students') skipped the whole dictation. The model id stays
the same so OpenWhispr's cleanup settings need no change.

Rules (all deterministic; never a language model):
  * Removes hesitation fillers anywhere in a line: um, umm, uh, uhh, uhm,
    erm, er, hmm (any length). Lowercase and Capitalised forms are fillers,
    as are ALL-CAPS forms of 3+ letters ('ERM'); 'UM'/'UH'/'ER' may be
    acronyms and stay. Plurals ('ums') and
    hyphenated forms ('uh-huh', 'um-hum') stay.
  * Never removes 'like', 'so', 'you know', 'ah', 'oh' or other real words.
  * Punctuation left behind is tidied: a comma or ellipsis attached to the
    filler goes with it, and commas wrapped around a mid-clause filler both
    go ('we should, uh, ship' -> 'we should ship') unless they follow an
    introductory word or a list ('So, um, I' -> 'So, I'),
    'I think um.' -> 'I think.', a standalone 'Um.' sentence disappears, and
    'Um, so I think' -> 'So I think' (a capitalised filler starting a
    sentence passes its capital to the next word).
  * Talk about fillers keeps them: a filler right after a wording word ('say
    um'), any filler in a sentence containing word(s), filler(s), spell...,
    letters, verbatim or phrase, and a filler joined to a kept one ('say um
    or uh'). 'uh oh', 'uh huh', 'um hum', 'mm hmm' stay, and a capitalised
    'Um' mid-sentence without a comma is treated as a name.
  * Optional: collapses an immediately repeated function word ('I I',
    'the the'). Grammatical or emphatic repetition ('that that', 'had had',
    'very very', 'or or'), names, and numbers are left alone.
  * Protected, byte-exact spans: fenced code blocks (complete fences; an
    unterminated fence protects the rest of the text), inline `code`,
    "double", curly and 'single' quoted spans. Cleanup never edits inside
    or across them. An unbalanced double quote or backtick protects the
    whole text; apostrophes ('em, students') do not.
  * A line with code-like symbols ({ } [ ] < > \\ = | _) is left unchanged.
  * Any configured protected term present anywhere disables mutation.
  * If cleanup would leave no letters or digits, the original is returned.
"""
from __future__ import annotations

import re
from typing import Iterable

__all__ = ["clean_text", "FUNCTION_WORDS"]

# Filler candidates (case-insensitive; casing is checked in code). The
# lookarounds keep fillers that are part of a word, contraction or hyphenated
# form (sodium, y'uh, uh-huh, um-hum), the plural 'ums', and URL, path or
# address parts (site.com/um, um@host, um.txt). 'er' is exact so
# the verb 'err' stays.
_FILLER_RE = re.compile(
    r"(?<![\w'’\-/@#\\.])(?:u+m+|u+h+m*|e+r+m+|er|h+m{2,})(?![\w'’\-/@]|\.\w)",
    re.IGNORECASE,
)
# Punctuation that belongs to the filler ('um,' 'uh...' 'erm;').
_FILLER_TAIL_RE = re.compile(r"(?:\.\.\.|…|[,;])?[ \t]*")
_TERMINAL = ".!?"
# The filler directly follows a word about wording: keep it ('say um').
_CUE_WORDS = frozenset({
    "say", "says", "said", "saying", "word", "words", "spell", "spelled",
    "spelling", "spelt", "called", "named", "type", "typed", "typing", "write",
    "writes", "wrote", "writing", "filler", "fillers", "literal", "literally",
    "verbatim", "pronounce", "pronounced", "letters", "quote", "quoted",
})
# Only a space may separate them: "say, um, I don't know" is still a filler.
# A sentence that talks about fillers keeps them all ('Words like um and uh').
_MENTION_RE = re.compile(
    r"\b(?:word|words|filler|fillers|spell|spelled|spelling|letters|verbatim|phrase)\b",
    re.IGNORECASE)
# 'uh oh', 'uh huh', 'um hum', 'mm hmm' carry meaning.
_INTERJECTION_NEXT_RE = re.compile(r"[ \t]+(?:oh|huh|hum|hmm|hm)(?![\w'’\-])", re.IGNORECASE)
_INTERJECTION_PREV_RE = re.compile(r"(?<![\w'’\-])(?:mm+|mhm|uh|um)[ \t]+$", re.IGNORECASE)
# 'um or uh', 'um, uh and er': a filler joined to a kept one is a mention too.
_JOINED_RE = re.compile(r"[ \t]*(?:,[ \t]*)?(?:(?:or|and)[ \t]+)?$", re.IGNORECASE)
# 'every um and uh': two fillers joined by and/or are being named, not said.
_PAIR_NEXT_RE = re.compile(
    r"[ \t]*,?[ \t]*(?:or|and)[ \t]+(?:u+m+|u+h+m*|e+r+m+|er|h+m{2,})(?![\w'’\-])", re.IGNORECASE)
_SENTENCE_RE = re.compile(r"[^.!?]*(?:[.!?]+|$)")

# Introductory words that keep their comma: 'So, um, I' -> 'So, I'.
_INTRO_WORDS = frozenset({
    "so", "yeah", "yes", "yep", "no", "well", "okay", "ok", "right", "alright",
    "anyway", "actually", "honestly", "basically", "oh", "hey", "sure", "now",
    "also", "first", "look", "listen",
})
_PREV_WORD_RE = re.compile(r"([A-Za-z]+)[ \t]+$")

# Repeated words that are almost never intended when spoken twice in a row.
FUNCTION_WORDS = frozenset({
    "a", "an", "the", "i", "to", "of", "in", "on", "at", "for", "with",
    "from", "by", "my", "your",
})
_REPEAT_RE = re.compile(r"(?<![\w'’\-])([A-Za-z]+)([ \t]+)(\1)(?![\w'’\-])", re.IGNORECASE)

# Symbols that only show up in dictated code; such a line is left alone.
_CODE_CHARS = set("{}[]<>\\=|_")
_TRANSPORT_RE = re.compile(r"</?transcript>", re.IGNORECASE)

# Protected spans, tried in order at each position.
_SPAN_RES = (
    re.compile(r"```.*?```", re.DOTALL),
    re.compile(r"`[^`\n]+`"),
    re.compile(r'"[^"\n]*"'),
    re.compile("“[^“”\n]*”"),
    re.compile("‘[^‘’\n]*’(?![\\w])"),
    re.compile(r"(?<![\w'])'[^'\n]+'(?![\w'])"),
)
_UNTERMINATED_FENCE = re.compile(r"```")
# Private-use delimiters: never produced by ASR and not word characters.
_PLACEHOLDER = "\ue000{}\ue001"
_PLACEHOLDER_RE = re.compile("\ue000(\\d+)\ue001")


def _mask_spans(text: str) -> tuple[str, list[str]] | None:
    """Replace protected spans by opaque placeholders.

    Returns None when the text has an unbalanced quote/backtick, in which case
    the caller must leave the whole text unchanged.
    """
    spans: list[str] = []
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        matched = None
        for rx in _SPAN_RES:
            m = rx.match(text, i)
            if m:
                matched = m
                break
        if matched is None and text.startswith("```", i):
            # Unterminated fence: everything after it is code.
            matched_text = text[i:]
            out.append(_PLACEHOLDER.format(len(spans)))
            spans.append(matched_text)
            i = n
            break
        if matched is not None:
            out.append(_PLACEHOLDER.format(len(spans)))
            spans.append(matched.group(0))
            i = matched.end()
            continue
        out.append(text[i])
        i += 1
    masked = "".join(out)
    # Leftover quote characters mean we could not pair them safely. Straight
    # and curly apostrophes are left alone: they are usually contractions,
    # possessives or elisions ('em, students'), not quotes.
    stripped = _PLACEHOLDER_RE.sub("", masked)
    if any(ch in stripped for ch in ('"', "`", "“", "”", "‘")):
        return None
    return masked, spans


def _unmask(text: str, spans: list[str]) -> str:
    return _PLACEHOLDER_RE.sub(lambda m: spans[int(m.group(1))], text)


def _line_has_code(line: str) -> bool:
    return any(ch in _CODE_CHARS for ch in line)


def _is_filler_token(token: str) -> bool:
    # Two-letter ALL-CAPS ('UM', 'UH', 'ER') may be an acronym; longer ALL-CAPS
    # forms are how Parakeet sometimes writes a filler ('ERM, let's'). Odd
    # mixed case ('uMm') stays.
    if token.isupper():
        return len(token) >= 3
    return token.islower() or (token[0].isupper() and token[1:].islower())


def _after_cue_word(before: str) -> bool:
    m = _PREV_WORD_RE.search(before)
    return bool(m) and m.group(1).lower() in _CUE_WORDS


def _capitalize_first_word(text: str) -> str:
    m = re.match(r"([^\w\ue000]*)([^\W\d_][\w'’]*)", text)
    if not m or not m.group(2).replace("'", "").replace("’", "").isalpha() or not m.group(2).islower():
        return text  # starts with a digit, placeholder, name or mixed-case word
    i = m.start(2)
    if len(text[i].upper()) != 1:
        return text  # 'ß' would become 'SS'
    return text[:i] + text[i].upper() + text[i + 1:]


def _mention_spans(line: str) -> list[tuple[int, int]]:
    return [m.span() for m in _SENTENCE_RE.finditer(line) if m.group(0) and _MENTION_RE.search(m.group(0))]


def _remove_fillers(line: str) -> str:
    out = ""
    pos = 0
    capitalize = False
    mentions = _mention_spans(line)
    kept_end = -1
    for m in _FILLER_RE.finditer(line):
        if m.start() < pos:
            continue
        token = m.group(0)
        before = line[:m.start()]
        prev = (out + line[pos:m.start()]).rstrip(" \t")
        sentence_start = not prev or prev[-1] in _TERMINAL
        keep = (
            not _is_filler_token(token)
            or _after_cue_word(before)
            or any(a <= m.start() < b for a, b in mentions)
            or (kept_end >= 0 and _JOINED_RE.fullmatch(line, kept_end, m.start()) is not None)
            or _INTERJECTION_NEXT_RE.match(line, m.end()) is not None
            or _PAIR_NEXT_RE.match(line, m.end()) is not None
            or (token.lower().startswith("h") and _INTERJECTION_PREV_RE.search(before) is not None)
            # A capitalised 'Um' mid-sentence may be a name ('Um Kalthoum').
            or (token[0].isupper() and not token.isupper() and not sentence_start
                and not re.match(r",|\.\.\.|…", line[m.end():]))
        )
        if keep:
            kept_end = m.end()
            continue
        segment = line[pos:m.start()]
        if capitalize and re.search(r"\w", segment):
            segment = _capitalize_first_word(segment)
            capitalize = False
        head = out + segment
        trimmed = head.rstrip(" \t")
        at_sentence_start = not trimmed or trimmed[-1] in _TERMINAL
        end = _FILLER_TAIL_RE.match(line, m.end()).end()
        following = line[end:end + 1]
        if following and following in _TERMINAL:
            if at_sentence_start:
                # A standalone 'Um.' sentence: drop it with its punctuation.
                end = re.compile(r"[.!?]+[ \t]*").match(line, end).end()
                out = head
            else:
                # 'I think um.' -> 'I think.'
                out = trimmed.rstrip(",;")
        elif not following:
            out = trimmed if at_sentence_start else trimmed.rstrip(",;")
        elif at_sentence_start:
            out = head
            capitalize = capitalize or token[0].isupper()
        elif following == ")" and trimmed.endswith("("):
            # 'I need (um) the thing' -> 'I need the thing'
            out = trimmed[:-1].rstrip(" \t")
            end += 1
            if not out:
                end = re.compile(r"[ \t]*").match(line, end).end()
            nxt = line[end:end + 1]
            if out and nxt and nxt not in " \t" + _TERMINAL + ",;:":
                out += " "
        elif following in ",;:)" or (trimmed and trimmed[-1] in "(["):
            out = trimmed
        elif trimmed.endswith(",") and line[m.end():m.end() + 1] == ",":
            # Commas around a mid-clause filler were only there for the filler
            # ('we should, uh, ship it'). Keep one after an introductory word
            # ('So, um, I') or inside a list ('eggs, bread, um, milk').
            clause = re.split(r"[.!?]", trimmed[:-1])[-1]
            words = clause.split()
            # A comma one or two words on means a list or appositive:
            # 'eggs, um, milk, and bread', 'my brother, uh, John, is here'.
            next_item = re.match(r"[ \t]*(?:[^\s,.!?]+[ \t]+)?[^\s,.!?]+,", line[m.end() + 1:])
            keep = ("," in clause or next_item is not None
                    or (len(words) == 1 and words[0].lower() in _INTRO_WORDS))
            out = (trimmed if keep else trimmed[:-1]) + " "
        else:
            out = trimmed + " " if trimmed else head
        pos = end
    if pos == 0:
        return line
    rest = line[pos:]
    if capitalize:
        rest = _capitalize_first_word(rest)
    return out + rest


def _collapse_repeats(line: str) -> str:
    def repl(m: re.Match) -> str:
        first, sep, second = m.group(1), m.group(2), m.group(3)
        word = first.lower()
        if word not in FUNCTION_WORDS:
            return m.group(0)
        # 'I I' -> 'I'; 'The the' -> 'The'; but 'the The' (new sentence-ish) stays.
        if second != first and second != word:
            return m.group(0)
        return first

    previous = None
    while previous != line:
        previous = line
        line = _REPEAT_RE.sub(repl, line)
    return line


def _clean_line(line: str, spans: list[str], remove_repetitions: bool) -> str:
    # Code symbols are checked on the unmasked line (quoted text included).
    if not line.strip() or _line_has_code(_unmask(line, spans)):
        return line
    cleaned = _remove_fillers(line)
    if not re.search(r"[^\W_]", _unmask(cleaned, spans)):
        cleaned = line  # a filler-only line stays as dictated
    if remove_repetitions:
        cleaned = _collapse_repeats(cleaned)
    return cleaned


def clean_text(text: str, enabled: bool = False, protected_terms: Iterable[str] = (),
               remove_repetitions: bool = False) -> str:
    """Return conservatively cleaned text, or the input unchanged."""
    if not isinstance(text, str) or enabled is not True or not text:
        return text
    terms = list(protected_terms or [])
    if not all(isinstance(t, str) for t in terms):
        return text
    folded = text.casefold()
    if any(t.strip() and t.strip().casefold() in folded for t in terms):
        return text
    if _TRANSPORT_RE.search(text):
        return text
    masked = _mask_spans(text)
    if masked is None:
        return text
    body, spans = masked
    lines = body.split("\n")
    cleaned = "\n".join(_clean_line(line, spans, remove_repetitions) for line in lines)
    if cleaned == body:
        return text
    result = _unmask(cleaned, spans)
    if not re.search(r"[^\W_]", result):
        return text
    return result
