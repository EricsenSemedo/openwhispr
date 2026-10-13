"""Build this fork for Windows on top of the official release folder.

  python -I local/build.py --official <app_dir> --out <new_app_dir> [--ref HEAD]
  python -I local/build.py --official <app_dir> --ref v1.10.3 --verify-upstream

Why an overlay: the official release already has the native helpers, models
and node_modules for its version. Only the app's own JavaScript differs in
this fork, so the build:
  1. checks out --ref in a CRLF worktree (the official Windows build uses
     CRLF sources; this makes an upstream tag rebuild byte-identical),
  2. builds the renderer with the release's public settings and the licensed
     fonts copied from the official app (never committed),
  3. writes a new app.asar: the official one with every source file and the
     whole src/dist replaced by this build,
  4. copies the official folder to --out with the new archive and
     resources/local-build.json (turns off the stock updater).

--verify-upstream builds an upstream tag and checks that the result equals
the official archive. Run it before trusting a new upstream version.
"""
import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_ROOT = os.path.join(REPO, "local", ".build")
# Public values from the upstream release workflow and the official bundle.
RELEASE_ENV = {
    "VITE_AUTH_URL": "https://auth.openwhispr.com",
    "VITE_OPENWHISPR_API_URL": "https://api.openwhispr.com",
    "VITE_OPENWHISPR_OAUTH_CALLBACK_URL": "https://openwhispr.com/auth/desktop-callback",
}
BLOCK = 4 * 1024 * 1024


# --- asar -----------------------------------------------------------------

def read_asar(path):
    with open(path, "rb") as f:
        data = f.read()
    four, header_size = struct.unpack_from("<II", data, 0)
    assert four == 4, "not an asar archive"
    _, slen = struct.unpack_from("<II", data, 8)
    header = json.loads(data[16:16 + slen].decode("utf-8"))
    return data, header, 8 + header_size


def walk(node, prefix=""):
    for name, child in node.get("files", {}).items():
        p = f"{prefix}/{name}" if prefix else name
        if "files" in child:
            yield from walk(child, p)
        else:
            yield p, child


def integrity(content):
    blocks = [hashlib.sha256(content[i:i + BLOCK]).hexdigest() for i in range(0, max(len(content), 1), BLOCK)]
    return {"algorithm": "SHA256", "hash": hashlib.sha256(content).hexdigest(), "blockSize": BLOCK, "blocks": blocks}


def write_asar(path, entries):
    """entries: list of (path, node) where node is either {'unpacked': ..., ...}
    (kept as is) or {'content': bytes, 'executable': bool}."""
    root = {"files": {}}
    body = bytearray()
    for p, node in sorted(entries, key=lambda e: e[0]):
        parts = p.split("/")
        d = root
        for part in parts[:-1]:
            d = d["files"].setdefault(part, {"files": {}})
        if "content" in node:
            content = node["content"]
            out = {"size": len(content), "offset": str(len(body)), "integrity": integrity(content)}
            if node.get("executable"):
                out["executable"] = True
            body += content
        else:
            out = node
        d["files"][parts[-1]] = out
    js = json.dumps(root, separators=(",", ":")).encode("utf-8")
    pad = (4 - len(js) % 4) % 4
    pickle = struct.pack("<II", 4 + len(js) + pad, len(js)) + js + b"\0" * pad
    with open(path, "wb") as f:
        f.write(struct.pack("<II", 4, len(pickle)) + pickle + bytes(body))


# --- build steps ----------------------------------------------------------

def run(cmd, cwd, env=None):
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def tool(name):
    found = shutil.which(name + ".cmd") or shutil.which(name)
    if not found:
        sys.exit(f"{name} not found on PATH")
    return found


def worktree(ref):
    sha = subprocess.run(["git", "rev-parse", "--verify", ref + "^{commit}"], cwd=REPO, check=True,
                         capture_output=True, text=True).stdout.strip()
    path = os.path.join(BUILD_ROOT, sha[:12])
    if not os.path.exists(path):
        os.makedirs(BUILD_ROOT, exist_ok=True)
        run(["git", "-c", "core.autocrlf=true", "worktree", "add", "--detach", path, sha], REPO)
    return sha, path


def copy_fonts(official, tree):
    out = os.path.join(tree, "src", "assets", "fonts", "yowza")
    os.makedirs(out, exist_ok=True)
    for p, node, content in official:
        name = p.rsplit("/", 1)[-1]
        if p.startswith("src/dist/assets/yowza") and name.endswith(".otf"):
            stem = name[:-4][:-9]  # drop Vite's "-" + 8-character hash
            with open(os.path.join(out, stem + ".otf"), "wb") as f:
                f.write(content)


def build_renderer(tree):
    if not os.path.isdir(os.path.join(tree, "node_modules")):
        run([tool("npm"), "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--engine-strict=false"], tree)
    env = dict(os.environ, **RELEASE_ENV)
    shutil.rmtree(os.path.join(tree, "src", "dist"), ignore_errors=True)
    run([os.path.join(tree, "node_modules", ".bin", "vite.cmd" if os.name == "nt" else "vite"), "build"],
        os.path.join(tree, "src"), env)


def packaged_sources(tree):
    """Source files electron-builder would pack (node_modules and dist aside)."""
    with open(os.path.join(tree, "electron-builder.json"), encoding="utf-8") as f:
        patterns = json.load(f)["files"]
    include = [p for p in patterns if isinstance(p, str) and not p.startswith("!") and "node_modules" not in p
               and not p.startswith("src/dist") and p != "package.json"]
    exclude = [p[1:] for p in patterns if isinstance(p, str) and p.startswith("!") and "node_modules" not in p]
    found = set()
    for pattern in include:
        if "*" not in pattern:
            if os.path.isfile(os.path.join(tree, pattern)):
                found.add(pattern)
            continue
        base = pattern.split("*", 1)[0].rstrip("/")
        for r, _, files in os.walk(os.path.join(tree, base)):
            for name in files:
                rel = os.path.relpath(os.path.join(r, name), tree).replace(os.sep, "/")
                if fnmatch.fnmatch(rel, pattern.replace("**/", "*")):
                    found.add(rel)
    return {p for p in found if not any(fnmatch.fnmatch(p, x.replace("**/", "*")) for x in exclude)}


def compose(official_asar, tree):
    data, header, base = read_asar(official_asar)
    official = []
    for p, node in walk(header):
        content = None if "unpacked" in node else data[base + int(node["offset"]):base + int(node["offset"]) + node["size"]]
        official.append((p, node, content))
    return official


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--official", required=True, help="official portable app folder (see local/official.py)")
    ap.add_argument("--out", help="new app folder to create")
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--verify-upstream", action="store_true")
    a = ap.parse_args()
    if not a.out and not a.verify_upstream:
        ap.error("--out is required unless --verify-upstream")

    official_asar = os.path.join(a.official, "resources", "app.asar")
    sha, tree = worktree(a.ref)
    official = compose(official_asar, tree)
    with open(os.path.join(tree, "package.json"), encoding="utf-8") as f:
        version = json.load(f)["version"]
    official_pkg = next(json.loads(c) for p, _, c in official if p == "package.json")
    if official_pkg["version"] != version:
        sys.exit(f"official app is {official_pkg['version']} but {a.ref} is {version}")

    copy_fonts(official, tree)
    build_renderer(tree)

    sources = packaged_sources(tree)
    entries, replaced, added, kept = [], 0, 0, []
    official_paths = set()
    for p, node, content in official:
        official_paths.add(p)
        if p.startswith("src/dist/"):
            continue
        if "unpacked" in node:
            entries.append((p, node))
            continue
        if p != "package.json" and not p.startswith("node_modules/"):
            src = os.path.join(tree, *p.split("/"))
            if os.path.isfile(src):
                with open(src, "rb") as f:
                    new = f.read()
                replaced += new != content
                content = new
            else:
                kept.append(p)
        entries.append((p, {"content": content, "executable": node.get("executable", False)}))
    for p in sorted(sources - official_paths):
        with open(os.path.join(tree, *p.split("/")), "rb") as f:
            entries.append((p, {"content": f.read()}))
        added += 1
    dist = os.path.join(tree, "src", "dist")
    for r, _, files in os.walk(dist):
        for name in files:
            full = os.path.join(r, name)
            rel = "src/dist/" + os.path.relpath(full, dist).replace(os.sep, "/")
            with open(full, "rb") as f:
                entries.append((rel, {"content": f.read()}))
    print(f"source files replaced {replaced}, added {added}; not in source (kept official): {len(kept)}")
    for p in kept[:10]:
        print("  kept", p)

    if a.verify_upstream:
        mine = {p: n.get("content") for p, n in entries}
        theirs = {p: c for p, _, c in official}
        bad = sorted(p for p in set(mine) | set(theirs) if mine.get(p) != theirs.get(p))
        print(f"verify-upstream: {len(theirs) - len(bad)} of {len(theirs)} entries identical")
        for p in bad[:20]:
            print("  differs", p)
        sys.exit(1 if bad else 0)

    if os.path.exists(a.out):
        sys.exit(f"{a.out} already exists")
    shutil.copytree(a.official, a.out)
    new_asar = os.path.join(a.out, "resources", "app.asar")
    write_asar(new_asar, entries)
    for name in ("app-update.yml",):
        path = os.path.join(a.out, "resources", name)
        if os.path.exists(path):
            os.remove(path)
    with open(new_asar, "rb") as f:
        asar_sha = hashlib.sha256(f.read()).hexdigest()
    with open(official_asar, "rb") as f:
        official_sha = hashlib.sha256(f.read()).hexdigest()
    receipt = {
        "upstreamVersion": version,
        "ref": a.ref,
        "commit": sha,
        "builtAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "officialAsarSha256": official_sha,
        "asarSha256": asar_sha,
    }
    with open(os.path.join(a.out, "resources", "local-build.json"), "w", encoding="utf-8") as f:
        json.dump(receipt, f, indent=2)
        f.write("\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
