from __future__ import annotations

import re
from pathlib import Path

import fitz
from pydantic import BaseModel, Field


class BookMetadata(BaseModel):
    board: str
    class_name: str
    subject: str
    book_title: str
    source_file: str


class ContentBlock(BaseModel):
    page: int
    text: str


class Subtopic(BaseModel):
    title: str
    contents: list[ContentBlock] = Field(default_factory=list)


class Chapter(BaseModel):
    title: str
    start_page: int
    end_page: int | None = None
    subtopics: list[Subtopic] = Field(default_factory=list)


class StructuredBook(BaseModel):
    board: str
    class_name: str
    subject: str
    book_title: str
    source_file: str
    chapters: list[Chapter] = Field(default_factory=list)


class PageText(BaseModel):
    page: int
    text: str


CHAPTER_PATTERNS = [
    re.compile(r"^(chapter|unit)\s+\d+[\s:.-]*(.*)$", re.IGNORECASE),
    re.compile(r"^\d+\s+[A-Z][A-Za-z0-9 ,:'()/-]{4,}$"),
]

SUBTOPIC_PATTERNS = [
    re.compile(r"^\d+(?:\.\d+)+\s+[A-Z][A-Za-z0-9 ,:'()/-]{3,}$"),
    re.compile(r"^[A-Z][A-Za-z0-9 ,:'()/-]{4,80}$"),
]

NOISE_PATTERNS = [
    re.compile(r"^\s*\d+\s*$"),
    re.compile(r"^\s*©.*$", re.IGNORECASE),
    re.compile(r"^\s*copyright.*$", re.IGNORECASE),
]


def parse_pdf_to_book(pdf_path: Path, metadata: BookMetadata) -> StructuredBook:
    pages = extract_pages(pdf_path)
    book = StructuredBook(**metadata.model_dump())

    current_chapter: Chapter | None = None
    current_subtopic: Subtopic | None = None

    for page in pages:
        for block in split_page_into_blocks(page.text):
            heading = first_line(block)

            if is_chapter_heading(heading):
                if current_chapter:
                    current_chapter.end_page = page.page
                current_chapter = Chapter(title=normalize_heading(heading), start_page=page.page)
                current_subtopic = Subtopic(title="Introduction")
                current_chapter.subtopics.append(current_subtopic)
                book.chapters.append(current_chapter)
                remaining = block_after_first_line(block)
                if remaining:
                    current_subtopic.contents.append(ContentBlock(page=page.page, text=remaining))
                continue

            if current_chapter and is_subtopic_heading(heading):
                current_subtopic = Subtopic(title=normalize_heading(heading))
                current_chapter.subtopics.append(current_subtopic)
                remaining = block_after_first_line(block)
                if remaining:
                    current_subtopic.contents.append(ContentBlock(page=page.page, text=remaining))
                continue

            if not current_chapter:
                current_chapter = Chapter(title="Front Matter", start_page=page.page)
                current_subtopic = Subtopic(title="Contents")
                current_chapter.subtopics.append(current_subtopic)
                book.chapters.append(current_chapter)

            if not current_subtopic:
                current_subtopic = Subtopic(title="Contents")
                current_chapter.subtopics.append(current_subtopic)

            current_subtopic.contents.append(ContentBlock(page=page.page, text=block))

    if book.chapters:
        book.chapters[-1].end_page = pages[-1].page if pages else book.chapters[-1].start_page

    return book


def extract_pages(pdf_path: Path) -> list[PageText]:
    pages: list[PageText] = []
    with fitz.open(pdf_path) as document:
        for index, page in enumerate(document, start=1):
            text = page.get_text("text")
            cleaned = clean_text(text)
            if cleaned:
                pages.append(PageText(page=index, text=cleaned))
    return pages


def clean_text(text: str) -> str:
    lines = []
    for raw_line in text.splitlines():
        line = normalize_spaces(raw_line)
        if not line:
            continue
        if any(pattern.match(line) for pattern in NOISE_PATTERNS):
            continue
        lines.append(line)
    return "\n".join(lines)


def split_page_into_blocks(text: str) -> list[str]:
    paragraphs: list[str] = []
    current: list[str] = []

    for line in text.splitlines():
        if is_standalone_heading(line) and current:
            paragraphs.append(" ".join(current).strip())
            current = [line]
            continue

        if is_standalone_heading(line):
            if current:
                paragraphs.append(" ".join(current).strip())
            current = [line]
            continue

        current.append(line)

        if line.endswith((".", "?", "!", ":")) and len(" ".join(current)) > 220:
            paragraphs.append(" ".join(current).strip())
            current = []

    if current:
        paragraphs.append(" ".join(current).strip())

    return [paragraph for paragraph in paragraphs if paragraph]


def is_chapter_heading(line: str) -> bool:
    return any(pattern.match(line) for pattern in CHAPTER_PATTERNS)


def is_subtopic_heading(line: str) -> bool:
    if is_chapter_heading(line):
        return False
    if len(line) > 90:
        return False
    return any(pattern.match(line) for pattern in SUBTOPIC_PATTERNS)


def is_standalone_heading(line: str) -> bool:
    return is_chapter_heading(line) or is_subtopic_heading(line)


def first_line(block: str) -> str:
    return block.splitlines()[0] if "\n" in block else block


def block_after_first_line(block: str) -> str:
    lines = block.splitlines()
    if len(lines) < 2:
        return ""
    return normalize_spaces(" ".join(lines[1:]))


def normalize_heading(line: str) -> str:
    return normalize_spaces(line).strip(" .:-")


def normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
