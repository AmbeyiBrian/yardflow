/**
 * Settings, Clock-in (R13; design §4.18.8, §4.18.11).
 *
 * Two numbers the organization owns: the hour a forgotten clock-in is closed
 * for the day (0-23) and the worst GPS accuracy, in metres, that still counts
 * as a reading (`/attendance/settings`). Below them, the places that cannot be
 * clocked in at because they have no coordinates: the sites, and the yards and
 * offices, each opening the same edit sheet the Network pane uses. A new
 * tenant's "Main yard" starts blank, so it is called out first.
 */

import { useEffect, useState } from 'react';

import { errorMessage, useAction, useList, useResource } from '../../api/hooks';
import { Banner, Button, Card, Field, Input, Spinner } from '../../components/ui';
import { PageHeader } from '../../components/ui/data';
import { LocationSheet, SiteSheet } from './NetworkPage';
import { hasArea } from './coordinates';
import type { AttendanceSettings, Client, Location, Site } from './types';

const SETTINGS = 'attendance/settings';

export default function AttendancePage() {
  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Clock-in"
        subtitle="When a forgotten clock-in closes, how precise a position must be, and which places still need coordinates."
      />
      <Limits />
      <MissingPlaces />
    </div>
  );
}

function Limits() {
  const settings = useResource<AttendanceSettings>(SETTINGS);
  const update = useAction<Partial<AttendanceSettings>, AttendanceSettings>({
    resource: SETTINGS,
    method: 'patch',
  });
  const [hour, setHour] = useState('');
  const [cap, setCap] = useState('');
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (!settings.data) return;
    setHour(String(settings.data.clock_auto_close_hour));
    setCap(String(settings.data.clock_accuracy_cap_m));
  }, [settings.data]);

  async function save() {
    setError('');
    setSaved(false);
    const hourValue = Number(hour);
    const capValue = Number(cap);
    if (hour.trim() === '' || !Number.isInteger(hourValue) || hourValue < 0 || hourValue > 23) {
      setError('The closing hour is a whole number from 0 to 23.');
      return;
    }
    if (cap.trim() === '' || !Number.isInteger(capValue) || capValue < 1) {
      setError('The accuracy limit is a whole number of metres, 1 or more.');
      return;
    }
    try {
      await update.mutateAsync({ clock_auto_close_hour: hourValue, clock_accuracy_cap_m: capValue });
      setSaved(true);
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }

  if (settings.isLoading) return <Spinner className="text-slate-400" />;
  if (settings.isError) return <Banner tone="error">{errorMessage(settings.error)}</Banner>;

  return (
    <Card className="flex flex-col gap-4">
      {error ? <Banner tone="error">{error}</Banner> : null}
      {saved ? <Banner tone="success">Saved.</Banner> : null}
      <Field
        label="Close open days at (hour, 0-23)"
        htmlFor="clock-hour"
        hint="A person still clocked in at this hour, in the organization's time zone, is clocked out and the day flagged."
      >
        <Input
          id="clock-hour"
          inputMode="numeric"
          className="max-w-32 text-right tabular-nums"
          value={hour}
          onChange={(event) => {
            setSaved(false);
            setHour(event.target.value);
          }}
        />
      </Field>
      <Field
        label="Accuracy limit (metres)"
        htmlFor="clock-cap"
        hint="A position less precise than this is refused with 'too vague' rather than guessed at."
      >
        <Input
          id="clock-cap"
          inputMode="numeric"
          className="max-w-32 text-right tabular-nums"
          value={cap}
          onChange={(event) => {
            setSaved(false);
            setCap(event.target.value);
          }}
        />
      </Field>
      <div>
        <Button onClick={save} loading={update.isPending}>
          Save
        </Button>
      </div>
    </Card>
  );
}

function MissingPlaces() {
  const sites = useList<Site>('sites', { missing_coordinates: 'true', page_size: 100 });
  const locations = useList<Location>('locations', { page_size: 300 });
  const clients = useList<Client>('clients', { page_size: 200 });
  const [site, setSite] = useState<Site | null>(null);
  const [location, setLocation] = useState<Location | null>(null);

  const missingSites = sites.data?.results ?? [];
  const allLocations = locations.data?.results ?? [];
  const missingLocations = allLocations.filter(
    (row) => hasArea('location', row.type) && row.has_coordinates === false,
  );
  // §4.18.8: a new tenant's "Main yard" is the one the prompt is really for.
  const mainYard = missingLocations.find((row) => row.is_system && row.type === 'YARD');

  if (sites.isLoading || locations.isLoading) return <Spinner className="text-slate-400" />;
  if (sites.isError) return <Banner tone="error">{errorMessage(sites.error)}</Banner>;

  return (
    <div className="flex flex-col gap-3">
      <h2 className="text-base font-semibold text-slate-900">Places without coordinates</h2>
      <p className="text-sm text-slate-600">
        Nobody can clock in at these until their position is set.
      </p>

      {mainYard ? (
        <Banner tone="warning">
          <span className="flex flex-wrap items-center gap-3">
            <span>{mainYard.name} has no position yet, so no one can clock in at the yard.</span>
            <Button variant="secondary" onClick={() => setLocation(mainYard)}>
              Set coordinates
            </Button>
          </span>
        </Banner>
      ) : null}

      {missingSites.length === 0 && missingLocations.length === 0 ? (
        <Card>
          <p className="text-sm text-slate-700">Every site, yard and office has coordinates.</p>
        </Card>
      ) : (
        <ul className="flex flex-col gap-2">
          {missingLocations.map((row) => (
            <li key={`l${row.id}`}>
              <button
                type="button"
                onClick={() => setLocation(row)}
                className="flex min-h-[44px] w-full items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white p-3 text-left active:bg-slate-50"
              >
                <span className="font-medium text-slate-900">{row.name}</span>
                <span className="text-sm text-slate-600">{row.type.toLowerCase()}</span>
              </button>
            </li>
          ))}
          {missingSites.map((row) => (
            <li key={`s${row.id}`}>
              <button
                type="button"
                onClick={() => setSite(row)}
                className="flex min-h-[44px] w-full items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white p-3 text-left active:bg-slate-50"
              >
                <span className="font-medium text-slate-900">
                  {row.internal_ref} {row.name}
                </span>
                <span className="text-sm text-slate-600">site</span>
              </button>
            </li>
          ))}
        </ul>
      )}

      <SiteSheet
        open={site !== null}
        site={site ?? undefined}
        onClose={() => setSite(null)}
        clients={clients.data?.results ?? []}
      />
      <LocationSheet
        open={location !== null}
        location={location ?? undefined}
        onClose={() => setLocation(null)}
        locations={allLocations}
      />
    </div>
  );
}
