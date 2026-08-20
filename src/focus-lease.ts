export type FocusLeasePhase =
  | "idle"
  | "not-required"
  | "leased"
  | "already-selected"
  | "unsupported"
  | "precondition-failed"
  | "lost"
  | "restored"
  | "error";

export interface FocusLeaseDebugState {
  available: boolean;
  required: boolean;
  owned: boolean;
  phase: FocusLeasePhase;
  appId: number | null;
  windowId: number | null;
  previousWindowId: number | null;
  acquisitionCount: number;
  releaseCount: number;
  lastError: string | null;
  lastTransitionAt: number | null;
}

export interface SteamFocusWindowStore {
  GetAppWindowIDs(appId: number): unknown;
  GetAppFocusedWindowID(appId: number): unknown;
  SetFocusedAppWindowID(appId: number, windowId: number): unknown;
}

export interface SteamFocusContext {
  windowStore: SteamFocusWindowStore;
  mainRunningAppId: unknown;
  focusedAppId: unknown;
  focusedWindowId: unknown;
  compositionIdle: unknown;
}

export type SteamFocusResolver = () => SteamFocusContext;

interface OwnedFocusLease {
  windowStore: SteamFocusWindowStore;
  appId: number;
  windowId: number;
  previousWindowId: 0;
}

type OwnedLeaseObservation = "owned" | "restored" | "lost";

type UnknownRecord = Record<PropertyKey, unknown>;

const UINT32_MAX = 0xffff_ffff;
const SETTER_MARKERS = [
  "m_mapAppWindows.get(",
  "focusedWindowID:",
  "windowids:",
  "m_mapAppWindows.set(",
] as const;

function asRecord(value: unknown): UnknownRecord | null {
  if (
    (typeof value !== "object" || value === null) &&
    typeof value !== "function"
  ) {
    return null;
  }
  return value as UnknownRecord;
}

function readProperty(value: unknown, property: PropertyKey): unknown {
  const record = asRecord(value);
  if (record === null) {
    throw new Error(`Steam focus API is missing ${String(property)}`);
  }
  return record[property];
}

function method(
  value: unknown,
  name: PropertyKey,
  arity: number,
): (...args: unknown[]) => unknown {
  const candidate = readProperty(value, name);
  if (typeof candidate !== "function" || candidate.length !== arity) {
    throw new Error(
      `Steam focus API ${String(name)} does not match the validated signature`,
    );
  }
  return candidate as (...args: unknown[]) => unknown;
}

function validatedSetter(value: unknown): void {
  const setter = readProperty(value, "SetFocusedAppWindowID");
  if (
    typeof setter !== "function" ||
    setter.name !== "SetFocusedAppWindowID" ||
    (setter.length !== 0 && setter.length !== 2)
  ) {
    throw new Error(
      "Steam focus API SetFocusedAppWindowID does not match a validated signature",
    );
  }
  let source: string;
  try {
    source = Function.prototype.toString.call(setter);
  } catch {
    throw new Error("Steam focus setter source is unreadable");
  }
  let descriptorOwner: object | null = null;
  let descriptorCount = 0;
  let cursor = value as object | null;
  for (let depth = 0; cursor !== null && depth < 16; depth += 1) {
    const descriptor = Object.getOwnPropertyDescriptor(
      cursor,
      "SetFocusedAppWindowID",
    );
    if (descriptor !== undefined) {
      descriptorCount += 1;
      descriptorOwner = cursor;
      if (
        descriptor.value !== setter ||
        descriptor.get !== undefined ||
        descriptor.set !== undefined
      ) {
        throw new Error("Steam focus setter descriptor is ambiguous");
      }
    }
    cursor = Object.getPrototypeOf(cursor) as object | null;
  }
  if (descriptorCount !== 1 || descriptorOwner === null) {
    throw new Error("Steam focus setter is not unique in its prototype chain");
  }

  if (setter.length === 2) {
    if (!SETTER_MARKERS.every((marker) => source.includes(marker))) {
      throw new Error(
        "Steam focus setter does not match the validated store action",
      );
    }
    return;
  }

  // MobX wraps the current action in an arity-zero dispatcher. Validate both
  // that wrapper and the unique class method body retained by toString().
  if (
    !/^function [$_A-Za-z][\w$]*\(\)\{return [\w$.]+\([^)]*\|\|this,arguments\)\}$/.test(
      source,
    )
  ) {
    throw new Error("Steam focus setter has an unknown action wrapper");
  }
  const constructor = (descriptorOwner as { constructor?: unknown })
    .constructor;
  if (typeof constructor !== "function") {
    throw new Error("Steam WindowStore constructor is unavailable");
  }
  let constructorSource: string;
  try {
    constructorSource = Function.prototype.toString.call(constructor);
  } catch {
    throw new Error("Steam WindowStore constructor source is unreadable");
  }
  const methodStart = constructorSource.indexOf("SetFocusedAppWindowID(");
  const followingGetter = /}\s*get\s+MainRunningAppWindowIDs\b/.exec(
    constructorSource.slice(methodStart),
  );
  if (
    methodStart < 0 ||
    followingGetter === null ||
    constructorSource.indexOf("SetFocusedAppWindowID(", methodStart + 1) >= 0
  ) {
    throw new Error("Steam WindowStore focus action is not uniquely readable");
  }
  const methodEnd = methodStart + followingGetter.index;
  const methodSource = constructorSource.slice(methodStart, methodEnd + 1);
  if (!SETTER_MARKERS.every((marker) => methodSource.includes(marker))) {
    throw new Error(
      "Steam WindowStore focus action body does not match the validated contract",
    );
  }
}

function readSubscribableValue(store: unknown, getterName: string): unknown {
  const getter = method(store, getterName, 0);
  return readProperty(getter.call(store), "Value");
}

function isPositiveUint32(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isSafeInteger(value) &&
    value > 0 &&
    value <= UINT32_MAX
  );
}

function isNonNegativeUint32(value: unknown): value is number {
  return value === 0 || isPositiveUint32(value);
}

function isExactWindowList(value: unknown, windowId: number): boolean {
  return (
    Array.isArray(value) &&
    value.length === 1 &&
    value[0] === windowId
  );
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * Resolve only the exact Steam stores and methods validated on the current
 * GamepadUI build. This intentionally fails closed if Valve changes a method
 * signature or replaces the public WindowStore action.
 */
export function resolveSteamFocusContext(
  root: unknown = globalThis,
): SteamFocusContext {
  let windowStore: unknown;
  try {
    windowStore = readProperty(
      readProperty(root, "SteamUIStore"),
      "WindowStore",
    );
    if (windowStore === null || windowStore === undefined) {
      throw new Error("SteamUIStore.WindowStore is unavailable");
    }
  } catch {
    const dfl = readProperty(root, "DFL");
    windowStore = readProperty(readProperty(dfl, "Router"), "WindowStore");
  }

  method(windowStore, "GetAppWindowIDs", 1);
  method(windowStore, "GetAppFocusedWindowID", 1);
  validatedSetter(windowStore);

  const mainInstance = readProperty(
    windowStore,
    "GamepadUIMainWindowInstance",
  );
  let compositionStore: unknown;
  try {
    compositionStore = readProperty(mainInstance, "CompositionStateStore");
  } catch {
    compositionStore = readProperty(mainInstance, "m_CompositionStateStore");
  }
  const getCompositionState = method(
    compositionStore,
    "GetCompositionState",
    0,
  );
  const requestMap = readProperty(
    compositionStore,
    "m_mapCompositionStateRequests",
  );
  const ownerMap = readProperty(
    compositionStore,
    "m_mapCompostionRequestsDebugInfo",
  );
  if (!(requestMap instanceof Map) || !(ownerMap instanceof Map)) {
    throw new Error("Steam composition request maps are unavailable");
  }
  const requestCounts = [...requestMap.values()];
  if (
    requestCounts.some(
      (count) =>
        typeof count !== "number" ||
        !Number.isSafeInteger(count) ||
        count < 0,
    )
  ) {
    throw new Error("Steam composition request counters are invalid");
  }

  return {
    windowStore: windowStore as SteamFocusWindowStore,
    mainRunningAppId: readProperty(mainInstance, "MainRunningAppID"),
    focusedAppId: readSubscribableValue(
      compositionStore,
      "GetCurrentlyFocusedAppidSubscribableValue",
    ),
    focusedWindowId: readSubscribableValue(
      compositionStore,
      "GetCurrentlyFocusedWindowIDSubscribableValue",
    ),
    compositionIdle:
      getCompositionState.call(compositionStore) === 0 &&
      requestCounts.every((count) => count === 0) &&
      ownerMap.size === 0,
  };
}

/**
 * Temporarily selects Steam's already-focused native game window so Steam's
 * own Notification request can reach Gamescope. The lease owns only the
 * transition from Steam's sentinel value 0 to the one uniquely validated
 * focused window, and restores 0 with compare-and-swap semantics.
 */
export class SteamCompositionFocusLease {
  private available = false;
  private required = false;
  private ownedLease: OwnedFocusLease | null = null;
  private phase: FocusLeasePhase = "idle";
  private appId: number | null = null;
  private windowId: number | null = null;
  private previousWindowId: number | null = null;
  private acquisitionCount = 0;
  private releaseCount = 0;
  private lastError: string | null = null;
  private lastTransitionAt: number | null = null;

  constructor(
    private readonly resolveContext: SteamFocusResolver = () =>
      resolveSteamFocusContext(),
    private readonly now: () => number = Date.now,
  ) {}

  acquire(): boolean {
    if (this.ownedLease !== null) {
      return this.revalidateOwnedLease();
    }

    let context: SteamFocusContext;
    try {
      context = this.resolveContext();
      this.available = true;
    } catch (error) {
      this.available = false;
      this.required = false;
      this.setContext(null, null, null);
      this.transition("unsupported", errorMessage(error));
      return false;
    }

    const mainAppId = context.mainRunningAppId;
    if (mainAppId === 0) {
      this.required = false;
      this.setContext(null, null, null);
      this.transition("not-required", null);
      return true;
    }

    this.required = true;
    if (!isPositiveUint32(mainAppId)) {
      this.setContext(null, null, null);
      this.transition(
        "precondition-failed",
        "Steam MainRunningAppID is not a positive 32-bit application ID",
      );
      return false;
    }
    if (context.focusedAppId !== mainAppId) {
      this.setContext(mainAppId, null, null);
      this.transition(
        "precondition-failed",
        "Steam's focused application does not match MainRunningAppID",
      );
      return false;
    }
    if (!isPositiveUint32(context.focusedWindowId)) {
      this.setContext(mainAppId, null, null);
      this.transition(
        "precondition-failed",
        "Steam's focused native window ID is invalid",
      );
      return false;
    }

    const focusedWindowId = context.focusedWindowId;
    let windowIds: unknown;
    let previousWindowId: unknown;
    try {
      windowIds = context.windowStore.GetAppWindowIDs(mainAppId);
      previousWindowId =
        context.windowStore.GetAppFocusedWindowID(mainAppId);
    } catch (error) {
      this.setContext(mainAppId, focusedWindowId, null);
      this.transition("error", errorMessage(error));
      return false;
    }

    if (!isExactWindowList(windowIds, focusedWindowId)) {
      this.setContext(mainAppId, focusedWindowId, null);
      this.transition(
        "precondition-failed",
        "Steam's app window list is not the unique focused native window",
      );
      return false;
    }
    if (!isNonNegativeUint32(previousWindowId)) {
      this.setContext(mainAppId, focusedWindowId, null);
      this.transition(
        "precondition-failed",
        "Steam's selected app window ID is invalid",
      );
      return false;
    }

    this.setContext(mainAppId, focusedWindowId, previousWindowId);
    if (previousWindowId === focusedWindowId) {
      this.transition("already-selected", null);
      return true;
    }
    if (previousWindowId !== 0) {
      this.transition(
        "precondition-failed",
        "Steam already selected a different app window",
      );
      return false;
    }
    if (context.compositionIdle !== true) {
      this.transition(
        "precondition-failed",
        "Steam composition is not at an idle Hidden baseline",
      );
      return false;
    }

    const lease: OwnedFocusLease = {
      windowStore: context.windowStore,
      appId: mainAppId,
      windowId: focusedWindowId,
      previousWindowId,
    };
    // Record tentative ownership before the call so a throw-after-apply can
    // still be rolled back with the same compare-and-swap rules.
    this.ownedLease = lease;
    try {
      context.windowStore.SetFocusedAppWindowID(
        mainAppId,
        focusedWindowId,
      );
      if (
        context.windowStore.GetAppFocusedWindowID(mainAppId) !==
        focusedWindowId
      ) {
        throw new Error("Steam focus setter did not retain the requested ID");
      }
    } catch (error) {
      this.rollbackFailedAcquisition(errorMessage(error));
      return false;
    }

    this.acquisitionCount += 1;
    this.transition("leased", null);
    return true;
  }

  release(): boolean {
    const lease = this.ownedLease;
    if (lease === null) {
      return false;
    }

    this.releaseCount += 1;
    let observation: OwnedLeaseObservation;
    try {
      observation = this.observeOwnedLease(lease);
    } catch (error) {
      this.transition("error", errorMessage(error));
      return false;
    }
    if (observation === "restored") {
      this.ownedLease = null;
      this.transition("restored", null);
      return true;
    }
    if (observation === "lost") {
      this.ownedLease = null;
      this.transition(
        "lost",
        "Steam focus changed while the OSD lease was active; restoration skipped",
      );
      return false;
    }

    try {
      lease.windowStore.SetFocusedAppWindowID(
        lease.appId,
        lease.previousWindowId,
      );
    } catch (error) {
      return this.settleRestoreAttempt(
        lease,
        `Steam focus restore setter failed: ${errorMessage(error)}`,
        "restored",
      );
    }
    return this.settleRestoreAttempt(
      lease,
      "Steam focus setter did not restore the previous ID",
      "restored",
    );
  }

  debugState(): FocusLeaseDebugState {
    return {
      available: this.available,
      required: this.required,
      owned: this.ownedLease !== null,
      phase: this.phase,
      appId: this.appId,
      windowId: this.windowId,
      previousWindowId: this.previousWindowId,
      acquisitionCount: this.acquisitionCount,
      releaseCount: this.releaseCount,
      lastError: this.lastError,
      lastTransitionAt: this.lastTransitionAt,
    };
  }

  private revalidateOwnedLease(): boolean {
    const lease = this.ownedLease;
    if (lease === null) {
      return false;
    }
    try {
      const observation = this.observeOwnedLease(lease);
      if (observation === "owned") {
        return true;
      }
      this.ownedLease = null;
      if (observation === "restored") {
        this.transition("restored", null);
      } else {
        this.transition(
          "lost",
          "Steam focus changed while the OSD lease was active",
        );
      }
      return false;
    } catch (error) {
      this.transition("error", errorMessage(error));
      return false;
    }
  }

  private rollbackFailedAcquisition(reason: string): void {
    const lease = this.ownedLease;
    if (lease === null) {
      this.transition("error", reason);
      return;
    }

    let observation: OwnedLeaseObservation;
    try {
      observation = this.observeOwnedLease(lease);
    } catch (error) {
      this.transition(
        "error",
        `${reason}; rollback state is unreadable: ${errorMessage(error)}`,
      );
      return;
    }
    if (observation === "restored") {
      this.ownedLease = null;
      this.transition("error", reason);
      return;
    }
    if (observation === "lost") {
      this.ownedLease = null;
      this.transition(
        "lost",
        `${reason}; Steam focus changed before rollback`,
      );
      return;
    }

    try {
      lease.windowStore.SetFocusedAppWindowID(
        lease.appId,
        lease.previousWindowId,
      );
    } catch (error) {
      this.settleRestoreAttempt(
        lease,
        `${reason}; rollback setter failed: ${errorMessage(error)}`,
        "error",
      );
      return;
    }
    this.settleRestoreAttempt(
      lease,
      `${reason}; rollback did not restore the previous ID`,
      "error",
    );
  }

  private observeOwnedLease(
    lease: OwnedFocusLease,
  ): OwnedLeaseObservation {
    const selectedWindowId = lease.windowStore.GetAppFocusedWindowID(
      lease.appId,
    );
    if (!isNonNegativeUint32(selectedWindowId)) {
      throw new Error("Steam's selected app window ID became unreadable");
    }
    if (selectedWindowId === lease.previousWindowId) {
      return "restored";
    }
    if (selectedWindowId !== lease.windowId) {
      return "lost";
    }

    const windowIds = lease.windowStore.GetAppWindowIDs(lease.appId);
    if (
      !Array.isArray(windowIds) ||
      windowIds.some((windowId) => !isPositiveUint32(windowId))
    ) {
      throw new Error("Steam's app window list became unreadable");
    }
    return isExactWindowList(windowIds, lease.windowId) ? "owned" : "lost";
  }

  private settleRestoreAttempt(
    lease: OwnedFocusLease,
    failureReason: string,
    restoredPhase: "restored" | "error",
  ): boolean {
    let observation: OwnedLeaseObservation;
    try {
      observation = this.observeOwnedLease(lease);
    } catch (error) {
      this.transition(
        "error",
        `${failureReason}; restore state is unreadable: ${errorMessage(error)}`,
      );
      return false;
    }
    if (observation === "restored") {
      this.ownedLease = null;
      this.transition(
        restoredPhase,
        restoredPhase === "restored" ? null : failureReason,
      );
      return true;
    }
    if (observation === "lost") {
      this.ownedLease = null;
      this.transition(
        "lost",
        `${failureReason}; Steam focus changed and was not overwritten`,
      );
      return false;
    }

    // The compare-and-swap still belongs to this lease. Retain ownership so a
    // later component effect, shutdown path, or explicit release can retry.
    this.transition("error", failureReason);
    return false;
  }

  private setContext(
    appId: number | null,
    windowId: number | null,
    previousWindowId: number | null,
  ): void {
    this.appId = appId;
    this.windowId = windowId;
    this.previousWindowId = previousWindowId;
  }

  private transition(phase: FocusLeasePhase, error: string | null): void {
    this.phase = phase;
    this.lastError = error;
    this.lastTransitionAt = this.now();
  }
}

export interface CompositionRequestPort {
  isAvailable(): boolean;
  isBridgeMounted(): boolean;
  getRequested(): boolean;
  setRequested(requested: boolean): boolean;
}

/**
 * Keeps focus selection and Steam's composition request in a strict order.
 * Acquisition always precedes Notification; restoration happens only after
 * the component has committed the released hook state (or if no bridge exists).
 */
export class CompositionFocusCoordinator {
  constructor(
    private readonly lease: SteamCompositionFocusLease,
    private readonly composition: CompositionRequestPort,
  ) {}

  request(active: boolean): boolean {
    if (active) {
      if (this.composition.getRequested()) {
        return true;
      }
      if (!this.composition.isAvailable()) {
        return false;
      }
      if (!this.lease.acquire()) {
        this.lease.release();
        return false;
      }
      try {
        this.composition.setRequested(true);
      } catch (error) {
        try {
          this.composition.setRequested(false);
        } catch {
          // Preserve the original activation error.
        }
        // A false activity bit does not prove that React has committed the
        // corresponding null Steam hook yet.  Restoring while the bridge is
        // mounted could retarget a still-live Notification request to window
        // zero.  The post-hook effect (or an observed bridge unmount) owns the
        // release in that case.
        if (
          !this.composition.getRequested() &&
          !this.composition.isBridgeMounted()
        ) {
          this.lease.release();
        }
        throw error;
      }
      return true;
    }

    try {
      this.composition.setRequested(false);
    } catch (error) {
      if (
        !this.composition.getRequested() &&
        !this.composition.isBridgeMounted()
      ) {
        this.lease.release();
      }
      throw error;
    }
    if (!this.composition.isBridgeMounted()) {
      this.lease.release();
    }
    return true;
  }

  afterCompositionCommit(requested: boolean): void {
    if (!requested && !this.composition.getRequested()) {
      this.lease.release();
    }
  }

  afterBridgeUnmount(): boolean {
    if (
      this.composition.isBridgeMounted() ||
      this.composition.getRequested()
    ) {
      return false;
    }
    return this.lease.release();
  }

  shutdown(): void {
    try {
      this.composition.setRequested(false);
    } catch {
      // Teardown is best-effort, but never restore focus ahead of a request
      // that the activity store still reports as active.
    }
    if (
      !this.composition.getRequested() &&
      !this.composition.isBridgeMounted()
    ) {
      this.lease.release();
    }
  }
}
