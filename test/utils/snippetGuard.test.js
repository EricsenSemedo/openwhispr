const test = require("node:test");
const assert = require("node:assert/strict");

const {
  expandSnippets,
  findSnippetTriggerRanges,
  normalizeWhole,
  validateSnippet,
} = require("../../src/utils/snippets.ts");
const { mutateSnippets } = require("../../src/helpers/snippetMutation.js");

const signature = [{ trigger: "my signature", replacement: "um TEST CONTENT" }];

test("saved text is inserted exactly and never expanded again", () => {
  assert.equal(expandSnippets("um my signature", signature), "um um TEST CONTENT");
  const loop = [{ trigger: "my signature", replacement: "my signature" }];
  assert.equal(expandSnippets("my signature", loop), "my signature");
});

test("a trigger-only utterance becomes exactly the saved text", () => {
  const link = [{ trigger: "my portfolio", replacement: "https://example.com\n" }];
  assert.equal(expandSnippets("My portfolio.", link), "https://example.com\n");
});

test("quotes, code and lines about words are left alone", () => {
  for (const text of [
    'Keep "my signature" exactly.',
    "Keep 'my signature'.",
    "Keep `my signature`.",
    "```\nmy signature\n```",
    "The trigger my signature expands.",
    "const x = my signature",
  ]) {
    assert.equal(expandSnippets(text, signature), text);
    assert.deepEqual(findSnippetTriggerRanges(text, signature), []);
  }
});

test("URLs, emails and paths are left alone", () => {
  const home = [{ trigger: "home", replacement: "HOME" }];
  for (const text of ["see https://x.com/home now", "mail home@x.com", "open ./home/file"]) {
    assert.equal(expandSnippets(text, home), text);
  }
  assert.equal(expandSnippets("go home now", home), "go HOME now");
});

test("n't contractions are not split, possessives still match", () => {
  const can = [{ trigger: "can", replacement: "CAN" }];
  assert.equal(expandSnippets("I can't go", can), "I can't go");
  assert.equal(expandSnippets("my signature's end", signature), "um TEST CONTENT's end");
});

test("validateSnippet rejects empty, long and conflicting triggers", () => {
  assert.match(validateSnippet("!!", "x", []), /letters or numbers/);
  assert.match(validateSnippet("a".repeat(101), "x", []), /100 characters/);
  assert.match(validateSnippet("hi", "  ", []), /text to insert/);
  assert.match(validateSnippet("My-Signature", "x", signature), /conflicts with “my signature”/);
  assert.equal(validateSnippet("My Signature", "x", signature, "my signature"), "");
  assert.equal(normalizeWhole("My, Portfolio!"), "myportfolio");
});

test("mutateSnippets refuses a stale editor snapshot", () => {
  let stored = [{ trigger: "a", replacement: "1" }];
  const db = { getSnippets: () => stored.map((s) => ({ ...s })), setSnippets: (next) => (stored = next) };
  assert.throws(
    () => mutateSnippets(db, { action: "delete", expected: [], previousTrigger: "a" }),
    /library changed/
  );
  const saved = mutateSnippets(db, {
    action: "save",
    expected: [{ trigger: "a", replacement: "1" }],
    previousTrigger: null,
    trigger: " b ",
    replacement: "two\n",
  });
  assert.deepEqual(saved, [
    { trigger: "a", replacement: "1" },
    { trigger: "b", replacement: "two\n" },
  ]);
  assert.throws(
    () =>
      mutateSnippets(db, {
        action: "save",
        expected: saved,
        previousTrigger: null,
        trigger: "B!",
        replacement: "x",
      }),
    /distinctive trigger/
  );
});
