"""Reading a scanned label (P2, P3, design §4.15.6).

A label in the yard is rarely just a serial number: it can be a gate-pass QR, a
vendor's JSON payload, a GS1 barcode, a product URL, a printed ``SN:`` line or a
sheet of several serials. The storekeeper should not have to say which — so one
function reads whatever was scanned and says what it found.

The rules run in a fixed order and the first that finds something usable wins.
A non-empty label is never discarded: a rule that matches but yields nothing
falls through, and the last rule returns the raw value as the single serial, so
the worst case is the behaviour from before this module existed.

The browser implements the same rules; both are tested against
``stock/tests/data/label_vectors.json``. Change a rule and the vectors together.
Box codes and gate-pass tokens are only *read* here — resolving them is later work.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

GROUP_SEPARATOR = "\x1d"  # FNC1, ends a variable-length GS1 field
# Not str.strip(): Python counts the group separator (0x1D) as whitespace, which
# would eat the very character that ends a GS1 field. The browser must trim the same set.
_BLANKS = " \t\n\r\f\v"

_SERIAL_KEYS = ("serial", "serialnumber", "serial_number", "sn")
_BOX_KEYS = ("box", "carton", "pallet")
_URL_SERIAL_PARAMS = ("serial", "sn", "s")

# AI -> fixed data length. Everything else is read up to the next separator.
_GS1_FIXED = {"00": 18, "01": 14, "02": 14, "11": 6, "13": 6, "15": 6, "17": 6}
_GS1_SYMBOLOGY = re.compile(r"^\][A-Za-z][0-9A-Za-z]")
_GS1_BRACKETED_START = re.compile(r"^\(\d{2,4}\)")
_GS1_BRACKETED = re.compile(r"\((\d{2,4})\)([^(]*)")

_LABELLED = re.compile(
    r"^[ \t]*(?:s/n[ \t]*:?|sn[ \t]*:|serial(?:[ \t]*no)?[ \t]*:)[ \t]*(\S.*?)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_LIST_SPLIT = re.compile(r"[\r\n,;]+")


@dataclass(frozen=True)
class LabelReading:
    serials: tuple[str, ...]
    box_code: str | None
    document_token: str | None
    raw: str


def read_label(raw: str) -> LabelReading:
    """Read whatever was scanned or typed (P2, P3)."""
    raw = raw or ""
    text = raw.strip(_BLANKS)
    if not text:
        return LabelReading((), None, None, raw)

    token = _gate_pass_token(text)
    if token:
        return LabelReading((), None, token, raw)

    for rule in (_from_json, _from_gs1, _from_url, _from_labelled, _from_list):
        found = rule(text)
        if found is not None:
            serials, box_code = found
            unique = _unique(serials)
            if unique or box_code:
                return LabelReading(unique, box_code, None, raw)

    return LabelReading((text,), None, None, raw)


def _unique(values) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for value in values:
        value = str(value).strip()
        if value:
            seen.setdefault(value)
    return tuple(seen)


def _gate_pass_token(text: str) -> str | None:
    """Rule 1: the gate-pass QR is a link ending ``/qr/scan?token=...``."""
    marker = text.find("/qr/scan?")
    if marker < 0:
        return None
    query = text[marker + len("/qr/scan?") :].split("#", 1)[0]
    values = parse_qs(query).get("token") or []
    return next((v.strip() for v in values if v.strip()), None)


def _from_json(text: str):
    """Rule 2: an object, or an array of strings or objects."""
    if text[0] not in "{[":
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    serials: list[str] = []
    box_code: str | None = None
    items = data if isinstance(data, list) else [data]
    for item in items:
        if isinstance(item, dict):
            for key, value in item.items():
                name = str(key).lower()
                if name in _SERIAL_KEYS and isinstance(value, (str, int)):
                    serials.append(str(value))
                elif name == "serials" and isinstance(value, list):
                    serials.extend(str(v) for v in value if isinstance(v, (str, int)))
                elif name in _BOX_KEYS and isinstance(value, (str, int)) and not box_code:
                    box_code = str(value).strip() or None
        elif isinstance(item, (str, int)) and not isinstance(item, bool):
            serials.append(str(item))
    return serials, box_code


def _from_gs1(text: str):
    """Rule 3: GS1 element strings, with or without brackets (AI 21 serial, 00 SSCC).

    Never guessed from bare digits: only bracketed form, a symbology identifier or
    an FNC1 separator say it is GS1.
    """
    if _GS1_BRACKETED_START.match(text):
        pairs = [(ai, value.strip()) for ai, value in _GS1_BRACKETED.findall(text)]
    elif _GS1_SYMBOLOGY.match(text) or GROUP_SEPARATOR in text:
        pairs = _walk_gs1(_GS1_SYMBOLOGY.sub("", text, count=1))
    else:
        return None
    serials = [value for ai, value in pairs if ai == "21"]
    box_code = next((value for ai, value in pairs if ai == "00" and value), None)
    return serials, box_code


def _walk_gs1(body: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    i = 0
    while i < len(body):
        if body[i] == GROUP_SEPARATOR:
            i += 1
            continue
        ai = body[i : i + 2]
        i += 2
        length = _GS1_FIXED.get(ai)
        if length is not None:
            value, i = body[i : i + length], i + length
        else:
            end = body.find(GROUP_SEPARATOR, i)
            end = len(body) if end < 0 else end
            value, i = body[i:end], end
        pairs.append((ai, value.strip()))
    return pairs


def _from_url(text: str):
    """Rule 4: a product link carries the serial in a parameter or the last segment."""
    if not re.match(r"^https?://", text, re.IGNORECASE):
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    query = {k.lower(): v for k, v in parse_qs(parts.query).items()}
    for name in _URL_SERIAL_PARAMS:
        values = [v for v in query.get(name, []) if v.strip()]
        if values:
            return values, None
    segments = [s for s in parts.path.split("/") if s]
    if segments:
        return [unquote(segments[-1])], None
    return None


def _from_labelled(text: str):
    """Rule 5: ``SN: x`` / ``S/N x`` / ``Serial No: x``, one per line."""
    return _LABELLED.findall(text), None


def _from_list(text: str):
    """Rule 6: several serials on lines, or split by commas or semicolons."""
    tokens = [t.strip() for t in _LIST_SPLIT.split(text) if t.strip()]
    return (tokens, None) if len(tokens) >= 2 else None
