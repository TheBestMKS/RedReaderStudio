import json
import threading
import wave
from pathlib import Path

import pytest

from project import Project, split_text
from engine import compare, run_job


def wav(path, frames=2400):
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000)
        w.writeframes(b'\x01\x00'*frames)


def test_split_is_lossless():
    for text in ['Слово '*300+'\n\nПоследний абзац.','ы'*2100,'Первый! Второй?\n\nТретий. ', 'A🙂 Б '*60]:
        parts=split_text(text,60)
        assert ''.join(parts)==text
        assert all(len(p)<=60 for p in parts)


def test_hierarchy_export_and_history(tmp_path):
    p=Project.create(tmp_path/'book','Книга');root=p.data['root']
    a=p.add_node(root,'Глава');b=p.add_node(a['id'],'Сцена')
    for n in [root,a['id'],b['id']]:p.append_text(n,'Привет, мир.',100)
    for _,s in p.rows(root):
        rel=f"audio/{s['id']}.wav";path=p.path(rel);path.parent.mkdir(exist_ok=True);wav(path)
        p.commit(s['id'],rel,.1,[.2],dict(passed=True),{})
    p.export(root,tmp_path/'all.wav',100)
    p.export(a['id'],tmp_path/'chapter.wav',100)
    with wave.open(str(tmp_path/'all.wav')) as w:assert w.getnframes()==12000
    with wave.open(str(tmp_path/'chapter.wav')) as w:assert w.getnframes()==7200
    s=p.rows(root)[0][1];p.edit(s['id'],'Новый текст.')
    assert s['status']=='pending' and len(s['versions'])==1
    p.export(root,tmp_path/'incomplete.wav')
    with wave.open(str(tmp_path/'incomplete.wav')) as w:assert w.getnframes()>0
    reopened=Project.open(p.folder)
    assert len(reopened.rows(root))==3
    assert reopened.rows(root)[0][1]['versions'][0]['text']=='Привет, мир.'


def test_split_move_and_restart(tmp_path):
    p=Project.create(tmp_path,'Тест');root=p.data['root']
    p.append_text(root,'Первая часть. Вторая часть.',100)
    s=p.rows(root)[0][1];p.split(s['id'],14)
    assert ''.join(s['text'] for _,s in p.rows(root))=='Первая часть. Вторая часть.'
    node=p.add_node(root,'Подраздел')
    p.move([p.rows(root)[0][1]['id']],node['id'])
    p.rows(root)[0][1]['status']='generating'
    p.data['job']=dict(status='running',ids=[],done=[]);p.save()
    restored=Project.open(tmp_path)
    assert restored.rows(root)[0][1]['status']=='pending'
    assert restored.data['job']['status']=='paused'
    with pytest.raises(ValueError):p.path('../escape')


def test_verification_detects_omissions_and_repetition():
    assert compare('Привет, мир!','Привет мир.')['passed']
    assert compare('У меня 12 книг.','У меня двенадцать книг')['passed']
    assert not compare('Первое второе последнее','Первое второе')['passed']
    assert not compare('Первое второе','Первое первое второе')['passed']
    assert not compare('Тест','')['passed']


class FakeEngine:
    def __init__(self,stop=None,fail=False):self.calls=[];self.stop=stop;self.fail=fail
    def synthesize(self,text,path,settings,notify):
        self.calls.append(text);wav(path)
        if self.stop:self.stop.set()
        return .1,[.1]
    def verify(self,path,text,language,notify):return dict(passed=not self.fail,transcript=text)


def test_stop_resume_does_not_regenerate_completed(tmp_path):
    p=Project.create(tmp_path,'Тест');root=p.data['root']
    for t in ['Один.','Два.','Три.']:p.append_text(root,t,100)
    ids=[s['id'] for _,s in p.rows(root)];stop=threading.Event();engine=FakeEngine(stop)
    run_job(p,engine,ids,{},stop)
    assert len(engine.calls)==1
    assert p.data['job']['status']=='paused'
    p=Project.open(tmp_path)
    remaining=[sid for sid in p.data['job']['ids'] if sid not in p.data['job']['done']]
    engine=FakeEngine();run_job(p,engine,remaining,{},threading.Event())
    assert engine.calls==['Два.','Три.']
    assert all(s['status']=='ready' for _,s in p.rows(root))
    with wave.open(str(tmp_path/'preview.wav')) as w:assert w.getnframes()==14880


def test_review_does_not_stop_queue(tmp_path):
    p=Project.create(tmp_path,'Тест');root=p.data['root']
    for text in ['Один','Два']:p.append_text(root,text,100)
    engine=FakeEngine(fail=True)
    run_job(p,engine,[s['id'] for _,s in p.rows(root)],{},threading.Event())
    assert len(engine.calls)==2
    assert p.rows(root)[0][1]['status']=='review'
    assert p.rows(root)[1][1]['status']=='review'
    assert p.data['job']['status']=='completed'
    assert p.path(p.rows(root)[0][1]['audio']).exists()


def test_regeneration_archives_old_audio(tmp_path):
    p=Project.create(tmp_path,'Тест');p.append_text(p.data['root'],'Текст',100)
    s=p.rows(p.data['root'])[0][1]
    run_job(p,FakeEngine(),[s['id']],{},threading.Event());old=s['audio']
    run_job(p,FakeEngine(),[s['id']],{},threading.Event())
    assert s['audio']!=old
    assert s['versions'][0]['audio']==old
    assert p.path(old).exists()


def test_preview_appends_and_recovers_interrupted_write(tmp_path, monkeypatch):
    p=Project.create(tmp_path,'Тест');root=p.data['root']
    for t in ['Один','Два']:p.append_text(root,t,100)
    ids=[s['id'] for _,s in p.rows(root)]
    run_job(p,FakeEngine(),ids[:1],{},threading.Event())
    original_export=p.export
    def unexpected(*args,**kwargs):raise AssertionError('Sequential generation must append')
    monkeypatch.setattr(p,'export',unexpected)
    run_job(p,FakeEngine(),ids[1:],{},threading.Event())
    with wave.open(str(tmp_path/'preview.wav')) as w:assert w.getnframes()==8640
    monkeypatch.setattr(p,'export',original_export)
    with (tmp_path/'preview.wav').open('ab') as f:f.write(b'broken')
    p.update_preview()
    with wave.open(str(tmp_path/'preview.wav')) as w:assert w.getnframes()==8640
    assert (tmp_path/'preview.wav').stat().st_size==44+8640*2


def test_large_book_uses_rf64(tmp_path,monkeypatch):
    import project
    import soundfile as sf
    monkeypatch.setattr(project,'RIFF_LIMIT',1000)
    p=Project.create(tmp_path,'Большая книга');root=p.data['root']
    for text in ['Начало','Конец']:p.append_text(root,text,100)
    run_job(p,FakeEngine(),[s['id'] for _,s in p.rows(root)],{},threading.Event())
    output=p.export(root,tmp_path/'book.wav')
    assert output.read_bytes()[:4]==b'RF64'
    assert sf.info(str(output)).frames==8640
    assert sf.info(str(tmp_path/'preview.wav')).frames==8640
