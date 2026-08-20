export const COMPOSITION_COMPONENT_NAME = "CECVolumeOSDComposition";
export const COMPOSITION_OWNER = "CECVolumeOSD";
export const NOTIFICATION_COMPOSITION = 1;

export type CompositionHook = (
  composition: number | null,
  owner: string,
) => { releaseComposition?: () => void } | void;

export interface CompositionDebugState {
  compositionAvailable: boolean;
  compositionHookAvailable: boolean;
  compositionCandidateCount: number;
  compositionBridgeMounted: boolean;
  compositionRequested: boolean;
  compositionReleased: boolean;
  compositionMode: "Notification" | null;
  compositionOwner: typeof COMPOSITION_OWNER;
  requestedComposition: "Notification" | null;
  compositionOwnerToken: typeof COMPOSITION_OWNER;
  compositionReleaseState: "unsupported" | "pending" | "held" | "released";
  compositionTransitionCount: number;
  compositionLastTransition: "initial" | "requested" | "released";
  compositionLastTransitionAt: number | null;
}

const HOOK_SIGNATURES = [
  "AddMinimumCompositionStateRequest",
  "ChangeMinimumCompositionStateRequest",
  "RemoveMinimumCompositionStateRequest",
] as const;

export function isCompositionHook(value: unknown): value is CompositionHook {
  if (typeof value !== "function") {
    return false;
  }

  try {
    const source = Function.prototype.toString.call(value);
    return (
      HOOK_SIGNATURES.every((signature) => source.includes(signature)) &&
      !source.includes("m_mapCompositionStateRequests")
    );
  } catch {
    return false;
  }
}

export function findCompositionHooks(
  moduleValues: Iterable<unknown>,
): CompositionHook[] {
  const hooks = new Set<CompositionHook>();

  for (const moduleValue of moduleValues) {
    const possibleModules: unknown[] = [moduleValue];
    if (
      (typeof moduleValue === "object" && moduleValue !== null) ||
      typeof moduleValue === "function"
    ) {
      try {
        possibleModules.push(
          (moduleValue as { default?: unknown }).default,
        );
      } catch {
        // Ignore hostile or lazy getters while walking Steam's module cache.
      }
    }

    for (const possibleModule of possibleModules) {
      if (
        (typeof possibleModule !== "object" || possibleModule === null) &&
        typeof possibleModule !== "function"
      ) {
        continue;
      }

      let exportNames: string[];
      try {
        exportNames = Object.keys(possibleModule);
      } catch {
        continue;
      }

      for (const exportName of exportNames) {
        try {
          const candidate = (possibleModule as Record<string, unknown>)[
            exportName
          ];
          if (isCompositionHook(candidate)) {
            hooks.add(candidate);
          }
        } catch {
          // Steam modules can expose getters with unavailable side effects.
        }
      }
    }
  }

  return [...hooks];
}

type CompositionListener = () => void;

export class CompositionActivityStore {
  private requested = false;
  private bridgeMounted = false;
  private transitionCount = 0;
  private lastTransition: CompositionDebugState["compositionLastTransition"] =
    "initial";
  private lastTransitionAt: number | null = null;
  private readonly listeners = new Set<CompositionListener>();

  constructor(private readonly candidateCount: number) {}

  readonly getSnapshot = (): boolean => this.requested;

  readonly getRequested = (): boolean => this.requested;

  readonly isAvailable = (): boolean => this.candidateCount === 1;

  readonly isBridgeMounted = (): boolean => this.bridgeMounted;

  readonly subscribe = (listener: CompositionListener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  setRequested(requested: boolean): boolean {
    if (requested === this.requested) {
      return false;
    }

    this.requested = requested;
    this.transitionCount += 1;
    this.lastTransition = requested ? "requested" : "released";
    this.lastTransitionAt = Date.now();
    for (const listener of [...this.listeners]) {
      listener();
    }
    return true;
  }

  setBridgeMounted(mounted: boolean): void {
    this.bridgeMounted = mounted;
  }

  debugState(): CompositionDebugState {
    const available = this.isAvailable();
    return {
      compositionAvailable: available,
      compositionHookAvailable: available,
      compositionCandidateCount: this.candidateCount,
      compositionBridgeMounted: this.bridgeMounted,
      compositionRequested: this.requested,
      compositionReleased: !this.requested,
      compositionMode: this.requested ? "Notification" : null,
      compositionOwner: COMPOSITION_OWNER,
      requestedComposition: this.requested ? "Notification" : null,
      compositionOwnerToken: COMPOSITION_OWNER,
      compositionReleaseState: !available
        ? "unsupported"
        : this.requested
          ? this.bridgeMounted
            ? "held"
            : "pending"
          : "released",
      compositionTransitionCount: this.transitionCount,
      compositionLastTransition: this.lastTransition,
      compositionLastTransitionAt: this.lastTransitionAt,
    };
  }
}
