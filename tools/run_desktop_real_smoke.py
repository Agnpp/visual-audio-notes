"""Real local recognition -> candidates -> archive, without supplied subtitles.

This is a mechanism check on previously used public materials, not a blind
alignment benchmark or a transcription accuracy score. No reference/answer
files, original video, known page timing, or paid API are accessed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
import traceback
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def run(destination):
    from PIL import Image, ImageDraw, ImageFont
    from visual_audio_notes.core import file_hash, read_json
    from visual_audio_notes.desktop_store import DesktopStore
    from visual_audio_notes.model_assets import ASR_FILES, ASR_REVISION, validate_asr_model
    from visual_audio_notes.task_runtime import atomic_json

    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    workspace = destination / 'workspace'
    if workspace.exists():
        raise ValueError('Use a new real-smoke workspace; previous results are preserved')
    source_audio = ROOT / 'output/recognition-qa/zh-crop-30s.wav'
    source_pdf = ROOT / 'data/public/ntu_ml/materials.pdf'
    cached_model = ROOT / '.cache/models/asr'
    if not source_audio.is_file() or not source_pdf.is_file():
        raise ValueError('The already downloaded public PDF or 30-second independent WAV is unavailable')
    model = workspace / 'models/whisper-small'
    model.mkdir(parents=True)
    seeded = []
    for name, (size, expected) in ASR_FILES.items():
        source = cached_model / name
        if source.stat().st_size != size or file_hash(source) != expected:
            raise ValueError('Cached ASR weights do not match the pinned desktop model')
        os.link(source, model / name)
        seeded.append({'filename':name,'bytes':size,'sha256':expected,'method':'hardlink_existing_verified_cache'})
    validate_asr_model(model)
    image = destination / '原创中英文字图.png'
    expected_lines = ['机器学习课堂资料', '录音重点与课件补充分开', 'Cache 2026 API 37']
    with Image.new('RGB',(1200,440),'white') as canvas:
        draw = ImageDraw.Draw(canvas)
        font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc',48)
        for index,line in enumerate(expected_lines):
            draw.text((65,55+index*115),line,font=font,fill='#15273d')
        canvas.save(image)
    inputs = [source_pdf,source_audio,image]
    hashes = {path.name:file_hash(path) for path in inputs}
    store = DesktopStore(workspace)
    store.save_recognition({'asr':{'provider':'local_whisper','model':'small','language':'zh'},
                            'ocr':{'provider':'local_rapidocr'}})
    if store.public_connection()['configured']:
        raise ValueError('This mechanism check must not have a configured text-model connection')
    network_attempts = []
    original_open = urllib.request.OpenerDirector.open
    def no_network(self, request, *args, **kwargs):
        network_attempts.append('blocked')
        raise RuntimeError('Network access is disabled in the local desktop mechanism check')
    urllib.request.OpenerDirector.open = no_network
    started = time.perf_counter()
    try:
        imported = store.import_files(inputs,'真实独立录音与资料 · 本地闭环验证')
        print(json.dumps({'stage':'imported','pages':imported['counts']['pages'],
                          'segments_before':len(imported['segments']),
                          'needs':imported['pipeline_needs']},ensure_ascii=False),flush=True)
        if imported['segments']:
            raise ValueError('No supplied subtitles are allowed in this mechanism test')
        store.start_ai(imported['id'])
        last = None
        deadline = time.monotonic()+180
        while store._thread.is_alive():
            if time.monotonic()>deadline:
                store.cancel()
                store._thread.join(timeout=15)
                raise TimeoutError('Local desktop mechanism test exceeded its 180-second budget')
            job = store.state()['job']
            status = (job['stage'],job['message'])
            if status != last:
                print(json.dumps({'stage':job['stage'],'message':job['message']},ensure_ascii=False),flush=True)
                last = status
            store._thread.join(timeout=.5)
        job = store.state()['job']
        if job['state']!='complete':
            raise RuntimeError('Desktop recognition job did not complete: '+json.dumps(job,ensure_ascii=False))
        current = store.project(imported['id'])
        project = workspace / 'projects' / current['id']
        recognition = read_json(project/'prepared/recognition_sources.json')
        meta = read_json(project/'project.json')
        if not current['segments']:
            raise ValueError('Real ASR did not produce a timed transcript')
        if any(not 0<=segment['start']<segment['end']<=30.25 for segment in current['segments']):
            raise ValueError('Real ASR timestamps escaped the original 30-second recording')
        photo = next(page for page in current['pages'] if page.get('original_filename')==image.name)
        normalized = re.sub(r'\s+','',photo['text']).casefold()
        expected_found = {line:re.sub(r'\s+','',line).casefold() in normalized for line in expected_lines}
        # Keep recognition errors visible while still verifying whether the
        # application can archive and reopen the actual draft result. A failed
        # exact-text check is never relabelled as a successful OCR quality test.
        remote_calls = (recognition.get('asr') or {}).get('api_calls',[])+(recognition.get('ocr') or {}).get('api_calls',[])
        if network_attempts or remote_calls:
            raise ValueError('A supposedly local-only test attempted a remote request')
        if any(current['topics'].values()):
            raise ValueError('No text-model summary should be generated without a text connection')
        out = destination/'archive'
        export = store.export_project(current['id'],out)
        manifest = read_json(out/'project.json')
        if manifest.get('uses_ai') is not True or manifest.get('transcript_kind')!='generated_asr':
            raise ValueError('Recognized text was incorrectly labelled as imported or model-free')
        originals = read_json(out/'原件清单.json')['originals']
        archived = {item['filename']:file_hash(out/item['path']) for item in originals}
        if archived!=hashes:
            raise ValueError('Archived original bytes differ from the imported inputs')
        if {path.name:file_hash(path) for path in inputs}!=hashes:
            raise ValueError('An original input changed during processing')
        restored = DesktopStore(workspace).project(current['id'])
        if restored['segments']!=current['segments'] or restored['export_path']!=str(out):
            raise ValueError('The recognized transcript or archive path did not survive reopening')
        # Re-running must reuse the prepared transcript and OCR text, and must
        # not manufacture a second ASR result or a new preparation revision.
        revision = meta.get('preparation_revision')
        store.start_ai(current['id'])
        store._thread.join(timeout=15)
        if store._thread.is_alive() or store.state()['job']['state']!='complete':
            raise ValueError('Second local organize did not finish by reusing prepared results')
        if read_json(project/'project.json').get('preparation_revision')!=revision:
            raise ValueError('Repeated organization unnecessarily regenerated recognition output')
        report = {
            'scope':'real local desktop mechanism test on previously used public materials; not new blind matching or WER',
            'independent_inputs_only':True,'supplied_srt_vtt':False,'text_llm_configured':False,
            'input_files':[{'filename':path.name,'sha256':hashes[path.name],'bytes':path.stat().st_size} for path in inputs],
            'model_cache':{'reused_existing_weights':True,'new_download_bytes':0,'revision':ASR_REVISION,'files':seeded},
            'import_pages':current['counts']['pages'],'recognized_segments':len(current['segments']),
            'asr':{'identity':(recognition.get('asr') or {}).get('identity'),
                   'duration_seconds':(recognition.get('asr') or {}).get('duration_seconds'),
                   'seconds':(recognition.get('asr') or {}).get('seconds'),
                   'first_start':current['segments'][0]['start'],'last_end':current['segments'][-1]['end'],
                   'timestamps_within_original_recording':True},
            'ocr':{'metrics':(recognition.get('ocr') or {}).get('metrics'),
                   'failed_pages':len((recognition.get('ocr') or {}).get('failures',[])),
                   'self_authored_text_checks':expected_found,'all_exact_text_checks_passed':all(expected_found.values()),
                   'recognized_self_authored_picture_text':photo['text'], 'photo_unit_id':photo['unit_id']},
            'candidates':{'chunks_with_candidates':sum(bool(row['candidates']) for row in current['review_items']),
                          'total_chunks':len(current['review_items']),'automatically_confirmed':sum(row['status']=='confirmed' for row in current['review_items']),
                          'accuracy_evaluated':False,'method':current['candidate_method']},
            'remote_api_calls':len(remote_calls),'blocked_network_attempts':len(network_attempts),
            'topics_generated':False,'export_mode':export['mode'],'archive_uses_ai':manifest['uses_ai'],
            'archive_transcript_kind':manifest['transcript_kind'],'original_hashes_equal':True,
            'restart_preserved':True,'repeat_preparation_reused':True,
            'seconds_total':round(time.perf_counter()-started,3),'project_id':current['id'],
            'warnings':current['preparation_warnings'],
        }
        atomic_json(destination/'real_desktop_smoke.json',report)
        doc = ['# 真实本地桌面闭环验证','',
               '这轮验证实际调用本地 Whisper small 和 RapidOCR，输入是既有独立中文 30 秒音频、原始课件 PDF 和原创中英文字图片。没有提供 SRT/VTT，也没有配置文字大模型。','',
               '只验证处理机制和归档，未读取评分答案、原视频或页码对齐标注；不是新盲测，也不作为转写错误率或候选正确率成绩。','',
               f"- 导入资料 {report['import_pages']} 页，生成 {report['recognized_segments']} 段带时间转写。",
               f"- OCR 原创图片 {len(expected_found)} 条文字检查中，{sum(expected_found.values())} 条精确通过；未通过项保留在报告，不能称为识字质量全面通过。",
               '- 实际识别出现 API → APl（大写 I 与小写 l）的歧义；保留原图与识别草稿供核对，没有替模型静默改正。' if not all(expected_found.values()) else '- 此图片的指定文字检查通过，不代表其他材料上无识别错误。',
               f"- {report['candidates']['chunks_with_candidates']}/{report['candidates']['total_chunks']} 段存在内容候选；这不是准确率，自动确认数为 0。",
               '- 云端 API 调用与网络尝试均为 0；语音模型复用已有校验通过的缓存，没有新下载。',
               '- 归档标明识别使用模型、转写来自 ASR，原件哈希一致。',
               '- 重开后转写及归档路径保留；第二次整理复用现有识别结果。',
               f"- 总耗时 {report['seconds_total']} 秒。",'',
               '公开课原件与识别内容只保留在忽略目录中，不打进软件分发包。具体数据见 real_desktop_smoke.json。']
        (destination/'真实闭环验证.md').write_text('\n'.join(doc),encoding='utf-8')
        print(json.dumps({key:report[key] for key in ('recognized_segments','remote_api_calls','seconds_total','export_mode','repeat_preparation_reused')},ensure_ascii=False),flush=True)
        return report
    finally:
        urllib.request.OpenerDirector.open = original_open


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--out',type=Path,default=ROOT/'outputs/completion-real-20261031')
    args = parser.parse_args()
    names = ('DEEPSEEK_API_KEY','VAN_DESKTOP_RUNTIME_KEY','VAN_RECOGNITION_ASR','VAN_RECOGNITION_OCR')
    previous = {name:os.environ.pop(name,None) for name in names}
    os.environ['HF_HUB_OFFLINE']='1'
    try:
        run(args.out)
    except BaseException:
        args.out.mkdir(parents=True,exist_ok=True)
        (args.out/'failure.txt').write_text(traceback.format_exc(),encoding='utf-8')
        raise
    finally:
        for name,value in previous.items():
            if value is not None:
                os.environ[name]=value
