const test = require("node:test");
const assert = require("node:assert/strict");
const Module = require("node:module");

const clipboardPath = require.resolve("../../src/helpers/clipboard");
const originalLoad = Module._load;

function setup({ enabled = true, wrote = true, platform = "win32" } = {}, t) {
  const fake = {
    text: "dictated text",
    writes: [],
    availableFormats: () => ["text/plain"],
    readText: () => fake.text,
    readBuffer: () => Buffer.from(""),
    writeText(text) {
      fake.writes.push(["writeText", text]);
      fake.text = text;
    },
    write(payload) {
      fake.writes.push(["write", payload]);
      fake.text = payload.text ?? "";
    },
    writeImage(image) {
      fake.writes.push(["writeImage", image]);
      fake.text = "";
    },
  };
  const privateWrites = [];
  const flow = {
    isEnabled: () => enabled,
    writePrivateClipboard: async (text, rich) => {
      privateWrites.push([text, rich]);
      if (wrote) fake.text = text;
      return wrote;
    },
  };
  delete require.cache[clipboardPath];
  Module._load = function loadWithMocks(request, parent, isMain) {
    if (request === "electron") return { clipboard: fake };
    if (request === "./flowAdapter") return flow;
    return originalLoad.call(this, request, parent, isMain);
  };
  let Manager;
  try {
    Manager = require("../../src/helpers/clipboard");
  } finally {
    Module._load = originalLoad;
  }
  const descriptor = Object.getOwnPropertyDescriptor(process, "platform");
  Object.defineProperty(process, "platform", { value: platform });
  t.after(() => Object.defineProperty(process, "platform", descriptor));
  return { manager: new Manager(), fake, privateWrites };
}

const restore = (manager, original) =>
  manager._restoreClipboardAfterDelay(original, { delayMs: 0, expectedText: "dictated text" });

test("old text goes back privately, with no normal clipboard write", async (t) => {
  const { manager, fake, privateWrites } = setup({}, t);
  await restore(manager, { type: "text", data: "previous clipboard" });
  assert.deepEqual(privateWrites, [["previous clipboard", {}]]);
  assert.deepEqual(fake.writes, []);
  assert.equal(fake.text, "previous clipboard");
});

test("a rich copy goes back privately with its html and rtf", async (t) => {
  const { manager, fake, privateWrites } = setup({}, t);
  await restore(manager, {
    type: "formats",
    data: { text: "plain", html: "<b>plain</b>", rtf: "{\\rtf1 plain}" },
  });
  assert.deepEqual(privateWrites, [["plain", { html: "<b>plain</b>", rtf: "{\\rtf1 plain}" }]]);
  assert.deepEqual(fake.writes, []);
});

test("a failed private write falls back to the normal restore", async (t) => {
  const { manager, fake } = setup({ wrote: false }, t);
  await restore(manager, { type: "text", data: "previous clipboard" });
  assert.deepEqual(fake.writes, [["writeText", "previous clipboard"]]);
});

test("images keep the normal restore", async (t) => {
  const image = { isEmpty: () => false };
  const { manager, fake, privateWrites } = setup({}, t);
  await restore(manager, { type: "image", data: image });
  assert.deepEqual(privateWrites, []);
  assert.deepEqual(fake.writes, [["writeImage", image]]);

  fake.text = "dictated text";
  await restore(manager, { type: "formats", data: { text: "plain", image } });
  assert.deepEqual(privateWrites, []);
  assert.equal(fake.writes.at(-1)[0], "write");
});

test("an empty old clipboard keeps the normal restore", async (t) => {
  const { manager, fake, privateWrites } = setup({}, t);
  await restore(manager, { type: "text", data: "" });
  assert.deepEqual(privateWrites, []);
  assert.deepEqual(fake.writes, [["writeText", ""]]);
});

test("a clipboard the user changed after the paste is left alone", async (t) => {
  const { manager, fake, privateWrites } = setup({}, t);
  fake.text = "copied by the user";
  await restore(manager, { type: "text", data: "previous clipboard" });
  assert.deepEqual(privateWrites, []);
  assert.deepEqual(fake.writes, []);
  assert.equal(fake.text, "copied by the user");
});

test("stock builds keep the normal restore", async (t) => {
  const stock = setup({ enabled: false }, t);
  await restore(stock.manager, { type: "text", data: "previous clipboard" });
  assert.deepEqual(stock.privateWrites, []);
  assert.deepEqual(stock.fake.writes, [["writeText", "previous clipboard"]]);
});

test("other platforms keep the normal restore", async (t) => {
  const linux = setup({ platform: "linux" }, t);
  linux.manager._restoreClipboard = (original) => linux.fake.writes.push(["normal", original.data]);
  await restore(linux.manager, { type: "text", data: "previous clipboard" });
  assert.deepEqual(linux.privateWrites, []);
  assert.deepEqual(linux.fake.writes, [["normal", "previous clipboard"]]);
});
