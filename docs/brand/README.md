# Brand assets

Candidate wordmarks for the product name, and how they were made.

## What is here

| Path | What it is |
| --- | --- |
| `wordmark-candidates.html` | The specimen sheet. Open it in a browser — each candidate is shown at full size, at 18 mm in mono for a printed gate pass, at 17 px on the slate app header, and as a 32 px browser tab. |
| `wordmark-candidates.png` | The same sheet as an image, for viewing without a browser. |
| `candidates/*.svg` | The marks themselves. Vector outlines, one flat fill, tight viewBox. |
| `candidates/*-Y.svg` | The compact form of each — the bare `Y`, cut from the same outlines. |

**Chosen: `archivo-condensed-700`** — Archivo Semi-Condensed Bold. It reads as
infrastructure rather than software, and at 4.43:1 it fits a phone header at full
size instead of being shrunk to get there.

It is wired in: the login screen, the app-shell sidebar, the phone header (as the
compact `Y`), the four printed documents, the favicon and the PWA icons. The two
marks live in `frontend/src/components/Logo.tsx`; see README.md at the repository
root for the full map, and design §7.3a for why.

The other six stay here as the record of what was considered.

## Why these are SVG and not generated images

A wordmark is type. These were set in a real typeface, shaped with HarfBuzz so
the kerning is the typeface designer's rather than a guess, then converted to
outlines — which is what makes them resolution-independent. That matters twice
over here: a gate pass prints the mark at 18 mm on a mono laser printer, and the
browser tab renders it at 32 px. An image asset is soft at one end and a smudge
at the other.

The `-Y` files exist because the full word is about five times wider than it is
tall. Nothing that shape survives a 32 px tab, so the tab and the Android
home-screen icon need a compact form. It is a second asset cut from the same
outlines, not a different logo.

## Typefaces and licence

All four are open-licence (SIL Open Font License 1.1), which permits commercial
use, embedding and conversion to outlines:

- [Archivo](https://github.com/google/fonts/tree/main/ofl/archivo)
- [Manrope](https://github.com/google/fonts/tree/main/ofl/manrope)
- [Space Grotesk](https://github.com/google/fonts/tree/main/ofl/spacegrotesk)
- [Sora](https://github.com/google/fonts/tree/main/ofl/sora)

Because the marks are outlines, the chosen font file does not ship with the app
and nothing has to be licensed at runtime.

## Regenerating

The generator lives beside this file:

```bash
pip install fonttools uharfbuzz
mkdir -p fonts && cd fonts
# variable font files, one per family, from the links above
cd ..
python build.py    # writes out/*.svg
python sheet.py    # writes sheet.html
```

`build.py` holds the candidate list: typeface, weight, width axis and tracking.
Adding a candidate is one row.

`wire.py` is the third step — it takes the chosen candidate and writes every
consumer from it: the React component, the favicon and maskable icon (centred on
the glyph's own bounds, not by eye), the standalone wordmark, and the Django
include used by the printed documents. Change `CHOICE` at the top and re-run to
swap the mark everywhere at once:

```bash
python wire.py
cd ../../frontend && node scripts/rasterize-icons.mjs   # then the PNGs
```
