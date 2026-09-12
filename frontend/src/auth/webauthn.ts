/**
 * Fingerprint enrolment and approval step-up in the browser (design §5.3, §7.2;
 * B5, F4, T8.10).
 *
 * T8.10's criterion has two halves and the second is the one that decides the
 * design: "an owner approves with a fingerprint on Android Chrome, **and the
 * same flow falls back cleanly on a desktop without a sensor**."
 *
 * So every function here answers "can this device do it?" before offering
 * anything, and `signApproval` returns `null` rather than throwing when there is
 * no sensor or no enrolled credential. A refusal that reads as an error would
 * teach an owner that the fingerprint is broken; returning null lets the caller
 * approve with a password and say nothing.
 *
 * The base64url conversions are here rather than inline because WebAuthn's JSON
 * shape is base64url while its JavaScript API is `ArrayBuffer`, and getting a
 * conversion wrong produces a verification failure on the server that is
 * impossible to diagnose from the message.
 */

import { api } from '../api/client';

/**
 * Say what happened in words somebody can act on.
 *
 * WebAuthn failures arrive as a `DOMException` whose message is written for
 * browser engineers. The one that prompted this reads:
 *
 *   "The relying party ID is not a registrable domain suffix of, nor equal to
 *   the current domain. Subsequently, an attempt to fetch the
 *   .well-known/webauthn resource of the claimed RP ID failed."
 *
 * — in front of somebody who was trying to add their fingerprint. The `name` on
 * the exception is the reliable part; the message is not, so it is only used to
 * separate the two kinds of `SecurityError`.
 */
export function describeWebAuthnFailure(error: unknown): string {
  const name = error instanceof Error ? error.name : '';
  const detail = error instanceof Error ? error.message : String(error);

  switch (name) {
    case 'NotAllowedError':
    case 'AbortError':
      // Also what a timeout looks like — the browser deliberately does not say
      // which, so neither does this.
      return 'No fingerprint was given, so nothing was set up. You can try again.';

    case 'InvalidStateError':
      return 'This device is already set up for approvals. There is nothing more to do here.';

    case 'NotSupportedError':
      return 'This device has no fingerprint or face unlock that this browser can use. You can still approve with your password.';

    case 'SecurityError':
      // Two very different causes wear the same name.
      if (/relying party|rp id|registrable/i.test(detail)) {
        return 'Fingerprint approval is not available on this web address. Everything else works normally, and you can approve with your password. If you administer this system, the fingerprint domain needs to match the address in the browser.';
      }
      return 'This page is not on a secure connection, so the browser will not allow fingerprint setup.';

    case 'ConstraintError':
      return 'Your device would not confirm it is you — check that a fingerprint, face or PIN is set up on the device itself.';

    default:
      return detail || 'The fingerprint step did not complete. You can approve with your password.';
  }
}

/** Whether this browser can do platform authentication at all. */
export async function biometricsAvailable(): Promise<boolean> {
  if (typeof window === 'undefined' || !window.PublicKeyCredential) return false;
  try {
    // The specific question: is there a *platform* authenticator — a fingerprint
    // sensor or face unlock — rather than a security key somebody might own.
    // D9's step-up is meant to be one tap on the phone already in their hand.
    return await window.PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable();
  } catch {
    return false;
  }
}

function fromBase64Url(value: string): ArrayBuffer {
  const padded = value.replace(/-/g, '+').replace(/_/g, '/');
  const binary = atob(padded + '='.repeat((4 - (padded.length % 4)) % 4));
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes.buffer;
}

function toBase64Url(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

interface ServerOptions {
  challenge: string;
  rp?: { id: string; name: string };
  user?: { id: string; name: string; displayName: string };
  pubKeyCredParams?: PublicKeyCredentialParameters[];
  timeout?: number;
  excludeCredentials?: { id: string; type: string }[];
  allowCredentials?: { id: string; type: string }[];
  authenticatorSelection?: AuthenticatorSelectionCriteria;
  attestation?: AttestationConveyancePreference;
  userVerification?: UserVerificationRequirement;
}

/** B5, T8.8: enrol this device. Returns the label the server stored. */
export async function enrolDevice(deviceLabel: string): Promise<string> {
  const begin = await api.post<{ options: string }>(
    '/auth/webauthn/register/begin',
    { device_label: deviceLabel },
  );
  const options = JSON.parse(begin.options) as ServerOptions;

  let credential: PublicKeyCredential | null;
  try {
    credential = (await navigator.credentials.create({
      publicKey: {
        ...options,
        challenge: fromBase64Url(options.challenge),
        user: {
          ...options.user!,
          id: fromBase64Url(options.user!.id),
        },
        excludeCredentials: (options.excludeCredentials ?? []).map((entry) => ({
          ...entry,
          id: fromBase64Url(entry.id),
          type: 'public-key' as const,
        })),
      } as PublicKeyCredentialCreationOptions,
    })) as PublicKeyCredential | null;
  } catch (failure) {
    // Translated here rather than at the screen, so every caller of this gets
    // the readable version and none of them has to know what a relying party is.
    throw new Error(describeWebAuthnFailure(failure));
  }

  if (!credential) {
    throw new Error('Enrolment was cancelled.');
  }

  const attestation = credential.response as AuthenticatorAttestationResponse;
  const stored = await api.post<{ device_label: string }>(
    '/auth/webauthn/register/complete',
    {
      device_label: deviceLabel,
      credential: {
        id: credential.id,
        rawId: toBase64Url(credential.rawId),
        type: credential.type,
        response: {
          clientDataJSON: toBase64Url(attestation.clientDataJSON),
          attestationObject: toBase64Url(attestation.attestationObject),
        },
      },
    },
  );
  return stored.device_label;
}

/**
 * T8.9, T8.10: sign one approval with a fingerprint.
 *
 * Returns the assertion to attach to the approve call, or `null` when this
 * device cannot do it — no sensor, nothing enrolled, or the user dismissed the
 * prompt. The caller then approves with a password, which is the clean fallback
 * the criterion asks for.
 *
 * `approvalRequestId` is not optional and not inferred: the challenge is bound to
 * it server-side so an assertion cannot be replayed against another document
 * (T8.9), and passing the wrong one fails rather than silently authorising the
 * wrong pass.
 */
export async function signApproval(
  approvalRequestId: number,
): Promise<Record<string, unknown> | null> {
  if (!(await biometricsAvailable())) return null;

  let options: ServerOptions;
  try {
    const begin = await api.post<{ options: string }>(
      '/auth/webauthn/assert/begin',
      { approval_request: approvalRequestId },
    );
    options = JSON.parse(begin.options) as ServerOptions;
  } catch {
    // Commonest cause: nothing enrolled on this account. Not an error worth
    // showing — the password path is right there.
    return null;
  }

  let assertion: PublicKeyCredential | null;
  try {
    assertion = (await navigator.credentials.get({
      publicKey: {
        ...options,
        challenge: fromBase64Url(options.challenge),
        allowCredentials: (options.allowCredentials ?? []).map((entry) => ({
          ...entry,
          id: fromBase64Url(entry.id),
          type: 'public-key' as const,
        })),
      } as PublicKeyCredentialRequestOptions,
    })) as PublicKeyCredential | null;
  } catch {
    // The user dismissed the prompt, or the sensor failed. Falling back is
    // correct: refusing to approve at all would make the biometric a hurdle
    // rather than a step-up.
    return null;
  }

  if (!assertion) return null;

  const response = assertion.response as AuthenticatorAssertionResponse;
  return {
    id: assertion.id,
    rawId: toBase64Url(assertion.rawId),
    type: assertion.type,
    response: {
      clientDataJSON: toBase64Url(response.clientDataJSON),
      authenticatorData: toBase64Url(response.authenticatorData),
      signature: toBase64Url(response.signature),
      userHandle: response.userHandle ? toBase64Url(response.userHandle) : null,
    },
  };
}

export interface EnrolledDevice {
  id: number;
  device_label: string;
  aaguid: string;
  is_active: boolean;
  last_used_at: string | null;
  created_at: string;
}

export async function enrolledDevices(): Promise<EnrolledDevice[]> {
  const response = await api.get<{ results: EnrolledDevice[] }>(
    '/auth/webauthn/credentials',
  );
  return response.results;
}

export async function revokeDevice(id: number): Promise<void> {
  await api.post('/auth/webauthn/credentials', { credential: id });
}
