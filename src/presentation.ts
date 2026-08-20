export interface RawVolumeEvent {
  instance?: unknown;
  route?: unknown;
  volume?: unknown;
  muted?: unknown;
  generation?: unknown;
  source?: unknown;
  diagnostic?: unknown;
}

export interface VolumePresentation {
  instance: string | null;
  route: string;
  volume: number | null;
  muted: boolean | null;
  generation: number | null;
  source: "cec" | "preview" | "diagnostic";
  diagnostic: boolean;
}

export const FADE_IN_MS = 100;
export const HOLD_MS = 5000;
export const FADE_OUT_MS = 180;

const MAX_ROUTE_LENGTH = 256;
const MAX_INSTANCE_LENGTH = 128;

function nullableBoolean(value: unknown): boolean | null | undefined {
  if (value === null || value === undefined) {
    return null;
  }
  return typeof value === "boolean" ? value : undefined;
}

function nullableVolume(value: unknown): number | null | undefined {
  if (value === null || value === undefined) {
    return null;
  }
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 100) {
    return undefined;
  }
  return value;
}

export function normalizeVolumeEvent(raw: unknown): VolumePresentation | null {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return null;
  }

  const candidate = raw as RawVolumeEvent;
  if (
    typeof candidate.route !== "string" ||
    candidate.route.length === 0 ||
    candidate.route.length > MAX_ROUTE_LENGTH
  ) {
    return null;
  }

  const volume = nullableVolume(candidate.volume);
  const muted = nullableBoolean(candidate.muted);
  if (volume === undefined || muted === undefined || (volume === null && muted === null)) {
    return null;
  }

  const source =
    candidate.source === "preview" || candidate.source === "diagnostic"
      ? candidate.source
      : "cec";
  const instance =
    typeof candidate.instance === "string" &&
    candidate.instance.length > 0 &&
    candidate.instance.length <= MAX_INSTANCE_LENGTH
      ? candidate.instance
      : null;
  const generation =
    typeof candidate.generation === "number" &&
    Number.isSafeInteger(candidate.generation) &&
    candidate.generation >= 0
      ? candidate.generation
      : null;
  if (source === "cec" && (instance === null || generation === null)) {
    return null;
  }

  return {
    instance,
    route: candidate.route,
    volume,
    muted,
    generation,
    source,
    diagnostic: source === "diagnostic" || candidate.diagnostic === true,
  };
}

export function fillPercent(presentation: VolumePresentation): number {
  return presentation.volume ?? 0;
}
