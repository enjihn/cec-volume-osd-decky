import {
  addEventListener,
  callable,
  definePlugin,
  removeEventListener,
  routerHook,
} from "@decky/api";
import {
  ButtonItem,
  PanelSection,
  PanelSectionRow,
  findSP,
  staticClasses,
} from "@decky/ui";
import { useCallback, useEffect, useState } from "react";

import {
  CECVolumeOSDComposition,
  compositionActivity,
  compositionFocusLease,
  releaseOsdFocusAfterBridgeUnmount,
  requestOsdComposition,
} from "./composition.js";
import { COMPOSITION_COMPONENT_NAME } from "./composition-state.js";
import { SteamVolumeOsd } from "./osd.js";

const PLUGIN_VERSION = "1.0.0-rc.1";
const EVENT_CHANGED = "cec_volume_changed";
const EVENT_ACTIVITY = "cec_volume_activity";
const EVENT_STATUS = "cec_volume_status";
const EVENT_UNAVAILABLE = "cec_volume_unavailable";

interface BackendStatus {
  instance: string;
  available: boolean;
  route: string | null;
  volume: number | null;
  muted: boolean | null;
  generation: number;
  last_confirmed_at: number | null;
  last_error: string | null;
  cec_device_path: string | null;
  compatibility: {
    compatible: boolean;
    reason_code: string;
    message: string;
  };
  runtime: Record<string, string>;
}

const getStatus = callable<[], BackendStatus>("get_status");
const preview = callable<[], boolean>("preview");

function VolumeIcon() {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      height="1em"
      viewBox="0 0 32 32"
      width="1em"
    >
      <path
        d="M5 12h6l7-6v20l-7-6H5z"
        stroke="currentColor"
        strokeLinejoin="round"
        strokeWidth="2.5"
      />
      <path
        d="M22 12c1.1 1.1 1.7 2.4 1.7 4s-.6 2.9-1.7 4M25.5 8.5c2.1 2.1 3.2 4.6 3.2 7.5s-1.1 5.4-3.2 7.5"
        stroke="currentColor"
        strokeLinecap="round"
        strokeWidth="2.1"
      />
    </svg>
  );
}

function StatusContent() {
  const [status, setStatus] = useState<BackendStatus | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      setStatus(await getStatus());
    } catch {
      setStatus(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const changed = addEventListener(EVENT_CHANGED, () => void refresh());
    const status = addEventListener(EVENT_STATUS, () => void refresh());
    const unavailable = addEventListener(EVENT_UNAVAILABLE, () => void refresh());
    return () => {
      removeEventListener(EVENT_CHANGED, changed);
      removeEventListener(EVENT_STATUS, status);
      removeEventListener(EVENT_UNAVAILABLE, unavailable);
    };
  }, [refresh]);

  const stateText = loading
    ? "Connecting…"
    : status?.available
      ? `Confirmed HDMI-CEC state: ${status.volume === null ? "volume unknown" : Math.round(status.volume)}${
          status.muted === true ? " · muted" : ""
        }`
      : status?.compatibility.message ?? "Confirmed HDMI-CEC state unavailable";

  return (
    <>
      <PanelSection title="CEC monitor">
        <PanelSectionRow>
          <div style={{ lineHeight: 1.35 }}>{stateText}</div>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            disabled={status?.available !== true}
            layout="below"
            onClick={() => void preview()}
          >
            Preview volume display
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <div style={{ fontSize: "0.8em", opacity: 0.72 }}>
            Preview is visual only and never changes soundbar volume.
          </div>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  let compositionComponentRegistered = false;
  let compositionRegistrationError: string | null = null;
  try {
    routerHook.addGlobalComponent(
      COMPOSITION_COMPONENT_NAME,
      CECVolumeOSDComposition,
    );
    compositionComponentRegistered = true;
  } catch (error) {
    compositionRegistrationError =
      error instanceof Error ? error.message : String(error);
  }

  const osd = new SteamVolumeOsd(() => {
    try {
      return findSP() ?? null;
    } catch {
      return null;
    }
  }, window, requestOsdComposition);
  osd.requireCecInstanceBinding();
  const syncCecInstance = async (): Promise<void> => {
    try {
      const status = await getStatus();
      osd.bindCecInstance(status.instance);
    } catch {
      // Stay fail-closed until the backend supplies its current identity.
    }
  };
  void syncCecInstance();
  const statusListener = addEventListener<[payload: unknown]>(
    EVENT_STATUS,
    (payload) => {
      if (
        typeof payload !== "object" ||
        payload === null ||
        !osd.bindCecInstance(
          (payload as { instance?: unknown }).instance,
        )
      ) {
        void syncCecInstance();
      }
    },
  );
  const changedListener = addEventListener<[payload: unknown]>(
    EVENT_CHANGED,
    (payload) => {
      osd.handle(payload);
    },
  );
  const activityListener = addEventListener(EVENT_ACTIVITY, () => {
    osd.keepAlive();
  });
  const unavailableListener = addEventListener(EVENT_UNAVAILABLE, () => {
    osd.hide();
  });

  Object.defineProperty(window, "__CEC_VOLUME_OSD_DIAGNOSTICS__", {
    configurable: true,
    value: Object.freeze({
      version: PLUGIN_VERSION,
      getState: () => ({
        ...osd.debugState(),
        ...compositionActivity.debugState(),
        focusLease: compositionFocusLease.debugState(),
        compositionComponentRegistered,
        compositionRegistrationError,
      }),
      hide: () => osd.hide(),
    }),
  });

  return {
    name: "CEC Volume OSD",
    titleView: (
      <div className={staticClasses.Title}>CEC Volume OSD</div>
    ),
    content: <StatusContent />,
    icon: <VolumeIcon />,
    onDismount() {
      try {
        removeEventListener(EVENT_STATUS, statusListener);
        removeEventListener(EVENT_CHANGED, changedListener);
        removeEventListener(EVENT_ACTIVITY, activityListener);
        removeEventListener(EVENT_UNAVAILABLE, unavailableListener);
      } catch {
        // Continue teardown so composition can never remain requested.
      }

      try {
        osd.hide();
      } finally {
        requestOsdComposition(false);
        if (compositionComponentRegistered) {
          try {
            routerHook.removeGlobalComponent(COMPOSITION_COMPONENT_NAME);
          } catch {
            // The activity store is already released; never abort teardown.
          } finally {
            compositionComponentRegistered = false;
          }
        }
        // removeGlobalComponent may unmount asynchronously. Only restore here
        // if the bridge already proves that Steam's hook cleanup has run;
        // otherwise the component's post-hook effect/cleanup owns release.
        releaseOsdFocusAfterBridgeUnmount();
        delete window.__CEC_VOLUME_OSD_DIAGNOSTICS__;
      }
    },
  };
});
