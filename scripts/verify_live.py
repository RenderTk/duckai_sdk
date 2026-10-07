"""Live checks of the direct SDK with synthetic files. No FastAPI server is used."""

import argparse
import asyncio
import io
import json
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageDraw

from duckai import AsyncDuckAI, Attachment, DuckAI


def make_pdf(code: str) -> bytes:
    stream = f"BT /F1 18 Tf 72 720 Td (The access code is {code}.) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    result = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, value in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{index} 0 obj\n".encode() + value + b"\nendobj\n")
    xref = len(result)
    result.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(result)


def make_image(count: int) -> bytes:
    image = Image.new("RGB", (512, 256), "white")
    drawing = ImageDraw.Draw(image)
    for index in range(count):
        x = int(512 * (index + 1) / (count + 1))
        drawing.ellipse((x - 30, 98, x + 30, 158), fill=(220, 25, 25))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("output/sdk-live-verification.json"))
    parser.add_argument(
        "--checks",
        nargs="+",
        choices=[
            "sync_chat",
            "sync_stream",
            "pdf_and_image",
            "conversation_followup",
            "async_chat",
            "async_stream",
        ],
        default=[
            "sync_chat",
            "sync_stream",
            "pdf_and_image",
            "conversation_followup",
            "async_chat",
            "async_stream",
        ],
    )
    args = parser.parse_args()
    selected = set(args.checks)
    code = "SDK-" + secrets.token_hex(3).upper()
    count = 2 + secrets.randbelow(3)
    report = {
        "checked_at_utc": datetime.now(UTC).isoformat(),
        "passed": False,
        "expected_check_count": len(selected),
        "checks": [],
    }

    def record(name, response, expected):
        passed = response.done and response.text.strip() == expected
        report["checks"].append(
            {
                "name": name,
                "expected": expected,
                "actual": response.text,
                "done": response.done,
                "passed": passed,
            }
        )
        print(f"{name}: complete={response.done}, passed={passed}", flush=True)
        if not passed:
            raise RuntimeError(f"{name} failed: {response.text!r}")

    async def verify_async():
        async with AsyncDuckAI() as ai:
            if "async_chat" in selected:
                record("async_chat", await ai.chat("What is 8 + 9? Reply only the number."), "17")
            if "async_stream" in selected:
                async with ai.stream("Reply exactly ASYNCSTREAM.") as stream:
                    chunks = [chunk async for chunk in stream]
                assert "".join(chunks) == stream.response.text
                record("async_stream", stream.response, "ASYNCSTREAM")

    try:
        if selected & {"sync_chat", "sync_stream", "pdf_and_image", "conversation_followup"}:
            with DuckAI() as ai:
                if "sync_chat" in selected:
                    record("sync_chat", ai.chat("What is 13 + 29? Reply only the number."), "42")
                if "sync_stream" in selected:
                    with ai.stream("Reply exactly SYNCSTREAM.") as stream:
                        chunks = list(stream)
                    assert "".join(chunks) == stream.response.text
                    record("sync_stream", stream.response, "SYNCSTREAM")
                if selected & {"pdf_and_image", "conversation_followup"}:
                    with tempfile.TemporaryDirectory() as folder:
                        pdf = Path(folder) / "document.pdf"
                        pdf.write_bytes(make_pdf(code))
                        image = Attachment.from_bytes(make_image(count), "shapes.png", "image/png")
                        conversation = ai.conversation()
                        response = conversation.chat(
                            "Return the access code in the PDF and the number of red circles "
                            "in the image. Reply exactly CODE|COUNT.",
                            files=[pdf, image],
                        )
                        if "pdf_and_image" in selected:
                            record("pdf_and_image", response, f"{code}|{count}")
                        if "conversation_followup" in selected:
                            record(
                                "conversation_followup",
                                conversation.chat(
                                    "Repeat the code and circle count from the files I already "
                                    "sent. Reply exactly CODE|COUNT."
                                ),
                                f"{code}|{count}",
                            )
                            assert len(conversation.messages) == 4
        if selected & {"async_chat", "async_stream"}:
            asyncio.run(verify_async())
        report["passed"] = len(report["checks"]) == report["expected_check_count"]
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=args.output.parent, delete=False) as output:
            json.dump(report, output, indent=2)
            temporary = Path(output.name)
        temporary.replace(args.output)
        print(f"Report: {args.output.resolve()}", flush=True)


if __name__ == "__main__":
    main()
