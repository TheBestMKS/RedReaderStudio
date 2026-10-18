"""Document IO, structural analysis and bounded preparation in a worker thread."""
import threading
from pathlib import Path
from PySide6.QtCore import QThread, Signal
from project import uid, segment, iter_split_text
from document_structure import sections_from_blocks, plain_blocks, html_blocks


class ImportCancelled(Exception):pass


def read_blocks(path, report, stop):
    def progress(stage, done=0, total=0):
        if stop.is_set():raise ImportCancelled()
        report(stage, done, total)

    path=Path(path);suffix=path.suffix.lower()
    progress('Загрузка документа')
    if suffix=='.docx':
        from docx import Document
        from docx.text.paragraph import Paragraph
        import re
        doc=Document(path)
        total=len(doc.element.body)
        for i,block in enumerate(doc.iter_inner_content()):
            progress('Анализ абзацев',i+1,total)
            if isinstance(block,Paragraph):
                match=re.search(r'(?:heading|заголовок)\s*(\d+)',block.style.name,re.I)
                if match:yield block.text+'\n\n',int(match.group(1))
                else:yield from plain_blocks(block.text+'\n\n')
            else:
                for row in block.rows:yield ' | '.join(c.text for c in row.cells)+'\n',None
    elif suffix=='.epub':
        from ebooklib import epub,ITEM_DOCUMENT
        book=epub.read_epub(str(path))
        ordered=[book.get_item_with_id(k) for k,linear in book.spine if linear!='no']
        if not ordered:ordered=list(book.get_items_of_type(ITEM_DOCUMENT))
        for i,item in enumerate(ordered):
            progress('Анализ глав EPUB',i+1,len(ordered))
            if item is None or item.get_type()!=ITEM_DOCUMENT or isinstance(item,epub.EpubNav):continue
            blocks=list(html_blocks(item.get_content()))
            if not any(level for _,level in blocks):yield (item.title or Path(item.file_name).stem)+'\n\n',1
            yield from blocks
    elif suffix=='.pdf':
        from pypdf import PdfReader
        reader=PdfReader(str(path))
        for i,page in enumerate(reader.pages):
            progress('Анализ страниц PDF',i+1,len(reader.pages))
            yield from plain_blocks((page.extract_text() or '')+'\n\n')
    else:
        size=path.stat().st_size;data=bytearray()
        with path.open('rb') as f:
            while chunk:=f.read(1024*1024):
                data.extend(chunk);progress('Загрузка документа (байт)',len(data),size)
        text=None
        for encoding in ('utf-8-sig','cp1251','latin-1'):
            try:text=data.decode(encoding);break
            except UnicodeDecodeError:pass
        progress('Анализ структуры')
        if suffix in ('.html','.htm'):yield from html_blocks(text)
        else:
            if suffix=='.rtf':
                from striprtf.striprtf import rtf_to_text
                text=rtf_to_text(text)
            yield from plain_blocks(text)


class ImportWorker(QThread):
    progress=Signal(str,int,int)
    analyzed=Signal(int)
    batch=Signal(object,object)  # new nodes, (target id, segments) additions
    completed=Signal(bool,int)
    failure=Signal(str)

    def __init__(self,path,target,limit,offer_sections,options=None,project=None,models=None):
        super().__init__()
        self.path,self.target,self.limit,self.offer_sections=path,target,limit,offer_sections
        self.stop=threading.Event();self.decision=threading.Event();self.ack=threading.Event()
        self.options=options;self.project=project;self.models=models;self.llm=None;self.source_stream=None
        import copy
        self.cfg=copy.deepcopy(project.data["settings"]) if project else None
        self.use_sections=False;self.created=0;self._last_report=0

    def choose(self,use_sections):
        self.use_sections=use_sections;self.decision.set()

    def cancel(self):
        self.stop.set();self.decision.set();self.ack.set()

    def report(self,stage,done=0,total=0):
        import time
        if self.stop.is_set():raise ImportCancelled()
        now=time.monotonic()
        if now-self._last_report>=.08 or (total and done==total):
            self.progress.emit(stage,done,total);self._last_report=now

    def send(self,nodes,additions):
        if self.stop.is_set():raise ImportCancelled()
        self.ack.clear();self.batch.emit(nodes,additions)
        while not self.ack.wait(.1):
            if self.stop.is_set():raise ImportCancelled()
        if self.stop.is_set():raise ImportCancelled()

    def run(self):
        try:
            blocks=[]
            for i,block in enumerate(read_blocks(self.path,self.report,self.stop)):
                if self.stop.is_set():raise ImportCancelled()
                blocks.append(block)
                if i%500==0:self.report('Анализ структуры',i,0)
            self.report('Поиск разделов')
            sections=sections_from_blocks(blocks) if self.offer_sections else []
            if sections:
                self.analyzed.emit(len(sections))
                while not self.decision.wait(.1):
                    if self.stop.is_set():raise ImportCancelled()
            if self.stop.is_set():raise ImportCancelled()
            if not self.use_sections:
                sections=[dict(text=''.join(text for text,_ in blocks),level=0,title='')]
            del blocks
            total=sum(len(s['text']) for s in sections);done=0
            if not total or not any(s['text'].strip() for s in sections):raise ValueError('Документ не содержит текста')
            stack=[(0,self.target)]
            for section in sections:
                nodes=[];target=self.target
                if self.use_sections:
                    while len(stack)>1 and stack[-1][0]>=section['level']:stack.pop()
                    target=uid();nodes=[dict(id=target,parent=stack[-1][1],title=section['title'],segments=[])]
                    stack.append((section['level'],target))
                parts=[]
                if self.project is not None:
                    from text_pipeline import prepare_text
                    if self.cfg.get('mode')=='advanced' and (self.cfg.get('dialogue') or self.cfg.get('correct_text')) and self.llm is None:
                        from language_ai import LanguageModel
                        self.llm=LanguageModel(self.models)
                    if self.source_stream is None:
                        source_dir=self.project.path('texts');source_dir.mkdir(exist_ok=True)
                        self.source_path=source_dir/(uid()+'.txt');self.source_stream=self.source_path.open('wb')
                    payload=section['text'].encode('utf-8');offset=self.source_stream.tell();self.source_stream.write(payload);self.source_stream.flush()
                    source=dict(path=self.source_path.relative_to(self.project.folder).as_posix(),offset=offset,length=len(payload))
                    if nodes:nodes[0]['source_chunks']=[source]
                    else:
                        with self.project.lock:self.project.node(target).setdefault('source_chunks',[]).append(source)
                    generated=prepare_text(self.project,section['text'],self.cfg,self.llm,lambda msg:self.report(msg),self.stop)
                else:generated=(segment(t) for t in iter_split_text(section['text'],self.limit,self.options))
                for prepared in generated:
                    if self.stop.is_set():raise ImportCancelled()
                    parts.append(prepared);done+=len(prepared["text"])
                    if len(parts)>=128:
                        self.send(nodes,[(target,parts)]);nodes=[];parts=[]
                        self.report('Разбиение на фрагменты (символов)',done,total)
                if nodes or parts:self.send(nodes,[(target,parts)])
                self.created+=int(self.use_sections)
                stage=(f'Разделы: {self.created} / {len(sections)} · символов' if self.use_sections
                       else 'Разбиение на фрагменты (символов)')
                self.report(stage,done,total)
            self.completed.emit(False,self.created)
        except (ImportCancelled,InterruptedError):self.completed.emit(True,self.created)
        except Exception as exc:self.failure.emit(str(exc))

        finally:
            if self.source_stream is not None:self.source_stream.close()
            if self.llm is not None:self.llm.unload()
