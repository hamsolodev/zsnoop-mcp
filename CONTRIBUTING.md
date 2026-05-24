# Contributing to zsnoop-mcp

Thanks for your interest. zsnoop-mcp is a small, opinionated tool; the
sections below cover everything you need to make a change land cleanly.

## Code of Conduct

This project adheres to the [Contributor Covenant](CODE_OF_CONDUCT.md).
By participating you agree to abide by it. Reports go to the email
listed in that file.

## About the codebase (AI authorship)

This project was developed collaboratively with [Claude
Code](https://claude.com/claude-code) under human review. AI-assisted
PRs are welcome — but please review the diff yourself before opening
it. Don't paste raw model output without checking that it compiles,
runs, and respects the security model below. A useful rule of thumb:
if you wouldn't be comfortable explaining the change in a code review,
neither would I be.

## Dev environment

```sh
git clone https://github.com/hamsolodev/zsnoop-mcp.git
cd zsnoop-mcp
uv sync                            # runtime + dev deps into .venv
uv sync --group docs               # add mkdocs deps when working on docs
uv run pre-commit install          # set up pre-commit hooks
```

Everything from here on runs through `uv run`; you don't need to
`source .venv/bin/activate`.

## The development loop

```sh
uv run pytest                      # tests
uv run pytest -k some_test_name    # one test
uv run ruff check                  # lint
uv run ruff format                 # format
uv run mypy                        # type-check (strict)
uv run pip-audit --skip-editable   # CVE scan of locked deps
uv run mkdocs serve                # docs site, live-reloaded
```

Pre-commit runs ruff, ruff-format, mypy, and (when `pyproject.toml`
or `uv.lock` change) `pip-audit` automatically on `git commit`. If a
hook fails, fix the underlying issue and re-stage — don't bypass.

## Adding a new tool

The end-to-end recipe lives in [docs/onboarding/10-extending.md](docs/onboarding/10-extending.md).
The short version: agent method → `METHODS` dict entry → allowlist
test update → method test → MCP tool wrapper → server registered-tools
test update → README table row → USAGE example. Each new tool should
have at least one happy-path test and one validation/error test.

## Security review checklist

Every change that touches a tool or method must answer these (also
documented in [docs/onboarding/08-security.md](docs/onboarding/08-security.md)):

- Is any new RPC method added to the agent's `METHODS` dict read-only?
  (G1)
- Does any new dataset/snapshot/path input route through the validators
  before it touches `subprocess` or the filesystem? (G2/G3)
- Does any new read have a default bound and a hard cap? (G4)
- Are any new error paths returning structured JSON-RPC errors, not raw
  stack traces? (G6)
- If sudo mode is the only way the change makes sense, is the tradeoff
  documented?

For anything security-sensitive, please *don't* open a public issue —
follow [SECURITY.md](docs/SECURITY.md) instead.

## Pull request workflow

1. Fork the repository, create a branch.
2. Make focused commits with descriptive messages. Reference the issue
   number if one exists.
3. Run the full development loop above; CI runs the same checks and
   will block merge on failure.
4. Open a PR with a clear description: what changed, why, how you
   verified it.
5. Code review: I'll respond. Squash-merge is the default; commit
   history is preserved on `main` via the merge commit's body.

Small, focused PRs land faster than sprawling ones. If you have several
changes in mind, please split them across PRs unless they're tightly
coupled.

## What I'm unlikely to merge

- Changes that broaden the dispatch allowlist with mutation operations
  (the entire project is read-only by construction; this isn't
  negotiable).
- Convenience features that significantly enlarge the attack surface
  (e.g. shell-style path expansion, eval of caller-supplied code).
- Adding heavy runtime dependencies. The agent is intentionally
  stdlib-only; the server has two runtime deps (`mcp`, `python-dateutil`)
  and that bar is high to raise.
- Refactors with no test coverage change and no behavioural rationale.

## Releases

Releases are cut by tagging `vX.Y.Z` on `main`. CI builds the wheel and
publishes to PyPI via trusted publishing. See [docs/PUBLISHING.md](docs/PUBLISHING.md)
for the pre-flight checklist.

## Questions

Open a discussion or an issue. Both are fine. Thanks again for
contributing.
