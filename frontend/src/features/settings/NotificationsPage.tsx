/**
 * Who hears what, and how (L1, L2, C8).
 *
 * This screen exists because the first build had no editing surface for it, and
 * the reasoning offered for that — "who is told what is a control, not a
 * preference" — did not survive being questioned. The system already answers
 * both halves separately: `settings.manage` decides who may change it, and the
 * audit trail records what changed. Locking the decision away from the Owner and
 * Admin roles was not a safeguard.
 *
 * Two things make editing safe rather than merely possible, and both are visible
 * here rather than assumed:
 *
 * - **Nothing here is only a notification.** Every event is also visible in the
 *   app — a request nobody was told about still sits on Approvals, an overdue
 *   item still appears on Custody. Muting a message never hides work, and the
 *   screen says so.
 * - **The events that carry a control are marked**, with what silence costs
 *   stated in plain words at the point of choosing it. A warning, never a block.
 */

import { useEffect, useMemo, useState } from 'react';

import { api } from '../../api/client';
import { errorMessage, useResource } from '../../api/hooks';
import { Banner, Button, Card, Spinner } from '../../components/ui';
import { useSession } from '../../auth/session';
import { PERM } from '../../auth/permissions';

/** One row of the resolved matrix, as `/notifications/preferences` returns it. */
interface EventRow {
  event: string;
  label: string;
  recipients: string[];
  /** What will actually be used, after tenant-wide channel switches. */
  channels: string[];
  carries_a_control: boolean;
  enabled: boolean;
  /**
   * Who that is, by name. `people: null` means the group depends on the
   * document — whoever raised *this* request — so it cannot be named in advance.
   */
  people: { group: string; people: string[] | null }[];
}

/** Only sent to people who may change the settings — it is the company's
 *  commercial position, not information about the reader. */
interface SmsCredit {
  credit_balance: number;
  credit_price_kes: number;
  is_low: boolean;
}

interface StoredSettings {
  notification_channels: Record<string, boolean>;
  notification_matrix: Record<
    string,
    { recipients?: string[]; channels?: string[]; enabled?: boolean }
  >;
}

const CHANNELS: { key: string; label: string; hint: string }[] = [
  {
    key: 'in_app',
    label: 'In the app',
    hint: 'The bell in the top corner. Works without a phone signal.',
  },
  { key: 'email', label: 'Email', hint: 'For the people who work at a desk.' },
  {
    key: 'sms',
    label: 'SMS',
    // The cost and the balance are appended live — see `smsHint` below.
    hint: 'Reaches a technician who has no data.',
  },
  {
    key: 'whatsapp',
    label: 'WhatsApp',
    hint: 'Not available yet — we are waiting on WhatsApp Business approval, and will switch it on for you.',
  },
];

/**
 * What silence costs, per event, in the words of the yard.
 *
 * The screen used to badge these "carries a control" — a phrase from our own
 * requirements discussion that means nothing to somebody running a yard. Naming
 * the consequence is both plainer and more useful: the question an owner is
 * actually asking is "what happens if I turn this off?"
 */
const CONSEQUENCE: Record<string, string> = {
  'gate_out.awaiting_approval':
    'Approvers will not know a request is waiting, so releases may sit until somebody checks.',
  'approval.escalated':
    'Nobody is told when an approval has gone unanswered too long.',
  'custody.overdue': 'Overdue items stop being chased.',
  'variance.return_raised': 'A shortfall on return goes unannounced.',
  'release_variance.raised': 'A short release at the gate goes unannounced.',
  'job.closed_with_unaccounted':
    'A job closing with material unaccounted for is not reported to you.',
  'client_return.unacknowledged':
    'Nobody is reminded that a client has not signed for their material.',
  'disposal.awaiting_approval':
    'Approvers will not know a write-off is waiting for them.',
};

const RECIPIENT_LABELS: Record<string, string> = {
  approvers: 'Approvers',
  requester: 'Whoever raised it',
  storekeepers: 'Storekeepers',
  owner: 'Owner',
  custody_holder: 'Whoever is carrying it',
  holder: 'Whoever is holding it',
  supervisor: 'Their supervisor',
  fallback_approver: 'The fallback approver',
};

const CHANNEL_LABELS: Record<string, string> = {
  in_app: 'in-app',
  email: 'email',
  sms: 'SMS',
  whatsapp: 'WhatsApp',
};

/**
 * "Approvers" is the rule; these are the people.
 *
 * An owner deciding whether to mute a message wants to know who stops hearing
 * it, and the names are the answer — a role with nobody in it is worth seeing
 * before you rely on it. Groups that depend on the document keep their label,
 * because there is no honest way to name them in advance.
 */
function describeRecipients(row: EventRow): string {
  return row.people
    .map(({ group, people }) => {
      const label = RECIPIENT_LABELS[group] ?? group;
      if (people === null) return label;
      if (people.length === 0) return `${label} (nobody yet)`;
      if (people.length <= 3) return `${label}: ${people.join(', ')}`;
      return `${label}: ${people.slice(0, 2).join(', ')} and ${people.length - 2} more`;
    })
    .join(' · ');
}

export default function NotificationsPage() {
  const { hasAny } = useSession();
  const mayEdit = hasAny(PERM.SETTINGS_MANAGE);

  const stored = useResource<StoredSettings>('settings');
  const resolved = useResource<{ events: EventRow[]; sms?: SmsCredit }>(
    'notification-preferences',
  );

  const [channels, setChannels] = useState<Record<string, boolean>>({});
  const [matrix, setMatrix] = useState<StoredSettings['notification_matrix']>({});
  const [banner, setBanner] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!stored.data) return;
    setChannels(stored.data.notification_channels ?? {});
    setMatrix(stored.data.notification_matrix ?? {});
  }, [stored.data]);

  const events = useMemo(() => resolved.data?.events ?? [], [resolved.data]);
  const sms = resolved.data?.sms;

  // How many control-bearing events are currently silenced — worth stating once
  // at the top rather than only beside each row.
  const mutedControls = events.filter(
    (row) => row.carries_a_control && matrix[row.event]?.enabled === false,
  ).length;

  function setEventEnabled(row: EventRow, enabled: boolean) {
    setSaved(false);
    setMatrix((current) => ({
      ...current,
      [row.event]: {
        recipients: current[row.event]?.recipients ?? row.recipients,
        channels: current[row.event]?.channels ?? row.channels,
        enabled,
      },
    }));
  }

  async function save() {
    setBanner(null);
    setSaved(false);
    setSaving(true);
    try {
      await api.patch('/settings', {
        notification_channels: channels,
        notification_matrix: matrix,
      });
      await Promise.all([stored.refetch(), resolved.refetch()]);
      setSaved(true);
    } catch (error) {
      setBanner(errorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  if (stored.isLoading || resolved.isLoading) return <Spinner className="text-slate-400" />;
  if (stored.isError) return <Banner tone="error">{errorMessage(stored.error)}</Banner>;

  return (
    <div className="flex flex-col gap-6 pb-24">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Notifications</h1>
        <p className="mt-1 max-w-2xl text-sm text-slate-600">
          What your people are told, and how it reaches them. Turning a message
          off never hides anything — the work still appears in the app for
          anyone who looks. It only means nobody is told about it.
        </p>
      </div>

      {banner ? <Banner tone="error">{banner}</Banner> : null}
      {saved ? <Banner tone="success">Saved.</Banner> : null}
      {!mayEdit ? (
        <Banner tone="info">
          This is what you will be told. Changing it is for the owner and
          administrators.
        </Banner>
      ) : null}
      {sms && channels.sms && sms.credit_balance <= 0 ? (
        <Banner tone="error">
          No SMS credit left, so text messages are not being sent. Everything else
          is unaffected. Contact us to buy more.
        </Banner>
      ) : null}
      {mutedControls > 0 ? (
        <Banner tone="warning">
          {mutedControls === 1
            ? 'One thing worth knowing about is switched off.'
            : `${mutedControls} things worth knowing about are switched off.`}{' '}
          They still appear in the app; nobody is told about them.
        </Banner>
      ) : null}

      <Card>
        <h2 className="text-base font-semibold text-slate-900">Channels</h2>
        <p className="mt-1 text-sm text-slate-600">
          Switch one off here and no event can use it, whatever is ticked below.
        </p>
        <div className="mt-4 flex flex-col gap-3">
          {CHANNELS.map((channel) => (
            <label
              key={channel.key}
              className="flex min-h-[44px] items-start gap-3 text-sm"
            >
              <input
                type="checkbox"
                className="mt-1 size-4"
                checked={Boolean(channels[channel.key])}
                disabled={!mayEdit}
                onChange={(event) => {
                  setSaved(false);
                  setChannels((current) => ({
                    ...current,
                    [channel.key]: event.target.checked,
                  }));
                }}
              />
              <span>
                <span className="font-medium text-slate-900">{channel.label}</span>
                <span className="block text-slate-500">
                  {channel.hint}
                  {channel.key === 'sms' && sms ? (
                    <>
                      {' '}
                      Each message costs {sms.credit_price_kes} KES.{' '}
                      <span
                        className={
                          sms.is_low ? 'font-medium text-amber-800' : 'font-medium text-slate-700'
                        }
                      >
                        {sms.credit_balance} left
                        {sms.is_low ? ' — running low' : ''}.
                      </span>
                    </>
                  ) : null}
                </span>
              </span>
            </label>
          ))}
        </div>
      </Card>

      <Card>
        <h2 className="text-base font-semibold text-slate-900">Events</h2>
        <p className="mt-1 text-sm text-slate-600">
          Who is told, and how. This is what will actually happen.
        </p>

        <div className="mt-4 flex flex-col divide-y divide-slate-100">
          {events.map((row) => {
            const entry = matrix[row.event];
            const enabled = entry?.enabled ?? row.enabled;
            const reaches = row.channels.length
              ? row.channels.map((c) => CHANNEL_LABELS[c] ?? c).join(', ')
              : 'no channel is switched on for this';

            return (
              <div key={row.event} className="flex items-start gap-3 py-3">
                <input
                  type="checkbox"
                  className="mt-1 size-4 shrink-0"
                  checked={enabled}
                  disabled={!mayEdit}
                  aria-label={row.label}
                  onChange={(event) => setEventEnabled(row, event.target.checked)}
                />
                <div className="min-w-0">
                  <p className="text-sm font-medium text-slate-900">
                    {row.label}
                    {row.carries_a_control && !enabled ? (
                      <span className="ml-2 rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-900">
                        nobody is told
                      </span>
                    ) : null}
                  </p>
                  <p className="text-xs text-slate-500">
                    {/* When it is off, the badge already says so and the line
                        below says what that costs — repeating it here three
                        times helps nobody. */}
                    {describeRecipients(row)}
                    {enabled ? ` · ${reaches}` : ''}
                  </p>
                  {row.carries_a_control ? (
                    <p
                      className={
                        enabled ? 'mt-1 text-xs text-slate-500' : 'mt-1 text-xs text-amber-800'
                      }
                    >
                      {enabled ? 'If you turn this off: ' : ''}
                      {CONSEQUENCE[row.event] ?? 'Nobody will be told.'}
                    </p>
                  ) : null}
                </div>
              </div>
            );
          })}
        </div>
      </Card>

      {mayEdit ? (
        <div className="fixed inset-x-0 bottom-0 border-t border-slate-200 bg-white p-3 md:static md:border-0 md:bg-transparent md:p-0">
          <Button onClick={() => void save()} loading={saving} block>
            Save notification settings
          </Button>
        </div>
      ) : null}
    </div>
  );
}
