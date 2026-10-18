from __future__ import annotations

import difflib
import gc
import os
import re
import subprocess
import threading
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"


def words(text, language="ru"):
    from num2words import num2words
    def number(m):
        try:
            return " " + num2words(int(m.group()), lang=language if language in ("ru", "en") else "en") + " "
        except (ValueError, OverflowError):
            return m.group()
    text = re.sub(r"\d+", number, text.lower().replace("ё", "е"))
    return re.findall(r"[^\W_]+", text.replace('ё', 'е'), re.UNICODE)


def compare(expected, actual, language="ru"):
    a, b = words(expected, language), words(actual, language)
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    missing, extra, matched = [], [], 0
    for op, i, j, k, l in matcher.get_opcodes():
        if op == "equal":
            matched += j - i
        else:
            missing.extend(a[i:j]); extra.extend(b[k:l])
    coverage = matched / max(1, len(a))
    precision = matched / max(1, len(b))
    # Strict acceptance: uncertainty is reviewed, not silently accepted.
    passed = bool(a) and a == b
    return dict(passed=passed, coverage=coverage, precision=precision, missing=missing,
                extra=extra, transcript=actual, method="whisper-small / точное сравнение нормализованных слов")


class SpeechEngine:
    def __init__(self, models):
        self.models = Path(models)
        self.piper = None
        self.piper_key = None
        self.higgs = None
        self.tokenizer = None
        self.whisper = None

    def synthesize(self, text, output, settings, notify=lambda _: None):
        import numpy as np
        import soundfile as sf
        if settings["engine"].startswith("piper"):
            from model_manager import CATALOG, ModelManager
            key=settings["engine"]
            if key not in CATALOG or not ModelManager(self.models).installed(key):raise ValueError("Голос не установлен: "+key)
            if self.higgs is not None:
                self.unload()
            if self.piper is None or self.piper_key != key:
                notify("Загрузка "+CATALOG[key]["name"])
                from piper import PiperVoice
                from piper.phonemize_espeak import ESPEAK_DATA_DIR
                import shutil
                import tempfile
                # espeak's C file API cannot open UTF-8 Windows paths. Use a local
                # ASCII cache, populated from the bundled data (no network).
                data_dir = ESPEAK_DATA_DIR
                if os.name == 'nt' and not str(data_dir).isascii():
                    candidates = [Path(tempfile.gettempdir()), Path(os.environ.get('PUBLIC', 'C:/Users/Public'))]
                    cache_root = next((p for p in candidates if str(p).isascii()), None)
                    if cache_root is None:
                        raise RuntimeError('Не найден путь для локального кэша espeak. Переместите программу в папку с латинским именем.')
                    data_dir = cache_root / 'HiggsReader-0.3.0' / 'espeak-ng-data'
                    if not (data_dir / '.reader-complete').exists():
                        shutil.copytree(ESPEAK_DATA_DIR, data_dir, dirs_exist_ok=True)
                        (data_dir / '.reader-complete').write_text('0.3.0', encoding='ascii')
                self.piper = PiperVoice.load(str(self.models / CATALOG[key]["path"]), espeak_data_dir=data_dir)
            self.piper_key=key
            chunks = list(self.piper.synthesize(text))
            if not chunks:
                raise ValueError("Движок вернул пустое аудио")
            data = np.concatenate([c.audio_float_array for c in chunks])
            sample_rate = chunks[0].sample_rate
        else:
            if settings["engine"] != "higgs":raise ValueError("Неизвестный движок речи")
            import torch
            if self.higgs is None:
                notify("Загрузка Higgs Audio 4B…")
                from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
                device = "cuda" if torch.cuda.is_available() else "cpu"
                options = {}
                if device == "cuda":
                    free, _ = torch.cuda.mem_get_info()
                    if free < 10 * 1024**3:
                        notify("Higgs: загрузка в 4-битном режиме для экономии видеопамяти…")
                        options.update(device_map={'': 0}, quantization_config=BitsAndBytesConfig(
                            load_in_4bit=True, bnb_4bit_quant_type='nf4', bnb_4bit_compute_dtype=torch.bfloat16,
                            bnb_4bit_use_double_quant=True, llm_int8_skip_modules=['audio_head']))
                self.tokenizer = AutoTokenizer.from_pretrained(self.models / "higgs", local_files_only=True)
                model = AutoModelForCausalLM.from_pretrained(
                    self.models / "higgs", trust_remote_code=True, local_files_only=True,
                    dtype=torch.bfloat16 if device == "cuda" else torch.float32, **options)
                if not options:
                    model = model.to(device)
                self.higgs = model.eval()
                self.higgs.config.audio_tokenizer_id = str((self.models / "codec").resolve())
            kwargs = dict(temperature=settings.get("temperature", 0.7), top_p=settings.get("top_p", 0.95),
                          max_new_tokens=4096)
            if settings.get("reference"):
                audio, sr = sf.read(settings["reference"], dtype="float32", always_2d=True)
                kwargs.update(reference_audio=torch.from_numpy(audio.mean(axis=1)).unsqueeze(0),
                              reference_sample_rate=sr, reference_text=settings.get("reference_text") or None)
            with torch.inference_mode():
                wav = self.higgs.generate_speech(text, self.tokenizer, **kwargs)
            data = wav.detach().float().cpu().numpy().flatten()
            sample_rate = self.higgs.config.sample_rate
        if not len(data) or not np.isfinite(data).all() or np.max(np.abs(data)) < 0.0001:
            raise RuntimeError("Получено пустое, повреждённое или беззвучное аудио")
        # Both engines use a single project format for lossless streaming concatenation.
        if sample_rate != 24000:
            import scipy.signal
            from math import gcd
            divisor = gcd(sample_rate, 24000)
            data = scipy.signal.resample_poly(data, 24000 // divisor, sample_rate // divisor)
        sf.write(str(output), data, 24000, subtype="PCM_16", format="WAV")
        peaks = [round(float(np.max(np.abs(part))), 4) for part in np.array_split(data, min(180, len(data)))]
        return len(data) / 24000, peaks

    def verify(self, path, text, language="ru", notify=lambda _: None):
        if self.whisper is None:
            notify("Загрузка модели проверки речи…")
            from faster_whisper import WhisperModel
            self.whisper = WhisperModel(str(self.models / "whisper-small"), device="cpu", compute_type="int8",
                                        cpu_threads=max(2, min(8, (os.cpu_count() or 4) // 2)), local_files_only=True)
        parts, _ = self.whisper.transcribe(str(path), language=language or None, beam_size=5,
                                          word_timestamps=True, vad_filter=False, condition_on_previous_text=False)
        parts = list(parts)
        result = compare(text, " ".join(s.text.strip() for s in parts), language)
        result["words"] = [dict(word=w.word, start=w.start, end=w.end) for s in parts for w in (s.words or [])]
        return result

    def unload(self):
        self.higgs = None
        self.tokenizer = None
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass


def convert(wav, target):
    import imageio_ffmpeg
    target = Path(target)
    temp = target.with_name(target.stem + ".partial" + target.suffix)
    try:
        command = [imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-y", "-i", str(wav)]
        if target.suffix.lower() == ".mp3":
            command += ["-codec:a", "libmp3lame", "-q:a", "2"]
        elif target.suffix.lower() in (".m4a", ".m4b"):
            command += ["-c:a", "aac", "-b:a", "192k"]
        command.append(str(temp))
        subprocess.run(command, check=True, capture_output=True, creationflags=0x08000000 if os.name == "nt" else 0)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def _run_job(project, engine, ids, settings, stop, notify=lambda _: None, changed=lambda: None, settings_provider=None):
    """Commit each segment before starting the next. Job cursor survives crashes."""
    from project import uid
    project.data["job"] = dict(status="running", ids=list(ids), done=[], settings=dict(settings), error="")
    project.save()
    changed()
    try:
        for index, seg_id in enumerate(ids):
            if stop.is_set():
                break
            node, seg = project.find_segment(seg_id)
            try:
                if settings_provider is not None:
                    settings=settings_provider()
                    project.data['job']['settings']=dict(settings)
                best=None;errors=0;low_scores=0;attempt=0
                error_limit=int(settings.get('error_retries',10)) if settings.get('retry_errors',False) else 0
                quality_limit=int(settings.get('quality_retries',10)) if settings.get('retry_low_score',False) else 0
                seg['attempts']=[]
                while not stop.is_set():
                    attempt+=1
                    relative=f"audio/{seg_id}/{uid()}.wav"
                    path=project.path(relative);path.parent.mkdir(parents=True,exist_ok=True)
                    seg.update(status='generating',error='');project.save();changed()
                    notify(f"{index+1}/{len(ids)} · {node['title']} · Попытка {attempt}")
                    try:
                        if seg.get('text_prepared'):
                            options=dict(settings)
                            if settings.get('mode')=='advanced' and settings.get('dialogue'):
                                from characters import registry
                                char=registry(project).get(seg.get('speaker_id'),{})
                                voice=next((v for v in settings.get('voices',[]) if v['id']==char.get('voice')),None)
                                if voice:options.update({k:v for k,v in voice.items() if k in ('engine','reference','reference_text','temperature','top_p')})
                                elif seg.get('speaker_id') not in ('narrator',None):notify('Для персонажа голос не назначен: используется голос по умолчанию.')
                            if options.get('reference'):options['reference']=str(project.path(options['reference']))
                            spoken_text=seg['text'];duration,peaks=engine.synthesize(spoken_text,path,options,notify)
                        elif settings.get('mode')=='advanced' and (settings.get('correct_text') or settings.get('dialogue')):
                            from language_ai import synthesize_advanced
                            duration,peaks,spoken_text=synthesize_advanced(project,seg,engine,path,settings,notify)
                        else:
                            spoken_text=seg['text'];duration,peaks=engine.synthesize(spoken_text,path,settings,notify)
                        seg['status']='verifying';project.save();changed()
                        try:verification=engine.verify(path,spoken_text,settings.get('language','ru'),notify)
                        except Exception as exc:verification=dict(passed=False,coverage=0,transcript='',error=str(exc),missing=[],extra=[])
                        score=100*verification.get('coverage',1 if verification.get('passed') else 0)
                        seg['attempts'].append(dict(audio=relative,score=score,verification=verification))
                        if best is None or score>best[0]:best=(score,relative,duration,peaks,verification,spoken_text)
                        project.save()
                        if score>=float(settings.get('min_score',85)) or low_scores>=quality_limit:break
                        low_scores+=1
                        notify(f'Сверка {score:.1f}%. Повтор {low_scores}/{quality_limit}.')
                    except Exception as exc:
                        seg['attempts'].append(dict(error=str(exc)));project.save()
                        if errors>=error_limit:
                            if best is None:raise
                            break
                        errors+=1;notify(f'Ошибка: {exc}. Повтор {errors}/{error_limit}.')
                if best is None:
                    seg['status']='pending';break
                _,relative,duration,peaks,verification,spoken_text=best
                with project.lock:
                    project.data["job"]["done"].append(seg_id)
                    project.commit(seg_id, relative, duration, peaks, verification, settings)
                    seg["spoken_text"]=spoken_text
                    project.save()
                if settings.get('auto_cut_pauses',False) or settings.get('trim_start_pause',False) or settings.get('trim_end_pause',False):
                    from audio_edit import edit_segment
                    notify('Поиск и исключение длинных пауз…')
                    edit_segment(project,seg_id,'detect',threshold_ms=settings.get('max_pause_ms',1000),replacement_ms=settings.get('pause_replacement_ms',0),keep_start_ms=settings.get('pause_keep_start_ms',300),keep_end_ms=settings.get('pause_keep_end_ms',300),trim_edges=settings,detect_internal=settings.get('auto_cut_pauses',False))
                changed()
                # A real continuous recording, incrementally extended with crash recovery.
                notify(f"{index+1}/{len(ids)} · Обновление общей записи")
                try:
                    project.update_preview(settings.get('pause_ms', 160))
                    with project.lock:project.data['preview_error'] = ''
                except (OSError, ValueError) as exc:
                    # Segment audio is safe even when an external player locks
                    # preview.wav. Final export always reads the segment files.
                    with project.lock:project.data['preview_error'] = str(exc)
                    notify('Фрагмент сохранён; общая предварительная запись: ' + str(exc))
                if not verification["passed"]:
                    notify('Расхождения отмечены. Продолжаю генерацию следующего фрагмента.')
            except Exception as exc:
                seg.update(status="error", error=str(exc))
                project.data["job"]["error"] = str(exc)
                notify(str(exc))
                if settings.get("retry_errors",False):
                    project.data["job"].setdefault("skipped",[]).append(seg_id)
                    project.data["job"]["done"].append(seg_id)
                    project.data["job"]["error"]=""
                    project.save();changed();continue
                break
        job = project.data["job"]
        job["status"] = "completed" if len(job["done"]) == len(ids) and not job["error"] else "paused"
    finally:
        project.save(); changed()


def run_job(project,engine,ids,settings,stop,notify=lambda _:None,changed=lambda:None,settings_provider=None):
    from model_manager import INFERENCE_LOCK
    notify("Ожидание очереди вычислений…")
    while not stop.is_set():
        if INFERENCE_LOCK.acquire(timeout=.2):break
    else:return
    try:return _run_job(project,engine,ids,settings,stop,notify,changed,settings_provider)
    finally:
        try:
            if hasattr(engine,'llm'):engine.llm.unload()
            if hasattr(engine,'unload'):engine.unload()
        finally:INFERENCE_LOCK.release()
