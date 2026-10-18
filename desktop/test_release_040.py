import copy
import threading
import wave
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMenu,QMessageBox,QFormLayout,QWidget,QComboBox,QSpinBox
from test_ui import app,window
from project import Project,split_text
from text_processing import DEFAULT_PROCESSING,clean_text
from main import ExportWorker
from test_project import FakeEngine
from engine import run_job


def test_split_packs_sentences_from_next_paragraph():
    text='Первое предложение.\nКоротко. Второе предложение.\n'
    result=split_text(text,35)
    assert result==['Первое предложение.\nКоротко. ','Второе предложение.\n']
    assert ''.join(result)==text


def test_long_sentence_starts_new_fragment_then_splits_at_furthest_comma():
    long='а'*12+', '+'б'*12+', '+'в'*12+'.'
    assert split_text('Начало. '+long,30)==['Начало. ','а'*12+', '+'б'*12+', ','в'*12+'.']
    sentence='Большое предложение с несколькими словами.'
    assert split_text('Начало. Начало. '+sentence,50)==['Начало. Начало. ',sentence]


def test_priorities_and_cleanup_cycles_are_configurable():
    text='один два три четыре пять шесть семь'
    space=split_text(text,21,dict(priorities=['space','off','off','off']))
    hard=split_text(text,21,dict(priorities=['off']*4))
    assert space!=hard and ''.join(space)==''.join(hard)==text
    source='A'+' '*16+'B'+'\n'*16+'C'
    assert clean_text(source,dict(space_cycles=3,paragraph_cycles=3))=='A  B\n\nC'
    assert clean_text(source,dict(space_cycles=3,paragraph_cycles=3,remove_linebreaks=True))=='A  B C'
    opts={**DEFAULT_PROCESSING,'remove_linebreaks':True}
    fragments=split_text('Первое предложение.\nКоротко. Второе предложение.',35,opts)
    assert all('\n' not in x for x in fragments)
    assert len(fragments)==2


def test_project_defaults_and_processing_controls(window,tmp_path):
    p=Project.create(tmp_path/'defaults','Книга')
    assert p.data['settings']['max_chars']==300
    assert p.data['settings']['text_processing']==DEFAULT_PROCESSING
    box=QWidget();form=QFormLayout(box);read=window.processing_controls(form,{})
    assert read()==DEFAULT_PROCESSING
    assert len(box.findChildren(QComboBox))==4
    assert [s.value() for s in box.findChildren(QSpinBox)]==[3,3]
    window.editor.setPlainText('Привет\nмир🙂')
    assert window.character_count.text()=='11 символов'


def sections(window):
    p=window.project;root=p.data['root'];p.remove_segments([s['id'] for _,s in p.rows(root)])
    nodes=[p.add_node(root,title) for title in ['Один','Два','Три']]
    for n in nodes:p.append_text(n['id'],n['title']+'.',300)
    window.rebuild_tree();window.refresh()
    return p,nodes


def select_nodes(window,nodes):
    window.tree.clearSelection()
    for n in nodes:window.tree_items[n['id']].setSelected(True)


def test_group_menu_preserves_selection_and_generates_once(window,monkeypatch):
    p,nodes=sections(window);select_nodes(window,[nodes[0],nodes[2]])
    labels=[]
    class Menu(QMenu):
        def exec(self,*args):labels.extend(a.text() for a in self.actions())
    monkeypatch.setattr('main.QMenu',Menu)
    window.tree_context_menu(window.tree.visualItemRect(window.tree_items[nodes[0]['id']]).center())
    assert window.selected_node_ids()==[nodes[0]['id'],nodes[2]['id']]
    assert len(labels)==7 and 'Экспорт в аудио' in labels
    jobs=[];monkeypatch.setattr(window,'start_job',lambda ids:jobs.append(ids));window.generate()
    assert jobs==[[nodes[0]['segments'][0]['id'],nodes[2]['segments'][0]['id']]]
    select_nodes(window,[p.node(p.data['root']),nodes[0]])
    assert window.selected_node_ids()==[p.data['root']]


def test_tree_ctrl_shift_and_group_duplicate_delete(window,monkeypatch):
    p,nodes=sections(window)
    def click(node,modifier=Qt.NoModifier):
        QTest.mouseClick(window.tree.viewport(),Qt.LeftButton,modifier,window.tree.visualItemRect(window.tree_items[node['id']]).center())
    click(nodes[0]);click(nodes[2],Qt.ShiftModifier)
    assert len(window.selected_node_ids())==3
    click(nodes[1],Qt.ControlModifier)
    assert window.selected_node_ids()==[nodes[0]['id'],nodes[2]['id']]
    window.duplicate_section()
    assert len(p.data['nodes'])==6 and len(window.selected_node_ids())==2
    monkeypatch.setattr(QMessageBox,'question',lambda *a,**k:QMessageBox.Yes)
    window.delete_section()
    assert len(p.data['nodes'])==4


def test_group_export_is_one_audio_and_cleans_embedded_text(window,tmp_path):
    import mutagen
    p,nodes=sections(window);p.data['settings']['pause_ms']=0
    nodes[0]['segments'][0]['text']='Первый    текст.\n\n\n\n'
    ids=[s['id'] for _,s in p.rows(p.data['root'])]
    run_job(p,FakeEngine(),ids,p.data['settings'],threading.Event())
    output=tmp_path/'group.wav'
    worker=ExportWorker(p,[nodes[0],nodes[2]],output,metadata={'text':True,'processing':dict(space_cycles=1,paragraph_cycles=1)})
    errors=[];worker.failure.connect(errors.append);worker.run();assert not errors
    with wave.open(str(output)) as f:assert f.getnframes()==4800
    text=mutagen.File(output).tags.getall('USLT')[0].text
    assert text==clean_text(nodes[0]['segments'][0]['text']+'\n\n'+nodes[2]['segments'][0]['text'],dict(space_cycles=1,paragraph_cycles=1))
    assert 'Два' not in text


def test_group_rebuild_uses_current_priorities(window,monkeypatch):
    p,nodes=sections(window);select_nodes(window,[nodes[0],nodes[2]])
    ids=[n['segments'][0]['id'] for n in nodes]
    monkeypatch.setattr(QMessageBox,'question',lambda *a,**k:QMessageBox.Yes)
    p.data['settings'].update(max_chars=20,text_processing=dict(priorities=['off']*4))
    window.rebuild_section()
    assert nodes[0]['segments'][0]['id']!=ids[0]
    assert nodes[1]['segments'][0]['id']==ids[1]
    assert nodes[2]['segments'][0]['id']!=ids[2]
