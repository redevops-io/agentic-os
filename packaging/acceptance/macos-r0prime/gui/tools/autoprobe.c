/* Would an Apple Event (osascript "quit", System Events keystrokes) to <bundle-id> need consent? Calls
 * AEDeterminePermissionToAutomateTarget with askUserIfNeeded=false, which NEVER shows a prompt.
 * Result: 0 = already permitted, -1744 = would require user consent (blocked for us), -1743 = denied, -600 = target not running. */
#include <CoreServices/CoreServices.h>
#include <ApplicationServices/ApplicationServices.h>
#include <stdio.h>
#include <string.h>
int main(int argc, char **argv) {
  const char *bid = argc > 1 ? argv[1] : "com.apple.systemevents";
  AEAddressDesc addr;
  OSStatus e = AECreateDesc(typeApplicationBundleID, bid, strlen(bid), &addr);
  if (e) { printf("AECreateDesc=%d\n", (int)e); return 1; }
  OSStatus r = AEDeterminePermissionToAutomateTarget(&addr, typeWildCard, typeWildCard, false);
  printf("AEDeterminePermissionToAutomateTarget(%s, ask=false)=%d\n", bid, (int)r);
  AEDisposeDesc(&addr);
  return 0;
}
