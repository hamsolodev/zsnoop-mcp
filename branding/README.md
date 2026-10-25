# Branding

zsnoop-mcp's icon: snapshot layers under a magnifying glass, under the tropism sun, on a
light tile. It is drawn the way jot's, vitrine's, imgism's, meshism's and the svc icon are
(vitrine's `branding/README.md` is the reference), so it reads as one of that family: the
same tile, the same sun in the same place, the subject in the accent's blue. The layers are
ZFS snapshots, searched and read back.

The owner chose it on 2026-10-26 from the first round of candidates drawn for eleven repos
and Gitea, each shown at Home Screen size, at 32 and 16 px from its small version, and in a
row beside the family.

| File | Used at |
|---|---|
| `icon.svg` | 48 pixels and up: the top of the README, the Gitea repo avatar |
| `icon-small.svg` | 32 pixels and below. The sun loses its rays and the stack one of its layers, and the strokes double, or they blur away at that size |

Each file has the same two parts: `#tile` and `#mark`.

The README shows the icon from the public GitHub repository, `https://raw.githubusercontent.com/hamsolodev/zsnoop-mcp/main/branding/icon.svg`, not by a relative
path: the README is also the PyPI project page, where a relative path does not resolve, and
its other links point at GitHub for the same reason. The image appears once `main` with
`branding/` is on GitHub.

## Colours

jot's palette in its light theme, which vitrine, imgism and the svc pages took too:

| Colour | Token (jot) | Where |
|---|---|---|
| `#f6f7f9` | `--bg` | the tile |
| `#1f5f8b` | `--accent` | every outline |
| `#ffffff` | `--card` | the top layer and the magnifying glass |
| `#e09b12` | | the sun: the tropism sun, vitrine's and imgism's amber |

## The Gitea avatar

The repo avatar is `icon.svg` rendered at 1024 px and downsampled to 512, the way the
family's PNGs are made, keeping the tile's rounded corners transparent:

```bash
magick -background none -density 1536 branding/icon.svg -resize 512x512 avatar.png
```

The PNG is not committed: it is generated, and it lives in Gitea (repo Settings → Avatar,
or `POST /api/v1/repos/mch/zsnoop-mcp/avatar`). Redo it after any change here.
