/**
 * Pure helpers for opening a gate pass by scan (P11, G5, design §4.15.8).
 */

export interface TokenPayload {
  type: string;
  id: string;
  org: string;
}

function fromBase64Url(text: string): Uint8Array {
  const padded = text
    .replace(/-/g, '+')
    .replace(/_/g, '/')
    .padEnd(Math.ceil(text.length / 4) * 4, '=');
  const binary = atob(padded);
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

/**
 * Read the payload of a Django-signed QR token without checking the signature.
 *
 * Only for the offline path: with no signal the device cannot reach the server,
 * and the id it reads is only used to look in passes the server itself approved
 * and downloaded. Nothing is released on the strength of this read; the server
 * validates the release (and the pass) when it syncs, which is the control.
 *
 * `signing.dumps` gives `[.]<base64url(json)>:<timestamp>:<signature>`; a leading
 * `.` means the JSON was zlib-compressed first. Printed passes are never long
 * enough for Django to compress, but a compressed one is inflated here with the
 * platform's DecompressionStream. Where that does not exist, or anything is
 * malformed, the answer is `null`: unreadable, never a throw.
 */
export async function decodeTokenPayload(token: string): Promise<TokenPayload | null> {
  try {
    let body = token.trim().split(':')[0];
    const compressed = body.startsWith('.');
    if (compressed) body = body.slice(1);
    let bytes = fromBase64Url(body);
    if (compressed) {
      if (typeof DecompressionStream === 'undefined') return null;
      const stream = new Blob([bytes as BlobPart])
        .stream()
        .pipeThrough(new DecompressionStream('deflate'));
      bytes = new Uint8Array(await new Response(stream).arrayBuffer());
    }
    const data: unknown = JSON.parse(new TextDecoder().decode(bytes));
    if (!data || typeof data !== 'object') return null;
    const { type, id, org } = data as Record<string, unknown>;
    if (typeof type !== 'string' || id === undefined || id === null) return null;
    return { type, id: String(id), org: org === undefined || org === null ? '' : String(org) };
  } catch {
    return null;
  }
}

/**
 * Does typed text look like a gate pass number?
 *
 * Numbers are `<prefix>-<zero-padded sequence>`, e.g. `GP-000042`
 * (`backend/core/numbering.py:format_number`). The prefix is the tenant's to
 * change, so only the shape is recognised: letters/digits, a dash, digits. A
 * serial such as `HW-RRU-88001` has two dashes and is not taken for one. A
 * match only means "worth looking up"; whether a pass exists is for the lookup.
 */
const PASS_NUMBER = /^[A-Za-z][A-Za-z0-9]{0,11}-\d{1,12}$/;

export function looksLikePassNumber(text: string): boolean {
  return PASS_NUMBER.test(text.trim());
}

/** The canonical form to search by: trimmed and upper-cased, as numbers are issued. */
export function normalisePassNumber(text: string): string {
  return text.trim().toUpperCase();
}
