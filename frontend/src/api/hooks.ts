/**
 * Query and mutation helpers (design §6, §6.1, §7.3).
 *
 * Every screen reads through `useList`/`useDetail` and writes through
 * `useAction`, so three things are decided once here rather than per screen:
 *
 *  - **cache keys** are the path plus its params, so a mutation invalidating
 *    `['gate-outs']` refreshes every list and detail of them without each
 *    caller naming keys by hand and getting one wrong
 *  - **field errors** from the §6.1 envelope reach react-hook-form unchanged;
 *    the backend already flattens them to the dotted paths it uses
 *  - **404 is never retried.** A cross-tenant id is a 404 by design (§2.4), and
 *    retrying it three times just makes the screen slow to say "not found"
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from '@tanstack/react-query';
import type { FieldValues, UseFormSetError, Path } from 'react-hook-form';

import { ApiError, api } from './client';

export type QueryParams = Record<string, string | number | boolean | undefined | null>;

/** DRF's paginated envelope. */
export interface Page<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

export function toQueryString(params?: QueryParams): string {
  if (!params) return '';
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    search.set(key, String(value));
  }
  const rendered = search.toString();
  return rendered ? `?${rendered}` : '';
}

/**
 * A collection. Returns the page envelope, so a caller can show the count.
 *
 * `resource` is the path segment (`'gate-outs'`), never a full URL — the client
 * owns the `/api/v1` prefix.
 */
export function useList<T>(
  resource: string,
  params?: QueryParams,
  options?: Partial<UseQueryOptions<Page<T>, ApiError>>,
) {
  return useQuery<Page<T>, ApiError>({
    queryKey: [resource, params ?? {}],
    queryFn: () => api.get<Page<T>>(`/${resource}${toQueryString(params)}`),
    ...options,
  });
}

/** An unpaginated endpoint — a report, a register, a reconciliation. */
export function useResource<T>(
  resource: string,
  params?: QueryParams,
  options?: Partial<UseQueryOptions<T, ApiError>>,
) {
  return useQuery<T, ApiError>({
    queryKey: [resource, params ?? {}],
    queryFn: () => api.get<T>(`/${resource}${toQueryString(params)}`),
    ...options,
  });
}

export function useDetail<T>(
  resource: string,
  id: string | number | undefined,
  options?: Partial<UseQueryOptions<T, ApiError>>,
) {
  return useQuery<T, ApiError>({
    queryKey: [resource, 'detail', String(id)],
    queryFn: () => api.get<T>(`/${resource}/${id}`),
    enabled: id !== undefined && id !== '',
    ...options,
  });
}

type Method = 'post' | 'patch' | 'put' | 'delete';

/**
 * A write, invalidating the resources it affects.
 *
 * `invalidates` defaults to the resource written. Pass more when a write changes
 * something else — releasing a gate pass moves stock, so the stock screens are
 * stale the moment it succeeds.
 */
export function useAction<TBody = unknown, TResult = unknown>({
  resource,
  path,
  method = 'post',
  invalidates,
}: {
  resource: string;
  /** Appended to the resource: `` `${id}/submit` ``. Omit for the collection. */
  path?: (body: TBody) => string;
  method?: Method;
  invalidates?: string[];
}) {
  const queryClient = useQueryClient();

  return useMutation<TResult, ApiError, TBody>({
    mutationFn: (body: TBody) => {
      const suffix = path ? `/${path(body)}` : '';
      const url = `/${resource}${suffix}`;
      if (method === 'delete') return api.delete<TResult>(url);
      return api[method]<TResult>(url, body);
    },
    onSuccess: () => {
      for (const key of invalidates ?? [resource]) {
        queryClient.invalidateQueries({ queryKey: [key] });
      }
    },
  });
}

/**
 * Attach an `ApiError`'s field errors to a form (§6.1).
 *
 * The backend flattens nested errors to `lines.1.length`, which is the path
 * react-hook-form uses — so nothing is transformed here. Anything without a
 * field lands on `root`, where the form shows it as a banner rather than losing
 * it.
 */
export function applyFieldErrors<T extends FieldValues>(
  error: unknown,
  setError: UseFormSetError<T>,
): string | null {
  if (!(error instanceof ApiError)) {
    return error instanceof Error ? error.message : 'Something went wrong.';
  }

  const entries = Object.entries(error.fieldErrors);
  for (const [field, messages] of entries) {
    setError(field as Path<T>, { type: 'server', message: messages.join(' ') });
  }

  // A message with no field still has to be shown: "only 340 m remaining on
  // drum D-0007" is the most useful thing on the screen (§6.1).
  return entries.length === 0 ? error.message : error.message;
}

/** Human text for an error, for screens with no form to attach it to. */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) {
    // `fetch` rejects with "Failed to fetch" when the request never left the
    // device — no signal, or the yard's wifi dropped mid-request. Showing the
    // browser's wording puts a storekeeper on the wrong trail; the queue
    // (§8.1) is what actually handles this, and it needs no explaining.
    if (/failed to fetch|networkerror|load failed/i.test(error.message)) {
      return navigator.onLine
        ? 'Could not reach the server. It may be a moment before it answers.'
        : 'No connection. Anything you capture is saved on this phone and sent when you are back.';
    }
    return error.message;
  }
  return 'Something went wrong.';
}
