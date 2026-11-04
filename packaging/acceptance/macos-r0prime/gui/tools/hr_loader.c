/* Toy host: dlopen()s the library named by argv[1]; prints the dlerror on failure. Used to show what library
 * validation does/does not do for an ad-hoc host (no Team ID). */
#include <dlfcn.h>
#include <stdio.h>
int main(int argc, char **argv) {
  void *h = dlopen(argv[1], RTLD_NOW);
  if (!h) { printf("dlopen FAILED: %s\n", dlerror()); return 1; }
  printf("dlopen OK\n"); return 0;
}
