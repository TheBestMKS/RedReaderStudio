import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QSettings
from PySide6.QtTest import QTest

from main import MainWindow, configure_app
from project import Project


@pytest.fixture(scope='module')
def app():
    application=QApplication.instance() or QApplication([])
    configure_app(application)
    return application


@pytest.fixture
def window(app,tmp_path):
    preferences=QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat)
    win=MainWindow(preferences);p=Project.create(tmp_path,'Тест интерфейса')
    for text in ['Первый фрагмент.','Второй фрагмент.','Третий фрагмент.']:
        p.append_text(p.data['root'],text,100)
    win.set_project(p,win.acquire_project(tmp_path));win.show();app.processEvents()
    yield win
    if win.import_active():
        win.cancel_import()
        for _ in range(200):
            app.processEvents();QTest.qWait(10)
            if not win.import_active():break
    win.close()


def click_row(win,row,modifier=Qt.NoModifier):
    point=win.table.visualItemRect(win.table.item(row,1)).center()
    QTest.mouseClick(win.table.viewport(),Qt.LeftButton,modifier,point)


def test_ctrl_shift_and_editor_autosave(window,app):
    click_row(window,0)
    click_row(window,2,Qt.ShiftModifier)
    assert len(window.selected_ids())==3
    click_row(window,1,Qt.ControlModifier)
    assert len(window.selected_ids())==2
    click_row(window,0)
    window.editor.setPlainText('Изменённый текст.')
    click_row(window,2)
    assert window.rows[0][1]['text']=='Изменённый текст.'
    assert window.editor.toPlainText()=='Третий фрагмент.'


def test_manual_split_updates_rows_and_queue(window,app):
    click_row(window,0)
    seg_id=window.editor_id
    window.project.data['job']=dict(status='paused',ids=[seg_id],done=[])
    cursor=window.editor.textCursor();cursor.setPosition(7);window.editor.setTextCursor(cursor)
    window.split_segment();app.processEvents()
    assert window.table.rowCount()==4
    assert ''.join(s['text'] for _,s in window.rows[:2])=='Первый фрагмент.'
    assert window.project.data['job']['ids']==[s['id'] for _,s in window.rows[:2]]


def test_project_lock_prevents_second_writer(window):
    with pytest.raises(ValueError,match='другом окне'):
        window.acquire_project(window.project.folder)


def test_context_menus_preserve_selection_and_expose_actions(window,monkeypatch):
    from PySide6.QtWidgets import QMenu
    menus=[]
    class Menu(QMenu):
        def exec(self,*args):
            menus.append([a.text() for a in self.actions()])
    monkeypatch.setattr('main.QMenu',Menu)
    click_row(window,0);click_row(window,2,Qt.ControlModifier)
    before=set(window.selected_ids())
    window.fragment_context_menu(window.table.visualItemRect(window.table.item(0,1)).center())
    assert set(window.selected_ids())==before
    assert menus[-1]==['Удалить','Пересобрать под установленную длину фрагмента','Экспорт аудио','Генерировать аудио']
    item=window.tree.topLevelItem(0)
    window.tree_context_menu(window.tree.visualItemRect(item).center())
    assert len(menus[-1])==10 and 'Экспорт проекта в zip' in menus[-1]


def test_continue_section_skips_unconfirmed_audio(window,monkeypatch):
    import threading
    from engine import run_job
    from test_project import FakeEngine
    ids=[s['id'] for _,s in window.rows]
    run_job(window.project,FakeEngine(fail=True),ids[:1],{},threading.Event())
    requested=[];monkeypatch.setattr(window,'start_job',lambda ids,settings=None:requested.extend(ids))
    window.generate()
    assert requested==ids[1:]
    window.refresh()
    assert '[3 / 1 / 0]' in window.tree.topLevelItem(0).text(0)


def test_import_offers_and_creates_nested_headings(window,monkeypatch,tmp_path):
    from PySide6.QtWidgets import QFileDialog,QMessageBox
    p=window.project;p.remove_segments([s['id'] for _,s in window.rows]);window.refresh()
    path=tmp_path/'book.md';path.write_text('# Часть\nТекст.\n## Глава\nЕщё текст.',encoding='utf-8')
    monkeypatch.setattr(QFileDialog,'getOpenFileName',lambda *a,**k:(str(path),''))
    monkeypatch.setattr(window,'import_analyzed',lambda count:window.importer.choose(True))
    monkeypatch.setattr(QMessageBox,'question',lambda *a,**k:QMessageBox.No)
    window.import_document()
    for _ in range(200):
        QTest.qWait(10)
        if not window.import_active():break
    nodes=list(p.descendants(p.data['root']))
    assert len(nodes)==3 and nodes[2]['parent']==nodes[1]['id']
    assert 'Ещё текст.' in p.rows(nodes[2]['id'])[0][1]['text']


def test_generate_then_export_chains_without_confirmation(window,monkeypatch,tmp_path,app):
    from PySide6.QtWidgets import QMessageBox
    from test_project import FakeEngine
    window.engine=FakeEngine(fail=True)
    class Message(QMessageBox):
        def exec(self):return 0
        def clickedButton(self):
            return next(b for b in self.buttons() if b.text()=='Сгенерировать и экспортировать')
    monkeypatch.setattr('main.QMessageBox',Message)
    monkeypatch.setattr(window,'export_options',lambda target:{'text':False})
    target=tmp_path/'auto.wav'
    window.request_export([window.project.node(window.current_node)],str(target))
    for _ in range(200):
        app.processEvents();QTest.qWait(20)
        if target.exists() and not window.busy():break
    assert target.exists() and target.stat().st_size>44
    assert all(s['status']=='review' for _,s in window.rows)
    assert window.pending_export is None


def test_unrelated_completed_job_does_not_export_missing_pending_scope(window,monkeypatch,tmp_path):
    pending=([window.project.node(window.current_node)],str(tmp_path/'pending.wav'),False,None,{})
    window.pending_export=pending
    window.project.data['job']={'status':'completed','error':''}
    exported=[]
    monkeypatch.setattr(window,'begin_export',lambda *args:exported.append(args))
    window.generation_finished()
    assert not exported and window.pending_export is pending
