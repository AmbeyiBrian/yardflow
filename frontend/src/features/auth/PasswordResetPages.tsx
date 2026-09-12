/**
 * Password reset and invitation (requirements B2, A1).
 *
 * The same confirm screen serves both: an invited user setting their first
 * password (A1) and an existing user resetting a forgotten one (B2). One flow,
 * so no temporary password ever needs to exist.
 */

import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';

import { ApiError, api } from '../../api/client';
import { Banner, Button, Card, Field, Input } from '../../components/ui';

function AuthLayout({ title, subtitle, children }: { title: string; subtitle?: string; children: React.ReactNode }) {
  return (
    <div className="flex min-h-full items-center justify-center bg-slate-50 px-4 py-10">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <h1 className="text-2xl font-semibold text-slate-900">{title}</h1>
          {subtitle ? <p className="mt-1 text-sm text-slate-600">{subtitle}</p> : null}
        </div>
        <Card>{children}</Card>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Request a reset                                                            */
/* -------------------------------------------------------------------------- */

export function ForgotPasswordPage() {
  const [sent, setSent] = useState(false);
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<{ identifier: string }>({ defaultValues: { identifier: '' } });

  async function onSubmit(values: { identifier: string }) {
    try {
      await api.post('/auth/password-reset', { identifier: values.identifier.trim() }, { anonymous: true });
    } catch {
      // Deliberately ignored. The server reports success regardless so that this
      // screen cannot be used to discover who works at a company; showing an
      // error here would undo that.
    }
    setSent(true);
  }

  if (sent) {
    return (
      <AuthLayout title="Check your messages">
        <div className="flex flex-col gap-4">
          <Banner>
            If that account exists, we have sent a link to set a new password. It
            expires shortly, so use it soon.
          </Banner>
          <Link to="/login" className="text-center text-sm text-slate-600 underline underline-offset-4">
            Back to sign in
          </Link>
        </div>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout
      title="Reset your password"
      subtitle="We will send a link by email or SMS."
    >
      <form onSubmit={handleSubmit(onSubmit)} className="flex flex-col gap-4" noValidate>
        <Field
          label="Email or phone number"
          htmlFor="identifier"
          error={errors.identifier?.message}
        >
          <Input
            id="identifier"
            autoComplete="username"
            autoCapitalize="none"
            invalid={Boolean(errors.identifier)}
            {...register('identifier', { required: 'Enter your email address or phone number.' })}
          />
        </Field>
        <Button type="submit" block loading={isSubmitting}>
          Send the link
        </Button>
        <Link to="/login" className="text-center text-sm text-slate-600 underline underline-offset-4">
          Back to sign in
        </Link>
      </form>
    </AuthLayout>
  );
}

/* -------------------------------------------------------------------------- */
/* Set a new password                                                         */
/* -------------------------------------------------------------------------- */

interface ConfirmForm {
  password: string;
  confirm: string;
}

export function ResetPasswordPage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [formError, setFormError] = useState<string | null>(null);

  const uid = params.get('uid') ?? '';
  const token = params.get('token') ?? '';

  const {
    register,
    handleSubmit,
    watch,
    setError,
    formState: { errors, isSubmitting },
  } = useForm<ConfirmForm>({ defaultValues: { password: '', confirm: '' } });

  if (!uid || !token) {
    return (
      <AuthLayout title="That link is not valid">
        <div className="flex flex-col gap-4">
          <Banner tone="error">
            The link is missing information. Request a new one.
          </Banner>
          <Link to="/forgot-password" className="text-center text-sm underline underline-offset-4">
            Request a new link
          </Link>
        </div>
      </AuthLayout>
    );
  }

  async function onSubmit(values: ConfirmForm) {
    setFormError(null);
    try {
      await api.post(
        '/auth/password-reset/confirm',
        { uid, token, password: values.password },
        { anonymous: true },
      );
      navigate('/login', { replace: true });
    } catch (error) {
      if (error instanceof ApiError) {
        const messages = error.fieldErrors.password;
        if (messages?.length) {
          // Django's password validators come back here (T1.13).
          setError('password', { message: messages.join(' ') });
          return;
        }
        if (error.fieldErrors.token?.length) {
          setFormError(error.fieldErrors.token[0]);
          return;
        }
        setFormError(error.message);
      } else {
        setFormError('Could not reach the server. Check your connection and try again.');
      }
    }
  }

  return (
    <AuthLayout title="Set your password">
      <form onSubmit={handleSubmit(onSubmit)} className="flex flex-col gap-4" noValidate>
        <Field label="New password" htmlFor="password" error={errors.password?.message}>
          <Input
            id="password"
            type="password"
            autoComplete="new-password"
            invalid={Boolean(errors.password)}
            {...register('password', { required: 'Choose a password.' })}
          />
        </Field>

        <Field label="Confirm password" htmlFor="confirm" error={errors.confirm?.message}>
          <Input
            id="confirm"
            type="password"
            autoComplete="new-password"
            invalid={Boolean(errors.confirm)}
            {...register('confirm', {
              required: 'Type the password again.',
              validate: (value) => value === watch('password') || 'The passwords do not match.',
            })}
          />
        </Field>

        {formError ? (
          <p role="alert" className="text-sm text-red-600">
            {formError}
          </p>
        ) : null}

        <Button type="submit" block loading={isSubmitting}>
          Save and sign in
        </Button>
      </form>
    </AuthLayout>
  );
}
