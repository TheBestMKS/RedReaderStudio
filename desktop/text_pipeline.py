"""Ordered text preparation shared by document imports and API jobs."""
import re
from text_processing import clean_text,iter_fragments

DEFAULT_ORDER=['ai','roles','replace','symbols','cleanup','split']

def prepare_text(project,text,cfg,llm=None,notify=lambda _:None,stop=None):
    from project import segment
    options=cfg.get('text_processing',{});order=options.get('order',DEFAULT_ORDER)
    if len(order)!=len(DEFAULT_ORDER) or set(order)!=set(DEFAULT_ORDER) or order.index('roles')>order.index('split'):
        raise ValueError('Недопустимый порядок обработки: роли должны определяться до фрагментации')
    turns=[dict(text=text,speaker_id='narrator')]
    for stage in order:
        if stop and stop.is_set():raise InterruptedError('Импорт остановлен')
        if stage=='ai' and cfg.get('mode')=='advanced' and cfg.get('correct_text'):
            corrected=[]
            for turn in turns:
                # Bounded LLM windows, independent of the final TTS fragment size.
                for part in iter_fragments(turn['text'],2400,{'priorities':['paragraph','sentence','comma','space']}):
                    response=llm.ask(cfg.get('llm_model','qwen2.5-3b'),'Исправь только явные грамматические и смысловые ошибки. Сохрани имена, все факты, реплики, кавычки и абзацы. Ответ {"text":"исправленный текст"}.',dict(text=part),notify)
                    value=response.get('text')
                    if not isinstance(value,str) or not value.strip() or not .5<=len(value)/max(1,len(part))<=2:raise ValueError('Некорректный результат коррекции')
                    corrected.append({**turn,'text':value})
            turns=corrected
        elif stage=='roles' and cfg.get('mode')=='advanced' and cfg.get('dialogue'):
            from characters import label_chapter
            turns=label_chapter(project,''.join(t['text'] for t in turns),llm,cfg,notify,stop)
        elif stage=='replace':
            for turn in turns:
                for source,target in options.get('replacements',[]):
                    if source:turn['text']=turn['text'].replace(source,target)
        elif stage=='symbols':
            for turn in turns:turn['text']=clean_text(turn['text'],dict(remove_symbols=options.get('remove_symbols',False),symbols=options.get('symbols','')),remove_lines=False)
        elif stage=='cleanup':
            for turn in turns:turn['text']=clean_text(turn['text'],dict(space_cycles=options.get('space_cycles',3),paragraph_cycles=options.get('paragraph_cycles',3)),remove_lines=False)
        elif stage=='split':
            turns=[{**turn,'text':part} for turn in turns for part in iter_fragments(turn['text'],cfg.get('max_chars',300),dict(priorities=options.get('priorities',['paragraph','sentence','comma','off'])))]
    # Later substitutions may enlarge text; enforce the engine's hard limit again.
    for turn in turns:
        for part in iter_fragments(turn['text'],cfg.get('max_chars',300),dict(priorities=options.get('priorities',['paragraph','sentence','comma','off']),remove_linebreaks=options.get('remove_linebreaks',True))):
            if part.strip():
                result=segment(part);result.update(speaker_id=turn['speaker_id'],text_prepared=True)
                yield result
