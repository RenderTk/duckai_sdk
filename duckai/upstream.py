import json
from dataclasses import dataclass
from typing import Any

import anyio
import httpx

from duckai.anonymous import AnonymousTokenProvider
from duckai.config import FORWARDED_HEADERS, Settings
from duckai.errors import ChallengeError, DuckAIError, RateLimitError

CHAT_URL = "https://duck.ai/duckchat/v1/chat"
DEFAULT_HEADERS = {
    "accept": "text/event-stream",
    "accept-language": "en-US,en;q=0.6",
    "content-type": "application/json",
    "origin": "https://duck.ai",
    "referer": "https://duck.ai/",
    "dnt": "1",
    "sec-gpc": "1",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36"
    ),
}
RESPONSE_HEADERS = ("x-vqd-hash-1", "x-vqd-4", "retry-after")


async def close_response(response: httpx.Response) -> None:
    # A disconnected downstream client must still release its upstream connection.
    with anyio.CancelScope(shield=True):
        await response.aclose()


def forwarded_response_headers(response: httpx.Response) -> dict[str, str]:
    return {key: response.headers[key] for key in RESPONSE_HEADERS if key in response.headers}


class DuckAIClient:
    def __init__(
        self, client: httpx.AsyncClient, settings: Settings, tokens: AnonymousTokenProvider
    ):
        self.client = client
        self.settings = settings
        self.tokens = tokens

    async def open_chat(self, payload: dict[str, Any], request_headers: httpx.Headers):
        headers = DEFAULT_HEADERS | self.settings.duckai_headers_json
        headers.update(
            {key: request_headers[key] for key in FORWARDED_HEADERS if key in request_headers}
        )
        # The API caller's default UA (e.g. curl) is not the captured browser UA.
        if "user-agent" in self.settings.duckai_headers_json:
            headers["user-agent"] = self.settings.duckai_headers_json["user-agent"]
        if not headers.get("x-vqd-hash-1") and not headers.get("x-vqd-4"):
            if self.settings.duckai_auto_token:
                headers.update(await self.tokens.headers())
            else:
                raise DuckAIError(
                    status_code=503,
                    detail={
                        "error": "duckai_headers_required",
                        "message": (
                            "Supply a fresh x-vqd-hash-1 from a successful Duck.ai browser "
                            "request, via a request header or DUCKAI_HEADERS_JSON. "
                            "The /status challenge is not a ready-to-use chat token."
                        ),
                    },
                )
        try:
            response = await self.client.send(
                self.client.build_request("POST", CHAT_URL, headers=headers, json=payload),
                stream=True,
            )
            if not response.is_success:
                await self.raise_upstream_error(response)
            if "text/event-stream" not in response.headers.get("content-type", "").lower():
                await close_response(response)
                raise DuckAIError(502, detail="Duck.ai returned a non-SSE response")
            return response
        except httpx.TimeoutException as exc:
            raise DuckAIError(504, detail="Duck.ai request timed out") from exc
        except httpx.HTTPError as exc:
            raise DuckAIError(502, detail="Could not connect to Duck.ai") from exc

    async def raise_upstream_error(self, response: httpx.Response) -> None:
        try:
            # Bound error reads too; never echo arbitrary upstream HTML.
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 65536:
                    break
            try:
                upstream = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                upstream = {"message": "Duck.ai rejected the request"}
            detail = {"error": "duckai_error", "upstream": upstream}
            if response.status_code in {401, 403, 418}:
                detail["hint"] = (
                    "Duck.ai rejected this browser/session. Use an accepted browser via "
                    "DUCKAI_BROWSER_CDP_URL, or supply fresh captured request headers."
                )
            error_type = (
                RateLimitError
                if response.status_code == 429
                else ChallengeError
                if response.status_code in {401, 403, 418}
                else DuckAIError
            )
            raise error_type(
                status_code=response.status_code,
                detail=detail,
                headers=forwarded_response_headers(response),
            )
        finally:
            await close_response(response)


@dataclass(frozen=True)
class SSEEvent:
    data: Any = None
    event: str = "message"
    done: bool = False


async def iter_events(response: httpx.Response, max_bytes: int):
    """Incrementally decode SSE while bounding even unterminated frames."""
    received = 0
    data_lines: list[str] = []
    event_name = "message"

    def parse():
        nonlocal event_name
        name, event_name = event_name, "message"
        if not data_lines:
            return None
        data = "\n".join(data_lines)
        data_lines.clear()
        if data == "[DONE]":
            return SSEEvent(done=True)
        try:
            value = json.loads(data)
        except ValueError:
            value = {"data": data}
        return SSEEvent(value, name)

    async def bounded_bytes():
        nonlocal received
        async for chunk in response.aiter_bytes():
            received += len(chunk)
            if received > max_bytes:
                raise DuckAIError(502, detail="Duck.ai response exceeds collection limit")
            yield chunk

    try:
        decoder = httpx.Response(
            200,
            headers={"content-type": "text/event-stream; charset=utf-8"},
            content=bounded_bytes(),
        )
        async for line in decoder.aiter_lines():
            if not line:
                event = parse()
                if event is not None:
                    yield event
                    if event.done:
                        return
            elif line.startswith("data:"):
                data_lines.append(line[5:].removeprefix(" "))
            elif line.startswith("event:"):
                event_name = line[6:].removeprefix(" ")
        event = parse()
        if event is not None:
            yield event
    except httpx.TimeoutException as exc:
        raise DuckAIError(504, detail="Duck.ai stream timed out") from exc
    except httpx.HTTPError as exc:
        raise DuckAIError(502, detail="Duck.ai stream interrupted") from exc
    finally:
        await close_response(response)


async def collect_events(response: httpx.Response, max_bytes: int) -> dict[str, Any]:
    events: list[Any] = []
    done = False
    async for event in iter_events(response, max_bytes):
        if event.done:
            done = True
        else:
            events.append(event.data)
    text = "".join(
        event["message"]
        for event in events
        if isinstance(event, dict) and isinstance(event.get("message"), str)
    )
    return {"text": text, "events": events, "done": done}
