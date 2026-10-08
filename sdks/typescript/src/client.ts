import type {
  ChatOptions,
  ChatRequest,
  ClientOptions,
  ContentPart,
  FileInput,
  Message,
  TokenProvider,
} from "./types.js";
import { ChatResponse } from "./types.js";
import {
  DuckAIError,
  IncompleteResponseError,
  upstreamError,
} from "./errors.js";
import { createSettings } from "./settings.js";
import { getModel, listModels } from "./catalog.js";
import {
  attachmentLimits,
  prepareFiles,
  validatePayload,
} from "./attachments.js";
import { AnonymousTokenProvider, Mutex } from "./anonymous.js";
import { iterEvents, type SSEEvent } from "./sse.js";

export const CHAT_URL = "https://duck.ai/duckchat/v1/chat";
const defaultHeaders = {
  accept: "text/event-stream",
  "accept-language": "en-US,en;q=0.6",
  "content-type": "application/json",
  origin: "https://duck.ai",
  referer: "https://duck.ai/",
  dnt: "1",
  "sec-gpc": "1",
  "sec-fetch-dest": "empty",
  "sec-fetch-mode": "cors",
  "sec-fetch-site": "same-origin",
  "user-agent":
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36",
};

async function rejectedResponse(response: Response): Promise<never> {
  let value: unknown = { message: "Duck.ai rejected the request" };
  const reader = response.body?.getReader();
  if (reader) {
    const chunks: Uint8Array[] = [];
    let size = 0;
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        chunks.push(value.subarray(0, Math.max(0, 65536 - size)));
        size += value.length;
        if (size >= 65536) break;
      }
      try {
        value = JSON.parse(Buffer.concat(chunks).toString());
      } catch {
        /* Do not echo arbitrary HTML. */
      }
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }
  const headers: Record<string, string> = {};
  for (const key of ["x-vqd-hash-1", "x-vqd-4", "retry-after"]) {
    const value = response.headers.get(key);
    if (value) headers[key] = value;
  }
  throw upstreamError(
    response.status,
    { error: "duckai_error", upstream: value },
    headers,
  );
}

export class DuckAI {
  readonly settings;
  readonly model: string;
  private readonly fetch;
  private readonly tokens: TokenProvider;
  private readonly streams = new Set<ChatStream<unknown>>();
  private closed = false;
  private closePromise?: Promise<void>;
  constructor(options: ClientOptions = {}) {
    this.settings = createSettings(options);
    this.model = options.model ?? "gpt-6-luna";
    if (!this.model.trim()) throw new TypeError("model must not be empty");
    this.fetch = options.fetch ?? globalThis.fetch.bind(globalThis);
    this.tokens =
      options.tokenProvider ??
      new AnonymousTokenProvider(this.settings, this.fetch);
  }
  private check(): void {
    if (this.closed) throw new Error("Duck.ai client is closed");
  }
  async close(): Promise<void> {
    if (this.closePromise) return this.closePromise;
    this.closed = true;
    this.closePromise = (async () => {
      try {
        await Promise.all([...this.streams].map((s) => s.close()));
      } finally {
        await this.tokens.close();
      }
    })();
    return this.closePromise;
  }
  async [Symbol.asyncDispose](): Promise<void> {
    await this.close();
  }
  listModels(options?: { includeSubscriber?: boolean }) {
    this.check();
    return listModels(options);
  }
  attachmentLimits(model = this.model) {
    this.check();
    return attachmentLimits(model, this.settings);
  }
  async prepareFiles(
    files: readonly FileInput[],
    options: { model?: string; signal?: AbortSignal } = {},
  ): Promise<ContentPart[]> {
    this.check();
    const model = options.model ?? this.model;
    const parts = await prepareFiles(
      files,
      model,
      this.settings,
      options.signal,
    );
    await validatePayload(
      {
        model,
        messages: [{ role: "user", content: parts }],
        reasoningEffort: "none",
        canUseTools: true,
        canShowGreeting: false,
      },
      this.settings,
    );
    return parts;
  }
  stream(prompt?: string, options: ChatOptions = {}): ChatStream<string> {
    this.check();
    const stream = new ChatStream<string>(this, prompt, options, false);
    this.streams.add(stream);
    return stream;
  }
  events(prompt?: string, options: ChatOptions = {}): ChatStream<unknown> {
    this.check();
    const stream = new ChatStream(this, prompt, options, true);
    this.streams.add(stream);
    return stream;
  }
  async chat(
    prompt?: string,
    options: ChatOptions = {},
  ): Promise<ChatResponse> {
    const stream = this.stream(prompt, options);
    try {
      for await (const _ of stream) {
        /* Collected in stream.response. */
      }
      return stream.response;
    } finally {
      await stream.close();
    }
  }
  conversation(options: ChatOptions = {}): Conversation {
    this.check();
    return new Conversation(this, options);
  }
  /** @internal */
  unregister(stream: ChatStream<unknown>): void {
    this.streams.delete(stream);
  }
  /** @internal */
  async request(
    prompt: string | undefined,
    options: ChatOptions,
  ): Promise<ChatRequest> {
    this.check();
    options.signal?.throwIfAborted();
    if (prompt !== undefined && typeof prompt !== "string")
      throw new TypeError("prompt must be a string");
    const messages = structuredClone([...(options.messages ?? [])]);
    if (prompt !== undefined) messages.push({ role: "user", content: prompt });
    if (!messages.length)
      throw new TypeError("Provide a prompt or at least one message");
    for (const message of messages)
      if (!message || typeof message.role !== "string" || !message.role)
        throw new TypeError("Messages need a role");
    const model = options.model ?? this.model;
    if (!model.trim()) throw new TypeError("model must not be empty");
    if (options.files?.length) {
      const last = messages.at(-1)!;
      if (last.role !== "user")
        throw new TypeError("Files must attach to the final user message");
      const parts = await prepareFiles(
        options.files,
        model,
        this.settings,
        options.signal,
      );
      const content =
        typeof last.content === "string"
          ? [{ type: "text", text: last.content }]
          : (last.content ?? []);
      last.content = [...content, ...parts];
    }
    const info = getModel(model);
    const {
      signal: _signal,
      files: _files,
      messages: _messages,
      model: _model,
      ...wire
    } = options;
    const body: ChatRequest = {
      ...wire,
      model,
      messages,
      reasoningEffort:
        options.reasoningEffort ?? info?.defaultReasoningEffort ?? "none",
      canUseTools: options.canUseTools ?? info?.supportsTools ?? true,
      canShowGreeting: options.canShowGreeting ?? false,
    };
    if (info && !info.reasoningEfforts.includes(body.reasoningEffort))
      throw new TypeError(
        `${info.name} supports reasoning efforts: ${info.reasoningEfforts.join(", ")}`,
      );
    await validatePayload(body, this.settings);
    options.signal?.throwIfAborted();
    this.check();
    return body;
  }
  /** @internal */
  async open(
    body: ChatRequest,
    signal: AbortSignal,
    networkReady: () => void,
  ): Promise<Response> {
    this.check();
    const headers: Record<string, string> = {
      ...defaultHeaders,
      ...this.settings.headers,
    };
    if (!headers["x-vqd-hash-1"] && !headers["x-vqd-4"]) {
      if (!this.settings.autoToken)
        throw new DuckAIError(
          503,
          "Supply fresh captured Duck.ai headers or enable autoToken",
        );
      Object.assign(headers, await this.tokens.headers(signal));
    }
    signal.throwIfAborted();
    this.check();
    networkReady();
    let response: Response;
    try {
      response = await this.fetch(CHAT_URL, {
        method: "POST",
        headers,
        body: JSON.stringify(body),
        signal,
        redirect: "error",
      });
    } catch (error) {
      if (signal.aborted) throw signal.reason;
      if (error instanceof DuckAIError) throw error;
      throw new DuckAIError(
        502,
        "Could not connect to Duck.ai",
        {},
        { cause: error },
      );
    }
    if (!response.ok) return await rejectedResponse(response);
    if (
      !response.headers
        .get("content-type")
        ?.toLowerCase()
        .includes("text/event-stream") ||
      !response.body
    ) {
      await response.body?.cancel();
      throw new DuckAIError(502, "Duck.ai returned a non-SSE response");
    }
    return response;
  }
}

export class ChatStream<T = string> implements AsyncIterableIterator<T> {
  readonly response: ChatResponse;
  request?: ChatRequest;
  private readonly controller = new AbortController();
  private iterator?: AsyncGenerator<SSEEvent>;
  private startPromise?: Promise<void>;
  private closed = false;
  private timedOut = false;
  private timer?: ReturnType<typeof setTimeout>;
  private readonly signal: AbortSignal;
  private reading = false;
  constructor(
    private readonly client: DuckAI,
    private readonly prompt: string | undefined,
    private readonly options: ChatOptions,
    private readonly raw: boolean,
  ) {
    this.response = new ChatResponse(options.model ?? client.model);
    this.signal = options.signal
      ? AbortSignal.any([options.signal, this.controller.signal])
      : this.controller.signal;
  }
  [Symbol.asyncIterator](): AsyncIterableIterator<T> {
    return this;
  }
  async [Symbol.asyncDispose](): Promise<void> {
    await this.close();
  }
  private async start(): Promise<void> {
    if (this.closed) throw new Error("Duck.ai stream is closed");
    if (!this.startPromise)
      this.startPromise = (async () => {
        this.signal.throwIfAborted();
        this.request = await this.client.request(this.prompt, {
          ...this.options,
          signal: this.signal,
        });
        const response = await this.client.open(
          this.request,
          this.signal,
          () => {
            this.timer = setTimeout(() => {
              this.timedOut = true;
              this.controller.abort(
                new DOMException("Duck.ai request timed out", "TimeoutError"),
              );
            }, this.client.settings.timeout);
          },
        );
        if (this.signal.aborted) {
          await response.body?.cancel();
          this.signal.throwIfAborted();
        }
        this.iterator = iterEvents(
          response.body!,
          this.client.settings.maxCollectedBytes,
        );
      })();
    await this.startPromise;
  }
  async next(): Promise<IteratorResult<T>> {
    if (this.closed) return { done: true, value: undefined };
    if (this.reading)
      throw new Error("Only one consumer can read a stream at a time");
    this.reading = true;
    try {
      await this.start();
      while (true) {
        const next = await this.iterator!.next();
        if (next.done) throw new IncompleteResponseError(this.response);
        if (next.value.done) {
          this.response.done = true;
          await this.close();
          return { done: true, value: undefined };
        }
        const data = next.value.data;
        if (data && typeof data === "object" && !Array.isArray(data)) {
          const object = data as Record<string, unknown>;
          if (
            object.action === "error" ||
            ["error", "proxy_error"].includes(next.value.event)
          ) {
            const status =
              typeof object.status === "number" &&
              object.status >= 400 &&
              object.status <= 599
                ? object.status
                : 502;
            throw upstreamError(status, object);
          }
        }
        const delta = this.response.append(data);
        if (this.raw) return { done: false, value: data as T };
        if (delta) return { done: false, value: delta as T };
      }
    } catch (error) {
      await this.close();
      if (this.timedOut)
        throw new DuckAIError(
          504,
          "Duck.ai request timed out",
          {},
          { cause: error },
        );
      if (this.options.signal?.aborted) throw this.options.signal.reason;
      throw error;
    } finally {
      this.reading = false;
    }
  }
  async return(): Promise<IteratorResult<T>> {
    await this.close();
    return { done: true, value: undefined };
  }
  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    if (this.timer) clearTimeout(this.timer);
    this.controller.abort();
    try {
      if (this.startPromise) await this.startPromise.catch(() => {});
      if (this.iterator) await this.iterator.return(undefined);
    } finally {
      this.client.unregister(this);
    }
  }
}

export class Conversation {
  private history: Message[];
  private readonly lock = new Mutex();
  private active = 0;
  constructor(
    private readonly client: DuckAI,
    private readonly options: ChatOptions,
  ) {
    this.history = structuredClone([...(options.messages ?? [])]);
  }
  get messages(): Message[] {
    return structuredClone(this.history);
  }
  clear(): void {
    if (this.active)
      throw new Error("Cannot clear a conversation during a turn");
    this.history = [];
  }
  private async *turn<T>(
    prompt: string,
    options: ChatOptions,
    raw: boolean,
    result: { response?: ChatResponse },
  ): AsyncGenerator<T> {
    this.active++;
    let release: (() => void) | undefined,
      stream: ChatStream<unknown> | undefined;
    try {
      release = await this.lock.acquire(options.signal);
      const merged = { ...this.options, ...options, messages: this.messages };
      stream = raw
        ? this.client.events(prompt, merged)
        : this.client.stream(prompt, merged);
      result.response = stream.response;
      for await (const value of stream) yield value as T;
      if (stream.response.done)
        this.history = [
          ...structuredClone(stream.request!.messages),
          stream.response.assistantMessage,
        ];
    } finally {
      await stream?.close();
      release?.();
      this.active--;
    }
  }
  stream(
    prompt: string,
    options: ChatOptions = {},
  ): ConversationStream<string> {
    return this.make(prompt, options, false);
  }
  events(
    prompt: string,
    options: ChatOptions = {},
  ): ConversationStream<unknown> {
    return this.make(prompt, options, true);
  }
  private make<T>(
    prompt: string,
    options: ChatOptions,
    raw: boolean,
  ): ConversationStream<T> {
    const result: { response?: ChatResponse } = {};
    const controller = new AbortController();
    const caller = options.signal ?? this.options.signal;
    const signal = caller
      ? AbortSignal.any([caller, controller.signal])
      : controller.signal;
    return new ConversationStream(
      this.turn<T>(prompt, { ...options, signal }, raw, result),
      result,
      controller,
    );
  }
  async chat(prompt: string, options: ChatOptions = {}): Promise<ChatResponse> {
    const stream = this.stream(prompt, options);
    try {
      for await (const _ of stream) {
        /* Accumulate response. */
      }
      return stream.response;
    } finally {
      await stream.close();
    }
  }
}
export class ConversationStream<T> implements AsyncIterableIterator<T> {
  constructor(
    private readonly iterator: AsyncGenerator<T>,
    private readonly result: { response?: ChatResponse },
    private readonly controller: AbortController,
  ) {}
  get response(): ChatResponse {
    if (!this.result.response)
      throw new Error("Conversation stream has not started");
    return this.result.response;
  }
  [Symbol.asyncIterator](): AsyncIterableIterator<T> {
    return this;
  }
  next(): Promise<IteratorResult<T>> {
    return this.iterator.next();
  }
  async return(): Promise<IteratorResult<T>> {
    this.controller.abort();
    return this.iterator.return(undefined);
  }
  async close(): Promise<void> {
    this.controller.abort();
    await this.iterator.return(undefined);
  }
  async [Symbol.asyncDispose](): Promise<void> {
    await this.close();
  }
}
