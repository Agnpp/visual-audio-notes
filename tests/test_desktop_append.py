"""Appending sources is model-free, transactional and preserves user work."""
from pathlib import Path
import shutil

import pytest

from visual_audio_notes.binding import validate_binding
from visual_audio_notes.core import file_hash, read_json
from visual_audio_notes.desktop_append import append_files, recover_pending_appends
from visual_audio_notes.desktop_store import DesktopStore
from visual_audio_notes.task_runtime import atomic_json

from test_desktop_completion import materials, make_pdf, ready_model, finish


def snapshots(root):
    return {p.relative_to(root).as_posix(): file_hash(p) for p in root.rglob('*') if p.is_file()}


def test_append_document_preserves_corrected_ocr_transcript_notes_and_history(tmp_path, materials, ready_model, monkeypatch):
    from visual_audio_notes import recognition
    monkeypatch.setattr(recognition, 'recognize_pages', lambda pages, *a, **k: {
        'texts': {p['unit_id']: 'APl text from raw OCR.' for p in pages}, 'failures': [], 'uses_ai': True})
    store = DesktopStore(tmp_path/'workspace')
    project = store.import_files([materials['image'], materials['audio'], materials['subtitle']], '保持课堂标题')
    store.start_ai(project['id']); finish(store)
    project = store.project(project['id'])
    uid, sid = project['pages'][0]['unit_id'], project['segments'][0]['segment_id']
    store.edit_text(project['id'], 'page', uid, 'API text explicitly corrected by the user.')
    store.edit_text(project['id'], 'transcript', sid, 'Corrected transcript remains on the same audio axis.', start=.2, end=1.8)
    store.save_notes(project['id'], '手写重点必须保留')
    store.decide(project['id'], sid, [uid])
    root = store.root/'projects'/project['id']
    old = read_json(root/'project.json')
    before = snapshots(root/'prepared')
    recognition_hash = file_hash(root/'prepared/recognition_sources.json')
    corrections_hash = file_hash(root/'prepared/corrections.json')
    (root/'runs/historical-run').mkdir(parents=True)
    (root/'runs/historical-run/preserved.txt').write_text('old result', encoding='utf-8')
    monkeypatch.setattr(recognition, 'transcribe', lambda *a, **k: pytest.fail('Append must not call ASR'))
    monkeypatch.setattr(recognition, 'recognize_pages', lambda *a, **k: pytest.fail('Append must not call OCR'))
    result = append_files(store, project['id'], [materials['pdf']])
    assert result['id'] == project['id'] and result['title'] == '保持课堂标题'
    assert result['manual_notes'] == '手写重点必须保留'
    assert result['segments'][0]['segment_id'] == sid
    assert result['segments'][0]['text'] == 'Corrected transcript remains on the same audio axis.'
    assert result['segments'][0]['start'] == .2
    previous_page = next(p for p in result['pages'] if p['unit_id'] == uid)
    assert previous_page['text'] == 'API text explicitly corrected by the user.'
    assert previous_page['text_origin'] == 'user_corrected'
    assert result['pipeline_needs'] == {'asr': False, 'ocr': False}
    assert result['review_items'][0]['status'] == 'confirmed'
    assert result['review_items'][0]['confirmed_unit_ids'] == [uid]
    audit = read_json(root/'prepared/review.json')
    assert audit['history'][-1]['event'] == 'decision_carried'
    assert audit['items'][0]['review']['source_revision'] < audit['source_revision']
    assert result['topics'] == {'audio': [], 'courseware': []}
    assert file_hash(root/'prepared/recognition_sources.json') == recognition_hash
    assert file_hash(root/'prepared/corrections.json') == corrections_hash
    assert (root/'runs/historical-run/preserved.txt').read_text() == 'old result'
    event = read_json(root/'append_history.json')['events'][-1]
    assert event['uses_ai'] is False
    retained_preparation = snapshots(root/event['prepared_history'])
    assert all(retained_preparation[name] == checksum for name, checksum in before.items())
    meta = read_json(root/'project.json')
    assert meta['transcript'] == old['transcript'] and meta['transcript_origin'] == 'user_edited'
    validate_binding(read_json(root/'prepared/archive_binding.json'), root/'prepared', root/meta['audio'], root/meta['pdf'])
    out = tmp_path/'appended-archive'
    store.export_project(project['id'], out)
    assert read_json(out/'project.json')['transcript_kind'] == 'user_edited'
    assert read_json(out/'project.json')['human_confirmed_single_page_mappings'] == 1
    assert len(read_json(out/'原件清单.json')['originals']) == 4
    assert file_hash(out/'识别记录.json') == recognition_hash
    assert 'API text explicitly corrected' in (out/'识别文字.md').read_text(encoding='utf-8')


def test_append_user_subtitle_replaces_asr_preserves_generated_file_and_record(tmp_path, materials, ready_model, monkeypatch):
    from visual_audio_notes import recognition
    monkeypatch.setattr(recognition, 'transcribe', lambda *a, **k: {
        'segments': [{'segment_id': 'a_original_asr', 'start': .1, 'end': 1.8, 'text': 'Original automatic transcript.'}],
        'uses_ai': True, 'model': 'fixture-speech'})
    store = DesktopStore(tmp_path/'workspace')
    project = store.import_files([materials['pdf'], materials['audio']])
    store.start_ai(project['id']); finish(store)
    root = store.root/'projects'/project['id']
    old_meta = read_json(root/'project.json')
    old_vtt_hash = file_hash(root/old_meta['transcript'])
    old_recognition_hash = file_hash(root/'prepared/recognition_sources.json')
    result = append_files(store, project['id'], [materials['subtitle']])
    assert result['transcript_origin'] == 'user_provided_transcript'
    assert result['segments'][0]['text'].startswith('Append mutates')
    assert file_hash(root/old_meta['transcript']) == old_vtt_hash
    assert file_hash(root/'prepared/recognition_sources.json') == old_recognition_hash
    assert any('替代当前转写' in message for message in result['preparation_warnings'])
    assert read_json(root/'append_history.json')['events'][-1]['previous_transcript'] == old_meta['transcript']
    assert result['pipeline_needs'] == {'asr': False, 'ocr': False}
    out = tmp_path/'replacement-archive'
    store.export_project(project['id'], out)
    transcript_list = read_json(out/'派生转写清单.json')['transcripts']
    archived_asr = next(item for item in transcript_list if item['sha256'] == old_vtt_hash)
    assert file_hash(out/archived_asr['path']) == old_vtt_hash
    added = read_json(out/'资料补充记录.json')['events'][-1]
    assert added['previous_transcript'] == old_meta['transcript']
    assert added['current_transcript'] != added['previous_transcript']


def test_existing_office_conversion_can_be_appended_and_old_pdf_stays_in_history(tmp_path, materials, monkeypatch):
    monkeypatch.setattr('visual_audio_notes.office_import.find_converter', lambda: None)
    store = DesktopStore(tmp_path/'workspace')
    project = store.import_files([materials['slides'], materials['audio'], materials['subtitle']])
    root = store.root/'projects'/project['id']
    old_meta = read_json(root/'project.json')
    old_pdf_hash = file_hash(root/old_meta['pdf'])
    old_units = {page['unit_id']: page['text'] for page in project['pages']}
    result = append_files(store, project['id'], [materials['pdf']])
    assert result['counts']['pages'] == 3
    assert {page['unit_id']: page['text'] for page in result['pages'] if page['unit_id'] in old_units} == old_units
    event = read_json(root/'append_history.json')['events'][-1]
    history = root/event['prepared_history']
    snapshot = read_json(history/'document_snapshot.json')
    assert file_hash(history/snapshot['pdf']) == old_pdf_hash == snapshot['sha256']
    validate_binding(read_json(history/'archive_binding.json'), history, root/old_meta['audio'], history/snapshot['pdf'])


def test_append_audio_completes_existing_text_inputs_and_basic_export(tmp_path, materials, monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    store = DesktopStore(tmp_path/'workspace')
    project = store.import_files([materials['pdf'], materials['subtitle']])
    old_uid, old_segment = project['pages'][0]['unit_id'], project['segments'][0]
    result = append_files(store, project['id'], [materials['audio']])
    assert result['pages'][0]['unit_id'] == old_uid and result['segments'][0] == old_segment
    out = tmp_path/'basic-archive'
    assert store.export_project(project['id'], out)['mode'] == 'basic_no_ai'
    assert read_json(out/'project.json')['uses_ai'] is False
    assert DesktopStore(store.root).project(project['id'])['audio_path']


def test_duplicate_append_is_exact_noop_and_conflicting_name_is_rejected(tmp_path, materials):
    store = DesktopStore(tmp_path/'workspace')
    project = store.import_files([materials['pdf'], materials['audio'], materials['subtitle']])
    store.decide(project['id'], project['segments'][0]['segment_id'], [project['pages'][0]['unit_id']])
    root = store.root/'projects'/project['id']
    before = snapshots(root)
    append_files(store, project['id'], [materials['pdf'], materials['pdf']])
    assert snapshots(root) == before
    conflict = tmp_path/materials['pdf'].name
    conflict.write_bytes(materials['pdf'].read_bytes() + b'\nchanged')
    with pytest.raises(ValueError, match='同名'):
        append_files(store, project['id'], [conflict])
    assert snapshots(root) == before
    other_audio = tmp_path/'another.wav'
    shutil.copy2(materials['audio'], other_audio)
    with pytest.raises(ValueError, match='一段原录音'):
        append_files(store, project['id'], [other_audio])
    other_vtt = tmp_path/'another.vtt'
    shutil.copy2(materials['subtitle'], other_vtt)
    with pytest.raises(ValueError, match='一份用户字幕'):
        append_files(store, project['id'], [other_vtt])
    assert snapshots(root) == before


def test_invalid_document_and_tampered_original_do_not_mutate_project(tmp_path, materials):
    store = DesktopStore(tmp_path/'workspace')
    project = store.import_files([materials['pdf'], materials['audio'], materials['subtitle']])
    root = store.root/'projects'/project['id']
    before = snapshots(root)
    bad = tmp_path/'broken.pptx'
    bad.write_bytes(b'not an Office file')
    with pytest.raises(ValueError):
        append_files(store, project['id'], [bad])
    assert snapshots(root) == before
    assert not list(store.root.glob('.append-*'))
    original_pdf = root/'originals'/materials['pdf'].name
    original_pdf.write_bytes(original_pdf.read_bytes() + b'changed')
    tampered = snapshots(root)
    with pytest.raises(ValueError, match='原件与导入时不一致'):
        append_files(store, project['id'], [materials['image']])
    assert snapshots(root) == tampered


def test_swap_failure_restores_original_and_startup_recovers_interrupted_swap(tmp_path, materials, monkeypatch):
    store = DesktopStore(tmp_path/'workspace')
    project = store.import_files([materials['pdf'], materials['audio'], materials['subtitle']])
    root = store.root/'projects'/project['id']
    before = snapshots(root)
    rename = Path.rename
    def fail_commit(self, target):
        if self.name == 'n' and self.parent.name.startswith('.append-'):
            raise OSError('fixture failed directory commit')
        return rename(self, target)
    monkeypatch.setattr(Path, 'rename', fail_commit)
    with pytest.raises(OSError, match='fixture'):
        append_files(store, project['id'], [materials['image']])
    assert snapshots(root) == before
    monkeypatch.setattr(Path, 'rename', rename)
    transaction = store.root/('.append-'+'a'*32)
    transaction.mkdir()
    atomic_json(transaction/'transaction.json', {'version': 1, 'project_id': project['id']})
    root.rename(transaction/'previous-project')
    assert not root.exists()
    assert recover_pending_appends(store.root) == [project['id']]
    assert snapshots(root) == before
    assert not transaction.exists()


def test_append_preserves_near_max_path_cache_and_uses_shorter_transaction_roots(tmp_path, materials, monkeypatch):
    store = DesktopStore(tmp_path/'workspace')
    project = store.import_files([materials['pdf'], materials['audio'], materials['subtitle']])
    root = store.root/'projects'/project['id']
    cache = root/'.recognition-cache'/'asr'/'chunks'
    cache.mkdir(parents=True)
    # Actual native Windows I/O near MAX_PATH, rather than shortening the data
    # directory to conceal the production regression in the copy destination.
    name_size = 242 - len(str(cache)) - 1 - len('.json')
    assert 1 <= name_size <= 240
    record = cache/('c'*name_size+'.json')
    record.write_bytes(b'{"cached":"must survive append"}')
    assert len(str(record)) == 242
    checksum = file_hash(record)
    copytree = shutil.copytree
    observed = []
    def checked_copytree(source, destination, *args, **kwargs):
        source, destination = Path(source), Path(destination)
        if source == root:
            assert destination.name == 'n'
            assert len(destination.parent.name) == len('.append-') + 12
            assert len(str(destination)) < len(str(root))
            assert len(str(destination.parent/'b')) < len(str(root))
            assert len(str(destination/record.relative_to(root))) < 242
            observed.append(True)
        return copytree(source, destination, *args, **kwargs)
    monkeypatch.setattr(shutil, 'copytree', checked_copytree)
    result = append_files(store, project['id'], [materials['image']])
    assert observed == [True] and result['counts']['pages'] == 2
    assert file_hash(record) == checksum
    assert not list(store.root.glob('.append-*'))


def test_new_short_transaction_recovers_after_directory_swap_interruption(tmp_path, materials):
    store = DesktopStore(tmp_path/'workspace')
    project = store.import_files([materials['pdf']])
    root = store.root/'projects'/project['id']
    before = snapshots(root)
    transaction = store.root/('.append-'+'a'*12)
    transaction.mkdir()
    atomic_json(transaction/'transaction.json', {'version': 2, 'project_id': project['id']})
    root.rename(transaction/'b')
    assert recover_pending_appends(store.root) == [project['id']]
    assert snapshots(root) == before
    assert not transaction.exists()


def test_startup_recovery_leaves_untrusted_and_uncommitted_staging_untouched(tmp_path, materials):
    store = DesktopStore(tmp_path/'workspace')
    project = store.import_files([materials['pdf']])
    transaction = store.root/('.append-'+'b'*32)
    transaction.mkdir()
    atomic_json(transaction/'transaction.json', {'project_id': '../outside'})
    (transaction/'preserve.txt').write_text('do not delete')
    assert recover_pending_appends(store.root) == []
    assert (transaction/'preserve.txt').read_text() == 'do not delete'
    atomic_json(transaction/'transaction.json', {'project_id': project['id']})
    assert recover_pending_appends(store.root) == []
    assert transaction.exists()
