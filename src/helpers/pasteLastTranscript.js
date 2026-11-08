// The newest rows can be failed or empty (a stray tap saves a failed row), so
// look back far enough to get past a short run of them.
const LOOKBACK_ROWS = 25;

function findLastTranscriptText(rows) {
  for (const row of rows ?? []) {
    if (row?.status === "completed" && typeof row.text === "string" && row.text.trim()) {
      return row.text;
    }
  }
  return null;
}

// Pastes the newest finished dictation into whatever has focus now. No target
// window is passed on purpose: the first paste went to the window captured at
// record start, and this shortcut is for when that was the wrong place. The
// text is pasted as saved, and the clipboard manager restores the clipboard.
async function pasteLastTranscript({ databaseManager, clipboardManager, logger }) {
  if (!databaseManager || !clipboardManager) return { pasted: false, reason: "not-ready" };

  const text = findLastTranscriptText(databaseManager.getTranscriptions(LOOKBACK_ROWS));
  if (!text) {
    logger.info("Paste last transcript: nothing to paste", {}, "hotkey");
    return { pasted: false, reason: "no-transcript" };
  }

  await clipboardManager.pasteText(text, {});
  logger.info("Paste last transcript: pasted", { length: text.length }, "hotkey");
  return { pasted: true };
}

module.exports = { LOOKBACK_ROWS, findLastTranscriptText, pasteLastTranscript };
