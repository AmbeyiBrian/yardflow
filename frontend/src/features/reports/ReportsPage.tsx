/**
 * T7.7 — the report screens (design §7.4, §10; M1, M2).
 *
 * The criterion: "every day-one report is reachable and exportable from the UI."
 *
 * **Nothing here knows the name of a report.** The catalogue endpoint returns
 * each report's columns and filters, and this screen renders both — so a report
 * added on the server appears here, with a working filter panel and working
 * exports, without anybody touching the frontend. That is the same property T7.1
 * asks of the export code, and it is worth having on this side too: a reporting
 * screen that needs a code change per report is a reporting screen that lags the
 * reports by a release.
 *
 * Two consequences worth stating:
 *
 * **Filter panels are generated from `kind`.** A `reference` filter becomes a
 * dropdown fed by the resource the server named; a `date` becomes a date input.
 * The server owns which filters exist and which are required, so the screen
 * cannot offer a combination the report will refuse.
 *
 * **The table is the shared one.** `DataList` is cards on a phone and a table
 * from `md` up (§7.3), and columns marked `wide_only` drop off the card — which
 * is how a nine-column report stays readable on the device an owner actually has
 * in the yard.
 */

import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useCrumb } from '../../components/ui/breadcrumbs';

import { downloadFile } from '../../api/client';
import { errorMessage, useList, useResource } from '../../api/hooks';
import { Banner, Button, Card, Field, Input, Select, Spinner } from '../../components/ui';
import { DataList, EmptyState, PageHeader, Stat } from '../../components/ui/data';
import type { Column, ReportCatalogue, ReportCatalogueEntry, ReportResult } from './types';

/** Mirrors `CATEGORIES` on the server. Order matters: Finance first. */
const CATEGORY_ORDER = [
  'Finance',
  'Stock',
  'Movements',
  'Custody and control',
  'Exceptions and disposal',
];

function slugify(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, '-');
}

export default function ReportsPage() {
  const catalogue = useResource<ReportCatalogue>(
    'reports',
  );

  const reports = catalogue.data?.reports ?? [];

  // Grouped, in the server's order. Sixteen cards in one grid was a dump: the
  // person looking for "what is this yard worth" had to read past overdue
  // returns to find it. The vocabulary and the order live on the server so a
  // new report lands in the right place without a frontend change; anything
  // the server sends that this build has not heard of goes last, not lost.
  const groups = CATEGORY_ORDER.map((category) => ({
    category,
    reports: reports.filter((report) => report.category === category),
  })).filter((group) => group.reports.length > 0);
  const unknown = reports.filter((report) => !CATEGORY_ORDER.includes(report.category));
  if (unknown.length) groups.push({ category: 'Other', reports: unknown });

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Reports"
        subtitle="The day-one set an operator audit asks for. Every one exports."
        actions={
          // M5 lives here rather than in settings: it is a list to work
          // through, which makes it a report in everything but name.
          <Link
            to="/reports/retention"
            className="flex min-h-[44px] items-center rounded-lg border border-slate-300 px-4 text-sm font-medium text-slate-800"
          >
            Retention review
          </Link>
        }
      />

      {catalogue.isError ? (
        <Banner tone="error">{errorMessage(catalogue.error)}</Banner>
      ) : null}

      {catalogue.isLoading ? (
        <Spinner className="text-slate-400" />
      ) : reports.length === 0 ? (
        <EmptyState
          title="No reports available to you."
          hint="Reporting needs the report.view_all permission. An administrator can add it."
        />
      ) : (
        <div className="flex flex-col gap-6">
          {groups.map((group) => (
            <section key={group.category} aria-labelledby={`reports-${slugify(group.category)}`}>
              <h2
                id={`reports-${slugify(group.category)}`}
                className="mb-2 text-xs font-semibold tracking-wide text-slate-500 uppercase"
              >
                {group.category}
              </h2>
              <ul className="grid gap-2 sm:grid-cols-2">
                {group.reports.map((report) => (
                  <li key={report.slug}>
                    <Link to={`/reports/${report.slug}`} className="block h-full">
                      <Card className="flex h-full flex-col gap-1">
                        <p className="text-sm font-semibold text-slate-900">{report.title}</p>
                        <p className="text-sm text-slate-600">{report.description}</p>
                        {/* The `requirement` field is still on the API for
                            traceability, and deliberately not shown. It printed
                            as "M1, F7" under each report — our references into
                            the requirements document, meaningless to the person
                            choosing a report. What the report answers is the
                            description above. */}
                      </Card>
                    </Link>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}

/** One report: its filter panel, its rows, and its two exports. */
export function ReportPage() {
  const { slug } = useParams();
  const catalogue = useResource<ReportCatalogue>('reports');
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [applied, setApplied] = useState<Record<string, string>>({});
  const [banner, setBanner] = useState<string | null>(null);
  const [degraded, setDegraded] = useState<string | null>(null);
  const [exporting, setExporting] = useState<string | null>(null);

  const definition = (catalogue.data?.reports ?? []).find(
    (report) => report.slug === slug,
  );
  // Unknown until the catalogue arrives; only an explicit `false` disables.
  const pdfAvailable = catalogue.data?.pdf_available !== false;
  useCrumb(definition?.title);

  const missingRequired = (definition?.filters ?? []).filter(
    (filter) => filter.required && !applied[filter.key],
  );

  const result = useResource<ReportResult>(
    `reports/${slug}`,
    applied,
    // A report with an unfilled required filter would be a 400, and a red banner
    // on arrival is not how a screen should introduce itself.
    { enabled: Boolean(slug) && missingRequired.length === 0 },
  );

  // Derived during render rather than memoized: the compiler warns that a
  // memo over `definition` cannot be preserved, and copying a handful of column
  // descriptors is not work worth caching.
  const columns: Column[] = result.data?.columns ?? definition?.columns ?? [];

  async function runExport(format: 'xlsx' | 'pdf') {
    setBanner(null);
    setDegraded(null);
    setExporting(format);
    try {
      const queued = await downloadFile(`/reports/${slug}/export`, {
        body: { format, filters: applied },
        onHeaders: (headers) => {
          // §11: the server could not render a PDF and sent HTML instead. The
          // file is still useful; arriving unannounced as `.html` where a PDF
          // was asked for is what made it look broken.
          if (headers.get('X-YardFlow-Degraded') === 'pdf-unavailable') {
            setDegraded(
              'This server cannot produce PDFs, so you were given the report as an HTML ' +
                'file instead. It opens in any browser and prints to PDF from there.',
            );
          }
        },
      });
      if (queued) {
        // T7.5: over the threshold it runs in a worker and the notification
        // carries the link. Nothing has downloaded, so say so.
        setBanner(String(queued.message ?? 'Building it in the background.'));
      }
    } catch (error) {
      setBanner(errorMessage(error));
    } finally {
      setExporting(null);
    }
  }

  if (catalogue.isLoading) return <Spinner className="text-slate-400" />;
  if (!definition) {
    return (
      <Banner tone="warning">
        There is no report called “{slug}”, or you do not have permission for it.
      </Banner>
    );
  }

  const rows = result.data?.rows ?? [];

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={definition.title}
        subtitle={definition.description}
        actions={
          <>
            <Link
              to="/reports"
              className="flex min-h-[44px] items-center px-2 text-sm text-slate-700"
            >
              All reports
            </Link>
            <Button
              variant="secondary"
              loading={exporting === 'xlsx'}
              onClick={() => void runExport('xlsx')}
            >
              Excel
            </Button>
            <Button
              variant="secondary"
              loading={exporting === 'pdf'}
              disabled={!pdfAvailable}
              title={
                pdfAvailable
                  ? undefined
                  : 'This server cannot produce PDFs. Export to Excel, or print the screen.'
              }
              onClick={() => void runExport('pdf')}
            >
              PDF
            </Button>
          </>
        }
      />

      {banner ? <Banner tone="info">{banner}</Banner> : null}
      {degraded ? <Banner tone="warning">{degraded}</Banner> : null}
      {result.isError ? <Banner tone="error">{errorMessage(result.error)}</Banner> : null}

      {definition.filters.length > 0 ? (
        <Card className="flex flex-col gap-3">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {definition.filters.map((filter) => (
              <FilterInput
                key={filter.key}
                filter={filter}
                value={filters[filter.key] ?? ''}
                onChange={(value) =>
                  setFilters((current) => ({ ...current, [filter.key]: value }))
                }
              />
            ))}
          </div>
          <div className="flex gap-2">
            <Button onClick={() => setApplied(filters)}>Run it</Button>
            {Object.values(applied).some(Boolean) ? (
              <Button
                variant="secondary"
                onClick={() => {
                  setFilters({});
                  setApplied({});
                }}
              >
                Clear
              </Button>
            ) : null}
          </div>
        </Card>
      ) : null}

      {missingRequired.length > 0 ? (
        <EmptyState
          title={`Choose ${missingRequired.map((filter) => filter.label.toLowerCase()).join(' and ')}.`}
          hint="This report cannot be run without it."
        />
      ) : result.isLoading ? (
        <Spinner className="text-slate-400" />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Stat label="Rows" value={result.data?.row_count ?? 0} />
            {Object.entries(result.data?.totals ?? {})
              .slice(0, 3)
              .map(([key, value]) => (
                <Stat
                  key={key}
                  label={
                    columns.find((column) => column.key === key)?.label ?? key
                  }
                  value={value}
                />
              ))}
          </div>

          <DataList
            rows={rows}
            rowKey={(row) => JSON.stringify(row)}
            columns={columns.map((column) => ({
              header: column.label,
              // Values arrive formatted, so the screen and the exports cannot
              // disagree about what a number looks like (M2).
              cell: (row: Record<string, string>) => row[column.key],
              wideOnly: column.wide_only,
              // Tabular numerals so a column of figures lines up on the decimal
              // point. In a proportional font `1` is narrower than `8`, and a
              // report nobody can scan down is a report nobody reads.
              className: column.numeric
                ? 'text-right whitespace-nowrap tabular-nums'
                : undefined,
            }))}
            empty={
              <EmptyState
                title="Nothing to report."
                hint="No rows match those filters."
              />
            }
          />

          {result.data?.totals ? (
            <Card className="flex flex-wrap gap-4">
              {Object.entries(result.data.totals).map(([key, value]) => (
                <span key={key} className="text-sm">
                  <span className="text-slate-500">
                    {columns.find((column) => column.key === key)?.label ?? key}:{' '}
                  </span>
                  <span className="font-semibold text-slate-900">{value}</span>
                </span>
              ))}
            </Card>
          ) : null}
        </>
      )}
    </div>
  );
}

/** A filter input, from the kind the server declared (T7.7). */
function FilterInput({
  filter,
  value,
  onChange,
}: {
  filter: ReportCatalogueEntry['filters'][number];
  value: string;
  onChange: (value: string) => void;
}) {
  // A `reference` filter names an API resource, so the dropdown is fed by the
  // same endpoint the rest of the app uses for that thing.
  const options = useList<{ id: number; name?: string; full_name?: string; reference?: string }>(
    filter.resource || 'clients',
    { page_size: 200 },
    { enabled: filter.kind === 'reference' && Boolean(filter.resource) },
  );

  const label = filter.required ? `${filter.label} (required)` : filter.label;

  if (filter.kind === 'reference') {
    return (
      <Field label={label} htmlFor={`filter-${filter.key}`} hint={filter.help_text}>
        <Select
          id={`filter-${filter.key}`}
          value={value}
          onChange={(event) => onChange(event.target.value)}
        >
          <option value="">Any</option>
          {(options.data?.results ?? []).map((option) => (
            <option key={option.id} value={option.id}>
              {option.name ?? option.full_name ?? option.reference ?? option.id}
            </option>
          ))}
        </Select>
      </Field>
    );
  }

  if (filter.kind === 'choice') {
    return (
      <Field label={label} htmlFor={`filter-${filter.key}`} hint={filter.help_text}>
        <Select
          id={`filter-${filter.key}`}
          value={value}
          onChange={(event) => onChange(event.target.value)}
        >
          <option value="">Any</option>
          {filter.choices.map((choice) => (
            <option key={choice.value} value={choice.value}>
              {choice.label}
            </option>
          ))}
        </Select>
      </Field>
    );
  }

  return (
    <Field label={label} htmlFor={`filter-${filter.key}`} hint={filter.help_text}>
      <Input
        id={`filter-${filter.key}`}
        type={
          filter.kind === 'date'
            ? 'date'
            : filter.kind === 'datetime'
              ? 'datetime-local'
              : 'text'
        }
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </Field>
  );
}

/** M5, T7.6: what is past retention — and the reassurance that nothing was removed. */
export function RetentionReviewPage() {
  const review = useResource<{
    cutoff: string;
    retention_months: number;
    candidates: {
      document_type: string;
      document_id: string;
      number: string;
      label: string;
      closed_at: string | null;
      age_days: number;
    }[];
    still_open: {
      document_type: string;
      document_id: string;
      number: string;
      status: string;
      label: string;
      age_days: number;
    }[];
    note: string;
  }>('retention-review');

  const candidates = review.data?.candidates ?? [];
  const stillOpen = review.data?.still_open ?? [];

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Retention review"
        subtitle={
          review.data
            ? `Keeping ${review.data.retention_months} months. Anything before ${review.data.cutoff} is past that.`
            : 'What is past the retention period.'
        }
        actions={
          <Link
            to="/reports"
            className="flex min-h-[44px] items-center px-2 text-sm text-slate-700"
          >
            Reports
          </Link>
        }
      />

      {review.isError ? <Banner tone="error">{errorMessage(review.error)}</Banner> : null}

      {/* M5: the reassurance is the product. Somebody opening this screen needs
          to know immediately that reading it changed nothing. */}
      <Banner tone="info">
        {review.data?.note ??
          'Retention never deletes anything. This is a list for somebody to decide about.'}
      </Banner>

      {stillOpen.length > 0 ? (
        <Card className="flex flex-col gap-2 border-amber-300">
          <h2 className="text-sm font-semibold text-slate-900">
            {stillOpen.length} still open, and older than the retention period
          </h2>
          <p className="text-sm text-slate-600">
            Not candidates for archiving — these are things nobody finished.
          </p>
          <ul className="flex flex-col gap-1 text-sm">
            {stillOpen.map((entry) => (
              <li key={`${entry.document_type}-${entry.document_id}`}>
                <span className="font-medium text-slate-900">
                  {entry.number || entry.document_type}
                </span>{' '}
                — {entry.label}, {entry.age_days} days old
              </li>
            ))}
          </ul>
        </Card>
      ) : null}

      {review.isLoading ? (
        <Spinner className="text-slate-400" />
      ) : candidates.length === 0 ? (
        <EmptyState
          title="Nothing is past its retention period."
          hint="Every record is still inside the window this organization keeps."
        />
      ) : (
        <Card>
          <DataList
            rows={candidates}
            rowKey={(row) => `${row.document_type}-${row.document_id}`}
            columns={[
              { header: 'Document', cell: (row) => row.number || row.document_type },
              { header: 'What', cell: (row) => row.label },
              { header: 'Closed', cell: (row) => row.closed_at ?? '', wideOnly: true },
              {
                header: 'Age',
                cell: (row) => `${row.age_days} days`,
                className: 'text-right whitespace-nowrap',
              },
            ]}
          />
        </Card>
      )}
    </div>
  );
}
