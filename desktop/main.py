from __future__ import annotations

import copy
import json
import math
import os
import re
import shutil
import sys
import threading
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QUrl, QSettings, QLockFile, QItemSelectionModel
from PySide6.QtGui import QColor, QPainter, QPen, QDesktopServices, QIcon
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QTreeWidget, QTreeWidgetItem, QTableWidget, QTableWidgetItem, QAbstractItemView, QHeaderView,
    QPushButton, QLabel, QPlainTextEdit, QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox,
    QCheckBox, QProgressBar, QFileDialog, QInputDialog, QMessageBox, QDialog, QFormLayout,
    QDialogButtonBox, QScrollArea, QSlider, QMenu)

from project import Project, VERSION
from text_processing import DEFAULT_PROCESSING, clean_text
from engine import SpeechEngine, run_job, convert

BASE = Path(__file__).resolve().parent.parent
MODELS = BASE / "models" if (BASE / "models").exists() else BASE / "release" / VERSION / "models"
STATUS = dict(pending="Ожидает", generating="Генерация", verifying="Проверка", ready="Проверено",
              review="Нужна проверка", error="Ошибка")
COLORS = dict(pending="#98a8c0", generating="#6585ff", verifying="#ab7bff", ready="#3ccca3",
              review="#e9ae52", error="#e77983")


def clock(seconds):
    return f"{int(seconds)//60:02d}:{int(seconds)%60:02d}"


class Worker(QThread):
    changed = Signal()
    message = Signal(str)

    def __init__(self, project, engine, ids, settings):
        super().__init__()
        self.project, self.engine, self.ids, self.settings = project, engine, ids, settings
        self.stop = threading.Event()

    def run(self):
        try:
            def next_settings():
                with self.project.lock:settings=copy.deepcopy(self.project.data['settings'])
                if settings.get('reference'):settings['reference']=str(self.project.path(settings['reference']))
                return settings
            run_job(self.project, self.engine, self.ids, self.settings, self.stop, self.message.emit, self.changed.emit,next_settings)
        except Exception:
            self.message.emit(traceback.format_exc())


class ExportWorker(QThread):
    result = Signal(str)
    failure = Signal(str)

    def __init__(self, project, nodes, target, separate=False, segment_ids=None, metadata=None):
        super().__init__()
        with project.lock:snapshot=Project(project.folder,copy.deepcopy(project.data))
        self.project, self.nodes, self.target, self.separate = snapshot, copy.deepcopy(nodes), Path(target), separate
        self.segment_ids,self.metadata=segment_ids,metadata or {}

    def run(self):
        try:
            import tempfile
            from audio_metadata import embed_metadata
            written=0
            export_nodes=self.nodes if self.separate else self.nodes[:1]
            combined_ids={s['id'] for _,s in self.project.rows_for_nodes(n['id'] for n in self.nodes)
                          if self.segment_ids is None or s['id'] in self.segment_ids}
            for index, node in enumerate(export_nodes):
                title = self.project.export_name(node['id'])
                dest = self.target / f"{index+1:03d}_{title}.wav" if self.separate else self.target
                source_id=node['id'] if self.separate else self.project.data['root']
                rows=[s for _,s in self.project.rows(source_id) if self.project.has_audio(s) and not s.get('muted') and s['id'] in combined_ids]
                if not rows:continue
                dest.parent.mkdir(parents=True,exist_ok=True)
                with tempfile.TemporaryDirectory(prefix='.reader-export-',dir=dest.parent) as tmp:
                    wav=Path(tmp)/'audio.wav'
                    self.project.export(source_id,wav,self.project.data['settings']['pause_ms'],segment_ids=combined_ids)
                    result=wav
                    if dest.suffix.lower()!='.wav':
                        result=Path(tmp)/('audio'+dest.suffix.lower());convert(wav,result)
                    text='\n\n'.join(s.get('spoken_text',s['text']) for s in rows) if self.metadata.get('text') else ''
                    text=clean_text(text,self.metadata.get('processing',{}))
                    embed_metadata(result,node['title'],text,self.metadata.get('cover'))
                    if result.stat().st_size==0:raise ValueError('Экспорт создал пустой файл')
                    os.replace(result,dest)
                    written+=1
            if not written:raise ValueError('Нет сгенерированного аудио для экспорта. Файл не создан.')
            self.result.emit(str(self.target))
        except Exception as exc:
            self.failure.emit(str(exc))


class ArchiveWorker(QThread):
    result=Signal(str)
    failure=Signal(str)
    def __init__(self,project,node_id,path):
        super().__init__();self.project,self.node_id,self.path=project,node_id,path
    def run(self):
        try:self.project.export_zip(self.node_id,self.path);self.result.emit(str(self.path))
        except Exception as exc:self.failure.emit(str(exc))


class AudioEditWorker(QThread):
    failure=Signal(str)
    def __init__(self,project,seg_id,action,options):
        super().__init__();self.project,self.seg_id,self.action,self.options=project,seg_id,action,options
    def run(self):
        try:
            from audio_edit import edit_segment
            edit_segment(self.project,self.seg_id,self.action,**self.options)
            self.project.update_preview(self.project.data['settings']['pause_ms'])
        except Exception as exc:self.failure.emit(str(exc))


def display_duration(seg):
    return seg.get('source_frames',round(seg.get('duration',0)*24000))/24000


class Timeline(QWidget):
    clicked = Signal(str, object, float)
    audio_context = Signal(object)
    zoom_requested = Signal(int, object)
    pause_moved = Signal(str,str,int)

    def __init__(self):
        super().__init__()
        self.rows, self.selected, self.rects = [], set(), []
        self.playing, self.position = None, 0
        self.scale = 11
        self.edit_mode=False;self.part_selection=set();self.edit_segment_id=None
        self.audio_range=None;self.drag_anchor=None;self.part_anchor=None
        self.pause_drag=None;self.pause_preview=None;self.wheel_remainder=0
        self.setToolTip('Ctrl + колёсико — масштаб. Alt + перетаскивание отключённой паузы — смещение. Правая кнопка — монтаж.')
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.audio_context.emit)
        self.pause_ms = 160
        self.time_offset = 0
        self.source_time_offset=0
        self.setMinimumHeight(135)

    def set_rows(self, rows, selected):
        self.rows, self.selected = rows, set(selected)
        width = sum(self.clip_width(s) + 4 for _, s in rows)
        self.setMinimumWidth(max(700, int(width) + 30))
        self.update()

    def clip_width(self,seg):
        return int(max(105,display_duration(seg)*11)*self.scale/11)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#111925'))
        self.rects = []
        x, elapsed = 12, self.time_offset
        source_elapsed=self.source_time_offset;last_label_right=-100
        for _, s in self.rows:
            width = self.clip_width(s)
            painter.setPen(QColor('#9baabe'))
            duration=display_duration(s)
            pixels_per_second=width/max(.01,duration)
            steps=[.1,.2,.5,1,2,5,10,15,30,60,120,300,600,1800,3600]
            step=next((v for v in steps if v*pixels_per_second>=75),3600)
            painter.drawLine(x,24,x+width,24)
            tick=math.ceil(source_elapsed/step)*step
            while tick<=source_elapsed+duration and duration>0:
                px=x+int((tick-source_elapsed)*pixels_per_second)
                painter.drawLine(px,19,px,25)
                label=clock(tick)+(f'.{round(tick*10)%10}' if step<1 else '')
                label_width=painter.fontMetrics().horizontalAdvance(label)
                if px>last_label_right+5:
                    painter.drawText(px+2,15,label);last_label_right=px+label_width+2
                tick=round(tick+step,6)
            if not duration and x>last_label_right+5:
                label=clock(source_elapsed);painter.drawText(x,15,label);last_label_right=x+painter.fontMetrics().horizontalAdvance(label)
            color = QColor(COLORS.get(s['status'], '#394454'))
            painter.setBrush(color.darker(240))
            painter.setPen(QPen(QColor('#ffffff') if s['id'] in self.selected else color, 2 if s['id'] in self.selected else 1))
            painter.drawRoundedRect(x, 27, width, 90, 5, 5)
            peaks = s.get('source_peaks') or s.get('peaks', [])
            painter.setPen(color)
            for i, peak in enumerate(peaks):
                px = x + 6 + int(i * (width - 12) / max(1, len(peaks)))
                h = max(1, int(peak * 27))
                painter.drawLine(px, 60 - h, px, 60 + h)
            frames=max(1,s.get('source_frames',round(s.get('duration',0)*24000)))
            if s.get('audio_parts') is not None:
                cursor=0
                for part in s['audio_parts']+[dict(start=frames,end=frames)]:
                    if part['start']>cursor:
                        left=x+width*cursor/frames;gap=width*(part['start']-cursor)/frames
                        painter.fillRect(int(left),28,max(1,int(gap)),64,QColor('#111925'))
                    cursor=part['end']
            for part in s.get('audio_parts') or []:
                left=x+width*part['start']/frames;part_width=width*(part['end']-part['start'])/frames
                if not part['enabled']:
                    painter.fillRect(int(left),28,max(1,int(part_width)),64,QColor(35,39,45,220))
                    painter.setPen(QColor('#82909f'))
                    label=painter.fontMetrics().elidedText('Пауза' if part['kind']=='pause' else 'Выкл.',Qt.ElideRight,max(1,int(part_width)-4))
                    painter.drawText(int(left)+2,78,label)
                painter.setPen(QPen(QColor('#62d6b1') if s['id']==self.edit_segment_id and part['id'] in self.part_selection else QColor('#526075'),2))
                painter.setBrush(Qt.NoBrush);painter.drawRect(int(left),28,max(1,int(part_width)),64)
            if self.audio_range and self.audio_range[0]==s['id']:
                _,a,b=self.audio_range
                painter.fillRect(int(x+width*a/frames),28,max(1,int(width*(b-a)/frames)),64,QColor(65,135,230,100))
            if self.pause_preview and self.pause_preview[0]==s['id']:
                _,a,b=self.pause_preview
                painter.setPen(QPen(QColor('#ffce73'),2));painter.setBrush(Qt.NoBrush)
                painter.drawRect(int(x+width*a/frames),28,max(1,int(width*(b-a)/frames)),64)
            painter.setPen(QColor('#e1e8f3'))
            label = painter.fontMetrics().elidedText(s['text'].strip().replace('\n', ' '), Qt.ElideRight, width-12)
            painter.drawText(x+6, 103, label)
            if s['id'] == self.playing:
                from audio_edit import output_to_source
                fraction=output_to_source(s,self.position)/frames if s.get('audio_parts') is not None else self.position/max(.01,s.get('duration',0))
                px=x+int(width*min(1,fraction))
                painter.setPen(QPen(QColor('#ffffff'), 2)); painter.drawLine(px, 23, px, 121)
            self.rects.append((x, width, s))
            x += width + 4
            elapsed += s.get('duration', 0) + (self.pause_ms/1000 if s.get('audio') else 0)
            source_elapsed+=duration+(self.pause_ms/1000 if s.get('audio') else 0)
        if not self.rows:
            painter.setPen(QColor('#8b9bb3')); painter.drawText(24, 65, 'Здесь появятся фрагменты текста и волны аудио')

    def hit(self,point):
        for x,width,seg in self.rects:
            if x<=point.x()<=x+width:
                frames=seg.get('source_frames',round(seg.get('duration',0)*24000))
                frame=int((point.x()-x)/width*frames)
                parts=seg.get('audio_parts')
                if parts is None:parts=[dict(id='whole',start=0,end=frames,enabled=True)]
                part=next((p for p in parts if p['start']<=frame<=p['end']),None)
                return seg,part,frame
        return None

    def mouseMoveEvent(self,event):
        if self.pause_drag and event.buttons() & Qt.LeftButton:
            from audio_edit import pause_move_bounds
            sid,pid,anchor,start,end=self.pause_drag
            rect=next(((x,w,s) for x,w,s in self.rects if s['id']==sid),None)
            if rect:
                x,w,s=rect;frames=s['source_frames'];at=round((event.position().x()-x)/w*frames)
                low,high=pause_move_bounds(s['audio_parts'],pid);delta=max(low,min(high,at-anchor))
                self.pause_preview=(sid,start+delta,end+delta);self.update()
            return
        if self.edit_mode and self.drag_anchor and event.buttons() & Qt.LeftButton:
            sid,start,mouse_x=self.drag_anchor
            rect=next(((x,w,s) for x,w,s in self.rects if s['id']==sid),None)
            if rect and abs(event.position().x()-mouse_x)>3:
                x,w,s=rect;frames=s.get('source_frames',round(s.get('duration',0)*24000))
                end=round(max(0,min(1,(event.position().x()-x)/w))*frames)
                self.audio_range=(sid,min(start,end),max(start,end));self.update()
        elif event.buttons() & Qt.LeftButton and not event.modifiers() & (Qt.ControlModifier|Qt.ShiftModifier):
            self.mousePressEvent(event)

    def mouseReleaseEvent(self,event):
        if self.pause_drag and self.pause_preview:
            sid,pid,anchor,start,end=self.pause_drag;delta=self.pause_preview[1]-start
            self.pause_drag=None;self.pause_preview=None
            if delta:self.pause_moved.emit(sid,pid,delta)
            self.update()
        self.drag_anchor=None

    def wheelEvent(self,event):
        if event.modifiers() & Qt.ControlModifier:
            self.wheel_remainder+=event.angleDelta().y() or event.pixelDelta().y()*8
            steps=int(self.wheel_remainder/120)
            if steps:
                self.wheel_remainder-=steps*120;self.zoom_requested.emit(steps,event.position())
            event.accept()
        else:super().wheelEvent(event)

    def mousePressEvent(self,event):
        if event.button()!=Qt.LeftButton and not event.buttons() & Qt.LeftButton:return
        hit=self.hit(event.position())
        if not hit:return
        seg,part,frame=hit
        if event.modifiers() & Qt.AltModifier and part and not part.get('enabled',True) and part.get('kind')=='pause':
            self.pause_drag=(seg['id'],part['id'],frame,part['start'],part['end'])
            self.pause_preview=(seg['id'],part['start'],part['end']);self.drag_anchor=None
            self.edit_segment_id=seg['id'];self.part_selection={part['id']};self.update();return
        if self.edit_mode and seg.get('audio'):
            if self.edit_segment_id!=seg['id']:
                self.part_selection.clear();self.part_anchor=None
            self.edit_segment_id=seg['id'];self.audio_range=None
            if part:
                if event.modifiers() & Qt.ControlModifier:
                    if part['id'] in self.part_selection:self.part_selection.remove(part['id'])
                    else:self.part_selection.add(part['id'])
                elif event.modifiers() & Qt.ShiftModifier and self.part_anchor:
                    ids=[p['id'] for p in seg.get('audio_parts',[])];a=ids.index(self.part_anchor) if self.part_anchor in ids else 0;b=ids.index(part['id'])
                    self.part_selection.update(ids[min(a,b):max(a,b)+1])
                else:
                    self.part_selection={part['id']};self.part_anchor=part['id'];self.drag_anchor=(seg['id'],frame,event.position().x())
            self.update();return
        for x,width,s in self.rects:
            if s['id']==seg['id']:
                self.clicked.emit(seg['id'],event.modifiers(),(event.position().x()-x)/width);break


class MainWindow(QMainWindow):
    def __init__(self, preferences=None):
        super().__init__()
        self.project = None
        self.project_lock = None
        self.worker = None
        self.exporter = None
        self.pending_export = None
        self.importer = None
        self.import_running = False
        self.import_question = None
        self.import_dirty = False
        self.page = 0
        self.offpage_selection = set()
        self.tree_items = {}
        self.count_cache = {}
        self.engine = SpeechEngine(MODELS)
        self.current_node = None
        self.rows = []
        self.editor_id = None
        self.playlist, self.play_index = [], -1
        self.play_generation = 0
        self.play_offsets = []
        self.play_total = 0
        self.play_paused = False
        self.pending_seek = None
        self.switching_audio = False
        self.next_audio_timer=QTimer(self);self.next_audio_timer.setSingleShot(True)
        self.next_audio_timer.timeout.connect(self.play_next)
        self.setWindowTitle(f'RedReaderStudio {VERSION}')
        self.setWindowIcon(QIcon(str(Path(__file__).with_name('app.ico'))))
        self.resize(1460, 940)
        from model_manager import ModelManager
        self.model_manager=ModelManager(MODELS);self.api_service=None
        self.preferences = preferences if preferences is not None else QSettings('HiggsReader', 'Studio')
        central = QWidget(); self.setCentralWidget(central)
        layout = QVBoxLayout(central); layout.setContentsMargins(20, 16, 20, 16); layout.setSpacing(12)
        heading = QHBoxLayout()
        title = QLabel('HIGGS  /  READER STUDIO'); title.setStyleSheet('font-size:20px;font-weight:700;color:#eef3ff')
        heading.addWidget(title); heading.addStretch()
        badge = QLabel('ЛОКАЛЬНАЯ ОЗВУЧКА  •  '+VERSION); badge.setStyleSheet('color:#8ea3c6')
        heading.addWidget(badge); layout.addLayout(heading)
        bar = QHBoxLayout()
        self.add_button(bar, 'Новый проект', self.new_project)
        self.add_button(bar, 'Открыть…', self.open_project)
        self.add_button(bar, 'Папка проекта', self.show_folder)
        bar.addStretch()
        self.settings_button = self.add_button(bar, 'Настройка', self.settings)
        self.ai_button=self.add_button(bar,'Режим проекта / ИИ',self.ai_settings)
        self.add_button(bar,'Модели / API',self.services)
        self.add_button(bar, 'Справка', self.help)
        layout.addLayout(bar)
        split = QSplitter(Qt.Horizontal); layout.addWidget(split, 1)
        left = QWidget(); ll = QVBoxLayout(left); ll.setContentsMargins(0,0,10,0)
        ll.addWidget(QLabel('ПРОЕКТ И РАЗДЕЛЫ'))
        legend=QLabel('Всего / сгенерировано / подтверждено');legend.setWordWrap(True)
        legend.setStyleSheet('color:#8ea3c6;font-size:11px');ll.addWidget(legend)
        self.tree = QTreeWidget(); self.tree.setHeaderHidden(True); self.tree.currentItemChanged.connect(self.choose_node)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu);self.tree.customContextMenuRequested.connect(self.tree_context_menu)
        ll.addWidget(self.tree)
        self.node_add = self.add_button(ll, '+ Вложенный раздел', self.new_node)
        self.node_rename = self.add_button(ll, 'Переименовать раздел', self.rename_node)
        self.recursive = QCheckBox('Показывать вложенные разделы'); self.recursive.setChecked(True)
        self.recursive.toggled.connect(self.refresh); ll.addWidget(self.recursive)
        split.addWidget(left)
        right = QWidget(); rr = QVBoxLayout(right); rr.setContentsMargins(8,0,0,0)
        self.title = QLabel('Откройте или создайте проект'); self.title.setStyleSheet('font-size:22px;font-weight:600')
        rr.addWidget(self.title)
        self.summary = QLabel('Текст → фрагменты → проверка речи → единая запись'); rr.addWidget(self.summary)
        actions = QHBoxLayout()
        self.import_button = self.add_button(actions, 'Загрузить документ', self.import_document)
        self.paste_button = self.add_button(actions, 'Добавить текст', self.paste_text)
        self.move_button = self.add_button(actions, 'Перенести выбранные…', self.move_segments)
        actions.addStretch(); rr.addLayout(actions)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['№ / Раздел', 'Текст фрагмента', 'Статус', 'Длина', 'Сверка'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setColumnWidth(0,160); self.table.setColumnWidth(2,135); self.table.setColumnWidth(3,70)
        self.table.setColumnWidth(4,85); self.table.verticalHeader().hide()
        self.table.itemSelectionChanged.connect(self.selection_changed)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu);self.table.customContextMenuRequested.connect(self.fragment_context_menu)
        rr.addWidget(self.table, 3)
        pages=QHBoxLayout()
        self.previous_page=self.add_button(pages,'← Назад',lambda:self.change_page(-1))
        self.page_label=QLabel();pages.addWidget(self.page_label);pages.addStretch()
        self.next_page=self.add_button(pages,'Далее →',lambda:self.change_page(1))
        self.clear_selection_button=self.add_button(pages,'Снять выделение',self.clear_fragment_selection)
        rr.addLayout(pages)
        hint = QLabel('Шкала исходного времени · Shift — диапазон · Ctrl — отдельные фрагменты')
        hint.setStyleSheet('color:#8ea3c6'); rr.addWidget(hint)
        self.timeline = Timeline(); self.timeline.clicked.connect(self.timeline_click)
        scroll = QScrollArea(); scroll.setWidget(self.timeline); scroll.setWidgetResizable(True); scroll.setFixedHeight(155)
        self.timeline.audio_context.connect(self.audio_context_menu)
        self.timeline.pause_moved.connect(lambda sid,pid,delta:self.guard(lambda:self.edit_audio(sid,'move_pause',ids=[pid],delta_frames=delta)))
        self.timeline_scroll=scroll
        rr.addWidget(scroll)
        audio_tools=QHBoxLayout();edit_mode=QCheckBox('Выделение аудио мышью');edit_mode.toggled.connect(lambda value:setattr(self.timeline,'edit_mode',value));audio_tools.addWidget(edit_mode)
        audio_hint=QLabel('Ctrl + колесо — масштаб · Alt + пауза — смещение')
        audio_hint.setToolTip('Линейка показывает исходное время, включая отключённые паузы. Ctrl/Shift — выбор частей, протянуть мышью — диапазон, правая кнопка — монтаж.');audio_tools.addWidget(audio_hint)
        audio_tools.addStretch();audio_tools.addWidget(QLabel('Масштаб'))
        zoom=QSpinBox();zoom.setRange(1,20);zoom.setSuffix('×');zoom.setValue(1)
        self.timeline_zoom=zoom
        def change_zoom(value):
            self.timeline.scale=11*value;self.timeline.set_rows(self.rows,self.selected_ids())
        zoom.valueChanged.connect(change_zoom);audio_tools.addWidget(zoom);rr.addLayout(audio_tools)
        self.timeline.zoom_requested.connect(self.zoom_timeline)
        playback = QHBoxLayout()
        self.add_button(playback, '▶ Фрагмент', self.play_one)
        self.add_button(playback, '▶ Весь раздел', self.play_all)
        self.add_button(playback, 'Ⅱ Пауза', self.pause_audio)
        self.add_button(playback, '■ Стоп', self.stop_audio)
        self.seek = QSlider(Qt.Horizontal); self.seek.setRange(0,1000); self.seek.sliderMoved.connect(self.seek_audio)
        self.seek.setToolTip("Перемотка по всему воспроизводимому разделу")
        playback.addWidget(self.seek,1)
        self.time_label = QLabel('00:00 / 00:00'); playback.addWidget(self.time_label)
        rr.addLayout(playback)
        self.spoken = QLabel(''); self.spoken.setStyleSheet('color:#62d6b1'); rr.addWidget(self.spoken)
        editor_split = QSplitter(Qt.Horizontal)
        edit_box = QWidget(); el = QVBoxLayout(edit_box); el.setContentsMargins(0,0,8,0)
        editor_heading=QHBoxLayout();editor_heading.addWidget(QLabel('ИСХОДНЫЙ ТЕКСТ  /  РАЗДЕЛЕНИЕ ПО КУРСОРУ'))
        editor_heading.addStretch();self.character_count=QLabel('0 символов');editor_heading.addWidget(self.character_count)
        el.addLayout(editor_heading)
        self.editor = QPlainTextEdit(); self.editor.setPlaceholderText('Выберите фрагмент в таблице'); el.addWidget(self.editor)
        self.edit_timer=QTimer(self);self.edit_timer.setSingleShot(True);self.edit_timer.setInterval(700)
        self.edit_timer.timeout.connect(lambda:self.guard(self.autosave_text))
        self.editor.textChanged.connect(lambda:self.character_count.setText(f'{len(self.editor.toPlainText())} символов'))
        self.editor.textChanged.connect(lambda:self.edit_timer.start() if not self.busy() else None)
        eb = QHBoxLayout()
        self.save_button = self.add_button(eb, 'Сохранить текст', self.save_text)
        self.split_button = self.add_button(eb, 'Разделить по курсору', self.split_segment)
        self.history_button = self.add_button(eb, 'Версии', self.history)
        el.addLayout(eb); editor_split.addWidget(edit_box)
        check_box = QWidget(); cl = QVBoxLayout(check_box); cl.setContentsMargins(8,0,0,0)
        cl.addWidget(QLabel('РАСПОЗНАНО В АУДИО'))
        self.verification = QPlainTextEdit(); self.verification.setReadOnly(True); cl.addWidget(self.verification)
        self.accept_button = self.add_button(cl, 'Подтвердить после прослушивания', self.accept_segment)
        editor_split.addWidget(check_box); editor_split.setSizes([620,400]); rr.addWidget(editor_split,2)
        split.addWidget(right); split.setSizes([260,1160])
        controls = QHBoxLayout()
        self.generate_button = self.add_button(controls, '▶ Генерировать / продолжить раздел', self.generate)
        self.generate_button.setObjectName('primary')
        self.regen_button = self.add_button(controls, 'Перегенерировать выбранные', self.regenerate)
        self.resume_button = self.add_button(controls, 'Продолжить очередь', self.resume_job)
        self.stop_button = self.add_button(controls, 'Остановить', self.stop_generation)
        controls.addStretch()
        self.export_button = self.add_button(controls, 'Экспорт аудио…', self.export_audio)
        self.export_parts_button = self.add_button(controls, 'Экспорт по разделам…', self.export_parts)
        layout.addLayout(controls)
        self.progress = QProgressBar(); self.progress.setRange(0,100); layout.addWidget(self.progress)
        self.settings_notice=QLabel();self.settings_notice.setWordWrap(True);self.settings_notice.hide();layout.addWidget(self.settings_notice)
        self.message = QLabel('Готово к работе. Модели и зависимости находятся в папке программы.'); self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.import_status=QLabel('Импорт: нет активных задач')
        self.import_progress=QProgressBar();self.import_progress.setFixedWidth(190)
        self.import_progress.setRange(0,100);self.import_progress.setValue(0)
        self.cancel_import_button=QPushButton('Отменить импорт')
        self.cancel_import_button.clicked.connect(self.cancel_import)
        self.cancel_import_button.setEnabled(False)
        self.statusBar().addWidget(self.import_status,1)
        self.export_notice=QLabel();self.statusBar().addPermanentWidget(self.export_notice)
        self.statusBar().addPermanentWidget(self.import_progress)
        self.statusBar().addPermanentWidget(self.cancel_import_button)
        self.import_timer=QTimer(self);self.import_timer.setInterval(600)
        self.import_timer.timeout.connect(self.refresh_import);self.import_timer.start()
        self.audio = QAudioOutput(); self.audio.setVolume(0.8)
        self.player = QMediaPlayer(); self.player.setAudioOutput(self.audio)
        self.player.positionChanged.connect(self.audio_position)
        self.player.mediaStatusChanged.connect(self.audio_status)
        self.player.errorOccurred.connect(lambda *args: self.message.setText(self.player.errorString()))
        self.mutations = [self.node_add,self.node_rename,self.import_button,self.paste_button,self.move_button,
                          self.save_button,self.split_button,self.accept_button,self.history_button,self.settings_button,self.ai_button]
        self.refresh()
        last = self.preferences.value('project', '')
        if last and (Path(last)/'project.json').exists():
            QTimer.singleShot(0, lambda: self.load_project(last))

    def add_button(self, layout, label, handler):
        button = QPushButton(label); button.clicked.connect(lambda: self.guard(handler)); layout.addWidget(button)
        return button

    def guard(self, fn):
        try:
            fn()
        except Exception as exc:
            self.message.setText(str(exc)); QMessageBox.warning(self, 'Не удалось выполнить действие', str(exc))

    def busy(self):
        return bool((self.worker and self.worker.isRunning()) or (self.exporter and self.exporter.isRunning()))

    def require_project(self):
        if not self.project:
            raise ValueError('Сначала создайте или откройте проект')

    def check_idle(self):
        if self.busy():
            raise ValueError('Дождитесь завершения операции или остановите генерацию')

    def check_export_idle(self):
        if self.exporter and self.exporter.isRunning():raise ValueError('Дождитесь текущего экспорта, анализа или редактирования аудио')

    def acquire_project(self, folder):
        lock = QLockFile(str(Path(folder)/'.reader.lock')); lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            raise ValueError('Этот проект уже открыт в другом окне программы')
        return lock

    def new_project(self):
        self.check_import_idle();self.check_idle()
        self.flush_editor()
        title, ok = QInputDialog.getText(self,'Новый проект','Название проекта:')
        if not ok or not title.strip(): return
        parent = BASE/'projects'
        parent.mkdir(parents=True,exist_ok=True)
        name = re.sub(r'[<>:"/\\|?*]', '_', title).strip(' .') or 'Проект'
        folder = Path(parent)/name
        project = Project.create(folder, title)
        from characters import import_voice_library
        import_voice_library(project,BASE/'voices')
        roles=QMessageBox.question(self,'Персонажи','Разбивать текст на роли персонажей и чтеца с помощью ИИ?',QMessageBox.Yes|QMessageBox.No)==QMessageBox.Yes
        project.data['settings'].update(dialogue=roles,mode='advanced' if roles else 'simple');project.save()
        self.set_project(project, self.acquire_project(folder))

    def open_project(self):
        self.check_import_idle();self.check_idle()
        self.flush_editor()
        path, _ = QFileDialog.getOpenFileName(self,'Открыть проект','','Проект (project.json)')
        if path: self.load_project(Path(path).parent)

    def load_project(self, folder):
        try:
            if self.project and self.project.folder.resolve() == Path(folder).resolve(): return
            lock = self.acquire_project(folder)
            try: project = Project.open(folder)
            except Exception:
                lock.unlock(); raise
            self.set_project(project, lock)
        except Exception as exc:
            self.message.setText(str(exc))

    def set_project(self, project, lock):
        self.check_import_idle()
        self.stop_audio()
        if self.project:self.project.close_saves()
        self.page=0
        self.offpage_selection.clear()
        project.enable_background_save()
        if self.project_lock: self.project_lock.unlock()
        self.project, self.project_lock = project, lock
        self.pending_export=None
        self.preferences.setValue('project',str(project.folder.resolve()))
        self.current_node = project.data['root']; self.editor_id = None
        self.rebuild_tree(); self.refresh()
        self.message.setText('Проект сохраняется автоматически: '+str(project.folder))

    def rebuild_tree(self):
        self.count_cache=self.project.all_counts()
        self.tree.blockSignals(True); self.tree.clear(); self.tree_items = {}
        for node in self.project.descendants(self.project.data['root']):self.insert_tree_node(node)
        self.tree.expandAll(); self.tree.setCurrentItem(self.tree_items[self.current_node]); self.tree.blockSignals(False)

    def insert_tree_node(self,node):
        item=QTreeWidgetItem([self.node_label(node)]);item.setData(0,Qt.UserRole,node['id'])
        item.setToolTip(0,'Всего фрагментов / сгенерировано / подтверждено пользователем. Включая вложенные разделы.')
        if node['parent']:self.tree_items[node['parent']].addChild(item)
        else:self.tree.addTopLevelItem(item)
        self.tree_items[node['id']]=item
        return item

    def node_label(self,node):
        total,generated,confirmed=self.count_cache.get(node['id'],(len(node['segments']),0,0))
        return f'{node["title"]}  [{total} / {generated} / {confirmed}]'

    def update_tree_counts(self):
        self.count_cache=self.project.all_counts()
        for node in self.project.data['nodes']:
            item=self.tree_items.get(node['id'])
            if item:item.setText(0,self.node_label(node))

    def change_page(self,delta):
        self.flush_editor();self.editor_id=None
        self.offpage_selection=set(self.selected_ids())
        self.page=max(0,self.page+delta)
        self.refresh()

    def clear_fragment_selection(self):
        self.offpage_selection.clear();self.table.clearSelection();self.selection_changed()

    def choose_node(self, item, previous=None):
        if item:
            self.flush_editor()
            self.offpage_selection.clear()
            self.current_node = item.data(0,Qt.UserRole); self.page=0;self.refresh()

    def new_node(self):
        self.require_project(); self.check_idle()
        title,ok = QInputDialog.getText(self,'Вложенный раздел','Название:')
        if ok and title.strip():
            node = self.project.add_node(self.current_node,title.strip())
            self.current_node = node['id']; self.rebuild_tree(); self.refresh()

    def rename_node(self):
        self.require_project(); self.check_idle()
        node = self.project.node(self.current_node)
        title,ok = QInputDialog.getText(self,'Название раздела','Название:',text=node['title'])
        if ok and title.strip():
            node['title'] = title.strip(); self.project.save(); self.rebuild_tree(); self.refresh()

    def selected_ids(self):
        visible=[self.rows[i.row()][1]['id'] for i in self.table.selectionModel().selectedRows() if i.row()<len(self.rows)]
        page_ids={s['id'] for _,s in self.rows}
        return list(self.offpage_selection-page_ids)+visible

    def refresh(self):
        busy = self.busy()
        for button in self.mutations: button.setEnabled(bool(self.project) and not busy)
        for button in [self.generate_button,self.regen_button,self.resume_button,self.export_button,self.export_parts_button]:
            button.setEnabled(bool(self.project) and not busy)
        self.settings_button.setEnabled(bool(self.project))
        self.ai_button.setEnabled(bool(self.project))
        for button in (self.export_button,self.export_parts_button):button.setEnabled(bool(self.project) and not (self.exporter and self.exporter.isRunning()))
        self.stop_button.setEnabled(bool((self.worker and self.worker.isRunning()) or (self.exporter and self.exporter.isRunning() and hasattr(self.exporter,'stop'))))
        self.editor.setReadOnly(busy)
        self.import_button.setEnabled(bool(self.project) and not self.import_active())
        if not self.project: return
        self.update_tree_counts()
        selected = set(self.selected_ids())
        all_rows = self.project.rows(self.current_node,self.recursive.isChecked())
        self.offpage_selection.intersection_update(s['id'] for _,s in all_rows)
        page_size=300;self.page=min(self.page,max(0,(len(all_rows)-1)//page_size))
        start=self.page*page_size;self.rows=all_rows[start:start+page_size]
        self.page_label.setText(f'Фрагменты {start+1 if all_rows else 0}–{start+len(self.rows)} из {len(all_rows)}')
        self.previous_page.setEnabled(self.page>0);self.next_page.setEnabled(start+page_size<len(all_rows))
        self.title.setText(self.project.node(self.current_node)['title'])
        total = len(all_rows); ready = sum(self.project.has_audio(s) for _,s in all_rows)
        seconds = sum(s.get('duration',0) for _,s in all_rows)
        self.summary.setText(f'{total} фрагментов  ·  {ready} сгенерировано  ·  {clock(seconds)} аудио  ·  '+
                             self.project.data['settings']['engine']+' · '+('Расширенный режим' if self.project.data['settings'].get('mode')=='advanced' else 'Простой режим'))
        self.table.blockSignals(True); self.table.clearSelection();self.table.setRowCount(len(self.rows))
        for row,(node,s) in enumerate(self.rows):
            verification = s.get('verification') or {}
            role=self.project.data.get('cast',{}).get(s.get('speaker_id'),{})
            label=(role.get('name','')+' · '+role.get('gender','неизвестен')+' — ') if role else ''
            values = [f'{start+row+1:03d} / {node["title"]}',label+s['text'].strip().replace('\n',' '),STATUS[s['status']],
                      clock(s.get('duration',0)),f'{verification.get("coverage",0):.0%}' if verification else '—']
            for col,value in enumerate(values):
                item = QTableWidgetItem(value); item.setToolTip(s.get('error') or s['text'])
                if col == 2: item.setForeground(QColor(COLORS[s['status']]))
                self.table.setItem(row,col,item)
            if s['id'] in selected:
                self.table.selectionModel().select(self.table.model().index(row,0),QItemSelectionModel.Select|QItemSelectionModel.Rows)
        self.table.blockSignals(False)
        self.timeline.set_rows(self.rows,selected)
        self.timeline.pause_ms=self.project.data['settings']['pause_ms']
        self.timeline.time_offset=sum(s.get('duration',0)+self.timeline.pause_ms/1000
                                      for _,s in all_rows[:start] if s.get('audio'))
        self.timeline.source_time_offset=sum(display_duration(s)+self.timeline.pause_ms/1000 for _,s in all_rows[:start] if s.get('audio'))
        if self.exporter and self.exporter.isRunning() and not (isinstance(self.exporter,ExportWorker) and self.worker and self.worker.isRunning()):
            if hasattr(self.exporter,'total'):
                self.progress.setRange(0,max(1,self.exporter.total));self.progress.setValue(self.exporter.done);self.progress.setFormat(f'Анализ ИИ: {self.exporter.done} / {self.exporter.total}')
            else:self.progress.setRange(0,0)
            self.selection_changed()
            return
        self.progress.setRange(0,100)
        job = self.project.data.get('job')
        if job and job['status']=='running':
            done,len_ids = len(job['done']),len(job['ids'])
            self.progress.setValue(int(100*done/max(1,len_ids)))
            self.progress.setFormat(f'Очередь: {done} / {len_ids} · %p%')
        else:
            self.progress.setValue(int(100*ready/max(1,total)))
            self.progress.setFormat(f'Сгенерировано: {ready} / {total} · %p%')
        self.selection_changed()

    def selection_changed(self):
        ids = self.selected_ids(); self.timeline.selected=set(ids); self.timeline.update()
        self.clear_selection_button.setText(f'Снять выделение ({len(ids)})')
        visible={s['id'] for _,s in self.rows}
        editor_ids=[sid for sid in ids if sid in visible]
        if not editor_ids:
            self.flush_editor()
            self.editor_id=None; self.editor.clear(); self.verification.clear(); return
        current = self.table.currentRow()
        seg = self.rows[current][1] if 0<=current<len(self.rows) and self.rows[current][1]['id'] in editor_ids else self.project.find_segment(editor_ids[0])[1]
        if seg['id'] != self.editor_id:
            self.flush_editor()
            self.editor_id=seg['id']; self.editor.setPlainText(seg['text'])
        v=seg.get('verification')
        if v:
            result = v.get('transcript','')+'\n\n'
            if v.get('manual'): result+='Подтверждено пользователем после прослушивания.'
            elif v['passed']: result+='Все нормализованные слова совпали.'
            else:
                result+='Не совпало: '+(' '.join(v.get('missing',[])) or '—')
                result+='\nЛишнее: '+(' '.join(v.get('extra',[])) or '—')
            if v.get('audio_edited'):result+='\n\nАудио отредактировано. Исходный текст не менялся; проверьте соответствие записи.'
            if seg.get('spoken_text') and seg['spoken_text']!=seg['text']:result+='\n\nТекст после коррекции ИИ:\n'+seg['spoken_text']
            self.verification.setPlainText(result)
        else: self.verification.setPlainText(seg.get('error') or 'Проверка будет выполнена после генерации.')

    def timeline_click(self, seg_id, modifiers, fraction):
        row = next(i for i,(_,s) in enumerate(self.rows) if s['id']==seg_id)
        model=self.table.selectionModel(); idx=self.table.model().index(row,0)
        if modifiers & Qt.ShiftModifier and self.table.currentRow()>=0:
            anchor=self.table.currentRow()
            for i in range(min(anchor,row),max(anchor,row)+1):
                model.select(self.table.model().index(i,0),QItemSelectionModel.Select|QItemSelectionModel.Rows)
        elif modifiers & Qt.ControlModifier:
            model.select(idx,QItemSelectionModel.Toggle|QItemSelectionModel.Rows)
        else:
            self.table.selectRow(row)
            if self.playlist:
                target=next((i for i,s in enumerate(self.playlist) if s['id']==seg_id),None)
                if target is not None:
                    from audio_edit import source_to_output
                    seg=self.playlist[target]
                    seconds=source_to_output(seg,fraction*seg['source_frames']) if seg.get('audio_parts') is not None else fraction*seg['duration']
                    self.play_at(target,int(seconds*1000))
                else:self.message.setText('У этого фрагмента нет аудио в текущем воспроизводимом разделе.')
        self.table.scrollTo(idx)

    def audio_context_menu(self,position):
        hit=self.timeline.hit(position)
        if not hit:return
        seg,part,frame=hit
        if not seg.get('audio'):return
        if self.timeline.edit_segment_id!=seg['id']:
            self.timeline.edit_segment_id=seg['id'];self.timeline.part_selection.clear();self.timeline.audio_range=None
        if part and part['id'] not in self.timeline.part_selection:self.timeline.part_selection={part['id']}
        ids=list(self.timeline.part_selection);menu=QMenu(self)
        self.timeline.update()
        def add(title,action,enabled=True,**options):
            item=menu.addAction(title);item.setEnabled(enabled and not self.busy())
            item.triggered.connect(lambda checked=False:self.guard(lambda:self.edit_audio(seg['id'],action,**options)))
        add('Отключить/включить фрагмент','toggle',bool(ids),ids=ids)
        add('Удалить фрагмент','delete',bool(ids),ids=ids)
        add('Найти и разделить паузу','detect',threshold_ms=self.project.data['settings'].get('max_pause_ms',1000),replacement_ms=self.project.data['settings'].get('pause_replacement_ms',0),keep_start_ms=self.project.data['settings'].get('pause_keep_start_ms',300),keep_end_ms=self.project.data['settings'].get('pause_keep_end_ms',300),trim_edges=copy.deepcopy(self.project.data['settings']))
        movable=[p for p in seg.get('audio_parts',[]) if p['id'] in ids and not p['enabled'] and p['kind']=='pause']
        if len(movable)==1 and len(ids)==1:
            move=menu.addAction('Сместить паузу…');move.setEnabled(not self.busy())
            move.triggered.connect(lambda checked=False:self.guard(lambda:self.move_pause_dialog(seg,movable[0])))
        selected=self.timeline.audio_range
        if selected and selected[0]==seg['id'] and selected[2]>selected[1]:
            add('Удалить выделенную область','range_delete',start=selected[1],end=selected[2])
        add('Разъединить по курсору','split',start=frame)
        add('Объединить выбранные части','merge',len(ids)>1,ids=ids)
        add('Отменить изменение аудио','undo',bool(seg.get('audio_edit_history')))
        add('Восстановить исходное аудио','restore',bool(seg.get('source_audio')))
        menu.exec(self.timeline.mapToGlobal(position))

    def move_pause_dialog(self,seg,part):
        from audio_edit import pause_move_bounds
        low,high=pause_move_bounds(seg['audio_parts'],part['id'])
        delta,ok=QInputDialog.getInt(self,'Сместить отключённую паузу','Смещение в мс: отрицательное — влево, положительное — вправо',0,math.ceil(low/24),math.floor(high/24),10)
        if ok and delta:self.edit_audio(seg['id'],'move_pause',ids=[part['id']],delta_ms=delta)

    def zoom_timeline(self,steps,position):
        scroll=self.timeline_scroll.horizontalScrollBar();viewport_x=position.x()-scroll.value()
        hit=self.timeline.hit(position)
        value=max(self.timeline_zoom.minimum(),min(self.timeline_zoom.maximum(),self.timeline_zoom.value()+steps))
        if value==self.timeline_zoom.value():return
        self.timeline_zoom.setValue(value)
        if hit:
            target,_,frame=hit;x=12
            for _,seg in self.rows:
                width=self.timeline.clip_width(seg)
                if seg['id']==target['id']:
                    frames=max(1,seg.get('source_frames',round(seg.get('duration',0)*24000)))
                    wanted=round(x+width*frame/frames-viewport_x)
                    QTimer.singleShot(0,lambda:scroll.setValue(wanted));break
                x+=width+4

    def edit_audio(self,seg_id,action,**options):
        self.check_idle();self.stop_audio();self.flush_editor()
        self.timeline.part_selection.clear();self.timeline.audio_range=None;self.timeline.part_anchor=None
        self.exporter=AudioEditWorker(self.project,seg_id,action,options)
        self.exporter.failure.connect(lambda error:self.message.setText('Редактирование аудио: '+error))
        self.exporter.finished.connect(self.refresh);self.exporter.start();self.refresh()

    def import_active(self):
        return self.import_running

    def check_import_idle(self):
        if self.import_active():raise ValueError('Сначала завершите или отмените импорт документа')

    def import_document(self):
        self.require_project();self.check_import_idle();self.flush_editor()
        path,_=QFileDialog.getOpenFileName(self,'Загрузить текст','','Документы (*.txt *.md *.docx *.pdf *.epub *.html *.htm *.rtf)')
        if path:
            roles=QMessageBox.question(self,'Персонажи','Разбить импортируемый текст на роли персонажей и чтеца с помощью ИИ?',QMessageBox.Yes|QMessageBox.No)==QMessageBox.Yes
            with self.project.lock:
                self.project.data['settings']['dialogue']=roles
                if roles:self.project.data['settings']['mode']='advanced'
                self.project.save()
            self.start_import(path)

    def start_import(self,path):
        from document_import import ImportWorker
        self.check_import_idle()
        self.importer=ImportWorker(path,self.current_node,self.project.data['settings']['max_chars'],
                                   not self.project.rows(self.current_node),copy.deepcopy(self.project.data['settings'].get('text_processing',DEFAULT_PROCESSING)),self.project,getattr(self.engine,'models',MODELS))
        self.importer.progress.connect(self.import_progress_changed)
        self.importer.analyzed.connect(self.import_analyzed)
        self.importer.batch.connect(self.apply_import_batch)
        self.importer.completed.connect(self.import_completed)
        self.importer.failure.connect(lambda error:self.import_status.setText('Ошибка импорта: '+error))
        self.importer.finished.connect(self.import_finished)
        self.import_progress.setRange(0,0);self.import_status.setText('Загрузка документа…')
        self.import_running=True
        self.cancel_import_button.setEnabled(True);self.importer.start();self.refresh()

    def import_progress_changed(self,stage,done,total):
        self.import_status.setText(f'{stage}: {done:,} / {total:,}' if total else stage+'…')
        self.import_progress.setRange(0,100 if total else 0)
        if total:self.import_progress.setValue(int(100*done/max(1,total)))

    def import_analyzed(self,count):
        if not self.importer or self.importer.stop.is_set():return
        self.import_status.setText(f'Найдено разделов: {count}. Выберите способ импорта.')
        question=QMessageBox(QMessageBox.Question,'Структура документа',
                            f'Найдено разделов: {count}. Создать вложенные разделы?',
                            QMessageBox.Yes|QMessageBox.No,self)
        question.setWindowModality(Qt.NonModal)
        self.import_question=question
        worker=self.importer
        question.finished.connect(lambda result:worker.choose(result==QMessageBox.Yes)
                                  if result in (QMessageBox.Yes,QMessageBox.No) else worker.cancel())
        question.show()

    def apply_import_batch(self,nodes,additions):
        worker=self.importer
        try:
            if worker.stop.is_set():return
            with self.project.lock:
                self.project.node(worker.target)
                self.project.data['nodes'].extend(nodes)
                for target,segments in additions:self.project.node(target)['segments'].extend(segments)
                self.project.save()
            self.tree.blockSignals(True)
            for node in nodes:
                item=self.insert_tree_node(node)
                if item.parent():item.parent().setExpanded(True)
            self.tree.blockSignals(False)
            self.import_dirty=True
        except Exception as exc:
            worker.cancel();self.import_status.setText('Ошибка добавления раздела: '+str(exc))
        finally:worker.ack.set()

    def refresh_import(self):
        if self.import_dirty and self.project:
            self.import_dirty=False;self.refresh()
        if self.project and self.project._saver and self.project._saver.error:
            self.message.setText('Ошибка автосохранения: '+str(self.project._saver.error))

    def import_completed(self,cancelled,count):
        self.import_status.setText(('Импорт отменён. Добавленные данные сохранены.' if cancelled else
                                   f'Импорт завершён. Создано разделов: {count}.'))
        self.import_progress.setRange(0,100);self.import_progress.setValue(0 if cancelled else 100)

    def import_finished(self):
        self.import_running=False
        self.cancel_import_button.setEnabled(False)
        self.import_progress.setRange(0,100)
        self.import_dirty=False;self.refresh()

    def cancel_import(self):
        if self.import_active():
            self.importer.cancel();self.import_status.setText('Отмена импорта…')
            if self.import_question:self.import_question.close()

    def paste_text(self):
        self.require_project(); self.check_idle()
        dialog=QDialog(self); dialog.setWindowTitle('Добавить текст в текущий раздел'); dialog.resize(780,540)
        box=QVBoxLayout(dialog); edit=QPlainTextEdit(); box.addWidget(edit)
        box.addWidget(QLabel('Текст автоматически разделится по предложениям. Исходные фрагменты останутся.'))
        buttons=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject);box.addWidget(buttons)
        if dialog.exec(): self.append_text(edit.toPlainText())

    def append_text(self,text):
        if not text.strip():return
        from project import uid
        path=self.project.path('texts/pasted-'+uid()+'.txt');path.parent.mkdir(exist_ok=True);path.write_text(text,encoding='utf-8')
        self.start_import(path)

    def save_text(self):
        self.check_idle()
        if self.editor_id:
            self.project.edit(self.editor_id,self.editor.toPlainText()); self.refresh()

    def flush_editor(self):
        self.edit_timer.stop()
        if self.editor_id and self.project and not self.busy():
            try: _,s=self.project.find_segment(self.editor_id)
            except ValueError:return
            text=self.editor.toPlainText()
            if text.strip() and text!=s['text']:self.project.edit(self.editor_id,text)

    def autosave_text(self):
        if self.editor_id and self.editor.toPlainText().strip():
            self.flush_editor();self.refresh()

    def split_segment(self):
        self.check_idle()
        if not self.editor_id:return
        position=self.editor.textCursor().position()
        # Qt positions count UTF-16 units, Python slices count codepoints.
        position=len(self.editor.toPlainText().encode('utf-16-le')[:position*2].decode('utf-16-le'))
        self.project.edit(self.editor_id,self.editor.toPlainText())
        self.project.split(self.editor_id,position); self.editor_id=None; self.refresh()

    def move_segments(self):
        self.check_idle(); self.require_project(); ids=self.selected_ids()
        if not ids:raise ValueError('Выберите фрагменты с помощью Ctrl или Shift')
        nodes=list(self.project.descendants(self.project.data['root']))
        choices=[f'{i+1}. {n["title"]}' for i,n in enumerate(nodes)]
        title,ok=QInputDialog.getItem(self,'Перенести фрагменты','Раздел назначения:',choices,editable=False)
        if ok:self.project.move(ids,nodes[choices.index(title)]['id']); self.refresh()

    def accept_segment(self):
        self.check_idle()
        if not self.editor_id:return
        _,s=self.project.find_segment(self.editor_id)
        if not s.get('audio'):raise ValueError('У фрагмента ещё нет аудио')
        if self.editor.toPlainText()!=s['text']:raise ValueError('Сначала сохраните изменённый текст и озвучьте его')
        s['status']='ready'; s['verification']=dict(s.get('verification') or {},manual=True,passed=True)
        self.project.save(); self.refresh()

    def history(self):
        self.check_idle()
        if not self.editor_id:return
        _,s=self.project.find_segment(self.editor_id)
        versions=s['versions']
        if not versions:raise ValueError('Предыдущих версий пока нет')
        choices=[f'{i+1}. {clock(v.get("duration",0))} · {v["text"][:70]}' for i,v in enumerate(versions)]
        value,ok=QInputDialog.getItem(self,'Восстановить версию','Версия (будут восстановлены текст и аудио):',choices,editable=False)
        if ok:
            version=copy.deepcopy(versions[choices.index(value)]); self.project.archive(s)
            for key in ('source_audio','source_frames','source_verification','source_peaks','audio_parts','audio_edit_history','muted','spoken_text','ai'):s.pop(key,None)
            s.update(version)
            self.project.save(); self.editor_id=None; self.refresh()

    def ai_settings(self):
        from advanced_ui import project_dialog
        project_dialog(self)

    def services(self):
        from advanced_ui import service_dialog
        service_dialog(self)

    def settings(self):
        from settings_ui import settings_dialog
        settings_dialog(self)

    def start_job(self,ids,settings=None):
        self.require_project();self.check_idle()
        if not ids:raise ValueError('Все фрагменты готовы. Для повторной генерации выделите их и нажмите «Перегенерировать».')
        if self.editor_id and self.editor.toPlainText()!=self.project.find_segment(self.editor_id)[1]['text']:
            raise ValueError('Сохраните изменения текста перед генерацией')
        settings=copy.deepcopy(settings or self.project.data['settings'])
        if settings.get('reference'):settings['reference']=str(self.project.path(settings['reference']))
        self.worker=Worker(self.project,self.engine,ids,settings)
        self.worker.changed.connect(self.refresh);self.worker.message.connect(self.message.setText)
        self.worker.finished.connect(self.generation_finished);self.worker.start();self.refresh()

    def generate(self):
        self.require_project()
        self.flush_editor()
        nodes=self.selected_node_ids()
        if len(nodes)==1 and self.project.data['settings'].get('continue_next_sections',True):
            rows=self.project.generation_rows(nodes[0])
        else:rows=self.selected_section_rows()
        self.start_job([s['id'] for _,s in rows if not self.project.has_audio(s)])

    def regenerate(self):
        self.flush_editor()
        ids=self.selected_ids()
        if not ids:raise ValueError('Выберите фрагменты с помощью Ctrl или Shift')
        self.start_job(ids)

    def resume_job(self):
        self.require_project()
        self.flush_editor()
        job=self.project.data.get('job')
        if not job: return self.generate()
        available={s['id']:s for _,s in self.project.rows(self.project.data['root'])}
        ids=[sid for sid in job['ids'] if sid in available and (sid not in job['done'] or not self.project.has_audio(available[sid]))]
        self.start_job(ids,job.get('settings'))

    def stop_generation(self):
        if self.exporter and self.exporter.isRunning() and hasattr(self.exporter,'stop'):self.exporter.stop.set()
        if self.worker and self.worker.isRunning():
            self.worker.stop.set();self.message.setText('Остановка после текущего фрагмента и его проверки. Результат будет сохранён.')

    def generation_finished(self):
        self.refresh()
        job=self.project.data.get('job') or {}
        self.message.setText(job.get('error') or ('Очередь завершена. Все результаты сохранены.' if job.get('status')=='completed' else 'Генерация остановлена. Можно продолжить очередь.'))
        if self.project.data.get('preview_error'):
            self.message.setText(self.message.text()+' Предварительная запись: '+self.project.data['preview_error'])
        if self.pending_export and job.get('status')=='completed':
            if self.exporter and self.exporter.isRunning():return
            pending=self.pending_export
            nodes,_,_,selected,_=pending
            missing=any(not self.project.has_audio(s) for _,s in self.project.rows_for_nodes(n['id'] for n in nodes)
                        if selected is None or s['id'] in selected)
            if not missing:
                self.pending_export=None
                self.guard(lambda:self.begin_export(*pending))

    def play_one(self):
        if self.editor_id:self.start_playback([self.project.find_segment(self.editor_id)[1]])

    def play_all(self):
        self.require_project();self.start_playback([s for _,s in self.project.rows(self.current_node)])

    def start_playback(self,segments):
        self.stop_audio()
        self.playlist=[copy.deepcopy(s) for s in segments if self.project.has_audio(s) and not s.get('muted')]
        if not self.playlist:raise ValueError('Пока нет аудио для воспроизведения')
        elapsed=0;pause=self.project.data['settings']['pause_ms']
        for i,seg in enumerate(self.playlist):
            self.play_offsets.append(elapsed)
            elapsed+=round(seg['duration']*1000)+(pause if i+1<len(self.playlist) else 0)
        self.play_total=elapsed
        self.seek.setRange(0,max(1,min(2147483647,elapsed)))
        self.play_at(0)

    def play_at(self,index,position=0):
        if not 0<=index<len(self.playlist):return
        self.next_audio_timer.stop();self.play_generation+=1
        seg=self.playlist[index]
        position=max(0,min(int(position),max(0,round(seg['duration']*1000)-1)))
        same=index==self.play_index and self.player.source().isValid()
        self.play_index=index;self.timeline.playing=seg['id'];self.timeline.position=position/1000
        if same and self.pending_seek is None:
            self.player.setPosition(position)
            if not self.play_paused:self.player.play()
        else:
            self.pending_seek=position
            if not same:
                self.switching_audio=True
                self.player.setSource(QUrl.fromLocalFile(str(self.project.path(seg['audio']))))
                self.switching_audio=False
            if self.player.mediaStatus() in (QMediaPlayer.LoadedMedia,QMediaPlayer.BufferedMedia):
                self.apply_pending_seek()
        self.audio_position(position)
        self.reveal_playing_fragment()

    def apply_pending_seek(self):
        if self.pending_seek is None:return
        position=self.pending_seek;self.pending_seek=None
        self.player.setPosition(position)
        if self.play_paused:self.player.pause()
        else:self.player.play()
        self.audio_position(position)

    def reveal_playing_fragment(self):
        if not self.project or not self.timeline.playing:return
        rows=self.project.rows(self.current_node,self.recursive.isChecked())
        index=next((i for i,(_,s) in enumerate(rows) if s['id']==self.timeline.playing),None)
        if index is None:return
        page=index//300
        if page!=self.page:self.change_page(page-self.page)
        x=12+sum(int(max(105,display_duration(s)*self.timeline.scale))+4 for _,s in self.rows[:index%300])
        self.timeline_scroll.ensureVisible(x+45,60,70,0)

    def play_next(self):
        if self.play_paused:return
        index=self.play_index+1
        if index>=len(self.playlist):self.stop_audio();return
        self.play_at(index)

    def audio_status(self,status):
        if self.switching_audio:return
        if status in (QMediaPlayer.LoadedMedia,QMediaPlayer.BufferedMedia):self.apply_pending_seek()
        elif status==QMediaPlayer.EndOfMedia and self.playlist and self.pending_seek is None:
            if self.play_index+1>=len(self.playlist):self.stop_audio()
            elif not self.play_paused:self.next_audio_timer.start(self.project.data['settings']['pause_ms'])

    def pause_audio(self):
        if not self.playlist:return
        self.play_paused=not self.play_paused
        if self.play_paused:
            self.next_audio_timer.stop();self.player.pause()
        elif self.pending_seek is not None:
            if self.player.mediaStatus() in (QMediaPlayer.LoadedMedia,QMediaPlayer.BufferedMedia):self.apply_pending_seek()
        elif self.player.mediaStatus()==QMediaPlayer.EndOfMedia:self.play_next()
        else:self.player.play()

    def stop_audio(self):
        self.next_audio_timer.stop();self.play_generation+=1
        self.pending_seek=None;self.playlist=[];self.play_index=-1
        self.play_offsets=[];self.play_total=0;self.play_paused=False
        self.player.stop();self.player.setSource(QUrl())
        self.timeline.playing=None;self.timeline.position=0;self.timeline.update();self.spoken.setText('')
        self.seek.setRange(0,1000);self.seek.setValue(0);self.time_label.setText('00:00 / 00:00')

    def seek_audio(self,value):
        if not self.playlist:return
        from bisect import bisect_right
        target=max(0,min(self.play_total-1,round(value*self.play_total/self.seek.maximum())))
        index=max(0,bisect_right(self.play_offsets,target)-1)
        local=target-self.play_offsets[index]
        if local>=round(self.playlist[index]['duration']*1000) and index+1<len(self.playlist):
            index+=1;local=0  # A click inside an inter-fragment pause selects the next phrase.
        self.play_at(index,local)

    def audio_position(self,position):
        if not 0<=self.play_index<len(self.playlist):return
        if self.pending_seek is not None:position=self.pending_seek
        absolute=self.play_offsets[self.play_index]+position
        if not self.seek.isSliderDown():self.seek.setValue(round(absolute/max(1,self.play_total)*self.seek.maximum()))
        self.time_label.setText(clock(absolute/1000)+' / '+clock(self.play_total/1000))
        self.timeline.position=position/1000;self.timeline.update()
        s=self.playlist[self.play_index];v=s.get('verification') or {}
        active=[w['word'] for w in v.get('words',[]) if w['start']<=position/1000<=w['end']]
        self.spoken.setText('Сейчас: '+(' '.join(active) or s['text'].strip()[:130]))

    def begin_export(self,nodes,target,separate=False,segment_ids=None,metadata=None):
        self.check_export_idle()
        self.flush_editor()
        if separate:
            from datetime import datetime
            target=Path(target)/('Экспорт-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
            target.mkdir(parents=True,exist_ok=False)
        else:
            path=Path(target).resolve()
            if path.suffix.lower() not in ('.wav','.mp3','.flac','.m4a','.m4b'):
                raise ValueError('Выберите расширение WAV, MP3, FLAC, M4A или M4B')
            if path.is_relative_to(self.project.path('audio')) or path.is_relative_to(self.project.path('voices')) or path==self.project.path('preview.wav'):
                raise ValueError('Сохраните экспорт вне папок исходных записей audio и voices')
        self.exporter=ExportWorker(self.project,nodes,target,separate,segment_ids,metadata)
        def exported(path):
            self.message.setText('Сохранено: '+path);self.export_notice.setText('Экспорт сохранён');self.export_notice.setToolTip(path)
        def failed(error):
            self.message.setText('Экспорт: '+error);self.export_notice.setText('Экспорт: ошибка');self.export_notice.setToolTip(error)
        self.exporter.result.connect(exported);self.exporter.failure.connect(failed)
        def finish():
            self.refresh()
            if self.pending_export and not (self.worker and self.worker.isRunning()):self.generation_finished()
        self.exporter.finished.connect(finish);self.exporter.start()
        self.message.setText('Сборка аудио…');self.export_notice.setText('Экспорт аудио…');self.refresh()

    def export_audio(self,selected_only=False):
        self.require_project();self.check_export_idle()
        self.flush_editor()
        ids=self.selected_ids() if selected_only else None
        if selected_only and not ids:raise ValueError('Выберите фрагменты')
        nodes=[self.project.node(nid) for nid in (self.selected_node_ids() if not selected_only else [self.current_node])]
        name=self.project.export_name(nodes[0]['id'])+(f' — ещё {len(nodes)-1} разделов' if len(nodes)>1 else '')+'.wav'
        title='Экспорт выбранных разделов в один файл' if len(nodes)>1 else 'Экспорт текущего раздела со всеми вложенными'
        target,_=QFileDialog.getSaveFileName(self,title,str(self.project.folder/name),
                                           'WAV (*.wav);;MP3 (*.mp3);;FLAC (*.flac);;M4A (*.m4a);;M4B (*.m4b)')
        if target:self.request_export(nodes,target,False,ids)

    def export_parts(self):
        self.require_project();self.check_export_idle()
        target=QFileDialog.getExistingDirectory(self,'Папка для отдельных файлов разделов')
        if target:
            nodes=[n for n in self.project.descendants(self.current_node) if self.project.rows(n['id'])]
            self.request_export(nodes,target,True)

    def processing_controls(self,form,options):
        options={**DEFAULT_PROCESSING,**options}
        remove=QCheckBox('Удалять переносы строк и абзацы из готовых фрагментов');remove.setChecked(options['remove_linebreaks']);form.addRow(remove)
        symbols_box=QHBoxLayout();symbols_check=QCheckBox('Удалять лишние символы');symbols_check.setChecked(options.get('remove_symbols',False))
        symbols=QLineEdit(options.get('symbols',''));symbols.setPlaceholderText('«;»;^;');symbols.setToolTip('Разделитель — ;. Сам ; записывается как ^;, символ ^ — как ^^.');symbols_box.addWidget(symbols_check);symbols_box.addWidget(symbols);form.addRow(symbols_box)
        combos=[]
        choices=[('Абзац','paragraph'),('Точка','sentence'),('Запятая','comma'),('Пробел','space'),('Отключить','off')]
        for index in range(4):
            combo=QComboBox()
            for label,key in choices:combo.addItem(label,key)
            values=options['priorities'];key=values[index] if index<len(values) else 'off'
            combo.setCurrentIndex(max(0,combo.findData(key)));form.addRow(f'Приоритет разбиения {index+1}',combo);combos.append(combo)
        spaces=QSpinBox();spaces.setRange(0,100);spaces.setValue(options['space_cycles']);form.addRow('Удаление двойных пробелов: циклов',spaces)
        paragraphs=QSpinBox();paragraphs.setRange(0,100);paragraphs.setValue(options['paragraph_cycles']);form.addRow('Удаление двойных абзацев: циклов',paragraphs)
        return lambda:dict(options,priorities=[c.currentData() for c in combos],remove_linebreaks=remove.isChecked(),
                           space_cycles=spaces.value(),paragraph_cycles=paragraphs.value(),remove_symbols=symbols_check.isChecked(),symbols=symbols.text())

    def export_options(self,target):
        dialog=QDialog(self);dialog.setWindowTitle('Настройки экспорта');form=QFormLayout(dialog)
        processing=self.processing_controls(form,self.project.data['settings'].get('text_processing',DEFAULT_PROCESSING))
        note=QLabel('Приоритеты используются при следующем импорте/пересборке.\nОчистка при экспорте применяется к встроенному тексту аудио.');note.setWordWrap(True);form.addRow(note)
        text=QCheckBox('Встроить текст озвученных фрагментов');text.setChecked(True);form.addRow(text)
        cover=QLineEdit();line=QHBoxLayout();line.addWidget(cover)
        def pick():
            path,_=QFileDialog.getOpenFileName(dialog,'Обложка','','Изображения (*.jpg *.jpeg *.png)')
            if path:cover.setText(path)
        self.add_button(line,'Выбрать…',pick);form.addRow('Картинка (необязательно)',line)
        label=QLabel('Текст и обложка: MP3, FLAC, M4A/M4B и обычный WAV (ID3).\nRF64 сохраняется без обложки и текста. Поддержка отображения зависит от плеера.');label.setWordWrap(True);form.addRow(label)
        buttons=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel);buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject);form.addRow(buttons)
        if not dialog.exec():return None
        image=cover.text().strip()
        if image:
            from PySide6.QtGui import QImage
            if QImage(image).isNull():raise ValueError('Не удалось прочитать картинку')
        options=processing();self.project.data['settings']['text_processing']=options;self.project.save()
        return dict(text=text.isChecked(),cover=image or None,processing=options)

    def request_export(self,nodes,target,separate=False,segment_ids=None):
        self.flush_editor()
        selected=set(segment_ids) if segment_ids is not None else None
        segments={s['id']:s for _,s in self.project.rows_for_nodes(n['id'] for n in nodes)
                  if selected is None or s['id'] in selected}
        ids=list(segments)
        if not ids:raise ValueError('Нет фрагментов для экспорта')
        missing=[sid for sid in ids if not self.project.has_audio(segments[sid])]
        generate=False
        if missing:
            question=QMessageBox(self);question.setWindowTitle('Не все фрагменты сгенерированы')
            question.setText(f'Нет аудио у {len(missing)} из {len(ids)} фрагментов. Как экспортировать?')
            synth=question.addButton('Сгенерировать и экспортировать',QMessageBox.AcceptRole)
            if self.worker and self.worker.isRunning():
                synth.setEnabled(False);question.setInformativeText('Генерация уже выполняется. Сейчас можно экспортировать сохранённые фрагменты; текущая очередь продолжит работу.')
            skip=question.addButton('Пропустить несгенерированные',QMessageBox.ActionRole)
            question.addButton('Отмена',QMessageBox.RejectRole);question.exec()
            if question.clickedButton() not in (synth,skip):return
            generate=question.clickedButton()==synth
            if not generate and len(missing)==len(ids):raise ValueError('Нет сгенерированного аудио. Файл не создан.')
        metadata=self.export_options(target)
        if metadata is None:return
        args=(nodes,target,separate,segment_ids,metadata)
        if generate:
            self.pending_export=args;self.start_job(missing)
        else:self.begin_export(*args)

    def fragment_context_menu(self,position):
        if not self.project:return
        item=self.table.itemAt(position)
        if item is None:return
        if not item.isSelected():self.table.selectRow(item.row())
        menu=QMenu(self)
        actions=[('Удалить',self.delete_fragments),
                 ('Пересобрать под установленную длину фрагмента',self.rebuild_selected),
                 ('Экспорт аудио',lambda:self.export_audio(True)),('Генерировать аудио',self.regenerate)]
        for label,fn in actions:
            action=menu.addAction(label);action.setEnabled(not self.busy() or (label=='Экспорт аудио' and not (self.exporter and self.exporter.isRunning())));action.triggered.connect(lambda checked=False,fn=fn:self.guard(fn))
        menu.exec(self.table.viewport().mapToGlobal(position))

    def selected_node_ids(self):
        selected={item.data(0,Qt.UserRole) for item in self.tree.selectedItems()}
        if not selected and self.current_node:selected={self.current_node}
        nodes={n['id']:n for n in self.project.data['nodes']}
        result=[]
        for node in self.project.descendants(self.project.data['root']):
            if node['id'] not in selected:continue
            parent=node['parent'];covered=False
            while parent is not None:
                if parent in selected:covered=True;break
                parent=nodes[parent]['parent']
            if not covered:result.append(node['id'])
        return result

    def selected_section_rows(self):
        return self.project.rows_for_nodes(self.selected_node_ids())

    def tree_context_menu(self,position):
        if not self.project:return
        item=self.tree.itemAt(position)
        if item and not item.isSelected():
            self.tree.clearSelection();self.tree.setCurrentItem(item);item.setSelected(True)
        menu=QMenu(self)
        if len(self.tree.selectedItems())>1:
            actions=[('Дублировать',self.duplicate_section),
                     ('Пересобрать фрагменты текста под длину фрагмента',self.rebuild_section),
                     ('Экспорт в аудио',self.export_audio),('Продолжить генерацию',self.generate),
                     ('Удалить проект/раздел',self.delete_section)]
        else:
            actions=[('Дублировать',self.duplicate_section),('Переименовать',self.rename_node),
                     ('Добавить вложенный раздел',self.new_node),
                     ('Пересобрать фрагменты текста под длину фрагмента',self.rebuild_section),
                     ('Экспорт проекта в zip',self.export_project_zip),('Экспорт в аудио',self.export_audio),
                     ('Продолжить генерацию',self.generate),('Удалить проект/раздел',self.delete_section)]
        actions += [('Свойства проекта / раздела',self.section_properties),('Открыть в Проводнике папку проекта',lambda:os.startfile(str(self.project.folder)))]
        for label,fn in actions:
            action=menu.addAction(label);action.setEnabled(not self.busy() or (label=='Экспорт в аудио' and not (self.exporter and self.exporter.isRunning())));action.triggered.connect(lambda checked=False,fn=fn:self.guard(fn))
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def section_properties(self):
        from properties_ui import properties_dialog
        properties_dialog(self)

    def delete_fragments(self):
        self.check_idle();self.flush_editor();ids=self.selected_ids()
        if not ids:return
        if QMessageBox.question(self,'Удалить фрагменты',f'Удалить {len(ids)} фрагментов из проекта? Аудиофайлы останутся в папке проекта.',QMessageBox.Yes|QMessageBox.No)!=QMessageBox.Yes:return
        self.stop_audio();self.editor_id=None;self.pending_export=None
        self.project.remove_segments(ids);self.refresh()

    def rebuild_selected(self):
        self.rebuild_ids(self.selected_ids())

    def rebuild_section(self):
        self.rebuild_ids([s['id'] for _,s in self.selected_section_rows()])

    def rebuild_ids(self,ids):
        self.check_idle();self.flush_editor()
        if not ids:return
        limit=self.project.data['settings']['max_chars']
        if QMessageBox.question(self,'Пересобрать фрагменты',f'Заново разбить выбранный текст по {limit} символов? Новые фрагменты потребуют генерации; старые WAV сохранятся.',QMessageBox.Yes|QMessageBox.No)!=QMessageBox.Yes:return
        self.stop_audio();self.editor_id=None;self.pending_export=None
        self.project.rebuild_segments(ids,limit);self.refresh()

    def duplicate_section(self):
        self.check_import_idle();self.check_idle();self.flush_editor()
        selected=self.selected_node_ids()
        if len(selected)>1:
            clones=[self.project.duplicate_node(nid) for nid in selected]
            self.current_node=clones[0]['id'];self.rebuild_tree();self.tree.clearSelection()
            for clone in clones:self.tree_items[clone['id']].setSelected(True)
            self.refresh();return
        self.current_node=selected[0]
        if self.current_node==self.project.data['root']:
            parent=QFileDialog.getExistingDirectory(self,'Папка для копии проекта')
            if not parent:return
            from project import uid
            source=self.project.folder.resolve();target=Path(parent)/(source.name+' — копия-'+uid()[:6])
            if target.resolve().is_relative_to(source):raise ValueError('Выберите папку вне исходного проекта')
            self.project.flush_saves()
            shutil.copytree(source,target,ignore=shutil.ignore_patterns('.reader.lock','*.tmp'))
            duplicate=Project.open(target);duplicate.node(duplicate.data['root'])['title']+=' — копия';duplicate.save()
            self.set_project(duplicate,self.acquire_project(target))
        else:
            clone=self.project.duplicate_node(self.current_node);self.current_node=clone['id'];self.rebuild_tree();self.refresh()

    def delete_section(self):
        self.check_import_idle();self.check_idle();self.flush_editor()
        selected=self.selected_node_ids()
        if len(selected)>1:
            if QMessageBox.question(self,'Удалить разделы',f'Удалить {len(selected)} выбранных разделов вместе с вложенными?',QMessageBox.Yes|QMessageBox.No)!=QMessageBox.Yes:return
            self.stop_audio();self.editor_id=None;self.pending_export=None
            for nid in selected:self.project.remove_node(nid)
            self.current_node=self.project.data['root'];self.offpage_selection.clear();self.rebuild_tree();self.refresh();return
        self.current_node=selected[0]
        node=self.project.node(self.current_node)
        if QMessageBox.question(self,'Удалить проект/раздел',f'Удалить «{node["title"]}» вместе с вложенными разделами?\nПапка удалённого проекта будет перемещена в .reader-trash рядом с проектом.',QMessageBox.Yes|QMessageBox.No)!=QMessageBox.Yes:return
        self.stop_audio();self.editor_id=None;self.pending_export=None
        if self.current_node!=self.project.data['root']:
            self.current_node=node['parent'];self.project.remove_node(node['id']);self.rebuild_tree();self.refresh();return
        from project import uid
        source=self.project.folder.resolve()
        if source==source.parent or not (source/'project.json').is_file():raise ValueError('Недопустимая папка проекта')
        target=source.parent/'.reader-trash'/(source.name+'-'+uid()[:8])
        if not target.resolve().is_relative_to(source.parent):raise ValueError('Недопустимый путь корзины')
        target.parent.mkdir(exist_ok=True);self.project.close_saves();self.project_lock.unlock()
        try:shutil.move(str(source),str(target))
        except Exception:
            self.project_lock=self.acquire_project(source);raise
        self.project=None;self.project_lock=None;self.current_node=None;self.rows=[]
        self.tree.clear();self.table.setRowCount(0);self.editor.clear();self.verification.clear();self.timeline.set_rows([],[])
        self.preferences.remove('project');self.title.setText('Откройте или создайте проект');self.summary.clear();self.refresh()
        self.message.setText('Проект перемещён в '+str(target))

    def export_project_zip(self):
        self.check_idle();self.flush_editor()
        name=self.project.export_name(self.current_node)+'.zip'
        path,_=QFileDialog.getSaveFileName(self,'Экспорт проекта/раздела в ZIP',str(self.project.folder.parent/name),'ZIP (*.zip)')
        if not path:return
        self.exporter=ArchiveWorker(self.project,self.current_node,path)
        self.exporter.result.connect(lambda p:self.message.setText('ZIP сохранён: '+p))
        self.exporter.failure.connect(lambda e:self.message.setText('ZIP: '+e));self.exporter.finished.connect(self.refresh)
        self.exporter.start();self.refresh()

    def show_folder(self):
        self.require_project();QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.project.folder.resolve())))

    def help(self):
        QMessageBox.information(self,'Как работать',
            '1. Создайте проект и при необходимости вложенные разделы.\n'
            '2. Загрузите документ или вставьте текст в выбранный раздел.\n'
            '3. Выберите голос и нажмите «Генерировать / продолжить».\n'
            '4. Сверка выполняется автоматически и не останавливает очередь. Подтверждение необязательно.\n'
            '5. Ctrl и Shift выделяют несколько строк для перегенерации или переноса.\n'
            '6. Курсор в тексте + «Разделить» создаёт два независимых фрагмента.\n'
            '7. Экспорт объединяет текущий раздел и все вложенные разделы по порядку дерева. '
            '«Экспорт по разделам» создаёт отдельный файл каждого раздела с его дочерними разделами.\n\n'
            'Проект сохраняется автоматически. Для переноса скопируйте всю папку проекта. '
            'Старые записи доступны в «Версии». Проверка ASR не является гарантией: она может ошибаться на именах и числах. '
            'Higgs рассчитан прежде всего на английскую речь; для русского доступен голос Ирина.')

    def closeEvent(self,event):
        if self.api_service and self.api_service.active():
            self.message.setText('Остановите задания API и дождитесь сохранения перед закрытием.');event.ignore();return
        if self.api_service:self.api_service.close()
        if self.import_active():
            self.cancel_import();self.message.setText('Импорт останавливается. Добавленные разделы сохраняются; закройте окно после остановки.');event.ignore();return
        if self.busy():
            self.stop_generation();self.message.setText('Дождитесь сохранения текущего фрагмента или завершения экспорта, затем закройте окно.');event.ignore();return
        self.stop_audio()
        self.flush_editor()
        if self.project:
            self.project.save();self.project.close_saves()
        if self.project_lock:self.project_lock.unlock()
        event.accept()


def configure_app(app):
    app.setStyle('Fusion')
    app.setStyleSheet('''
        QWidget { background:#18212e;color:#e2e9f5;font-family:"Segoe UI";font-size:12px; }
        QMainWindow { background:#18212e; }
        QPushButton {background:#29374b;border:1px solid #3a4b65;border-radius:6px;padding:9px 12px;}
        QPushButton:hover {background:#364b68;}
        QPushButton:disabled {color:#64738a;background:#202b3a;}
        QPushButton#primary {background:#426ce6;border:1px solid #6c91ff;font-weight:600;}
        QTreeWidget,QTableWidget,QPlainTextEdit,QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox {background:#111925;border:1px solid #344358;border-radius:5px;padding:5px;selection-background-color:#354f86;}
        QTableWidget {gridline-color:#273346;}
        QHeaderView::section {background:#223046;border:0;padding:8px;color:#a6b7d1;}
        QProgressBar {border:0;background:#101823;border-radius:5px;text-align:center;height:20px;}
        QProgressBar::chunk {background:#367e72;border-radius:5px;}
        QSplitter::handle {background:#273346;}
        QScrollBar {background:#162031;}
    ''')


def main():
    app=QApplication(sys.argv);configure_app(app)
    preferences=QSettings('HiggsReader','SmokeTest') if '--smoke-test' in sys.argv else None
    window=MainWindow(preferences);window.show()
    if '--smoke-test' in sys.argv:
        def finish_smoke():
            log_dir=BASE/'logs';log_dir.mkdir(exist_ok=True)
            window.grab().save(str(log_dir/'startup.png'))
            (log_dir/'startup-ok.txt').write_text('Native Windows window opened successfully.',encoding='utf-8')
            window.close()
        QTimer.singleShot(1000,finish_smoke)
    return app.exec()


if __name__=='__main__':
    sys.exit(main())
