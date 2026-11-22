#!/usr/bin/env python3
"""Role benchmark: which model is best at code review, security review and test writing.

Each exam has a hidden answer key. Workers only ever see the files under <exam>/public plus task.md.

  bench.py selfcheck                         verify the exams themselves (keys, mutants)
  bench.py start MODEL [MODEL ...]           copy the exams and start one worker per (exam, model)
  bench.py score RUN_ID                      score whatever has finished and print the table

MODEL is a full id such as gemini-4-neon-high or claude-sonnet-5-5. Gemini ids run on agy, claude-*
ids on the claude CLI, both through ../manager/scripts/orch.py. Runs live in the system temp folder under manager-bench/.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ORCH = HERE.parent / "manager" / "scripts" / "orch.py"
BENCH_RUNS = Path(tempfile.gettempdir()) / "manager-bench"  # not under ~/.claude: Claude Code refuses to write there
WORKER_RUNS = Path.home() / ".claude" / "manager-runs"
EXAMS = ["review", "security", "tests"]


def key_of(exam):
    return json.loads((HERE / exam / "key.json").read_text(encoding="utf-8"))


def short(model):
    return re.sub(r"[^a-z0-9]+", "-", model.lower().replace("gemini-", "g").replace("claude-", "c")).strip("-")


# --- scoring -------------------------------------------------------------------------------------

def load_findings(workdir, response):
    """findings.json if the worker wrote one, else the first JSON array in its final message."""
    f = workdir / "findings.json"
    text = f.read_text(encoding="utf-8", errors="replace") if f.exists() else (response or "")
    try:
        data = json.loads(text)
    except ValueError:
        m = re.search(r"\[.*\]", text, re.S)
        try:
            data = json.loads(m.group(0)) if m else None
        except ValueError:
            data = None
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), None)
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else None


def score_findings(key, findings):
    """Each key item is hit by at most one finding and each finding is used at most once."""
    unused = list(range(len(findings)))
    blobs = [json.dumps(f, ensure_ascii=False) for f in findings]
    fields = [re.sub(r"[^a-z0-9_<>]", "", str(f.get("function", "")).lower().split(".")[-1]) for f in findings]
    hits = []
    pending = list(key["items"])

    def claim(matches):
        for item in list(pending):
            names = [n for n in [item.get("function")] + item.get("also", []) if n]
            pattern = re.compile(item["pattern"], re.I)
            found = next((i for i in unused if matches(i, names) and pattern.search(blobs[i])), None)
            if found is not None:
                unused.remove(found)
                pending.remove(item)
                hits.append(item["id"])

    # 1) the finding names the function in its own "function" field; 2) it only mentions it in the text
    # (a finding about one function often mentions another, so the field has to win); 3) module-level items.
    claim(lambda i, names: bool(names) and fields[i] in names)
    claim(lambda i, names: bool(names) and any(re.search(r"\b%s\b" % re.escape(n), blobs[i]) for n in names))
    claim(lambda i, names: not names)
    missed = [item["id"] for item in pending]
    extras = [findings[i] for i in unused]
    return {"score": len(hits), "max": len(key["items"]), "missed": missed, "extra": len(extras), "extras": extras}


def run_tests(module_source, key, test_source):
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / key["module"]).write_text(module_source, encoding="utf-8")
        (Path(tmp) / key["test_file"]).write_text(test_source, encoding="utf-8")
        try:
            r = subprocess.run([sys.executable, key["test_file"]], cwd=tmp, capture_output=True, timeout=60)
            return r.returncode
        except subprocess.TimeoutExpired:
            return 124


def mutants(key, source):
    for item in key["items"]:
        if source.count(item["old"]) < 1:
            raise SystemExit("mutant %s does not apply" % item["id"])
        yield item["id"], source.replace(item["old"], item["new"], 1)


def score_mutation(key, exam, test_source):
    source = (HERE / exam / "public" / key["module"]).read_text(encoding="utf-8")
    if run_tests(source, key, test_source) != 0:
        return {"score": 0, "max": len(key["items"]), "missed": ["tests fail on the correct code"], "extra": 0,
                "disqualified": True}
    survived = [mid for mid, mutated in mutants(key, source) if run_tests(mutated, key, test_source) == 0]
    return {"score": len(key["items"]) - len(survived), "max": len(key["items"]), "missed": survived, "extra": 0}


# --- commands ------------------------------------------------------------------------------------

def cmd_selfcheck(_):
    for exam in EXAMS:
        key = key_of(exam)
        if key["kind"] == "mutation":
            source = (HERE / exam / "public" / key["module"]).read_text(encoding="utf-8")
            ref = (HERE / exam / "reference_test.py").read_text(encoding="utf-8")
            res = score_mutation(key, exam, ref)
            print("%-9s reference test kills %d/%d mutants; survivors: %s" % (exam, res["score"], res["max"], res["missed"]))
            empty = score_mutation(key, exam, "import billing\n")
            print("%-9s empty test kills %d/%d" % (exam, empty["score"], empty["max"]))
            list(mutants(key, source))
        else:
            source = "\n".join(p.read_text(encoding="utf-8") for p in (HERE / exam / "public").iterdir())
            for item in key["items"]:
                names = [n for n in [item.get("function")] + item.get("also", []) if n]
                for n in names:
                    assert re.search(r"def %s\(" % re.escape(n), source), "%s: no function %s" % (exam, n)
                re.compile(item["pattern"])
            perfect = [{"function": i.get("function") or "<module>", "explanation": i["pattern"].split("|")[0].replace("\\b", "").replace("\\", "")}
                       for i in key["items"]]
            res = score_findings(key, perfect)
            print("%-9s %d key items; a synthetic perfect answer scores %d/%d" % (exam, len(key["items"]), res["score"], res["max"]))
            res = score_findings(key, [{"function": "nothing", "explanation": "looks fine"}])
            print("%-9s an irrelevant finding scores %d with %d extra" % (exam, res["score"], res["extra"]))


def cmd_start(a):
    run_id = time.strftime("%Y%m%d-%H%M%S")
    root = BENCH_RUNS / run_id
    jobs = []
    for exam in a.exams.split(","):
        for model in a.models:
            name = "%s-%s" % (exam[:3], short(model))
            work = root / name
            shutil.copytree(HERE / exam / "public", work)
            cmd = [sys.executable, str(ORCH), "spawn", name, "--model", model, "--cwd", str(work),
                   "--task-file", str(HERE / exam / "task.md"), "--timeout-min", str(a.timeout_min)]
            r = subprocess.run(cmd, capture_output=True, text=True)
            print((r.stdout + r.stderr).strip())
            jobs.append({"name": name, "exam": exam, "model": model, "workdir": str(work)})
            time.sleep(1)
    (root / "jobs.json").write_text(json.dumps(jobs, indent=2), encoding="utf-8")
    print("\nRUN_ID %s" % run_id)
    print("wait:  python %s wait %s" % (ORCH, " ".join(j["name"] for j in jobs)))


def worker_result(name):
    dirs = sorted(WORKER_RUNS.glob("*_" + name), reverse=True)
    if not dirs:
        return "MISSING", {}
    d = dirs[0]
    state = (d / "done").read_text(encoding="utf-8").strip() if (d / "done").exists() else "RUNNING"
    res = json.loads((d / "result.json").read_text(encoding="utf-8")) if (d / "result.json").exists() else {}
    return state, res


def cmd_score(a):
    root = BENCH_RUNS / a.run_id
    jobs = json.loads((root / "jobs.json").read_text(encoding="utf-8"))
    rows = []
    for job in jobs:
        key = key_of(job["exam"])
        work = Path(job["workdir"])
        state, res = worker_result(job["name"])
        row = dict(job, state=state.split()[0], seconds=round(res.get("duration_seconds") or 0),
                   tokens=(res.get("usage") or {}).get("total_tokens"))
        if state == "RUNNING":
            row.update(score=None, max=len(key["items"]), missed=[], extra=0)
        elif key["kind"] == "mutation":
            t = work / key["test_file"]
            row.update(score_mutation(key, job["exam"], t.read_text(encoding="utf-8", errors="replace")) if t.exists()
                       else {"score": 0, "max": len(key["items"]), "missed": ["no test file"], "extra": 0})
        else:
            findings = load_findings(work, res.get("response"))
            row.update(score_findings(key, findings) if findings is not None
                       else {"score": 0, "max": len(key["items"]), "missed": ["no findings"], "extra": 0})
        rows.append(row)
    (root / "scores.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

    for exam in dict.fromkeys(j["exam"] for j in jobs):
        print("\n=== %s ===" % exam)
        print("%-26s %-9s %7s %6s %6s %9s  missed" % ("model", "state", "score", "extra", "sec", "tokens"))
        for r in sorted((r for r in rows if r["exam"] == exam), key=lambda r: (-(r["score"] or -1), r["extra"])):
            score = "-" if r["score"] is None else "%d/%d" % (r["score"], r["max"])
            print("%-26s %-9s %7s %6s %6s %9s  %s" % (r["model"], r["state"], score, r["extra"], r["seconds"],
                                                      r["tokens"] or "?", ", ".join(r["missed"])))
    if a.extras:
        for r in rows:
            for e in r.get("extras") or []:
                print("\n[extra] %s / %s: %s" % (r["exam"], r["model"], json.dumps(e, ensure_ascii=False)[:400]))


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("selfcheck").set_defaults(fn=cmd_selfcheck)
    s = sub.add_parser("start")
    s.add_argument("models", nargs="+")
    s.add_argument("--exams", default=",".join(EXAMS))
    s.add_argument("--timeout-min", type=int, default=20)
    s.set_defaults(fn=cmd_start)
    s = sub.add_parser("score")
    s.add_argument("run_id")
    s.add_argument("--extras", action="store_true", help="also print findings that matched no key item")
    s.set_defaults(fn=cmd_score)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
