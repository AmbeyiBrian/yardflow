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

import { useCallback, useEffect, useRef, useState } from 'react';

import { Button, Field, Input } from './ui';
import { cn } from './ui/cn';

type Mode = 'idle' | 'starting' | 'scanning' | 'denied' | 'unsupported';

interface BarcodeDetectorLike {
  detect: (source: HTMLVideoElement) => Promise<{ rawValue: string }[]>;
}

declare global {
  interface Window {
    BarcodeDetector?: {
      new (options?: { formats?: string[] }): BarcodeDetectorLike;
      getSupportedFormats?: () => Promise<string[]>;
    };
  }
}

const FORMATS = ['code_128', 'code_39', 'ean_13', 'qr_code', 'data_matrix', 'itf'];

export function BarcodeScanner({
  onScan,
  onDraft,
  label = 'Scan or type a code',
  hint,
  autoStart = false,
}: {
  onScan: (value: string) => void;
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
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const stopRef = useRef<(() => void) | null>(null);

  const [mode, setMode] = useState<Mode>('idle');
  const [torchOn, setTorchOn] = useState(false);
  const [hasTorch, setHasTorch] = useState(false);
  const [manual, setManual] = useState('');
  const [note, setNote] = useState<string | null>(null);

  const stop = useCallback(() => {
    stopRef.current?.();
    stopRef.current = null;
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    setTorchOn(false);
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
      const capabilities = track.getCapabilities?.() as { torch?: boolean } | undefined;
      setHasTorch(Boolean(capabilities?.torch));

      setMode('scanning');

      if (window.BarcodeDetector) {
        stopRef.current = runNativeDetector(video, handleResult);
      } else {
        stopRef.current = await runZxing(video, handleResult);
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
  }, []);

  const handleResult = useCallback(
    (value: string) => {
      const cleaned = value.trim();
      if (!cleaned) return;
      // A short buzz, where the device has one: in a noisy yard it is the only
      // feedback that lands.
      navigator.vibrate?.(40);
      stop();
      onScan(cleaned);
    },
    [onScan, stop],
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

  return (
    <div className="flex flex-col gap-3">
      <div className={cn('relative overflow-hidden rounded-xl bg-slate-900', mode === 'idle' && 'hidden')}>
        {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
        <video
          ref={videoRef}
          className="aspect-[4/3] w-full object-cover"
          playsInline
          muted
        />
        {mode === 'scanning' ? (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
            <div className="h-24 w-4/5 rounded-lg border-2 border-white/80" />
          </div>
        ) : null}
      </div>

      <div className="flex flex-wrap gap-2">
        {mode === 'scanning' || mode === 'starting' ? (
          <>
            <Button variant="secondary" onClick={stop}>
              Stop camera
            </Button>
            {hasTorch ? (
              <Button variant="secondary" onClick={toggleTorch}>
                {torchOn ? 'Light off' : 'Light on'}
              </Button>
            ) : null}
          </>
        ) : (
          <Button variant="secondary" onClick={() => void start()}>
            Use the camera
          </Button>
        )}
      </div>

      {note ? <p className="text-sm text-amber-800">{note}</p> : null}

      {/* Always present, never behind a fallback. */}
      <form
        className="flex items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          handleResult(manual);
          setManual('');
          onDraft?.('');
        }}
      >
        <div className="flex-1">
          <Field label={label} htmlFor="scanner-manual" hint={hint}>
            <Input
              id="scanner-manual"
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

/** Poll the native detector. ~8/second is plenty and leaves the CPU alone. */
function runNativeDetector(video: HTMLVideoElement, onResult: (value: string) => void) {
  const detector = new window.BarcodeDetector!({ formats: FORMATS });
  let cancelled = false;

  const tick = async () => {
    if (cancelled) return;
    try {
      const found = await detector.detect(video);
      if (found.length > 0) {
        onResult(found[0].rawValue);
        return;
      }
    } catch {
      // A single failed frame is normal — motion blur, or the camera settling.
    }
    if (!cancelled) window.setTimeout(tick, 120);
  };
  void tick();

  return () => {
    cancelled = true;
  };
}

/** The WASM decoder, for browsers with no native detector. */
async function runZxing(video: HTMLVideoElement, onResult: (value: string) => void) {
  // Imported here rather than at module scope so the decoder is downloaded only
  // by a device that needs it — it is far larger than this component (N-1).
  const { BrowserMultiFormatReader } = await import('@zxing/browser');
  const reader = new BrowserMultiFormatReader();
  let cancelled = false;

  void reader.decodeFromVideoElement(video, (result) => {
    if (cancelled || !result) return;
    onResult(result.getText());
  });

  return () => {
    cancelled = true;
    // Older builds expose `reset`; newer ones a stop control on the returned
    // object. Either way the tracks are stopped by the caller.
    (reader as unknown as { reset?: () => void }).reset?.();
  };
}
