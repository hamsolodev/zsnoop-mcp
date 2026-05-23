# Onboarding tutorial

Welcome. This tutorial walks you through the project the way I'd explain it
to a new contributor sitting next to me — start with the big picture, then
zoom into each layer, then put it all together by adding a feature.

## How to use this tutorial

Each section is structured the same way:

!!! tip "Section structure"
    - **What** — the one-sentence description of the layer or topic.
    - **Why** — what problem it solves; what would be worse without it.
    - **How** — a guided code tour with the actual file:line references.

You can read top-to-bottom, or jump to whichever layer you're touching today.

## Suggested reading order

| # | Section | Read it when… |
| --- | --- | --- |
| 1 | [What this project is](01-what.md) | first time, or when explaining MCP to someone |
| 2 | [The remote agent](02-agent.md) | adding a new RPC method; debugging a remote crash |
| 3 | [The transport](03-transport.md) | touching SSH, subprocesses, JSON-RPC framing |
| 4 | [The MCP server](04-server.md) | adding a new tool, changing tool I/O |
| 5 | [Configuration](05-config.md) | adding a new config field; changing validation |
| 6 | [Time parsing](06-timeparse.md) | adding a new time-range parameter |
| 7 | [Testing patterns](07-testing.md) | writing tests; the fixtures don't make sense |
| 8 | [Security model](08-security.md) | reviewing a change for safety; threat-modelling |
| 9 | [Build, package, release](09-build.md) | bumping a version; cutting a release |
| 10 | [Adding a new tool](10-extending.md) | doing the actual end-to-end work; this is the worked example that ties it together |

## Conventions in this tutorial

- Filenames are linked directly — clicking takes you to the source on disk.
- Code blocks are excerpts. They're faithful but trimmed; check the file for
  the surrounding context.
- "We" means "the code authors and you, working together". "You" usually
  means the next change you'll make.
- Every claim about a guarantee is paired with the test that proves it.
