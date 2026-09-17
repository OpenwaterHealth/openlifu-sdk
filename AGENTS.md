# Agent workflow rules — OpenLIFU FDA Software

This file encodes the workflow that automated agents (and humans) follow
when working on **OpenLIFU FDA Software** project issues. It is enforced
by convention + PR review, not by CI (yet). Deviations must be justified
in the PR description.

Governing plans: `P-06` Software Development Plan · `P-07` Cybersecurity
Management Plan · `P-08` Software Maintenance Plan · `P-10` Risk
Management Plan.

## Where issues live

- **Requirement (SR-XXX) issues** — `OpenwaterHealth/openlifu-diathermy-application`
  only. Closed by a diathermy-repo PR that pins constituent releases and
  adds a verifying test.
- **Task issues** — the constituent repo where the code changes:
  `openlifu-operator-interface`, `openlifu-sdk`, `openlifu-transmitter-fw`,
  `openlifu-console-fw`. Closed by a PR to that repo.
- **Bug / Tech-debt issues** — the repo they were surfaced in.

## Picking work

1. Open the [OpenLIFU FDA Software project](https://github.com/orgs/OpenwaterHealth/projects/15).
2. Filter view to `Status = Ready` and `Assignees is empty`.
3. Assign yourself. Move the card to `In Progress`.

## Branching

- Task in constituent repo → branch `feature/SR-XXX-<short-slug>`
  (or `bugfix/` / `techdebt/` for those flavors).
- Requirement issue in diathermy → branch
  `feature/SR-XXX-pin-and-verify`.

## Commit messages (Conventional Commits)

```
<type>(<scope>): <imperative subject>

<body: what and why, 72-col wrapped>

Refs SR-XXX
Closes #<task-issue> (or Refs #<task-issue> for WIP commits)
```

- `type`: `feat` | `fix` | `refactor` | `test` | `docs` | `chore` | `build` | `ci`
- `scope`: optional module name, e.g. `configure`, `thermal`, `logger`
- Every commit MUST include the SR-XXX reference in body. Merge commits inherit from the PR title.

## Pull requests

- One PR per Task (avoid stacking multiple unrelated Tasks in one PR).
- PR title: same convention as commit subject.
- PR body must contain:
  - `Closes #<task-issue>` for the constituent-repo task
  - `Refs OpenwaterHealth/openlifu-diathermy-application#<REQ-issue>` for
    the parent Requirement
  - **Self-review section** (see below)
  - Test plan actually executed (unit tests added, screenshots for QML,
    bench-test evidence if applicable)
- PR must pass CI (unit tests, linters) before review.
- PR must be reviewed by a person / agent independent of the author (per
  P-06 SDP peer review).
- **When the work is complete, mark the PR as ready for review.** The
  SDP peer-review workflow (independent AI review + human review) only
  triggers on non-draft PRs. Do not leave a completed PR in draft
  state waiting for someone else to flip it; opening `Draft` is fine
  while work is in progress, but the last thing you do before handing
  off is flip it to `Ready for review` (via `gh pr ready <N>` or the
  GitHub UI). Removing `[WIP]` from the title is not enough on its
  own — the draft flag is a separate field.

### Self-review section (required in PR body)

```markdown
## Self-review

- **Assumption I'm making**: <the one assumption I'd defend under review>
- **Alternative considered**: <one path I did not take, and why>
- **New dependency introduced**: none / <name + justification>
- **Test coverage**: <which SR is covered, which cases are NOT covered>
- **Breaking change**: none / <what changes and downstream impact>
- **Tech-debt filed**: none / <link to tech-debt issue if any>
```

## Adversarial review checklist

The reviewer must **actively try to reject the PR**. Approve only when
none of the following apply:

- **Band-aid** — the fix hides a symptom instead of addressing the cause
- **Dead helper** — a helper function is added but has only one call site
- **Test theater** — tests exercise the code path but don't assert
  behavior meaningfully
- **New dependency** without a justified reason and SBOM update
- **Overloaded PR** — mixes SR-linked work with unrelated refactors /
  cleanups
- **Convoluted logic** — non-obvious control flow that reads unclearly
  in one pass
- **Style drift** — inconsistent with the repo's existing patterns
- **Missing traceability** — no SR reference in commit or PR body

If any apply: comment `Changes requested — <specific reason>`. Do not
approve.

## Tech-debt discovery

When mid-work you discover tech debt (something wrong / suboptimal that
you cannot fix in the current PR):

1. File a `tech-debt` issue in the affected repo.
2. Reference it from the current PR body.
3. Do not silently work around the debt in the current PR — call it out.
4. Priority defaults to `P2-medium`; management retriages during the
   next sprint review.

## Closing a Requirement issue

A Requirement (SR-XXX) issue closes only when:

1. Every constituent Task issue linked from it is closed.
2. Constituent repo version pin(s) updated in the diathermy repo's
   `pyproject.toml`.
3. A diathermy-side verifying test is committed and passing in CI. This
   test may mirror the constituent-repo test but MUST be present in the
   diathermy repo so the STM traceability is one-directional (SR →
   diathermy → constituent).
4. STM (workbook / Matrix Requirements) updated with implementation
   evidence.
5. The Requirement PR closes the Requirement issue via `Closes` link.

## Sprint / iteration cadence

- Sprint = 2 weeks (subject to iteration seeding on the project board).
- Sprint review at end of each iteration. Retriages `P0` / `P1` issues.
- Phase-gate review (per P-06) at completion of each Software Project
  (PROJ-XX).

## Escalation

- Blocked on external dependency for > 1 sprint: set Status = `Blocked`,
  document in the issue body under a `## Blocked on` section.
- P0 emergency: raise in the issue, tag `@peterhollender`, coordinate on
  release process.
- Plan-compliance question (P-06 / P-07 / P-08 / P-10): open a
  discussion in `OpenwaterHealth/openlifu-diathermy-application` under
  Discussions, do not proceed unilaterally.
