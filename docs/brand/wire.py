"""Wire the chosen mark into the app: React component, static assets, print partial.

One source of outlines, four consumers. The wordmark is emitted with
`fill="currentColor"` wherever it sits inside markup that already has a colour
(the app shell, the login screen, a printed document), and with an explicit navy
only in the standalone files, which have no cascade to inherit from.
"""

from __future__ import annotations

import re
from pathlib import Path

BRAND = Path("c:/Users/brian.ambeyi/PycharmProjects/yardflow/docs/brand/candidates")
FRONTEND = Path("c:/Users/brian.ambeyi/PycharmProjects/yardflow/frontend")
BACKEND = Path("c:/Users/brian.ambeyi/PycharmProjects/yardflow/backend")

CHOICE = "archivo-condensed-700"
NAVY = "#0f172a"
PAPER = "#f8fafc"


def read(name: str) -> tuple[str, str]:
    """(viewBox, path) from a generated candidate."""
    svg = (BRAND / f"{name}.svg").read_text(encoding="utf-8")
    view = re.search(r'viewBox="([^"]+)"', svg).group(1)
    path = re.search(r'd="([^"]+)"', svg).group(1)
    return view, path


word_view, word_path = read(CHOICE)
mark_view, mark_path = read(f"{CHOICE}-Y")

# -- 1. The React component ---------------------------------------------------
# Inline rather than an <img>: the mark then inherits colour from whatever it
# sits in (slate text on the login screen, near-white on the dark shell) and
# costs no second request on a phone with one bar of signal.
component = f'''/**
 * The product mark.
 *
 * Archivo Semi-Condensed Bold, converted to outlines — so there is no webfont to
 * load and nothing to fall back to. Both marks are drawn with
 * `fill="currentColor"`, which is what lets one asset serve the light login
 * screen and the dark app shell without a second colour variant.
 *
 * `Wordmark` is the full name and needs roughly 4.4 times its height in width.
 * `LogoMark` is the bare Y, for anywhere too small or too square for the word —
 * the browser tab, the Android home screen, a collapsed sidebar. It is cut from
 * the same outlines, so the two never drift apart.
 *
 * Source and alternatives: docs/brand/.
 */

type Props = {{
  /** Extra classes. Set the height and let the width follow the aspect ratio. */
  className?: string;
  /**
   * Announce it to a screen reader. On by default, because in the header and on
   * the login screen the mark *is* the name; pass false where visible text
   * already says "YardFlow" and a second announcement would only repeat it.
   */
  labelled?: boolean;
}};

function labelling(labelled: boolean) {{
  return labelled
    ? {{ role: 'img' as const, 'aria-label': 'YardFlow' }}
    : {{ 'aria-hidden': true as const, focusable: false as const }};
}}

export function Wordmark({{ className = 'h-5 w-auto', labelled = true }}: Props) {{
  return (
    <svg
      viewBox="{word_view}"
      className={{className}}
      fill="currentColor"
      {{...labelling(labelled)}}
    >
      <path d="{word_path}" />
    </svg>
  );
}}

export function LogoMark({{ className = 'h-5 w-auto', labelled = true }}: Props) {{
  return (
    <svg
      viewBox="{mark_view}"
      className={{className}}
      fill="currentColor"
      {{...labelling(labelled)}}
    >
      <path d="{mark_path}" />
    </svg>
  );
}}
'''
(FRONTEND / "src/components/Logo.tsx").write_text(component, encoding="utf-8")

# -- 2. Static assets ---------------------------------------------------------
# The favicon carries its own ground: a tab icon is composited onto whatever
# chrome the browser is wearing, and a bare navy Y disappears in dark mode.
BOX = 512


def icon(fraction: float, radius: int) -> str:
    """A square icon with the Y centred on its own bounds.

    `fraction` is how much of the box's height the letter occupies. Computed from
    the glyph box rather than eyeballed, because a Y that sits two per cent low
    reads as broken at 32 px and is invisible at 512.
    """
    x, y, w, h = (float(n) for n in mark_view.split())
    scale = (BOX * fraction) / h
    # Centre of the glyph box, in glyph coordinates, y flipped for SVG.
    cx, cy = x + w / 2, y + h / 2
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {BOX} {BOX}" '
        f'role="img" aria-label="YardFlow">\n'
        f"  <title>YardFlow</title>\n"
        f'  <rect width="{BOX}" height="{BOX}" rx="{radius}" fill="{NAVY}"/>\n'
        f'  <g transform="translate({BOX / 2:.1f} {BOX / 2:.1f}) '
        f"scale({scale:.4f}) translate({-cx:.1f} {-cy:.1f})\">\n"
        f'    <path fill="{PAPER}" d="{mark_path}"/>\n'
        f"  </g>\n</svg>\n"
    )


# A tab icon is not cropped, so it can fill more of the square.
(FRONTEND / "public/favicon.svg").write_text(icon(0.60, 96), encoding="utf-8")
# Maskable: Android crops to a circle and may eat the outer 10% on each side, so
# the letter stays well inside the safe zone and the slate runs to the edge.
(FRONTEND / "public/icon-maskable.svg").write_text(icon(0.42, 0), encoding="utf-8")

standalone = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="{word_view}" role="img" aria-label="YardFlow">
  <title>YardFlow</title>
  <path fill="{NAVY}" d="{word_path}"/>
</svg>
'''
(FRONTEND / "public/logo-wordmark.svg").write_text(standalone, encoding="utf-8")

# -- 3. The printed documents -------------------------------------------------
# An include rather than a copy: four documents share it, and a logo that is
# right on the gate pass and stale on the waybill is worse than no logo.
_w_mm = 16.0
_x, _y, _vw, _vh = (float(n) for n in word_view.split())
_h_mm = _w_mm * _vh / _vw

partial = f'''{{% comment %}}
The product mark for a printed document (§7). Inline SVG, one flat fill, no
external file — a gate pass is often printed from a phone through a driver's
hotspot, and an <img> that has not loaded prints as a broken box.

Two details are for WeasyPrint, which renders these to PDF. The fill is a
literal black rather than `currentColor`, because inheriting a colour through an
inline SVG is exactly the kind of thing a print engine gets wrong, and paper is
black anyway. Both dimensions are stated in millimetres rather than leaving the
height to the aspect ratio, for the same reason — {_w_mm:g}mm wide is legible on the
mono laser printer in a gate house.
{{% endcomment %}}<svg viewBox="{word_view}" width="{_w_mm:g}mm" height="{_h_mm:.2f}mm"
     xmlns="http://www.w3.org/2000/svg" role="img" aria-label="YardFlow"
     style="vertical-align:-0.6mm">
  <path fill="#000000" d="{word_path}"/>
</svg>
'''
# Not a byte of whitespace at either end. This include sits *inside a sentence*
# ("Produced by X."), and a stray newline renders as a space — "YardFlow ." with
# a gap before the full stop. Which is why `{% endcomment %}` and `<svg` share a
# line above, and why this strips the end.
(BACKEND / "templates/documents/_wordmark.html").write_text(
    partial.strip(), encoding="utf-8", newline=""
)

for target in [
    FRONTEND / "src/components/Logo.tsx",
    FRONTEND / "public/favicon.svg",
    FRONTEND / "public/icon-maskable.svg",
    FRONTEND / "public/logo-wordmark.svg",
    BACKEND / "templates/documents/_wordmark.html",
]:
    print(f"{target.stat().st_size:>7,} bytes  {target}")
