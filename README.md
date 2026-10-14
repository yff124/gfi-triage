# gfi-triage

**Before you claim a `good first issue`, find out whether it is still open — in one command.**

On a repo that merges quickly, the open `good first issue` list drifts away from reality:

- the maintainer merged the fix and **never closed the issue**;
- someone already commented *"this looks already fixed"*;
- someone **claimed it two days ago** and never opened a PR.

All three look identical from the issue list. So a contributor either writes a **no-op PR**, duplicates a claim, or re-fixes shipped code. `gfi-triage` answers, per issue:

| axis | how it is decided |
|---|---|
| **is it already fixed?** | it runs the checks the issue *itself* declares, against a local checkout of the default branch |
| **is someone on it?** | claiming comments, "looks already fixed" comments, and open PRs that reference the issue |
| **otherwise** | free |

## Real output

Against `ldbumble/taskuary` on 2026-10-14, fresh clone of `master`, one command:

```
$ python3 gfi_triage.py --repo ldbumble/taskuary --dir /path/to/taskuary

ldbumble/taskuary  label='good first issue'  open=6

#143  STILL BROKEN + CLAIMED
   Four Cancel/Dismiss buttons are lowercase while the rest are capitalised
   [EMPTY  ] grep -nE '>(cancel|dismiss)</Button>' website/src/*.jsx  -> 3 hit(s)
              website/src/RepoPicker.jsx:126
              website/src/ui.jsx:824
              website/src/ui.jsx:929
   claimed by: krishna2707

#147  ALREADY FIXED + REPORTED FIXED
   Docs list three task-page session controls that no longer exist
   [EMPTY  ] grep -n "Stop session\|Pause & save\|Give new prompt" docs/site/*.md  -> 0 hit(s)
   already reported fixed in a comment: yff124

#140  UNVERIFIED (no local check in the issue) + REPORTED FIXED
   Avatar style picker on About you is mouse-only and does not announce the selection
   already reported fixed in a comment: yff124
...
```

Read that as a work order: **#143 is the only one still worth touching**, and even that one is already claimed — so the honest move is to leave it alone. The other five are maintenance backlog, not contribution opportunities.

## How a check is read

Each issue body is scanned for `grep` / `rg` / `git grep` commands, in code fences and in inline code. The *expectation* comes from the sentence around the command:

- `"... finds nothing"` / `"no match"` / `"is gone"` → **EMPTY** → pass when the pattern is absent
- `"... finds"` / `"prints"` / `"shows"` → **PRESENT** → pass when the pattern is present

Commands are then re-run locally as regexes over the files the command names. Anything that is not a local grep (`pytest`, `npm test`, `python -m pytest`) is reported as **SKIP** and never guessed at.

`grep` writes alternation as `\|` and groups as `\(`; Python needs `|` and `(`. The tool translates those BRE escapes, because issue bodies in the wild mix both dialects — and silently taking `\|` literally would report a clean tree on a dirty one.

## What it deliberately does not do

- **It does not decide whether code is correct.** It re-runs checks the issue already states. If an issue states no check, the answer is `UNVERIFIED` — not a guess.
- **It does not touch the repo.** Read-only; it never comments, labels, or opens anything.
- **It does not run your test suite.** A green `npm test` is evidence; a green `grep` is evidence about one line. The tool stays on the side it can actually verify offline.

## Usage

```bash
python3 gfi_triage.py --repo owner/name --dir /path/to/checkout   # full triage
python3 gfi_triage.py --repo owner/name                          # claim axis only
python3 gfi_triage.py --repo owner/name --json                    # machine-readable
python3 gfi_triage.py --repo owner/name --label "help wanted"
python3 gfi_triage.py --selftest                                  # no network needed
```

- **Python 3.9+**, standard library only — no install step.
- Needs an authenticated [`gh` CLI](https://cli.github.com) for the API reads (it shells out to `gh api`; it never handles credentials itself).
- `--dir` is a checkout of the repo's **default branch**. Without it, only the claim axis runs.

## Tests

`--selftest` runs offline and asserts the parts that are easy to get quietly wrong: command extraction, quoting, the BRE translation, the EMPTY/PRESENT heuristic, the verdict matrix, and claim detection against real comment wording.

```
$ python3 gfi_triage.py --selftest
SELFTEST OK (0 failures)
```

## Why this exists

Written because the checks it automates were being done by hand, per session, per repo — and done inconsistently: the same list of open issues produced a different verdict depending on which of the three signals someone remembered to look at.

MIT.

---

Footnote, in case the output above is read as a claim of virtue: two of the fixes named in that run were merged from PRs by the author of this tool. The tool found them anyway — that is exactly the point. It reports the state of the tree, not whose patch it was.
