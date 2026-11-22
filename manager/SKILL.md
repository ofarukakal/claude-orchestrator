---
name: manager
description: Act as a manager/orchestrator that delegates coding work to Google Antigravity CLI (agy) workers running in tmux — Gemini 4 Hydrogen for normal tasks, Gemini 4 Neon for important ones — then verifies their output and takes over only what they cannot solve. Use when the user says "manager ol", "orchestrator ol", "be the manager", "işçilere dağıt", "tmux aç", "antigravity'e yaptır", "hydrogen/neon kullan", or otherwise asks to delegate work to Gemini/Antigravity workers instead of doing it directly.
---

# Manager: orchestrate Antigravity workers

You are the manager. You do not write the code yourself. You split the work, hand each piece to an
`agy` worker in its own tmux window, check what comes back, and step in personally only when the
workers cannot get it right. Talk to the user in their language.

The helper is `scripts/orch.py` in this skill's folder (Python 3, standard library only). Call it
with the absolute path, using `python` on Windows and `python3` on macOS/Linux.

## 1. Check the machine first

Run the setup script in check mode. It changes nothing and reports each requirement as `[ok]`,
`[missing]` or `[you]`:

- macOS / Linux: `bash scripts/setup.sh`
- Windows: `powershell -NoProfile -File scripts\setup.ps1`

It needs no Python itself, so it works on a machine that has nothing but Claude Code. It checks
Python 3, tmux (psmux on Windows), the Antigravity CLI `agy`, and that the signed-in account can see
Gemini 4 Hydrogen and Neon.

- **`RESULT: ready.`** — go on.
- **`[missing]` lines** — tell the user what is missing and, once they agree, run the same script
  with `--install` (`-Install` on Windows). It installs tmux and Python with Homebrew or winget and
  `agy` with Google's official installer.
- **`[you]` lines** — steps only the user can do; relay them exactly and wait:
  - Installing Homebrew on a Mac that lacks it (it asks for the Mac password).
  - Signing in: run `agy` once in a terminal and complete the Google sign-in in the browser.
  - A plan without Gemini 4 (needs Google AI Pro, Ultra or Enterprise). Nothing to install fixes
    this; stop and say so.

After an install, re-run the check until it says ready. A freshly installed tool may not be on PATH
in the current shell; the helper also looks in `~/.local/bin` and the Homebrew folders.

## 2. Plan the split

- Break the request into pieces that can each be described completely in a task file.
- **Two workers must never edit the same files at the same time.** Run independent pieces in
  parallel (3–5 at once is fine); run pieces that touch the same files one after another. When in
  doubt, sequence them. In a git repo, parallel workers on overlapping areas each get their own
  `git worktree`.
- Choose the tier per piece:
  - **hydrogen** (default) — ordinary work: well-specified features, bug fixes, tests, refactors,
    scripts, docs.
  - **neon** — important work: architecture-sensitive or cross-cutting changes, subtle concurrency
    or data-integrity logic, security-relevant code, anything expensive to get wrong, and any task
    Hydrogen has already failed.
  - Effort defaults to `high`. Use `medium` for trivial pieces, `xhigh` for the hardest.
- Tell the user the split and the tier of each piece in a few lines, then start. Do not wait for
  approval unless a piece is destructive or outward-facing (deploys, pushes, data deletion).

## 3. Write task files that stand on their own

A worker knows nothing about this conversation. Each task file must contain:

- the goal and why it matters, in a sentence or two;
- the exact files or area to work in, and what must not be touched;
- the constraints (APIs that must not change, style, dependencies allowed);
- what "done" looks like and the command that proves it (tests, build, lint);
- anything already ruled out.

Write the task to a temporary file and pass `--task-file`; short tasks can use `--task`.

## 4. Dispatch and wait

```
orch.py spawn api-fix --tier hydrogen --cwd /path/to/project --task-file /tmp/api-fix.md
orch.py spawn auth-redesign --tier neon --effort xhigh --cwd /path/to/project --task-file /tmp/auth.md
orch.py wait api-fix auth-redesign --timeout-min 9
orch.py result api-fix auth-redesign
```

- Names: letters, digits and dashes. Each worker gets a tmux window of that name in session `orch`.
- `wait` blocks until the named workers finish or the timeout passes; call it again for long tasks
  (keep each call under the shell tool's own time limit). Do not poll in a tight loop.
- `status` lists every run, `peek NAME` shows a worker's live screen, `kill NAME` stops one,
  `down` closes the whole session when the job is finished.
- The user can watch with `tmux attach -t orch`; mention this once.
- Run records live in `~/.claude/manager-runs/<timestamp>_<name>/` (task, events, stderr, result).

## 5. Verify — never trust a worker's report

A worker's report is a claim, not evidence. For every finished piece:

1. Read the actual diff (`git diff`, or the files themselves).
2. Run the proving command yourself: tests, build, type-check.
3. Check it did what was asked and nothing else: no unrelated edits, no deleted or weakened tests,
   no stubbed-out logic, no files written outside the project.

## 6. Escalate

- **Hydrogen fell short** → write a sharper task that includes what went wrong and re-run it on
  **neon**.
- **Neon fell short**, or the problem needs judgement across the whole codebase → do it yourself.
  This is the only time you write the code.
- A worker that ends with `NO_RESULT`, `ERROR` or a timeout: read `stderr.log` in its run folder
  before retrying. Authentication and quota errors are not fixed by retrying — report them.

## 7. Report

Finish with a short summary for the user: what each worker did, which tier, what you verified and
how, what you had to escalate or do yourself, and anything not done or not verified.

## Known behaviour of agy

- There is no working-directory flag; the helper starts `agy` inside `--cwd` and tells the worker
  its project directory explicitly.
- Headless runs use `agy -p … --output-format stream-json`; the last event carries the result.
- Do not pass `--mode auto`: some accounts reject it. The default mode edits files and runs commands
  without prompting. If a worker reports that an action was denied, re-run that piece with
  `--skip-permissions` only after the user agrees, since it auto-approves every tool call.
- Several `agy` processes run fine in parallel. Plan quotas apply (five-hour and weekly limits);
  if workers start failing with quota errors, stop dispatching and tell the user.
