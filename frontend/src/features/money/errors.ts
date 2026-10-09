/**
 * Server refusals in plain words (design §4.17.12).
 *
 * Codes whose message already names the other record (an overlapping request,
 * a duplicate casual) keep the server's sentence; the rest get words a clerk
 * can act on without knowing what a project is called in the database.
 */

import type { FieldValues, UseFormSetError } from 'react-hook-form';

import { ApiError } from '../../api/client';
import { applyFieldErrors } from '../../api/hooks';

const PLAIN: Record<string, string> = {
  PROJECT_AMBIGUOUS: 'This site is on more than one open project. Choose which one this is for.',
  SITE_HAS_NO_OPEN_PROJECT: 'This site has no open project, so there is nothing to charge this to.',
  SITE_NOT_ON_PROJECT: 'That project does not include this site. Pick the project again.',
  TRANSPORT_SCOPE_REQUIRED: 'Say whether the trip is within Nairobi or outside it.',
  FLOAT_NOT_OPEN: 'That float is not open any more. Choose another, or none.',
};

/** The banner text for a failed save, setting field errors on the form too. */
export function moneyError<T extends FieldValues>(
  error: unknown,
  setError: UseFormSetError<T>,
): string | null {
  const banner = applyFieldErrors(error, setError);
  if (error instanceof ApiError && PLAIN[error.code]) return PLAIN[error.code];
  return banner;
}
