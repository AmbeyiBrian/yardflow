/**
 * T3.18 — the barcode scanner (design §7.3; D7, G5).
 *
 * The criterion has two halves, and the second is the important one: "it reads a
 * printed barcode on a mid-range Android phone, **and the flow is completable
 * with the camera denied**."
 *
 * So manual entry is not a fallback that appears after an error — it is present
 * from the first render, focused and ready. A yard where the phone's camera is
 * broken, the label is torn, or the light is gone is a normal Tuesday, and a
 * scanner that becomes a dead end on any of those stops the delivery.
 *
 * Three tiers, in order of preference:
 *
 *  1. `BarcodeDetector` — native, in Chrome on Android. Fast and battery-cheap.
 *  2. `@zxing/browser` — a WASM decoder, everywhere else.
 *  3. the keyboard — always.
 *
 * The torch is offered only where the platform admits to having one, because a
 * button that does nothing is worse than no button in a dark yard.
 */

import { useCallback, useEffect, useId, useRef, useState } from 'react';

import { readLabel, type LabelReading } from '../features/boxes/readLabel';
import { AIM_FRACTION, aimRegion, pickNearestCentre } from './aimRegion';
import { valueToPass } from './scanValue';
import { Button, Field, Input } from './ui';
import { cn } from './ui/cn';

type Mode = 'idle' | 'starting' | 'scanning' | 'denied' | 'unsupported';

interface BarcodeDetectorLike {
  detect: (source: HTMLCanvasElement) => Promise<
    { rawValue: string; boundingBox?: { x: number; y: number; width: number; height: number } }[]
  >;
}

declare global {
  interface Window {
    BarcodeDetector?: {
      new (options?: { formats?: string[] }): BarcodeDetectorLike;
      getSupportedFormats?: () => Promise<string[]>;
    };
  }
}

/** The zoom steps offered, cycled in order and clipped to what the camera has. */
const ZOOM_STEPS = [1, 2, 3];

const FORMATS = ['code_128', 'code_39', 'ean_13', 'qr_code', 'data_matrix', 'itf'];

export function BarcodeScanner({
  onScan,
  onRead,
  onDraft,
  label = 'Scan or type a code',
  hint,
  autoStart = false,
  continuous = false,
  scannedCount,
}: {
  /**
   * One string, as ever: the single serial a label holds, else the trimmed raw
   * text (P3, §4.15.6). See `valueToPass`.
   */
  onScan: (value: string) => void;
  /**
   * The whole reading (serials, box code, pass token) for every result, called
   * before `onScan`, for screens that act on more than one serial.
   */
  onRead?: (reading: LabelReading) => void;
  /**
   * What is typed but not yet added.
   *
   * A code only counts once "Add" is pressed, which is right — but somebody who
   * types one and then presses the sheet's own button has done the work and
   * expects it to count. Reporting the half-finished text lets the screen
   * finish the job instead of discarding it with a message that reads like a
   * denial.
   */
  onDraft?: (value: string) => void;
  label?: string;
  hint?: string;
  /** Open the camera immediately. Off by default: most entry is typed. */
  autoStart?: boolean;
  /**
   * Keep scanning after a hit, for a delivery of many identified units.
   *
   * The camera used to close on every successful read, so receiving a sealed
   * box of twenty radios meant opening it twenty times — tap, wait for focus,
   * scan, tap again. That is most of the time a gate-in takes, and all of it is
   * spent on the phone rather than on the delivery. Left off for a lookup,
   * where one code answers the question and a camera left running is a battery
   * drain in somebody's pocket.
   */
  continuous?: boolean;
  /** How many have been accepted so far, shown on the viewfinder. */
  scannedCount?: number;
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const stopRef = useRef<(() => void) | null>(null);

  const [mode, setMode] = useState<Mode>('idle');
  const [torchOn, setTorchOn] = useState(false);
  const [hasTorch, setHasTorch] = useState(false);
  //: The camera's largest zoom, or null when it has none to offer (E10).
  const [zoomMax, setZoomMax] = useState<number | null>(null);
  const [zoom, setZoom] = useState(1);
  const [manual, setManual] = useState('');
  const [note, setNote] = useState<string | null>(null);
  //: What the label said, when it said more than the value passed on (P3).
  const [rawShown, setRawShown] = useState<string | null>(null);
  const [showRaw, setShowRaw] = useState(false);
  // Two scanners can mount on one screen, so the input id is per instance.
  const manualId = useId();
  const scanNoteRef = useRef(false);
  //: The last code accepted, so the same label under the lens is read once.
  const lastValueRef = useRef<{ value: string; at: number }>({ value: '', at: 0 });

  const stop = useCallback(() => {
    stopRef.current?.();
    stopRef.current = null;
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    setTorchOn(false);
    setZoomMax(null);
    setZoom(1);
    setMode('idle');
  }, []);

  // Releasing the camera on unmount is not tidiness: a stream left running keeps
  // the torch on and drains a phone that has to last the shift.
  useEffect(() => stop, [stop]);

  const start = useCallback(async () => {
    if (!navigator.mediaDevices?.getUserMedia) {
      setMode('unsupported');
      setNote('This browser cannot open a camera. Type the code instead.');
      return;
    }

    setMode('starting');
    setNote(null);

    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        // The rear camera, on a phone that has two.
        video: { facingMode: { ideal: 'environment' } },
        audio: false,
      });
      streamRef.current = stream;

      const video = videoRef.current;
      if (!video) return;
      video.srcObject = stream;
      await video.play();

      const track = stream.getVideoTracks()[0];
      const capabilities = track.getCapabilities?.() as
        | { torch?: boolean; zoom?: { min: number; max: number } }
        | undefined;
      setHasTorch(Boolean(capabilities?.torch));
      // Zoom is offered only where the camera admits to it, like the torch. It
      // lets a small code on a crowded label fill the box (E10).
      const maxZoom = capabilities?.zoom?.max;
      setZoomMax(typeof maxZoom === 'number' && maxZoom > 1 ? maxZoom : null);
      setZoom(1);

      setMode('scanning');

      if (window.BarcodeDetector) {
        stopRef.current = runNativeDetector(video, handleResult, continuous);
      } else {
        stopRef.current = await runZxing(video, handleResult, continuous);
      }
    } catch (error) {
      // A denial is the expected case, not an exception: the flow has to finish
      // without the camera, and saying so plainly is part of that.
      const denied =
        error instanceof DOMException &&
        (error.name === 'NotAllowedError' || error.name === 'SecurityError');
      setMode(denied ? 'denied' : 'unsupported');
      setNote(
        denied
          ? 'The camera is not available. Type the code below — everything works the same.'
          : 'The camera could not be started. Type the code below instead.',
      );
    }
    // handleResult is stable for the life of the component.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [continuous]);

  // Every result, camera or keyboard, is read the same way (P3, §4.15.6): the
  // scanner reports what the label said, then passes on the serial it found.
  // The camera loop captures `deliver` once, when it starts, and keeps it for
  // the whole session. Reading the callbacks through refs means it always
  // reaches the screen's *current* handlers -- a gate-in that switches to a new
  // box mid-session must not keep filling the old one.
  const onScanRef = useRef(onScan);
  const onReadRef = useRef(onRead);
  useEffect(() => {
    onScanRef.current = onScan;
    onReadRef.current = onRead;
  });

  const deliver = useCallback(
    (value: string, continuousNote: boolean) => {
      const reading = readLabel(value);
      const passed = valueToPass(reading);
      const raw = reading.raw.trim();
      onReadRef.current?.(reading);
      onScanRef.current(passed);
      setShowRaw(false);
      if (passed !== raw) {
        setRawShown(raw);
        setNote(`Scanned ${passed} from the label.`);
      } else {
        // A plain read leaves other notes (camera denied) alone; it only
        // retires a "from the label" note that no longer describes the scan.
        if (continuousNote) setNote(`Scanned ${passed}.`);
        else if (scanNoteRef.current) setNote(null);
        setRawShown(null);
      }
      scanNoteRef.current = passed !== raw || continuousNote;
    },
    [],
  );

  const handleResult = useCallback(
    (value: string) => {
      if (!value.trim()) return;
      // Compare the extracted value, so the same label read twice is still one
      // read however long its raw text.
      const cleaned = valueToPass(readLabel(value));

      if (continuous) {
        // The same label sits in front of the lens for a second or two and
        // would otherwise be read thirty times. Suppressing a repeat by value
        // rather than by time also means a genuine second unit with the same
        // code — which is a duplicate the server will refuse anyway — does not
        // silently vanish here.
        const last = lastValueRef.current;
        if (last.value === cleaned && Date.now() - last.at < 2500) return;
        lastValueRef.current = { value: cleaned, at: Date.now() };
        navigator.vibrate?.(40);
        deliver(value, true);
        return;
      }

      // A short buzz, where the device has one: in a noisy yard it is the only
      // feedback that lands.
      navigator.vibrate?.(40);
      stop();
      deliver(value, false);
    },
    [continuous, deliver, stop],
  );

  useEffect(() => {
    if (autoStart) void start();
  }, [autoStart, start]);

  async function toggleTorch() {
    const track = streamRef.current?.getVideoTracks()[0];
    if (!track) return;
    try {
      await track.applyConstraints({
        advanced: [{ torch: !torchOn } as MediaTrackConstraintSet],
      });
      setTorchOn(!torchOn);
    } catch {
      setHasTorch(false);
    }
  }

  async function cycleZoom() {
    const track = streamRef.current?.getVideoTracks()[0];
    if (!track || zoomMax === null) return;
    // The next step above the current one that the camera can reach, else
    // back to 1x.
    const next = ZOOM_STEPS.find((step) => step > zoom && step <= zoomMax) ?? 1;
    try {
      await track.applyConstraints({ advanced: [{ zoom: next } as MediaTrackConstraintSet] });
      setZoom(next);
    } catch {
      setZoomMax(null);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <div className={cn('relative overflow-hidden rounded-xl bg-slate-900', mode === 'idle' && 'hidden')}>
        {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
        <video
          ref={videoRef}
          className="aspect-square w-full object-cover"
          playsInline
          muted
        />
        {/* A square guide, because yard labels are QR codes and a letterbox
            frame tells the hand to line up a strip that is not there. It is
            also the decoder's whole field of view (E10): a palm-sized label
            carries several codes, and only the one inside the box is read. The
            dimmed surround says the rest of the picture is ignored. */}
        {mode === 'scanning' ? (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center overflow-hidden">
            <div
              className="relative aspect-square rounded-lg border-2 border-white/80 shadow-[0_0_0_9999px_rgba(0,0,0,0.45)]"
              style={{ width: `${AIM_FRACTION * 100}%` }}
            >
              <span className="absolute left-1/2 top-1/2 h-4 w-0.5 -translate-x-1/2 -translate-y-1/2 bg-white/80" />
              <span className="absolute left-1/2 top-1/2 h-0.5 w-4 -translate-x-1/2 -translate-y-1/2 bg-white/80" />
            </div>
          </div>
        ) : null}
        {mode === 'scanning' ? (
          <p className="pointer-events-none absolute inset-x-0 bottom-0 p-2 text-center text-sm font-medium text-white">
            Put the code inside the box
          </p>
        ) : null}
        {/* A running count on the viewfinder, because the whole point of
            keeping the camera open is that nobody looks away from the box. */}
        {mode === 'scanning' && continuous && scannedCount !== undefined ? (
          <div className="pointer-events-none absolute inset-x-0 top-0 flex justify-center p-2">
            <span className="rounded-full bg-slate-900/80 px-3 py-1 text-sm font-medium text-white">
              {scannedCount} scanned
            </span>
          </div>
        ) : null}
      </div>

      <div className="flex flex-wrap gap-2">
        {mode === 'scanning' || mode === 'starting' ? (
          <>
            <Button variant="secondary" onClick={stop}>
              {continuous ? 'Done scanning' : 'Stop camera'}
            </Button>
            {hasTorch ? (
              <Button variant="secondary" onClick={toggleTorch}>
                {torchOn ? 'Light off' : 'Light on'}
              </Button>
            ) : null}
            {zoomMax !== null ? (
              <Button variant="secondary" onClick={cycleZoom}>
                Zoom {zoom}×
              </Button>
            ) : null}
          </>
        ) : (
          <Button variant="secondary" onClick={() => void start()}>
            Scan QR code
          </Button>
        )}
      </div>

      {note ? (
        <div className="text-sm text-amber-800">
          <p>{note}</p>
          {rawShown !== null ? (
            <>
              <button
                type="button"
                className="mt-1 text-xs underline"
                aria-expanded={showRaw}
                onClick={() => setShowRaw((open) => !open)}
              >
                {showRaw ? 'Hide what the label said' : 'Show what the label said'}
              </button>
              {showRaw ? (
                <pre className="mt-1 max-w-full whitespace-pre-wrap break-all rounded bg-slate-100 p-2 font-mono text-xs text-slate-800">
                  {rawShown}
                </pre>
              ) : null}
            </>
          ) : null}
        </div>
      ) : null}

      {/* Always present, never behind a fallback. */}
      <form
        className="flex items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          const typed = manual.trim();
          if (!typed) return;
          // Deliberately not `handleResult`: that suppresses a repeat of the
          // same value, which is right for a label sitting under the lens and
          // wrong for somebody typing. Typing the same code twice is a
          // statement, and the screen above should answer it.
          navigator.vibrate?.(40);
          // A scanner gun types into this form and presses Enter, so it is
          // read like a camera result (P3); only the repeat check is skipped.
          deliver(typed, false);
          setManual('');
          onDraft?.('');
        }}
      >
        <div className="flex-1">
          <Field label={label} htmlFor={manualId} hint={hint}>
            <Input
              id={manualId}
              value={manual}
              autoComplete="off"
              // Not `type="number"`: serials contain letters, and a numeric
              // keypad on an alphanumeric code is a trap.
              inputMode="text"
              placeholder="Serial, asset tag or drum number"
              onChange={(event) => {
                setManual(event.target.value);
                onDraft?.(event.target.value);
              }}
            />
          </Field>
        </div>
        <Button type="submit" disabled={!manual.trim()}>
          Add
        </Button>
      </form>
    </div>
  );
}

/**
 * One decode loop for both decoders (E10).
 *
 * Every ~120 ms, the centre of the video is drawn onto an offscreen canvas and
 * only that canvas is decoded. Decoding the whole frame read whichever code on
 * a crowded label the decoder met first; the region is what makes the drawn box
 * mean something. It is drawn at 2x because a small code in a 40% window is
 * only a few hundred pixels across, and the decoders do better with more.
 * ~8/second is plenty and leaves the CPU alone.
 */
function runAimedLoop(
  video: HTMLVideoElement,
  decode: (canvas: HTMLCanvasElement) => Promise<string | null> | string | null,
  onResult: (value: string) => void,
  keepScanning: boolean,
) {
  const canvas = document.createElement('canvas');
  const context = canvas.getContext('2d', { willReadFrequently: true });
  let cancelled = false;

  const tick = async () => {
    if (cancelled) return;
    try {
      if (context && video.videoWidth > 0) {
        const { sx, sy, size } = aimRegion(video.videoWidth, video.videoHeight, AIM_FRACTION);
        canvas.width = canvas.height = size * 2;
        context.imageSmoothingEnabled = true;
        context.drawImage(video, sx, sy, size, size, 0, 0, canvas.width, canvas.height);
        const value = await decode(canvas);
        if (cancelled) return;
        if (value) {
          onResult(value);
          // Stopping here is what makes a single-shot scan single-shot. When
          // the caller wants a run of units, the loop carries on and the repeat
          // suppression upstream decides what counts.
          if (!keepScanning) return;
        }
      }
    } catch {
      // A single failed frame is normal — motion blur, the camera settling, or
      // (ZXing) nothing in the box at all.
    }
    if (!cancelled) window.setTimeout(tick, 120);
  };
  void tick();

  return () => {
    cancelled = true;
  };
}

/** The native detector, in Chrome on Android. */
function runNativeDetector(
  video: HTMLVideoElement,
  onResult: (value: string) => void,
  keepScanning = false,
) {
  const detector = new window.BarcodeDetector!({ formats: FORMATS });
  return runAimedLoop(
    video,
    async (canvas) => {
      const found = await detector.detect(canvas);
      // Several codes can still sit inside the box; the one nearest its centre
      // is the one the hand is pointing at.
      return pickNearestCentre(found, canvas.width, canvas.height)?.rawValue ?? null;
    },
    onResult,
    keepScanning,
  );
}

/** The WASM decoder, for browsers with no native detector (iPhone Safari). */
async function runZxing(
  video: HTMLVideoElement,
  onResult: (value: string) => void,
  keepScanning = false,
) {
  // Imported here rather than at module scope so the decoder is downloaded only
  // by a device that needs it — it is far larger than this component (N-1).
  const { BrowserMultiFormatReader } = await import('@zxing/browser');
  const reader = new BrowserMultiFormatReader();
  return runAimedLoop(
    video,
    // Throws NotFoundException on an empty frame; the loop treats that as one.
    (canvas) => reader.decodeFromCanvas(canvas).getText(),
    onResult,
    keepScanning,
  );
}
