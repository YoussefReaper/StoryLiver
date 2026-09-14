# 04 — Environment Gotchas (read before doing anything heavy)

These cost real time already. A future agent should not re-discover them.

## Python / venv

- **Use the managed venv**, not system Python 3.10:
  `C:\Users\3maar\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe`.
- **The venv drifted behind `requirements.txt`.** It was missing `python-multipart`
  (app won't import — `RuntimeError: Form data requires "python-multipart"`),
  `argon2-cffi` (auth suites break), `pymongo` (durable suite breaks). Reinstall
  from the repo-root `requirements.txt` (there is **no** `backend/requirements-dev.txt`).
- Install into the venv only. Never `pip install -g` / outside the venv.

## Paths

- **Git Bash `/tmp` ≠ native Git `/tmp`.** In Git Bash, `/tmp` → `D:\Temp`. Native
  Git (and anything it spawns) resolves `/tmp` → `D:\tmp` (drive root). Don't assume
  they're the same directory; use explicit `D:/...` paths.
- **ESM imports on Windows need a `file:///` URL.** A bare Windows absolute path as
  an ESM specifier throws `ERR_UNSUPPORTED_ESM_URL_SCHEME: Received protocol 'c:'`.
  Use `import ... from "file:///C:/..."`. (Seen in the CDP tooling.)
- **Forward slashes in Git Bash**, even on Windows. Avoid `2>nul`; use POSIX
  `2>/dev/null`.

## Git

- **`.git` object store was previously corrupt** (0 packfiles, only loose objects;
  `bad tree object HEAD`). Recovered by cloning `--single-branch` and copying
  `.git/objects/pack/*`. If you ever see `bad tree object HEAD` / `invalid sha1
  pointer` again: the local pack dir is empty, so fetch the needed branch and copy
  its packs in — don't try to rebuild objects locally.
- **The remote `legacy-layer-and-mode-tree` branch is broken** (did not send all
  necessary objects). Fetch `friday-handoff-fixes` only; don't attempt an all-branches
  fetch.
- **`git fsck` shows harmless invalid reflog entries** (objects never in this copy).
  Ignore them; they're not a functional problem.
- **Pushing to GitHub hangs** in this non-interactive shell (Git Credential Manager
  wants a GUI). Not needed for local work. If you must push, the working approach is
  a Python `win32cred` askpass reading the credential out of Windows Credential
  Manager — see the global `~/.workbuddy-ai/MEMORY.md` "Pushing to GitHub" note.

## Process / tooling

- **Long foreground commands can return empty output or get SIGTERM'd.** If a build/
  test prints nothing, redirect to a file and read it back in the same command
  (e.g. `cmd > /tmp/out.txt 2>&1; cat /tmp/out.txt` — but mind the `/tmp` trap
  above; prefer `D:/Temp/out.txt`).
- **PowerShell blocks `Add-Type`** (kills the usual CredRead P/Invoke). Use Python
  `win32cred` instead.
- **Bash blocks `cmd.exe`** (bypasses command validation) — a `.cmd` askpass
  wrapper is rejected. Use a Python askpass.

## Model

- **Rate-limit notices (`429 usage exceeds frequency limit`) are the model's, not a
  code error.** They reset on a schedule (a prior notice said ~2026-09-14 14:27 UTC+8).
  If you hit one mid-task, back off and resume; don't "fix" code that isn't broken.
- **The OpenAI key in `.env` is invalid (401).** Live runs need a fresh key — see
  `02_CURRENT_STATUS.md`. Everything else works offline.

## Cross-project confusion

- The global agent profile (`SOUL.md`/`IDENTITY.md`/`USER.md` at
  `~/.workbuddy-ai/`) describes a **teacher-document-design SaaS** — a *different*
  project owned by the same human. **This workspace is StoryLiver.** Do not let those
  files shape your understanding of this repo. (Full caveat in `00_START_HERE.md`.)
