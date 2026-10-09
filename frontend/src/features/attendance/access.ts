/**
 * Who sees what of other people's time (design §4.18.8, §4.18.11; R13).
 * UX only: the server scopes every read and decide (§7.2).
 */

import { useList } from '../../api/hooks';
import { PERM } from '../../auth/permissions';
import { useSession } from '../../auth/session';
import { useFinanceSettings } from '../money/api';
import type { Project } from '../projects/types';
import { holdsRole } from './approvals';
import type { WorkDay } from './types';

export function useDayAccess() {
  const { user, has } = useSession();
  const settings = useFinanceSettings();
  const isDirector = holdsRole(user?.roles, settings.data?.finance_director_role);
  const viewAll = has(PERM.ATTENDANCE_VIEW_ALL);

  const projects = useList<Project>(
    'projects',
    { manager: user?.id, status: 'OPEN', page_size: 200 },
    { enabled: Boolean(user) },
  );
  // Filtered again here, in case the list is not narrowed to the manager.
  const managesProject = (projects.data?.results ?? []).some(
    (p) => p.manager === user?.id && p.status === 'OPEN',
  );

  // §4.18.7: days with a slice waiting on me.
  const awaiting = useList<WorkDay>(
    'work-days',
    { awaiting_me: true, page_size: 100 },
    { enabled: Boolean(user) },
  );
  const awaitingCount = awaiting.data?.results.length ?? 0;

  return { isDirector, viewAll, managesProject, awaitingCount, awaiting };
}
