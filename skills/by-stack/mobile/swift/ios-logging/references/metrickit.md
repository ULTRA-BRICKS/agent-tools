# MetricKit Integration

MetricKit (iOS 13+, diagnostics iOS 14+, immediate delivery iOS 15+) is Apple's first-party diagnostic system that runs **out-of-process**. It catches OOM terminations, watchdog kills, and background terminations that in-process crash reporters miss entirely.

## Three Things You Must Always Mention

1. **Process `pastDiagnosticPayloads` on startup** — MetricKit only delivers new payloads once per day. If your app launches after a crash or watchdog kill, the diagnostic is sitting in `MXMetricManager.shared.pastDiagnosticPayloads`. Always process it on startup or that session's data is lost.

2. **dSYM symbolication required** — Call stacks in MetricKit payloads are unsymbolicated. You must upload dSYMs to your backend for symbolication. Raw addresses in crash reports are useless without them.

3. **Coverage is NOT 100%** — MetricKit data is only delivered for devices where the user has opted into **"Share with App Developers"** (in Settings → Privacy → Analytics). This is not the default on all devices. MetricKit is complementary to in-process crash reporters, not a replacement.

## Why MetricKit Matters

In-process crash reporters (Sentry, Crashlytics) install signal handlers inside your app's process. When the OS kills your app (OOM, watchdog timeout, thermal throttle), there is no signal — the process is terminated with SIGKILL, which cannot be caught. MetricKit runs in a separate system process, so it observes these terminations.

## Setup

```swift
import MetricKit

class AppMetrics: NSObject, MXMetricManagerSubscriber {
    static let shared = AppMetrics()

    func startReceiving() {
        MXMetricManager.shared.add(self)
        // iOS 15+: process any pending diagnostics immediately
        processDiagnostics(MXMetricManager.shared.pastDiagnosticPayloads)
    }

    func didReceive(_ payloads: [MXDiagnosticPayload]) {
        processDiagnostics(payloads)
    }

    func didReceive(_ payloads: [MXMetricPayload]) {
        processMetrics(payloads)
    }

    private func processDiagnostics(_ payloads: [MXDiagnosticPayload]) {
        for payload in payloads {
            payload.crashDiagnostics?.forEach { crash in
                forwardToBackend(type: "crash", data: crash.jsonRepresentation())
            }
            payload.hangDiagnostics?.forEach { hang in
                forwardToBackend(type: "hang", data: hang.jsonRepresentation())
            }
            payload.diskWriteExceptionDiagnostics?.forEach { diskWrite in
                forwardToBackend(type: "diskWrite", data: diskWrite.jsonRepresentation())
            }
            payload.cpuExceptionDiagnostics?.forEach { cpu in
                forwardToBackend(type: "cpuException", data: cpu.jsonRepresentation())
            }
        }
    }

    private func processMetrics(_ payloads: [MXMetricPayload]) {
        for payload in payloads {
            // Application exit metrics — shows WHY the app terminated
            if let exitMetrics = payload.applicationExitMetrics {
                let bg = exitMetrics.backgroundExitData
                let fg = exitMetrics.foregroundExitData

                // These are the invisible killers
                let oomCount = bg.cumulativeMemoryPressureExitCount
                    + fg.cumulativeMemoryPressureExitCount
                let watchdogCount = bg.cumulativeAppWatchdogExitCount

                if oomCount > 0 || watchdogCount > 0 {
                    forwardToBackend(type: "exitMetrics", data: [
                        "oom": oomCount,
                        "watchdog": watchdogCount,
                        "backgroundTaskTimeout": bg.cumulativeBackgroundTaskAssertionTimeoutExitCount
                    ])
                }
            }
        }
    }

    private func forwardToBackend(type: String, data: Any) {
        // Implement: send to your analytics backend
        // Stacks are unsymbolicated — requires server-side symbolication with dSYMs
    }
}
```

## Initialize in App Entry Point

```swift
@main
struct MyApp: App {
    init() {
        AppMetrics.shared.startReceiving()
    }

    var body: some Scene {
        WindowGroup { ContentView() }
    }
}
```

## MetricKit vs Third-Party SDKs

| Capability | MetricKit | Sentry/Crashlytics |
|---|---|---|
| OOM detection | Yes (out-of-process) | Limited (heuristic) |
| Watchdog kills | Yes | No |
| Background terminations | Yes (exit metrics) | No |
| Symbolication | Manual (needs dSYMs on server) | Automatic |
| User opt-in required | Yes ("Share with App Developers") | No |
| Coverage | Only opted-in users | All users |
| Breadcrumbs | No | Yes |
| Rich context/tags | No | Yes |

**Recommendation**: Use both. MetricKit for OOM/watchdog detection that in-process reporters miss. Third-party SDKs for rich context, breadcrumbs, and automatic symbolication.

## Key Diagnostic Types

- **MXCrashDiagnostic** — crash stacks (unsymbolicated)
- **MXHangDiagnostic** — main thread hangs >250ms
- **MXDiskWriteExceptionDiagnostic** — excessive disk I/O
- **MXCPUExceptionDiagnostic** — excessive CPU usage
- **applicationExitMetrics** — counts of each termination reason (OOM, watchdog, etc.)

## WWDC Sessions

- **"What's new in MetricKit" (WWDC 2020, 10081)** — diagnostics API
- **"Diagnose Power and Performance regressions" (WWDC 2021, 10087)** — immediate delivery in iOS 15
- **"Understand and eliminate hangs from your app" (WWDC 2021, 10258)** — hang diagnostics
