"""Coalesced, atomic persistence without disk IO on the GUI thread."""
import copy
import json
import os
import threading


class BackgroundSave:
    def __init__(self, project):
        self.project = project
        self.condition = threading.Condition()
        self.requested = self.written = 0
        self.error = None
        self.stopping = False
        self.thread = threading.Thread(target=self.run, name='project-save', daemon=True)
        self.thread.start()

    def request(self):
        with self.condition:
            self.requested += 1
            self.condition.notify_all()

    def flush(self):
        with self.condition:
            target = self.requested
            self.condition.wait_for(lambda: self.written >= target or self.error is not None)
            if self.error: raise OSError('Не удалось сохранить проект: '+str(self.error))

    def close(self):
        try:self.flush()
        finally:
            with self.condition:
                self.stopping = True
                self.condition.notify_all()
            self.thread.join()

    def run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.stopping or self.requested > self.written)
                if self.stopping:return
            # Coalesce bursts of imported chapters and generation state changes.
            threading.Event().wait(.15)
            with self.condition:revision = self.requested
            try:
                with self.project.lock:
                    snapshot = copy.deepcopy(self.project.data)
                folder = self.project.folder
                folder.mkdir(parents=True, exist_ok=True)
                temp = folder/'project.json.tmp'
                with temp.open('w', encoding='utf-8') as f:
                    json.dump(snapshot, f, ensure_ascii=False, indent=2)
                    f.flush();os.fsync(f.fileno())
                os.replace(temp, folder/'project.json')
                with self.condition:
                    self.written = revision
                    self.error = None
                    self.condition.notify_all()
            except Exception as exc:
                with self.condition:
                    self.error = exc
                    self.condition.notify_all()
                    self.condition.wait_for(lambda: self.stopping or self.requested > revision)
                    if self.stopping:return
