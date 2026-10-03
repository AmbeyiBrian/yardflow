/**
 * Ticking the load at release (P11, G1, design §4.15.8), shared by the online
 * and the offline release sheets.
 *
 * Every reading from the continuous scanner goes through `matchScan` on the
 * device, so it behaves the same with no signal. The hook only holds state;
 * the rules (what a tick means for a quantity, what the payload looks like)
 * are the pure functions in `loadScan.ts`.
 */
import { useCallback, useMemo, useState } from 'react';

import { decodeTokenPayload } from '../dispatch/passScan';
import {
  applyTicks,
  describeScan,
  emptyTicks,
  tickedCount,
  toLoadLines,
  toScanPass,
  untickLine,
  untickUnit,
  type LoadLine,
  type LoadLineInput,
  type LoadNotice,
} from './loadScan';
import { matchScan } from './matchScan';
import type { LabelReading } from './readLabel';
import type { Id, Refusal, ScanResult, Tick, TickState } from './types';

export interface LoadScan {
  /** The pass's lines in the shape the sheets need. */
  lines: LoadLine[];
  state: TickState;
  /** `PassSerial.id`s that are ticked. */
  tickedSerials: ReadonlySet<Id>;
  /** Non-serialized lines ticked by a box scan. */
  tickedLines: ReadonlySet<Id>;
  /** Units and box lines ticked: the scanner's counter. */
  count: number;
  /** The latest outcome, as words for the screen. */
  notice: LoadNotice | null;
  /** Everything refused this session, newest first. */
  refusals: Refusal[];
  /** Feed a scanner reading; returns the result so a sheet can react to line ticks. */
  read: (reading: LabelReading) => ScanResult;
  untickUnit: (serialId: Id) => void;
  untickLine: (lineId: Id) => void;
  reset: () => void;
}

export function useLoadScan({
  lines: input,
  passId,
  lineName,
}: {
  lines: LoadLineInput[];
  passId: Id;
  /** Name for a ticked bulk line, for the on-screen message. */
  lineName?: (lineId: Id) => string;
}): LoadScan {
  const scanPass = useMemo(() => toScanPass(input), [input]);
  const lines = useMemo(() => toLoadLines(input), [input]);

  const [state, setState] = useState<TickState>(emptyTicks);
  const [notice, setNotice] = useState<LoadNotice | null>(null);
  const [refusals, setRefusals] = useState<Refusal[]>([]);

  const read = useCallback(
    (reading: LabelReading): ScanResult => {
      const result = matchScan(scanPass, reading.raw, state);
      const names = (tick: Tick) =>
        tick.kind === 'unit' ? tick.serialNumber : (lineName?.(tick.lineId) ?? 'a box');

      if (result.ticks.length > 0) {
        setState((current) => applyTicks(current, result.ticks));
      }
      setNotice(describeScan(result, names));
      if (result.refused) {
        const refusal = result.refused;
        setRefusals((current) => [refusal, ...current]);
      }

      // Whose pass it is needs the token decoded, which is async. Say that it
      // is a pass at once and refine the words when the answer arrives.
      if (result.outcome === 'gate_pass' && result.documentToken) {
        void decodeTokenPayload(result.documentToken).then((payload) => {
          const known = payload && /gate/i.test(payload.type);
          if (!known) return;
          setNotice(describeScan(result, names, String(payload.id) === String(passId)));
        });
      }
      return result;
    },
    [scanPass, state, passId, lineName],
  );

  return {
    lines,
    state,
    tickedSerials: state.serials,
    tickedLines: state.lines,
    count: tickedCount(lines, state),
    notice,
    refusals,
    read,
    untickUnit: (serialId) => setState((current) => untickUnit(current, serialId)),
    untickLine: (lineId) => setState((current) => untickLine(current, lineId)),
    reset: () => {
      setState(emptyTicks());
      setNotice(null);
      setRefusals([]);
    },
  };
}
