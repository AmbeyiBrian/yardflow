/**
 * The "Scan the load" pieces both release sheets share (P11, G1, §4.15.8).
 *
 * `ScanTheLoadPanel` is the scanner, the announced outcome and the refusals.
 * `UnitTicks` is one serialized line's units with a tick each. They live apart
 * because each sheet interleaves the per-line part with its own line cards.
 */
import { BarcodeScanner } from '../../components/BarcodeScanner';
import { Banner } from '../../components/ui';
import type { LabelReading } from './readLabel';
import { openUnits, tickedUnits, type LoadLine } from './loadScan';
import type { LoadScan } from './useLoadScan';

export function ScanTheLoadPanel({
  scan,
  required,
  onRead,
}: {
  scan: LoadScan;
  required: boolean;
  /** Called with every reading; the sheet feeds `scan.read` and reacts to ticks. */
  onRead: (reading: LabelReading) => void;
}) {
  return (
    <section className="flex flex-col gap-2 rounded-lg border border-slate-200 p-3">
      <h3 className="text-sm font-semibold text-slate-900">Scan the load</h3>
      <p className="text-sm text-slate-600">
        {required
          ? 'This organization requires every serialized unit to be scanned. Scan each unit, or its box or pallet.'
          : 'Scan each unit, or its box or pallet, as it goes on the vehicle. Anything not scanned can still be confirmed by hand below.'}
      </p>

      <BarcodeScanner
        continuous
        scannedCount={scan.count}
        label="Scan or type a serial, box or pallet code"
        onRead={onRead}
        onScan={() => {
          /* everything is handled from the whole reading in onRead */
        }}
      />

      {/* Announced, so the person with the phone in one hand and a unit in the
          other does not have to look at the screen to know it counted. */}
      <div aria-live="polite" role="status" className="min-h-[1.25rem] text-sm">
        {scan.notice ? (
          <span
            className={
              scan.notice.tone === 'good'
                ? 'text-emerald-700'
                : scan.notice.tone === 'bad'
                  ? 'font-medium text-red-700'
                  : 'text-slate-700'
            }
          >
            {scan.notice.message}
          </span>
        ) : null}
      </div>

      {scan.refusals.length > 0 ? (
        <Banner tone="warning">
          <p className="font-medium">Not on this pass ({scan.refusals.length})</p>
          <ul className="mt-1 list-disc pl-5">
            {scan.refusals.map((refusal, index) => (
              <li key={`${index}-${refusal.scanned}`} className="break-words">
                {refusal.message}
              </li>
            ))}
          </ul>
        </Banner>
      ) : null}
    </section>
  );
}

export function UnitTicks({ line, scan }: { line: LoadLine; scan: LoadScan }) {
  const units = openUnits(line);
  if (units.length === 0) return null;
  const done = tickedUnits(line, scan.state).length;
  return (
    <div className="mt-2">
      <p className="text-sm font-medium text-slate-800">
        {done} of {units.length} scanned
      </p>
      <ul className="mt-1 flex flex-col">
        {units.map((unit) => {
          const ticked = scan.tickedSerials.has(unit.id);
          return (
            <li key={String(unit.id)}>
              <label className="flex min-h-[44px] items-center gap-3">
                <input
                  type="checkbox"
                  className="size-5 shrink-0 accent-slate-900"
                  checked={ticked}
                  // Ticking by hand is not offered: a unit counts when it is
                  // scanned. Unticking is, for a unit that was scanned in error.
                  disabled={!ticked}
                  aria-label={`${unit.serial_number} scanned`}
                  onChange={() => scan.untickUnit(unit.id)}
                />
                <span className="min-w-0 break-all font-mono text-xs text-slate-700">
                  {unit.serial_number}
                  {unit.asset_tag ? ` · ${unit.asset_tag}` : ''}
                </span>
              </label>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
