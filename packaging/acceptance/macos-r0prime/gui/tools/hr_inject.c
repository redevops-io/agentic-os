/* Test dylib: when loaded, writes a marker file named by $HR_MARKER (proves code ran inside the host process). */
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
__attribute__((constructor)) static void hr_init(void) {
  const char *p = getenv("HR_MARKER");
  if (!p) return;
  FILE *f = fopen(p, "a"); if (f) { fprintf(f, "loaded in pid %d\n", (int)getpid()); fclose(f); }
}
