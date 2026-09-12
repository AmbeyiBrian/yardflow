/**
 * T8.5 — releasing an approved pass with no signal (design §8.3; N3).
 *
 * The criterion is a refusal: "attempting to submit-and-release offline is
 * **refused by the client** and would be refused by the server."
 *
 * Both halves are deliberate. The client refuses because a storekeeper should
 * find out at the gate, not tomorrow when the queue drains; the server refuses
 * because a client check is a courtesy and never a control. §8.3 is blunt about
 * why:
 *
 * > This is the single most important constraint in the offline design: without
 * > it, offline mode is a bypass around the entire approval control the system
 * > exists to provide.
 *
 * So this screen can only ever act on passes the *server* said were approved,
 * downloaded into `releasable`. There is no path here that creates a pass,
 * approves one, or releases one that is not in that table — the list is the
 * authorisation, and the device cannot add to it.
 */

import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';

import { errorMessage } from '../api/hooks';
import { Banner, Button, Card, Field, Input, Spinner } from '../components/ui';
import { EmptyState, PageHeader } from '../components/ui/data';
import { type ReleasablePass, readReleasable } from './db';
import { useOffline } from './OfflineProvider';
import { submitOrQueue } from './sync';

interface PassLine {
  id: number;
  item_name: string;
  requested_qty: string;
  released_qty: string;
  uom: string;
  tracking_mode: string;
  serials: { serial_unit: number; serial_number: string }[];
  reels: { reel: number; drum_number: string; length_requested: string }[];
}

export default function OfflineReleasePage() {
  const { online, syncNow, syncing, refresh } = useOffline();
  const [passes, setPasses] = useState<ReleasablePass[]>([]);
  const [loading, setLoading] = useState(true);
  const [releasing, setReleasing] = useState<ReleasablePass | null>(null);

  useEffect(() => {
    void readReleasable().then((rows) => {
      setPasses(rows);
      setLoading(false);
    });
  }, [syncing]);

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Release at the gate"
        subtitle="Approved passes downloaded to this device. Nothing else can be released here."
        actions={
          <Button variant="secondary" loading={syncing} onClick={() => void syncNow()}>
            {online ? 'Refresh the list' : 'Try to refresh'}
          </Button>
        }
      />

      {/* N3, said plainly. Somebody reading this screen for the first time
          should understand the limit before they hit it. */}
      <Banner tone="info">
        Only passes that were <strong>already approved</strong> can be released
        without a connection. A new request can be captured, but it has to reach
        the yard for approval before anything leaves.
      </Banner>

      {!online && passes.length === 0 ? (
        <Banner tone="warning">
          Nothing was downloaded before the signal went. Without a connection
          there is no way to check whether a pass was approved, so nothing can be
          released — which is the control, not a fault.
        </Banner>
      ) : null}

      {loading ? (
        <Spinner className="text-slate-400" />
      ) : passes.length === 0 ? (
        <EmptyState
          title="No approved passes on this device."
          hint="Refresh while you have signal to download whatever is approved."
        />
      ) : (
        <ul className="flex flex-col gap-2">
          {passes.map((pass) => {
            const expired = pass.expires_at
              ? new Date(pass.expires_at) < new Date()
              : false;
            return (
              <li key={pass.id}>
                <Card className="flex flex-col gap-2">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="min-w-0">
                      <p className="text-sm font-semibold text-slate-900">
                        {pass.number} · {pass.destination}
                      </p>
                      <p className="text-sm text-slate-600">
                        for {pass.custody_holder} ·{' '}
                        {(pass.lines as PassLine[]).length} line
                        {(pass.lines as PassLine[]).length === 1 ? '' : 's'}
                      </p>
                      {pass.expires_at ? (
                        <p
                          className={
                            expired
                              ? 'text-xs font-semibold text-red-700'
                              : 'text-xs text-slate-500'
                          }
                        >
                          {expired ? 'Expired ' : 'Expires '}
                          {pass.expires_at.slice(0, 16).replace('T', ' ')}
                        </p>
                      ) : null}
                    </div>
                    <Button disabled={expired} onClick={() => setReleasing(pass)}>
                      Release
                    </Button>
                  </div>

                  <ul className="text-sm text-slate-700">
                    {(pass.lines as PassLine[]).map((line) => (
                      <li key={line.id}>
                        {line.requested_qty} {line.uom} {line.item_name}
                        {line.serials.length > 0 ? (
                          <span className="block font-mono text-xs text-slate-600">
                            {line.serials.map((entry) => entry.serial_number).join(', ')}
                          </span>
                        ) : null}
                      </li>
                    ))}
                  </ul>

                  {expired ? (
                    // Q3: an expired approval is not an approval. Releasing on
                    // one would be releasing against stock that has since moved.
                    <p className="text-sm text-red-700">
                      This approval has expired. It has to be resubmitted, which
                      needs a connection.
                    </p>
                  ) : null}
                </Card>
              </li>
            );
          })}
        </ul>
      )}

      <p className="text-sm text-slate-500">
        Captured releases appear under{' '}
        <Link to="/sync" className="underline">
          Sync
        </Link>{' '}
        until they reach the yard.
      </p>

      <ReleaseSheet
        pass={releasing}
        onClose={() => setReleasing(null)}
        onDone={async () => {
          await refresh();
          setPasses(await readReleasable());
        }}
      />
    </div>
  );
}

function ReleaseSheet({
  pass,
  onClose,
  onDone,
}: {
  pass: ReleasablePass | null;
  onClose: () => void;
  onDone: () => void;
}) {
  const [vehicle, setVehicle] = useState('');
  const [driver, setDriver] = useState('');
  const [banner, setBanner] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (!pass) return null;

  async function release() {
    setBanner(null);
    setBusy(true);
    try {
      const payload = {
        gate_out: pass.id,
        vehicle_reg: vehicle,
        driver_name: driver,
      };
      const outcome = await submitOrQueue('GATE_OUT_RELEASE', payload, async () => {
        const { api } = await import('../api/client');
        return api.post(`/gate-outs/${pass.id}/release`, {
          vehicle_reg: vehicle,
          driver_name: driver,
        });
      });

      setBanner(
        outcome.queued
          ? `${pass.number} recorded on this device. It reaches the yard when you have signal.`
          : `${pass.number} released.`,
      );
      onDone();
    } catch (error) {
      setBanner(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-40 flex items-end justify-center bg-slate-900/40 md:items-center">
      <button
        type="button"
        aria-label="Close"
        className="absolute inset-0 cursor-default"
        onClick={onClose}
      />
      <div className="relative w-full max-w-lg rounded-t-2xl bg-white p-4 md:rounded-2xl">
        <h2 className="mb-2 text-base font-semibold text-slate-900">
          Release {pass.number}
        </h2>

        {banner ? <Banner tone="info">{banner}</Banner> : null}

        <div className="mt-3 flex flex-col gap-3">
          {/* G2: the load has to be attributable, offline as much as online. */}
          <Field label="Vehicle" htmlFor="offline-vehicle">
            <Input
              id="offline-vehicle"
              value={vehicle}
              onChange={(event) => setVehicle(event.target.value)}
            />
          </Field>
          <Field label="Driver" htmlFor="offline-driver">
            <Input
              id="offline-driver"
              value={driver}
              onChange={(event) => setDriver(event.target.value)}
            />
          </Field>

          <div className="flex gap-3">
            <Button variant="secondary" block onClick={onClose}>
              Close
            </Button>
            <Button block loading={busy} onClick={() => void release()}>
              Release it
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
