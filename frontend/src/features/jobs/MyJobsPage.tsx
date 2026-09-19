/**
 * T5.10 — my jobs (design §7.4; H1, H2).
 *
 * The technician's landing screen. Everything here is filtered to *them*: a
 * technician scrolling a list of the company's four hundred jobs to find their
 * two is a technician who stops using the app and phones the yard instead.
 *
 * The list is deliberately thin. What a technician needs before they set off is
 * the site, its reference, and whether anything is outstanding; the job itself is
 * one tap away. Sites are what people say on the phone ("I'm at Kileleshwa"), so
 * the site name leads and the job reference follows.
 */

import { useState } from 'react';
import { Link } from 'react-router-dom';

import { errorMessage, useList } from '../../api/hooks';
import { PERM } from '../../auth/permissions';
import { useSession } from '../../auth/session';
import { Banner, Button, Spinner } from '../../components/ui';
import { EmptyState, PageHeader, StatusBadge } from '../../components/ui/data';
import { JobSheet } from '../projects/JobSheet';
import type { Job } from './types';

export default function MyJobsPage() {
  const { has } = useSession();
  const seesEverything = has(PERM.REPORT_VIEW_ALL);
  // H1: raising a job had no screen at all until now, so the requirement named
  // an actor — the storekeeper — who could not carry it out.
  const [raising, setRaising] = useState(false);

  // `open=true` rather than a status this screen picks: "still to be closed
  // out" is three statuses, and a client that named one of them silently hid
  // every job already awaiting closeout — which is the list a technician came
  // for. The server owns that definition (see JobViewSet.get_queryset).
  const jobsQuery = useList<Job>('jobs', {
    assignee: seesEverything ? undefined : 'me',
    open: 'true',
    page_size: 50,
  });

  const jobs = jobsQuery.data?.results ?? [];
  const loading = jobsQuery.isLoading;
  const error = jobsQuery.error;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={seesEverything ? 'Open jobs' : 'My jobs'}
        subtitle={
          seesEverything
            ? 'Everything still open, whoever it is assigned to.'
            : 'What is assigned to you and still open.'
        }
        actions={
          <>
            <Link
              to="/jobs/custody"
              className="flex min-h-[44px] items-center rounded-lg border border-slate-300 px-4 text-sm font-medium text-slate-800"
            >
              What I'm carrying
            </Link>
            {has(PERM.JOB_MANAGE) ? (
              <Button onClick={() => setRaising(true)}>New job</Button>
            ) : null}
          </>
        }
      />

      {error ? <Banner tone="error">{errorMessage(error)}</Banner> : null}

      {loading ? (
        <Spinner className="text-slate-400" />
      ) : jobs.length === 0 ? (
        <EmptyState
          title="No open jobs."
          hint="A job appears here when it is assigned to you."
        />
      ) : (
        <ul className="flex flex-col gap-2">
          {jobs.map((job) => (
            <li key={job.id}>
              <Link
                to={`/jobs/${job.id}`}
                className="flex items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white p-3"
              >
                <div className="min-w-0">
                  <p className="truncate text-base font-semibold text-slate-900">
                    {job.site_name || job.reference}
                  </p>
                  <p className="truncate text-sm text-slate-600">
                    {job.site_ref ? `${job.site_ref} · ` : ''}
                    {job.reference}
                    {job.client_name ? ` · ${job.client_name}` : ''}
                  </p>
                  {job.description ? (
                    <p className="truncate text-sm text-slate-500">{job.description}</p>
                  ) : null}
                </div>
                <StatusBadge status={job.status} />
              </Link>
            </li>
          ))}
        </ul>
      )}

      <Button variant="ghost" onClick={() => void jobsQuery.refetch()}>
        Refresh
      </Button>

      <JobSheet
        open={raising}
        onClose={() => setRaising(false)}
        onCreated={() => jobsQuery.refetch()}
      />

    </div>
  );
}
