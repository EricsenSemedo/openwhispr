const test = require("node:test");
const assert = require("node:assert/strict");

const { DualShortcutGesture } = require("../../src/helpers/dualShortcutGesture.js");

function harness() {
  let now = 0;
  const timers = [];
  const calls = [];
  const actions = {};
  for (const name of ["prepare", "start", "stop", "cancel", "cancelPreparation", "hide"]) {
    actions[name] = () => calls.push(name);
  }
  const gesture = new DualShortcutGesture(actions, {
    now: () => now,
    schedule: (fn, ms) => {
      const t = { at: now + ms, fn, done: false };
      timers.push(t);
      return t;
    },
    clear: (t) => {
      t.done = true;
    },
  });
  const advance = (ms) => {
    now += ms;
    for (const t of timers) {
      if (!t.done && t.at <= now) {
        t.done = true;
        t.fn();
      }
    }
  };
  return { gesture, calls, advance };
}

test("hold records and release stops", () => {
  const { gesture, calls, advance } = harness();
  gesture.press("D");
  advance(160);
  gesture.release("D");
  assert.deepEqual(calls, ["prepare", "start", "stop"]);
});

test("a single brief tap never records", () => {
  const { gesture, calls, advance } = harness();
  gesture.press("D");
  advance(50);
  gesture.release("D");
  advance(400);
  assert.deepEqual(calls, ["prepare", "cancelPreparation", "hide"]);
});

test("double tap latches, third press finishes on release", () => {
  const { gesture, calls, advance } = harness();
  gesture.press("D");
  advance(40);
  gesture.release("D");
  advance(100);
  gesture.press("D");
  advance(40);
  gesture.release("D");
  assert.deepEqual(calls.slice(-2), ["prepare", "start"]);
  advance(2000);
  gesture.press("D");
  assert.equal(calls.at(-1), "start", "stop waits for the release");
  gesture.release("D");
  assert.equal(calls.at(-1), "stop");
});

test("Escape while held blocks key repeat from starting a new capture", () => {
  const { gesture, calls, advance } = harness();
  gesture.press("D");
  advance(200);
  gesture.cancel();
  assert.deepEqual(calls.slice(-2), ["cancel", "hide"]);
  gesture.press("D");
  advance(200);
  assert.equal(calls.filter((c) => c === "start").length, 1);
  gesture.release("D");
  assert.equal(gesture.state, "idle");
});
