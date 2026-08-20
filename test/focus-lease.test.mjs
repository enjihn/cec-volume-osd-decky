import test from "node:test";
import assert from "node:assert/strict";

import {
  CompositionFocusCoordinator,
  SteamCompositionFocusLease,
  resolveSteamFocusContext,
} from "../.test-build/focus-lease.js";

function fixture(overrides = {}) {
  const runtime = {
    selected: overrides.selected ?? 0,
    windows: overrides.windows ?? [42],
    setterCalls: [],
    getterCalls: 0,
  };
  const store = {
    GetAppWindowIDs(appId) {
      assert.equal(appId, overrides.mainRunningAppId ?? 77);
      if (overrides.throwWindowList) {
        throw new Error("window list failed");
      }
      return runtime.windows;
    },
    GetAppFocusedWindowID(appId) {
      assert.equal(appId, overrides.mainRunningAppId ?? 77);
      runtime.getterCalls += 1;
      if (overrides.throwFocusedGetter) {
        throw new Error("focused getter failed");
      }
      return runtime.selected;
    },
    SetFocusedAppWindowID(appId, windowId) {
      assert.equal(appId, overrides.mainRunningAppId ?? 77);
      runtime.setterCalls.push(windowId);
      runtime.selected = windowId;
      overrides.onSet?.(runtime, windowId);
    },
  };
  const context = {
    windowStore: store,
    mainRunningAppId: overrides.mainRunningAppId ?? 77,
    focusedAppId: overrides.focusedAppId ?? 77,
    focusedWindowId: overrides.focusedWindowId ?? 42,
    compositionIdle: overrides.compositionIdle ?? true,
  };
  let timestamp = 100;
  const lease = new SteamCompositionFocusLease(
    () => {
      if (overrides.resolveError) {
        throw new Error("resolver unavailable");
      }
      return context;
    },
    () => ++timestamp,
  );
  return { context, lease, runtime, store };
}

test("leases zero to the unique Steam-focused native window and CAS-restores it", () => {
  const { lease, runtime } = fixture();

  assert.equal(lease.acquire(), true);
  assert.equal(runtime.selected, 42);
  assert.deepEqual(runtime.setterCalls, [42]);
  assert.deepEqual(lease.debugState(), {
    available: true,
    required: true,
    owned: true,
    phase: "leased",
    appId: 77,
    windowId: 42,
    previousWindowId: 0,
    acquisitionCount: 1,
    releaseCount: 0,
    lastError: null,
    lastTransitionAt: 101,
  });

  assert.equal(lease.release(), true);
  assert.equal(runtime.selected, 0);
  assert.deepEqual(runtime.setterCalls, [42, 0]);
  assert.equal(lease.debugState().phase, "restored");
  assert.equal(lease.debugState().owned, false);
  assert.equal(lease.debugState().releaseCount, 1);
});

test("a repeated acquire is idempotent while the owned mapping is unchanged", () => {
  const { lease, runtime } = fixture();

  assert.equal(lease.acquire(), true);
  assert.equal(lease.acquire(), true);
  assert.deepEqual(runtime.setterCalls, [42]);
  assert.equal(lease.debugState().acquisitionCount, 1);
});

test("no running game and an already-selected game are safe no-write paths", () => {
  const noGame = fixture({
    mainRunningAppId: 0,
    focusedAppId: 769,
    focusedWindowId: 31,
  });
  assert.equal(noGame.lease.acquire(), true);
  assert.deepEqual(noGame.runtime.setterCalls, []);
  assert.equal(noGame.lease.debugState().required, false);
  assert.equal(noGame.lease.debugState().phase, "not-required");

  const alreadySelected = fixture({ selected: 42 });
  assert.equal(alreadySelected.lease.acquire(), true);
  assert.deepEqual(alreadySelected.runtime.setterCalls, []);
  assert.equal(alreadySelected.lease.debugState().owned, false);
  assert.equal(alreadySelected.lease.debugState().phase, "already-selected");
  assert.equal(alreadySelected.lease.release(), false);
});

test("all ambiguous or active Steam focus/composition states fail closed", async (t) => {
  const cases = [
    ["focused app mismatch", { focusedAppId: 88 }],
    ["invalid focused window", { focusedWindowId: 0 }],
    ["multiple app windows", { windows: [42, 43] }],
    ["different selected window", { selected: 41 }],
    ["active Steam composition", { compositionIdle: false }],
    ["unsafe app id", { mainRunningAppId: 0x1_0000_0000 }],
  ];

  for (const [name, options] of cases) {
    await t.test(name, () => {
      const { lease, runtime } = fixture(options);
      assert.equal(lease.acquire(), false);
      assert.deepEqual(runtime.setterCalls, []);
      assert.equal(lease.debugState().owned, false);
      assert.equal(lease.debugState().phase, "precondition-failed");
    });
  }
});

test("resolver and read failures do not call Steam's setter", () => {
  const unresolved = fixture({ resolveError: true });
  assert.equal(unresolved.lease.acquire(), false);
  assert.equal(unresolved.lease.debugState().available, false);
  assert.equal(unresolved.lease.debugState().phase, "unsupported");
  assert.deepEqual(unresolved.runtime.setterCalls, []);

  const unreadable = fixture({ throwWindowList: true });
  assert.equal(unreadable.lease.acquire(), false);
  assert.equal(unreadable.lease.debugState().phase, "error");
  assert.deepEqual(unreadable.runtime.setterCalls, []);
});

test("throw-after-apply is rolled back because ownership is recorded first", () => {
  let first = true;
  const { lease, runtime } = fixture({
    onSet(_runtime, windowId) {
      if (windowId === 42 && first) {
        first = false;
        throw new Error("setter threw after apply");
      }
    },
  });

  assert.equal(lease.acquire(), false);
  assert.equal(runtime.selected, 0);
  assert.deepEqual(runtime.setterCalls, [42, 0]);
  assert.equal(lease.debugState().owned, false);
  assert.equal(lease.debugState().phase, "error");
  assert.match(lease.debugState().lastError, /after apply/);
});

test("release never overwrites a newer Steam selection", () => {
  const { lease, runtime } = fixture();
  assert.equal(lease.acquire(), true);

  runtime.selected = 99;
  assert.equal(lease.release(), false);
  assert.equal(runtime.selected, 99);
  assert.deepEqual(runtime.setterCalls, [42]);
  assert.equal(lease.debugState().phase, "lost");
  assert.equal(lease.debugState().owned, false);
});

test("release never recreates or rewrites an app entry after window-list churn", () => {
  const { lease, runtime } = fixture();
  assert.equal(lease.acquire(), true);

  runtime.windows = [42, 43];
  assert.equal(lease.release(), false);
  assert.equal(runtime.selected, 42);
  assert.deepEqual(runtime.setterCalls, [42]);
  assert.equal(lease.debugState().phase, "lost");
});

test("release retains ownership across a transient read error and retries", () => {
  const { lease, runtime, store } = fixture();
  assert.equal(lease.acquire(), true);
  const readSelected = store.GetAppFocusedWindowID.bind(store);
  let failRead = true;
  store.GetAppFocusedWindowID = (appId) => {
    if (failRead) {
      throw new Error("transient focus read");
    }
    return readSelected(appId);
  };

  assert.equal(lease.release(), false);
  assert.equal(runtime.selected, 42);
  assert.equal(lease.debugState().owned, true);
  assert.equal(lease.debugState().phase, "error");

  failRead = false;
  assert.equal(lease.release(), true);
  assert.equal(runtime.selected, 0);
  assert.equal(lease.debugState().owned, false);
  assert.equal(lease.debugState().releaseCount, 2);
});

test("release retries a setter throw-before-apply", () => {
  const { lease, runtime, store } = fixture();
  assert.equal(lease.acquire(), true);
  const setFocused = store.SetFocusedAppWindowID.bind(store);
  let failWrite = true;
  store.SetFocusedAppWindowID = (appId, windowId) => {
    if (windowId === 0 && failWrite) {
      failWrite = false;
      throw new Error("restore failed before apply");
    }
    return setFocused(appId, windowId);
  };

  assert.equal(lease.release(), false);
  assert.equal(runtime.selected, 42);
  assert.equal(lease.debugState().owned, true);
  assert.equal(lease.release(), true);
  assert.equal(runtime.selected, 0);
  assert.equal(lease.debugState().owned, false);
});

test("release verifies a setter throw-after-apply as restored", () => {
  const { lease, runtime, store } = fixture();
  assert.equal(lease.acquire(), true);
  const setFocused = store.SetFocusedAppWindowID.bind(store);
  let failWrite = true;
  store.SetFocusedAppWindowID = (appId, windowId) => {
    const result = setFocused(appId, windowId);
    if (windowId === 0 && failWrite) {
      failWrite = false;
      throw new Error("restore threw after apply");
    }
    return result;
  };

  assert.equal(lease.release(), true);
  assert.equal(runtime.selected, 0);
  assert.equal(lease.debugState().owned, false);
  assert.equal(lease.debugState().phase, "restored");
});

test("failed acquisition rollback retains ownership until an explicit retry", () => {
  const { lease, runtime, store } = fixture();
  let rollbackFails = true;
  store.SetFocusedAppWindowID = (_appId, windowId) => {
    runtime.setterCalls.push(windowId);
    if (windowId === 42) {
      runtime.selected = 42;
      throw new Error("acquire threw after apply");
    }
    if (rollbackFails) {
      rollbackFails = false;
      throw new Error("rollback threw before apply");
    }
    runtime.selected = windowId;
  };

  assert.equal(lease.acquire(), false);
  assert.equal(runtime.selected, 42);
  assert.equal(lease.debugState().owned, true);
  assert.equal(lease.release(), true);
  assert.equal(runtime.selected, 0);
  assert.equal(lease.debugState().owned, false);
});

test("coordinator sets focus before Notification and restores after its release commit", () => {
  const log = [];
  const { lease, runtime } = fixture({
    onSet(_runtime, windowId) {
      log.push(`focus:${windowId}`);
    },
  });
  let requested = false;
  let bridgeMounted = true;
  const port = {
    isAvailable: () => true,
    isBridgeMounted: () => bridgeMounted,
    getRequested: () => requested,
    setRequested(value) {
      requested = value;
      log.push(`composition:${value}`);
      return true;
    },
  };
  const coordinator = new CompositionFocusCoordinator(lease, port);

  assert.equal(coordinator.request(true), true);
  assert.deepEqual(log, ["focus:42", "composition:true"]);
  assert.equal(runtime.selected, 42);

  assert.equal(coordinator.request(false), true);
  assert.deepEqual(log, ["focus:42", "composition:true", "composition:false"]);
  assert.equal(runtime.selected, 42);

  coordinator.afterCompositionCommit(false);
  assert.deepEqual(log, [
    "focus:42",
    "composition:true",
    "composition:false",
    "focus:0",
  ]);
  assert.equal(runtime.selected, 0);

  bridgeMounted = false;
  assert.equal(coordinator.request(true), true);
  assert.equal(coordinator.request(false), true);
  assert.equal(runtime.selected, 0);
});

test("coordinator never leases focus without a unique composition hook", () => {
  const { lease, runtime } = fixture();
  let requested = false;
  const coordinator = new CompositionFocusCoordinator(lease, {
    isAvailable: () => false,
    isBridgeMounted: () => true,
    getRequested: () => requested,
    setRequested(value) {
      requested = value;
      return true;
    },
  });

  assert.equal(coordinator.request(true), false);
  assert.equal(requested, false);
  assert.equal(runtime.selected, 0);
  assert.deepEqual(runtime.setterCalls, []);
});

test("coordinator transaction unwinds a throw-after-partial composition request", () => {
  const { lease, runtime } = fixture();
  let requested = false;
  let activationThrows = true;
  let bridgeMounted = true;
  const coordinator = new CompositionFocusCoordinator(lease, {
    isAvailable: () => true,
    isBridgeMounted: () => bridgeMounted,
    getRequested: () => requested,
    setRequested(value) {
      requested = value;
      if (value && activationThrows) {
        activationThrows = false;
        throw new Error("partial composition activation");
      }
      return true;
    },
  });

  assert.throws(
    () => coordinator.request(true),
    /partial composition activation/,
  );
  assert.equal(requested, false);
  assert.equal(runtime.selected, 42);
  assert.equal(lease.debugState().owned, true);

  // The false activity bit is not the release boundary.  Steam's hook must
  // first commit null while the global component remains mounted.
  coordinator.afterCompositionCommit(false);
  assert.equal(runtime.selected, 0);
  assert.equal(lease.debugState().owned, false);

  // A missing bridge is the only path allowed to release synchronously.
  bridgeMounted = false;
  activationThrows = true;
  assert.throws(
    () => coordinator.request(true),
    /partial composition activation/,
  );
  assert.equal(runtime.selected, 0);
  assert.equal(lease.debugState().owned, false);
});

test("release exceptions retain focus until the null hook commits", () => {
  const { lease, runtime } = fixture();
  let requested = false;
  let releaseThrows = true;
  const coordinator = new CompositionFocusCoordinator(lease, {
    isAvailable: () => true,
    isBridgeMounted: () => true,
    getRequested: () => requested,
    setRequested(value) {
      requested = value;
      if (!value && releaseThrows) {
        releaseThrows = false;
        throw new Error("partial composition release");
      }
      return true;
    },
  });

  assert.equal(coordinator.request(true), true);
  assert.throws(
    () => coordinator.request(false),
    /partial composition release/,
  );
  assert.equal(requested, false);
  assert.equal(runtime.selected, 42);
  assert.equal(lease.debugState().owned, true);

  coordinator.afterCompositionCommit(false);
  assert.equal(runtime.selected, 0);
  assert.equal(lease.debugState().owned, false);
});

test("partial activation retains focus until a failed release can retry", () => {
  const { lease, runtime } = fixture();
  let requested = false;
  let releaseAttempts = 0;
  const coordinator = new CompositionFocusCoordinator(lease, {
    isAvailable: () => true,
    isBridgeMounted: () => true,
    getRequested: () => requested,
    setRequested(value) {
      if (value) {
        requested = true;
        throw new Error("partial activation");
      }
      releaseAttempts += 1;
      if (releaseAttempts === 1) {
        throw new Error("first composition release failed");
      }
      requested = false;
      return true;
    },
  });

  assert.throws(() => coordinator.request(true), /partial activation/);
  assert.equal(requested, true);
  assert.equal(runtime.selected, 42);
  assert.equal(lease.debugState().owned, true);

  assert.equal(coordinator.request(false), true);
  assert.equal(requested, false);
  assert.equal(runtime.selected, 42);
  coordinator.afterCompositionCommit(false);
  assert.equal(runtime.selected, 0);
  assert.equal(lease.debugState().owned, false);
});

test("shutdown waits for hook cleanup, then retries an owned focus restore", () => {
  const { lease, runtime, store } = fixture();
  let requested = false;
  let bridgeMounted = true;
  const coordinator = new CompositionFocusCoordinator(lease, {
    isAvailable: () => true,
    isBridgeMounted: () => bridgeMounted,
    getRequested: () => requested,
    setRequested(value) {
      requested = value;
      return true;
    },
  });
  assert.equal(coordinator.request(true), true);
  const setFocused = store.SetFocusedAppWindowID.bind(store);
  let failRestore = true;
  store.SetFocusedAppWindowID = (appId, windowId) => {
    if (windowId === 0 && failRestore) {
      failRestore = false;
      throw new Error("first shutdown restore failed");
    }
    return setFocused(appId, windowId);
  };

  assert.equal(coordinator.request(false), true);
  coordinator.shutdown();
  assert.equal(runtime.selected, 42);
  assert.equal(lease.debugState().owned, true);

  coordinator.afterCompositionCommit(false);
  assert.equal(runtime.selected, 42);
  assert.equal(lease.debugState().owned, true);

  bridgeMounted = false;
  coordinator.shutdown();
  assert.equal(runtime.selected, 0);
  assert.equal(lease.debugState().owned, false);
});

test("asynchronous bridge removal cannot restore before hook cleanup", () => {
  const { lease, runtime } = fixture();
  let requested = false;
  let bridgeMounted = true;
  const coordinator = new CompositionFocusCoordinator(lease, {
    isAvailable: () => true,
    isBridgeMounted: () => bridgeMounted,
    getRequested: () => requested,
    setRequested(value) {
      requested = value;
      return true;
    },
  });

  assert.equal(coordinator.request(true), true);
  assert.equal(coordinator.request(false), true);
  assert.equal(coordinator.afterBridgeUnmount(), false);
  assert.equal(runtime.selected, 42);

  // This models removeGlobalComponent completing (or throwing before an
  // already-scheduled unmount) only after Steam's hook cleanup has run.
  bridgeMounted = false;
  assert.equal(coordinator.afterBridgeUnmount(), true);
  assert.equal(runtime.selected, 0);
});

function validatedRoot({ setterArity = 2, compositionState = 0 } = {}) {
  const compositionStore = {
    GetCompositionState() {
      return compositionState;
    },
    GetCurrentlyFocusedAppidSubscribableValue() {
      return { Value: 77 };
    },
    GetCurrentlyFocusedWindowIDSubscribableValue() {
      return { Value: 42 };
    },
    m_mapCompositionStateRequests: new Map([
      [0, 0],
      [1, 0],
      [2, 0],
    ]),
    m_mapCompostionRequestsDebugInfo: new Map(),
  };
  class ValidatedWindowStore {
    m_mapAppWindows = new Map([
      [77, { appid: 77, focusedWindowID: 0, windowids: [42] }],
    ]);

    SetFocusedAppWindowID(appId, windowId) {
      windowId = windowId ?? 0;
      const record = this.m_mapAppWindows.get(appId);
      if (!record) {
        this.m_mapAppWindows.set(appId, {
          appid: appId,
          focusedWindowID: windowId,
          windowids: windowId ? [windowId] : [],
        });
        return;
      }
      record.focusedWindowID = windowId;
      this.m_mapAppWindows.set(appId, record);
    }

    get MainRunningAppWindowIDs() {
      return this.GetAppWindowIDs(77);
    }

    GetAppWindowIDs(appId) {
      return this.m_mapAppWindows.get(appId)?.windowids ?? [];
    }

    GetAppFocusedWindowID(appId) {
      return this.m_mapAppWindows.get(appId)?.focusedWindowID ?? 0;
    }
  }
  if (setterArity === 0) {
    const original =
      ValidatedWindowStore.prototype.SetFocusedAppWindowID;
    const dispatch = (_e, _r, _t, receiver, args) =>
      original.apply(receiver, args);
    const wrapped = Function(
      "qe",
      "e",
      "r",
      "t",
      "n",
      "return function SetFocusedAppWindowID(){return qe(e,r,t,n||this,arguments)}",
    )(dispatch, null, null, null, null);
    Object.defineProperty(
      ValidatedWindowStore.prototype,
      "SetFocusedAppWindowID",
      {
        configurable: true,
        enumerable: false,
        writable: true,
        value: wrapped,
      },
    );
  }
  const windowStore = new ValidatedWindowStore();
  windowStore.GamepadUIMainWindowInstance = {
    MainRunningAppID: 77,
    CompositionStateStore: compositionStore,
  };
  return {
    root: { SteamUIStore: { WindowStore: windowStore } },
    windowStore,
  };
}

test("default resolver accepts only the source-validated arity-0/2 store action", () => {
  for (const setterArity of [0, 2]) {
    const { root, windowStore } = validatedRoot({ setterArity });
    const context = resolveSteamFocusContext(root);
    assert.equal(context.windowStore, windowStore);
    assert.equal(context.mainRunningAppId, 77);
    assert.equal(context.focusedAppId, 77);
    assert.equal(context.focusedWindowId, 42);
    assert.equal(context.compositionIdle, true);
  }
});

test("default resolver supports the validated DFL-only WindowStore fallback", () => {
  const { windowStore } = validatedRoot({ setterArity: 0 });
  for (const steamUiStore of [undefined, {}]) {
    const context = resolveSteamFocusContext({
      ...(steamUiStore === undefined ? {} : { SteamUIStore: steamUiStore }),
      DFL: { Router: { WindowStore: windowStore } },
    });

    assert.equal(context.windowStore, windowStore);
    assert.equal(context.mainRunningAppId, 77);
    assert.equal(context.focusedAppId, 77);
    assert.equal(context.focusedWindowId, 42);
    assert.equal(context.compositionIdle, true);
  }
});

test("default resolver rejects altered setter source and unreadable composition maps", () => {
  const altered = validatedRoot();
  altered.windowStore.SetFocusedAppWindowID = function changed(a, b) {
    void a;
    void b;
  };
  assert.throws(
    () => resolveSteamFocusContext(altered.root),
    /validated signature|validated store action|unique/,
  );

  const missingMap = validatedRoot();
  missingMap.windowStore.GamepadUIMainWindowInstance.CompositionStateStore
    .m_mapCompositionStateRequests = null;
  assert.throws(
    () => resolveSteamFocusContext(missingMap.root),
    /request maps are unavailable/,
  );
});
