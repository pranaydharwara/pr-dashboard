#import <Cocoa/Cocoa.h>
#import <ApplicationServices/ApplicationServices.h>
#import <UserNotifications/UserNotifications.h>
#include <signal.h>
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
#ifndef PRD_CURSOR_BUNDLE
#define PRD_CURSOR_BUNDLE "com.todesktop.230313mzl4w4u92"
#endif

static const CGKeyCode PRDKeyK = 0x28;
static const CGKeyCode PRDKeyReturn = 0x24;

static pid_t serverPid = 0;
static NSString *const PRDNotificationName = @"com.prdashboard.notify";

static void stopServer(int signalNumber) {
    if (serverPid > 0) {
        kill(serverPid, signalNumber);
    }
    _exit(0);
}

#pragma mark - Keystroke synthesis

// Sending keystrokes requires Accessibility. Doing it here, inside the bundle
// the user actually grants, keeps macOS from attributing the request to the
// Python interpreter or to osascript further down the process tree.
static BOOL prdHasAccessibility(BOOL prompt) {
    NSDictionary *options = @{(__bridge id)kAXTrustedCheckOptionPrompt: @(prompt)};
    return AXIsProcessTrustedWithOptions((__bridge CFDictionaryRef)options);
}

// A private source is essential: kCGEventSourceStateHIDSystemState makes
// synthesized events inherit the real keyboard's modifier state, so the
// Command flag from the Cmd+K that opens the palette bleeds into the
// characters typed next and each one fires as a shortcut instead of text.
static CGEventSourceRef prdCreateEventSource(void) {
    return CGEventSourceCreate(kCGEventSourceStatePrivate);
}

static void prdPostKeyWithSource(CGEventSourceRef source,
                                 CGKeyCode key,
                                 CGEventFlags flags) {
    CGEventRef down = CGEventCreateKeyboardEvent(source, key, true);
    CGEventRef up = CGEventCreateKeyboardEvent(source, key, false);
    // Set unconditionally, including to 0, so no modifier survives from a
    // previous event.
    CGEventSetFlags(down, flags);
    CGEventSetFlags(up, flags);
    CGEventPost(kCGHIDEventTap, down);
    usleep(20000);
    CGEventPost(kCGHIDEventTap, up);
    if (down) CFRelease(down);
    if (up) CFRelease(up);
}

static void prdTypeStringWithSource(CGEventSourceRef source, NSString *text) {
    if (text.length == 0) {
        return;
    }
    enum { chunkSize = 16 };
    NSUInteger index = 0;
    while (index < text.length) {
        NSUInteger count = MIN((NSUInteger)chunkSize, text.length - index);
        // Splitting a surrogate pair would emit two replacement characters.
        if (count > 1) {
            unichar last = [text characterAtIndex:index + count - 1];
            if (CFStringIsSurrogateHighCharacter(last)) {
                count--;
            }
        }
        unichar buffer[chunkSize];
        [text getCharacters:buffer range:NSMakeRange(index, count)];

        CGEventRef down = CGEventCreateKeyboardEvent(source, 0, true);
        CGEventSetFlags(down, 0);
        CGEventKeyboardSetUnicodeString(down, count, buffer);
        CGEventPost(kCGHIDEventTap, down);
        if (down) CFRelease(down);

        CGEventRef up = CGEventCreateKeyboardEvent(source, 0, false);
        CGEventSetFlags(up, 0);
        CGEventKeyboardSetUnicodeString(up, count, buffer);
        CGEventPost(kCGHIDEventTap, up);
        if (up) CFRelease(up);

        index += count;
        usleep(12000);
    }
}

// NSWorkspace's frontmostApplication is refreshed by notifications, so in a
// short-lived CLI process with no run loop it can report a stale value
// forever. The Accessibility API answers directly, and the run loop is pumped
// so the NSWorkspace fallback has a chance to update too.
static pid_t prdFocusedAppPid(void) {
    AXUIElementRef system = AXUIElementCreateSystemWide();
    if (!system) {
        return 0;
    }
    CFTypeRef focused = NULL;
    pid_t pid = 0;
    if (AXUIElementCopyAttributeValue(system, kAXFocusedApplicationAttribute,
                                      &focused) == kAXErrorSuccess && focused) {
        AXUIElementGetPid((AXUIElementRef)focused, &pid);
        CFRelease(focused);
    }
    CFRelease(system);
    return pid;
}

static BOOL prdIsFrontmost(NSRunningApplication *app) {
    if (app == nil) {
        return NO;
    }
    pid_t focused = prdFocusedAppPid();
    if (focused != 0 && focused == app.processIdentifier) {
        return YES;
    }
    NSRunningApplication *front =
        [[NSWorkspace sharedWorkspace] frontmostApplication];
    return front != nil &&
           front.processIdentifier == app.processIdentifier;
}

static BOOL prdWaitUntilFrontmost(NSRunningApplication *app,
                                  double timeoutSeconds) {
    NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:timeoutSeconds];
    while ([deadline timeIntervalSinceNow] > 0) {
        if (prdIsFrontmost(app)) {
            return YES;
        }
        [[NSRunLoop currentRunLoop]
            runMode:NSDefaultRunLoopMode
         beforeDate:[NSDate dateWithTimeIntervalSinceNow:0.05]];
    }
    return prdIsFrontmost(app);
}

static BOOL prdActivateCursor(void) {
    NSString *bundleId = @PRD_CURSOR_BUNDLE;
    NSArray<NSRunningApplication *> *running =
        [NSRunningApplication runningApplicationsWithBundleIdentifier:bundleId];
    if (running.count > 0) {
        NSRunningApplication *cursor = running.firstObject;
        [cursor activateWithOptions:NSApplicationActivateAllWindows];
        // Confirmation is best-effort. Cursor is running, so a focus check
        // that never confirms is far more likely to be a limitation of this
        // process than a genuinely failed activation, and reporting failure
        // here would block a flow that otherwise works.
        prdWaitUntilFrontmost(cursor, 3.0);
        return YES;
    }

    NSURL *appURL = [[NSWorkspace sharedWorkspace]
        URLForApplicationWithBundleIdentifier:bundleId];
    if (appURL == nil) {
        return NO;
    }
    NSWorkspaceOpenConfiguration *config = [NSWorkspaceOpenConfiguration configuration];
    config.activates = YES;
    dispatch_semaphore_t done = dispatch_semaphore_create(0);
    __block NSRunningApplication *launched = nil;
    [[NSWorkspace sharedWorkspace]
        openApplicationAtURL:appURL
               configuration:config
           completionHandler:^(NSRunningApplication *app, __unused NSError *error) {
               launched = app;
               dispatch_semaphore_signal(done);
           }];
    dispatch_semaphore_wait(done,
        dispatch_time(DISPATCH_TIME_NOW, (int64_t)(10 * NSEC_PER_SEC)));
    if (launched == nil) {
        return NO;
    }
    // A cold launch needs longer than an activate before it takes key focus.
    prdWaitUntilFrontmost(launched, 15.0);
    return YES;
}

enum {
    PRDOpenChatOK = 0,
    PRDOpenChatNotTrusted = 3,
    PRDOpenChatNoCursor = 4,
};

// Cursor has no documented deep link for a specific chat, so the chat is
// reopened by driving its Cmd+K conversation search.
static int prdOpenCursorChat(NSString *title) {
    if (!prdHasAccessibility(YES)) {
        return PRDOpenChatNotTrusted;
    }
    if (!prdActivateCursor()) {
        return PRDOpenChatNoCursor;
    }
    CGEventSourceRef source = prdCreateEventSource();

    // Let the window finish coming forward before the palette shortcut.
    usleep(500000);
    prdPostKeyWithSource(source, PRDKeyK, kCGEventFlagMaskCommand);

    // The palette animates in and steals focus; typing too early drops the
    // opening characters and leaves the search box with a partial title.
    usleep(700000);
    prdTypeStringWithSource(source, title);

    // Give the result list time to filter before committing to the top hit.
    usleep(600000);
    prdPostKeyWithSource(source, PRDKeyReturn, 0);

    if (source) CFRelease(source);
    return PRDOpenChatOK;
}

#pragma mark - App delegate

@interface PRDAppDelegate : NSObject
    <NSApplicationDelegate, UNUserNotificationCenterDelegate>
@property(nonatomic, strong) NSTask *serverTask;
@end

@implementation PRDAppDelegate

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    UNUserNotificationCenter *center = [UNUserNotificationCenter currentNotificationCenter];
    center.delegate = self;
    [center requestAuthorizationWithOptions:(UNAuthorizationOptionAlert | UNAuthorizationOptionSound)
                          completionHandler:^(__unused BOOL granted, __unused NSError *error) {}];

    // Asking once at startup is what registers this bundle in the Accessibility
    // list, so the user has something to enable before the first chat click.
    prdHasAccessibility(YES);

    [[NSDistributedNotificationCenter defaultCenter]
        addObserver:self
           selector:@selector(deliverDashboardNotification:)
               name:PRDNotificationName
             object:nil];

    self.serverTask = [[NSTask alloc] init];
    self.serverTask.executableURL = [NSURL fileURLWithPath:@PRD_PYTHON];
    self.serverTask.arguments = @[[NSString stringWithFormat:@"%s/server.py", PRD_DIR]];
    self.serverTask.currentDirectoryURL = [NSURL fileURLWithPath:@PRD_DIR];
    __weak PRDAppDelegate *weakSelf = self;
    self.serverTask.terminationHandler = ^(__unused NSTask *task) {
        dispatch_async(dispatch_get_main_queue(), ^{
            if (weakSelf) {
                [NSApp terminate:nil];
            }
        });
    };

    NSError *error = nil;
    if (![self.serverTask launchAndReturnError:&error]) {
        NSLog(@"Could not launch PR Dashboard server: %@", error);
        [NSApp terminate:nil];
        return;
    }
    serverPid = self.serverTask.processIdentifier;
}

- (void)applicationWillTerminate:(NSNotification *)notification {
    [[NSDistributedNotificationCenter defaultCenter] removeObserver:self];
    if (self.serverTask.running) {
        [self.serverTask terminate];
    }
}

- (BOOL)applicationShouldHandleReopen:(NSApplication *)sender
                    hasVisibleWindows:(BOOL)flag {
    [[NSWorkspace sharedWorkspace] openURL:[NSURL URLWithString:@PRD_URL]];
    return NO;
}

- (void)deliverDashboardNotification:(NSNotification *)notification {
    NSDictionary *info = notification.userInfo ?: @{};
    UNMutableNotificationContent *content = [[UNMutableNotificationContent alloc] init];
    content.title = info[@"title"] ?: @"PR Dashboard";
    content.body = info[@"body"] ?: @"";
    content.sound = [UNNotificationSound defaultSound];
    content.userInfo = @{@"url": info[@"url"] ?: @PRD_URL};

    NSString *identifier = info[@"id"] ?: [[NSUUID UUID] UUIDString];
    UNNotificationRequest *request =
        [UNNotificationRequest requestWithIdentifier:identifier content:content trigger:nil];
    [[UNUserNotificationCenter currentNotificationCenter]
        addNotificationRequest:request
         withCompletionHandler:^(NSError *error) {
             if (error) {
                 NSLog(@"Could not deliver notification: %@", error);
             }
         }];
}

- (void)userNotificationCenter:(UNUserNotificationCenter *)center
       willPresentNotification:(UNNotification *)notification
         withCompletionHandler:(void (^)(UNNotificationPresentationOptions))completionHandler {
    completionHandler(UNNotificationPresentationOptionBanner |
                      UNNotificationPresentationOptionList |
                      UNNotificationPresentationOptionSound);
}

- (void)userNotificationCenter:(UNUserNotificationCenter *)center
didReceiveNotificationResponse:(UNNotificationResponse *)response
         withCompletionHandler:(void (^)(void))completionHandler {
    NSString *urlString = response.notification.request.content.userInfo[@"url"];
    if (urlString.length > 0) {
        [[NSWorkspace sharedWorkspace] openURL:[NSURL URLWithString:urlString]];
    }
    completionHandler();
}

@end

#pragma mark - CLI modes

static int postNotification(int argc, const char *argv[]) {
    if (argc < 6) {
        fprintf(stderr, "usage: PR Dashboard --notify TITLE BODY URL ID\n");
        return 2;
    }
    NSDictionary *info = @{
        @"title": [NSString stringWithUTF8String:argv[2]],
        @"body": [NSString stringWithUTF8String:argv[3]],
        @"url": [NSString stringWithUTF8String:argv[4]],
        @"id": [NSString stringWithUTF8String:argv[5]],
    };
    [[NSDistributedNotificationCenter defaultCenter]
        postNotificationName:PRDNotificationName
                      object:nil
                    userInfo:info
          deliverImmediately:YES];
    return 0;
}

// Typed from this process rather than handed to the --serve instance. Both are
// the same signed bundle, so macOS resolves the Accessibility grant to
// PR Dashboard either way, and doing it here reports real success or failure
// back to the caller instead of firing into a notification and hoping.
static int runOpenChat(int argc, const char *argv[]) {
    if (argc < 3) {
        fprintf(stderr, "usage: PR Dashboard --open-chat TITLE\n");
        return 2;
    }
    NSString *title = [NSString stringWithUTF8String:argv[2]];
    int status = prdOpenCursorChat(title);
    if (status == PRDOpenChatNotTrusted) {
        fprintf(stderr, "Accessibility permission missing\n");
    } else if (status == PRDOpenChatNoCursor) {
        fprintf(stderr, "Cursor could not be activated\n");
    }
    return status;
}

// Types into whatever is frontmost after a countdown. Separates "keystroke
// synthesis is broken" from "Cursor's palette did not accept the input",
// which otherwise look identical from the dashboard.
static int runTypeTest(int argc, const char *argv[]) {
    if (argc < 3) {
        fprintf(stderr, "usage: PR Dashboard --type-test TEXT\n");
        return 2;
    }
    if (!prdHasAccessibility(YES)) {
        fprintf(stderr, "Accessibility permission missing\n");
        return PRDOpenChatNotTrusted;
    }
    NSString *text = [NSString stringWithUTF8String:argv[2]];
    for (int i = 5; i > 0; i--) {
        fprintf(stderr, "typing in %d... (focus a text field)\n", i);
        sleep(1);
    }
    CGEventSourceRef source = prdCreateEventSource();
    prdTypeStringWithSource(source, text);
    if (source) CFRelease(source);
    fprintf(stderr, "done\n");
    return 0;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc > 1 && strcmp(argv[1], "--notify") == 0) {
            return postNotification(argc, argv);
        }
        if (argc > 1 && strcmp(argv[1], "--type-test") == 0) {
            return runTypeTest(argc, argv);
        }
        if (argc > 1 && strcmp(argv[1], "--cursor-status") == 0) {
            NSString *bundleId = @PRD_CURSOR_BUNDLE;
            NSArray<NSRunningApplication *> *running =
                [NSRunningApplication runningApplicationsWithBundleIdentifier:bundleId];
            printf("bundle id       : %s\n", bundleId.UTF8String);
            printf("running copies  : %lu\n", (unsigned long)running.count);
            if (running.count == 0) {
                printf("resolved path   : %s\n",
                       [[NSWorkspace sharedWorkspace]
                           URLForApplicationWithBundleIdentifier:bundleId]
                               .path.UTF8String ?: "(not installed)");
                return 1;
            }
            NSRunningApplication *cursor = running.firstObject;
            printf("cursor pid      : %d\n", cursor.processIdentifier);
            printf("ax focused pid  : %d\n", prdFocusedAppPid());
            printf("accessibility   : %s\n",
                   prdHasAccessibility(NO) ? "trusted" : "untrusted");
            [cursor activateWithOptions:NSApplicationActivateAllWindows];
            BOOL front = prdWaitUntilFrontmost(cursor, 3.0);
            printf("became frontmost: %s\n", front ? "yes" : "no (proceeding anyway)");
            return 0;
        }
        if (argc > 1 && strcmp(argv[1], "--open-chat") == 0) {
            return runOpenChat(argc, argv);
        }
        if (argc > 1 && strcmp(argv[1], "--ax-status") == 0) {
            BOOL trusted = prdHasAccessibility(NO);
            printf("%s\n", trusted ? "trusted" : "untrusted");
            return trusted ? 0 : 1;
        }
        if (argc > 1 && strcmp(argv[1], "--serve") == 0) {
            signal(SIGTERM, stopServer);
            signal(SIGINT, stopServer);
            NSApplication *app = [NSApplication sharedApplication];
            app.activationPolicy = NSApplicationActivationPolicyAccessory;
            PRDAppDelegate *delegate = [[PRDAppDelegate alloc] init];
            app.delegate = delegate;
            [app run];
            return 0;
        }
        [[NSWorkspace sharedWorkspace] openURL:[NSURL URLWithString:@PRD_URL]];
        return 0;
    }
}
