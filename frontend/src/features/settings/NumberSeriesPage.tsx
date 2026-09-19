/**
 * Number series (design §4.13, §7.4; M6).
 *
 * M6 wants numbers that are sequential, gap-free and never reused — that is
 * what lets an auditor say nothing was removed. A contractor wants their own
 * prefix, and often wants to carry on from a sequence they already ran on
 * paper. Both are satisfiable, and this screen is where the line sits:
 *
 * * **prefix and padding** are theirs;
 * * **the counter moves forward only** — the server refuses to move it back
 *   onto numbers already issued, because that would put the same number on two
 *   documents;
 * * **documents already numbered keep their numbers.** Changing a prefix does
 *   not rewrite history, and the warning below says so rather than pretending
 *   the two eras will look alike.
 */

import { useState } from 'react';

import { errorMessage, useAction, useResource } from '../../api/hooks';
import { Banner, Button, Card, Field, Input, Spinner } from '../../components/ui';
import { DataList, PageHeader } from '../../components/ui/data';

interface Series {
  document_type: string;
  label: string;
  prefix: string;
  width: number;
  next_number: number;
  highest_issued: number;
  example: string;
}

export default function NumberSeriesPage() {
  const series = useResource<Series[]>('number-series');
  const [editing, setEditing] = useState<Series | null>(null);

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Number series"
        subtitle="How each kind of document is numbered, and what the next one will be."
      />

      <Banner tone="info">
        Numbers already issued keep the number they were given. Changing a prefix
        affects documents numbered from then on, so a tenant that switches will
        have two eras in its records — which is fine, as long as it is on
        purpose.
      </Banner>

      {series.isLoading ? <Spinner /> : null}
      {series.isError ? <Banner tone="error">{errorMessage(series.error)}</Banner> : null}

      <DataList<Series>
        rows={series.data ?? []}
        rowKey={(row) => row.document_type}
        onRowClick={(row) => setEditing(row)}
        columns={[
          { header: 'Document', cell: (row) => row.label },
          {
            header: 'Next',
            cell: (row) => <span className="tabular-nums">{row.example}</span>,
          },
          {
            header: 'Issued so far',
            cell: (row) => <span className="tabular-nums">{row.highest_issued}</span>,
            wideOnly: true,
          },
        ]}
      />

      {editing ? (
        <EditSeries
          series={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            series.refetch();
          }}
        />
      ) : null}
    </div>
  );
}

function EditSeries({
  series,
  onClose,
  onSaved,
}: {
  series: Series;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [prefix, setPrefix] = useState(series.prefix);
  const [width, setWidth] = useState(String(series.width));
  const [next, setNext] = useState(String(series.next_number));
  const [error, setError] = useState('');

  const save = useAction<Record<string, unknown>>({
    resource: 'number-series',
    method: 'patch',
    invalidates: ['number-series'],
  });

  // Shown live, so somebody can see what they are choosing before they commit
  // to it rather than after the next document is numbered.
  const preview = `${prefix || '—'}-${String(Number(next) || 1).padStart(
    Number(width) || 1,
    '0',
  )}`;

  return (
    <Card>
      <h2 className="text-base font-semibold text-slate-900">{series.label}</h2>

      <div className="mt-3 grid gap-3 sm:grid-cols-3">
        <Field label="Prefix" htmlFor="ns-prefix">
          <Input
            id="ns-prefix"
            value={prefix}
            onChange={(event) => setPrefix(event.target.value.toUpperCase())}
          />
        </Field>
        <Field label="Digits" htmlFor="ns-width" hint="How many the number is padded to.">
          <Input
            id="ns-width"
            inputMode="numeric"
            className="tabular-nums"
            value={width}
            onChange={(event) => setWidth(event.target.value)}
          />
        </Field>
        <Field
          label="Next number"
          htmlFor="ns-next"
          hint={
            series.highest_issued
              ? `Already issued up to ${series.highest_issued}. It can move forward, not back.`
              : 'Nothing issued yet.'
          }
        >
          <Input
            id="ns-next"
            inputMode="numeric"
            className="tabular-nums"
            value={next}
            onChange={(event) => setNext(event.target.value)}
          />
        </Field>
      </div>

      <p className="mt-2 text-sm text-slate-600">
        The next one will be <span className="font-medium tabular-nums">{preview}</span>
      </p>

      {error ? (
        <div className="mt-3">
          <Banner tone="error">{error}</Banner>
        </div>
      ) : null}

      <div className="mt-4 flex gap-3">
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button
          loading={save.isPending}
          onClick={async () => {
            setError('');
            try {
              await save.mutateAsync({
                document_type: series.document_type,
                prefix,
                width: Number(width),
                next_number: Number(next),
              });
              onSaved();
            } catch (caught) {
              setError(errorMessage(caught));
            }
          }}
        >
          Save
        </Button>
      </div>
    </Card>
  );
}
