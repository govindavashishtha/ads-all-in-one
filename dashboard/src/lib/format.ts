import type { Money } from "./types";

/** Display-only conversion. Money stays a Decimal string in the API; never do maths you persist. */
export function toNum(v: Money | number | string | null | undefined): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

/** Locale follows the account currency so INR gets lakh/crore grouping; overridable. */
export function localeFor(currency: string | null | undefined): string | undefined {
  const override = process.env.NEXT_PUBLIC_LOCALE;
  if (override) return override;
  if (currency === "INR") return "en-IN";
  return undefined; // browser default
}

const fmtCache = new Map<string, Intl.NumberFormat>();
function nf(locale: string | undefined, opts: Intl.NumberFormatOptions): Intl.NumberFormat {
  const key = `${locale}|${JSON.stringify(opts)}`;
  let f = fmtCache.get(key);
  if (!f) {
    f = new Intl.NumberFormat(locale, opts);
    fmtCache.set(key, f);
  }
  return f;
}

/**
 * Format a money amount in major currency units.
 *
 * Rule (BUILD_PLAN §0 / §8: money is Decimal, major units, 2 dp outside connectors):
 * always show exactly two decimal places, including trailing zeros
 * (`₹2,000.00`, `₹500.00`, `$1,234.50`). Grouping follows the account
 * currency's locale (Indian grouping for INR). Override with `opts.decimals`.
 * `opts.compact` is for chart axis ticks only (compact notation, 1 fraction digit).
 */
export function formatMoney(
  v: Money | null | undefined,
  currency: string | null | undefined,
  opts: { compact?: boolean; decimals?: number } = {},
): string {
  const n = toNum(v);
  if (n === null) return "—";
  const decimals = opts.decimals ?? 2;
  if (!currency) {
    return nf(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals }).format(n);
  }
  const o: Intl.NumberFormatOptions = { style: "currency", currency };
  if (opts.compact) {
    o.notation = "compact";
    o.maximumFractionDigits = 1;
  } else {
    o.minimumFractionDigits = decimals;
    o.maximumFractionDigits = decimals;
  }
  return nf(localeFor(currency), o).format(n);
}

export function formatUsd(v: Money | null | undefined, decimals = 2): string {
  const n = toNum(v);
  if (n === null) return "—";
  return nf("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(n);
}

export function formatNumber(v: number | string | null | undefined, decimals = 0, locale?: string): string {
  const n = toNum(v);
  if (n === null) return "—";
  return nf(locale, { maximumFractionDigits: decimals, minimumFractionDigits: 0 }).format(n);
}

export function formatCompact(v: number | string | null | undefined, locale?: string): string {
  const n = toNum(v);
  if (n === null) return "—";
  return nf(locale, { notation: "compact", maximumFractionDigits: 1 }).format(n);
}

/** `ratio` 0.123 → "12.3%". */
export function formatPct(ratio: number | string | null | undefined, decimals = 1): string {
  const n = toNum(ratio);
  if (n === null) return "—";
  return `${(n * 100).toFixed(decimals)}%`;
}

export function formatRoas(v: number | string | null | undefined): string {
  const n = toNum(v);
  return n === null ? "—" : `${n.toFixed(2)}×`;
}

/** Share of a cap used; null when the cap is unknown or zero. */
export function capRatio(spend: Money | null | undefined, cap: Money | null | undefined): number | null {
  const s = toNum(spend);
  const c = toNum(cap);
  if (s === null || c === null || c <= 0) return null;
  return s / c;
}

export type PaceTone = "ok" | "warn" | "danger" | "unknown";

/** Mirrors §12.4 thresholds for colouring only (the engine decides alerts). */
export function paceTone(ratio: number | null, warnAt = 0.85, dangerAt = 0.97): PaceTone {
  if (ratio === null) return "unknown";
  if (ratio >= dangerAt) return "danger";
  if (ratio >= warnAt) return "warn";
  return "ok";
}

export function formatDateTime(iso: string | null | undefined, timeZone?: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return new Intl.DateTimeFormat("en-GB", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone,
    timeZoneName: "short",
  }).format(d);
}

export function formatDay(date: string): string {
  // account-local YYYY-MM-DD, rendered without timezone shifts
  const [y, m, d] = date.split("-").map(Number);
  if (!y || !m || !d) return date;
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${d} ${months[m - 1]}`;
}

export function relativeTime(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "never";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return iso;
  const s = Math.round((now.getTime() - t) / 1000);
  const abs = Math.abs(s);
  const sign = s >= 0 ? "ago" : "from now";
  if (abs < 60) return `${abs}s ${sign}`;
  if (abs < 3600) return `${Math.round(abs / 60)}m ${sign}`;
  if (abs < 86400) return `${Math.round(abs / 3600)}h ${sign}`;
  return `${Math.round(abs / 86400)}d ${sign}`;
}

export function humanize(s: string | null | undefined): string {
  if (!s) return "—";
  return s.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

/** YYYY-MM-DD for `now − days` in the given IANA timezone (account-local dates, §0.9). */
export function localDateMinus(days: number, timeZone?: string, now: Date = new Date()): string {
  const d = new Date(now.getTime() - days * 86_400_000);
  return new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).format(d);
}

/** Add days to an account-local YYYY-MM-DD date string. */
export function addDaysIso(date: string, n: number): string {
  const d = new Date(`${date}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}


/** "1 call" / "2 calls". */
export function pluralize(n: number, singular: string, plural = `${singular}s`): string {
  return `${n} ${n === 1 ? singular : plural}`;
}

/**
 * Display label for an ad-account `status` string.
 *
 * Meta stores Marketing API `account_status` as a decimal string (V-13 is still
 * VERIFY-P0 open). Google already uses names like ENABLED. Mapping is display-only.
 * Source: Meta Ad Account `account_status` values.
 */
const META_ACCOUNT_STATUS: Record<string, string> = {
  "1": "active",
  "2": "disabled",
  "3": "unsettled",
  "7": "pending_risk_review",
  "8": "pending_settlement",
  "9": "in_grace_period",
  "100": "pending_closure",
  "101": "closed",
  "201": "any_active",
  "202": "any_closed",
};

export function accountStatusLabel(status: string | null | undefined, platform?: string | null): string | null {
  if (status == null || status === "") return null;
  if (platform === "meta" && META_ACCOUNT_STATUS[status] != null) return META_ACCOUNT_STATUS[status];
  return status;
}


/**
 * Friendly label for an ad `format` stored from the platform
 * (Meta `creative.object_type`, Google `ad.type`). The raw value is kept
 * for tooltips by the caller.
 */
const AD_FORMAT_LABELS: Record<string, string> = {
  // Meta AdCreative.object_type
  SHARE: "Link",
  PHOTO: "Single image",
  VIDEO: "Video",
  STATUS: "Text",
  DOMAIN: "Link",
  EVENT: "Event",
  OFFER: "Offer",
  APPLICATION: "App",
  PAGE: "Page",
  STORE_ITEM: "Store item",
  MUSIC: "Music",
  INVALID: "Unknown",
  // Google AdType
  RESPONSIVE_SEARCH_AD: "Responsive search ad",
  EXPANDED_TEXT_AD: "Expanded text ad",
  RESPONSIVE_DISPLAY_AD: "Responsive display ad",
  APP_AD: "App ad",
  VIDEO_AD: "Video ad",
  IMAGE_AD: "Image ad",
  TEXT_AD: "Text ad",
  CALL_AD: "Call ad",
  HOTEL_AD: "Hotel ad",
  SMART_CAMPAIGN_AD: "Smart campaign ad",
  DEMAND_GEN_MULTI_ASSET_AD: "Demand Gen ad",
  DEMAND_GEN_VIDEO_RESPONSIVE_AD: "Demand Gen video ad",
  DEMAND_GEN_CAROUSEL_AD: "Demand Gen carousel ad",
  DEMAND_GEN_PRODUCT_AD: "Demand Gen product ad",
};

function titleCaseToken(raw: string): string {
  return raw
    .toLowerCase()
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function adFormatLabel(format: string | null | undefined): string {
  if (format == null || format === "") return "—";
  return AD_FORMAT_LABELS[format] ?? AD_FORMAT_LABELS[format.toUpperCase()] ?? titleCaseToken(format);
}
