# Duck.ai Python SDK

Talk to [Duck.ai](https://duck.ai/) directly from Python. The SDK handles anonymous
sessions, browser challenges, request headers, streaming, conversation history,
and PDF/image attachments. You don't need to start a FastAPI server or copy tokens
from your browser.

## Install

Requires Python 3.11 or newer.

```sh
python -m pip install "git+https://github.com/RenderTk/duckai_sdk.git"
python -m playwright install chromium
```

Or install your local checkout:

```sh
git clone https://github.com/RenderTk/duckai_sdk.git
cd duckai_sdk
python -m pip install -e .
python -m playwright install chromium
```

On Linux, install Chromium's system dependencies with
`python -m playwright install --with-deps chromium`. The default browser mode needs
a display. For a Linux machine without a desktop, install Xvfb and run your script
with `xvfb-run -a python your_script.py`.

## Your first chat

```python
from duckai import DuckAI

with DuckAI() as ai:
    response = ai.chat("Explain recursion with a simple example.")
    print(response.text)
```

`with` closes the HTTP session, streams, temporary browser, and browser profile
when you finish. Keep one client open across requests so its browser can be reused.
The browser starts only when the first request needs an anonymous token.

## Follow-up questions

Use a conversation when you want the model to remember previous turns:

```python
from duckai import DuckAI

with DuckAI() as ai:
    chat = ai.conversation()
    print(chat.chat("My project's name is Later. Remember it.").text)
    print(chat.chat("What is my project's name?").text)

    history = chat.messages  # a copy you can save as JSON
```

Each conversation has independent history. A turn is saved only after a complete
reply. Failed requests and streams you stop early leave the previous history intact.
`chat.clear()` starts over. To restore history, use
`ai.conversation(messages=saved_history)`.

`ai.chat(...)` is a single request. To supply history yourself, use
`ai.chat("Follow up", messages=history)` or `ai.chat(messages=history)`.

## Streaming

```python
from duckai import DuckAI

with DuckAI() as ai:
    with ai.stream("Write a short story about a lost robot.") as stream:
        for chunk in stream:
            print(chunk, end="", flush=True)
    print()
    print("Complete:", stream.response.done)
```

Chunks arrive as text becomes available. The stream context manager also closes
the upstream connection if you break out of the loop. For a conversation, use
`with chat.stream("Follow up") as stream:` in the same way.

## PDFs and images

Pass file paths in `files`:

```python
from pathlib import Path
from duckai import DuckAI

with DuckAI() as ai:
    chat = ai.conversation()
    answer = chat.chat(
        "Summarize the PDF and explain the screenshot.",
        files=[Path("document.pdf"), Path("screenshot.png")],
    )
    print(answer.text)
    print(chat.chat("What are the three main points?").text)
```

For files already in memory:

```python
from duckai import Attachment, DuckAI

with open("document.pdf", "rb") as file:
    document = Attachment.from_bytes(file.read(), filename="document.pdf")

with DuckAI() as ai:
    print(ai.chat("Summarize this document.", files=[document]).text)
```

Images are decoded, oriented, resized, and converted to WebP. PDF bytes are
preserved and encoded in Duck.ai's native attachment format. The SDK checks files
and conversation limits before sending a request.

The observed anonymous limits for the default model are:

- PDF: up to 3 per conversation, 15 pages per file, 5 MiB combined.
- Images: PNG, JPEG, WebP, or GIF; up to 3 per message and 5 per conversation.
- Text in a message containing images: up to 4,500 characters.

`ai.attachment_limits()` returns the configured limits. `ai.prepare_files([...])`
returns native content parts without making a network request, useful when you
manage your own message history. Prepared PDFs/images still count toward the limits
when included in later messages.

## Async applications

```python
import asyncio
from duckai import AsyncDuckAI

async def main():
    async with AsyncDuckAI() as ai:
        response = await ai.chat("Say hello in Spanish.")
        print(response.text)

        async with ai.stream("Count from one to five.") as stream:
            async for chunk in stream:
                print(chunk, end="", flush=True)
        print()

asyncio.run(main())
```

Async conversations have the same interface, with `await chat.chat(...)` and
`async with chat.stream(...)`. Use `AsyncDuckAI` within a running event loop,
including async web applications and notebooks. The sync `DuckAI` client should
be used in the thread that created it. Both clients can be closed explicitly with
`ai.close()` or `await ai.aclose()` if you don't use a context manager.

## Models and options

The default model is `gpt-6-luna`. Duck.ai controls which models are available to an
anonymous session.

```python
from duckai import DuckAI

with DuckAI(model="gpt-6-luna", timeout=120) as ai:
    response = ai.chat(
        "Explain what an HTTP request is.",
        reasoning_effort="none",
        can_use_tools=True,
    )
    print(response.text)
```

Pass `model=...` to a particular chat or conversation to override the client
default. Python option names such as `reasoning_effort`, `can_use_tools`, and
`durable_stream` are translated to Duck.ai's native names. Native option names and
additional fields are also accepted. Supply only one spelling of a given option.

`response.text` is the reply, `response.done` indicates the completion marker,
`response.events` contains original parsed stream events, and
`response.assistant_message` is reusable in message history. Opaque reasoning and
tool fields in supplied historical messages are preserved.

For raw parsed events, use `with ai.events("Hello") as stream:` and iterate over
the stream. Its `response` still collects text and completion state. Async clients
and conversations also provide `events()`.

## How it works

On your first chat, the SDK starts an isolated Chromium session. It reads Duck.ai's
current frontend version and the browser's actual user agent, bootstraps an anonymous
session, and fetches a fresh browser challenge. Chromium evaluates the challenge
inside Duck.ai's own page; the SDK hashes the returned client values and constructs
the headers needed for the chat request.

The Python HTTP client then posts directly to
`https://duck.ai/duckchat/v1/chat` and decodes its event stream. Follow-up requests
reuse the browser while obtaining fresh challenges. There is no local API server
between your code and Duck.ai. No HAR file, account, or captured token is needed at
runtime. Prompts and attached files are sent to Duck.ai.

**Chromium is still required.** The working default opens a regular browser window
with a temporary profile. Headless/automated mode was rejected by Duck.ai in live
checks, so `DuckAI(headless=True)` is an optional setting, not a guaranteed way to
hide the browser. For Linux without a desktop, use Xvfb as described above.

You can select an installed Chromium executable with
`DuckAI(browser_executable="/absolute/path/to/chromium")`, or attach to an existing
local Chromium debugging session with
`DuckAI(browser_cdp_url="http://127.0.0.1:9222")`. The SDK closes its own tab but
keeps an externally managed browser running.

Configuration also reads `DUCKAI_*` environment variables and an optional `.env`
file. See [.env.example](.env.example). Constructor options override these values.
For advanced settings, pass a `duckai.Settings` instance; set `_env_file=None` when
you want to ignore `.env`.

## Errors

```python
from duckai import DuckAI, DuckAIError, RateLimitError

try:
    with DuckAI() as ai:
        print(ai.chat("Hello").text)
except RateLimitError as error:
    print("Rate limited. Retry-After:", error.retry_after)
except DuckAIError as error:
    print("Request failed:", error.status_code, error.detail)
```

Errors include `AttachmentError`, `ChallengeError`, `RateLimitError`, and
`IncompleteResponseError`, all subclasses of `DuckAIError`. An incomplete response
exposes partial output as `error.response`. Local file-system failures raise normal
Python file errors; invalid arguments may raise `ValueError`, `TypeError`, or a
Pydantic validation error.

Rate limits and challenge rejections are propagated without automatic retries.
This is an unofficial client for Duck.ai's web endpoint, so upstream behavior and
availability can change.

## Development and verification

```sh
python -m pip install -e '.[dev]'
python -m pytest -q
python -m ruff check duckai tests scripts examples
```

Unit tests cover direct requests, anonymous-token refresh, synchronous and
asynchronous streams, native attachments, history isolation, interrupted replies,
rate limits, cancellation, and resource cleanup. They do not need Chromium or a
live Duck.ai connection.

To run live semantic checks with synthetic PDFs and images:

```sh
python scripts/verify_live.py
```

This makes six chat requests and writes an ignored report in `output/`. Duck.ai
may rate-limit a burst of requests. To run a particular check after a cooldown:

```sh
python scripts/verify_live.py --checks async_stream --output output/async-stream.json
```
