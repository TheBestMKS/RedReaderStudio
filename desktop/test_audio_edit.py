import copy
import threading
import zipfile
import numpy as np
import pytest
from PySide6.QtCore import Qt,QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMenu
from project import Project
from engine import run_job
from audio_edit import read,edit_segment,source_to_output,output_to_source
from text_processing import clean_text,symbol_list
from test_ui import app,window
from test_background_import import wait_until


class PausedEngine:
    def synthesize(self,text,path,settings,notify):
        import wave
        tone=(np.sin(np.arange(9600)*.1)*9000).astype('<i2')
        data=np.concatenate([tone,np.zeros(28800,dtype='<i2'),tone,np.zeros(36000,dtype='<i2'),tone])
        with wave.open(str(path),'wb') as f:
            f.setparams((1,2,24000,0,'NONE','not compressed'));f.writeframes(data.tobytes())
        return len(data)/24000,[.3]*160
    def verify(self,*args):
        return dict(passed=True,transcript='Один два три',words=[dict(word='Один',start=.1,end=.3),dict(word='два',start=1.7,end=1.9),dict(word='три',start=3.6,end=3.8)])


def generated(tmp_path):
    p=Project.create(tmp_path/'p','Книга');p.append_text(p.data['root'],'Один два три',300)
    s=p.rows(p.data['root'])[0][1]
    run_job(p,PausedEngine(),[s['id']],p.data['settings'],threading.Event())
    assert p.data['job']['status']=='completed',p.data['job']
    return p,s


def test_symbols_escape_and_toggle():
    assert symbol_list('«;»;^;;^^')==['«','»',';','^']
    assert symbol_list('^x;^')==['^x','^']
    options=dict(remove_symbols=True,symbols='«;»;^;;^^')
    assert clean_text('«Текст»; ^',options)=='Текст '
    assert clean_text('«Текст»;',dict(remove_symbols=False,symbols='«;»;^;'))=='«Текст»;'


def test_auto_pauses_are_reversible_and_timestamps_remapped(tmp_path):
    p,s=generated(tmp_path)
    assert len(s['audio_parts'])==5
    assert sum(not part['enabled'] for part in s['audio_parts'])==2
    assert s['duration']==pytest.approx(2.4)
    assert s['verification']['words'][1]['start']==pytest.approx(1.1)
    exported=p.export(p.data['root'],tmp_path/'edited.wav',pause_ms=0)
    assert len(read(exported))==57600
    np.testing.assert_array_equal(read(exported),read(p.path(s['audio'])))
    original=read(p.path(s['source_audio']));assert len(original)==93600
    pauses=[part['id'] for part in s['audio_parts'] if not part['enabled']]
    edit_segment(p,s['id'],'toggle',ids=pauses)
    assert s['duration']==pytest.approx(3.9)
    np.testing.assert_array_equal(read(p.path(s['audio'])),original)
    edit_segment(p,s['id'],'undo');assert s['duration']==pytest.approx(2.4)
    assert source_to_output(s,1.7*24000)==pytest.approx(1.1)
    assert output_to_source(s,1.1)/24000==pytest.approx(1.7)


def test_split_merge_range_delete_undo_and_all_muted_export(tmp_path):
    p,s=generated(tmp_path);edit_segment(p,s['id'],'restore')
    original=read(p.path(s['audio']))
    edit_segment(p,s['id'],'split',start=2400)
    assert len(s['audio_parts'])==2
    edit_segment(p,s['id'],'merge',ids=[part['id'] for part in s['audio_parts']])
    assert len(s['audio_parts'])==1
    edit_segment(p,s['id'],'range_delete',start=2400,end=4800)
    np.testing.assert_array_equal(read(p.path(s['audio'])),np.concatenate([original[:2400],original[4800:]]))
    with pytest.raises(ValueError):edit_segment(p,s['id'],'merge',ids=[part['id'] for part in s['audio_parts']])
    edit_segment(p,s['id'],'undo');np.testing.assert_array_equal(read(p.path(s['audio'])),original)
    edit_segment(p,s['id'],'toggle',ids=[part['id'] for part in s['audio_parts']])
    assert s['muted'] and p.has_audio(s)
    with pytest.raises(ValueError):p.export(p.data['root'],tmp_path/'empty.wav')
    p.update_preview();assert not (p.folder/'preview.wav').exists()
    edit_segment(p,s['id'],'restore');assert not s['muted']


def test_original_and_piece_edits_survive_zip_and_regeneration(tmp_path):
    p,s=generated(tmp_path);archive=tmp_path/'book.zip';p.export_zip(p.data['root'],archive)
    with zipfile.ZipFile(archive) as z:
        assert s['source_audio'] in z.namelist();z.extractall(tmp_path/'restored')
    restored=Project.open(tmp_path/'restored');rs=restored.rows(restored.data['root'])[0][1]
    edit_segment(restored,rs['id'],'restore');assert rs['duration']==pytest.approx(3.9)
    settings={**p.data['settings'],'auto_cut_pauses':False}
    run_job(p,PausedEngine(),[s['id']],settings,threading.Event())
    assert not s.get('audio_parts') and s['duration']==pytest.approx(3.9)
    assert s['versions'][-1]['source_audio']


def test_pause_threshold_is_configurable_and_detection_preserves_short_gaps(tmp_path):
    p,s=generated(tmp_path);edit_segment(p,s['id'],'restore')
    edit_segment(p,s['id'],'detect',threshold_ms=1300)
    assert sum(not part['enabled'] for part in s['audio_parts'])==1
    assert s['duration']==pytest.approx(2.48)


def test_timeline_range_selection_context_menu_and_edit_worker(window,app,monkeypatch):
    p=window.project;s=window.rows[0][1]
    run_job(p,PausedEngine(),[s['id']],p.data['settings'],threading.Event());window.refresh();app.processEvents()
    window.timeline.edit_mode=True
    x,w,seg=window.timeline.rects[0]
    start=QPoint(x+3,60);end=QPoint(x+int(w*.25),60)
    QTest.mousePress(window.timeline,Qt.LeftButton,Qt.NoModifier,start)
    QTest.mouseMove(window.timeline,end);QTest.mouseRelease(window.timeline,Qt.LeftButton,Qt.NoModifier,end)
    assert window.timeline.audio_range[2]>window.timeline.audio_range[1]
    labels=[]
    class Menu(QMenu):
        def exec(self,*args):labels.extend(a.text() for a in self.actions())
    monkeypatch.setattr('main.QMenu',Menu);window.audio_context_menu(end)
    assert 'Удалить выделенную область' in labels and 'Найти и разделить паузу' in labels
    before=s['duration'];_,a,b=window.timeline.audio_range
    window.edit_audio(s['id'],'range_delete',start=a,end=b)
    wait_until(app,lambda:not window.busy());assert s['duration']<before
