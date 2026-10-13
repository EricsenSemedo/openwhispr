const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");

function load(url) {
  process.env.OPENWHISPR_FLOW_URL = url;
  delete require.cache[require.resolve("../../src/helpers/flowAdapter.js")];
  return require("../../src/helpers/flowAdapter.js");
}

async function withServer(handler, fn) {
  const server = http.createServer(handler);
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  try {
    return await fn(`http://127.0.0.1:${server.address().port}`);
  } finally {
    server.closeAllConnections?.();
    await new Promise((r) => server.close(r));
  }
}

function json(res, status, body) {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body));
}

test("adjustForCursor uses the adapter's text", async () => {
  await withServer(
    (req, res) => {
      let body = "";
      req.on("data", (c) => (body += c));
      req.on("end", () => {
        assert.equal(req.url, "/v1/flow/adjust");
        assert.deepEqual(JSON.parse(body), { text: "Hello there", hwnd: "0x1" });
        json(res, 200, { text: " hello there" });
      });
    },
    async (url) => {
      const flow = load(url);
      assert.equal(await flow.adjustForCursor("Hello there", "0x1"), " hello there");
    }
  );
});

test("stock behaviour when the adapter is down, slow or wrong", async () => {
  const down = load("http://127.0.0.1:9");
  assert.equal(await down.adjustForCursor("Hi", null), "Hi");
  assert.equal(await down.writePrivateClipboard("x"), false);
  assert.deepEqual(await down.reviewCorrections([{ from: "a", to: "b" }], "a"), []);

  await withServer(
    (req, res) => {
      if (req.url === "/v1/flow/adjust") setTimeout(() => json(res, 200, { text: "late" }), 1000);
      else json(res, 500, {});
    },
    async (url) => {
      const flow = load(url);
      const started = Date.now();
      assert.equal(await flow.adjustForCursor("Hi", null), "Hi");
      assert.ok(Date.now() - started < 900, "gives up after its short deadline");
      assert.equal(await flow.writePrivateClipboard("x"), false);
    }
  );

  await withServer(
    (req, res) => json(res, 200, { text: "   " }),
    async (url) => {
      assert.equal(await load(url).adjustForCursor("Hi", null), "Hi", "blank text is ignored");
    }
  );
});

test("reviewCorrections sends the edited sentence and keeps valid pairs", async () => {
  await withServer(
    (req, res) => {
      let body = "";
      req.on("data", (c) => (body += c));
      req.on("end", () => {
        const sent = JSON.parse(body);
        assert.match(sent.sentence, /Tailscale/);
        json(res, 200, { accepted: [{ from: "TealScale", to: "Tailscale" }, { from: "x" }, null] });
      });
    },
    async (url) => {
      const flow = load(url);
      const accepted = await flow.reviewCorrections(
        [{ from: "TealScale", to: "Tailscale" }],
        "I set up Tailscale on the PC."
      );
      assert.deepEqual(accepted, [{ from: "TealScale", to: "Tailscale" }]);
    }
  );
});
