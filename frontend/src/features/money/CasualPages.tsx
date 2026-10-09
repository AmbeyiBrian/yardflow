/**
 * Registering a casual worker (Epic R, R3; design §4.17.2, §4.17.10).
 *
 * A casual is paid cash and is not a user, so the register is the only record
 * of who they are: name, ID number, phone, and a photo of the ID. The ID number
 * is the identity — the server refuses a second casual with the same one
 * (CASUAL_ID_DUPLICATE) and names the existing person, which is shown as sent.
 *
 * `CasualForm` is shared: this page uses it standalone, and the expense form
 * opens it in a sheet so a clerk does not leave a half-filled expense.
 */

import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { useNavigate } from 'react-router-dom';

import { newUuid } from '../../offline/db';
import { PhotoCapture } from '../../components/PhotoCapture';
import { Banner, Button, Card, Field, Input, Spinner } from '../../components/ui';
import { PageHeader } from '../../components/ui/data';
import { useCreateCasual } from './api';
import { moneyError } from './errors';
import { normaliseIdNumber } from './rules';
import type { Casual } from './types';

interface Values {
  name: string;
  id_number: string;
  phone: string;
}

export function CasualForm({ onDone }: { onDone: (casual: Casual) => void }) {
  const [saved, setSaved] = useState<Casual | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const create = useCreateCasual();
  const form = useForm<Values>({ defaultValues: { name: '', id_number: '', phone: '' } });

  if (saved) {
    return (
      <div className="flex flex-col gap-3">
        <Banner tone="success">{saved.name} is on the register.</Banner>
        {/* After saving: an attachment needs something to hang off. */}
        <PhotoCapture
          targetType="commercials.Casual"
          targetId={String(saved.id)}
          label="Photograph the ID"
          caption="ID"
        />
        <Button block onClick={() => onDone(saved)}>
          Done
        </Button>
      </div>
    );
  }

  return (
    <form className="flex flex-col gap-3">
      {banner ? <Banner tone="error">{banner}</Banner> : null}
      <Field label="Name" htmlFor="casual-name" error={form.formState.errors.name?.message}>
        <Input id="casual-name" {...form.register('name', { required: 'What is their name?' })} />
      </Field>
      <Field
        label="ID number"
        htmlFor="casual-id"
        hint="Spaces and dashes do not matter."
        error={form.formState.errors.id_number?.message}
      >
        <Input
          id="casual-id"
          autoCapitalize="characters"
          {...form.register('id_number', { required: 'The ID number is needed.' })}
        />
      </Field>
      <Field label="Phone" htmlFor="casual-phone" hint="Optional.">
        <Input id="casual-phone" type="tel" {...form.register('phone')} />
      </Field>
      <Button
        block
        disabled={create.isPending}
        onClick={form.handleSubmit(async (values) => {
          setBanner(null);
          try {
            setSaved(
              await create.mutateAsync({
                name: values.name.trim(),
                id_number: normaliseIdNumber(values.id_number),
                phone: values.phone.trim(),
                client_uuid: newUuid(),
              }),
            );
          } catch (error) {
            setBanner(moneyError(error, form.setError));
          }
        })}
      >
        {create.isPending ? <Spinner /> : 'Add casual'}
      </Button>
    </form>
  );
}

export default function AddCasualPage() {
  const navigate = useNavigate();
  return (
    <div className="flex flex-col gap-4">
      <PageHeader title="Add a casual" subtitle="Register someone paid by the day, once." />
      <Card>
        <CasualForm onDone={() => navigate('/money?tab=casuals')} />
      </Card>
    </div>
  );
}
