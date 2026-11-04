/* Harmless TCC probes: each *Preflight* / AXIsProcessTrusted call only REPORTS the current grant for the
 * responsible process; none of them shows a permission prompt (the *Request* variants would; they are
 * deliberately never called here). Build: clang -framework ApplicationServices -framework CoreGraphics tccprobe.c */
#include <ApplicationServices/ApplicationServices.h>
#include <stdio.h>
int main(void) {
  printf("AXIsProcessTrusted=%d\n", (int)AXIsProcessTrusted());
  printf("CGPreflightScreenCaptureAccess=%d\n", (int)CGPreflightScreenCaptureAccess());
  printf("CGPreflightPostEventAccess=%d\n", (int)CGPreflightPostEventAccess());
  printf("CGPreflightListenEventAccess=%d\n", (int)CGPreflightListenEventAccess());
  return 0;
}
