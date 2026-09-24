"""Turn every reference `<Select>` into a `ReferenceSelect` (one-off, §7.3).

A reference select is one whose options are mapped from an entity list —
projects, sites, clients, subcontractors, locations, item types, people. Each
gains an "＋ Add new …" option:

* a react-hook-form field (`{...form.register('x', rules)}`) becomes
  `<ReferenceSelect resource=… form={form} name="x" rules={…}>`, keeping
  every other attribute (`id`, `disabled`, …) exactly where it was;
* a controlled select (`value={…} onChange={…}`) becomes
  `<ControlledReferenceSelect resource=…>` with its attributes untouched.

Selects over anything else — status choices, report filters, derived lists —
are left exactly as they are and listed so a person can check the list.

    python scripts/convert-reference-selects.py          # report only
    python scripts/convert-reference-selects.py --write  # apply
    python scripts/convert-reference-selects.py --undo   # put converted
        # blocks back to the opening tag they have in HEAD (by position)
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "frontend" / "src" / "features"

# The variable an option list is mapped from -> the API resource it came from.
# Only resources with a create sheet in `features/quickCreate` belong here.
LIST_TO_RESOURCE = {
    "projects": "projects",
    "sites": "sites",
    "clients": "clients",
    "contractors": "subcontractors",
    "subcontractors": "subcontractors",
    "locations": "locations",
    "yards": "locations",
    "items": "item-types",
    "itemTypes": "item-types",
    "people": "users",
    "users": "users",
    "assignees": "users",
}

OPEN = re.compile(r"<Select\b")
REGISTER = re.compile(r"\{\.\.\.form\.register\(\s*'([a-z_]+)'")
LIST_HEAD = re.compile(
    r"(?:\(\s*([a-zA-Z]+)\.data\?\.results \?\? \[\]\s*\)|\b([a-zA-Z]+))\s*\.(filter|map)\("
)


def close_bracket(text: str, start: int, open_ch: str, close_ch: str) -> int:
    depth = 0
    for i in range(start, len(text)):
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
    raise ValueError("unbalanced")


def list_source(body: str) -> str | None:
    """The variable the options are mapped from, seeing past a `.filter(...)`."""
    m = LIST_HEAD.search(body)
    if not m:
        return None
    name = m.group(1) or m.group(2)
    if m.group(3) == "filter":
        after = close_bracket(body, m.end() - 1, "(", ")")
        if not re.match(r"\s*\.map\(", body[after:]):
            return None
    return name


def end_of_opening_tag(source: str, start: int) -> int:
    depth = 0
    for i in range(start, len(source)):
        c = source[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == ">" and depth == 0:
            return i + 1
    raise ValueError("no end of tag")


def blocks(source: str, tag: re.Pattern[str], closing: str):
    """Every `<tag …>…</closing>` block: (start, tag_end, close_start)."""
    pos = 0
    while True:
        m = tag.search(source, pos)
        if not m:
            return
        tag_end = end_of_opening_tag(source, m.end())
        close = source.find(closing, tag_end)
        yield m.start(), m.end(), tag_end, close
        pos = close + len(closing)


def plan(source: str):
    """Which `<Select>` blocks are convertible, in order, with how."""
    out = []
    for start, name_end, tag_end, close in blocks(source, OPEN, "</Select>"):
        opening = source[start:tag_end]
        body = source[tag_end:close]
        src = list_source(body)
        resource = LIST_TO_RESOURCE.get(src) if src else None
        reg = REGISTER.search(opening)
        controlled = "value={" in opening and "onChange={" in opening
        mode = None
        if resource and reg:
            mode = "form"
        elif resource and controlled:
            mode = "controlled"
        out.append((start, name_end, tag_end, close, opening, src, resource, mode))
    return out


def convert(source: str, path: Path) -> tuple[str, list[str], bool]:
    out: list[str] = []
    notes: list[str] = []
    pos = 0
    used_form = used_controlled = False

    for start, name_end, tag_end, close, opening, src, resource, mode in plan(source):
        line = source.count("\n", 0, start) + 1
        end = close + len("</Select>")
        if mode is None:
            if src:
                notes.append(f"  skip  {path.name}:{line:<5} list `{src}` — not convertible")
            out.append(source[pos:end])
            pos = end
            continue

        rel = name_end - start  # length of "<Select" inside `opening`
        if mode == "form":
            spread_start = opening.index("{...form.register(")
            spread_end = close_bracket(opening, spread_start, "{", "}")
            spread = opening[spread_start:spread_end]
            args = spread[spread.index("register(") + len("register(") : -2]
            name_m = re.match(r"\s*'([a-z_]+)'\s*(?:,\s*)?", args)
            assert name_m
            name = name_m.group(1)
            rules = args[name_m.end() :].strip()
            rules_attr = f" rules={{{rules}}}" if rules else ""
            new_opening = (
                "<ReferenceSelect"
                + f' resource="{resource}" form={{form}} name="{name}"{rules_attr}'
                + opening[rel:spread_start].rstrip()
                + opening[spread_end:]
            )
            closing = "</ReferenceSelect>"
            used_form = True
            notes.append(f"  ok    {path.name}:{line:<5} {name} -> {resource}  (form)")
        else:
            new_opening = "<ControlledReferenceSelect" + f' resource="{resource}"' + opening[rel:]
            closing = "</ControlledReferenceSelect>"
            used_controlled = True
            notes.append(f"  ok    {path.name}:{line:<5} -> {resource}  (controlled)")

        new_opening = re.sub(r"\n[ \t]*\n", "\n", new_opening)
        out.append(source[pos:start])
        out.append(new_opening)
        out.append(source[tag_end:close])
        out.append(closing)
        pos = end

    out.append(source[pos:])
    result = "".join(out)
    changed = used_form or used_controlled
    if changed and "components/ui/ReferenceSelect'" not in result:
        names = [
            n
            for n, used in (
                ("ControlledReferenceSelect", used_controlled),
                ("ReferenceSelect", used_form),
            )
            if used
        ]
        imp = f"import {{ {', '.join(names)} }} from '../../components/ui/ReferenceSelect';\n"
        anchor = re.search(r"^import \{[^}]*\} from '\.\./\.\./components/ui';\n", result, re.M)
        if anchor:
            result = result[: anchor.end()] + imp + result[anchor.end() :]
        else:
            notes.append(f"  WARN  {path.name}: could not place import")
    return result, notes, changed


CONVERTED = re.compile(r"<(?:Controlled)?ReferenceSelect\b")


def undo(source: str, path: Path) -> tuple[str, list[str]]:
    """Restore each converted block's opening tag from HEAD, by position."""
    rel = path.relative_to(REPO).as_posix()
    head = subprocess.run(
        ["git", "show", f"HEAD:{rel}"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    originals = [(o, m) for (_s, _n, _t, _c, o, _src, _r, m) in plan(head) if m]

    out: list[str] = []
    notes: list[str] = []
    pos = 0
    n = 0
    for start, _name_end, tag_end, close in blocks(source, CONVERTED, "</"):
        # `blocks` stopped at the first "</" — find this block's real closing.
        opening = source[start:tag_end]
        closing = (
            "</ControlledReferenceSelect>"
            if opening.startswith("<Controlled")
            else "</ReferenceSelect>"
        )
        close = source.find(closing, tag_end)
        if n >= len(originals):
            notes.append(f"  WARN  {path.name}: more converted blocks than HEAD has convertible ones")
            break
        original_opening, _mode = originals[n]
        n += 1
        out.append(source[pos:start])
        out.append(original_opening)
        out.append(source[tag_end:close])
        out.append("</Select>")
        pos = close + len(closing)
    out.append(source[pos:])
    result = "".join(out)
    result = re.sub(
        r"^import \{[^}]*\} from '\.\./\.\./components/ui/ReferenceSelect';\n", "", result, flags=re.M
    )
    notes.append(f"  undo  {path.name}: {n} block(s) restored")
    return result, notes


def main() -> None:
    write = "--write" in sys.argv
    do_undo = "--undo" in sys.argv
    converted = 0
    for path in sorted(ROOT.rglob("*.tsx")):
        source = path.read_text(encoding="utf-8")
        if do_undo:
            if not CONVERTED.search(source):
                continue
            result, notes = undo(source, path)
            for note in notes:
                print(note)
            path.write_text(result, encoding="utf-8", newline="\n")
            continue
        if "<Select" not in source:
            continue
        result, notes, changed = convert(source, path)
        for note in notes:
            print(note)
        converted += sum(1 for n in notes if n.strip().startswith("ok"))
        if write and changed:
            path.write_text(result, encoding="utf-8", newline="\n")
    if not do_undo:
        print(f"{'converted' if write else 'would convert'}: {converted}")


if __name__ == "__main__":
    main()
