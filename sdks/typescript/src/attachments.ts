import { PDFDocument, EncryptedPDFError } from "pdf-lib";
import sharp from "sharp";
import {
  Attachment,
  type ChatRequest,
  type ContentPart,
  type FileInput,
  type Settings,
} from "./types.js";
import { AttachmentError } from "./errors.js";
import { getModel, nativeProfile } from "./catalog.js";
import {
  DOCUMENT_PREFIX,
  prepareDocument,
  supportedFileExtensions,
} from "./documents.js";

const PDF_BYTES = 5 * 1024 * 1024;
const imageMimes: Record<string, string> = {
  png: "image/png",
  jpeg: "image/jpeg",
  webp: "image/webp",
  gif: "image/gif",
};
function fail(code: string, message: string, status = 422): never {
  throw new AttachmentError(status, { error: code, message });
}
function profile(model: string) {
  const value = nativeProfile(model);
  if (!value)
    fail(
      "attachments_model_unsupported",
      `${model} has no known native attachment support. Documents can still be sent as text.`,
    );
  return value;
}
function base64(value: unknown): Uint8Array {
  if (
    typeof value !== "string" ||
    value.length % 4 ||
    !/^[A-Za-z0-9+/]*={0,2}$/.test(value)
  )
    fail("invalid_attachment", "Invalid base64 attachment.");
  return Buffer.from(value, "base64");
}
async function checkPDF(data: Uint8Array): Promise<void> {
  if (data.byteLength > PDF_BYTES)
    fail("pdf_too_large", "Anonymous Duck.ai PDFs must fit within 5 MiB.", 413);
  if (Buffer.from(data.subarray(0, 5)).toString() !== "%PDF-")
    fail("invalid_pdf", "The file is not a PDF.");
  try {
    const pdf = await PDFDocument.load(data);
    const pages = pdf.getPageCount();
    if (!pages) fail("empty_pdf", "The PDF has no pages.");
    if (pages > 15)
      fail("pdf_page_limit", "Duck.ai supports up to 15 pages per PDF.");
  } catch (error) {
    if (error instanceof AttachmentError) throw error;
    if (error instanceof EncryptedPDFError)
      fail("encrypted_pdf", "Upload an unencrypted PDF.");
    fail("invalid_pdf", "The PDF could not be read.");
  }
}
async function inspectImage(
  data: Uint8Array,
  settings: Settings,
  declaredMime?: string,
) {
  if (data.byteLength > settings.maxImageUploadBytes)
    fail("image_too_large", "Image exceeds the upload byte limit.", 413);
  try {
    const metadata = await sharp(data, {
      limitInputPixels: settings.maxImagePixels,
      animated: false,
    }).metadata();
    const detected = imageMimes[metadata.format ?? ""];
    if (!detected)
      fail(
        "unsupported_file_type",
        "Supported images: PNG, JPEG, WebP and GIF.",
        415,
      );
    if (declaredMime && declaredMime !== detected)
      fail(
        "file_type_mismatch",
        "Image contents do not match their MIME type.",
        415,
      );
    return metadata;
  } catch (error) {
    if (error instanceof AttachmentError) throw error;
    if (String(error).toLowerCase().includes("pixel limit"))
      fail("image_pixel_limit", "Image exceeds the pixel limit.", 413);
    fail("invalid_image", "The image could not be decoded.");
  }
}
export function attachmentLimits(model: string, settings: Settings) {
  const p = nativeProfile(model);
  const info = getModel(model);
  return Object.freeze({
    model,
    documentTextSupported: true,
    documentUploadBytes: settings.maxDocumentUploadBytes,
    documentMessageTextCharacters: settings.maxDocumentTextCharacters,
    supportedFileExtensions: supportedFileExtensions(),
    imageSupported: !!p,
    pdfSupported: !!p?.supports_pdf,
    imageMimeTypes: p ? Object.values(imageMimes) : [],
    imagesPerMessage: p?.images_per_message ?? 0,
    imagesPerConversation: p?.images_per_conversation ?? 0,
    imageMaxDimension: p?.max_dimension ?? 0,
    pdfsPerConversation: p?.supports_pdf ? 3 : 0,
    pdfBytesPerFileAndConversation: PDF_BYTES,
    pdfPagesPerFile: 15,
    imageMessageTextCharacters: 4500,
    maxImageUploadBytes: settings.maxImageUploadBytes,
    maxImagePixels: settings.maxImagePixels,
    modelKnown: info !== undefined,
  });
}
async function read(file: FileInput, settings: Settings): Promise<Attachment> {
  const max = Math.max(
    PDF_BYTES,
    settings.maxImageUploadBytes,
    settings.maxDocumentUploadBytes,
  );
  if (typeof file === "string") return await Attachment.fromPath(file, max);
  if (typeof File !== "undefined" && file instanceof File) {
    if (file.size > max)
      fail("attachment_too_large", "File exceeds the upload limit.", 413);
    return new Attachment(
      new Uint8Array(await file.arrayBuffer()),
      file.name,
      file.type || undefined,
    );
  }
  if (
    !("data" in file) ||
    !(file.data instanceof Uint8Array) ||
    typeof file.filename !== "string"
  )
    throw new TypeError(
      "Files must be paths, File objects or Attachment instances",
    );
  return new Attachment(file.data, file.filename, file.mimeType);
}
export async function prepareFiles(
  files: readonly FileInput[],
  model: string,
  settings: Settings,
  signal?: AbortSignal,
): Promise<ContentPart[]> {
  if (!Array.isArray(files))
    throw new TypeError(
      "Pass a sequence of files, for example files: ['report.docx']",
    );
  const parts: ContentPart[] = [];
  for (const file of files) {
    signal?.throwIfAborted();
    const a = await read(file, settings);
    if (!a.data.byteLength)
      fail("empty_attachment", "Files must not be empty.");
    const document = await prepareDocument(
      a.data,
      a.filename,
      settings,
      signal,
    );
    if (document) {
      parts.push(document);
      continue;
    }
    const p = profile(model);
    const mime = a.mimeType
      .toLowerCase()
      .split(";")[0]!
      .trim()
      .replace("image/jpg", "image/jpeg");
    const generic = !mime || mime === "application/octet-stream";
    if (
      mime === "application/pdf" ||
      (generic && Buffer.from(a.data.subarray(0, 5)).toString() === "%PDF-")
    ) {
      if (!p.supports_pdf)
        fail(
          "pdf_model_unsupported",
          `${model} does not support PDF attachments.`,
        );
      await checkPDF(a.data);
      parts.push({
        type: "file",
        mimeType: "application/pdf",
        encoding: "base64",
        filename:
          a.filename
            .replaceAll("\\", "/")
            .split("/")
            .at(-1)!
            .replace(/[\x00-\x1f\x7f]/g, "") || "document.pdf",
        content: Buffer.from(a.data).toString("base64"),
      });
    } else {
      if (!generic && !Object.values(imageMimes).includes(mime))
        fail("unsupported_file_type", "Unsupported attachment type.", 415);
      await inspectImage(a.data, settings, generic ? undefined : mime);
      const image = await sharp(a.data, {
        limitInputPixels: settings.maxImagePixels,
        animated: false,
      })
        .rotate()
        .resize({
          width: p.max_dimension,
          height: p.max_dimension,
          fit: "inside",
          withoutEnlargement: true,
        })
        .webp({ quality: 90 })
        .toBuffer();
      parts.push({
        type: "image",
        mimeType: "image/webp",
        image: "data:image/webp;base64," + image.toString("base64"),
      });
    }
  }
  parts.sort((a, b) => Number(a.type === "file") - Number(b.type === "file"));
  signal?.throwIfAborted();
  return parts;
}
export async function validatePayload(
  body: ChatRequest,
  settings: Settings,
): Promise<void> {
  const messages = body.messages.filter(
    (m) => m.role === "user" && Array.isArray(m.content),
  );
  let pdfs = 0,
    pdfBytes = 0,
    images = 0;
  for (const message of messages) {
    const parts = message.content as ContentPart[];
    if (parts.some((p) => !p || typeof p !== "object" || Array.isArray(p)))
      throw new TypeError("Content parts must be objects");
    const texts = parts.flatMap((p) =>
      p.type === "text" && "text" in p && typeof p.text === "string"
        ? [p.text]
        : [],
    );
    const textLength = texts.join("").length;
    if (
      texts.some((t) => t.startsWith(DOCUMENT_PREFIX)) &&
      textLength > settings.maxDocumentTextCharacters
    )
      fail(
        "document_text_limit",
        "The prompt and documents exceed the text limit. Attach smaller sections or fewer files.",
        413,
      );
    let messageImages = 0;
    for (const part of parts) {
      if (part.type === "file") {
        if (
          !profile(body.model).supports_pdf ||
          part.mimeType !== "application/pdf"
        )
          fail(
            "pdf_model_unsupported",
            "This model does not support the PDF attachment.",
          );
        if (part.encoding !== "base64")
          fail("invalid_attachment", "PDF encoding must be base64.");
        if (
          typeof part.content === "string" &&
          part.content.length > Math.ceil(PDF_BYTES / 3) * 4
        )
          fail("pdf_too_large", "PDF exceeds the byte limit.", 413);
        const bytes = base64(part.content);
        await checkPDF(bytes);
        pdfs++;
        pdfBytes += bytes.length;
      } else if (part.type === "image") {
        const prefix = `data:${String(part.mimeType)};base64,`;
        if (
          !Object.values(imageMimes).includes(String(part.mimeType)) ||
          typeof part.image !== "string" ||
          !part.image.startsWith(prefix)
        )
          fail(
            "invalid_attachment",
            "Images must use a matching base64 data URL.",
          );
        if (part.image.length > (settings.maxImageUploadBytes * 4) / 3 + 256)
          fail("image_too_large", "Image exceeds the upload limit.", 413);
        await inspectImage(
          base64(part.image.slice(prefix.length)),
          settings,
          String(part.mimeType),
        );
        messageImages++;
      }
    }
    images += messageImages;
    if (messageImages && messageImages > profile(body.model).images_per_message)
      fail("image_count_limit", "Duck.ai supports up to 3 images per message.");
    if (messageImages) {
      profile(body.model);
      if (textLength > 4500)
        fail(
          "image_text_limit",
          "Messages with images support up to 4,500 text characters.",
        );
    }
  }
  if (images && images > profile(body.model).images_per_conversation)
    fail("image_conversation_limit", "Too many images in this conversation.");
  if (pdfs > 3)
    fail("pdf_count_limit", "Duck.ai supports up to 3 PDFs per conversation.");
  if (pdfBytes > PDF_BYTES)
    fail(
      "pdf_combined_size",
      "All conversation PDFs must fit within 5 MiB combined.",
      413,
    );
}
