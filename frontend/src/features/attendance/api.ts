/**
 * Clock-in endpoints (design §4.18.7; R13), as thin hooks over `useList` /
 * `useAction`, in the pattern of `features/money/api.ts`. Resources carry no
 * `/api/v1` prefix; the client adds it.
 */

import { useQuery } from '@tanstack/react-query';

import { api, type ApiError } from '../../api/client';
import { useAction, useDetail, useList, type QueryParams } from '../../api/hooks';
import type {
  AddDayBody,
  ClockInBody,
  ClockOutBody,
  CorrectionBody,
  DayStatus,
  DecideDayBody,
  WorkDay,
  WorkSession,
} from './types';

const SESSIONS = 'work-sessions';
const DAYS = 'work-days';
/** A clock-in or clock-out changes the open session and the day's hours. */
const BOTH = [SESSIONS, DAYS, 'approvals/pending'];

export interface WorkDayParams extends QueryParams {
  /** `mine` always; `team` and `all` by permission (§4.18.8). */
  scope?: 'mine' | 'team' | 'all';
  status?: DayStatus;
  person?: number;
  project?: number;
  from?: string;
  to?: string;
  /** §4.18.7: days with a slice waiting on the caller. */
  awaiting_me?: boolean;
}

/** The caller's open session, or null when clocked out. */
export function useOpenSession(enabled = true) {
  return useQuery<WorkSession | null, ApiError>({
    queryKey: [SESSIONS, 'open'],
    queryFn: async () => {
      const row = await api.get<WorkSession | null | undefined>(`/${SESSIONS}/open`);
      // 204 / null / {} all mean "not clocked in".
      return row && typeof row === 'object' && 'id' in row ? row : null;
    },
    enabled,
    refetchOnWindowFocus: true,
  });
}

export const useClockIn = () =>
  useAction<ClockInBody, WorkSession>({
    resource: SESSIONS,
    path: () => 'clock-in',
    invalidates: BOTH,
  });

export const useClockOut = () =>
  useAction<ClockOutBody, WorkSession>({
    resource: SESSIONS,
    path: () => 'clock-out',
    invalidates: BOTH,
  });

export const useWorkDays = (params?: WorkDayParams) =>
  useList<WorkDay>(DAYS, { scope: 'mine', page_size: 60, ...params });

export const useWorkDay = (id: string | number | undefined) =>
  useDetail<WorkDay>(DAYS, id);

/** §4.18.6: the person corrects a session of their rejected slice. */
export const useCorrectSession = () =>
  useAction<CorrectionBody, WorkDay>({
    resource: SESSIONS,
    path: (b) => `${b.id}/correct`,
    invalidates: BOTH,
  });

/** §4.18.5: the caller decides their own slice only; the server enforces it. */
export const useDecideDay = () =>
  useAction<DecideDayBody, WorkDay>({
    resource: DAYS,
    path: (b) => `${b.id}/decide`,
    invalidates: [DAYS, 'approvals/pending'],
  });

/** §4.18.6a: Director-role holders add a day for someone else. */
export const useAddDay = () =>
  useAction<AddDayBody, WorkDay>({
    resource: DAYS,
    path: () => 'add',
    invalidates: [DAYS, 'approvals/pending'],
  });
