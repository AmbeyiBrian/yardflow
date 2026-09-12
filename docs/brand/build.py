"""Build real vector wordmarks for "YardFlow".

A wordmark is type, so this sets the word in a properly licensed open font,
shapes it with HarfBuzz (so the kerning is the typeface designer's, not a
guess), converts the glyphs to outlines, and writes an SVG whose viewBox is
tight to the letterforms. The result is resolution-independent: crisp at 18 mm
on a printed gate pass and at 32 px in a browser tab, which is exactly what the
generated PNG could not be.
"""
from __future__ import annotations

import json
from pathlib import Path

import uharfbuzz as hb
from fontTools.misc.transform import Transform
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.recordingPen import RecordingPen
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

HERE = Path(__file__).parent
FONTS = HERE / "fonts"
OUT = HERE / "out"
OUT.mkdir(exist_ok=True)

NAVY = "#0F172A"


def instantiate(path: Path, axes: dict) -> TTFont:
    font = TTFont(path)
    if "fvar" in font and axes:
        font = instancer.instantiateVariableFont(font, axes, inplace=False, updateFontNames=False)
    return font


def shape(font: TTFont, path: Path, axes: dict, text: str):
    """(glyph name, x-offset) pairs, with the typeface's own kerning applied."""
    blob = hb.Blob.from_file_path(str(path))
    face = hb.Face(blob)
    hbfont = hb.Font(face)
    if axes:
        hbfont.set_variations(axes)
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(hbfont, buf)
    order = font.getGlyphOrder()
    out, x = [], 0
    for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
        out.append((order[info.codepoint], x + pos.x_offset))
        x += pos.x_advance
    return out, x


def build(name: str, runs: list[dict], tracking: int, note: str) -> dict:
    """runs = [{'text', 'font', 'axes'}] — one run per weight."""
    recording = RecordingPen()
    cursor = 0
    upem = 1000
    for run in runs:
        path = FONTS / run["font"]
        font = instantiate(path, run["axes"])
        upem = font["head"].unitsPerEm
        glyphset = font.getGlyphSet()
        placed, advance = shape(font, path, run["axes"], run["text"])
        for index, (glyph, x) in enumerate(placed):
            pen = TransformPen(recording, Transform().translate(cursor + x + tracking * index, 0))
            glyphset[glyph].draw(pen)
        cursor += advance + tracking * len(placed)

    # Tight bounds, so the asset has no arbitrary whitespace baked in.
    bounds = BoundsPen(None)
    recording.replay(bounds)
    xmin, ymin, xmax, ymax = bounds.bounds

    # Flip to SVG's y-down space and pad by a fraction of the cap height.
    pad = round(upem * 0.08)
    svgpen = SVGPathPen(None, ntos=lambda v: f"{v:.1f}")
    recording.replay(TransformPen(svgpen, Transform().scale(1, -1)))
    d = svgpen.getCommands()

    width = (xmax - xmin) + pad * 2
    height = (ymax - ymin) + pad * 2
    view = f"{xmin - pad:.0f} {-ymax - pad:.0f} {width:.0f} {height:.0f}"

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view}" '
        f'role="img" aria-label="YardFlow">\n'
        f'  <title>YardFlow</title>\n'
        f'  <path fill="{NAVY}" d="{d}"/>\n</svg>\n'
    )
    (OUT / f"{name}.svg").write_text(svg, encoding="utf-8")
    return {"name": name, "note": note, "ratio": width / height, "path": d, "view": view}


# "Yard" and "Flow" one step apart, not Bold-next-to-Light: the pair has to read
# as one word. Single-weight settings are here too, because for a wordmark that
# is usually the stronger answer.
CANDIDATES = [
    ("archivo-700", [{"text": "YardFlow", "font": "Archivo.ttf", "axes": {"wght": 700, "wdth": 100}}],
     -8, "Archivo Bold, one weight. Squared industrial grotesque."),
    ("archivo-700-500", [
        {"text": "Yard", "font": "Archivo.ttf", "axes": {"wght": 700, "wdth": 100}},
        {"text": "Flow", "font": "Archivo.ttf", "axes": {"wght": 500, "wdth": 100}}],
     -8, "Archivo, Bold + Medium. One step apart, so it still reads as one word."),
    ("archivo-condensed-700", [{"text": "YardFlow", "font": "Archivo.ttf", "axes": {"wght": 700, "wdth": 84}}],
     -6, "Archivo Semi-Condensed Bold. Narrow — fits a phone header."),
    ("spacegrotesk-700", [{"text": "YardFlow", "font": "SpaceGrotesk.ttf", "axes": {"wght": 700}}],
     -10, "Space Grotesk Bold. Distinctive a, r and w."),
    ("manrope-800", [{"text": "YardFlow", "font": "Manrope.ttf", "axes": {"wght": 800}}],
     -12, "Manrope ExtraBold. Rounded geometric, very legible small."),
    ("manrope-800-600", [
        {"text": "Yard", "font": "Manrope.ttf", "axes": {"wght": 800}},
        {"text": "Flow", "font": "Manrope.ttf", "axes": {"wght": 600}}],
     -12, "Manrope, ExtraBold + SemiBold."),
    ("sora-700", [{"text": "YardFlow", "font": "Sora.ttf", "axes": {"wght": 700}}],
     -10, "Sora Bold. Geometric, engineered feel."),
]

built = [build(*c) for c in CANDIDATES]
(HERE / "built.json").write_text(json.dumps(built, indent=1), encoding="utf-8")
for b in built:
    print(f"{b['name']:26} aspect {b['ratio']:.2f}:1  {len(b['path'])} chars of path")

# A wordmark five times wider than it is tall cannot be a 32px browser tab icon —
# it becomes a smudge. Every family therefore also needs a compact form, so build
# the bare Y from the same outlines: same typeface, same weight, no redrawing.
compact = []
for name, runs, tracking, note in CANDIDATES:
    single = [dict(runs[0], text="Y")]
    compact.append(build(f"{name}-Y", single, 0, f"Compact form — {note}"))
(HERE / "compact.json").write_text(json.dumps(compact, indent=1), encoding="utf-8")
print(f"\n{len(compact)} compact forms built")
