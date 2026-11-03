#!/usr/bin/env python3
"""One-time fetch of the two CPAN distributions diff_dataset_cache.py and diff_config_merge.py need
for real (not mocked) Config::IniFiles behavior: Config::IniFiles itself, and IO::Stringy (one of
its own dependencies). Pure-Perl, pinned versions, unpacked into dev/.cpan_cache/ (gitignored --
not vendored, since these are a third party's released packages, not upstream sanoid's own source;
re-fetching takes a few seconds and always gets the same pinned bytes).

Run once: python3 dev/fetch_cpan.py
"""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
import urllib.request
from pathlib import Path

DEV_DIR = Path(__file__).resolve().parent
CACHE = DEV_DIR / ".cpan_cache"

# Pinned by exact version + sha256, from the metacpan release API, not "latest" -- reproducibility
# matters more than freshness for a differential-testing fixture.
DISTS = {
    "Config-IniFiles": {"version": "3.003000", "module": "Config::IniFiles"},
    "IO-stringy": {"version": "2.111", "module": "IO::Stringy"},
}


def metacpan_download_url(module: str, version: str) -> tuple[str, str]:
    # metacpan's release-by-exact-name endpoint -- "Dist-Version" -- resolves the specific pinned
    # release rather than "whatever is current", which is the whole point of pinning.
    name = f"{module.replace('::', '-')}-{version}"
    with urllib.request.urlopen(f"https://fastapi.metacpan.org/v1/release/{name}", timeout=20) as r:
        meta = json.loads(r.read())
    return meta["download_url"], meta.get("checksum_sha256", "")


def fetch(name: str, info: dict) -> Path:
    dest = CACHE / f"{name}-{info['version']}"
    if dest.exists():
        print(f"{name}-{info['version']}: already present at {dest}")
        return dest
    CACHE.mkdir(parents=True, exist_ok=True)
    print(f"{name}-{info['version']}: fetching from metacpan...")
    url, sha256 = metacpan_download_url(info["module"], info["version"])
    with urllib.request.urlopen(url, timeout=60) as r:
        data = r.read()
    if sha256 and hashlib.sha256(data).hexdigest() != sha256:
        raise RuntimeError(f"{name}: checksum mismatch (metacpan reported {sha256})")
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        tf.extractall(CACHE, filter="data")
    print(f"{name}-{info['version']}: unpacked to {dest}")
    return dest


def main() -> None:
    paths = {name: fetch(name, info) for name, info in DISTS.items()}
    print("\nINI_LIB     =", paths["Config-IniFiles"] / "lib")
    print("STRINGY_LIB =", paths["IO-stringy"] / "lib")
    print("\ndiff_dataset_cache.py and diff_config_merge.py find these on their own; this script is "
          "just here to run once (or after clearing dev/.cpan_cache/) before using them.")


if __name__ == "__main__":
    main()
