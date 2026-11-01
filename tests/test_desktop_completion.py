"""Completion integration: real import/preview, fixture recognition, no paid AI."""
from io import BytesIO
import json
import os
from pathlib import Path
import wave
import zipfile

from PIL import Image, ImageDraw
import pytest

from visual_audio_notes.core import file_hash, read_json
from visual_audio_notes.desktop_store import DesktopStore


A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PACKAGE_R = 'http://schemas.openxmlformats.org/package/2006/relationships'


def make_pdf(path):
    text = b'BT /F1 18 Tf 25 90 Td (List concatenation creates a new list.) Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 540 180] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               b'<< /Length ' + str(len(text)).encode() + b' >>\nstream\n' + text + b'\nendstream']
    data, offsets = bytearray(b'%PDF-1.4\n'), [0]
    for i, value in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f'{i} 0 obj\n'.encode() + value + b'\nendobj\n')
    start = len(data)
    data.extend(f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:
        data.extend(f'{offset:010d} 00000 n \n'.encode())
    data.extend(f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF'.encode())
    path.write_bytes(data)


def make_image(path):
    image = Image.new('RGB', (600, 220), 'white')
    ImageDraw.Draw(image).text((30,80), 'Append mutates original list.', fill='black')
    image.save(path)
    image.close()


def make_audio(path, seconds=2):
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\0\0' * (16000 * seconds))


def make_subtitles(path):
    path.write_text('WEBVTT\n\n00:00:00.100 --> 00:00:01.900\nAppend mutates the original list, while concatenation returns a new list.\n', encoding='utf-8')


def make_slides(path, image):
    def rels(rows):
        return f'<Relationships xmlns="{PACKAGE_R}">' + ''.join(
            f'<Relationship Id="{identifier}" Type="{R}/{kind}" Target="{target}"/>'
            for identifier, kind, target in rows) + '</Relationships>'
    def slide(text='', with_image=False):
        shape = (f'<p:sp><p:spPr><a:xfrm><a:off x="200000" y="200000"/><a:ext cx="11000000" cy="3000000"/></a:xfrm></p:spPr><p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>' if text else '')
        picture = ('<p:pic><p:spPr><a:xfrm><a:off x="300000" y="3400000"/><a:ext cx="7000000" cy="2200000"/></a:xfrm></p:spPr><p:blipFill><a:blip r:embed="picture"/></p:blipFill></p:pic>' if with_image else '')
        return f'<p:sld xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}"><p:cSld><p:spTree>{shape}{picture}</p:spTree></p:cSld></p:sld>'
    parts = {
        'ppt/presentation.xml': f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst><p:sldId id="1" r:id="one"/><p:sldId id="2" r:id="two"/></p:sldIdLst><p:sldSz cx="12192000" cy="6858000"/></p:presentation>',
        'ppt/_rels/presentation.xml.rels': rels([('one','slide','slides/slide1.xml'),('two','slide','slides/slide2.xml')]),
        'ppt/slides/slide1.xml': slide('拼接产生新列表，追加修改原列表。', True),
        'ppt/slides/slide2.xml': slide(with_image=True),
        'ppt/slides/_rels/slide1.xml.rels': rels([('picture','image','../media/picture.png'),('note','notesSlide','../notesSlides/note.xml')]),
        'ppt/slides/_rels/slide2.xml.rels': rels([('picture','image','../media/picture.png')]),
        'ppt/notesSlides/note.xml': f'<p:notes xmlns:p="{P}" xmlns:a="{A}"><a:p><a:r><a:t>备注补充：检查别名共享。</a:t></a:r></a:p></p:notes>',
        'ppt/media/picture.png': image.read_bytes(),
    }
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as package:
        for name, value in parts.items():
            package.writestr(name, value)


@pytest.fixture
def materials(tmp_path):
    root = tmp_path / '用户 原件'
    root.mkdir()
    pdf, image, audio, subtitle, slides = [root/name for name in
        ('讲义.pdf','照片.png','独立录音.wav','已有转写.vtt','原课件.pptx')]
    make_pdf(pdf); make_image(image); make_audio(audio); make_subtitles(subtitle); make_slides(slides,image)
    return {'root':root,'pdf':pdf,'image':image,'audio':audio,'subtitle':subtitle,'slides':slides}


@pytest.fixture
def ready_model(monkeypatch):
    from visual_audio_notes import model_assets
    from visual_audio_notes.desktop_recognition import RecognitionSettings
    monkeypatch.setattr(model_assets, 'asr_model_status', lambda *args, **kwargs: {
        'id':'whisper-small','model':'small','ready':True,'download_bytes':0,'license':'MIT','path':'fixture-only'})
    original = RecognitionSettings.public
    def fixture_engines(self):
        result = original(self)
        result['asr']['local_engine_available'] = True
        result['ocr']['local_engine_available'] = True
        return result
    monkeypatch.setattr(RecognitionSettings,'public',fixture_engines)
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)


def finish(store):
    store._thread.join(timeout=15)
    assert not store._thread.is_alive()
    assert store.state()['job']['state'] == 'complete', store.state()['job']


def test_direct_office_pdf_and_photos_preserve_native_content_notes_and_originals(tmp_path, materials, monkeypatch):
    monkeypatch.setattr('visual_audio_notes.office_import.find_converter', lambda:None)
    selected = [materials[name] for name in ('slides','pdf','image','audio','subtitle')]
    hashes = {path:file_hash(path) for path in selected}
    store = DesktopStore(tmp_path/'workspace')
    project = store.import_files(selected, '一堂课的混合材料')
    assert project['counts'] == {'pages':4,'segments':1,'pending':1}
    first = next(page for page in project['pages'] if page.get('original_filename') == materials['slides'].name and page.get('original_page') == 1)
    assert '拼接产生新列表' in first['text'] and '备注补充：检查别名共享' in first['text']
    assert '未证明录音讲到' in first['text']
    assert first['preview_kind'] == 'derived_readable'
    assert first['preview_warnings']
    prepared = store.root/'projects'/project['id']/'prepared'
    assert read_json(prepared/'archive_binding.json')['prepared_hashes']['features.json'] == file_hash(prepared/'features.json')
    assert all((store.root/page['image']).is_file() for page in project['pages'])
    assert {path:file_hash(path) for path in selected} == hashes


def test_raw_audio_and_image_complete_recognition_candidates_without_text_llm(tmp_path, materials, ready_model, monkeypatch):
    from visual_audio_notes import recognition
    calls = []
    def transcribe(audio, config, cache_dir, progress=None, *, cancel_event=None):
        assert cancel_event is not None and not cancel_event.is_set()
        calls.append(('asr',audio,config))
        return {'segments':[{'segment_id':'a_fixture','start':.1,'end':1.9,'text':'Append mutates the original list.'}],
                'uses_ai':True,'provider':'fixture_asr','cache_hit':False,'model':'fixture-speech'}
    def ocr(pages, config, cache_dir, progress=None, *, cancel_event=None):
        assert cancel_event is not None and not cancel_event.is_set()
        calls.append(('ocr',pages,config))
        return {'texts':{page['unit_id']:'Append mutates the original list.' for page in pages if not page['text']},
                'uses_ai':True,'failures':[],'provider':'fixture_ocr'}
    monkeypatch.setattr(recognition,'transcribe',transcribe)
    monkeypatch.setattr(recognition,'recognize_pages',ocr)
    store = DesktopStore(tmp_path/'workspace')
    original_hashes = {path:file_hash(path) for path in (materials['image'],materials['audio'])}
    project = store.import_files(list(original_hashes))
    assert not project['can_ai'] and project['pipeline_needs'] == {'asr':True,'ocr':True}
    store.start_ai(project['id']); finish(store)
    prepared_project = store.project(project['id'])
    assert [item[0] for item in calls] == ['asr','ocr']
    assert prepared_project['pipeline_needs'] == {'asr':False,'ocr':False}
    assert prepared_project['transcript_origin'] == 'generated_asr'
    assert prepared_project['topics'] == {'audio':[],'courseware':[]}
    assert prepared_project['status'] == 'prepared'
    assert prepared_project['review_items'][0]['candidates']
    assert prepared_project['review_items'][0]['status'] == 'needs_review'
    assert not prepared_project['review_items'][0]['confirmed_unit_ids']
    # Manual correction remains available after recognition even without LLM.
    reviewed = store.decide(project['id'],prepared_project['chunks'][0]['segment_id'],[prepared_project['pages'][0]['unit_id']])
    assert reviewed['review_items'][0]['status'] == 'confirmed'
    assert {path:file_hash(path) for path in original_hashes} == original_hashes
    root = store.root/'projects'/project['id']
    assert list((root/'prepared_history').iterdir())
    assert read_json(root/'prepared/archive_binding.json')['prepared_hashes']['recognition_sources.json'] == file_hash(root/'prepared/recognition_sources.json')
    store.start_ai(project['id']); finish(store)
    assert len(calls) == 2  # Existing transcript/native OCR text is reused.
    assert store.project(project['id'])['review_items'][0]['status'] == 'confirmed'


def test_recognized_archive_labels_generated_text_and_keeps_sources(tmp_path, materials, ready_model, monkeypatch):
    from visual_audio_notes import recognition
    monkeypatch.setattr(recognition,'transcribe',lambda *args,**kwargs: {
        'segments':[{'segment_id':'a_fixture','start':.1,'end':1.9,'text':'List concatenation creates a new list.'}],
        'uses_ai':True,'provider':'fixture_asr','model':'fixture-speech','cache_hit':False})
    store = DesktopStore(tmp_path/'workspace')
    project = store.import_files([materials['pdf'],materials['audio']])
    store.start_ai(project['id']); finish(store)
    out = tmp_path/'archive'
    store.export_project(project['id'],out)
    manifest = read_json(out/'project.json')
    assert manifest['uses_ai'] is True  # Recognition is AI even without a text LLM.
    assert manifest['transcript_kind'] == 'generated_asr'
    assert (out/'转写稿.md').is_file()
    assert file_hash(out/'sources/materials.pdf') == file_hash(materials['pdf'])
    assert file_hash(out/'sources/recording.wav') == file_hash(materials['audio'])
    restored = DesktopStore(store.root).project(project['id'])
    assert restored['transcript_origin'] == 'generated_asr' and restored['export_path'] == str(out.resolve())


def test_later_ocr_retry_keeps_original_asr_provenance_and_previous_ocr_output(tmp_path, materials, ready_model, monkeypatch):
    from visual_audio_notes import recognition
    asr_calls = []
    def asr(*args, **kwargs):
        asr_calls.append(True)
        return {'segments':[{'segment_id':'a_original','start':.1,'end':1.9,'text':'Append changes the original list.'}],
                'uses_ai':True, 'identity':{'provider':'original-asr','revision':'pinned-fixture'}}
    attempts = []
    def ocr(pages, *args, **kwargs):
        uid = pages[0]['unit_id']
        attempts.append(True)
        if len(attempts) == 1:
            return {'texts':{}, 'failures':[{'unit_id':uid}], 'uses_ai':True, 'warning':'first attempt failed'}
        return {'texts':{uid:'Append changes the original list.'}, 'failures':[], 'uses_ai':True}
    monkeypatch.setattr(recognition,'transcribe',asr)
    monkeypatch.setattr(recognition,'recognize_pages',ocr)
    store = DesktopStore(tmp_path/'workspace')
    project = store.import_files([materials['image'],materials['audio']])
    store.start_ai(project['id']); finish(store)
    assert store.project(project['id'])['pipeline_needs']['ocr']
    store.start_ai(project['id']); finish(store)
    assert len(asr_calls) == 1 and len(attempts) == 2
    root = store.root/'projects'/project['id']
    sources = read_json(root/'prepared/recognition_sources.json')
    assert sources['asr']['identity']['provider'] == 'original-asr'
    assert len(sources['history']) == 2
    assert sources['history'][0]['ocr']['warning'] == 'first attempt failed'
    assert sources['history'][1]['asr'] is None
    from visual_audio_notes.task_runtime import atomic_json
    atomic_json(root/'.recognition-cache/attempts/failed-speech.json', {
        'purpose':'speech_to_text','status':'failed_usage_unknown','audio_seconds':2,'usage':{}})
    atomic_json(root/'.recognition-cache/attempts/failed-ocr.json', {
        'purpose':'ocr','status':'invalid_response','usage':{'prompt_tokens':10,'completion_tokens':2,'total_tokens':12}})
    store.export_project(project['id'], tmp_path/'archive')
    archived = read_json(tmp_path/'archive/识别记录.json')
    assert archived['asr']['identity']['revision'] == 'pinned-fixture'
    accounting = read_json(tmp_path/'archive/识别用量.json')['usage']
    assert accounting['api_attempts'] == 2
    assert accounting['audio_usage']['submitted_audio_seconds'] == 2
    assert accounting['audio_usage']['provider_reported_totals']['seconds'] is None
    assert accounting['reported_token_totals']['total_tokens'] == 12


def test_office_ocr_only_receives_clean_source_image_and_skips_native_text_pages(tmp_path, materials, ready_model, monkeypatch):
    from visual_audio_notes import recognition
    monkeypatch.setattr('visual_audio_notes.office_import.find_converter', lambda:None)
    store = DesktopStore(tmp_path/'workspace')
    project = store.import_files([materials['slides'],materials['pdf'],materials['audio'],materials['subtitle']])
    received = []
    def ocr(pages,config,cache_dir,progress=None,*,cancel_event=None):
        received.extend(pages)
        assert len(pages) == 1  # Native slide text+notes and native PDF were reused.
        clean = pages[0]['image']
        assert clean.parent.name == 'ocr_inputs'
        with Image.open(clean) as image:
            assert image.size == (1200,730)  # Slide content crop excludes generated banners.
        return {'texts':{pages[0]['unit_id']:'Append mutates original list.'},'failures':[],'uses_ai':True}
    monkeypatch.setattr(recognition,'recognize_pages',ocr)
    monkeypatch.setattr(recognition,'transcribe',lambda *args,**kwargs:pytest.fail('Existing VTT must not be transcribed again'))
    store.start_ai(project['id']); finish(store)
    assert len(received) == 1
    current = store.project(project['id'])
    assert current['pipeline_needs'] == {'asr':False,'ocr':False}
    assert any('备注补充：检查别名共享' in page['text'] for page in current['pages'])
    assert all('自动生成的阅读预览' not in page['text'] for page in current['pages'])


def test_ocr_source_change_invalidates_old_manual_decision_and_retains_old_binding(tmp_path, materials, ready_model, monkeypatch):
    from visual_audio_notes.desktop_prepare import prepare_recognition
    from visual_audio_notes import recognition
    store = DesktopStore(tmp_path/'workspace')
    project = store.import_files([materials['image'],materials['audio'],materials['subtitle']])
    store.decide(project['id'],project['chunks'][0]['segment_id'],[project['pages'][0]['unit_id']])
    root = store.root/'projects'/project['id']
    old_binding = file_hash(root/'prepared/archive_binding.json')
    old_features = file_hash(root/'prepared/features.json')
    monkeypatch.setattr(recognition,'recognize_pages',lambda pages,*args,**kwargs: {
        'texts':{page['unit_id']:'Append mutates the original list.' for page in pages},'failures':[],'uses_ai':True})
    result = prepare_recognition(root,store.recognition,lambda *args:None)
    assert result['changed']
    current = store.project(project['id'])
    assert current['review_items'][0]['status'] == 'needs_review'
    assert not current['review_items'][0]['confirmed_unit_ids']
    history = next((root/'prepared_history').iterdir())
    assert file_hash(history/'archive_binding.json') == old_binding
    assert file_hash(history/'features.json') == old_features
    assert read_json(root/'prepared/review.json')['history'][-1]['event'] == 'inputs_changed'


def test_recognition_credentials_are_independent_and_never_enter_project_json(tmp_path, monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY',raising=False)
    monkeypatch.setenv('VAN_RECOGNITION_ASR','previous-asr-env')
    store = DesktopStore(tmp_path/'workspace')
    text_key, asr_key, ocr_key = 'fixture-text-key-unique','fixture-asr-key-unique','fixture-ocr-key-unique'
    store.save_connection({'api_key':text_key})
    public = store.save_recognition({
        'asr':{'provider':'openai_compatible_asr','endpoint':'https://speech.example/v1','model':'whisper-1','api_key':asr_key},
        'ocr':{'provider':'deepseek_vision','endpoint':'https://api.deepseek.com','model':'deepseek-flash','api_key':ocr_key}})
    assert public['asr']['key_configured'] and public['ocr']['key_configured']
    assert store.public_connection()['key_configured']
    assert not any(key in json.dumps(store.state()) for key in (text_key,asr_key,ocr_key))
    with pytest.raises(RuntimeError):
        with store.recognition.credentials():
            assert os.environ['VAN_RECOGNITION_ASR'] == asr_key
            assert os.environ['VAN_RECOGNITION_OCR'] == ocr_key
            raise RuntimeError('fixture interruption')
    assert os.environ['VAN_RECOGNITION_ASR'] == 'previous-asr-env'
    assert 'VAN_RECOGNITION_OCR' not in os.environ
    for path in store.root.rglob('*'):
        if path.is_file():
            assert all(key.encode() not in path.read_bytes() for key in (text_key,asr_key,ocr_key))
    store.save_recognition({'asr':{'endpoint':'https://different-speech.example/v1'}})
    assert not store.recognition.public()['asr']['key_configured']
    assert store.recognition.public()['ocr']['key_configured']
    assert store.public_connection()['key_configured']


def test_cancel_during_recognition_preserves_current_preparation(tmp_path, materials, ready_model, monkeypatch):
    from visual_audio_notes import recognition
    called_ocr = []
    def asr(audio,config,cache_dir,progress=None,*,cancel_event=None):
        assert cancel_event is not None
        cancel_event.set()
        return {'segments':[{'segment_id':'a_cancelled','start':.1,'end':1.9,'text':'Temporary recognized words.'}], 'uses_ai':True}
    monkeypatch.setattr(recognition,'transcribe',asr)
    monkeypatch.setattr(recognition,'recognize_pages',lambda *args,**kwargs:called_ocr.append(True))
    store = DesktopStore(tmp_path/'workspace')
    project = store.import_files([materials['image'],materials['audio']])
    root = store.root/'projects'/project['id']
    before = file_hash(root/'prepared/features.json')
    store.start_ai(project['id'])
    store._thread.join(timeout=10)
    assert not store._thread.is_alive()
    assert store.state()['job']['state'] == 'cancelled'
    assert not called_ocr
    assert file_hash(root/'prepared/features.json') == before
    assert store.project(project['id'])['segments'] == []


def test_late_asr_cues_keep_original_audio_axis_through_generated_vtt_and_archive(tmp_path, materials, ready_model, monkeypatch):
    from visual_audio_notes import recognition
    make_audio(materials['audio'],seconds=20)
    segments = [
        {'segment_id':'a_after_silence','start':12.25,'end':13.75,'text':'List concatenation creates a new list.'},
        {'segment_id':'a_later','start':16.0,'end':18.25,'text':'A returned value is different from a changed object.'},
    ]
    monkeypatch.setattr(recognition,'transcribe',lambda *args,**kwargs: {
        'segments':segments,'uses_ai':True,'provider':'fixture_asr','model':'fixture-speech','cache_hit':False})
    store = DesktopStore(tmp_path/'workspace')
    project = store.import_files([materials['pdf'],materials['audio']])
    store.start_ai(project['id']); finish(store)
    current = store.project(project['id'])
    assert [(item['start'],item['end']) for item in current['segments']] == [(12.25,13.75),(16.0,18.25)]
    assert [(item['start'],item['end']) for item in current['chunks']] == [(12.25,13.75),(16.0,18.25)]
    root = store.root/'projects'/project['id']
    meta = read_json(root/'project.json')
    subtitle = (root/meta['transcript']).read_text(encoding='utf-8')
    assert '00:00:12.250 --> 00:00:13.750' in subtitle
    assert '00:00:16.000 --> 00:00:18.250' in subtitle
    store.decide(project['id'],'a_after_silence',[current['pages'][0]['unit_id']])
    out = tmp_path/'late-audio-archive'
    store.export_project(project['id'],out)
    manifest = read_json(out/'project.json')
    assert [(item['start'],item['end']) for item in manifest['timeline']] == [(12.25,13.75),(16.0,18.25)]
    assert manifest['timeline'][0]['unit_id'] == current['pages'][0]['unit_id']
    assert manifest['timeline'][1]['unit_id'] is None


def test_explicit_transcript_edit_preserves_raw_vtt_history_binding_and_no_ai_export(tmp_path,materials,monkeypatch):
    from visual_audio_notes.binding import validate_binding
    from visual_audio_notes import recognition
    monkeypatch.delenv('DEEPSEEK_API_KEY',raising=False)
    monkeypatch.setattr(recognition,'transcribe',lambda *args,**kwargs:pytest.fail('Explicit correction must not rerun ASR'))
    store = DesktopStore(tmp_path/'workspace')
    project = store.import_files([materials['pdf'],materials['audio'],materials['subtitle']])
    root = store.root/'projects'/project['id']
    before_meta = read_json(root/'project.json')
    original_vtt = root/before_meta['transcript']
    before_vtt = original_vtt.read_bytes()
    before_binding = file_hash(root/'prepared/archive_binding.json')
    before_features = file_hash(root/'prepared/features.json')
    cue = project['segments'][0]
    store.decide(project['id'],cue['segment_id'],[project['pages'][0]['unit_id']])
    changed = store.edit_text(project['id'],'transcript',cue['segment_id'],
                              'Concatenation returns a new outer list; nested objects may still be shared.',start=.25,end=1.75)
    assert original_vtt.read_bytes() == before_vtt
    assert materials['subtitle'].read_bytes() == before_vtt
    assert changed['transcript_origin'] == 'user_edited'
    assert changed['segments'][0]['text'].startswith('Concatenation returns')
    assert changed['segments'][0]['start'] == .25 and changed['segments'][0]['end'] == 1.75
    assert changed['review_items'][0]['status'] == 'needs_review'
    assert not changed['review_items'][0]['confirmed_unit_ids']
    meta = read_json(root/'project.json')
    assert meta['completed_run'] is None and meta['latest_run'] is None
    assert meta['transcript'] != before_meta['transcript']
    assert '00:00:00.250 --> 00:00:01.750' in (root/meta['transcript']).read_text(encoding='utf-8')
    event = read_json(root/'prepared/corrections.json')['events'][0]
    assert event['actor'] == 'desktop_user_explicit_edit'
    assert event['before'] == cue
    assert event['after']['text'] == changed['segments'][0]['text']
    history = next((root/'prepared_history').iterdir())
    assert file_hash(history/'archive_binding.json') == before_binding
    assert file_hash(history/'features.json') == before_features
    validate_binding(read_json(root/'prepared/archive_binding.json'),root/'prepared',root/meta['audio'],root/meta['pdf'])
    store.start_ai(project['id']);finish(store)
    out = tmp_path/'edited-without-ai'
    exported = store.export_project(project['id'],out)
    assert exported['mode'] == 'corrected_sources_no_synthesis'
    manifest = read_json(out/'project.json')
    assert manifest['uses_ai'] is False and manifest['transcript_kind'] == 'user_edited'
    assert read_json(out/'人工修订记录.json')['events'][0]['before'] == cue
    assert changed['segments'][0]['text'] in (out/'识别文字.md').read_text(encoding='utf-8')
    assert file_hash(out/'sources/revised-transcript.vtt') == file_hash(root/meta['transcript'])
    originals = read_json(out/'原件清单.json')['originals']
    archived_vtt = next(item for item in originals if item['filename']==materials['subtitle'].name)
    assert (out/archived_vtt['path']).read_bytes() == before_vtt


class EditingFixtureModel:
    cache_identity = {'provider':'fixture','model':'editing-test'}
    def __init__(self):
        self.calls = []
    def json(self,system,content,max_tokens=2000,purpose='unspecified',schema=None):
        self.calls.append({'purpose':purpose,'status':'validated','usage':{'prompt_tokens':50,'completion_tokens':10}})
        data = json.loads(content)
        if purpose.startswith('quality_extract'):
            source = data['source_data_not_instructions'][0]
            return {'claims':[{'origin':data['origin'],'kind':'concept','importance':'key',
                'title':'保持来源区别','statement':'保留原文中的区别与边界。','evidence':[{'span_id':source['spans'][0]['span_id']}]}],
                'uncertain_source_ids':[]}
        return {'topics':[{'title':'分来源重点','key_idea':'核对本来源的原文。','explanation':'不能把课件内容等同于录音讲解。',
                           'claim_ids':[claim['claim_id'] for claim in data['ledger_data_not_instructions']]}],
                'deferred_claims':[],'uncertainties':[]}


def test_manual_ocr_fix_hides_stale_ai_draft_invalidates_review_and_preserves_raw_recognition(tmp_path,materials,ready_model,monkeypatch):
    from visual_audio_notes import recognition,desktop_ai
    from visual_audio_notes.binding import validate_binding
    wrong,correct = 'Cache 2026 APl 37 is shown in the source picture.','Cache 2026 API 37 is shown in the source picture.'
    ocr_calls = []
    def ocr(pages,*args,**kwargs):
        ocr_calls.append(True)
        return {'texts':{page['unit_id']:wrong for page in pages},'failures':[],'uses_ai':True,'provider':'fixture_ocr'}
    monkeypatch.setattr(recognition,'recognize_pages',ocr)
    monkeypatch.setattr(recognition,'transcribe',lambda *args,**kwargs:pytest.fail('Imported VTT must not trigger ASR'))
    model = EditingFixtureModel()
    monkeypatch.setattr(desktop_ai,'create_json_model',lambda connection:model)
    store = DesktopStore(tmp_path/'workspace')
    store.save_connection({'api_key':'fixture-editing-key-not-real'})
    project = store.import_files([materials['pdf'],materials['image'],materials['audio'],materials['subtitle']])
    store.start_ai(project['id']);finish(store)
    generated = store.project(project['id'])
    assert any(generated['topics'].values())
    image_page = next(page for page in generated['pages'] if page.get('original_filename')==materials['image'].name)
    assert image_page['text'] == wrong
    store.decide(project['id'],generated['segments'][0]['segment_id'],[image_page['unit_id']])
    root = store.root/'projects'/project['id']
    meta = read_json(root/'project.json')
    old_run = root/'runs'/meta['completed_run']
    old_hashes = {path.name:file_hash(path) for path in old_run.iterdir() if path.is_file()}
    raw_ocr_hash = file_hash(root/'prepared/recognition_sources.json')
    old_binding_hash = file_hash(root/'prepared/archive_binding.json')
    edited = store.edit_text(project['id'],'page',image_page['unit_id'],correct)
    assert edited['topics'] == {'audio':[],'courseware':[]}
    assert edited['review_items'][0]['status'] == 'needs_review'
    assert not edited['review_items'][0]['confirmed_unit_ids']
    meta = read_json(root/'project.json')
    assert meta['completed_run'] is None and meta['latest_run'] is None
    assert {path.name:file_hash(path) for path in old_run.iterdir() if path.is_file()} == old_hashes
    assert file_hash(root/'prepared/recognition_sources.json') == raw_ocr_hash
    assert any(file_hash(path/'archive_binding.json') == old_binding_hash for path in (root/'prepared_history').iterdir() if (path/'archive_binding.json').is_file())
    validate_binding(read_json(root/'prepared/archive_binding.json'),root/'prepared',root/meta['audio'],root/meta['pdf'])
    store.save_connection({'clear_key':True})
    calls_before = len(model.calls)
    store.start_ai(project['id']);finish(store)
    assert len(ocr_calls) == 1 and len(model.calls) == calls_before
    out = tmp_path/'ocr-fixed-archive'
    store.export_project(project['id'],out)
    assert read_json(out/'project.json')['uses_ai'] is True
    assert read_json(out/'识别记录.json')['ocr']['texts'][image_page['unit_id']] == wrong
    revised = read_json(out/'识别文字.json')
    assert next(unit['text'] for unit in revised['units'] if unit['unit_id']==image_page['unit_id']) == correct
    event = read_json(out/'人工修订记录.json')['events'][0]
    assert event['before']['text'] == wrong and event['after']['text'] == correct


@pytest.mark.parametrize('start,end',[(1.8,1.2),(-.1,1.8),(True,1.8),(float('nan'),1.8)])
def test_invalid_transcript_edit_time_rejected_before_any_revision(tmp_path,materials,start,end):
    store = DesktopStore(tmp_path/'workspace')
    project = store.import_files([materials['pdf'],materials['audio'],materials['subtitle']])
    root = store.root/'projects'/project['id']
    before = {name:file_hash(root/name) for name in ('project.json','prepared/features.json','prepared/archive_binding.json')}
    with pytest.raises(ValueError):
        store.edit_text(project['id'],'transcript',project['segments'][0]['segment_id'],'A corrected statement.',start=start,end=end)
    assert {name:file_hash(root/name) for name in before} == before
    assert not (root/'prepared_history').exists()
    assert not list(root.glob('preparing-edit-*'))


def test_nonmonotonic_transcript_edit_is_rejected_without_history(tmp_path,materials):
    materials['subtitle'].write_text('WEBVTT\n\n00:00:00.100 --> 00:00:00.800\nFirst statement.\n\n00:00:01.000 --> 00:00:01.900\nSecond statement.\n',encoding='utf-8')
    store = DesktopStore(tmp_path/'workspace')
    project = store.import_files([materials['pdf'],materials['audio'],materials['subtitle']])
    root = store.root/'projects'/project['id']
    before = file_hash(root/'prepared/features.json')
    with pytest.raises(ValueError,match='前一段'):
        store.edit_text(project['id'],'transcript',project['segments'][1]['segment_id'],'Second revised.',start=.05,end=1.9)
    assert file_hash(root/'prepared/features.json') == before
    assert not (root/'prepared_history').exists()


def test_edit_text_http_route_returns_revised_project(tmp_path,materials):
    import urllib.request
    from visual_audio_notes.desktop_server import start_server
    store = DesktopStore(tmp_path/'workspace')
    project = store.import_files([materials['pdf'],materials['audio'],materials['subtitle']])
    server,url = start_server(store.root,Path(__file__).resolve().parents[1],store=store)
    try:
        payload = {'id':project['id'],'kind':'transcript','source_id':project['segments'][0]['segment_id'],
                   'text':'通过本机接口明确修订的文字。','start':.2,'end':1.7}
        request = urllib.request.Request(url+'/api/edit_text',data=json.dumps(payload).encode('utf-8'),
                                        headers={'X-Van-Token':server.token,'Content-Type':'application/json'})
        with urllib.request.urlopen(request) as response:
            result = json.load(response)
        assert result['ok'] and result['data']['segments'][0]['text'] == payload['text']
        assert result['data']['transcript_origin'] == 'user_edited'
    finally:
        server.shutdown();server.server_close()
