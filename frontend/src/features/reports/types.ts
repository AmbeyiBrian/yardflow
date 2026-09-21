/**
 * Report shapes (design §10; M1, M2).
 *
 * These describe the *framework*, not any particular report — which is the whole
 * point of T7.1 and T7.7: the screen renders whatever the catalogue declares, so
 * a report added on the server needs no type here.
 */

export interface Column {
  key: string;
  label: string;
  /** text | quantity | money | integer | date | datetime | boolean */
  kind: string;
  /** Right-aligned. Set by the server from the kind, so it is decided once. */
  numeric: boolean;
  /** Dropped from the phone card, kept in the exports (§7.3). */
  wide_only: boolean;
}

export interface ReportFilter {
  key: string;
  label: string;
  /** text | date | datetime | choice | reference */
  kind: string;
  required: boolean;
  choices: { value: string; label: string }[];
  /** For `reference`: the API resource whose rows populate the dropdown. */
  resource: string;
  help_text: string;
}

/** `GET /reports`: what may be run, and whether this server can make a PDF. */
export interface ReportCatalogue {
  count: number;
  reports: ReportCatalogueEntry[];
  /**
   * False where the renderer's native libraries are missing (§11). The PDF
   * button is disabled rather than offered, because a server that will hand
   * back HTML should not promise a PDF.
   */
  pdf_available: boolean;
}

export interface ReportCatalogueEntry {
  slug: string;
  title: string;
  description: string;
  /** Which requirement it answers — shown so an auditor's question maps to one. */
  requirement: string;
  /**
   * The group it is listed under. The server fixes both the vocabulary and the
   * order (`CATEGORIES` in the framework); the screen only arranges.
   */
  category: string;
  columns: Column[];
  filters: ReportFilter[];
  can_be_large: boolean;
}

export interface ReportResult {
  slug: string;
  title: string;
  columns: Column[];
  /**
   * Values arrive **already formatted**, from the same column spec the exports
   * use — so the screen and the spreadsheet cannot disagree about what a number
   * looks like (M2).
   */
  rows: Record<string, string>[];
  totals: Record<string, string> | null;
  row_count: number;
  params: Record<string, string>;
}
