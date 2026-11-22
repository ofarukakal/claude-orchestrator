#!/usr/bin/env python3
"""Run Antigravity CLI (agy) workers in tmux windows and collect their results.

Works the same on Windows (psmux provides `tmux`) and macOS/Linux (real tmux).
Standard library only.

  orch.py check
  orch.py spawn  NAME --tier hydrogen|neon [--effort high] --cwd DIR (--task TEXT | --task-file FILE)
                      [--allow PATH ...] [--read-only]
  orch.py status
  orch.py wait   NAME [NAME ...] [--timeout-min 9]
  orch.py result NAME [NAME ...]
  orch.py peek   NAME
  orch.py kill   NAME [NAME ...]
  orch.py down
  orch.py prune  [--days 14] [--dry-run]
  orch.py version

Watch live with:  tmux attach -t orch
"""
import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

SESSION = "orch"
RUNS = Path.home() / ".claude" / "manager-runs"
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

def git(cwd, *args):
    exe = find_tool("git")
    if not exe:
        return None
    r = subprocess.run([exe, "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", stdin=subprocess.DEVNULL)
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
    return {"git": True, "changed": changed, "outside": outside, "problems": problems,
            "shared_with": overlapping_workers(d, cwd, started)}


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

    d = RUNS / ("%s_%s" % (time.strftime("%Y%m%d-%H%M%S"), a.name))
    d.mkdir(parents=True)
    (d / "task.md").write_text(task, encoding="utf-8")
    model = a.model or "gemini-4-%s-%s" % (a.tier, a.effort)
    engine = "claude" if model.startswith("claude") else "agy"
    meta = {"name": a.name, "model": model, "engine": engine, "cwd": str(cwd), "timeout_min": a.timeout_min,
            "skip_permissions": a.skip_permissions, "read_only": a.read_only, "allow": a.allow or []}
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
        r = tmux("new-window", "-d", "-t", SESSION, "-n", a.name, command)
    else:
        r = tmux("new-session", "-d", "-s", SESSION, "-n", a.name, command)
    if r.returncode != 0:
        sys.exit("tmux failed to start the window")
    print("%s -> %s  (%s)" % (a.name, model, d))


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
            # agy has no switch that removes its write tools; a read-only agy worker is held to the
            # instruction above and to the change check below.
            args = [exe, "-p", prompt, "--model", meta["model"], "--output-format", "stream-json",
                    "--add-dir", str(d), "--print-timeout", "%dm" % meta["timeout_min"]]
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
        if status != "SUCCESS":
            if quota_hit(d):
                status = "QUOTA"
            elif timed_out.is_set():
                status = "TIMEOUT"
        check = change_check(before, meta, d, started)
        (d / "check.json").write_text(json.dumps(check, indent=2, ensure_ascii=False), encoding="utf-8")
    verdict = "%s exit=%s" % (status, code)
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
    """Remove run records older than --days. A worker that is still running is never touched."""
    cutoff = time.time() - a.days * 86400
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
    s.add_argument("--tier", choices=["hydrogen", "neon"], default="hydrogen")
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

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
