export interface Snippet {
  trigger: string;
  replacement: string;
}

// Mirrors the cap database.js enforces when it stores snippets.
export const MAX_SNIPPET_TRIGGER_LENGTH = 100;

interface SnippetMatcher {
  regex: RegExp;
  replacements: Map<string, string>;
}

export interface SnippetTriggerRange {
  /** Index of the trigger's first character. */
  start: number;
  /** Index one past the trigger's last character. */
  end: number;
}

let cachedSnippets: Snippet[] | null = null;
let cachedMatcher: SnippetMatcher | null = null;

// The regex /i flag can't case-fold Turkish İ (U+0130) or dotless ı
// (U+0131), and İ's toLowerCase() form is two code units ("i" + U+0307), so
// triggers like "İmza" never matched. Folding İ to a plain "i" gives Map
// keys a canonical form, and matching İ/ı explicitly in the pattern lets the
// regex find them in the transcript.
function foldCapitalIDot(value: string): string {
  return value.replace(/İ/g, "i");
}

function buildMatcher(snippets: Snippet[]): SnippetMatcher | null {
  const replacements = new Map<string, string>();
  for (const { trigger, replacement } of snippets) {
    const folded = foldCapitalIDot(trigger.trim().normalize("NFC"));
    const key = folded.toLowerCase();
    if (!key) continue;
    replacements.set(key, replacement);
    // An uppercase I in the trigger may mean Turkish ı as well as English i,
    // so register both readings; an explicit trigger wins over a variant.
    const dotlessKey = folded.replace(/I/g, "ı").toLowerCase();
    if (!replacements.has(dotlessKey)) replacements.set(dotlessKey, replacement);
  }
  if (replacements.size === 0) return null;

  // Longest-first so "investor ask" wins over a shorter "ask" trigger.
  const escaped = [...replacements.keys()]
    .sort((a, b) => b.length - a.length)
    .map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .map((t) => t.replace(/i/g, "[iİ]").replace(/ı/g, "[ıI]"));
  // Unicode-aware word boundaries — triggers never match inside a word.
  const regex = new RegExp(
    `(?<=^|[\\s\\p{P}\\p{S}])(?:${escaped.join("|")})(?=$|[\\s\\p{P}\\p{S}])`,
    "giu"
  );
  return { regex, replacements };
}

// Memoized against the snippets array reference (the settings store replaces
// the array on every change).
function getMatcher(snippets?: Snippet[] | null): SnippetMatcher | null {
  if (!Array.isArray(snippets) || snippets.length === 0) return null;
  if (snippets !== cachedSnippets) {
    cachedSnippets = snippets;
    cachedMatcher = buildMatcher(snippets);
  }
  return cachedMatcher;
}

// The pattern case-folds the Unicode way (/iu) while the keys are toLowerCase'd,
// so a match is not proof of a replacement: "[ıI]mza" matches a plain "imza",
// which is no key at all. Both callers resolve through here, so a reported range
// always means an expansion — wake-word suppression depends on it.
function resolveReplacement(match: string, replacements: Map<string, string>): string | undefined {
  const folded = foldCapitalIDot(match);
  return (
    replacements.get(folded.toLowerCase()) ??
    // An uppercase I can be capital dotless ı as well as English i.
    replacements.get(folded.replace(/I/g, "ı").toLowerCase())
  );
}

/**
 * Character ranges of every trigger occurrence, matched against `text` exactly
 * as given so the offsets index that same string. Wake-word detection uses
 * these to ignore an agent name that only appears because it opens a trigger
 * the user chose, such as "openwhispr review" (see `agentDetection`).
 */
export function findSnippetTriggerRanges(
  text: string,
  snippets?: Snippet[] | null
): SnippetTriggerRange[] {
  if (!text) return [];
  return guardedMatches(text, snippets).map(({ start, end }) => ({ start, end }));
}

// Local guard: text that is already literal data is never expanded. That is
// quoted text, code, URLs, emails, paths, and lines that talk about words or
// code ("the trigger my signature expands").
function protectedRanges(text: string): Array<[number, number]> {
  const ranges: Array<[number, number]> = [];
  const quotes =
    /```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)|"[^"\n]*"|“[^”\n]*”|`[^`\n]*`|(?<!\w)'[^'\n]*'(?!\w)|‘[^’\n]*’/gu;
  for (const m of text.matchAll(quotes)) ranges.push([m.index, m.index + m[0].length]);
  const literals =
    /(?:https?:\/\/|www\.)[^\s<>"'`]+|[\p{L}\p{N}._%+\-]+@[\p{L}\p{N}.\-]+\.[\p{L}]{2,}|(?:[A-Za-z]:\\|\.{0,2}\/)[^\s<>"'`]+/gu;
  for (const m of text.matchAll(literals)) ranges.push([m.index, m.index + m[0].length]);
  let offset = 0;
  for (const line of text.split("\n")) {
    if (
      /\b(?:literal|literally|word|words|phrase|trigger|snippet|keyword|quote|quoted|spell|spelled|named|called|variable|string|code|print)\b/i.test(
        line
      ) ||
      /(?:\b(?:const|let|var|import)\s|[\p{L}_][\p{L}\p{N}_]*\s*(?:=>|==|=(?!=))|[{}]|^\s*return\b.*;)/u.test(line)
    ) {
      ranges.push([offset, offset + line.length]);
    }
    offset += line.length + 1;
  }
  return ranges;
}

function guardedMatches(
  text: string,
  snippets?: Snippet[] | null
): Array<SnippetTriggerRange & { replacement: string }> {
  const matcher = getMatcher(snippets);
  if (!matcher) return [];
  const spans = protectedRanges(text);
  const matches: Array<SnippetTriggerRange & { replacement: string }> = [];
  for (const match of text.matchAll(matcher.regex)) {
    const replacement = resolveReplacement(match[0], matcher.replacements);
    const start = match.index;
    const end = start + match[0].length;
    if (replacement === undefined || spans.some(([a, b]) => start < b && end > a)) continue;
    // Apostrophes allow possessives, but not pieces of n't contractions.
    if (/^[’']t\b/i.test(text.slice(end)) || /n[’']$/i.test(text.slice(0, start))) continue;
    matches.push({ start, end, replacement });
  }
  return matches;
}

/** Trigger identity: case, punctuation, symbols and spaces are ignored. */
export function normalizeWhole(value: string): string {
  return value.normalize("NFC").toLowerCase().replace(/[\p{P}\p{S}\s]/gu, "");
}

/**
 * Replace every spoken trigger with its saved text in a single pass. Saved
 * text is inserted exactly and never expanded again.
 */
export function expandSnippets(text: string, snippets?: Snippet[] | null): string {
  if (!text) return text;
  if (!getMatcher(snippets)) return text;
  // NFC so a decomposed "I" + U+0307 in the transcript recombines into İ.
  const normalized = text.normalize("NFC");
  // An utterance that is only a trigger ("My portfolio.") becomes exactly the
  // saved text, without the punctuation dictation added around it.
  const whole = normalizeWhole(normalized);
  const exact = whole && snippets!.find((s) => normalizeWhole(s?.trigger ?? "") === whole);
  if (exact && !protectedRanges(normalized).some(([a, b]) => a < normalized.length && b > 0)) {
    return exact.replacement;
  }
  let result = "";
  let cursor = 0;
  for (const m of guardedMatches(normalized, snippets)) {
    result += normalized.slice(cursor, m.start) + m.replacement;
    cursor = m.end;
  }
  return result + normalized.slice(cursor);
}

/** An error message for the snippet editor, or "" when the snippet is valid. */
export function validateSnippet(
  trigger: string,
  replacement: string,
  snippets: Snippet[],
  previousTrigger: string | null = null
): string {
  if (!trigger.trim() || !normalizeWhole(trigger)) {
    return "Enter a spoken trigger with letters or numbers.";
  }
  if (trigger.trim().length > MAX_SNIPPET_TRIGGER_LENGTH) {
    return "Use a trigger of 100 characters or fewer.";
  }
  if (!replacement.trim()) return "Enter the text to insert.";
  const fold = (value: string) => value.normalize("NFC").toLowerCase();
  const conflict = (Array.isArray(snippets) ? snippets : []).find(
    (s) =>
      typeof s?.trigger === "string" &&
      s.trigger.trim() &&
      fold(s.trigger.trim()) !== fold((previousTrigger || "").trim()) &&
      normalizeWhole(s.trigger) === normalizeWhole(trigger)
  );
  return conflict
    ? `This conflicts with “${conflict.trigger.trim()}”. Choose a different phrase.`
    : "";
}

/**
 * Dictionary words plus snippet triggers — the hint list fed to the STT
 * prompt and cleanup-model dictionary suffix so triggers survive both.
 */
export function getDictionaryHintWords(
  settings?: {
    customDictionary?: string[] | null;
    snippets?: Snippet[] | null;
  } | null
): string[] {
  const dictionary = Array.isArray(settings?.customDictionary) ? settings.customDictionary : [];
  const snippets = Array.isArray(settings?.snippets) ? settings.snippets : [];
  if (snippets.length === 0) return [...dictionary];

  const triggers = snippets
    .map((s) => s?.trigger)
    .filter((t): t is string => typeof t === "string" && t.length > 0);
  return [...dictionary, ...triggers];
}
