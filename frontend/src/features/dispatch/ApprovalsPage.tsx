/**
 * T4.22 — the approval screens (design §7.4, §5.3; F4, F5, M3).
 *
 * The criterion: "an owner can approve **from a phone in two taps** after
 * authenticating."
 *
 * Two taps means the decision is on the list, not two screens away — an owner
 * reading a notification on a phone should not have to open a document, scroll,
 * and hunt for a button. So each pending row carries its own approve and reject,
 * and the detail view is there for when the owner wants to look properly.
 *
 * What a row has to show for that to be *safe* is the substance of F4: every
 * line, its criticality, and whether the material belongs to a client. Approving
 * blind is worse than not approving at all, and an owner tapping through a list
 * is the exact situation where a screen has to make ownership impossible to miss
 * (E1).
 */

import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';

import { errorMessage, useAction, useDetail, useList } from '../../api/hooks';
import { PERM } from '../../auth/permissions';
import { useCrumb } from '../../components/ui/breadcrumbs';
import { TabStrip } from '../../components/ui/TabStrip';
import { SwipePane } from '../../components/ui/SwipePane';
import { useSwipeTabs } from '../../components/ui/useSwipeTabs';
import { CloseoutQueue, ExpenseQueue } from '../projects/ProjectQueuesPage';
import { biometricsAvailable, signApproval } from '../../auth/webauthn';
import { useSession } from '../../auth/session';
import {
  Banner,
  Button,
  Card,
  Field,
  OwnershipBadge,
  Spinner,
  Textarea,
} from '../../components/ui';
import { EmptyState, PageHeader, Sheet, StatusBadge } from '../../components/ui/data';
import type { ApprovalRequest } from './types';

/**
 * One decision, in words.
 *
 * The pieces used to be concatenated straight from the record, which for an
 * automatic approval produced "auto by Automatically approved — no approval
 * required — No approval rule applies to this request. · system". Every part of
 * that is true and nobody can read it. An automatic approval is one fact and
 * reads as one sentence; a person's decision keeps the attribution, because
 * "X on behalf of Y" must never collapse into "Y".
 */
function decisionLine(action: {
  decision: string;
  attribution?: string | null;
  actor_name?: string | null;
  reason?: string | null;
}): string {
  if (action.decision === 'AUTO') {
    const reason = (action.reason ?? '').trim().replace(/\.$/, '');
    return reason
      ? `Approved automatically — ${reason.charAt(0).toLowerCase()}${reason.slice(1)}`
      : 'Approved automatically — no approval rule applies';
  }
  const who = action.attribution || action.actor_name || 'somebody';
  const verb = action.decision.toLowerCase();
  return `${verb} by ${who}${action.reason ? ` — ${action.reason}` : ''}`;
}

type Tab = 'material' | 'expenses' | 'closeouts';

/**
 * Every decision waiting on one person, in one place.
 *
 * The project queues were briefly a screen of their own, which left a manager
 * two lists to check for the same act. What separates them is only whether the
 * thing agreed to is a movement or a number — a fact about the record, not
 * about the decision — so they are tabs here instead.
 */
export default function ApprovalsPage() {
  const { has } = useSession();

  // Only the tabs this person can act on. A manager who approves expenses but
  // releases no material should not be shown an empty material queue and left
  // wondering whether it is empty or forbidden.
  const tabs: { key: Tab; label: string }[] = [
    ...(has(PERM.GATE_OUT_APPROVE) || has(PERM.DISPOSAL_APPROVE)
      ? [{ key: 'material' as Tab, label: 'Material' }]
      : []),
    ...(has(PERM.PROJECT_VIEW_COST)
      ? [
          { key: 'expenses' as Tab, label: 'Expenses' },
          { key: 'closeouts' as Tab, label: 'Closeout costs' },
        ]
      : []),
  ];

  const [tab, setTab] = useState<Tab>(tabs[0]?.key ?? 'material');
  // On a phone, a thumb across the queue moves to the next one. The strip
  // still works; this is the gesture people already make.
  const swipe = useSwipeTabs<Tab>(
    tabs.map((entry) => entry.key),
    tab,
    setTab,
  );

  return (
    <div className="flex flex-1 flex-col gap-4" {...swipe.handlers}>
      <PageHeader
        title="Approvals"
        subtitle="What is waiting on a decision, and everything already decided."
      />

      {/* One tab is no choice, so do not draw a strip for it. */}
      {tabs.length > 1 ? (
        <TabStrip<Tab> tabs={tabs} current={tab} onSelect={setTab} aria-label="Queues" />
      ) : null}

      <SwipePane {...swipe.pane} className="flex-1">
        {tab === 'expenses' ? <ExpenseQueue /> : null}
        {tab === 'closeouts' ? <CloseoutQueue /> : null}
        {tab === 'material' ? <MaterialQueue /> : null}
      </SwipePane>
    </div>
  );
}

function MaterialQueue() {
  const [scope, setScope] = useState<'pending' | 'all'>('pending');
  const pending = useList<ApprovalRequest>(
    scope === 'pending' ? 'approvals/pending' : 'approvals',
    { page_size: 50 },
  );

  return (
    <div className="flex flex-col gap-4">
      <div className="flex justify-end">
        <Button
          variant="secondary"
          onClick={() => setScope(scope === 'pending' ? 'all' : 'pending')}
        >
          {scope === 'pending' ? 'Show all' : 'Show only waiting'}
        </Button>
      </div>

      {pending.isError ? <Banner tone="error">{errorMessage(pending.error)}</Banner> : null}

      {pending.isLoading ? (
        <Spinner className="text-slate-400" />
      ) : (pending.data?.results ?? []).length === 0 ? (
        <EmptyState
          title={scope === 'pending' ? 'Nothing waiting on you.' : 'No approvals yet.'}
          hint="A request routes here when its contents need a sign-off."
        />
      ) : (
        <ul className="flex flex-col gap-3">
          {(pending.data?.results ?? []).map((request) => (
            <ApprovalCard key={request.id} request={request} />
          ))}
        </ul>
      )}
    </div>
  );
}

function ApprovalCard({ request }: { request: ApprovalRequest }) {
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState('');
  const [banner, setBanner] = useState<string | null>(null);
  const { user } = useSession();

  const gateOut = request.document;
  const invalidates = ['approvals', 'approvals/pending', 'gate-outs', 'notifications'];

  const approve = useAction<{ reason: string; assertion?: Record<string, unknown> }>({
    resource: 'gate-outs',
    path: () => `${request.document_id}/approve`,
    invalidates,
  });
  const reject = useAction<{ reason: string }>({
    resource: 'gate-outs',
    path: () => `${request.document_id}/reject`,
    invalidates,
  });

  const isOwnRequest = gateOut?.requested_by_id === user?.id;
  const isPending = request.status === 'PENDING';

  async function decide(which: 'approve' | 'reject') {
    setBanner(null);
    try {
      if (which === 'approve') {
        // T8.10: ask for the fingerprint where there is one, and carry on
        // without it where there is not. `signApproval` returns null rather
        // than throwing for "no sensor", "nothing enrolled" and "prompt
        // dismissed" — all three are a password approval, not an error.
        const assertion = await signApproval(request.id);
        await approve.mutateAsync({
          reason: '',
          ...(assertion ? { assertion } : {}),
        });
      } else {
        await reject.mutateAsync({ reason });
      }
      setRejecting(false);
      setReason('');
    } catch (error) {
      setBanner(errorMessage(error));
    }
  }

  const lines = gateOut?.lines ?? [];
  const [biometric, setBiometric] = useState(false);
  useEffect(() => {
    // Asked once per card render rather than assumed: the same build runs on an
    // Android phone with a sensor and a desktop without one (§7.2).
    void biometricsAvailable().then(setBiometric);
  }, []);
  const highLines = lines.filter((line) => line.criticality === 'HIGH');
  const clientLines = lines.filter((line) => line.is_client_owned);

  return (
    <li>
      <Card className="flex flex-col gap-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <p className="text-base font-semibold text-slate-900">
              <Link to={`/approvals/${request.id}`} className="underline-offset-2 hover:underline">
                {request.document_number || 'Request'}
              </Link>
            </p>
            <p className="text-sm text-slate-600">
              {gateOut?.destination ?? request.document_type} · for{' '}
              {gateOut?.custody_holder ?? 'someone'}
            </p>
          </div>
          <div className="flex flex-col items-end gap-1">
            <StatusBadge status={request.status} />
            {request.level > 0 ? (
              <span className="text-xs text-slate-500">
                level {request.level}
                {request.role_name ? ` · ${request.role_name}` : ''}
              </span>
            ) : null}
          </div>
        </div>

        {banner ? <Banner tone="error">{banner}</Banner> : null}

        {/* "Superseded" is a database word. What happened is that the request
            was changed after this decision, so the decision no longer stands —
            which is the whole point of the record being kept. */}
        {request.status === 'SUPERSEDED' ? (
          <p className="text-sm text-slate-600">
            The request was amended after this decision, so it no longer stands.
            It is kept as a record of what was decided before the change.
          </p>
        ) : null}

        {/* F4: every line, with what routed it here. */}
        <ul className="flex flex-col gap-1 text-sm">
          {lines.map((line, index) => (
            <li key={`${line.item}-${index}`} className="flex flex-wrap items-center gap-2">
              <span className="font-medium text-slate-900">
                {line.quantity} {line.uom}
              </span>
              <span className="text-slate-700">{line.item}</span>
              {line.criticality === 'HIGH' ? (
                <span className="rounded-full bg-red-100 px-2 py-0.5 text-xs font-medium text-red-900">
                  high criticality
                </span>
              ) : null}
              {/* E1: ownership on the approval screen, not a tap away — it
                  changes whether you approve. */}
              <OwnershipBadge client={line.is_client_owned ? line.owner : null} />
            </li>
          ))}
        </ul>

        {clientLines.length > 0 ? (
          <Banner tone="warning">
            This includes material belonging to a client. It stays theirs wherever
            it goes, and an operator audit will ask about it.
          </Banner>
        ) : null}

        {highLines.length > 0 ? (
          <p className="text-sm text-slate-600">
            {highLines.length} high-criticality line{highLines.length === 1 ? '' : 's'} is
            what routed this to you.
          </p>
        ) : null}

        {gateOut?.notes ? (
          <p className="text-sm text-slate-600">“{gateOut.notes}”</p>
        ) : null}

        {request.actions.length > 0 ? (
          <ul className="flex flex-col gap-1 border-t border-slate-100 pt-2 text-sm">
            {request.actions.map((action) => (
              <li key={action.id} className="text-slate-600">
                {/* §4.2: "X on behalf of Y", never just Y — a delegated
                    approval must never look like the principal's own. */}
                {decisionLine(action)}
                <span className="text-slate-400">
                  {' '}
                  · {action.decided_at?.slice(0, 16).replace('T', ' ')}
                </span>
                {action.decision !== 'AUTO' &&
                action.auth_method &&
                action.auth_method !== 'PASSWORD' ? (
                  <span className="text-slate-400"> · {action.auth_method.toLowerCase()}</span>
                ) : null}
              </li>
            ))}
          </ul>
        ) : null}

        {isPending ? (
          isOwnRequest ? (
            <Banner tone="info">
              You raised this. §5.3 refuses a self-approval unless this
              organization has deliberately allowed it — somebody else has to sign
              it off.
            </Banner>
          ) : (
            <div className="flex gap-2">
              <Button block loading={approve.isPending} onClick={() => decide('approve')}>
                {biometric ? 'Approve with fingerprint' : 'Approve'}
              </Button>
              <Button variant="danger" block onClick={() => setRejecting(true)}>
                Reject
              </Button>
            </div>
          )
        ) : null}
      </Card>

      <Sheet
        open={rejecting}
        title={`Reject ${request.document_number}`}
        onClose={() => setRejecting(false)}
        footer={
          <>
            <Button variant="secondary" block onClick={() => setRejecting(false)}>
              Back
            </Button>
            <Button
              variant="danger"
              block
              disabled={!reason.trim()}
              loading={reject.isPending}
              onClick={() => decide('reject')}
            >
              Reject
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          <p className="text-sm text-slate-700">
            A rejection has to say why. The requester sees the reason, which is
            what lets them fix it rather than ask around.
          </p>
          <Field label="Reason" htmlFor={`reject-${request.id}`}>
            <Textarea
              id={`reject-${request.id}`}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </Field>
        </div>
      </Sheet>
    </li>
  );
}

/** The full history of one approval request (M3). */
export function ApprovalDetailPage() {
  const { id } = useParams();
  const request = useDetail<ApprovalRequest>('approvals', id);
  useCrumb(request.data?.document_number);

  if (request.isLoading) return <Spinner className="text-slate-400" />;
  if (request.isError) return <Banner tone="error">{errorMessage(request.error)}</Banner>;

  const data = request.data!;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={data.document_number || 'Approval request'}
        subtitle={
          <span className="flex items-center gap-2">
            <StatusBadge status={data.status} />
            {data.level > 0 ? `level ${data.level}` : null}
            {data.role_name ? ` · ${data.role_name}` : ''}
          </span>
        }
        actions={
          <>
            <Link
              to="/approvals"
              className="flex min-h-[44px] items-center px-2 text-sm text-slate-700"
            >
              Back
            </Link>
            {data.document_id ? (
              <Link
                to={`/gate-out/${data.document_id}`}
                className="flex min-h-[44px] items-center rounded-lg border border-slate-300 px-4 text-sm font-medium text-slate-800"
              >
                Open the pass
              </Link>
            ) : null}
          </>
        }
      />

      <ul className="flex flex-col gap-3">
        <ApprovalCard request={data} />
      </ul>

      <Card className="flex flex-col gap-2">
        {/* M3: who authorised what, and by what means. This is the record an
            auditor asks for, so it is a screen rather than a log file. */}
        <h2 className="text-sm font-semibold text-slate-900">Decision trail</h2>
        {data.actions.length === 0 ? (
          <p className="text-sm text-slate-500">No decision recorded yet.</p>
        ) : (
          <ol className="flex flex-col gap-2 text-sm">
            {data.actions.map((action) => (
              <li key={action.id} className="border-l-2 border-slate-200 pl-3">
                <p className="font-medium text-slate-900">
                  {decisionLine(action)}
                </p>
                <p className="text-slate-600">
                  {action.decided_at?.slice(0, 16).replace('T', ' ')}
                  {action.auth_method ? ` · ${action.auth_method.toLowerCase()}` : ''}
                </p>
                {action.reason ? <p className="text-slate-700">{action.reason}</p> : null}
              </li>
            ))}
          </ol>
        )}
      </Card>
    </div>
  );
}
