// Local flow adapter (OpenWhisprLocal, 2026-10-12). The loopback adapter that
// already serves transcription and cleanup also offers Wispr Flow-style
// helpers: casing/spacing that fits the text around the cursor, a clipboard
// write that stays out of Win+V history, and reviewed auto-learn. Every call
// is best-effort with a short deadline; null means "use stock behaviour".
const http = require("http");

const FLOW_ADAPTER_URL = process.env.OPENWHISPR_FLOW_URL || "http://127.0.0.1:18765";

function flowRequest(method, pathname, body, timeoutMs) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (!settled) {
        settled = true;
        resolve(value);
      }
    };
    const deadline = setTimeout(() => finish(null), timeoutMs + 50);
    try {
      const data = body ? Buffer.from(JSON.stringify(body)) : null;
      const req = http.request(
        new URL(pathname, FLOW_ADAPTER_URL),
        {
          method,
          timeout: timeoutMs,
          headers: data ? { "Content-Type": "application/json", "Content-Length": data.length } : {},
        },
        (res) => {
          const chunks = [];
          res.on("data", (chunk) => chunks.push(chunk));
          res.on("error", () => finish(null));
          res.on("end", () => {
            clearTimeout(deadline);
            if (res.statusCode !== 200) return finish(null);
            try {
              finish(JSON.parse(Buffer.concat(chunks).toString("utf8")));
            } catch {
              finish(null);
            }
          });
        }
      );
      req.on("timeout", () => {
        req.destroy();
        finish(null);
      });
      req.on("error", () => finish(null));
      if (data) req.write(data);
      req.end();
    } catch {
      finish(null);
    }
  });
}

// Fit the text to the field: capital at a sentence start, lower-case first
// word mid-sentence, a leading space after a word. Unknown context = no change.
async function adjustForCursor(text, targetWindow) {
  if (typeof text !== "string" || !text) return text;
  const result = await flowRequest(
    "POST",
    "/v1/flow/adjust",
    { text, hwnd: targetWindow ?? null },
    350
  );
  return typeof result?.text === "string" && result.text.trim() ? result.text : text;
}

// Text on the clipboard with ExcludeClipboardContentFromMonitorProcessing,
// CanIncludeInClipboardHistory=0 and CanUploadToCloudClipboard=0.
async function writePrivateClipboard(text) {
  const result = await flowRequest("POST", "/v1/flow/clipboard", { text }, 400);
  return result?.ok === true;
}

// The edited sentence, for the reviewer: about 200 characters either side of
// the first corrected word.
function editContext(fieldValue, pairs) {
  const at = fieldValue.indexOf(pairs[0]?.to ?? "");
  if (at < 0) return fieldValue.slice(-400);
  return fieldValue.slice(Math.max(0, at - 200), at + 200);
}

// Ask the adapter which "misheard -> correct" pairs are real vocabulary.
// Returns the accepted {from, to} pairs, or [] when the adapter is unavailable.
// The adapter only reviews; call rememberPairs once the words are saved.
async function reviewCorrections(pairs, fieldValue) {
  if (!Array.isArray(pairs) || pairs.length === 0) return [];
  const sentence = editContext(fieldValue || "", pairs);
  const result = await flowRequest("POST", "/v1/flow/learn", { pairs, sentence }, 8000);
  return Array.isArray(result?.accepted)
    ? result.accepted.filter((a) => typeof a?.from === "string" && typeof a?.to === "string" && a.to)
    : [];
}

// Store accepted pairs so future dictations fix the same mishearing.
function rememberPairs(pairs) {
  if (!Array.isArray(pairs) || pairs.length === 0) return Promise.resolve(null);
  return flowRequest(
    "POST",
    "/v1/flow/remember",
    { pairs: pairs.map(({ from, to }) => ({ from, to })) },
    2000
  );
}

// Undo from the "learned" toast: drop the auto-learned pairs for these words.
function forgetLearned(words) {
  if (!Array.isArray(words) || words.length === 0) return Promise.resolve(null);
  return flowRequest("POST", "/v1/flow/forget", { words }, 2000);
}

module.exports = {
  forgetLearned,
  adjustForCursor,
  writePrivateClipboard,
  reviewCorrections,
  rememberPairs,
  flowRequest,
};
