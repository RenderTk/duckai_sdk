"""Bounded local readers for Office and Google Workspace exports.

Documents become labeled text parts, not invented Duck.ai upload types. Direct
readers never run macros, evaluate formulas, fetch links, or follow Google shortcuts.
"""

import io
import json
import os
import shutil
import signal
import subprocess
import tempfile
import zipfile
from contextlib import suppress
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from xml.etree.ElementTree import Element

import olefile
from defusedxml.ElementTree import fromstring
from python_calamine import CalamineWorkbook
from striprtf.striprtf import rtf_to_text

from duckai.config import Settings
from duckai.errors import AttachmentError

WORD = frozenset({".docx", ".docm", ".dotx", ".dotm"})
SHEETS = frozenset(
    {".xlsx", ".xlsm", ".xltx", ".xltm", ".xlam", ".xls", ".xlt", ".xla", ".xlsb", ".ods", ".ots"}
)
SLIDES = frozenset({".pptx", ".pptm", ".ppsx", ".ppsm", ".potx", ".potm"})
OPEN_DOCUMENT = frozenset({".odt", ".ott", ".odp", ".otp", ".odg", ".otg"})
FLAT_DOCUMENT = frozenset({".fodt", ".fods", ".fodp", ".fodg"})
VISIO = frozenset({".vsdx", ".vsdm", ".vstx", ".vstm", ".vssx", ".vssm", ".vdx"})
TEXT = frozenset(
    {
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
    }
)
MAIL = frozenset({".eml", ".msg", ".oft"})
CONVERT = {
    ".doc": "docx",
    ".dot": "docx",
    ".ppt": "pptx",
    ".pps": "pptx",
    ".pot": "pptx",
    ".pub": "pdf",
    ".vsd": "pdf",
    ".vss": "pdf",
    ".vst": "pdf",
}
EXPORT_GUIDANCE = {
    **dict.fromkeys(
        (".gdoc", ".gsheet", ".gslides", ".gdraw", ".gform"),
        "Google shortcut files contain links, not document content. "
        "Export as DOCX, XLSX, PPTX, OpenDocument, PDF, CSV, or HTML first.",
    ),
    **dict.fromkeys(
        (".one", ".onepkg"), "Export the OneNote page or notebook as PDF or DOCX first."
    ),
    **dict.fromkeys(
        (".mdb", ".accdb", ".accde", ".mde"),
        "Export the Access tables or query results as XLSX or CSV first.",
    ),
    **dict.fromkeys((".pst", ".ost"), "Export individual Outlook messages as EML or MSG first."),
    ".mpp": "Export the Project plan as XLSX, CSV, or PDF first.",
    ".pages": "Export the document as DOCX or PDF first.",
    ".numbers": "Export the spreadsheet as XLSX or CSV first.",
    ".key": "Export the presentation as PPTX or PDF first.",
}
DOCUMENT_EXTENSIONS = frozenset().union(
    WORD,
    SHEETS,
    SLIDES,
    OPEN_DOCUMENT,
    FLAT_DOCUMENT,
    VISIO,
    TEXT,
    MAIL,
    CONVERT,
    {".epub", ".zip", ".svg"},
)
NATIVE_EXTENSIONS = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif"})
DOCUMENT_PREFIX = "[Attached document: "


def supported_file_extensions() -> tuple[str, ...]:
    """Local readers plus native Duck.ai formats; legacy conversion needs LibreOffice."""
    return tuple(sorted(DOCUMENT_EXTENSIONS | NATIVE_EXTENSIONS))


def file_kind(filename: str) -> str | None:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in NATIVE_EXTENSIONS:
        return "image"
    if suffix in DOCUMENT_EXTENSIONS:
        return "document"
    return None


def reject(code: str, message: str, status: int = 422):
    raise AttachmentError(status, detail={"error": code, "message": message})


class TextOutput:
    def __init__(self, limit: int):
        self.limit = limit
        self.size = 0
        self.parts: list[str] = []

    def add(self, value: str):
        value = value.strip("\r\n")
        if not value.strip():
            return
        self.size += len(value) + 1
        if self.size > self.limit:
            reject(
                "document_text_limit",
                "Extracted text is too long. Attach a smaller section or fewer rows/slides.",
                413,
            )
        self.parts.append(value)

    def text(self) -> str:
        return "\n".join(self.parts)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def xml(data: bytes) -> Element:
    return fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)


class Archive:
    """Read members in memory after checking the entire directory for expansion."""

    def __init__(self, data: bytes):
        self.zip = zipfile.ZipFile(io.BytesIO(data))
        entries = self.zip.infolist()
        if len(entries) > 2000 or sum(e.file_size for e in entries) > 64 * 1024 * 1024:
            self.zip.close()
            reject(
                "document_archive_limit", "Document archive exceeds the safe expansion limit.", 413
            )
        if any(e.flag_bits & 1 for e in entries):
            self.zip.close()
            reject("encrypted_document", "Export an unencrypted copy of the document.")
        self.names = self.zip.namelist()

    def read(self, name: str) -> bytes:
        return self.zip.read(name)

    def root(self, name: str) -> Element:
        return xml(self.read(name))

    def close(self):
        self.zip.close()


def paragraph(node: Element) -> str:
    # Office text, tabs, and line breaks in document order (without field codes).
    return "".join(
        child.text or ""
        if local_name(child.tag) in {"t", "Text"}
        else "\t"
        if local_name(child.tag) == "tab"
        else "\n"
        if local_name(child.tag) in {"br", "cr"}
        else ""
        for child in node.iter()
    )


def word(archive: Archive, out: TextOutput):
    root = archive.root("word/document.xml")

    def walk(node: Element):
        kind = local_name(node.tag)
        if kind == "p":
            out.add(paragraph(node))
        elif kind == "tr":
            out.add(
                "\t".join(
                    " / ".join(paragraph(p) for p in cell.iter() if local_name(p.tag) == "p")
                    for cell in node
                    if local_name(cell.tag) == "tc"
                )
            )
        else:
            for child in node:
                walk(child)

    walk(root)
    for name in archive.names:
        if name.startswith(
            ("word/header", "word/footer", "word/footnotes", "word/endnotes", "word/comments")
        ) and name.endswith(".xml"):
            out.add(f"[{Path(name).stem}]")
            for node in archive.root(name).iter():
                if local_name(node.tag) == "p":
                    out.add(paragraph(node))


def relationships(archive: Archive, name: str) -> dict[str, str]:
    if name not in archive.names:
        return {}
    return {
        n.get("Id", ""): n.get("Target", "")
        for n in archive.root(name)
        if n.get("TargetMode") != "External"
    }


def part_target(base: str, target: str) -> str:
    # OPC relationships use forward slash paths relative to the source part.
    import posixpath

    return posixpath.normpath(posixpath.join(posixpath.dirname(base), target)).lstrip("/")


def slides(archive: Archive, out: TextOutput):
    presentation = "ppt/presentation.xml"
    rels = relationships(archive, "ppt/_rels/presentation.xml.rels")
    ordered = []
    for node in archive.root(presentation).iter():
        if local_name(node.tag) == "sldId":
            rid = next(
                (v for k, v in node.attrib.items() if local_name(k) == "id" and k.startswith("{")),
                "",
            )
            if rid in rels:
                ordered.append(part_target(presentation, rels[rid]))
    for index, name in enumerate(ordered, 1):
        out.add(f"Slide {index}")
        for node in archive.root(name).iter():
            if local_name(node.tag) == "p":
                out.add(paragraph(node))
        slide_rels = relationships(
            archive, str(Path(name).parent / "_rels" / (Path(name).name + ".rels"))
        )
        for target in slide_rels.values():
            resolved = part_target(name, target)
            if resolved.startswith("ppt/notesSlides/") and resolved in archive.names:
                out.add("Speaker notes")
                for node in archive.root(resolved).iter():
                    if local_name(node.tag) == "p":
                        out.add(paragraph(node))


def cell_text(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value).replace("\t", " ").replace("\n", " ")


def spreadsheet(data: bytes, archive: Archive | None, out: TextOutput):
    if archive:
        # Check sparse ranges before the native reader allocates their rectangular grid.
        for name in archive.names:
            if name.startswith("xl/worksheets/") and name.endswith(".xml"):
                root = archive.root(name)
                for node in root.iter():
                    if local_name(node.tag) in {"dimension", "c"}:
                        last = (node.get("ref") or node.get("r") or "A1").split(":")[-1]
                        column = 0
                        for c in last:
                            if c.isalpha() and c.isascii():
                                column = column * 26 + ord(c.upper()) - ord("A") + 1
                        digits = "".join(c for c in last if c.isdigit())
                        if column * int(digits or 1) > 200_000:
                            reject(
                                "document_cell_limit",
                                "Sheet range is too large. Export a smaller range as CSV.",
                                413,
                            )
    workbook = CalamineWorkbook.from_filelike(io.BytesIO(data))
    try:
        for name in workbook.sheet_names:
            sheet = workbook.get_sheet_by_name(name)
            # Calamine 0.8's iter_rows panics on empty sheets (start/end are None).
            if not sheet.height or not sheet.width or sheet.start is None:
                continue
            out.add(f"Sheet: {name}")
            if sheet.height * sheet.width > 200_000:
                reject(
                    "document_cell_limit", "Sheet is too large. Export a smaller range as CSV.", 413
                )
            start = sheet.start
            first_col = start[1] + 1 if start else 1
            out.add(f"Columns start at {first_col}; values are tab-separated.")
            # iter_rows includes leading empty rows, but columns start at sheet.start.
            for index, row in enumerate(sheet.iter_rows(), 1):
                if any(value != "" and value is not None for value in row):
                    out.add(f"Row {index}: " + "\t".join(cell_text(v) for v in row))
        # Retain formula source even when the file has no calculated cache. Never evaluate it.
        if archive and "xl/workbook.xml" in archive.names:
            rels = relationships(archive, "xl/_rels/workbook.xml.rels")
            for node in archive.root("xl/workbook.xml").iter():
                if local_name(node.tag) != "sheet":
                    continue
                rid = next((v for k, v in node.attrib.items() if local_name(k) == "id"), "")
                target = part_target("xl/workbook.xml", rels.get(rid, ""))
                if target not in archive.names:
                    continue
                formula_heading = False
                for cell in archive.root(target).iter():
                    if local_name(cell.tag) == "c":
                        for child in cell:
                            if local_name(child.tag) == "f":
                                if not formula_heading:
                                    out.add(
                                        f"Formulas (not recalculated), sheet {node.get('name', '')}"
                                    )
                                    formula_heading = True
                                out.add(f"{cell.get('r', '?')}: ={child.text or ''}")
    finally:
        workbook.close()


def od_text(node: Element) -> str:
    chunks = [node.text or ""]
    for child in node:
        kind = local_name(child.tag)
        if kind == "s":
            count = next((int(v) for k, v in child.attrib.items() if local_name(k) == "c"), 1)
            if count > 16_000:
                reject("document_text_limit", "Document spacing exceeds the text limit.", 413)
            chunks.append(" " * count)
        elif kind in {"tab", "line-break"}:
            chunks.append("\t" if kind == "tab" else "\n")
        else:
            chunks.append(od_text(child))
        chunks.append(child.tail or "")
    return "".join(chunks)


def open_document(root: Element, out: TextOutput):
    def walk(node: Element):
        kind = local_name(node.tag)
        if kind in {"p", "h"}:
            out.add(od_text(node))
            return
        if kind in {"table", "page"}:
            name = next((v for k, v in node.attrib.items() if local_name(k) == "name"), "")
            out.add(f"{'Sheet/table' if kind == 'table' else 'Slide/page'}: {name}")
        if kind == "table-row":
            cells = []
            for cell in node:
                repeats = next(
                    (
                        int(v)
                        for k, v in cell.attrib.items()
                        if local_name(k) == "number-columns-repeated"
                    ),
                    1,
                )
                paragraphs = [od_text(p) for p in cell.iter() if local_name(p.tag) == "p"]
                value = " ".join(paragraphs) or next(
                    (
                        v
                        for k, v in cell.attrib.items()
                        if local_name(k) in {"value", "string-value", "date-value", "boolean-value"}
                    ),
                    "",
                )
                if repeats > 200_000:
                    reject(
                        "document_cell_limit",
                        "Repeated cell range is too large. Export a smaller range as CSV.",
                        413,
                    )
                # Trailing blank repeated columns are common in OpenDocument exports.
                cells.extend([value] * repeats)
            line = "\t".join(cells).rstrip()
            row_repeats = next(
                (int(v) for k, v in node.attrib.items() if local_name(k) == "number-rows-repeated"),
                1,
            )
            if line and row_repeats > 1000:
                reject(
                    "document_cell_limit",
                    "Repeated row range is too large. Export a smaller range as CSV.",
                    413,
                )
            if line:
                for _ in range(row_repeats):
                    out.add(line)
            return
        for child in node:
            walk(child)

    walk(root)


class HTMLText(HTMLParser):
    def __init__(self, out: TextOutput):
        super().__init__(convert_charrefs=True)
        self.out, self.hidden, self.line = out, 0, []

    def flush(self):
        self.out.add("".join(self.line))
        self.line.clear()

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.hidden += 1
        if not self.hidden and tag in {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "section"}:
            self.flush()

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"}:
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden:
            if tag in {"td", "th"}:
                self.line.append("\t")
            elif tag in {"p", "div", "tr", "li", "h1", "h2", "h3", "section"}:
                self.flush()

    def handle_data(self, data):
        if not self.hidden:
            self.line.append(data)
            if sum(map(len, self.line)) > self.out.limit:
                self.flush()


def html_text(value: str, out: TextOutput):
    parser = HTMLText(out)
    parser.feed(value)
    parser.close()
    parser.flush()


def decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def mail(data: bytes, out: TextOutput, *, web_archive=False):
    message = BytesParser(policy=policy.default).parsebytes(data)
    if not web_archive:
        for header in ("Subject", "From", "To", "Cc", "Date"):
            if message.get(header):
                out.add(f"{header}: {message[header]}")
    body = message.get_body(preferencelist=("plain", "html"))
    if body is None and not message.is_multipart():
        body = message
    if body:
        if body.get_content_type() == "text/html":
            html_text(body.get_content(), out)
        elif body.get_content_type() == "text/plain":
            out.add(body.get_content())
    for part in message.iter_attachments():
        if part.get_filename():
            out.add(f"Embedded attachment (not read): {part.get_filename()}")


def outlook(data: bytes, out: TextOutput):
    with olefile.OleFileIO(io.BytesIO(data)) as ole:
        if ole.exists("EncryptedPackage"):
            reject("encrypted_document", "Export an unencrypted document.")
        for tag, title in (("0037", "Subject"), ("0C1A", "From"), ("0E04", "To"), ("1000", "Body")):
            for encoding, codec in (("001F", "utf-16-le"), ("001E", "cp1252")):
                name = f"__substg1.0_{tag}{encoding}"
                if ole.exists(name):
                    value = ole.openstream(name).read().decode(codec).rstrip("\x00")
                    out.add(f"{title}: {value}")
                    break
        has_body = any(p.startswith("Body:") for p in out.parts)
        if not has_body and ole.exists("__substg1.0_10130102"):
            before = len(out.parts)
            html_text(ole.openstream("__substg1.0_10130102").read().decode("utf-8"), out)
            has_body = len(out.parts) > before
        if not has_body:
            reject(
                "outlook_body_unsupported",
                "This Outlook message has no readable body. Export it as EML or PDF.",
            )


def convert(data: bytes, suffix: str, settings: Settings, out: TextOutput):
    executable = settings.office_converter_executable or shutil.which("soffice")
    if not executable and Path("/Applications/LibreOffice.app/Contents/MacOS/soffice").is_file():
        executable = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    if not executable:
        reject(
            "office_converter_required",
            f"Reading {suffix} requires LibreOffice. Install it or export as DOCX, PPTX, or PDF.",
            415,
        )
    target = CONVERT[suffix]
    with tempfile.TemporaryDirectory(prefix="duckai-office-") as directory:
        root = Path(directory)
        profile = root / "profile"
        profile.mkdir()
        # Highest macro security for this isolated conversion profile.
        (profile / "registrymodifications.xcu").write_text(
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>'
            '<item oor:path="/org.openoffice.Office.Writer/Content/Update">'
            '<prop oor:name="Link" oor:op="fuse"><value>2</value></prop>'
            '<prop oor:name="Field" oor:op="fuse"><value>false</value></prop>'
            '<prop oor:name="Chart" oor:op="fuse"><value>false</value></prop>'
            "</item></oor:items>",
            encoding="utf-8",
        )
        source = root / ("document" + suffix)
        source.write_bytes(data)
        try:
            process = subprocess.Popen(
                [
                    executable,
                    f"-env:UserInstallation={profile.as_uri()}",
                    "--headless",
                    "--nologo",
                    "--nodefault",
                    "--nofirststartwizard",
                    "--convert-to",
                    target,
                    "--outdir",
                    directory,
                    str(source),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=os.name != "nt",
            )
            try:
                process.wait(timeout=settings.office_conversion_timeout)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                else:
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                        check=False,
                    )
                process.wait()
                raise
        except subprocess.TimeoutExpired:
            reject(
                "office_conversion_timeout",
                "Office conversion timed out. Export the file to a modern format first.",
            )
        except OSError:
            reject(
                "office_converter_unavailable",
                "LibreOffice could not start. "
                "Check office_converter_executable or export the file first.",
            )
        converted = root / ("document." + target)
        if process.returncode or not converted.is_file():
            reject(
                "office_conversion_failed",
                "This document could not be converted. Export an unencrypted modern file or PDF.",
            )
        if converted.stat().st_size > settings.max_document_upload_bytes:
            reject("document_too_large", "Converted document exceeds the upload limit.", 413)
        converted_data = converted.read_bytes()
        if target == "pdf":
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(converted_data))
            for index, page in enumerate(reader.pages, 1):
                out.add(f"Page {index}")
                out.add(page.extract_text() or "")
        else:
            read_document(converted_data, "." + target, settings, out)


def read_document(data: bytes, suffix: str, settings: Settings, out: TextOutput):
    archive = Archive(data) if data.startswith(b"PK\x03\x04") else None
    try:
        if suffix in WORD:
            if archive is None:
                reject("invalid_document", "Expected an unencrypted Word document.")
            word(archive, out)
        elif suffix in {".ods", ".ots"}:
            if archive is None:
                reject("invalid_document", "Expected an OpenDocument spreadsheet.")
            open_document(archive.root("content.xml"), out)
        elif suffix in SHEETS:
            spreadsheet(data, archive, out)
        elif suffix in SLIDES:
            if archive is None:
                reject("invalid_document", "Expected an unencrypted PowerPoint presentation.")
            slides(archive, out)
        elif suffix in OPEN_DOCUMENT:
            if archive is None:
                reject("invalid_document", "Expected an OpenDocument archive.")
            open_document(archive.root("content.xml"), out)
        elif suffix in FLAT_DOCUMENT:
            open_document(xml(data), out)
        elif suffix in VISIO:
            roots = (
                [
                    archive.root(n)
                    for n in archive.names
                    if n.startswith(("visio/pages/page", "visio/masters/master"))
                    and n.endswith(".xml")
                ]
                if archive
                else [xml(data)]
            )
            for index, root in enumerate(roots, 1):
                out.add(f"Diagram page {index}")
                for node in root.iter():
                    if local_name(node.tag) == "Text":
                        out.add("".join(node.itertext()))
        elif suffix in CONVERT:
            convert(data, suffix, settings, out)
        elif suffix == ".eml" or suffix in {".mht", ".mhtml"}:
            mail(data, out, web_archive=suffix != ".eml")
        elif suffix in {".msg", ".oft"}:
            outlook(data, out)
        elif suffix == ".epub":
            if archive is None:
                reject("invalid_document", "Expected an EPUB archive.")
            container = archive.root("META-INF/container.xml")
            opf = next(
                n.get("full-path") for n in container.iter() if local_name(n.tag) == "rootfile"
            )
            package = archive.root(opf)
            manifest = {
                n.get("id"): part_target(opf, n.get("href", ""))
                for n in package.iter()
                if local_name(n.tag) == "item"
            }
            for node in package.iter():
                if local_name(node.tag) == "itemref":
                    html_text(decode(archive.read(manifest[node.get("idref")])), out)
        elif suffix == ".rtf":
            out.add(rtf_to_text(data.decode("cp1252")))
        elif suffix == ".zip":
            if archive is None:
                reject("invalid_document", "Expected a ZIP of exported HTML or text files.")
            for name in archive.names:
                extension = Path(name).suffix.lower()
                if not name.startswith("__MACOSX/") and extension in {
                    ".html",
                    ".htm",
                    ".txt",
                    ".md",
                    ".csv",
                    ".tsv",
                }:
                    out.add(f"Exported file: {name}")
                    if extension in {".html", ".htm"}:
                        html_text(decode(archive.read(name)), out)
                    else:
                        out.add(decode(archive.read(name)))
        elif suffix == ".svg":
            for node in xml(data).iter():
                if local_name(node.tag) in {"text", "title", "desc"}:
                    out.add("".join(node.itertext()))
        elif suffix in {".html", ".htm", ".xhtml"}:
            html_text(decode(data), out)
        elif suffix == ".json":
            out.add(json.dumps(json.loads(decode(data)), ensure_ascii=False, indent=2))
        elif suffix == ".xml":
            out.add("\n".join(xml(data).itertext()))
        else:
            text = decode(data)
            if "\x00" in text:
                reject("invalid_document", "The file contains binary data, not a text export.")
            out.add(text)
    finally:
        if archive:
            archive.close()


def prepare_document(data: bytes, filename: str, settings: Settings) -> dict | None:
    suffix = Path(filename).suffix.lower()
    if suffix in EXPORT_GUIDANCE:
        reject("document_export_required", EXPORT_GUIDANCE[suffix], 415)
    if suffix not in DOCUMENT_EXTENSIONS:
        return None
    if len(data) > settings.max_document_upload_bytes:
        reject("document_too_large", "Document exceeds max_document_upload_bytes.", 413)
    safe_name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    safe_name = "".join(c for c in safe_name if c.isprintable())[:255]
    header = f"{DOCUMENT_PREFIX}{safe_name}]\n"
    footer = "\n[End attached document]"
    out = TextOutput(settings.max_document_text_characters - len(header) - len(footer))
    try:
        if data.startswith(b"\xd0\xcf\x11\xe0") and suffix not in {".msg", ".oft"}:
            with olefile.OleFileIO(io.BytesIO(data)) as ole:
                if ole.exists("EncryptedPackage"):
                    reject("encrypted_document", "Export an unencrypted copy of the document.")
        read_document(data, suffix, settings, out)
    except AttachmentError:
        raise
    except BaseException as exc:
        # PyO3 parser panics inherit BaseException. Keep malformed native-reader
        # input from terminating a desktop worker, while preserving cancellation.
        if not isinstance(exc, Exception) and type(exc).__name__ != "PanicException":
            raise
        # Library exceptions differ by format. Never leak local paths/tracebacks upstream.
        reject(
            "invalid_document",
            f"Could not read {suffix}: {type(exc).__name__}. "
            "Export an unencrypted modern file, PDF, or CSV.",
        )
    if not out.text():
        reject(
            "empty_document",
            "No readable text found. Export a text-bearing document "
            "or use a supported PDF/image model for scans.",
        )
    return {"type": "text", "text": header + out.text() + footer}
