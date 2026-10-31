#!/usr/bin/env python3
"""Rebuild the repository indexes from the archives a Release actually holds.

Why this exists: `build_repo.py` writes the index and the archives in one pass, so the two
agree only as long as the archives uploaded to the Release are the ones that build produced.
If a run uploads its archives but the Pages deploy fails, the Release ends up ahead of the
index and every install fails its checksum with no way for a client to recover.

This tool takes the metadata (names, versions, dependencies, licences, categories) from the
index that is already published and replaces only the fields that describe the archive:
`url`, `sha256` and `size`. Those come from the Release asset list, which is the same thing
the client downloads. Running it repairs drift without rebuilding or re-uploading anything.

    python tools/reindex.py --out dist
    python tools/reindex.py --source-dir dist --out dist   # offline, from a local index

The result is a set of `index-<arch>.json` files ready for `actions/upload-pages-artifact`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ARCHES = ("aarch64", "arm", "x86_64")
DEFAULT_REPO = "0xberkay/wearterm-repo"
DEFAULT_TAG = "repo"
DEFAULT_SOURCE = "https://0xberkay.github.io/wearterm-repo"


def fail(message: str) -> None:
    print(f"reindex: {message}", file=sys.stderr)
    raise SystemExit(1)


def release_assets(repo: str) -> dict[str, tuple[int, str]]:
    """`name -> (size, sha256)` for every asset on the rolling Release, all pages included."""
    release_id = subprocess.run(
        ["gh", "api", f"repos/{repo}/releases/tags/{DEFAULT_TAG}", "--jq", ".id"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    listing = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repo}/releases/{release_id}/assets",
            "--jq",
            r'.[] | "\(.name)\t\(.size)\t\(.digest)"',
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assets: dict[str, tuple[int, str]] = {}
    for line in listing.splitlines():
        if not line.strip():
            continue
        name, size, digest = line.split("\t")
        digest = digest.removeprefix("sha256:")
        if not digest or digest == "null":
            fail(f"{name}: Release reports no digest")
        assets[name] = (int(size), digest.lower())
    if not assets:
        fail("Release holds no assets")
    return assets


def read_index(source: str, arch: str) -> dict:
    if source.startswith(("http://", "https://")):
        url = f"{source.rstrip('/')}/index-{arch}.json"
        with urllib.request.urlopen(url, timeout=60) as response:
            return json.load(response)
    path = Path(source) / f"index-{arch}.json"
    if not path.exists():
        fail(f"{path}: no such index (needed as the metadata source)")
    return json.loads(path.read_text())


def rebase(index: dict, assets: dict[str, tuple[int, str]], arch: str) -> tuple[dict, int]:
    changed = 0
    for entry in index["packages"]:
        name = entry["name"]
        # The URL is the single source of truth for which asset this entry points at; the
        # asset name is its last path segment, percent-decoded.
        asset_name = urllib.parse.unquote(entry["url"].rsplit("/", 1)[-1])
        asset = assets.get(asset_name)
        if asset is None:
            fail(f"{arch}: {name} points at {asset_name}, which the Release does not have")
        size, digest = asset
        if entry.get("sha256", "").lower() != digest or entry.get("size") != size:
            changed += 1
            entry["sha256"] = digest
            entry["size"] = size
        entry["arch"] = arch
    index["arch"] = arch
    return index, changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/name holding the Release")
    parser.add_argument(
        "--source",
        default=DEFAULT_SOURCE,
        help="Base URL or directory holding the indexes whose metadata is kept.",
    )
    parser.add_argument("--out", default="dist")
    parser.add_argument(
        "--revision",
        default=os.environ.get("GITHUB_SHA", ""),
        help="Value for the index revision (default: GITHUB_SHA).",
    )
    parser.add_argument("--arch", action="append", choices=ARCHES, default=None)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only verify that the indexes match the Release; exit non-zero on any drift.",
    )
    args = parser.parse_args()

    assets = release_assets(args.repo)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    drifted = 0
    for arch in args.arch or ARCHES:
        index = read_index(args.source, arch)
        index, changed = rebase(index, assets, arch)
        if args.check:
            drifted += changed
            total = len(index["packages"])
            print(f"[{arch}] {total} packages, {changed} out of sync with the Release")
            continue
        if args.revision:
            index["revision"] = args.revision
        target = out / f"index-{arch}.json"
        target.write_text(json.dumps(index, indent=2))
        total = len(index["packages"])
        print(f"[{arch}] {total} packages, {changed} hash/size corrected -> {target}")

    if args.check and drifted:
        fail(f"{drifted} index entries do not match the archives on the Release")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
