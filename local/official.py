"""Unpack an official OpenWhispr Windows installer into a portable app folder.

  python -I local/official.py <OpenWhispr-Setup-X.Y.Z.exe> <out_dir> [--sha256 HEX]

The NSIS installer carries the app as an embedded 7z archive. This finds the
archive by its signature, checks it is complete (end-header CRC), and extracts
it with Windows' built-in tar.exe (libarchive). Nothing from the installer is
executed. The installer should come from the GitHub release; pass the digest
the release lists to check it first.
"""
import argparse
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

SIGNATURE = b"7z\xbc\xaf\x27\x1c"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_archive(data):
    """Offset and length of the largest complete 7z archive in data."""
    best = None
    at = data.find(SIGNATURE)
    while at >= 0:
        if at + 32 <= len(data):
            start_crc, next_off, next_size, next_crc = struct.unpack_from("<IQQI", data, at + 8)
            ok = zlib.crc32(data[at + 12:at + 32]) == start_crc
            end = at + 32 + next_off + next_size
            if ok and end <= len(data) and zlib.crc32(data[at + 32 + next_off:end]) == next_crc:
                if best is None or end - at > best[1]:
                    best = (at, end - at)
        at = data.find(SIGNATURE, at + 1)
    return best


def unpack(setup, out_dir, expected=None):
    digest = sha256(setup)
    if expected and digest != expected.lower():
        sys.exit(f"sha256 mismatch: {digest} != {expected}")
    with open(setup, "rb") as f:
        data = f.read()
    found = find_archive(data)
    if not found:
        sys.exit("no complete 7z archive found in the installer")
    at, size = found
    # The NSIS payload may itself wrap app-64.7z; unwrap once if needed.
    with tempfile.TemporaryDirectory(dir=os.path.dirname(os.path.abspath(out_dir))) as tmp:
        archive = os.path.join(tmp, "payload.7z")
        with open(archive, "wb") as f:
            f.write(data[at:at + size])
        stage = os.path.join(tmp, "stage")
        os.makedirs(stage)
        tar = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "tar.exe")
        subprocess.run([tar, "-xf", archive, "-C", stage], check=True)
        inner = [os.path.join(stage, n) for n in os.listdir(stage) if n.lower().startswith("app-") and n.endswith(".7z")]
        if not os.path.exists(os.path.join(stage, "OpenWhispr.exe")) and inner:
            nested = os.path.join(tmp, "app")
            os.makedirs(nested)
            subprocess.run([tar, "-xf", inner[0], "-C", nested], check=True)
            stage = nested
        if not os.path.exists(os.path.join(stage, "resources", "app.asar")):
            sys.exit("extracted payload has no resources/app.asar")
        if os.path.exists(out_dir):
            sys.exit(f"{out_dir} already exists")
        shutil.move(stage, out_dir)
    print(f"installer sha256 {digest}")
    print(f"7z archive at offset {at}, {size} bytes")
    print(f"unpacked to {out_dir}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("setup")
    p.add_argument("out_dir")
    p.add_argument("--sha256")
    a = p.parse_args()
    unpack(a.setup, a.out_dir, a.sha256)
