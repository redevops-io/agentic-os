/* List on-screen windows via CGWindowListCopyWindowInfo (no prompt; kCGWindowName is only filled when the
 * caller has Screen Recording permission or owns the window). Usage: winlist [owner-substring] */
#include <ApplicationServices/ApplicationServices.h>
#include <stdio.h>
#include <string.h>
static void cs(CFStringRef s, char *buf, size_t n) { buf[0] = 0; if (s) CFStringGetCString(s, buf, n, kCFStringEncodingUTF8); }
int main(int argc, char **argv) {
  CFArrayRef a = CGWindowListCopyWindowInfo(kCGWindowListOptionAll, kCGNullWindowID);
  if (!a) { puts("no list"); return 1; }
  CFIndex n = CFArrayGetCount(a); int shown = 0;
  for (CFIndex i = 0; i < n; i++) {
    CFDictionaryRef d = CFArrayGetValueAtIndex(a, i);
    char owner[256], name[256];
    cs(CFDictionaryGetValue(d, kCGWindowOwnerName), owner, sizeof owner);
    cs(CFDictionaryGetValue(d, kCGWindowName), name, sizeof name);
    if (argc > 1 && !strstr(owner, argv[1])) continue;
    int pid = 0, layer = 0, onscreen = 0; double alpha = 0;
    CFNumberRef p = CFDictionaryGetValue(d, kCGWindowOwnerPID); if (p) CFNumberGetValue(p, kCFNumberIntType, &pid);
    CFNumberRef l = CFDictionaryGetValue(d, kCGWindowLayer); if (l) CFNumberGetValue(l, kCFNumberIntType, &layer);
    CFNumberRef al = CFDictionaryGetValue(d, kCGWindowAlpha); if (al) CFNumberGetValue(al, kCFNumberDoubleType, &alpha);
    CFBooleanRef o = CFDictionaryGetValue(d, kCGWindowIsOnscreen); onscreen = o && CFBooleanGetValue(o);
    CGRect r = {0}; CFDictionaryRef b = CFDictionaryGetValue(d, kCGWindowBounds); if (b) CGRectMakeWithDictionaryRepresentation(b, &r);
    printf("pid=%d owner=\"%s\" name=\"%s\" layer=%d onscreen=%d alpha=%.1f bounds=%.0f,%.0f %.0fx%.0f\n", pid, owner, name, layer, onscreen, alpha, r.origin.x, r.origin.y, r.size.width, r.size.height);
    shown++;
  }
  printf("total=%ld shown=%d\n", (long)n, shown);
  CFRelease(a); return 0;
}
