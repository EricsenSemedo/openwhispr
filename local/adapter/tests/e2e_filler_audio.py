"""End-to-end check: audio -> Parakeet ASR -> cleanup, via the running adapter.

Uses the same two requests OpenWhispr makes: multipart POST to
/v1/audio/transcriptions, then the transcript wrapped in the app's
<transcript> envelope to /v1/chat/completions (conservative-filler-filter-v1).

Usage: python -I e2e_filler_audio.py <audio.webm|wav> [--base http://127.0.0.1:18765]
"""
import argparse
import json
import sys
import urllib.request
import uuid
from pathlib import Path


def post(url, data, ctype):
    req = urllib.request.Request(url, data=data, headers={"Content-Type": ctype}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", type=Path)
    ap.add_argument("--base", default="http://127.0.0.1:18765")
    a = ap.parse_args()
    boundary = "e2e-" + uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\nparakeet-v3-q8\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{a.audio.name}"\r\n'
            f'Content-Type: application/octet-stream\r\n\r\n').encode() + a.audio.read_bytes() + \
        f'\r\n--{boundary}--\r\n'.encode()
    raw = post(a.base + "/v1/audio/transcriptions", body, f"multipart/form-data; boundary={boundary}")["text"]
    envelope = "<transcript>\n" + raw + "\n</transcript>\n\nOutput only the cleaned transcript."
    chat = {"model": "conservative-filler-filter-v1", "messages": [{"role": "user", "content": envelope}]}
    cleaned = post(a.base + "/v1/chat/completions", json.dumps(chat).encode(), "application/json")
    print("RAW    :", raw)
    print("CLEANED:", cleaned["choices"][0]["message"]["content"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
