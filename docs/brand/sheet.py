"""Render the specimen sheet: every candidate under the conditions it has to survive."""

import json
from pathlib import Path

HERE = Path(__file__).parent
marks = {m["name"]: m for m in json.loads((HERE / "built.json").read_text())}
compact = {m["name"]: m for m in json.loads((HERE / "compact.json").read_text())}

SPECS = {
    "archivo-700": ("Archivo", "Bold 700", "one weight", False),
    "archivo-700-500": ("Archivo", "Bold 700 + Medium 500", "two weights, one step apart", False),
    "archivo-condensed-700": ("Archivo", "Bold 700, width 84", "one weight, semi-condensed", True),
    "spacegrotesk-700": ("Space Grotesk", "Bold 700", "one weight", False),
    "manrope-800": ("Manrope", "ExtraBold 800", "one weight", False),
    "manrope-800-600": ("Manrope", "ExtraBold 800 + SemiBold 600", "two weights, two steps apart", False),
    "sora-700": ("Sora", "Bold 700", "one weight", False),
}

VERDICTS = {
    "archivo-700": "Squared terminals and a tight, even rhythm. Reads as infrastructure rather than as a startup.",
    "archivo-700-500": "The two-weight idea done properly &mdash; Bold beside Medium, so the halves still bind into one word.",
    "archivo-condensed-700": "The same voice, narrower. Survives the phone header at full size instead of being scaled down to fit it.",
    "spacegrotesk-700": "Character in the a, r and w. Distinctive, and a little more software than yard.",
    "manrope-800": "Round, open counters &mdash; the most legible of the set at small sizes, and the least industrial.",
    "manrope-800-600": "Two steps apart is already too far: the halves start to separate again, which was the original fault.",
    "sora-700": "Engineered and wide. The w is the feature; it is also the first thing to muddy in print.",
}

ORDER = [
    "archivo-condensed-700",
    "archivo-700",
    "archivo-700-500",
    "spacegrotesk-700",
    "manrope-800",
    "manrope-800-600",
    "sora-700",
]


def svg(mark, cls, **attrs):
    a = " ".join(f'{k}="{v}"' for k, v in attrs.items())
    return (
        f'<svg class="{cls}" {a} viewBox="{mark["view"]}" xmlns="http://www.w3.org/2000/svg" '
        f'role="img" aria-label="YardFlow"><path fill="currentColor" d="{mark["path"]}"/></svg>'
    )


def card(key):
    mark, comp = marks[key], compact[key + "-Y"]
    family, weight, structure, pick = SPECS[key]
    badge = '<p class="pick">Recommended</p>' if pick else ""
    return f"""
      <article class="card{' card--pick' if pick else ''}">
        <header class="card__head">
          <div>
            <h2>{family}</h2>
            <p class="spec">{weight} &middot; {structure} &middot; {mark['ratio']:.2f}&thinsp;:&thinsp;1</p>
          </div>
          {badge}
        </header>

        <div class="paper">{svg(mark, "mark", height="58")}</div>
        <p class="verdict">{VERDICTS[key]}</p>

        <div class="tests">
          <div class="test">
            <div class="test__stage test__stage--paper mono-black">{svg(mark, "mark", width="68")}</div>
            <p class="test__label">Gate pass<span>18&thinsp;mm, mono laser</span></p>
          </div>
          <div class="test">
            <div class="test__stage test__stage--slate">{svg(mark, "mark", height="17")}</div>
            <p class="test__label">App header<span>17&thinsp;px on slate</span></p>
          </div>
          <div class="test">
            <div class="test__stage test__stage--paper"><div class="tab">{svg(comp, "mark", height="20")}</div></div>
            <p class="test__label">Browser tab<span>32&thinsp;px compact form</span></p>
          </div>
        </div>
      </article>"""


cards = "\n".join(card(k) for k in ORDER)

HTML = f"""<title>YardFlow Wordmark Candidates</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Serif:ital,wght@0,400;1,400&display=swap">
<style>
:root {{
  --ground:#F2F5F8; --panel:#FFFFFF; --ink:#101419; --muted:#5C6875;
  --rule:#DFE5EB; --accent:#1F6F6B;
  --paper:#FFFFFF; --paper-ink:#0F172A; --slate:#0F172A; --slate-ink:#F8FAFC;
  --sans:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  --serif:"IBM Plex Serif",Georgia,serif;
  --mono:"IBM Plex Mono",ui-monospace,"Cascadia Mono",Consolas,monospace;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --ground:#12161A; --panel:#1A1F25; --ink:#E6EAEF; --muted:#93A0AE;
    --rule:#28303A; --accent:#4FA9A2;
  }}
}}
:root[data-theme="dark"] {{
  --ground:#12161A; --panel:#1A1F25; --ink:#E6EAEF; --muted:#93A0AE;
  --rule:#28303A; --accent:#4FA9A2;
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0; background:var(--ground); color:var(--ink);
  font-family:var(--sans); font-size:16px; line-height:1.55;
  -webkit-font-smoothing:antialiased;
}}
.wrap {{ max-width:64rem; margin:0 auto; padding:clamp(2rem,5vw,4.5rem) clamp(1rem,4vw,2rem) 5rem; }}
.eyebrow {{
  font-family:var(--mono); font-size:.72rem; letter-spacing:.14em;
  text-transform:uppercase; color:var(--accent); margin:0 0 .9rem;
}}
h1 {{ font-size:clamp(1.9rem,4.4vw,2.9rem); line-height:1.1; margin:0 0 1.1rem;
     font-weight:600; letter-spacing:-.02em; text-wrap:balance; }}
.lede {{ font-family:var(--serif); font-size:1.06rem; line-height:1.7;
         max-width:34em; margin:0 0 1rem; }}
.lede + .lede {{ color:var(--muted); }}
.note {{ font-family:var(--serif); font-style:italic; color:var(--muted);
         max-width:34em; margin:1.6rem 0 0; padding-left:1rem;
         border-left:2px solid var(--rule); }}
hr.rule {{ border:0; border-top:1px solid var(--rule); margin:2.75rem 0; }}
.grid {{ display:flex; flex-direction:column; gap:1.25rem; }}
.card {{
  background:var(--panel); border:1px solid var(--rule); border-radius:4px;
  padding:1.5rem clamp(1rem,3vw,1.75rem);
  display:flex; flex-direction:column; gap:1.1rem;
}}
.card--pick {{ border-color:var(--accent); }}
.card__head {{ display:flex; align-items:baseline; justify-content:space-between;
               gap:1rem; flex-wrap:wrap; }}
.card h2 {{ font-size:1.12rem; font-weight:600; margin:0; letter-spacing:-.01em; }}
.spec {{ font-family:var(--mono); font-size:.75rem; color:var(--muted); margin:.25rem 0 0;
         font-variant-numeric:tabular-nums; }}
.pick {{ font-family:var(--mono); font-size:.68rem; letter-spacing:.12em;
         text-transform:uppercase; color:var(--accent);
         border:1px solid var(--accent); border-radius:2px;
         padding:.2rem .5rem; margin:0; white-space:nowrap; }}
.verdict {{ font-family:var(--serif); color:var(--muted); margin:0;
            max-width:44em; font-size:.97rem; }}
.paper {{
  background:var(--paper); color:var(--paper-ink); border:1px solid var(--rule);
  border-radius:3px; padding:1.6rem 1.4rem; display:flex; align-items:center;
  justify-content:center; overflow-x:auto;
}}
.mark {{ display:block; }}
.paper .mark {{ max-width:100%; }}
.tests {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(9.5rem,1fr)); gap:.85rem; }}
.test__stage {{
  height:5.25rem; border:1px solid var(--rule); border-radius:3px;
  display:flex; align-items:center; justify-content:center;
}}
.test__stage--paper {{ background:var(--paper); color:var(--paper-ink); }}
.test__stage--slate {{ background:var(--slate); color:var(--slate-ink); border-color:var(--slate); }}
.mono-black {{ color:#000; }}
.tab {{ width:32px; height:32px; border-radius:6px; background:var(--slate);
        color:var(--slate-ink); display:flex; align-items:center; justify-content:center; }}
.test__label {{ font-family:var(--mono); font-size:.7rem; color:var(--ink);
                margin:.5rem 0 0; display:flex; flex-direction:column; gap:.1rem; }}
.test__label span {{ color:var(--muted); font-size:.66rem; }}
.foot {{ margin-top:3rem; font-family:var(--serif); color:var(--muted); max-width:38em; }}
.foot h2 {{ font-family:var(--sans); font-size:1rem; font-weight:600;
            color:var(--ink); margin:0 0 .5rem; }}
.foot code {{ font-family:var(--mono); font-size:.85em; background:var(--ground);
              border:1px solid var(--rule); border-radius:2px; padding:.05rem .3rem; }}
a {{ color:var(--accent); }}
a:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}
</style>

<div class="wrap">
  <p class="eyebrow">Identity &middot; 22 August 2026</p>
  <h1>Seven wordmarks, set in type</h1>
  <p class="lede">The generated image had the right idea and the wrong execution: Bold beside
  Light with a gap before the <em>F</em>, so it read as two words; looser spacing in
  <em>Flow</em> than in <em>Yard</em>; soft edges and grey fringing in the transparency.</p>
  <p class="lede">These are the alternative. Each is the word set in a properly licensed open
  typeface, kerned by that typeface&rsquo;s own metrics, converted to outlines and saved as
  vector &mdash; so it stays sharp at 18&thinsp;mm on a gate pass and at 32&thinsp;px in a
  browser tab. Every candidate is shown under all three conditions, because a mark that only
  works large is not finished.</p>

  <hr class="rule">

  <div class="grid">
{cards}
  </div>

  <div class="foot">
    <h2>One thing the sheet makes obvious</h2>
    <p>At roughly five times wider than it is tall, no version of the full word survives a
    32&thinsp;px browser tab &mdash; so the tab and the Android home-screen icon need the
    compact <em>Y</em> shown in the third test, cut from the same outlines as the wordmark.
    That is a second asset, not a different logo.</p>
    <p class="note">Tell me which one and I will wire it in: the login screen, the app-shell
    header and the four printed documents &mdash; gate pass, GRN, disposal certificate, return
    waybill &mdash; plus <code>favicon.svg</code> and the two PWA icons.</p>
  </div>
</div>
"""

(HERE / "wordmark-candidates.html").write_text(HTML, encoding="utf-8")
print(f"sheet.html written, {len(HTML):,} bytes")
