---
name: no-native-telegram-desktop-automation
description: Use when testing, verifying, or automating ANY Telegram-related behavior in this ecosystem — tg-cli/tg-ctl sends, a Bot API integration, a hatch-approval flow, checking how a message rendered. Drive Telegram through qa-cli's headless Telegram Web automation (qa_cli/telegram.py + qa_cli/browser.py, TELEGRAM_WEB_URL, via agent-browser) — never launch, script, or otherwise automate the native Telegram Desktop app (`open -a Telegram`, AppleScript/osascript, NSWorkspace, or computer-use/accessibility automation of its window).
---

# Test Telegram via qa-cli's Web client, never the native Desktop app

## The incident this closes out

tg-cli#420: the user reported Telegram Desktop on their Mac kept auto-opening and
stealing window focus unprompted. Two independent full-repo code audits of tg-cli and
agent-tools found **zero** call sites that open/foreground the app (`open -a`, `NSWorkspace`,
`osascript … activate`, a shelled-out `tg://`) — this was never a tg-cli code bug. The
confirmed root cause: an **agent session was running tests that drove the native Telegram
Desktop app directly**, repeatedly foregrounding it on the user's real machine.

## The rule

Any Telegram testing, verification, or automation in this ecosystem goes through qa-cli's
headless Telegram **Web** driver, never the native desktop client:

- `qa telegram login` / `qa telegram status` — persists a session under `~/.config/qa-cli`,
  drives `TELEGRAM_WEB_URL = "https://web.telegram.org/a/"` in headless Chromium via the
  `agent-browser` CLI.
- Implementation: `qa_cli/telegram.py` (login/status flow) + `qa_cli/browser.py`
  (`BrowserDriver`/`AgentBrowserDriver`).

Never, for this purpose: `open -a Telegram`, AppleScript/`osascript … activate`,
`NSWorkspace`, or computer-use/accessibility automation of the desktop window
(`computer.window({app: "Telegram"})`, walking its AX tree, clicking into its window). If
qa-cli is missing a capability you need, extend qa-cli — don't fall back to the desktop app.

## Why this matters beyond "it's annoying"

The native Telegram Desktop app is the user's real, live client, not an isolated test
fixture. Automating it steals window focus on the user's actual machine, can act on real
messages in real chats, and gives no headless, repeatable, CI-safe test path. qa-cli exists
specifically so Telegram testing never touches the live desktop session — see qa-cli#1
("Live Telegram tests currently risk opening Telegram.app or sending junk … into real
chats").

## Enforcement already in place

- tg-cli ships a permanent static guard, `tests/no-telegram-desktop-launch.test.ts`, that
  fails the build the moment any tracked source file grows a spawn/exec/AppleScript shape
  that opens or foregrounds an external app.
- qa-cli's own module docstrings and README state the same never-native-desktop invariant.

This skill is where an agent should land *before* writing a test or verification step for
any Telegram-related change, in any repo in this ecosystem — not just tg-cli.
