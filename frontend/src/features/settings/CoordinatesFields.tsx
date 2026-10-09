/**
 * The coordinates block shared by the site and location sheets
 * (R13; design §4.18.8, §4.18.11).
 *
 * Latitude, longitude and radius, with "Use my location" to fill the first two.
 * The boxes stay editable so a position can be typed or pasted from a map.
 * Validation lives in `coordinates.ts`; the form calls `checkCoordinates` on
 * submit and the server's COORDINATES_REQUIRED field errors land on the same
 * field names through `applyFieldErrors`.
 */

import type { UseFormReturn } from 'react-hook-form';

import { Field, Input } from '../../components/ui';
import { UseMyLocation } from '../../components/ui/UseMyLocation';
import { formatCoordinate, validateCoordinates, type CoordinateInput } from './coordinates';

export type CoordinateForm = CoordinateInput;

/**
 * Run the client-side rules and put the messages on the form. Returns true when
 * the three boxes are acceptable.
 */
export function checkCoordinates(form: UseFormReturn<CoordinateForm>, required: boolean): boolean {
  const values = form.getValues();
  const errors = validateCoordinates(values, required);
  for (const field of ['latitude', 'longitude', 'radius_m'] as const) {
    const message = errors[field];
    if (message) form.setError(field, { type: 'validate', message });
  }
  return Object.keys(errors).length === 0;
}

export function CoordinatesFields({
  form: given,
  required,
}: {
  // The sheets' own form types differ; only these three fields are touched.
  form: unknown;
  required: boolean;
}) {
  const form = given as UseFormReturn<CoordinateForm>;
  const errors = form.formState.errors;

  return (
    <fieldset className="flex flex-col gap-3 rounded-lg border border-slate-200 p-3">
      <legend className="px-1 text-sm font-medium text-slate-700">
        Where it is{required ? '' : ' (optional)'}
      </legend>
      <p className="text-sm text-slate-600">
        {required
          ? 'Clock-in checks people are inside this area. Stand at the place and tap the button, or type the coordinates.'
          : 'Leave blank if this place has no fixed position.'}
      </p>
      <UseMyLocation
        onPosition={(reading) => {
          const options = { shouldDirty: true, shouldValidate: false };
          form.setValue('latitude', formatCoordinate(reading.latitude), options);
          form.setValue('longitude', formatCoordinate(reading.longitude), options);
          form.clearErrors(['latitude', 'longitude']);
        }}
      />
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Latitude" htmlFor="place-lat" error={errors.latitude?.message}>
          <Input
            id="place-lat"
            inputMode="decimal"
            placeholder="-1.286389"
            invalid={Boolean(errors.latitude)}
            {...form.register('latitude')}
          />
        </Field>
        <Field label="Longitude" htmlFor="place-lng" error={errors.longitude?.message}>
          <Input
            id="place-lng"
            inputMode="decimal"
            placeholder="36.817223"
            invalid={Boolean(errors.longitude)}
            {...form.register('longitude')}
          />
        </Field>
      </div>
      <Field
        label="Radius (metres)"
        htmlFor="place-radius"
        hint="How far from the point still counts as being there. 20 to 2000."
        error={errors.radius_m?.message}
      >
        <Input
          id="place-radius"
          inputMode="numeric"
          invalid={Boolean(errors.radius_m)}
          {...form.register('radius_m')}
        />
      </Field>
    </fieldset>
  );
}
