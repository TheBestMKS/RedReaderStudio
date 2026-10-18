"""Local model catalogue and atomic, revision-pinned background downloads."""
import json
import os
import threading
import uuid
import urllib.request
from pathlib import Path

INFERENCE_LOCK = threading.RLock()
CATALOG = {
    'piper': dict(name='Piper · Ирина', kind='tts', path='piper/ru_RU-irina-medium.onnx'),
    'higgs': dict(name='Higgs Audio 4B · образец голоса', kind='tts', path='higgs/model.safetensors'),
}
for voice, title in [('denis','Денис'), ('dmitri','Дмитрий'), ('ruslan','Руслан')]:
    prefix=f'ru/ru_RU/{voice}/medium/'
    file=f'ru_RU-{voice}-medium.onnx'
    CATALOG['piper-'+voice]=dict(name='Piper · '+title,kind='tts',path='piper/'+file,
        repo='rhasspy/piper-voices',files={prefix+file:'piper/'+file,prefix+file+'.json':'piper/'+file+'.json',
                                         prefix+'MODEL_CARD':'piper/'+voice+'-MODEL_CARD'})
for size in ('1.5B','3B','7B'):
    key='qwen2.5-'+size.lower()
    CATALOG[key]=dict(name='Qwen 2.5 '+size+' Instruct',kind='text',path=key,repo='Qwen/Qwen2.5-'+size+'-Instruct')


class ModelManager:
    def __init__(self, root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.jobs={};self.lock=threading.RLock()

    def installed(self,key):
        item=CATALOG[key];p=self.root/item['path']
        if item['kind']=='text':return (p/'config.json').is_file() and (p/'.complete').is_file()
        return p.is_file() and (key=='higgs' or Path(str(p)+'.json').is_file())

    def list(self):
        return [dict(id=k,name=v['name'],kind=v['kind'],installed=self.installed(k),downloadable='repo' in v) for k,v in CATALOG.items()]

    def download(self,key):
        if key not in CATALOG:raise ValueError('Неизвестная модель')
        with self.lock:
            for job in self.jobs.values():
                if job['model']==key and job['status'] in ('queued','downloading'):return dict(job)
            job=dict(id=uuid.uuid4().hex,model=key,status='queued',bytes=0,total=0,file='',error='')
            self.jobs[job['id']]=job
            if self.installed(key):
                job['status']='completed'
                return dict(job)
            if 'repo' not in CATALOG[key]:
                job.update(status='error',error='Восстановите эту модель из полной поставки программы')
                return dict(job)
        threading.Thread(target=self._download,args=(job,),daemon=True).start()
        return dict(job)

    def status(self,job_id):
        with self.lock:return dict(self.jobs[job_id])

    def _download(self,job):
        try:
            item=CATALOG[job['model']];repo=item['repo']
            with urllib.request.urlopen('https://huggingface.co/api/models/'+repo,timeout=60) as r:meta=json.load(r)
            revision=meta['sha'];files=item.get('files')
            if files is None:
                names=[s['rfilename'] for s in meta['siblings']]
                names=[n for n in names if '/' not in n and (n.endswith(('.json','.safetensors','.txt','.model')) or n in ('LICENSE','README.md'))]
                files={n:item['path']+'/'+n for n in names}
            job.update(status='downloading',revision=revision)
            for source,target in files.items():
                dest=self.root/target;dest.parent.mkdir(parents=True,exist_ok=True)
                temp=dest.with_name(dest.name+'.download')
                url=f'https://huggingface.co/{repo}/resolve/{revision}/{source}'
                job['file']=source
                with urllib.request.urlopen(url,timeout=120) as response, temp.open('wb') as out:
                    expected=int(response.headers.get('Content-Length',0));job['total']=job['bytes']+expected
                    while chunk:=response.read(1024*1024):out.write(chunk);job['bytes']+=len(chunk)
                    if expected and out.tell()!=expected:raise IOError('Загрузка оборвалась; повторите скачивание модели')
                os.replace(temp,dest)
            if item['kind']=='text':(self.root/item['path']/'.complete').write_text(revision,encoding='ascii')
            job.update(status='completed',total=job['bytes'])
        except Exception as exc:job.update(status='error',error=str(exc))
