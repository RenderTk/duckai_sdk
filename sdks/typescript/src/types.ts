/** Public, runtime-neutral protocol types. Unknown upstream fields are retained. */
export type JsonRecord = Record<string, unknown>;
export interface TextPart {
  type: "text";
  text: string;
}
export interface ImagePart {
  type: "image";
  mimeType: string;
  image: string;
}
export interface PDFPart {
  type: "file";
  mimeType: "application/pdf";
  encoding: "base64";
  filename: string;
  content: string;
}
export type ContentPart = TextPart | ImagePart | PDFPart | JsonRecord;
export interface Message extends JsonRecord {
  role: string;
  content?: string | ContentPart[] | null;
  parts?: JsonRecord[];
}
export interface ModelInfo {
  readonly id: string;
  readonly name: string;
  readonly provider: string;
  readonly description: string;
  readonly accessTier: "free" | "plus" | "pro";
  readonly reasoningEfforts: readonly string[];
  readonly supportsImages: boolean;
  readonly supportsPDF: boolean;
  readonly supportsTools: boolean;
  readonly beta: boolean;
  readonly defaultReasoningEffort: string;
}
export interface ChatOptions {
  model?: string;
  messages?: readonly Message[];
  files?: readonly FileInput[];
  reasoningEffort?: string;
  canUseTools?: boolean;
  canUseApproxLocation?: boolean;
  canDelegateImageGeneration?: boolean;
  canShowGreeting?: boolean;
  durableStream?: JsonRecord;
  signal?: AbortSignal;
}
export interface ChatRequest extends JsonRecord {
  model: string;
  messages: Message[];
  reasoningEffort: string;
  canUseTools: boolean;
  canShowGreeting: boolean;
}
export type FetchFunction = typeof globalThis.fetch;
export interface TokenProvider {
  headers(signal?: AbortSignal): Promise<Record<string, string>>;
  close(): Promise<void>;
}
export interface ClientOptions {
  model?: string;
  settings?: Partial<Settings>;
  headers?: Record<string, string>;
  headless?: boolean;
  browserExecutable?: string;
  browserCDPUrl?: string;
  browserAutoInstall?: boolean;
  timeout?: number;
  fetch?: FetchFunction;
  tokenProvider?: TokenProvider;
}
export interface Settings {
  headers: Record<string, string>;
  autoToken: boolean;
  headless: boolean;
  browserAutoInstall: boolean;
  browserExecutable?: string;
  browserCDPUrl?: string;
  browserChannel?: string;
  browserInstallTimeout: number;
  tokenGenerationTimeout: number;
  timeout: number;
  maxCollectedBytes: number;
  maxImageUploadBytes: number;
  maxImagePixels: number;
  maxDocumentUploadBytes: number;
  maxDocumentTextCharacters: number;
  officeConverterExecutable?: string;
  officeConversionTimeout: number;
}
export interface AttachmentInput {
  data: Uint8Array;
  filename: string;
  mimeType?: string;
}
export type FileInput = string | AttachmentInput | File;

export class ChatResponse {
  text = "";
  events: unknown[] = [];
  done = false;
  private parts: JsonRecord[] = [];
  constructor(public readonly model: string) {}
  get assistantMessage(): Message {
    const message: Message = { role: "assistant", content: this.text };
    if (this.parts.length) message.parts = structuredClone(this.parts);
    return message;
  }
  /** @internal */
  append(event: unknown): string {
    this.events.push(structuredClone(event));
    if (!event || typeof event !== "object" || Array.isArray(event)) return "";
    const data = event as JsonRecord;
    if (Array.isArray(data.parts)) {
      for (const part of data.parts) {
        if (!part || typeof part !== "object" || Array.isArray(part)) continue;
        const value = part as JsonRecord;
        const existing = value.id
          ? this.parts.find((p) => p.id === value.id)
          : undefined;
        if (existing) Object.assign(existing, structuredClone(value));
        else this.parts.push(structuredClone(value));
      }
    }
    if (typeof data.message !== "string") return "";
    this.text += data.message;
    return data.message;
  }
  toString(): string {
    return this.text;
  }
}

export class Attachment implements AttachmentInput {
  readonly data: Uint8Array;
  constructor(
    data: Uint8Array,
    public readonly filename: string,
    public readonly mimeType = "application/octet-stream",
  ) {
    this.data = new Uint8Array(data);
  }
  static fromBytes(
    data: Uint8Array,
    filename: string,
    mimeType?: string,
  ): Attachment {
    return new Attachment(data, filename, mimeType);
  }
  static async fromPath(
    path: string,
    maxBytes = 20 * 1024 * 1024,
  ): Promise<Attachment> {
    if (!Number.isSafeInteger(maxBytes) || maxBytes <= 0)
      throw new TypeError("maxBytes must be a positive safe integer");
    const { open } = await import("node:fs/promises");
    const { basename } = await import("node:path");
    const { AttachmentError } = await import("./errors.js");
    const file = await open(path, "r");
    try {
      const buffer = new Uint8Array(Math.min(maxBytes + 1, 65536));
      const chunks: Uint8Array[] = [];
      let size = 0;
      while (size <= maxBytes) {
        const { bytesRead } = await file.read(
          buffer,
          0,
          Math.min(buffer.length, maxBytes + 1 - size),
          null,
        );
        if (!bytesRead) break;
        size += bytesRead;
        chunks.push(buffer.slice(0, bytesRead));
      }
      if (size > maxBytes)
        throw new AttachmentError(413, "File exceeds the upload limit");
      return new Attachment(Buffer.concat(chunks), basename(path));
    } finally {
      await file.close();
    }
  }
}
