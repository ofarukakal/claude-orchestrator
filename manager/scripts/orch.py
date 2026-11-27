#!/usr/bin/env python3
"""Run Antigravity CLI (agy) workers in tmux windows and collect their results.

Works the same on Windows (psmux provides `tmux`) and macOS/Linux (real tmux).
Standard library only.

  orch.py check
  orch.py spawn  NAME --tier hydrogen|neon|carbon [--effort high] --cwd DIR (--task TEXT | --task-file FILE)
                      [--allow PATH ...] [--run COMMAND ...] [--read-only] [--sandbox auto|on|off]
  orch.py status
  orch.py wait   NAME [NAME ...] [--timeout-min 9]
  orch.py result NAME [NAME ...]
  orch.py peek   NAME
  orch.py kill   NAME [NAME ...]
  orch.py down
  orch.py prune  [--days 14] [--dry-run]
  orch.py version

A job records the user's request, runs the proving commands itself and has the manager's report
audited by a model of another family. The hook refuses `git commit` and `git push` until it passes.

  orch.py job start   --cwd DIR (--request TEXT | --request-file FILE)
  orch.py job verify  [--cwd DIR] (-- COMMAND ... | --none REASON)
  orch.py job audit   [--cwd DIR] (--report TEXT | --report-file FILE) [--model ID]
  orch.py job show | gate | close   [--cwd DIR]
  orch.py job abandon [--cwd DIR] --reason TEXT      (the user's decision; the hook refuses it to Claude)
  orch.py hook-install | hook-uninstall

Watch live with:  tmux attach -t orch
"""
import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SESSION = "orch"
RUNS = Path.home() / ".claude" / "manager-runs"
JOBS = RUNS / "jobs"
JOB_TTL = 12 * 3600  # an open job older than this no longer gates anything
AUDIT_MODEL = "gemini-4-neon-high"
SETTINGS = Path.home() / ".claude" / "settings.json"
IS_WIN = os.name == "nt"
QUOTA_MARKS = ("resource_exhausted", "quota reached", "quota exceeded")
# What a read-only Claude worker may run in a shell; everything else is refused.
READ_ONLY_SHELL = ("git diff", "git log", "git show", "git status", "git blame", "git ls-files")


def find_tool(name):
    """PATH first, then the usual install spots (a tmux window or a non-login shell may lack them)."""
    found = shutil.which(name)
    if found:
        return found
    local = Path.home() / "AppData" / "Local"
    for folder in (Path.home() / ".local" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"),
                   local / "agy" / "bin", local / "Microsoft" / "WinGet" / "Links"):
        for candidate in (folder / name, folder / (name + ".exe")):
            if candidate.is_file():
                return str(candidate)
    # winget installs each tool in its own package folder; a tmux server started earlier never sees it on PATH
    packages = local / "Microsoft" / "WinGet" / "Packages"
    if packages.is_dir():
        for candidate in packages.glob("*/" + name + ".exe"):
            return str(candidate)
    return None


def tmux(*args, capture=False):
    exe = find_tool("tmux")
    if not exe:
        sys.exit("tmux not found - run the setup script in this skill's scripts folder")
    return subprocess.run([exe, *args], capture_output=capture, text=True, encoding="utf-8", errors="replace")


def session_exists():
    return tmux("has-session", "-t", SESSION, capture=True).returncode == 0


def live_windows():
    """Names of the session's windows; an empty set when the session is gone; None when tmux cannot tell."""
    r = tmux("list-windows", "-t", SESSION, "-F", "#{window_name}", capture=True)
    if r.returncode != 0:
        return None if session_exists() else set()
    return set(r.stdout.split())


def run_dir(name):
    dirs = sorted(RUNS.glob("*_" + name), reverse=True)
    if not dirs:
        sys.exit("no such worker: " + name)
    return dirs[0]


def worker_name(d):
    return d.name.split("_", 1)[1]


def state(d):
    f = d / "done"
    return f.read_text(encoding="utf-8").strip() if f.exists() else "RUNNING"


def read_json(f):
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# --- change check: what a worker did to the git working tree -----------------------------------

def git(cwd, *args, env=None):
    exe = find_tool("git")
    if not exe:
        return None
    r = subprocess.run([exe, "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", stdin=subprocess.DEVNULL, env=env)
    return r.stdout if r.returncode == 0 else None


def file_digest(f):
    try:
        if f.is_dir():
            return "dir"
        if not f.exists():
            return "absent"
        h = hashlib.sha1()
        with open(f, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return "unreadable"


def snapshot(cwd):
    """HEAD, branch, stash count and every dirty file's content hash; None outside a git repository."""
    top = git(cwd, "rev-parse", "--show-toplevel")
    if top is None:
        return None
    top = top.strip()
    files = {}
    listing = git(cwd, "status", "--porcelain", "-z", "--no-renames", "--untracked-files=all") or ""
    for entry in listing.split("\0"):
        if len(entry) > 3:
            files[entry[3:]] = entry[:2] + " " + file_digest(Path(top) / entry[3:])
    return {"top": top, "files": files,
            "head": (git(cwd, "rev-parse", "HEAD") or "").strip(),
            "branch": (git(cwd, "symbolic-ref", "-q", "--short", "HEAD") or "").strip(),
            "stashes": len((git(cwd, "stash", "list") or "").splitlines())}


def is_allowed(rel_to_top, top, cwd, patterns):
    rel = os.path.relpath(os.path.join(top, rel_to_top), cwd).replace(os.sep, "/")
    for pattern in patterns:
        pattern = pattern.replace("\\", "/").rstrip("/")
        if pattern.startswith("./"):
            pattern = pattern[2:]
        if pattern in ("", ".") or rel == pattern or rel.startswith(pattern + "/") \
                or fnmatch.fnmatchcase(rel, pattern):
            return True
    return False


def overlapping_workers(d, cwd, started):
    """Other workers that ran in the same directory while this one did; their edits look like this one's."""
    names = []
    for other in RUNS.glob("*_*"):
        if other == d or read_json(other / "meta.json").get("cwd") != cwd:
            continue
        done = other / "done"
        try:
            if not done.exists() or done.stat().st_mtime >= started:
                names.append(worker_name(other))
        except OSError:
            pass
    return sorted(names)


def change_check(before, meta, d, started):
    """Compare the working tree with the snapshot taken before the worker started."""
    cwd = meta["cwd"]
    after = snapshot(cwd)
    if before is None or after is None:
        return {"git": False}
    changed = sorted(p for p in set(before["files"]) | set(after["files"])
                     if before["files"].get(p) != after["files"].get(p))
    problems = []
    if after["head"] != before["head"]:
        problems.append("HEAD moved %s -> %s" % (before["head"][:9] or "none", after["head"][:9] or "none"))
    if after["branch"] != before["branch"]:
        problems.append("branch changed %s -> %s" % (before["branch"] or "detached", after["branch"] or "detached"))
    if after["stashes"] != before["stashes"]:
        problems.append("stash list changed (%d -> %d)" % (before["stashes"], after["stashes"]))
    outside = []
    if meta.get("read_only"):
        outside = changed
        if outside:
            problems.append("%d file(s) changed by a read-only worker" % len(outside))
    elif meta.get("allow"):
        outside = [p for p in changed if not is_allowed(p, after["top"], cwd, meta["allow"])]
        if outside:
            problems.append("%d file(s) changed outside --allow" % len(outside))
    # What each changed file looked like when this worker finished: a later edit by anyone else shows.
    digests = {p: file_digest(Path(after["top"]) / p) for p in changed}
    return {"git": True, "changed": changed, "outside": outside, "problems": problems, "digests": digests,
            "shared_with": overlapping_workers(d, cwd, started)}


def agy_profile(exe):
    """How headless agy treats shell commands on this account: "default" when it runs them, "sandbox"
    when it refuses them (the request-review preset, which business sign-ins cannot leave through
    auto mode). Found by one small probe, remembered until agy or its settings change."""
    watched = (Path(exe), Path.home() / ".gemini" / "config" / "config.json",
               Path.home() / ".gemini" / "antigravity-cli" / "settings.json")
    key = "|".join(str(int(f.stat().st_mtime)) if f.exists() else "-" for f in watched)
    cache = RUNS / ".agy-profile.json"
    cached = read_json(cache)
    if cached.get("key") == key and cached.get("profile"):
        return cached["profile"]
    with tempfile.TemporaryDirectory() as tmp:
        try:
            out = subprocess.run([exe, "-p", "Run the shell command `true`, then reply with the word ok.",
                                  "--output-format", "json", "--model", "gemini-4-hydrogen-low"],
                                 cwd=tmp, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 stdin=subprocess.DEVNULL, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            return "default"
    if '"status"' not in out.stdout:
        return "default"  # the probe itself failed (sign-in, quota): decide nothing, try again next time
    profile = "sandbox" if re.search(r'"denied_actions":\s*\[\s*\{', out.stdout) else "default"
    RUNS.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"key": key, "profile": profile}), encoding="utf-8")
    return profile


def quota_hit(d):
    for name in ("stderr.log", "events.ndjson"):
        try:
            with open(d / name, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - 20000))
                tail = fh.read().decode("utf-8", "replace").lower()
        except OSError:
            continue
        if any(mark in tail for mark in QUOTA_MARKS):
            return True
    return False


# --- commands ----------------------------------------------------------------------------------

def cmd_check(_):
    ok = True
    for tool in ("agy", "tmux"):
        path = find_tool(tool)
        print("%-5s %s" % (tool, path or "MISSING"))
        ok = ok and bool(path)
    agy = find_tool("agy")
    if agy:
        stdout, stderr = "", ""
        try:
            out = subprocess.run([agy, "models"], capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", stdin=subprocess.DEVNULL, timeout=60)
            stdout, stderr = out.stdout, out.stderr
        except subprocess.TimeoutExpired:
            stderr = "agy models timed out (probably waiting for sign-in)"
        tiers = {t: re.findall(r"^(gemini-4-%s-\w+)" % t, stdout, re.M) for t in ("hydrogen", "neon")}
        for t, slugs in tiers.items():
            print("%-8s %s" % (t, ", ".join(slugs) if slugs else "NOT AVAILABLE"))
        if not all(tiers.values()):
            print("not signed in, or the plan lacks these models")
            print(stderr.strip()[-400:])
        ok = ok and all(tiers.values())
        # Optional tier: listed for accounts signed in with a Google Cloud project.
        carbon = re.findall(r"^(gemini-4-carbon-\w+)", stdout, re.M)
        print("%-8s %s" % ("carbon", ", ".join(carbon) if carbon else "not available (optional)"))
    # Optional: only needed for claude-* workers (security review role).
    print("%-5s %s" % ("claude", find_tool("claude") or "not installed (optional; claude-* workers unavailable)"))
    sys.exit(0 if ok else 1)


def cmd_spawn(a):
    if not re.fullmatch(r"[A-Za-z0-9-]+", a.name):
        sys.exit("name may contain only letters, digits and dashes")
    if a.read_only and a.skip_permissions:
        sys.exit("--read-only and --skip-permissions cannot be combined")
    cwd = Path(a.cwd).resolve()
    if not cwd.is_dir():
        sys.exit("cwd does not exist: %s" % cwd)
    task = Path(a.task_file).read_text(encoding="utf-8") if a.task_file else a.task
    if not task or not task.strip():
        sys.exit("give --task or --task-file")
    if a.tier == "carbon" and a.effort == "xhigh" and not a.model:
        sys.exit("Carbon has no xhigh effort: use --effort high")
    job = None
    if not a.no_job and common_dir(cwd):
        job = active_job(cwd, session_id())
        if not job:
            sys.exit("no open job for this repository. Start one first:\n"
                     "  orch.py job start --cwd %s --request-file FILE   (the user's request, word for word)\n"
                     "or pass --no-job for a piece too small to be worth an audit." % cwd)
    start_worker(a.name, a.model or "gemini-4-%s-%s" % (a.tier, a.effort), cwd, task, a.timeout_min,
                 read_only=a.read_only, allow=a.allow, skip_permissions=a.skip_permissions,
                 job=job[1]["id"] if job else None, run=a.run, sandbox=a.sandbox)


def start_worker(name, model, cwd, task, timeout_min, read_only=False, allow=None, skip_permissions=False,
                 job=None, audit=False, files=None, run=None, sandbox="auto"):
    """Create the run folder and open the worker's tmux window. `{RUN_DIR}` in the task and the extra
    files is replaced by the run folder, the one place besides the project a worker can read."""
    d = RUNS / ("%s_%s" % (time.strftime("%Y%m%d-%H%M%S"), name))
    d.mkdir(parents=True)
    for fname, text in (files or {}).items():
        (d / fname).write_text(text, encoding="utf-8")
    (d / "task.md").write_text(task.replace("{RUN_DIR}", str(d)), encoding="utf-8")
    engine = "claude" if model.startswith("claude") else "agy"
    meta = {"name": name, "model": model, "engine": engine, "cwd": str(cwd), "timeout_min": timeout_min,
            "skip_permissions": skip_permissions, "read_only": read_only, "allow": allow or [],
            "job": job, "audit": audit, "run": run or [], "sandbox": sandbox}
    (d / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    me = str(Path(__file__).resolve())
    if IS_WIN:
        launcher = d / "launch.cmd"
        launcher.write_text('@echo off\r\n"%s" "%s" _worker "%s"\r\n' % (sys.executable, me, d), encoding="utf-8")
        command = 'cmd /c "%s"' % launcher
    else:
        launcher = d / "launch.sh"
        launcher.write_text("#!/bin/sh\nexec '%s' '%s' _worker '%s'\n" % (sys.executable, me, d), encoding="utf-8")
        command = "sh '%s'" % launcher

    if session_exists():
        r = tmux("new-window", "-d", "-t", SESSION, "-n", name, command)
    else:
        r = tmux("new-session", "-d", "-s", SESSION, "-n", name, command)
    if r.returncode != 0:
        sys.exit("tmux failed to start the window")
    print("%s -> %s  (%s)" % (name, model, d))
    return d


def read_event(engine, ev):
    """Echo progress to the pane; return the normalised final result when this event carries it."""
    if engine == "claude":
        kind = ev.get("type")
        if kind == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    print(block.get("text", ""), flush=True)
                elif block.get("type") == "tool_use":
                    print("> %s" % block.get("name"), flush=True)
        elif kind == "result":
            u = ev.get("usage") or {}
            total = sum(u.get(k) or 0 for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                "cache_creation_input_tokens"))
            return {"status": "ERROR" if ev.get("is_error") else "SUCCESS", "response": ev.get("result") or "",
                    "duration_seconds": (ev.get("duration_ms") or 0) / 1000.0, "num_turns": ev.get("num_turns"),
                    "usage": {"total_tokens": total}, "cost_usd": ev.get("total_cost_usd")}
        return None
    if ev.get("event") == "result":
        return ev.get("result")
    if ev.get("event") == "step_update":
        s = ev.get("step_update", {})
        if s.get("text_delta"):
            print(s["text_delta"], end="", flush=True)
        elif s.get("state") == "ACTIVE" and s.get("step_type") != "agent_response":
            print("\n> %s" % s.get("step_type"), flush=True)
    return None


def stop_tree(p):
    """Stop the worker and everything it started; a surviving child would keep its output pipe open."""
    if IS_WIN:
        p.kill()
        return
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        pass


def cmd_worker(a):
    d = Path(a.run_dir)
    try:
        run_worker(d)
    except BaseException as e:
        # Whatever happens, leave a verdict behind: a run without one looks like it is still going.
        if not (d / "done").exists():
            (d / "done").write_text("CRASHED (%s: %s)" % (type(e).__name__, e), encoding="utf-8")
        raise


def run_worker(d):
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    name, cwd = meta["name"], meta["cwd"]
    read_only, allow = meta.get("read_only"), meta.get("allow") or []
    print("[%s] model=%s cwd=%s%s" % (name, meta["model"], cwd, " read-only" if read_only else ""), flush=True)

    # The task is read from a file by the worker itself, so quoting never matters.
    prompt = (
        "Read the task file at %s and carry it out completely. "
        "Your project directory is %s - create and edit files only there, and run commands from there. "
        "The folder holding the task file is read-only metadata: never write anything into it. "
    ) % (d / "task.md", cwd)
    if read_only:
        prompt += (
            "This is a read-only task: do not create, edit, move or delete any file, and do not run any command "
            "that changes files, git state or services. Everything you found goes in your final report. "
        )
    elif allow:
        prompt += (
            "You may change only these paths (relative to the project directory): %s. If something outside them "
            "needs to change, describe the change in your report instead of making it. " % ", ".join(allow)
        )
    prompt += (
        "Do not ask questions; make reasonable choices and note them. "
        "End with a short report: what you changed, how you verified it, and anything left undone or uncertain."
    )

    engine = meta.get("engine", "agy")
    exe = find_tool(engine)
    status, code, final, check = "NO_RESULT", -1, None, None
    sandboxed = False
    if exe and engine == "agy" and not meta.get("skip_permissions"):
        choice = meta.get("sandbox") or "auto"
        sandboxed = choice == "on" or (choice == "auto" and agy_profile(exe) == "sandbox")
    if sandboxed:
        runs = meta.get("run") or []
        prompt += (
            " Shell commands run in a sandbox that cannot write files or reach the network: create and edit "
            "files with your file tools, never with shell redirection, mkdir, cp or scripts. "
            + ("These commands may run outside the sandbox: %s. " % "; ".join(runs) if runs else "")
            + "If a command you need is refused, do not work around it: name it in your report."
        )
    if not exe:
        status = "%s_NOT_FOUND" % engine.upper()
    else:
        env = None
        if engine == "claude":
            args = [exe, "-p", prompt, "--model", meta["model"], "--output-format", "stream-json", "--verbose",
                    "--add-dir", str(d)]
            if read_only:
                shell = ",".join("Bash(%s:*)" % c for c in READ_ONLY_SHELL)
                # --strict-mcp-config: no connectors either (the user's mail, drive and so on).
                args += ["--tools", "Read,Glob,Grep,Bash", "--allowedTools", "Read,Glob,Grep," + shell,
                         "--strict-mcp-config"]
            else:
                args += ["--permission-mode", "acceptEdits", "--allowedTools", "Bash,Read,Write,Edit,Glob,Grep"]
            # A worker started from inside a Claude Code session must not inherit that session's wiring.
            env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("CLAUDE", "ANTHROPIC"))}
        else:
            # agy has no switch that removes its write tools; outside the sandbox profile a read-only
            # agy worker is held to the instruction above and to the change check below.
            args = [exe, "-p", prompt, "--model", meta["model"], "--output-format", "stream-json",
                    "--add-dir", str(d), "--print-timeout", "%dm" % meta["timeout_min"]]
            if sandboxed:
                # Commands run unprompted inside the terminal sandbox; the named ones may leave it
                # (`unsandboxed` on business accounts, `command` on the others). File edits go through
                # the file tools, which accept-edits approves inside the workspace - a read-only worker
                # does not get that, so it cannot write at all.
                settings = {"toolPermission": "proceed-in-sandbox", "enableTerminalSandbox": True}
                rules = [r % c for c in meta.get("run") or [] for r in ("unsandboxed(%s)", "command(%s)")]
                if rules:
                    settings["permissions"] = {"allow": rules}
                args += ["--settings", json.dumps(settings)]
                if not read_only:
                    args += ["--mode", "accept-edits"]
        if meta.get("skip_permissions"):
            args.append("--dangerously-skip-permissions")
        started = time.time()
        before = snapshot(cwd)
        timed_out = threading.Event()
        with open(d / "stderr.log", "w", encoding="utf-8") as err, \
                open(d / "events.ndjson", "w", encoding="utf-8") as events:
            # Its own process group (not on Windows), so a timeout can stop the tools it started as well.
            p = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=err,
                                 stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                 start_new_session=not IS_WIN)

            def expire():
                timed_out.set()
                stop_tree(p)

            def closed(_signum, _frame):
                # The tmux window was closed: the group above no longer dies with it, so take it along.
                stop_tree(p)
                os._exit(1)

            if not IS_WIN:
                signal.signal(signal.SIGHUP, closed)
                signal.signal(signal.SIGTERM, closed)

            watchdog = threading.Timer(meta["timeout_min"] * 60 + 60, expire)
            watchdog.daemon = True
            watchdog.start()
            for line in p.stdout:
                events.write(line)
                events.flush()
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                final = read_event(engine, ev) or final
            code = p.wait()
            watchdog.cancel()
        if final:
            (d / "result.json").write_text(json.dumps(final, indent=2, ensure_ascii=False), encoding="utf-8")
            status = final.get("status", "UNKNOWN")
            # agy calls a turn a success even when it stopped because headless mode refused a tool.
            denied = sorted({a.get("display_name") or a.get("action") or "?" for a in final.get("denied_actions") or []})
            if denied and not (final.get("response") or "").strip():
                status = "DENIED"
        if status != "SUCCESS":
            if quota_hit(d):
                status = "QUOTA"
            elif timed_out.is_set():
                status = "TIMEOUT"
        check = change_check(before, meta, d, started)
        (d / "check.json").write_text(json.dumps(check, indent=2, ensure_ascii=False), encoding="utf-8")
    verdict = "%s exit=%s" % (status, code)
    if final and final.get("denied_actions"):
        verdict += " | DENIED: " + ", ".join(denied)
    if check and check.get("problems"):
        verdict += " | CHECK: " + "; ".join(check["problems"])
    (d / "done").write_text(verdict, encoding="utf-8")
    print("\n[%s] finished: %s" % (name, verdict), flush=True)


def cmd_status(_):
    windows = live_windows() if find_tool("tmux") else None
    for d in sorted(RUNS.glob("*_*")) if RUNS.exists() else []:
        s = state(d)
        if s == "RUNNING" and windows is not None and worker_name(d) not in windows:
            s = "RUNNING? (no tmux window - `wait` will mark it LOST)"
        print("%-40s %-26s %s" % (d.name, read_json(d / "meta.json").get("model", ""), s))


def cmd_wait(a):
    dirs = [run_dir(n) for n in a.names]
    deadline = time.time() + a.timeout_min * 60
    missing = {d: 0 for d in dirs}
    while time.time() < deadline and any(state(d) == "RUNNING" for d in dirs):
        time.sleep(5)
        # A worker whose window is gone (its own, or the whole session) will never report:
        # do not wait out the whole timeout for it.
        windows = live_windows()
        if windows is None:
            continue
        for d in dirs:
            if state(d) != "RUNNING":
                continue
            missing[d] = 0 if worker_name(d) in windows else missing[d] + 1
            if missing[d] >= 3 and state(d) == "RUNNING":
                (d / "done").write_text("LOST (tmux window ended before the worker finished)", encoding="utf-8")
    for d in dirs:
        print("%s: %s" % (d.name, state(d)))


def cmd_result(a):
    for n in a.names:
        d = run_dir(n)
        print("=== %s : %s ===" % (n, state(d)))
        r = d / "result.json"
        if r.exists():
            res = json.loads(r.read_text(encoding="utf-8"))
            print(res.get("response", "").strip())
            u = res.get("usage", {})
            print("(%.0fs, %s tokens)" % (res.get("duration_seconds", 0), u.get("total_tokens", "?")))
        else:
            print("(no result yet)")
            e = d / "stderr.log"
            if e.exists():
                print(e.read_text(encoding="utf-8", errors="replace")[-1500:])
        c = d / "check.json"
        if not c.exists():
            continue
        check = read_json(c)
        if not check.get("git"):
            print("-- change check: not a git repository, nothing compared")
            continue
        print("-- change check: %d file(s) changed in the working tree" % len(check["changed"]))
        for p in check["changed"]:
            print("   %s %s" % ("!" if p in check["outside"] else " ", p))
        for problem in check["problems"]:
            print("-- CHECK FAILED: %s" % problem)
        if check.get("shared_with"):
            print("-- note: %s ran in the same directory at the same time; some of these changes may be theirs"
                  % ", ".join(check["shared_with"]))


def cmd_peek(a):
    for n in a.names:
        print("=== %s ===" % n)
        print(tmux("capture-pane", "-p", "-t", "%s:%s" % (SESSION, n), capture=True).stdout.rstrip())


def cmd_kill(a):
    for n in a.names:
        tmux("kill-window", "-t", "%s:%s" % (SESSION, n), capture=True)
        d = run_dir(n)
        if state(d) == "RUNNING":
            (d / "done").write_text("KILLED", encoding="utf-8")
        print("%s stopped" % n)


def cmd_down(_):
    if session_exists():
        tmux("kill-session", "-t", SESSION)
        print("session closed")
    else:
        print("session already closed")


def cmd_prune(a):
    """Remove run and job records older than --days. A worker that is still running is never touched."""
    cutoff = time.time() - a.days * 86400
    for d in sorted(JOBS.glob("*")) if JOBS.exists() else []:
        started = read_json(d / "job.json").get("started")
        if started and started <= cutoff and d.is_dir() and not d.is_symlink():
            if a.dry_run:
                print("would remove job %s" % d.name)
            else:
                shutil.rmtree(d, ignore_errors=True)
    windows = (live_windows() if find_tool("tmux") else None) or set()
    removed = kept = 0
    for d in sorted(RUNS.glob("*_*")) if RUNS.exists() else []:
        try:
            started = time.mktime(time.strptime(d.name[:15], "%Y%m%d-%H%M%S"))
        except ValueError:
            continue
        if not d.is_dir() or d.is_symlink():
            continue
        if started > cutoff or (state(d) == "RUNNING" and worker_name(d) in windows):
            kept += 1
            continue
        removed += 1
        if a.dry_run:
            print("would remove %s" % d.name)
        else:
            shutil.rmtree(d, ignore_errors=True)
    print("%d run(s) %s, %d kept" % (removed, "would be removed" if a.dry_run else "removed", kept))


def cmd_version(_):
    f = Path(__file__).resolve().parent.parent / "VERSION"
    print(f.read_text(encoding="utf-8").strip() if f.exists() else "unknown (not installed with the install script)")


# --- jobs: the manager's own work is recorded, verified and audited ----------------------------

def session_id():
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or None


def common_dir(cwd):
    """The repository's shared .git folder: the same for the main checkout and every worktree of it."""
    out = git(cwd, "rev-parse", "--git-common-dir")
    if out is None:
        return None
    p = Path(out.strip())
    return str((p if p.is_absolute() else Path(cwd) / p).resolve())


def tree_id(top, scratch):
    """Id of a git tree holding the whole working tree (tracked and untracked, ignore rules applied).
    Built with a throwaway index, so neither the real index nor any file is touched. Two calls
    return the same id exactly when no file differs."""
    index = git(top, "rev-parse", "--git-path", "index")
    if index is None:
        return None
    real = Path(index.strip())
    real = real if real.is_absolute() else Path(top) / real
    tmp = Path(scratch) / ("index.%d.tmp" % os.getpid())
    try:
        if real.exists():
            shutil.copyfile(real, tmp)
        env = dict(os.environ, GIT_INDEX_FILE=str(tmp))
        if git(top, "add", "-A", env=env) is None:
            return None
        return (git(top, "write-tree", env=env) or "").strip() or None
    finally:
        for leftover in (tmp, Path(str(tmp) + ".lock")):
            try:
                leftover.unlink()
            except OSError:
                pass


def open_jobs(common, session=None):
    found = []
    for d in sorted(JOBS.glob("*"), reverse=True) if JOBS.exists() else []:
        job = read_json(d / "job.json")
        if not job or job.get("common") != common or (d / "closed").exists():
            continue
        if time.time() - job.get("started", 0) > JOB_TTL:
            continue
        if session and job.get("session") and job["session"] != session:
            continue
        found.append((d, job))
    return found


def active_job(cwd, session=None):
    common = common_dir(cwd)
    jobs = open_jobs(common, session) if common else []
    return jobs[0] if jobs else None


def need_job(cwd):
    job = active_job(Path(cwd).resolve(), session_id())
    if not job:
        sys.exit("no open job for this repository - start one with `orch.py job start`")
    return job


def records(d, kind):
    files = sorted(d.glob(kind + "-*.json"), key=lambda f: int(f.stem.rsplit("-", 1)[1]))
    return [read_json(f) for f in files]


def add_record(d, kind, data):
    data["n"] = len(records(d, kind)) + 1
    (d / ("%s-%d.json" % (kind, data["n"]))).write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                                         encoding="utf-8")
    return data["n"]


def job_workers(job_id):
    """Every worker started under this job, auditors excluded."""
    for d in sorted(RUNS.glob("*_*")) if RUNS.exists() else []:
        meta = read_json(d / "meta.json")
        if meta.get("job") == job_id and not meta.get("audit"):
            yield d, meta


def audit_outcome(audit):
    """(state, verdict, text) of an audit record."""
    rd = RUNS / audit.get("worker", "-")
    if not rd.is_dir():
        return "MISSING", None, ""
    text = read_json(rd / "result.json").get("response", "") or ""
    found = re.findall(r"VERDICT:\W*(PASS|FAIL)\b", text)
    return state(rd), (found[-1] if found else None), text.strip()


def verification_ok(v):
    return v.get("exit") == 0 or v.get("cmd") is None


def gate(d, job, tree=None):
    """Why this job's work may not be committed or pushed yet; an empty list means it may."""
    tree = tree or tree_id(job["top"], d)
    reasons = []
    verifications = records(d, "verify")
    latest = {}
    for v in verifications:
        if v.get("tree") == tree:
            latest[v.get("cmd")] = v
    if not verifications:
        reasons.append("no verification recorded: run `orch.py job verify -- <proving command>`")
    elif not latest:
        reasons.append("files changed after the last verification: run it again")
    else:
        for v in latest.values():
            if not verification_ok(v):
                reasons.append("verification #%d failed (exit %s): %s" % (v["n"], v.get("exit"), v.get("cmd")))
    audits = records(d, "audit")
    if not audits:
        reasons.append("no audit: run `orch.py job audit --report-file <your report to the user>`")
    else:
        last = audits[-1]
        worker_state, verdict, _ = audit_outcome(last)
        if worker_state == "RUNNING":
            reasons.append("the audit is still running: `orch.py wait %s`" % worker_name(RUNS / last["worker"]))
        elif not worker_state.startswith("SUCCESS"):
            reasons.append("the audit worker ended with %s: run the audit again "
                           "(with another --model if the quota ran out)" % worker_state)
        elif last.get("tree") != tree:
            reasons.append("files changed after the audit: verify and audit again")
        elif verdict != "PASS":
            reasons.append("the auditor's verdict is %s" % (verdict or "missing"))
    return reasons


AUDIT_TASK = """You are an independent auditor. A manager (an AI model) ran a job for a user in the project
directory and wrote a report about it. Your job is to find where the report and the work disagree
with each other and with what the user asked for. Assume the report may be wrong or incomplete.
This is a read-only task: change nothing.

Read these files first (they are in {RUN_DIR}):

- request.md       what the user asked for, in the user's own words
- report.md        what the manager says was done
- diff.patch       every change made to the repository during the job (the ground truth)
- verification.txt the proving commands the helper ran itself, with exit codes and output
- workers.txt      the workers the manager used, their end states and failed change checks

You may read any file in the project directory to check a claim.

%(unreviewed)s

Write your findings under these headings, each finding with the file and line it rests on, as
plain text like `calc.py:12` (no links). Write "none" under a heading that has nothing.

1. CLAIMED BUT NOT DONE - statements in report.md that diff.patch or verification.txt do not
   support. "Tests pass" without a passing run recorded on the final files belongs here.
2. ASKED BUT MISSING - parts of request.md that were neither done nor named in report.md as not
   done.
3. DONE BUT NOT REPORTED - changes in diff.patch that report.md does not mention.
4. NOT ASKED FOR - changes that request.md does not call for.
5. BUGS - real defects in the changed code (wrong behaviour, crashes, data loss, security), each
   with the function, the line and the input that triggers it. No style remarks.

Do not fix anything and do not soften anything. Your text goes to the user unedited.

End with exactly one line, `VERDICT: PASS` or `VERDICT: FAIL`. It is FAIL when heading 1 or 2 has
any finding, or when heading 3, 4 or 5 has a finding the user would want to know before accepting
the work.
"""


def cmd_job_start(a):
    cwd = Path(a.cwd).resolve()
    top = git(cwd, "rev-parse", "--show-toplevel") if cwd.is_dir() else None
    if top is None:
        sys.exit("a job needs a git repository: there is nothing to compare the work against in %s" % cwd)
    top = top.strip()
    request = Path(a.request_file).read_text(encoding="utf-8") if a.request_file else a.request
    if not request or not request.strip():
        sys.exit("give the user's request word for word with --request or --request-file")
    existing = active_job(cwd, session_id())
    if existing:
        sys.exit("job %s is already open for this repository: close it first (`orch.py job close`)"
                 % existing[1]["id"])
    job_id = time.strftime("%Y%m%d-%H%M%S")
    d = JOBS / job_id
    d.mkdir(parents=True)
    (d / "request.md").write_text(request, encoding="utf-8")
    base = tree_id(top, d)
    if not base:
        shutil.rmtree(d, ignore_errors=True)
        sys.exit("could not record the starting state of %s" % top)
    job = {"id": job_id, "cwd": str(cwd), "top": top, "common": common_dir(cwd), "session": session_id(),
           "started": time.time(), "base_tree": base, "head": (git(top, "rev-parse", "HEAD") or "").strip()}
    (d / "job.json").write_text(json.dumps(job, indent=2), encoding="utf-8")
    print("job %s opened for %s" % (job_id, top))
    print("next: spawn workers, then `job verify -- <command>`, `job audit --report-file <report>`, `job show`")


def cmd_job_verify(a):
    d, job = need_job(a.cwd)
    if a.none:
        n = add_record(d, "verify", {"cmd": None, "reason": a.none, "exit": None, "at": time.time(),
                                     "tree": tree_id(job["top"], d)})
        print("-- verify #%d: recorded that nothing proves this work (%s). The auditor will see this." % (n, a.none))
        return
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        sys.exit("give the proving command after --, or --none REASON")
    if len(cmd) == 1:
        line = cmd[0]
    else:
        line = subprocess.list2cmdline(cmd) if IS_WIN else " ".join(shlex.quote(c) for c in cmd)
    n = len(records(d, "verify")) + 1
    log = d / ("verify-%d.log" % n)
    began = time.time()
    with open(log, "w", encoding="utf-8") as out:
        try:
            code = subprocess.run(line, shell=True, cwd=job["cwd"], stdout=out, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, timeout=a.timeout_min * 60).returncode
        except subprocess.TimeoutExpired:
            code = "timeout"
    add_record(d, "verify", {"cmd": line, "exit": code, "seconds": round(time.time() - began, 1),
                             "at": time.time(), "log": log.name, "tree": tree_id(job["top"], d)})
    print("\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]))
    print("-- verify #%d: exit %s (%.0fs), recorded: %s" % (n, code, time.time() - began, line))
    sys.exit(0 if code == 0 else 1)


def cmd_job_audit(a):
    d, job = need_job(a.cwd)
    report = Path(a.report_file).read_text(encoding="utf-8") if a.report_file else a.report
    if not report or not report.strip():
        sys.exit("give the report you are about to send the user with --report or --report-file")
    top = job["top"]
    tree = tree_id(top, d)
    if not tree:
        sys.exit("could not read the current state of %s" % top)
    names = (git(top, "diff", "--name-only", "--no-renames", job["base_tree"], tree) or "").splitlines()
    patch = git(top, "diff", job["base_tree"], tree) or ""
    stat = git(top, "diff", "--stat", job["base_tree"], tree) or ""

    by_worker, workers = {}, []
    for rd, meta in job_workers(job["id"]):
        check = read_json(rd / "check.json")
        workers.append("%s  model=%s%s  %s" % (meta.get("name"), meta.get("model"),
                                               "  read-only" if meta.get("read_only") else "", state(rd)))
        workers.extend("    changed outside what it was allowed: %s" % p for p in check.get("outside") or [])
        if not meta.get("read_only"):
            for p, digest in (check.get("digests") or {}).items():
                by_worker.setdefault(p, set()).add(digest)
    # A changed file whose present content no worker left behind was written or edited by the manager.
    unreviewed = [p for p in names if file_digest(Path(top) / p) not in by_worker.get(p, ())]
    if unreviewed:
        note = ("These changed files were written or last edited by the manager itself, outside any worker "
                "run, and nobody else has read them. Review them as a code reviewer would:\n\n"
                + "\n".join("- " + p for p in unreviewed))
    else:
        note = "Every changed file was last written by a worker; none was edited by the manager afterwards."

    verification = []
    for v in records(d, "verify"):
        on_final = "on the final files" if v.get("tree") == tree else "STALE: files changed after this run"
        if v.get("cmd") is None:
            verification.append("#%d  no proving command - the manager's reason: %s  [%s]"
                                % (v["n"], v.get("reason"), on_final))
            continue
        verification.append("#%d  exit %s  [%s]  %s" % (v["n"], v.get("exit"), on_final, v["cmd"]))
        try:
            tail = (d / v["log"]).read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        except (OSError, KeyError):
            tail = []
        verification.extend("    | " + line for line in tail)

    n = len(records(d, "audit")) + 1
    (d / ("report-%d.md" % n)).write_text(report, encoding="utf-8")
    files = {
        "request.md": (d / "request.md").read_text(encoding="utf-8"),
        "report.md": report,
        "diff.patch": "%d file(s) changed:\n%s\n%s" % (len(names), stat, patch),
        "verification.txt": ("\n".join(verification) or "No verification was recorded.") + "\n",
        "workers.txt": ("\n".join(workers) or "No workers were used: the manager did all of the work itself.") + "\n",
    }
    if a.model.startswith("claude"):
        print("note: %s is the manager's own model family. Tell the user the audit was not independent "
              "in that sense." % a.model)
    name = "audit%d-%s" % (n, job["id"])
    rd = start_worker(name, a.model, top, AUDIT_TASK % {"unreviewed": note}, a.timeout_min, read_only=True,
                      job=job["id"], audit=True, files=files)
    add_record(d, "audit", {"worker": rd.name, "model": a.model, "tree": tree, "at": time.time(),
                            "unreviewed": unreviewed})
    print("next: `orch.py wait %s`, then `orch.py job show` and give the user its output unedited" % name)


def cmd_job_show(a):
    d, job = need_job(a.cwd)
    tree = tree_id(job["top"], d)
    print("=== JOB %s  %s ===" % (job["id"], job["top"]))
    print("--- the user's request, as recorded ---")
    print((d / "request.md").read_text(encoding="utf-8").strip())
    print("\n--- verification (run by the helper, not reported by the manager) ---")
    verifications = records(d, "verify")
    for v in verifications:
        fresh = "final files" if v.get("tree") == tree else "STALE"
        if v.get("cmd") is None:
            print("#%d  no proving command: %s  [%s]" % (v["n"], v.get("reason"), fresh))
        else:
            print("#%d  exit %s  [%s]  %s" % (v["n"], v.get("exit"), fresh, v["cmd"]))
    if not verifications:
        print("none")
    audits = records(d, "audit")
    if audits:
        last = audits[-1]
        worker_state, verdict, text = audit_outcome(last)
        fresh = "final files" if last.get("tree") == tree else "STALE: files changed after it"
        print("\n--- audit #%d by %s  [%s]  %s ---" % (last["n"], last.get("model"), fresh, worker_state))
        print(text or "(no text yet)")
    else:
        print("\n--- audit ---\nnone")
    reasons = gate(d, job, tree)
    print("\n--- gate ---")
    print("\n".join("BLOCKED: " + r for r in reasons) if reasons else "OPEN: verified and audited on the final files")


def cmd_job_gate(a):
    d, job = need_job(a.cwd)
    reasons = gate(d, job)
    print("\n".join("BLOCKED: " + r for r in reasons) if reasons else "OPEN")
    sys.exit(2 if reasons else 0)


def cmd_job_close(a):
    d, job = need_job(a.cwd)
    reasons = gate(d, job)
    if reasons:
        print("\n".join("BLOCKED: " + r for r in reasons))
        sys.exit("job %s stays open. Only the user can end it without an audit: `orch.py job abandon`" % job["id"])
    (d / "closed").write_text("closed %s" % time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    print("job %s closed" % job["id"])


def cmd_job_abandon(a):
    d, job = need_job(a.cwd)
    (d / "closed").write_text("abandoned %s: %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), a.reason), encoding="utf-8")
    print("job %s abandoned without passing the gate: %s" % (job["id"], a.reason))


# --- hook: Claude Code runs this before every Bash call ----------------------------------------

def git_writes(command, cwd):
    """Folders in which this shell command runs `git commit` or `git push`."""
    try:
        tokens = shlex.split(command, comments=False, posix=True)
    except ValueError:
        tokens = command.split()
    here, folders = Path(cwd), []
    for i, tok in enumerate(tokens):
        tok = tok.lstrip("(").rstrip(";")
        if tok == "cd" and i + 1 < len(tokens):
            here = (here / os.path.expanduser(tokens[i + 1].rstrip(";"))).resolve()
        if tok != "git" and not tok.endswith("/git"):
            continue
        where, rest = here, tokens[i + 1:]
        while rest:
            arg = rest.pop(0)
            if arg in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
                value = rest.pop(0) if rest else ""
                if arg == "-C":
                    where = (where / os.path.expanduser(value)).resolve()
            elif not arg.startswith("-"):
                if arg.rstrip(";") in ("commit", "push"):
                    folders.append(where)
                break
    return folders


def hook_verdict(data):
    if data.get("tool_name") != "Bash":
        return None
    command = (data.get("tool_input") or {}).get("command") or ""
    if re.search(r"orch\.py\S*\s+job\s+abandon\b", command):
        return ("`orch.py job abandon` ends a job without an audit. That is the user's decision: ask them to "
                "run it in their own terminal if they want it.")
    session = data.get("session_id") or session_id()
    for folder in git_writes(command, data.get("cwd") or os.getcwd()):
        if not folder.is_dir():
            continue
        found = active_job(folder, session)
        if not found:
            continue
        d, job = found
        reasons = gate(d, job)
        if reasons:
            return ("Manager job %s is open for %s and its work has not passed the gate:\n%s\n"
                    "Commit and push stay blocked until it does (`orch.py job show`). Only the user can lift this, "
                    "by running `orch.py job abandon --reason ...` in their own terminal."
                    % (job["id"], job["top"], "\n".join("- " + r for r in reasons)))
    return None


def cmd_hook(_):
    # Fail open: a broken hook must never stop ordinary work in every session on the machine.
    try:
        verdict = hook_verdict(json.load(sys.stdin))
    except Exception as e:
        print("manager hook error (not blocking): %s" % e, file=sys.stderr)
        sys.exit(0)
    if verdict:
        print(verdict, file=sys.stderr)
        sys.exit(2)


def hook_command():
    return '"%s" "%s" hook' % (sys.executable, Path(__file__).resolve())


def is_our_hook(entry):
    return any("orch.py" in (h.get("command") or "") and (h.get("command") or "").rstrip().endswith(" hook")
               for h in entry.get("hooks") or [])


def edit_settings(install):
    settings = {}
    if SETTINGS.exists():
        try:
            settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except ValueError:
            sys.exit("%s is not valid JSON; fix it first, nothing was changed" % SETTINGS)
        backup = SETTINGS.with_name(SETTINGS.name + ".before-manager-hook")
        if not backup.exists():
            shutil.copyfile(SETTINGS, backup)
    pre = [e for e in settings.get("hooks", {}).get("PreToolUse", []) if not is_our_hook(e)]
    if install:
        pre.append({"matcher": "Bash", "hooks": [{"type": "command", "command": hook_command(), "timeout": 60}]})
    hooks = settings.setdefault("hooks", {})
    if pre:
        hooks["PreToolUse"] = pre
    else:
        hooks.pop("PreToolUse", None)
    if not hooks:
        settings.pop("hooks")
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def cmd_hook_install(_):
    edit_settings(True)
    print("hook installed in %s: `git commit` and `git push` by Claude are refused while a manager job "
          "is open and unaudited. New sessions pick it up." % SETTINGS)


def cmd_hook_uninstall(_):
    edit_settings(False)
    print("hook removed from %s" % SETTINGS)


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("check").set_defaults(fn=cmd_check)

    s = sub.add_parser("spawn")
    s.add_argument("name")
    s.add_argument("--tier", choices=["hydrogen", "neon", "carbon"], default="hydrogen")
    s.add_argument("--effort", choices=["low", "medium", "high", "xhigh"], default="high")
    s.add_argument("--model", help="full model id; overrides --tier/--effort. A claude-* id runs on the claude CLI")
    s.add_argument("--cwd", default=".")
    s.add_argument("--task")
    s.add_argument("--task-file")
    s.add_argument("--timeout-min", type=int, default=30)
    s.add_argument("--skip-permissions", action="store_true")
    s.add_argument("--allow", action="append", metavar="PATH",
                   help="file, folder or glob (relative to --cwd) the worker may change; repeatable. "
                        "Changes elsewhere are reported as a failed check")
    s.add_argument("--run", action="append", metavar="COMMAND",
                   help="command prefix an agy worker may run outside the sandbox (tests, builds, installs: "
                        "anything that writes files or needs the network); repeatable")
    s.add_argument("--sandbox", choices=["auto", "on", "off"], default="auto",
                   help="agy only. on: commands run unprompted in the terminal sandbox, edits go through the "
                        "file tools. auto (default): on when this account refuses headless shell commands")
    s.add_argument("--no-job", action="store_true",
                   help="start a worker in a git repository without an open job (nothing will audit it)")
    s.add_argument("--read-only", action="store_true",
                   help="reviewer mode: a claude-* worker gets no write tools; any change to the working tree "
                        "is reported as a failed check")
    s.set_defaults(fn=cmd_spawn)

    w = sub.add_parser("_worker")
    w.add_argument("run_dir")
    w.set_defaults(fn=cmd_worker)

    sub.add_parser("status").set_defaults(fn=cmd_status)
    for name, fn in (("result", cmd_result), ("peek", cmd_peek), ("kill", cmd_kill)):
        x = sub.add_parser(name)
        x.add_argument("names", nargs="+")
        x.set_defaults(fn=fn)
    x = sub.add_parser("wait")
    x.add_argument("names", nargs="+")
    x.add_argument("--timeout-min", type=float, default=9)
    x.set_defaults(fn=cmd_wait)
    sub.add_parser("down").set_defaults(fn=cmd_down)
    x = sub.add_parser("prune")
    x.add_argument("--days", type=float, default=14)
    x.add_argument("--dry-run", action="store_true")
    x.set_defaults(fn=cmd_prune)
    sub.add_parser("version").set_defaults(fn=cmd_version)
    sub.add_parser("hook").set_defaults(fn=cmd_hook)
    sub.add_parser("hook-install").set_defaults(fn=cmd_hook_install)
    sub.add_parser("hook-uninstall").set_defaults(fn=cmd_hook_uninstall)

    job = sub.add_parser("job").add_subparsers(dest="job_command", required=True)
    for name, fn in (("start", cmd_job_start), ("verify", cmd_job_verify), ("audit", cmd_job_audit),
                     ("show", cmd_job_show), ("gate", cmd_job_gate), ("close", cmd_job_close),
                     ("abandon", cmd_job_abandon)):
        x = job.add_parser(name)
        x.add_argument("--cwd", default=".")
        x.set_defaults(fn=fn)
        if name == "start":
            x.add_argument("--request")
            x.add_argument("--request-file")
        elif name == "verify":
            x.add_argument("--none", metavar="REASON", help="record that nothing can prove this work, and why")
            x.add_argument("--timeout-min", type=float, default=20)
            x.add_argument("cmd", nargs=argparse.REMAINDER)
        elif name == "audit":
            x.add_argument("--report")
            x.add_argument("--report-file")
            x.add_argument("--model", default=AUDIT_MODEL)
            x.add_argument("--timeout-min", type=int, default=30)
        elif name == "abandon":
            x.add_argument("--reason", required=True)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
