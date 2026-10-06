"""Vendor three.js's ES module build and the addons the 3D scene uses.

Downloads the pinned release from the npm registry, checks it against the
registry's integrity hash, and copies the files below into
``waldo_commander/scene3d/vendor/three``. Run from the repository root after
changing ``VERSION``: ``python scripts/vendor_three.py``.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import tarfile
import urllib.request
from pathlib import Path

VERSION = "0.184.0"

FILES = {
    "build/three.module.min.js": "three.module.min.js",
    "build/three.core.min.js": "three.core.min.js",
    "examples/jsm/controls/OrbitControls.js": "addons/controls/OrbitControls.js",
    "examples/jsm/controls/TransformControls.js": "addons/controls/TransformControls.js",
    "examples/jsm/helpers/ViewHelper.js": "addons/helpers/ViewHelper.js",
    "examples/jsm/loaders/STLLoader.js": "addons/loaders/STLLoader.js",
    "examples/jsm/renderers/CSS2DRenderer.js": "addons/renderers/CSS2DRenderer.js",
    "LICENSE": "LICENSE",
}

DEST = Path(__file__).resolve().parent.parent / "waldo_commander/scene3d/vendor/three"


def main() -> None:
    with urllib.request.urlopen(f"https://registry.npmjs.org/three/{VERSION}") as r:
        dist = json.load(r)["dist"]
    with urllib.request.urlopen(dist["tarball"]) as r:
        tarball = r.read()
    algorithm, expected = dist["integrity"].split("-", 1)
    actual = base64.b64encode(hashlib.new(algorithm, tarball).digest()).decode()
    if actual != expected:
        raise SystemExit(f"three {VERSION}: integrity mismatch")

    shutil.rmtree(DEST, ignore_errors=True)
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        for source, target in FILES.items():
            member = tar.extractfile(f"package/{source}")
            if member is None:
                raise SystemExit(f"three {VERSION} has no {source}")
            path = DEST / target
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(member.read())
    (DEST / "index.js").write_text('export * from "./three.module.min.js";\n')
    (DEST / "VERSION").write_text(f"{VERSION}\n")


if __name__ == "__main__":
    main()
