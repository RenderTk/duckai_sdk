import asyncio
import base64
import io
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image
from pypdf import PdfWriter

from duckai import (
    AsyncDuckAI,
    Attachment,
    AttachmentError,
    ChallengeError,
    DuckAI,
    DuckAIError,
    IncompleteResponseError,
    Message,
    RateLimitError,
    Settings,
)
from duckai.anonymous import AnonymousTokenProvider

SSE_HEADERS = {"content-type": "text/event-stream"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def settings(**kwargs):
    return Settings(_env_file=None, duckai_headers_json={"x-vqd-hash-1": "test"}, **kwargs)


def sse(*events, done=True):
    text = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    return (text + ("data: [DONE]\n\n" if done else "")).encode()


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, chunks, failure=None):
        self.chunks = chunks
        self.failure = failure
        self.read = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read += 1
            yield chunk
        if self.failure:
            raise self.failure

    async def aclose(self):
        self.closed = True


def transport(handler):
    return httpx.MockTransport(handler)


def reply(*events):
    return httpx.Response(200, headers=SSE_HEADERS, content=sse(*events))


def pdf_bytes():
    pdf = PdfWriter()
    pdf.add_blank_page(width=612, height=792)
    output = io.BytesIO()
    pdf.write(output)
    return output.getvalue()


def image_bytes():
    output = io.BytesIO()
    Image.new("RGB", (900, 600), "red").save(output, "PNG")
    return output.getvalue()


def test_sync_chat_calls_duckai_directly_and_keeps_one_loop_and_opaque_history():
    loops, requests = [], []
    history = [
        {
            "role": "assistant",
            "content": "",
            "parts": [
                {"type": "reasoning", "encryptedText": "opaque"},
            ],
        }
    ]

    def handler(request):
        loops.append(asyncio.get_running_loop())
        assert str(request.url) == "https://duck.ai/duckchat/v1/chat"
        body = json.loads(request.content)
        requests.append(body)
        return reply({"message": "Hello"}, {"message": " world"})

    with DuckAI(settings=settings(), transport=transport(handler)) as ai:
        result = ai.chat("Hi", messages=history, futureOption={"keep": True})
        assert result.text == "Hello world" and result.done
        assert str(result) == result.text
        assert result.assistant_message == {"role": "assistant", "content": "Hello world"}
        assert ai.chat(messages=[Message(role="user", content="Again")]).done
    assert loops[0] is loops[1]
    assert requests[0]["messages"][0] == history[0]
    assert requests[0]["futureOption"] == {"keep": True}
    assert history == [
        {
            "role": "assistant",
            "content": "",
            "parts": [
                {"type": "reasoning", "encryptedText": "opaque"},
            ],
        }
    ]


@pytest.mark.anyio
async def test_async_generates_fresh_tokens_without_a_server(monkeypatch):
    monkeypatch.setattr(AnonymousTokenProvider, "_prepare_runtime", AsyncMock())
    monkeypatch.setattr(
        AnonymousTokenProvider,
        "_browser_metadata",
        AsyncMock(
            return_value={"userAgent": "actual-browser", "version": "current-version"},
        ),
    )
    solve = AsyncMock(side_effect=lambda challenge: "solved-" + challenge)
    monkeypatch.setattr(AnonymousTokenProvider, "_solve", solve)
    paths = []
    counter = 0

    def handler(request):
        nonlocal counter
        paths.append(request.url.path)
        if paths[-1].endswith("/auth/token"):
            return httpx.Response(200, json={})
        if paths[-1].endswith("/status"):
            counter += 1
            return httpx.Response(200, headers={"x-vqd-hash-1": str(counter)})
        assert request.headers["x-vqd-hash-1"] == f"solved-{counter}"
        assert request.headers["user-agent"] == "actual-browser"
        return reply({"message": "ok"})

    async with AsyncDuckAI(settings=Settings(_env_file=None), transport=transport(handler)) as ai:
        assert (await ai.chat("one")).text == "ok"
        assert (await ai.chat("two")).text == "ok"
    assert paths == ["/duckchat/v1/auth/token", "/duckchat/v1/status", "/duckchat/v1/chat"] * 2
    assert solve.await_count == 2


def test_stream_is_incremental_and_early_exit_closes_connection():
    stream = TrackedStream([sse({"message": "first"}, done=False), sse({"message": "second"})])
    with DuckAI(
        settings=settings(),
        transport=transport(
            lambda _: httpx.Response(
                200,
                headers=SSE_HEADERS,
                stream=stream,
            )
        ),
    ) as ai:
        with ai.stream("hi") as chunks:
            assert next(chunks) == "first"
            assert stream.read == 1
        assert not chunks.response.done
        assert stream.closed
        assert not ai._client._streams


@pytest.mark.anyio
async def test_async_stream_multiline_unicode_and_raw_opaque_parts():
    parts = [{"type": "reasoning", "id": "rs_1", "encryptedText": "opaque"}]
    data = (
        ': ping\r\ndata: {"message":\r\ndata: "héllo"}\r\n\r\n'
        + sse({"parts": parts}, {"tool": {"name": "future-tool"}}).decode()
    ).encode()
    index = data.index(b"\xc3") + 1
    tracked = TrackedStream([data[:index], data[index:]])
    async with AsyncDuckAI(
        settings=settings(),
        transport=transport(
            lambda _: httpx.Response(
                200,
                headers=SSE_HEADERS,
                stream=tracked,
            )
        ),
    ) as ai:
        async with ai.events("hi") as stream:
            events = [e async for e in stream]
        assert stream.response.text == "héllo" and stream.response.done
        assert events[1]["parts"] == parts
        assert events[2]["tool"]["name"] == "future-tool"
        assert stream.response.assistant_message["parts"] == parts
    assert tracked.closed


def test_path_and_in_memory_attachments_use_native_protocol(tmp_path):
    pdf = pdf_bytes()
    path = tmp_path / "report.pdf"
    path.write_bytes(pdf)
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        parts = body["messages"][-1]["content"]
        assert [p["type"] for p in parts] == ["text", "image", "file"]
        assert base64.b64decode(parts[-1]["content"]) == pdf
        assert parts[-1]["filename"] == "report.pdf"
        with Image.open(io.BytesIO(base64.b64decode(parts[1]["image"].split(",")[1]))) as image:
            assert image.format == "WEBP" and image.size == (512, 341)
        return reply({"message": "read"})

    with DuckAI(settings=settings(), transport=transport(handler)) as ai:
        assert (
            ai.chat(
                "Read",
                files=[
                    path,
                    Attachment.from_bytes(
                        image_bytes(),
                        "image.png",
                        "image/png",
                    ),
                ],
            ).text
            == "read"
        )
        prepared = ai.prepare_files([path])
        assert prepared[0]["type"] == "file"
        assert ai.attachment_limits()["pdf_pages_per_file"] == 15
    assert len(requests) == 1  # preparation is local


@pytest.mark.parametrize(
    "file",
    [
        Attachment.from_bytes(b"bad", "bad.pdf", "application/pdf"),
        Attachment.from_bytes(b"", "empty.png"),
        Attachment.from_bytes(b"bad", "bad.png", "image/png"),
    ],
)
def test_invalid_attachments_fail_before_network(file):
    with (
        DuckAI(settings=settings(), transport=transport(lambda _: pytest.fail("network"))) as ai,
        pytest.raises(AttachmentError),
    ):
        ai.chat("read", files=[file])


@pytest.mark.parametrize(
    "status,error", [(429, RateLimitError), (418, ChallengeError), (500, DuckAIError)]
)
def test_errors_are_typed_preserve_retry_after_and_do_not_retry(status, error):
    tracked = TrackedStream([b'{"type":"REJECTED"}'])
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"retry-after": "60"}, stream=tracked)

    with DuckAI(settings=settings(), transport=transport(handler)) as ai:
        with pytest.raises(error) as exc:
            ai.chat("Hi")
        assert exc.value.status_code == status
        assert exc.value.retry_after == "60"
        assert exc.value.detail["upstream"]["type"] == "REJECTED"
    assert len(calls) == 1 and tracked.closed


def test_incomplete_stream_keeps_partial_response_and_closes():
    tracked = TrackedStream([sse({"message": "partial"}, done=False)])
    with DuckAI(
        settings=settings(),
        transport=transport(
            lambda _: httpx.Response(
                200,
                headers=SSE_HEADERS,
                stream=tracked,
            )
        ),
    ) as ai:
        with pytest.raises(IncompleteResponseError) as exc:
            ai.chat("Hi")
        assert exc.value.response.text == "partial"
        assert not exc.value.response.done
    assert tracked.closed


def test_read_timeout_and_collection_limit_close_stream():
    for stream, max_bytes, expected in [
        (
            TrackedStream([sse({"message": "partial"}, done=False)], httpx.ReadTimeout("timeout")),
            1000,
            504,
        ),
        (TrackedStream([b"data: " + b"x" * 100]), 32, 502),
    ]:
        with DuckAI(
            settings=settings(max_collected_bytes=max_bytes),
            transport=transport(
                lambda _, stream=stream: httpx.Response(200, headers=SSE_HEADERS, stream=stream),
            ),
        ) as ai:
            with pytest.raises(DuckAIError) as exc:
                ai.chat("Hi")
            assert exc.value.status_code == expected
        assert stream.closed


def test_in_band_error_is_not_a_successful_reply():
    with (
        DuckAI(
            settings=settings(),
            transport=transport(
                lambda _: reply(
                    {"action": "error", "status": 429, "type": "ERR_LIMIT"},
                )
            ),
        ) as ai,
        pytest.raises(RateLimitError),
    ):
        ai.chat("Hi")


def test_conversations_preserve_attachments_reasoning_and_independent_history():
    bodies = []
    parts = [{"type": "reasoning", "id": "rs_1", "encryptedText": "opaque"}]

    def handler(request):
        bodies.append(json.loads(request.content))
        return reply({"parts": parts}, {"message": "answer"})

    with DuckAI(settings=settings(), transport=transport(handler)) as ai:
        chat = ai.conversation(reasoningEffort="none")
        other = ai.conversation()
        chat.chat("Remember", files=[Attachment.from_bytes(pdf_bytes(), "file.pdf")])
        snapshot = chat.messages
        snapshot[0]["content"].clear()
        with chat.stream("What was it?") as stream:
            assert list(stream) == ["answer"]
        other.chat("New")
        assert len(chat.messages) == 4 and len(other.messages) == 2
        assert bodies[1]["messages"][0]["content"][-1]["type"] == "file"
        assert bodies[1]["messages"][1]["parts"] == parts
        assert len(bodies[2]["messages"]) == 1
        chat.clear()
        assert chat.messages == []


def test_failed_and_abandoned_turns_do_not_modify_conversation():
    responses = [reply({"message": "ok"}), reply({"message": "bad"})]
    responses.append(httpx.Response(429, json={"error": "limit"}))
    with DuckAI(settings=settings(), transport=transport(lambda _: responses.pop(0))) as ai:
        conversation = ai.conversation()
        conversation.chat("first")
        before = conversation.messages
        with conversation.stream("abandon") as stream:
            assert next(stream) == "bad"
        assert conversation.messages == before
        with pytest.raises(RateLimitError):
            conversation.chat("failed")
        assert conversation.messages == before
        assert not conversation._conversation._lock.locked()


@pytest.mark.anyio
async def test_concurrent_conversation_turns_are_serialized():
    bodies = []

    async def handler(request):
        await asyncio.sleep(0)
        bodies.append(json.loads(request.content))
        return reply({"message": "ok"})

    async with AsyncDuckAI(settings=settings(), transport=transport(handler)) as ai:
        conversation = ai.conversation()
        await asyncio.gather(conversation.chat("one"), conversation.chat("two"))
        assert len(conversation.messages) == 4
        assert [len(b["messages"]) for b in bodies] == [1, 3]


@pytest.mark.anyio
async def test_cancellation_releases_stream_and_conversation_lock():
    started = asyncio.Event()

    class WaitingStream(TrackedStream):
        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield b""

    tracked = WaitingStream([])
    async with AsyncDuckAI(
        settings=settings(),
        transport=transport(
            lambda _: httpx.Response(
                200,
                headers=SSE_HEADERS,
                stream=tracked,
            )
        ),
    ) as ai:
        conversation = ai.conversation()
        task = asyncio.create_task(conversation.chat("wait"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert conversation.messages == []
        assert not conversation._lock.locked()
        assert tracked.closed


def test_client_close_releases_active_stream_and_is_idempotent():
    tracked = TrackedStream([sse({"message": "partial"}, done=False)])
    ai = DuckAI(
        settings=settings(),
        transport=transport(
            lambda _: httpx.Response(
                200,
                headers=SSE_HEADERS,
                stream=tracked,
            )
        ),
    )
    stream = ai.stream("Hi")
    assert next(stream) == "partial"
    ai.close()
    ai.close()
    assert tracked.closed
    with pytest.raises(RuntimeError, match="closed"):
        ai.chat("Hi")


def test_client_close_also_releases_conversation_lock():
    tracked = TrackedStream([sse({"message": "partial"}, done=False)])
    ai = DuckAI(
        settings=settings(),
        transport=transport(
            lambda _: httpx.Response(
                200,
                headers=SSE_HEADERS,
                stream=tracked,
            )
        ),
    )
    conversation = ai.conversation()
    stream = conversation.stream("Hi")
    assert next(stream) == "partial"
    ai.close()
    assert tracked.closed
    assert not conversation._conversation._lock.locked()


def test_python_style_options_translate_to_native_wire_names():
    def handler(request):
        body = json.loads(request.content)
        assert body["reasoningEffort"] == "none"
        assert body["canUseTools"] is False
        assert body["futureOption"] == 123
        assert "reasoning_effort" not in body and "can_use_tools" not in body
        return reply({"message": "ok"})

    with DuckAI(settings=settings(), transport=transport(handler)) as ai:
        ai.chat("Hi", reasoning_effort="none", can_use_tools=False, futureOption=123)
        with pytest.raises(ValueError):
            ai.chat("Hi", can_use_tools=False, canUseTools=True)


@pytest.mark.anyio
async def test_sync_calls_in_async_code_give_actionable_error_without_leaking_coroutines():
    ai = DuckAI(settings=settings())
    with pytest.raises(RuntimeError, match="AsyncDuckAI"):
        ai.chat("Hi")
    await ai._client.aclose()
    ai._runner.close()


def test_missing_prompt_and_invalid_attachment_target_fail_locally():
    with DuckAI(settings=settings(), transport=transport(lambda _: pytest.fail("network"))) as ai:
        with pytest.raises(ValueError):
            ai.chat()
        with pytest.raises(TypeError):
            ai.prepare_files("file.pdf")
        with pytest.raises(ValueError):
            ai.chat(
                messages=[{"role": "assistant", "content": "Hi"}],
                files=[Attachment.from_bytes(pdf_bytes(), "file.pdf")],
            )
