"""Persistent character identities and chapter-first, lossless speaker labeling."""
import copy
import uuid
import re
from pathlib import Path

NARRATOR='narrator'

def registry(project):
    cast=project.data.setdefault('cast',{})
    cast.setdefault(NARRATOR,dict(id=NARRATOR,name='Чтец',gender='неизвестен',aliases=['Рассказчик','Чтец'],description='',voice=None,full_name='',origin='',nicknames=[]))
    return cast

def merge_character(project,token,info):
    cast=registry(project)
    if token in ('Рассказчик','Чтец','narrator'):return NARRATOR
    existing=info.get('existing_id') or token
    if existing not in cast:
        # Only exact unique aliases are safe; short names can denote different people.
        matches=[key for key,c in cast.items() if token==c['name'] or token in c.get('aliases',[])]
        existing=matches[0] if len(matches)==1 else None
    if existing is None or existing not in cast:
        existing='character-'+uuid.uuid4().hex
        number=project.data.get('anonymous_counter',0)+1;project.data['anonymous_counter']=number
        cast[existing]=dict(id=existing,name=f'Персонаж {number}',gender='неизвестен',aliases=[],description='',voice=None,full_name='',origin='',nicknames=[])
    char=cast[existing]
    for key in ('name','full_name','origin','description','gender'):
        value=info.get(key)
        if isinstance(value,str) and value.strip():char[key]=value.strip()
    aliases=info.get('aliases',[]);aliases=aliases if isinstance(aliases,list) else []
    token_alias=[] if re.fullmatch(r'(?:person|unknown|character)\d+',token,re.I) else [token]
    char['aliases']=list(dict.fromkeys(char.get('aliases',[])+token_alias+[v for v in aliases if isinstance(v,str)]))
    nicknames=info.get('nicknames',[])
    if isinstance(nicknames,list):char['nicknames']=list(dict.fromkeys(char.get('nicknames',[])+[n for n in nicknames if isinstance(n,str)]))
    return existing

def label_chapter(project,text,llm,cfg,notify=lambda _:None,stop=None):
    from language_ai import dialogue_units
    units=dialogue_units(text);turns=[];offset=0
    while offset<len(units):
        if stop and stop.is_set():raise InterruptedError('Анализ остановлен')
        batch=[];size=0
        while offset+len(batch)<len(units) and (size<6000 or not batch):
            part=units[offset+len(batch)];batch.append(part);size+=len(part)
        with project.lock:cast=copy.deepcopy(registry(project))
        notify(f'ИИ: персонажи и реплики {offset+1}/{len(units)}')
        result=llm.ask(cfg.get('llm_model','qwen2.5-3b'),
            'Разметь литературный текст по говорящим. Не переписывай и не пропускай текст. Авторские слова читает narrator. '
            'Сохраняй имена и обращения в точном написании из текста, без перевода или транслитерации. В aliases перечисли встреченные в тексте формы имени. Укажи для каждого id метку говорящего в labels. Повторно используй id из cast, включая ранее неизвестных персонажей. '
            'Не смешивай людей с одинаковыми короткими именами. Учитывай пол, полное имя, фамилию, отчество, происхождение, прозвища, обращения и контекст. '
            'Для нового неизвестного человека используй отдельную временную метку; если позднее раскрыто имя, укажи existing_id и новое name. '
            'Формат {"labels":{"1":"narrator","2":"person1"},"characters":{"person1":{"existing_id":"","name":"","gender":"мужской/женский/неизвестен","full_name":"","origin":"","aliases":[],"nicknames":[],"description":""}}}.',
            dict(units=[dict(id=str(i+1),text=t) for i,t in enumerate(batch)],cast=cast,previous=project.data.get('cast_context','')[-1500:]),notify)
        labels=result.get('labels',{});infos=result.get('characters',{})
        if not isinstance(labels,dict) or not isinstance(infos,dict) or any(not isinstance(labels.get(str(i+1)),str) or not labels[str(i+1)].strip() for i in range(len(batch))):raise ValueError('ИИ пропустил реплики. Повторите импорт; исходный документ не изменён.')
        with project.lock:
            mapping={}
            for token in dict.fromkeys(labels.values()):
                info=infos.get(token,{})
                if not isinstance(info,dict):info={}
                if token not in registry(project) and token not in ('narrator','Рассказчик','Чтец') and not info.get('name') and not token.lower().startswith(('person','unknown','неизвест','персонаж')):info={**info,'name':token}
                mapping[token]=merge_character(project,token,info)
            for i,t in enumerate(batch):
                speaker=mapping[labels[str(i+1)]]
                if turns and turns[-1]['speaker_id']==speaker:turns[-1]['text']+=t
                else:turns.append(dict(speaker_id=speaker,text=t))
            project.data['cast_context']=''.join(batch)[-1500:];project.save()
        offset+=len(batch)
    # Assignment is optional. An absent library leaves explicit unassigned roles.
    voices=cfg.get('voices',[])
    if voices:
        with project.lock:unassigned={k:c for k,c in registry(project).items() if not c.get('voice') and k!=NARRATOR}
        if unassigned:
            result=llm.ask(cfg.get('llm_model','qwen2.5-3b'),'Назначь голоса по описанию и полу. Только id из voices. Ответ {"assignments":{"id персонажа":"id голоса"}}.',dict(characters=unassigned,voices=voices),notify)
            assignments=result.get('assignments',{})
            if isinstance(assignments,dict):
                with project.lock:
                    for key,value in assignments.items():
                        if key in unassigned and value in {v['id'] for v in voices}:registry(project)[key]['voice']=value
                    project.save()
    return turns

def import_voice_library(project,folder):
    import shutil,json
    folder=Path(folder)
    if not folder.is_dir():return
    known={v.get('library_name') for v in project.data['settings'].get('voices',[])}
    for sample in sorted(folder.iterdir()):
        if sample.suffix.lower() not in ('.wav','.flac','.mp3') or sample.name in known:continue
        metadata={}
        sidecar=sample.with_suffix('.json')
        if sidecar.is_file():
            try:metadata=json.loads(sidecar.read_text(encoding='utf-8'))
            except ValueError:pass
        destination=project.path('voices/'+uuid.uuid4().hex+sample.suffix.lower());destination.parent.mkdir(exist_ok=True);shutil.copy2(sample,destination)
        project.data['settings'].setdefault('voices',[]).append(dict(id='voice-'+uuid.uuid4().hex[:12],library_name=sample.name,description=metadata.get('description',sample.stem),engine='higgs',reference=destination.relative_to(project.folder).as_posix(),reference_text=metadata.get('reference_text','')))
    project.save()
