#!/usr/bin/env python3
"""Build the zbd package repository from upstream Termux packages.

Termux ships prebuilt Android binaries for exactly the architectures Wear OS runs on, so
rebuilding them from source would be weeks of work for an identical result. The one thing that
has to change is the install prefix, which Termux bakes into binaries as an absolute path.

That is why the app's application id is `com.zbd.wt`: it is ten characters, exactly as long as
`com.termux`, so `/data/data/com.zbd.wt/files/usr` and `/data/data/com.termux/files/usr` have
the same byte length. The prefix can therefore be rewritten in place inside ELF files, scripts
and data files without shifting a single offset -- no patchelf, no proot, no ptrace overhead.

Output:
    dist/index-<arch>.json   the repository index, served from GitHub Pages
    dist/<name>_<ver>_<arch>.zbd   package archives, uploaded as Release assets
"""

from __future__ import annotations

import argparse
import hashlib
import gzip
import io
import json
import lzma
import os
import re
import shutil
import sys
import tarfile
import urllib.request
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

TERMUX_REPO = "https://packages.termux.dev/apt/termux-main"
TERMUX_PACKAGES = "https://github.com/termux/termux-packages"
TERMUX_RECIPES = "https://raw.githubusercontent.com/termux/termux-packages/master"

# Where a package's build.sh lives, in the order worth trying.
RECIPE_DIRS = ("packages", "root-packages", "x11-packages")
OLD_ID = b"com.termux"
NEW_ID = b"com.zbd.wt"

assert len(OLD_ID) == len(NEW_ID), "the in-place patch depends on equal-length ids"

OLD_PREFIX = "data/data/com.termux/files/usr"
NEW_PREFIX = "data/data/com.zbd.wt/files/usr"

ARCHES = {"aarch64": "aarch64", "arm": "arm", "x86_64": "x86_64"}

# The smallest set that gives a usable interactive shell on first run. Anything else the user
# can install later; the bootstrap has to stay small because it downloads before first use.
BOOTSTRAP_PACKAGES = ["bash", "busybox", "coreutils", "ncurses", "readline", "libandroid-support"]

# Category for anything that entered the snapshot as somebody else's dependency.
DEPENDENCY_CATEGORY = "Libraries"

PROFILE = """# zbd userland
export PS1='\\w $ '
export PATH=$PREFIX/bin:/system/bin:/system/xbin
export TMPDIR=$PREFIX/tmp
[ -d "$HOME" ] || mkdir -p "$HOME"
"""

# The package manager itself lives in the app, where the index cache, progress reporting and
# storage checks already are. This shim is just an IPC front end for it, so there is exactly
# one implementation of dependency resolution rather than two that can disagree.
ZBD_SHIM = """#!/data/data/com.zbd.wt/files/usr/bin/sh
run="$PREFIX/var/run"
mkdir -p "$run"
: > "$run/zbd.log"
printf '%s\\n' "$*" > "$run/zbd.request"
while ! grep -q '^__zbd_done' "$run/zbd.log" 2>/dev/null; do
    sleep 0.3
done
sed '/^__zbd_done/d' "$run/zbd.log"
"""


@dataclass
class Package:
    name: str
    version: str
    arch: str
    filename: str
    description: str = ""
    homepage: str = ""
    license: str = ""
    depends: list[str] = field(default_factory=list)


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url) as response:
        return response.read()


@dataclass
class Provenance:
    """What the apt index cannot tell us: the licence and where the source actually is."""

    license: str = ""
    recipe: str = ""
    source: str = ""


def _recipe_field(script: str, key: str) -> str:
    match = re.search(rf"^{key}=(.*)$", script, re.MULTILINE)
    if not match:
        return ""
    value = match.group(1).strip()
    if value.startswith("("):  # an array of mirrors; the recipe URL covers the user anyway
        return ""
    return value.strip('"').strip("'")


@lru_cache(maxsize=None)
def subpackage_parents() -> dict[str, tuple[str, str]]:
    """Maps a subpackage to the recipe that produces it: `<name> -> (directory, parent)`.

    `curl`, `xz-utils` and friends have no recipe of their own — they are split out of
    `libcurl`, `liblzma` and so on by a `<name>.subpackage.sh` beside the parent's build.sh.
    One tree listing is enough to find every one of them.
    """
    url = "https://api.github.com/repos/termux/termux-packages/git/trees/master?recursive=1"
    try:
        tree = json.loads(fetch(url))["tree"]
    except Exception as error:
        print(f"warning: subpackage lookup unavailable ({error})", file=sys.stderr)
        return {}

    parents: dict[str, tuple[str, str]] = {}
    for node in tree:
        path = node.get("path", "")
        if not path.endswith(".subpackage.sh"):
            continue
        parts = path.split("/")
        if len(parts) != 3 or parts[0] not in RECIPE_DIRS:
            continue
        directory, parent, filename = parts
        parents[filename[: -len(".subpackage.sh")]] = (directory, parent)
    return parents


@lru_cache(maxsize=None)
def load_provenance(name: str) -> Provenance:
    """Reads `TERMUX_PKG_LICENSE` and `TERMUX_PKG_SRCURL` from the upstream build recipe.

    The apt index has no `License` field at all, and its only pointer is the `.deb` — a
    binary, which is not the "corresponding source" the GPL asks a redistributor to offer.
    The recipe has both the licence and the upstream tarball, and it is also the thing
    someone would need to reproduce the build.
    """
    candidates = [(directory, name) for directory in RECIPE_DIRS]
    parent = subpackage_parents().get(name)
    if parent:
        # A subpackage inherits the licence and source of the recipe it was split out of.
        candidates.append(parent)

    for directory, recipe in candidates:
        try:
            script = fetch(f"{TERMUX_RECIPES}/{directory}/{recipe}/build.sh").decode(
                "utf-8", "replace"
            )
        except Exception:
            continue
        source = _recipe_field(script, "TERMUX_PKG_SRCURL")
        version = _recipe_field(script, "TERMUX_PKG_VERSION")
        if version:
            source = source.replace("${TERMUX_PKG_VERSION}", version)
        if "$" in source:
            # Some recipes build the URL with shell substitutions we are not going to
            # evaluate. A missing tarball link is better than a broken one — the recipe URL
            # below still leads to the source.
            source = ""
        return Provenance(
            license=_recipe_field(script, "TERMUX_PKG_LICENSE"),
            recipe=f"{TERMUX_PACKAGES}/tree/master/{directory}/{recipe}",
            source=source,
        )
    return Provenance()


def parse_depends(value: str) -> list[str]:
    """Strips version constraints and alternatives; the snapshot pins one version anyway."""
    names = []
    for clause in value.split(","):
        first = clause.split("|")[0].strip()
        name = first.split("(")[0].strip()
        if name:
            names.append(name)
    return names


def load_upstream_index(arch: str) -> dict[str, Package]:
    base = f"{TERMUX_REPO}/dists/stable/main/binary-{arch}/Packages"
    # The upstream index is served plain and gzipped; prefer the smaller transfer.
    try:
        raw = gzip.decompress(fetch(base + ".gz")).decode("utf-8", "replace")
    except Exception:
        raw = fetch(base).decode("utf-8", "replace")

    packages: dict[str, Package] = {}
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        fields: dict[str, str] = {}
        key = None
        for line in block.splitlines():
            if line.startswith((" ", "\t")) and key:
                fields[key] += " " + line.strip()
            elif ":" in line:
                key, _, value = line.partition(":")
                fields[key] = value.strip()
        name = fields.get("Package")
        if not name:
            continue
        packages[name] = Package(
            name=name,
            version=fields.get("Version", "0"),
            arch=arch,
            filename=fields.get("Filename", ""),
            description=fields.get("Description", "").strip(),
            homepage=fields.get("Homepage", ""),
            license=fields.get("License", ""),
            depends=parse_depends(fields.get("Depends", "")),
        )
    return packages


def read_ar_members(data: bytes) -> dict[str, bytes]:
    """Minimal `ar` reader: a .deb is an ar archive of control/data tarballs."""
    if not data.startswith(b"!<arch>\n"):
        raise ValueError("not an ar archive")
    members: dict[str, bytes] = {}
    offset = 8
    while offset + 60 <= len(data):
        header = data[offset:offset + 60]
        name = header[0:16].decode().strip().rstrip("/")
        size = int(header[48:58].decode().strip())
        start = offset + 60
        members[name] = data[start:start + size]
        offset = start + size + (size % 2)
    return members


def patch_bytes(payload: bytes) -> bytes:
    """Equal-length prefix rewrite; safe inside ELF sections because nothing moves."""
    return payload.replace(OLD_ID, NEW_ID)


def extract_payload(deb: bytes, staging: Path) -> None:
    members = read_ar_members(deb)
    data_name = next(name for name in members if name.startswith("data.tar"))
    stream = io.BytesIO(members[data_name])

    mode = "r:xz" if data_name.endswith(".xz") else "r:*"
    with tarfile.open(fileobj=stream, mode=mode) as archive:
        for member in archive.getmembers():
            path = member.name.lstrip("./")
            if not path.startswith(OLD_PREFIX):
                continue
            relative = path[len(OLD_PREFIX):].lstrip("/")
            if not relative:
                continue
            target = staging / relative

            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.issym() or member.islnk():
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink() or target.exists():
                    target.unlink()
                link = member.linkname.replace(OLD_PREFIX, NEW_PREFIX)
                os.symlink(link, target)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                target.write_bytes(patch_bytes(extracted.read()))
                target.chmod(member.mode)


def resolve(names: list[str], upstream: dict[str, Package]) -> list[Package]:
    ordered: dict[str, Package] = {}

    def visit(name: str) -> None:
        if name in ordered or name not in upstream:
            return
        package = upstream[name]
        ordered[name] = package
        for dependency in package.depends:
            visit(dependency)

    for name in names:
        if name not in upstream:
            print(f"warning: unknown upstream package {name}", file=sys.stderr)
        visit(name)
    return list(ordered.values())


def directory_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            file_path = Path(root) / name
            if not file_path.is_symlink():
                total += file_path.stat().st_size
    return total


def asset_version(version: str) -> str:
    """
    A version as it may appear in an asset filename. GitHub rewrites `:` to `.` when a
    release asset is uploaded, so an epoch like `1:2026.07.16` has to be flattened here or
    every URL written into the index points at a name the release does not have.
    """
    return version.replace(":", ".")


def build_archive(package: Package, staging: Path, manifest: dict, out: Path) -> Path:
    """Writes `<name>_<version>_<arch>.zbd`: gzip so the client needs no extra dependency."""
    archive_path = (
        out / f"{package.name}_{asset_version(package.version)}_{package.arch}.zbd"
    )
    manifest_bytes = json.dumps(manifest, indent=2).encode()

    with tarfile.open(archive_path, "w:gz") as archive:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest_bytes)
        archive.addfile(info, io.BytesIO(manifest_bytes))
        archive.add(staging, arcname="files", recursive=True)
    return archive_path


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_arch(
    arch: str, wanted: list[str], categories: dict[str, str], out: Path, asset_base: str
) -> None:
    upstream = load_upstream_index(arch)
    selected = resolve(wanted, upstream)
    work = out / f"work-{arch}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    entries = []
    for package in selected:
        print(f"[{arch}] {package.name} {package.version}")
        deb = fetch(f"{TERMUX_REPO}/{package.filename}")

        staging = work / package.name
        staging.mkdir(parents=True, exist_ok=True)
        extract_payload(deb, staging)

        installed_size = directory_size(staging)
        provenance = load_provenance(package.name)
        manifest = {
            "name": package.name,
            "version": package.version,
            "arch": arch,
            # Pulled-in dependencies were never asked for by name, so they land in Libraries
            # and stay out of the way of anyone browsing by category.
            "category": categories.get(package.name, DEPENDENCY_CATEGORY),
            "description": package.description,
            "homepage": package.homepage,
            "license": provenance.license or package.license,
            # The build recipe, not the .deb: it names the licence, points at the upstream
            # tarball and is enough to rebuild the binary.
            "sourceUrl": provenance.recipe,
            "upstreamSourceUrl": provenance.source,
            "binaryUrl": f"{TERMUX_REPO}/{package.filename}",
            "deps": [d for d in package.depends if d in upstream],
        }

        archive = build_archive(package, staging, manifest, out)
        entry = dict(manifest)
        entry.update(
            {
                "url": f"{asset_base}/{archive.name}",
                "sha256": sha256_of(archive),
                "size": archive.stat().st_size,
                "installedSize": installed_size,
            }
        )
        entries.append(entry)

    entries.append(build_bootstrap(arch, selected, work, out, asset_base))

    unlicensed = [e["name"] for e in entries if not e["license"]]
    if unlicensed:
        # Subpackages carry no recipe of their own, so a gap here is expected rather than a
        # failure — but it is worth seeing in the log rather than discovering in the index.
        print(f"[{arch}] no licence found for: {' '.join(sorted(unlicensed))}")

    index = {"revision": os.environ.get("GITHUB_SHA", "local"), "arch": arch, "packages": entries}
    (out / f"index-{arch}.json").write_text(json.dumps(index, indent=2))
    shutil.rmtree(work, ignore_errors=True)


def build_bootstrap(
    arch: str, selected: list[Package], work: Path, out: Path, asset_base: str
) -> dict:
    """Merges the core packages into one archive so first run is a single download."""
    closure = _bootstrap_closure(selected)
    included = [package for package in selected if package.name in closure]

    staging = work / "__bootstrap"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    for package in included:
        merge_tree(work / package.name, staging)

    (staging / "etc").mkdir(parents=True, exist_ok=True)
    (staging / "etc" / "profile").write_text(PROFILE)

    bin_dir = staging / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "zbd"
    shim.write_text(ZBD_SHIM)
    shim.chmod(0o700)

    # Termux's bash package does not provide `sh`; the app launches $PREFIX/bin/sh.
    sh = bin_dir / "sh"
    if not sh.exists() and not sh.is_symlink() and (bin_dir / "bash").exists():
        os.symlink("bash", sh)

    version = os.environ.get("GITHUB_RUN_NUMBER", "1")
    manifest = {
        "name": "bootstrap",
        "version": version,
        "arch": arch,
        "category": "Core",
        "description": "Core userland: shell, coreutils and the zbd client",
        "homepage": "",
        # A merged archive carries every licence it merged, listed rather than summarised so
        # the strongest terms in the set are visible without opening anything.
        # Split on commas first: a single recipe often names several licences, and deduping
        # whole strings would leave "GPL-3.0, GPL-3.0" in the list.
        "license": ", ".join(
            sorted(
                {
                    term.strip()
                    for p in included
                    for term in load_provenance(p.name).license.split(",")
                    if term.strip()
                }
            )
        ),
        "sourceUrl": f"{TERMUX_PACKAGES}/tree/master/packages",
        "components": sorted(p.name for p in included),
        "deps": [],
    }
    package = Package(name="bootstrap", version=version, arch=arch, filename="")
    archive = build_archive(package, staging, manifest, out)

    entry = dict(manifest)
    entry.update(
        {
            "url": f"{asset_base}/{archive.name}",
            "sha256": sha256_of(archive),
            "size": archive.stat().st_size,
            "installedSize": directory_size(staging),
        }
    )
    return entry


def _bootstrap_closure(selected: list[Package]) -> set[str]:
    by_name = {p.name: p for p in selected}
    closure: set[str] = set()

    def visit(name: str) -> None:
        if name in closure or name not in by_name:
            return
        closure.add(name)
        for dependency in by_name[name].depends:
            visit(dependency)

    for name in BOOTSTRAP_PACKAGES:
        visit(name)
    return closure


def merge_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        return
    for root, dirs, files in os.walk(source):
        relative = Path(root).relative_to(source)
        (destination / relative).mkdir(parents=True, exist_ok=True)
        for name in dirs + files:
            src = Path(root) / name
            dst = destination / relative / name
            if src.is_symlink():
                if dst.is_symlink() or dst.exists():
                    continue
                os.symlink(os.readlink(src), dst)
            elif src.is_file():
                shutil.copy2(src, dst)


def read_package_list(path: Path) -> dict[str, str]:
    """
    Maps every requested package to its category. Order is preserved: the file reads as the
    menu the watch ends up showing, so the two cannot drift apart.
    """
    categories: dict[str, str] = {}
    current = DEPENDENCY_CATEGORY
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            marker = re.match(r"#\s*category:\s*(.+)", line, re.IGNORECASE)
            if marker:
                current = marker.group(1).strip()
            continue
        categories[line] = current
    return categories


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packages", default="packages.list")
    parser.add_argument("--out", default="dist")
    parser.add_argument(
        "--asset-base",
        default=os.environ.get("ASSET_BASE", "https://example.invalid/releases/download/repo"),
        help="Base URL the .zbd archives are served from (a GitHub Release tag).",
    )
    parser.add_argument("--arch", action="append", choices=sorted(ARCHES), default=None)
    args = parser.parse_args()

    categories = read_package_list(Path(args.packages))
    wanted = list(categories)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    for arch in args.arch or sorted(ARCHES):
        build_arch(arch, wanted, categories, out, args.asset_base)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
