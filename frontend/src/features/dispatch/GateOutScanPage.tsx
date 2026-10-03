/**
 * Scan a gate pass to open it (P11, G5, design §4.15.8; T11.17).
 *
 * The guard at the gate holds a printed pass. Finding it in a list by number is
 * the slow part of a release, so the pass's own QR opens it: online through the
 * server (which checks the token and the tenant), offline against the passes
 * already downloaded to this device (N3).
 */

import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';

import { api, ApiError } from '../../api/client';
import { BarcodeScanner } from '../../components/BarcodeScanner';
import { Banner } from '../../components/ui';
import { PageHeader } from '../../components/ui/data';
import { findReleasable, readReleasable } from '../../offline/db';
import { useOffline } from '../../offline/OfflineProvider';
import type { LabelReading } from '../boxes/readLabel';
import { decodeTokenPayload, looksLikePassNumber, normalisePassNumber } from './passScan';
import type { GateOut } from './types';

interface ScanResult {
  type: string;
  id: string | number;
  number: string;
  status: string;
  resource: string;
}

type Problem = { text: string; link?: { to: string; label: string } };

const NOT_A_PASS: Problem = { text: 'That is not a gate pass.' };
const NOT_CACHED: Problem = {
  text: 'That pass is not among the passes saved on this device. Sync when you are back online, then scan it again.',
};

export default function GateOutScanPage() {
  const navigate = useNavigate();
  const { online } = useOffline();
  const [problem, setProblem] = useState<Problem | null>(null);
  const [busy, setBusy] = useState(false);

  async function openOnline(token: string) {
    try {
      const found = await api.get<ScanResult>(`/qr/scan?token=${encodeURIComponent(token)}`);
      if (found.type === 'dispatch.GateOut') {
        navigate(`/gate-out/${found.id}?release=1`);
      } else if (found.type === 'receiving.GateIn') {
        setProblem({
          text: `${found.number} is a delivery note, not a gate pass.`,
          link: { to: `/gate-in/${found.id}`, label: 'Open the delivery note' },
        });
      } else {
        setProblem(NOT_A_PASS);
      }
    } catch (error) {
      setProblem(
        error instanceof ApiError && error.status === 404
          ? { text: 'That code is not a gate pass for this organization.' }
          : { text: error instanceof Error ? error.message : 'Could not check that code.' },
      );
    }
  }

  async function openOffline(token: string) {
    // The signature is not checked here: only the server can. The id only looks
    // in passes the server approved and downloaded, and it validates the release
    // when it syncs.
    const payload = await decodeTokenPayload(token);
    if (!payload || payload.type !== 'dispatch.GateOut') {
      setProblem(NOT_A_PASS);
      return;
    }
    const cached = await findReleasable(Number(payload.id));
    if (cached) navigate(`/gate-out/offline?pass=${cached.id}`);
    else setProblem(NOT_CACHED);
  }

  async function openByNumber(text: string) {
    if (!looksLikePassNumber(text)) {
      setProblem(NOT_A_PASS);
      return;
    }
    const number = normalisePassNumber(text);
    if (online) {
      const page = await api.get<{ results: GateOut[] }>(
        `/gate-outs?search=${encodeURIComponent(number)}&page_size=20`,
      );
      // Search is a substring match, so GP-12 also finds GP-123: take the exact one.
      const match = page.results.find((row) => row.number.toUpperCase() === number);
      if (match) navigate(`/gate-out/${match.id}?release=1`);
      else setProblem({ text: `No gate pass numbered ${number}.` });
    } else {
      const match = (await readReleasable()).find((row) => row.number.toUpperCase() === number);
      if (match) navigate(`/gate-out/offline?pass=${match.id}`);
      else setProblem(NOT_CACHED);
    }
  }

  async function handle(reading: LabelReading) {
    setProblem(null);
    setBusy(true);
    try {
      if (reading.documentToken) {
        if (online) await openOnline(reading.documentToken);
        else await openOffline(reading.documentToken);
      } else {
        await openByNumber(reading.raw);
      }
    } catch (error) {
      setProblem({ text: error instanceof Error ? error.message : 'Could not check that code.' });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Scan a gate pass"
        subtitle="Scan the code on the printed pass, or type its number."
        actions={
          <Link
            to="/gate-out"
            className="flex min-h-[44px] items-center px-2 text-sm text-slate-700"
          >
            Back
          </Link>
        }
      />

      {!online ? (
        <Banner tone="warning">
          No connection. Only passes already saved on this device can be opened.
        </Banner>
      ) : null}

      {problem ? (
        <Banner tone="error">
          {problem.text}
          {problem.link ? (
            <>
              {' '}
              <Link to={problem.link.to} className="underline">
                {problem.link.label}
              </Link>
            </>
          ) : null}
        </Banner>
      ) : null}

      {busy ? <p className="text-sm text-slate-500">Checking…</p> : null}

      <BarcodeScanner
        autoStart
        label="Scan or type a pass number"
        onScan={() => undefined}
        onRead={(reading) => void handle(reading)}
      />
    </div>
  );
}
