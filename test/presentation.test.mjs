import test from "node:test";
import assert from "node:assert/strict";
import { Window } from "happy-dom";

import {
  FADE_IN_MS,
  FADE_OUT_MS,
  HOLD_MS,
  fillPercent,
  normalizeVolumeEvent,
} from "../.test-build/presentation.js";
import { SteamVolumeOsd } from "../.test-build/osd.js";

class FakeTimers {
  nextId = 0;
  tasks = new Map();

  setTimeout(handler, timeout) {
    const id = ++this.nextId;
    this.tasks.set(id, { handler, timeout });
    return id;
  }

  clearTimeout(id) {
    this.tasks.delete(id);
  }

  count(timeout) {
    return [...this.tasks.values()].filter((task) => task.timeout === timeout)
      .length;
  }

  run(timeout) {
    const entry = [...this.tasks.entries()].find(
      ([, task]) => task.timeout === timeout,
    );
    assert.ok(entry, `missing ${timeout}ms timer`);
    this.tasks.delete(entry[0]);
    entry[1].handler();
  }
}

class FakeAnimationFrames {
  tasks = [];

  request(callback) {
    this.tasks.push(callback);
    return this.tasks.length;
  }

  runFrame() {
    const tasks = this.tasks;
    this.tasks = [];
    assert.ok(tasks.length > 0, "missing animation frame");
    for (const callback of tasks) {
      callback(0);
    }
  }
}

function steamWindow(
  title = "Steam Big Picture Mode",
  animationFrames = null,
) {
  const window = new Window({ url: "https://steamloopback.host/" });
  window.document.title = title;
  window.requestAnimationFrame = (callback) => {
    if (animationFrames !== null) {
      return animationFrames.request(callback);
    }
    callback(0);
    return 1;
  };
  return window;
}

function event(
  volume,
  muted = false,
  source = "cec",
  generation = volume,
  instance = "backend-a",
) {
  return {
    instance,
    route: "hdmi-output-0",
    volume,
    muted,
    generation,
    source,
  };
}

function meterLevel(host) {
  return host.shadowRoot
    .querySelector("[data-cec-card]")
    .style.getPropertyValue("--cec-level");
}

test("uses the requested transient timing", () => {
  assert.equal(FADE_IN_MS, 100);
  assert.equal(HOLD_MS, 5000);
  assert.equal(FADE_OUT_MS, 180);
});

test("normalizes confirmed CEC state", () => {
  const state = normalizeVolumeEvent({
    instance: "backend-a",
    route: "hdmi-output-0",
    volume: 16,
    muted: false,
    generation: 4,
    source: "cec",
  });
  assert.deepEqual(state, {
    instance: "backend-a",
    route: "hdmi-output-0",
    volume: 16,
    muted: false,
    generation: 4,
    source: "cec",
    diagnostic: false,
  });
  assert.equal(fillPercent(state), 16);
});

test("preserves mute-only state without inventing volume", () => {
  const state = normalizeVolumeEvent({
    instance: "backend-a",
    route: "hdmi-output-0",
    volume: null,
    muted: true,
    generation: 4,
  });
  assert.ok(state);
  assert.equal(state.volume, null);
  assert.equal(fillPercent(state), 0);
});

test("forces diagnostic events into the hidden path", () => {
  const state = normalizeVolumeEvent({
    route: "diagnostic",
    volume: 73,
    muted: false,
    source: "diagnostic",
  });
  assert.ok(state);
  assert.equal(state.instance, null);
  assert.equal(state.diagnostic, true);
  assert.equal(state.source, "diagnostic");
});

test("rejects malformed or out-of-range telemetry", () => {
  const invalid = [
    null,
    [],
    {},
    { route: "", volume: 10, muted: false },
    { route: "r", volume: -1, muted: false },
    { route: "r", volume: 101, muted: false },
    { route: "r", volume: Number.NaN, muted: false },
    { route: "r", volume: 10, muted: "false" },
    { route: "r", volume: null, muted: null },
    {
      route: "r",
      volume: 10,
      muted: false,
      generation: 1,
      source: "cec",
    },
    {
      instance: "backend-a",
      route: "r",
      volume: 10,
      muted: false,
      source: "cec",
    },
  ];
  for (const value of invalid) {
    assert.equal(normalizeVolumeEvent(value), null);
  }
});

test("paints an entering frame, holds, fades, and fully removes the Steam DOM host", () => {
  const animationFrames = new FakeAnimationFrames();
  const window = steamWindow("Steam Big Picture Mode", animationFrames);
  const timers = new FakeTimers();
  const osd = new SteamVolumeOsd(() => window, timers);

  assert.equal(osd.handle(event(73, false, "diagnostic")), true);
  const host = window.document.getElementById("cec-volume-osd-steam-root");
  assert.ok(host);
  assert.equal(host.dataset.visible, "false");
  assert.equal(host.dataset.phase, "entering");
  assert.equal(host.dataset.diagnostic, "true");
  assert.equal(host.dataset.volume, "73");
  assert.equal(host.shadowRoot.querySelector("[data-cec-number]"), null);
  assert.equal(meterLevel(host), "73%");
  assert.match(
    host.shadowRoot.querySelector("style").textContent,
    /pointer-events:\s*none\s*!important/,
  );
  assert.doesNotMatch(
    host.shadowRoot.querySelector("style").textContent,
    /backdrop-filter|(?:^|[;{\n]\s*)filter\s*:|translate3d/,
  );
  assert.match(
    host.shadowRoot.querySelector("style").textContent,
    /transition:\s*opacity/,
  );
  assert.match(
    host.shadowRoot.querySelector("style").textContent,
    new RegExp(`--cec-fade-in:\\s*${FADE_IN_MS}ms`),
  );
  assert.match(
    host.shadowRoot.querySelector("style").textContent,
    new RegExp(`--cec-fade-out:\\s*${FADE_OUT_MS}ms`),
  );

  animationFrames.runFrame();
  assert.equal(host.dataset.visible, "false");
  assert.equal(host.dataset.phase, "entering");
  animationFrames.runFrame();
  assert.equal(host.dataset.visible, "true");
  assert.equal(host.dataset.phase, "holding");

  assert.equal(timers.count(HOLD_MS), 1);
  timers.run(HOLD_MS);
  assert.equal(host.dataset.visible, "false");
  assert.equal(host.dataset.phase, "fading");
  assert.equal(timers.count(FADE_OUT_MS + 50), 1);
  const transitionEnd = new window.Event("transitionend", { bubbles: true });
  Object.defineProperty(transitionEnd, "propertyName", { value: "opacity" });
  host.dispatchEvent(transitionEnd);
  assert.equal(timers.count(FADE_OUT_MS + 50), 0);
  assert.equal(
    window.document.getElementById("cec-volume-osd-steam-root"),
    null,
  );
  assert.equal(osd.debugState().mounted, false);
  assert.equal(timers.tasks.size, 0);
});

test("a second event during entry reuses the host and extends dismissal", () => {
  const animationFrames = new FakeAnimationFrames();
  const window = steamWindow("Steam Big Picture Mode", animationFrames);
  const timers = new FakeTimers();
  const osd = new SteamVolumeOsd(() => window, timers);

  assert.equal(osd.handle(event(14, false, "cec", 14)), true);
  const firstHost = window.document.getElementById(
    "cec-volume-osd-steam-root",
  );
  const firstShadow = firstHost.shadowRoot;
  const firstCard = firstShadow.querySelector("[data-cec-card]");
  const firstTimerId = [...timers.tasks.keys()][0];

  animationFrames.runFrame();
  assert.equal(firstHost.dataset.visible, "false");
  assert.equal(firstHost.dataset.phase, "entering");

  assert.equal(osd.handle(event(16, false, "cec", 16)), true);
  const secondHost = window.document.getElementById(
    "cec-volume-osd-steam-root",
  );
  assert.equal(secondHost, firstHost);
  assert.equal(secondHost.shadowRoot, firstShadow);
  assert.equal(
    secondHost.shadowRoot.querySelector("[data-cec-card]"),
    firstCard,
  );
  assert.equal(secondHost.dataset.visible, "false");
  assert.equal(secondHost.dataset.phase, "entering");
  assert.equal(secondHost.dataset.volume, "16");
  assert.equal(meterLevel(secondHost), "16%");
  assert.equal(timers.tasks.has(firstTimerId), false);
  assert.equal(timers.count(HOLD_MS), 1);

  animationFrames.runFrame();
  assert.equal(secondHost.dataset.visible, "true");
  assert.equal(secondHost.dataset.phase, "holding");

  timers.run(HOLD_MS);
  timers.run(FADE_OUT_MS + 50);
  assert.equal(
    window.document.getElementById("cec-volume-osd-steam-root"),
    null,
  );
});

test("a later change coalesces the dismissal deadline", () => {
  const window = steamWindow();
  const timers = new FakeTimers();
  const osd = new SteamVolumeOsd(() => window, timers);

  osd.handle(event(14));
  const firstHost = window.document.getElementById(
    "cec-volume-osd-steam-root",
  );
  const firstShadow = firstHost.shadowRoot;
  const firstCard = firstShadow.querySelector("[data-cec-card]");
  const firstTimerId = [...timers.tasks.keys()][0];
  assert.equal(timers.count(HOLD_MS), 1);

  osd.handle(event(16));
  const secondHost = window.document.getElementById(
    "cec-volume-osd-steam-root",
  );
  assert.equal(secondHost, firstHost);
  assert.equal(secondHost.shadowRoot, firstShadow);
  assert.equal(
    secondHost.shadowRoot.querySelector("[data-cec-card]"),
    firstCard,
  );
  assert.equal(timers.count(HOLD_MS), 1);
  assert.equal(timers.tasks.has(firstTimerId), false);
  assert.equal(secondHost.dataset.volume, "16");
  assert.equal(meterLevel(secondHost), "16%");
  timers.run(HOLD_MS);
  assert.equal(secondHost.dataset.visible, "false");
  assert.equal(secondHost.dataset.phase, "fading");
  assert.equal(timers.count(FADE_OUT_MS + 50), 1);

  osd.handle(event(18));
  const revivedHost = window.document.getElementById(
    "cec-volume-osd-steam-root",
  );
  assert.equal(revivedHost, firstHost);
  assert.equal(revivedHost.dataset.visible, "true");
  assert.equal(revivedHost.dataset.phase, "holding");
  assert.equal(timers.count(FADE_OUT_MS + 50), 0);
  assert.equal(timers.count(HOLD_MS), 1);
  assert.equal(revivedHost.dataset.volume, "18");
  assert.equal(meterLevel(revivedHost), "18%");

  timers.run(HOLD_MS);
  timers.run(FADE_OUT_MS + 50);
  assert.equal(
    window.document.getElementById("cec-volume-osd-steam-root"),
    null,
  );
});

test("command activity extends and can revive the existing real OSD", () => {
  const window = steamWindow();
  const timers = new FakeTimers();
  const osd = new SteamVolumeOsd(() => window, timers);

  osd.handle(event(20));
  const host = window.document.getElementById(
    "cec-volume-osd-steam-root",
  );
  const firstTimerId = [...timers.tasks.keys()][0];

  assert.equal(osd.keepAlive(), true);
  assert.equal(timers.tasks.has(firstTimerId), false);
  assert.equal(timers.count(HOLD_MS), 1);
  assert.equal(host.dataset.volume, "20");

  timers.run(HOLD_MS);
  assert.equal(host.dataset.phase, "fading");
  assert.equal(osd.keepAlive(), true);
  assert.equal(host.dataset.visible, "true");
  assert.equal(host.dataset.phase, "holding");
  assert.equal(timers.count(FADE_OUT_MS + 50), 0);
  assert.equal(timers.count(HOLD_MS), 1);

  timers.run(HOLD_MS);
  timers.run(FADE_OUT_MS + 50);
  assert.equal(
    window.document.getElementById("cec-volume-osd-steam-root"),
    null,
  );
});

test("stale CEC generations cannot overwrite the current display", () => {
  const window = steamWindow();
  const timers = new FakeTimers();
  const osd = new SteamVolumeOsd(() => window, timers);

  osd.handle(event(22, false, "cec", 8));
  const host = window.document.getElementById("cec-volume-osd-steam-root");
  const timerId = [...timers.tasks.keys()][0];

  assert.equal(osd.handle(event(21, false, "cec", 7)), true);
  assert.equal(host.dataset.volume, "22");
  assert.equal(meterLevel(host), "22%");
  assert.equal([...timers.tasks.keys()][0], timerId);
  assert.equal(osd.debugState().eventCount, 1);
  assert.equal(osd.debugState().lastCecInstance, "backend-a");
  assert.equal(osd.debugState().lastCecGeneration, 8);
});

test("a restarted backend can begin a fresh generation epoch immediately", () => {
  const window = steamWindow();
  const timers = new FakeTimers();
  const osd = new SteamVolumeOsd(() => window, timers);

  assert.equal(osd.handle(event(22, false, "cec", 8, "backend-a")), true);
  assert.equal(osd.handle(event(24, false, "cec", 1, "backend-b")), true);
  const host = window.document.getElementById("cec-volume-osd-steam-root");
  assert.equal(host.dataset.volume, "24");
  assert.equal(meterLevel(host), "24%");
  assert.equal(osd.debugState().eventCount, 2);
  assert.equal(osd.debugState().lastCecInstance, "backend-b");
  assert.equal(osd.debugState().lastCecGeneration, 1);

  // An event queued by the retired backend must not reclaim the epoch.
  assert.equal(osd.handle(event(23, false, "cec", 9, "backend-a")), true);
  assert.equal(host.dataset.volume, "24");
  assert.equal(meterLevel(host), "24%");
  assert.equal(osd.debugState().eventCount, 2);
  assert.equal(osd.debugState().lastCecInstance, "backend-b");
});

test("bound backend identity rejects queued events from every other lifecycle", () => {
  const window = steamWindow();
  const timers = new FakeTimers();
  const osd = new SteamVolumeOsd(() => window, timers);
  osd.requireCecInstanceBinding();

  assert.equal(osd.handle(event(20, false, "cec", 8, "backend-old")), true);
  assert.equal(osd.debugState().eventCount, 0);
  assert.equal(osd.debugState().mounted, false);

  assert.equal(osd.bindCecInstance("backend-current"), true);
  assert.equal(osd.debugState().expectedCecInstance, "backend-current");
  assert.equal(
    osd.handle(event(22, false, "cec", 1, "backend-current")),
    true,
  );
  assert.equal(osd.debugState().volume, 22);
  assert.equal(osd.debugState().lastCecGeneration, 1);

  assert.equal(osd.handle(event(99, false, "cec", 100, "backend-old")), true);
  assert.equal(osd.debugState().volume, 22);
  assert.equal(osd.debugState().eventCount, 1);

  assert.equal(osd.bindCecInstance("backend-next"), true);
  assert.equal(osd.debugState().lastCecInstance, null);
  assert.equal(osd.debugState().lastCecGeneration, null);
  assert.equal(osd.handle(event(24, false, "cec", 1, "backend-next")), true);
  assert.equal(osd.debugState().volume, 24);
  assert.equal(osd.debugState().eventCount, 2);
});

test("preview and diagnostic events never move the real CEC cursor", () => {
  const window = steamWindow();
  const timers = new FakeTimers();
  const osd = new SteamVolumeOsd(() => window, timers);

  assert.equal(osd.handle(event(20, false, "cec", 4, "backend-a")), true);
  assert.equal(
    osd.handle({
      route: "hdmi-output-0",
      volume: 70,
      muted: false,
      generation: 999,
      source: "preview",
    }),
    true,
  );
  assert.equal(
    osd.handle({
      route: "diagnostic",
      volume: 73,
      muted: false,
      source: "diagnostic",
      diagnostic: true,
    }),
    true,
  );
  assert.equal(osd.debugState().lastCecInstance, "backend-a");
  assert.equal(osd.debugState().lastCecGeneration, 4);

  assert.equal(osd.handle(event(21, false, "cec", 5, "backend-a")), true);
  assert.equal(osd.debugState().volume, 21);
  assert.equal(osd.debugState().lastCecGeneration, 5);
});

test("invalid telemetry and unavailable state fail closed", () => {
  const window = steamWindow();
  const timers = new FakeTimers();
  const osd = new SteamVolumeOsd(() => window, timers);

  assert.equal(osd.handle(event(30)), true);
  assert.equal(osd.handle(event(130)), false);
  assert.equal(
    window.document.getElementById("cec-volume-osd-steam-root"),
    null,
  );
  assert.equal(osd.debugState().lastError, "rejected invalid volume event");

  assert.equal(osd.handle(event(31)), true);
  osd.hide();
  assert.equal(
    window.document.getElementById("cec-volume-osd-steam-root"),
    null,
  );
  assert.equal(timers.tasks.size, 0);
});

test("remounts into a recreated Steam GamepadUI document", () => {
  const firstWindow = steamWindow("first SP");
  const secondWindow = steamWindow("second SP");
  const timers = new FakeTimers();
  let currentWindow = firstWindow;
  const osd = new SteamVolumeOsd(() => currentWindow, timers);

  osd.handle(event(20));
  assert.ok(firstWindow.document.getElementById("cec-volume-osd-steam-root"));

  currentWindow = secondWindow;
  osd.handle(event(22));
  assert.equal(
    firstWindow.document.getElementById("cec-volume-osd-steam-root"),
    null,
  );
  assert.ok(secondWindow.document.getElementById("cec-volume-osd-steam-root"));
  assert.equal(osd.debugState().ownerTitle, "second SP");
  assert.equal(timers.count(HOLD_MS), 1);
});

test("holds one composition request through updates and the final fade frame", () => {
  const animationFrames = new FakeAnimationFrames();
  const window = steamWindow("Steam Big Picture Mode", animationFrames);
  const timers = new FakeTimers();
  const activity = [];
  const osd = new SteamVolumeOsd(
    () => window,
    timers,
    (active) => activity.push(active),
  );

  assert.equal(osd.handle(event(20)), true);
  assert.deepEqual(activity, [true]);
  animationFrames.runFrame();
  animationFrames.runFrame();

  assert.equal(osd.handle(event(21)), true);
  assert.deepEqual(activity, [true]);

  timers.run(HOLD_MS);
  const host = window.document.getElementById("cec-volume-osd-steam-root");
  assert.ok(host);
  assert.equal(host.dataset.phase, "fading");
  assert.deepEqual(activity, [true]);

  timers.run(FADE_OUT_MS + 50);
  assert.equal(
    window.document.getElementById("cec-volume-osd-steam-root"),
    null,
  );
  assert.deepEqual(activity, [true, false]);
});

test("a QAM-blocked lease retries on the next event without tearing down the host", () => {
  const window = steamWindow();
  const timers = new FakeTimers();
  const activity = [];
  let allowComposition = false;
  const osd = new SteamVolumeOsd(
    () => window,
    timers,
    (active) => {
      activity.push(active);
      return !active || allowComposition;
    },
  );

  assert.equal(osd.handle(event(20)), true);
  const firstHost = window.document.getElementById(
    "cec-volume-osd-steam-root",
  );
  assert.ok(firstHost);
  assert.deepEqual(activity, [true]);

  allowComposition = true;
  assert.equal(osd.handle(event(21)), true);
  assert.equal(
    window.document.getElementById("cec-volume-osd-steam-root"),
    firstHost,
  );
  assert.deepEqual(activity, [true, true]);

  osd.hide();
  assert.deepEqual(activity, [true, true, false]);
});

test("a throw-after-partial activation is explicitly unwound", () => {
  const window = steamWindow();
  const timers = new FakeTimers();
  const activity = [];
  const osd = new SteamVolumeOsd(
    () => window,
    timers,
    (active) => {
      activity.push(active);
      if (active) {
        throw new Error("partial activation");
      }
      return true;
    },
  );

  assert.equal(osd.handle(event(20)), false);
  assert.deepEqual(activity, [true, false]);
  assert.equal(
    window.document.getElementById("cec-volume-osd-steam-root"),
    null,
  );
  assert.match(osd.debugState().lastError, /partial activation/);
});

test("a failed release keeps local ownership so a later hide retries", () => {
  const window = steamWindow();
  const timers = new FakeTimers();
  const activity = [];
  let firstRelease = true;
  const osd = new SteamVolumeOsd(
    () => window,
    timers,
    (active) => {
      activity.push(active);
      if (!active && firstRelease) {
        firstRelease = false;
        throw new Error("transient release failure");
      }
      return true;
    },
  );

  assert.equal(osd.handle(event(20)), true);
  osd.hide();
  assert.deepEqual(activity, [true, false]);
  osd.hide();
  assert.deepEqual(activity, [true, false, false]);
});

test("fade revival retains the existing composition request", () => {
  const window = steamWindow();
  const timers = new FakeTimers();
  const activity = [];
  const osd = new SteamVolumeOsd(
    () => window,
    timers,
    (active) => activity.push(active),
  );

  osd.handle(event(20));
  timers.run(HOLD_MS);
  assert.equal(osd.keepAlive(), true);
  assert.deepEqual(activity, [true]);

  timers.run(HOLD_MS);
  timers.run(FADE_OUT_MS + 50);
  assert.deepEqual(activity, [true, false]);
});

test("explicit hide removes the host before releasing composition", () => {
  const window = steamWindow();
  const timers = new FakeTimers();
  const releases = [];
  const osd = new SteamVolumeOsd(
    () => window,
    timers,
    (active) => {
      if (!active) {
        releases.push(
          window.document.getElementById("cec-volume-osd-steam-root") !== null,
        );
      }
    },
  );

  osd.handle(event(20));
  osd.hide();

  assert.deepEqual(releases, [false]);
  assert.equal(timers.tasks.size, 0);
});

test("document recreation releases and reacquires composition", () => {
  const firstWindow = steamWindow("first SP");
  const secondWindow = steamWindow("second SP");
  const timers = new FakeTimers();
  const activity = [];
  let currentWindow = firstWindow;
  const osd = new SteamVolumeOsd(
    () => currentWindow,
    timers,
    (active) => activity.push(active),
  );

  osd.handle(event(20));
  currentWindow = secondWindow;
  osd.handle(event(21));

  assert.deepEqual(activity, [true, false, true]);
  assert.equal(
    firstWindow.document.getElementById("cec-volume-osd-steam-root"),
    null,
  );
  assert.ok(
    secondWindow.document.getElementById("cec-volume-osd-steam-root"),
  );
});

test("owner document teardown immediately removes the host and releases composition", () => {
  const window = steamWindow("retiring SP");
  const timers = new FakeTimers();
  const activity = [];
  const osd = new SteamVolumeOsd(
    () => window,
    timers,
    (active) => activity.push(active),
  );

  osd.handle(event(20));
  assert.deepEqual(activity, [true]);
  window.dispatchEvent(new window.Event("pagehide"));

  assert.equal(
    window.document.getElementById("cec-volume-osd-steam-root"),
    null,
  );
  assert.deepEqual(activity, [true, false]);
  assert.equal(timers.tasks.size, 0);
});

test("hidden diagnostics never request game composition", () => {
  const window = steamWindow();
  const timers = new FakeTimers();
  const activity = [];
  const osd = new SteamVolumeOsd(
    () => window,
    timers,
    (active) => activity.push(active),
  );

  assert.equal(osd.handle(event(73, false, "diagnostic")), true);
  assert.deepEqual(activity, []);
  timers.run(HOLD_MS);
  timers.run(FADE_OUT_MS + 50);
  assert.deepEqual(activity, []);
});
