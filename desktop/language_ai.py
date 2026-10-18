"""Offline correction and speaker assignment; source text is never overwritten."""
import copy
import gc
import hashlib
import json
import re
from model_manager import CATALOG, ModelManager, INFERENCE_LOCK

DEFAULT_AI=dict(mode='simple',llm_model='qwen2.5-3b',correct_text=False,dialogue=False,voices=[],pause_replacement_ms=0,continue_next_sections=True,
    retry_errors=True,error_retries=10,retry_low_score=True,min_score=85,quality_retries=10,
    pause_keep_start_ms=300,pause_keep_end_ms=300,
    trim_start_pause=True,start_pause_threshold_ms=800,start_pause_keep_start_ms=50,start_pause_keep_end_ms=200,
    trim_end_pause=True,end_pause_threshold_ms=800,end_pause_keep_start_ms=200,end_pause_keep_end_ms=50)


class LanguageModel:
    def __init__(self,models):self.models=models;self.model=None;self.tokenizer=None;self.key=None

    def unload(self):
        self.model=None;self.tokenizer=None;self.key=None;gc.collect()
        import torch
        if torch.cuda.is_available():torch.cuda.empty_cache()

    def ask(self,key,instruction,payload,notify=lambda _:None):
        import torch
        from transformers import AutoTokenizer,AutoModelForCausalLM
        with INFERENCE_LOCK:
            if key not in CATALOG or CATALOG[key]['kind']!='text':raise ValueError('Неизвестная языковая модель')
            if not ModelManager(self.models).installed(key):raise ValueError('Сначала скачайте языковую модель: '+key)
            if self.key!=key:
                self.unload();notify('Загрузка '+CATALOG[key]['name'])
                path=self.models/CATALOG[key]['path']
                self.tokenizer=AutoTokenizer.from_pretrained(path,local_files_only=True)
                size=sum(p.stat().st_size for p in path.glob('*.safetensors'))
                device='cuda' if torch.cuda.is_available() and torch.cuda.mem_get_info()[0]>size+1536*1024**2 else 'cpu'
                if device=='cpu':
                    torch.set_num_threads(min(8,torch.get_num_threads()))
                    notify('Языковая модель работает на CPU (видеопамять недоступна или занята)')
                self.model=AutoModelForCausalLM.from_pretrained(path,local_files_only=True,
                    dtype=torch.bfloat16 if device=='cuda' else torch.float32).to(device).eval();self.key=key
            messages=[dict(role='system',content=instruction+' Ответ только JSON, без Markdown. Кавычки внутри строк экранируй.'),dict(role='user',content=json.dumps(payload,ensure_ascii=False))]
            for attempt in range(3):
                prompt=self.tokenizer.apply_chat_template(messages,tokenize=False,add_generation_prompt=True)
                inputs=self.tokenizer(prompt,return_tensors='pt').to(self.model.device)
                if inputs.input_ids.shape[1]>24000:raise ValueError('Слишком большой контекст анализа. Уменьшите число описаний голосов.')
                with torch.inference_mode():output=self.model.generate(**inputs,max_new_tokens=4096,do_sample=False,pad_token_id=self.tokenizer.eos_token_id)
                answer=self.tokenizer.decode(output[0,inputs.input_ids.shape[1]:],skip_special_tokens=True).strip();self.last_response=answer
                start=answer.find('{');end=answer.rfind('}');raw=answer[start:end+1]
                try:
                    parsed=json.loads(raw)
                    if not isinstance(parsed,dict):raise ValueError('Требуется JSON объект')
                    return parsed
                except ValueError:
                    # A frequent small-model error: unescaped quotation marks in
                    # the sole text field. Recover only this unambiguous shape.
                    match=re.fullmatch(r'\s*\{\s*"text"\s*:\s*"(.*)"\s*\}\s*',raw,re.S)
                    if match and 'исправ' in instruction.lower():
                        text=match.group(1).replace('\\"','"').replace('\\n','\n').replace('\\r','\r').replace('\\t','\t')
                        return dict(text=text)
                    messages.extend([dict(role='assistant',content=answer),dict(role='user',content='Исправь синтаксис JSON предыдущего ответа, не меняя данные. Верни один корректный JSON объект.')])
                    notify('Повторная проверка формата ответа языковой модели…')
            raise ValueError('Языковая модель вернула некорректный ответ после трёх попыток. Исходный текст сохранён.')


def dialogue_units(text):
    boundaries={0,len(text)}
    for match in re.finditer(r'«[^»]*»|“[^”]*”|"[^"]*"',text):boundaries.update((match.start(),match.end()))
    for match in re.finditer(r'—|\n|[.!?](?=\s)',text):boundaries.add(match.start() if match.group()=='—' else match.end())
    points=sorted(boundaries);parts=[text[a:b] for a,b in zip(points,points[1:])]
    result=[]
    for part in parts:
        if not part:continue
        if result and not re.search(r'\w',part):result[-1]+=part
        else:result.append(part)
    return result or [text]


def signature(text,cfg):
    return hashlib.sha256(json.dumps([text,{k:cfg.get(k,DEFAULT_AI.get(k)) for k in ('llm_model','correct_text','dialogue','voices')}],ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def prepare_segment(project,seg,cfg,llm,notify=lambda _:None):
    stamp=signature(seg['text'],cfg)
    valid_ids={v['id'] for v in cfg.get('voices',[])}
    if seg.get('ai',{}).get('signature')==stamp and (not cfg.get('dialogue') or all(project.data.get('characters',{}).get(t['speaker'],{}).get('voice') in valid_ids for t in seg['ai']['turns'])):return seg['ai']
    text=seg['text'];key=cfg.get('llm_model','qwen2.5-3b')
    if cfg.get('correct_text'):
        notify('ИИ: исправление текста')
        result=llm.ask(key,'Исправь только явные грамматические и смысловые ошибки в русском тексте. Сохрани язык, имена, факты, стиль и все реплики. Не сокращай и не добавляй содержание. Формат {"text":"исправленный текст"}.',dict(text=text),notify)
        corrected=result.get('text')
        if not isinstance(corrected,str) or not corrected.strip() or not .5<=len(corrected)/max(1,len(text))<=2:
            raise ValueError('Некорректный результат коррекции; исходный текст сохранён')
        text=corrected
    turns=[dict(speaker='Рассказчик',text=text)]
    characters=copy.deepcopy(project.data.get('characters',{}))
    if cfg.get('dialogue'):
        notify('ИИ: распределение реплик')
        # The LLM labels immutable spans instead of rewriting the text. This makes
        # omissions, reordering and punctuation loss structurally impossible.
        units=dialogue_units(text)
        quoted=list(re.finditer(r'«[^»]*»|“[^”]*”|"[^"]*"',text))
        quoted_narration=set()
        if quoted and '—' not in text and text.count('«')==text.count('»') and text.count('“')==text.count('”') and text.count('"')%2==0:
            offset=0
            for i,t in enumerate(units):
                if not any(m.start()<offset+len(t) and m.end()>offset for m in quoted):quoted_narration.add(str(i+1))
                offset+=len(t)
        result=llm.ask(key,'Определи, кто произносит каждый пронумерованный фрагмент литературного текста. '
            'Авторские слова (например, «сказала Анна») читает Рассказчик. Прямую речь читает названный персонаж. '
            'Для всех id верни имя говорящего. Используй стабильные имена из characters. '
            'Формат {"labels":{"1":"Рассказчик","2":"Анна"},"descriptions":{"Анна":"женский голос"}}. '
            'Не переписывай текст, возвращай только метки по id.',
            dict(text=text,units=[dict(id=str(i+1),text=t) for i,t in enumerate(units)],characters=characters,previous=project.data.get('ai_context','')[-1500:]),notify)
        labels=result.get('labels',{})
        if isinstance(labels,dict):
            for key_id in quoted_narration:labels[key_id]='Рассказчик'
        if not isinstance(labels,dict) or any(not isinstance(labels.get(str(i+1)),str) or not labels[str(i+1)].strip() for i in range(len(units))):
            raise ValueError('ИИ пропустил метки реплик. Исходный текст сохранён; повторите анализ.')
        turns=[]
        for i,t in enumerate(units):
            speaker=labels[str(i+1)].strip()
            if turns and turns[-1]['speaker']==speaker:turns[-1]['text']+=t
            else:turns.append(dict(speaker=speaker,text=t))
        descriptions=result.get('descriptions',{})
        if not isinstance(descriptions,dict):descriptions={}
    else:descriptions={}
    for turn in turns:
        name=turn['speaker'].strip();turn['speaker']=name
        if name not in characters:characters[name]=dict(description=str(descriptions.get(name,'')),voice=None)
    voices=cfg.get('voices',[])
    unknown=[name for name in characters if not characters[name].get('voice') or characters[name]['voice'] not in {v['id'] for v in voices}]
    if cfg.get('dialogue') and unknown and voices:
        result=llm.ask(key,'Сопоставь каждого персонажа наиболее подходящему голосу по описаниям. Используй только указанные id голосов. '
            'Голоса могут повторяться, если персонажей больше, чем голосов. Формат {"assignments":{"имя персонажа":"id голоса"}}.',
            dict(characters={n:characters[n]['description'] for n in unknown},voices=[dict(id=v['id'],description=v.get('description','')) for v in voices]),notify)
        assignments=result.get('assignments',{})
        if not isinstance(assignments,dict):assignments={}
        for name in unknown:
            chosen=assignments.get(name)
            if not isinstance(chosen,str) or chosen not in valid_ids:
                if len(voices)==1:chosen=voices[0]['id']
                else:
                    retry=llm.ask(key,'Выбери один голос для персонажа. Ответ {"voice":"id"}. В voice разрешён только один из id списка voices, не имя персонажа и не описание. Учитывай пол, возраст и описание. Не используй занятые голоса, если есть подходящие свободные.',
                        dict(character=name,description=characters[name]['description'],voices=[dict(id=v['id'],description=v.get('description','')) for v in voices],used=[v.get('voice') for v in characters.values() if v.get('voice')]),notify)
                    chosen=retry.get('voice')
            if not isinstance(chosen,str) or chosen not in valid_ids:raise ValueError('ИИ не назначил допустимый голос персонажу '+name+'. Доступны: '+', '.join(sorted(valid_ids)))
            characters[name]['voice']=chosen
    prepared=dict(signature=stamp,text=text,turns=turns,model=key)
    with project.lock:
        seg['ai']=prepared;project.data['characters']=characters;project.data['ai_context']=text[-1500:];project.save()
    return prepared


def analyze_project(project,llm,cfg,stop,notify=lambda _:None,changed=lambda:None):
    from text_pipeline import prepare_text
    nodes=list(project.descendants(project.data['root']))
    for i,node in enumerate(nodes):
        if stop.is_set():break
        if not node['segments']:continue
        notify(f'Анализ главы {i+1}/{len(nodes)} · '+node['title'])
        if node.get('source_chunks'):
            pieces=[]
            for entry in node['source_chunks']:
                with project.path(entry['path']).open('rb') as stream:
                    stream.seek(entry['offset']);pieces.append(stream.read(entry['length']).decode('utf-8'))
            source=''.join(pieces)
        else:source=project.path(node['source_text']).read_text(encoding='utf-8') if node.get('source_text') else ''.join(s['text'] for s in node['segments'])
        prepared=list(prepare_text(project,source,{**cfg,'dialogue':True,'mode':'advanced'},llm,notify,stop))
        with project.lock:
            project.data.setdefault('trash',[]).extend(copy.deepcopy(node['segments']))
            node['segments']=prepared;node['character_ids']=list(dict.fromkeys(s['speaker_id'] for s in prepared));project.save()
        changed()
    return project.data.get('cast',{})


def synthesize_advanced(project,seg,engine,path,cfg,notify):
    import numpy as np
    import soundfile as sf
    from project import split_text
    if not hasattr(engine,'llm'):engine.llm=LanguageModel(engine.models)
    if engine.higgs is not None:engine.unload()
    prepared=prepare_segment(project,seg,cfg,engine.llm,notify)
    if cfg.get('engine')=='higgs' or any(v.get('engine')=='higgs' for v in cfg.get('voices',[])):engine.llm.unload()
    voices={v['id']:v for v in cfg.get('voices',[])}
    voices.update({k:v for k,v in {'irina':dict(engine='piper'),'denis':dict(engine='piper-denis'),'dmitri':dict(engine='piper-dmitri')}.items() if k not in voices})
    parts=[]
    for index,turn in enumerate(prepared['turns']):
        options=dict(cfg)
        if cfg.get('dialogue'):
            voice=project.data.get('characters',{}).get(turn['speaker'],{}).get('voice')
            options.update({k:v for k,v in voices.get(voice,{}).items() if k in ('engine','reference','reference_text','temperature','top_p')})
        if options.get('reference'):options['reference']=str(project.path(options['reference']))
        for chunk in split_text(turn['text'],cfg.get('max_chars',300),cfg.get('text_processing')):
            temp=path.with_name(path.stem+f'-turn-{len(parts)}.wav')
            try:
                engine.synthesize(chunk,temp,options,notify);data,_=sf.read(temp,dtype='float32');parts.append(data)
            finally:temp.unlink(missing_ok=True)
    if not parts:raise ValueError('Нет текста для озвучки')
    data=np.concatenate(parts);sf.write(path,data,24000,subtype='PCM_16')
    return len(data)/24000,[float(abs(p).max(initial=0)) for p in np.array_split(data,180)],prepared['text']
