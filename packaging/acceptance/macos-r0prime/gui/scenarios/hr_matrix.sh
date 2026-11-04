#!/bin/bash
# Hardened-runtime / entitlement matrix on COPIES in $GUI_WORK/hr (ad-hoc `codesign -s -`, NO identity, NO keychain).
# Needs: source lib/gui-env.sh ; PYTHONPATH=$GUI_DIR/scenarios ; ENT=<path to packaging/macos/sidecar.entitlements.plist>
set -u
HR="$GUI_WORK/hr"; T="$HR/tools"; mkdir -p "$HR"/{A-unsigned,B-hardened-noent,C-hardened-ent,app-signed} "$T"
for v in A-unsigned B-hardened-noent C-hardened-ent; do cp "$APP/Contents/MacOS/redevops-sidecar" "$HR/$v/redevops-sidecar"; done
codesign --force -s - --options runtime --timestamp=none "$HR/B-hardened-noent/redevops-sidecar" 2>/dev/null
codesign --force -s - --options runtime --timestamp=none --entitlements "$ENT" "$HR/C-hardened-ent/redevops-sidecar" 2>/dev/null
clang -dynamiclib -o "$T/hr_inject.dylib" "$GUI_DIR/tools/hr_inject.c"; clang -o "$T/hr_loader" "$GUI_DIR/tools/hr_loader.c"
echo "## 1. serve under each signing variant (flags from codesign -dv)"
for v in A-unsigned B-hardened-noent C-hardened-ent; do codesign -dv "$HR/$v/redevops-sidecar" 2>&1 | grep -E "flags" | sed "s/^/$v: /"; python3 "$GUI_DIR/scenarios/hr_run.py" "$HR/$v/redevops-sidecar" "$v" | cut -c1-700; done
echo "## 2. DYLD_INSERT_LIBRARIES (marker file = injected code ran)"
for v in A-unsigned C-hardened-ent; do rm -f "$HR/marker-$v"; python3 "$GUI_DIR/scenarios/hr_run.py" "$HR/$v/redevops-sidecar" "$v+DYLD_INSERT" DYLD_INSERT_LIBRARIES="$T/hr_inject.dylib" HR_MARKER="$HR/marker-$v" | cut -c1-200; echo "marker($v): $(cat "$HR/marker-$v" 2>/dev/null | head -1 || true)"; done
echo "## 3. planted extension module in the extraction dir (same user, race with extraction)"
for v in A-unsigned C-hardened-ent; do python3 "$GUI_DIR/scenarios/hr_plant.py" "$HR/$v/redevops-sidecar" "$v" "$T/hr_inject.dylib"; done
echo "## 4. toy host: which libraries does library validation accept (ad-hoc host, no Team ID)"
cd "$T"; cp hr_loader hl_plain; cp hr_loader hl_rt; cp hr_loader hl_rt_ent; cp hr_inject.dylib lib_adhoc.dylib; cp hr_inject.dylib lib_nosig.dylib
codesign --force -s - --options runtime --timestamp=none hl_rt 2>/dev/null; codesign --force -s - --options runtime --timestamp=none --entitlements "$ENT" hl_rt_ent 2>/dev/null; codesign --remove-signature lib_nosig.dylib 2>/dev/null
for h in hl_plain hl_rt hl_rt_ent; do for l in lib_adhoc.dylib lib_nosig.dylib; do printf "%-10s %-16s " $h $l; ./$h "$PWD/$l" 2>&1 | head -1 | cut -c1-40; done; done
echo "## 5. whole .app COPY signed inner-first (sidecar+entitlement, launcher, bundle; no --deep)"
A="$HR/app-signed/Agentic Apps.app"; ditto "$APP" "$A"
codesign --force -s - --options runtime --timestamp=none --entitlements "$ENT" "$A/Contents/MacOS/redevops-sidecar" 2>/dev/null
codesign --force -s - --options runtime --timestamp=none "$A/Contents/MacOS/agentic-apps" 2>/dev/null
codesign --force -s - --options runtime --timestamp=none "$A" 2>/dev/null
codesign --verify --deep --strict -vv "$A" 2>&1 | tail -2
for f in "$A" "$A/Contents/MacOS/redevops-sidecar"; do codesign -dvv --entitlements - "$f" 2>&1 | grep -E "flags|disable-library|Sealed" | sed "s/^/$(basename "$f"): /"; done
spctl --assess --type execute -vv "$A" 2>&1 | head -1 | sed "s#$A#<app>#"
echo "## 6. the UNMODIFIED candidate copy, for contrast"
codesign --verify --strict -vv "$APP" 2>&1 | head -1 | sed "s#$APP#<app>#"
