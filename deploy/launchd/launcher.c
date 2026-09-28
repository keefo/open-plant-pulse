/* The app bundle's main executable: starts the hub and waits for it.
 *
 * macOS attributes Bluetooth and local network permission to the app a process
 * belongs to. Local network privacy also needs that app's main executable to be
 * a real binary: with a shell script there, a launchd agent is refused every LAN
 * connection with "No route to host", and no prompt ever appears. So this binary
 * is the app, and it runs the rendered hub.sh in Contents/Resources as its child,
 * which the permissions then cover.
 *
 * It forwards the signals launchd stops a job with, and exits as the hub did.
 */
#include <errno.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;
static pid_t child = 0;

static void forward(int signal_number) {
    if (child > 0) kill(child, signal_number);
}

int main(void) {
    char executable[PATH_MAX];
    uint32_t size = sizeof(executable);
    if (_NSGetExecutablePath(executable, &size) != 0) {
        fprintf(stderr, "launcher: executable path too long\n");
        return 1;
    }
    char resolved[PATH_MAX];
    if (realpath(executable, resolved) == NULL) {
        perror("launcher: realpath");
        return 1;
    }
    /* .../Contents/MacOS/hub -> .../Contents/Resources/hub.sh */
    char script[PATH_MAX];
    snprintf(script, sizeof(script), "%s/../Resources/hub.sh", dirname(resolved));

    signal(SIGTERM, forward);
    signal(SIGINT, forward);
    signal(SIGHUP, forward);

    char *arguments[] = {"/bin/sh", script, NULL};
    int error = posix_spawn(&child, "/bin/sh", NULL, NULL, arguments, environ);
    if (error != 0) {
        fprintf(stderr, "launcher: cannot start %s: %s\n", script, strerror(error));
        return 1;
    }
    int status;
    while (waitpid(child, &status, 0) < 0) {
        if (errno != EINTR) {
            perror("launcher: waitpid");
            return 1;
        }
    }
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    return 128 + WTERMSIG(status);
}
