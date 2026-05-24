<!--
Thanks for opening a PR. Brief is fine — see CONTRIBUTING.md for the
full workflow. Delete sections that don't apply.
-->

## What changed

<!-- One or two sentences. Focus on the *why* rather than restating the diff. -->

## How I verified it

<!--
- New / updated tests
- Manual smoke tests run (`uv run pytest`, `uv run mkdocs build --strict`, etc.)
- E2E against a real ZFS host (if applicable; brief output excerpt is welcome)
-->

## Security checklist (if this touches a tool or method)

<!-- See docs/onboarding/08-security.md for the full reviewer checklist. -->

- [ ] Any new RPC method added to `METHODS` is **read-only**. (G1)
- [ ] Any new dataset / snapshot / path input routes through the
      validators before touching `subprocess` or the filesystem. (G2/G3)
- [ ] Any new read has a default bound **and** a hard cap. (G4)
- [ ] Any new error path returns a structured JSON-RPC error, not a raw
      stack trace. (G6)
- [ ] If the change only makes sense in sudo mode, the tradeoff is
      documented.

## Related issues

<!-- Closes #N -->
