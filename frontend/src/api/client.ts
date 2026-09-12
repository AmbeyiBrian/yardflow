/**
 * HTTP client (design §6, §6.1, §7.2).
 *
 * Two responsibilities that must not be spread around the codebase:
 *
 *  - **the error envelope.** Every failure comes back as
 *    `{ error: { code, message, field_errors, details } }` (§6.1). This module
 *    turns that into an `ApiError` so callers switch on `code` and never on
 *    message text — messages get reworded and localised (N-8), codes do not.
 *
 *  - **token refresh.** The access token is short-lived. A single 401 triggers
 *    one refresh and one retry; a second failure logs out. Refreshes are shared
 *    so ten parallel requests cause one refresh, not ten.
 */

export interface ApiErrorBody {
  code: string;
  message: string;
  field_errors?: Record<string, string[]>;
  details?: Record<string, unknown>;
}

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly fieldErrors: Record<string, string[]>;
  readonly details: Record<string, unknown>;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message);
    this.name = 'ApiError';
    this.status = status;
    this.code = body.code;
    this.fieldErrors = body.field_errors ?? {};
    this.details = body.details ?? {};
  }

  /** A cross-tenant or missing object. Always 404, never 403 (§2.4). */
  get isNotFound() {
    return this.status === 404;
  }

  /** A2: the tenant is suspended — reads work, writes do not. */
  get isOrganizationSuspended() {
    return this.code === 'ORGANIZATION_SUSPENDED';
  }
}

const API_ROOT = '/api/v1';

/**
 * Where tokens live.
 *
 * The refresh token is persisted so a reload does not log the user out. The
 * access token is kept in memory only: it is the credential presented on every
 * request, and keeping it out of storage limits what an injected script can
 * read. On boot the refresh token buys a new access token.
 */
const REFRESH_STORAGE_KEY = 'yardflow.refresh';

let accessToken: string | null = null;
let refreshInFlight: Promise<string | null> | null = null;

type UnauthorizedHandler = () => void;
let onUnauthorized: UnauthorizedHandler = () => {};

export function setUnauthorizedHandler(handler: UnauthorizedHandler) {
  onUnauthorized = handler;
}

export function getAccessToken() {
  return accessToken;
}

export function getRefreshToken(): string | null {
  try {
    return localStorage.getItem(REFRESH_STORAGE_KEY);
  } catch {
    // Private mode, or storage disabled. The session simply will not survive a
    // reload, which is better than failing to start.
    return null;
  }
}

export function storeTokens(tokens: { access: string; refresh?: string }) {
  accessToken = tokens.access;
  if (tokens.refresh) {
    try {
      localStorage.setItem(REFRESH_STORAGE_KEY, tokens.refresh);
    } catch {
      /* see getRefreshToken */
    }
  }
}

export function clearTokens() {
  accessToken = null;
  try {
    localStorage.removeItem(REFRESH_STORAGE_KEY);
  } catch {
    /* nothing to clear */
  }
}

/**
 * What to say when the response carries no message of its own.
 *
 * Every error our API raises comes back as `{"error": {"code", "message"}}` and
 * that message is what gets shown. This is for the responses that do not: a
 * proxy error page, a stale bundle asking for a URL that no longer routes, a
 * gateway timeout. Those used to surface as "Request failed (404)." on the
 * screen — a status code shown to a storekeeper, which tells them nothing about
 * what happened or what to do next (§6.1, N-1).
 */
function fallbackMessage(status: number): string {
  if (status === 404) {
    // Deliberately not "not found": §2.4 answers 404 for another tenant's
    // records too, and this text has to be true either way.
    return 'That is not available. If this screen was open for a while, reload it.';
  }
  if (status === 401 || status === 403) {
    return 'You are not signed in, or no longer have access to this.';
  }
  if (status === 408 || status === 504) {
    return 'The server took too long to answer. Check your connection and try again.';
  }
  if (status >= 500) {
    return 'The server had a problem with that. Nothing you entered was lost.';
  }
  if (status >= 400) {
    return 'That request was not accepted. Check what you entered and try again.';
  }
  return 'Something went wrong.';
}

async function parseError(response: Response): Promise<ApiError> {
  let body: ApiErrorBody = {
    code: 'UNKNOWN',
    // The status stays in `ApiError.status` for anyone debugging; it does not
    // belong in a sentence somebody reads at a gate.
    message: fallbackMessage(response.status),
  };
  try {
    const payload = await response.json();
    if (payload?.error?.code) {
      body = payload.error as ApiErrorBody;
    } else if (payload?.detail) {
      body = { code: 'UNKNOWN', message: String(payload.detail) };
    }
  } catch {
    // A non-JSON body (a proxy error page, say). The default message stands.
  }
  return new ApiError(response.status, body);
}

/** Exchange the refresh token for a new access token. At most one at a time. */
async function refreshAccessToken(): Promise<string | null> {
  if (refreshInFlight) return refreshInFlight;

  const refresh = getRefreshToken();
  if (!refresh) return null;

  refreshInFlight = (async () => {
    try {
      const response = await fetch(`${API_ROOT}/auth/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh }),
      });
      if (!response.ok) return null;

      const payload = await response.json();
      // Rotation is on server-side, so a new refresh token comes back too and
      // the old one is blacklisted. Storing it is not optional.
      storeTokens({ access: payload.access, refresh: payload.refresh });
      return payload.access as string;
    } catch {
      return null;
    } finally {
      refreshInFlight = null;
    }
  })();

  return refreshInFlight;
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
  /** Skip the Authorization header — used by login and password reset. */
  anonymous?: boolean;
}

async function send(path: string, options: RequestOptions, retrying = false): Promise<Response> {
  const headers: Record<string, string> = {};
  let body: BodyInit | undefined;

  if (options.body instanceof FormData) {
    body = options.body;
  } else if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(options.body);
  }

  if (!options.anonymous && accessToken) {
    headers.Authorization = `Bearer ${accessToken}`;
  }

  const response = await fetch(`${API_ROOT}${path}`, {
    method: options.method ?? 'GET',
    headers,
    body,
    signal: options.signal,
  });

  if (response.status === 401 && !options.anonymous && !retrying) {
    const refreshed = await refreshAccessToken();
    if (refreshed) {
      return send(path, options, true);
    }
    clearTokens();
    onUnauthorized();
  }

  return response;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await send(path, options);

  if (!response.ok) {
    throw await parseError(response);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

/**
 * Fetch a printed document and hand it to the browser (§11, G4, K2, J3).
 *
 * A plain `<a href="/api/v1/gate-outs/1/pdf">` cannot work here, and it was what
 * the gate-pass button used: the access token lives in memory rather than in a
 * cookie (see above), so the browser sends no `Authorization` header and the
 * request is a 401. The document the whole of G4 exists for — the paper the
 * driver carries — could not be printed from the app at all.
 *
 * So the bytes are fetched with the header, turned into a blob URL, and opened.
 * The URL is revoked on a timer rather than immediately: revoking it before the
 * new tab has finished loading leaves the tab blank.
 */
export async function openDocument(path: string): Promise<void> {
  const response = await send(path, {});
  if (!response.ok) {
    throw await parseError(response);
  }

  const blob = await response.blob();
  const url = URL.createObjectURL(blob);

  const opened = window.open(url, "_blank");
  if (!opened) {
    // A blocked popup is normal on a phone. An anchor click is treated as a
    // user gesture and survives it.
    const link = document.createElement("a");
    link.href = url;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.click();
  }

  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

/**
 * Ask for a file, and hand it to the browser — or report that it was queued.
 *
 * An export is a POST (it produces something, and a queued one has side
 * effects), and it comes back either as bytes or as a 202 with a message. So the
 * caller cannot simply await JSON or simply await a blob: this decides from the
 * content type, which is the only thing that actually distinguishes them.
 *
 * Returns `null` when a file was delivered, or the JSON body when the work was
 * queued — so the caller shows a message in exactly the case there is one.
 */
export async function downloadFile(
  path: string,
  options: { method?: string; body?: unknown } = {},
): Promise<Record<string, unknown> | null> {
  const response = await send(path, { method: options.method ?? 'POST', body: options.body });
  if (!response.ok) {
    throw await parseError(response);
  }

  const contentType = response.headers.get('Content-Type') ?? '';
  if (contentType.includes('application/json')) {
    return (await response.json()) as Record<string, unknown>;
  }

  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const filename = filenameFrom(response.headers.get('Content-Disposition'));

  const link = document.createElement('a');
  link.href = url;
  // A spreadsheet has to be saved rather than rendered, and the server already
  // said what it should be called.
  if (filename) link.download = filename;
  else link.target = '_blank';
  link.click();

  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
  return null;
}

function filenameFrom(disposition: string | null): string {
  if (!disposition) return '';
  const match = /filename="?([^"]+)"?/.exec(disposition);
  return match ? match[1] : '';
}

export const api = {
  get: <T>(path: string, options?: RequestOptions) => request<T>(path, { ...options }),
  post: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>(path, { ...options, method: 'POST', body }),
  patch: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>(path, { ...options, method: 'PATCH', body }),
  put: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>(path, { ...options, method: 'PUT', body }),
  delete: <T>(path: string, options?: RequestOptions) =>
    request<T>(path, { ...options, method: 'DELETE' }),
};

/** Restore a session on boot, if a refresh token survived the reload. */
export async function restoreSession(): Promise<boolean> {
  if (accessToken) return true;
  const token = await refreshAccessToken();
  return token !== null;
}
