# Publishing to PyPI

Releases are cut by **pushing a `vX.Y.Z` tag** to GitHub. The CI
workflow at
[`.github/workflows/release.yml`](https://github.com/hamsolodev/zsnoop-mcp/blob/main/.github/workflows/release.yml)
builds the wheel + sdist, verifies the agent is force-included,
publishes to PyPI via OIDC trusted publishing, and creates a GitHub
Release with the CHANGELOG entry as the body.

You don't need an API token; nothing about the release path requires
credentials living anywhere on your machine.

## Per-release checklist

For each new version (e.g. `v0.1.1`, `v0.2.0`):

1. **Bump the version** in `pyproject.toml` (`version = "X.Y.Z"`).
2. **Add a CHANGELOG entry.** Put a new `## [X.Y.Z] — YYYY-MM-DD` section
   above the prior one, summarising what changed. Update the link
   references at the bottom of the file.
3. **Local pre-flight** (also runs in CI but quicker to catch here):
   ```sh
   uv run pytest                      # full suite green
   uv run ruff check && uv run ruff format --check
   uv run mypy
   uv run pip-audit --skip-editable   # no new CVEs against pinned deps
   uv run mkdocs build --strict       # docs still build cleanly
   rm -rf dist/ && uv build           # wheel + sdist build cleanly
   unzip -l dist/zsnoop_mcp-*.whl | grep _agent_source  # agent included
   ```
4. **Commit, tag, push:**
   ```sh
   git commit -am "release: vX.Y.Z"
   git tag vX.Y.Z
   git push origin main vX.Y.Z
   ```
5. **Watch the Release workflow** at
   <https://github.com/hamsolodev/zsnoop-mcp/actions/workflows/release.yml>.
   Three jobs in sequence: `build` → `publish` → `github-release`.

6. **Verify the published package:**
   - PyPI: <https://pypi.org/project/zsnoop-mcp/>
   - GitHub Release: <https://github.com/hamsolodev/zsnoop-mcp/releases>
   - Post-publish smoke test (see below).

## Post-publish smoke test

In a fresh shell with no source checkout in scope:

```sh
uv tool install --force zsnoop-mcp
zsnoop-mcp --help                                          # CLI loads
ZSNOOP_CONFIG=/tmp/does-not-exist.toml zsnoop-mcp          # helpful missing-config message
```

Then point at your real `hosts.toml` and exercise it via Claude Code
or the verification snippet in [INSTALL.md](INSTALL.md).

## If something goes wrong

- **Build job fails:** no release happens. Fix locally, push again — the
  tag still points at the broken commit, so either move the tag
  (`git tag -f vX.Y.Z; git push --force origin vX.Y.Z`, only safe
  if nothing on PyPI was published yet) or bump to `vX.Y.Z+1` and
  re-tag.
- **Publish step fails after a successful build** (e.g. version already
  exists on PyPI, OIDC misconfigured): the GitHub Release won't be
  created. Diagnose, then push a fresh tag with a higher version. Once
  a version is published to PyPI it can be *yanked* but not deleted, so
  versions are one-shot.
- **GitHub Release creation fails** after a successful PyPI publish: the
  package is live but the GH Release is missing. Recreate it manually
  (`gh release create vX.Y.Z dist/*`) or via the GitHub UI.

## One-time setup (already done for this repo)

These steps were completed before the first release; they're documented
here for forks or recreating the setup elsewhere.

### GitHub Environment

Repo → Settings → Environments → **New environment** → name `pypi`.
Under *Deployment branches and tags* → **Selected tags** → pattern
`v*.*.*`. This restricts the `pypi` deployment environment so it can
only be entered by workflow runs triggered by a version tag.

### PyPI trusted publisher

On pypi.org, sign in, then:

- For a brand-new project that doesn't yet exist on PyPI: account
  page → **Your projects** → **Publishing** in the sidebar → *Add a
  new pending publisher*.
- For an existing project: project page → Settings → Publishing →
  *Add a new publisher*.

Fill in:

| Field | Value |
| --- | --- |
| Owner | `hamsolodev` |
| Repository name | `zsnoop-mcp` |
| Workflow filename | `release.yml` |
| Environment name | `pypi` |

The first successful tag push creates the project on PyPI (for the
pending-publisher case) or pushes a new version (for the existing-
project case).

### Manual fallback (only if CI is broken)

If you can't push through CI for some reason, the
[`uv publish`](https://docs.astral.sh/uv/guides/package/#publishing-your-package)
escape hatch is:

```sh
rm -rf dist/ && uv build
uv publish --token <pypi-api-token>
```

Generate a project-scoped token at
<https://pypi.org/manage/account/token/> and pass it via
`UV_PUBLISH_TOKEN` env var or `--token`. Note that this stores a token
in your shell history / env; prefer the CI path unless CI itself is
down.

## TestPyPI dry run

For risky releases (major version bumps, build-system changes), publish
to TestPyPI first to catch metadata or wheel-shape errors before the
real one:

```sh
uv build
uv publish --publish-url https://test.pypi.org/legacy/ --token <test-token>
pip install --index-url https://test.pypi.org/simple/ \
            --extra-index-url https://pypi.org/simple/ \
            zsnoop-mcp
```

(The `--extra-index-url` is so transitive deps resolve from real
PyPI; TestPyPI doesn't mirror them.)
