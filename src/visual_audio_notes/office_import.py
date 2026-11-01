"""Model-free Office ingestion with honest, portable reading previews.

OOXML text is extracted directly; built-in previews are derived reading pages,
not claims of Office layout fidelity. Originals, formulas and slide notes remain
available. No macros, external relationships or embedded programs are executed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from contextvars import ContextVar
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import shutil
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile

from .desktop_locale import normalize_language, translate

_PREVIEW_LANGUAGE = ContextVar("office_preview_language", default="zh-CN")


OOXML = {".pptx", ".docx", ".xlsx", ".vsdx"}
LEGACY = {".ppt", ".doc", ".xls", ".vsd"}
SUPPORTED = OOXML | LEGACY
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
V = "http://schemas.microsoft.com/office/visio/2012/main"


@dataclass
class Page:
    label: str
    text: str
    notes: str = ""
    image: object = None
    locators: list[dict] = field(default_factory=list)
    ocr_image: object = None

    def __post_init__(self):
        # Keep compressed page data rather than hundreds of decoded RGB pages.
        for name in ("image", "ocr_image"):
            image = getattr(self, name)
            if image is not None:
                buffer = BytesIO()
                image.save(buffer, "PNG")
                image.close()
                setattr(self, name, buffer.getvalue())


class Package:
    def __init__(self, path: Path):
        try:
            self.zip = zipfile.ZipFile(path)
            infos = self.zip.infolist()
            if (not infos or len(infos) > 10000 or sum(i.file_size for i in infos) > 150 * 1024 ** 2
                    or any(i.file_size > 30 * 1024 ** 2 for i in infos)):
                raise ValueError()
            names = set()
            for info in infos:
                name = info.filename
                if (info.orig_filename != name or "\x00" in info.orig_filename
                        or "\\" in name or name.startswith("/") or ".." in PurePosixPath(name).parts
                        or ":" in name or name in names or info.flag_bits & 1
                        or (info.file_size > 1024 ** 2 and info.file_size / max(info.compress_size, 1) > 1000)):
                    raise ValueError()
                names.add(name)
            self.names = names
            self.has_external_relationships = False
            for name in names:
                if name.endswith(".rels"):
                    for relation in self.xml(name):
                        if relation.get("TargetMode", "").lower() == "external":
                            self.has_external_relationships = True
        except (ValueError, OSError, zipfile.BadZipFile):
            if hasattr(self, "zip"):
                self.zip.close()
            raise ValueError("Office 文件无法读取、已加密或超出安全解析范围。") from None

    def close(self):
        self.zip.close()

    def data(self, name):
        try:
            return self.zip.read(name)
        except (KeyError, zipfile.BadZipFile, RuntimeError):
            raise ValueError("Office 文件缺少必需内容，可能已损坏。") from None

    def xml(self, name):
        data = self.data(name)
        guard = data.replace(b"\x00", b"").upper()
        if b"<!DOCTYPE" in guard or b"<!ENTITY" in guard:
            raise ValueError("Office XML 包含不支持的实体定义。")
        try:
            return ET.fromstring(data)
        except ET.ParseError:
            raise ValueError("Office XML 内容无法读取。") from None

    def relationships(self, part):
        name = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
        if name not in self.names:
            return {}
        result = {}
        for item in self.xml(name):
            if item.get("TargetMode", "").lower() == "external":
                continue
            target = item.get("Target", "")
            if not target or "\\" in target or ":" in target:
                continue
            resolved = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(posixpath.dirname(part), target))
            if resolved.startswith("../") or resolved not in self.names:
                continue
            result[item.get("Id")] = (resolved, item.get("Type", "").rsplit("/", 1)[-1])
        return result


def _text(element, namespace=A):
    return "\n".join("".join(t.text or "" for t in paragraph.iter("{" + namespace + "}t"))
                     for paragraph in element.iter("{" + namespace + "}p")).strip()


def _font(size=24):
    from PIL import ImageFont
    # Rasterize installed fonts; never redistribute the font files themselves.
    for name in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/arial.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if Path(name).is_file():
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def _wrap(text, width, font):
    lines = []
    for line in text.splitlines() or [""]:
        value = ""
        for character in line:
            if value and font.getlength(value + character) > width:
                lines.append(value)
                value = character
            else:
                value += character
        lines.append(value)
    return lines


def _draw_text(draw, text, box, *, size=25, fill="#263445"):
    left, top, right, bottom = box
    font = _font(size)
    lines = _wrap(text, max(20, right - left), font)
    for line in lines:
        if top + size + 4 > bottom:
            return False
        draw.text((left, top), line, font=font, fill=fill)
        top += int(size * 1.5)
    return True


def _canvas(label, size=(1280, 900)):
    from PIL import Image, ImageDraw
    image = Image.new("RGB", size, "#ffffff")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, size[0], 65), fill="#edf3ff")
    draw.text((35, 17), translate(label, _PREVIEW_LANGUAGE.get()), font=_font(23), fill="#174c9c")
    draw.text((35, size[1] - 38), translate("自动生成的阅读预览 · 不是原版排版 · 原件完整保留", _PREVIEW_LANGUAGE.get()), font=_font(16), fill="#68778a")
    return image


def _picture(package, name):
    from PIL import Image, ImageOps
    try:
        with Image.open(BytesIO(package.data(name))) as image:
            if image.width * image.height > 40_000_000:
                raise ValueError()
            image.load()
            return ImageOps.exif_transpose(image).convert("RGB")
    except (OSError, ValueError, SyntaxError):
        return None


def _pptx(package):
    from PIL import ImageDraw, ImageOps
    main = package.xml("ppt/presentation.xml")
    rels = package.relationships("ppt/presentation.xml")
    dimensions = main.find("{" + P + "}sldSz")
    width = float(dimensions.get("cx", "12192000")) if dimensions is not None else 12192000
    height = float(dimensions.get("cy", "6858000")) if dimensions is not None else 6858000
    if width <= 0 or height <= 0:
        raise ValueError("幻灯片尺寸不正确。")
    pages, warnings = [], []
    ids = main.find("{" + P + "}sldIdLst")
    if ids is not None and len(ids) > 1000:
        raise ValueError("课件超过 1000 张幻灯片，请分成多个项目。")
    for index, item in enumerate(ids if ids is not None else []):
        target = rels.get(item.get(RID))
        if not target or target[1] != "slide":
            raise ValueError("幻灯片关系不完整。")
        part = target[0]
        slide = package.xml(part)
        relations = package.relationships(part)
        text = _text(slide)
        notes = ""
        for note_part, kind in relations.values():
            if kind == "notesSlide":
                notes = _text(package.xml(note_part))
        label = f"幻灯片 {index + 1}"
        image = _canvas(label)
        draw = ImageDraw.Draw(image)
        sx, sy = 1200 / width, 730 / height
        tree = slide.find(".//{" + P + "}spTree")
        if tree is not None:
            for shape in tree:
                local = shape.tag.rsplit("}", 1)[-1]
                if local in {"nvGrpSpPr", "grpSpPr"}:
                    continue
                xfrm = shape.find(".//{" + A + "}xfrm")
                off = xfrm.find("{" + A + "}off") if xfrm is not None else None
                ext = xfrm.find("{" + A + "}ext") if xfrm is not None else None
                if off is not None and ext is not None:
                    x, y = float(off.get("x", 0)) * sx + 40, float(off.get("y", 0)) * sy + 85
                    w, h = float(ext.get("cx", 0)) * sx, float(ext.get("cy", 0)) * sy
                    box = (x, y, x + max(1, w), y + max(1, h))
                else:
                    box = (55, 100, 1220, 800)
                if local == "pic":
                    blip = shape.find(".//{" + A + "}blip")
                    embedded = blip.get("{" + RID[1:].split("}")[0] + "}embed") if blip is not None else None
                    relation = relations.get(embedded)
                    picture = _picture(package, relation[0]) if relation and relation[1] == "image" else None
                    if picture is not None:
                        thumbnail = ImageOps.contain(picture, (max(1, int(box[2]-box[0])), max(1, int(box[3]-box[1]))))
                        image.paste(thumbnail, (int(box[0]), int(box[1])))
                        thumbnail.close()
                        picture.close()
                    else:
                        warnings.append(f"{label} 的一张图片无法在内置阅读预览中显示。")
                elif local == "sp":
                    shape_text = _text(shape)
                    fill = shape.find(".//{" + A + "}solidFill/{" + A + "}srgbClr")
                    background = "#" + fill.get("val") if fill is not None and len(fill.get("val", "")) == 6 else None
                    if background:
                        try:
                            draw.rounded_rectangle(box, radius=8, fill=background)
                        except ValueError:
                            pass
                    if shape_text:
                        size_node = shape.find(".//{" + A + "}rPr")
                        point_size = float(size_node.get("sz", 1800)) / 100 if size_node is not None else 18
                        if not _draw_text(draw, shape_text, (box[0]+8, box[1]+5, box[2]-5, box[3]-3), size=max(16, min(48, int(point_size*1.45)))):
                            warnings.append(f"{label} 的文本框预览空间不足；完整文字仍已提取。")
                else:
                    warnings.append(f"{label} 含组合图形、图表或其他复杂对象，预览可能不完整。")
        if not text.strip() and not any("image" == kind for _, kind in relations.values()):
            warnings.append(f"{label} 没有可直接提取的文字，复杂图形仍需视觉核对。")
        if tree is None or not any(_text(child) for child in tree):
            if text:
                _draw_text(draw, text, (55, 100, 1220, 800))
        combined = text + ("\n\n课件演讲者备注（未证明录音讲到）：\n" + notes if notes else "")
        pages.append(Page(label, combined, notes, image, [{"slide": index+1, "part": part}],
                          image.crop((40, 85, 1240, 815)) if not combined.strip() else None))
    if not pages:
        raise ValueError("课件中没有可读取的幻灯片。")
    warnings.append("内置课件预览不保证母版、主题、动画、图表和复杂图形的原版效果；请保留并核对原件。")
    return pages, warnings


def _flow_pages(label, records):
    from PIL import ImageDraw
    pages, lines, locators = [], [], []
    font = _font(24)
    characters = 0
    for text, locator in records:
        characters += len(text)
        if len(text) > 250000 or characters > 2000000:
            raise ValueError("文档文字量过大，请按章节或工作表拆分。")
        wrapped = _wrap(text, 1170, font)
        for line in wrapped:
            if len(lines) >= 28:
                image = _canvas(f"{label} · 阅读页 {len(pages)+1}", (1280, 1280))
                _draw_text(ImageDraw.Draw(image), "\n".join(lines), (55, 95, 1220, 1200), size=24)
                pages.append(Page(f"{label} · 阅读页 {len(pages)+1}", "\n".join(lines), image=image, locators=locators))
                if len(pages) > 1000:
                    raise ValueError("文档超过 1000 个阅读页，请拆分后导入。")
                lines, locators = [], []
            lines.append(line)
            if locator not in locators:
                locators.append(locator)
        lines.append("")
    if lines or not pages:
        image = _canvas(f"{label} · 阅读页 {len(pages)+1}", (1280, 1280))
        _draw_text(ImageDraw.Draw(image), "\n".join(lines) or "此文件没有可直接提取的文字。", (55, 95, 1220, 1200), size=24)
        pages.append(Page(f"{label} · 阅读页 {len(pages)+1}", "\n".join(lines), image=image, locators=locators))
    return pages


def _docx(package):
    from PIL import ImageDraw, ImageOps
    document = package.xml("word/document.xml")
    body = document.find("{" + W + "}body")
    if body is None:
        raise ValueError("Word 文档没有正文。")
    records, pages = [], []
    relationships = package.relationships("word/document.xml")
    def flush():
        nonlocal records
        if records:
            pages.extend(_flow_pages("Word 正文", records))
            records = []
    for index, child in enumerate(body):
        local = child.tag.rsplit("}", 1)[-1]
        if local == "p":
            text = _text(child, W)
        elif local == "tbl":
            text = "\n".join(" | ".join(_text(cell, W) for cell in row.findall("{"+W+"}tc")) for row in child.findall("{"+W+"}tr"))
        else:
            continue
        if text:
            records.append((text, {"body_block": index + 1, "kind": local}))
        for drawing in child.iter("{" + A + "}blip"):
            relation = relationships.get(drawing.get("{" + RID[1:].split("}")[0] + "}embed"))
            picture = _picture(package, relation[0]) if relation and relation[1] == "image" else None
            if picture is None:
                continue
            flush()
            image = _canvas(f"Word 插图 · 正文区块 {index+1}", (1280, 1280))
            thumbnail = ImageOps.contain(picture, (1160, 1090))
            image.paste(thumbnail, (60 + (1160-thumbnail.width)//2, 90+(1090-thumbnail.height)//2))
            thumbnail.close()
            pages.append(Page(f"Word 插图 · 正文区块 {index+1}", "", image=image,
                              locators=[{"body_block": index+1, "image_part": relation[0]}], ocr_image=picture))
    flush()
    warnings = ["Word 使用重排阅读页，不等同于原始页码；复杂版式、图片、公式和批注请核对原件。"]
    if not pages:
        pages = _flow_pages("Word 正文", [])
    return pages, warnings


def _xlsx(package):
    workbook = package.xml("xl/workbook.xml")
    relations = package.relationships("xl/workbook.xml")
    strings = []
    if "xl/sharedStrings.xml" in package.names:
        strings = ["".join(t.text or "" for t in item.iter("{"+S+"}t")) for item in package.xml("xl/sharedStrings.xml")]
    pages, warnings, count = [], [], 0
    formulas = False
    for item in workbook.findall("{"+S+"}sheets/{"+S+"}sheet"):
        relation = relations.get(item.get(RID))
        if not relation or relation[1] != "worksheet":
            continue
        part, name = relation[0], item.get("name", "工作表")
        root = package.xml(part)
        records = []
        for row in root.findall("{"+S+"}sheetData/{"+S+"}row"):
            cells = []
            for cell in row.findall("{"+S+"}c"):
                count += 1
                if count > 50000:
                    raise ValueError("表格超过 50000 个非空单元格，请拆分后导入。")
                address, kind = cell.get("r", "?"), cell.get("t")
                value = cell.findtext("{"+S+"}v", "")
                if kind == "s":
                    try:
                        value = strings[int(value)]
                    except (ValueError, IndexError):
                        raise ValueError("共享字符串引用不正确。") from None
                elif kind == "inlineStr":
                    value = "".join(t.text or "" for t in cell.iter("{"+S+"}t"))
                formula = cell.findtext("{"+S+"}f")
                if formula is not None:
                    formulas = True
                    value = f"{value or '无缓存值'} [公式: ={formula}]"
                if value:
                    cells.append(f"{address}: {value}")
            if cells:
                records.append(("    |    ".join(cells), {"sheet": name, "row": row.get("r")}))
        if item.get("state") in {"hidden", "veryHidden"}:
            records.insert(0, ("此工作表在原件中为隐藏状态。", {"sheet": name, "state": item.get("state")}))
        pages.extend(_flow_pages("工作表 · " + name, records))
    if not pages:
        raise ValueError("工作簿没有可读取的工作表。")
    if formulas:
        warnings.append("公式原样保留，但本程序不重算；缓存显示值可能已经过期。")
    warnings.append("数值采用文件原生存储值；日期、百分比和自定义数字格式仍请对照原件。")
    warnings.append("Excel 使用带单元格定位的重排阅读页；图表、格式和打印区域不保证原样呈现。")
    return pages, warnings


def _cells(shape):
    result = {}
    for cell in shape.findall("{"+V+"}Cell"):
        try:
            result[cell.get("N")] = float(cell.get("V", "0"))
        except ValueError:
            continue
    return result


def _vsdx(package):
    from PIL import ImageDraw
    listing = package.xml("visio/pages/pages.xml")
    relations = package.relationships("visio/pages/pages.xml")
    pages, warnings = [], []
    for index, item in enumerate(listing):
        rel_node = item.find("{"+V+"}Rel")
        relation = relations.get(rel_node.get(RID)) if rel_node is not None else relations.get(item.get(RID))
        if not relation:
            raise ValueError("Visio 页面关系不完整。")
        content = package.xml(relation[0])
        label = item.get("Name") or item.get("NameU") or f"绘图页 {index+1}"
        shapes = list(content.iter("{"+V+"}Shape"))
        shape_text = {shape.get("ID"): "".join(shape.find("{"+V+"}Text").itertext()).strip()
                      if shape.find("{"+V+"}Text") is not None else "" for shape in shapes}
        image = _canvas("Visio · " + label)
        draw = ImageDraw.Draw(image)
        page_sheet = item.find("{"+V+"}PageSheet")
        settings = _cells(page_sheet) if page_sheet is not None else {}
        page_width, page_height = settings.get("PageWidth", 10), settings.get("PageHeight", 7)
        if page_width <= 0 or page_height <= 0:
            page_width, page_height = 10, 7
        sx, sy = 1180/page_width, 710/page_height
        positions, records = {}, []
        for shape in shapes:
            identifier, cells = shape.get("ID"), _cells(shape)
            x = 45 + cells.get("PinX", 1) * sx
            y = 90 + (page_height-cells.get("PinY", 1)) * sy
            width, height = max(60, cells.get("Width", 1)*sx), max(30, cells.get("Height", .5)*sy)
            positions[identifier] = (x, y)
            if shape_text[identifier]:
                draw.rounded_rectangle((x-width/2, y-height/2, x+width/2, y+height/2), radius=8, fill="#eef4ff", outline="#6583b4", width=2)
                _draw_text(draw, shape_text[identifier], (x-width/2+7,y-height/2+4,x+width/2-5,y+height/2-3),size=20)
                records.append(f"图形 {identifier}: {shape_text[identifier]}")
        connections = {}
        for connection in content.findall("{"+V+"}Connects/{"+V+"}Connect"):
            connections.setdefault(connection.get("FromSheet"), {})[connection.get("FromCell")] = connection.get("ToSheet")
        for endpoints in connections.values():
            left, right = endpoints.get("BeginX"), endpoints.get("EndX")
            if left in positions and right in positions:
                draw.line((positions[left], positions[right]), fill="#64748b", width=3)
                records.append(f"连接端点: {shape_text.get(left) or left} — {shape_text.get(right) or right}")
        pages.append(Page("Visio · " + label, "\n".join(records), image=image,
                          locators=[{"page_name": label, "page_part": relation[0], "shape_ids": list(shape_text)}]))
    if not pages:
        raise ValueError("Visio 文件没有可读取的绘图页。")
    warnings.append("Visio 预览仅按原生图形文本、基本位置和连接端点绘制；母版、复杂几何、箭头语义和嵌入图像不保证原样呈现。")
    return pages, warnings


def find_converter():
    candidates = [shutil.which("soffice")]
    for base in (os.environ.get("PROGRAMFILES", "C:/Program Files"), os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")):
        candidates.append(str(Path(base) / "LibreOffice/program/soffice.exe"))
    return next((Path(path) for path in candidates if path and Path(path).is_file()), None)


def _external_convert(source, destination, converter, timeout=90):
    # A unique process profile prevents attaching to or closing a user's Office
    # session. The private source copy also avoids source-lock side effects.
    working = destination / "native_conversion"
    working.mkdir()
    input_dir = working / "input"
    input_dir.mkdir()
    copied = input_dir / source.name
    shutil.copy2(source, copied)
    profile = working / ("profile-" + uuid.uuid4().hex)
    profile.mkdir()
    profile.joinpath("registrymodifications.xcu").write_text(
        '<?xml version="1.0" encoding="UTF-8"?><oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item><item oor:path="/org.openoffice.Office.Common/Save/Document"><prop oor:name="UpdateDocMode" oor:op="fuse"><value>3</value></prop></item></oor:items>', encoding="utf-8")
    arguments = [str(converter), "-env:UserInstallation=" + profile.as_uri(), "--headless", "--nologo", "--nodefault", "--norestore", "--convert-to", "pdf", "--outdir", str(working), str(copied)]
    process = subprocess.Popen(arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt" and process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            process.kill()
        process.wait()
        return None
    output = working / (source.stem + ".pdf")
    return output if process.returncode == 0 and output.is_file() and output.stat().st_size else None


def _write_readable_pdf(pages, pdf):
    from PIL import Image
    import pypdfium2 as pdfium
    output = pdfium.PdfDocument.new()
    try:
        for page in pages:
            buffer = BytesIO()
            with Image.open(BytesIO(page.image)) as image:
                image.save(buffer, "PDF", resolution=110)
            with pdfium.PdfDocument(buffer.getvalue()) as single:
                output.import_pages(single)
        output.save(pdf)
    finally:
        output.close()


def convert_office(source: Path, destination: Path, progress=None, *, use_converter=True, language="zh-CN") -> dict:
    token = _PREVIEW_LANGUAGE.set(normalize_language(language))
    try:
        return _convert_office(source, destination, progress, use_converter=use_converter)
    finally:
        _PREVIEW_LANGUAGE.reset(token)


def _convert_office(source: Path, destination: Path, progress=None, *, use_converter=True) -> dict:
    """Read a supported Office original into new PDF + native-text sidecar.

    destination must be a new managed directory. Returned Paths are local; the
    JSON sidecar uses only relative resources and source filename, never secrets.
    """
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source.suffix.lower() not in SUPPORTED or not source.is_file() or not source.stat().st_size:
        raise ValueError("请选择 PPTX、DOCX、XLSX 或 VSDX；旧版格式需要可用的转换组件。")
    if destination.exists() or destination == source or destination in source.parents:
        raise ValueError("Office 转换需要新的工作目录，不能覆盖原文件。")
    if progress:
        progress("读取 Office 原生内容…")
    pages, warnings = [], []
    external_relationships = False
    if source.suffix.lower() in OOXML:
        package = Package(source)
        try:
            external_relationships = package.has_external_relationships
            parser = {".pptx": _pptx, ".docx": _docx, ".xlsx": _xlsx, ".vsdx": _vsdx}[source.suffix.lower()]
            pages, warnings = parser(package)
        finally:
            package.close()
    converter = find_converter() if use_converter and not external_relationships else None
    if external_relationships:
        warnings.append("文档包含外部链接；本次只读取包内内容，不访问链接，也不交给外部转换器自动更新。")
    if not pages and converter is None:
        raise ValueError("此旧版 Office 格式需要转换组件；当前机器未检测到 LibreOffice。原件尚未修改。")
    destination.mkdir(parents=True)
    pdf = destination / "materials.pdf"
    kind = "derived_readable"
    if converter is not None:
        if progress:
            progress("使用已安装的转换组件生成预览…")
        try:
            converted = _external_convert(source, destination, converter)
            if converted:
                import pypdfium2 as pdfium
                with pdfium.PdfDocument(converted) as document:
                    if not len(document):
                        raise ValueError("转换器未生成有效页面。")
                    converted_text = []
                    for index in range(len(document)):
                        page = document[index]
                        textpage = page.get_textpage()
                        converted_text.append(textpage.get_text_range())
                        textpage.close()
                        page.close()
                shutil.copy2(converted, pdf)
                if source.suffix.lower() == ".pptx" and len(converted_text) == len(pages):
                    for index, text in enumerate(converted_text):
                        pages[index].text = text + ("\n\n课件演讲者备注（未证明录音讲到）：\n" + pages[index].notes if pages[index].notes else "")
                else:
                    pages = [Page(f"转换预览第 {i+1} 页", text) for i, text in enumerate(converted_text)]
                kind = "native_converter"
                warnings = [warning for warning in warnings if not warning.startswith(("内置课件预览", "Word 使用重排", "Excel 使用", "Visio 预览"))]
                warnings.append("预览由已安装的 LibreOffice 转换；复杂版式仍应对照保留的原件。")
        except (ValueError, OSError, subprocess.SubprocessError):
            warnings.append("已安装的转换组件未能完成预览，改用内置阅读预览。")
    if not pdf.exists():
        if not pages:
            raise ValueError("Office 转换未完成，原件已保留。")
        if progress:
            progress("生成内置阅读预览…")
        _write_readable_pdf(pages, pdf)
    ocr_paths = []
    for index, page in enumerate(pages):
        if kind == "derived_readable" and page.ocr_image is not None:
            folder = destination / "ocr_sources"
            folder.mkdir(exist_ok=True)
            ocr_path = folder / f"source-{index+1:04d}.png"
            ocr_path.write_bytes(page.ocr_image)
            ocr_paths.append(ocr_path.relative_to(destination).as_posix())
        else:
            ocr_paths.append(None)
    data = {"source_filename": source.name, "source_format": source.suffix.lower(),
            "preview_kind": kind, "uses_ai": False, "model_calls": 0,
            "pdf": pdf.name, "native_text_by_page": [page.text for page in pages],
            "page_labels": [page.label for page in pages], "notes_by_page": [page.notes for page in pages],
            "ocr_image_by_page": ocr_paths,
            "locators_by_page": [page.locators for page in pages], "warnings": list(dict.fromkeys(warnings))}
    (destination / "office_import.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**data, "pdf_path": pdf, "manifest_path": destination / "office_import.json"}


def prepare_document(paths: list[Path], destination: Path, progress=None):
    if not isinstance(paths, list) or len(paths) != 1:
        raise ValueError("当前每份项目请导入一份 Office 文档，图片可以另行整批导入。")
    result = convert_office(paths[0], destination, progress)
    return {**result, "sources": [Path(paths[0])], "conversions": [{"from": Path(paths[0]).suffix.lower(), "to": ".pdf", "preview_kind": result["preview_kind"]}]}
