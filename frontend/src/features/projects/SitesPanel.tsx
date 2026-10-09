/** Project sites tab (R10; §4.19.6): dates, certificate, accepted badge, collection and dispatch. */

import { useState } from 'react';
import { Link } from 'react-router-dom';

import { errorMessage, useResource } from '../../api/hooks';
import { PERM } from '../../auth/permissions';
import { useSession } from '../../auth/session';
import { PhotoCapture, type Attachment } from '../../components/PhotoCapture';
import { Banner, Button, Card, Input, Spinner } from '../../components/ui';
import { EmptyState } from '../../components/ui/data';
import { dateOnly, isAcceptedSite } from './budget';
import { useProjectSites, useUpdateProjectSite, type ProjectSite } from './stage2Api';

const CERTIFICATE = 'Acceptance certificate';
const TARGET = 'network.ProjectSite';

export default function SitesPanel({
  projectId,
  isManager,
}: {
  projectId: number;
  /** The project's PM; `catalogue.manage` also edits (§4.19.10). */
  isManager: boolean;
}) {
  const { has } = useSession();
  const canEdit = isManager || has(PERM.CATALOGUE_MANAGE);
  const sites = useProjectSites(projectId);
  if (sites.isLoading) return <Spinner />;
  const rows = sites.data?.results ?? [];
  if (rows.length === 0) {
    return <EmptyState title="No sites on this project." />;
  }
  return (
    <div className="flex flex-col gap-3">
      {rows.map((s) => (
        <SiteCard key={s.id} site={s} canEdit={canEdit} />
      ))}
    </div>
  );
}

function SiteCard({ site, canEdit }: { site: ProjectSite; canEdit: boolean }) {
  const update = useUpdateProjectSite();
  const [mobilised, setMobilised] = useState(site.mobilised_on ?? '');
  const [accepted, setAccepted] = useState(site.accepted_on ?? '');
  const certs = useResource<Attachment[] | { results: Attachment[] }>('attachments', {
    target_type: TARGET,
    target_id: String(site.id),
    page_size: 100,
  });
  const list = Array.isArray(certs.data) ? certs.data : (certs.data?.results ?? []);
  const certificates = list.filter((a) => (a.caption ?? CERTIFICATE) === CERTIFICATE).length;
  const isAccepted = isAcceptedSite(site.accepted_on, certificates);
  const dirty = mobilised !== (site.mobilised_on ?? '') || accepted !== (site.accepted_on ?? '');

  return (
    <Card className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm font-semibold text-slate-900">
          {site.site_ref || site.site_name || `Site ${site.site}`}
          {site.site_ref && site.site_name ? ` · ${site.site_name}` : ''}
        </p>
        <span
          className={
            isAccepted
              ? 'rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-800'
              : 'rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-700'
          }
        >
          {isAccepted ? 'Accepted' : 'Not accepted'}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <label className="flex flex-col gap-1 text-xs text-slate-500">
          Mobilised on
          <Input
            type="date"
            value={mobilised}
            disabled={!canEdit}
            onChange={(e) => setMobilised(e.target.value)}
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-500">
          Accepted on
          <Input
            type="date"
            value={accepted}
            disabled={!canEdit}
            onChange={(e) => setAccepted(e.target.value)}
          />
        </label>
      </div>
      {canEdit && dirty ? (
        <div>
          <Button
            disabled={update.isPending}
            onClick={() =>
              update.mutate({
                id: site.id,
                mobilised_on: mobilised || null,
                accepted_on: accepted || null,
              })
            }
          >
            Save dates
          </Button>
        </div>
      ) : null}
      {update.error ? <Banner tone="error">{errorMessage(update.error)}</Banner> : null}

      <dl className="grid grid-cols-2 gap-2 text-sm">
        <div>
          <dt className="text-xs text-slate-500">First collection</dt>
          <dd className="font-medium text-slate-900">{dateOnly(site.first_collection_at)}</dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Latest dispatch</dt>
          <dd className="font-medium text-slate-900">{dateOnly(site.last_dispatch_at)}</dd>
        </div>
      </dl>

      {site.accepted_on && certificates === 0 ? (
        <p className="text-xs text-amber-700">
          An acceptance date is set but no certificate is attached, so the site is not accepted
          yet.
        </p>
      ) : null}

      {canEdit ? (
        <PhotoCapture
          targetType={TARGET}
          targetId={site.id}
          kind="DOCUMENT"
          label="Acceptance certificate"
          caption={CERTIFICATE}
          onChange={() => certs.refetch()}
        />
      ) : (
        list.map((a) => (
          <a
            key={a.id}
            href={a.download_url}
            target="_blank"
            rel="noreferrer"
            className="text-sm text-sky-700 underline"
          >
            {a.filename}
          </a>
        ))
      )}

      <Link to={`/stock?earmarked_for=${site.site}`} className="text-sm text-sky-700 underline">
        Material waiting in the yard for this site
      </Link>
    </Card>
  );
}
