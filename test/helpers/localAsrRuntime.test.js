const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const path = require("node:path");

const { LocalAsrRuntime } = require("../../src/helpers/localAsrRuntime.js");

const logger = { info() {}, warn() {}, error() {} };
const abs = (p) => path.resolve(p);

function fakeChild() {
  const child = new EventEmitter();
  child.pid = 4242;
  child.kill = () => setImmediate(() => child.emit("exit", 0, null));
  return child;
}

test("does nothing when the runtime is not enabled or not configured", () => {
  let spawned = 0;
  const spawnProcess = () => {
    spawned++;
    return fakeChild();
  };
  new LocalAsrRuntime({ profile: abs("p"), logger, spawnProcess, readConfig: () => ({ enabled: false }), openLog: () => null }).start();
  const missing = Object.assign(new Error("missing"), { code: "ENOENT" });
  new LocalAsrRuntime({ profile: abs("p"), logger, spawnProcess, readConfig: () => { throw missing; }, openLog: () => null }).start();
  new LocalAsrRuntime({ profile: abs("p"), logger, spawnProcess, readConfig: () => ({ enabled: true, pythonw: "rel.exe", supervisor: "s.py" }), openLog: () => null }).start();
  assert.equal(spawned, 0);
});

test("starts the supervisor with the app's pid and stops it on quit", async () => {
  const calls = [];
  const child = fakeChild();
  const runtime = new LocalAsrRuntime({
    profile: abs("profile"),
    executable: abs("OpenWhispr.exe"),
    logger,
    spawnProcess: (cmd, args, opts) => {
      calls.push({ cmd, args, opts });
      return child;
    },
    readConfig: () => ({ enabled: true, pythonw: abs("pythonw.exe"), supervisor: abs("sup/supervisor.py") }),
    openLog: () => null,
  });
  runtime.start();
  runtime.start();
  assert.equal(calls.length, 1, "a running worker is not started twice");
  const { cmd, args, opts } = calls[0];
  assert.equal(cmd, abs("pythonw.exe"));
  assert.deepEqual(args.slice(0, 3), ["-X", "utf8", abs("sup/supervisor.py")]);
  assert.equal(args[args.indexOf("--parent-pid") + 1], String(process.pid));
  assert.equal(opts.windowsHide, true);
  assert.equal(opts.shell, false);
  await runtime.stop();
  assert.equal(runtime.child, null);
});

test("an exited worker is retried until stop", async () => {
  let spawned = 0;
  const children = [];
  const runtime = new LocalAsrRuntime({
    profile: abs("p"),
    logger,
    retryMs: 5,
    spawnProcess: () => {
      spawned++;
      const c = fakeChild();
      children.push(c);
      return c;
    },
    readConfig: () => ({ enabled: true, pythonw: abs("pythonw.exe"), supervisor: abs("s.py") }),
    openLog: () => null,
  });
  runtime.start();
  children[0].emit("exit", 1, null);
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(spawned, 2);
  await runtime.stop();
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(spawned, 2);
});
