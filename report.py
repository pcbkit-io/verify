#!/usr/bin/env python3
"""PCBKit verify: `pcbkit check` on a board repository, as a reproducible report (ADR 0108).

Python standard library only, so it runs on any CI image. Four commands:

  report.py run    [--project DIR] [--require LEVEL] [--strict] [--out report.json] [--check check.json] [--binary PCBKIT]
      Runs `pcbkit check` on a copy of DIR (or reads a saved answer with --check), writes the report
      (`pcbkit-report@1`) and its Markdown summary (to $GITHUB_STEP_SUMMARY when set, else stdout),
      and exits 0 when the gate passes, 1 when it does not, 2 when the check could not run.
  report.py verify report.json [--project DIR]
      Recomputes the report's digest, the inputs' digest from DIR's files and, when the same pcbkit
      version is on PATH, re-runs the check: the report is reproducible or it is not.
  report.py summary report.json      the Markdown summary of a report
  report.py junit report.json        the report as JUnit XML (one test case per level, then the gate)

The report shows the verdict per level, what each level judged (coverage), what the project waived
and every finding. Its body holds no time and no host: the same binary on the same files gives the
same body and the same `digest`.
"""
import argparse, hashlib, json, os, shutil, subprocess, sys, tempfile

FORMAT = "pcbkit-report@1"
LEVELS = ["language", "wiring", "ratings", "intent", "layout", "ready"]
HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = "https://github.com/pcbkit-io/verify#readme"
CHECKS_DOCS = "https://pcbkit.io/docs/format"
KICAD_EXT = (".kicad_pcb", ".kicad_sch", ".kicad_pro", ".kicad_dru", ".kicad_sym", ".kicad_mod", ".sch")
SKIP_DIRS = {".git", "node_modules", "target"}
EXPORT_WORDS = ("bom", "cpl", "pos", "placement", "pick", "centroid")


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def canonical(v):
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def is_input(rel):
    """A file `pcbkit check` reads: the KiCad project, everything in `.pcbkit/` (the design, part
    sheets, certificates, firmware facts), and a BOM or placement list exported beside the board
    (the rule of crates/pcbkit-core/src/project.rs `export_path`)."""
    parts = rel.split("/")
    if parts[0] == ".pcbkit":
        return True
    name = parts[-1].lower()
    if name.endswith(KICAD_EXT):
        return True
    return name.endswith(".csv") and any(w in name for w in EXPORT_WORDS) and not any(d in ("fab", "fabrication", "cache") for d in parts[:-1])


def inputs(project):
    """Every file that decides the verdict, with its SHA-256, and one digest over the list. Hidden
    directories are tooling (the CI's own, a vendored copy of this action), except `.pcbkit/`."""
    files = []
    for root, dirs, names in os.walk(project):
        rel_root = os.path.relpath(root, project).replace(os.sep, "/")
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and (not d.startswith(".") or (d == ".pcbkit" and rel_root == ".")) and os.path.abspath(os.path.join(root, d)) != HERE)
        for n in sorted(names):
            rel = os.path.normpath(os.path.join(rel_root, n)).replace(os.sep, "/")
            if is_input(rel):
                files.append({"path": rel, "sha256": sha256_file(os.path.join(project, rel))})
    return {"files": files, "digest": hashlib.sha256(canonical(files)).hexdigest()}


def tool(binary):
    path = shutil.which(binary)
    if not path:
        return {"name": "pcbkit", "version": None, "sha256": None}
    v = subprocess.run([path, "--version"], capture_output=True, text=True).stdout.strip().split()[-1:] or [None]
    return {"name": "pcbkit", "version": v[0], "sha256": sha256_file(path)}


def run_check(project, binary):
    """`pcbkit check` on a copy of the project: check may write `.pcbkit/` back, and the gate must
    not change the files it judges. The findings come in pages of at most 500: all are read."""
    if not shutil.which(binary):
        sys.stderr.write(f"no pcbkit at {binary}\n")
        return None
    with tempfile.TemporaryDirectory() as tmp:
        work = os.path.join(tmp, "project")
        shutil.copytree(project, work, ignore=shutil.ignore_patterns(".git"))
        first, offset = None, 0
        while True:
            p = subprocess.run([binary, "check", "--limit", "500", "--offset", str(offset)], cwd=work, capture_output=True, text=True)
            try:
                page = json.loads(p.stdout)
            except json.JSONDecodeError:
                sys.stderr.write(p.stdout[-2000:] + p.stderr[-2000:])
                return None
            r = page.get("result") or {}
            if first is None:
                first = page
            else:
                first["result"]["items"] += r.get("items", [])
            offset += len(r.get("items", []))
            if not r.get("more") or not r.get("items"):
                break
        if first and "result" in first:
            first["result"]["more"] = False
        return first


def coverage(verdict):
    """Per level, how many of the design's cells its rules judged: checked on stated facts, unknown
    (a fact a rule needs is missing), unchecked (no rule looks). A level that passes having judged
    none of its cells is `hollow`: its pass says nothing."""
    states = {l["level"]: l.get("state") for l in verdict.get("levels", [])}
    out = []
    for c in verdict.get("coverage", []):
        checked, unknown, unchecked = c.get("checked", 0), c.get("unknown", 0), c.get("unchecked", 0)
        n = checked + unknown + unchecked
        out.append({"level": c["level"], "checked": checked, "unknown": unknown, "unchecked": unchecked, "cells": n,
                    "state": states.get(c["level"]), "hollow": states.get(c["level"]) == "pass" and n > 0 and checked == 0})
    return out


def waived(verdict):
    """What the project waives: each finding the person decided, and where the decision is recorded
    (KiCad's DRC exclusions in the `.kicad_pro`, or an `@allow(...)` in the design)."""
    return [{"text": w, "source": "kicad-drc-exclusion" if "excluded in the KiCad project's DRC" in w else "design-allow"} for w in verdict.get("waived", [])]


def gate(verdict, unrouted, cov, require, strict):
    """Pass when the board is verified to `require` or beyond, with no error in the levels up to it
    and no unrouted net. `strict` also fails on unconfirmed findings (checks still on probation) and
    on a required level that passes having judged nothing."""
    levels = {l["level"]: l for l in verdict.get("levels", [])}
    verified = verdict.get("verified")
    need = LEVELS[: LEVELS.index(require) + 1]
    reasons = []
    if verified not in LEVELS or LEVELS.index(verified) < LEVELS.index(require):
        reasons.append(f"verified to {verified or 'nothing'}, the gate requires {require}")
    for lv in need:
        l = levels.get(lv, {})
        if l.get("errors"):
            reasons.append(f"{lv}: {l['errors']} errors")
        if strict and l.get("evaluating"):
            reasons.append(f"{lv}: {l['evaluating']} unconfirmed findings (strict gate)")
    if unrouted:
        reasons.append(f"{len(unrouted)} unrouted nets")
    if strict:
        reasons += [f"{c['level']}: passes with none of its {c['cells']} cells judged (strict gate)" for c in cov if c["hollow"] and c["level"] in need]
    return {"require": require, "strict": strict, "pass": not reasons, "reasons": reasons}


def build(result, project, binary, require, strict):
    verdict = result.get("verdict", {})
    findings = sorted(({"code": it.get("code"), "level": it.get("level"), "severity": it.get("severity"), "message": it.get("message"),
                        "items": it.get("items", []), "nets": it.get("nets", []), "at": it.get("at")} for it in result.get("items", [])),
                      key=lambda f: (f["level"] or "", f["code"] or "", f["items"], f["message"] or ""))
    cov = coverage(verdict)
    body = {
        "format": FORMAT,
        "tool": tool(binary),
        "inputs": inputs(project),
        "gate": gate(verdict, result.get("unrouted"), cov, require, strict),
        "verdict": verdict,
        "counts": result.get("counts", {}),
        "coverage": cov,
        "waived": waived(verdict),
        "findings": findings,
    }
    return {**body, "digest": hashlib.sha256(canonical(body)).hexdigest()}


SEVERITIES = ["error", "warning", "unconfirmed"]


def summary(rep):
    g, v = rep["gate"], rep["verdict"]
    lines = [f"## PCBKit verify: {'PASS' if g['pass'] else 'FAIL'}", "",
             f"Verified to **{v.get('verified') or 'nothing'}** (the gate requires {g['require']}{', strict' if g['strict'] else ''}). "
             f"pcbkit {rep['tool']['version']}; inputs {rep['inputs']['digest'][:12]} ({len(rep['inputs']['files'])} files); report {rep['digest'][:12]}.", ""]
    if g["reasons"]:
        lines += ["**Why it fails:** " + "; ".join(g["reasons"]), ""]
    cov = {c["level"]: c for c in rep.get("coverage") or []}
    lines += ["| level | state | errors | warnings | unconfirmed | judged (checked / unknown / unchecked cells) |", "|---|---|---|---|---|---|"]
    for l in v.get("levels", []):
        c = cov.get(l["level"])
        judged = f"{c['checked']} / {c['unknown']} / {c['unchecked']}{' **passes judging nothing**' if c['hollow'] else ''}" if c else "-"
        lines.append(f"| {l['level']} | {l.get('state')} | {l.get('errors', 0)} | {l.get('warnings', 0)} | {l.get('evaluating', 0)} | {judged} |")
    lines += ["", "_Checked: decided on facts the design states. Unknown: a rule needs a fact the board does not carry. "
              "Unchecked: no rule looks at it. A pass over few checked cells is a weak pass._"]
    wv = rep.get("waived") or []
    if wv:
        by = {}
        for w in wv:
            by[w["source"]] = by.get(w["source"], 0) + 1
        lines += ["", f"**Waived by the project** ({len(wv)}: " + ", ".join(f"{n} {'by its KiCad DRC exclusions' if s == 'kicad-drc-exclusion' else 'by @allow(...) in the design'}" for s, n in sorted(by.items()))
                  + "; the project's recorded decisions, kept in the report, not judged here):"]
        lines += [f"- {w['text'][:200]}" for w in wv[:20]] + ([f"- ... {len(wv) - 20} more in the JSON report"] if len(wv) > 20 else [])
    if rep["findings"]:
        groups = {}
        for f in rep["findings"]:
            groups.setdefault((f["severity"], f["level"] or "", f["code"] or ""), []).append(f)
        lines += ["", "| severity | level | code | n | first |", "|---|---|---|---|---|"]
        for (sev, lv, code), fs in sorted(groups.items(), key=lambda kv: (SEVERITIES.index(kv[0][0]) if kv[0][0] in SEVERITIES else 3, kv[0])):
            first = fs[0]
            lines.append(f"| {sev} | {lv} | `{code}` | {len(fs)} | {', '.join(first['items'][:4])}: {(first['message'] or '')[:120]} |")
        lines += ["", "_An unconfirmed finding comes from a check still on probation: it never fails the gate unless `strict` is on._"]
    lines += ["", f"What the levels, coverage and waivers mean, and what this report does not decide: {DOCS}. The checks and their codes: {CHECKS_DOCS}."]
    return "\n".join(lines) + "\n"


def junit(rep):
    """One test case per level, failed by its errors (each error a line), the gate as the last case."""
    from xml.sax.saxutils import escape, quoteattr
    by_level = {}
    for f in rep["findings"]:
        by_level.setdefault(f["level"], []).append(f)
    cases = []
    for l in rep["verdict"].get("levels", []):
        fs = by_level.get(l["level"], [])
        errs = [f for f in fs if f["severity"] == "error"]
        body = ""
        if errs:
            text = "\n".join(f"{f['code']}: {', '.join(f['items'][:4])}: {f['message']}" for f in errs)
            body = f"<failure message={quoteattr(f'{len(errs)} errors')}>{escape(text)}</failure>"
        note = "%d warnings and unconfirmed findings; state %s" % (len(fs) - len(errs), l.get("state"))
        cases.append(f'<testcase classname="pcbkit" name={quoteattr(l["level"])}>{body}<system-out>{escape(note)}</system-out></testcase>')
    g = rep["gate"]
    gb = "" if g["pass"] else f"<failure message={quoteattr('; '.join(g['reasons']))}/>"
    cases.append(f'<testcase classname="pcbkit" name={quoteattr("gate: " + g["require"])}>{gb}</testcase>')
    fails = sum(1 for c in cases if "<failure" in c)
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<testsuites><testsuite name="pcbkit verify" tests="{len(cases)}" failures="{fails}">'
            + "".join(cases) + "</testsuite></testsuites>\n")


def main():
    ap = argparse.ArgumentParser(description="PCBKit verify: a reproducible pre-production report of a circuit board")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--project", default=".")
    r.add_argument("--require", default="ready", choices=LEVELS)
    r.add_argument("--strict", action="store_true")
    r.add_argument("--out", default="pcbkit-report.json")
    r.add_argument("--check", help="a saved `pcbkit check` answer instead of running it")
    r.add_argument("--binary", default=os.environ.get("PCBKIT", "pcbkit"))
    v = sub.add_parser("verify")
    v.add_argument("report")
    v.add_argument("--project", default=".")
    v.add_argument("--binary", default=os.environ.get("PCBKIT", "pcbkit"))
    for name in ("summary", "junit"):
        sub.add_parser(name).add_argument("report")
    a = ap.parse_args()

    if a.cmd == "junit":
        sys.stdout.write(junit(json.load(open(a.report))))
        return 0
    if a.cmd == "summary":
        sys.stdout.write(summary(json.load(open(a.report))))
        return 0
    if a.cmd == "verify":
        rep = json.load(open(a.report))
        ok = hashlib.sha256(canonical({k: x for k, x in rep.items() if k != "digest"})).hexdigest() == rep["digest"]
        print(f"report digest: {'ok' if ok else 'MISMATCH (edited after it was made)'}")
        same_files = inputs(a.project)["digest"] == rep["inputs"]["digest"]
        print(f"inputs: {'same files' if same_files else 'DIFFERENT files from the report'}")
        t = tool(a.binary)
        if t["version"] is None or t["version"] != rep["tool"]["version"]:
            print(f"re-run: skipped (pcbkit {t['version']} on PATH, the report was made by {rep['tool']['version']})")
            return 0 if ok and same_files else 1
        res = run_check(a.project, a.binary)
        again = build(res["result"], a.project, a.binary, rep["gate"]["require"], rep["gate"]["strict"]) if res and "result" in res else None
        same = again is not None and again["digest"] == rep["digest"]
        print(f"re-run: {'reproduced (same body digest)' if same else 'NOT reproduced'}")
        return 0 if ok and same else 1

    res = json.load(open(a.check)) if a.check else run_check(a.project, a.binary)
    if not res or not res.get("ok", True) or "result" not in res:
        sys.stderr.write("pcbkit check could not run on this project\n")
        return 2
    rep = build(res["result"], a.project, a.binary, a.require, a.strict)
    with open(a.out, "w") as f:
        json.dump(rep, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    md = summary(rep)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a") as f:
            f.write(md)
    else:
        sys.stdout.write(md)
    return 0 if rep["gate"]["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
