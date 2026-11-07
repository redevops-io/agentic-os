"""Attribute every entry of a PyInstaller archive to a shipped component.

Input: an Archive (executable or listing), a distribution inventory, optional facts (for listing-only
runs), optional wheelhouse (for wheel-embedded upstream SBOMs). Output: components + a complete
attribution map (every PKG entry and every PYZ module -> component key) + the entries nothing claims.

Nothing is guessed: a version is recorded only with its evidence (bytes of the archive, wheel metadata,
the build manifest, or a named facts file). Licences are DECLARED values from package metadata.
"""
from __future__ import annotations

import hashlib
import json
import plistlib
import re
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import jsscan, pe
from .archive import Archive
from .inventory import module_owner_map, norm

HERE = Path(__file__).resolve().parent
STDLIB = set((HERE / "stdlib_312.txt").read_text(encoding="utf-8").split())
sys.path.insert(0, str(HERE.parent / "tools"))
from first_party import first_party_names  # noqa: E402  (packaging/tools/first_party.py: git-pins.json + agentic-os)

FIRST_PARTY = set(first_party_names())
OPENSSL_BANNER = re.compile(rb"OpenSSL (\d+\.\d+\.\d+[a-z]?)(?:-[a-z0-9]+)? +(\d{1,2} [A-Z][a-z]{2} \d{4})")
NATIVE_EXT = (".so", ".dylib", ".dll", ".pyd")


@dataclass
class Comp:
    key: str
    name: str
    version: str | None
    kind: str = "library"            # library | application | file | data | platform | framework
    purl: str | None = None
    scope: str = "required"
    hashes: dict[str, str] = field(default_factory=dict)
    licence: dict | None = None      # {"expression"|"name": ..., "source": ...}  (DECLARED)
    evidence: list[str] = field(default_factory=list)
    shipped_in: set[str] = field(default_factory=set)
    props: dict[str, str] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    entries: list[str] = field(default_factory=list)
    refs: dict[str, str] = field(default_factory=dict)      # externalReferences: type -> url
    nested_of: str | None = None


@dataclass
class Analysis:
    comps: dict[str, Comp] = field(default_factory=dict)
    attribution: dict[str, str] = field(default_factory=dict)   # "pkg:<entry>" / "pyz:<module>" -> comp key
    unattributed: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    limitations: dict[str, str] = field(default_factory=dict)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def declared_licence(inv_entry: dict | None) -> dict | None:
    if not inv_entry:
        return None
    lic = inv_entry.get("licence") or {}
    if lic.get("expression"):
        return {"expression": lic["expression"], "source": "METADATA License-Expression"}
    text = (lic.get("license") or "").strip()
    if text and "\n" not in text and len(text) <= 120:
        return {"name": text, "source": "METADATA License"}
    cls = [c.split("::")[-1].strip() for c in lic.get("classifiers", [])]
    if cls:
        return {"name": " / ".join(cls), "source": "METADATA License classifier"}
    if text:
        return {"name": text.splitlines()[0][:100] + " ...", "source": "METADATA License (free text, truncated)"}
    return None


def classify_pkg(name: str, stdlib: set[str] = STDLIB) -> tuple[str, str]:
    """-> (rule, hint). hint is a component key or a top-level name for later dist lookup."""
    n = name.replace("\\", "/")
    low = n.lower()
    base = low.rsplit("/", 1)[-1]
    if re.fullmatch(r"(struct|pyimod\d+_\w+|pyiboot\d+_\w+|pyi_rth_\w+)", n):
        return "pyinstaller-runtime", "pyinstaller-runtime"
    if n == "sidecar_main":
        return "sidecar", "sidecar"
    if n.startswith("Licenses/"):   # the Licenses folder embedded by the sidecar spec (REDEVOPS_LICENSES_DIR)
        return "licenses", "licenses-folder"
    if n == "base_library.zip" or re.fullmatch(r"python3?\d*\.dll|libpython[\d.]*\.dylib|libpython[\d.]*\.so[.\d]*|Python|Python\.framework/.*", n) or "/lib-dynload/" in low:
        return "cpython", "cpython"
    if re.fullmatch(r"(vcruntime140(_1)?|ucrtbase|msvcp140\w*|concrt140|vcomp140)\.dll", base):
        return "ms-dll", base
    if re.fullmatch(r"api-ms-win-[\w\-]+\.dll", base):
        return "ms-apiset", "api-ms-win"
    if re.fullmatch(r"lib(crypto|ssl)[-.\d]*(-x64)?\.(dylib|dll|so[.\d]*)", base):
        return "openssl-lib", base
    if re.fullmatch(r"libffi[-.\d]*\.(dylib|dll|so[.\d]*)", base):
        return "libffi", base
    # Linux: PyInstaller copies these two GCC runtime libraries from the BUILD HOST into the archive (libstdc++ for the DuckDB and
    # other C++ extensions, libgcc_s for it and for pthread cancellation); their version is not discoverable from the files.
    if re.fullmatch(r"(libgcc_s|libstdc\+\+)\.so[.\d]*", base):
        return "gcc-runtime", base
    m = re.match(r"^([A-Za-z0-9_.]+?)-(\d[^/]*)\.dist-info/", n)
    if m:
        return "dist-info", norm(m.group(1))
    top = n.split("/", 1)[0]
    stem = re.sub(r"(\.cpython-\d+[a-z]*-[a-z0-9_]+(-[a-z0-9_]+)*|\.cp\d+-\w+|\.abi3)?\.(py|so|pyd|dylib|dll)$", "", top)
    if "/" not in n and base.endswith(".pyd") and stem in stdlib:
        return "cpython", "cpython"
    return "top-level", stem


def probe_openssl(data: bytes) -> list[tuple[str, str]]:
    return sorted({(m.group(1).decode(), m.group(2).decode()) for m in OPENSSL_BANNER.finditer(data)})


def analyze(archive: Archive, inv: dict[str, dict], facts: dict | None = None, wheelhouse: str | Path | None = None,
            python_version: str | None = None, pyinstaller_version: str | None = None) -> Analysis:
    facts = facts or {}
    A = Analysis()
    owners = module_owner_map(inv)
    owners_extra = {t: k for k, v in inv.items() for t in v.get("top_level", [])}
    owners.update(owners_extra)

    def comp(c: Comp) -> Comp:
        return A.comps.setdefault(c.key, c)

    def pypi(dist_norm: str) -> Comp | None:
        e = inv.get(dist_norm)
        if not e:
            return None
        key = f"pypi:{dist_norm}"
        if key in A.comps:
            return A.comps[key]
        first = dist_norm in FIRST_PARTY
        c = Comp(key=key, name=e["name"], version=e["version"], purl=f"pkg:pypi/{dist_norm}@{e['version']}",
                 licence=declared_licence(e))
        c.evidence.append(f"distribution metadata ({e['source']})")
        if e.get("wheel_sha256"):
            c.hashes["SHA-256"] = e["wheel_sha256"]
            c.props["redevops:hash-of"] = f"wheel {e.get('wheel_file')} (the installed distribution), not of the bytes inside the archive"
        c.props["redevops:top-level-modules"] = ",".join(e.get("top_level", []))
        txt = (e.get("licence") or {}).get("license")
        if txt and (not c.licence or c.licence.get("name") != txt):
            c.props["redevops:licence-declared-text"] = " ".join(txt.split())[:400]
        if (e.get("licence") or {}).get("license_files"):
            c.props["redevops:licence-files"] = ",".join(e["licence"]["license_files"])
        if first:
            c.props["redevops:origin"] = "first-party (git dependency built into the wheelhouse)"
        else:
            c.refs["distribution"] = f"https://pypi.org/project/{dist_norm}/{e['version']}/"
        return comp(c)

    # ---- Python runtime component -------------------------------------------------------------
    cpy = comp(Comp(key="cpython", name="CPython", version=None, kind="framework",
                    purl=None, evidence=[], licence=None))
    ver = python_version
    if archive.has_bytes:
        for n in archive.pkg:
            if n.endswith("Info.plist") and "Python.framework" in n:
                try:
                    v = plistlib.loads(archive.read(n) or b"").get("CFBundleVersion")
                    if v:
                        ver = v
                        cpy.evidence.append(f"{n}: CFBundleVersion {v}")
                except Exception:
                    pass
            m = re.fullmatch(r"python3(\d+)\.dll", n.lower())
            if m:
                fv = pe.file_version(archive.read(n) or b"")
                if fv:
                    ver = ".".join(fv.split(".")[:3])
                    cpy.evidence.append(f"{n}: VS_FIXEDFILEINFO FileVersion {fv}")
    if not ver and facts.get("python_version"):
        ver = facts["python_version"]
        cpy.evidence.append(f"facts file: {facts.get('_source', 'facts')}")
    if ver and not cpy.evidence:
        cpy.evidence.append("build manifest / toolchain pin (not read from the archive)")
    cpy.version = ver
    cpy.purl = f"pkg:generic/cpython@{ver}" if ver else None
    cpy.props["redevops:licence-note"] = "interpreter licence text is not part of the archive; no licence asserted here"
    cpy.shipped_in.add("sidecar")

    # ---- PKG entries -------------------------------------------------------------------------
    native_scans: dict[str, list[tuple[str, str]]] = {}
    ms_versions: dict[str, str | None] = {}
    for name in archive.pkg:
        if name == "PYZ.pyz":  # the container of the PYZ modules, listed separately
            continue
        rule, hint = classify_pkg(name)
        key: str | None = None
        if rule == "pyinstaller-runtime":
            key = "pyinstaller-runtime"
        elif rule == "sidecar":
            key = "sidecar"
        elif rule == "licenses":
            key = "licenses-folder"
        elif rule == "cpython":
            key = "cpython"
        elif rule in ("openssl-lib", "libffi", "ms-dll", "ms-apiset", "gcc-runtime"):
            key = f"{rule}:{hint}" if rule in ("ms-dll", "libffi", "gcc-runtime") else ("ms-apiset" if rule == "ms-apiset" else "openssl-dynamic")
        elif rule == "dist-info":
            c = pypi(hint)
            key = c.key if c else None
            if c:
                m = re.match(r"^[^/]+?-(\d[^/]*)\.dist-info/", name.replace("\\", "/"))
                if m and m.group(1) != c.version:
                    A.problems.append(f"{name}: dist-info version {m.group(1)} != inventory {c.version}")
        else:
            owner = owners.get(hint)
            if owner:
                key = pypi(owner).key  # type: ignore[union-attr]
                if hint == "agentic_os" and "projects_ui/" in name.replace("\\", "/"):
                    key = "ui-bundle"
                elif hint == "certifi" and name.endswith("cacert.pem"):
                    key = "certifi-bundle"
        if key is None:
            A.unattributed.append(f"pkg:{name}")
            continue
        A.attribution[f"pkg:{name}"] = key
        # components created lazily by rule
        A.comps.setdefault(key, Comp(key=key, name=key, version=None))
        if key.startswith("openssl"):
            comp(Comp(key="openssl-dynamic", name="OpenSSL", version=None, purl=None))
        low = name.lower()
        if archive.has_bytes and low.endswith(NATIVE_EXT + ("python", "base_library.zip")) or name in ("Python",) or "libpython" in low:
            data = archive.read(name)
            if data:
                for v, d in probe_openssl(data):
                    native_scans.setdefault(name, []).append((v, d))
                if key.startswith("ms-dll") or key == "ms-apiset":
                    ms_versions[name] = pe.file_version(data)

    # PYZ modules ------------------------------------------------------------------------------
    for mod in archive.pyz:
        top = mod.split(".")[0]
        if top in STDLIB or top.startswith("_sysconfigdata_"):
            key = "cpython"
        elif top in owners:
            key = pypi(owners[top]).key  # type: ignore[union-attr]
        else:
            A.unattributed.append(f"pyz:{mod}")
            continue
        A.attribution[f"pyz:{mod}"] = key

    # ---- special components ------------------------------------------------------------------
    pyi = inv.get("pyinstaller")
    pyiv = pyinstaller_version or (pyi or {}).get("version") or facts.get("pyinstaller_version")
    if "pyinstaller-runtime" in A.comps:
        c = A.comps["pyinstaller-runtime"]
        c.name, c.version, c.kind = "PyInstaller (bootloader + runtime modules + runtime hooks)", pyiv, "library"
        c.purl = f"pkg:pypi/pyinstaller@{pyiv}" if pyiv else None
        c.licence = declared_licence(pyi)
        c.props["redevops:role"] = "build tool whose bootloader and pyimod*/pyi_rth_* runtime are inside the executable"
        c.props["redevops:linkage"] = "the bootloader is the executable itself (statically part of redevops-sidecar)"
        if not pyiv:
            c.limitations.append("PyInstaller version not discoverable from this run: pass --inventory-env of the build venv or a build manifest")
        else:
            c.evidence.append("version from the build venv metadata / build manifest (the archive carries no PyInstaller version string)")
        hooks = inv.get("pyinstaller-hooks-contrib")
        if hooks:
            c.props["redevops:hooks-contrib-version"] = hooks["version"]
        c.shipped_in.add("sidecar:PKG")
    if "licenses-folder" in A.comps:
        c = A.comps["licenses-folder"]
        c.name, c.kind, c.version = "Licenses folder (product licence files + THIRD_PARTY_NOTICES, embedded in the sidecar)", "data", None
        c.props["redevops:role"] = "first-party licence/notice text files generated by packaging/notices; not third-party code"
        c.shipped_in.add("sidecar:PKG")
        if archive.has_bytes:
            for e in archive.pkg:
                if e.startswith("Licenses/"):
                    d = archive.read(e)
                    if d:
                        c.props[f"redevops:sha256:{e}"] = _sha(d)
    if "sidecar" in A.comps:
        c = A.comps["sidecar"]
        c.name, c.kind, c.version = "redevops-sidecar", "application", None
        if archive.has_bytes and archive.source_sha256:
            c.hashes["SHA-256"] = archive.source_sha256
            c.evidence.append("SHA-256 of the executable that was analysed")
        c.props["redevops:role"] = "the frozen Python sidecar executable; sidecar_main is its entry script"
        c.shipped_in.add("sidecar")

    # OpenSSL --------------------------------------------------------------------------------
    ssl_seen: dict[str, dict] = {}
    for entry, hits in native_scans.items():
        for v, d in hits:
            ssl_seen.setdefault(v, {"date": d, "entries": []})["entries"].append(entry)
    for f in facts.get("openssl", []):
        ssl_seen.setdefault(f["version"], {"date": f.get("date"), "entries": []})["entries"].append(f["entry"] + " (facts)")
    A.comps.pop("openssl-dynamic", None)
    for v, info in sorted(ssl_seen.items()):
        c = Comp(key=f"openssl:{v}", name="OpenSSL", version=v, purl=f"pkg:generic/openssl@{v}",
                 evidence=[f'string "OpenSSL {v} {info["date"] or ""}" found in: ' + ", ".join(info["entries"])])
        shared = [e for e in info["entries"] if re.search(r"lib(crypto|ssl)", e)]
        c.props["redevops:linkage"] = ("shared library file shipped in the archive: " + ", ".join(shared)) if shared else \
            "statically linked into: " + ", ".join(info["entries"])
        c.props["redevops:licence-note"] = "OpenSSL licence not read from the binary; no licence asserted here"
        c.shipped_in.add("sidecar:PKG")
        c.entries = list(info["entries"])
        A.comps[c.key] = c
    # OpenSSL libraries ship in pairs: a libssl whose banner is not readable (listing-only run) takes the version of the
    # libcrypto in the same directory
    for e, key in list(A.attribution.items()):
        if key == "openssl-dynamic" and e.split(":", 1)[1].lower().rsplit("/", 1)[-1].startswith("libssl"):
            d = e.split(":", 1)[1].rsplit("/", 1)[0] if "/" in e.split(":", 1)[1] else ""
            for v, i in ssl_seen.items():
                if any(x.lower().startswith((d + "/" if d else "") + "libcrypto") for x in i["entries"]):
                    i["entries"].append(e.split(":", 1)[1] + " (paired with libcrypto)")
    for e, key in list(A.attribution.items()):
        if key == "openssl-dynamic":
            # point the library file at the OpenSSL version its banner shows
            ent = e.split(":", 1)[1]
            tgt = next((f"openssl:{v}" for v, i in ssl_seen.items() if any(x == ent or x.startswith(ent) for x in i["entries"])), None)
            A.attribution[e] = tgt or "openssl:unknown"
    if any(v == "openssl:unknown" for v in A.attribution.values()):
        A.comps["openssl:unknown"] = Comp(key="openssl:unknown", name="OpenSSL", version=None,
                                          limitations=["library files present but no version banner readable (listing-only run without facts)"])

    # Microsoft DLLs ---------------------------------------------------------------------------
    # (c.entries is only filled further down, so the archive entries of a component are taken from A.attribution here: with
    # c.entries the file name fell back to the lower-case key, ms_versions[<name as archived>] never matched, and the version of
    # an upper-case file such as VCRUNTIME140.dll was lost whenever the bytes were available.)
    def archived(key: str) -> list[str]:
        return [k.split(":", 1)[1] for k, v in A.attribution.items() if v == key and k.startswith("pkg:")]

    for k, c in list(A.comps.items()):
        if k.startswith("ms-dll:"):
            base = k.split(":", 1)[1]
            fname = next((e for e in archived(k) if e.replace("\\", "/").split("/")[-1].lower() == base), base)
            fv = ms_versions.get(fname) or (facts.get("ms_dlls", {}).get(base))
            c.name, c.version, c.kind = fname.split("/")[-1], fv, "library"
            c.purl = f"pkg:generic/microsoft-{base.replace('.dll', '')}@{fv}" if fv else None
            c.evidence.append("PE VS_FIXEDFILEINFO of the shipped file" if ms_versions.get(fname) else f"facts file ({facts.get('_source', 'facts')})")
            c.props["redevops:licence-note"] = "Microsoft redistributable runtime; licence not read from the file, none asserted"
            c.props["redevops:role"] = "Microsoft C/C++ runtime shipped inside the Windows PyInstaller archive"
        if k == "ms-apiset":
            files = archived(k)
            read = sorted({ms_versions[f] for f in files if ms_versions.get(f)})
            c.name, c.version, c.kind = "Windows Universal CRT API-set forwarders (api-ms-win-*.dll)", facts.get("ms_dlls", {}).get("ucrtbase.dll"), "library"
            if not c.version and len(read) == 1:
                c.version = read[0]      # every forwarder carries the same UCRT version resource
            elif len(read) > 1:
                c.limitations.append("the api-ms-win-*.dll files carry more than one version resource: " + ", ".join(read))
            c.evidence.append(f"{len(files)} api-ms-win-*.dll files in the archive" + (f"; PE VS_FIXEDFILEINFO of the files: {', '.join(read)}" if read else ""))
            c.props["redevops:licence-note"] = "Microsoft redistributable runtime; none asserted"
            if not c.version:
                c.limitations.append("version of the api-set forwarder DLLs not discovered (they carry the Windows 10 SDK UCRT version; read the files' VS_FIXEDFILEINFO)")
    # libffi -----------------------------------------------------------------------------------
    for k, c in A.comps.items():
        if k.startswith("libffi:"):
            c.name, c.version = "libffi", None
            c.limitations.append("version not discoverable: the shipped library carries only its soname")
            c.props["redevops:licence-note"] = "none asserted"

    # GCC runtime libraries copied from the Linux build host ---------------------------------------
    for k, c in A.comps.items():
        if k.startswith("gcc-runtime:"):
            c.name, c.version, c.kind = k.split(":", 1)[1], None, "library"
            c.limitations.append("version not discoverable: PyInstaller copied the library from the build host and the file carries only its soname")
            c.props["redevops:licence-note"] = "none asserted (the GCC runtime libraries are published under GPL-3.0-or-later with the GCC Runtime Library Exception; not read from the file)"
            c.props["redevops:role"] = "GCC runtime library shipped inside the Linux PyInstaller archive"

    # certifi bundle ---------------------------------------------------------------------------
    cert = inv.get("certifi")
    if "certifi-bundle" in A.comps or any(e.endswith("certifi/cacert.pem") for e in archive.pkg):
        c = A.comps.setdefault("certifi-bundle", Comp(key="certifi-bundle", name="certifi CA bundle (cacert.pem)", version=(cert or {}).get("version")))
        c.name, c.kind = "Mozilla CA certificate bundle (certifi/cacert.pem)", "data"
        c.version = (cert or {}).get("version")
        c.purl = f"pkg:pypi/certifi@{c.version}" if c.version else None
        c.licence = declared_licence(cert)
        data = archive.read("certifi/cacert.pem") if archive.has_bytes else None
        if data:
            c.hashes["SHA-256"] = _sha(data)
            c.evidence.append(f"cacert.pem {len(data)} bytes read from the archive")
            c.props["redevops:certificates"] = str(data.count(b"-----BEGIN CERTIFICATE-----"))
        c.shipped_in.add("sidecar:PKG")

    # UI bundle --------------------------------------------------------------------------------
    ui_entries = [e for e in archive.pkg if "projects_ui/" in e.replace("\\", "/")]
    if ui_entries:
        ui = A.comps.setdefault("ui-bundle", Comp(key="ui-bundle", name="Projects UI bundle (agentic_os/projects_ui)", version=None))
        ui.kind = "data"
        ui.name = "Projects UI bundle (agentic_os/projects_ui)"
        agentic = inv.get("agentic-os")
        ui.version = (agentic or {}).get("version")
        ui.props["redevops:origin"] = "built from the separate redevops-projects repository and vendored into agentic_os; its npm lock is not in this repository"
        ui_rec = HERE.parent / "ui-bundle-source.json"
        if ui_rec.exists():
            u = json.loads(ui_rec.read_text(encoding="utf-8"))
            ui.props["redevops:origin"] += (f"; source commit {u['source_commit']} ("
                                            + ("published" if u.get("source_commit_published") else "LOCAL, NOT PUBLISHED") + f"), npm lock git blob {u['package_lock_git_blob']}")
        ui.limitations.append("only package banners/version strings in the minified JS are read; other third-party code without a banner is not enumerable")
        ui.shipped_in.add("sidecar:PKG")
        ui.entries = [f"pkg:{e}" for e in ui_entries]
        for e in ui_entries:
            A.attribution[f"pkg:{e}"] = "ui-bundle"
        js_hits: list[dict] = []
        for e in ui_entries:
            data = archive.read(e) if archive.has_bytes else None
            if data is None:
                continue
            ui.props[f"redevops:sha256:{e.replace(chr(92), '/').split('projects_ui/')[1]}"] = _sha(data)
            if e.endswith(".js"):
                js_hits += jsscan.scan(data.decode("utf-8", "replace"))
            if e.lower().endswith((".jpg", ".jpeg", ".png")):
                hero = Comp(key=f"asset:{e}", name=e.replace("\\", "/").split("/")[-1], version=None, kind="data",
                            hashes={"SHA-256": _sha(data)}, evidence=[f"{len(data)} bytes in the archive"])
                if b"c2pa" in data or b"jumb" in data:
                    hero.props["redevops:c2pa-manifest"] = "present (byte search for 'c2pa'/'jumb')"
                    hero.props["redevops:ai-generated-marker"] = "present" if b"trainedAlgorithmicMedia" in data else "not found"
                hero.limitations.append("image provenance/licence not established by this tool (see decision register)")
                hero.shipped_in.add("sidecar:PKG")
                A.comps[hero.key] = hero
                A.attribution[f"pkg:{e}"] = hero.key
        for f in facts.get("ui_js", []):
            js_hits.append({"name": f["name"], "version": f.get("version"), "licence_text": f.get("licence_text"),
                            "evidence": [f"facts file ({facts.get('_source', 'facts')})"]})
        merged: dict[str, dict] = {}
        for h in js_hits:
            m = merged.setdefault(h["name"], h)
            if m is not h:
                m["evidence"] += h["evidence"]
                m["version"] = m["version"] or h["version"]
        for h in merged.values():
            c = Comp(key=f"npm:{h['name']}", name=h["name"], version=h["version"], purl=(f"pkg:npm/{h['name']}@{h['version']}" if h["version"] else None),
                     evidence=h["evidence"], nested_of="ui-bundle")
            if h.get("licence_text"):
                c.licence = {"name": h["licence_text"], "source": "licence banner text in the bundle"}
            if not h["version"]:
                c.limitations.append("no version string in the bundle; version not asserted")
            c.shipped_in.add("sidecar:PKG")
            A.comps[c.key] = c

    # wheel-embedded upstream SBOMs (cryptography Rust crates + vendored OpenSSL, pydantic_core crates)
    if wheelhouse:
        for dist, key in (("cryptography", "pypi:cryptography"), ("pydantic-core", "pypi:pydantic-core")):
            e = inv.get(dist)
            if key not in A.comps or not e or not e.get("wheel_file"):
                continue
            wf = Path(wheelhouse) / e["wheel_file"]
            if not wf.exists():
                continue
            with zipfile.ZipFile(wf) as z:
                for n in z.namelist():
                    if "/sboms/" not in n or not n.endswith(".json"):
                        continue
                    try:
                        d = json.loads(z.read(n))
                    except ValueError:
                        continue
                    for x in d.get("components", []):
                        nm, vr = x.get("name"), x.get("version")
                        if not nm:
                            continue
                        if x.get("purl", "").startswith("pkg:cargo") and "file://" in x.get("purl", "") and nm.startswith("cryptography"):
                            continue  # the crate is the wheel's own code
                        ck = f"embedded:{dist}:{nm}@{vr}"
                        if ck in A.comps:
                            continue
                        lic = (x.get("licenses") or [{}])[0]
                        c = Comp(key=ck, name=nm, version=vr, purl=(x.get("purl") or "").split("?")[0] or None, nested_of=key,
                                 evidence=[f"declared by the SBOM embedded in the {e['wheel_file']} wheel ({n}); NOT read from the compiled binary"])
                        if "expression" in lic:
                            c.licence = {"expression": lic["expression"], "source": "upstream embedded SBOM"}
                        elif lic.get("license"):
                            c.licence = {"name": lic["license"].get("id") or lic["license"].get("name"), "source": "upstream embedded SBOM"}
                        for h in x.get("hashes", []):
                            c.hashes[h["alg"]] = h["content"]
                        c.props["redevops:linkage"] = f"statically linked into the {dist} native extension (per upstream SBOM)"
                        c.shipped_in.add("sidecar:PKG")
                        A.comps[ck] = c

    # fill entries lists + shipped_in (after every component has been created)
    for c in A.comps.values():
        if not c.key.startswith("openssl:"):
            c.entries = []
    for k, key in A.attribution.items():
        kind, entry = k.split(":", 1)
        c = A.comps.get(key)
        if c is None:
            c = comp(Comp(key=key, name=key, version=None))
        if k not in c.entries:
            c.entries.append(k)
        c.shipped_in.add("sidecar:PYZ" if kind == "pyz" else "sidecar:PKG")
    for c in A.comps.values():
        if c.nested_of == "ui-bundle" and not c.entries:
            c.entries = [e for e in A.comps["ui-bundle"].entries if e.endswith(".js")]

    # explicit limitations ---------------------------------------------------------------------
    L = A.limitations
    present = {k.split(":", 1)[1] for k in A.comps if k.startswith("pypi:")}
    if "duckdb" in present:
        L["L-DUCKDB"] = ("DuckDB (_duckdb extension) statically embeds third-party libraries; they cannot be enumerated from the shipped "
                         "binary and the wheel carries no component SBOM. Only the duckdb wheel and its licence file are listed.")
    if "pydantic-core" in present:
        L["L-PYDANTIC-CORE"] = ("pydantic_core statically links Rust crates; the list here is the SBOM embedded in the wheel (upstream-declared) "
                                "when the wheelhouse was supplied, not a reading of the compiled binary.")
    if "pyyaml" in present:
        L["L-LIBYAML"] = "The PyYAML _yaml extension statically links libyaml; its version and licence text are not discoverable from the shipped binary."
    if "cffi" in present:
        L["L-LIBFFI"] = "_cffi_backend (and the CPython _ctypes module) link libffi; version not discoverable from the shipped binaries."
    L["L-CPYTHON-EXT"] = ("CPython extension modules embed or link zlib, expat, libmpdec, bz2, xz/lzma, sqlite and others depending on the interpreter build; "
                          "the interpreter's own bundled-software list is not part of the archive and is not enumerated.")
    L["L-OS"] = "Operating-system libraries and frameworks (libSystem, libedit, WebKit, WebView2, ...) are loaded from the OS, are not shipped, and are not listed."
    L["L-LICENCE"] = "Licences are DECLARED by package metadata or banners and are not verified against the shipped files; no licence is asserted as cleared."
    L["L-HASH"] = "Python components are hashed as the installed wheel; per-entry hashes of the archive contents are in the attribution file."
    if not archive.has_bytes:
        L["L-LISTING-ONLY"] = "This run had only an archive LISTING: native versions, UI banners and file hashes come from the facts file, not from bytes."
    return A
