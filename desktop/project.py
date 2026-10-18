"""Portable, atomic project storage. All media paths are relative to the project."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import struct
import threading
import uuid
import wave
from pathlib import Path

VERSION = "0.7.0"
RIFF_LIMIT = 2**32 - 1


def synchronized(fn):
    from functools import wraps
    @wraps(fn)
    def call(self, *args, **kwargs):
        with self.lock:return fn(self, *args, **kwargs)
    return call


def uid():
    return uuid.uuid4().hex


def fingerprint(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


from text_processing import iter_fragments, DEFAULT_PROCESSING


def iter_split_text(text, limit=300, options=None):
    yield from iter_fragments(text,limit,options)


def split_text(text, limit=300, options=None):
    return list(iter_split_text(text,limit,options))


def segment(text):
    return dict(id=uid(), text=text, status="pending", audio=None, duration=0,
                peaks=[], verification=None, versions=[], error="")


class Project:
    def __init__(self, folder, data):
        self.folder = Path(folder)
        self.data = data
        self.lock = threading.RLock()
        self._audio_cache = {}
        self._saver = None

    @classmethod
    def create(cls, folder, title):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        if (folder / "project.json").exists():
            raise ValueError("В этой папке уже есть проект")
        root = uid()
        obj = cls(folder, dict(version=VERSION, root=root, nodes=[dict(id=root, parent=None, title=title, segments=[])],
                               settings=dict(engine="piper", max_chars=300, pause_ms=160, temperature=0.7,
                                             top_p=0.95, reference="", reference_text="", language="ru",auto_cut_pauses=True,max_pause_ms=1000,text_processing=copy.deepcopy(DEFAULT_PROCESSING)), job=None))
        from language_ai import DEFAULT_AI
        for key,value in DEFAULT_AI.items():obj.data["settings"].setdefault(key,copy.deepcopy(value))
        obj.save()
        return obj

    @classmethod
    def open(cls, folder):
        folder = Path(folder)
        obj = cls(folder, json.loads((folder / "project.json").read_text(encoding="utf-8")))
        if obj.data.get("version") not in ('0.2.0', '0.3.0', '0.3.1', '0.3.2', '0.4.0', '0.5.0', '0.6.0', VERSION):
            raise ValueError("Неподдерживаемая версия проекта")
        if obj.data.get('version')=='0.2.0':
            backup=folder/'project-v0.2.0.backup.json'
            if not backup.exists():
                import shutil
                shutil.copy2(folder/'project.json',backup)
        obj.data['version'] = VERSION
        obj.data['settings'].setdefault('auto_cut_pauses',True)
        obj.data['settings'].setdefault('max_pause_ms',1000)
        obj.data['settings'].setdefault('max_chars',300)
        obj.data['settings'].setdefault('text_processing',copy.deepcopy(DEFAULT_PROCESSING))
        for n in obj.data["nodes"]:
            for s in n["segments"]:
                if s["status"] in ("generating", "verifying"):
                    s["status"] = "pending"
                if s.get("audio") and not obj.path(s["audio"]).is_file():
                    s.update(audio=None, status="pending", duration=0, peaks=[])
        if obj.data.get("job") and obj.data["job"]["status"] == "running":
            obj.data["job"]["status"] = "paused"
        from language_ai import DEFAULT_AI
        for key,value in DEFAULT_AI.items():obj.data["settings"].setdefault(key,copy.deepcopy(value))
        obj.save()
        return obj

    def generation_rows(self,node_id):
        """Selected section and every subsequent section in depth-first project order."""
        ordered=list(self.descendants(self.data['root']))
        ids=[n['id'] for n in ordered]
        if node_id not in ids:return []
        return [(n,s) for n in ordered[ids.index(node_id):] for s in n['segments']]

    def path(self, relative):
        path = (self.folder / relative).resolve()
        if not path.is_relative_to(self.folder.resolve()):
            raise ValueError("Файл вне папки проекта")
        return path

    def enable_background_save(self):
        if self._saver is None:
            from background_save import BackgroundSave
            self._saver = BackgroundSave(self)

    def flush_saves(self):
        if self._saver:self._saver.flush()

    def close_saves(self):
        if self._saver:
            self._saver.close();self._saver=None

    def save(self):
        if self._saver:
            self._saver.request();return
        with self.lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            temp = self.folder / "project.json.tmp"
            with temp.open("w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, self.folder / "project.json")

    def node(self, node_id):
        return next(n for n in self.data["nodes"] if n["id"] == node_id)

    def export_name(self, node_id):
        """Readable Windows filename including the project and ancestor titles."""
        nodes={n['id']:n for n in self.data['nodes']}
        titles=[]
        while node_id is not None:
            node=nodes[node_id]
            title=re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', node['title']).strip(' .') or 'Раздел'
            titles.append(title);node_id=node['parent']
        titles.reverse()
        name=' — '.join(titles)
        if len(name)>180:
            # Keep both project and deepest section visible when the full path is long.
            if len(titles)>20:titles=titles[:1]+['…']+titles[-18:]
            width=max(1,(180-3*(len(titles)-1))//len(titles))
            name=' — '.join(t if len(t)<=width else t[:max(0,width-1)]+'…' for t in titles)
        if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)',name,re.I):name='_'+name
        return name.rstrip(' .')

    @synchronized
    def add_node(self, parent, title):
        self.node(parent)
        n = dict(id=uid(), parent=parent, title=title, segments=[])
        self.data["nodes"].append(n)
        self.save()
        return n

    def descendants(self, node_id):
        # Build adjacency once, rather than scan every node for every chapter.
        nodes = list(self.data['nodes'])
        by_id = {n['id']:n for n in nodes}
        children = {}
        for node in nodes:children.setdefault(node['parent'], []).append(node)
        stack = [by_id[node_id]]
        while stack:
            node = stack.pop();yield node
            stack.extend(reversed(children.get(node['id'], [])))

    def all_counts(self):
        ordered = list(self.descendants(self.data['root']))
        counts = {}
        for node in reversed(ordered):
            own = [0,0,0]
            for seg in node['segments']:
                ready = self.has_audio(seg)
                own[0] += 1;own[1] += ready
                own[2] += ready and bool((seg.get('verification') or {}).get('manual'))
            previous = counts.setdefault(node['id'], [0,0,0])
            for i in range(3):previous[i] += own[i]
            if node['parent'] is not None:
                parent = counts.setdefault(node['parent'], [0,0,0])
                for i in range(3):parent[i] += previous[i]
        return counts

    def attach_import_batch(self, nodes, target, segments):
        with self.lock:
            self.node(target)  # The target must still exist.
            self.data['nodes'].extend(nodes)
            if segments:self.node(target)['segments'].extend(segments)
            self.save()

    def rows(self, node_id, recursive=True):
        nodes = self.descendants(node_id) if recursive else [self.node(node_id)]
        return [(n, s) for n in nodes for s in n["segments"]]

    def rows_for_nodes(self,node_ids):
        selected=set(node_ids);included=set();rows=[]
        for node in self.descendants(self.data['root']):
            if node['id'] in selected or node['parent'] in included:
                included.add(node['id']);rows.extend((node,s) for s in node['segments'])
        return rows

    def find_segment(self, seg_id):
        for n, s in self.rows(self.data["root"]):
            if s["id"] == seg_id:
                return n, s
        raise ValueError("Фрагмент не найден")

    @synchronized
    def append_text(self, node_id, text, limit):
        parts = split_text(text, limit, self.data["settings"].get("text_processing"))
        if not parts:
            raise ValueError("Текст пуст")
        self.node(node_id)["segments"].extend(segment(t) for t in parts)
        self.save()

    @synchronized
    def edit(self, seg_id, text):
        if not text.strip():
            raise ValueError("Фрагмент не может быть пустым")
        _, s = self.find_segment(seg_id)
        if text != s["text"]:
            self.archive(s)
            for key in ("source_audio","source_frames","source_verification","source_peaks","audio_parts","audio_edit_history","muted"):s.pop(key,None)
            s.pop("spoken_text",None);s.pop("ai",None)
            s.update(text=text, status="pending", audio=None, duration=0, peaks=[], verification=None)
            self.save()

    @staticmethod
    def archive(s):
        if s.get("audio"):
            previous={k:copy.deepcopy(s.get(k)) for k in ("text", "audio", "duration", "peaks", "verification", "status", "source_audio", "source_frames", "source_verification", "source_peaks", "audio_parts", "audio_edit_history", "muted", "spoken_text", "ai") if k in s}
            previous['status']='ready' if (s.get('verification') or {}).get('passed') else 'review'
            s['versions'].append(previous)

    @synchronized
    def split(self, seg_id, position):
        n, s = self.find_segment(seg_id)
        if not s["text"][:position].strip() or not s["text"][position:].strip():
            raise ValueError("Поставьте курсор между непустыми частями текста")
        parts = [segment(s["text"][:position]), segment(s["text"][position:])]
        for part in parts:
            for key in ("speaker_id","text_prepared"):
                if key in s:part[key]=s[key]
        # Keep the original audio and transcript in the first child's history.
        parts[0]["versions"] = copy.deepcopy(s["versions"])
        if s.get("audio"):
            parts[0]["versions"].append({k: copy.deepcopy(s.get(k)) for k in
                                        ("text", "audio", "duration", "peaks", "verification", "status", "source_audio", "source_frames", "source_verification", "source_peaks", "audio_parts", "audio_edit_history", "muted", "spoken_text", "ai") if k in s})
        index = n["segments"].index(s)
        n["segments"][index:index + 1] = parts
        job = self.data.get('job')
        if job and seg_id in job['ids']:
            index = job['ids'].index(seg_id)
            job['ids'][index:index+1] = [part['id'] for part in parts]
            job['done'] = [value for value in job['done'] if value != seg_id]
        self.save()

    @synchronized
    def move(self, ids, target):
        destination = self.node(target)
        selected = [(n, s) for n, s in self.rows(self.data["root"]) if s["id"] in ids]
        for n, s in selected:
            n["segments"].remove(s)
            destination["segments"].append(s)
        self.save()

    def commit(self, seg_id, relative, duration, peaks, verification, settings):
        with self.lock:
            _, s = self.find_segment(seg_id)
            self.archive(s)
            for key in ("source_audio","source_frames","source_verification","source_peaks","audio_parts","audio_edit_history","muted"):s.pop(key,None)
            s.update(audio=relative, duration=duration, peaks=peaks, verification=verification,
                     status="ready" if verification["passed"] else "review", error="",
                     text_hash=fingerprint(s["text"]), generation=copy.deepcopy(settings))
            self.save()

    def has_audio(self, s):
        if not s.get('audio'):
            return False
        try:
            path=self.path(s['audio']);stat=path.stat();key=(str(path),stat.st_size,stat.st_mtime_ns)
            if key not in self._audio_cache:
                with wave.open(str(path), 'rb') as source:
                    self._audio_cache[key]=source.getnframes()>0
            return self._audio_cache[key]
        except (OSError, ValueError, wave.Error, EOFError):
            return False

    def counts(self, node_id):
        rows = self.rows(node_id)
        return len(rows), sum(self.has_audio(s) for _,s in rows), sum(
            self.has_audio(s) and bool((s.get('verification') or {}).get('manual')) for _,s in rows)

    @synchronized
    def remove_segments(self, ids):
        ids = set(ids)
        removed = []
        for n in self.data['nodes']:
            removed.extend(copy.deepcopy(s) for s in n['segments'] if s['id'] in ids)
            n['segments'] = [s for s in n['segments'] if s['id'] not in ids]
        self.data.setdefault('trash', []).extend(removed)
        self._prune_job()
        self.save()

    def _prune_job(self):
        available = {s['id'] for _,s in self.rows(self.data['root'])}
        job = self.data.get('job')
        if job:
            for key in ('ids','done'):
                job[key] = [sid for sid in job.get(key,[]) if sid in available]

    @synchronized
    def rebuild_segments(self, ids, limit):
        # Only adjacent selections within the same node are merged. Unselected
        # fragments, section boundaries and the exact text order are preserved.
        ids = set(ids)
        for node in self.data['nodes']:
            result, run = [], []
            def flush():
                if not run:return
                text = ''.join(s['text'] for s in run)
                self.data.setdefault('trash', []).extend(copy.deepcopy(run))
                from text_pipeline import prepare_text
                cfg={**self.data['settings'],'mode':'simple','max_chars':limit}
                prepared=list(prepare_text(self,text,cfg))
                for part in prepared:
                    part['speaker_id']=run[0].get('speaker_id','narrator')
                result.extend(prepared)
                run.clear()
            for s in node['segments']:
                if s['id'] in ids:
                    if run and run[-1].get('speaker_id')!=s.get('speaker_id'):flush()
                    run.append(s)
                else:flush();result.append(s)
            flush()
            node['segments'] = result
        self._prune_job();self.save()

    @synchronized
    def duplicate_node(self, node_id):
        originals = list(self.descendants(node_id))
        mapping = {n['id']:uid() for n in originals}
        copies = []
        for node in originals:
            clone = copy.deepcopy(node)
            clone['id'] = mapping[node['id']]
            clone['parent'] = mapping.get(node['parent'], node['parent'])
            for s in clone['segments']:s['id'] = uid()
            copies.append(clone)
        copies[0]['title'] += ' — копия'
        if node_id == self.data['root']:
            copies[0]['parent'] = self.data['root']
        self.data['nodes'].extend(copies);self.save()
        return copies[0]

    @synchronized
    def remove_node(self, node_id):
        if node_id == self.data['root']:
            raise ValueError('Для удаления корня используйте удаление проекта')
        removed = list(self.descendants(node_id))
        ids = {n['id'] for n in removed}
        self.data.setdefault('deleted_nodes', []).extend(copy.deepcopy(removed))
        self.data['nodes'] = [n for n in self.data['nodes'] if n['id'] not in ids]
        self._prune_job();self.save()

    def export_zip(self, node_id, destination):
        import zipfile
        destination = Path(destination)
        if destination.resolve().is_relative_to(self.folder.resolve()):
            raise ValueError('Сохраните ZIP вне папки проекта')
        data = copy.deepcopy(self.data)
        data['nodes'] = copy.deepcopy(list(self.descendants(node_id)))
        data['nodes'][0]['parent'] = None
        data.update(root=node_id, job=None)
        for key in ('preview_state','trash','deleted_nodes'):data.pop(key,None)
        files = set()
        for n in data['nodes']:
            if n.get('source_text') and self.path(n['source_text']).is_file():files.add(n['source_text'])
            for entry in n.get('source_chunks',[]):
                if self.path(entry['path']).is_file():files.add(entry['path'])
            for s in n['segments']:
                for item in [s]+s.get('versions',[])+s.get('attempts',[]):
                    for key in ('audio','source_audio'):
                        if item.get(key) and self.path(item[key]).is_file():files.add(item[key])
        for key in ('reference','cover'):
            relative=data['settings'].get(key)
            if relative and self.path(relative).is_file():files.add(relative)
        for voice in data['settings'].get('voices',[]):
            relative=voice.get('reference')
            if relative and self.path(relative).is_file():files.add(relative)
        temp = destination.with_name(destination.name+'.tmp')
        try:
            with zipfile.ZipFile(temp,'w',compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('project.json',json.dumps(data,ensure_ascii=False,indent=2))
                for relative in sorted(files):archive.write(self.path(relative),relative.replace('\\','/'))
            os.replace(temp,destination)
        finally:temp.unlink(missing_ok=True)

    def export(self, node_id, destination, pause_ms=160, allow_partial=True, segment_ids=None):
        """Stream chunks to disk (bounded memory), atomically replace final WAV."""
        rows = self.rows(node_id)
        if segment_ids is not None:
            selected = set(segment_ids)
            rows = [(n,s) for n,s in rows if s['id'] in selected]
        if not rows:
            raise ValueError("Нет фрагментов для экспорта")
        incomplete = [s for _, s in rows if not self.has_audio(s)]
        if incomplete and not allow_partial:
            raise ValueError(f"Не сгенерированы {len(incomplete)} фрагм.")
        usable = [s for _, s in rows if self.has_audio(s) and not s.get("muted")]
        if not usable:
            raise ValueError("Нет готового аудио")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(destination.name + ".tmp")
        try:
            estimated_bytes = 44 + sum(round(s.get('duration', 0)*24000)*2 for s in usable)
            estimated_bytes += max(0,len(usable)-1)*int(24000*pause_ms/1000)*2
            if estimated_bytes >= RIFF_LIMIT:
                self._write_rf64(usable, temp, pause_ms)
            else:
                self._write_wav(usable, temp, pause_ms)
            import soundfile as sf
            if not temp.is_file() or temp.stat().st_size == 0 or sf.info(str(temp)).frames <= 0:
                raise ValueError('Экспорт не содержит аудио; существующий файл сохранён')
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)
        return destination

    def _write_rf64(self, usable, destination, pause_ms):
        import numpy as np
        import soundfile as sf
        with sf.SoundFile(str(destination), 'w', samplerate=24000, channels=1, subtype='PCM_16', format='RF64') as out:
            for i,s in enumerate(usable):
                if i:out.write(np.zeros(int(24000*pause_ms/1000), dtype=np.int16))
                with wave.open(str(self.path(s['audio'])), 'rb') as src:
                    if (src.getnchannels(),src.getsampwidth(),src.getframerate()) != (1,2,24000):
                        raise ValueError('RF64 требует фрагменты моно PCM 16-bit / 24 кГц')
                    while buf := src.readframes(65536):
                        out.write(np.frombuffer(buf,dtype='<i2'))

    def _write_wav(self, usable, destination, pause_ms):
            with wave.open(str(destination), "wb") as out:
                fmt = None
                for i, s in enumerate(usable):
                    with wave.open(str(self.path(s["audio"])), "rb") as src:
                        current = (src.getnchannels(), src.getsampwidth(), src.getframerate())
                        if fmt is None:
                            fmt = current
                            out.setnchannels(fmt[0]); out.setsampwidth(fmt[1]); out.setframerate(fmt[2])
                        elif current != fmt:
                            raise ValueError("Несовместимые форматы фрагментов")
                        if i:
                            out.writeframes(b"\0" * (int(fmt[2] * pause_ms / 1000) * fmt[0] * fmt[1]))
                        while True:
                            buf = src.readframes(65536)
                            if not buf:
                                break
                            out.writeframes(buf)

    def update_preview(self, pause_ms=160):
        """Append newly finished audio in linear time. Rebuild after edits/reordering.

        The manifest records the committed file size. An interrupted append is
        detected on next use and rebuilt from the immutable segment WAVs.
        """
        rows = [s for _, s in self.rows(self.data['root']) if s.get('audio') and not s.get('muted')]
        if not rows:
            (self.folder/'preview.wav').unlink(missing_ok=True)
            with self.lock:self.data.pop('preview_state',None);self.save()
            return
        audio = [s['audio'] for s in rows]
        previous = self.data.get('preview_state') or {}
        old = previous.get('audio', [])
        path = self.folder / 'preview.wav'
        append = (path.exists() and old and audio[:len(old)] == old
                  and previous.get('pause_ms') == pause_ms
                  and path.stat().st_size == 44 + previous.get('frames', 0)*2)
        expected_size = 44 + sum(round(s.get('duration',0)*24000)*2 for s in rows)
        expected_size += max(0,len(rows)-1)*int(24000*pause_ms/1000)*2
        if expected_size >= RIFF_LIMIT:
            append = False
        if append:
            try:
                with wave.open(str(path), 'rb') as check:
                    append = (check.getnframes() == previous['frames'] and check.getframerate() == 24000
                              and check.getsampwidth() == 2 and check.getnchannels() == 1)
            except (wave.Error, EOFError):
                append = False
        if append:
            if audio == old:
                return
            with path.open('r+b') as dest:
                dest.seek(0, 2)
                for s in rows[len(old):]:
                    dest.write(b'\0' * (int(24000*pause_ms/1000)*2))
                    with wave.open(str(self.path(s['audio'])), 'rb') as src:
                        if (src.getnchannels(), src.getsampwidth(), src.getframerate()) != (1,2,24000):
                            raise ValueError('Неверный формат аудио для общей дорожки')
                        while buf := src.readframes(65536):
                            dest.write(buf)
                size = dest.tell()
                if size >= RIFF_LIMIT:
                    raise ValueError('Промежуточная WAV-запись превысила 4 ГБ. Экспортируйте книгу по разделам.')
                dest.seek(4); dest.write(struct.pack('<I', size-8))
                dest.seek(40); dest.write(struct.pack('<I', size-44))
                dest.flush(); os.fsync(dest.fileno())
            frames = (size-44)//2
        else:
            self.export(self.data['root'], path, pause_ms, allow_partial=True)
            import soundfile as sf
            frames = sf.info(str(path)).frames
        with self.lock:
            self.data['preview_state'] = dict(audio=audio, frames=frames, pause_ms=pause_ms)
            self.save()
