import base64
import json
import threading
import time
import urllib.request
import urllib.error
import wave
import zipfile
import numpy as np
import pytest
from project import Project,segment
from test_project import FakeEngine
from audio_edit import edit_segment,read,source_to_output,output_to_source
from local_api import APIService,validate_settings
from language_ai import prepare_segment


def test_following_sections_depth_first_and_persistence(tmp_path):
    p=Project.create(tmp_path,'Книга');root=p.data['root']
    a=p.add_node(root,'Глава 1');b=p.add_node(root,'Глава 2');c=p.add_node(a['id'],'Часть 1')
    for n in (a,b,c):p.append_text(n['id'],n['title'],300)
    assert [n['id'] for n,s in p.generation_rows(a['id'])]==[a['id'],c['id'],b['id']]
    assert [n['id'] for n,s in p.generation_rows(c['id'])]==[c['id'],b['id']]
    assert p.data['settings']['continue_next_sections'] is True
    p.data['settings']['mode']='advanced';p.data['characters']={'Анна':{'voice':'a'}};p.save()
    reopened=Project.open(tmp_path);reopened.data['settings']['mode']='simple';reopened.save()
    assert Project.open(tmp_path).data['characters']['Анна']['voice']=='a'


class FakeLLM:
    def __init__(self,bad=False):self.bad=bad
    def ask(self,key,instruction,payload,notify):
        if 'Исправь' in instruction:return {'text':'Анна сказала: «Привет!»'}
        if 'Сопоставь' in instruction:return {'assignments':{name:'a' for name in payload['characters']}}
        return {'labels':{} if self.bad else {'1':'Рассказчик','2':'Анна'}}


def test_correction_dialogue_loss_guard_and_zip_samples(tmp_path):
    p=Project.create(tmp_path/'p','Книга');s=segment('Анна сказали: «Привет!»');p.node(p.data['root'])['segments']=[s]
    cfg={**p.data['settings'],'mode':'advanced','correct_text':True,'dialogue':True,'voices':[{'id':'a','engine':'higgs','description':'женский','reference':'voices/a.wav'}]}
    with pytest.raises(ValueError,match='пропустил'):prepare_segment(p,s,cfg,FakeLLM(True))
    assert 'ai' not in s
    prepared=prepare_segment(p,s,cfg,FakeLLM())
    assert s['text']=='Анна сказали: «Привет!»'
    assert prepared['text']=='Анна сказала: «Привет!»'
    assert set(p.data['characters'])=={'Рассказчик','Анна'}
    p.data['settings']=cfg;p.path('voices').mkdir();p.path('voices/a.wav').write_bytes(b'sample')
    target=tmp_path/'bundle.zip';p.export_zip(p.data['root'],target)
    with zipfile.ZipFile(target) as z:assert 'voices/a.wav' in z.namelist()


@pytest.mark.parametrize('replacement',[50,160,500])
def test_exact_pause_replacement_restoration_and_seek(tmp_path,replacement):
    p=Project.create(tmp_path,'Паузы');s=segment('Текст');p.node(p.data['root'])['segments']=[s]
    tone=(np.sin(np.arange(9600)*.1)*10000).astype('<i2');data=np.concatenate([tone,np.zeros(28800,dtype='<i2'),tone])
    path=p.path('source.wav')
    with wave.open(str(path),'wb') as f:f.setparams((1,2,24000,0,'NONE',''));f.writeframes(data.tobytes())
    p.commit(s['id'],'source.wav',2,[],dict(passed=True,words=[]),{})
    edit_segment(p,s['id'],'detect',threshold_ms=1000,replacement_ms=replacement)
    rendered=read(p.path(s['audio']));assert len(rendered)==19200+replacement*24
    assert np.array_equal(rendered[:9600],tone) and np.array_equal(rendered[-9600:],tone)
    pause=next(part for part in s['audio_parts'] if not part['enabled'])
    point=(pause['start']+pause['end'])/2
    assert output_to_source(s,source_to_output(s,point))==pytest.approx(point)
    edit_segment(p,s['id'],'split',start=int(point));assert len(read(p.path(s['audio'])))==len(rendered)
    edit_segment(p,s['id'],'restore');assert np.array_equal(read(p.path(s['audio'])),data)


def wait_done(service,key):
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        state=service.status(key)
        if state['status'] in ('completed','error','paused'):return state
        time.sleep(.02)
    raise AssertionError('API timeout')


def test_http_api_long_text_audio_metadata_and_validation(tmp_path):
    service=APIService(tmp_path/'api',tmp_path/'models',FakeEngine());port=service.start(0)
    def request(path,body=None,auth=True):
        headers={'Authorization':'Bearer '+service.token} if auth else {}
        req=urllib.request.Request(f'http://127.0.0.1:{port}/v1/'+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        with urllib.request.urlopen(req,timeout=20) as r:return r.read()
    try:
        with pytest.raises(urllib.error.HTTPError) as error:request('settings',auth=False)
        assert error.value.code==401
        assert json.loads(request('settings'))['modes']==['simple','advanced']
        assert any(m['kind']=='text' for m in json.loads(request('models')))
        with pytest.raises(urllib.error.HTTPError):request('jobs',{'text':'Текст','settings':{'engine':'unknown'}})
        job=json.loads(request('jobs',{'text':'Длинная книга. '*200,'settings':{'max_chars':100,'auto_cut_pauses':False},'format':'wav'}))
        state=wait_done(service,job['id']);assert state['status']=='completed',state
        assert state['total']>20 and state['progress']==state['total']
        audio=request('jobs/'+job['id']+'/audio');assert audio[:4]==b'RIFF' and len(audio)>10000
        assert b'USLT' in audio
        assert json.loads(request('jobs/'+job['id']))['result'].endswith('/audio')
        direct=request('audio',{'text':'Прямой ответ аудио.','settings':{'auto_cut_pauses':False},'format':'flac'})
        assert direct[:4]==b'fLaC'
    finally:service.close()
    recovered=APIService(tmp_path/'api',tmp_path/'models',FakeEngine())
    assert recovered.status(job['id'])['status']=='completed'


def test_api_upload_and_voice_assignment_validation(tmp_path):
    service=APIService(tmp_path/'api',tmp_path/'models',FakeEngine())
    with pytest.raises(ValueError):validate_settings({'voices':[{'id':'one','reference':'C:/private'}]})
    with pytest.raises(ValueError):validate_settings({'max_chars':20.5})
    with pytest.raises(ValueError):service.create({'text':'Текст','voice_count':0})
    with pytest.raises(ValueError):service._asset('../private')
    with pytest.raises(Exception):service.upload({'kind':'cover','extension':'.png','data':base64.b64encode(b'not image').decode()})
    from PySide6.QtGui import QImage,QColor
    image=QImage(8,8,QImage.Format_RGB32);image.fill(QColor('red'));image.save(str(tmp_path/'cover.png'))
    uploaded=service.upload({'kind':'cover','extension':'.png','data':base64.b64encode((tmp_path/'cover.png').read_bytes()).decode()})
    assert service._asset(uploaded['id']).is_file()
    with pytest.raises(ValueError):service.create({'text':'Текст','settings':{'voices':[dict(id='x',sample_id=uploaded['id'])]}})


def test_advanced_synthesis_uses_corrected_transcript_and_speaker_settings(tmp_path):
    from engine import run_job
    class LLM(FakeLLM):
        def unload(self):pass
    class Engine(FakeEngine):
        def __init__(self):super().__init__();self.higgs=None;self.llm=LLM();self.options=[]
        def unload(self):pass
        def synthesize(self,text,path,settings,notify):self.options.append(dict(settings));return super().synthesize(text,path,settings,notify)
    p=Project.create(tmp_path,'Диалог');s=segment('Анна сказали: «Привет!»');p.node(p.data['root'])['segments']=[s]
    p.data['settings'].update(mode='advanced',dialogue=True,correct_text=True,auto_cut_pauses=False,voices=[dict(id='a',engine='piper-denis')])
    engine=Engine();run_job(p,engine,[s['id']],p.data['settings'],threading.Event())
    assert p.data['job']['status']=='completed',p.data['job']
    assert s['spoken_text']=='Анна сказала: «Привет!»'
    assert s['verification']['transcript']==s['spoken_text']
    assert s['text']=='Анна сказали: «Привет!»'
    assert all(o['engine']=='piper-denis' for o in engine.options)


def test_api_stop_resume_keeps_completed_fragments(tmp_path):
    class SlowEngine(FakeEngine):
        def synthesize(self,*args,**kwargs):time.sleep(.08);return super().synthesize(*args,**kwargs)
    service=APIService(tmp_path/'api',tmp_path/'models',SlowEngine())
    job=service.create({'text':'Предложение. '*100,'settings':{'max_chars':100,'auto_cut_pauses':False}})
    deadline=time.monotonic()+10
    while service.status(job['id'])['progress']<1 and time.monotonic()<deadline:time.sleep(.01)
    service.stops[job['id']].set();state=wait_done(service,job['id']);assert state['status']=='paused'
    service.threads[job['id']].join()
    p=Project.open(service.root/'jobs'/job['id']);first=p.node(p.data['root'])['segments'][0]['audio']
    service.resume(job['id']);state=wait_done(service,job['id']);assert state['status']=='completed',state
    p=Project.open(service.root/'jobs'/job['id']);assert p.node(p.data['root'])['segments'][0]['audio']==first


def test_move_disabled_pause_restores_previous_location_and_undo(tmp_path):
    from test_audio_edit import generated
    from audio_edit import pause_move_bounds
    p,s=generated(tmp_path);original=read(p.path(s['source_audio']));before=read(p.path(s['audio']))
    pause=next(part for part in s['audio_parts'] if not part['enabled']);start=pause['start'];pid=pause['id']
    duration=s['duration'];edit_segment(p,s['id'],'move_pause',ids=[pid],delta_ms=100)
    moved=next(part for part in s['audio_parts'] if part['id']==pid)
    assert moved['start']==start+2400 and s['duration']==pytest.approx(duration)
    expected=np.concatenate([original[part['start']:part['end']] for part in s['audio_parts'] if part['enabled']])
    assert np.array_equal(read(p.path(s['audio'])),expected)
    assert all(a['end']<=b['start'] for a,b in zip(s['audio_parts'],s['audio_parts'][1:]))
    edit_segment(p,s['id'],'undo');assert np.array_equal(read(p.path(s['audio'])),before)
    low,high=pause_move_bounds(s['audio_parts'],pid)
    edit_segment(p,s['id'],'move_pause',ids=[pid],delta_frames=high+999999)
    assert next(part for part in s['audio_parts'] if part['id']==pid)['start']==start+high
    edit_segment(p,s['id'],'restore');assert np.array_equal(read(p.path(s['audio'])),original)


from test_ui import app,window


def test_ctrl_wheel_zoom_anchor_and_alt_pause_drag(window,app,monkeypatch):
    from PySide6.QtCore import Qt,QPoint,QPointF
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication,QMenu
    from test_audio_edit import PausedEngine
    from test_background_import import wait_until
    from engine import run_job
    p=window.project;s=window.rows[0][1]
    run_job(p,PausedEngine(),[s['id']],p.data['settings'],threading.Event());window.refresh();app.processEvents()
    timeline=window.timeline;window.timeline_zoom.setValue(10);app.processEvents()
    scroll=window.timeline_scroll.horizontalScrollBar();scroll.setValue(200);app.processEvents()
    pos=QPointF(400,60);before=timeline.hit(pos);viewport_x=pos.x()-scroll.value()
    event=QWheelEvent(pos,QPointF(timeline.mapToGlobal(pos.toPoint())),QPoint(),QPoint(0,120),Qt.NoButton,Qt.ControlModifier,Qt.NoScrollPhase,False)
    QApplication.sendEvent(timeline,event);app.processEvents()
    assert window.timeline_zoom.value()==11
    after=timeline.hit(QPointF(viewport_x+scroll.value(),60))
    assert before[0]['id']==after[0]['id'] and abs(before[2]-after[2])<150
    pause=next(part for part in s['audio_parts'] if not part['enabled']);old=pause['start'];frames=s['source_frames']
    x,width,_=timeline.rects[0];point=QPoint(round(x+width*(pause['start']+pause['end'])/2/frames),60)
    labels=[]
    class Menu(QMenu):
        def exec(self,*a):labels.extend(action.text() for action in self.actions())
    monkeypatch.setattr('main.QMenu',Menu);window.audio_context_menu(point);assert 'Сместить паузу…' in labels
    QTest.mousePress(timeline,Qt.LeftButton,Qt.AltModifier,point)
    QTest.mouseMove(timeline,point+QPoint(15,0));QTest.mouseRelease(timeline,Qt.LeftButton,Qt.AltModifier,point+QPoint(15,0))
    wait_until(app,lambda:not window.busy());assert next(part for part in s['audio_parts'] if part['id']==pause['id'])['start']>old


def test_gui_single_section_queues_following_and_option_disables(window,monkeypatch):
    p=window.project;root=p.data['root'];nodes=[p.add_node(root,str(i)) for i in range(3)]
    for n in nodes:p.append_text(n['id'],'Раздел '+n['title'],300)
    window.rebuild_tree();window.tree.setCurrentItem(window.tree_items[nodes[1]['id']]);window.refresh()
    jobs=[];monkeypatch.setattr(window,'start_job',lambda ids:jobs.append(ids));window.generate()
    assert jobs[-1]==[s['id'] for n in nodes[1:] for s in n['segments']]
    p.data['settings']['continue_next_sections']=False;window.generate()
    assert jobs[-1]==[s['id'] for s in nodes[1]['segments']]


def test_export_and_live_settings_during_generation(window,app,tmp_path):
    from engine import run_job
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QComboBox,QSpinBox,QDialogButtonBox
    from test_background_import import wait_until
    p=window.project;rows=p.rows(p.data['root']);ready=rows[0][1]
    p.data['settings']['auto_cut_pauses']=False
    run_job(p,FakeEngine(),[ready['id']],p.data['settings'],threading.Event())
    gate=threading.Event();started=threading.Event()
    class BlockingEngine(FakeEngine):
        def __init__(self):super().__init__();self.options=[]
        def synthesize(self,text,path,settings,notify):
            self.options.append(dict(settings))
            if len(self.options)==1:started.set();assert gate.wait(10)
            return super().synthesize(text,path,settings,notify)
    engine=BlockingEngine();window.engine=engine
    window.start_job([s['id'] for _,s in rows[1:]])
    try:
        wait_until(app,started.is_set)
        assert window.settings_button.isEnabled() and window.export_button.isEnabled()
        target=tmp_path/'while-generating.wav'
        window.begin_export([p.node(p.data['root'])],target,segment_ids=[ready['id']])
        wait_until(app,lambda:not window.exporter.isRunning())
        assert window.worker.isRunning() and target.stat().st_size>44
        def change_settings():
            dialog=app.activeModalWidget();spin=next(s for s in dialog.findChildren(QSpinBox) if s.maximum()==2400)
            spin.setValue(200);dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok).click()
        QTimer.singleShot(50,change_settings);window.settings()
        assert 'следующего фрагмента' in window.settings_notice.text()
        assert p.data['settings']['max_chars']==200
    finally:
        gate.set();wait_until(app,lambda:not window.worker.isRunning())
    assert [s['max_chars'] for s in engine.options]==[300,200]
    assert rows[1][1]['generation']['max_chars']==300 and rows[2][1]['generation']['max_chars']==200


def test_export_snapshot_is_immutable_after_regeneration(tmp_path):
    from main import ExportWorker
    from test_project import wav
    p=Project.create(tmp_path/'project','Снимок');s=segment('Текст');p.node(p.data['root'])['segments']=[s]
    wav(p.path('old.wav'),2400);p.commit(s['id'],'old.wav',.1,[],dict(passed=True),{})
    out=tmp_path/'snapshot.wav';worker=ExportWorker(p,[p.node(p.data['root'])],out)
    wav(p.path('new.wav'),4800);p.commit(s['id'],'new.wav',.2,[],dict(passed=True),{})
    worker.run()
    with wave.open(str(out)) as f:assert f.getnframes()==2400
