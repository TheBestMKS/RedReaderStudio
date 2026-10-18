"""Non-destructive audio pieces in source-frame coordinates."""
import copy
import uuid
import wave
import numpy as np


def piece(start,end,enabled=True,kind='audio'):
    return dict(id=uuid.uuid4().hex,start=int(start),end=int(end),enabled=enabled,kind=kind)


def read(path):
    with wave.open(str(path),'rb') as f:
        if (f.getnchannels(),f.getsampwidth(),f.getframerate())!=(1,2,24000):
            raise ValueError('Редактирование поддерживает WAV 24 кГц, моно, PCM 16-bit')
        return np.frombuffer(f.readframes(f.getnframes()),dtype='<i2').copy()


def pauses(data,threshold_ms,keep=960,keep_start_ms=None,keep_end_ms=None):
    hop=240;minimum=round(threshold_ms*24);runs=[];start=None
    for at in range(0,len(data),hop):
        quiet=np.max(np.abs(data[at:at+hop].astype(np.int32)),initial=0)<=130
        if quiet and start is None:start=at
        if not quiet and start is not None:
            if at-start>minimum:runs.append((start,at))
            start=None
    if start is not None and len(data)-start>minimum:runs.append((start,len(data)))
    if keep_start_ms is not None or keep_end_ms is not None:
        left=round(max(0,keep_start_ms or 0)*24);right=round(max(0,keep_end_ms or 0)*24)
        return [(a+left,b-right) for a,b in runs if b-a>left+right]
    return [(a+min(keep,(b-a)//4),b-min(keep,(b-a)//4)) for a,b in runs]


def subpiece(p,start,end):
    part=piece(start,end,p['enabled'],p['kind'])
    if p.get('replacement_frames'):
        # Preserve total replacement duration when manually splitting a pause.
        length=p['end']-p['start']
        part['replacement_frames']=round((end-p['start'])*p['replacement_frames']/length)-round((start-p['start'])*p['replacement_frames']/length)
    return part


def cut(parts,start,end,delete=False,disable=False):
    result=[]
    for p in parts:
        a=max(start,p['start']);b=min(end,p['end'])
        if a>=b:result.append(p);continue
        if p['start']<a:result.append(subpiece(p,p['start'],a))
        if not delete:result.append(piece(a,b,False,'pause') if disable else subpiece(p,a,b))
        if b<p['end']:result.append(subpiece(p,b,p['end']))
    return result


def pause_move_bounds(parts,part_id):
    index=next((i for i,p in enumerate(parts) if p['id']==part_id),None)
    if index is None:raise ValueError('Пауза не найдена')
    p=parts[index]
    if p['enabled'] or p['kind']!='pause':raise ValueError('Выберите отключённую паузу')
    left=right=index
    while left>0 and parts[left-1]['enabled'] and parts[left-1]['end']==parts[left]['start']:left-=1
    while right+1<len(parts) and parts[right+1]['enabled'] and parts[right]['end']==parts[right+1]['start']:right+=1
    return parts[left]['start']-p['start'],parts[right]['end']-p['end']


def move_pause(parts,part_id,delta):
    low,high=pause_move_bounds(parts,part_id);delta=max(low,min(high,int(delta)))
    if not delta:return parts
    selected=next(p for p in parts if p['id']==part_id)
    start,end=selected['start']+delta,selected['end']+delta
    restored=[{**p,'id':uuid.uuid4().hex,'enabled':True,'kind':'audio','replacement_frames':0} if p['id']==part_id else p for p in parts]
    result=cut(restored,start,end,delete=True)
    moved={**selected,'start':start,'end':end}
    result.append(moved);result.sort(key=lambda p:p['start'])
    return result


def modify(parts,action,ids=None,start=None,end=None,data=None,threshold_ms=1000,replacement_ms=0,delta_ms=0,delta_frames=None,keep_start_ms=None,keep_end_ms=None,trim_edges=None,detect_internal=True):
    parts=copy.deepcopy(parts);ids=set(ids or [])
    if action=='detect':
        edge=trim_edges or {}
        leading=trailing=0
        hop=240
        for at in range(0,len(data),hop):
            if np.max(np.abs(data[at:at+hop].astype(np.int32)),initial=0)>130:break
            leading=min(len(data),at+hop)
        for at in range(len(data),0,-hop):
            if np.max(np.abs(data[max(0,at-hop):at].astype(np.int32)),initial=0)>130:break
            trailing=len(data)-max(0,at-hop)
        intervals=[]
        if detect_internal:
            for a,b in pauses(data,threshold_ms,0 if replacement_ms else 960,keep_start_ms,keep_end_ms):
                # Edge rules own their respective silence, even when below their threshold.
                if edge and ((edge.get('trim_start_pause') and a<leading) or (edge.get('trim_end_pause') and b>len(data)-trailing)):continue
                intervals.append((a,b))
        for side,length,start in [('start',leading,0),('end',trailing,len(data)-trailing)]:
            if edge.get('trim_'+side+'_pause') and length>edge.get(side+'_pause_threshold_ms',800)*24:
                left=edge.get(side+'_pause_keep_start_ms',50 if side=='start' else 200)*24
                right=edge.get(side+'_pause_keep_end_ms',200 if side=='start' else 50)*24
                if length>left+right:intervals.append((round(start+left),round(start+length-right)))
        for a,b in intervals:parts=cut(parts,a,b,disable=True)
        merged=[]
        for p in parts:
            if merged and not p['enabled'] and p['kind']=='pause' and not merged[-1]['enabled'] and merged[-1]['kind']=='pause' and merged[-1]['end']==p['start']:
                merged[-1]['end']=p['end']
            else:merged.append(p)
        parts=merged
        for p in parts:
            if p['kind']=='pause' and not p['enabled']:p['replacement_frames']=max(0,round(replacement_ms*24))
    elif action=='move_pause':
        if len(ids)!=1:raise ValueError('Выберите одну отключённую паузу')
        parts=move_pause(parts,next(iter(ids)),delta_frames if delta_frames is not None else round(delta_ms*24))
    elif action=='range_delete':parts=cut(parts,start,end,delete=True)
    elif action=='split':
        for i,p in enumerate(parts):
            if p['start']<start<p['end']:
                parts[i:i+1]=[subpiece(p,p['start'],start),subpiece(p,start,p['end'])];break
    elif action=='toggle':
        enable=any(not p['enabled'] for p in parts if p['id'] in ids)
        for p in parts:
            if p['id'] in ids:p['enabled']=enable
    elif action=='delete':parts=[p for p in parts if p['id'] not in ids]
    elif action=='merge':
        indexes=[i for i,p in enumerate(parts) if p['id'] in ids]
        if len(indexes)<2:raise ValueError('Выберите минимум две соседние части через Ctrl или Shift')
        chosen=[parts[i] for i in indexes]
        if indexes!=list(range(indexes[0],indexes[-1]+1)) or any(a['end']!=b['start'] for a,b in zip(chosen,chosen[1:])):
            raise ValueError('Объединять можно только соседние части без удалённых промежутков')
        if len({p['enabled'] for p in chosen})!=1:raise ValueError('Перед объединением установите одинаковую активность частей')
        merged=piece(chosen[0]['start'],chosen[-1]['end'],chosen[0]['enabled'])
        if not merged['enabled']:merged.update(kind='pause',replacement_frames=sum(p.get('replacement_frames',0) for p in chosen))
        parts[indexes[0]:indexes[-1]+1]=[merged]
    return parts


def edit_segment(project,seg_id,action,**kwargs):
    _,seg=project.find_segment(seg_id)
    source=seg.get('source_audio') or seg.get('audio')
    if not source:raise ValueError('Сначала сгенерируйте аудио')
    data=read(project.path(source))
    original_parts=seg.get('audio_parts')
    parts=copy.deepcopy(original_parts if original_parts is not None else [piece(0,len(data))])
    if original_parts is None and 'whole' in (kwargs.get('ids') or []):kwargs['ids']=[p['id'] for p in parts]
    history=copy.deepcopy(seg.get('audio_edit_history') or [])
    if action=='undo':
        if not history:raise ValueError('Нет действий для отмены')
        changed=history.pop()
    elif action=='restore':
        history.append(parts);changed=[piece(0,len(data))]
    else:
        changed=modify(parts,action,data=data,**kwargs)
        if changed==parts:return
        history.append(parts)
    usable=[p for p in changed if output_length(p)>0]
    audio=np.concatenate([data[p['start']:p['end']] if p['enabled'] else np.zeros(output_length(p),dtype='<i2') for p in usable]) if usable else np.empty(0,dtype='<i2')
    relative=f'audio/{seg_id}/{uuid.uuid4().hex}.wav'
    dest=project.path(relative);dest.parent.mkdir(exist_ok=True,parents=True)
    with wave.open(str(dest),'wb') as f:
        f.setparams((1,2,24000,0,'NONE','not compressed'));f.writeframes(audio.astype('<i2').tobytes())
    verification=copy.deepcopy(seg.get('source_verification') or seg.get('verification'))
    source_verification=copy.deepcopy(verification)
    if verification:
        words=[];offset=0
        for p in usable:
            for word in (verification.get('words',[]) if p['enabled'] else []):
                a=max(word['start']*24000,p['start']);b=min(word['end']*24000,p['end'])
                if a<b:words.append({**word,'start':(offset+a-p['start'])/24000,'end':(offset+b-p['start'])/24000})
            offset+=output_length(p)
        verification['words']=words
        if action!='detect':verification.update(manual=False,passed=False,audio_edited=True)
    peaks=[float(np.abs(chunk.astype(np.float32)).max(initial=0)/32768) for chunk in np.array_split(audio,160)]
    with project.lock:
        seg.update(source_audio=source,source_frames=len(data),audio_parts=changed,audio_edit_history=history[-100:],
                   source_peaks=seg.get('source_peaks') or seg.get('peaks',[]),source_verification=source_verification,verification=verification,audio=relative if usable else source,
                   muted=not bool(usable),duration=len(audio)/24000,peaks=peaks)
        if action!='detect':seg['status']='review'
        project.data.pop('preview_state',None);project.save()


def output_length(p):
    return p['end']-p['start'] if p['enabled'] else p.get('replacement_frames',0)


def source_to_output(seg,frame):
    elapsed=0
    for p in seg.get('audio_parts',[]):
        length=output_length(p)
        if frame<p['start']:return elapsed/24000
        if frame<=p['end']:return (elapsed+(frame-p['start'])*length/max(1,p['end']-p['start']))/24000
        elapsed+=length
    return elapsed/24000


def output_to_source(seg,seconds):
    frame=seconds*24000
    for p in seg.get('audio_parts',[]):
        length=output_length(p)
        if not length:continue
        if frame<=length:return p['start']+frame*(p['end']-p['start'])/length
        frame-=length
    return seg.get('source_frames',0)
