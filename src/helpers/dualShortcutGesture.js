"use strict";

// Optional Windows hold/double-tap behavior; no keyboard hook or mic ownership.
class DualShortcutGesture {
  constructor(actions, { now = Date.now, schedule = setTimeout, clear = clearTimeout,
    holdMs = 150, doubleTapMs = 350, maxMs = 300000 } = {}) {
    this.actions = actions;
    this.now = now; this.schedule = schedule; this.clear = clear;
    this.holdMs = holdMs; this.doubleTapMs = doubleTapMs; this.maxMs = maxMs;
    this.state = "idle"; this.key = null; this.physicalDown = false;
    this.generation = 0; this.timers = new Set(); this.confirmed = false;
  }
  later(ms, fn) {
    const generation = this.generation;
    const id = this.schedule(() => {
      this.timers.delete(id);
      if (generation === this.generation) fn();
    }, ms);
    this.timers.add(id);
  }
  reset() {
    for (const id of this.timers) this.clear(id);
    this.timers.clear(); this.generation++;
    this.state = "idle"; this.key = null; this.physicalDown = false;
    this.confirmed = false;
  }
  start() {
    this.actions.start();
    this.later(this.maxMs, () => this.cancel());
  }
  press(key) {
    if (this.physicalDown || (this.key && key !== this.key)) return;
    this.physicalDown = true;
    if (this.state === "latched") {
      // Paste must wait until the shortcut modifiers have been released.
      this.state = "stopping";
      return;
    }
    if (this.state === "awaiting" && this.now() <= this.deadline) {
      this.reset(); this.key = key; this.physicalDown = true;
      this.state = "latched"; this.actions.prepare(); this.start();
      return;
    }
    this.reset(); this.key = key; this.physicalDown = true;
    this.state = "armed"; this.actions.prepare();
    this.later(this.holdMs, () => {
      if (this.state !== "armed" || !this.physicalDown) return;
      this.state = "held"; this.start();
    });
  }
  release(key) {
    if (!this.physicalDown || key !== this.key) return;
    this.physicalDown = false;
    if (this.state === "armed") {
      const savedKey = this.key;
      this.reset(); this.key = savedKey; this.state = "awaiting";
      this.deadline = this.now() + this.doubleTapMs;
      this.actions.cancelPreparation(); this.actions.hide();
      this.later(this.doubleTapMs + 1, () => this.reset());
    } else if (this.state === "held" || this.state === "stopping") {
      this.reset(); this.actions.stop();
    } else if (this.state === "blocked") {
      this.reset();
    }
  }
  cancel({ notify = true, preservePhysicalDown = true } = {}) {
    const active = this.state !== "idle" && this.state !== "blocked";
    const key = this.key, down = this.physicalDown;
    this.reset();
    // Escape while held cannot turn keyboard auto-repeat into a fresh capture.
    if (down && preservePhysicalDown) { this.key = key; this.physicalDown = true; this.state = "blocked"; }
    if (active && notify) { this.actions.cancel(); this.actions.hide(); }
  }
  lifecycle(state) {
    if (state === "recording") this.confirmed = true;
    // Discard/cancel from the HUD must also clear any latch and pending timers.
    if ((this.confirmed && state === "idle") || state === "error") {
      this.cancel({ notify: false });
    }
  }
}
module.exports = { DualShortcutGesture };
