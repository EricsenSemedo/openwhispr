"""Diff cleanup_filter against the laptop JS reference (localFillerCleanup.js).

Usage: python -I compare_js_reference.py <path-to-localFillerCleanup.js> [node]
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cleanup_filter import clean_text  # noqa: E402
from compare_endpoints import SAMPLES  # noqa: E402

JS_FIXTURES = ["um I can help", "I uh think so", "um, hello uh there", "10 um tickets",
               "Uh can help", "UH is a name", "Um arrives at 10", "sodium uhx umber", "αum βuh",
               'Say "um hello"', "`uh` is a variable", "The word um means hesitation",
               "a literal um marker", "It's um working", "um", "uh.", "I think um", "uh. Next.",
               "um hello uh there", "I uh agree", "Uh um says yes"]


def main():
    js, node = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "node")
    texts = JS_FIXTURES + SAMPLES
    script = ("const {cleanFillers}=require(process.argv[1]);const t=JSON.parse(require('fs')"
              ".readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(t.map(x=>cleanFillers(x,"
              "{enabled:true,protectedTerms:[]}))))")
    out = subprocess.run([node, "-e", script, str(Path(js).resolve())], input=json.dumps(texts),
                         capture_output=True, text=True, encoding="utf-8", check=True).stdout
    js_out = json.loads(out)
    same = 0
    for t, j in zip(texts, js_out):
        p = clean_text(t, enabled=True, protected_terms=[], remove_repetitions=True)
        if p == j:
            same += 1
        else:
            print(f"DIFF {t!r}\n   js : {j!r}\n   py : {p!r}")
    print(f"identical {same}/{len(texts)}")


if __name__ == "__main__":
    main()
