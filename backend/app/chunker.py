from __future__ import annotations

import re

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…])\s+(?=[A-ZА-ЯЁ0-9«\"—–(])")
_WHITESPACE = re.compile(r"[ \t]+")


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(_WHITESPACE.sub(" ", line).strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _hard_split(text: str, limit: int) -> list[str]:
    parts: list[str] = []
    rest = text.strip()
    while len(rest) > limit:
        cut = rest.rfind(" ", 0, limit + 1)
        if cut < int(limit * 0.55):
            cut = limit
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        parts.append(rest)
    return parts


def chunk_text(text: str, max_chars: int = 900) -> list[str]:
    """Split prose into TTS-friendly chunks while preserving paragraph boundaries."""
    if max_chars < 200:
        raise ValueError("max_chars must be >= 200")

    text = normalize_text(text)
    if not text:
        return []

    chunks: list[str] = []
    current = ""

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    for paragraph in paragraphs:
        sentences = _SENTENCE_BOUNDARY.split(paragraph)
        expanded: list[str] = []
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            expanded.extend(_hard_split(sentence, max_chars))

        for sentence in expanded:
            candidate = sentence if not current else f"{current} {sentence}"
            if len(candidate) <= max_chars:
                current = candidate
                continue
            if current:
                chunks.append(current.strip())
            current = sentence

        if current:
            chunks.append(current.strip())
            current = ""

    return [c for c in chunks if c]
