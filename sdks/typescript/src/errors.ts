import type { ChatResponse } from "./types.js";

export class DuckAIError extends Error {
  readonly retryAfter?: string;
  constructor(
    public readonly statusCode = 502,
    public readonly detail: unknown = "Duck.ai request failed",
    public readonly headers: Readonly<Record<string, string>> = {},
    options?: ErrorOptions,
  ) {
    const message =
      typeof detail === "string" ? detail : JSON.stringify(detail);
    super(message, options);
    this.name = new.target.name;
    this.retryAfter = headers["retry-after"];
  }
}
export class AttachmentError extends DuckAIError {}
export class RateLimitError extends DuckAIError {}
export class ChallengeError extends DuckAIError {}
export class IncompleteResponseError extends DuckAIError {
  constructor(public readonly response: ChatResponse) {
    super(502, "Duck.ai stream ended before its completion marker");
  }
}
/** @internal */
export function upstreamError(
  status: number,
  detail: unknown,
  headers: Record<string, string> = {},
): DuckAIError {
  const ErrorType =
    status === 429
      ? RateLimitError
      : [401, 403, 418].includes(status)
        ? ChallengeError
        : DuckAIError;
  return new ErrorType(status, detail, headers);
}
