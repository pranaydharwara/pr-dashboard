#import <Cocoa/Cocoa.h>
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

static pid_t serverPid = 0;
static NSString *const PRDNotificationName = @"com.prdashboard.notify";

static void stopServer(int signalNumber) {
    if (serverPid > 0) {
        kill(serverPid, signalNumber);
    }
    _exit(0);
}

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

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc > 1 && strcmp(argv[1], "--notify") == 0) {
            return postNotification(argc, argv);
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
