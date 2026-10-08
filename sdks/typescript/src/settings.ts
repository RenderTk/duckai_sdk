import type { ClientOptions, Settings } from "./types.js";

const allowedHeaders = new Set([
  "x-vqd-hash-1",
  "x-vqd-4",
  "x-fe-signals",
  "x-fe-version",
  "x-ddg-journey-id",
  "user-agent",
  "accept-language",
]);
const env = (key: string): string | undefined => process.env[key];
const bool = (key: string, fallback: boolean): boolean => {
  const value = env(key);
  if (value === undefined) return fallback;
  if (["true", "1", "yes"].includes(value.toLowerCase())) return true;
  if (["false", "0", "no"].includes(value.toLowerCase())) return false;
  throw new TypeError(`${key} must be a boolean`);
};
const number = (key: string, fallback: number): number =>
  env(key) === undefined ? fallback : Number(env(key));
const duration = (key: string, fallback: number): number =>
  env(key) === undefined ? fallback : Number(env(key)) * 1000;

export function createSettings(options: ClientOptions): Settings {
  const settings: Settings = {
    headers: JSON.parse(env("DUCKAI_HEADERS_JSON") ?? "{}") as Record<
      string,
      string
    >,
    autoToken: bool("DUCKAI_AUTO_TOKEN", true),
    headless: bool("DUCKAI_BROWSER_HEADLESS", true),
    browserAutoInstall: bool("DUCKAI_BROWSER_AUTO_INSTALL", true),
    browserExecutable: env("DUCKAI_BROWSER_EXECUTABLE"),
    browserCDPUrl: env("DUCKAI_BROWSER_CDP_URL"),
    browserChannel: env("DUCKAI_BROWSER_CHANNEL"),
    browserInstallTimeout: duration("BROWSER_INSTALL_TIMEOUT", 300_000),
    tokenGenerationTimeout: duration("TOKEN_GENERATION_TIMEOUT", 30_000),
    timeout: duration("UPSTREAM_READ_TIMEOUT", 120_000),
    maxCollectedBytes: number("MAX_COLLECTED_BYTES", 8 * 1024 * 1024),
    maxImageUploadBytes: number("MAX_IMAGE_UPLOAD_BYTES", 10 * 1024 * 1024),
    maxImagePixels: number("MAX_IMAGE_PIXELS", 20_000_000),
    maxDocumentUploadBytes: number(
      "MAX_DOCUMENT_UPLOAD_BYTES",
      20 * 1024 * 1024,
    ),
    maxDocumentTextCharacters: number("MAX_DOCUMENT_TEXT_CHARACTERS", 16_000),
    officeConverterExecutable: env("OFFICE_CONVERTER_EXECUTABLE"),
    officeConversionTimeout: duration("OFFICE_CONVERSION_TIMEOUT", 60_000),
    ...options.settings,
  };
  for (const [key, value] of Object.entries({
    headers: options.headers,
    headless: options.headless,
    browserExecutable: options.browserExecutable,
    browserCDPUrl: options.browserCDPUrl,
    browserAutoInstall: options.browserAutoInstall,
    timeout: options.timeout,
  })) {
    if (value !== undefined) Object.assign(settings, { [key]: value });
  }
  for (const [key, value] of Object.entries(settings)) {
    if (typeof value === "number" && (!Number.isFinite(value) || value <= 0)) {
      throw new TypeError(`${key} must be a positive finite number`);
    }
  }
  for (const key of [
    "maxCollectedBytes",
    "maxImageUploadBytes",
    "maxImagePixels",
    "maxDocumentUploadBytes",
    "maxDocumentTextCharacters",
  ] as const) {
    if (!Number.isSafeInteger(settings[key]))
      throw new TypeError(`${key} must be an integer`);
  }
  if (settings.maxDocumentTextCharacters > 16_000)
    throw new TypeError("Document text limit cannot exceed 16,000");
  if (
    !settings.headers ||
    Array.isArray(settings.headers) ||
    typeof settings.headers !== "object"
  )
    throw new TypeError("headers must be an object");
  const headers: Record<string, string> = {};
  for (const [key, value] of Object.entries(settings.headers)) {
    if (
      !allowedHeaders.has(key.toLowerCase()) ||
      typeof value !== "string" ||
      /[\r\n]/.test(value)
    ) {
      throw new TypeError("Unsupported or invalid Duck.ai request header");
    }
    headers[key.toLowerCase()] = value;
  }
  settings.headers = headers;
  return settings;
}
