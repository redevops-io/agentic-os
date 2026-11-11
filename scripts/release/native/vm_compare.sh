#!/bin/bash
# vm_compare.sh A B : compare the artifacts of two collected builds (~/art/A and ~/art/B) INSIDE the VM, writing ~/art/cmp-A-B/ : whole-file hashes, the .deb extracted data and control trees,
# the AppImage extracted squashfs trees (content hashes, modes, ownership and, separately, mtimes), the byte-level diff ranges of the containers, and the ar/tar header fields.
set -uo pipefail
A="$1"; B="$2"; D="$HOME/art/cmp-$A-$B"; rm -rf "$D"; mkdir -p "$D/t"; cd "$D/t"
{
echo "== whole-file sha256, $A vs $B"
for f in $(cd ~/art/$A && ls); do [ -f ~/art/$A/$f ] && [ -f ~/art/$B/$f ] && printf '%-60s %s\n' "$f" "$(cmp -s ~/art/$A/$f ~/art/$B/$f && echo IDENTICAL || echo DIFFERENT)"; done
for n in $A $B; do
  deb=$(ls ~/art/$n/*.deb); img=$(ls ~/art/$n/*.AppImage)
  mkdir -p deb-$n debctl-$n ai-$n
  dpkg-deb -x "$deb" deb-$n; dpkg-deb -e "$deb" debctl-$n
  ( cd ai-$n && cp "$img" ./a.AppImage && chmod +x a.AppImage && ./a.AppImage --appimage-extract >/dev/null 2>&1; rm -f a.AppImage )
  ar t "$deb" | tr '\n' ' '; echo "(ar members of $n)"
  ar tv "$deb" | sed 's/^/  /'
done
echo "== deb data tree content+mode+owner (names, sha256, mode, uid, gid), $A vs $B"
for n in $A $B; do ( cd deb-$n && find . -type f -o -type l | LC_ALL=C sort | while IFS= read -r f; do printf '%s %s %s %s %s\n' "$f" "$(sha256sum "$f" | cut -c1-64)" "$(stat -c '%a' "$f")" "$(stat -c '%u' "$f")" "$(stat -c '%g' "$f")"; done ) > deb-content-$n.txt; done
diff deb-content-$A.txt deb-content-$B.txt >/dev/null && echo "DEB_CONTENT_IDENTICAL" || { echo "DEB_CONTENT_DIFFERS"; diff deb-content-$A.txt deb-content-$B.txt | head -20; }
echo "== deb data tree mtimes, $A vs $B"
for n in $A $B; do ( cd deb-$n && find . | LC_ALL=C sort | while IFS= read -r f; do printf '%s %s\n' "$f" "$(stat -c '%Y' "$f")"; done ) > deb-mtime-$n.txt; done
diff deb-mtime-$A.txt deb-mtime-$B.txt >/dev/null && echo "DEB_MTIMES_IDENTICAL" || { echo "DEB_MTIMES_DIFFER ($(diff deb-mtime-$A.txt deb-mtime-$B.txt | grep -c '^<') entries)"; diff deb-mtime-$A.txt deb-mtime-$B.txt | head -6; }
diff -r debctl-$A debctl-$B >/dev/null && echo "DEB_CONTROL_IDENTICAL" || { echo "DEB_CONTROL_DIFFERS"; diff -r debctl-$A debctl-$B | head -10; }
echo "== AppImage extracted tree content+mode (squashfs-root), $A vs $B"
for n in $A $B; do ( cd ai-$n/squashfs-root && find . -type f -o -type l | LC_ALL=C sort | while IFS= read -r f; do printf '%s %s %s\n' "$f" "$( [ -L "$f" ] && readlink "$f" | sha256sum | cut -c1-64 || sha256sum "$f" | cut -c1-64)" "$(stat -c '%a' "$f")"; done ) > ai-content-$n.txt; done
diff ai-content-$A.txt ai-content-$B.txt >/dev/null && echo "APPIMAGE_CONTENT_IDENTICAL ($(wc -l < ai-content-$A.txt) entries)" || { echo "APPIMAGE_CONTENT_DIFFERS"; diff ai-content-$A.txt ai-content-$B.txt | head -20; }
echo "== AppImage extracted tree mtimes, $A vs $B"
for n in $A $B; do ( cd ai-$n/squashfs-root && find . | LC_ALL=C sort | while IFS= read -r f; do printf '%s %s\n' "$f" "$(stat -c '%Y' "$f")"; done ) > ai-mtime-$n.txt; done
diff ai-mtime-$A.txt ai-mtime-$B.txt >/dev/null && echo "APPIMAGE_MTIMES_IDENTICAL" || echo "APPIMAGE_MTIMES_DIFFER ($(diff ai-mtime-$A.txt ai-mtime-$B.txt | grep -c '^<') entries)"
} > "$D/compare.txt" 2>&1
cd "$D" && rm -rf t/deb-* t/ai-* t/debctl-* && mv t/*.txt . 2>/dev/null; rmdir t 2>/dev/null; cat compare.txt | head -80
