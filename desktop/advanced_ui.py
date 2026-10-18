"""Project AI controls, editable cast, downloads and local API controls."""
import copy
import json
import shutil
import threading
import uuid
from pathlib import Path
from PySide6.QtCore import QThread,Signal,QTimer,Qt
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,QComboBox,QCheckBox,
    QPushButton,QTableWidget,QTableWidgetItem,QFileDialog,QMessageBox,QTabWidget,QWidget,QLineEdit,
    QPlainTextEdit,QSpinBox,QDoubleSpinBox,QDialogButtonBox)
from model_manager import CATALOG,ModelManager,INFERENCE_LOCK
from language_ai import LanguageModel,analyze_project


class AnalysisWorker(QThread):
    message=Signal(str)
    changed=Signal()
    failure=Signal(str)
    def __init__(self,project,engine,cfg):
        super().__init__();self.project=project;self.engine=engine;self.cfg=cfg;self.stop=threading.Event();self.done=0;self.total=len(project.rows(project.data['root']))
    def run(self):
        try:
            while not self.stop.is_set():
                if INFERENCE_LOCK.acquire(timeout=.2):break
            else:return
            try:
                self.engine.unload();llm=LanguageModel(self.engine.models)
                def changed():self.done+=1;self.changed.emit()
                try:
                    analyze_project(self.project,llm,self.cfg,self.stop,self.message.emit,changed)
                    self.message.emit(f"{'Анализ остановлен' if self.stop.is_set() else 'Анализ завершён'}: {self.done}/{self.total}. Голосовых ролей: {len(self.project.data.get('characters',{}))}. Результаты — в настройках ИИ.")
                finally:llm.unload()
            finally:INFERENCE_LOCK.release()
        except Exception as exc:self.failure.emit(str(exc))


def project_dialog(window):
    window.require_project();window.flush_editor()
    project=window.project;cfg=project.data['settings'];dialog=QDialog(window);dialog.setWindowTitle('Режим проекта · ИИ и персонажи');dialog.resize(950,670)
    layout=QVBoxLayout(dialog);form=QFormLayout();layout.addLayout(form)
    mode=QComboBox();mode.addItem('Простой — обычная озвучка','simple');mode.addItem('Расширенный — коррекция и персонажи','advanced');mode.setCurrentIndex(max(0,mode.findData(cfg.get('mode','simple'))));form.addRow('Режим проекта',mode)
    model=QComboBox()
    manager=window.model_manager
    for item in manager.list():
        if item['kind']=='text':model.addItem(item['name']+(' · установлен' if item['installed'] else ' · загрузите в каталоге'),item['id'])
    model.setCurrentIndex(max(0,model.findData(cfg.get('llm_model','qwen2.5-3b'))));form.addRow('Языковая модель',model)
    correct=QCheckBox('Исправлять грамматику и смысловые ошибки перед озвучкой');correct.setChecked(cfg.get('correct_text',False));form.addRow(correct)
    dialogue=QCheckBox('Распределять реплики персонажей и рассказчика по голосам');dialogue.setChecked(cfg.get('dialogue',False));form.addRow(dialogue)
    default_engine=QComboBox()
    for item in manager.list():
        if item['kind']=='tts':default_engine.addItem(item['name'],item['id'])
    default_engine.setCurrentIndex(max(0,default_engine.findData(cfg.get('engine','piper'))));form.addRow('Голос по умолчанию / чтец',default_engine)
    default_reference=QLineEdit(cfg.get('reference',''));refrow=QHBoxLayout();refrow.addWidget(default_reference)
    choose=QPushButton('Образец…');refrow.addWidget(choose)
    def choose_default():
        path,_=QFileDialog.getOpenFileName(dialog,'Образец голоса','','Аудио (*.wav *.flac *.mp3)')
        if path:default_reference.setText(path);default_engine.setCurrentIndex(default_engine.findData('higgs'))
    choose.clicked.connect(choose_default);form.addRow('Образец голоса по умолчанию',refrow)
    default_transcript=QLineEdit(cfg.get('reference_text',''));form.addRow('Текст образца',default_transcript)
    temperature=QDoubleSpinBox();temperature.setRange(.05,2);temperature.setValue(cfg.get('temperature',.7));form.addRow('Температура Higgs',temperature)
    tabs=QTabWidget();layout.addWidget(tabs)
    voice_page=QWidget();vl=QVBoxLayout(voice_page);tabs.addTab(voice_page,'Голоса')
    vl.addWidget(QLabel('ИИ выбирает голос по описанию. Для клонирования выберите Higgs и образец.'))
    table=QTableWidget(0,5);table.setHorizontalHeaderLabels(['ID','Описание голоса','Модель','Образец','Текст образца']);table.horizontalHeader().setStretchLastSection(True);vl.addWidget(table)
    for column,width in enumerate([85,215,225,140]):table.setColumnWidth(column,width)
    def add_voice(v=None):
        v=v or dict(id='voice-'+uuid.uuid4().hex[:6],engine='piper');i=table.rowCount();table.insertRow(i)
        for col,key in [(0,'id'),(1,'description'),(3,'reference'),(4,'reference_text')]:table.setItem(i,col,QTableWidgetItem(v.get(key,'')))
        combo=QComboBox()
        for item in manager.list():
            if item['kind']=='tts':combo.addItem(item['name'],item['id'])
        combo.setCurrentIndex(max(0,combo.findData(v.get('engine','piper'))));table.setCellWidget(i,2,combo)
    for voice in cfg.get('voices',[]):add_voice(voice)
    buttons=QHBoxLayout();vl.addLayout(buttons)
    def button(box,title,fn):
        b=QPushButton(title);b.clicked.connect(lambda:window.guard(fn));box.addWidget(b);return b
    button(buttons,'Добавить голос',add_voice)
    button(buttons,'Удалить голос',lambda:table.removeRow(table.currentRow()) if table.currentRow()>=0 else None)
    def sample():
        i=table.currentRow()
        if i<0:raise ValueError('Выберите строку голоса')
        path,_=QFileDialog.getOpenFileName(dialog,'Образец голоса','','Аудио (*.wav *.flac *.mp3)')
        if path:table.item(i,3).setText(path);table.cellWidget(i,2).setCurrentIndex(table.cellWidget(i,2).findData('higgs'))
    button(buttons,'Выбрать образец…',sample)
    cast_page=QWidget();cl=QVBoxLayout(cast_page);tabs.addTab(cast_page,'Персонажи')
    characters=copy.deepcopy(project.data.get('characters',{}));cl.addWidget(QLabel(f'Обнаружено персонажей и рассказчиков: {len(characters)}. Для уникального звучания нужно голосов: {len(characters)}.'))
    cast=QTableWidget(len(characters),3);cast.setHorizontalHeaderLabels(['Персонаж','Описание','ID назначенного голоса']);cast.horizontalHeader().setStretchLastSection(True);cl.addWidget(cast)
    cast.setColumnWidth(0,200);cast.setColumnWidth(1,350)
    for i,(name,data) in enumerate(characters.items()):
        for col,value in enumerate([name,data.get('description',''),data.get('voice') or '']):cast.setItem(i,col,QTableWidgetItem(value))
        cast.item(i,0).setFlags(cast.item(i,0).flags() & ~Qt.ItemIsEditable)
    preview=QPlainTextEdit();preview.setReadOnly(True)
    preview.setPlainText('\n\n'.join(f"Исходный: {s['text']}\nДля озвучки: {s.get('ai',{}).get('text','—')}\n"+'\n'.join(t['speaker']+': '+t['text'] for t in s.get('ai',{}).get('turns',[])) for _,s in list(project.rows(window.current_node or project.data['root']))[:100]))
    tabs.addTab(preview,'Результаты ИИ (первые 100 фрагментов)')
    selected=project.find_segment(window.editor_id)[1] if window.editor_id else None
    turn_table=None
    if selected and selected.get('ai'):
        page=QWidget();pl=QVBoxLayout(page);tabs.addTab(page,'Редактировать реплики')
        note=QLabel('Реплики выбранного фрагмента. Изменения применятся при перегенерации в расширенном режиме. Исходный текст сохраняется.');note.setWordWrap(True);pl.addWidget(note)
        turns=selected['ai']['turns'];turn_table=QTableWidget(len(turns),2);turn_table.setHorizontalHeaderLabels(['Говорящий','Текст для озвучки']);turn_table.horizontalHeader().setStretchLastSection(True);pl.addWidget(turn_table)
        for i,turn in enumerate(turns):
            turn_table.setItem(i,0,QTableWidgetItem(turn['speaker']));turn_table.setItem(i,1,QTableWidgetItem(turn['text']))
    def save():
        voices=[];ids=set()
        for i in range(table.rowCount()):
            voice={key:table.item(i,col).text().strip() for col,key in [(0,'id'),(1,'description'),(3,'reference'),(4,'reference_text')]}
            voice['engine']=table.cellWidget(i,2).currentData()
            if not voice['id'] or voice['id'] in ids:raise ValueError('ID голосов должны быть непустыми и уникальными')
            ids.add(voice['id'])
            if voice['reference']:
                src=Path(voice['reference']);src=src if src.is_absolute() else project.path(voice['reference'])
                if not src.is_file():raise ValueError('Образец не найден: '+str(src))
                dest=project.path('voices/'+uuid.uuid4().hex+src.suffix.lower());dest.parent.mkdir(exist_ok=True)
                if src.is_relative_to(project.folder):voice['reference']=src.relative_to(project.folder).as_posix()
                else:shutil.copy2(src,dest);voice['reference']=dest.relative_to(project.folder).as_posix()
            voices.append(voice)
        cast_data={}
        for i in range(cast.rowCount()):
            name,description,voice=[cast.item(i,c).text().strip() for c in range(3)]
            if voice and voice not in ids:raise ValueError('Неизвестный ID голоса для '+name)
            cast_data[name]=dict(description=description,voice=voice or None)
        updated=dict(cfg,mode=mode.currentData(),llm_model=model.currentData(),correct_text=correct.isChecked(),dialogue=dialogue.isChecked(),voices=voices)
        if turn_table is not None:
            turns=[]
            for i in range(turn_table.rowCount()):
                speaker=turn_table.item(i,0).text().strip();text=turn_table.item(i,1).text()
                if speaker not in cast_data:raise ValueError('Неизвестный персонаж: '+speaker)
                if not text.strip():raise ValueError('Реплика не может быть пустой')
                turns.append(dict(speaker=speaker,text=text))
            if turns!=selected['ai']['turns']:
                from language_ai import signature
                selected['ai'].update(turns=turns,text=''.join(t['text'] for t in turns),signature=signature(selected['text'],updated),manual=True)
        ref=default_reference.text().strip()
        if ref:
            src=Path(ref) if Path(ref).is_absolute() else project.path(ref)
            if not src.is_file():raise ValueError('Образец не найден')
            if not src.resolve().is_relative_to(project.folder.resolve()):
                dst=project.path('voices/'+uuid.uuid4().hex+src.suffix.lower());dst.parent.mkdir(exist_ok=True);shutil.copy2(src,dst);src=dst
            ref=src.relative_to(project.folder).as_posix()
        updated.update(engine=default_engine.currentData(),reference=ref,reference_text=default_transcript.text(),temperature=temperature.value())
        with project.lock:cfg.update(updated);project.data['characters']=cast_data;project.save()
        window.refresh()
        window.settings_notice.setText('Голос по умолчанию и параметры применятся со следующего фрагмента.');window.settings_notice.show();QTimer.singleShot(15000,window.settings_notice.hide)
    def analyze():
        window.check_idle();window.check_import_idle();save()
        if cfg['mode']!='advanced':raise ValueError('Выберите расширенный режим')
        worker=AnalysisWorker(project,window.engine,copy.deepcopy(cfg));window.exporter=worker
        worker.message.connect(window.message.setText);worker.changed.connect(window.refresh);worker.failure.connect(window.message.setText)
        worker.finished.connect(window.refresh);worker.start();dialog.accept();window.refresh()
    bottom=QHBoxLayout();layout.addLayout(bottom)
    button(bottom,'Свойства персонажей',lambda:window.section_properties())
    button(bottom,'Анализировать весь проект / продолжить',analyze)
    button(bottom,'Сохранить',lambda:(save(),dialog.accept()))
    button(bottom,'Закрыть',dialog.reject)
    dialog.exec()


def service_dialog(window):
    dialog=QDialog(window);dialog.setWindowTitle('Модели и локальный API');dialog.resize(800,600);layout=QVBoxLayout(dialog)
    combo=QComboBox()
    for item in window.model_manager.list():combo.addItem(item['name'],item['id'])
    layout.addWidget(combo);status=QPlainTextEdit();status.setReadOnly(True);layout.addWidget(status)
    download=QPushButton('Скачать выбранную модель');layout.addWidget(download)
    download.clicked.connect(lambda:window.guard(lambda:window.model_manager.download(combo.currentData())))
    api_label=QLabel();api_label.setWordWrap(True);layout.addWidget(api_label)
    row=QHBoxLayout();layout.addLayout(row);port=QSpinBox();port.setRange(1024,65535);port.setValue(8765);row.addWidget(port)
    start=QPushButton('Запустить API');row.addWidget(start);stop=QPushButton('Остановить API');row.addWidget(stop)
    cancel=QPushButton('Остановить задания API');layout.addWidget(cancel)
    def cancel_jobs():
        if window.api_service:
            for event in window.api_service.stops.values():event.set()
    cancel.clicked.connect(cancel_jobs)
    token=QLineEdit();token.setReadOnly(True);token.setEchoMode(QLineEdit.Password);layout.addWidget(token)
    reveal=QCheckBox('Показать токен доступа');reveal.toggled.connect(lambda checked:token.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password));layout.addWidget(reveal)
    def begin():
        from local_api import APIService
        if window.api_service is None:window.api_service=APIService(window.engine.models.parent/'api-data',window.engine.models)
        if window.api_service.server is None:window.api_service.start(port.value())
        update()
    def end():
        if window.api_service:
            if window.api_service.active():raise ValueError('Сначала остановите задания API и дождитесь сохранения')
            window.api_service.close()
        update()
    start.clicked.connect(lambda:window.guard(begin));stop.clicked.connect(lambda:window.guard(end))
    def update():
        lines=[item['name']+(' — установлен' if item['installed'] else ' — не установлен') for item in window.model_manager.list()]
        for job in list(window.model_manager.jobs.values()):lines.append(f"{job['model']}: {job['status']} · {job['bytes']/1024**2:.1f} МиБ · {job['file']} {job['error']}")
        status.setPlainText('\n'.join(lines))
        service=window.api_service
        api_label.setText('API выключен. Документация и примеры: API_RU.md в папке программы.' if not service or not service.server else f'API: http://127.0.0.1:{service.server.server_address[1]}/v1 · заданий: {len(service.jobs)}. Токен сохранён в api-data/api-token.txt')
        token.setText(service.token if service else '')
    timer=QTimer(dialog);timer.timeout.connect(update);timer.start(750);update();dialog.exec()
