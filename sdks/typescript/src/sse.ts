import { DuckAIError } from "./errors.js";

export interface SSEEvent {
  data?: unknown;
  event: string;
  done: boolean;
}

/** Incremental UTF-8/SSE decoding with a bound on even unterminated frames. */
export async function* iterEvents(
  body: ReadableStream<Uint8Array>,
  maxBytes: number,
): AsyncGenerator<SSEEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let received = 0,
    buffer = "",
    event = "message";
  let lines: string[] = [];
  const parse = (): SSEEvent | undefined => {
    const name = event;
    event = "message";
    if (!lines.length) return undefined;
    const data = lines.join("\n");
    lines = [];
    if (data === "[DONE]") return { event: name, done: true };
    let value: unknown;
    try {
      value = JSON.parse(data);
    } catch {
      value = { data };
    }
    return { event: name, data: value, done: false };
  };
  const line = (value: string): SSEEvent | undefined => {
    if (!value) return parse();
    if (value.startsWith("data:")) lines.push(value.slice(5).replace(/^ /, ""));
    else if (value.startsWith("event:"))
      event = value.slice(6).replace(/^ /, "");
    return undefined;
  };
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (value) {
        received += value.byteLength;
        if (received > maxBytes)
          throw new DuckAIError(
            502,
            "Duck.ai response exceeds collection limit",
          );
        buffer += decoder.decode(value, { stream: true });
      }
      if (done) buffer += decoder.decode();
      // Preserve a trailing CR until we know whether the next chunk starts with LF.
      while (true) {
        const match = /[\r\n]/.exec(buffer);
        if (
          !match ||
          (!done && match[0] === "\r" && match.index === buffer.length - 1)
        )
          break;
        const width =
          buffer.slice(match.index, match.index + 2) === "\r\n" ? 2 : 1;
        const parsed = line(buffer.slice(0, match.index));
        buffer = buffer.slice(match.index + width);
        if (parsed) {
          yield parsed;
          if (parsed.done) return;
        }
      }
      if (done) {
        if (buffer) line(buffer);
        const parsed = parse();
        if (parsed) yield parsed;
        return;
      }
    }
  } finally {
    try {
      await reader.cancel();
    } catch {
      /* Connection may already be closed. */
    }
    reader.releaseLock();
  }
}
