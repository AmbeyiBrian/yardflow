/**
 * Where the camera is aimed (E10, design §7.3d).
 *
 * A palm-sized label carries several QR codes and barcodes. Decoding the whole
 * frame reads whichever one the decoder meets first, which is rarely the one
 * the hand is pointing at. So only the centre of the picture is decoded, and
 * the drawn box on screen is exactly that region.
 */

/** The share of the shorter video side that is read, and drawn as the box. */
export const AIM_FRACTION = 0.4;

export interface AimRegion {
  sx: number;
  sy: number;
  size: number;
}

/**
 * The centred square of side `fraction x min(w, h)`, in video pixels.
 *
 * The video is shown `object-cover` in a square, which crops the long side
 * evenly, so the visible centre is the video's own centre and the box on
 * screen maps straight onto this region.
 */
export function aimRegion(videoWidth: number, videoHeight: number, fraction: number): AimRegion {
  const size = Math.round(fraction * Math.min(videoWidth, videoHeight));
  return {
    sx: Math.round((videoWidth - size) / 2),
    sy: Math.round((videoHeight - size) / 2),
    size,
  };
}

interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * Of several codes inside the region, the one nearest its centre: the one the
 * box was held over. A code with no bounding box cannot be ranked, so it only
 * wins when nothing else can be; the order the detector gave is the tie-break.
 */
export function pickNearestCentre<T extends { boundingBox?: Box }>(
  found: T[],
  canvasWidth: number,
  canvasHeight: number,
): T | undefined {
  const cx = canvasWidth / 2;
  const cy = canvasHeight / 2;
  let best: T | undefined;
  let bestDistance = Infinity;
  for (const item of found) {
    const box = item.boundingBox;
    if (!box) continue;
    const distance = Math.hypot(box.x + box.width / 2 - cx, box.y + box.height / 2 - cy);
    if (distance < bestDistance) {
      best = item;
      bestDistance = distance;
    }
  }
  return best ?? found[0];
}
