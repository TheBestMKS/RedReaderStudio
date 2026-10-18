from text_processing import DEFAULT_PROCESSING
from project import Project,split_text

def test_new_projects_remove_paragraphs_after_priority_split(tmp_path):
    p=Project.create(tmp_path/'book','Book')
    opts=p.data['settings']['text_processing']
    assert opts['remove_linebreaks'] is True
    text='Первое предложение.\nКоротко. Второе предложение.\n'
    raw=split_text(text,35,{**opts,'remove_linebreaks':False})
    result=split_text(text,35,opts)
    assert result==[x.replace('\n',' ') for x in raw]
    assert all('\n' not in x and '\r' not in x and len(x)<=35 for x in result)

def test_explicit_paragraph_preservation_survives_reopen(tmp_path):
    p=Project.create(tmp_path/'book','Book')
    p.data['settings']['text_processing']['remove_linebreaks']=False
    p.save();p=Project.open(p.folder)
    assert p.data['settings']['text_processing']['remove_linebreaks'] is False
    assert '\n' in ''.join(split_text('Первое предложение.\nВторое.',35,p.data['settings']['text_processing']))
import threading
import numpy as np
from test_project import FakeEngine
from engine import run_job
from audio_edit import pauses
from characters import merge_character,registry,label_chapter
from text_pipeline import prepare_text,DEFAULT_ORDER


def test_retry_errors_skips_and_queue_continues(tmp_path):
    p=Project.create(tmp_path,'Retry');p.append_text(p.data['root'],'Bad',300);p.append_text(p.data['root'],'Good',300)
    p.data['settings'].update(error_retries=2,auto_cut_pauses=False)
    class Engine(FakeEngine):
        def synthesize(self,text,path,settings,notify):
            if text=='Bad':self.calls.append(text);raise ValueError('failure')
            return super().synthesize(text,path,settings,notify)
    engine=Engine();rows=p.rows(p.data['root']);run_job(p,engine,[s['id'] for _,s in rows],p.data['settings'],threading.Event())
    assert engine.calls==['Bad']*3+['Good']
    assert rows[0][1]['status']=='error' and p.has_audio(rows[1][1])
    assert p.data['job']['status']=='completed' and p.data['job']['skipped']==[rows[0][1]['id']]


def test_quality_retries_commit_best_not_last(tmp_path):
    p=Project.create(tmp_path,'Quality');p.append_text(p.data['root'],'Text',300);s=p.rows(p.data['root'])[0][1]
    p.data['settings'].update(quality_retries=2,auto_cut_pauses=False)
    class Engine(FakeEngine):
        def verify(self,*args):return dict(passed=False,coverage=[.4,.8,.6][len(self.calls)-1])
    engine=Engine();run_job(p,engine,[s['id']],p.data['settings'],threading.Event())
    assert len(engine.calls)==3 and s['verification']['coverage']==.8
    assert s['audio']==s['attempts'][1]['audio']
    assert all(p.path(a['audio']).is_file() for a in s['attempts'])


def test_pause_guards_never_cut_protected_edges():
    data=np.concatenate([np.full(2400,800),np.zeros(28800),np.full(2400,800)]).astype('int16')
    assert pauses(data,1000,keep_start_ms=300,keep_end_ms=300)==[(9600,24000)]
    assert pauses(data,1000,keep_start_ms=700,keep_end_ms=700)==[]


def test_stable_character_reveal_and_ambiguous_alias(tmp_path):
    p=Project.create(tmp_path,'Cast')
    key=merge_character(p,'unknown1',{'gender':'мужской','aliases':['незнакомец']})
    assert registry(p)[key]['name']=='Персонаж 1'
    assert merge_character(p,'незнакомец',{})==key
    assert merge_character(p,'Пётр',{'existing_id':key,'name':'Пётр Иванов','full_name':'Пётр Иванов','aliases':['Петя']})==key
    assert merge_character(p,'Петя',{})==key
    p.save();assert registry(Project.open(tmp_path))[key]['name']=='Пётр Иванов'


def test_roles_before_fragmentation_no_omission_or_reordering(tmp_path):
    p=Project.create(tmp_path,'Roles');text='Автор. «'+'Реплика персонажа, '*50+'»'
    class LLM:
        def ask(self,key,instruction,payload,notify):
            return dict(labels={v['id']:('narrator' if v['text'].strip()=='Автор.' else 'anna') for v in payload['units']},characters={'anna':{'name':'Анна','gender':'женский'}})
    cfg={**p.data['settings'],'mode':'advanced','dialogue':True,'max_chars':100}
    parts=list(prepare_text(p,text,cfg,LLM()))
    assert ''.join(s['text'] for s in parts)==text
    assert all(len(s['text'])<=100 for s in parts)
    assert parts[0]['speaker_id']=='narrator' and len({s['speaker_id'] for s in parts[1:]})==1
    assert all(c['voice'] is None for c in registry(p).values())


def test_processing_order_changes_result_and_preserves_bound(tmp_path):
    p=Project.create(tmp_path,'Order');cfg=p.data['settings'];cfg['max_chars']=20
    cfg['text_processing'].update(remove_symbols=True,symbols='@',replacements=[['A','@'+('B'*40)]])
    a=list(prepare_text(p,'A',cfg));assert ''.join(s['text'] for s in a)=='B'*40
    cfg['text_processing']['order']=['ai','roles','symbols','cleanup','split','replace']
    b=list(prepare_text(p,'A',cfg));assert ''.join(s['text'] for s in b)=='@'+'B'*40
    assert all(len(s['text'])<=20 for s in b)

def test_edge_pause_trimming_threshold_padding_and_restoration(tmp_path):
    from audio_edit import piece,modify,edit_segment,read
    from test_project import wav
    import wave
    p=Project.create(tmp_path,'Edges');p.append_text(p.data['root'],'Слово.',300);s=p.rows(p.data['root'])[0][1]
    tone=np.full(4800,12000,dtype='int16');data=np.concatenate([np.zeros(24000,dtype='int16'),tone,np.zeros(21600,dtype='int16')])
    path=p.path('source.wav')
    with wave.open(str(path),'wb') as f:f.setparams((1,2,24000,0,'NONE',''));f.writeframes(data.astype('<i2').tobytes())
    p.commit(s['id'],'source.wav',len(data)/24000,[],dict(passed=True),{})
    cfg=p.data['settings'];edit_segment(p,s['id'],'detect',detect_internal=False,trim_edges=cfg)
    result=read(p.path(s['audio']))
    assert len(result)==4800+2*6000
    assert np.array_equal(result[6000:10800],tone)
    inactive=[part for part in s['audio_parts'] if not part['enabled']]
    assert [(v['start'],v['end']) for v in inactive]==[(1200,19200),(33600,49200)]
    edit_segment(p,s['id'],'restore');assert np.array_equal(read(p.path(s['audio'])),data)
    cfg={**cfg,'start_pause_threshold_ms':1100,'trim_end_pause':False}
    edit_segment(p,s['id'],'detect',detect_internal=False,trim_edges=cfg)
    assert np.array_equal(read(p.path(s['audio'])),data)
