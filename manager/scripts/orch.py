#!/usr/bin/env python3
"""Run Antigravity CLI (agy) workers in tmux windows and collect their results.

Works the same on Windows (psmux provides `tmux`) and macOS/Linux (real tmux).
Standard library only.

  orch.py check
  orch.py spawn  NAME --tier hydrogen|neon [--effort high] --cwd DIR (--task TEXT | --task-file FILE)
  orch.py status
  orch.py wait   NAME [NAME ...] [--timeout-min 9]
  orch.py result NAME [NAME ...]
  orch.py peek   NAME
  orch.py kill   NAME [NAME ...]
  orch.py down

Watch live with:  tmux attach -t orch
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

SESSION = "orch"
RUNS = Path.home() / ".claude" / "manager-runs"
IS_WIN = os.name == "nt"


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


def run_dir(name):
    dirs = sorted(RUNS.glob("*_" + name), reverse=True)
    if not dirs:
        sys.exit("no such worker: " + name)
    return dirs[0]


def state(d):
    f = d / "done"
    return f.read_text(encoding="utf-8").strip() if f.exists() else "RUNNING"


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
            "skip_permissions": a.skip_permissions}
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


def cmd_worker(a):
    d = Path(a.run_dir)
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    name, cwd = meta["name"], meta["cwd"]
    print("[%s] model=%s cwd=%s" % (name, meta["model"], cwd), flush=True)

    # The task is read from a file by the worker itself, so quoting never matters.
    prompt = (
        "Read the task file at %s and carry it out completely. "
        "Your project directory is %s - create and edit files only there, and run commands from there. "
        "The folder holding the task file is read-only metadata: never write anything into it. "
        "Do not ask questions; make reasonable choices and note them. "
        "End with a short report: what you changed, how you verified it, and anything left undone or uncertain."
    ) % (d / "task.md", cwd)

    engine = meta.get("engine", "agy")
    exe = find_tool(engine)
    status, code, final = "NO_RESULT", -1, None
    if not exe:
        status = "%s_NOT_FOUND" % engine.upper()
    else:
        env = None
        if engine == "claude":
            args = [exe, "-p", prompt, "--model", meta["model"], "--output-format", "stream-json", "--verbose",
                    "--add-dir", str(d), "--permission-mode", "acceptEdits",
                    "--allowedTools", "Bash,Read,Write,Edit,Glob,Grep"]
            # A worker started from inside a Claude Code session must not inherit that session's wiring.
            env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("CLAUDE", "ANTHROPIC"))}
        else:
            args = [exe, "-p", prompt, "--model", meta["model"], "--output-format", "stream-json",
                    "--add-dir", str(d), "--print-timeout", "%dm" % meta["timeout_min"]]
        if meta.get("skip_permissions"):
            args.append("--dangerously-skip-permissions")
        with open(d / "stderr.log", "w", encoding="utf-8") as err, \
                open(d / "events.ndjson", "w", encoding="utf-8") as events:
            p = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=err,
                                 stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
            watchdog = threading.Timer(meta["timeout_min"] * 60 + 60, p.kill)
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
    (d / "done").write_text("%s exit=%s" % (status, code), encoding="utf-8")
    print("\n[%s] finished: %s (exit %s)" % (name, status, code), flush=True)


def cmd_status(_):
    for d in sorted(RUNS.glob("*_*")) if RUNS.exists() else []:
        model = ""
        try:
            model = json.loads((d / "meta.json").read_text(encoding="utf-8"))["model"]
        except Exception:
            pass
        print("%-40s %-26s %s" % (d.name, model, state(d)))


def cmd_wait(a):
    dirs = [run_dir(n) for n in a.names]
    deadline = time.time() + a.timeout_min * 60
    while time.time() < deadline and any(state(d) == "RUNNING" for d in dirs):
        time.sleep(5)
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

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
