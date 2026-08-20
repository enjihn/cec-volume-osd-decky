import {
  FADE_IN_MS,
  FADE_OUT_MS,
  HOLD_MS,
  fillPercent,
  normalizeVolumeEvent,
  type VolumePresentation,
} from "./presentation.js";

const ROOT_ID = "cec-volume-osd-steam-root";
const FADE_TEARDOWN_GRACE_MS = 50;

interface TimerHost {
  setTimeout(handler: () => void, timeout: number): number;
  clearTimeout(handle: number): void;
}

interface DebugState {
  mounted: boolean;
  visible: boolean;
  diagnostic: boolean;
  eventCount: number;
  lastHandledAt: number | null;
  route: string | null;
  volume: number | null;
  muted: boolean | null;
  ownerTitle: string | null;
  expectedCecInstance: string | null;
  lastCecInstance: string | null;
  lastCecGeneration: number | null;
  lastError: string | null;
}

type SteamWindowFinder = () => Window | null;
type CompositionRequest = (active: boolean) => boolean | void;

export class SteamVolumeOsd {
  private host: HTMLDivElement | null = null;
  private presentation: VolumePresentation | null = null;
  private hideTimer: number | null = null;
  private removeTimer: number | null = null;
  private fadeTransition:
    | {
        host: HTMLDivElement;
        listener: (event: TransitionEvent) => void;
      }
    | null = null;
  private eventCount = 0;
  private lastHandledAt: number | null = null;
  private cecBindingRequired = false;
  private expectedCecInstance: string | null = null;
  private lastCecInstance: string | null = null;
  private lastCecGeneration: number | null = null;
  private readonly seenCecInstances = new Set<string>();
  private lastError: string | null = null;
  private compositionRequested = false;
  private ownerWindow: Window | null = null;
  private readonly ownerWindowClosed = (): void => this.hide();

  constructor(
    private readonly findSteamWindow: SteamWindowFinder,
    private readonly timers: TimerHost = window,
    private readonly requestComposition: CompositionRequest = () => {},
  ) {}

  requireCecInstanceBinding(): void {
    this.cecBindingRequired = true;
  }

  bindCecInstance(instance: unknown): boolean {
    if (
      typeof instance !== "string" ||
      instance.length === 0 ||
      instance.length > 128
    ) {
      return false;
    }
    if (instance === this.expectedCecInstance) {
      return true;
    }
    this.expectedCecInstance = instance;
    this.lastCecInstance = null;
    this.lastCecGeneration = null;
    return true;
  }

  keepAlive(): boolean {
    const host = this.host;
    if (
      host === null ||
      !host.isConnected ||
      this.presentation === null ||
      this.presentation.diagnostic
    ) {
      return false;
    }
    if (host.dataset.phase === "fading") {
      this.setCompositionRequested(true);
      host.dataset.visible = "true";
      host.dataset.phase = "holding";
    }
    this.scheduleDismissal(host);
    return true;
  }

  handle(raw: unknown): boolean {
    const presentation = normalizeVolumeEvent(raw);
    if (presentation === null) {
      this.hide();
      this.lastError = "rejected invalid volume event";
      return false;
    }
    if (
      presentation.source === "cec" &&
      presentation.instance !== null &&
      presentation.generation !== null &&
      ((this.cecBindingRequired &&
        presentation.instance !== this.expectedCecInstance) ||
        (presentation.instance === this.lastCecInstance &&
        this.lastCecGeneration !== null &&
        presentation.generation <= this.lastCecGeneration) ||
        (presentation.instance !== this.lastCecInstance &&
          this.seenCecInstances.has(presentation.instance)))
    ) {
      return true;
    }

    try {
      const host = this.ensureHost();
      if (host === null) {
        this.lastError = "Steam GamepadUI document is unavailable";
        return false;
      }

      this.presentation = presentation;
      this.eventCount += 1;
      this.lastHandledAt = Date.now();
      this.lastError = null;
      if (!presentation.diagnostic) {
        this.setCompositionRequested(true);
      }
      this.render(host, presentation);
      this.scheduleDismissal(host);
      if (
        presentation.source === "cec" &&
        presentation.instance !== null &&
        presentation.generation !== null
      ) {
        this.lastCecInstance = presentation.instance;
        this.lastCecGeneration = presentation.generation;
        this.seenCecInstances.add(presentation.instance);
      }
      return true;
    } catch (error) {
      this.lastError = error instanceof Error ? error.message : String(error);
      this.hide();
      return false;
    }
  }

  hide(): void {
    this.clearTimers();
    this.host?.remove();
    this.host = null;
    this.presentation = null;
    this.detachOwnerWindow();
    this.setCompositionRequested(false);
  }

  debugState(): DebugState {
    return {
      mounted: this.host?.isConnected === true,
      visible: this.host?.dataset.visible === "true",
      diagnostic: this.host?.dataset.diagnostic === "true",
      eventCount: this.eventCount,
      lastHandledAt: this.lastHandledAt,
      route: this.presentation?.route ?? null,
      volume: this.presentation?.volume ?? null,
      muted: this.presentation?.muted ?? null,
      ownerTitle: this.host?.ownerDocument.title ?? null,
      expectedCecInstance: this.expectedCecInstance,
      lastCecInstance: this.lastCecInstance,
      lastCecGeneration: this.lastCecGeneration,
      lastError: this.lastError,
    };
  }

  private steamWindow(): Window | null {
    try {
      return this.findSteamWindow();
    } catch {
      return null;
    }
  }

  private ensureHost(): HTMLDivElement | null {
    const steamWindow = this.steamWindow();
    if (steamWindow === null) {
      return null;
    }
    const document = steamWindow.document;
    if (document?.body === undefined || document.body === null) {
      return null;
    }

    if (this.host?.ownerDocument === document && this.host.isConnected) {
      return this.host;
    }

    this.clearTimers();
    this.host?.remove();
    this.host = null;
    this.presentation = null;
    this.detachOwnerWindow();
    this.setCompositionRequested(false);
    document.getElementById(ROOT_ID)?.remove();

    const host = document.createElement("div");
    host.id = ROOT_ID;
    host.setAttribute("aria-hidden", "true");
    host.dataset.visible = "false";
    host.dataset.diagnostic = "false";
    host.dataset.phase = "hidden";
    const shadow = host.attachShadow({ mode: "open" });
    shadow.innerHTML = markup();
    document.body.appendChild(host);
    this.host = host;
    this.attachOwnerWindow(steamWindow);
    return host;
  }

  private render(host: HTMLDivElement, presentation: VolumePresentation): void {
    const shadow = host.shadowRoot;
    if (shadow === null) {
      throw new Error("volume OSD shadow root is missing");
    }

    const card = shadow.querySelector<HTMLElement>("[data-cec-card]");
    if (card === null) {
      throw new Error("volume OSD elements are missing");
    }

    card.style.setProperty("--cec-level", `${fillPercent(presentation)}%`);
    card.dataset.muted = String(presentation.muted === true);
    host.dataset.route = presentation.route;
    host.dataset.volume = presentation.volume === null ? "" : String(presentation.volume);
    host.dataset.muted = presentation.muted === null ? "" : String(presentation.muted);
    host.dataset.source = presentation.source;
    host.dataset.diagnostic = String(presentation.diagnostic);

    if (host.dataset.visible === "true") {
      host.dataset.phase = "holding";
      return;
    }

    if (host.dataset.phase === "fading") {
      host.dataset.visible = "true";
      host.dataset.phase = "holding";
      return;
    }

    if (host.dataset.phase === "entering") {
      return;
    }

    host.dataset.visible = "false";
    host.dataset.phase = "entering";
    const ownerWindow = host.ownerDocument.defaultView;
    if (ownerWindow === null) {
      throw new Error("Steam GamepadUI window is unavailable");
    }

    // Two animation frames guarantee that Chromium paints the initial
    // transparent state before beginning the CSS opacity transition.
    ownerWindow.requestAnimationFrame(() => {
      if (this.host !== host || !host.isConnected) {
        return;
      }
      ownerWindow.requestAnimationFrame(() => {
        if (this.host !== host || !host.isConnected) {
          return;
        }
        host.dataset.visible = "true";
        host.dataset.phase = "holding";
      });
    });
  }

  private scheduleDismissal(host: HTMLDivElement): void {
    this.clearTimers();
    this.hideTimer = this.timers.setTimeout(() => {
      if (this.host !== host) {
        return;
      }
      host.dataset.visible = "false";
      host.dataset.phase = "fading";
      const listener = (event: TransitionEvent): void => {
        if (event.target !== host || event.propertyName !== "opacity") {
          return;
        }
        this.finishFade(host);
      };
      host.addEventListener("transitionend", listener);
      this.fadeTransition = { host, listener };
      this.removeTimer = this.timers.setTimeout(() => {
        this.removeTimer = null;
        this.finishFade(host);
      }, FADE_OUT_MS + FADE_TEARDOWN_GRACE_MS);
      this.hideTimer = null;
    }, HOLD_MS);
  }

  private finishFade(host: HTMLDivElement): void {
    if (this.host !== host || host.dataset.phase !== "fading") {
      return;
    }
    if (this.removeTimer !== null) {
      this.timers.clearTimeout(this.removeTimer);
      this.removeTimer = null;
    }
    this.clearFadeTransition();

    const remove = (): void => {
      if (this.host !== host || host.dataset.phase !== "fading") {
        return;
      }
      host.remove();
      this.host = null;
      this.presentation = null;
      this.detachOwnerWindow();
      this.setCompositionRequested(false);
    };
    // transitionend fires after the last fade frame. The timer fallback also
    // expires after the full fade plus grace, so synchronous removal here
    // cannot truncate the animation and cannot depend on a retiring window's
    // animation-frame queue.
    remove();
  }

  private clearTimers(): void {
    if (this.hideTimer !== null) {
      this.timers.clearTimeout(this.hideTimer);
      this.hideTimer = null;
    }
    if (this.removeTimer !== null) {
      this.timers.clearTimeout(this.removeTimer);
      this.removeTimer = null;
    }
    this.clearFadeTransition();
  }

  private clearFadeTransition(): void {
    if (this.fadeTransition === null) {
      return;
    }
    this.fadeTransition.host.removeEventListener(
      "transitionend",
      this.fadeTransition.listener,
    );
    this.fadeTransition = null;
  }

  private attachOwnerWindow(ownerWindow: Window): void {
    this.detachOwnerWindow();
    this.ownerWindow = ownerWindow;
    ownerWindow.addEventListener("pagehide", this.ownerWindowClosed);
    ownerWindow.addEventListener("unload", this.ownerWindowClosed);
  }

  private detachOwnerWindow(): void {
    if (this.ownerWindow === null) {
      return;
    }
    this.ownerWindow.removeEventListener("pagehide", this.ownerWindowClosed);
    this.ownerWindow.removeEventListener("unload", this.ownerWindowClosed);
    this.ownerWindow = null;
  }

  private setCompositionRequested(active: boolean): boolean {
    if (active === this.compositionRequested) {
      return true;
    }
    let accepted: boolean | void;
    try {
      accepted = this.requestComposition(active);
    } catch (error) {
      if (active) {
        // The callback may have acquired focus or partially changed its
        // activity store before throwing. Explicitly unwind while our local
        // state remains released, then preserve the activation error.
        try {
          this.requestComposition(false);
        } catch {
          // The coordinator retains uncertain ownership for a later retry.
        }
        this.compositionRequested = false;
        throw error;
      }
      // Keep local ownership true. A repeated hide/teardown call must retry
      // instead of declaring a potentially live Steam request released.
      return false;
    }
    if (active && accepted === false) {
      // Keep the local state released. A later confirmed event on this same
      // DOM host must retry after QAM or another Steam owner has gone away.
      return false;
    }
    this.compositionRequested = active;
    return true;
  }
}

function markup(): string {
  return `
    <style>
      :host {
        --cec-fade-in: ${FADE_IN_MS}ms;
        --cec-fade-out: ${FADE_OUT_MS}ms;
        position: fixed;
        z-index: 2147483647;
        top: calc(50% - 16.666667vh);
        right: 4.444444vh;
        width: 8.148148vh;
        height: 33.333333vh;
        display: block;
        pointer-events: none !important;
        user-select: none;
        touch-action: none;
        contain: layout style;
        opacity: 0;
        transition: opacity var(--cec-fade-out) ease-in;
      }

      :host([data-visible="true"]) {
        opacity: 1;
        transition-duration: var(--cec-fade-in);
        transition-timing-function: ease-out;
      }

      :host([data-diagnostic="true"]) {
        visibility: hidden !important;
        opacity: 0 !important;
      }

      *, *::before, *::after {
        box-sizing: border-box;
      }

      .card {
        --cec-level: 0%;
        width: 100%;
        height: 100%;
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 1.111111vh;
        padding: 1.851852vh 1.296296vh;
        overflow: hidden;
        color: #f5f9ff;
        border: 0.092593vh solid rgba(135, 196, 255, 0.22);
        border-radius: 2.592593vh;
        background:
          linear-gradient(180deg, rgba(27, 42, 57, 0.88), rgba(12, 22, 32, 0.84));
        box-shadow:
          0 1.111111vh 3.703704vh rgba(0, 0, 0, 0.42),
          inset 0 0.092593vh 0 rgba(255, 255, 255, 0.08);
      }

      .speaker {
        position: relative;
        width: 3.333333vh;
        height: 3.333333vh;
        flex: 0 0 auto;
      }

      .speaker svg {
        width: 100%;
        height: 100%;
        overflow: visible;
      }

      .speaker-body,
      .speaker-wave,
      .speaker-slash {
        fill: none;
        stroke: currentColor;
        stroke-linecap: round;
        stroke-linejoin: round;
      }

      .speaker-body {
        stroke-width: 2.5;
      }

      .speaker-wave {
        stroke-width: 2.1;
      }

      .speaker-slash {
        opacity: 0;
        color: #f7fbff;
        stroke-width: 3;
      }

      .card[data-muted="true"] .speaker-wave {
        opacity: 0.16;
      }

      .card[data-muted="true"] .speaker-slash {
        opacity: 1;
      }

      .meter {
        position: relative;
        width: 1.851852vh;
        min-height: 0;
        flex: 1 1 auto;
        overflow: hidden;
        border: 0.092593vh solid rgba(255, 255, 255, 0.10);
        border-radius: 999px;
        background: rgba(3, 10, 17, 0.58);
        box-shadow:
          inset 0 0.185185vh 0.555556vh rgba(0, 0, 0, 0.45),
          0 0.092593vh 0 rgba(255, 255, 255, 0.06);
      }

      .meter::after {
        content: "";
        position: absolute;
        inset: 0;
        z-index: 2;
        border-radius: inherit;
        background: linear-gradient(
          90deg,
          rgba(255, 255, 255, 0.12),
          transparent 42%,
          rgba(255, 255, 255, 0.04)
        );
      }

      .fill {
        position: absolute;
        z-index: 1;
        left: 0;
        right: 0;
        bottom: 0;
        height: var(--cec-level);
        min-height: 0;
        border-radius: 999px;
        background:
          linear-gradient(180deg, #65c8ff 0%, #1a9fff 48%, #0879cf 100%);
        box-shadow:
          0 0 1.111111vh rgba(26, 159, 255, 0.70),
          inset 0 0.092593vh 0 rgba(255, 255, 255, 0.48);
      }

      .card[data-muted="true"] .fill {
        opacity: 0.28;
        box-shadow: none;
      }
    </style>
    <div class="card" data-cec-card data-muted="false">
      <div class="speaker" aria-hidden="true">
        <svg viewBox="0 0 32 32" role="presentation">
          <path class="speaker-body" d="M5 12h6l7-6v20l-7-6H5z" />
          <path class="speaker-wave" d="M22 12c1.1 1.1 1.7 2.4 1.7 4s-.6 2.9-1.7 4" />
          <path class="speaker-wave" d="M25.5 8.5c2.1 2.1 3.2 4.6 3.2 7.5s-1.1 5.4-3.2 7.5" />
          <path class="speaker-slash" d="M5 5l22 22" />
        </svg>
      </div>
      <div class="meter" aria-hidden="true">
        <div class="fill"></div>
      </div>
    </div>
  `;
}
