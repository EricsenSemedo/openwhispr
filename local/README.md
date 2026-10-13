# Local fork (JamPC)

This fork is upstream OpenWhispr plus a small stack of local commits on the
`local` branch. The stack runs on Ericsen's PC with a local speech and
cleanup service (`%LOCALAPPDATA%\OpenWhisprLocal`, not in this repo).

## What the stack adds

| Commit | What it does |
|---|---|
| Snippets: keep literal text literal | Quotes, code, URLs and "the word X" are not expanded. A stale editor cannot overwrite snippets. |
| Local Dictation dashboard | Home screen with Overview, Snippets and History. "Settings" opens the stock panel. |
| Hold or double-tap shortcut | Opt-in (`dualShortcut` in `local-dictation-options.json`). |
| Local speech runtime | Starts and stops the local ASR supervisor with the app. |
| Wispr Flow-style paste | Cursor-aware casing, private clipboard, reviewed auto-learn. Each call falls back to stock behaviour. |
| Stock updater off | `resources/local-build.json` stops the upstream updater from replacing this build. |

Every feature talks to the local adapter on `127.0.0.1:18765` and falls back
to stock behaviour when the adapter is missing. If upstream ships an
equivalent feature, drop the matching commit.

## Update to a new upstream release

Run on Windows (PowerShell paths shown; Python 3.12, Node 22+, Git).

1. `git fetch upstream --tags` and pick the release tag, e.g. `v1.10.4`.
2. Get `OpenWhispr-Setup-<version>.exe` and its sha256 from the GitHub
   release, into a new folder under `%LOCALAPPDATA%\OpenWhisprLocal\downloads\`.
3. Unpack it (nothing is executed):
   `python -I local/official.py <setup.exe> <downloads>\official --sha256 <digest>`
4. Prove the pipeline still reproduces upstream:
   `python -I local/build.py --official <downloads>\official --ref v<version> --verify-upstream`
   Every entry must be identical. If not, fix the build first.
5. `git switch local && git rebase --onto v<version> <old tag> local`.
   Resolve conflicts, keeping each commit's intent.
6. `node --import tsx --test` on the touched tests (see each commit), then
   `cd src && npx tsc --noEmit`.
7. Build: `python -I local/build.py --official <downloads>\official --out <OpenWhisprLocal>\client\openwhispr-v<version>-local`
8. Install with `%LOCALAPPDATA%\OpenWhisprLocal\SWITCH-CLIENT.ps1` (keeps the
   old client for rollback), then dictate a test sentence.
9. Push `local` (force-with-lease after a rebase) and open a PR on the fork
   for review.

## Build notes

- The official Windows build uses CRLF sources. `build.py` checks out the
  ref in a CRLF worktree under `local/.build/` (ignored).
- The licensed Yowza fonts are copied from the official app into that
  worktree only. They are never committed (Blaze Type EULA).
- Native helpers, models and `node_modules` come from the official release,
  so no native toolchain is needed. A commit that changes native code or
  dependencies needs the full upstream build instead.
