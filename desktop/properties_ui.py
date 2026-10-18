"""Project/chapter cast properties; references use stable IDs, never display names."""
import copy,json
from PySide6.QtWidgets import QDialog,QVBoxLayout,QTableWidget,QTableWidgetItem,QDialogButtonBox,QLabel
from PySide6.QtCore import Qt
from characters import registry

def properties_dialog(window):
    project=window.project;node_id=window.current_node or project.data['root'];node=project.node(node_id)
    dialog=QDialog(window);dialog.setWindowTitle('Свойства: '+node['title']);dialog.resize(1120,650);layout=QVBoxLayout(dialog)
    cast=copy.deepcopy(registry(project));used={s.get('speaker_id') for _,s in project.rows(node_id)}
    keys=list(cast) if node_id==project.data['root'] else [key for key in cast if key in used]
    layout.addWidget(QLabel('Имена и сведения общие для проекта. Переименование по постоянному ID обновляет все главы. Алиасы и прозвища разделяйте символом ;.'))
    columns=[('name','Имя'),('gender','Пол'),('full_name','ФИО'),('aliases','Имена в тексте'),('nicknames','Прозвища'),('origin','Происхождение'),('description','Сведения'),('voice','ID голоса')]
    table=QTableWidget(len(keys),len(columns));table.setHorizontalHeaderLabels([label for _,label in columns]);layout.addWidget(table)
    for i,key in enumerate(keys):
        for col,(field,_) in enumerate(columns):
            value=cast[key].get(field,'') or ''
            item=QTableWidgetItem('; '.join(value) if isinstance(value,list) else str(value));item.setToolTip('Постоянный ID: '+key);table.setItem(i,col,item)
    buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel);layout.addWidget(buttons);buttons.rejected.connect(dialog.reject)
    def save():
        changes={}
        for i,key in enumerate(keys):
            values={field:table.item(i,col).text().strip() for col,(field,_) in enumerate(columns)}
            if not values['name']:raise ValueError('Укажите имя персонажа')
            if values['voice'] and values['voice'] not in {v['id'] for v in project.data['settings'].get('voices',[])}:raise ValueError('Неизвестный ID голоса: '+values['voice'])
            for field in ('aliases','nicknames'):values[field]=[v.strip() for v in values[field].split(';') if v.strip()]
            changes[key]=values
        with project.lock:
            for key,values in changes.items():registry(project)[key].update(values)
            node['character_ids']=keys;project.save()
        window.refresh();dialog.accept()
    buttons.accepted.connect(lambda:window.guard(save));dialog.exec()
