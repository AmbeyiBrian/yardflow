/**
 * The company's own details, and its logo (A4).
 *
 * These print on every gate pass, GRN, disposal certificate and return waybill —
 * the documents a client reads and a driver carries — so they belong to the
 * company, and the company should be able to change them. Until now the field
 * existed on the model and nothing let anybody upload to it, which made a
 * customer's branding a support request.
 *
 * The preview is the point of the screen. A logo is uploaded once and then seen
 * only on paper, weeks later, by which time nobody remembers what was chosen —
 * so this shows it at the size it will actually print, in black and white as
 * well, because the printer in a gate house rarely has colour.
 */

import { useEffect, useRef, useState } from 'react';

import { api } from '../../api/client';
import { errorMessage, useResource } from '../../api/hooks';
import { Banner, Button, Card, Field, Input, Spinner } from '../../components/ui';
import { useSession } from '../../auth/session';
import { PERM } from '../../auth/permissions';

interface Guidance {
  print_height_mm: number;
  print_width_mm: number;
  accepted: string[];
  max_bytes: number;
  min_long_edge_px: number;
  recommended: string;
}

interface Profile {
  name: string;
  legal_name: string;
  email: string;
  phone: string;
  address: string;
  tax_pin: string;
  logo_url: string;
  logo_guidance: Guidance;
}

/** Millimetres at roughly 96 dpi, so the preview is the size it prints. */
const MM = 3.78;

export default function OrganizationPage() {
  const { hasAny, refreshUser } = useSession();
  const mayEdit = hasAny(PERM.SETTINGS_MANAGE);

  const profile = useResource<Profile>('organization');
  const fileInput = useRef<HTMLInputElement>(null);

  const [form, setForm] = useState({
    name: '',
    legal_name: '',
    email: '',
    phone: '',
    address: '',
    tax_pin: '',
  });
  const [banner, setBanner] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!profile.data) return;
    const { name, legal_name, email, phone, address, tax_pin } = profile.data;
    setForm({ name, legal_name, email, phone, address, tax_pin });
  }, [profile.data]);

  async function saveDetails() {
    setBanner(null);
    setNotice(null);
    setSaving(true);
    try {
      await api.patch('/organization', form);
      await Promise.all([profile.refetch(), refreshUser()]);
      setNotice('Saved. New documents carry these details.');
    } catch (error) {
      setBanner(errorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  async function uploadLogo(file: File) {
    setBanner(null);
    setNotice(null);
    setSaving(true);
    try {
      // Multipart rather than JSON: this is a file, and the endpoint accepts
      // both so the details form above can stay ordinary JSON.
      const body = new FormData();
      body.append('logo', file);
      await api.patch('/organization', body);
      await profile.refetch();
      setNotice('Logo updated. Print a gate pass to see it on paper.');
    } catch (error) {
      setBanner(errorMessage(error));
    } finally {
      setSaving(false);
      if (fileInput.current) fileInput.current.value = '';
    }
  }

  async function removeLogo() {
    setBanner(null);
    setNotice(null);
    try {
      await api.delete('/organization');
      await profile.refetch();
      setNotice('Logo removed. Documents now show the company name alone.');
    } catch (error) {
      setBanner(errorMessage(error));
    }
  }

  if (profile.isLoading) return <Spinner className="text-slate-400" />;
  if (profile.isError) return <Banner tone="error">{errorMessage(profile.error)}</Banner>;

  const guidance = profile.data?.logo_guidance;
  const logoUrl = profile.data?.logo_url;

  return (
    <div className="flex flex-col gap-6 pb-16">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Your company</h1>
        <p className="mt-1 max-w-2xl text-sm text-slate-600">
          What appears at the top of every gate pass, GRN, certificate and
          waybill — the documents your clients read and your drivers carry.
        </p>
      </div>

      {notice ? <Banner tone="success">{notice}</Banner> : null}
      {banner ? <Banner tone="error">{banner}</Banner> : null}
      {!mayEdit ? (
        <Banner tone="info">
          Changing these is for the owner and administrators.
        </Banner>
      ) : null}

      <Card className="flex flex-col gap-4">
        <h2 className="text-base font-semibold text-slate-900">Logo</h2>

        <div className="flex flex-wrap items-start gap-6">
          <div>
            <p className="mb-1 text-xs font-medium text-slate-500">
              As it prints, actual size
            </p>
            <div
              className="flex items-center justify-center rounded border border-dashed border-slate-300 bg-white p-2"
              style={{
                width: (guidance?.print_width_mm ?? 45) * MM,
                height: (guidance?.print_height_mm ?? 18) * MM,
              }}
            >
              {logoUrl ? (
                <img
                  src={logoUrl}
                  alt="Your logo"
                  className="max-h-full max-w-full object-contain"
                />
              ) : (
                <span className="text-xs text-slate-400">Nothing yet</span>
              )}
            </div>
          </div>

          <div>
            <p className="mb-1 text-xs font-medium text-slate-500">
              In black and white, as most gate houses print
            </p>
            <div
              className="flex items-center justify-center rounded border border-dashed border-slate-300 bg-white p-2"
              style={{
                width: (guidance?.print_width_mm ?? 45) * MM,
                height: (guidance?.print_height_mm ?? 18) * MM,
              }}
            >
              {logoUrl ? (
                <img
                  src={logoUrl}
                  alt="Your logo in black and white"
                  className="max-h-full max-w-full object-contain grayscale contrast-125"
                />
              ) : (
                <span className="text-xs text-slate-400">Nothing yet</span>
              )}
            </div>
          </div>
        </div>

        {guidance ? (
          <p className="text-sm text-slate-600">
            {guidance.accepted.join(', ')}, up to{' '}
            {Math.round(guidance.max_bytes / 1024 / 1024)} MB, at least{' '}
            {guidance.min_long_edge_px} px on the longest side.{' '}
            {guidance.recommended} It prints at {guidance.print_width_mm} ×{' '}
            {guidance.print_height_mm} mm, so a wide logo suits the space better
            than a tall one.
          </p>
        ) : null}

        {mayEdit ? (
          <div className="flex flex-wrap items-center gap-2">
            {/* The input itself is hidden and driven by the button: the native
                control renders as "Choose File / No file chosen", which reads
                like an unfinished form next to everything else here. */}
            <input
              ref={fileInput}
              type="file"
              accept="image/png,image/jpeg,image/svg+xml"
              className="sr-only"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) void uploadLogo(file);
              }}
            />
            <Button
              variant="secondary"
              loading={saving}
              onClick={() => fileInput.current?.click()}
            >
              {logoUrl ? 'Replace logo' : 'Upload a logo'}
            </Button>
            {logoUrl ? (
              <Button variant="ghost" className="text-red-700" onClick={() => void removeLogo()}>
                Remove
              </Button>
            ) : null}
          </div>
        ) : null}
      </Card>

      <Card className="flex flex-col gap-4">
        <h2 className="text-base font-semibold text-slate-900">Company details</h2>

        <Field label="Name" htmlFor="org-name" hint="How you are known day to day.">
          <Input
            id="org-name"
            value={form.name}
            disabled={!mayEdit}
            onChange={(event) => setForm({ ...form, name: event.target.value })}
          />
        </Field>

        <Field
          label="Registered name"
          htmlFor="org-legal"
          hint="If it differs from the above, this is what prints on documents."
        >
          <Input
            id="org-legal"
            value={form.legal_name}
            disabled={!mayEdit}
            onChange={(event) => setForm({ ...form, legal_name: event.target.value })}
          />
        </Field>

        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Email" htmlFor="org-email">
            <Input
              id="org-email"
              value={form.email}
              disabled={!mayEdit}
              onChange={(event) => setForm({ ...form, email: event.target.value })}
            />
          </Field>
          <Field label="Phone" htmlFor="org-phone">
            <Input
              id="org-phone"
              value={form.phone}
              disabled={!mayEdit}
              onChange={(event) => setForm({ ...form, phone: event.target.value })}
            />
          </Field>
        </div>

        <Field
          label="Address"
          htmlFor="org-address"
          hint="Printed in the header, where a client or a police check reads it."
        >
          <Input
            id="org-address"
            value={form.address}
            disabled={!mayEdit}
            onChange={(event) => setForm({ ...form, address: event.target.value })}
          />
        </Field>

        <Field label="KRA PIN" htmlFor="org-pin">
          <Input
            id="org-pin"
            value={form.tax_pin}
            disabled={!mayEdit}
            onChange={(event) => setForm({ ...form, tax_pin: event.target.value })}
          />
        </Field>

        {mayEdit ? (
          <div>
            <Button onClick={() => void saveDetails()} loading={saving}>
              Save company details
            </Button>
          </div>
        ) : null}
      </Card>
    </div>
  );
}
