/**
 * "Add new …" from inside any form (design §7.3).
 *
 * Every reference select — project, site, client, subcontractor, location,
 * item, person — ends with one more option: add a new one. Choosing it opens
 * the same sheet the settings screen uses, in place, and when the record is
 * saved the select lands on it. The form the person was filling in keeps
 * everything they had typed.
 *
 * Before this, a storekeeper raising a gate-out for a site that was not yet
 * on the system had to abandon the form, find Settings → Network → Sites,
 * create it, come back and start over. That is the round trip this removes.
 *
 * One registry so the sheets are defined once. They are imported lazily:
 * pulling the whole settings area into the gate-in screen's bundle to gain a
 * button would undo the code-splitting §7.1 depends on.
 */

import { lazy, type ComponentType, type LazyExoticComponent } from 'react';

import { PERM, type Permission } from '../auth/permissions';

/** What every create sheet in the registry accepts. */
export interface QuickCreateSheetProps {
  open: boolean;
  onClose: () => void;
  /** Called with the saved record. The select uses its `id`. */
  onCreated?: (record: { id: number }) => void;
}

export interface QuickCreateEntry {
  /** As it reads in "Add new site…". */
  noun: string;
  /** Who may create one. Others simply do not see the option. */
  permission: Permission;
  Sheet: LazyExoticComponent<ComponentType<QuickCreateSheetProps>>;
}

type Loader<T extends QuickCreateSheetProps> = () => Promise<{ default: ComponentType<T> }>;

function sheet<T extends QuickCreateSheetProps>(load: Loader<T>) {
  return lazy(load) as LazyExoticComponent<ComponentType<QuickCreateSheetProps>>;
}

/**
 * Keyed by the API resource the select lists from, so a select declares
 * `resource="sites"` once and gets both its options and its "add new" from it.
 */
export const QUICK_CREATE: Record<string, QuickCreateEntry> = {
  projects: {
    noun: 'project',
    permission: PERM.CATALOGUE_MANAGE,
    Sheet: sheet(() =>
      import('./projects/ProjectsPage').then((m) => ({ default: m.ProjectSheet })),
    ),
  },
  sites: {
    noun: 'site',
    permission: PERM.CATALOGUE_MANAGE,
    Sheet: sheet(() => import('./settings/NetworkPage').then((m) => ({ default: m.SiteSheet }))),
  },
  clients: {
    noun: 'client',
    permission: PERM.CATALOGUE_MANAGE,
    Sheet: sheet(() =>
      import('./settings/NetworkPage').then((m) => ({ default: m.ClientSheet })),
    ),
  },
  subcontractors: {
    noun: 'subcontractor',
    permission: PERM.CATALOGUE_MANAGE,
    Sheet: sheet(() =>
      import('./settings/NetworkPage').then((m) => ({ default: m.SubcontractorSheet })),
    ),
  },
  locations: {
    noun: 'location',
    permission: PERM.CATALOGUE_MANAGE,
    Sheet: sheet(() =>
      import('./settings/NetworkPage').then((m) => ({ default: m.LocationSheet })),
    ),
  },
  'item-types': {
    noun: 'item',
    permission: PERM.CATALOGUE_MANAGE,
    Sheet: sheet(() =>
      import('./settings/CataloguePage').then((m) => ({ default: m.ItemTypeSheet })),
    ),
  },
  users: {
    noun: 'person',
    permission: PERM.USERS_MANAGE,
    Sheet: sheet(() =>
      import('./settings/UsersPage').then((m) => ({ default: m.NewPersonSheet })),
    ),
  },
};
