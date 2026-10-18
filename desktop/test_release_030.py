import base64
import json
import threading
import wave
import zipfile
from pathlib import Path

import pytest
from project import Project
from engine import run_job, convert
from test_project import FakeEngine


def book(tmp_path):
    p=Project.create(tmp_path/'book','Книга');root=p.data['root']
    for text in ['Первый. ','Второй. ','Третий. ']:p.append_text(root,text,100)
    return p,root,[s['id'] for _,s in p.rows(root)]


def test_partial_unconfirmed_export_and_empty_safety(tmp_path):
    p,root,ids=book(tmp_path)
    run_job(p,FakeEngine(fail=True),ids[::2],{},threading.Event())
    path=p.export(root,tmp_path/'mixed.wav')
    with wave.open(str(path)) as w:assert w.getnframes()==8640
    # A requested empty selection must never truncate an existing result.
    original=path.read_bytes()
    with pytest.raises(ValueError):p.export(root,path,segment_ids=[ids[1]])
    assert path.read_bytes()==original
    selected=p.export(root,tmp_path/'selected.wav',segment_ids=[ids[2]])
    with wave.open(str(selected)) as w:assert w.getnframes()==2400


def test_asr_exception_keeps_audio_and_continues(tmp_path):
    p,root,ids=book(tmp_path)
    class BrokenASR(FakeEngine):
        def verify(self,*args):raise RuntimeError('ASR unavailable')
    run_job(p,BrokenASR(),ids,{},threading.Event())
    assert all(p.has_audio(s) for _,s in p.rows(root))
    assert p.data['job']['status']=='completed'
    assert all(s['verification']['error']=='ASR unavailable' for _,s in p.rows(root))


def test_rebuild_disjoint_selection_and_remove(tmp_path):
    p,root,ids=book(tmp_path)
    middle=p.find_segment(ids[1])[1]
    p.rebuild_segments([ids[0],ids[2]],20)
    assert p.rows(root)[1][1] is middle
    assert ''.join(s['text'] for _,s in p.rows(root))=='Первый. Второй. Третий. '
    assert p.rows(root)[0][1]['id']!=ids[0]
    p.remove_segments([ids[1]])
    assert len(p.rows(root))==2


def test_nested_clone_counts_delete_and_zip(tmp_path):
    p,root,ids=book(tmp_path)
    child=p.add_node(root,'Глава');sub=p.add_node(child['id'],'Сцена')
    p.move(ids,sub['id']);run_job(p,FakeEngine(fail=True),ids[:2],{},threading.Event())
    p.find_segment(ids[0])[1]['verification']['manual']=True;p.save()
    assert p.counts(root)==(3,2,1)
    assert p.counts(child['id'])==(3,2,1)
    clone=p.duplicate_node(child['id'])
    assert p.counts(root)==(6,4,2)
    p.edit(p.rows(clone['id'])[0][1]['id'],'Копия изменена')
    assert p.find_segment(ids[0])[1]['text']=='Первый. '
    archive=tmp_path/'chapter.zip';p.export_zip(child['id'],archive)
    unpack=tmp_path/'unpacked'
    with zipfile.ZipFile(archive) as z:z.extractall(unpack)
    restored=Project.open(unpack)
    assert restored.counts(restored.data['root'])==(3,2,1)
    restored.export(restored.data['root'],tmp_path/'restored.wav')
    p.remove_node(clone['id']);assert p.counts(root)==(3,2,1)


def test_old_project_migration(tmp_path):
    p,root,ids=book(tmp_path);p.data['version']='0.2.0';p.save()
    loaded=Project.open(p.folder)
    from project import VERSION
    assert loaded.data['version']==VERSION
    assert json.loads((p.folder/'project-v0.2.0.backup.json').read_text(encoding='utf-8'))['version']=='0.2.0'


@pytest.mark.parametrize('extension',['wav','mp3','flac','m4a','m4b'])
def test_export_worker_includes_artwork_and_only_spoken_text(tmp_path,extension):
    from main import ExportWorker
    import mutagen
    p,root,ids=book(tmp_path)
    run_job(p,FakeEngine(fail=True),[ids[0]],{},threading.Event())
    cover=tmp_path/'cover.png'
    cover.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZ1cAAAAASUVORK5CYII='))
    target=tmp_path/('book.'+extension)
    worker=ExportWorker(p,[p.node(root)],target,metadata={'text':True,'cover':str(cover)})
    errors=[];worker.failure.connect(errors.append);worker.run()
    assert not errors,errors
    assert target.stat().st_size>100
    audio=mutagen.File(target)
    if extension in ('mp3','wav'):
        assert audio.tags.getall('USLT')[0].text=='Первый. '
        assert audio.tags.getall('APIC')[0].data==cover.read_bytes()
    elif extension=='flac':
        assert audio['lyrics']==['Первый. ']
        assert audio.pictures[0].data==cover.read_bytes()
    else:
        assert audio['\xa9lyr']==['Первый. ']
        assert bytes(audio['covr'][0])==cover.read_bytes()


def test_export_worker_does_not_touch_existing_file_on_failure(tmp_path):
    from main import ExportWorker
    p,root,ids=book(tmp_path);target=tmp_path/'existing.wav';target.write_bytes(b'original')
    worker=ExportWorker(p,[p.node(root)],target);errors=[];worker.failure.connect(errors.append);worker.run()
    assert errors and target.read_bytes()==b'original'
