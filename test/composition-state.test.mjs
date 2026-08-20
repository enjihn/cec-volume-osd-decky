import test from "node:test";
import assert from "node:assert/strict";

import {
  COMPOSITION_OWNER,
  CompositionActivityStore,
  findCompositionHooks,
  isCompositionHook,
} from "../.test-build/composition-state.js";

function matchingHook() {
  return Function(
    "return function useComposition() { " +
      "return 'AddMinimumCompositionStateRequest " +
      "ChangeMinimumCompositionStateRequest " +
      "RemoveMinimumCompositionStateRequest'; }",
  )();
}

test("recognizes only the Steam composition hook shape", () => {
  const hook = matchingHook();
  const storeMethod = Function(
    "return function compositionStore() { " +
      "return 'AddMinimumCompositionStateRequest " +
      "ChangeMinimumCompositionStateRequest " +
      "RemoveMinimumCompositionStateRequest " +
      "m_mapCompositionStateRequests'; }",
  )();

  assert.equal(isCompositionHook(hook), true);
  assert.equal(isCompositionHook(storeMethod), false);
  assert.equal(isCompositionHook(() => "unrelated"), false);
  assert.equal(isCompositionHook(null), false);
});

test("deduplicates the same hook exported through module aliases", () => {
  const hook = matchingHook();
  const hooks = findCompositionHooks([
    { hook, default: { alias: hook } },
    { duplicate: hook },
  ]);

  assert.deepEqual(hooks, [hook]);
});

test("reports ambiguous hook matches instead of selecting one", () => {
  const first = matchingHook();
  const second = matchingHook();
  const hooks = findCompositionHooks([{ first, second }]);

  assert.equal(hooks.length, 2);
  assert.notEqual(hooks[0], hooks[1]);
});

test("ignores module getters that throw while scanning", () => {
  const hook = matchingHook();
  const hostile = { hook };
  Object.defineProperty(hostile, "broken", {
    enumerable: true,
    get() {
      throw new Error("unavailable module side effect");
    },
  });

  assert.deepEqual(findCompositionHooks([hostile]), [hook]);
});

test("activity changes are idempotent and fully observable", () => {
  const store = new CompositionActivityStore(1);
  let notifications = 0;
  const unsubscribe = store.subscribe(() => {
    notifications += 1;
  });

  assert.equal(store.setRequested(true), true);
  assert.equal(store.setRequested(true), false);
  store.setBridgeMounted(true);

  let state = store.debugState();
  assert.equal(notifications, 1);
  assert.equal(state.compositionAvailable, true);
  assert.equal(state.compositionHookAvailable, true);
  assert.equal(state.compositionCandidateCount, 1);
  assert.equal(state.compositionBridgeMounted, true);
  assert.equal(state.compositionRequested, true);
  assert.equal(state.compositionReleased, false);
  assert.equal(state.compositionMode, "Notification");
  assert.equal(state.compositionOwner, COMPOSITION_OWNER);
  assert.equal(state.requestedComposition, "Notification");
  assert.equal(state.compositionOwnerToken, COMPOSITION_OWNER);
  assert.equal(state.compositionReleaseState, "held");
  assert.equal(state.compositionTransitionCount, 1);
  assert.equal(state.compositionLastTransition, "requested");
  assert.equal(typeof state.compositionLastTransitionAt, "number");

  assert.equal(store.setRequested(false), true);
  assert.equal(store.setRequested(false), false);
  unsubscribe();
  store.setRequested(true);

  state = store.debugState();
  assert.equal(notifications, 2);
  assert.equal(state.compositionRequested, true);
  assert.equal(state.compositionTransitionCount, 3);
});

test("zero or multiple candidates fail closed", () => {
  for (const candidateCount of [0, 2]) {
    const state = new CompositionActivityStore(candidateCount).debugState();
    assert.equal(state.compositionAvailable, false);
    assert.equal(state.compositionHookAvailable, false);
    assert.equal(state.compositionReleaseState, "unsupported");
  }
});
