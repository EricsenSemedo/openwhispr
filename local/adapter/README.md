# Local adapter (OpenWhisprLocal)

The Python service the `local` fork talks to on `127.0.0.1:18765`. It is
deployed by copying this folder's `*.py` and `data\` into
`%LOCALAPPDATA%\OpenWhisprLocal\adapter\`; the runtime supervisor starts it
from there (`local-asr-runtime.json`, service `adapter`). The settings live in
the profile (`profile\local-dictation-options.json`); the template here has
placeholder phone-link values.

| File | What |
|---|---|
| `local_cleanup_shim.py` | Entry point: ASR proxy, cleanup endpoint, Flow endpoints, phone link |
| `nemo_openwhispr_shim.py` | OpenAI-style `/audio/transcriptions` in front of nemo-speech (Parakeet) |
| `cleanup_filter.py` | Filler rules |
| `flow_*.py` | Context, style, learned pairs, cleanup model, clipboard, number/scratch rules |

Tests (Windows, Python 3.12, no network; the local model is never called):

```powershell
cd local\adapter\tests
foreach ($t in Get-ChildItem test_*.py) { python -I -X utf8 $t.Name }
```

Test fixtures are made-up sentences. Never paste real dictations into this
public repo; replays against real history belong outside it.

Deploy: copy the changed files into the adapter folder, then end the
`local_cleanup_shim.py` python process. The supervisor restarts it in about
2 s; OpenWhispr itself keeps running.
