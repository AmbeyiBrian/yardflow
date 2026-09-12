/**
 * Login (design §7.2; requirement B1).
 *
 * One `identifier` field, not separate email and phone fields: B1 lets staff log
 * in with either, and a technician typing their phone number should not first
 * have to tell the system what kind of thing they typed.
 */

import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { Link, useLocation, useNavigate } from 'react-router-dom';

import { ApiError } from '../../api/client';
import { Button, Card, Field, Input } from '../../components/ui';
import { Wordmark } from '../../components/Logo';
import { useSession } from '../../auth/session';

interface LoginForm {
  identifier: string;
  password: string;
}

export default function LoginPage() {
  const { login } = useSession();
  const navigate = useNavigate();
  const location = useLocation();
  const [formError, setFormError] = useState<string | null>(null);

  const {
    register,
    handleSubmit,
    setError,
    formState: { errors, isSubmitting },
  } = useForm<LoginForm>({ defaultValues: { identifier: '', password: '' } });

  // Where the guard sent them from, so they land back where they were headed.
  const intended = (location.state as { from?: string } | null)?.from ?? '/';

  async function onSubmit(values: LoginForm) {
    setFormError(null);
    try {
      await login(values.identifier.trim(), values.password);
      navigate(intended, { replace: true });
    } catch (error) {
      if (error instanceof ApiError) {
        // §6.1: field_errors map straight onto react-hook-form paths.
        const fields = error.fieldErrors;
        let attached = false;
        for (const [path, messages] of Object.entries(fields)) {
          if (path === 'identifier' || path === 'password') {
            setError(path, { message: messages[0] });
            attached = true;
          }
        }
        if (!attached) {
          setFormError(
            // The server deliberately does not say whether the account exists,
            // so neither does this.
            error.message || 'Those credentials are not correct.',
          );
        }
      } else {
        setFormError('Could not reach the server. Check your connection and try again.');
      }
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center bg-slate-50 px-4 py-10">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex flex-col items-center text-center">
          {/* The mark is the page's heading, so it carries the h1 rather than
              sitting beside a duplicate one. `labelled` gives it the accessible
              name that the discarded text used to provide. */}
          <h1 className="flex">
            <Wordmark className="h-7 w-auto text-slate-900" />
          </h1>
          <p className="mt-2 text-sm text-slate-600">Yard inventory and gate control</p>
        </div>

        <Card>
          <form onSubmit={handleSubmit(onSubmit)} className="flex flex-col gap-4" noValidate>
            <Field
              label="Email or phone number"
              htmlFor="identifier"
              hint="Whichever your company registered for you."
              error={errors.identifier?.message}
            >
              <Input
                id="identifier"
                // `username` rather than `email`: the field accepts either, and
                // an email-typed input would make a phone number look invalid
                // to the browser.
                autoComplete="username"
                inputMode="text"
                autoCapitalize="none"
                autoCorrect="off"
                invalid={Boolean(errors.identifier)}
                {...register('identifier', {
                  required: 'Enter your email address or phone number.',
                })}
              />
            </Field>

            <Field label="Password" htmlFor="password" error={errors.password?.message}>
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                invalid={Boolean(errors.password)}
                {...register('password', { required: 'Enter your password.' })}
              />
            </Field>

            {formError ? (
              <p role="alert" className="text-sm text-red-600">
                {formError}
              </p>
            ) : null}

            <Button type="submit" block loading={isSubmitting}>
              Sign in
            </Button>

            <Link
              to="/forgot-password"
              className="text-center text-sm text-slate-600 underline underline-offset-4"
            >
              Forgotten your password?
            </Link>
          </form>
        </Card>
      </div>
    </div>
  );
}
