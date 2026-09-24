/**
 * T2.15 — users, roles and delegation (design §7.4, §4.2; B3, B4, B5, F5).
 *
 * The criterion: "deactivating a user holding custody warns and blocks until
 * custody is cleared."
 *
 * So this screen does not ask the server to deactivate and then report whatever
 * came back as a generic failure. It shows *what they are holding*, from the
 * refusal's own details, because "Hilda is still holding a torque wrench" is
 * actionable and "could not deactivate user" is not.
 *
 * The role editor shows each permission's rationale (§4.2). B4 lets a tenant
 * invent any role it likes, which only works if the person ticking the boxes can
 * tell what each box does — a list of dotted codenames cannot.
 */

import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { useSearchParams } from 'react-router-dom';

import { TabStrip } from '../../components/ui/TabStrip';
import { SwipePane } from '../../components/ui/SwipePane';
import { useSwipeTabs } from '../../components/ui/useSwipeTabs';

import { ApiError } from '../../api/client';
import { applyFieldErrors, errorMessage, useAction, useList, useResource } from '../../api/hooks';
import { useSession } from '../../auth/session';
import {
  Banner,
  Button,
  Card,
  Checkbox,
  Field,
  Input,
  Select,
  Spinner,
  Textarea,
} from '../../components/ui';
import { ReferenceSelect } from '../../components/ui/ReferenceSelect';
import { DataList, EmptyState, PageHeader, Sheet, StatusBadge } from '../../components/ui/data';
import type { Delegation, ManagedUser, PermissionGroup, Role } from './types';

type Tab = 'people' | 'roles' | 'delegations';

const USER_TABS: readonly Tab[] = ['people', 'roles', 'delegations'];

export default function UsersPage() {
  const [params, setParams] = useSearchParams();
  const tab = (params.get('tab') as Tab) || 'people';
  // A thumb across the content moves between tabs; the strip still works.
  const swipe = useSwipeTabs(USER_TABS, tab, (next) => setParams({ tab: next }));

  return (
    <div className="flex flex-col gap-4" {...swipe.handlers}>
      <PageHeader
        title="People and permissions"
        subtitle="Who can do what, and who is covering for whom."
      />

      <TabStrip
        tabs={[
          { key: 'people', label: 'People' },
          { key: 'roles', label: 'Roles' },
          { key: 'delegations', label: 'Delegations' },
        ]}
        current={tab}
        onSelect={(next) => setParams({ tab: next })}
      />

      <SwipePane {...swipe.pane}>
        {tab === 'people' ? <PeopleTab /> : null}
        {tab === 'roles' ? <RolesTab /> : null}
        {tab === 'delegations' ? <DelegationsTab /> : null}
      </SwipePane>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* People                                                                     */
/* -------------------------------------------------------------------------- */

interface Holding {
  item: string;
  quantity: string;
  uom: string;
  owner: string;
}

function PeopleTab() {
  const [sheet, setSheet] = useState(false);
  const [editing, setEditing] = useState<ManagedUser | null>(null);
  const [blocked, setBlocked] = useState<{ user: ManagedUser; holdings: Holding[] } | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const users = useList<ManagedUser>('users', { page_size: 200 });
  const roles = useList<Role>('roles', { page_size: 200 });

  const deactivate = useAction<{ id: number }>({
    resource: 'users',
    path: (body) => `${body.id}/deactivate`,
    invalidates: ['users'],
  });
  const reactivate = useAction<{ id: number }>({
    resource: 'users',
    path: (body) => `${body.id}/reactivate`,
    invalidates: ['users'],
  });
  // B1: an account is created with no password and the person sets their own.
  // Links get lost — a phone is wiped, an email goes to spam — and until now the
  // only way to send another was to ask the platform owner, which is not
  // something a tenant should have to do about their own staff.
  const resendInvitation = useAction<{ id: number }>({
    resource: 'users',
    path: (body) => `${body.id}/resend-invitation`,
    invalidates: ['users'],
  });

  async function onResend(user: ManagedUser) {
    setBanner(null);
    setNotice(null);
    try {
      await resendInvitation.mutateAsync({ id: user.id });
      // `notice`, not `banner`: this worked, and a green line saying so is the
      // whole point. It went out red until somebody read it back to me.
      setNotice(
        `Invitation sent again to ${user.email || user.phone}. They set their own password from the link.`,
      );
    } catch (error) {
      setBanner(errorMessage(error));
    }
  }

  async function onDeactivate(user: ManagedUser) {
    setBanner(null);
    setBlocked(null);
    try {
      await deactivate.mutateAsync({ id: user.id });
    } catch (error) {
      // B3's edge case. The refusal carries what they are holding, so the
      // administrator can go and get it rather than guessing why.
      if (error instanceof ApiError && error.code === 'HOLDER_STILL_HAS_MATERIAL') {
        setBlocked({
          user,
          holdings: (error.details.holdings as Holding[] | undefined) ?? [],
        });
        return;
      }
      setBanner(errorMessage(error));
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex justify-end">
        <Button onClick={() => setSheet(true)}>Add someone</Button>
      </div>

      {notice ? <Banner tone="success">{notice}</Banner> : null}
      {banner ? <Banner tone="error">{banner}</Banner> : null}

      {blocked ? (
        <Card className="border-amber-300 bg-amber-50">
          <h2 className="text-sm font-semibold text-amber-900">
            {blocked.user.full_name || 'This person'} is still holding material
          </h2>
          <p className="mt-1 text-sm text-amber-900">
            Deactivating them now would leave it somewhere with nobody accountable
            for it. Have it returned at the gate, or handed over to someone else,
            and then try again.
          </p>
          <ul className="mt-2 flex flex-col gap-1 text-sm text-amber-900">
            {blocked.holdings.map((holding) => (
              <li key={`${holding.item}-${holding.owner}`}>
                {holding.quantity} {holding.uom} {holding.item}
                <span className="text-amber-700"> · {holding.owner}</span>
              </li>
            ))}
          </ul>
          <div className="mt-3">
            <Button variant="secondary" onClick={() => setBlocked(null)}>
              Understood
            </Button>
          </div>
        </Card>
      ) : null}

      {users.isLoading ? (
        <Spinner className="text-slate-400" />
      ) : (
        <DataList
          rows={users.data?.results ?? []}
          rowKey={(row) => row.id}
          empty={<EmptyState title="Nobody here yet." />}
          columns={[
            {
              header: 'Name',
              cell: (row) => (
                <span className={row.is_active ? '' : 'text-slate-400'}>
                  {row.full_name || row.email || row.phone}
                </span>
              ),
            },
            { header: 'Email', cell: (row) => row.email || '—', wideOnly: true },
            { header: 'Phone', cell: (row) => row.phone || '—', wideOnly: true },
            {
              header: 'Roles',
              cell: (row) =>
                row.roles.length ? row.roles.map((role) => role.name).join(', ') : '—',
            },
            {
              header: 'Status',
              cell: (row) => <StatusBadge status={row.is_active ? 'OPEN' : 'CLOSED'} />,
            },
            {
              header: '',
              wideOnly: true,
              cell: (row) => (
                <div className="flex gap-2">
                  <Button
                    variant="ghost"
                    className="min-h-0 px-2 py-1 text-sm"
                    onClick={() => setEditing(row)}
                  >
                    Edit
                  </Button>
                  {row.is_active && !row.has_signed_in_yet ? (
                    <Button
                      variant="ghost"
                      className="min-h-0 px-2 py-1 text-sm"
                      onClick={() => void onResend(row)}
                    >
                      Resend invitation
                    </Button>
                  ) : null}
                  {row.is_active ? (
                    <Button
                      variant="ghost"
                      className="min-h-0 px-2 py-1 text-sm text-red-700"
                      onClick={() => onDeactivate(row)}
                    >
                      Deactivate
                    </Button>
                  ) : (
                    <Button
                      variant="ghost"
                      className="min-h-0 px-2 py-1 text-sm"
                      onClick={() => reactivate.mutate({ id: row.id })}
                    >
                      Reactivate
                    </Button>
                  )}
                </div>
              ),
            },
          ]}
        />
      )}

      <p className="text-sm text-slate-500">
        Nobody is ever deleted. Deactivating preserves every document they
        posted, which is the point of an audit trail.
      </p>

      <PersonSheet
        open={sheet || editing !== null}
        user={editing}
        roles={roles.data?.results ?? []}
        onClose={() => {
          setSheet(false);
          setEditing(null);
        }}
      />
    </div>
  );
}

interface PersonForm {
  full_name: string;
  email: string;
  phone: string;
  role_ids: string[];
}

export function PersonSheet({
  open,
  user,
  roles: givenRoles,
  onClose,
  onCreated,
}: {
  open: boolean;
  user: ManagedUser | null;
  /** The People tab has these; a form elsewhere does not, so fetch them. */
  roles?: Role[];
  onClose: () => void;
  onCreated?: (record: { id: number }) => void;
}) {
  const fetchedRoles = useList<Role>('roles', { page_size: 200 }, { enabled: givenRoles === undefined });
  const roles = givenRoles ?? fetchedRoles.data?.results ?? [];
  const form = useForm<PersonForm>({
    values: {
      full_name: user?.full_name ?? '',
      email: user?.email ?? '',
      phone: user?.phone ?? '',
      role_ids: (user?.roles ?? []).map((role) => String(role.id)),
    },
  });
  const [banner, setBanner] = useState<string | null>(null);

  const create = useAction<Record<string, unknown>, { id: number }>({ resource: 'users' });
  const update = useAction<Record<string, unknown>>({
    resource: 'users',
    path: () => String(user?.id),
    method: 'patch',
    invalidates: ['users'],
  });

  const submit = form.handleSubmit(async (values) => {
    setBanner(null);
    const payload = {
      full_name: values.full_name,
      // B1: either identifier is enough, and a yard hand may have only a phone.
      email: values.email || null,
      phone: values.phone || null,
      role_ids: values.role_ids.map(Number),
    };
    try {
      let created: { id: number } | null = null;
      if (user) await update.mutateAsync(payload);
      else created = await create.mutateAsync(payload);
      form.reset();
      onClose();
      if (created) onCreated?.(created);
    } catch (error) {
      setBanner(applyFieldErrors(error, form.setError));
    }
  });

  return (
    <Sheet
      open={open}
      title={user ? `Edit ${user.full_name || 'person'}` : 'Add someone'}
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} block>
            Cancel
          </Button>
          <Button onClick={submit} loading={create.isPending || update.isPending} block>
            Save
          </Button>
        </>
      }
    >
      <form className="flex flex-col gap-3" onSubmit={submit}>
        {banner ? <Banner tone="error">{banner}</Banner> : null}

        <Field label="Name" htmlFor="person-name" error={form.formState.errors.full_name?.message}>
          <Input
            id="person-name"
            {...form.register('full_name', { required: 'A name is required.' })}
          />
        </Field>

        <Field
          label="Email"
          htmlFor="person-email"
          hint="An email or a phone number. Either is enough to sign in with."
        >
          <Input id="person-email" type="email" {...form.register('email')} />
        </Field>

        <Field label="Phone" htmlFor="person-phone">
          <Input id="person-phone" inputMode="tel" placeholder="+2547…" {...form.register('phone')} />
        </Field>

        <fieldset className="flex flex-col rounded-lg border border-slate-200 p-3">
          <legend className="px-1 text-sm font-medium text-slate-700">Roles</legend>
          {roles.length === 0 ? (
            <p className="text-sm text-slate-500">No roles defined yet.</p>
          ) : (
            roles.map((role) => (
              <Checkbox
                key={role.id}
                id={`person-role-${role.id}`}
                label={role.name}
                hint={role.description || `${role.codenames.length} permissions`}
                value={String(role.id)}
                {...form.register('role_ids')}
              />
            ))
          )}
        </fieldset>

        {user ? (
          <div>
            <h3 className="text-sm font-medium text-slate-700">
              What that resolves to
            </h3>
            <p className="text-sm text-slate-500">
              Resolved by the server, including anything delegated to them.
            </p>
            <ul className="mt-1 grid gap-0.5 sm:grid-cols-2">
              {user.permissions.map((permission) => (
                <li key={permission} className="font-mono text-xs text-slate-600">
                  {permission}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </form>
    </Sheet>
  );
}

/* -------------------------------------------------------------------------- */
/* Roles                                                                      */
/* -------------------------------------------------------------------------- */

function RolesTab() {
  const [editing, setEditing] = useState<Role | null>(null);
  const [creating, setCreating] = useState(false);
  const roles = useList<Role>('roles', { page_size: 200 });

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-slate-600">
          Roles are yours to define. The permissions they can contain are
          fixed by what the software actually checks.
        </p>
        <Button onClick={() => setCreating(true)}>New role</Button>
      </div>

      {roles.isLoading ? (
        <Spinner className="text-slate-400" />
      ) : (
        <DataList
          rows={roles.data?.results ?? []}
          rowKey={(row) => row.id}
          onRowClick={(row) => setEditing(row)}
          empty={<EmptyState title="No roles yet." />}
          columns={[
            { header: 'Role', cell: (row) => row.name },
            { header: 'People', cell: (row) => row.user_count },
            { header: 'Permissions', cell: (row) => row.codenames.length },
            {
              header: 'Origin',
              wideOnly: true,
              cell: (row) => (row.is_system ? 'seeded with the tenant' : 'made here'),
            },
          ]}
        />
      )}

      <RoleSheet
        open={creating || editing !== null}
        role={editing}
        onClose={() => {
          setCreating(false);
          setEditing(null);
        }}
      />
    </div>
  );
}

function RoleSheet({
  open,
  role,
  onClose,
}: {
  open: boolean;
  role: Role | null;
  onClose: () => void;
}) {
  const groups = useResource<{ groups: PermissionGroup[] }>('permission-groups');
  const form = useForm<{ name: string; description: string; codenames: string[] }>({
    values: {
      name: role?.name ?? '',
      description: role?.description ?? '',
      codenames: role?.codenames ?? [],
    },
  });
  const [banner, setBanner] = useState<string | null>(null);

  const create = useAction<Record<string, unknown>>({ resource: 'roles' });
  const update = useAction<Record<string, unknown>>({
    resource: 'roles',
    path: () => String(role?.id),
    method: 'patch',
    invalidates: ['roles', 'users'],
  });

  const submit = form.handleSubmit(async (values) => {
    setBanner(null);
    try {
      if (role) await update.mutateAsync(values);
      else await create.mutateAsync(values);
      form.reset();
      onClose();
    } catch (error) {
      setBanner(applyFieldErrors(error, form.setError));
    }
  });

  return (
    <Sheet
      open={open}
      title={role ? `Edit ${role.name}` : 'New role'}
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} block>
            Cancel
          </Button>
          <Button onClick={submit} loading={create.isPending || update.isPending} block>
            Save
          </Button>
        </>
      }
    >
      <form className="flex flex-col gap-3" onSubmit={submit}>
        {banner ? <Banner tone="error">{banner}</Banner> : null}

        <Field label="Name" htmlFor="role-name" error={form.formState.errors.name?.message}>
          <Input id="role-name" {...form.register('name', { required: 'A name is required.' })} />
        </Field>

        <Field label="What it is for" htmlFor="role-description">
          <Textarea id="role-description" {...form.register('description')} />
        </Field>

        {groups.isLoading ? <Spinner className="text-slate-400" /> : null}
        {(groups.data?.groups ?? []).map((group) => (
          <fieldset key={group.group} className="rounded-lg border border-slate-200 p-3">
            <legend className="px-1 text-sm font-medium text-slate-700">{group.group}</legend>
            {group.permissions.map((permission) => (
              <Checkbox
                key={permission.codename}
                id={`perm-${permission.codename}`}
                label={permission.label}
                // §4.2: why this is its own switch. Without it, an
                // administrator is ticking codenames and hoping.
                hint={permission.rationale}
                value={permission.codename}
                {...form.register('codenames')}
              />
            ))}
          </fieldset>
        ))}
      </form>
    </Sheet>
  );
}

/* -------------------------------------------------------------------------- */
/* Delegations                                                                */
/* -------------------------------------------------------------------------- */

function DelegationsTab() {
  const [sheet, setSheet] = useState(false);
  const delegations = useList<Delegation>('delegations', { page_size: 100 });
  const revoke = useAction<{ id: number }>({
    resource: 'delegations',
    path: (body) => `${body.id}/revoke`,
    invalidates: ['delegations'],
  });

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-slate-600">
          Approvals continue while someone is away. A delegated approval is
          always recorded as "X on behalf of Y" — never as Y, because that would
          be a signature they never gave.
        </p>
        <Button onClick={() => setSheet(true)}>New delegation</Button>
      </div>

      {delegations.isLoading ? (
        <Spinner className="text-slate-400" />
      ) : (
        <DataList
          rows={delegations.data?.results ?? []}
          rowKey={(row) => row.id}
          empty={<EmptyState title="No delegations." hint="Nobody is covering for anybody." />}
          columns={[
            { header: 'From', cell: (row) => row.from_user_name },
            { header: 'To', cell: (row) => row.to_user_name },
            {
              // Without this the table said somebody was covering, but not for
              // what — and a delegation carrying nothing looked identical to one
              // carrying everything.
              header: 'Delegates',
              cell: (row) =>
                row.role_name
                  ? row.role_name
                  : row.codenames.length === 1
                    ? '1 permission'
                    : `${row.codenames.length} permissions`,
            },
            {
              header: 'Window',
              cell: (row) =>
                `${row.starts_at.slice(0, 10)} → ${row.ends_at.slice(0, 10)}`,
            },
            {
              header: 'Active',
              cell: (row) => (
                <StatusBadge
                  status={
                    row.is_revoked ? 'CANCELLED' : row.is_currently_active ? 'OPEN' : 'CLOSED'
                  }
                />
              ),
            },
            { header: 'Reason', cell: (row) => row.reason || '—', wideOnly: true },
            {
              header: '',
              wideOnly: true,
              cell: (row) =>
                row.is_revoked ? null : (
                  <Button
                    variant="ghost"
                    className="min-h-0 px-2 py-1 text-sm"
                    onClick={() => revoke.mutate({ id: row.id })}
                  >
                    Revoke
                  </Button>
                ),
            },
          ]}
        />
      )}

      <DelegationSheet open={sheet} onClose={() => setSheet(false)} />
    </div>
  );
}

function DelegationSheet({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { user } = useSession();
  const users = useList<ManagedUser>('users', { page_size: 200 });
  const roles = useList<Role>('roles', { page_size: 200 });

  const form = useForm<{
    from_user: string;
    to_user: string;
    role: string;
    starts_at: string;
    ends_at: string;
    reason: string;
  }>({
    defaultValues: {
      from_user: user ? String(user.id) : '',
      to_user: '',
      role: '',
      starts_at: '',
      ends_at: '',
      reason: '',
    },
  });
  const [banner, setBanner] = useState<string | null>(null);
  const create = useAction<Record<string, unknown>>({ resource: 'delegations' });

  const submit = form.handleSubmit(async (values) => {
    setBanner(null);
    try {
      await create.mutateAsync({
        from_user: Number(values.from_user),
        to_user: Number(values.to_user),
        role: values.role ? Number(values.role) : null,
        starts_at: new Date(values.starts_at).toISOString(),
        ends_at: new Date(values.ends_at).toISOString(),
        reason: values.reason,
      });
      form.reset();
      onClose();
    } catch (error) {
      setBanner(applyFieldErrors(error, form.setError));
    }
  });

  const people = users.data?.results ?? [];

  return (
    <Sheet
      open={open}
      title="New delegation"
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} block>
            Cancel
          </Button>
          <Button onClick={submit} loading={create.isPending} block>
            Create
          </Button>
        </>
      }
    >
      <form className="flex flex-col gap-3" onSubmit={submit}>
        {banner ? <Banner tone="error">{banner}</Banner> : null}

        <Field
          label="Lending authority"
          htmlFor="del-from"
          error={form.formState.errors.from_user?.message}
        >
          <ReferenceSelect resource="users" form={form} name="from_user" rules={{ required: true }} id="del-from">
            {people.map((person) => (
              <option key={person.id} value={person.id}>
                {person.full_name || person.email}
              </option>
            ))}
          </ReferenceSelect>
        </Field>

        <Field
          label="Acting for them"
          htmlFor="del-to"
          error={form.formState.errors.to_user?.message}
        >
          <ReferenceSelect resource="users" form={form} name="to_user" rules={{ required: true }} id="del-to">
            <option value="">Choose…</option>
            {people.map((person) => (
              <option key={person.id} value={person.id}>
                {person.full_name || person.email}
              </option>
            ))}
          </ReferenceSelect>
        </Field>

        <Field
          label="Role delegated"
          htmlFor="del-role"
          hint="The usual case: lend a whole role for the window."
          error={form.formState.errors.role?.message}
        >
          <Select id="del-role" {...form.register('role')}>
            <option value="">Choose…</option>
            {(roles.data?.results ?? []).map((role) => (
              <option key={role.id} value={role.id}>
                {role.name}
              </option>
            ))}
          </Select>
        </Field>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field
            label="From"
            htmlFor="del-start"
            error={form.formState.errors.starts_at?.message}
          >
            <Input
              id="del-start"
              type="datetime-local"
              {...form.register('starts_at', { required: 'A start is required.' })}
            />
          </Field>
          <Field
            label="Until"
            htmlFor="del-end"
            error={form.formState.errors.ends_at?.message}
          >
            <Input
              id="del-end"
              type="datetime-local"
              {...form.register('ends_at', { required: 'An end is required.' })}
            />
          </Field>
        </div>

        <Field label="Why" htmlFor="del-reason">
          <Input id="del-reason" placeholder="Away at a site build." {...form.register('reason')} />
        </Field>
      </form>
    </Sheet>
  );
}

/**
 * "Add new person…" from inside another form (features/quickCreate). The
 * same sheet the People tab uses, fixed to create mode.
 */
export function NewPersonSheet({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated?: (record: { id: number }) => void;
}) {
  return <PersonSheet open={open} user={null} onClose={onClose} onCreated={onCreated} />;
}
