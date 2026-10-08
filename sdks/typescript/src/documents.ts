/** Bounded local Office/Workspace readers. No macros, link fetching or formula execution. */
import { extname, basename, dirname, join, posix, delimiter } from "node:path";
import {
  access,
  mkdtemp,
  readFile,
  writeFile,
  rm,
  stat,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { pathToFileURL } from "node:url";
import { XMLParser, XMLValidator } from "fast-xml-parser";
import { fromBuffer, type Entry, type ZipFile } from "yauzl";
import { convert as htmlToText } from "html-to-text";
import { simpleParser } from "mailparser";
import iconv from "iconv-lite";
import type { Settings, TextPart } from "./types.js";
import { AttachmentError } from "./errors.js";
import { packageRequire, runProcess } from "./process.js";

export const DOCUMENT_PREFIX = "[Attached document: ";
const word = new Set([".docx", ".docm", ".dotx", ".dotm"]);
const sheets = new Set([
  ".xlsx",
  ".xlsm",
  ".xltx",
  ".xltm",
  ".xlam",
  ".xls",
  ".xlt",
  ".xla",
  ".xlsb",
]);
const slides = new Set([".pptx", ".pptm", ".ppsx", ".ppsm", ".potx", ".potm"]);
const odf = new Set([
  ".odt",
  ".ott",
  ".ods",
  ".ots",
  ".odp",
  ".otp",
  ".odg",
  ".otg",
]);
const flat = new Set([".fodt", ".fods", ".fodp", ".fodg"]);
const visio = new Set([
  ".vsdx",
  ".vsdm",
  ".vstx",
  ".vstm",
  ".vssx",
  ".vssm",
  ".vdx",
]);
const text = new Set([
  ".txt",
  ".md",
  ".csv",
  ".tsv",
  ".rtf",
  ".html",
  ".htm",
  ".xhtml",
  ".mht",
  ".mhtml",
  ".json",
  ".xml",
  ".ics",
  ".vcf",
  ".dif",
  ".slk",
]);
const mail = new Set([".eml", ".msg", ".oft"]);
const legacy: Record<string, string> = {
  ".doc": "docx",
  ".dot": "docx",
  ".ppt": "pptx",
  ".pps": "pptx",
  ".pot": "pptx",
  ".pub": "pdf",
  ".vsd": "pdf",
  ".vss": "pdf",
  ".vst": "pdf",
};
export const exportGuidance: Readonly<Record<string, string>> = Object.freeze(
  Object.fromEntries([
    ...[".gdoc", ".gsheet", ".gslides", ".gdraw", ".gform"].map((s) => [
      s,
      "Google shortcuts contain links. Export as DOCX, XLSX, PPTX, OpenDocument, PDF, CSV or HTML first.",
    ]),
    ...[".one", ".onepkg"].map((s) => [
      s,
      "Export OneNote as PDF or DOCX first.",
    ]),
    ...[".mdb", ".accdb", ".accde", ".mde"].map((s) => [
      s,
      "Export Access tables/query results as XLSX or CSV first.",
    ]),
    ...[".pst", ".ost"].map((s) => [
      s,
      "Export individual Outlook messages as EML or MSG first.",
    ]),
    [".mpp", "Export Project as XLSX, CSV or PDF first."],
    [".pages", "Export Pages as DOCX or PDF first."],
    [".numbers", "Export Numbers as XLSX or CSV first."],
    [".key", "Export Keynote as PPTX or PDF first."],
  ]),
);
const documentExtensions = new Set([
  ...word,
  ...sheets,
  ...slides,
  ...odf,
  ...flat,
  ...visio,
  ...text,
  ...mail,
  ...Object.keys(legacy),
  ".epub",
  ".zip",
  ".svg",
]);
export function supportedFileExtensions(): readonly string[] {
  return Object.freeze(
    [
      ...documentExtensions,
      ".pdf",
      ".png",
      ".jpg",
      ".jpeg",
      ".webp",
      ".gif",
    ].sort(),
  );
}
function fail(code: string, message: string, status = 422): never {
  throw new AttachmentError(status, { error: code, message });
}
class Output {
  readonly parts: string[] = [];
  private size = 0;
  constructor(readonly limit: number) {}
  add(value: string): void {
    value = value.replace(/^[\r\n]+|[\r\n]+$/g, "");
    if (!value.trim()) return;
    this.size += value.length + 1;
    if (this.size > this.limit)
      fail(
        "document_text_limit",
        "Extracted text is too long. Attach smaller sections or fewer rows/slides.",
        413,
      );
    this.parts.push(value);
  }
  toString(): string {
    return this.parts.join("\n");
  }
}
function decode(data: Uint8Array): string {
  if (data[0] === 255 && data[1] === 254)
    return new TextDecoder("utf-16le", { fatal: true }).decode(data);
  if (data[0] === 254 && data[1] === 255)
    return new TextDecoder("utf-16be", { fatal: true }).decode(data);
  return new TextDecoder("utf-8", { fatal: true }).decode(data);
}

async function unzip(data: Uint8Array): Promise<Map<string, Uint8Array>> {
  const zip = await new Promise<ZipFile>((resolve, reject) =>
    fromBuffer(
      Buffer.from(data),
      { lazyEntries: true, autoClose: false },
      (error, zip) => (error ? reject(error) : resolve(zip!)),
    ),
  );
  try {
    const entries = await new Promise<Entry[]>((resolve, reject) => {
      const entries: Entry[] = [];
      let total = 0;
      zip.once("error", reject);
      zip.once("end", () => resolve(entries));
      zip.on("entry", (entry: Entry) => {
        entries.push(entry);
        total += entry.uncompressedSize;
        if (entries.length > 2000 || total > 64 * 1024 * 1024) {
          reject(
            new AttachmentError(413, {
              error: "document_archive_limit",
              message: "Document archive exceeds safe expansion limits.",
            }),
          );
          return;
        }
        if (entry.generalPurposeBitFlag & 1) {
          reject(
            new AttachmentError(422, {
              error: "encrypted_document",
              message: "Export an unencrypted document.",
            }),
          );
          return;
        }
        zip.readEntry();
      });
      zip.readEntry();
    });
    const files = new Map<string, Uint8Array>();
    for (const entry of entries) {
      if (entry.fileName.endsWith("/")) continue;
      const stream = await new Promise<NodeJS.ReadableStream>(
        (resolve, reject) =>
          zip.openReadStream(entry, (error, stream) =>
            error ? reject(error) : resolve(stream!),
          ),
      );
      const bytes = await new Promise<Uint8Array>((resolve, reject) => {
        const chunks: Buffer[] = [];
        let size = 0;
        stream.on("data", (chunk: Buffer) => {
          size += chunk.length;
          if (size > entry.uncompressedSize) {
            reject(
              new AttachmentError(
                413,
                "Archive member exceeded its declared size",
              ),
            );
            (stream as import("node:stream").Readable).destroy();
          } else chunks.push(chunk);
        });
        stream.once("error", reject);
        stream.once("end", () => resolve(Buffer.concat(chunks)));
      });
      files.set(entry.fileName, bytes);
    }
    return files;
  } finally {
    zip.close();
  }
}

type XMLNode = Record<string, unknown>;
function kind(node: XMLNode): string {
  return Object.keys(node).find((k) => k !== ":@" && k !== "#text") ?? "#text";
}
function local(name: string): string {
  return name.split(":").at(-1)!;
}
function children(node: XMLNode): XMLNode[] {
  const value = node[kind(node)];
  return Array.isArray(value) ? (value as XMLNode[]) : [];
}
function attrs(node: XMLNode): Record<string, string> {
  return (node[":@"] ?? {}) as Record<string, string>;
}
function attr(node: XMLNode, name: string, qualified = false): string {
  return (
    Object.entries(attrs(node)).find(
      ([k]) =>
        local(k.replace(/^@_/, "")) === name && (!qualified || k.includes(":")),
    )?.[1] ?? ""
  );
}
function* walk(nodes: XMLNode[]): Generator<XMLNode> {
  for (const node of nodes) {
    yield node;
    yield* walk(children(node));
  }
}
function content(nodes: XMLNode[]): string {
  return nodes
    .map((n) =>
      kind(n) === "#text" ? String(n["#text"] ?? "") : content(children(n)),
    )
    .join("");
}
function xml(data: Uint8Array): XMLNode[] {
  const value = decode(data);
  if (
    /<!DOCTYPE|<!ENTITY/i.test(value) ||
    XMLValidator.validate(value) !== true
  )
    fail("invalid_document", "Invalid XML or forbidden document entities/DTD.");
  return new XMLParser({
    preserveOrder: true,
    ignoreAttributes: false,
    trimValues: false,
    parseTagValue: false,
    parseAttributeValue: false,
  }).parse(value) as XMLNode[];
}
function get(files: Map<string, Uint8Array>, name: string): Uint8Array {
  const value = files.get(name);
  if (!value) fail("invalid_document", `Missing document part: ${name}`);
  return value;
}
function relationMap(
  files: Map<string, Uint8Array>,
  path: string,
): Map<string, string> {
  const data = files.get(path);
  if (!data) return new Map();
  return new Map(
    [...walk(xml(data))]
      .filter(
        (n) =>
          local(kind(n)) === "Relationship" &&
          attr(n, "TargetMode") !== "External",
      )
      .map((n) => [attr(n, "Id"), attr(n, "Target")]),
  );
}
function target(base: string, value: string): string {
  return posix
    .normalize(posix.join(posix.dirname(base), value))
    .replace(/^\//, "");
}
function paragraph(nodes: XMLNode[]): string {
  return [...walk(nodes)]
    .map((n) =>
      local(kind(n)) === "t"
        ? content(children(n))
        : local(kind(n)) === "tab"
          ? "\t"
          : ["br", "cr"].includes(local(kind(n)))
            ? "\n"
            : "",
    )
    .join("");
}
function odText(nodes: XMLNode[]): string {
  return nodes
    .map((n) => {
      const name = local(kind(n));
      if (name === "#text") return String(n["#text"] ?? "");
      if (name === "s") {
        const count = Number(attr(n, "c") || 1);
        if (count > 16_000)
          fail(
            "document_text_limit",
            "Document spacing exceeds the text limit.",
            413,
          );
        return " ".repeat(count);
      }
      if (name === "tab") return "\t";
      if (name === "line-break") return "\n";
      return odText(children(n));
    })
    .join("");
}
function openDocument(nodes: XMLNode[], out: Output): void {
  for (const node of nodes) {
    const name = local(kind(node));
    if (["p", "h"].includes(name)) out.add(odText(children(node)));
    else if (name === "table-row") {
      const cells: string[] = [];
      for (const cell of children(node).filter((n) =>
        ["table-cell", "covered-table-cell"].includes(local(kind(n))),
      )) {
        const repeats = Number(attr(cell, "number-columns-repeated") || 1);
        if (repeats > 200_000)
          fail(
            "document_cell_limit",
            "Export a smaller cell range as CSV.",
            413,
          );
        const value =
          [...walk(children(cell))]
            .filter((n) => local(kind(n)) === "p")
            .map((n) => odText(children(n)))
            .join(" ") ||
          attr(cell, "value") ||
          attr(cell, "string-value") ||
          attr(cell, "date-value") ||
          attr(cell, "boolean-value");
        for (let i = 0; i < repeats; i++) cells.push(value);
      }
      const row = cells.join("\t").trimEnd();
      const repeat = Number(attr(node, "number-rows-repeated") || 1);
      if (row && repeat > 1000)
        fail(
          "document_cell_limit",
          "Export a smaller repeated row range as CSV.",
          413,
        );
      if (row) for (let i = 0; i < repeat; i++) out.add(row);
    } else {
      if (["table", "page"].includes(name))
        out.add(
          `${name === "table" ? "Sheet/table" : "Slide/page"}: ${attr(node, "name")}`,
        );
      openDocument(children(node), out);
    }
  }
}
function html(value: string): string {
  return htmlToText(value, {
    wordwrap: false,
    selectors: [
      { selector: "a", options: { ignoreHref: true } },
      { selector: "img", format: "skip" },
    ],
  });
}

function rtf(data: Uint8Array): string {
  const value = Buffer.from(data).toString("latin1");
  const codec = /\\ansicpg(\d+)/.exec(value)?.[1] ?? "1252";
  let result = "",
    state = { skip: false, uc: 1 },
    fallback = 0;
  const stack: (typeof state)[] = [];
  for (let i = 0; i < value.length; i++) {
    const c = value[i]!;
    if (c === "{") {
      stack.push({ ...state });
      continue;
    }
    if (c === "}") {
      state = stack.pop() ?? state;
      continue;
    }
    if (c === "\\") {
      const next = value[++i];
      if (["\\", "{", "}"].includes(next ?? "")) {
        if (fallback) fallback--;
        else if (!state.skip) result += next;
        continue;
      }
      if (next === "*") {
        state.skip = true;
        continue;
      }
      if (next === "'") {
        const byte = Number.parseInt(value.slice(i + 1, i + 3), 16);
        i += 2;
        if (fallback) fallback--;
        else if (!state.skip && Number.isFinite(byte))
          result += iconv.decode(Buffer.from([byte]), "cp" + codec);
        continue;
      }
      const match = /^([a-z]+)(-?\d+)? ?/.exec(value.slice(i));
      if (!match) {
        if (!state.skip && next === "~") result += " ";
        continue;
      }
      i += match[0].length - 1;
      const name = match[1],
        number = Number(match[2] ?? 0);
      if (
        [
          "fonttbl",
          "colortbl",
          "stylesheet",
          "info",
          "pict",
          "object",
          "fldinst",
        ].includes(name!)
      )
        state.skip = true;
      else if (name === "uc") state.uc = Math.max(0, Math.min(number, 32));
      else if (!state.skip && name === "u") {
        result += String.fromCharCode(number & 65535);
        fallback = state.uc;
      } else if (!state.skip && ["par", "line"].includes(name!)) result += "\n";
      else if (!state.skip && name === "tab") result += "\t";
    } else if (c !== "\r" && c !== "\n") {
      if (fallback) fallback--;
      else if (!state.skip)
        result += iconv.decode(Buffer.from([c.charCodeAt(0)]), "cp" + codec);
    }
  }
  return result;
}

async function executable(settings: Settings): Promise<string | undefined> {
  const paths = settings.officeConverterExecutable
    ? [settings.officeConverterExecutable]
    : [
        ...(process.env.PATH ?? "")
          .split(delimiter)
          .map((p) =>
            join(p, process.platform === "win32" ? "soffice.exe" : "soffice"),
          ),
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
      ];
  for (const path of paths) {
    try {
      await access(path);
      return path;
    } catch {
      /* Try next path. */
    }
  }
  return undefined;
}
async function convertLegacy(
  data: Uint8Array,
  suffix: string,
  settings: Settings,
  out: Output,
  signal?: AbortSignal,
): Promise<void> {
  const bin = await executable(settings);
  if (!bin)
    fail(
      "office_converter_required",
      `Reading ${suffix} requires LibreOffice; install it or export as DOCX, PPTX or PDF.`,
      415,
    );
  const root = await mkdtemp(join(tmpdir(), "duckai-office-"));
  try {
    const { mkdir } = await import("node:fs/promises");
    const profile = join(root, "profile");
    await mkdir(profile);
    await writeFile(
      join(profile, "registrymodifications.xcu"),
      '<oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item><item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item></oor:items>',
    );
    const source = join(root, "document" + suffix);
    await writeFile(source, data);
    const format = legacy[suffix]!;
    await runProcess(
      bin,
      [
        `-env:UserInstallation=${pathToFileURL(profile).href}`,
        "--headless",
        "--nologo",
        "--nodefault",
        "--nofirststartwizard",
        "--convert-to",
        format,
        "--outdir",
        root,
        source,
      ],
      settings.officeConversionTimeout,
      signal,
    );
    const destination = join(root, "document." + format);
    if ((await stat(destination)).size > settings.maxDocumentUploadBytes)
      fail(
        "document_too_large",
        "Converted document exceeds the upload limit.",
        413,
      );
    const converted = await readFile(destination);
    if (format === "pdf") {
      const { PDFParse } = await import("pdf-parse");
      const reader = new PDFParse({ data: new Uint8Array(converted) });
      try {
        out.add((await reader.getText()).text);
      } finally {
        await reader.destroy();
      }
    } else await readDocument(converted, "." + format, settings, out, signal);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

async function readDocument(
  data: Uint8Array,
  suffix: string,
  settings: Settings,
  out: Output,
  signal?: AbortSignal,
): Promise<void> {
  signal?.throwIfAborted();
  const files =
    data[0] === 80 && data[1] === 75
      ? await unzip(data)
      : new Map<string, Uint8Array>();
  if (word.has(suffix)) {
    const visit = (nodes: XMLNode[]): void => {
      for (const node of nodes) {
        if (local(kind(node)) === "p") out.add(paragraph(children(node)));
        else if (local(kind(node)) === "tr")
          out.add(
            children(node)
              .filter((n) => local(kind(n)) === "tc")
              .map((cell) =>
                [...walk(children(cell))]
                  .filter((n) => local(kind(n)) === "p")
                  .map((p) => paragraph(children(p)))
                  .join(" / "),
              )
              .join("\t"),
          );
        else visit(children(node));
      }
    };
    visit(xml(get(files, "word/document.xml")));
    for (const [name, value] of files)
      if (
        /^word\/(header|footer|footnotes|endnotes|comments).*\.xml$/.test(name)
      ) {
        out.add(`[${basename(name, ".xml")}]`);
        visit(xml(value));
      }
  } else if (sheets.has(suffix)) {
    const XLSX = packageRequire("xlsx") as typeof import("xlsx");
    // Check OOXML dimensions/cell coordinates before allocating rectangular ranges.
    for (const [name, value] of files)
      if (/^xl\/worksheets\/.*\.xml$/.test(name)) {
        for (const node of walk(xml(value)))
          if (["dimension", "c"].includes(local(kind(node)))) {
            const reference = attr(node, "ref") || attr(node, "r");
            if (reference) {
              const range = XLSX.utils.decode_range(reference);
              if ((range.e.r + 1) * (range.e.c + 1) > 200_000)
                fail(
                  "document_cell_limit",
                  "Export a smaller sheet range as CSV.",
                  413,
                );
            }
          }
      }
    const workbook = XLSX.read(data, {
      type: "array",
      cellDates: true,
      cellFormula: true,
    });
    for (const name of workbook.SheetNames) {
      const sheet = workbook.Sheets[name]!;
      const reference = sheet["!ref"];
      if (!reference) continue;
      const range = XLSX.utils.decode_range(reference);
      if ((range.e.r - range.s.r + 1) * (range.e.c - range.s.c + 1) > 200_000)
        fail(
          "document_cell_limit",
          "Export a smaller sheet range as CSV.",
          413,
        );
      out.add(`Sheet: ${name}`);
      out.add(`Columns start at ${range.s.c + 1}; values are tab-separated.`);
      for (let r = range.s.r; r <= range.e.r; r++) {
        const cells: string[] = [];
        for (let c = range.s.c; c <= range.e.c; c++) {
          const cell = sheet[XLSX.utils.encode_cell({ r, c })];
          const value = cell?.v;
          cells.push(
            value instanceof Date
              ? value.toISOString()
              : String(value ?? "").replace(/[\t\n]/g, " "),
          );
        }
        if (cells.some(Boolean)) out.add(`Row ${r + 1}: ${cells.join("\t")}`);
      }
      let heading = false;
      for (const [address, cell] of Object.entries(sheet))
        if (
          !address.startsWith("!") &&
          cell &&
          typeof cell === "object" &&
          "f" in cell &&
          cell.f
        ) {
          if (!heading) {
            out.add(`Formulas (not recalculated), sheet ${name}`);
            heading = true;
          }
          out.add(`${address}: =${String(cell.f)}`);
        }
    }
  } else if (slides.has(suffix)) {
    const presentation = "ppt/presentation.xml";
    const rels = relationMap(files, "ppt/_rels/presentation.xml.rels");
    let index = 0;
    for (const node of walk(xml(get(files, presentation))))
      if (local(kind(node)) === "sldId") {
        const part = rels.get(attr(node, "id", true));
        if (!part) continue;
        const path = target(presentation, part);
        out.add(`Slide ${++index}`);
        for (const p of walk(xml(get(files, path))))
          if (local(kind(p)) === "p") out.add(paragraph(children(p)));
        const notes = relationMap(
          files,
          `${posix.dirname(path)}/_rels/${posix.basename(path)}.rels`,
        );
        for (const destination of notes.values()) {
          const resolved = target(path, destination);
          if (resolved.startsWith("ppt/notesSlides/") && files.has(resolved)) {
            out.add("Speaker notes");
            for (const p of walk(xml(get(files, resolved))))
              if (local(kind(p)) === "p") out.add(paragraph(children(p)));
          }
        }
      }
  } else if (odf.has(suffix) || flat.has(suffix))
    openDocument(xml(odf.has(suffix) ? get(files, "content.xml") : data), out);
  else if (visio.has(suffix)) {
    const pages = files.size
      ? [...files]
          .filter(([name]) =>
            /^visio\/(pages\/page|masters\/master).*\.xml$/.test(name),
          )
          .map(([, value]) => value)
      : [data];
    for (const [index, page] of pages.entries()) {
      out.add(`Diagram page ${index + 1}`);
      for (const node of walk(xml(page)))
        if (local(kind(node)) === "Text") out.add(content(children(node)));
    }
  } else if (legacy[suffix])
    await convertLegacy(data, suffix, settings, out, signal);
  else if ([".eml", ".mht", ".mhtml"].includes(suffix)) {
    const message = await simpleParser(Buffer.from(data));
    if (suffix === ".eml") {
      for (const [title, value] of [
        ["Subject", message.subject],
        ["From", message.from?.text],
        [
          "To",
          Array.isArray(message.to)
            ? message.to.map((v) => v.text).join(", ")
            : message.to?.text,
        ],
        ["Date", message.date?.toISOString()],
      ])
        if (value) out.add(`${title}: ${value}`);
    }
    out.add(
      suffix === ".eml" && message.text
        ? message.text
        : message.html
          ? html(message.html)
          : (message.text ?? ""),
    );
    for (const file of message.attachments)
      if (file.filename)
        out.add(`Embedded attachment (not read): ${file.filename}`);
  } else if ([".msg", ".oft"].includes(suffix)) {
    const module = packageRequire("@kenjiuno/msgreader") as {
      default: new (data: Uint8Array) => {
        getFileData(): {
          subject?: string;
          senderName?: string;
          body?: string;
          bodyHTML?: Uint8Array;
          error?: string;
        };
      };
    };
    const message = new module.default(data).getFileData();
    if (message.error)
      fail(
        "invalid_document",
        "Unreadable Outlook message. Export as EML or PDF.",
      );
    if (message.subject) out.add(`Subject: ${message.subject}`);
    if (message.senderName) out.add(`From: ${message.senderName}`);
    if (message.body) out.add(`Body: ${message.body}`);
    else if (message.bodyHTML) out.add(html(decode(message.bodyHTML)));
    else
      fail(
        "outlook_body_unsupported",
        "This Outlook message has no plain/HTML body. Export as EML or PDF.",
      );
  } else if (suffix === ".epub") {
    const opf = [...walk(xml(get(files, "META-INF/container.xml")))].find(
      (n) => local(kind(n)) === "rootfile",
    );
    if (!opf) fail("invalid_document", "Missing EPUB rootfile");
    const path = attr(opf, "full-path");
    const nodes = [...walk(xml(get(files, path)))];
    const manifest = new Map(
      nodes
        .filter((n) => local(kind(n)) === "item")
        .map((n) => [attr(n, "id"), target(path, attr(n, "href"))]),
    );
    for (const node of nodes)
      if (local(kind(node)) === "itemref")
        out.add(
          html(decode(get(files, manifest.get(attr(node, "idref")) ?? ""))),
        );
  } else if (suffix === ".zip") {
    for (const [name, value] of files)
      if (
        !name.startsWith("__MACOSX/") &&
        [".html", ".htm", ".txt", ".md", ".csv", ".tsv"].includes(
          extname(name).toLowerCase(),
        )
      ) {
        out.add(`Exported file: ${name}`);
        out.add(
          [".html", ".htm"].includes(extname(name).toLowerCase())
            ? html(decode(value))
            : decode(value),
        );
      }
  } else if (suffix === ".svg") {
    for (const node of walk(xml(data)))
      if (["text", "title", "desc"].includes(local(kind(node))))
        out.add(content(children(node)));
  } else if (suffix === ".rtf") out.add(rtf(data));
  else if ([".html", ".htm", ".xhtml"].includes(suffix))
    out.add(html(decode(data)));
  else if (suffix === ".json")
    out.add(JSON.stringify(JSON.parse(decode(data)), null, 2));
  else if (suffix === ".xml") out.add(content(xml(data)));
  else {
    const value = decode(data);
    if (value.includes("\0"))
      fail("invalid_document", "Binary data is not a text export.");
    out.add(value);
  }
  signal?.throwIfAborted();
}

export async function prepareDocument(
  data: Uint8Array,
  filename: string,
  settings: Settings,
  signal?: AbortSignal,
): Promise<TextPart | undefined> {
  const suffix = extname(filename).toLowerCase();
  if (exportGuidance[suffix])
    fail("document_export_required", exportGuidance[suffix]!, 415);
  if (!documentExtensions.has(suffix)) return undefined;
  if (data.byteLength > settings.maxDocumentUploadBytes)
    fail("document_too_large", "Document exceeds maxDocumentUploadBytes.", 413);
  const name = basename(filename.replaceAll("\\", "/"))
    .replace(/[\x00-\x1f\x7f]/g, "")
    .slice(0, 255);
  const header = `${DOCUMENT_PREFIX}${name}]\n`,
    footer = "\n[End attached document]";
  const out = new Output(
    settings.maxDocumentTextCharacters - header.length - footer.length,
  );
  try {
    await readDocument(data, suffix, settings, out, signal);
  } catch (error) {
    if (signal?.aborted) throw signal.reason;
    if (error instanceof AttachmentError) throw error;
    fail(
      "invalid_document",
      `Could not read ${suffix}. Export an unencrypted modern file, PDF or CSV.`,
    );
  }
  if (!out.parts.length)
    fail(
      "empty_document",
      "No readable text found. Export a text-bearing file or use a PDF/image model for scans.",
    );
  return { type: "text", text: header + out.toString() + footer };
}
