#!/usr/bin/env python3
"""PCBKit verify: `pcbkit check` on the boards of a repository, as reproducible reports (ADR 0108).

Python standard library only, so it runs on any CI image. Commands:

  report.py run [PROJECT|GLOB ...] [--require LEVEL] [--policy gate|report|strict] [--out DIR]
                [--base DIR] [--sarif] [--annotations] [--check check.json] [--binary PCBKIT]
      Runs `pcbkit check` on a copy of each board (in parallel), writes one report per board
      (`pcbkit-report@2`, DIR/<board>.json), the combined Markdown summary (DIR/summary.md, and to
      $GITHUB_STEP_SUMMARY when set, else stdout), with --base (the same repository at the base of
      a pull request) the change per board (DIR/comment.md), with --sarif DIR/pcbkit.sarif, with
      --annotations GitHub workflow annotations. A GLOB keeps the directories that hold a board.
      Exit 0 when every board passes the gate (always under `report`), 1 when one does not, 2 when
      the check could not run on one.
  report.py verify report.json [--project DIR]
      Recomputes the report's digest, the inputs' digest from DIR's files and, when the same pcbkit
      version is on PATH, re-runs the check: the report is reproducible or it is not.
  report.py summary report.json ...  the Markdown summary of reports
  report.py junit report.json ...    the reports as JUnit XML (per board one test case per level, then the gate)
  report.py comment DIR/comment.md   posts or updates the pull request's comment (GitHub; once per PR)

A report shows the verdict per level, what each level judged (coverage), what the project waived and
every finding. Its body holds no time and no host: the same binary on the same files gives the same
body and the same `digest`. Links and pictures (the summary only, never the report) come from the CI's
environment: GITHUB_SERVER_URL, GITHUB_REPOSITORY, PCBKIT_SHA (the commit the boards are at),
PCBKIT_PUBLIC=true (the repository is public: pictures and the viewer at app.pcbkit.io).
"""
import argparse, collections, concurrent.futures, functools, glob, hashlib, json, os, shutil, subprocess, sys, tempfile, time, urllib.error, urllib.parse, urllib.request

FORMAT = "pcbkit-report@2"
LEVELS = ["language", "wiring", "ratings", "intent", "layout", "ready"]
POLICIES = ["gate", "report", "strict"]
SEVERITIES = ["error", "warning", "unconfirmed"]
HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = "https://github.com/pcbkit-io/verify#readme"
CHECKS_DOCS = "https://pcbkit.io/docs/format"
APP = "https://app.pcbkit.io"
MARKER = "<!-- pcbkit-verify -->"
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


def is_board(d):
    """A directory with a board: a KiCad board or project, or a PCBKit design."""
    try:
        names = os.listdir(d)
    except OSError:
        return False
    return any(n.endswith((".kicad_pro", ".kicad_pcb")) for n in names) or os.path.isfile(os.path.join(d, ".pcbkit", "design.pcbkit"))


def projects(patterns):
    """The boards the patterns name, in order, once each: a path as it is, a glob (`boards/*`,
    `hardware/**`) the directories under it that hold a board. Patterns split on spaces and lines."""
    out = []
    for pat in (p for s in patterns for p in s.split()):
        if glob.has_magic(pat):
            found = [p for p in sorted(glob.glob(pat.rstrip("/"), recursive=True)) if os.path.isdir(p) and is_board(p)]
        else:
            found = [pat]
        for p in found:
            p = os.path.normpath(p).replace(os.sep, "/")
            if p not in out:
                out.append(p)
    return out


@functools.lru_cache(maxsize=None)
def tool(binary):
    path = shutil.which(binary)
    if not path:
        return {"name": "pcbkit", "version": None, "sha256": None}
    v = subprocess.run([path, "--version"], capture_output=True, text=True).stdout.strip().split()[-1:] or [None]
    return {"name": "pcbkit", "version": v[0], "sha256": sha256_file(path)}


def run_check(project, binary):
    """`pcbkit check` on a copy of the project: check may write `.pcbkit/` back, and the gate must
    not change the files it judges. The findings come in pages of at most 500: all are read.
    Returns (answer, None) or (None, why it could not run)."""
    exe = shutil.which(binary)
    if not exe:
        return None, f"no pcbkit at {binary}"
    exe = os.path.abspath(exe)
    if not os.path.isdir(project):
        return None, f"no directory {project}"
    def page(work, offset):
        p = subprocess.run([exe, "check", "--limit", "500", "--offset", str(offset)], cwd=work, capture_output=True, text=True)
        try:
            ans = json.loads(p.stdout)
        except json.JSONDecodeError:
            return None, (p.stderr.strip() or p.stdout.strip())[-600:] or f"pcbkit check exited {p.returncode} with no answer"
        if not ans.get("ok", True) or "result" not in ans:
            e = ans.get("error")
            return None, (e.get("message") if isinstance(e, dict) else None) or str(e or ans.get("summary") or "pcbkit check answered no result")
        return ans, None
    with tempfile.TemporaryDirectory() as tmp:
        work = os.path.join(tmp, "project")
        shutil.copytree(project, work, ignore=shutil.ignore_patterns(".git"), symlinks=True)
        first, why = page(work, 0)
        if first is None:
            return None, why
        # Each page is a whole check: once the first gives the total, the others run at once, each
        # on its own copy (a check may write `.pcbkit/` back).
        r = first["result"]
        n, total = len(r.get("items", [])), r.get("total") or 0
        offsets = list(range(n, total, 500)) if r.get("more") and n else []
        def rest(offset):
            w = os.path.join(tmp, f"page-{offset}")
            shutil.copytree(project, w, ignore=shutil.ignore_patterns(".git"), symlinks=True)
            return page(w, offset)
        with concurrent.futures.ThreadPoolExecutor(max(1, len(offsets))) as ex:
            for ans, why in ex.map(rest, offsets):
                if ans is None:
                    return None, why
                r["items"] += ans["result"].get("items", [])
        first["result"]["more"] = False
        return first, None


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


def gate(verdict, unrouted, cov, require, policy):
    """Pass when the board is verified to `require` or beyond, with no error in the levels up to it
    and no unrouted net. `strict` also fails on unconfirmed findings (checks still on probation) and
    on a required level that passes having judged nothing. `report` judges as `gate` does; only the
    job's exit differs (it never fails)."""
    strict = policy == "strict"
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
            reasons.append(f"{lv}: {l['evaluating']} unconfirmed findings (strict)")
    if unrouted:
        reasons.append(f"{len(unrouted)} unrouted nets")
    if strict:
        reasons += [f"{c['level']}: passes with none of its {c['cells']} cells judged (strict)" for c in cov if c["hollow"] and c["level"] in need]
    return {"require": require, "policy": policy, "pass": not reasons, "reasons": reasons}


FIELDS = ("code", "level", "severity", "message", "items", "ids", "nets", "at", "file", "line", "column")


def build(result, project, binary, require, policy, label=None):
    """The report of `pcbkit check`'s answer on the files at `project`, named `label` (the board's
    directory in the repository; default `project`)."""
    verdict = result.get("verdict", {})
    findings = sorted(({k: it.get(k, [] if k in ("items", "ids", "nets") else None) for k in FIELDS} for it in result.get("items", [])),
                      key=lambda f: (f["level"] or "", f["code"] or "", f["items"], f["message"] or ""))
    cov = coverage(verdict)
    body = {
        "format": FORMAT,
        "project": label or project,
        "tool": tool(binary),
        "inputs": inputs(project),
        "gate": gate(verdict, result.get("unrouted"), cov, require, policy),
        "verdict": verdict,
        "counts": result.get("counts", {}),
        "unrouted": result.get("unrouted") or [],
        "coverage": cov,
        "waived": waived(verdict),
        "findings": findings,
    }
    return {**body, "digest": hashlib.sha256(canonical(body)).hexdigest()}


def failed(project, why, require, policy):
    """A board the check could not run on: no verdict, the gate fails, the reason kept."""
    return {"format": FORMAT, "project": project, "error": why, "gate": {"require": require, "policy": policy, "pass": False, "reasons": [f"pcbkit check could not run: {why}"]},
            "verdict": {}, "coverage": [], "waived": [], "findings": [], "unrouted": []}


def check_one(project, binary, require, policy, saved=None):
    res, why = (saved, None) if saved is not None else run_check(project, binary)
    if res is None:
        return failed(project, why, require, policy)
    return build(res["result"], project, binary, require, policy)


def check_all(boards, binary, require, policy, jobs=None):
    """Every board's report, the checks in parallel (each check is one process on a copy)."""
    jobs = jobs or min(len(boards), os.cpu_count() or 2) or 1
    def size(p):
        return sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(p) for f in fs if f.endswith(".kicad_pcb"))
    # The largest boards first: the run lasts as long as its longest check.
    order = sorted(range(len(boards)), key=lambda i: -size(boards[i]) if os.path.isdir(boards[i]) else 0)
    with concurrent.futures.ThreadPoolExecutor(jobs) as ex:
        done = dict(zip(order, ex.map(lambda i: check_one(boards[i], binary, require, policy), order)))
    return [done[i] for i in range(len(boards))]


# ── Reading a report ────────────────────────────────────────────────────────────────────────────

def counts(rep):
    """errors, warnings, unconfirmed over the whole board."""
    c = collections.Counter(f["severity"] for f in rep["findings"])
    return c["error"], c["warning"], c["unconfirmed"]


def status(rep):
    """fail; review (passes, but with unconfirmed findings or a required level that judged nothing);
    pass."""
    if not rep["gate"]["pass"]:
        return "fail"
    need = LEVELS[: LEVELS.index(rep["gate"]["require"]) + 1]
    if counts(rep)[2] or any(c["hollow"] and c["level"] in need for c in rep["coverage"]):
        return "review"
    return "pass"


BADGE = {"fail": "🔴 fail", "review": "🟡 review", "pass": "🟢 pass"}


def rank(rep):
    """Worst first: failing, then lowest level, most errors, most unconfirmed, most warnings."""
    v = rep["verdict"].get("verified")
    e, w, u = counts(rep)
    return (["fail", "review", "pass"].index(status(rep)), LEVELS.index(v) if v in LEVELS else -1, -e, -u, -w, rep["project"])


def name(rep):
    p = rep["project"]
    return p if p != "." else os.path.basename(os.getcwd())


def file_id(project):
    return (project.strip("./").replace("/", "--") or os.path.basename(os.getcwd()) or "board")


def key(f):
    """A finding's identity across commits: its code and the items it names (messages carry numbers)."""
    return (f["code"], tuple(sorted(f["ids"] or f["items"])))


# ── Where the boards are (the CI's environment) ────────────────────────────────────────────────

def context(env=None):
    env = os.environ if env is None else env
    server, repo, sha = env.get("GITHUB_SERVER_URL", ""), env.get("GITHUB_REPOSITORY", ""), env.get("PCBKIT_SHA") or env.get("GITHUB_SHA", "")
    on_github = bool(server and repo and sha)
    public = on_github and server == "https://github.com" and env.get("PCBKIT_PUBLIC") == "true"
    return {"repo": f"{server}/{repo}" if on_github else None, "slug": repo, "sha": sha, "public": public, "base_sha": env.get("PCBKIT_BASE_SHA") or None, "unchanged": set()}


def under(project, rel):
    return rel if project in (".", "") else f"{project}/{rel}"


def board_url(ctx, project):
    return f"{ctx['repo']}/tree/{ctx['sha']}/{project}" if ctx["repo"] else None


def file_url(ctx, project, f):
    return f"{ctx['repo']}/blob/{ctx['sha']}/{under(project, f['file'])}#L{f['line']}" if ctx["repo"] and f.get("file") and f.get("line") else None


def viewer_url(ctx, project, ids=()):
    """The board in PCBKit's viewer (public repositories), the finding's items selected and framed."""
    if not ctx["public"]:
        return None
    base = f"{APP}/github/{ctx['slug']}/tree/{ctx['sha']}" + ("" if project in (".", "") else f"/{project}")
    ids = [i for i in ids if i][:20]
    return base + (f"?view=2d&select={urllib.parse.quote(','.join(ids), safe=':,[]')}" if ids else "")


def picture_url(ctx, project):
    """The board's picture as PCBKit draws it per commit (public repositories)."""
    if not ctx["public"]:
        return None
    # A board a pull request leaves as it is: its picture at the base, drawn when the base was checked.
    sha = ctx["base_sha"] if ctx["base_sha"] and project in ctx["unchanged"] else ctx["sha"]
    return f"{APP}/embed/github/{ctx['slug']}/tree/{sha}" + ("" if project in (".", "") else f"/{project}") + ".webp?size=s&mark=0"


def warm(urls, wait=90):
    """Ask for each picture before the summary shows it, so the first reader gets the board, not
    the stand-in (an SVG) the server answers while it draws. Best effort, in parallel, `wait`
    seconds for all of them; a picture the server refuses (no such board) is not asked again."""
    end = time.time() + wait
    def one(u):
        while time.time() < end:
            try:
                with urllib.request.urlopen(urllib.request.Request(u, headers={"user-agent": "pcbkit-verify"}), timeout=max(1, end - time.time())) as r:
                    if "svg" not in r.headers.get("content-type", ""):
                        return True
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    return False
            except Exception:
                pass
            time.sleep(3)
        return False
    if not urls:
        return 0
    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        return sum(ex.map(one, urls))


# ── The summary ─────────────────────────────────────────────────────────────────────────────────

def cell(s, n=160):
    s = " ".join(str(s or "").split())
    s = s if len(s) <= n else s[: n - 1] + "…"
    return s.replace("|", "\\|").replace("<", "&lt;")


def link(text, url):
    return f"[{text}]({url})" if url else text


def judged(rep):
    """`wiring 5/11 · ratings 1/4 · ready 0/9`: cells a level's rules decided, of its cells; bold
    where a passing level judged none."""
    out = []
    for c in rep["coverage"]:
        s = f"{c['level']} {c['checked']}/{c['cells']}"
        out.append(f"**{s}**" if c["hollow"] else s)
    return " · ".join(out) or "-"


def top_codes(rep, severity, n=3):
    c = collections.Counter(f["code"] for f in rep["findings"] if f["severity"] == severity)
    return ", ".join(f"`{k}` ×{v}" if v > 1 else f"`{k}`" for k, v in c.most_common(n))


def why(rep):
    if rep.get("error"):
        return "check could not run"
    if rep["gate"]["pass"]:
        need = LEVELS[: LEVELS.index(rep["gate"]["require"]) + 1]
        hollow = [c["level"] for c in rep["coverage"] if c["hollow"] and c["level"] in need]
        return top_codes(rep, "unconfirmed", 2) or (f"{', '.join(hollow)} judged none of {'its' if len(hollow) == 1 else 'their'} cells" if hollow else "")
    return top_codes(rep, "error") or "; ".join(rep["gate"]["reasons"])


def where(ctx, rep, f):
    if f.get("file") and f.get("line"):
        return link(f"{f['file'].split('/')[-1]}:{f['line']}", file_url(ctx, rep["project"], f))
    if f.get("at"):
        return f"({f['at'][0]:.2f}, {f['at'][1]:.2f}) mm"
    return ", ".join(f["items"][:3]) or "-"


def finding_rows(ctx, rep, fs, n):
    """One row per code, worst first: how many, and the first of them (where, what, a link that
    opens the board with its items selected)."""
    groups = {}
    for f in ordered(fs):
        groups.setdefault((f["severity"], f["code"]), []).append(f)
    lines = ["| | code | n | first: where | what | |", "|---|---|--:|---|---|---|"]
    for (sev, code), g in list(groups.items())[:n]:
        f = g[0]
        ids = f["ids"] or [f"net:{x}" for x in f["nets"]]
        open_ = link("open", viewer_url(ctx, rep["project"], ids)) if ids and ctx["public"] else ""
        lines.append(f"| {sev} | `{code}` | {len(g)} | {where(ctx, rep, f)} | {cell(f['message'])} | {open_} |")
    if len(groups) > n:
        rest = sum(len(g) for g in list(groups.values())[n:])
        lines.append(f"| | … {len(groups) - n} more codes | {rest} | | in the board's report | |")
    return lines


def ordered(fs):
    return sorted(fs, key=lambda f: (SEVERITIES.index(f["severity"]) if f["severity"] in SEVERITIES else 3, LEVELS.index(f["level"]) if f["level"] in LEVELS else 9, f["code"] or ""))


def card(rep):
    """The engineer's checklist: one line per question, with its state."""
    levels = {l["level"]: l for l in rep["verdict"].get("levels", [])}
    fs = rep["findings"]
    n = collections.Counter(f["code"] for f in fs)
    def lv(name):
        l = levels.get(name) or {}
        st = l.get("state") or "-"
        bits = [f"{l[k]} {w}" for k, w in (("errors", "errors"), ("warnings", "warnings"), ("evaluating", "unconfirmed")) if l.get(k)]
        return f"{st}{': ' + ', '.join(bits) if bits else ''}"
    cov = {c["level"]: c for c in rep["coverage"]}
    def judged_line(name):
        c = cov.get(name)
        return f" ({c['checked']} of {c['cells']} cells judged)" if c else ""
    tick = lambda ok: "✅" if ok else "⚠️"
    ready = levels.get("ready") or {}
    parts_named = n["part-missing"]
    parts_bought = n["part-maker"]
    lines = [
        f"- {tick(levels.get('layout', {}).get('state') == 'pass')} **Layout** (the board under its own rules): {lv('layout')}",
        f"- {tick(ready.get('state') == 'pass' and not ready.get('evaluating'))} **Fab pre-check** (ready): {lv('ready')}{judged_line('ready')}",
        f"- {tick(not parts_named)} **Parts named**: {f'{parts_named} without a part to buy (`part-missing`)' if parts_named else 'every part names what to buy'}",
        f"- {tick(not parts_bought)} **Parts as bought**: {f'{parts_bought} bought from another maker than their sheet (`part-maker`)' if parts_bought else 'each bought part is the part its sheet describes'}",
        f"- {tick(not rep['unrouted'])} **Routing**: {len(rep['unrouted'])} unrouted nets",
        f"- {tick(levels.get('wiring', {}).get('state') == 'pass')} **Wiring and ratings**: wiring {lv('wiring')}{judged_line('wiring')}; ratings {lv('ratings')}{judged_line('ratings')}",
    ]
    if rep["waived"]:
        lines.append(f"- ℹ️ **Waived by the project**: {len(rep['waived'])} findings (kept in the report)")
    return lines


def details(ctx, rep, n=10, pictures=True):
    e, w, u = counts(rep)
    v = rep["verdict"].get("verified") or "nothing"
    head = f"{BADGE[status(rep)]} <b>{cell(name(rep))}</b> · verified to {v} · {e} errors, {w} warnings, {u} unconfirmed"
    lines = ["<details>", f"<summary>{head}</summary>", ""]
    if rep.get("error"):
        lines += [f"The check could not run on this board: `{cell(rep['error'], 400)}`", ""]
    else:
        pic, view = picture_url(ctx, rep["project"]), viewer_url(ctx, rep["project"])
        if pic and pictures:
            lines += [f'<a href="{view}"><img src="{pic}" width="420" alt="{cell(name(rep))}"></a>', ""]
        if rep["gate"]["reasons"]:
            lines += ["**Why it fails:** " + "; ".join(rep["gate"]["reasons"]), ""]
        lines += card(rep) + [""]
        if rep["findings"]:
            lines += finding_rows(ctx, rep, ordered(rep["findings"]), n) + [""]
    lines += ["Reproduce on your machine (the same files and release give the same report digest):", "", "```sh",
              f"cd {rep['project']}" if rep["project"] not in (".", "") else "# in the repository", "pcbkit check", "```",
              f"Report `{file_id(rep['project'])}.json`" + (f", digest `{rep['digest'][:12]}`, inputs `{rep['inputs']['digest'][:12]}` ({len(rep['inputs']['files'])} files)." if rep.get("digest") else "."),
              "", "</details>", ""]
    return lines


def summary(reps, ctx=None, title="PCBKit verify", pictures=True):
    ctx = ctx or context({})
    reps = sorted(reps, key=rank)
    if not reps:
        return f"## {title}: no boards\n\nNo directory matched holds a board (a `.kicad_pro`, a `.kicad_pcb` or `.pcbkit/design.pcbkit`).\n"
    g = reps[0]["gate"]
    st = collections.Counter(status(r) for r in reps)
    nfail = st["fail"]
    head = f"## {title}: {len(reps)} board{'s' if len(reps) != 1 else ''}, {len(reps) - nfail} pass{'es' if len(reps) - nfail == 1 else ''} `require: {g['require']}`, {nfail} fail{'s' if nfail == 1 else ''}"
    mode = {"report": "Report mode: the job passes whatever the boards show.", "strict": "Strict: unconfirmed findings and levels that judged nothing fail too.", "gate": "The job fails when a board fails."}[g["policy"]]
    tools = sorted({(r.get("tool") or {}).get("version") or "?" for r in reps if not r.get("error")})
    at = collections.Counter(r["verdict"].get("verified") or "nothing" for r in reps)
    spread = " · ".join(f"**{lv}** {at[lv]}" for lv in ["nothing"] + LEVELS if at[lv])
    lines = [head, "", f"{mode} pcbkit {', '.join(tools) or '?'}. Verified to: {spread}.", ""]
    if pictures and ctx["public"] and len(reps) > 1:
        lines += grid(ctx, reps) + [""]
    waives = any(r["waived"] for r in reps)
    lines += ["| | board | verified to | errors | warnings | unconfirmed |" + (" waived |" if waives else "") + " judged (checked/cells) | why |",
              "|---|---|---|--:|--:|--:|" + ("--:|" if waives else "") + "---|---|"]
    for r in reps:
        e, w, u = counts(r)
        lines.append(f"| {BADGE[status(r)]} | {link(cell(name(r), 60), board_url(ctx, r['project']))} | {r['verdict'].get('verified') or '-'} | {e} | {w} | {u} |"
                     + (f" {len(r['waived'])} |" if waives else "") + f" {judged(r)} | {why(r)} |")
    lines += ["", "_🔴 fails the gate · 🟡 passes, with unconfirmed findings (checks on probation) or a required level that judged nothing · 🟢 passes. "
              "Judged: of each level's cells, how many its rules decided on stated facts; **bold**: the level passes having judged none._", ""]
    for r in reps:
        lines += details(ctx, r, pictures=pictures)
    lines += [f"What the levels, coverage and waivers mean, and what a report does not decide: {DOCS}. The checks and their codes: {CHECKS_DOCS}."]
    return "\n".join(lines) + "\n"


def grid(ctx, reps, per_row=8):
    """The library at a glance: each board's picture, its name and state, worst first."""
    cells = [f'<td align="center" valign="top"><a href="{viewer_url(ctx, r["project"])}"><img src="{picture_url(ctx, r["project"])}" width="104" alt=""></a><br><sub>{BADGE[status(r)].split()[0]} {cell(name(r).split("/")[-1], 28)}<br>{r["verdict"].get("verified") or "-"}</sub></td>'
             for r in reps if not r.get("error")]
    rows = ["<tr>" + "".join(cells[i: i + per_row]) + "</tr>" for i in range(0, len(cells), per_row)]
    return ["<table>"] + rows + ["</table>"]


# ── A pull request: what the change did to each board ───────────────────────────────────────────

def delta(base, head):
    """New and fixed findings (by code and items), and the level before and after."""
    b = collections.Counter(key(f) for f in base["findings"]) if base else collections.Counter()
    h = collections.Counter(key(f) for f in head["findings"])
    new_keys, fixed_keys = h - b, b - h
    def pick(fs, want):
        out, want = [], collections.Counter(want)
        for f in fs:
            if want[key(f)] > 0:
                want[key(f)] -= 1
                out.append(f)
        return ordered(out)
    return {"project": head["project"], "before": (base or {}).get("verdict", {}).get("verified") if base else None, "after": head["verdict"].get("verified"),
            "new": pick(head["findings"], new_keys), "fixed": pick(base["findings"], fixed_keys) if base else [], "added": base is None,
            "unrouted": sorted(nets(head) - nets(base)), "routed": sorted(nets(base) - nets(head))}


def nets(rep):
    return {u.get("net") if isinstance(u, dict) else str(u) for u in (rep or {}).get("unrouted", [])}


def comment(ctx, deltas, reps, unchanged):
    """The pull request's comment: per board this change touched, its level and its new and fixed
    findings."""
    by = {r["project"]: r for r in reps}
    lines = [MARKER, "### PCBKit verify: what this change does to the boards", ""]
    moved = [d for d in deltas if d["added"] or d["new"] or d["fixed"] or d["unrouted"] or d["routed"] or d["before"] != d["after"]]
    if not moved:
        lines += [f"No board's verdict changed ({len(deltas)} changed board{'s' if len(deltas) != 1 else ''} checked, {unchanged} untouched)."]
        return "\n".join(lines) + "\n"
    def lvl(d):
        if d["added"]:
            return f"new board, {d['after'] or 'nothing'}"
        if d["before"] == d["after"]:
            return d["after"] or "nothing"
        up = LEVELS.index(d["after"]) > LEVELS.index(d["before"]) if d["after"] in LEVELS and d["before"] in LEVELS else d["after"] in LEVELS
        return f"{d['before'] or 'nothing'} → {d['after'] or 'nothing'} {'⬆️' if up else '⬇️'}"
    def sev(fs, unrouted=()):
        c = collections.Counter(f["severity"] for f in fs)
        out = [f"{c[s]} {s}{'s' if c[s] != 1 and s != 'unconfirmed' else ''}" for s in SEVERITIES if c[s]]
        if unrouted:
            out.append(f"{len(unrouted)} unrouted net{'s' if len(unrouted) != 1 else ''} ({cell(', '.join(unrouted[:4]), 60)})")
        return ", ".join(out) or "-"
    lines += ["| | board | level | new | fixed |", "|---|---|---|---|---|"]
    for d in sorted(moved, key=lambda d: rank(by[d["project"]])):
        lines.append(f"| {BADGE[status(by[d['project']])]} | {link(cell(d['project'], 60), board_url(ctx, d['project']))} | {lvl(d)} | {sev(d['new'], d['unrouted'])} | {sev(d['fixed'], d['routed'])} |")
    lines.append("")
    for d in sorted(moved, key=lambda d: rank(by[d["project"]])):
        r = by[d["project"]]
        if not (d["new"] or d["fixed"]):
            continue
        lines += ["<details>", f"<summary><b>{cell(d['project'])}</b>: {len(d['new'])} new, {len(d['fixed'])} fixed</summary>", ""]
        pic = picture_url(ctx, d["project"])
        if pic:
            lines += [f'<a href="{viewer_url(ctx, d["project"])}"><img src="{pic}" width="360" alt=""></a>', ""]
        if d["new"]:
            lines += ["**New**", ""] + finding_rows(ctx, r, d["new"], 10) + [""]
        if d["fixed"]:
            lines += ["**Fixed**", ""] + [f"- `{f['code']}` {cell(', '.join(f['items'][:4]), 80)}: {cell(f['message'], 140)}" for f in d["fixed"][:10]]
            lines += ([f"- … {len(d['fixed']) - 10} more"] if len(d["fixed"]) > 10 else []) + [""]
        lines += ["</details>", ""]
    lines.append(f"_{unchanged} boards untouched by this change. Updated on every push; the run's summary has every board._")
    return "\n".join(lines) + "\n"


def post_comment(path, env=None):
    """Create the pull request's comment, or update the one this action made before: one per PR.
    A token that may not write (a fork's pull request) gets a warning, not a failure."""
    env = os.environ if env is None else env
    body = open(path).read()
    api, repo, pr, token = env.get("GITHUB_API_URL", "https://api.github.com"), env.get("GITHUB_REPOSITORY"), env.get("PCBKIT_PR"), env.get("GITHUB_TOKEN")
    if not (repo and pr and token):
        return 0
    def call(method, url, data=None):
        req = urllib.request.Request(url, method=method, data=json.dumps(data).encode() if data else None,
                                     headers={"authorization": f"Bearer {token}", "accept": "application/vnd.github+json", "user-agent": "pcbkit-verify"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    try:
        mine = None
        for page in range(1, 11):
            got = call("GET", f"{api}/repos/{repo}/issues/{pr}/comments?per_page=100&page={page}")
            mine = mine or next((c for c in got if MARKER in (c.get("body") or "")), None)
            if mine or len(got) < 100:
                break
        nothing = "No board's verdict changed" in body
        if mine:
            call("PATCH", f"{api}/repos/{repo}/issues/comments/{mine['id']}", {"body": body})
        elif not nothing:
            call("POST", f"{api}/repos/{repo}/issues/{pr}/comments", {"body": body})
    except Exception as e:
        print(f"::warning title=PCBKit verify::could not write the pull request's comment ({e}); the job's summary has it (a fork's pull request has no write token)")
    return 0


# ── Annotations, SARIF, JUnit ───────────────────────────────────────────────────────────────────

def board_file(rep):
    """The KiCad board file the board's findings without a line belong to."""
    names = [f["path"] for f in (rep.get("inputs") or {}).get("files", []) if f["path"].endswith(".kicad_pcb") and "/" not in f["path"]]
    return under(rep["project"], names[0]) if names else None


def located(rep, f):
    """(file in the repository, line, column) of a finding: the design's own line where the check
    gives one, else the board file."""
    if f.get("file") and f.get("line"):
        return under(rep["project"], f["file"]), f["line"], f.get("column")
    return board_file(rep), None, None


def annotations(reps, only=None, cap=10):
    """GitHub workflow annotations, worst first, a few per severity (GitHub shows ten of each per
    step): `only` limits them to these findings (a pull request's new ones)."""
    kinds = {"error": "error", "warning": "warning", "unconfirmed": "notice"}
    left = {k: cap for k in kinds}
    out = []
    pool = []
    for r in sorted(reps, key=rank):
        fs = only.get(r["project"], []) if only is not None else r["findings"]
        pool += [(r, f) for f in fs]
    pool.sort(key=lambda rf: (SEVERITIES.index(rf[1]["severity"]) if rf[1]["severity"] in SEVERITIES else 3, 0 if rf[1].get("line") else 1))
    esc = lambda s: str(s).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    escp = lambda s: esc(s).replace(":", "%3A").replace(",", "%2C")
    for r, f in pool:
        k = f["severity"] if f["severity"] in kinds else "warning"
        if left[k] <= 0:
            continue
        left[k] -= 1
        path, line, col = located(r, f)
        props = ([f"file={escp(path)}"] if path else []) + ([f"line={line}"] if line else []) + ([f"col={col}"] if col else []) + [f"title={escp(f'pcbkit {f['code']} ({name(r)})')}"]
        at = f" at ({f['at'][0]:.2f}, {f['at'][1]:.2f}) mm" if f.get("at") and not line else ""
        out.append(f"::{kinds[k]} {','.join(props)}::{esc((f['message'] or '') + at)}")
    return out


def sarif(reps, ctx=None):
    """Every finding as SARIF 2.1.0 for GitHub code scanning: at the design's line where the check
    gives one, else at the board file; fingerprinted by board, code and items so an alert follows a
    finding across commits."""
    ctx = ctx or context({})
    rules, results = {}, []
    level = {"error": "error", "warning": "warning", "unconfirmed": "note"}
    version = next(((r.get("tool") or {}).get("version") for r in reps if r.get("tool")), None)
    for r in sorted(reps, key=lambda r: r["project"]):
        for f in r["findings"]:
            rules.setdefault(f["code"], {"id": f["code"], "name": f["code"], "shortDescription": {"text": f"PCBKit check {f['code']} ({f['level']})"},
                                         "helpUri": CHECKS_DOCS, "properties": {"tags": ["pcb", f["level"] or "check"]}})
            path, line, col = located(r, f)
            loc = {"physicalLocation": {"artifactLocation": {"uri": path or under(r["project"], ".")}}}
            if line:
                loc["physicalLocation"]["region"] = {"startLine": line, **({"startColumn": col} if col else {})}
            text = (f["message"] or f["code"]) + (f" at ({f['at'][0]:.2f}, {f['at'][1]:.2f}) mm" if f.get("at") and not line else "")
            view = viewer_url(ctx, r["project"], f["ids"])
            fp = hashlib.sha256(canonical([r["project"], *key(f)])).hexdigest()[:32]
            results.append({"ruleId": f["code"], "level": level.get(f["severity"], "warning"),
                            "message": {"text": text + (f" Open it: {view}" if view else ""), "markdown": text.replace("[", "\\[") + (f" [Open it in PCBKit]({view})" if view else "")},
                            "locations": [loc], "partialFingerprints": {"pcbkitFinding/v1": fp},
                            "properties": {"board": r["project"], "items": f["items"], "severity": f["severity"]}})
    return {"$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0",
            "runs": [{"tool": {"driver": {"name": "PCBKit verify", "version": version or "0", "informationUri": DOCS, "rules": sorted(rules.values(), key=lambda x: x["id"])}},
                      "results": results}]}


def junit(reps):
    """Per board one test case per level, failed by its errors (each error a line), then the gate."""
    from xml.sax.saxutils import escape, quoteattr
    suites = []
    for rep in reps:
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
            cases.append(f'<testcase classname={quoteattr("pcbkit." + name(rep))} name={quoteattr(l["level"])}>{body}<system-out>{escape(note)}</system-out></testcase>')
        g = rep["gate"]
        gb = "" if g["pass"] else f"<failure message={quoteattr('; '.join(g['reasons']))}/>"
        cases.append(f'<testcase classname={quoteattr("pcbkit." + name(rep))} name={quoteattr("gate: " + g["require"])}>{gb}</testcase>')
        fails = sum(1 for c in cases if "<failure" in c)
        suites.append(f'<testsuite name={quoteattr("pcbkit verify: " + name(rep))} tests="{len(cases)}" failures="{fails}">' + "".join(cases) + "</testsuite>")
    return '<?xml version="1.0" encoding="UTF-8"?>\n<testsuites>' + "".join(suites) + "</testsuites>\n"


# ── The command line ────────────────────────────────────────────────────────────────────────────

def write_json(path, v):
    with open(path, "w") as f:
        json.dump(v, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")


def run(a):
    boards = projects(a.projects or ["."])
    saved = json.load(open(a.check)) if a.check else None
    if saved is not None and len(boards) != 1:
        sys.stderr.write("--check reads one saved answer: name one project\n")
        return 2
    t0 = time.time()
    reps = [check_one(boards[0], a.binary, a.require, a.policy, saved)] if saved is not None else check_all(boards, a.binary, a.require, a.policy)
    os.makedirs(a.out, exist_ok=True)
    for r in reps:
        write_json(os.path.join(a.out, file_id(r["project"]) + ".json"), r)
    ctx = context()
    deltas, changed = None, set(r["project"] for r in reps)
    if a.base:
        # Only the boards whose files this change touched are checked at the base.
        touched = [r for r in reps if r.get("error") or not os.path.isdir(os.path.join(a.base, r["project"])) or inputs(os.path.join(a.base, r["project"]))["digest"] != r["inputs"]["digest"]]
        changed = set(r["project"] for r in touched)
        ctx["unchanged"] = set(r["project"] for r in reps) - changed
        def at_base(r):
            p = os.path.join(a.base, r["project"])
            if not os.path.isdir(p):
                return None
            res, _ = run_check(p, a.binary)
            return build(res["result"], p, a.binary, a.require, a.policy, r["project"]) if res else None
        with concurrent.futures.ThreadPoolExecutor(max(1, min(len(touched), os.cpu_count() or 2))) as ex:
            bases = list(ex.map(at_base, touched))
        deltas = [delta(b, h) for b, h in zip(bases, touched) if not h.get("error")]
        with open(os.path.join(a.out, "comment.md"), "w") as f:
            f.write(comment(ctx, deltas, reps, len(reps) - len(touched)))
    checked = time.time() - t0
    if ctx["public"] and a.pictures:
        warm([picture_url(ctx, r["project"]) for r in reps if r["project"] in changed and not r.get("error")])
    md = summary(reps, ctx, pictures=a.pictures)
    md += f"\n<sub>{len(reps)} boards checked in {checked:.1f} s{f'; {len(changed)} changed since the base' if a.base else ''}.</sub>\n"
    with open(os.path.join(a.out, "summary.md"), "w") as f:
        f.write(md)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a") as f:
            f.write(md)
    else:
        sys.stdout.write(md)
    if a.sarif:
        write_json(os.path.join(a.out, "pcbkit.sarif"), sarif(reps, ctx))
    if a.annotations:
        only = {d["project"]: d["new"] for d in deltas} if deltas is not None else None
        for line in annotations([r for r in reps if not r.get("error")], only):
            print(line)
    passed = all(r["gate"]["pass"] for r in reps)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"pass={'true' if passed else 'false'}\n")
    if a.policy == "report":
        return 0
    if any(r.get("error") for r in reps):
        return 2
    return 0 if passed else 1


def main():
    ap = argparse.ArgumentParser(description="PCBKit verify: reproducible pre-production reports of circuit boards")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("projects", nargs="*", help="board directories or globs (default: .)")
    r.add_argument("--project", action="append", dest="projects_opt", default=[], help="a board directory or glob (repeatable)")
    r.add_argument("--require", default="ready", choices=LEVELS)
    r.add_argument("--policy", default="gate", choices=POLICIES)
    r.add_argument("--out", default="pcbkit-reports")
    r.add_argument("--base", help="the repository at the pull request's base: the change per board")
    r.add_argument("--sarif", action="store_true")
    r.add_argument("--annotations", action="store_true")
    r.add_argument("--no-pictures", dest="pictures", action="store_false")
    r.add_argument("--check", help="a saved `pcbkit check` answer instead of running it (one project)")
    r.add_argument("--binary", default=os.environ.get("PCBKIT", "pcbkit"))
    v = sub.add_parser("verify")
    v.add_argument("report")
    v.add_argument("--project", default=None)
    v.add_argument("--binary", default=os.environ.get("PCBKIT", "pcbkit"))
    for n in ("summary", "junit"):
        sub.add_parser(n).add_argument("reports", nargs="+")
    sub.add_parser("comment").add_argument("path")
    a = ap.parse_args()

    if a.cmd == "junit":
        sys.stdout.write(junit([json.load(open(p)) for p in a.reports]))
        return 0
    if a.cmd == "summary":
        sys.stdout.write(summary([json.load(open(p)) for p in a.reports], context()))
        return 0
    if a.cmd == "comment":
        return post_comment(a.path)
    if a.cmd == "verify":
        rep = json.load(open(a.report))
        project = a.project or rep.get("project") or "."
        ok = hashlib.sha256(canonical({k: x for k, x in rep.items() if k != "digest"})).hexdigest() == rep.get("digest")
        print(f"report digest: {'ok' if ok else 'MISMATCH (edited after it was made)'}")
        same_files = inputs(project)["digest"] == rep["inputs"]["digest"]
        print(f"inputs: {'same files' if same_files else 'DIFFERENT files from the report'}")
        t = tool(a.binary)
        if t["version"] is None or t["version"] != rep["tool"]["version"]:
            print(f"re-run: skipped (pcbkit {t['version']} on PATH, the report was made by {rep['tool']['version']})")
            return 0 if ok and same_files else 1
        res, _ = run_check(project, a.binary)
        again = build(res["result"], project, a.binary, rep["gate"]["require"], rep["gate"]["policy"], rep["project"]) if res else None
        same = again is not None and again["digest"] == rep["digest"]
        print(f"re-run: {'reproduced (same body digest)' if same else 'NOT reproduced'}")
        return 0 if ok and same else 1
    a.projects = a.projects + a.projects_opt
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
