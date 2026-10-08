export {
  DuckAI,
  ChatStream,
  Conversation,
  ConversationStream,
} from "./client.js";
export { Attachment, ChatResponse } from "./types.js";
export type {
  AttachmentInput,
  ChatOptions,
  ChatRequest,
  ClientOptions,
  ContentPart,
  FetchFunction,
  FileInput,
  ImagePart,
  JsonRecord,
  Message,
  ModelInfo,
  PDFPart,
  Settings,
  TextPart,
  TokenProvider,
} from "./types.js";
export {
  DuckAIError,
  AttachmentError,
  ChallengeError,
  RateLimitError,
  IncompleteResponseError,
} from "./errors.js";
export { Model, getModel, listModels } from "./catalog.js";
export { supportedFileExtensions } from "./documents.js";
