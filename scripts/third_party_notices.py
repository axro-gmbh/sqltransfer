"""Write THIRD_PARTY_NOTICES.txt from the packages actually installed.

Run it in the build environment before every release, so the file lists exactly
what goes into the bundle:

    python scripts/third_party_notices.py

It walks the dependency closure of [project].dependencies in pyproject.toml, takes
each package's declared license and its shipped license texts, lists the Rust
crates compiled into apitap from the SBOM in its wheel, and adds the native
components that no Python metadata describes.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "THIRD_PARTY_NOTICES.txt"
APITAP_LICENSE = ROOT / ".vendor" / "apitap-lib" / "LICENSE"
# The pod is the copy that ships; the release download is the fallback before a first build.
SPARKLE_LICENSES = [ROOT / "build" / "flutter" / "macos" / "Pods" / "Sparkle" / "LICENSE",
                    ROOT / ".vendor" / "sparkle" / "LICENSE"]

# Build and test tools listed in the environment but not shipped in the app.
NOT_SHIPPED = {"pytest", "pluggy", "iniconfig", "packaging", "pygments"}

COPYLEFT = re.compile(r"GPL|LGPL|MPL|Mozilla|Lesser", re.I)

NATIVE = """\
Native components without Python metadata
------------------------------------------

Python runtime (CPython), bundled by Flet's serious_python
    License: Python Software Foundation License 2.0
    https://docs.python.org/3/license.html

Flutter engine, framework and Flutter/Dart packages
    License: BSD-3-Clause for Flutter itself; the complete notices of every
    Flutter and Dart package are shipped inside the app by Flutter:
    sqltransfer.app/Contents/Frameworks/App.framework/Resources/flutter_assets/NOTICES.Z

Sparkle (sqltransfer.app/Contents/Frameworks/Sparkle.framework), the update framework
    License: MIT, plus the external licenses in its license text below
    https://sparkle-project.org

Libraries bundled inside psycopg-binary (psycopg_binary/.dylibs)
    libpq                          PostgreSQL License
    OpenSSL (libssl, libcrypto)    Apache-2.0
    MIT Kerberos (libkrb5 etc.)    MIT
    OpenLDAP (libldap, liblber)    OpenLDAP Public License 2.8
"""


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _license_of(dist) -> str:
    meta = dist.metadata
    expression = meta.get("License-Expression")
    if expression:
        return expression
    classifiers = [c.split("::")[-1].strip() for c in (meta.get_all("Classifier") or []) if c.startswith("License ::")]
    if classifiers:
        return ", ".join(classifiers)
    text = (meta.get("License") or "").strip().splitlines()
    return text[0][:60] if text else "unknown"


def _homepage(dist) -> str:
    meta = dist.metadata
    for entry in meta.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        if label.strip().lower() in {"source", "source code", "repository", "homepage", "home"}:
            return url.strip()
    return (meta.get("Home-page") or "").strip() or f"https://pypi.org/project/{meta['Name']}/"


def _license_texts(dist) -> list[tuple[str, str]]:
    texts = []
    for f in dist.files or []:
        if re.search(r"(LICEN[CS]E|COPYING|NOTICE)", Path(str(f)).name, re.I):
            try:
                texts.append((str(f), Path(f.locate()).read_text(encoding="utf-8", errors="replace")))
            except OSError:
                pass
    return texts


def _closure(roots: list[str]) -> dict[str, object]:
    seen: dict[str, object] = {}
    queue = list(roots)
    while queue:
        requirement = queue.pop()
        name = re.split(r"[ ;<>=!~\[(]", requirement, maxsplit=1)[0]
        key = _norm(name)
        if key in seen or key in NOT_SHIPPED:
            continue
        try:
            dist = distribution(name)
        except PackageNotFoundError:
            continue
        seen[key] = dist
        extras = re.search(r"\[([^\]]+)\]", requirement)
        wanted = {e.strip() for e in extras.group(1).split(",")} if extras else set()
        for req in dist.requires or []:
            marker = re.search(r"extra\s*==\s*['\"]([^'\"]+)['\"]", req)
            if marker and marker.group(1) not in wanted:
                continue
            queue.append(req)
    return seen


def _rust_crates(apitap_dist) -> list[tuple[str, str, str]]:
    sbom = next((f for f in apitap_dist.files or [] if str(f).endswith(".cyclonedx.json")), None)
    if sbom is None:
        return []
    doc = json.loads(sbom.read_text(encoding="utf-8"))
    crates = []
    for comp in doc.get("components", []):
        licenses = []
        for entry in comp.get("licenses", []):
            licenses.append(entry.get("expression") or (entry.get("license") or {}).get("id")
                            or (entry.get("license") or {}).get("name") or "?")
        crates.append((comp.get("name", "?"), comp.get("version", "?"), " / ".join(licenses) or "?"))
    return sorted(crates)


def main() -> int:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dists = _closure(pyproject["project"]["dependencies"])
    if "apitap" not in dists:
        print("apitap is not installed in this environment", file=sys.stderr)
        return 1
    if not APITAP_LICENSE.exists():
        print(f"missing {APITAP_LICENSE}: clone apitap-lib as described in README.md", file=sys.stderr)
        return 1
    sparkle_license = next((path for path in SPARKLE_LICENSES if path.exists()), None)
    if sparkle_license is None:
        print("missing Sparkle's LICENSE: run flet build once, or download Sparkle as in MACOS_BUILD.md", file=sys.stderr)
        return 1

    lines = [
        "Third-party components shipped with SQL Transfer",
        "=================================================",
        "",
        "Generated by scripts/third_party_notices.py from the installed packages; do not edit by hand.",
        "",
        "Python packages",
        "---------------",
        "",
    ]
    for key in sorted(dists):
        dist = dists[key]
        license_name = _license_of(dist)
        mark = "  [weak copyleft, see below]" if COPYLEFT.search(license_name) else ""
        lines.append(f"{dist.metadata['Name']} {dist.version}")
        lines.append(f"    License: {license_name}{mark}")
        lines.append(f"    {_homepage(dist)}")
    lines += [
        "",
        "Weak copyleft components",
        "------------------------",
        "",
        "paramiko (LGPL-2.1), psycopg and psycopg-binary (LGPL-3.0) and certifi (MPL-2.0) are",
        "shipped unmodified, as separate Python packages that can be replaced. Their source code",
        "is available from the project pages listed above and from https://pypi.org.",
        "",
        NATIVE,
        "Rust crates compiled into apitap (from the SBOM in its wheel)",
        "--------------------------------------------------------------",
        "",
    ]
    lines += [f"{name} {version}    {lic}" for name, version, lic in _rust_crates(dists["apitap"])]
    lines += ["", "", "License texts", "=============", ""]
    lines += ["----- apitap (.vendor/apitap-lib/LICENSE) -----", "", APITAP_LICENSE.read_text(encoding="utf-8").strip(), ""]
    lines += ["----- Sparkle (LICENSE) -----", "", sparkle_license.read_text(encoding="utf-8").strip(), ""]
    for key in sorted(dists):
        for path, text in _license_texts(dists[key]):
            lines += [f"----- {dists[key].metadata['Name']} ({path}) -----", "", text.strip(), ""]

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(dists)} Python packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
