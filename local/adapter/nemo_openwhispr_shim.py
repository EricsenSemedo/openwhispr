#!/usr/bin/env python3
"""Generic local shim that lets OpenWhispr's Self-Hosted transcription talk
to an ASR backend that does NOT speak the OpenAI /audio/transcriptions
protocol.

The contract OpenWhispr speaks (verified against its source):

  - It POSTs multipart/form-data to {Server URL}/audio/transcriptions.
  - Fields: `file` (recorded audio, usually WebM/Opus, filename audio.webm),
    optional `model` (the string from the Self-Hosted panel), optional
    `language` (ISO code), optional `prompt` (custom-dictionary hint).
    Self-Hosted transcription is batch HTTP; `stream` is not used on this path.
  - Do not require Authorization from OpenWhispr. Hold vendor keys in env vars.
  - It expects HTTP 200 with JSON {"text": "..."}. Empty text shows
    "No audio detected"; non-200 is surfaced as an API error.

This file is the vendor-neutral template: fill in `transcribe()` with your
backend's call and everything else works as-is. See README.md for details.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8765
# Rebuild 2026-10-08: the adapter may override these (alternate test port,
# explicit ffmpeg). Defaults equal the previously hard-coded values.
NEMO_BASE_URL = "http://127.0.0.1:8080"
FFMPEG = "ffmpeg"
MAX_BODY_BYTES = 25 * 1024 * 1024  # reject anything larger than ~25 MB (HTTP 413)
# Local-only NeMo adapter; no credentials or external endpoint.


def parse_multipart_form(body: bytes, content_type: str) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    """Parse multipart/form-data into (text_fields, files). files maps name -> (filename, bytes).
    Stdlib-only; works on Python 3.8-3.13+ (the cgi module was removed in 3.13)."""
    m = re.search(r'boundary="?([^";]+)"?', content_type)
    if not m:
        raise ValueError("missing multipart boundary in Content-Type")
    delim = b"--" + m.group(1).strip().encode()
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for chunk in body.split(delim):
        if not chunk or chunk.startswith(b"--"):  # preamble, closing delimiter
            continue
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        if b"\r\n\r\n" not in chunk:
            continue
        raw_headers, content = chunk.split(b"\r\n\r\n", 1)
        disposition = ""
        for line in raw_headers.decode("utf-8", "replace").split("\r\n"):
            if line.lower().startswith("content-disposition:"):
                disposition = line
        name_match = re.search(r'name="([^"]*)"', disposition)
        if not name_match:
            continue
        name = name_match.group(1)
        file_match = re.search(r'filename="([^"]*)"', disposition)
        if file_match is not None:
            files[name] = (file_match.group(1), content)
        else:
            fields[name] = content.decode("utf-8", "replace")
    return fields, files


def convert_audio(input_path: str) -> str:
    """Transcode any input container to 16 kHz mono PCM WAV via ffmpeg.
    Returns the path to a new temp WAV; caller owns cleanup."""
    fd, out_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        subprocess.run(
            [FFMPEG, "-y", "-i", input_path, "-ar", "16000", "-ac", "1",
             "-c:a", "pcm_s16le", "-f", "wav", out_path],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        os.remove(out_path)
        raise
    return out_path


def build_nemo_request(audio_path: str, model: str, language: str | None, prompt: str | None):
    """Only loopback WAV requests; recorded OpenWhispr hints are forwarded."""
    boundary = "nemo-local-" + uuid.uuid4().hex
    parts = []
    for key, value in (("model", model), ("language", language), ("prompt", prompt)):
        if value:
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode())
    with open(audio_path, "rb") as audio:
        parts.append(audio.read())
    parts.append((f'\r\n--{boundary}--\r\n').encode())
    return urllib.request.Request(
        NEMO_BASE_URL + "/v1/audio/transcriptions", data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST"
    )


def transcribe(audio_path: str, model: str, language: str | None, prompt: str | None) -> str:
    request = build_nemo_request(audio_path, model, language, prompt)
    # Explicitly bypass ambient HTTP proxies for this hard-coded loopback endpoint.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=120) as response:
        result = json.load(response)
    if not isinstance(result, dict) or not isinstance(result.get("text"), str):
        raise ValueError("NeMo response must contain a string text field")
    return result["text"]


# Bug review 2026-10-17: Parakeet returns empty text for some quiet but clear
# clips (a 6 s sentence peaking near -15 dBFS came back empty 3 times out of
# 3 and was transcribed correctly at +6 dB). An empty result for a clip that
# has sound is retried once, 6 dB louder. Digital silence (a gated or muted
# mic) and very quiet clips are not retried, so noise never turns into words.
#
# Replay 2026-11-08: the gain retry recovered 0 of its 4 live tries, and 1 of
# the 7 sounding clips in the whole history that decode empty. The same clip
# with half a second of silence added at the end recovered 6 of the 7: these
# are short dictations that end right at the last word, not quiet ones. So the
# silence is tried first, then the gain. Only on an empty result: padding every
# clip changed a third of the good transcripts with no gain.
RETRY_TAIL_SECONDS = 0.5
RETRY_GAIN_DB = 6.0
RETRY_MIN_PEAK = 328         # about -40 dBFS on 16-bit samples
RETRY_MAX_SECONDS = 60       # bounds the extra work on a long clip


def louder_copy(wav_path: str, gain_db: float = RETRY_GAIN_DB) -> str | None:
    """A temp WAV gain_db louder (clipped), or None when the clip should not
    be retried: not 16-bit mono PCM, longer than RETRY_MAX_SECONDS, or a peak
    below RETRY_MIN_PEAK. The caller removes the returned file."""
    import array
    import wave
    with wave.open(wav_path, "rb") as src:
        params = src.getparams()
        if params.sampwidth != 2 or params.nchannels != 1 or params.comptype != "NONE":
            return None
        if params.nframes > RETRY_MAX_SECONDS * params.framerate:
            return None
        samples = array.array("h", src.readframes(params.nframes))
    if not samples or max(max(samples), -min(samples)) < RETRY_MIN_PEAK:
        return None
    gain = 10 ** (gain_db / 20)
    louder = array.array("h", (max(-32768, min(32767, round(x * gain))) for x in samples))
    fd, out_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with wave.open(out_path, "wb") as dst:
        dst.setparams(params)
        dst.writeframes(louder.tobytes())
    return out_path


def tail_padded_copy(wav_path: str, seconds: float = RETRY_TAIL_SECONDS) -> str | None:
    """A temp WAV with `seconds` of silence added at the end, or None under the
    same conditions as louder_copy(). The caller removes the returned file."""
    import array
    import wave
    with wave.open(wav_path, "rb") as src:
        params = src.getparams()
        if params.sampwidth != 2 or params.nchannels != 1 or params.comptype != "NONE":
            return None
        if params.nframes > RETRY_MAX_SECONDS * params.framerate:
            return None
        frames = src.readframes(params.nframes)
    samples = array.array("h", frames)
    if not samples or max(max(samples), -min(samples)) < RETRY_MIN_PEAK:
        return None
    fd, out_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with wave.open(out_path, "wb") as dst:
        dst.setparams(params)
        dst.writeframes(frames + b"\0\0" * int(seconds * params.framerate))
    return out_path


# Set by the adapter (flow_recovery): called with a probe after a decode failed
# because of the server. Returns True when decoding again is worth trying.
recover_backend = None


def timed_out(exc: Exception) -> bool:
    return isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)


def backend_failed(exc: Exception) -> bool:
    """The server itself failed: a 5xx, or a refused or dropped connection. A
    4xx is about the request. A time-out may be a server that is only slow (a
    busy GPU, a long clip), and restarting would lose its work."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    if timed_out(exc):
        return False
    return isinstance(exc, (urllib.error.URLError, OSError, http.client.HTTPException))


def remove_temp(path: str) -> None:
    """A virus scan or the indexer can hold a temp file for a moment. That must
    not turn a good result into an error."""
    try:
        os.remove(path)
    except OSError:
        pass


def probe_wav_bytes(seconds: float = 0.5, rate: int = 16000) -> bytes:
    """A low tone, not silence: the server must really run the model to answer."""
    import array
    import io
    import math
    import wave
    samples = array.array("h", (round(1500 * math.sin(2 * math.pi * 220 * i / rate)) for i in range(int(seconds * rate))))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(samples.tobytes())
    return buffer.getvalue()


def backend_answers(model: str) -> bool:
    """Decodes a tiny clip. An answer of any text means the server works."""
    fd, path = tempfile.mkstemp(suffix=".wav")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(probe_wav_bytes())
        request = build_nemo_request(path, model, None, None)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=10) as response:
            return isinstance(json.load(response), dict)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        # A busy server is slow, not broken: a time-out must not get it replaced.
        return timed_out(exc)
    finally:
        remove_temp(path)


def transcribe_recovering(wav_path: str, model: str, language: str | None, prompt: str | None) -> str:
    try:
        return transcribe(wav_path, model, language, prompt)
    except Exception as exc:
        if not backend_failed(exc) or recover_backend is None or not recover_backend(lambda: backend_answers(model)):
            raise
    return transcribe(wav_path, model, language, prompt)


# A gated or muted mic records exact zeros, and the model can still "hear" words
# in that: one clip with a peak of 0 pasted 2 words (replay 2026-11-08, all 502
# recordings: 16 clips peak under 50, and the quietest clip with 3+ words peaks at
# 2,579). Nothing was said, so such a clip is not decoded at all.
SILENT_PEAK = 50


def is_silent(wav_path: str) -> bool:
    import array
    import wave
    try:
        with wave.open(wav_path, "rb") as src:
            params = src.getparams()
            if params.sampwidth != 2 or params.comptype != "NONE":
                return False
            samples = array.array("h", src.readframes(params.nframes))
    except (OSError, EOFError, ValueError, wave.Error):
        return False  # let the speech server judge a clip this can't read
    return not samples or max(max(samples), -min(samples)) < SILENT_PEAK


def transcribe_with_retry(wav_path: str, model: str, language: str | None,
                          prompt: str | None) -> str:
    if is_silent(wav_path):
        print(json.dumps({"t": time.strftime("%H:%M:%S"), "event": "asr-skip-silence"}), flush=True)
        return ""
    text = transcribe_recovering(wav_path, model, language, prompt)
    if (text or "").strip():
        return text
    for kind, make_copy in (("tail", tail_padded_copy), ("gain", louder_copy)):
        try:
            copy = make_copy(wav_path)
        except Exception:  # a retry must never turn "no text" into an error
            return text
        if copy is None:
            return text
        try:
            retried = transcribe(copy, model, language, prompt)
        finally:
            remove_temp(copy)
        recovered = bool((retried or "").strip())
        print(json.dumps({"t": time.strftime("%H:%M:%S"), "event": "asr-retry", "kind": kind,
                          "recovered": recovered}), flush=True)
        if recovered:
            return retried
    return text


class ShimHandler(BaseHTTPRequestHandler):
    """Handles the single POST endpoint OpenWhispr calls."""

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        # OpenWhispr onboarding probes /models without capturing audio.
        if self.path.rstrip('/') not in ('/models', '/v1/models', '/health'):
            self._send_json(404, {'error': 'not found'})
            return
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(NEMO_BASE_URL + '/ready', timeout=3) as response:
                ready = json.load(response)
            if not ready.get('ready') or 'asr' not in ready.get('capabilities', []):
                raise RuntimeError('Local ASR backend is not ready')
        except Exception as exc:
            self._send_json(503, {'error': str(exc)})
            return
        if self.path.rstrip('/') == '/health':
            self._send_json(200, ready)
        else:
            self._send_json(200, {'object': 'list', 'data': [
                {'id': 'parakeet-v3-q8', 'object': 'model', 'owned_by': 'local'}
            ]})

    def do_POST(self) -> None:  # noqa: N802 (http.server naming)
        if self.path.rstrip("/") not in ("/audio/transcriptions", "/v1/audio/transcriptions"):
            self._send_json(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "invalid Content-Length"})
            return
        if length <= 0:
            self._send_json(400, {"error": "empty body"})
            return
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"error": "request body too large"})
            return

        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        try:
            fields, files = parse_multipart_form(body, content_type)
        except ValueError as exc:
            self._send_json(400, {"error": f"bad multipart: {exc}"})
            return

        if "file" not in files:
            self._send_json(400, {"error": "missing 'file' field"})
            return

        filename, file_bytes = files["file"]
        model = fields.get("model", "")
        language = fields.get("language") or None
        prompt = fields.get("prompt") or None

        suffix = os.path.splitext(filename)[1] or ".webm"
        fd, in_path = tempfile.mkstemp(suffix=suffix)
        wav_path = None
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(file_bytes)
            wav_path = convert_audio(in_path)
            text = transcribe_with_retry(wav_path, model, language, prompt)
            self._send_json(200, {"text": text or "", "object": "transcription"})
        except FileNotFoundError:
            self._send_json(500, {"error": "ffmpeg not found on PATH"})
        except subprocess.CalledProcessError:
            self._send_json(500, {"error": "ffmpeg failed to transcode audio"})
        except NotImplementedError as exc:
            self._send_json(500, {"error": str(exc)})
        except Exception as exc:  # surface, never swallow
            self._send_json(500, {"error": f"transcription failed: {exc}"})
        finally:
            for path in (in_path, wav_path):
                if path and os.path.exists(path):
                    remove_temp(path)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), ShimHandler)
    server.daemon_threads = True  # let Ctrl+C exit even with a request in flight
    print(f"Custom ASR shim listening on http://localhost:{PORT}")
    print("Point OpenWhispr at it: Settings -> Transcription -> Self-Hosted")
    print(f"  Server URL: http://localhost:{PORT}")
    print("  Vendor API key: hold it in an env var inside transcribe()")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
