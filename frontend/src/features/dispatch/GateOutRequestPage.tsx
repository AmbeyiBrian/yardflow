/**
 * T4.21 — raising a gate-out request (design §7.3, §7.4; F1, F2, C7).
 *
 * The criterion is a stopwatch: "a technician raises a request from a phone in
 * **under a minute**." Three decisions follow from that and nothing else.
 *
 * *The destination is one choice, not four.* F1 allows a site, a project, a
 * client or a location, and the model enforces exactly one (§4.7). Four separate
 * pickers would mean reading all four to find the one that applies, so this asks
 * "where is it going?" once and then asks for the right thing.
 *
 * *Defaults do the work.* The requester is the custody holder, because a
 * technician taking material for their own job is the ordinary case — and it is
 * still a field, because a storekeeper raising it for someone else is the other
 * ordinary case (Q2).
 *
 * *Lines go in by scanning.* A serialized line is a specific unit (§4.7:
 * "these two RRUs", not "two RRUs"), which is what lets the gate check the load
 * against the pass (G1). Scanning is the fast path; searching is always there.
 */

import { drumOrLooseMessage, LOOSE_LENGTH_LABEL, lengthLabel, lineTrackingMode } from './looseLength';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';

import { api, ApiError } from '../../api/client';
import { errorMessage, useAction, useDetail, useList } from '../../api/hooks';
import { useSession } from '../../auth/session';
import { newUuid } from '../../offline/db';
import { BarcodeScanner } from '../../components/BarcodeScanner';
import {
  ActionBar,
  Banner,
  Button,
  Card,
  Checkbox,
  Field,
  Input,
  OwnershipBadge,
  Select,
  Textarea,
} from '../../components/ui';
import { ControlledReferenceSelect } from '../../components/ui/ReferenceSelect';
import { ItemPicker, type PickedItem } from '../../components/ItemPicker';
import { EmptyState, PageHeader, Sheet } from '../../components/ui/data';
import type { Reel, SerialUnit, StockBalance } from '../receiving/types';
import type { Client, ItemType, Location, Site, Project } from '../settings/types';
import {
  boxHeading,
  bulkQuantityProblem,
  exclusionText,
  groupLinesByBox,
  matchProblems,
  normaliseDraftLine,
  proposalSummary,
  proposalToLines,
  removeSerial,
  submitRefusal,
  trimQuantity,
  unitBoxText,
  type IssuableResult,
  type LookedUpBox,
} from './gateOutBoxes';
import {
  allTicked,
  divertErrors,
  earmarkedBulkProblem,
  earmarkedRows,
  earmarkedToLines,
  groupJobsByProject,
  jobMissing,
  jobRule,
  type EarmarkedJob,
  type EarmarkedResult,
} from './earmarkedGateOut';
import type { GateOut, GateOutLine, GateOutPurpose } from './types';

const DRAFT_KEY = 'yardflow.gate-out.draft';

const PURPOSES: { value: GateOutPurpose; label: string; hint: string }[] = [
  { value: 'INSTALLATION', label: 'Installation at a site', hint: '' },
  { value: 'MAINTENANCE', label: 'Maintenance', hint: '' },
  { value: 'TRANSFER', label: 'Transfer to another location', hint: '' },
  {
    value: 'RETURN_TO_CLIENT',
    label: 'Return to the client',
    hint: 'Leaves available stock but stays our exposure until they acknowledge it.',
  },
  { value: 'DISPOSAL', label: 'Disposal', hint: 'Always needs an approval.' },
  { value: 'LOAN', label: 'Loan', hint: 'Expected back, so it goes on somebody’s record.' },
];

type DestinationKind = 'site' | 'project' | 'client' | 'to_location';

interface Draft {
  purpose_type: GateOutPurpose;
  destination_kind: DestinationKind;
  destination_id: string;
  /**
   * O5: which job this material is for. An **attribution**, not a destination —
   * the pass still goes to exactly one place. This is what makes it project
   * material, and what routes it to that project's manager (O6).
   */
  job: string;
  from_location: string;
  custody_holder: string;
  notes: string;
  lines: GateOutLine[];
  /**
   * This request's identity, sent with it so a second press of the button
   * cannot become a second pass (N2, §8.2). The same reasoning as the gate-in
   * draft: disabling the control while a request is in flight leaves a window
   * the client cannot close, and the server settles it instead.
   */
  client_uuid: string;
}

function emptyDraft(holderId: string): Draft {
  return {
    purpose_type: 'INSTALLATION',
    destination_kind: 'site',
    destination_id: '',
    job: '',
    from_location: '',
    custody_holder: holderId,
    notes: '',
    lines: [],
    client_uuid: newUuid(),
  };
}

export default function GateOutRequestPage() {
  const navigate = useNavigate();
  const { user } = useSession();
  const holderId = user ? String(user.id) : '';
  // Present when an existing draft is being corrected. The same screen, because
  // a correction is the same work as the entry.
  const { id: editingId } = useParams();
  const existing = useDetail<GateOut>('gate-outs', editingId);
  const [loaded, setLoaded] = useState(false);

  const [draft, setDraft] = useState<Draft>(() => {
    if (editingId) return emptyDraft(holderId);
    try {
      const stored = localStorage.getItem(DRAFT_KEY);
      if (stored) {
        const parsed = JSON.parse(stored) as Draft;
        if (parsed?.lines && Array.isArray(parsed.lines)) {
          // A draft stored before this field existed still deserves one.
          // P6: a draft from before boxes has no box fields on its lines; they
          // are filled in rather than the draft being thrown away.
          const lines = parsed.lines.map(normaliseDraftLine);
          return parsed.client_uuid
            ? { ...parsed, lines }
            : { ...parsed, lines, client_uuid: newUuid() };
        }
      }
    } catch {
      /* a stored shape from an older build must not break the screen */
    }
    return emptyDraft(holderId);
  });
  const [lineSheet, setLineSheet] = useState(false);
  /** Which line is open for correction, or null when a new one is being added. */
  const [editingLine, setEditingLine] = useState<number | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  // P10: the server's refusal of a unit or a box claim, kept apart from the
  // generic banner so each unit it names can be taken off the request.
  const [refusal, setRefusal] = useState<{ message: string; problems: string[] } | null>(null);
  // Q3: the server's "this line needs a reason" by line index, and which lines
  // have had the reason field opened by hand.
  const [divertMessages, setDivertMessages] = useState<Record<number, string>>({});
  const [reasonOpen, setReasonOpen] = useState<Set<number>>(new Set());
  // Q6: the store-and-site the preload panel was last put away for.
  const [dismissedKey, setDismissedKey] = useState('');
  // A refused submit leaves the draft saved on the server. The next send must
  // update that one: creating again would hand back the old copy unchanged
  // (N2), and the reason just typed would never reach it.
  const savedId = useRef<number | null>(null);

  // Skipped while editing: that draft lives on the server, and writing it here
  // would overwrite an unsaved request the same person may have in progress.
  useEffect(() => {
    if (editingId) return;
    try {
      localStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
    } catch {
      /* private mode, or the quota is full */
    }
  }, [draft, editingId]);

  // Fill the form from the saved draft, once.
  useEffect(() => {
    if (!editingId || loaded || !existing.data) return;
    const document = existing.data;
    // Exactly one destination is set on the record; which one it is decides
    // what the picker shows.
    const kind: DestinationKind = document.site
      ? 'site'
      : document.project
        ? 'project'
        : document.client
          ? 'client'
          : 'to_location';
    const destinationId =
      document.site ?? document.project ?? document.client ?? document.to_location;

    setDraft({
      purpose_type: document.purpose_type,
      destination_kind: kind,
      destination_id: destinationId ? String(destinationId) : '',
      job: document.job ? String(document.job) : '',
      from_location: document.from_location ? String(document.from_location) : '',
      custody_holder: document.custody_holder ? String(document.custody_holder) : holderId,
      notes: document.notes ?? '',
      lines: (document.lines ?? []).map(normaliseDraftLine),
      client_uuid: document.client_uuid ?? newUuid(),
    });
    setLoaded(true);
  }, [editingId, loaded, existing.data, holderId]);

  const locations = useList<Location>('locations', { page_size: 200 });
  const sites = useList<Site>('sites', { page_size: 300 });
  const projects = useList<Project>('projects', { page_size: 200 });
  const clients = useList<Client>('clients', { page_size: 200 });
  const people = useList<{ id: number; full_name: string }>('users', { page_size: 200 });

  // O5: the jobs this pass could plausibly be for. Scoped to the destination
  // site, because a job elsewhere would attribute the material to a project it
  // never reached — which the server refuses anyway (§4.7).
  const jobsAtSite = useList<{
    id: number;
    reference: string;
    project: number | null;
    project_reference?: string;
    status: string;
  }>(
    'jobs',
    { site: draft.destination_id, status: 'OPEN', page_size: 100 },
    { enabled: draft.destination_kind === 'site' && Boolean(draft.destination_id) },
  ).data?.results ?? [];

  // Q6: what is earmarked for the chosen site at the chosen store, and the
  // site's open jobs. Fetched as soon as both are known.
  const earmarkedKey = `${draft.destination_id}:${draft.from_location}`;
  const preload = useQuery({
    queryKey: ['earmarked', draft.destination_id, draft.from_location],
    queryFn: () =>
      api.get<EarmarkedResult>(
        `/stock/earmarked?site=${encodeURIComponent(draft.destination_id)}&from_location=${encodeURIComponent(draft.from_location)}`,
      ),
    enabled:
      draft.destination_kind === 'site' &&
      Boolean(draft.destination_id) &&
      Boolean(draft.from_location),
    staleTime: 0,
    gcTime: 0,
  });
  // The earmarked answer names jobs with their project; until it arrives (or
  // while no store is chosen) the plain job list stands in.
  const siteJobs: EarmarkedJob[] =
    draft.destination_kind !== 'site' || !draft.destination_id
      ? []
      : (preload.data?.jobs ??
        jobsAtSite.map((job) => ({
          id: job.id,
          reference: job.reference,
          project: job.project,
          project_name: job.project_reference ?? '',
        })));
  const rule = jobRule(siteJobs);
  const siteName = (sites.data?.results ?? []).find(
    (site) => String(site.id) === draft.destination_id,
  )?.name;

  // Exactly one job at the site: it is chosen for them.
  // Derived, not stored: the one job is the job unless something else is chosen.
  const jobId = draft.job || (rule.kind === 'single' ? String(rule.job.id) : '');

  const selectedJob = siteJobs.find((job) => String(job.id) === jobId);
  const selectedProject = projects.data?.results.find(
    (project) => project.id === selectedJob?.project,
  );
  // A project with a PO but no manager is one nobody can approve material for.
  const blockedProject =
    selectedProject && selectedProject.po_number && !selectedProject.manager
      ? selectedProject.po_number
      : '';

  const create = useAction<Record<string, unknown>, { id: number }>({
    resource: 'gate-outs',
  });
  const update = useAction<Record<string, unknown>, { id: number }>({
    resource: 'gate-outs',
    method: 'patch',
    path: () => String(editingId),
    invalidates: ['gate-outs'],
  });
  const submit = useAction<{ id: number }, { id: number; number: string; status: string }>({
    resource: 'gate-outs',
    path: (body) => `${body.id}/submit`,
    invalidates: ['gate-outs', 'approvals'],
  });

  const yards = useMemo(
    () => (locations.data?.results ?? []).filter((row) => row.type !== 'QUARANTINE'),
    [locations.data],
  );

  function set(patch: Partial<Draft>) {
    setDraft((current) => ({ ...current, ...patch }));
  }

  /** "Client owned" without the client is half a sentence. */
  function clientName(id: number): string {
    return (clients.data?.results ?? []).find((row) => row.id === id)?.name ?? 'A client';
  }

  function clearDraft() {
    if (editingId) return;
    savedId.current = null;
    setDivertMessages({});
    setReasonOpen(new Set());
    setDraft(emptyDraft(holderId));
    try {
      localStorage.removeItem(DRAFT_KEY);
    } catch {
      /* nothing to clear */
    }
  }

  function payload() {
    return {
      purpose_type: draft.purpose_type,
      // Exactly one destination — the model refuses more, and refuses none
      // (§4.7). Sending only the chosen one is what makes that easy to satisfy.
      site: draft.destination_kind === 'site' ? Number(draft.destination_id) : null,
      project:
        draft.destination_kind === 'project' ? Number(draft.destination_id) : null,
      client: draft.destination_kind === 'client' ? Number(draft.destination_id) : null,
      to_location:
        draft.destination_kind === 'to_location' ? Number(draft.destination_id) : null,
      job: jobId ? Number(jobId) : null,
      from_location: Number(draft.from_location),
      custody_holder: Number(draft.custody_holder),
      notes: draft.notes,
      client_uuid: draft.client_uuid,
      lines: draft.lines,
    };
  }

  async function saveThenSubmit(alsoSubmit: boolean) {
    setBanner(null);
    setRefusal(null);
    setDivertMessages({});
    try {
      const existingId = editingId ?? savedId.current;
      const created = existingId
        ? editingId
          ? await update.mutateAsync(payload())
          : await api.patch<{ id: number }>(`/gate-outs/${existingId}`, payload())
        : await create.mutateAsync(payload());
      if (!editingId) savedId.current = created.id;
      if (alsoSubmit) {
        const result = await submit.mutateAsync({ id: created.id });
        clearDraft();
        navigate(`/gate-out/${result.id}`);
        return;
      }
      clearDraft();
      navigate(`/gate-out/${created.id}`);
    } catch (error) {
      const refused = error instanceof ApiError ? submitRefusal(error) : null;
      const diverted = error instanceof ApiError ? divertErrors(error.fieldErrors) : {};
      if (refused) setRefusal(refused);
      else if (Object.keys(diverted).length > 0) {
        // Q3: say so on the line itself, where the reason is typed.
        setDivertMessages(diverted);
        setBanner(
          'Some of this is going from material kept for another site. Say why, on each line marked below, then send again.',
        );
      } else setBanner(errorMessage(error));
    }
  }

  function setLineReason(index: number, reason: string) {
    setDraft((current) => ({
      ...current,
      lines: current.lines.map((line, position) =>
        position === index ? { ...line, divert_reason: reason } : line,
      ),
    }));
  }

  function removeLine(index: number) {
    // Positions shift, so what was said about a position no longer holds.
    setDivertMessages({});
    setReasonOpen(new Set());
    setDraft((current) => ({
      ...current,
      lines: current.lines.filter((_line, position) => position !== index),
    }));
  }

  /** P9: a bulk box line may take part of what the box holds. */
  function setLineQuantity(index: number, quantity: string) {
    setDraft((current) => ({
      ...current,
      lines: current.lines.map((line, position) =>
        position === index ? { ...line, requested_qty: quantity } : line,
      ),
    }));
  }

  const problems = refusal ? matchProblems(refusal.problems, draft.lines) : [];
  const lineGroups = groupLinesByBox(draft.lines);

  const destinations: { kind: DestinationKind; label: string; options: { id: number; label: string }[] }[] = [
    {
      kind: 'site',
      label: 'A site',
      options: (sites.data?.results ?? []).map((site) => ({
        id: site.id,
        label: `${site.internal_ref} · ${site.name}`,
      })),
    },
    // Q6: a project is no longer something a new request is addressed to — it
    // is worked out from the site's job. A draft or pass that already has one
    // still shows it (read-only, below); it is simply not offered otherwise.
    ...(draft.destination_kind === 'project'
      ? [
          {
            kind: 'project' as const,
            label: 'A project',
            options: (projects.data?.results ?? []).map((project) => ({
              id: project.id,
              label: project.reference,
            })),
          },
        ]
      : []),
    {
      kind: 'client',
      label: 'Back to a client',
      options: (clients.data?.results ?? []).map((client) => ({
        id: client.id,
        label: client.name,
      })),
    },
    {
      kind: 'to_location',
      label: 'Another of our locations',
      options: yards.map((location) => ({ id: location.id, label: location.name })),
    },
  ];

  const chosen = destinations.find((entry) => entry.kind === draft.destination_kind)!;
  const purpose = PURPOSES.find((entry) => entry.value === draft.purpose_type);
  const canSubmit =
    Boolean(draft.from_location) &&
    Boolean(draft.destination_id) &&
    Boolean(draft.custody_holder) &&
    !jobMissing(rule, jobId) &&
    draft.lines.length > 0;

  return (
    <div className="flex flex-col gap-4 pb-24">
      <PageHeader
        title={editingId ? 'Correct this request' : 'Request material'}
        subtitle={
          editingId
            ? 'It is still a draft, so it authorises nothing yet.'
            : 'What is leaving the yard, where it is going, and who is taking it.'
        }
        actions={
          // "Discard" throws away what is on this device, which a saved draft
          // is not — offering it while editing was a button that did nothing.
          editingId ? (
            <Button variant="ghost" onClick={() => navigate(`/gate-out/${editingId}`)}>
              Cancel
            </Button>
          ) : draft.lines.length > 0 ? (
            <Button variant="ghost" onClick={clearDraft}>
              Discard
            </Button>
          ) : undefined
        }
      />

      {banner ? <Banner tone="error">{banner}</Banner> : null}

      {refusal ? (
        <Banner tone="error">
          <div className="flex flex-col gap-2">
            <p>{refusal.message}</p>
            {problems.length > 0 ? (
              <ul className="flex flex-col gap-2">
                {problems.map((problem) => (
                  <li
                    key={problem.message}
                    className="flex flex-wrap items-center justify-between gap-2"
                  >
                    <span>{problem.message}</span>
                    {problem.serial_unit !== null && problem.lineIndex !== null ? (
                      <Button
                        variant="secondary"
                        onClick={() => {
                          const unit = problem.serial_unit as number;
                          const at = problem.lineIndex as number;
                          setDraft((current) => ({
                            ...current,
                            lines: removeSerial(current.lines, at, unit),
                          }));
                          setRefusal(null);
                        }}
                      >
                        Remove {problem.serial_number}
                      </Button>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : (
              <p>Lower the quantity on the box line below, or remove it.</p>
            )}
          </div>
        </Banner>
      ) : null}

      <Card className="flex flex-col gap-3">
        <Field label="What for" htmlFor="go-purpose" hint={purpose?.hint}>
          <Select
            id="go-purpose"
            value={draft.purpose_type}
            onChange={(event) => set({ purpose_type: event.target.value as GateOutPurpose })}
          >
            {PURPOSES.map((entry) => (
              <option key={entry.value} value={entry.value}>
                {entry.label}
              </option>
            ))}
          </Select>
        </Field>

        <Field
          label="Where it is going"
          htmlFor="go-destination-kind"
          hint={
            draft.destination_kind === 'project'
              ? 'This request was addressed to a project, and stays that way. Choose something else to change it.'
              : 'A site brings what is waiting for it. The project follows from the site’s job.'
          }
        >
          <Select
            id="go-destination-kind"
            value={draft.destination_kind}
            onChange={(event) =>
              set({
                destination_kind: event.target.value as DestinationKind,
                destination_id: '',
                job: '',
              })
            }
          >
            {destinations.map((entry) => (
              <option key={entry.kind} value={entry.kind}>
                {entry.label}
              </option>
            ))}
          </Select>
        </Field>

        <Field label={chosen.label} htmlFor="go-destination">
          <Select
            id="go-destination"
            value={draft.destination_id}
            disabled={draft.destination_kind === 'project'}
            onChange={(event) => set({ destination_id: event.target.value, job: '' })}
          >
            <option value="">Choose…</option>
            {chosen.options.map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </Select>
        </Field>

        {/*
          O5: naming the job is what makes this project material, and what sends
          it to that project's manager instead of through the criticality rules.
          Only offered for a site destination, because that is the case the job
          resolves — a pass addressed to a project already says which.
        */}
        {draft.destination_kind === 'site' && draft.destination_id ? (
          rule.kind === 'single' ? (
            <p className="text-sm text-slate-700" data-testid="go-job-single">
              For job {rule.job.reference || `Job ${rule.job.id}`}
              {rule.job.project_name ? ` · ${rule.job.project_name}` : ''}, the only open job at{' '}
              {siteName ?? 'this site'}.
            </p>
          ) : rule.kind === 'none' ? null : (
            <Field
              label={rule.kind === 'required' ? 'Which job' : 'For which job'}
              htmlFor="go-job"
              hint={
                rule.kind === 'required'
                  ? 'This site has open jobs in more than one project. Choose the one this is for.'
                  : 'Optional. Naming it is what costs the material to a project.'
              }
            >
              <Select
                id="go-job"
                value={draft.job}
                onChange={(event) => set({ job: event.target.value })}
              >
                <option value="">
                  {rule.kind === 'required' ? 'Choose…' : 'Not for a particular job'}
                </option>
                {groupJobsByProject(siteJobs).map((group) => (
                  <optgroup key={group.project ?? 'none'} label={group.label}>
                    {group.jobs.map((job) => (
                      <option key={job.id} value={job.id}>
                        {job.reference || `Job ${job.id}`}
                        {job.description ? ` · ${job.description}` : ''}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </Select>
            </Field>
          )
        ) : null}

        {/* Q6: what is already waiting for this site at this store. */}
        {!editingId &&
        draft.destination_kind === 'site' &&
        draft.destination_id &&
        draft.from_location &&
        preload.data &&
        dismissedKey !== earmarkedKey ? (
          <EarmarkedPanel
            key={earmarkedKey}
            data={preload.data}
            siteName={siteName ?? 'the site'}
            storeName={
              (locations.data?.results ?? []).find(
                (row) => String(row.id) === draft.from_location,
              )?.name ?? 'that store'
            }
            existing={draft.lines}
            onAdd={(lines) => {
              setDraft((current) => ({ ...current, lines: [...current.lines, ...lines] }));
              setDismissedKey(earmarkedKey);
            }}
            onDismiss={() => setDismissedKey(earmarkedKey)}
          />
        ) : null}

        {/*
          D28: a project whose manager cannot act is stopped, and the failure
          mode to avoid is a request that merely looks slow. So it is said here,
          before the pass is raised, in the words the remedy needs.
        */}
        {blockedProject ? (
          <Banner tone="error">
            {blockedProject} has no active manager, so material cannot leave for
            it. An owner has to assign a new manager before this pass can be
            approved.
          </Banner>
        ) : null}

        <Field label="Out of" htmlFor="go-from">
          <ControlledReferenceSelect resource="locations"
            id="go-from"
            value={draft.from_location}
            onChange={(event) => set({ from_location: event.target.value })}
          >
            <option value="">Choose…</option>
            {yards.map((location) => (
              <option key={location.id} value={location.id}>
                {location.name}
              </option>
            ))}
          </ControlledReferenceSelect>
        </Field>

        <Field
          label="Who is taking it"
          htmlFor="go-holder"
          hint="It goes on their custody record when it leaves the gate."
        >
          <ControlledReferenceSelect resource="users"
            id="go-holder"
            value={draft.custody_holder}
            onChange={(event) => set({ custody_holder: event.target.value })}
          >
            <option value="">Choose…</option>
            {(people.data?.results ?? []).map((person) => (
              <option key={person.id} value={person.id}>
                {person.full_name}
              </option>
            ))}
          </ControlledReferenceSelect>
        </Field>
      </Card>

      <Card className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-semibold text-slate-900">
            What is going {draft.lines.length ? `(${draft.lines.length})` : ''}
          </h2>
          <Button onClick={() => setLineSheet(true)}>Add</Button>
        </div>

        {draft.lines.length === 0 ? (
          <EmptyState
            title="Nothing on the request yet."
            hint="Scan a serial or a drum, or search the catalogue."
          />
        ) : (
          // §4.15.7: lines from one box sit under that box's heading, loose
          // lines after them, so a carton reads as one thing on the request.
          <div className="flex flex-col gap-3">
            {lineGroups.map((group) => (
              <section key={group.key} className="flex flex-col gap-2">
                {group.heading ? (
                  <h3 className="text-xs font-semibold tracking-wide text-slate-600 uppercase">
                    {group.heading}
                  </h3>
                ) : lineGroups.length > 1 ? (
                  <h3 className="text-xs font-semibold tracking-wide text-slate-600 uppercase">
                    Loose
                  </h3>
                ) : null}
                <ul className="flex flex-col gap-2">
                  {group.lines.map(({ line, index }) => {
                    const fromBox = Boolean(boxHeading(line));
                    const bulkFromBox = fromBox && line.tracking_mode !== 'SERIALIZED';
                    return (
                      <li
                        key={`${line.item_type}-${index}`}
                        className="flex flex-wrap items-start justify-between gap-3 rounded-lg border border-slate-200 p-3 text-sm"
                      >
                        <div className="min-w-0 break-words">
                          <p className="font-medium text-slate-900">
                            {line.requested_qty} {line.uom} · {line.item_name}
                          </p>
                          <p className="text-slate-600">
                            {line.serials?.length
                              ? line.serials.map((serial) => serial.serial_number).join(', ')
                              : line.reels?.length
                                ? line.reels
                                    .map((reel) => `${reel.drum_number} (${reel.length_requested})`)
                                    .join(', ')
                                : 'bulk'}
                            {' · '}
                            {/* Which lot this takes. Two lines that look identical can
                                be asking for two different piles of the same item, and
                                only one of them may exist. */}
                            {line.owner_client
                              ? `${clientName(line.owner_client)}’s stock`
                              : 'your own stock'}
                          </p>
                          {bulkFromBox ? (
                            // P9: part of a box's bulk can go. The server checks the
                            // box's claim at submit; this is the edit.
                            <Field label="How much of it" htmlFor={`go-line-qty-${index}`}>
                              <Input
                                id={`go-line-qty-${index}`}
                                inputMode="decimal"
                                value={line.requested_qty}
                                onChange={(event) => setLineQuantity(index, event.target.value)}
                              />
                            </Field>
                          ) : null}
                          {line.is_returnable ? (
                            <p className="text-amber-800">
                              Expected back
                              {line.expected_return_date ? ` by ${line.expected_return_date}` : ''}.
                            </p>
                          ) : null}
                          {/* Q3: the server says which lines would use another
                              site's earmarked material; the reason is typed here. */}
                          {divertMessages[index] ? (
                            <p className="mt-1 rounded-md bg-amber-50 p-2 text-amber-900" role="alert">
                              {divertMessages[index]}
                            </p>
                          ) : null}
                          {divertMessages[index] || line.divert_reason || reasonOpen.has(index) ? (
                            <Field
                              label="Why is it going here instead?"
                              htmlFor={`go-line-reason-${index}`}
                            >
                              <Input
                                id={`go-line-reason-${index}`}
                                value={line.divert_reason ?? ''}
                                onChange={(event) => setLineReason(index, event.target.value)}
                              />
                            </Field>
                          ) : null}
                        </div>
                        <div className="flex shrink-0 items-center gap-1">
                          {fromBox || line.serials?.length || line.reels?.length ? (
                            // A line that names units or drums keeps them; the
                            // line sheet would rebuild it. The reason can still
                            // be given (Q3).
                            <Button
                              variant="ghost"
                              className="min-h-0 px-2 py-1 text-sm"
                              onClick={() =>
                                setReasonOpen((current) => new Set(current).add(index))
                              }
                            >
                              Reason
                            </Button>
                          ) : (
                            <Button
                              variant="ghost"
                              className="min-h-0 px-2 py-1 text-sm"
                              onClick={() => setEditingLine(index)}
                            >
                              Change
                            </Button>
                          )}
                          <Button
                            variant="ghost"
                            className="min-h-0 px-2 py-1 text-sm text-red-700"
                            onClick={() => removeLine(index)}
                          >
                            Remove
                          </Button>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              </section>
            ))}
          </div>
        )}
      </Card>

      <Card>
        <Field label="Notes" htmlFor="go-notes">
          <Textarea
            id="go-notes"
            value={draft.notes}
            onChange={(event) => set({ notes: event.target.value })}
          />
        </Field>
      </Card>

      {/* `ActionBar`, not a hand-rolled fixed bar. A `fixed bottom-0` div sits
          *under* the shell's phone tab bar, which is also fixed at the bottom —
          so on the device this screen was designed for, its primary button was
          covered and unreachable. Found by running T8.13 on a phone viewport.
          `ActionBar` is sticky, so it stacks above the content and below the
          tabs. */}
      <ActionBar>
        <Button
          variant="secondary"
          block
          disabled={!canSubmit}
          loading={create.isPending && !submit.isPending}
          onClick={() => saveThenSubmit(false)}
        >
          Save as draft
        </Button>
        <Button
          block
          disabled={!canSubmit}
          loading={submit.isPending}
          onClick={() => saveThenSubmit(true)}
        >
          Send for approval
        </Button>
      </ActionBar>

      <LineSheet
        open={lineSheet || editingLine !== null}
        initial={editingLine !== null ? draft.lines[editingLine] : undefined}
        onClose={() => {
          setLineSheet(false);
          setEditingLine(null);
        }}
        fromLocation={draft.from_location}
        fromLocationName={
          (locations.data?.results ?? []).find(
            (row) => String(row.id) === draft.from_location,
          )?.name ?? 'that location'
        }
        fromNodeId={
          (locations.data?.results ?? []).find(
            (row) => String(row.id) === draft.from_location,
          )?.node_id ?? null
        }
        // P6, §4.15.7: a scanned box adds all its proposed lines at once.
        onAddMany={(lines) => {
          setDraft((current) => ({ ...current, lines: [...current.lines, ...lines] }));
          setLineSheet(false);
          setEditingLine(null);
        }}
        onAdd={(line) => {
          setDraft((current) => ({
            ...current,
            lines:
              editingLine === null
                ? [...current.lines, line]
                : current.lines.map((existing, position) =>
                    position === editingLine ? line : existing,
                  ),
          }));
          setLineSheet(false);
          setEditingLine(null);
        }}
      />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* One line                                                                   */
/* -------------------------------------------------------------------------- */

/** One lot on the shelf: whose it is, and in what condition. */
function keyOf(row: StockBalance): string {
  return `${row.owner_client ?? 'own'}:${row.condition}`;
}

/** A number a person would say out loud: 5, not 5.000. */
function trim(quantity: string): string {
  const text = String(quantity);
  return text.includes('.') ? text.replace(/0+$/, '').replace(/\.$/, '') : text;
}

function describe(row: StockBalance): string {
  const owner = row.owner_client_name ? `${row.owner_client_name}’s` : 'your own';
  return `${owner} stock (${row.condition.replaceAll('_', ' ').toLowerCase()})`;
}

function LineSheet({
  open,
  initial,
  onClose,
  onAdd,
  onAddMany,
  fromLocation,
  fromLocationName,
  fromNodeId,
}: {
  open: boolean;
  /** A line being corrected. Absent when a new one is being added. */
  initial?: GateOutLine;
  onClose: () => void;
  onAdd: (line: GateOutLine) => void;
  onAddMany: (lines: GateOutLine[]) => void;
  fromLocation: string;
  fromLocationName: string;
  fromNodeId: number | null;
}) {
  const [itemId, setItemId] = useState('');
  const [quantity, setQuantity] = useState('');
  const [returnable, setReturnable] = useState(false);
  const [returnDate, setReturnDate] = useState('');
  const [scanned, setScanned] = useState<{
    // `box_path` is read if the lookup ever sends it; today's serial-unit
    // serializer does not, and then nothing is shown (P6).
    unit?: SerialUnit & { box_path?: string[] | null };
    reel?: Reel;
  } | null>(null);
  // P5, P6: what a scanned box (or pallet) would send from the chosen location.
  const [proposal, setProposal] = useState<{
    box: LookedUpBox;
    result: IssuableResult;
    /** Bulk quantities as typed, by position in `result.lines` (P9). */
    quantities: Record<number, string>;
  } | null>(null);
  // D3: a serialized item can only go out as a quantity if the yard holds
  // untagged units, and then the reason is part of the record. The server
  // refuses the line without it, so the reason is asked for here rather than
  // left as a 400 nobody can act on.
  const [noSerialReason, setNoSerialReason] = useState('');
  /** Cable taken as loose length rather than from a named drum (D10). */
  const [looseLength, setLooseLength] = useState(false);
  // Q3: given up front when the line is known to use another site's material.
  const [divertReason, setDivertReason] = useState('');
  const [error, setError] = useState<string | null>(null);

  void fromLocation;

  // The item is what the picker handed back; when a scan or an edited line set
  // the id instead, fetch it (ItemPicker shares the same cached detail).
  const [picked, setPicked] = useState<PickedItem | null>(null);
  const itemDetail = useDetail<ItemType>('item-types', itemId || undefined, { retry: false });
  const item: Pick<ItemType, 'id' | 'name' | 'uom' | 'default_tracking_mode' | 'is_returnable'> | undefined =
    picked && String(picked.id) === itemId
      ? picked
      : itemDetail.data && String(itemDetail.data.id) === itemId
        ? itemDetail.data
        : undefined;

  /**
   * What this location actually holds of this item, by owner and condition.
   *
   * From a real refusal. A technician asked for three vests, the stock screen
   * showed five at the yard, and "Send for approval" said some lines ask for
   * more than is in stock. Both were right: the five belonged to Safaricom and
   * the line asked for the company's own, because the line had no way to say
   * whose stock it wanted. The choice was never offered, so it was always
   * "ours" — and the mismatch only surfaced at the end, to the approver's
   * screen rather than the requester's.
   */
  const holdings = useList<StockBalance>(
    'stock',
    { item_type: itemId || undefined, node: fromNodeId ?? undefined },
    { enabled: Boolean(itemId && fromNodeId) },
  );
  const available = (holdings.data?.results ?? []).filter(
    (row) => Number(row.quantity) > 0,
  );
  const [holdingKey, setHoldingKey] = useState('');
  // A key seeded from a line whose lot has since gone (or never existed at this
  // location) must not quietly fall back to "ours" — it has to read as unset.
  const chosenKey = available.some((row) => keyOf(row) === holdingKey) ? holdingKey : '';
  const holding =
    available.find((row) => keyOf(row) === chosenKey) ??
    (available.length === 1 ? available[0] : undefined);

  function reset() {
    setItemId('');
    setQuantity('');
    setReturnable(false);
    setReturnDate('');
    setScanned(null);
    setProposal(null);
    setNoSerialReason('');
    setLooseLength(false);
    setDivertReason('');
    setHoldingKey('');
    setError(null);
  }

  /** D7, G1: a scanned identifier names the exact unit that is going. */
  async function lookup(value: string) {
    setError(null);
    setProposal(null);
    try {
      const found = await api.get<{ kind: string; object: SerialUnit | Reel | LookedUpBox }>(
        `/stock/lookup?q=${encodeURIComponent(value)}`,
      );
      if (found.kind === 'box') {
        // P5: a carton or pallet goes out as the units and bulk it holds, so the
        // server expands it — but only from a place, since "what is in it" is
        // only issuable where it is.
        const box = found.object as LookedUpBox;
        if (!fromLocation) {
          setError('Choose where it is going out from first.');
          return;
        }
        try {
          const result = await api.get<IssuableResult>(
            `/stock/boxes/${encodeURIComponent(box.code)}/issuable?from_location=${encodeURIComponent(fromLocation)}`,
          );
          setScanned(null);
          setItemId('');
          setProposal({ box, result, quantities: {} });
        } catch (failure) {
          setError(errorMessage(failure));
        }
        return;
      }
      if (found.kind === 'serial_unit') {
        const unit = found.object as SerialUnit;
        setScanned({ unit });
        setItemId(String(unit.item_type));
        setQuantity('1');
      } else {
        const reel = found.object as Reel;
        setScanned({ reel });
        setItemId(String(reel.item_type));
      }
    } catch {
      setError(`Nothing here matches "${value}". It may be another organization's.`);
    }
  }

  function add() {
    setError(null);
    if (!item) {
      setError('Choose an item, or scan one.');
      return;
    }
    if (!quantity || Number(quantity) <= 0) {
      setError('How much is going?');
      return;
    }

    // Refuse here, where it can be fixed, rather than at "Send for approval"
    // where the only thing left to do is go back and start again.
    if (!scanned && fromNodeId && !holdings.isLoading) {
      if (available.length === 0) {
        setError(`${fromLocationName} holds no ${item.name}.`);
        return;
      }
      if (available.length > 1 && !holding) {
        setError(`${fromLocationName} holds more than one lot of ${item.name}. Choose which one is going.`);
        return;
      }
      if (holding && Number(quantity) > Number(holding.quantity)) {
        setError(
          `${fromLocationName} holds ${trim(holding.quantity)} ${holding.uom} of ` +
            `${describe(holding)}. Ask for that much or less.`,
        );
        return;
      }
    }

    // F1: a serialized item is requested by identity. Without a scan there is
    // nothing to identify, so the line either names a unit or explains why it
    // cannot — the same rule the server enforces.
    const serializedWithoutAScan =
      !scanned?.unit && item.default_tracking_mode === 'SERIALIZED';
    if (serializedWithoutAScan && !noSerialReason.trim()) {
      setError(
        `${item.name} is tracked by serial number. Scan the unit that is going, ` +
          `or say why there is no serial.`,
      );
      return;
    }

    // D10: cable is either a named drum or loose length, never neither.
    if (!scanned && item.default_tracking_mode === 'REEL' && !looseLength) {
      setError(drumOrLooseMessage(item.name));
      return;
    }

    const line: GateOutLine = {
      item_type: item.id,
      item_name: item.name,
      tracking_mode: lineTrackingMode({
        itemMode: item.default_tracking_mode,
        scanned: scanned?.unit ? 'unit' : scanned?.reel ? 'reel' : null,
        looseLength,
        // Untagged units are a quantity, with the reason recorded (D3).
        untagged: serializedWithoutAScan,
      }),
      no_serial_reason: serializedWithoutAScan ? noSerialReason.trim() : '',
      divert_reason: divertReason.trim(),
      requested_qty: quantity,
      uom: item.uom,
      // Whose stock, and in what condition — taken from the lot that was
      // chosen, so the request matches something that is really there.
      owner_type: holding?.owner_client ? 'CLIENT' : 'OWN',
      owner_client: holding?.owner_client ?? null,
      condition: holding?.condition ?? 'NEW',
      // C3: a returnable item defaults to returnable, and the date comes from
      // the item type — so nobody has to remember which tools come back.
      is_returnable: returnable || item.is_returnable,
      expected_return_date: returnDate || null,
      serials: scanned?.unit
        ? [{ serial_unit: scanned.unit.id, serial_number: scanned.unit.serial_number }]
        : [],
      reels: scanned?.reel
        ? [
            {
              reel: scanned.reel.id,
              drum_number: scanned.reel.drum_number,
              length_requested: quantity,
            },
          ]
        : [],
    };

    onAdd(line);
    reset();
  }

  /** P6: every proposed line goes onto the draft at once, units named. */
  function addProposal() {
    if (!proposal) return;
    for (const [index, line] of proposal.result.lines.entries()) {
      if (line.tracking_mode === 'SERIALIZED') continue;
      // P9: the quantity may be lowered to take part of the box, never raised.
      const problem = bulkQuantityProblem(
        proposal.quantities[index] ?? trimQuantity(line.requested_qty),
        line.requested_qty,
        line.uom,
      );
      if (problem) {
        setError(`${line.item_name}: ${problem}`);
        return;
      }
    }
    onAddMany(proposalToLines(proposal.result.lines, proposal.quantities));
    reset();
  }

  useEffect(() => {
    if (item?.is_returnable) setReturnable(true);
  }, [item]);

  // Fill the controls from the line being corrected. Keyed on the sheet
  // opening, so typing into it afterwards is not overwritten.
  useEffect(() => {
    if (!open) return;
    if (!initial) return;
    setItemId(String(initial.item_type));
    setQuantity(String(initial.requested_qty));
    setReturnable(Boolean(initial.is_returnable));
    setReturnDate(initial.expected_return_date ?? '');
    setNoSerialReason(initial.no_serial_reason ?? '');
    setDivertReason(initial.divert_reason ?? '');
    setHoldingKey(`${initial.owner_client ?? 'own'}:${initial.condition ?? 'NEW'}`);
  }, [open, initial]);

  return (
    <Sheet
      open={open}
      title={initial ? 'Change this line' : 'Add to the request'}
      onClose={() => {
        reset();
        onClose();
      }}
      footer={
        <>
          <Button
            variant="secondary"
            block
            onClick={() => {
              reset();
              onClose();
            }}
          >
            Cancel
          </Button>
          {proposal ? (
            proposal.result.lines.length > 0 ? (
              <Button block onClick={addProposal}>
                Add all
              </Button>
            ) : null
          ) : (
            <Button block onClick={add}>
              {initial ? 'Save the line' : 'Add'}
            </Button>
          )}
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {error ? <Banner tone="error">{error}</Banner> : null}

        <BarcodeScanner
          label="Scan a serial or a drum"
          hint="Fastest route. Serialized material goes out by identity, not by count."
          onScan={lookup}
        />

        {scanned?.unit ? (
          <Banner tone="info">
            <span className="flex flex-wrap items-center gap-2">
              {scanned.unit.serial_number} · {scanned.unit.item_name} · at{' '}
              {scanned.unit.node_label}
              <OwnershipBadge client={scanned.unit.owner_client_name || null} />
              {/* P6: a unit taken out of a box goes alone, with quantity 1 and
                  no box on its line — but the person should see where it sat. */}
              {unitBoxText(scanned.unit) ? <span>{unitBoxText(scanned.unit)}</span> : null}
            </span>
          </Banner>
        ) : null}

        {scanned?.reel ? (
          <Banner tone="info">
            {scanned.reel.drum_number} · {scanned.reel.remaining_length}{' '}
            {scanned.reel.uom} left · at {scanned.reel.node_label}
          </Banner>
        ) : null}

        {proposal ? (
          <BoxProposalPanel
            proposal={proposal}
            onQuantity={(index, value) =>
              setProposal((current) =>
                current
                  ? { ...current, quantities: { ...current.quantities, [index]: value } }
                  : current,
              )
            }
          />
        ) : (
          <>
        {!scanned ? (
          <Field label="Or choose an item" htmlFor="gol-item">
            <ItemPicker
              id="gol-item"
              value={itemId}
              onChange={(next) => {
                setPicked(next);
                setItemId(next ? String(next.id) : '');
              }}
            />
          </Field>
        ) : null}

        {!scanned && item && fromNodeId ? (
          holdings.isLoading ? (
            <p className="text-sm text-slate-500">
              Checking what {fromLocationName} holds…
            </p>
          ) : available.length === 0 ? (
            <Banner tone="warning">
              {fromLocationName} holds no {item.name}. Nothing can go out of it
              that is not there.
            </Banner>
          ) : available.length === 1 ? (
            <p className="text-sm text-slate-600">
              {fromLocationName} holds {trim(available[0].quantity)}{' '}
              {available[0].uom} of {describe(available[0])}. That is what this
              line takes.
            </p>
          ) : (
            <Field
              label="Whose stock is going"
              htmlFor="gol-holding"
              hint="The same item can sit in the yard under more than one owner or condition. They are counted separately, and a pass has to name one."
            >
              <Select
                id="gol-holding"
                value={chosenKey}
                onChange={(event) => setHoldingKey(event.target.value)}
              >
                <option value="">Choose…</option>
                {available.map((row) => (
                  <option key={keyOf(row)} value={keyOf(row)}>
                    {trim(row.quantity)} {row.uom} · {describe(row)}
                  </option>
                ))}
              </Select>
            </Field>
          )
        ) : null}

        {!scanned?.unit && item?.default_tracking_mode === 'SERIALIZED' ? (
          <Field
            label="No serial available — why?"
            htmlFor="gol-no-serial"
            hint="This item is normally tracked by serial number. Scanning it above is the better route; this is for units the yard holds untagged."
          >
            <Input
              id="gol-no-serial"
              value={noSerialReason}
              onChange={(event) => setNoSerialReason(event.target.value)}
            />
          </Field>
        ) : null}

        {!scanned && item?.default_tracking_mode === 'REEL' ? (
          <Button
            variant={looseLength ? 'primary' : 'secondary'}
            block
            role="switch"
            aria-checked={looseLength}
            onClick={() => setLooseLength((current) => !current)}
          >
            {LOOSE_LENGTH_LABEL}
          </Button>
        ) : null}

        <Field
          label={
            looseLength && !scanned && item?.default_tracking_mode === 'REEL'
              ? lengthLabel(item.uom)
              : `How much${item ? ` (${item.uom})` : ''}`
          }
          htmlFor="gol-quantity"
          hint={
            looseLength && !scanned
              ? 'Only what is not on a drum. Metres on a drum go out by scanning the drum.'
              : scanned?.reel
              ? 'Less than the drum holds is a cut; all of it means the drum goes with them.'
              : undefined
          }
        >
          <Input
            id="gol-quantity"
            inputMode="decimal"
            value={quantity}
            disabled={Boolean(scanned?.unit)}
            onChange={(event) => setQuantity(event.target.value)}
          />
        </Field>

        <Field
          label="Why is it going here instead?"
          htmlFor="gol-divert"
          hint="Only if this takes material kept for another site. The request will ask if it needs to."
        >
          <Input
            id="gol-divert"
            value={divertReason}
            onChange={(event) => setDivertReason(event.target.value)}
          />
        </Field>

        {returnable ? (
          <Field
            label="Expected back"
            htmlFor="gol-return"
            hint="It stays on their record until it comes back, and gets chased if it does not."
          >
            <Input
              id="gol-return"
              type="date"
              value={returnDate}
              onChange={(event) => setReturnDate(event.target.value)}
            />
          </Field>
        ) : null}
          </>
        )}
      </div>
    </Sheet>
  );
}

/* -------------------------------------------------------------------------- */
/* A scanned box                                                              */
/* -------------------------------------------------------------------------- */

/**
 * What a scanned box or pallet would send, and what it would not (P5, P6, P10).
 *
 * The exclusions are listed with the server's own words, because "why is RRU-3
 * not in the list" is the first thing a storekeeper asks and the answer (it is
 * on another open pass) is something only they can act on.
 */
function BoxProposalPanel({
  proposal,
  onQuantity,
}: {
  proposal: { box: LookedUpBox; result: IssuableResult; quantities: Record<number, string> };
  onQuantity: (index: number, value: string) => void;
}) {
  const { box, result, quantities } = proposal;
  return (
    <div className="flex flex-col gap-3">
      <Banner tone="info">{proposalSummary(box, result)}</Banner>

      {result.lines.length > 0 ? (
        <ul className="flex flex-col gap-2">
          {result.lines.map((line, index) => {
            const serialized = line.tracking_mode === 'SERIALIZED';
            return (
              <li
                key={`${line.box}-${line.item_type}-${line.owner_client ?? 'own'}-${line.condition}-${index}`}
                className="rounded-lg border border-slate-200 p-3 text-sm"
              >
                <p className="font-medium break-words text-slate-900">
                  {serialized
                    ? `${line.units.length} ${line.units.length === 1 ? 'unit' : 'units'}`
                    : `${trimQuantity(line.requested_qty)} ${line.uom}`}{' '}
                  · {line.item_name}
                </p>
                <p className="text-slate-600">
                  {line.owner_client ? 'Client stock' : 'Your own stock'} ·{' '}
                  {line.condition.replaceAll('_', ' ').toLowerCase()}
                  {line.box_path.length > 1 || line.box_code !== box.code
                    ? ` · in ${line.box_path.join(' › ') || line.box_code}`
                    : ''}
                </p>
                {serialized ? (
                  <p className="break-words text-slate-600">
                    {line.units.map((unit) => unit.serial_number).join(', ')}
                  </p>
                ) : (
                  <Field
                    label="How much of it"
                    htmlFor={`gol-box-qty-${index}`}
                    hint={`The box holds ${trimQuantity(line.requested_qty)} ${line.uom}. Lower it to take part.`}
                  >
                    <Input
                      id={`gol-box-qty-${index}`}
                      inputMode="decimal"
                      value={quantities[index] ?? trimQuantity(line.requested_qty)}
                      onChange={(event) => onQuantity(index, event.target.value)}
                    />
                  </Field>
                )}
              </li>
            );
          })}
        </ul>
      ) : null}

      {result.excluded.length > 0 ? (
        <Banner tone="warning">
          <div className="flex flex-col gap-1">
            <p className="font-medium">
              {result.excluded.length} cannot go
            </p>
            <ul className="flex flex-col gap-1">
              {result.excluded.map((row, index) => (
                <li key={`${row.box}-${row.serial_unit ?? row.item_type}-${index}`}>
                  {exclusionText(row)}
                </li>
              ))}
            </ul>
          </div>
        </Banner>
      ) : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* What is waiting for the site                                               */
/* -------------------------------------------------------------------------- */

/**
 * Everything earmarked for the chosen site at the chosen store, all ticked
 * (Q6). Untick what is not going, lower a bulk quantity to take part of it,
 * then "Add ticked" turns the rest into lines. Remounted (by key) when the site
 * or store changes, so each starts ticked again.
 */
function EarmarkedPanel({
  data,
  siteName,
  storeName,
  existing,
  onAdd,
  onDismiss,
}: {
  data: EarmarkedResult;
  siteName: string;
  storeName: string;
  existing: GateOutLine[];
  onAdd: (lines: GateOutLine[]) => void;
  onDismiss: () => void;
}) {
  const rows = earmarkedRows(data.lines);
  const [ticked, setTicked] = useState<Set<string>>(() => allTicked(data.lines));
  const [quantities, setQuantities] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);

  if (rows.length === 0) {
    return (
      <div className="flex flex-col gap-2">
        <p className="text-sm text-slate-600">
          Nothing is earmarked for {siteName} at {storeName}.
        </p>
        {data.excluded.length > 0 ? <ExcludedList rows={data.excluded} /> : null}
      </div>
    );
  }

  function toggle(key: string) {
    setTicked((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function addTicked() {
    for (const row of rows) {
      if (row.kind !== 'bulk' || !ticked.has(row.key)) continue;
      const line = data.lines[row.lineIndex];
      const problem = earmarkedBulkProblem(
        quantities[row.key] ?? row.quantity ?? '',
        row.quantity ?? '0',
        line.uom,
      );
      if (problem) {
        setError(`${line.item_name}: ${problem}`);
        return;
      }
    }
    setError(null);
    onAdd(earmarkedToLines(data.lines, ticked, quantities, existing).lines);
  }

  return (
    <section className="flex flex-col gap-3 rounded-lg border border-slate-200 p-3">
      <h2 className="text-sm font-semibold text-slate-900">Waiting for {siteName}</h2>
      {error ? <Banner tone="error">{error}</Banner> : null}
      <ul className="flex flex-col gap-2">
        {data.lines.map((line, lineIndex) => (
          <li key={`${line.item_type}-${lineIndex}`} className="flex flex-col gap-1 text-sm">
            <p className="font-medium break-words text-slate-900">
              {line.item_name}
              <span className="font-normal text-slate-600">
                {line.owner_client ? ' · client stock' : ''}
              </span>
            </p>
            {rows
              .filter((row) => row.lineIndex === lineIndex)
              .map((row) =>
                row.kind === 'bulk' ? (
                  <div key={row.key} className="flex flex-col gap-1">
                    <Checkbox
                      label={`${row.quantity} ${line.uom} earmarked`}
                      checked={ticked.has(row.key)}
                      onChange={() => toggle(row.key)}
                    />
                    {ticked.has(row.key) ? (
                      <Field label="How much of it" htmlFor={`go-earmark-qty-${lineIndex}`}>
                        <Input
                          id={`go-earmark-qty-${lineIndex}`}
                          inputMode="decimal"
                          value={quantities[row.key] ?? row.quantity ?? ''}
                          onChange={(event) =>
                            setQuantities((current) => ({
                              ...current,
                              [row.key]: event.target.value,
                            }))
                          }
                        />
                      </Field>
                    ) : null}
                  </div>
                ) : (
                  <Checkbox
                    key={row.key}
                    label={row.label}
                    checked={ticked.has(row.key)}
                    onChange={() => toggle(row.key)}
                  />
                ),
              )}
          </li>
        ))}
      </ul>
      {data.excluded.length > 0 ? <ExcludedList rows={data.excluded} /> : null}
      <div className="flex gap-2">
        <Button block onClick={addTicked} disabled={ticked.size === 0}>
          Add ticked
        </Button>
        <Button block variant="secondary" onClick={onDismiss}>
          Not now
        </Button>
      </div>
    </section>
  );
}

function ExcludedList({ rows }: { rows: EarmarkedResult['excluded'] }) {
  return (
    <Banner tone="warning">
      <div className="flex flex-col gap-1">
        <p className="font-medium">{rows.length} cannot go</p>
        <ul className="flex flex-col gap-1">
          {rows.map((row, index) => (
            <li key={`${row.serial_number || row.drum_number}-${index}`}>{row.message}</li>
          ))}
        </ul>
      </div>
    </Banner>
  );
}
