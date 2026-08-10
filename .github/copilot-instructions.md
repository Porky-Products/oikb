# GitHub Copilot Organization Instructions v1.0 (Base version always available at https://github.com/Porky-Products/porky-docs/edit/main/docs/GitHub%20Copilot%20Organization%20Instruction%20Default.md)

## Purpose

These instructions provide GitHub Copilot and other approved development agents with an actionable version of the organization’s GitHub developer conventions.

Apply these instructions when asked to:

- Create or update an issue.
- Suggest issue labels, type, priority, scope, or risk.
- Create or name a branch.
- Draft or improve a commit message.
- Organize commits.
- Create or update a pull request.
- Review a pull request.
- Rebase or merge a branch.
- Create or update GitHub Actions workflows.
- Prepare a deployment, release, version, or hotfix.
- Draft an Architecture Decision Record.
- Plan or evaluate a technical spike.
- Recommend an exception to the normal workflow.

These instructions supplement the full GitHub Organization Developer Conventions handbook. The handbook remains the authoritative source when additional context is required.

---

## Instruction Priority

Instructions use the following markers:

- **`[POLICY]`** — Must be followed unless an approved exception applies.
- **`[GUIDELINE]`** — Should normally be followed but may be adapted when the repository or task requires a different approach.
- **`[CONDITIONAL]`** — Applies only when the stated condition is true.

Repository-specific instructions may add stricter requirements.

Do not silently override organization policy with a repository convention. When instructions conflict or are unclear, identify the conflict and request clarification rather than guessing.

---

## Agent Integrity and Evidence

These requirements apply to every task.

- **`[POLICY]`** Never invent issue IDs, test results, approvals, deployment results, screenshots, logs, version numbers, build numbers, or runtime evidence.
- **`[POLICY]`** Never claim that a test, build, review, deployment, migration, or release succeeded unless supporting evidence is available.
- **`[POLICY]`** Never expose or reproduce credentials, passwords, tokens, API keys, cookies, private keys, customer data, order data, or other sensitive values.
- **`[POLICY]`** Do not present unverified assumptions as established facts.
- **`[POLICY]`** Do not push directly to `main`.
- **`[POLICY]`** Do not substitute an AI review for a required human review on High-risk changes.
- **`[GUIDELINE]`** Prefer the smallest safe and complete change that solves the stated problem.
- **`[GUIDELINE]`** Keep work traceable from issue through branch, commit, PR, review, CI, merge, deployment, and issue closure.
- **`[GUIDELINE]`** Use `N/A — <reason>` when a required field does not apply.
- **`[GUIDELINE]`** State what was not tested, not inspected, or not proven instead of hiding missing evidence.
- **`[GUIDELINE]`** Avoid unnecessary process, but do not remove controls required by the change’s risk.

---

# 1. Repository Setup

A production repository should allow a new developer to clone, run, test, and safely modify the project within minutes.

## Required Files for Production Repositories

When evaluating or creating a production repository, confirm that the following files or folders exist:

| File or Folder | Expected Content |
|---|---|
| `README.md` | Purpose, owner team, local setup, required runtimes, common commands, and deployment/readiness notes |
| `.gitignore` | Build output, local configuration, IDE files, logs, secrets, and other local artifacts |
| `.env.example` | Placeholder-only local configuration; never real secret values |
| `docs/` | Relevant documentation such as `api/`, `adr/`, `deployment/`, `development/`, `maintenance/`, or `user-guides/` |

## Suggested Files

Consider these files when applicable:

| File or Folder | Purpose |
|---|---|
| `.github/dependabot.yml` | Dependency update and security update automation |
| `.github/workflows/*.yml` | CI or other repository automation |
| `.github/copilot-instructions.md` | Organization-approved Copilot instructions |

- **`[GUIDELINE]`** Scale repository automation to the repository’s importance and production impact.
- **`[GUIDELINE]`** Do not add unnecessary CI or release automation to documentation-only, archival, or non-executable repositories.
- **`[POLICY]`** Do not place real credentials or sensitive values in repository documentation or example configuration.

---

# 2. Standard Developer Workflow

The standard workflow is:

```text
Issue
→ Branch
→ Commit
→ Pull Request
→ Review
→ CI
→ Merge
→ Deploy
→ Close Issue
```

Not every repository or change requires every step, but production-impacting work should follow this flow unless a documented exception applies.

| Step | Expected Action | Purpose |
|---|---|---|
| Issue | Create or confirm the issue or work item | Makes the reason for the change visible and trackable |
| Branch | Create a correctly named working branch | Isolates work from long-lived branches |
| Commit | Create clear, atomic commits | Makes history understandable, reviewable, and revertible |
| Pull Request | Open a PR with useful reviewer information | Provides context and gives CI a place to validate the change |
| Review | Address feedback and obtain required review | Improves correctness and team awareness |
| CI | Ensure required checks pass | Prevents known broken code from merging |
| Merge | Use the approved merge strategy | Integrates reviewed and validated changes into the correct long-lived branch |
| Deploy | Use an organized release process | Keeps production changes intentional and traceable |
| Close Issue | Close or update the linked issue | Keeps work tracking current |

---

# 3. Issues and Work Tracking

Issues define, prioritize, and track work before it becomes code.

A useful issue should explain:

- The problem or requested outcome.
- Why the work matters.
- Expected behavior or acceptance criteria.
- Relevant context and constraints.
- Priority.
- Primary issue type.
- Area or scope when useful.
- Risk when the work affects security, data, infrastructure, deployment, or production behavior.

Use issues for non-trivial work such as:

- Bugs.
- Features.
- Technical debt.
- Spikes and research.
- Refactors.
- Documentation.
- CI/CD changes.
- Infrastructure changes.
- Dependency upgrades.
- Security changes.
- Production follow-up work.

## Issue Types

Every non-trivial issue should have one primary type.

| Issue Type | Use For |
|---|---|
| `bug` | Broken, unexpected, or incorrect behavior |
| `feature` | New user-facing or business functionality |
| `chore` | Maintenance that does not directly change product behavior |
| `tech-debt` | Cleanup or improvement that reduces future engineering risk |
| `spike` | Time-boxed research, prototype, or feasibility work |
| `docs` | Documentation-only work |
| `security` | Security hardening, vulnerability remediation, or sensitive access changes |
| `ci` | CI/CD, GitHub Actions, build, or automation work |
| `infra` | Infrastructure, hosting, runners, deployment platform, or environment work |

Use `feature` as the issue type but `feat` as the corresponding branch or commit type.

## Issue Labels

- **`[POLICY]`** Each non-trivial issue should have a type and priority.
- **`[GUIDELINE]`** Add area or scope labels when useful.
- **`[GUIDELINE]`** Add a risk label when work affects security, data, deployment, infrastructure, internal systems, or production behavior.
- **`[GUIDELINE]`** Avoid adding labels that do not improve filtering, routing, planning, or reporting.

## Priority

| Priority | Meaning |
|---|---|
| `p0` | Critical issue actively blocking production, security, or major business operations |
| `p1` | High-priority issue that should be handled soon |
| `p2` | Normal planned work |
| `p3` | Low-priority, backburner, or nice-to-have work |

Do not confuse priority with effort. A small issue can be urgent, and a large issue can be low priority.

---

# 4. Branch Naming

## Format

Use:

```text
<type>/<issue-id>-<short-description>
```

Examples:

```text
feat/MOE-123-catalog-search
fix/MOE-247-order-rounding
ci/MOE-305-add-build-checks
security/MOE-330-tighten-session-handling
spike/MOE-401-wrapper-feasibility
hotfix/MOE-500-login-outage
```

When no tracked issue exists and the change is genuinely trivial:

```text
docs/update-readme
chore/update-gitignore
```

## Branch Naming Rules

- **`[POLICY]`** Use an existing issue ID when one is available.
- **`[POLICY]`** Never invent an issue ID.
- **`[GUIDELINE]`** Use lowercase kebab-case.
- **`[GUIDELINE]`** Keep the short description concise and specific.
- **`[GUIDELINE]`** Prefer one to three meaningful words in the short description.
- **`[GUIDELINE]`** Do not use a developer’s name as the primary branch identifier.
- **`[GUIDELINE]`** Do not create vague branch names.

Avoid:

```text
fix
changes
stuff
final
new-work
my-branch
test123
```

## Allowed Branch and Commit Types

| Type | Use For |
|---|---|
| `feat` | New user-facing or business functionality |
| `fix` | Bug fix or correction to existing behavior |
| `chore` | Maintenance without direct product behavior change |
| `docs` | Documentation-only changes |
| `test` | Test-only changes or test coverage improvements |
| `refactor` | Internal code cleanup with no intended behavior change |
| `perf` | Performance or optimization work |
| `ci` | CI/CD or GitHub Actions workflow changes |
| `spike` | Investigative or prototype work |
| `hotfix` | Urgent production fix |
| `build` | Build tooling, packaging, or dependency pipeline work |
| `revert` | Reverting a prior change |
| `release` | Large release preparation or staging work when needed |
| `style` | Formatting-only changes with no behavior impact |
| `security` | Security hardening or vulnerability fix |

---

# 5. Commit Messages

## Format

Use:

```text
<type>(<scope>): <description>
```

The scope is optional.

Examples:

```text
feat(catalog): add customer availability filtering
fix(auth): preserve the middleware session cookie
test(orders): add duplicate-submit regression coverage
ci(actions): add pull request build checks
docs(onboarding): document local setup
chore(deps): update TypeScript
security(auth): restrict privileged session access
```

## Common Scopes

| Scope | Use For |
|---|---|
| `auth` | Login, sessions, permissions, or user identity |
| `ui` | Shared UI components, layout, or visual behavior |
| `api` | API routes, request/response handling, or backend integration |
| `db` | Database schema, migrations, or queries |
| `ci` | CI checks or deployment workflows |
| `actions` | GitHub Actions |
| `deps` | Dependency updates |
| `docs` | Documentation |
| `config` | App configuration, environment templates, or runtime settings |
| `infra` | Infrastructure, hosting, runners, or deployment |
| `tests` | Test setup or coverage |

Repository-specific feature scopes may also be used:

```text
catalog
orders
checkout
pricing
notifications
scanner
mobile
```

## Commit Quality

A good commit should:

- Represent one logical change.
- Use the correct type.
- Use a meaningful scope when useful.
- Clearly explain what changed.
- Be concise but not vague.
- Leave the application buildable and testable where practical.
- Avoid unrelated formatting, dependency, generated-file, or behavior changes.
- Use a commit body when the reason or tradeoff is not obvious.

## Do and Don’t

| Do Not Use | Prefer |
|---|---|
| `fix` | `fix(orders): correct total rounding` |
| `update` | `chore(deps): update TypeScript` |
| `changes` | `refactor(api): isolate response normalization` |
| `more tests` | `test(auth): add expired-session coverage` |
| `final changes` | `docs(readme): add local setup instructions` |
| `wip` | A descriptive logical commit or squash before merge |
| A paragraph-long subject | A concise subject with context in the body |

## Commit Body

Use a commit body when necessary:

```text
fix(auth): preserve the middleware session cookie

The client previously discarded Set-Cookie responses, causing each
request to create a new middleware session.

Refs #247
```

The subject explains what changed. The body explains why.

## Commit Atomicity

- **`[POLICY]`** Each commit should represent one logical change.
- **`[GUIDELINE]`** Keep commits independently understandable and revertible.
- **`[GUIDELINE]`** Leave the application working and tests passing after each commit where practical.
- **`[GUIDELINE]`** Do not mix unrelated feature, dependency, formatting, database, and CI work merely to reduce commit count.
- **`[GUIDELINE]`** Prefer a clear revert commit over silently rewriting shared history.

---

# 6. What Must Not Be Committed

Do not commit generated or local-only artifacts unless the repository explicitly requires them.

Common examples:

```text
node_modules/
dist/
build/
coverage/
.env
.DS_Store
temporary logs
local IDE state
local build output
personal AI scratch files
temporary prompt files
```

Organization-approved instruction files may be committed:

```text
.github/copilot-instructions.md
```

## Secrets

- **`[POLICY]`** Never commit credentials, passwords, API keys, tokens, cookies, private keys, or service-account material.
- **`[POLICY]`** `.env.example` must contain placeholders only.
- **`[POLICY]`** Production and shared secrets belong in GitHub Secrets, GitHub Environments, or another approved secrets manager.
- **`[POLICY]`** Do not include sensitive values in issues, commit messages, PR descriptions, logs, screenshots, fixtures, or documentation.
- **`[GUIDELINE]`** Local development secrets may be stored in ignored `.env` files.
- **`[GUIDELINE]`** Before drafting or reviewing a PR, check whether logs, screenshots, fixtures, or examples contain sensitive data.

---

# 7. Branching Strategy

## Long-Lived Branches

The organization uses a release-branch-first workflow.

- `main` is the source of truth for production code.
- `release` is the staging and integration branch used for production-like validation and additional QA.
- Other branches should normally be short-lived.

## Branch Rules

- **`[POLICY]`** Do not push directly to `main`.
- **`[POLICY]`** Do not force-push to `main`.
- **`[POLICY]`** Do not delete `main`.
- **`[POLICY]`** Create ordinary working branches from the latest `release` branch.
- **`[GUIDELINE]`** Do not start a new short-lived branch from another short-lived branch unless the dependency is intentional and documented.
- **`[GUIDELINE]`** Merge short-lived branches promptly, preferably daily when practical.
- **`[GUIDELINE]`** If a working branch cannot be merged promptly, rebase the working branch onto the latest `release` branch regularly.
- **`[GUIDELINE]`** If work was mistakenly based on `main`, create the correct branch from `release` and cherry-pick the relevant commits.
- **`[GUIDELINE]`** Keep branch lifetime short to reduce conflicts and integration risk.

---

# 8. Rebase Instructions

When asked to update a working branch with the latest `release` branch, use:

```bash
git fetch origin
git switch <working-branch>
git rebase origin/release
```

When conflicts occur:

```bash
# Resolve the affected files
git add <resolved-files>
git rebase --continue
```

To cancel the rebase:

```bash
git rebase --abort
```

After rebasing a branch that was already pushed:

```bash
git push --force-with-lease
```

- **`[POLICY]`** Do not rebase `main`.
- **`[POLICY]`** Do not rebase `release` merely to update an individual working branch.
- **`[GUIDELINE]`** Rebase only branches you own or branches whose collaborators have coordinated the history rewrite.
- **`[GUIDELINE]`** Use `--force-with-lease`, not plain `--force`, for a previously pushed working branch.

---

# 9. Pull Requests

A PR should be opened for each focused unit of work, including:

- Feature.
- Bug fix.
- Refactor.
- Security change.
- Documentation update.
- Test change.
- Spike.
- CI/CD change.
- Infrastructure change.
- Hotfix.

Do not describe every PR as a feature.

## PR Description Requirements

Every PR should include:

```md
## Summary

- <Brief breakdown of what changed>

## Why is this change needed?

<Explain the problem or requirement being addressed. Mention meaningful
alternatives when applicable.>

## How was it tested?

- `<Exact command or verification method>` — `<Observed result>`
- `<Exact command or verification method>` — `<Observed result>`

## Risk Level

`Low | Medium | High`

**Risk rationale:** <Explain why>

## Linked Issue

`Closes #___ | Refs #___ | Part of #___ | N/A — <reason>`

## Screenshots / Logs

<Links or `N/A — no visual or log evidence required`>

## Deployment Notes

<Environment variables, configuration, migration, deployment, versioning,
or `N/A — no deployment impact`>

## Rollback Notes

<Required for High-risk changes. Otherwise use `N/A — <reason>`.>
```

## PR Authoring Rules

- **`[POLICY]`** Include every required heading.
- **`[POLICY]`** Use exact test commands or exact verification methods.
- **`[POLICY]`** State the observed result of each check.
- **`[POLICY]`** Do not claim all tests passed without evidence.
- **`[POLICY]`** Do not invent links, issue IDs, screenshots, logs, or deployment results.
- **`[POLICY]`** Identify the PR’s risk level.
- **`[POLICY]`** Include rollback notes for High-risk changes.
- **`[GUIDELINE]`** Keep the PR body proportional to the change.
- **`[GUIDELINE]`** Small changes may have concise descriptions.
- **`[GUIDELINE]`** Complex or High-risk changes should provide more detailed evidence.
- **`[GUIDELINE]`** Prefer structured evidence over repeated narrative.
- **`[GUIDELINE]`** Use `N/A — <reason>` instead of leaving a section blank.
- **`[GUIDELINE]`** Redact sensitive information from logs and screenshots.

## Issue Linking

Use:

```text
Closes #123
```

only when the PR fully completes the issue.

Use:

```text
Refs #123
Part of #123
```

when the PR is related to a larger issue but does not fully complete it.

---

# 10. Pull Request Review

When reviewing a PR, inspect the actual diff and available evidence.

Do not rely only on the PR description.

## Review Checklist

Determine:

1. Does the change solve the stated problem or linked issue?
2. Is the approach clear and maintainable?
3. Is the stated scope accurate?
4. Is the declared risk level accurate?
5. Are tests or manual validation steps documented?
6. Do the tests meaningfully cover the changed behavior?
7. Are there security, data, deployment, compatibility, or rollback concerns?
8. Is sensitive information present?
9. Are undocumented behavior changes present in the diff?
10. Are material claims unsupported by evidence?
11. Does the change require human review under the risk policy?
12. Does the PR modify CI/CD, deployment, auth, data, or internal-system boundaries?

## Review Comment Style

Clearly state whether a finding is:

- **Blocking** — Must be corrected before merge.
- **Suggestion** — Recommended but non-blocking.
- **Question** — Clarification is required.
- **Observation** — Relevant context with no requested action.

Avoid vague comments.

Include:

- File and location.
- Evidence.
- Impact.
- Required or recommended correction.
- Confidence when uncertainty exists.

## Copilot Review Output

Use this format:

```md
## Review Outcome

- **Outcome:** `NO_BLOCKING_FINDINGS | CHANGES_REQUIRED | HUMAN_REVIEW_REQUIRED`
- **Assessed risk:** `Low | Medium | High`
- **Confidence:** `High | Medium | Low`
- **Human review required:** `Yes | No`

## Blocking Findings

### Finding 1

- **Severity:** `Critical | High | Medium`
- **Category:** `Correctness | Security | Data | Reliability | Testing | Compatibility | CI/CD | Deployment | Documentation`
- **Location:** `<file>:<line or range>`
- **Evidence:** <What supports the finding>
- **Impact:** <What can fail and who or what is affected>
- **Required correction:** <Specific expected resolution>
- **Confidence:** `High | Medium | Low`

## Non-Blocking Suggestions

- ...

## Unverified Claims or Missing Evidence

- ...

## Required Human Review Areas

- ...
```

Do not represent an AI review outcome as a human approval.

---

# 11. Risk Classification

Risk determines the required review, testing, documentation, and rollback controls.

## Low Risk

Small, isolated changes with minimal likelihood of breaking production or exposing sensitive data.

Examples:

- Documentation.
- Small UI polish.
- Simple internal cleanup.
- Test-only changes.

Requirements:

- Copilot review.
- Required CI checks when applicable.
- Rollback plan not normally required.

## Medium Risk

Changes that affect behavior, maintainability, dependencies, or normal application flow.

Examples:

- Features.
- Bug fixes.
- Refactors.
- Dependency updates.
- Non-critical CI changes.

Requirements:

- Copilot review.
- Required CI checks.
- Rollback planning recommended, especially when behavior changes.

## High Risk

Changes that affect security, production stability, data integrity, production deployment, or other internal systems.

Examples:

- Authentication.
- Authorization.
- Security boundaries.
- Production deployment.
- Database migrations.
- Order submission.
- Customer or order data.
- Secrets or credentials.
- Infrastructure.
- Self-hosted runners.
- CI/CD release paths.
- Privileged integrations.
- Breaking API or schema changes.

Requirements:

- Copilot review.
- Required CI checks.
- At least one human reviewer.
- Rollback plan required.

## Risk Escalation

- **`[POLICY]`** If risk is uncertain, treat the change as High risk until a reviewer or owner confirms otherwise.
- **`[POLICY]`** High-risk changes must not merge until review, testing, and rollback requirements are satisfied.
- **`[GUIDELINE]`** Do not lower risk merely to reduce review requirements.
- **`[GUIDELINE]`** Reassess risk after the final diff, not only when the PR is first opened.

---

# 12. Merging Procedures

## Default: Rebase Merge

Rebase merge is the default when the PR contains clean, atomic, meaningful commits.

Prefer rebase merge when:

- Commit messages follow the convention.
- Each commit represents a useful logical step.
- Preserving individual commits improves traceability.
- The commit history does not contain unnecessary work-in-progress noise.

## Fallback: Squash Merge

Use squash merge when:

- The branch contains `wip`, `fix`, `try again`, or similarly vague commits.
- Review fixes created many low-value commits.
- The commit history is messy.
- The individual commits do not provide useful history.
- The PR is clearer as one final commit.

The final squash commit must follow:

```text
<type>(<scope>): <description>
```

## Merge Rules

- **`[POLICY]`** Do not merge before required reviews and CI checks are complete.
- **`[POLICY]`** Apply the review requirement associated with the final PR risk.
- **`[GUIDELINE]`** Prefer rebase merge for clean history.
- **`[GUIDELINE]`** Prefer squash merge when the commit history is not worth preserving.
- **`[GUIDELINE]`** Do not recommend a normal merge commit unless a repository-specific process or approved exception requires preserving branch topology.
- **`[POLICY]`** If new work is required after a PR has merged, open a new PR.

---

# 13. CI/CD Guidelines

Production, shared-library, infrastructure, and automation repositories should use CI where applicable.

Scale CI to the repository’s importance and risk.

## Minimum CI for Production Code

CI normally includes:

```text
Install or restore locked dependencies
→ Lint
→ Typecheck, when applicable
→ Test
→ Build
→ Security or dependency checks, when applicable
```

## CI Policies

- **`[POLICY]`** Required CI checks must pass before merge.
- **`[POLICY]`** Production secrets must never be exposed to PR workflows.
- **`[POLICY]`** Workflow permissions should use the least access required.
- **`[GUIDELINE]`** CI should run on pull requests by default.
- **`[GUIDELINE]`** Protect build servers with appropriate authentication, authorization, and role-based access.
- **`[GUIDELINE]`** Integrate security testing early rather than treating it as an afterthought.
- **`[GUIDELINE]`** Treat CI/CD workflow, runner, permission, and deployment changes as Medium or High risk depending on impact.
- **`[GUIDELINE]`** Keep CI and production deployment responsibilities clearly separated.
- **`[GUIDELINE]`** Do not claim deployment success when only CI checks were run.

## Scheduled Workflows

Use scheduled workflows when appropriate for:

- Heavy end-to-end tests.
- Integration tests.
- Security scans.
- Performance tests.
- Dependency update validation.
- Resource-intensive checks that should not block normal development.

Schedules are commonly daily or weekly depending on the purpose.

## Default GitHub Actions Shape

When asked to generate a simple Node.js CI workflow for this organization, start from this pattern and adapt it to the repository:

```yaml
name: CI

on:
  pull_request:
    branches:
      - release
      - main
  push:
    branches:
      - release
      - main

permissions:
  contents: read

concurrency:
  group: ci-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  verify:
    name: Install, test, and build

    runs-on:
      - self-hosted
      - linux
      - x64
      - standard-ci

    timeout-minutes: 20

    steps:
      - name: Check out source
        uses: actions/checkout@v6

      - name: Set up Node.js
        uses: actions/setup-node@v7
        with:
          node-version: "22.x"
          cache: npm

      - name: Install locked dependencies
        run: npm ci

      - name: Lint
        run: npm run lint --if-present

      - name: Typecheck
        run: npm run typecheck --if-present

      - name: Run tests
        run: npm test

      - name: Build project
        run: npm run build
```

Do not copy this workflow blindly. Match the repository’s actual runtime, package manager, commands, runner labels, and supported branches.

---

# 14. Deployment and Release

Deployment and release rules apply only to repositories that deploy or publish artifacts.

Repositories that deploy should document:

- Environments.
- Deployment triggers.
- Deployment process.
- Required approvals.
- Validation steps.
- Rollback expectations.

## Branches and Environments

- `main` is the production source of truth.
- `release` is used for staging and production-like validation under the release-branch-first workflow.

## Release Tags

- **`[POLICY]`** Use release tags for meaningful release points.
- **`[POLICY]`** Do not reuse an existing release tag.
- **`[POLICY]`** Do not invent a release tag or build number.
- **`[GUIDELINE]`** Do not tag every commit, PR, or deleted branch.
- **`[GUIDELINE]`** Let CI generate version and build identifiers when the repository uses automated versioning.

## Automatic Versioning

Where configured:

- CI owns build-number generation.
- Build numbers must be unique.
- Build numbers must be monotonically increasing.
- Developers should not manually generate a CI-controlled build number.
- Release tags should reflect meaningful releases, not every test build.

When major, minor, and patch versions are used:

- Patch: backward-compatible fix or hotfix.
- Minor: backward-compatible feature or update.
- Major: breaking or backward-incompatible change.

---

# 15. Hotfix Process

A hotfix is an urgent production-level fix.

Urgency may expedite the process but does not remove traceability or required safety controls.

When preparing a hotfix:

1. Create a `hotfix/` branch.
2. Make the smallest safe correction.
3. Run required CI checks.
4. Run relevant QA checks.
5. Obtain review according to the actual risk level.
6. Deploy through the approved production process.
7. Tag the patch release when applicable.
8. Create follow-up issues for cleanup, tests, documentation, or root-cause remediation.

- **`[POLICY]`** Do not skip required human review for a High-risk hotfix unless an approved emergency exception applies.
- **`[GUIDELINE]`** Restore stability first, then complete follow-up improvements.
- **`[GUIDELINE]`** Keep emergency changes narrowly scoped.

---

# 16. Architecture Decision Records

Create an ADR for significant, long-term, or standards-deviating technical decisions.

Common ADR subjects include:

- Framework selection.
- Deployment platform.
- Authentication or session strategy.
- Mobile or wrapper strategy.
- Major vendor or tooling choice.
- Breaking architecture or compatibility change.
- CI/CD strategy.
- Significant data or integration architecture.

An ADR should document:

- Context.
- Decision drivers.
- Alternatives considered.
- Selected decision.
- Benefits.
- Costs and drawbacks.
- Accepted tradeoffs.
- Consequences.

- **`[GUIDELINE]`** Keep ADRs concise.
- **`[GUIDELINE]`** Store ADRs in Markdown under `docs/` or `docs/adr/`.
- **`[GUIDELINE]`** Use ADRs to encourage collaborative architectural awareness rather than relying on a single architect.

---

# 17. Spike Standards

A spike is a time-boxed research or prototype effort.

## Spike Rules

- **`[POLICY]`** Mark spike code as non-production.
- **`[POLICY]`** Do not promote spike code into production without review.
- **`[GUIDELINE]`** Begin with a testable question or hypothesis.
- **`[GUIDELINE]`** Define a narrow scope.
- **`[GUIDELINE]`** Use the simplest proof of concept needed to answer the question.
- **`[GUIDELINE]`** Time-box the spike to prevent unnecessary analysis.
- **`[GUIDELINE]`** Prefer a few days or a small number of weeks rather than open-ended research.
- **`[GUIDELINE]`** Do not turn a feasibility spike into an undocumented production scaffold.

A spike result should document:

- Question or hypothesis.
- Scope.
- What was tested.
- Evidence.
- Findings.
- Benefits.
- Drawbacks.
- Risks.
- Recommendation.
- Remaining unknowns.

---

# 18. Exceptions

Exceptions are allowed when the normal process does not fit the situation.

Examples include:

- Especially urgent hotfix.
- Critical security patch.
- Limited reviewer availability during an emergency.
- Template fields that genuinely do not apply.
- Immediate revert or rollback to restore stability.
- Temporary external tool or service outage.
- Approved release-specific process variation.

## Exception Rules

- **`[POLICY]`** Do not silently assume an exception.
- **`[POLICY]`** Identify the normal policy or guideline being bypassed.
- **`[POLICY]`** Use the smallest safe change.
- **`[GUIDELINE]`** Document the reason when possible.
- **`[GUIDELINE]`** Identify the approver when applicable.
- **`[GUIDELINE]`** Record the risk and follow-up work.
- **`[GUIDELINE]`** Perform follow-up review after emergency work.
- **`[GUIDELINE]`** Do not allow a temporary exception to become an undocumented permanent process.

When documenting an exception, use:

```md
## Exception

- **Reason:**
- **Normal process bypassed:**
- **Risk:**
- **Approver:**
- **Verification performed:**
- **Rollback or recovery plan:**
- **Follow-up issue:**
```

The general rule is to use discretion without sacrificing traceability, safety, or long-term process clarity.

---

# 19. Output-Specific Instructions

## When Asked for a Branch Name

Return:

```text
<type>/<issue-id>-<short-description>
```

Do not invent the issue ID. When no issue exists, either ask for one or omit it only for trivial work.

## When Asked for a Commit Message

Return:

```text
<type>(<scope>): <description>
```

Add a body only when meaningful context, reasoning, or compatibility information is needed.

## When Asked to Draft a PR

Use every required PR heading.

Do not omit sections. Use `N/A — <reason>` when something does not apply.

## When Asked to Review a PR

Use the structured review format.

Independently assess risk. Do not simply repeat the author-declared risk.

## When Asked to Create an Issue

Provide:

- Clear issue title.
- Problem or requested outcome.
- Context.
- Acceptance criteria.
- Primary type.
- Priority.
- Area or scope when useful.
- Risk when applicable.

## When Asked to Generate CI/CD

Inspect the repository first.

Use the actual:

- Runtime.
- Package manager.
- Lockfile.
- Build commands.
- Test commands.
- Target branches.
- Runner labels.
- Deployment boundaries.

Do not invent commands that are not supported by repository files or user instructions.

## When Asked to Prepare a Release

Do not invent a version or build number.

Determine whether versioning is:

- CI-generated.
- Repository-controlled.
- Release-tag-controlled.
- Managed by an external platform.

Respect the repository’s established mechanism.

---

# Final Rule

Prefer changes that are:

- Small.
- Safe.
- Complete.
- Traceable.
- Testable.
- Reviewable.
- Revertible.
- Proportional to risk.

Do not add unnecessary processes, but do not bypass required controls merely for speed.
