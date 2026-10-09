/**
 * "Use my location" (R13; design §4.18.11).
 *
 * Reads the phone's position once, at high accuracy, and hands it to the form.
 * It never watches: standing at the gate of a site and tapping once is the
 * whole job. The accuracy is shown because a reading good to 400 m is worse
 * than typing the coordinates, and the person should be able to tell. Typing
 * or pasting into the boxes stays possible; this only fills them.
 */

import { useState } from 'react';

import { Button } from './index';

export interface PositionReading {
  latitude: number;
  longitude: number;
  /** Metres, as the device reports it. */
  accuracy: number;
}

function failureText(error: GeolocationPositionError | undefined): string {
  if (error?.code === 1) return 'Location is turned off for this app. Allow it in the browser, or type the coordinates.';
  if (error?.code === 3) return 'Could not get a position in time. Try again outside, or type the coordinates.';
  return 'Could not read your position. Try again, or type the coordinates.';
}

export function UseMyLocation({
  onPosition,
  disabled,
}: {
  onPosition: (reading: PositionReading) => void;
  disabled?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [accuracy, setAccuracy] = useState<number | null>(null);
  const [failure, setFailure] = useState('');

  function read() {
    setFailure('');
    if (!('geolocation' in navigator)) {
      setFailure('This device cannot give its location. Type the coordinates instead.');
      return;
    }
    setBusy(true);
    navigator.geolocation.getCurrentPosition(
      (position) => {
        setBusy(false);
        setAccuracy(Math.round(position.coords.accuracy));
        onPosition({
          latitude: position.coords.latitude,
          longitude: position.coords.longitude,
          accuracy: position.coords.accuracy,
        });
      },
      (error) => {
        setBusy(false);
        setFailure(failureText(error));
      },
      { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 },
    );
  }

  return (
    <div className="flex flex-col gap-1.5">
      <Button type="button" variant="secondary" onClick={read} loading={busy} disabled={disabled}>
        Use my location
      </Button>
      {accuracy !== null && !failure ? (
        <p className="text-sm text-slate-600">
          Position taken, accurate to about {accuracy} m.
          {accuracy > 100 ? ' That is rough; try again in the open.' : ''}
        </p>
      ) : null}
      {failure ? (
        <p role="alert" className="text-sm text-red-600">
          {failure}
        </p>
      ) : null}
    </div>
  );
}
