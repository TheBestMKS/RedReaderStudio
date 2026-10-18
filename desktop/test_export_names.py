from pathlib import Path
from PySide6.QtWidgets import QFileDialog
from test_ui import app,window
from project import Project


def test_export_names_include_complete_ancestry(tmp_path):
    p=Project.create(tmp_path,'Книга')
    chapter=p.add_node(p.data['root'],'Часть первая')
    scene=p.add_node(chapter['id'],'Глава 12')
    assert p.export_name(p.data['root'])=='Книга'
    assert p.export_name(scene['id'])=='Книга — Часть первая — Глава 12'
    scene['title']='Глава: "день/ночь"?'
    assert p.export_name(scene['id']).endswith('Глава_ _день_ночь__')
    p.node(p.data['root'])['title']='CON'
    assert p.export_name(p.data['root'])=='_CON'
    for n in p.data['nodes']:n['title']=n['title']*100
    assert len(p.export_name(scene['id']))<=180


def test_audio_dialog_suggests_path_and_preserves_user_filename(window,monkeypatch,tmp_path):
    p=window.project;child=p.add_node(p.data['root'],'Глава');sub=p.add_node(child['id'],'Сцена')
    window.current_node=sub['id'];window.rebuild_tree();window.refresh()
    suggested=[];requested=[]
    custom=tmp_path/'Моё имя.mp3'
    def choose(parent,title,default,filters):
        suggested.append(Path(default).name);return str(custom),'MP3 (*.mp3)'
    monkeypatch.setattr(QFileDialog,'getSaveFileName',choose)
    monkeypatch.setattr(window,'request_export',lambda *args:requested.append(args))
    window.export_audio()
    assert suggested==['Тест интерфейса — Глава — Сцена.wav']
    assert requested[0][1]==str(custom)
