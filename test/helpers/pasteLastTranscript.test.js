const test = require("node:test");
const assert = require("node:assert/strict");

const {
  findLastTranscriptText,
  pasteLastTranscript,
} = require("../../src/helpers/pasteLastTranscript");

const silentLogger = { info: () => {} };

function setup(rows) {
  const pastes = [];
  return {
    pastes,
    deps: {
      logger: silentLogger,
      databaseManager: { getTranscriptions: () => rows },
      clipboardManager: {
        pasteText: async (text, options) => {
          pastes.push({ text, options });
        },
      },
    },
  };
}

test("skips failed, pending and blank rows to find the newest finished dictation", () => {
  const text = findLastTranscriptText([
    { status: "failed", text: "" },
    { status: "pending", text: "not finished" },
    { status: "completed", text: "   " },
    { status: "completed", text: "the one you said" },
    { status: "completed", text: "older" },
  ]);
  assert.equal(text, "the one you said");
});

test("a stray failed tap does not hide the dictation before it", async () => {
  const { deps, pastes } = setup([
    { status: "failed", text: "" },
    { status: "completed", text: "Hello there." },
  ]);
  assert.deepEqual(await pasteLastTranscript(deps), { pasted: true });
  assert.equal(pastes.length, 1);
  assert.equal(pastes[0].text, "Hello there.");
});

test("pastes where the focus is now, not at the window captured at record start", async () => {
  const { deps, pastes } = setup([{ status: "completed", text: "Hello there." }]);
  await pasteLastTranscript(deps);
  assert.equal(pastes[0].options.targetWindow, undefined);
  assert.notEqual(pastes[0].options.restoreClipboard, false);
});

test("does nothing when there is no finished dictation", async () => {
  const { deps, pastes } = setup([{ status: "failed", text: "" }]);
  assert.deepEqual(await pasteLastTranscript(deps), { pasted: false, reason: "no-transcript" });
  assert.equal(pastes.length, 0);
});

test("does nothing before the managers exist", async () => {
  assert.deepEqual(await pasteLastTranscript({ logger: silentLogger }), {
    pasted: false,
    reason: "not-ready",
  });
});
