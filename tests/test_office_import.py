from io import BytesIO
import json
from pathlib import Path
import zipfile

from PIL import Image
import pypdfium2 as pdfium
import pytest

from visual_audio_notes.office_import import convert_office, prepare_document, Package


REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
RID = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
S = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
V = 'http://schemas.microsoft.com/office/visio/2012/main'


def make(path, entries):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            info = zipfile.ZipInfo()
            info.filename = name  # Preserve deliberately invalid test paths on Windows.
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, value)
    return path


def relationships(items):
    return f'<Relationships xmlns="{REL}">' + ''.join(
        f'<Relationship Id="{rid}" Type="{RID}/{kind}" Target="{target}"{extra}/>' for rid, kind, target, extra in items) + '</Relationships>'


def slide(text, image=False):
    return (f'<p:sld xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{RID}"><p:cSld><p:spTree>'
            '<p:sp><p:spPr><a:xfrm><a:off x="100000" y="200000"/><a:ext cx="11000000" cy="2000000"/></a:xfrm></p:spPr>'
            f'<p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>' +
            ('<p:pic><p:spPr><a:xfrm><a:off x="300000" y="3000000"/><a:ext cx="5000000" cy="3000000"/></a:xfrm></p:spPr><p:blipFill><a:blip r:embed="img"/></p:blipFill></p:pic>' if image else '') +
            '</p:spTree></p:cSld></p:sld>')


def image_bytes():
    stream = BytesIO()
    Image.new('RGB', (120, 80), '#e72837').save(stream, 'PNG')
    return stream.getvalue()


def assert_pdf(result, expected_pages):
    assert result['pdf_path'].is_file()
    with pdfium.PdfDocument(result['pdf_path']) as document:
        assert len(document) == expected_pages
        page = document[0]
        bitmap = page.render(scale=.5)
        image = bitmap.to_pil()
        assert image.width > 100 and image.height > 100
        assert len(image.getcolors(image.width * image.height)) > 10
        image.close()
        bitmap.close()
        page.close()
    assert result['uses_ai'] is False and result['model_calls'] == 0
    assert result['preview_kind'] == 'derived_readable'
    assert len(result['native_text_by_page']) == expected_pages


def test_pptx_uses_presentation_relationship_order_and_keeps_notes_and_images(tmp_path):
    source = make(tmp_path / '中文 课件.pptx', {
        'ppt/presentation.xml': f'<p:presentation xmlns:p="{P}" xmlns:r="{RID}"><p:sldIdLst><p:sldId id="2" r:id="second"/><p:sldId id="1" r:id="first"/></p:sldIdLst><p:sldSz cx="12192000" cy="6858000"/></p:presentation>',
        'ppt/_rels/presentation.xml.rels': relationships([('first','slide','slides/slide1.xml',''),('second','slide','slides/slide2.xml','')]),
        'ppt/slides/slide1.xml': slide('First numbered file is presented second'),
        'ppt/slides/slide2.xml': slide('中文重点：追加修改对象，拼接返回新列表。', True),
        'ppt/slides/_rels/slide2.xml.rels': relationships([('img','image','../media/image1.png',''),('notes','notesSlide','../notesSlides/notes1.xml','')]),
        'ppt/notesSlides/notes1.xml': f'<p:notes xmlns:p="{P}" xmlns:a="{A}"><a:p><a:r><a:t>讲者备注只能算课件补充。</a:t></a:r></a:p></p:notes>',
        'ppt/media/image1.png': image_bytes(),
    })
    before = source.read_bytes()
    result = convert_office(source, tmp_path / 'converted', use_converter=False)
    assert_pdf(result, 2)
    assert result['native_text_by_page'][0].startswith('中文重点')
    assert '讲者备注只能算课件补充' in result['notes_by_page'][0]
    assert '未证明录音讲到' in result['native_text_by_page'][0]
    assert result['native_text_by_page'][1].startswith('First numbered')
    assert source.read_bytes() == before
    manifest = json.loads(result['manifest_path'].read_text(encoding='utf-8'))
    assert str(tmp_path) not in json.dumps(manifest)
    with pdfium.PdfDocument(result['pdf_path']) as document:
        page = document[0]
        bitmap = page.render(scale=.5)
        image = bitmap.to_pil().convert('RGB')
        assert any((lambda c: c[0] > 180 and c[1] < 90 and c[2] < 100)(image.getpixel((x,y)))
                   for x in range(0,image.width,10) for y in range(0,image.height,10))
        image.close(); bitmap.close(); page.close()


def test_docx_keeps_paragraph_table_and_embedded_picture_as_reading_pages(tmp_path):
    source = make(tmp_path / 'notes.docx', {
        'word/document.xml': f'<w:document xmlns:w="{W}" xmlns:a="{A}" xmlns:r="{RID}"><w:body><w:p><w:r><w:t>中文段落与概念条件。</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>操作</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>返回值</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p><w:r><w:drawing><a:blip r:embed="image"/></w:drawing></w:r></w:p></w:body></w:document>',
        'word/_rels/document.xml.rels': relationships([('image','image','media/img.png','')]),
        'word/media/img.png': image_bytes(),
    })
    result = convert_office(source, tmp_path / 'result', use_converter=False)
    assert_pdf(result, 2)
    assert '中文段落' in result['native_text_by_page'][0]
    assert '操作 | 返回值' in result['native_text_by_page'][0]
    assert result['native_text_by_page'][1] == ''  # image needs OCR, never invented text
    assert result['locators_by_page'][1][0]['image_part'] == 'word/media/img.png'
    clean_image = result['pdf_path'].parent / result['ocr_image_by_page'][1]
    with Image.open(clean_image) as image:
        assert image.size == (120,80)  # no program-generated header/footer enters OCR


def test_xlsx_preserves_shared_strings_inline_strings_formula_and_cell_locations(tmp_path):
    source = make(tmp_path / 'book.xlsx', {
        'xl/workbook.xml': f'<workbook xmlns="{S}" xmlns:r="{RID}"><sheets><sheet name="课堂统计" sheetId="1" r:id="sheet"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': relationships([('sheet','worksheet','worksheets/sheet1.xml','')]),
        'xl/sharedStrings.xml': f'<sst xmlns="{S}"><si><t>概念</t></si></sst>',
        'xl/worksheets/sheet1.xml': f'<worksheet xmlns="{S}"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="inlineStr"><is><t>覆盖数量</t></is></c></row><row r="2"><c r="B2"><f>SUM(B3:B4)</f><v>7</v></c><c r="C2"><f>OTHER()</f></c></row></sheetData></worksheet>',
    })
    result = convert_office(source, tmp_path / 'result', use_converter=False)
    assert_pdf(result, 1)
    text = result['native_text_by_page'][0]
    assert 'A1: 概念' in text and 'B1: 覆盖数量' in text
    assert 'B2: 7 [公式: =SUM(B3:B4)]' in text
    assert '无缓存值' in text and any('不重算' in item for item in result['warnings'])
    assert result['locators_by_page'][0][0]['sheet'] == '课堂统计'


def test_visio_keeps_page_shape_text_and_neutral_connection_endpoints(tmp_path):
    source = make(tmp_path / 'diagram.vsdx', {
        'visio/pages/pages.xml': f'<Pages xmlns="{V}" xmlns:r="{RID}"><Page ID="0" Name="流程"><Rel r:id="page"/></Page></Pages>',
        'visio/pages/_rels/pages.xml.rels': relationships([('page','page','page1.xml','')]),
        'visio/pages/page1.xml': f'<PageContents xmlns="{V}"><Shapes><Shape ID="1"><Cell N="PinX" V="2"/><Cell N="PinY" V="4"/><Text>读取资料</Text></Shape><Shape ID="2"><Cell N="PinX" V="6"/><Cell N="PinY" V="4"/><Text>人工核对</Text></Shape><Shape ID="3"/></Shapes><Connects><Connect FromSheet="3" FromCell="BeginX" ToSheet="1"/><Connect FromSheet="3" FromCell="EndX" ToSheet="2"/></Connects></PageContents>',
    })
    result = convert_office(source, tmp_path / 'result', use_converter=False)
    assert_pdf(result, 1)
    assert '读取资料' in result['native_text_by_page'][0]
    assert '连接端点: 读取资料 — 人工核对' in result['native_text_by_page'][0]
    assert '->' not in result['native_text_by_page'][0]


@pytest.mark.parametrize('name', ['../escape.xml', '/absolute.xml', 'a\\file.xml', 'C:/file.xml'])
def test_archive_paths_cannot_escape_package(tmp_path, name):
    source = make(tmp_path / 'bad.pptx', {name: '<a/>'})
    with pytest.raises(ValueError, match='安全解析'):
        Package(source)


def test_xml_entities_are_not_resolved_and_external_relationships_are_not_fetched(tmp_path):
    source = make(tmp_path / 'doc.docx', {
        'word/document.xml': '<!DOCTYPE doc [<!ENTITY test SYSTEM "file:///secret">]><doc>&test;</doc>',
        'word/_rels/document.xml.rels': relationships([('outside','image','https://external.invalid/private', ' TargetMode="External"')]),
    })
    package = Package(source)
    try:
        assert package.relationships('word/document.xml') == {}
        with pytest.raises(ValueError, match='实体'):
            package.xml('word/document.xml')
    finally:
        package.close()


def test_invalid_or_legacy_without_converter_cannot_create_fake_preview(tmp_path, monkeypatch):
    monkeypatch.setattr('visual_audio_notes.office_import.find_converter', lambda: None)
    bad = tmp_path / 'broken.pptx'
    bad.write_bytes(b'not an office archive')
    with pytest.raises(ValueError):
        convert_office(bad, tmp_path / 'bad-output')
    assert not (tmp_path / 'bad-output').exists()
    old = tmp_path / 'slides.ppt'
    old.write_bytes(b'legacy fixture')
    with pytest.raises(ValueError, match='旧版'):
        convert_office(old, tmp_path / 'old-output')
    assert not (tmp_path / 'old-output').exists()


def test_existing_destination_and_multiple_documents_are_preserved(tmp_path):
    source = tmp_path / 'a.pptx'
    source.write_bytes(b'exists')
    destination = tmp_path / 'keep'
    destination.mkdir()
    marker = destination / 'original.txt'
    marker.write_text('keep', encoding='utf-8')
    with pytest.raises(ValueError, match='不能覆盖'):
        convert_office(source, destination)
    assert marker.read_text(encoding='utf-8') == 'keep'
    with pytest.raises(ValueError, match='一份'):
        prepare_document([source, source], tmp_path / 'new')
