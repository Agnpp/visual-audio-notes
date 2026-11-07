import json
from pathlib import Path
import re

import pytest

from visual_audio_notes.core import file_hash, read_json
from visual_audio_notes.desktop_locale import catalog, localize_payload, translate
from visual_audio_notes.desktop_store import DesktopStore
from test_desktop_completion import materials


def test_application_messages_are_localized_but_all_user_text_is_untouched():
    original = {"title":"全部资料", "manual_notes":"已归档", "summary":"原件已保留，可阅读、回听和手动记录",
        "pages":[{"text":"全部资料", "original_filename":"已归档.pdf", "preview_warnings":["Word 使用重排阅读页，不等同于原始页码；复杂版式、图片、公式和批注请核对原件。"]}],
        "topics":{"audio":[{"title":"全部资料","summary":"已归档","evidence":[{"quote":"已归档"}]}]},
        "segments":[{"text":"已归档"}], "message":"正在请求语音识别，第 2/4 段…"}
    english=localize_payload(original, 'en')
    assert english['message']=='Requesting speech recognition, chunk 2/4…'
    assert english['title']==original['title'] and english['manual_notes']==original['manual_notes']
    assert english['pages'][0]['text']=='全部资料'
    assert english['pages'][0]['original_filename']=='已归档.pdf'
    assert english['topics']==original['topics'] and english['segments']==original['segments']
    assert english['pages'][0]['preview_warnings'][0].startswith('Word uses reflowed')
    assert original['summary'].startswith('原件')  # Localization does not mutate storage.
    assert localize_payload(original,'zh-CN')==original
    assert translate('识别 API 请求失败（HTTP 429），未自动重试或切换服务。','en').startswith('Recognition API request failed (HTTP 429)')


def test_language_preference_survives_restart_and_invalid_values_never_replace_it(tmp_path):
    store=DesktopStore(tmp_path/'workspace')
    store.save_preferences({'language':'en'})
    assert DesktopStore(store.root).state()['preferences']=={'language':'en'}
    for invalid in [{'language':'de'}, {'language':'en','api_key':'do-not-store'}, ['en']]:
        with pytest.raises(ValueError): store.save_preferences(invalid)
    assert read_json(store.root/'preferences.json')=={'language':'en'}
    store._job={'state':'running'}
    with pytest.raises(ValueError): store.save_preferences({'language':'zh-CN'})


def test_english_archive_keeps_original_quotes_manual_notes_and_confirmed_mapping(tmp_path, materials, monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY',raising=False)
    store=DesktopStore(tmp_path/'workspace')
    project=store.import_files([materials['pdf'],materials['audio'],materials['subtitle']], '全部资料 <script>alert(1)</script>')
    store.save_notes(project['id'],'已归档 <img src=x onerror=alert(1)>')
    store.decide(project['id'],project['segments'][0]['segment_id'],[project['pages'][0]['unit_id']])
    project_path=store.root/'projects'/project['id']
    before=file_hash(project_path/'prepared/features.json')
    out=tmp_path/'archive'
    result=store.export_project(project['id'],out,language='en')
    assert result['reader_entry']=='index.html'
    doc=(out/'index.html').read_text(encoding='utf-8')
    assert '<html lang="en">' in doc and 'No AI summary generated' in doc
    assert '已归档 &lt;img src=x onerror=alert(1)&gt;' in doc
    assert '<script>alert(1)</script>' not in doc
    assert 'Append mutates the original list' in doc
    payload=json.loads(re.search(r'id="archive-data">(.*?)</script>',doc,re.S)[1])
    import base64, hashlib
    script=re.search(r'<script>(.*?)</script>',doc,re.S)[1]
    assert 'sha256-'+base64.b64encode(hashlib.sha256(script.encode()).digest()).decode() in doc
    assert all((out/unit['image']).is_file() for unit in payload['units'].values())
    assert payload['timeline'][0]['unit_id']==project['pages'][0]['unit_id']
    assert file_hash(project_path/'prepared/features.json')==before
    assert (out/'阅读.html').is_file() and (out/'notes.md').is_file()
    assert read_json(out/'archive_checks.json')['english_reader']


def test_partial_english_archive_remains_readable_without_recording(tmp_path, materials):
    store=DesktopStore(tmp_path/'workspace')
    project=store.import_files([materials['pdf']])
    out=tmp_path/'archive'
    store.export_project(project['id'],out,language='en')
    assert (out/'index.html').is_file()
    assert 'Document page 1' in (out/'index.html').read_text(encoding='utf-8')
    assert (out/'assets'/ (project['pages'][0]['unit_id']+'.png')).is_file()


def test_catalog_covers_all_chinese_javascript_ui_literals_and_both_prompt_variants():
    import ast
    root=Path(__file__).resolve().parents[1]
    source=(root/'src/visual_audio_notes/desktop_ui/app.js').read_text(encoding='utf-8')
    for match in re.finditer(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"",source):
        value=ast.literal_eval(match.group())
        if re.search(r'[\u3400-\u9fff]',value):
            assert value in catalog(), value
    for name in ['quality_spans','topic_notes']:
        zh=(root/'prompts'/ (name+'.txt')).read_text(encoding='utf-8')
        en=(root/'prompts'/ (name+'.en.txt')).read_text(encoding='utf-8')
        assert 'English' in en and en!=zh


def test_english_ai_prompts_language_cache_and_export_keep_origins_separate(tmp_path, materials, monkeypatch):
    from test_desktop_store import FixtureModel, finish
    from visual_audio_notes import desktop_ai
    class LanguageModel(FixtureModel):
        def __init__(self):
            super().__init__()
            self.prompts, self.requests = [], []
        def json(self, system, content, *args, **kwargs):
            self.prompts.append(system)
            self.requests.append(json.loads(content))
            result=super().json(system,content,*args,**kwargs)
            if 'English' in system and kwargs.get('purpose','').startswith('quality_extract'):
                for claim in result['claims']:
                    claim.update(title='List concatenation',statement='List concatenation creates a new list.')
            elif 'in English' in system:
                for topic in result['topics']:
                    topic.update(title='Lists and new objects',key_idea='Concatenation creates a new list.',explanation='Based on this source excerpt.')
            return result
    model=LanguageModel()
    monkeypatch.setattr(desktop_ai,'create_json_model',lambda connection:model)
    store=DesktopStore(tmp_path/'workspace')
    store.save_connection({'api_key':'fixture-not-a-real-key'})
    project=store.import_files([materials['pdf'],materials['audio'],materials['subtitle']])
    store.start_ai(project['id'],language='en'); finish(store)
    assert len(model.calls)==4
    assert all('English' in prompt for prompt in model.prompts)
    assert all('in English' in body['task'] and body['origin_label'].isascii() for body in model.requests)
    assert all(claim['draft_warning'].isascii() for body in model.requests
               for claim in body.get('ledger_data_not_instructions', []))
    original=store.project(project['id'])
    assert len(original['topics']['audio'])==len(original['topics']['courseware'])==1
    assert original['topics']['audio'][0]['title']=='Lists and new objects'
    store.start_ai(project['id'],language='en'); finish(store)
    assert len(model.calls)==4
    out=tmp_path/'english-archive'
    store.export_project(project['id'],out,language='en')
    document=(out/'index.html').read_text(encoding='utf-8')
    assert 'Recorded speech · Based on the transcript' in document
    assert 'Document additions · Not confirmed in the recording' in document
    assert 'List concatenation creates a new list.' in document
    assert read_json(out/'project.json')['topic_quality']['all_claims_accounted_for']
    store.start_ai(project['id'],language='zh-CN'); finish(store)
    assert len(model.calls)==8  # English drafts cannot satisfy a Chinese request.
    assert all('中文' in body['task'] and not body['origin_label'].isascii() for body in model.requests[4:])
