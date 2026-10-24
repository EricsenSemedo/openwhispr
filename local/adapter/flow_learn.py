"""Learned vocabulary: "misheard -> correct" pairs, reviewed before saving.

OpenWhispr's own auto-learn saved any edited word that wasn't on a short
common-word list, which is how "thing", "Send", "break" and "PR274" got into
the dictionary. Wispr Flow instead caps candidates per edit, filters common
words, and asks a model to approve the rest. This does the same locally:

  * at most 4 candidates per edit;
  * edits that contain digits (numbers, IDs, PR links) are content, not vocabulary;
  * a corrected word that is NOT a common English word (Tailscale, Fiverr,
    TMDB) is accepted;
  * when both words are common English words ("sent" -> "Send", "Astro" ->
    "Astra"), the local model decides; if it is unavailable, nothing is learned.

Junk filter (2026-10-23, after a function word, a grammar fix and a word
glued to its neighbour were learned as vocabulary):
  * a source among the 100 most frequent English words ("the", "and", "time")
    is never learned: those 100 words make up about half of all running
    English text, so a hint for one would reach almost every dictation;
  * a target that is the source glued to a neighbouring word ("the" ->
    "boxthe", "flare" -> "Cloudflare") is a word-boundary slip of the edit
    diff, not a mishearing;
  * a rare target that shares few letters with the source (similarity under
    0.5) needs the model's yes, like a common one.

Pairs are stored as {"from", "to"}. When the misheard form is itself an
unusual word ("TealScale") it is replaced directly; when it is a real word
("roadblocks" -> Roblox) it is only passed to the cleanup model as a hint.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from difflib import SequenceMatcher

from flow_style import DATA_DIR, common_words

MAX_CANDIDATES = 4
TOP_COMMON = 100  # see the module docstring
MIN_SIMILARITY = 0.5
_top: frozenset[str] | None = None
_lock = threading.Lock()
_WORD = re.compile(r"[\w'’.]+")

REVIEW_PROMPT = """A user dictated a sentence with speech recognition, then corrected one word in it. Decide whether the correction teaches a vocabulary item: a name, brand, product, technical term, acronym, or unusual spelling that the speech recognizer got wrong. Ordinary rewording, grammar fixes, word choice changes, and typo fixes are not vocabulary. Answer with exactly one word: yes or no."""


def _store_path(profile_dir: Path) -> Path:
    return profile_dir / 'learned-replacements.json'


def load_pairs(profile_dir: Path) -> list[dict]:
    try:
        data = json.loads(_store_path(profile_dir).read_text(encoding='utf-8-sig'))
        pairs = data.get('pairs', [])
        return [p for p in pairs if isinstance(p, dict) and isinstance(p.get('from'), str)
                and isinstance(p.get('to'), str)]
    except (OSError, ValueError, AttributeError):
        return []


def save_pairs(profile_dir: Path, pairs: list[dict]) -> None:
    target = _store_path(profile_dir)
    pending = target.with_suffix('.json.next')
    pending.write_text(json.dumps({'version': 1, 'pairs': pairs}, indent=1, ensure_ascii=False) + '\n',
                       encoding='utf-8')
    pending.replace(target)


def _is_common(word: str) -> bool:
    w = word.lower().strip(".'’")
    common = common_words()
    if w in common:
        return True
    for suffix in ('s', 'es', 'ed', 'ing', "'s", '’s'):
        if w.endswith(suffix) and w[:-len(suffix)] in common:
            return True
    return False


def _top_common() -> frozenset[str]:
    global _top
    if _top is None:
        try:
            lines = (DATA_DIR / 'common-en-30k.txt').read_text(encoding='utf-8').split()
        except OSError:
            lines = []
        _top = frozenset(lines[:TOP_COMMON])
    return _top


def _letters(text: str) -> str:
    return re.sub(r"[^\w]|_", '', text.lower())


def is_joined(src: str, dst: str, sentence: str = '') -> bool:
    """dst is src glued to a neighbouring word ("the" -> "boxthe")."""
    s, d = _letters(src), _letters(dst)
    if not s or len(d) <= len(s):
        return False
    if d.startswith(s):
        rest = d[len(s):]
    elif d.endswith(s):
        rest = d[:-len(s)]
    else:
        return False
    nearby = {w.lower().strip(".'’") for w in _WORD.findall(sentence)}
    return len(rest) >= 2 and (rest in common_words() or rest in nearby)


def similarity(src: str, dst: str) -> float:
    return SequenceMatcher(None, src.lower(), dst.lower()).ratio()


def is_direct(from_text: str) -> bool:
    """Replace directly only when the misheard form can't be a real phrase."""
    words = _WORD.findall(from_text)
    if not words or any(ch.isdigit() for ch in from_text):
        return False
    if len(words) >= 2:
        return True
    return not _is_common(words[0])


def split_pairs(pairs: list[dict]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    direct, hints = [], []
    for p in pairs:
        (direct if is_direct(p['from']) else hints).append((p['from'], p['to']))
    return direct, hints


def review(candidates: list[dict], sentence: str, ask_model,
           budget: float | None = None) -> tuple[list[dict], list[dict]]:
    """Returns (accepted, rejected-with-reason). ask_model(system, user) -> str.
    Model questions stop once `budget` seconds have passed (rejected: budget)."""
    accepted, rejected = [], []
    deadline = time.monotonic() + budget if budget else None
    for c in candidates[:MAX_CANDIDATES]:
        src, dst = (c.get('from') or '').strip(), (c.get('to') or '').strip()
        reason = None
        if not src or not dst or src.lower() == dst.lower():
            reason = 'unchanged'
        elif len(dst) < 3:
            reason = 'short'
        elif any(ch.isdigit() for ch in src + dst):
            reason = 'digits'
        elif len(_WORD.findall(src)) == 1 and src.lower().strip(".'’") in _top_common():
            reason = 'common-source'
        elif is_joined(src, dst, sentence):
            reason = 'joined'
        elif not _is_common(dst) and similarity(src, dst) >= MIN_SIMILARITY:
            accepted.append({'from': src, 'to': dst, 'why': 'rare-word'})
            continue
        elif deadline is not None and time.monotonic() > deadline:
            reason = 'budget'
        else:
            try:
                answer = ask_model(REVIEW_PROMPT, f'Sentence: {sentence[:400]}\nCorrection: "{src}" -> "{dst}"')
            except Exception as exc:
                reason = 'model-unavailable:' + type(exc).__name__
            else:
                if answer.strip().lower().startswith('yes'):
                    accepted.append({'from': src, 'to': dst, 'why': 'model-approved'})
                    continue
                reason = 'model-declined'
        rejected.append({'from': src, 'to': dst, 'why': reason})
    for c in candidates[MAX_CANDIDATES:]:
        rejected.append({'from': c.get('from'), 'to': c.get('to'), 'why': 'over-cap'})
    return accepted, rejected


def remember(profile_dir: Path, accepted: list[dict], source: str) -> list[dict]:
    with _lock:
        pairs = load_pairs(profile_dir)
        known = {(p['from'].lower(), p['to'].lower()) for p in pairs}
        for a in accepted:
            key = (a['from'].lower(), a['to'].lower())
            if key not in known:
                pairs.append({'from': a['from'], 'to': a['to'], 'source': source,
                              'added': time.strftime('%Y-%m-%d')})
                known.add(key)
        save_pairs(profile_dir, pairs)
        return pairs


def forget(profile_dir: Path, words: list[str]) -> int:
    """Undo in the app: drop auto-learned pairs whose corrected word is listed.
    Imported pairs (e.g. from Wispr Flow) are kept."""
    targets = {w.lower() for w in words if isinstance(w, str)}
    with _lock:
        pairs = load_pairs(profile_dir)
        kept = [p for p in pairs if not (p.get('source') == 'auto-learn' and p['to'].lower() in targets)]
        if len(kept) != len(pairs):
            save_pairs(profile_dir, kept)
        return len(pairs) - len(kept)
