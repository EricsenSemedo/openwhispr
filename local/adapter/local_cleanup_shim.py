"""Local ASR adapter plus dictation cleanup for OpenWhispr (loopback only).

POST /audio/transcriptions and /v1/audio/transcriptions: delegated unchanged to
nemo_openwhispr_shim (NeMo Parakeet backend). When context awareness is on,
the arrival of audio also starts a read-only snapshot of the app and text
field being dictated into (flow_context), in parallel with recognition.

POST /v1/chat/completions (model conservative-filler-filter-v1): the
deterministic filler rules (cleanup_filter), then learned replacements, the
user's terms, "scratch that" and numbers as digits (flow_text), then, when
"llmCleanup" is enabled and the model answers in time, a light context-aware
cleanup by the small local model (flow_llm; punctuation and spelling only in
agent apps), then the user's Wispr style for that app (flow_style). If the
model is slow, down, or its output drifts from what was said, the rules-only
text is used. The app keeps raw_text and exposes 'View raw transcript' in its
history menu.

Flow endpoints for the OpenWhispr main process (no Origin header allowed):
  POST /v1/flow/adjust     {text} -> {text} casing/spacing for the cursor
  POST /v1/flow/clipboard  {text} -> put text on the clipboard, kept out of
                                     Win+V history and cloud clipboard
  POST /v1/flow/learn      {pairs:[{from,to}], sentence} -> {accepted, rejected}
                           (review only; nothing is stored)
  POST /v1/flow/remember   {pairs} -> store pairs the app has saved
  POST /v1/flow/forget     {words} -> drop auto-learned pairs (app Undo)
  GET  /v1/flow/dictionary -> {words, replacements, snippets}

Phone link (optional, 127.0.0.1:18766 behind tailscale serve): /v1/health,
/v1/cleanup, /v1/dictionary for the Android keyboard; see FLOW-PARITY.md.
"""
from __future__ import annotations
import importlib.util
import json
import os
from datetime import datetime
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from cleanup_filter import clean_text
import flow_context
import flow_learn
import flow_llm
import flow_style
import flow_text

# Rebuild 2026-10-08 (durable layout). The original assumed parents[1] was the
# Temp staging root. Every path is now explicit and overridable on the command
# line; defaults are relative to this file in the OpenWhisprLocal layout:
#   <root>\adapter\local_cleanup_shim.py      (this file)
#   <root>\adapter\nemo_openwhispr_shim.py
#   <root>\profile\local-dictation-options.json
ADAPTER_DIR = Path(__file__).resolve().parent
ROOT = ADAPTER_DIR.parent
OPTIONS = ROOT / 'profile' / 'local-dictation-options.json'
MAX_OPTIONS_BYTES = 65536
MAX_BODY_BYTES = 1024 * 1024
MAX_FLOW_BODY_BYTES = 64 * 1024
APP_ORIGIN = 'null'  # Electron file:// pages send 'Origin: null'
MODEL = 'conservative-filler-filter-v1'
APP_TRANSCRIPT_PREFIX = '<transcript>\n'
APP_TRANSCRIPT_SUFFIX = '\n</transcript>\n\nOutput only the cleaned transcript.'
DICTIONARY_MARKER = 'Custom Dictionary (use these exact spellings when they appear in the text): '
TERMINAL_EXES = {'windowsterminal.exe', 'cmd.exe', 'powershell.exe', 'pwsh.exe', 'conhost.exe',
                 'wezterm-gui.exe', 'alacritty.exe', 'mintty.exe', 'openconsole.exe'}
_CODE_LIKE = re.compile(r'```|`[^`]+`|[{}\[\]<>\\=|_]')
spec = importlib.util.spec_from_file_location('owned_asr_shim', ADAPTER_DIR / 'nemo_openwhispr_shim.py')
asr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(asr)

CONTEXT = flow_context.ContextCache()
_last_vocabulary: list[str] = []


LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_KEEP = 3  # adapter.log.1 .. adapter.log.3
_log_lock = threading.Lock()


def log(event, **fields):
    # Timing and decisions only; dictated text is never logged.
    # 't' is local ISO 8601 with the UTC offset (2026-10-23T21:05:09-04:00).
    line = json.dumps({'t': datetime.now().astimezone().isoformat(timespec='seconds'), 'event': event, **fields})
    with _log_lock:
        print(line, flush=True)
        rotate_own_log()


def rotate_own_log(path: Path | None = None) -> bool:
    """Size-based rotation of local-asr-logs\\adapter.log (2026-10-23).

    The supervisor opens that file and hands it to this process as stdout and
    stderr, and Windows does not let an open file be renamed. So the adapter
    copies it to adapter.log.1 (older copies shift up to LOG_KEEP), then
    truncates its own handle and seeks to the start ("copytruncate"). It acts
    only when stdout really is that file. cleanup-llm.log belongs to
    llama-server and is not touched."""
    try:
        fd = sys.stdout.fileno()
        st = os.fstat(fd)
        if st.st_size <= LOG_MAX_BYTES:
            return False
        path = path or profile_dir() / 'local-asr-logs' / 'adapter.log'
        if not os.path.samestat(st, os.stat(path)):
            return False
        sys.stdout.flush()
        sys.stderr.flush()
        for i in range(LOG_KEEP - 1, 0, -1):
            older = path.with_name(f'{path.name}.{i}')
            if older.exists():
                os.replace(older, path.with_name(f'{path.name}.{i + 1}'))
        shutil.copyfile(path, path.with_name(path.name + '.1'))
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        return True
    except (OSError, ValueError, AttributeError):
        return False  # logging must never break a dictation


def options():
    # Invalid, oversized or deleted config preserves text (cleanup disabled).
    try:
        if OPTIONS.stat().st_size > MAX_OPTIONS_BYTES:
            return {}
        value = json.loads(OPTIONS.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def profile_dir() -> Path:
    return OPTIONS.parent


def local_vocabulary() -> list[str]:
    """profile\\local-vocabulary.json: terms kept by the adapter, in their exact
    spelling (added in the 2026-10-14 audit). They come first, so "Robux" here
    wins over a lower-case "robux" in the app dictionary."""
    try:
        words = json.loads((profile_dir() / 'local-vocabulary.json').read_text(encoding='utf-8-sig'))['words']
    except (OSError, ValueError, KeyError, TypeError):
        return []
    return [w for w in words if isinstance(w, str) and w.strip()][:200] if isinstance(words, list) else []


def merge_terms(*groups) -> list[str]:
    """Case-insensitive de-duplication; the first spelling wins."""
    seen, out = set(), []
    for group in groups:
        for word in group:
            key = word.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(word.strip())
    return out


def llm_config(settings):
    cfg = settings.get('llmCleanup')
    if not isinstance(cfg, dict) or cfg.get('enabled') is not True or not isinstance(cfg.get('baseUrl'), str):
        return None
    base = cfg['baseUrl']
    if not (base.startswith('http://127.0.0.1:') or base.startswith('http://localhost:')):
        return None  # loopback model servers only
    return cfg


def style_for(category, settings):
    styles = dict(flow_style.DEFAULT_STYLES)
    if isinstance(settings.get('styles'), dict):
        styles.update({k: v for k, v in settings['styles'].items() if v in ('formal', 'casual')})
    return styles.get(category, 'formal')


def split_dictionary(system_prompt):
    """OpenWhispr appends the dictionary (and snippet triggers) to its prompt."""
    if not isinstance(system_prompt, str) or DICTIONARY_MARKER not in system_prompt:
        return []
    tail = system_prompt.rsplit(DICTIONARY_MARKER, 1)[1].split('\n', 1)[0]
    return [w.strip() for w in tail.split(',') if w.strip()][:200]


def warm_prefix():
    """Vocabulary and hints the next dictation will most likely send, built
    the way flow_cleanup builds them, so warm() caches the same prompt start.
    The app's dictionary is known from the last cleanup request."""
    direct, hints = flow_learn.split_pairs(flow_learn.load_pairs(profile_dir()))
    vocabulary = _last_vocabulary or local_vocabulary()
    return merge_terms(vocabulary, (t for _, t in direct + hints)), hints


def ask_model(cfg):
    def ask(system, user):
        return flow_llm.call_model(cfg['baseUrl'], cfg.get('model', 'cleanup'), system, user, 3,
                                   cfg.get('reviewTimeoutMs', 4000) / 1000, cfg.get('apiKey'))
    return ask


def flow_cleanup(text, *, ctx, category, vocabulary, settings, use_rules=True):
    """Rules -> learned replacements -> local model -> style. Returns (text, info)."""
    t0 = time.monotonic()
    protected = settings.get('protectedTerms', [])
    if use_rules:
        text = clean_text(text, enabled=settings.get('conservativeCleanup') is True,
                          protected_terms=protected,
                          remove_repetitions=settings.get('removeRepeatedFunctionWords') is True)
    style = style_for(category, settings)
    info = {'engine': 'rules', 'style': style, 'category': category}
    if any(term and term in text for term in protected) or _CODE_LIKE.search(text):
        info['skipped'] = 'protected'
        return text, info
    direct, hints = flow_learn.split_pairs(flow_learn.load_pairs(profile_dir()))
    # These run on every dictation, short ones included (the model skips
    # dictations under minWords).
    fixes, before = [], text
    text = flow_style.apply_replacements(text, direct)
    vocabulary = merge_terms(vocabulary, (t for _, t in direct + hints))
    text = flow_text.fix_vocabulary(text, vocabulary)
    if text != before:
        fixes.append('vocabulary')
    if settings.get('scratchThat') is True:
        before, text = text, flow_text.scratch_that(text)
        if text != before:
            fixes.append('scratch')
    if settings.get('numbersAsDigits') is True:
        before, text = text, flow_text.numbers_to_digits(text)
        if text != before:
            fixes.append('digits')
    # After scratch_that, which reads an ellipsis as a sentence end.
    before, text = text, flow_text.drop_pause_ellipses(text, style, set(vocabulary))
    if text != before:
        fixes.append('ellipsis')
    if fixes:
        info['fixes'] = fixes
    cfg = llm_config(settings)
    words = len(text.split())
    if cfg and words >= cfg.get('minWords', 4):
        cfg = dict(cfg)
        # Long dictations get proportionally longer before falling back.
        cfg['timeoutMs'] = min(cfg.get('timeoutMs', 2500) + 20 * words, cfg.get('maxTimeoutMs', 6000))
        out, llm_info = flow_llm.clean_with_model(text, ctx=ctx, style=style, category=category,
                                                  vocabulary=vocabulary, hints=hints, cfg=cfg)
        info.update(llm_info)
        if out:
            # The model may type pauses as "..." too.
            out = flow_text.drop_pause_ellipses(out, style, set(vocabulary))
            text = flow_text.fix_vocabulary(out, vocabulary)
    elif cfg:
        info['skipped'] = 'short'
    if settings.get('applyStyle') is True:
        text = flow_style.apply_style(text, style)
    info['totalMs'] = round((time.monotonic() - t0) * 1000)
    return text, info


def cleanup_completion(body, settings=None):
    global _last_vocabulary
    if not isinstance(body, dict) or body.get('model') != MODEL:
        raise ValueError('Use the explicitly labeled conservative-filler-filter-v1 model')
    if body.get('stream'):
        raise ValueError('This text-only filter supports non-streaming cleanup only')
    messages = body.get('messages')
    if not isinstance(messages, list):
        raise ValueError('messages must be an array')
    users = [m.get('content') for m in messages if isinstance(m, dict) and m.get('role') == 'user']
    if len(users) != 1 or not isinstance(users[0], str):
        raise ValueError('Provide exactly one user message containing the raw transcript')
    systems = [m.get('content') for m in messages if isinstance(m, dict) and m.get('role') == 'system']
    raw = users[0]
    # The shipped ReasoningService wraps its raw transcript when no custom
    # system prompt is configured. This exact transport envelope is not text
    # to paste. Strip one outer envelope only; preserve nested/literal content.
    if raw.startswith(APP_TRANSCRIPT_PREFIX) and raw.endswith(APP_TRANSCRIPT_SUFFIX):
        raw = raw[len(APP_TRANSCRIPT_PREFIX):-len(APP_TRANSCRIPT_SUFFIX)]
    settings = options() if settings is None else settings
    protected = settings.get('protectedTerms', [])
    if not isinstance(protected, list) or not all(isinstance(x, str) for x in protected):
        raise ValueError('protectedTerms must be a string array')
    literal_transport_markup = '<transcript>' in raw or '</transcript>' in raw
    info = {'engine': 'rules'}
    if literal_transport_markup or settings.get('conservativeCleanup') is not True:
        text = clean_text(raw, enabled=False, protected_terms=protected)
    else:
        vocabulary = merge_terms(local_vocabulary(), split_dictionary(systems[0] if systems else None))
        _last_vocabulary = vocabulary
        ctx = CONTEXT.latest(wait=0.5) if settings.get('contextAwareness') is True else None
        category = flow_style.app_category((ctx or {}).get('exe'), (ctx or {}).get('title'))
        try:
            text, info = flow_cleanup(raw, ctx=ctx, category=category, vocabulary=vocabulary, settings=settings)
        except Exception as exc:  # never lose a dictation to a new stage or a config slip
            text = clean_text(raw, enabled=True, protected_terms=protected,
                              remove_repetitions=settings.get('removeRepeatedFunctionWords') is True)
            info = {'engine': 'rules', 'error': type(exc).__name__}
        log('cleanup', words=len(raw.split()), contextMs=(ctx or {}).get('ms'),
            caret=(ctx or {}).get('caret_known'), app=(ctx or {}).get('exe'), **info)
    # OpenWhispr treats empty cleanup as a failure; preserve filler-only dictation
    # instead of risking an empty reply or silently discarding the user's text.
    if not text.strip():
        text = raw
    return {'id':'local-conservative-cleanup', 'object':'chat.completion',
        'created':int(time.time()), 'model':MODEL,
        'choices':[{'index':0,'message':{'role':'assistant','content':text},'finish_reason':'stop'}],
        'usage':{'prompt_tokens':0,'completion_tokens':0,'total_tokens':0},
        'local_cleanup':{'engine':info.get('engine', 'rules'),'language_model':info.get('engine') == 'llm',
            'changed':raw != text, 'raw_retained_by':'OpenWhispr local history'}}


def parse_hwnd(value):
    """windows-fast-paste prints the target as hex ("TARGET %p")."""
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r'(0[xX])?[0-9a-fA-F]{1,16}', value):
        return int(value, 16)
    return None


_last_paste: dict[int, float] = {}


def adjust_for_cursor(text, settings, hwnd=None):
    if settings.get('contextAwareness') is not True or not text:
        return text, 'disabled'
    fg = flow_context.foreground_app()
    # OpenWhispr pastes into the window captured when recording started
    # (hwnd). When that window is in front, its field is read right now, so
    # an earlier paste (or typing) is already included. Otherwise the
    # snapshot taken when this dictation's audio arrived is used, once, and
    # only if it was taken after the last paste into that window.
    target = hwnd or fg.get('hwnd')
    if fg.get('hwnd') == target:
        ctx = flow_context.capture(0.25)
    else:
        ctx = CONTEXT.take(target, _last_paste.get(target, 0.0))
        if ctx is None:
            return text, 'target-not-foreground'
    if target:
        _last_paste[target] = time.monotonic()
    if not ctx or not ctx.get('caret_known') or ctx.get('password'):
        return text, 'no-caret'
    if (ctx.get('exe') or '').lower() in TERMINAL_EXES:
        return text, 'terminal'
    adjusted = flow_style.cursor_adjust(text, ctx.get('before'), ctx.get('after'), set(_last_vocabulary))
    return adjusted, 'ok'


def local_db_rows(query):
    db = profile_dir() / 'transcriptions.db'
    try:
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=1)
        try:
            return con.execute(query).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []


def dictionary_payload():
    words = merge_terms(local_vocabulary(),
                        (r[0] for r in local_db_rows('select word from custom_dictionary where deleted_at is null')))
    snippets = [{'trigger': t, 'replacement': r} for t, r in
                local_db_rows('select trigger, replacement from snippets where deleted_at is null')]
    pairs = [{'from': p['from'], 'to': p['to']} for p in flow_learn.load_pairs(profile_dir())]
    return {'words': words, 'replacements': pairs, 'snippets': snippets}


def learn(body, settings):
    pairs = body.get('pairs')
    sentence = body.get('sentence') or ''
    if not isinstance(pairs, list) or not isinstance(sentence, str):
        raise ValueError('pairs must be an array')
    cfg = llm_config(settings)

    def unavailable(system, user):
        raise ConnectionError('no local model')
    # Review only. The app saves the accepted words to its dictionary and
    # then calls /v1/flow/remember, so pairs are never stored for words the
    # app gave up on (timeout) or no longer wants (auto-learn switched off).
    accepted, rejected = flow_learn.review(pairs, sentence, ask_model(cfg) if cfg else unavailable,
                                           budget=6.0)
    log('learn', accepted=[a['why'] for a in accepted], rejected=[r['why'] for r in rejected])
    return {'accepted': accepted, 'rejected': rejected}


def remember(body):
    pairs = body.get('pairs')
    if not isinstance(pairs, list) or not all(
            isinstance(p, dict) and isinstance(p.get('from'), str) and isinstance(p.get('to'), str)
            and p['from'].strip() and p['to'].strip() for p in pairs):
        raise ValueError('pairs must be [{from, to}]')
    flow_learn.remember(profile_dir(), [{'from': p['from'].strip(), 'to': p['to'].strip()}
                                        for p in pairs[:flow_learn.MAX_CANDIDATES]], 'auto-learn')
    return {'ok': True}


class LocalCleanupShim(asr.ShimHandler):
    CHAT_PATHS = ('/v1/chat/completions', '/chat/completions')
    AUDIO_PATHS = ('/audio/transcriptions', '/v1/audio/transcriptions')
    FLOW_POST = ('/v1/flow/adjust', '/v1/flow/clipboard', '/v1/flow/learn', '/v1/flow/remember',
                 '/v1/flow/forget')
    timeout = 120  # socket timeout; uploads of long recordings stay well inside it

    def log_message(self, format, *args):
        pass  # request lines would be noise in the supervisor log

    def _discard_body(self, length):
        # Read (and drop) a refused body before answering; closing with unread
        # data makes Windows reset the connection before the client sees 413.
        remaining = min(max(length, 0), 4 * 1024 * 1024)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _host_ok(self):
        # Blocks DNS rebinding: a web page that rebinds its own name to
        # 127.0.0.1 still sends its own name as Host.
        port = self.server.server_address[1]
        if self.headers.get('Host') in (f'127.0.0.1:{port}', f'localhost:{port}'):
            return True
        self._send_json(403, {'error': 'Loopback host required'})
        return False

    def _origin_allowed(self):
        origin = self.headers.get('Origin')
        return origin is None or origin == APP_ORIGIN

    def end_headers(self):
        if self.headers.get('Origin') == APP_ORIGIN:
            self.send_header('Access-Control-Allow-Origin', APP_ORIGIN)
            self.send_header('Vary', 'Origin')
        super().end_headers()

    def _send_json(self, status, payload):
        if status == 200 and payload.get('object') == 'list':
            payload = dict(payload)
            payload['data'] = [*payload['data'], {'id':MODEL,'object':'model','owned_by':'local-conservative-rules'}]
        super()._send_json(status, payload)

    def do_OPTIONS(self):
        if not self._host_ok():
            return
        if self.path.rstrip('/') not in self.CHAT_PATHS:
            self._send_json(404, {'error': 'not found'})
            return
        if not self._origin_allowed():
            self._send_json(403, {'error': 'Only the installed local app origin is accepted'})
            return
        self.send_response(204)
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        if not self._host_ok():
            return
        if self.path.rstrip('/') == '/v1/flow/dictionary':
            if self.headers.get('Origin') is not None:
                self._send_json(403, {'error': 'Flow endpoints are for the local app process only'})
                return
            self._send_json(200, dictionary_payload())
            return
        super().do_GET()

    def _read_json(self, limit):
        length = int(self.headers.get('Content-Length', '0'))
        if not 1 <= length <= limit:
            self._discard_body(length)
            self._send_json(413, {'error': f'Body must be 1 byte to {limit} bytes'})
            return None
        if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
            self._send_json(415, {'error': 'Requires application/json'})
            return None
        return json.loads(self.rfile.read(length).decode('utf-8'))

    def do_POST(self):
        if not self._host_ok():
            return
        path = self.path.rstrip('/')
        if path in self.FLOW_POST:
            self._flow_post(path)
            return
        if path not in self.CHAT_PATHS:
            # Audio and any other path: unchanged ASR shim behaviour.
            if path in self.AUDIO_PATHS and self._origin_allowed():
                try:
                    settings = options()
                    if settings.get('contextAwareness') is True:
                        CONTEXT.start_capture()
                    cfg = llm_config(settings)
                    if cfg:
                        # The app isn't known yet; keep both prompts warm.
                        vocabulary, hints = warm_prefix()
                        for category in ('other', 'agent'):
                            threading.Thread(target=flow_llm.warm, args=(
                                cfg, category, vocabulary, hints, style_for(category, settings)),
                                daemon=True).start()
                except Exception:
                    pass  # extras must never block speech recognition
            super().do_POST()
            return
        if not self._origin_allowed():
            self._send_json(403, {'error': 'Only the installed local app origin is accepted'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 1 <= length <= MAX_BODY_BYTES:
                self._discard_body(length)
                self._send_json(413, {'error': 'Cleanup text body must be 1 byte to 1 MiB'})
                return
            if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                self._send_json(415, {'error': 'Cleanup requires application/json'})
                return
            body = json.loads(self.rfile.read(length).decode('utf-8'))
            self._send_json(200, cleanup_completion(body))
        except ValueError as exc:
            self._send_json(400,{'error':str(exc)})

    def _flow_post(self, path):
        # Browsers always send Origin on these requests; the app's main
        # process (Node) never does. Web pages therefore can't reach them.
        if self.headers.get('Origin') is not None:
            self._send_json(403, {'error': 'Flow endpoints are for the local app process only'})
            return
        try:
            body = self._read_json(MAX_FLOW_BODY_BYTES)
            if body is None:
                return
            if not isinstance(body, dict):
                raise ValueError('JSON object required')
            settings = options()
            if path == '/v1/flow/learn':
                self._send_json(200, learn(body, settings))
                return
            if path == '/v1/flow/remember':
                self._send_json(200, remember(body))
                return
            if path == '/v1/flow/forget':
                words = body.get('words')
                if not isinstance(words, list):
                    raise ValueError('words must be an array')
                self._send_json(200, {'removed': flow_learn.forget(profile_dir(), words)})
                return
            text = body.get('text')
            if not isinstance(text, str) or not text:
                raise ValueError('text must be a non-empty string')
            if path == '/v1/flow/adjust':
                t0 = time.monotonic()
                adjusted, why = adjust_for_cursor(text, settings, parse_hwnd(body.get('hwnd')))
                log('adjust', result=why, changed=adjusted != text, ms=round((time.monotonic() - t0) * 1000))
                self._send_json(200, {'text': adjusted, 'result': why})
            else:
                if settings.get('privateClipboard') is not True:
                    self._send_json(409, {'error': 'privateClipboard is off'})
                    return
                import flow_clipboard
                # The app waits 400 ms; a write after that could land after
                # its own fallback write, so give up well before.
                flow_clipboard.set_private_text(text, deadline=time.monotonic() + 0.25)
                self._send_json(200, {'ok': True})
        except (ValueError, OSError) as exc:
            self._send_json(400, {'error': str(exc)})
        except Exception as exc:
            self._send_json(500, {'error': type(exc).__name__})


class PhoneLinkHandler(BaseHTTPRequestHandler):
    """Tailnet-only API for the Android keyboard (via tailscale serve).

    tailscale serve adds Tailscale-User-Login for requests from tailnet
    users; only the configured owner is accepted. The port listens on
    loopback, so nothing outside this PC and the tailnet can reach it.
    Browser requests (any Origin) and unexpected Host names are refused, so
    a web page can't reach it through DNS rebinding either."""

    timeout = 15

    def log_message(self, format, *args):
        pass

    def _send(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _route(self):
        path = self.path.split('?', 1)[0].rstrip('/')
        if path.startswith('/openwhispr-link'):
            path = path[len('/openwhispr-link'):]
        return path

    def _authorised(self, link):
        if link.get('enabled') is not True:
            self._send(404, {'error': 'phone link is off'})
            return False
        hosts = link.get('hosts') or []
        if self.headers.get('Origin') is not None or (hosts and self.headers.get('Host') not in hosts):
            log('phone-link-refused', host=self.headers.get('Host'), origin=self.headers.get('Origin') is not None)
            self._send(403, {'error': 'not allowed'})
            return False
        login = self.headers.get('Tailscale-User-Login')
        if not login or login != link.get('owner'):
            self._send(401, {'error': 'tailnet owner only'})
            return False
        return True

    def do_GET(self):
        settings = options()
        link = settings.get('phoneLink') or {}
        if self._route() == '/v1/health':
            self._send(200, {'ok': True, 'llm': llm_config(settings) is not None})
            return
        if not self._authorised(link):
            return
        if self._route() == '/v1/dictionary':
            self._send(200, dictionary_payload())
        else:
            self._send(404, {'error': 'not found'})

    def do_POST(self):
        settings = options()
        link = settings.get('phoneLink') or {}
        if not self._authorised(link):
            return
        if self._route() != '/v1/cleanup':
            self._send(404, {'error': 'not found'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 1 <= length <= MAX_FLOW_BODY_BYTES:
                self._send(413, {'error': 'body too large'})
                return
            body = json.loads(self.rfile.read(length).decode('utf-8'))
            if not isinstance(body, dict):
                raise ValueError('JSON object required')
            text = body.get('text')
            if not isinstance(text, str) or not text.strip() or len(text) > 8000:
                raise ValueError('text must be 1-8000 characters')
            app = body.get('app') if isinstance(body.get('app'), dict) else {}
            category = app.get('category') if app.get('category') in flow_style.DEFAULT_STYLES else 'other'
            context = body.get('context') if isinstance(body.get('context'), dict) else {}
            ctx = {'exe': str(app.get('package') or 'Android app')[:100],
                   'before': str(context.get('before') or '')[-400:] or None,
                   'after': str(context.get('after') or '')[:200] or None}
            # A string here would otherwise become one vocabulary "word" per character.
            words = body.get('dictionary')
            vocabulary = [w for w in words if isinstance(w, str)][:200] if isinstance(words, list) else []
            vocabulary = merge_terms(local_vocabulary(), vocabulary, (r[0] for r in local_db_rows(
                'select word from custom_dictionary where deleted_at is null')))
        except (ValueError, TypeError, AttributeError) as exc:
            self._send(400, {'error': str(exc)})
            return
        t0 = time.monotonic()
        try:
            # The phone runs its own filler rules first, but they miss some
            # (um/uh survived agent-mode phone requests), so the PC rules run
            # again here, as on the PC. They change nothing that is clean.
            out, info = flow_cleanup(text, ctx=ctx, category=category, vocabulary=vocabulary,
                                     settings=settings, use_rules=True)
        except Exception as exc:
            out, info = text, {'engine': 'rules', 'error': type(exc).__name__}
        ms = round((time.monotonic() - t0) * 1000)
        log('phone-cleanup', words=len(text.split()), **info)
        self._send(200, {'text': out or text, 'engine': info.get('engine', 'rules'),
                         'style': info.get('style'), 'ms': ms})


def start_phone_link(settings):
    link = settings.get('phoneLink')
    if not isinstance(link, dict) or link.get('enabled') is not True or not link.get('owner'):
        return
    try:
        # A typo in the port must not stop the adapter (and PC dictation) from starting.
        port = int(link.get('port', 18766))
        server = ThreadingHTTPServer(('127.0.0.1', port), PhoneLinkHandler)
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        log('phone-link-unavailable', error=str(exc))
        return
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name='phone-link', daemon=True).start()
    log('phone-link', port=port)


def main(port=18765):
    server=ThreadingHTTPServer(('127.0.0.1',port),LocalCleanupShim)
    server.daemon_threads=True
    settings = options()
    start_phone_link(settings)
    if settings.get('contextAwareness') is True:
        threading.Thread(target=flow_context.capture, daemon=True).start()  # warm UI Automation
    print(f'Local ASR plus dictation cleanup listening on 127.0.0.1:{port}',flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

def cli(argv=None):
    import argparse
    import shutil
    global OPTIONS
    parser = argparse.ArgumentParser(description='OpenWhispr local ASR adapter + dictation cleanup')
    parser.add_argument('--port', type=int, default=18765)
    parser.add_argument('--options', type=Path, default=OPTIONS,
        help='local-dictation-options.json (normally inside the OpenWhispr profile)')
    parser.add_argument('--nemo-url', default=asr.NEMO_BASE_URL,
        help='NeMo backend base URL; must be loopback')
    parser.add_argument('--ffmpeg', default=None,
        help='ffmpeg executable used to transcode uploads (default: PATH)')
    args = parser.parse_args(argv)
    if not (args.nemo_url.startswith('http://127.0.0.1:') or args.nemo_url.startswith('http://localhost:')):
        parser.error('--nemo-url must be a loopback http URL')
    OPTIONS = args.options.resolve()
    asr.NEMO_BASE_URL = args.nemo_url.rstrip('/')
    ffmpeg = args.ffmpeg or shutil.which('ffmpeg')
    if not ffmpeg or not Path(ffmpeg).is_file():
        parser.error('ffmpeg not found; pass --ffmpeg')
    asr.FFMPEG = str(ffmpeg)
    print(f'options={OPTIONS} nemo={asr.NEMO_BASE_URL} ffmpeg={asr.FFMPEG}', flush=True)
    main(args.port)

if __name__ == '__main__':
    cli()
