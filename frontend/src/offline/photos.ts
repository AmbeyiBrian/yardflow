/**
 * The photo step of the drain (design §4.17.8, §8; R6).
 *
 * An entry captured offline cannot carry its photos in the same request: the
 * attachment needs a target id, and the target does not exist until the sync
 * has created it. So the photos wait on the phone, and once `drainQueue` has
 * marked the entry applied (it receives `document_id`) each one is uploaded to
 * `/attachments` with its own `client_uuid`. A response lost on the way back
 * then replays to the same attachment rather than a second one.
 *
 * Until they land, the server reports `evidence_state: "arriving"`, so the
 * approver reads "photos on the way" rather than "no evidence".
 */

import { ApiError, api } from '../api/client';
import { allQueued, markPhoto, queuedPhotos } from './db';
import { isPermanentUploadFailure, photosReadyToUpload } from './financeQueue';

export interface AttachmentUpload {
  targetType: string;
  targetId: string | number;
  caption: string;
  client_uuid: string;
  blob: Blob;
  filename: string;
}

/**
 * One photo to `/attachments`. The drain and the money forms (photos taken
 * before saving, §4.17.8) both come through here, so there is one place that
 * knows the form fields and that `client_uuid` makes a retry land once.
 */
export function postAttachment(upload: AttachmentUpload): Promise<unknown> {
  const form = new FormData();
  form.set('target_type', upload.targetType);
  form.set('target_id', String(upload.targetId));
  form.set('kind', 'PHOTO');
  form.set('caption', upload.caption);
  form.set('client_uuid', upload.client_uuid);
  form.set('file', new File([upload.blob], upload.filename, { type: upload.blob.type }));
  return api.post('/attachments', form);
}

export interface PhotoOutcome {
  uploaded: number;
  /** Still queued: no entry applied yet for them, or the upload failed. */
  waiting: number;
  /** Refused as files; kept on the phone but no longer retried. */
  failed: number;
}

/** Upload what can be uploaded. Safe to call often — it returns quickly when idle. */
export async function drainPhotos(): Promise<PhotoOutcome> {
  const photos = await queuedPhotos();
  if (photos.length === 0) return { uploaded: 0, waiting: 0, failed: 0 };

  const ready = photosReadyToUpload(await allQueued(), photos);
  let uploaded = 0;
  let failed = 0;

  for (const { photo, targetType, targetId } of ready) {
    try {
      await postAttachment({
        targetType,
        targetId,
        caption: photo.caption,
        // Reused on every retry: this is what makes the upload idempotent.
        client_uuid: photo.client_uuid,
        blob: photo.blob,
        filename: photo.filename,
      });
      await markPhoto(photo.client_uuid, { status: 'UPLOADED' });
      uploaded += 1;
    } catch (error) {
      if (error instanceof ApiError && isPermanentUploadFailure(error.status)) {
        await markPhoto(photo.client_uuid, { status: 'FAILED', error: error.message });
        failed += 1;
        continue;
      }
      // Stays QUEUED. A dropped connection part way through is the normal case;
      // stop here so the remaining photos are tried again in the same order.
      await markPhoto(photo.client_uuid, {
        error: error instanceof ApiError ? error.message : 'No connection.',
      });
      break;
    }
  }

  return { uploaded, waiting: photos.length - uploaded - failed, failed };
}
