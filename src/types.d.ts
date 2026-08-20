declare global {
  interface Window {
    __CEC_VOLUME_OSD_DIAGNOSTICS__?: {
      version: string;
      getState: () => {
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
        compositionAvailable: boolean;
        compositionHookAvailable: boolean;
        compositionCandidateCount: number;
        compositionBridgeMounted: boolean;
        compositionRequested: boolean;
        compositionReleased: boolean;
        compositionMode: "Notification" | null;
        compositionOwner: "CECVolumeOSD";
        requestedComposition: "Notification" | null;
        compositionOwnerToken: "CECVolumeOSD";
        compositionReleaseState: "unsupported" | "pending" | "held" | "released";
        compositionTransitionCount: number;
        compositionLastTransition: "initial" | "requested" | "released";
        compositionLastTransitionAt: number | null;
        focusLease: {
          available: boolean;
          required: boolean;
          owned: boolean;
          phase:
            | "idle"
            | "not-required"
            | "leased"
            | "already-selected"
            | "unsupported"
            | "precondition-failed"
            | "lost"
            | "restored"
            | "error";
          appId: number | null;
          windowId: number | null;
          previousWindowId: number | null;
          acquisitionCount: number;
          releaseCount: number;
          lastError: string | null;
          lastTransitionAt: number | null;
        };
        compositionComponentRegistered: boolean;
        compositionRegistrationError: string | null;
      };
      hide: () => void;
    };
  }
}

export {};
