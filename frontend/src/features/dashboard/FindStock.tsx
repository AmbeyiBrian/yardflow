/**
 * Find stock, at the top of Home (E8, design §7.3d).
 *
 * "Do we have it, and where?" is the first question of most days, so the answer
 * starts here rather than two taps away on Stock.
 *
 * **One field, not two.** `BarcodeScanner` always carries its own typed field
 * (it is never behind a fallback), so this card uses that field and the
 * scanner's camera button, and lists matching items beneath. A second input
 * above it would put two boxes on the phone's first screen that both say
 * "type here". The scanner reports each keystroke through `onDraft` (which
 * drives the list) and an Enter or a camera read through `onScan`; a read that
 * arrives while text is typed is an Enter.
 */

import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { api } from '../../api/client';
import { useResource } from '../../api/hooks';
import { BarcodeScanner } from '../../components/BarcodeScanner';
import { Banner, Card, Spinner } from '../../components/ui';
import { useOffline } from '../../offline/OfflineProvider';
import { resolveFind, searchable, type FindVia } from './findStockLogic';

interface FoundItem {
  id: number;
  code: string;
  name: string;
  unit: string;
  on_hand: string;
}

const DEBOUNCE_MS = 250;

export function FindStock() {
  const navigate = useNavigate();
  const { online } = useOffline();
  const [draft, setDraft] = useState('');
  const [debounced, setDebounced] = useState('');
  const [message, setMessage] = useState<string | null>(null);
  const draftRef = useRef('');
  const countRef = useRef(0);
  const enteredRef = useRef(false);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(draft.trim()), DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [draft]);

  const wanted = online && searchable(debounced);
  const found = useResource<{ results: FoundItem[] }>(
    'stock/find',
    { q: debounced },
    { enabled: wanted, retry: false },
  );
  const items = wanted ? (found.data?.results ?? []) : [];
  const settling = draft.trim() !== debounced;
  useEffect(() => {
    countRef.current = items.length;
  });

  async function onScan(value: string) {
    // Typed text still present means this was Enter and not the camera.
    const via: FindVia = draftRef.current.trim() ? 'enter' : 'scan';
    enteredRef.current = via === 'enter';
    if (!navigator.onLine) {
      setMessage('Searching needs a connection.');
      return;
    }
    setMessage(null);
    let resource: string | null = null;
    try {
      const hit = await api.get<{ kind: string; resource: string }>(
        `/stock/lookup?q=${encodeURIComponent(value)}`,
      );
      resource = hit.resource;
    } catch {
      // A 404 reads the same as another tenant's identifier (§2.4).
    }
    const outcome = resolveFind(via, value, resource, countRef.current);
    if (outcome.kind === 'navigate') navigate(outcome.to);
    else setMessage(outcome.text);
  }

  const offline = !online;
  const typed = searchable(draft);
  const showEmpty =
    wanted && !settling && !found.isFetching && !found.isError && items.length === 0;

  return (
    <Card className="flex flex-col gap-2">
      <BarcodeScanner
        label="Find stock"
        onScan={(value) => void onScan(value)}
        onDraft={(value) => {
          // The scanner empties its field right after an Enter. The list stays,
          // so an Enter that found no unit still shows the items.
          if (value === '' && enteredRef.current) {
            enteredRef.current = false;
            draftRef.current = '';
            return;
          }
          draftRef.current = value;
          setDraft(value);
          setMessage(null);
        }}
      />

      {offline ? (
        <p className="text-sm text-slate-600" role="status">
          Searching needs a connection.
        </p>
      ) : null}
      {message ? <Banner tone="warning">{message}</Banner> : null}
      {wanted && found.isError ? (
        <Banner tone="warning">Could not search just now. Try again.</Banner>
      ) : null}

      {!offline && typed && (settling || found.isFetching) && items.length === 0 ? (
        <Spinner className="size-4 text-slate-400" />
      ) : null}

      {items.length > 0 ? (
        <ul className="m-0 flex list-none flex-col divide-y divide-slate-200 p-0">
          {items.map((item) => {
            const none = Number(item.on_hand) <= 0;
            return (
              <li key={item.id}>
                <button
                  type="button"
                  onClick={() => navigate(`/stock?item=${item.id}`)}
                  className="flex min-h-[44px] w-full items-center justify-between gap-3 py-1.5 text-left"
                >
                  <span className="min-w-0">
                    <span className="block break-words font-semibold text-slate-900">
                      {item.name}
                    </span>
                    <span className="block break-words text-sm text-slate-500">{item.code}</span>
                  </span>
                  <span
                    className={`shrink-0 text-sm tabular-nums ${
                      none ? 'text-slate-500' : 'font-medium text-slate-900'
                    }`}
                  >
                    {none ? 'none in stock' : `${item.on_hand} ${item.unit}`}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      ) : null}

      {showEmpty && !message ? (
        <p className="text-sm text-slate-600" role="status">
          {`Nothing here matches "${debounced}".`}
        </p>
      ) : null}
    </Card>
  );
}
