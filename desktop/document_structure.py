"""Extract document headings without changing the reading order."""
import re
from pathlib import Path


def sections_from_blocks(blocks):
    sections, current, parts = [], None, []
    found = False
    for text, level in blocks:
        if not text.strip():continue
        if level:
            found = True
            if current is not None:current['text']=''.join(parts)
            current = dict(title=text.strip()[:120], level=level, text='')
            parts=[text];sections.append(current)
        else:
            if current is None:
                current = dict(title='Вступление', level=1, text='')
                sections.append(current)
            parts.append(text)
    if current is not None:current['text']=''.join(parts)
    return sections if found else []


def plain_blocks(text):
    for line in text.splitlines(keepends=True):
        heading = re.match(r'^\s*(#{1,6})\s+(.+)', line)
        if heading:
            yield heading.group(2).strip()+'\n',len(heading.group(1))
        elif re.match(r'^\s*(?:глава|часть|раздел|книга|chapter|part|section|book)\s+(?:\d+|[IVXLCDM]+|[А-Яа-яЁё]+)(?:[. :—–-]|\s|$)',line,re.I) and len(line.strip())<140:
            level=1 if re.match(r'^\s*(часть|книга|part|book)\b',line,re.I) else 2
            yield line,level
        else:yield line,None


def html_blocks(html):
    from bs4 import BeautifulSoup
    soup=BeautifulSoup(html,'html.parser');body=soup.body or soup
    for unwanted in body(['script','style','nav']):unwanted.decompose()
    # Iterate strings once; a heading becomes one block, not duplicated children.
    consumed=set()
    for value in body.find_all(string=True):
        if not value.strip():continue
        heading=value.find_parent(re.compile('^h[1-6]$'))
        if heading:
            if id(heading) not in consumed:
                consumed.add(id(heading));yield heading.get_text(' ',strip=True)+'\n\n',int(heading.name[1])
        else:yield str(value).strip()+'\n',None


def extract_sections(path):
    from app.document_reader import extract_text
    path=Path(path);suffix=path.suffix.lower()
    if suffix=='.docx':
        from docx import Document
        from docx.text.paragraph import Paragraph
        blocks=[]
        for block in Document(path).iter_inner_content():
            if isinstance(block,Paragraph):
                match=re.search(r'(?:heading|заголовок)\s*(\d+)',block.style.name,re.I)
                level=int(match.group(1)) if match else None
                blocks.append((block.text+'\n\n',level))
            else:
                for row in block.rows:blocks.append((' | '.join(c.text for c in row.cells)+'\n',None))
        result=sections_from_blocks(blocks)
        return result or sections_from_blocks(plain_blocks(extract_text(path)))
    if suffix in ('.html','.htm'):
        from app.document_reader import _read_text
        return sections_from_blocks(html_blocks(_read_text(path)))
    if suffix=='.epub':
        from ebooklib import epub,ITEM_DOCUMENT
        book=epub.read_epub(str(path));blocks=[]
        for item_id,linear in book.spine:
            item=book.get_item_with_id(item_id)
            if linear=='no' or item is None or item.get_type()!=ITEM_DOCUMENT or isinstance(item,epub.EpubNav):continue
            item_blocks=list(html_blocks(item.get_content()))
            if not any(level for _,level in item_blocks):
                blocks.append(((item.title or Path(item.file_name).stem)+'\n\n',1))
            blocks.extend(item_blocks)
        return sections_from_blocks(blocks)
    return sections_from_blocks(plain_blocks(extract_text(path)))
