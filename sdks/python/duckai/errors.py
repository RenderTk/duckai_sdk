"""SDK errors, independent of HTTP server frameworks."""

from typing import Any


class DuckAIError(Exception):
    def __init__(
        self,
        status_code: int = 502,
        detail: Any = "Duck.ai request failed",
        headers: dict[str, str] | None = None,
    ):
        self.status_code = status_code
        self.detail = detail
        self.headers = headers or {}
        self.retry_after = self.headers.get("retry-after")
        super().__init__(str(detail))


class AttachmentError(DuckAIError):
    """An attachment is invalid or exceeds the observed model limits."""


class RateLimitError(DuckAIError):
    """Duck.ai rejected the request with HTTP 429; inspect retry_after."""


class ChallengeError(DuckAIError):
    """Duck.ai rejected the browser session or challenge."""


class IncompleteResponseError(DuckAIError):
    """The stream ended without its completion marker; response contains partial output."""

    def __init__(self, response):
        self.response = response
        super().__init__(502, "Duck.ai stream ended before [DONE]")
