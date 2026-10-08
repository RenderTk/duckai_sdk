"""Document extraction and the SDK's actual wire payload, without network calls."""

import asyncio
import io
import json
import os
import struct
import subprocess
import zipfile
from email.message import EmailMessage
from pathlib import Path

import httpx
import pytest

from duckai import Attachment, AttachmentError, DuckAI, Model, Settings, supported_file_extensions
from duckai.attachments import Attachments
from duckai.documents import prepare_document

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELS = "http://schemas.openxmlformats.org/package/2006/relationships"


def archive(parts):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in parts.items():
            package.writestr(name, content)
    return output.getvalue()


def docx():
    return archive(
        {
            "word/document.xml": f'<w:document xmlns:w="{W}"><w:body>'
            "<w:p><w:r><w:t>Quarterly plan</w:t></w:r></w:p>"
            "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Revenue</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>42</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
            "</w:body></w:document>",
            "word/header1.xml": f'<w:hdr xmlns:w="{W}">'
            "<w:p><w:r><w:t>Confidential</w:t></w:r></w:p></w:hdr>",
            "word/vbaProject.bin": b"Never executed",
        }
    )


def pptx():
    def slide(text):
        return (
            f'<p:sld xmlns:p="{P}" xmlns:a="{A}">'
            f"<p:cSld><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:cSld></p:sld>"
        )

    return archive(
        {
            "ppt/presentation.xml": f'<p:presentation xmlns:p="{P}" xmlns:r="{R}">'
            '<p:sldIdLst><p:sldId id="256" r:id="rId2"/>'
            '<p:sldId id="257" r:id="rId1"/></p:sldIdLst></p:presentation>',
            "ppt/_rels/presentation.xml.rels": f'<Relationships xmlns="{RELS}">'
            '<Relationship Id="rId1" Target="slides/slide1.xml"/>'
            '<Relationship Id="rId2" Target="slides/slide2.xml"/>'
            '<Relationship Id="external" Target="https://example.invalid" '
            'TargetMode="External"/></Relationships>',
            "ppt/slides/slide1.xml": slide("Second in presentation"),
            "ppt/slides/slide2.xml": slide("First in presentation"),
            "ppt/slides/_rels/slide2.xml.rels": f'<Relationships xmlns="{RELS}">'
            '<Relationship Id="notes" Target="../notesSlides/notesSlide2.xml"/></Relationships>',
            "ppt/notesSlides/notesSlide2.xml": slide("Talk about the budget"),
        }
    )


def xlsx(dimension="B3:D4"):
    return archive(
        {
            "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/></Types>',
            "_rels/.rels": f'<Relationships xmlns="{RELS}">'
            f'<Relationship Id="rId1" Target="xl/workbook.xml" '
            f'Type="{R}/officeDocument"/></Relationships>',
            "xl/workbook.xml": f'<workbook xmlns="{S}" xmlns:r="{R}">'
            '<sheets><sheet name="Budget" sheetId="1" r:id="rId1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{RELS}">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml" '
            f'Type="{R}/worksheet"/></Relationships>',
            "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{S}">'
            f'<dimension ref="{dimension}"/><sheetData><row r="3">'
            '<c r="B3" t="inlineStr"><is><t>Product</t></is></c>'
            '<c r="C3" t="inlineStr"><is><t>Amount</t></is></c></row><row r="4">'
            '<c r="B4" t="inlineStr"><is><t>Widget</t></is></c>'
            '<c r="C4"><v>42</v></c><c r="D4"><f>C4*2</f><v>84</v></c>'
            "</row></sheetData></worksheet>",
        }
    )


def odf():
    return b"""<office:document-content
        xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
        xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
        xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
        xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0">
        <office:body><text:h>Annual report</text:h>
        <text:p>Hello<text:s text:c="2"/>world</text:p>
        <table:table table:name="Sales"><table:table-row table:number-rows-repeated="2">
        <table:table-cell><text:p>Widget</text:p></table:table-cell>
        <table:table-cell office:value="42"/>
        </table:table-row></table:table>
        <draw:page draw:name="Introduction"><text:p>Speaker notes</text:p></draw:page>
        </office:body></office:document-content>"""


def prepare(data, name, **config):
    return prepare_document(data, name, Settings(_env_file=None, **config))


@pytest.mark.parametrize("suffix", ["docx", "docm", "dotx", "dotm"])
def test_word_and_templates(suffix):
    part = prepare(docx(), f"plan.{suffix}")
    assert part["type"] == "text"
    assert "Quarterly plan\nRevenue\t42" in part["text"]
    assert "Confidential" in part["text"]
    assert "Never executed" not in part["text"]


@pytest.mark.parametrize("suffix", ["pptx", "pptm", "ppsx", "ppsm", "potx", "potm"])
def test_slides_use_presentation_order_and_notes(suffix):
    text = prepare(pptx(), "deck." + suffix)["text"]
    assert "Slide 1\nFirst in presentation\nSpeaker notes\nTalk about the budget" in text
    assert "Slide 2\nSecond in presentation" in text
    assert "example.invalid" not in text


@pytest.mark.parametrize("suffix", ["xlsx", "xlsm", "xltx", "xltm", "xlam"])
def test_sheets_preserve_coordinates_cached_values_and_formulas(suffix):
    text = prepare(xlsx(), "budget." + suffix)["text"]
    assert "Sheet: Budget" in text
    assert "Columns start at 2" in text
    assert "Row 3: Product\tAmount" in text
    assert "Row 4: Widget\t42.0\t84.0" in text
    assert "D4: =C4*2" in text


@pytest.mark.parametrize("suffix", ["odt", "ott", "ods", "ots", "odp", "otp", "odg", "otg"])
def test_open_document_exports(suffix):
    text = prepare(archive({"content.xml": odf()}), "export." + suffix)["text"]
    assert "Hello  world" in text
    assert "Sheet/table: Sales\nWidget\t42\nWidget\t42" in text
    assert "Slide/page: Introduction" in text


@pytest.mark.parametrize("suffix", ["fodt", "fods", "fodp", "fodg"])
def test_flat_exports(suffix):
    assert "Annual report" in prepare(odf(), "export." + suffix)["text"]


@pytest.mark.parametrize("suffix", ["vsdx", "vsdm", "vstx", "vstm", "vssx", "vssm"])
def test_visio_diagram_text(suffix):
    data = archive(
        {"visio/pages/page1.xml": "<Page><Shape><Text>Start<cp/> here</Text></Shape></Page>"}
    )
    assert "Start here" in prepare(data, "diagram." + suffix)["text"]


@pytest.mark.parametrize(
    "name,data,expected",
    [
        ("data.csv", b"name,amount\nWidget,42\n", "Widget,42"),
        ("data.tsv", "name\tamount\nCafé\t42".encode("utf-16"), "Café\t42"),
        (
            "page.html",
            b"<head><style>hidden</style></head><h1>Report</h1><table><tr><td>Widget</td><td>42</td></tr></table><script>secret</script>",
            "Widget\t42",
        ),
        ("document.rtf", rb"{\rtf1\ansi Hello \b Office\b0\par report}", "Hello Office"),
        ("document.json", b'{"amount":42}', '"amount": 42'),
        ("document.xml", b"<report><amount>42</amount></report>", "42"),
        ("diagram.vdx", b"<Visio><Page><Text>Start here</Text></Page></Visio>", "Start here"),
    ],
)
def test_text_exports(name, data, expected):
    text = prepare(data, name)["text"]
    assert expected in text
    assert "hidden" not in text and "secret" not in text


def test_eml_uses_readable_body_and_never_opens_embedded_files():
    message = EmailMessage()
    message["Subject"] = "Budget"
    message["From"] = "sender@example.invalid"
    message.set_content("Approved amount: 42")
    message.add_alternative("<b>HTML alternative</b>", subtype="html")
    message.add_attachment(
        b"not read", maintype="application", subtype="octet-stream", filename="macro.docm"
    )
    text = prepare(message.as_bytes(), "mail.eml")["text"]
    assert "Subject: Budget" in text and "Approved amount: 42" in text
    assert "Embedded attachment (not read): macro.docm" in text
    assert "HTML alternative" not in text and "not read\n" not in text


def test_epub_spine_order():
    data = archive(
        {
            "META-INF/container.xml": "<container>"
            '<rootfile full-path="OEBPS/content.opf"/></container>',
            "OEBPS/content.opf": '<package><manifest><item id="a" href="a.xhtml"/>'
            '<item id="b" href="b.xhtml"/></manifest>'
            '<spine><itemref idref="b"/><itemref idref="a"/></spine></package>',
            "OEBPS/a.xhtml": "<p>Second chapter</p>",
            "OEBPS/b.xhtml": "<p>First chapter</p>",
        }
    )
    assert "First chapter\nSecond chapter" in prepare(data, "book.epub")["text"]


def test_google_html_zip_and_drawing_export():
    data = archive(
        {"Report.html": "<h1>Report</h1><p>Oranges: 19</p>", "images/ignored.png": b"not text"}
    )
    text = prepare(data, "Report.zip")["text"]
    assert "Exported file: Report.html\nReport\nOranges: 19" in text
    assert "ignored.png" not in text
    drawing = (
        b'<svg xmlns="http://www.w3.org/2000/svg"><title>Process</title>'
        b'<text>Step <tspan>one</tspan></text><path d="M0 0"/></svg>'
    )
    assert "Process\nStep one" in prepare(drawing, "Drawing.svg")["text"]


@pytest.mark.parametrize(
    "name,data,code",
    [
        ("bad.docx", b"not a zip", "invalid_document"),
        ("empty.txt", b"   ", "empty_document"),
        ("binary.txt", b"abc\x00def", "invalid_document"),
        ("export.gdoc", b'{"url":"https://example.invalid"}', "document_export_required"),
        ("database.accdb", b"unsupported", "document_export_required"),
        ("notebook.one", b"unsupported", "document_export_required"),
        ("archive.pst", b"unsupported", "document_export_required"),
        (
            "evil.fodt",
            b'<!DOCTYPE root [<!ENTITY e "expanded">]><root>&e;</root>',
            "invalid_document",
        ),
        ("sparse.xlsx", xlsx("A1:XFD1048576"), "document_cell_limit"),
    ],
)
def test_invalid_and_export_only_files(name, data, code):
    with pytest.raises(AttachmentError) as error:
        prepare(data, name)
    assert error.value.detail["error"] == code


def test_bounded_upload_text_and_archive():
    for data, name, config, code in [
        (b"long text", "a.txt", {"max_document_upload_bytes": 4}, "document_too_large"),
        (b"x" * 200, "a.txt", {"max_document_text_characters": 100}, "document_text_limit"),
        (archive({f"file{i}": "" for i in range(2001)}), "a.docx", {}, "document_archive_limit"),
    ]:
        with pytest.raises(AttachmentError) as error:
            prepare(data, name, **config)
        assert error.value.detail["error"] == code
        assert error.value.status_code == 413


@pytest.mark.parametrize(
    "offset,value,code",
    [
        (8, 1, "encrypted_document"),
        (24, 64 * 1024 * 1024 + 1, "document_archive_limit"),
    ],
)
def test_archive_metadata_limits_before_member_reads(offset, value, code):
    data = bytearray(docx())
    header = data.index(b"PK\x01\x02")
    struct.pack_into("<H" if offset == 8 else "<I", data, header + offset, value)
    with pytest.raises(AttachmentError, match=code):
        prepare(bytes(data), "document.docx")


def test_workbook_empty_sheet_does_not_crash_native_reader():
    with zipfile.ZipFile(io.BytesIO(xlsx())) as source:
        parts = {name: source.read(name).decode() for name in source.namelist()}
    parts["xl/workbook.xml"] = parts["xl/workbook.xml"].replace(
        "</sheets>", '<sheet name="Empty" sheetId="2" r:id="rId2"/></sheets>'
    )
    parts["xl/_rels/workbook.xml.rels"] = parts["xl/_rels/workbook.xml.rels"].replace(
        "</Relationships>",
        '<Relationship Id="rId2" Target="worksheets/sheet2.xml" '
        f'Type="{R}/worksheet"/></Relationships>',
    )
    parts["xl/worksheets/sheet2.xml"] = f'<worksheet xmlns="{S}"><sheetData/></worksheet>'
    assert "Widget" in prepare(archive(parts), "budget.xlsx")["text"]


def test_converter_absent_failed_and_timed_out(monkeypatch):
    monkeypatch.setattr("duckai.documents.shutil.which", lambda _: None)
    monkeypatch.setattr(Path, "is_file", lambda _: False)
    with pytest.raises(AttachmentError, match="office_converter_required"):
        prepare(b"old document", "old.doc")

    class FailedProcess:
        returncode = 1

        def __init__(self, *args, **kwargs):
            pass

        def wait(self, **kwargs):
            return self.returncode

    monkeypatch.setattr("duckai.documents.subprocess.Popen", FailedProcess)
    with pytest.raises(AttachmentError, match="office_conversion_failed"):
        prepare(b"old document", "old.doc", office_converter_executable="soffice")

    class TimedOutProcess(FailedProcess):
        pid = 12345

        def __init__(self, *args, **kwargs):
            assert "--headless" in args[0]

        def wait(self, **kwargs):
            if kwargs:
                assert kwargs["timeout"] == 1
                raise subprocess.TimeoutExpired("soffice", 1)
            return 0

    monkeypatch.setattr("duckai.documents.subprocess.Popen", TimedOutProcess)
    if os.name != "nt":
        monkeypatch.setattr("duckai.documents.os.killpg", lambda *args: None)
    else:
        monkeypatch.setattr("duckai.documents.subprocess.run", lambda *args, **kwargs: None)
    with pytest.raises(AttachmentError, match="office_conversion_timeout"):
        prepare(
            b"old document",
            "old.doc",
            office_converter_executable="soffice",
            office_conversion_timeout=1,
        )


def test_documents_on_text_only_model_and_actual_sdk_payload():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text='data: {"message":"Read it"}\n\ndata: [DONE]\n\n',
        )

    with DuckAI(
        model=Model.MISTRAL_SMALL_4,
        settings=Settings(_env_file=None, duckai_headers_json={"x-vqd-hash-1": "test"}),
        transport=httpx.MockTransport(handler),
    ) as ai:
        response = ai.chat(
            "Summarize these",
            files=[
                Attachment.from_bytes(docx(), filename="plan.docx"),
                Attachment.from_bytes(xlsx(), filename="budget.xlsx"),
            ],
        )
        assert response.text == "Read it"
        content = requests[0]["messages"][0]["content"]
        assert all(p["type"] == "text" for p in content)
        assert "Quarterly plan" in content[1]["text"]
        assert "Sheet: Budget" in content[2]["text"]
        limits = ai.attachment_limits()
        assert limits["document_text_supported"] and not limits["image_supported"]
        assert ".xlsb" in limits["supported_file_extensions"]


def test_combined_documents_and_prompt_are_bounded_before_http():
    from duckai import AsyncDuckAI

    async def run():
        async with AsyncDuckAI(
            settings=Settings(_env_file=None, max_document_text_characters=150)
        ) as ai:
            files = [Attachment.from_bytes(b"x" * 50, filename="a.txt")]
            with pytest.raises(AttachmentError, match="document_text_limit"):
                await ai._request("p" * 100, None, files, None, {})
            with pytest.raises(AttachmentError, match="document_text_limit"):
                await ai.prepare_files(files * 2)

    asyncio.run(run())


def test_filename_and_catalogue():
    text = prepare(b"hello", "../private\nname.txt")["text"]
    assert text.startswith("[Attached document: privatename.txt]")
    assert ".gdoc" not in supported_file_extensions()
    assert (
        Attachments(Settings(_env_file=None)).prepare(
            b"hello", "a.txt", "text/plain", Model.GPT_OSS_120B
        )["type"]
        == "text"
    )
