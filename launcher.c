// Launcher binary for PR Dashboard.app.
//
// This exists so the bundle executable is a real Mach-O binary. A shell script
// would run as /bin/bash, and macOS attributes the Accessibility grant to the
// process image -- meaning the grant would land on bash (ungrantable) rather
// than on this app, and the server could never send keystrokes to Cursor.
//
// Python runs as a child rather than via exec for the same reason: exec would
// replace this process image with the python binary and move the grant onto
// whatever versioned python path Homebrew currently points at.
//
//   (no arguments)  open the dashboard in the default browser
//   --serve         run the server in the foreground; used by the launchd agent

#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef PRD_PYTHON
#define PRD_PYTHON "/usr/bin/python3"
#endif
#ifndef PRD_DIR
#define PRD_DIR "."
#endif
#ifndef PRD_URL
#define PRD_URL "http://localhost:9847"
#endif

static volatile pid_t child_pid = 0;

static void forward_signal(int sig) {
    if (child_pid > 0) {
        kill(child_pid, sig);
    }
}

static int run_server(void) {
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = forward_signal;
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);

    child_pid = fork();
    if (child_pid < 0) {
        perror("pr-dashboard: fork");
        return 1;
    }

    if (child_pid == 0) {
        if (chdir(PRD_DIR) != 0) {
            perror("pr-dashboard: chdir");
            _exit(127);
        }
        execl(PRD_PYTHON, PRD_PYTHON, PRD_DIR "/server.py", (char *)NULL);
        perror("pr-dashboard: exec python3");
        _exit(127);
    }

    int status = 0;
    while (waitpid(child_pid, &status, 0) < 0) {
        if (errno != EINTR) {
            perror("pr-dashboard: waitpid");
            return 1;
        }
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}

static int open_browser(void) {
    execl("/usr/bin/open", "open", PRD_URL, (char *)NULL);
    perror("pr-dashboard: exec open");
    return 127;
}

int main(int argc, char **argv) {
    if (argc > 1 && strcmp(argv[1], "--serve") == 0) {
        return run_server();
    }
    return open_browser();
}
