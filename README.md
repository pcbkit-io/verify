# PCBKit verify

A pre-production gate for circuit boards. On every push and pull request it runs PCBKit's check on
your KiCad projects and writes one file per board, `<board>.json`: the verdict per level, what each
level judged, what your project waived, and every finding. The job's summary shows every board in
one table, worst first; a pull request gets one comment with what the change did to each board.
Anyone with the same files and the same pcbkit release can re-run a check and get the same report,
digest for digest.

```yaml
# .github/workflows/board.yml
on: [push, pull_request]
permissions: { contents: read, pull-requests: write }
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: pcbkit-io/verify@v0
        with:
          projects: hardware/main-board  # the directory with the .kicad_pro and .kicad_pcb
          require: layout                # language | wiring | ratings | intent | layout | ready
```

A repository of many boards names them with a glob, and a library that shows its boards' state
without blocking anyone runs in report mode:

```yaml
      - uses: pcbkit-io/verify@v0
        with:
          projects: "*/*"     # every directory two levels down that holds a board
          policy: report      # the summary and the comment, never a failed job
          sarif: true         # every finding in code scanning (permissions: security-events: write)
```

| input | default | what |
|---|---|---|
| `projects` | `.` | the boards' directories, or globs of them (`boards/*`, `hardware/**`), separated by spaces or lines; a glob keeps the directories that hold a board (a `.kicad_pro`, a `.kicad_pcb` or `.pcbkit/design.pcbkit`) |
| `require` | `ready` | the level each board must be verified to |
| `policy` | `gate` | `gate`: the job fails when a board fails; `report`: never fails the job; `strict`: also fails on unconfirmed findings and on a required level that passes having judged nothing |
| `annotations` | `true` | findings annotated on the files, at the design's line where the check gives one (on a pull request: only its new findings) |
| `sarif` | `false` | every finding uploaded to GitHub code scanning (needs `security-events: write`) |
| `comment` | `true` | on a pull request, one comment with each changed board's new and fixed findings and level, updated in place (needs `pull-requests: write`; a fork's pull request, whose token cannot write, gets a warning) |
| `pictures` | `auto` | pictures of the boards in the summary and the comment, drawn by app.pcbkit.io from the commit; `auto`: on public repositories |
| `version` | the release this action version was tested with | the pcbkit release to install (cached between runs) |
| `binary` | | a pcbkit already on the runner, instead of installing the release |
| `attest` | `false` | optional: sign the reports with a GitHub artifact attestation (below) |

Outputs: `pass` (`true` when every board passed the gate) and `reports` (the reports' directory,
`pcbkit-reports`). The reports, the summary and the SARIF file are uploaded as the `pcbkit-reports`
artifact. Linux and macOS runners. The boards are checked in parallel, each on a copy of its files:
your files are never changed.

## What you see

- **The run's summary**: the boards as a grid of pictures (public repositories), then one row per
  board, worst first: its state (🔴 fails the gate, 🟡 passes with unconfirmed findings or with a
  required level that judged nothing, 🟢 passes), the level it reached, errors, warnings and
  unconfirmed findings, what each level judged, and the codes that fail it. Under it, per board: its
  picture, a checklist (layout, fab pre-check, parts named and bought, routing, wiring and ratings),
  its findings by code with the first of each (the design's line, or where on the board, and a link
  that opens the board in PCBKit's viewer with those items selected) and how to reproduce it.
- **A pull request's comment**: per board the change touched, its level before and after and its
  new and fixed findings (unrouted nets too). Only boards whose files changed are checked at the
  base. One comment per pull request, updated on every push; none while no board changed.
- **Annotations and code scanning**: findings at the design's line (`.pcbkit/design.pcbkit`), or on
  the board file with the position in millimetres.

## The levels

The verdict comes in levels, each built on the one before it. A board is *verified to* the last
level that passes with every level before it.

| level | what it judges | on a KiCad board without a PCBKit design |
|---|---|---|
| language | the design file parses and its statements agree | nothing to judge |
| wiring | electrical rules: pins driven, supplies and grounds, polarity, mating connectors, reset and USB conventions | from the schematic's pin types; many rules lack the facts they need |
| ratings | each part against its ratings: voltage, current, power, temperature | only where a part's rating is known |
| intent | the board against the requirements its design states | nothing stated, nothing judged |
| layout | the board's geometry under its own rules: clearances, shorts, courtyards, holes, thermals, silkscreen | fully: this is the board file |
| ready | can it be ordered: the fab's capability (track, gap, drill, ring, via), holes, vias in pads, exports, parts | the fab half fully; the parts half only where parts name what they buy |

A new gate on an existing KiCad board usually starts at `require: layout` and moves to `ready`
once the parts carry order codes.

## What each level judged

Each level counts the cells of the design its rules looked at:

- **checked**: decided on facts the board or its design states;
- **unknown**: a rule needs a fact the board does not carry (a part's rating, a net's current);
- **unchecked**: no rule looks at it.

The summary prints the three counts per level. **A level that passes with no checked cell passed
because it had nothing to judge**, and the summary says so ("passes judging nothing"). Read such a
pass as "no error found", not as "checked and correct". With `policy: strict` the gate refuses it.
A PCBKit design states voltages, currents, ratings and requirements, so its levels judge far more
of the board than an imported KiCad file's.

## Findings

- An **error** fails its level.
- A **warning** is a finding that does not fail it.
- An **unconfirmed** finding comes from a check still on probation: it has not yet cleared a blind
  review on boards nobody tuned it on. It is shown and counted, and fails the gate only with
  `policy: strict`. Check it against the part's datasheet and fix it when it is real.
- An unrouted net fails the gate.

The codes and what each one means: [the board format, § Checks](https://pcbkit.io/docs/format).

## Waivers

A gate you cannot waive is a gate you turn off. Two places record a decision, and the report keeps
every waived finding under **Waived by the project**, with its reason; it is never dropped:

- **KiCad's DRC exclusions**: what you excluded in KiCad's DRC dialog (`drc_exclusions` in the
  `.kicad_pro`) is waived when PCBKit's finding is the same violation on the same items;
- **`@allow(...)` in a PCBKit design**, on the component, net or block the finding is about, with
  its reason: `@allow(.intentCurrent, "the strip runs at most 6 A: the firmware limits it")`.

A waiver that waives nothing is itself a warning (`allow-unused`).

## What a pass says, and what it does not

A pass at `require: X` means: on these exact files (each file's SHA-256 is in the report), this
exact binary (its SHA-256 too) found no error in any level up to X, and no unrouted net. It is
evidence, not proof. The report does not decide:

- footprints and pin tables against the makers' drawings (a land that is the wrong size for the
  part you buy, a pin table that differs from the datasheet), unless the design carries part
  certificates;
- emissions, immunity and safety marks (a lab's work);
- the firmware on the board.

## Other CI systems

**GitLab CI**: include the template of the same tag. It fetches report.py of that tag, installs
the same pcbkit release and writes the reports, the summary and a JUnit file whose suites are the
boards and test cases their levels, so the merge request's test widget shows which board fails at
which level and why.

```yaml
include:
  - remote: https://raw.githubusercontent.com/pcbkit-io/verify/v0.2.0/gitlab/pcbkit-verify.gitlab-ci.yml
boards:
  extends: .pcbkit-verify
  variables: { PCBKIT_PROJECTS: "hardware/*", PCBKIT_REQUIRE: layout }
```

**Anything else** runs report.py (Python 3, standard library only) with pcbkit on the PATH:

```sh
curl -fsSL https://pcbkit.io/install.sh | PCBKIT_VERSION=2026.10.31 sh
python3 report.py run "hardware/*" --require layout   # exit 0 pass, 1 a board fails, 2 a check could not run
python3 report.py junit pcbkit-reports/*.json > pcbkit-junit.xml
```

The same files and release give the same report digest on GitHub, GitLab and your laptop.

## Checking a report you were given

```sh
python3 report.py verify pcbkit-reports/hardware--main-board.json   # --project DIR: the board's files elsewhere
```

`verify` recomputes the report's digest (was it edited after it was made?), the inputs' digest
from the files you have (is it the same board?) and, with the same pcbkit version installed, runs
the check again and compares (does it reproduce?). The report holds no time and no host name.

## Signing (optional)

`attest: true` signs every board's report with a
[GitHub artifact attestation](https://docs.github.com/actions/security-for-github-actions/using-artifact-attestations)
(Sigstore), so `gh attestation verify hardware--main-board.json -R OWNER/REPO` shows which repository,
commit and workflow made it. It needs the job's permissions
`{ contents: read, id-token: write, attestations: write }`. Public repositories can use it on every
plan; on private repositories, check that your GitHub plan offers artifact attestations. The
report's own digest and `report.py verify` work everywhere, signed or not.

## The report

One per board, `pcbkit-reports/<board>.json` (`pcbkit-report@2`; the board's directory with `/` as
`--`): `project` (the board's directory), `gate` (pass, the level required, the policy, the
reasons), `verdict` (the levels, as pcbkit gives them), `coverage` (per level: checked, unknown,
unchecked cells, and whether a pass is hollow), `waived`, `findings[]` (code, level, severity,
message, items, ids, nets, position, and the design's file, line and column where the check gives
them), `unrouted`, `counts`, `inputs` (every file and its SHA-256, and their digest), `tool` (the
pcbkit version and the binary's SHA-256) and `digest` (over all of it). Links and pictures live in
the summary only: a report holds no address, time or host.

## Versions

Tags `v0.1.0`, `v0.2.0`, ... are immutable; `v0` moves to the latest `v0.x`. Each tag pins the
pcbkit release it was tested with, end to end on public KiCad repositories. MIT licence (this
action); the pcbkit program is free, under [its own licence](https://github.com/pcbkit-io/cli/blob/main/LICENSE.md).
This repository is published from PCBKit's main repository; issues are welcome here.
