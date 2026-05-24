# Publishing to PyPI

This project is structured so it can be installed from PyPI today. What's
missing before a first release is essentially: a public source location, a
decision on the version-bump workflow, and a credential to publish with.

## Pre-flight checklist

- [x] Build backend (`hatchling`) configured in `pyproject.toml`.
- [x] PEP 621 metadata: `name`, `version`, `description`, `readme`, `license`,
      `authors`, `requires-python`, `classifiers`.
- [x] `agent/zfs_snoop_agent.py` is force-included into the wheel at
      `zsnoop_mcp/_agent_source/zfs_snoop_agent.py`, and `find_agent_source()`
      reads it via `importlib.resources` at runtime.
- [x] Entry point `zsnoop-mcp = zsnoop_mcp.__main__:main` defined so
      `uv tool install zsnoop-mcp` exposes the CLI.
- [x] MIT license shipped at the project root.
- [ ] Public source location (PyPI's metadata wants a `Homepage` URL that
      isn't a `*.lan` address — see [§ Source hosting](#source-hosting)).
- [ ] A PyPI account + project name reservation.

Verify a fresh build:

```sh
rm -rf dist/
uv build
unzip -l dist/zsnoop_mcp-*.whl   # confirm agent is in there
```

CVE scan the locked dependency tree against the PyPI advisory database
before publishing:

```sh
uv run pip-audit --skip-editable
```

This runs automatically in pre-commit whenever `pyproject.toml` or
`uv.lock` change, but re-run it manually here — a vulnerability may have
been published against an unchanged pinned dep since your last commit.
Address findings by bumping the dep (`uv lock --upgrade-package <name>`)
and re-running, or by acknowledging a specific advisory ID via
`--ignore-vuln <ID>` with a note explaining why.

Expected wheel layout:

```text
zsnoop_mcp/__init__.py
zsnoop_mcp/__main__.py
zsnoop_mcp/_agent_source/zfs_snoop_agent.py
zsnoop_mcp/config.py
zsnoop_mcp/server.py
zsnoop_mcp/timeparse.py
zsnoop_mcp/transport.py
zsnoop_mcp-0.1.0.dist-info/...
```

## Source hosting

PyPI doesn't require a public source URL, but `pip install` users will look
for one in the project's PyPI page. Options:

- **GitHub mirror** — push to `github.com/<you>/zsnoop-mcp` and set
  `[project.urls] Source = "https://github.com/<you>/zsnoop-mcp"` in
  `pyproject.toml`. Enables OIDC-based "trusted publishing" (see below).
- **Self-hosted public** — expose the Forgejo instance on c3po at a public
  URL, OR host a public mirror somewhere; either works.
- **No public source** — release a wheel/sdist with an internal URL in
  metadata. Works but discoverability is poor.

## Version-bump workflow

Pick one:

1. **Hand-bump** (current). Edit `version = "0.1.0"` in `pyproject.toml`,
   commit, tag `v0.1.0`, build, publish. Simple, explicit.
2. **`hatch-vcs`** derives the version from the latest git tag. Requires
   adding to `[build-system] requires` and `[tool.hatch.version]`. Means
   `git tag v0.2.0 && uv build` is sufficient; pyproject.toml doesn't change.

Hand-bump is enough at v0.x; consider `hatch-vcs` once releases happen
regularly.

## Publishing

### Option A — manual `uv publish` with API token

One-time:

1. Create a PyPI account, create a project-scoped API token under
   `https://pypi.org/manage/account/token/`.
2. Store it locally (e.g. `pass insert pypi/zsnoop-mcp`).

Each release:

```sh
# bump version in pyproject.toml, then:
git commit -am "release: v0.1.0"
git tag -a v0.1.0 -m "v0.1.0"
git push --follow-tags
rm -rf dist/
uv build
UV_PUBLISH_TOKEN=$(pass pypi/zsnoop-mcp) uv publish
```

### Option B — trusted publishing via GitHub Actions (recommended)

Higher-friction first time, but no long-lived secret stored anywhere
afterwards. The workflow at
[`.github/workflows/release.yml`]({{ config.repo_url }}{{ source_url_prefix }}/{{ repo_branch }}/.github/workflows/release.yml)
implements this; you only need to wire it up on the GitHub and PyPI
sides once.

**One-time setup:**

1. **Push the repo to GitHub** at `hamsolodev/zsnoop-mcp`.
2. **Create the `pypi` environment.** Repo → Settings → Environments →
   New environment → name `pypi`. Optionally restrict deployments to
   tags matching `v*.*.*` for an extra safety net.
3. **Add a PyPI trusted publisher.** On pypi.org (after creating the
   project, which can be done by uploading once manually OR by adding
   a *pending publisher* before any release exists):
   PyPI project page → Settings → Publishing → *Add a new pending
   publisher*. Fill in:
   - Owner: `hamsolodev`
   - Repository: `zsnoop-mcp`
   - Workflow filename: `release.yml`
   - Environment name: `pypi`

**Per-release:**

1. Bump the version in `pyproject.toml` and add a new section to
   `CHANGELOG.md`.
2. Commit, then tag:

   ```sh
   git tag v0.1.0
   git push origin main v0.1.0
   ```

3. The workflow:
   - Builds wheel + sdist, asserts the agent is present in the wheel.
   - Publishes to PyPI via OIDC (no token).
   - Creates a GitHub Release with the artifacts attached and the
     CHANGELOG.md entry for that version as the release body.

If the build job fails, no release happens. If the publish step fails
after build succeeds (e.g. version already exists on PyPI), fix the
underlying issue, delete the tag (`git push --delete origin v0.1.0;
git tag -d v0.1.0`), bump the version, and try again. Once a version
is published to PyPI it can be *yanked* but not deleted, so be sure
about the version number before tagging.

### TestPyPI dry run

Before either approach, validate against TestPyPI to catch metadata errors:

```sh
uv publish --publish-url https://test.pypi.org/legacy/ --token <test-token>
pip install -i https://test.pypi.org/simple/ zsnoop-mcp
```

## Post-publish smoke test

Fresh venv, somewhere with no source checkout in scope:

```sh
uv tool install zsnoop-mcp
zsnoop-mcp --help                    # CLI loads
ZSNOOP_CONFIG=/tmp/empty.toml zsnoop-mcp --config /tmp/empty.toml
# should fail with "config must have a [hosts] table" — confirms config path works
```

Then point at your real `hosts.toml` and exercise it via Claude Code or the
verification snippet in [INSTALL.md](INSTALL.md#verify).
