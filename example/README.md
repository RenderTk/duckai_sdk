# Duck.ai Desktop

A native Python desktop chat application built with **PySide6** and this SDK.
The layout takes inspiration from Gemini: a quiet sidebar, generous conversation
space, a gradient welcome screen, suggestion cards, and a rounded composer.
All chat responses come from **Duck.ai**. There is no demo responder or local server.
This app is kept in the SDK repository as an example project. It is excluded from
SDK wheels and source distributions; normal SDK installs do not install PySide6
or start this app. Clone the repository to run it.

## Run

From the repository root, with Python 3.11 or newer:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[desktop]'
python -m example
```

On Windows, activate with `.venv\Scripts\activate` instead. Alternatively, install
with `python -m pip install -r example/requirements.txt` from the repository root.
`python example/main.py` is also supported.

The SDK handles Duck.ai's browser challenge using an isolated headless Chromium
process. No browser window opens. If Chromium is not cached, the SDK downloads it
automatically on first use. No API key or browser configuration is required. The
desktop window stays responsive during setup and streaming, and the background
browser closes when you quit the app.

On Linux, run `python -m playwright install --with-deps chromium` to install browser
dependencies. A graphical desktop and the platform's Qt libraries are required.
On Debian/Ubuntu, an xcb plugin error commonly means `libxcb-cursor0` is missing.
The chat backend does not need a display; the native desktop interface does.

## What is included

- Streamed Markdown replies with headings, lists, tables, and fenced code blocks.
- Copy a complete reply or just its fenced code blocks to the system clipboard.
- Stop requests, retry failures, regenerate the last reply, or edit and resend the
  last prompt. Partial replies remain visible but are excluded from model context.
- Independent conversations, automatic titles, pinning, renaming, deletion, and
  search across conversation titles and visible message content.
- Drafts and attachment selections retained when switching conversations.
- PDF/image selection and file drops onto the composer, with removable file chips.
- Canonical SDK history retained, including encoded attachments and opaque response
  fields. Regeneration and edits with unchanged attachments do not require the
  original source file after a successful turn.
- JSON export/import for continuing conversations, plus readable Markdown export.
- Light, dark, and system appearance; a collapsible sidebar; a jump-to-latest button
  that respects your position while reading older messages.
- An editable model selector. The default is `gpt-6-luna`; enter another model ID
  supported by your Duck.ai session. The app does not claim an authoritative catalogue.
- Settings for timeout, tools, reasoning effort, Chromium executable, browser CDP
  URL, and silent browser mode (enabled by default).
- Atomic local saves, restrictive workspace file permissions where supported,
  recovery of interrupted turns, backups of unreadable workspace files, and a
  workspace lock that prevents two app instances overwriting the same history.
- Explicit rate-limit, challenge, attachment, and connection errors, with manual
  retry controls. The app never retries a request automatically.
- One background asyncio loop and reusable SDK client; graceful cancellation and
  HTTP/browser cleanup on exit.

The SDK enforces attachment limits. For the default model, documented limits
include three PDFs per conversation, 15 pages per PDF, 5 MiB combined PDF bytes,
three images per message, five images per conversation, and 4,500 text characters
in messages containing images. Supported image formats: PNG, JPEG, WebP, and GIF.
Duck.ai determines model availability and may change upstream limits.

## Keyboard shortcuts

| Action | Shortcut |
| --- | --- |
| Send | Enter |
| Insert a new line | Shift + Enter |
| New chat | Ctrl + N / Command + N |
| Search conversations | Ctrl + F / Command + F |
| Focus composer | Ctrl + L / Command + L |
| Import conversation | Ctrl + O / Command + O |
| Export conversation | Ctrl + S / Command + S |
| Settings | Ctrl + , / Command + , |
| Toggle sidebar | Ctrl + B / Command + B |
| Toggle light/dark | Ctrl + Shift + D / Command + Shift + D |
| Stop response / cancel editing | Escape |

Right-click a conversation for its menu, or use the top-right menu. Double-click a
conversation to rename it. Suggestions fill the composer; they send only when you
press Enter or click Send.

## Local data

The app stores `workspace.json` in Qt's application data directory. Open
**Settings → Data** to see the exact path. The file contains conversations, drafts,
settings, and native SDK history, including prepared attachment content. It is
plain JSON, not an encrypted vault. Prompts and attachments are sent to Duck.ai
when submitted. JSON exports contain full history and attachment payloads;
Markdown exports contain visible text and attachment names.

Choose a separate directory or start in dark mode:

```sh
python -m example --data-dir ./output/my-desktop-chats
python -m example --dark
```

SDK `.env` / environment settings still apply to SDK options the desktop does not
override. Desktop timeout and headless preferences are passed explicitly; nonempty
desktop browser fields override SDK environment defaults.
Workspaces created by the earlier visible-browser version migrate to the silent
default on first launch. You can explicitly disable silent mode in Settings →
Connection for debugging; that choice persists.

## Architecture and verification

- `app.py`: native window, conversation controls, settings, and state transitions.
- `widgets.py` / `theme.py`: reusable controls, Markdown, icons, and themes.
- `worker.py`: owns `AsyncDuckAI` in a `QThread` running an asyncio event loop.
  Qt signals carry text deltas and outcomes to the GUI thread. Only a completed
  stream commits canonical request/response messages to history.
- `store.py`: versioned local persistence and validated conversation import/export.
- `__main__.py` / `main.py`: launchers.

Run the SDK and desktop tests:

```sh
python -m pip install -e '.[desktop,dev]'
python -m pytest -q
python -m ruff check duckai tests scripts examples example
```

Desktop integration tests use the actual SDK with a controlled HTTP transport to
verify cancellation, attachment replay, history isolation, client reuse, and
cleanup without rate-limiting the live service. They are skipped if PySide6 is not
installed; persistence tests always run.

For a screenshot of the actual window without sending a message:

```sh
python -m example --screenshot output/desktop.png
```

The example is not affiliated with Google or DuckDuckGo.
