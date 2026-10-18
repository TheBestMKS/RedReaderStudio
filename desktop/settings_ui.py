"""Tabbed generation settings. Voice ownership stays in project/AI settings."""
import copy
from PySide6.QtCore import Qt,QTimer
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QFormLayout,QTabWidget,QWidget,QCheckBox,QSpinBox,QComboBox,
    QDialogButtonBox,QLabel,QTableWidget,QTableWidgetItem,QPushButton,QHBoxLayout,QListWidget,QAbstractItemView,QScrollArea)
from text_processing import DEFAULT_PROCESSING

STAGES={'ai':'Обработка текста с помощью ИИ','roles':'Распределение ролей','replace':'Автозамена символов',
        'symbols':'Удаление символов','cleanup':'Удаление лишних пробелов и абзацев','split':'Деление на фрагменты'}
DEFAULT_ORDER=list(STAGES)

def settings_dialog(window):
    window.require_project();cfg=window.project.data['settings'];dialog=QDialog(window);dialog.setWindowTitle('Настройка');dialog.resize(820,720)
    layout=QVBoxLayout(dialog);tabs=QTabWidget();layout.addWidget(tabs)
    def page(title):
        scroll=QScrollArea();scroll.setWidgetResizable(True);content=QWidget();form=QFormLayout(content);scroll.setWidget(content);tabs.addTab(scroll,title);return form
    def spin(form,label,value,low=0,high=60000,suffix=''):
        control=QSpinBox();control.setRange(low,high);control.setValue(value);control.setSuffix(suffix);form.addRow(label,control);return control
    def check(form,label,value):
        control=QCheckBox(label);control.setChecked(value);form.addRow(control);return control
    form=page('Текст и фрагментация')
    size=spin(form,'Символов в фрагменте',cfg.get('max_chars',300),20,2400)
    processing=window.processing_controls(form,cfg.get('text_processing',DEFAULT_PROCESSING))
    form=page('Автозамена')
    table=QTableWidget(0,2);table.setHorizontalHeaderLabels(['Заменить','На']);table.horizontalHeader().setStretchLastSection(True);form.addRow(table)
    def add(pair=None):
        i=table.rowCount();table.insertRow(i)
        for col,value in enumerate(pair or ['','']):table.setItem(i,col,QTableWidgetItem(value))
    for pair in cfg.get('text_processing',{}).get('replacements',[]):add(pair)
    row=QHBoxLayout();form.addRow(row)
    plus=QPushButton('+');plus.clicked.connect(lambda:add());row.addWidget(plus)
    minus=QPushButton('Удалить');minus.clicked.connect(lambda:table.removeRow(table.currentRow()));row.addWidget(minus)
    form.addRow(QLabel('Замены выполняются сверху вниз, с учётом регистра. Поддерживаются слова и предложения.'))
    form=page('Порядок обработки')
    order=QListWidget();order.setDragDropMode(QAbstractItemView.InternalMove)
    for key in cfg.get('text_processing',{}).get('order',DEFAULT_ORDER):
        if key in STAGES:
            order.addItem(STAGES[key]);order.item(order.count()-1).setData(Qt.UserRole,key)
    form.addRow(QLabel('Перетаскивайте действия. Роли должны определяться до деления на фрагменты.'))
    form.addRow(order)
    form=page('Генерация и сверка')
    retry_errors=check(form,'Перегенерировать фрагмент автоматически при ошибке',cfg.get('retry_errors',True))
    errors=spin(form,'Не более повторов после ошибки',cfg.get('error_retries',10),0,100)
    retry_score=check(form,'Перегенерировать автоматически при низкой сверке',cfg.get('retry_low_score',True))
    score=spin(form,'Если сверка ниже',cfg.get('min_score',85),0,100,' %')
    repeats=spin(form,'Не более повторов при низкой сверке',cfg.get('quality_retries',10),0,100)
    form.addRow(QLabel('После исчерпания повторов очередь продолжается. Сохраняется лучший результат сверки.'))
    following=check(form,'Автоматически продолжать следующие разделы',cfg.get('continue_next_sections',True))
    lang=QComboBox();lang.addItem('Русский','ru');lang.addItem('English','en');lang.setCurrentIndex(max(0,lang.findData(cfg.get('language','ru'))));form.addRow('Язык сверки',lang)
    form=page('Паузы')
    automatic=check(form,'Автоматически исключать длинные паузы',cfg.get('auto_cut_pauses',True))
    maximum=spin(form,'Паузы длиннее',cfg.get('max_pause_ms',1000),100,60000,' мс')
    replacement=spin(form,'Длина заменяющей паузы',cfg.get('pause_replacement_ms',0),0,60000,' мс')
    start=spin(form,'Сохранять от начала найденной паузы',cfg.get('pause_keep_start_ms',300),0,60000,' мс')
    end=spin(form,'Сохранять перед концом найденной паузы',cfg.get('pause_keep_end_ms',300),0,60000,' мс')
    edge_controls={}
    for side,title,left,right in [('start','в начале фрагмента',50,200),('end','в конце фрагмента',200,50)]:
        edge_controls['trim_'+side+'_pause']=check(form,'Убирать аудио паузу '+title,cfg.get('trim_'+side+'_pause',True))
        edge_controls[side+'_pause_threshold_ms']=spin(form,'Если пауза '+title+' больше',cfg.get(side+'_pause_threshold_ms',800),100,60000,' мс')
        edge_controls[side+'_pause_keep_start_ms']=spin(form,'Запас от начала этой паузы',cfg.get(side+'_pause_keep_start_ms',left),0,60000,' мс')
        edge_controls[side+'_pause_keep_end_ms']=spin(form,'Запас от конца этой паузы',cfg.get(side+'_pause_keep_end_ms',right),0,60000,' мс')
    interval=spin(form,'Между фрагментами' ,cfg.get('pause_ms',160),0,60000,' мс')
    form.addRow(QLabel('Защитные края сохраняются в дополнение к заменяющей паузе. Исходное аудио можно восстановить.'))
    buttons=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel);layout.addWidget(buttons);buttons.rejected.connect(dialog.reject)
    def save():
        stages=[order.item(i).data(Qt.UserRole) for i in range(order.count())]
        if stages.index('roles')>stages.index('split'):raise ValueError('Распределение ролей должно предшествовать делению на фрагменты')
        opts=processing();opts.update(order=stages,replacements=[[table.item(i,0).text(),table.item(i,1).text()] for i in range(table.rowCount()) if table.item(i,0).text()])
        with window.project.lock:
            cfg.update(max_chars=size.value(),text_processing=opts,retry_errors=retry_errors.isChecked(),error_retries=errors.value(),retry_low_score=retry_score.isChecked(),min_score=score.value(),quality_retries=repeats.value(),continue_next_sections=following.isChecked(),language=lang.currentData(),auto_cut_pauses=automatic.isChecked(),max_pause_ms=maximum.value(),pause_replacement_ms=replacement.value(),pause_keep_start_ms=start.value(),pause_keep_end_ms=end.value(),pause_ms=interval.value())
            cfg.update({key:control.isChecked() if isinstance(control,QCheckBox) else control.value() for key,control in edge_controls.items()})
        window.project.save();window.refresh();dialog.accept()
        window.settings_notice.setText('Настройки сохранены. Параметры генерации применятся со следующего фрагмента. Обработка текста — при следующем импорте или пересборке.');window.settings_notice.show();QTimer.singleShot(15000,window.settings_notice.hide)
    buttons.accepted.connect(lambda:window.guard(save));dialog.exec()
