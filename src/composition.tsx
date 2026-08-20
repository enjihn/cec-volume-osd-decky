import { modules } from "@decky/ui";
import { useEffect, useSyncExternalStore } from "react";

import {
  COMPOSITION_OWNER,
  CompositionActivityStore,
  NOTIFICATION_COMPOSITION,
  findCompositionHooks,
  type CompositionHook,
} from "./composition-state.js";
import {
  CompositionFocusCoordinator,
  SteamCompositionFocusLease,
} from "./focus-lease.js";

const compositionHooks = findCompositionHooks(modules.values());
const useSteamComposition: CompositionHook | null =
  compositionHooks.length === 1 ? compositionHooks[0] : null;

export const compositionActivity = new CompositionActivityStore(
  compositionHooks.length,
);
export const compositionFocusLease = new SteamCompositionFocusLease();
const compositionCoordinator = new CompositionFocusCoordinator(
  compositionFocusLease,
  compositionActivity,
);

export function requestOsdComposition(active: boolean): boolean {
  return compositionCoordinator.request(active);
}

export function releaseOsdFocusAfterBridgeUnmount(): boolean {
  return compositionCoordinator.afterBridgeUnmount();
}

export function CECVolumeOSDComposition() {
  const requested = useSyncExternalStore(
    compositionActivity.subscribe,
    compositionActivity.getSnapshot,
    compositionActivity.getSnapshot,
  );

  // This is the same minimum-composition hook used by Steam's VolumePopin.
  // It lets Steam arbitrate concurrent QAM, keyboard, modal, and toast states.
  if (useSteamComposition !== null) {
    useSteamComposition(
      requested ? NOTIFICATION_COMPOSITION : null,
      COMPOSITION_OWNER,
    );
  }

  // Keep this effect after Steam's hook: on release, the hook commits its
  // Notification update before the compare-and-swap focus restoration.
  useEffect(() => {
    compositionActivity.setBridgeMounted(true);
    return () => {
      compositionActivity.setBridgeMounted(false);
      // A Decky global-component remount must not cancel an active OSD. Steam's
      // hook cleanup runs first; retaining the request and lease lets the new
      // component reacquire Notification without targeting window ID 0.
      if (!compositionActivity.getRequested()) {
        compositionCoordinator.afterBridgeUnmount();
      }
    };
  }, []);

  useEffect(() => {
    compositionCoordinator.afterCompositionCommit(requested);
  }, [requested]);

  return null;
}
