# Attachment format guide

Both SDK clients (`DuckAI` / `AsyncDuckAI`) and the native example app accept the
formats below through the existing `files` argument. Use paths or
`Attachment.from_bytes(data, filename="report.docx")`. The extension selects the
local document reader, so supply the real filename for in-memory files.

Office and Workspace exports become labeled text content. They work on all chat
models, including models without native image/PDF support. PDFs and images keep
Duck.ai's existing binary attachment protocol and model-specific limits.

## Included local readers

| Family | Extensions | Extracted content |
| --- | --- | --- |
| Word documents and templates | `.docx`, `.docm`, `.dotx`, `.dotm` | Paragraphs, table rows, headers, footers, footnotes, endnotes, comments |
| Excel workbooks, templates and add-ins | `.xlsx`, `.xlsm`, `.xltx`, `.xltm`, `.xlam`, `.xls`, `.xlt`, `.xla`, `.xlsb` | Named sheets, row/column coordinates, stored values; OOXML formula source without recalculation |
| PowerPoint presentations, shows and templates | `.pptx`, `.pptm`, `.ppsx`, `.ppsm`, `.potx`, `.potm` | Slide text in presentation order and speaker notes |
| OpenDocument documents, sheets, slides and drawings | `.odt`, `.ott`, `.ods`, `.ots`, `.odp`, `.otp`, `.odg`, `.otg` | Text, table values, named sheets/pages, notes |
| Flat OpenDocument | `.fodt`, `.fods`, `.fodp`, `.fodg` | Text and table values |
| Modern Visio and XML diagrams | `.vsdx`, `.vsdm`, `.vstx`, `.vstm`, `.vssx`, `.vssm`, `.vdx` | Shape text grouped by page |
| Outlook messages and templates | `.eml`, `.msg`, `.oft` | Headers and plain/HTML body; embedded mail attachments are not recursively read |
| Calendar and contacts exports | `.ics`, `.vcf` | Exported text fields |
| Spreadsheet interchange | `.csv`, `.tsv`, `.dif`, `.slk` | Exported text values |
| Documents and web exports | `.rtf`, `.txt`, `.md`, `.html`, `.htm`, `.xhtml`, `.mht`, `.mhtml`, `.epub` | Readable text; EPUB chapters follow the spine |
| Google HTML ZIP exports | `.zip` | HTML, text, Markdown, CSV and TSV members; images/binary members are ignored |
| Google Drawings SVG exports | `.svg` | Text labels, title and description; geometry is not rendered |
| Structured text exports | `.json`, `.xml` | JSON data and XML text |
| Native Duck.ai attachments | `.pdf`, `.png`, `.jpg`, `.jpeg`, `.webp`, `.gif` | Original PDF or processed image using the native protocol |

Macro-enabled extensions mean their readable content is supported. Macros,
scripts, embedded executables and spreadsheet formulas are not executed by the
local readers. Charts, embedded pictures, handwriting, animation and page layout
are not included in text extraction. Attach a PDF or screenshot on a compatible
model when visual content matters. MSG/OFT supports plain or HTML MAPI bodies;
compressed-RTF-only messages need an EML/PDF export. Workbook add-ins with no
readable sheets are not useful chat attachments.

## Google Workspace downloads

Choose **File → Download** in Google Docs, Sheets, Slides or Drawings, then attach
the downloaded file. Docs exports include DOCX, ODT, RTF, TXT, Markdown, EPUB,
HTML ZIP and PDF. Sheets exports include XLSX, ODS, CSV, TSV and PDF. Slides
exports include PPTX, ODP, TXT and PDF. Drawings can use PDF, PNG, JPEG or SVG.
The SDK does not authenticate to Google, fetch Drive links or open a browser for
document import. `.gdoc`, `.gsheet`, `.gslides`, `.gdraw` and `.gform` are shortcut
files containing links and require an actual content export.

Google's [official export format reference](https://developers.google.com/workspace/drive/api/guides/ref-export-formats)
describes the available exports.

## Optional legacy conversion

| Family | Extensions | Conversion |
| --- | --- | --- |
| Legacy Word | `.doc`, `.dot` | DOCX, then text extraction |
| Legacy PowerPoint | `.ppt`, `.pps`, `.pot` | PPTX, then text extraction |
| Publisher | `.pub` | PDF, then local PDF text extraction |
| Binary Visio | `.vsd`, `.vss`, `.vst` | PDF, then local PDF text extraction |

Install [LibreOffice](https://www.libreoffice.org/download/download-libreoffice/)
and put `soffice` on PATH. The usual macOS application location is also detected.
For another location:

```python
from duckai import DuckAI, Settings

with DuckAI(settings=Settings(office_converter_executable="/path/to/soffice")) as ai:
    print(ai.chat("Summarize this legacy document.", files=["report.doc"]).text)
```

Or set `OFFICE_CONVERTER_EXECUTABLE` in the environment. Each conversion uses a
temporary file and isolated headless profile with highest macro security. It
does not modify the source file. Conversion defaults to a 60-second timeout
(`OFFICE_CONVERSION_TIMEOUT`). Installed LibreOffice versions determine legacy
import compatibility; a missing converter, unsupported import or failed
conversion produces an actionable `AttachmentError`.

## Formats that need an export

| Source | Attach an export |
| --- | --- |
| OneNote `.one`, `.onepkg` | PDF or DOCX |
| Access `.mdb`, `.accdb`, `.mde`, `.accde` | Tables/query results as XLSX or CSV |
| Outlook archives `.pst`, `.ost` | Individual EML or MSG messages |
| Project `.mpp` | XLSX, CSV or PDF |
| Apple Pages, Numbers, Keynote | DOCX, XLSX, PPTX or PDF |

These proprietary databases, notebooks and archives are not treated as readable
Office documents. The SDK and desktop app show the corresponding export guidance.
Password-protected files require an unencrypted export. Scans require a supported
native PDF/image model; document readers do not perform OCR.

## Limits and retained data

- `MAX_DOCUMENT_UPLOAD_BYTES`: 20 MiB per source or converted file by default.
- `MAX_DOCUMENT_TEXT_CHARACTERS`: 16,000 maximum, including prompt, document labels
  and all extracted files in a user message. You can configure a lower limit.
- Messages with images remain limited to 4,500 text characters, including documents.
- Archives are bounded to 2,000 members and 64 MiB total uncompressed content.
  Large spreadsheet ranges and XML entities/DTDs are rejected.

Oversized text is rejected rather than silently truncated. Export a smaller range,
slide selection or document section. PDF/image limits still apply independently.

`supported_file_extensions()` returns the format catalogue;
`ai.attachment_limits(model)` reports document limits alongside native model
capabilities. `ai.prepare_files(...)` does local extraction without contacting
Duck.ai. Retain the returned text parts in history to reuse documents without
the source file. The desktop app does this automatically for retries, edits,
saved chats and JSON exports. Submitted extracted content is sent to Duck.ai;
saved desktop history and JSON exports retain that text.
