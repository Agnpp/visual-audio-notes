"""Original, tiny UI test inputs; the WAV is a tone, not recorded speech."""
from pathlib import Path
import json
import math
import struct
import sys
import wave


def create(destination: Path):
    destination.mkdir(parents=True, exist_ok=False)
    pages = [
        ("Lists and returned values", "append changes an existing list.", "Concatenation creates a new outer list."),
        ("Mutation and aliases", "Two names may refer to the same object.", "Changing the object is different from rebinding a name."),
        ("Make the boundaries clear", "An outer list copy does not deeply copy nested objects.", "Check the original examples before accepting a summary."),
    ]
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for lines in pages:
        page_id, stream_id = len(objects) + 1, len(objects) + 2
        kids.append(f"{page_id} 0 R")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 720 480] /Resources << /Font << /F1 3 0 R >> >> /Contents {stream_id} 0 R >>".encode())
        stream = ("0.96 0.98 1 rg 0 0 720 480 re f\n0.04 0.34 0.82 rg\nBT /F1 28 Tf 55 390 Td (" + lines[0] + ") Tj ET\n" +
                  "0.17 0.20 0.25 rg\nBT /F1 18 Tf 55 310 Td (" + lines[1] + ") Tj 0 -45 Td (" + lines[2] + ") Tj ET\n" +
                  "0.45 0.49 0.55 rg BT /F1 12 Tf 55 60 Td (Original synthetic UI fixture - no lecture recording.) Tj ET").encode()
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    objects[1] = ("<< /Type /Pages /Count 3 /Kids [" + " ".join(kids) + "] >>").encode()
    document = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, value in enumerate(objects, 1):
        offsets.append(len(document))
        document.extend(f"{index} 0 obj\n".encode() + value + b"\nendobj\n")
    xref = len(document)
    document.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        document.extend(f"{offset:010d} 00000 n \n".encode())
    document.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    (destination / "Lesson.pdf").write_bytes(document)
    rate = 16000
    with wave.open(str(destination / "UI-test-tone.wav"), "wb") as audio:
        audio.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        audio.writeframes(b"".join(struct.pack("<h", int(900 * math.sin(2 * math.pi * 440 * i / rate)))
                                   for i in range(rate * 6)))
    (destination / "Imported-test-text.vtt").write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n界面测试文字：append 会修改原列表，拼接返回新列表。\n\n"
        "00:00:02.000 --> 00:00:04.000\n界面测试文字：修改对象与给变量重新赋值不同。\n\n"
        "00:00:04.000 --> 00:00:06.000\n界面测试文字：外层复制不等于深度复制。\n", encoding="utf-8")
    print(json.dumps({"directory": str(destination.resolve()), "pages": 3, "duration": 6,
                      "synthetic": True, "audio_is_tone_not_speech": True}, ensure_ascii=False))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    create(Path(sys.argv[1]))
