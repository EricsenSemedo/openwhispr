"""Send the same cleanup requests to several adapters and diff the replies.

Text-only: no audio is sent anywhere. Usage:
  python -I compare_endpoints.py http://127.0.0.1:18765 http://127.0.0.1:18767 http://127.0.0.1:18766
The first URL is the reference; 'id'/'created' fields are ignored.
"""
import json
import sys
import urllib.error
import urllib.request

MODEL = "conservative-filler-filter-v1"
WRAP = ("<transcript>\n", "\n</transcript>\n\nOutput only the cleaned transcript.")

SAMPLES = [
    "um so like I I think we should uh go",
    "Um, Dana needs 42 at 10:30.",
    "Um I'm not sure, uh, maybe tomorrow.",
    "I want to paint uh the old fence. And what is the weather? I I can't tell.",
    "Talk about the the garden plan uh later.",
    "The ums are still there yeah, it should uh be the default for the store.",
    "The word um means hesitation, uh, in English.",
    'He said "um hello" and uh left.',
    "schedule the um movers",
    "Is that its name? right now, uh in town",
    "I think that that shelf broke, or or what?",
    "UH is a union and Uh can help with sodium umber.",
    "um",
    "I think um",
    "uh. Next.",
    "send it to me@example.com um tomorrow",
    "line one uh here\n```\nuh the the code\n```\nuh last line",
    "It's um working and can't uh stop.",
    "so umm we uhh go to to the store",
    "Um, so I think we should, uh, go to the the store tomorrow.",
]


def post(base, payload, origin=None):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(base + "/v1/chat/completions", data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    if origin:
        req.add_header("Origin", origin)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as r:
            status, body, hdrs = r.status, json.load(r), r.headers
    except urllib.error.HTTPError as e:
        status, body, hdrs = e.code, json.load(e), e.headers
    body.pop("id", None) if status != 200 else None
    if isinstance(body, dict):
        body.pop("created", None)
    cors = (hdrs.get("Access-Control-Allow-Origin"), hdrs.get("Vary"))
    return status, body, cors


def content(reply):
    status, body, _ = reply
    if status == 200:
        return body["choices"][0]["message"]["content"]
    return f"<{status} {body.get('error')}>"


def main(urls):
    ref, others = urls[0], urls[1:]
    rows = []
    protocol_diffs = 0
    for i, text in enumerate(SAMPLES, 1):
        wrapped = i % 3 == 0  # exercise the app's transport envelope too
        msg = WRAP[0] + text + WRAP[1] if wrapped else text
        payload = {"model": MODEL, "messages": [{"role": "system", "content": "x"},
                                                {"role": "user", "content": msg}]}
        replies = [post(u, payload, origin="null" if i % 2 else None) for u in urls]
        rows.append((i, text, wrapped, [content(r) for r in replies]))
        for u, r in zip(others, replies[1:]):
            same_shape = (r[0] == replies[0][0] and r[2] == replies[0][2] and
                          set(r[1]) == set(replies[0][1]))
            if not same_shape:
                protocol_diffs += 1
                print(f"PROTOCOL DIFF sample {i} {u}: {r[0]} {r[2]} vs {replies[0][0]} {replies[0][2]}")
    # Error-path parity
    bad = [
        ({"model": "x", "messages": []}, None),
        ({"model": MODEL, "stream": True, "messages": []}, None),
        ({"model": MODEL, "messages": {}}, None),
        ({"model": MODEL, "messages": []}, None),
        ({"model": MODEL, "messages": [{"role": "user", "content": 5}]}, None),
        ({"model": MODEL, "messages": [{"role": "user", "content": "um hi"}]}, "http://evil.example"),
    ]
    for payload, origin in bad:
        replies = [post(u, payload, origin) for u in urls]
        for u, r in zip(others, replies[1:]):
            if (r[0], r[1], r[2]) != (replies[0][0], replies[0][1], replies[0][2]):
                protocol_diffs += 1
                print(f"ERROR-PATH DIFF {u}: {r} vs {replies[0]}")
    print(json.dumps({"samples": len(SAMPLES), "error_cases": len(bad),
                      "protocol_diffs": protocol_diffs}, indent=1))
    for i, text, wrapped, outs in rows:
        print(f"\n[{i}] {'(wrapped) ' if wrapped else ''}{text!r}")
        for u, o in zip(urls, outs):
            mark = "" if o == outs[0] else "   <-- differs from reference"
            print(f"   {u}: {o!r}{mark}")


if __name__ == "__main__":
    main(sys.argv[1:])
