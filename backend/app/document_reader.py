from __future__ import annotations

import html
import re
from pathlib import Path

from .chunker import normalize_text


def _read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(errors="replace")


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()

    if suffix in {".txt", ".md", ".markdown"}:
        return normalize_text(_read_text(path))

    if suffix == ".docx":
        from docx import Document
        from docx.text.paragraph import Paragraph
        from docx.table import Table

        doc = Document(path)
        blocks: list[str] = []
        for block in doc.iter_inner_content():
            if isinstance(block, Paragraph):
                value = block.text.strip()
                if value:
                    blocks.append(value)
            elif isinstance(block, Table):
                for row in block.rows:
                    values = [cell.text.strip() for cell in row.cells]
                    if any(values):
                        blocks.append(" | ".join(values))
        return normalize_text("\n\n".join(blocks))

    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        return normalize_text("\n\n".join(p for p in pages if p))

    if suffix == ".epub":
        from bs4 import BeautifulSoup
        from ebooklib import ITEM_DOCUMENT, epub

        book = epub.read_epub(str(path))
        blocks: list[str] = []
        ordered = [book.get_item_with_id(item_id) for item_id, linear in book.spine if linear != 'no']
        if not ordered:
            ordered = list(book.get_items_of_type(ITEM_DOCUMENT))
        for item in ordered:
            if item is None or item.get_type() != ITEM_DOCUMENT or isinstance(item, epub.EpubNav):
                continue
            soup = BeautifulSoup(item.get_content(), "html.parser")
            value = soup.get_text("\n", strip=True)
            if value:
                blocks.append(value)
        return normalize_text("\n\n".join(blocks))

    if suffix in {".html", ".htm"}:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(_read_text(path), "html.parser")
        return normalize_text(soup.get_text("\n", strip=True))

    if suffix == ".rtf":
        from striprtf.striprtf import rtf_to_text

        return normalize_text(rtf_to_text(_read_text(path)))

    raise ValueError(
        "Unsupported document type. Supported: TXT, MD, DOCX, PDF, EPUB, HTML, RTF"
    )


def guess_title(path: Path, text: str) -> str:
    stem = path.stem.strip()
    if stem:
        return stem
    first = re.split(r"[\n.!?]", text.strip(), maxsplit=1)[0].strip()
    return html.unescape(first[:100]) or "Untitled"
