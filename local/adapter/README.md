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
| `flow_*.py` | Context, style, learned pairs, cleanup model, clipboard, number/scratch/"And" rules |
| `flow_recovery.py` | Restarts `nemo` or `cleanup-llm` after the GPU device is lost |

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

Logs: the supervisor writes the adapter's output to
`profile\local-asr-logs\adapter.log`. Each JSON line carries `t`, the local
ISO 8601 time with its UTC offset. Above 2 MB the adapter copies the file to
`adapter.log.1` (keeping 3 copies) and empties it in place; `cleanup-llm.log`
belongs to llama-server and is not rotated by the adapter.

`tests/compare_endpoints.py`, `tests/compare_js_reference.py` and
`tests/history_parity.py` are manual helpers, not suites (`test_*.py`): they
diff adapters, the JS reference and a copy of the history. `local/runtime/supervisor/`
is the copy of the supervisor that runs from `%LOCALAPPDATA%\OpenWhisprLocal\runtime\supervisor\`.
