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

### Option B — trusted publishing via GitHub Actions

Higher-friction first time, but no long-lived secret stored anywhere.

1. Push the repo to GitHub (mirror is fine).
2. Add a publisher to the PyPI project: *Settings → Publishing → Add a new
   pending publisher*, fill in `owner/repo`, workflow filename, environment
   name.
3. Add `.github/workflows/release.yml` (or `.forgejo/workflows/release.yml`
   for Forgejo Actions, syntax-compatible) along these lines:

   ```yaml
   name: release
   on:
     push:
       tags: ["v*"]
   jobs:
     publish:
       runs-on: ubuntu-latest
       environment: pypi
       permissions:
         id-token: write
       steps:
         - uses: actions/checkout@v4
         - uses: astral-sh/setup-uv@v3
         - run: uv build
         - uses: pypa/gh-action-pypi-publish@release/v1
   ```

4. Tag and push as in option A; CI publishes.

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
