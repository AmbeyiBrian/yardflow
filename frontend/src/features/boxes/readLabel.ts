/**
 * Reading a scanned label (P2, P3, design §4.15.6).
 *
 * A faithful port of `backend/stock/labels.py:read_label`. A label in the yard
 * is rarely just a serial: it can be a gate-pass QR, a vendor's JSON payload, a
 * GS1 barcode, an ISO 15434 (Huawei) label, a product URL, a printed `SN:` line or a sheet of several
 * serials. One function reads whatever was scanned and says what it found, so
 * the storekeeper never has to say which.
 *
 * The phone and the server must never disagree about what a label says, so both
 * run the shared vectors in `backend/stock/tests/data/label_vectors.json`.
 * Change a rule and the vectors together, in both languages.
 *
 * Where JavaScript's own helpers differ from Python's, this file does what the
 * Python does, on purpose:
 *  - the outer trim is only space, \t \n \r \f \v. `String.trim()` would also eat
 *    NBSP and the BOM, and must never touch the GS1 separator (\x1d);
 *  - de-duplication trims with Python's `str.strip()` set (which *does* include
 *    \x1d and \x85), because the backend does;
 *  - query strings and path segments are decoded by hand, the way `parse_qs`
 *    and `unquote` do (`+` is a space; a malformed escape stays as written;
 *    invalid UTF-8 becomes U+FFFD), instead of `decodeURIComponent`, which throws.
 *
 * Known, accepted gap: JSON integers beyond 2^53 lose precision in `JSON.parse`,
 * and `1.0` is indistinguishable from `1`. Neither occurs on a real label.
 */

export interface LabelReading {
  serials: string[];
  boxCode: string | null;
  documentToken: string | null;
  /** What was scanned, exactly as given. */
  raw: string;
}

const GROUP_SEPARATOR = '\x1d'; // FNC1, ends a variable-length GS1 field
const BLANKS = ' \t\n\r\f\v';

const SERIAL_KEYS = ['serial', 'serialnumber', 'serial_number', 'sn'];
const BOX_KEYS = ['box', 'carton', 'pallet'];
const URL_SERIAL_PARAMS = ['serial', 'sn', 's'];

// AI -> fixed data length. Everything else is read up to the next separator.
const GS1_FIXED: Record<string, number> = {
  '00': 18,
  '01': 14,
  '02': 14,
  '11': 6,
  '13': 6,
  '15': 6,
  '17': 6,
};
const GS1_SYMBOLOGY = /^\][A-Za-z][0-9A-Za-z]/;
const GS1_BRACKETED_START = /^\(\p{Nd}{2,4}\)/u;
const GS1_BRACKETED = /\((\p{Nd}{2,4})\)([^(]*)/gu;

// ISO/IEC 15434 format 06 (E9): header, then GS-separated fields, ended by RS (and EOT).
const RECORD_SEPARATOR = '\x1e';
const END_OF_TRANSMISSION = '\x04';
const ISO15434_HEADER = `[)>${RECORD_SEPARATOR}06${GROUP_SEPARATOR}`;
const DATA_IDENTIFIER = /^(\d{0,3}[A-Z])([\s\S]*)$/;

// Python's `^`/`$` in MULTILINE mode and `.` only know \n; JavaScript's also
// count carriage return and the Unicode line and paragraph separators, so the line
// anchors are spelled out.
const LABELLED =
  /(?<![^\n])[ \t]*(?:s\/n[ \t]*:?|sn[ \t]*:|serial(?:[ \t]*no)?[ \t]*:)[ \t]*(\S[^\n]*?)[ \t]*(?![^\n])/gi;
const LIST_SPLIT = /[\r\n,;]+/;

// Characters Python's str.isspace() is true for.
const PY_SPACE =
  '\\t\\n\\v\\f\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000';
const PY_STRIP = new RegExp(`^[${PY_SPACE}]+|[${PY_SPACE}]+$`, 'g');

function pyStrip(value: string): string {
  return value.replace(PY_STRIP, '');
}

function trimBlanks(value: string): string {
  let start = 0;
  let end = value.length;
  while (start < end && BLANKS.includes(value[start])) start++;
  while (end > start && BLANKS.includes(value[end - 1])) end--;
  return value.slice(start, end);
}

/** Python's `unquote`: `%XX` runs decode as UTF-8, bad bytes become U+FFFD,
 * and anything that is not a valid escape is left alone. */
function unquote(value: string): string {
  if (!value.includes('%')) return value;
  return value.replace(/(?:%[0-9a-fA-F]{2})+/g, (run) => {
    const bytes = new Uint8Array(run.length / 3);
    for (let i = 0; i < bytes.length; i++) {
      bytes[i] = parseInt(run.slice(i * 3 + 1, i * 3 + 3), 16);
    }
    return new TextDecoder('utf-8').decode(bytes);
  });
}

/** Python's `parse_qs` with its defaults: `&` separates, blank values and
 * pairs with no `=` are dropped, `+` is a space. */
function parseQs(query: string): Map<string, string[]> {
  const out = new Map<string, string[]>();
  for (const pair of query.split('&')) {
    if (!pair) continue;
    const eq = pair.indexOf('=');
    if (eq < 0) continue;
    const value = unquote(pair.slice(eq + 1).replace(/\+/g, ' '));
    if (!value) continue;
    const name = unquote(pair.slice(0, eq).replace(/\+/g, ' '));
    const list = out.get(name);
    if (list) list.push(value);
    else out.set(name, [value]);
  }
  return out;
}

type Found = { serials: string[]; boxCode: string | null } | null;

/** Read whatever was scanned or typed (P2, P3). */
export function readLabel(rawInput: string | null | undefined): LabelReading {
  const raw = rawInput ?? '';
  const text = trimBlanks(raw);
  if (!text) return { serials: [], boxCode: null, documentToken: null, raw };

  const token = gatePassToken(text);
  if (token) return { serials: [], boxCode: null, documentToken: token, raw };

  for (const rule of [fromIso15434, fromJson, fromGs1, fromUrl, fromLabelled, fromList]) {
    const found = rule(text);
    if (found) {
      const serials = unique(found.serials);
      if (serials.length > 0 || found.boxCode) {
        return { serials, boxCode: found.boxCode, documentToken: null, raw };
      }
    }
  }
  return { serials: [text], boxCode: null, documentToken: null, raw };
}

function unique(values: string[]): string[] {
  const seen = new Set<string>();
  for (const value of values) {
    const stripped = pyStrip(value);
    if (stripped) seen.add(stripped);
  }
  return [...seen];
}

/** Rule 1: the gate-pass QR is a link ending `/qr/scan?token=...`. */
function gatePassToken(text: string): string | null {
  const marker = text.indexOf('/qr/scan?');
  if (marker < 0) return null;
  const query = text.slice(marker + '/qr/scan?'.length).split('#', 1)[0];
  for (const value of parseQs(query).get('token') ?? []) {
    const stripped = pyStrip(value);
    if (stripped) return stripped;
  }
  return null;
}

/** Rule 2: an ISO/IEC 15434 format-06 envelope; the `S` fields are serials (E9).
 * The header needs its separators, so run-together text never matches and the
 * fallback keeps it whole. Without an `S` field it falls through. */
function fromIso15434(text: string): Found {
  if (!text.startsWith(ISO15434_HEADER)) return null;
  let body = text.slice(ISO15434_HEADER.length).split(RECORD_SEPARATOR, 1)[0];
  while (body.endsWith(END_OF_TRANSMISSION)) body = body.slice(0, -1);
  const serials: string[] = [];
  for (const field of body.split(GROUP_SEPARATOR)) {
    const match = DATA_IDENTIFIER.exec(field);
    if (match && match[1] === 'S') serials.push(match[2]);
  }
  return serials.length > 0 ? { serials, boxCode: null } : null;
}

/** A string or a whole number. Never a boolean, null, float or nested value. */
function scalar(value: unknown): value is string | number {
  return typeof value === 'string' || (typeof value === 'number' && Number.isInteger(value));
}

/** Rule 3: an object, or an array of strings or objects. */
function fromJson(text: string): Found {
  if (text[0] !== '{' && text[0] !== '[') return null;
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    return null;
  }
  const serials: string[] = [];
  let boxCode: string | null = null;
  const items = Array.isArray(data) ? data : [data];
  for (const item of items) {
    if (item !== null && typeof item === 'object' && !Array.isArray(item)) {
      for (const [key, value] of Object.entries(item)) {
        const name = key.toLowerCase();
        if (SERIAL_KEYS.includes(name) && scalar(value)) {
          serials.push(String(value));
        } else if (name === 'serials' && Array.isArray(value)) {
          for (const v of value) if (scalar(v)) serials.push(String(v));
        } else if (BOX_KEYS.includes(name) && scalar(value) && !boxCode) {
          boxCode = pyStrip(String(value)) || null;
        }
      }
    } else if (scalar(item)) {
      serials.push(String(item));
    }
  }
  return { serials, boxCode };
}

/** Rule 4: GS1 element strings, with or without brackets (AI 21 serial, 00 SSCC).
 * Never guessed from bare digits: only the bracketed form, a symbology
 * identifier or an FNC1 separator say it is GS1. */
function fromGs1(text: string): Found {
  let pairs: [string, string][];
  if (GS1_BRACKETED_START.test(text)) {
    pairs = [...text.matchAll(GS1_BRACKETED)].map((m) => [m[1], pyStrip(m[2])]);
  } else if (GS1_SYMBOLOGY.test(text) || text.includes(GROUP_SEPARATOR)) {
    pairs = walkGs1(text.replace(GS1_SYMBOLOGY, ''));
  } else {
    return null;
  }
  const serials = pairs.filter(([ai]) => ai === '21').map(([, value]) => value);
  const box = pairs.find(([ai, value]) => ai === '00' && value);
  return { serials, boxCode: box ? box[1] : null };
}

function walkGs1(body: string): [string, string][] {
  const pairs: [string, string][] = [];
  let i = 0;
  while (i < body.length) {
    if (body[i] === GROUP_SEPARATOR) {
      i += 1;
      continue;
    }
    const ai = body.slice(i, i + 2);
    i += 2;
    const length = GS1_FIXED[ai];
    let value: string;
    if (length !== undefined) {
      value = body.slice(i, i + length);
      i += length;
    } else {
      let end = body.indexOf(GROUP_SEPARATOR, i);
      if (end < 0) end = body.length;
      value = body.slice(i, end);
      i = end;
    }
    pairs.push([ai, pyStrip(value)]);
  }
  return pairs;
}

/** Rule 5: a product link carries the serial in a parameter or the last segment. */
function fromUrl(text: string): Found {
  if (!/^https?:\/\//i.test(text)) return null;
  // urlsplit drops tabs and newlines anywhere in the address.
  const url = text.replace(/[\t\r\n]/g, '');
  const afterScheme = url.slice(url.indexOf('://') + 3);
  const hash = afterScheme.indexOf('#');
  const noFragment = hash < 0 ? afterScheme : afterScheme.slice(0, hash);
  const q = noFragment.indexOf('?');
  const beforeQuery = q < 0 ? noFragment : noFragment.slice(0, q);
  const queryText = q < 0 ? '' : noFragment.slice(q + 1);
  const slash = beforeQuery.indexOf('/');
  const netloc = slash < 0 ? beforeQuery : beforeQuery.slice(0, slash);
  const path = slash < 0 ? '' : beforeQuery.slice(slash);
  // urlsplit raises ValueError on unbalanced IPv6 brackets.
  if (netloc.includes('[') !== netloc.includes(']')) return null;

  const query = new Map<string, string[]>();
  for (const [k, v] of parseQs(queryText)) query.set(k.toLowerCase(), v);
  for (const name of URL_SERIAL_PARAMS) {
    const values = (query.get(name) ?? []).filter((v) => pyStrip(v));
    if (values.length > 0) return { serials: values, boxCode: null };
  }
  const segments = path.split('/').filter(Boolean);
  if (segments.length > 0) {
    return { serials: [unquote(segments[segments.length - 1])], boxCode: null };
  }
  return null;
}

/** Rule 6: `SN: x` / `S/N x` / `Serial No: x`, one per line. */
function fromLabelled(text: string): Found {
  const serials = [...text.matchAll(LABELLED)].map((m) => m[1]);
  return { serials, boxCode: null };
}

/** Rule 7: several serials on lines, or split by commas or semicolons. */
function fromList(text: string): Found {
  const tokens = text
    .split(LIST_SPLIT)
    .map(pyStrip)
    .filter((t) => t);
  return tokens.length >= 2 ? { serials: tokens, boxCode: null } : null;
}
