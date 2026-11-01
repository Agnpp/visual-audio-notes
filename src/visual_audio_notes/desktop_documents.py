"""Assemble imported originals into previews without asking users to convert."""
from __future__ import annotations

from pathlib import Path

from .core import file_hash
from .task_runtime import atomic_json


def assemble_documents(project: Path, files: list[dict], *, language="zh-CN") -> dict:
    import pypdfium2 as pdfium
    from PIL import Image
    sources = [item for item in files if item["kind"] in {"document", "office", "image"}]
    if not sources:
        return {"pdf": None, "overrides": [], "locators": [], "conversions": []}
    converted = project / "converted"
    converted.mkdir(exist_ok=True)
    documents, overrides, locators, conversions = [], [], [], []
    for index, item in enumerate(sources):
        source = project / item["path"]
        native = None
        warnings, notes = [], []
        kind = "original_pdf"
        clean_images = []
        if item["kind"] == "office":
            from .office_import import convert_office
            result = convert_office(source, converted / ("office-" + str(index)), language=language)
            pdf = Path(result["pdf_path"])
            native = result["native_text_by_page"]
            notes = result.get("notes_by_page", [])
            clean_images = result.get("ocr_image_by_page", [])
            kind, warnings = result["preview_kind"], result.get("warnings", [])
            conversions.append({"source": source.name, "preview_kind": kind, "warnings": warnings,
                                "source_sha256": file_hash(source)})
        elif item["kind"] == "image":
            pdf = converted / ("image-" + str(index) + ".pdf")
            with Image.open(source) as image:
                rgb = image.convert("RGB")
                try:
                    rgb.save(pdf)
                finally:
                    rgb.close()
            kind = "original_image"
        else:
            pdf = source
        with pdfium.PdfDocument(pdf) as document:
            count = len(document)
        if native is not None and len(native) != count:
            raise ValueError("文档文字与预览页数不一致，已保留原件，请检查该文件。")
        documents.append(pdf)
        for page in range(count):
            text = native[page] if native is not None else None
            note = notes[page] if page < len(notes) else ""
            if text is not None and note.strip() and note.strip() not in text:
                text += "\n\n课件备注：\n" + note
            overrides.append(text)
            locators.append({"original_filename": source.name, "original_page": page + 1,
                             "preview_kind": kind, "preview_warnings": warnings,
                             "clean_ocr_source": str((pdf.parent / clean_images[page]).resolve()) if page < len(clean_images) and clean_images[page] else None,
                             "ocr_unavailable": kind == "derived_readable" and not text and not (page < len(clean_images) and clean_images[page])})
    if len(documents) == 1:
        pdf = documents[0]
    else:
        pdf = project / "materials.pdf"
        with pdfium.PdfDocument.new() as merged:
            for document in documents:
                with pdfium.PdfDocument(document) as incoming:
                    merged.import_pages(incoming)
            merged.save(pdf)
    return {"pdf": pdf, "overrides": overrides, "locators": locators, "conversions": conversions}


def apply_document_text(prepared: Path, assembled: dict):
    """The supplied native text is provenance data, not a model interpretation."""
    from .core import read_json
    features = read_json(prepared / "features.json")
    source_map = read_json(prepared / "source_map.json")
    units = {item["unit_id"]: item for item in features["units"]}
    for uid, locator in source_map.items():
        index = locator["page"] - 1
        additional = dict(assembled["locators"][index])
        clean = additional.pop("clean_ocr_source", None)
        if clean:
            source = Path(clean).resolve()
            if not source.is_relative_to(prepared.parent.resolve()) or not source.is_file():
                raise ValueError("文档识别图片缺失或路径不正确。")
            import shutil
            target = prepared / "ocr_inputs" / (uid + source.suffix.lower())
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(source, target)
            additional["ocr_image"] = target.relative_to(prepared).as_posix()
        locator.update(additional)
        override = assembled["overrides"][index]
        if override is not None:
            units[uid]["text"] = override
            locator["original_text"] = override
            locator["text_origin"] = "office_native_text_and_notes"
    atomic_json(prepared / "features.json", features)
    atomic_json(prepared / "source_map.json", source_map)
    if assembled["conversions"]:
        atomic_json(prepared / "office_sources.json", {"uses_ai": False, "conversions": assembled["conversions"]})
