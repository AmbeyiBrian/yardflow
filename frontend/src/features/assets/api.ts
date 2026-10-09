/**
 * Asset register endpoints (design §4.20.4, §4.20.6; R14). Resources carry no
 * `/api/v1` prefix; the client adds it.
 *
 * `cost` and `purchase_terms` are withheld by the server from those without
 * `asset.manage`, `finance.approve` or `project.view_cost` (§4.20.7), so they
 * are optional: absent is "not told", not zero.
 */

import { useQueryClient } from '@tanstack/react-query';

import { useAction, useDetail, useList, useResource, type QueryParams } from '../../api/hooks';

const ASSETS = 'assets';

export type AssetType = 'VEHICLE' | 'GENERATOR' | 'TOOL' | 'EQUIPMENT' | 'OTHER';
export type AssetStatus = 'ACTIVE' | 'CLOSED';
export type ClosedReason = 'SOLD' | 'WRITTEN_OFF';

export const ASSET_TYPES: { value: AssetType; label: string }[] = [
  { value: 'VEHICLE', label: 'Vehicle' },
  { value: 'GENERATOR', label: 'Generator' },
  { value: 'TOOL', label: 'Tool' },
  { value: 'EQUIPMENT', label: 'Equipment' },
  { value: 'OTHER', label: 'Other' },
];

/** Attachment captions for an asset's documents (§4.20.2). */
export const ASSET_DOCUMENT_KINDS = [
  'Photo',
  'Contract',
  'Logbook',
  'Insurance',
  'Warranty',
  'Other',
] as const;

export interface Asset {
  id: number;
  type: AssetType;
  name: string;
  tag: string;
  purchase_date: string | null;
  supplier: number | null;
  supplier_name?: string;
  cost?: string | null;
  purchase_terms?: string;
  make: string;
  model: string;
  insurance_expires_on: string | null;
  inspection_expires_on: string | null;
  /** Null: held in the yard. */
  holder: number | null;
  holder_name?: string | null;
  status: AssetStatus;
  closed_on: string | null;
  closed_reason: ClosedReason | '';
  closed_note: string;
}

export interface AssetHandover {
  id: number;
  from_holder: number | null;
  from_holder_name?: string | null;
  to_holder: number | null;
  to_holder_name?: string | null;
  handed_over_by: number;
  handed_over_by_name?: string;
  handed_over_on: string;
  note: string;
}

/** `GET /assets/{id}/fuel` (§4.20.4). */
export interface AssetFuel {
  litres: string | null;
  spend: string;
  fill_count: number;
  spend_per_litre: string | null;
  /** Spend not yet approved, shown separately as "awaiting approval". */
  pending_spend?: string;
}

export interface AssetParams extends QueryParams {
  type?: string;
  status?: string;
  holder?: number | string;
  search?: string;
}

export interface AssetInput {
  type: AssetType;
  name: string;
  tag?: string;
  purchase_date?: string | null;
  supplier?: number | null;
  cost?: string | null;
  purchase_terms?: string;
  make?: string;
  model?: string;
  insurance_expires_on?: string | null;
  inspection_expires_on?: string | null;
  /** On create only: the first handover (null: the yard). */
  holder?: number | null;
}

export const useAssets = (params?: AssetParams) =>
  useList<Asset>(ASSETS, { page_size: 200, ...params });

export const useAsset = (id: string | number | undefined) => useDetail<Asset>(ASSETS, id);

export const useAssetHandovers = (id: number | undefined) =>
  useResource<AssetHandover[] | { results: AssetHandover[] }>(
    `${ASSETS}/${id}/handovers`,
    undefined,
    { enabled: id !== undefined },
  );

export const useAssetFuel = (id: number | undefined, from: string, to: string) =>
  useResource<AssetFuel>(`${ASSETS}/${id}/fuel`, { from, to }, { enabled: id !== undefined });

/**
 * Sub-resources (`assets/5/handovers`, `assets/5/fuel`) are their own query
 * keys, so invalidating `assets` alone leaves them stale. Call after a handover
 * or close.
 */
export function useRefreshAssets() {
  const client = useQueryClient();
  return () =>
    client.invalidateQueries({
      predicate: (q) => typeof q.queryKey[0] === 'string' && q.queryKey[0].startsWith(ASSETS),
    });
}

export const useCreateAsset = () => useAction<AssetInput, Asset>({ resource: ASSETS });

export const useUpdateAsset = () =>
  useAction<Partial<AssetInput> & { id: number }, Asset>({
    resource: ASSETS,
    method: 'patch',
    path: (b) => String(b.id),
  });

export const useHandOverAsset = () =>
  useAction<
    { id: number; to_holder: number | null; note?: string; handed_over_on?: string },
    AssetHandover
  >({
    resource: ASSETS,
    path: (b) => `${b.id}/handover`,
  });

export const useCloseAsset = () =>
  useAction<
    { id: number; closed_on: string; closed_reason: ClosedReason; closed_note?: string },
    Asset
  >({ resource: ASSETS, path: (b) => `${b.id}/close` });
