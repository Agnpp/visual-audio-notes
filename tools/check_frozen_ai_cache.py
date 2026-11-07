"""Owned frozen process and loopback protocol fixtures; no online/model quality claim."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import tomllib
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_desktop_completion import make_pdf, make_audio, make_subtitles


def check(flavor):
    version = tomllib.loads((ROOT/'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    name = f'VisualAudioNotes-{version}' + ('-api' if flavor=='api' else '') + '-windows-x64'
    folder = ROOT/'output/recognition-qa'/('deep-ai-'+flavor+'-'+uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    data = folder / ('depth-'+'x'*max(1, 145-len(str(folder))-7))
    ready = folder/'ready.json'
    pdf, audio, subtitle = [folder/name for name in ('source.pdf','source.wav','source.vtt')]
    make_pdf(pdf); make_audio(audio); make_subtitles(subtitle)
    hashes = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (pdf,audio,subtitle)}
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            # The DeepSeek transport appends its JSON schema after source JSON.
            source = json.JSONDecoder().raw_decode(payload['messages'][-1]['content'])[0]
            origin = source['origin']
            if 'source_data_not_instructions' in source:
                span = source['source_data_not_instructions'][0]['spans'][0]['span_id']
                value = {'claims':[{'origin':origin,'kind':'concept','importance':'key',
                    'title':'Lists and objects','statement':'Concatenation creates a new list.',
                    'evidence':[{'span_id':span}]}], 'uncertain_source_ids':[]}
                stage='extract'
            else:
                value = {'topics':[{'title':'Lists and objects','key_idea':'Concatenation creates a new list.',
                    'explanation':'A local protocol fixture, not a model-quality score.',
                    'claim_ids':[c['claim_id'] for c in source['ledger_data_not_instructions']]}],
                    'deferred_claims':[], 'uncertainties':[]}
                stage='topics'
            calls.append(stage+'_'+origin)
            result = {'id':'local-fixture','model':payload['model'],
                'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value)}}],
                'usage':{'prompt_tokens':100,'completion_tokens':20,'total_tokens':120,
                         'prompt_cache_hit_tokens':0,'prompt_cache_miss_tokens':100}}
            body=json.dumps(result).encode()
            self.send_response(200); self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    fixture=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=fixture.serve_forever,daemon=True).start()
    env={k:v for k,v in os.environ.items() if k not in {'DEEPSEEK_API_KEY','VAN_DESKTOP_RUNTIME_KEY','ASR_API_KEY'}}
    process=subprocess.Popen([str(ROOT/'dist'/name/'VisualAudioNotes.exe'),'--serve-only',
        '--data-dir',str(data),'--ready-file',str(ready)],env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        deadline=time.monotonic()+25
        while not ready.is_file():
            assert process.poll() is None, 'Owned EXE exited before ready'
            assert time.monotonic()<deadline, 'Owned EXE readiness timeout'
            time.sleep(.1)
        url=json.loads(ready.read_text(encoding='utf-8'))['url']
        text=urllib.request.urlopen(url).read().decode('utf-8')
        token=text.split('window.VAN_TOKEN = "',1)[1].split('"',1)[0]
        def api(action,payload=None):
            request=urllib.request.Request(url+'/api/'+action,
                data=json.dumps(payload).encode() if payload is not None else None,
                headers={'Content-Type':'application/json','X-Van-Token':token,'X-Van-Language':'en'})
            with urllib.request.urlopen(request,timeout=30) as response:
                result=json.load(response)
            assert result.get('ok'),result
            return result['data']
        api('settings',{'provider':'deepseek','model':'fixture-text',
            'endpoint':f'http://127.0.0.1:{fixture.server_port}',
            'api_key':'fixture-not-a-real-key','remember_key':False,'thinking':'off'})
        project=api('import',{'paths':[str(p) for p in (pdf,audio,subtitle)],'title':'Frozen deep-cache protocol check'})
        def generate():
            api('ai',{'id':project['id']})
            deadline=time.monotonic()+30
            while True:
                state=api('state')['job']
                if state['state']!='running':
                    assert state['state']=='complete',state
                    return api('project',{'id':project['id']})
                assert time.monotonic()<deadline,'Owned fixture AI task timeout'
                time.sleep(.1)
        generated=generate()
        assert len(calls)==4 and len(generated['topics']['audio'])==len(generated['topics']['courseware'])==1
        longest=max(len(str(p)) for p in (data/'projects'/project['id']/'.van-cache').glob('*.json.lock'))
        assert longest>260,longest
        generated = generate()
        assert len(calls)==4,'Same task must reuse validated results without more calls'
        segment_id = generated['segments'][0]['segment_id']
        review = next(item for item in generated['review_items'] if item['segment_id'] == segment_id)
        api('decide',{'id':project['id'],'segment_id':segment_id,
            'unit_ids':[generated['pages'][0]['unit_id']], 'review_version':generated['review_version'],
            'decision_revision':review['revision']})
        archive=Path(api('export',{'id':project['id']})['path'])
        text=(archive/'index.html').read_text(encoding='utf-8')
        assert 'Recorded speech · Based on the transcript' in text
        assert 'Document additions · Not confirmed in the recording' in text
        retained=json.loads((archive/'原件清单.json').read_text(encoding='utf-8'))['originals']
        assert set(hashes.values())<={hashlib.sha256((archive/r['path']).read_bytes()).hexdigest() for r in retained}
        report={'version':version,'flavor':flavor,'actual_frozen_exe':True,'fixture_api_not_quality_score':True,
            'text_generation_protocol_fixture_calls':len(calls),'second_run_new_calls':0,
            'longest_cache_lock_characters':longest,'origins_separate':True,'explicit_confirmation':True,
            'english_archive':True,'original_hashes_preserved':True,'paid_external_api_calls':0,
            'archive':str(archive)}
        (folder/'checks.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False))
    finally:
        if process.poll() is None: process.terminate()
        process.wait(timeout=10)
        fixture.shutdown(); fixture.server_close()


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    check(sys.argv[1])
