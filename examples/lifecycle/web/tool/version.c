/* A tiny program built from source by the host build (cc), run by a check. */
#include <stdio.h>

static const char *VERSION = "1";

int main(void) {
    printf("web-version %s\n", VERSION);
    return 0;
}
