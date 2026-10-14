#!/usr/bin/env python3
"""gfi_triage.py -- before you claim a "good first issue", find out if it is still open.

The problem this solves
-----------------------
On a repo that merges fast, the open `good first issue` list drifts away from reality:

  * the maintainer merged the fix and never closed the issue;
  * a maintainer/contributor already answered "this looks already fixed" in a comment;
  * somebody claimed it two days ago and never opened a PR.

All three look identical from the issue list. A contributor who does not check
writes a no-op PR (wasted), duplicates a claim (rude), or re-fixes shipped code.

This tool answers, per issue, with one command:

  1. Is it already fixed on the default branch?
     -- by running the checks the issue *itself* states, against a local checkout.
  2. Is someone already on it?
     -- claiming comments, and open PRs that reference the issue.
  3. Otherwise: free.

What it deliberately does NOT do
--------------------------------
  * It does not decide whether code is correct. It runs the `grep`-shaped checks
    the issue body already declares ("`grep -n ... finds nothing`"). Checks that
    need a real test runner (`pytest`, `npm test`) are reported as SKIP, never
    guessed at.
  * It does not write anything to the repo. Read-only.

Requirements: Python 3.9+, and an authenticated `gh` CLI for the API reads.

Usage
-----
  python3 gfi_triage.py --repo ldbumble/taskuary --dir /path/to/checkout
  python3 gfi_triage.py --repo owner/name --json
  python3 gfi_triage.py --repo owner/name --label "help wanted"
  python3 gfi_triage.py --selftest
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shlex
import subprocess
import sys

CLAIM_PHRASES = [
    "i'd like to work on", "i would like to work on", "i'll work on",
    "i will work on", "can i work on", "could i work on", "assign me",
    "i am working on", "i'm working on", "working on this", "i can take this",
    "taking this one",
]
# "is this still open" is a question, not a claim: it blocks a claim only until
# answered, so it is reported separately rather than as a hard claim.
QUESTION_PHRASES = ["is this still open", "is anyone working on this"]

EMPTY_HINT = re.compile(
    r"finds?\s+nothing|no match|no matches|returns?\s+nothing|should\s+find\s+nothing"
    r"|is\s+gone|no longer|prints?\s+nothing",
    re.I,
)
# A comment that says "this looks already fixed" is worth as much as a claim:
# it is the reason you would not want to write a no-op PR.
FIXED_HINT = re.compile(
    r"already\s+(fixed|done|implemented|addressed|resolved|shipped)"
    r"|looks\s+(like\s+)?(it\s+)?(is|may\s+be|might\s+be)?\s*(already\s+)?(fixed|done|implemented)"
    r"|appears\s+to\s+be\s+(fixed|done|implemented)"
    r"|fixed\s+by\s+[0-9a-f]{7,40}",
    re.I,
)
CODE_FENCE = re.compile(r"```[a-zA-Z0-9_-]*\n(.*?)```", re.S)
INLINE_CODE = re.compile(r"`([^`\n]+)`")
GREP_CMD = re.compile(r"^(?:git\s+)?(?:grep|rg)\b")


def gh(path: str) -> object:
    """GET an api.github.com path via the gh CLI. Paginates `--paginate`-safe lists."""
    out = subprocess.run(
        ["gh", "api", path], capture_output=True, text=True, check=False
    )
    if out.returncode != 0:
        raise RuntimeError(f"gh api {path} failed: {out.stderr.strip()}")
    return json.loads(out.stdout or "null")


def gh_paged(path: str) -> list:
    sep = "&" if "?" in path else "?"
    out = subprocess.run(
        ["gh", "api", "--paginate", f"{path}{sep}per_page=100"],
        capture_output=True, text=True, check=False,
    )
    if out.returncode != 0:
        raise RuntimeError(f"gh api {path} failed: {out.stderr.strip()}")
    text = out.stdout.strip()
    if not text:
        return []
    # --paginate concatenates JSON arrays; splice them.
    dec = json.JSONDecoder()
    items, i = [], 0
    while i < len(text):
        while i < len(text) and text[i] in " \n\r\t":
            i += 1
        if i >= len(text):
            break
        obj, j = dec.raw_decode(text, i)
        items.extend(obj if isinstance(obj, list) else [obj])
        i = j
    return items


# --------------------------------------------------------------------------
# 1. read the checks out of the issue body
# --------------------------------------------------------------------------

def candidate_commands(body: str) -> list:
    """Every grep-shaped command the issue text mentions, fence or inline."""
    cmds = []
    for block in CODE_FENCE.findall(body):
        for line in block.splitlines():
            line = line.strip().lstrip("$").strip()
            if line:
                cmds.append(line)
    for inline in INLINE_CODE.findall(body):
        cmds.append(inline.strip())
    return cmds


def expectation_for(body: str, cmd: str) -> str:
    """EMPTY / PRESENT / UNKNOWN, read from the sentence around the command."""
    idx = body.find(cmd)
    if idx < 0:
        return "UNKNOWN"
    # the sentence, generously delimited so a following clause still counts
    start = max(0, idx - 240)
    window = body[start: idx + len(cmd) + 240]
    if EMPTY_HINT.search(window):
        return "EMPTY"
    if re.search(r"\bfinds\b|\bprints\b|\bshows\b|\bexpect\b", window, re.I):
        return "PRESENT"
    return "UNKNOWN"


def parse_grep(cmd: str):
    """-> (pattern, [paths]) for a local grep/rg command, else None."""
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return None
    if toks and toks[0] == "git" and len(toks) > 1 and toks[1] == "grep":
        toks = toks[1:]
    if not toks or not GREP_CMD.match(toks[0]):
        return None
    recursive = toks[0] == "rg"
    pattern, paths = None, []
    i = 1
    while i < len(toks):
        t = toks[i]
        if t in ("-e", "--regexp") and i + 1 < len(toks):
            pattern = toks[i + 1]
            i += 2
            continue
        if t.startswith("-"):
            if t in ("-r", "-R", "--recursive"):
                recursive = True
            i += 1
            continue
        if pattern is None:
            pattern = t
        else:
            paths.append(t)
        i += 1
    if pattern is None:
        return None
    if not paths:
        paths = ["."] if recursive else []
    return pattern, paths


def files_for(root: str, paths: list) -> list:
    found = []
    for p in paths or ["."]:
        target = os.path.join(root, p)
        if any(ch in p for ch in "*?["):
            found.extend(sorted(glob.glob(target)))
        elif os.path.isdir(target):
            for dirpath, _dirs, names in os.walk(target):
                found.extend(os.path.join(dirpath, n) for n in sorted(names))
        elif os.path.isfile(target):
            found.append(target)
    return [f for f in found if os.path.isfile(f)]


def normalize_pattern(pattern: str) -> str:
    """grep BRE writes alternation as `\\|` and groups as `\\\\(`; Python needs `|`, `(`.

    Issue bodies in the wild mix both dialects, so translate the BRE escapes.
    A genuine literal pipe is rare in these checks and is not supported.
    """
    return pattern.replace("\\|", "|").replace("\\(", "(").replace("\\)", ")")


def run_local_check(cmd: str, root: str) -> dict:
    parsed = parse_grep(cmd)
    if not parsed:
        return {"cmd": cmd, "status": "SKIP", "detail": "not a local grep; run it yourself"}
    pattern, paths = parsed
    try:
        rx = re.compile(normalize_pattern(pattern))
    except re.error as exc:
        return {"cmd": cmd, "status": "SKIP", "detail": f"unescaped pattern ({exc})"}
    hits = []
    for f in files_for(root, paths):
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh, 1):
                    if rx.search(line):
                        hits.append(f"{os.path.relpath(f, root)}:{n}")
        except OSError:
            continue
    return {"cmd": cmd, "status": "RAN", "hits": hits, "count": len(hits)}


# --------------------------------------------------------------------------
# 2. who else is on it
# --------------------------------------------------------------------------

def claim_state(number: int, comments: list, open_prs: list) -> dict:
    claimants, questions, reported, prs = [], [], [], []
    tag = re.compile(rf"(?:closes|fixes|resolves|closed|fixed)\s+#{number}\b", re.I)
    bare = re.compile(rf"#{number}\b")
    for c in comments:
        body = c.get("body") or ""
        low = body.lower()
        who = (c.get("user") or {}).get("login", "?")
        if any(p in low for p in QUESTION_PHRASES):
            questions.append(who)
        if any(p in low for p in CLAIM_PHRASES):
            claimants.append(who)
        if FIXED_HINT.search(body):
            reported.append(who)
    for pr in open_prs:
        body = pr.get("body") or ""
        title = pr.get("title") or ""
        head = ((pr.get("head") or {}).get("ref") or "")
        if tag.search(body) or tag.search(title) or bare.search(title) or \
                re.search(rf"\b0*{number}\b", head):
            prs.append(f"#{pr.get('number')}@{pr.get('user', {}).get('login', '?')}")
    return {"claimants": sorted(set(claimants)), "questions": sorted(set(questions)),
            "reported_fixed": sorted(set(reported)), "open_prs": prs}


# --------------------------------------------------------------------------
# 3. put it together
# --------------------------------------------------------------------------

def verdict_for(checks: list, claim: dict) -> str:
    ran = [c for c in checks if c["status"] == "RAN"]
    verdicts = []
    for c in ran:
        exp = c["expectation"]
        if exp == "EMPTY":
            verdicts.append(c["count"] == 0)
        elif exp == "PRESENT":
            verdicts.append(c["count"] > 0)
    if ran and verdicts and all(verdicts):
        state = "ALREADY FIXED"
    elif ran and verdicts and not any(verdicts):
        state = "STILL BROKEN"
    elif ran:
        state = "PARTIAL (checks disagree/undecidable)"
    else:
        state = "UNVERIFIED (no local check in the issue)"
    if claim["claimants"] or claim["open_prs"]:
        state += " + CLAIMED"
    elif claim["reported_fixed"]:
        state += " + REPORTED FIXED"
    return state


def triage(repo: str, label: str, root: str) -> list:
    issues = gh_paged(f"repos/{repo}/issues?state=open&labels={label.replace(' ', '%20')}")
    issues = [i for i in issues if "pull_request" not in i]
    open_prs = gh_paged(f"repos/{repo}/pulls?state=open")
    rows = []
    for iss in issues:
        n = iss["number"]
        body = iss.get("body") or ""
        comments = gh_paged(f"repos/{repo}/issues/{n}/comments")
        checks = []
        for cmd in candidate_commands(body):
            if not GREP_CMD.match(cmd.lstrip("$ ").strip()) and \
                    not cmd.lstrip("$ ").strip().startswith("git grep"):
                continue
            cmd = cmd.lstrip("$ ").strip()
            res = run_local_check(cmd, root) if root else {"cmd": cmd, "status": "SKIP",
                                                           "detail": "no --dir given"}
            res["expectation"] = expectation_for(body, cmd)
            checks.append(res)
        claim = claim_state(n, comments, open_prs)
        rows.append({
            "number": n, "title": iss.get("title", ""), "created": iss.get("created_at", ""),
            "comments": len(comments), "checks": checks, "claim": claim,
            "verdict": verdict_for(checks, claim),
        })
    return rows


def render(rows: list) -> str:
    out = []
    order = {"STILL BROKEN": 0, "PARTIAL": 1, "UNVERIFIED": 2, "ALREADY FIXED": 3}
    rows = sorted(rows, key=lambda r: (r["verdict"].startswith("STILL BROKEN") is False,
                                       order.get(r["verdict"].split(" +")[0], 9), r["number"]))
    for r in rows:
        out.append(f'#{r["number"]}  {r["verdict"]}')
        out.append(f'   {r["title"]}')
        for c in r["checks"]:
            if c["status"] == "RAN":
                out.append(f'   [{c["expectation"]:7}] {c["cmd"]}  -> {c["count"]} hit(s)')
                for h in c["hits"][:4]:
                    out.append(f'              {h}')
                if c["count"] > 4:
                    out.append(f'              ... +{c["count"] - 4} more')
            else:
                out.append(f'   [SKIP   ] {c["cmd"]}  ({c["detail"]})')
        c = r["claim"]
        if c["claimants"]:
            out.append(f'   claimed by: {", ".join(c["claimants"])}')
        if c["questions"]:
            out.append(f'   asked if still open: {", ".join(c["questions"])}')
        if c["reported_fixed"]:
            out.append(f'   already reported fixed in a comment: {", ".join(c["reported_fixed"])}')
        if c["open_prs"]:
            out.append(f'   open PR touching it: {", ".join(c["open_prs"])}')
        if not (c["claimants"] or c["open_prs"] or c["reported_fixed"]):
            out.append('   nobody has claimed it and no open PR references it')
        out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------
# selftest -- no network
# --------------------------------------------------------------------------

FIXTURE_BODY = """\
On the settings page the two chips are plain boxes.

**Where:** `website/src/Settings.jsx`

**Done when** each chip is a `<button type="button">` and
`grep -nE '>(cancel)</Button>' website/src/*.jsx` finds nothing.

**Test:** `cd website && npm test`
"""


def selftest() -> int:
    fails = []

    def eq(got, want, what):
        if got != want:
            fails.append(f"{what}: got {got!r}, want {want!r}")

    cmds = candidate_commands(FIXTURE_BODY)
    eq(any("grep -nE" in c for c in cmds), True, "fenced grep is found")
    eq(any(c == "cd website && npm test" for c in cmds), True, "test command is found")

    eq(parse_grep("grep -nE '>(cancel)</Button>' website/src/*.jsx"),
       (">(cancel)</Button>", ["website/src/*.jsx"]), "parse grep -nE")
    eq(parse_grep("git grep -rn 'Stop session' docs/site/*.md"),
       ("Stop session", ["docs/site/*.md"]), "parse git grep -rn")
    eq(parse_grep("cd website && npm test"), None, "non-grep returns None")
    eq(parse_grep("grep -rn \"a b\" ."), ("a b", ["."]), "quoted pattern")

    eq(expectation_for(FIXTURE_BODY, "grep -nE '>(cancel)</Button>' website/src/*.jsx"),
       "EMPTY", "finds nothing -> EMPTY")

    # a real run against a fixture tree
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "website/src"))
        open(os.path.join(d, "website/src/Settings.jsx"), "w").write(
            '<Box component="button" type="button" aria-pressed={on}>x</Box>\n')
        open(os.path.join(d, "website/src/Old.jsx"), "w").write(
            '<Button size="small">cancel</Button>\n')
        res = run_local_check("grep -nE '>(cancel)</Button>' website/src/*.jsx", d)
        eq(res["status"], "RAN", "check runs")
        eq(res["count"], 1, "one leftover found")
        os.remove(os.path.join(d, "website/src/Old.jsx"))
        eq(run_local_check("grep -nE '>(cancel)</Button>' website/src/*.jsx", d)["count"],
           0, "zero once the leftover is gone")
        # BRE dialect: grep writes alternation as \|, Python needs |
        os.makedirs(os.path.join(d, "docs/site"))
        open(os.path.join(d, "docs/site/troubleshooting.md"), "w").write(
            "Stop session ends the process and changes nothing else.\n")
        eq(normalize_pattern(r"Stop session\|Pause & save\|Give new prompt"),
           "Stop session|Pause & save|Give new prompt", "BRE alternation translated")
        eq(run_local_check(r"grep -n 'Stop session\|Give new prompt' docs/site/*.md", d)["count"],
           1, "translated pattern actually matches (0 would mean the \\| was taken literally)")
        eq(expectation_for("Done when `grep -n 'Stop session' docs/site/*.md` finds nothing.",
                           "grep -n 'Stop session' docs/site/*.md"), "EMPTY", "inline EMPTY hint")

    fixed = [{"status": "RAN", "expectation": "EMPTY", "count": 0}]
    broken = [{"status": "RAN", "expectation": "EMPTY", "count": 1}]
    none = {"claimants": [], "questions": [], "reported_fixed": [], "open_prs": []}
    claimed = {"claimants": ["someone"], "questions": [], "reported_fixed": [], "open_prs": []}
    reported = {"claimants": [], "questions": [], "reported_fixed": ["other"], "open_prs": []}
    eq(verdict_for(fixed, none), "ALREADY FIXED", "fixed verdict")
    eq(verdict_for(broken, none), "STILL BROKEN", "broken verdict")
    eq(verdict_for(fixed, claimed), "ALREADY FIXED + CLAIMED", "claimed suffix")
    eq(verdict_for(fixed, reported), "ALREADY FIXED + REPORTED FIXED", "reported suffix")
    eq(verdict_for([], none), "UNVERIFIED (no local check in the issue)", "unverified")

    claim = claim_state(143, [{"body": "Hi! I'd like to work on this issue.", "user": {"login": "k"}},
                              {"body": "is this still open?", "user": {"login": "q"}},
                              {"body": "This looks already done on master", "user": {"login": "r"}}], [])
    eq(claim["claimants"], ["k"], "claim detected")
    eq(claim["questions"], ["q"], "question detected")
    eq(claim["reported_fixed"], ["r"], "reported-fixed detected")
    eq(claim_state(143, [], [{"number": 9, "title": "fix: labels", "body": "Closes #143",
                              "head": {"ref": "x"}, "user": {"login": "u"}}])["open_prs"],
       ["#9@u"], "open PR closes #143")

    if fails:
        print("SELFTEST FAILED")
        for f in fails:
            print("  -", f)
        return 1
    print(f"SELFTEST OK ({len(fails)} failures)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", help="owner/name")
    ap.add_argument("--label", default="good first issue")
    ap.add_argument("--dir", default="", help="local checkout of the default branch")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.repo:
        ap.error("--repo is required (or use --selftest)")
    if not a.dir:
        print("note: no --dir, so no check can be run; the claim axis still works\n",
              file=sys.stderr)
    rows = triage(a.repo, a.label, a.dir)
    if a.json:
        print(json.dumps(rows, indent=2))
    else:
        print(f"{a.repo}  label={a.label!r}  open={len(rows)}\n")
        print(render(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
