"""Loopback JSON job API. Uses the same project files and synthesis pipeline as GUI."""
import base64
import copy
import json
import mimetypes
import secrets
import threading
import uuid
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from project import Project,split_text,segment
from engine import SpeechEngine,run_job,convert
from model_manager import ModelManager,CATALOG
from language_ai import DEFAULT_AI


def validate_settings(settings):
    if not isinstance(settings,dict):raise ValueError('settings должен быть объектом')
    allowed={'engine','max_chars','pause_ms','temperature','top_p','language','auto_cut_pauses','max_pause_ms','text_processing',*DEFAULT_AI}
    if set(settings)-allowed:raise ValueError('Неизвестные настройки: '+', '.join(sorted(set(settings)-allowed)))
    if settings.get('mode','simple') not in ('simple','advanced'):raise ValueError('mode: simple или advanced')
    if settings.get('engine','piper') not in {k for k,v in CATALOG.items() if v['kind']=='tts'}:raise ValueError('Неизвестный движок')
    if settings.get('llm_model','qwen2.5-3b') not in {k for k,v in CATALOG.items() if v['kind']=='text'}:raise ValueError('Неизвестная языковая модель')
    for k,lo,hi in [('max_chars',20,2400),('pause_ms',0,60000),('max_pause_ms',100,60000),('pause_replacement_ms',0,60000),('temperature',.05,2),('top_p',.01,1),('error_retries',0,100),('quality_retries',0,100),('min_score',0,100),('pause_keep_start_ms',0,60000),('pause_keep_end_ms',0,60000)]:
        if k in settings and (not isinstance(settings[k],(int,float)) or not lo<=settings[k]<=hi):raise ValueError('Недопустимое значение '+k)
    for side in ('start','end'):
        for suffix in ('threshold_ms','keep_start_ms','keep_end_ms'):
            key=side+'_pause_'+suffix
            if key in settings and (type(settings[key]) is not int or not 0<=settings[key]<=60000):raise ValueError('Недопустимое значение '+key)
    if 'max_chars' in settings and type(settings['max_chars']) is not int:raise ValueError('max_chars должен быть целым')
    voices=settings.get('voices',[])
    if not isinstance(voices,list) or len(voices)>100:raise ValueError('Допустимо до 100 голосов')
    seen=set()
    for v in voices:
        if not isinstance(v,dict) or not isinstance(v.get('id'),str) or not v['id'] or v['id'] in seen:raise ValueError('Голосам нужны уникальные id')
        seen.add(v['id'])
        if v.get('engine','piper') not in {k for k,item in CATALOG.items() if item['kind']=='tts'}:raise ValueError('Неизвестный голос')
        if 'reference' in v:raise ValueError('Передавайте sample_id вместо пути к файлу')
    return copy.deepcopy(settings)


class APIService:
    def __init__(self,root,models,engine=None):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.models=ModelManager(models);self.engine=engine or SpeechEngine(models)
        self.jobs={};self.lock=threading.RLock();self.server=None;self.stopping=False
        token=self.root/'api-token.txt'
        if not token.exists():token.write_text(secrets.token_urlsafe(32),encoding='ascii')
        self.token=token.read_text(encoding='ascii').strip()
        for p in self.root.glob('jobs/*/api.json'):
            try:
                job=json.loads(p.read_text(encoding='utf-8'))
                if job['status'] in ('queued','running'):job['status']='paused'
                self.jobs[job['id']]=job
            except (ValueError,KeyError,OSError):pass
        self.stops={};self.threads={}

    def _save(self,job):
        folder=self.root/'jobs'/job['id'];folder.mkdir(parents=True,exist_ok=True)
        temp=folder/'api.tmp';temp.write_text(json.dumps(job,ensure_ascii=False),encoding='utf-8');temp.replace(folder/'api.json')

    def upload(self,payload):
        kind=payload.get('kind');suffix=payload.get('extension','').lower()
        allowed={'sample':('.wav','.flac','.mp3'),'cover':('.png','.jpg','.jpeg')}
        if suffix not in allowed.get(kind,()):raise ValueError('Недопустимый формат файла')
        data=base64.b64decode(payload['data'],validate=True)
        if not data or len(data)>32*1024**2:raise ValueError('Размер вложения должен быть 1 байт — 32 МиБ')
        name=uuid.uuid4().hex+suffix;folder=self.root/'uploads';folder.mkdir(exist_ok=True);path=folder/name
        path.write_bytes(data)
        try:
            if kind=='sample':
                import soundfile as sf
                info=sf.info(path)
                if info.duration<=0 or info.duration>120:raise ValueError('Образец должен длиться до 120 секунд')
            else:
                from PySide6.QtGui import QImageReader
                reader=QImageReader(str(path));size=reader.size()
                if not reader.canRead() or size.width()*size.height()>16_000_000 or reader.read().isNull():raise ValueError('Повреждённое или слишком большое изображение')
        except Exception:path.unlink(missing_ok=True);raise
        return dict(id=name,kind=kind)

    def _asset(self,name):
        if not isinstance(name,str) or Path(name).name!=name:raise ValueError('Неверный id вложения')
        p=self.root/'uploads'/name
        if not p.is_file():raise ValueError('Вложение не найдено')
        return p

    def create(self,payload):
        if self.stopping:raise ValueError('API останавливается')
        text=payload.get('text')
        if not isinstance(text,str) or not text.strip():raise ValueError('Передайте непустой text')
        cfg=validate_settings(payload.get('settings',{}));fmt=payload.get('format','wav')
        if fmt not in ('wav','mp3','flac','m4a','m4b'):raise ValueError('Неподдерживаемый формат аудио')
        count=payload.get('voice_count')
        if count is not None:
            if type(count) is not int or not 1<=count<=100:raise ValueError('voice_count: от 1 до 100')
            if cfg.get('voices') and len(cfg['voices'])!=count:raise ValueError('voice_count должен совпадать с количеством voices')
            if not cfg.get('voices'):
                available=[m['id'] for m in self.models.list() if m['kind']=='tts' and m['installed'] and m['id'].startswith('piper')]
                if count>len(available):raise ValueError('Для такого числа разных голосов задайте voices с моделями или образцами. Установлено голосов Piper: '+str(len(available)))
                descriptions=payload.get('voice_descriptions',[])
                if descriptions and (not isinstance(descriptions,list) or len(descriptions)!=count):raise ValueError('voice_descriptions должен содержать voice_count описаний')
                cfg['voices']=[dict(id=str(i+1),description=str(descriptions[i]) if i<len(descriptions) else f'Голос {i+1}',engine=available[i]) for i in range(count)]
        if cfg.get('mode')=='advanced' and (count or cfg.get('voices')):cfg.setdefault('dialogue',True)
        for voice in cfg.get('voices',[]):
            if voice.get('sample_id') and self._asset(voice['sample_id']).suffix not in ('.wav','.flac','.mp3'):raise ValueError('sample_id должен указывать на аудио')
        if payload.get('cover_id') and self._asset(payload['cover_id']).suffix not in ('.png','.jpg','.jpeg'):raise ValueError('cover_id должен указывать на картинку')
        job=dict(id=uuid.uuid4().hex,status='queued',progress=0,total=0,message='',error='',format=fmt,cover_id=payload.get('cover_id'),settings=cfg)
        folder=self.root/'jobs'/job['id'];project=Project.create(folder,str(payload.get('title','API генерация'))[:180])
        project.data['settings'].update(cfg)
        if not project.data['settings'].get('voices'):
            from characters import import_voice_library
            import_voice_library(project,self.models.root.parent/'voices')
        import shutil
        for voice in project.data['settings'].get('voices',[]):
            if voice.get('sample_id'):
                source=self._asset(voice['sample_id']);dest=project.path('voices/'+source.name);dest.parent.mkdir(exist_ok=True)
                shutil.copy2(source,dest);voice['reference']='voices/'+source.name;voice['engine']='higgs'
        # Fragmentation runs in the job thread, including for very large requests.
        (folder/'input.txt').write_text(text,encoding='utf-8');project.save()
        with self.lock:self.jobs[job['id']]=job;self._save(job)
        self.resume(job['id']);return self.status(job['id'])

    def status(self,key):
        with self.lock:return copy.deepcopy(self.jobs[key])

    def resume(self,key):
        with self.lock:
            if self.stopping:raise ValueError('API останавливается')
            if key in self.threads and self.threads[key].is_alive():return
            stop=threading.Event();self.stops[key]=stop
            job=self.jobs[key];job.update(status='queued',error='');self._save(job)
            thread=threading.Thread(target=self._run,args=(job,stop),daemon=True);self.threads[key]=thread;thread.start()

    def _run(self,job,stop):
        try:
            project=Project.open(self.root/'jobs'/job['id']);cfg=project.data['settings'];root=project.node(project.data['root'])
            if not root['segments']:
                text=(project.folder/'input.txt').read_text(encoding='utf-8')
                from text_pipeline import prepare_text
                from language_ai import LanguageModel
                llm=LanguageModel(self.models.root)
                try:root['segments']=list(prepare_text(project,text,cfg,llm,lambda message:job.update(message=message),stop))
                finally:
                    if llm.model is not None:llm.unload()
                project.save()
            ids=[s['id'] for _,s in project.rows(project.data['root']) if not project.has_audio(s)]
            job.update(status='running',total=len(root['segments']));self._save(job)
            def notify(message):
                with self.lock:job['message']=message
            def changed():
                with self.lock:
                    job['progress']=sum(project.has_audio(s) for s in root['segments']);self._save(job)
            run_job(project,self.engine,ids,cfg,stop,notify,changed)
            state=project.data.get('job') or {}
            if stop.is_set() or state.get('status')!='completed':
                job.update(status='paused',error=state.get('error',''));return
            wav=project.folder/'result.wav';project.export(project.data['root'],wav,cfg['pause_ms'])
            result=project.folder/('result.'+job['format'])
            if result!=wav:convert(wav,result)
            from audio_metadata import embed_metadata
            embed_metadata(result,root['title'],'\n\n'.join(s.get('spoken_text',s['text']) for s in root['segments']),self._asset(job['cover_id']) if job.get('cover_id') else None)
            job.update(status='completed',result='/v1/jobs/'+job['id']+'/audio',characters=project.data.get('cast') or project.data.get('characters',{}))
        except Exception as exc:job.update(status='error',error=str(exc))
        finally:
            with self.lock:self._save(job)

    def active(self):return any(t.is_alive() for t in self.threads.values())

    def start(self,port=8765):
        self.stopping=False
        from PySide6.QtCore import QLockFile
        self.file_lock=QLockFile(str(self.root/'server.lock'));self.file_lock.setStaleLockTime(0)
        if not self.file_lock.tryLock(0):raise ValueError('API уже запущен для этой папки программы')
        service=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def _handle(self):
                try:
                    if not secrets.compare_digest(self.headers.get('Authorization',''),'Bearer '+service.token):return self._json(401,dict(error='Bearer token required'))
                    path=urlsplit(self.path).path.strip('/').split('/');body={}
                    if self.command=='POST':
                        length=int(self.headers.get('Content-Length','0'))
                        if not 0<length<=64*1024**2:raise ValueError('JSON body: до 64 МиБ')
                        body=json.loads(self.rfile.read(length))
                        if not isinstance(body,dict):raise ValueError('Требуется JSON объект')
                    if self.command=='GET' and path==['v1','models']:return self._json(200,service.models.list())
                    if self.command=='GET' and path==['v1','settings']:
                        from text_processing import DEFAULT_PROCESSING
                        return self._json(200,dict(defaults={**DEFAULT_AI,'engine':'piper','max_chars':300,'pause_ms':160,'auto_cut_pauses':True,'max_pause_ms':1000,'temperature':.7,'top_p':.95,'language':'ru','text_processing':DEFAULT_PROCESSING},formats=['wav','mp3','flac','m4a','m4b'],modes=['simple','advanced']))
                    if self.command=='POST' and path==['v1','uploads']:return self._json(201,service.upload(body))
                    if self.command=='POST' and path==['v1','server','stop']:
                        service.stopping=True
                        def shutdown():
                            for event in list(service.stops.values()):event.set()
                            for thread in list(service.threads.values()):thread.join()
                            service.close()
                        self._json(202,dict(status='stopping'))
                        threading.Thread(target=shutdown,daemon=True).start();return
                    if self.command=='POST' and path==['v1','downloads']:return self._json(202,service.models.download(body['model']))
                    if self.command=='GET' and len(path)==3 and path[:2]==['v1','downloads']:return self._json(200,service.models.status(path[2]))
                    if self.command=='POST' and path==['v1','jobs']:return self._json(202,service.create(body))
                    if self.command=='POST' and path==['v1','audio']:
                        job=service.create(body);service.threads[job['id']].join();job=service.status(job['id'])
                        if job['status']!='completed':return self._json(422,job)
                        return self._audio(job)
                    if self.command=='GET' and path==['v1','jobs']:return self._json(200,[service.status(k) for k in list(service.jobs)])
                    if len(path)>=3 and path[:2]==['v1','jobs']:
                        job=service.status(path[2])
                        if self.command=='GET' and len(path)==3:return self._json(200,job)
                        if self.command=='POST' and path[3:]==['resume']:service.resume(path[2]);return self._json(202,service.status(path[2]))
                        if self.command=='POST' and path[3:]==['stop']:
                            if path[2] in service.stops:service.stops[path[2]].set()
                            return self._json(202,service.status(path[2]))
                        if self.command=='GET' and path[3:]==['audio']:
                            if job['status']!='completed':return self._json(409,dict(error='Audio is not ready'))
                            return self._audio(job)
                    self._json(404,dict(error='Not found'))
                except KeyError as exc:self._json(404,dict(error=str(exc)))
                except (ValueError,TypeError) as exc:self._json(400,dict(error=str(exc)))
                except (BrokenPipeError,ConnectionResetError):pass
                except Exception as exc:self._json(500,dict(error=str(exc)))
            def _json(self,status,data):
                blob=json.dumps(data,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(blob)));self.end_headers();self.wfile.write(blob)
            def _audio(self,job):
                p=service.root/'jobs'/job['id']/('result.'+job['format'])
                self.send_response(200);self.send_header('Content-Type',mimetypes.guess_type(p)[0] or 'application/octet-stream');self.send_header('Content-Length',str(p.stat().st_size));self.send_header('X-Job-ID',job['id']);self.end_headers()
                with p.open('rb') as f:
                    while chunk:=f.read(1024*1024):self.wfile.write(chunk)
            do_GET=_handle
            do_POST=_handle
        try:self.server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
        except Exception:self.file_lock.unlock();raise
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        return self.server.server_address[1]

    def close(self):
        if self.server:self.server.shutdown();self.server.server_close();self.server=None
        if hasattr(self,'file_lock'):self.file_lock.unlock()


if __name__=='__main__':
    import argparse,time
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8765);args=parser.parse_args()
    base=Path(__file__).resolve().parent.parent
    service=APIService(base/'api-data',base/'models');service.start(args.port)
    print('API http://127.0.0.1:'+str(args.port)+'; token: '+str(service.root/'api-token.txt'),flush=True)
    try:
        while service.server is not None:time.sleep(1)
    except KeyboardInterrupt:
        for stop in service.stops.values():stop.set()
        for thread in service.threads.values():thread.join()
        service.close()
