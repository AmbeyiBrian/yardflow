/**
 * T8.10 — enrolling a fingerprint, and managing what is enrolled (§5.3; B5).
 *
 * Everyone's own pane, not an administrator's screen: the person who owns the
 * phone is the person who enrols it. What an administrator can do — revoke
 * somebody else's lost device (B5) — happens from the people pane, because that
 * is where they are already looking when somebody rings to say their phone is
 * gone.
 *
 * The screen states what the fingerprint is *for*. D9 limits WebAuthn to the
 * approval step-up in v1, and an owner who thinks they have enabled fingerprint
 * *login* will be confused the next time they sign in — so the copy says
 * approvals, plainly, rather than "two-factor authentication".
 */

import { useCallback, useEffect, useState } from 'react';

import { errorMessage } from '../../api/hooks';
import {
  type EnrolledDevice,
  biometricsAvailable,
  enrolDevice,
  enrolledDevices,
  revokeDevice,
} from '../../auth/webauthn';
import { Banner, Button, Card, Field, Input, Spinner } from '../../components/ui';
import { EmptyState, PageHeader } from '../../components/ui/data';

export default function SecurityPage() {
  const [devices, setDevices] = useState<EnrolledDevice[]>([]);
  const [available, setAvailable] = useState<boolean | null>(null);
  const [label, setLabel] = useState('');
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      setDevices(await enrolledDevices());
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void biometricsAvailable().then(setAvailable);
    void load();
  }, [load]);

  async function enrol() {
    setBanner(null);
    setError(null);
    setBusy(true);
    try {
      const stored = await enrolDevice(label || defaultLabel());
      setBanner(`${stored} is enrolled. It will be offered the next time you approve.`);
      setLabel('');
      await load();
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  }

  const active = devices.filter((device) => device.is_active);

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Approving with a fingerprint"
        subtitle="Used when you approve a gate pass, so the signature is yours."
      />

      {banner ? <Banner tone="success">{banner}</Banner> : null}
      {error ? <Banner tone="error">{error}</Banner> : null}

      {available === false ? (
        // T8.10's fallback, stated rather than hidden. An owner on a desktop
        // should understand why there is no button, not wonder if it is broken.
        <Banner tone="info">
          This device has no fingerprint sensor or face unlock, so there is
          nothing to enrol here. Approving with your password works exactly as
          before — and a phone you enrol will still be offered on that phone.
        </Banner>
      ) : null}

      {available ? (
        <Card className="flex flex-col gap-3">
          <div>
            <h2 className="text-sm font-semibold text-slate-900">Enrol this device</h2>
            <p className="text-sm text-slate-600">
              You will be asked for your fingerprint when you approve. It is not
              used to sign in — that stays your password.
            </p>
          </div>
          <Field
            label="What to call this device"
            htmlFor="device-label"
            hint="So you can tell which one to remove if you lose it."
          >
            <Input
              id="device-label"
              value={label}
              placeholder={defaultLabel()}
              onChange={(event) => setLabel(event.target.value)}
            />
          </Field>
          <Button loading={busy} onClick={() => void enrol()}>
            Enrol
          </Button>
        </Card>
      ) : null}

      <Card className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold text-slate-900">
          Enrolled devices {active.length > 0 ? `(${active.length})` : ''}
        </h2>
        {loading ? (
          <Spinner className="text-slate-400" />
        ) : devices.length === 0 ? (
          <EmptyState
            title="Nothing enrolled."
            hint="You can still approve with your password."
          />
        ) : (
          <ul className="flex flex-col gap-2">
            {devices.map((device) => (
              <li
                key={device.id}
                className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 pb-2 last:border-0"
              >
                <div className="min-w-0">
                  <p className="text-sm font-medium text-slate-900">
                    {device.device_label}
                    {device.is_active ? '' : ' — revoked'}
                  </p>
                  <p className="text-xs text-slate-500">
                    enrolled {device.created_at.slice(0, 10)}
                    {device.last_used_at
                      ? ` · last used ${device.last_used_at.slice(0, 10)}`
                      : ' · not used yet'}
                  </p>
                </div>
                {device.is_active ? (
                  <Button
                    variant="danger"
                    className="min-h-0 px-3 py-1 text-sm"
                    onClick={async () => {
                      setError(null);
                      try {
                        await revokeDevice(device.id);
                        setBanner(`${device.device_label} can no longer approve.`);
                        await load();
                      } catch (failure) {
                        setError(errorMessage(failure));
                      }
                    }}
                  >
                    Remove
                  </Button>
                ) : null}
              </li>
            ))}
          </ul>
        )}
        {devices.some((device) => !device.is_active) ? (
          // M3: a revoked credential stays listed, because an approval signed
          // with it has to remain explicable years later.
          <p className="text-xs text-slate-500">
            Removed devices stay listed. An approval signed with one has to stay
            explicable.
          </p>
        ) : null}
      </Card>
    </div>
  );
}

function defaultLabel(): string {
  if (typeof navigator === 'undefined') return 'This device';
  const agent = navigator.userAgent;
  if (/android/i.test(agent)) return 'My Android phone';
  if (/iphone|ipad/i.test(agent)) return 'My iPhone';
  if (/mac/i.test(agent)) return 'My Mac';
  if (/windows/i.test(agent)) return 'My Windows PC';
  return 'This device';
}
