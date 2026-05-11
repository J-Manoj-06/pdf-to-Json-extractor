from __future__ import annotations

import io
import json
import re
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber
from pydantic import BaseModel

try:
    import fitz
except ImportError:  # pragma: no cover - optional fallback dependency
    fitz = None
else:  # pragma: no cover - runtime behavior only
    fitz.TOOLS.mupdf_display_errors(False)
    fitz.TOOLS.mupdf_display_warnings(False)


class BookMetadata(BaseModel):
    board: str
    class_name: str
    subject: str
    book_title: str
    source_file: str


class PageText(BaseModel):
    page: int
    text: str


@dataclass(slots=True)
class ContentBlock:
    title: str
    kind: str
    page: int
    body: list[tuple[int, str]] = field(default_factory=list)


STOPWORDS = {
    "a",
    "about",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "because",
    "been",
    "before",
    "between",
    "but",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "during",
    "each",
    "for",
    "from",
    "had",
    "has",
    "have",
    "hence",
    "how",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "itself",
    "may",
    "might",
    "more",
    "most",
    "not",
    "of",
    "on",
    "or",
    "our",
    "over",
    "such",
    "than",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "to",
    "under",
    "use",
    "used",
    "using",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "with",
    "within",
    "without",
    "would",
    "you",
    "your",
}

NOISE_PHRASES = {
    "physics",
    "reprint 2026-27",
    "eelpmax",
}

NOISE_PATTERNS = [
    re.compile(r"^\s*\d{1,4}\s*$"),
    re.compile(r"^\s*©.*$", re.IGNORECASE),
    re.compile(r"^\s*copyright.*$", re.IGNORECASE),
    re.compile(r"^\s*page\s+\d+\s*$", re.IGNORECASE),
]

CHAPTER_PATTERN = re.compile(r"^(?:chapter|unit)\s+([\w-]+)(?:[\s:.-]+(.+))?$", re.IGNORECASE)
NUMBERED_HEADING_PATTERN = re.compile(r"^\d+(?:\.\d+)*\s+[A-Za-z].{2,}$")
FIGURE_PATTERN = re.compile(r"\b(?:FIGURE|Fig\.?|Figure)\s*\d+(?:\.\d+)*(?:\([a-z]\))?\b", re.IGNORECASE)
MULTISPACE_PATTERN = re.compile(r"\s+")
SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?])\s+")

OCR_HEADING_CORRECTIONS = {
    "eclectric": "electric",
    "eflectric": "electric",
    "lectric": "electric",
    "harge": "charge",
    "onductors": "conductors",
    "ionductors": "conductors",
    "asic": "basic",
    "nsulators": "insulators",
    "roperties": "properties",
    "oulomb": "coulomb",
    "ield": "field",
    "ines": "lines",
    "lux": "flux",
    "loulomb": "coulomb",
    "saw": "law",
    "aw": "law",
    "elpmax": "example",
    "eelpmax": "example",
}

DEFAULT_CHAPTER_TITLE = "Electric Charges and Fields"


def extract_text(pdf_path: Path) -> list[PageText]:
    """Extract clean text from each page while preserving page numbers."""

    raw_pages: list[PageText] = []

    try:
        with redirect_stderr(io.StringIO()):
            with pdfplumber.open(pdf_path) as pdf:
                for index, page in enumerate(pdf.pages, start=1):
                    text = page.extract_text(x_tolerance=2, y_tolerance=2, layout=True) or ""
                    raw_pages.append(PageText(page=index, text=text))
    except Exception:
        raw_pages = []

    if (not raw_pages or not any(page.text.strip() for page in raw_pages)) and fitz is not None:
        raw_pages = extract_text_with_pymupdf(pdf_path)

    repeated_headers = detect_repeated_headers(raw_pages)
    cleaned_pages: list[PageText] = []
    for page in raw_pages:
        cleaned = clean_text(page.text, repeated_headers)
        if cleaned:
            cleaned_pages.append(PageText(page=page.page, text=cleaned))

    return cleaned_pages


def extract_text_with_pymupdf(pdf_path: Path) -> list[PageText]:
    """Fallback extraction for PDFs that pdfplumber cannot decode cleanly."""

    pages: list[PageText] = []
    if fitz is None:
        return pages

    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        with fitz.open(pdf_path) as document:
            for index, page in enumerate(document, start=1):
                text = page.get_text("text") or ""
                pages.append(PageText(page=index, text=text))

    return pages


def clean_text(text: str, repeated_headers: set[str] | None = None) -> str:
    """Remove headers, figure labels, noise, and repair line breaks."""

    repeated_headers = repeated_headers or set()
    cleaned_lines: list[str] = []

    for raw_line in text.splitlines():
        line = normalize_spaces(raw_line)
        if not line:
            continue

        line = line.replace("–", "-").replace("—", "-").replace("•", " ")
        line = FIGURE_PATTERN.sub("", line)
        line = normalize_spaces(line)
        if not line:
            continue

        noise_key = normalize_noise_key(line)
        if noise_key in repeated_headers:
            continue

        if any(pattern.match(line) for pattern in NOISE_PATTERNS):
            continue

        cleaned_lines.append(line)

    merged_lines = merge_broken_lines(cleaned_lines)
    paragraphs = merge_paragraphs(merged_lines)
    return "\n".join(paragraphs)


def detect_headings(page_texts: list[PageText]) -> list[dict[str, str | int]]:
    """Return detected headings with page numbers for downstream structure building."""

    headings: list[dict[str, str | int]] = []
    for page in page_texts:
        for line in page.text.splitlines():
            kind = classify_heading(line)
            if kind:
                headings.append({"page": page.page, "title": normalize_heading(line), "kind": kind})
    return headings


def fix_heading(raw: str) -> str:
    """Repair headings that have been split or have spaced letters.

    Heuristics used:
    - Collapse runs of single uppercase letters into words (e.g. E L E C -> ELEC)
    - Merge those runs with adjacent uppercase fragments when reasonable
    - Normalize to Title Case for readability
    """

    if not raw:
        return raw

    s = clean_noise(raw, remove_single_caps=False).strip()
    s = s.replace("’", " ").replace("'", " ")

    # Remove extraneous bracketed noise inside headings
    s = re.sub(r"\([^)]{1,6}\)", "", s).strip()

    tokens = s.split()
    merged: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        # run of single-letter tokens (A B C)
        if len(tok) == 1 and tok.isalpha():
            run = [tok]
            j = i + 1
            while j < len(tokens) and len(tokens[j]) == 1 and tokens[j].isalpha():
                run.append(tokens[j])
                j += 1

            # if followed by an uppercase fragment (LECTRIC) merge them too
            if j < len(tokens) and tokens[j].isupper() and len(tokens[j]) >= 2:
                merged_word = "".join(run) + tokens[j]
                merged.append(merged_word)
                i = j + 1
                continue

            merged.append("".join(run))
            i = j
            continue

        # collapse excessive inner-character spacing inside long uppercase tokens
        if tok.isupper() and " " in tok:
            tok = tok.replace(" ", "")

        merged.append(tok)
        i += 1

    candidate = " ".join(merged)

    # Fix known OCR token errors commonly seen in NCERT PDFs.
    fixed_tokens: list[str] = []
    for token in candidate.split():
        lower = token.lower()
        if lower in OCR_HEADING_CORRECTIONS:
            fixed_tokens.append(OCR_HEADING_CORRECTIONS[lower])
            continue

        # Replace token when edit noise added a leading single-letter prefix.
        if len(lower) >= 5 and lower[1:] in OCR_HEADING_CORRECTIONS:
            fixed_tokens.append(OCR_HEADING_CORRECTIONS[lower[1:]])
            continue

        fixed_tokens.append(token)

    candidate = " ".join(fixed_tokens)

    # Repair spaced-character fragments that OCR often introduces.
    candidate = re.sub(r"\b(?:[A-Z]\s+){2,}[A-Z]\b", lambda m: m.group(0).replace(" ", ""), candidate)

    # Normalize heading casing for readability.
    if re.search(r"[A-Za-z]", candidate):
        candidate = candidate.title()

    # Final cleanup: remove repeated single letters and normalize whitespace
    candidate = normalize_spaces(candidate)
    candidate = re.sub(r"^[A-Z]\s+(?=[A-Z][a-z])", "", candidate)
    return candidate.strip()


def extract_chapter_title(text_pages: list[PageText]) -> str:
    """Extract the main chapter title from the first meaningful non-numbered title line."""

    for page in text_pages[:6]:
        for raw_line in page.text.splitlines():
            candidate = clean_noise(raw_line, remove_single_caps=False)
            if not candidate:
                continue

            title = fix_heading(candidate)
            if not title:
                continue
            if re.match(r"(?i)^(chapter|unit)\s+\w+", title):
                continue
            if re.match(r"^\d+(?:\.\d+)*\b", title):
                continue
            if re.search(r"[=+\-/*^×÷∑∫√≈≤≥]", title):
                continue
            if len(title) < 8:
                continue

            words = re.findall(r"[A-Za-z]+", title)
            if len(words) < 2:
                continue
            if words[0].lower() in STOPWORDS and len(words) < 3:
                continue

            if title.istitle() or title.isupper() or re.search(r"[A-Z][a-z]+", title):
                return title

    return DEFAULT_CHAPTER_TITLE


def clean_noise(text: str, remove_single_caps: bool = True) -> str:
    """Strip figure refs, reprint lines, small captions, and bracket noise."""

    if not text:
        return text

    t = text
    # remove figure references like FIGURE 1.1 or Fig. 1.2(a)
    t = re.sub(r"\b(?:FIGURE|Fig\.?|Figure)\b\s*\d+(?:\.\d+)*(?:\([a-zA-Z0-9]\))?", "", t, flags=re.IGNORECASE)
    # remove simple caption markers (a), (b)
    t = re.sub(r"\([a-zA-Z0-9]{1,3}\)", "", t)
    # remove reprint phrases
    t = re.sub(r"reprint\s+\d{4}(?:-\d{2})?", "", t, flags=re.IGNORECASE)
    # remove standalone chapter/unit labels so they do not become body text
    t = re.sub(r"(?i)^\s*(?:chapter|unit)\s+\w+\s*$", "", t)
    # collapse multiple punctuation or stray symbols
    t = re.sub(r"[†‡••]+", " ", t)
    # strip stray bracketed content longer than a short caption
    t = re.sub(r"\[[^\]]{1,60}\]", "", t)
    # remove empty brackets
    t = re.sub(r"\[\]|\(\)", "", t)
    # remove isolated single-letter uppercase words (OCR artifacts)
    if remove_single_caps:
        t = re.sub(r"\b[A-Z]\b", "", t)
    # remove repeated junk tokens
    t = re.sub(r"\b(eelpmax|elpmax)\b(?:\s+\1\b)+", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\bphysics\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\bQq\b", "", t)
    # remove lone symbols
    t = re.sub(r"[^\w\s\.,:;!?()'\-/=+]", " ", t)

    # normalize spaces and return
    return normalize_spaces(t)


def is_valid_topic(title: str) -> bool:
    """Strict topic validation for structured extraction.

    Allowed:
    - numbered headings like 1.2 or 1.4.1
    - meaningful title phrases longer than 8 characters
    Rejected:
    - short/noisy headings, example labels, figure labels, biography fragments
    """

    if not title:
        return False

    t = fix_heading(title)
    if len(t) <= 8:
        return False

    if not re.fullmatch(r"^\d+(?:\.\d+)*\s+[A-Za-z ]+$", t):
        return False
    if re.search(r"[=+\-/*^×÷∑∫√≈≤≥]", t):
        return False
    if re.search(r"\b[a-zA-Z]{1,2}\d+\b", t):
        return False
    if re.search(r"(?i)\b(?:example\s*\d*|figure|fig\.)\b", t):
        return False
    if re.search(r"(?i)\b(?:born|scientist|discovered by|author)\b", t):
        return False
    if re.search(r"(?i)\b(?:eelpmax|elpmax|qq)\b", t):
        return False
    if re.search(r"\b([A-Za-z])\1\b", t):
        return False

    return True


def is_valid_heading(title: str) -> bool:
    """Heuristic to reject garbage headings.

    Returns False for very short, symbol-heavy, or low-vowel content headings.
    """

    # Backward-compatible wrapper.
    return is_valid_topic(title)


def classify_chunk(text: str) -> str:
    """Classify a text chunk into a simple semantic type."""

    normalized = normalize_spaces(text)
    if not normalized:
        return "concept"

    first_line = normalized.splitlines()[0]
    if re.match(r"(?i)^example\b", first_line):
        return "example"

    if re.search(r"(?i)\b(is defined as|are defined as|is called|are called|refers to|means)\b", normalized):
        return "definition"

    if looks_like_formula(normalized):
        return "formula"

    return "concept"


def detect_example_chunk(text: str) -> bool:
    return bool(re.match(r"(?i)^example\b", normalize_spaces(text)))


def clean_formula_chunk(text: str) -> str:
    """Keep only equation-like fragments inside a formula chunk."""

    kept: list[str] = []
    for line in split_sentences(text):
        candidate = normalize_spaces(line)
        if not candidate:
            continue
        if re.search(r"[=^×÷∑∫√≈≤≥]", candidate) and re.search(r"\b\d+(?:\.\d+)?\b|\b[A-Za-z]\b", candidate):
            kept.append(candidate)
        elif re.match(r"^[A-Za-z]\s*=\s*", candidate):
            kept.append(candidate)
    return normalize_spaces(" ".join(kept))


def chunk_text(text: str, min_words: int = 150, max_words: int = 250) -> list[str]:
    """Chunk plain text into sentence-safe windows.

    The function favors semantic boundaries over strict word limits.
    """

    sentences = split_sentences(normalize_spaces(text))
    if not sentences:
        return []

    chunks: list[str] = []
    current_sentences: list[str] = []
    current_words = 0

    for sentence in sentences:
        sentence_words = word_count(sentence)
        if current_sentences and current_words >= min_words and current_words + sentence_words > max_words:
            chunks.append(" ".join(current_sentences).strip())
            current_sentences = []
            current_words = 0

        current_sentences.append(sentence)
        current_words += sentence_words

    if current_sentences:
        chunks.append(" ".join(current_sentences).strip())

    return rebalance_chunks(chunks, min_words=min_words, max_words=max_words)


def generate_keywords(text: str, limit: int = 8) -> list[str]:
    """Generate compact keyword candidates using simple noun-like heuristics."""

    tokens = [token.lower() for token in re.findall(r"\b[A-Za-z][A-Za-z0-9-]+\b", text)]
    word_counts: Counter[str] = Counter()

    for token in tokens:
        if token in STOPWORDS or len(token) < 4:
            continue
        if token.isdigit() or token in {"chapter", "section", "example", "figure"}:
            continue
        word_counts[token] += 1

    for left, right in zip(tokens, tokens[1:]):
        if left in STOPWORDS or right in STOPWORDS:
            continue
        if len(left) < 4 or len(right) < 4:
            continue
        if left == right:
            continue
        word_counts[f"{left} {right}"] += 1

    ordered = [item[0] for item in word_counts.most_common(limit * 2)]
    keywords: list[str] = []
    for keyword in ordered:
        if keyword not in keywords:
            keywords.append(keyword)
        if len(keywords) >= limit:
            break

    return keywords[:limit]


def parse_pdf_to_book(pdf_path: Path, metadata: BookMetadata) -> dict:
    """Build the final AI-ready JSON structure from a PDF file."""

    pages = extract_text(pdf_path)
    if not pages:
        raise ValueError("No extractable text was found in the PDF.")

    chapter_title = extract_chapter_title(pages)

    blocks = build_content_blocks(pages, chapter_title=chapter_title)
    if not blocks:
        raise ValueError("No headings or content blocks could be detected in the PDF.")
    chapter_index = find_primary_chapter_index(blocks)
    if chapter_index is None:
        chapter_index = 0

    topics: list[dict[str, object]] = []

    intro_block = blocks[chapter_index]
    intro_topic = build_topic_record("Introduction", intro_block.body)
    if intro_topic:
        topics.append(intro_topic)

    for index, block in enumerate(blocks):
        if index == chapter_index:
            continue
        if not is_valid_topic(block.title):
            continue
        topic = build_topic_record(block.title, block.body)
        if topic:
            topics.append(topic)

    return {
        "board": metadata.board,
        "class": metadata.class_name,
        "subject": metadata.subject,
        "chapter": chapter_title,
        "topics": topics,
    }


def build_content_blocks(page_texts: list[PageText], chapter_title: str | None = None) -> list[ContentBlock]:
    """Split page text into heading-led content blocks."""

    blocks: list[ContentBlock] = []
    current_block: ContentBlock | None = None

    for page in page_texts:
        for raw_line in page.text.splitlines():
            line = clean_noise(raw_line)
            if not line:
                continue

            heading_kind = classify_heading(line)
            if heading_kind:
                # sanitize heading text and validate
                fixed = fix_heading(line)
                if chapter_title and same_heading(fixed, chapter_title):
                    continue
                if re.search(r"(?i)^example\b", fixed) or re.search(r"(?i)\b(?:figure|fig\.)\b", fixed):
                    # treat example and figure titles as body content
                    if current_block is None:
                        current_block = ContentBlock(title="Front Matter", kind="front_matter", page=page.page)
                    current_block.body.append((page.page, fixed))
                    continue

                if not is_valid_topic(fixed) or looks_like_formula(fixed):
                    # treat as normal paragraph if heading is garbage
                    if current_block is None:
                        current_block = ContentBlock(title="Front Matter", kind="front_matter", page=page.page)
                    current_block.body.append((page.page, fixed))
                    continue

                if current_block is not None:
                    blocks.append(current_block)
                current_block = ContentBlock(
                    title=fixed,
                    kind=heading_kind,
                    page=page.page,
                )
                continue

            if current_block is None:
                current_block = ContentBlock(title="Front Matter", kind="front_matter", page=page.page)

            current_block.body.append((page.page, line))

    if current_block is not None:
        blocks.append(current_block)

    return blocks


def build_topic_record(title: str, body: list[tuple[int, str]]) -> dict[str, object] | None:
    """Convert a block body into chunked, typed topic records."""

    chunks = build_chunk_records(body)
    if not chunks:
        return None

    return {"title": title, "chunks": chunks}


def build_chunk_records(body: list[tuple[int, str]]) -> list[dict[str, object]]:
    """Chunk body text while keeping the first contributing page number."""

    sentences: list[tuple[int, str]] = []
    for page, paragraph in body:
        for sentence in split_sentences(paragraph):
            clean_sentence = normalize_spaces(sentence)
            if clean_sentence:
                sentences.append((page, clean_sentence))

    if not sentences:
        return []

    chunks: list[dict[str, object]] = []
    current_sentences: list[str] = []
    current_page = sentences[0][0]
    current_words = 0
    current_mode: str | None = None

    # Target chunk size: 150-250 words
    MIN_WORDS = 150
    MAX_WORDS = 250

    def flush_current():
        nonlocal current_sentences, current_page, current_words
    def flush_current():
        nonlocal current_sentences, current_page, current_words, current_mode
        if not current_sentences:
            return
        chunk_text_value = " ".join(current_sentences).strip()
        if not chunk_text_value:
            current_sentences = []
            current_words = 0
            return

        # clean duplicates and noise inside chunk
        ct = clean_noise(chunk_text_value)
        ct = re.sub(r"\b(\w+)(?:\s+\1\b)+", r"\1", ct, flags=re.IGNORECASE)
        ct = normalize_spaces(ct)
        ct = normalize_final_text(ct)

        if current_mode == "example" or detect_example_chunk(ct):
            if ct:
                chunks.append({
                    "type": "example",
                    "content": ct,
                    "page": current_page,
                    "keywords": generate_keywords(ct),
                })
            current_sentences = []
            current_words = 0
            current_mode = None
            return

        if classify_chunk(ct) == "formula":
            ct = clean_formula_chunk(ct)
            ct = normalize_final_text(ct)
            if not ct:
                current_sentences = []
                current_words = 0
                current_mode = None
                return

        # if chunk mixes example/formula/definition signals, attempt to split along sentence boundaries
        types_in_sentences = [classify_chunk(s) for s in split_sentences(ct)]
        if len(set(types_in_sentences)) > 1:
            # split at change points
            sents = split_sentences(ct)
            temp_sent = []
            temp_page = current_page
            for sent in sents:
                ttype = classify_chunk(sent)
                if temp_sent and classify_chunk(" ".join(temp_sent)) != ttype and word_count(" ".join(temp_sent)) >= MIN_WORDS:
                    piece = normalize_final_text(normalize_spaces(" ".join(temp_sent)))
                    piece_type = classify_chunk(piece)
                    if piece_type == "formula":
                        piece = clean_formula_chunk(piece)
                        piece = normalize_final_text(piece)
                    if piece:
                        chunks.append({
                            "type": piece_type,
                            "content": piece,
                            "page": temp_page,
                            "keywords": generate_keywords(piece),
                        })
                    temp_sent = [sent]
                    temp_page = current_page
                else:
                    temp_sent.append(sent)

            if temp_sent:
                piece = normalize_final_text(normalize_spaces(" ".join(temp_sent)))
                piece_type = classify_chunk(piece)
                if piece_type == "formula":
                    piece = clean_formula_chunk(piece)
                    piece = normalize_final_text(piece)
                if piece:
                    chunks.append({
                        "type": piece_type,
                        "content": piece,
                        "page": temp_page,
                        "keywords": generate_keywords(piece),
                    })
        else:
            piece_type = classify_chunk(ct)
            if piece_type == "formula":
                ct = clean_formula_chunk(ct)
                ct = normalize_final_text(ct)
            if ct:
                chunks.append({
                    "type": piece_type,
                    "content": ct,
                    "page": current_page,
                    "keywords": generate_keywords(ct),
                })

        current_sentences = []
        current_words = 0
        current_mode = None

    for page, sentence in sentences:
        sentence = normalize_final_text(clean_noise(sentence))
        if not sentence:
            continue

        sentence_words = word_count(sentence)

        if detect_example_chunk(sentence):
            if current_sentences:
                flush_current()
            current_mode = "example"
            current_page = page
            current_sentences = [sentence]
            current_words = sentence_words
            continue

        if current_mode == "example":
            current_sentences.append(sentence)
            current_words += sentence_words
            if current_words >= MAX_WORDS:
                flush_current()
            continue

        # if a sentence itself is too large, break it into smaller logical bits by comma
        if sentence_words > MAX_WORDS:
            parts = [p.strip() for p in sentence.split(",") if p.strip()]
            for part in parts:
                part_words = word_count(part)
                if current_sentences and current_words + part_words > MAX_WORDS and current_words >= MIN_WORDS:
                    flush_current()
                current_sentences.append(part)
                current_words += part_words
            continue

        if current_sentences and current_words >= MIN_WORDS and current_words + sentence_words > MAX_WORDS:
            flush_current()

        if not current_sentences:
            current_page = page

        current_sentences.append(sentence)
        current_words += sentence_words

    if current_sentences:
        flush_current()

    return chunks


def same_heading(left: str, right: str) -> bool:
    return normalize_noise_key(left) == normalize_noise_key(right)


def normalize_final_text(text: str) -> str:
    """Apply final readability cleanup to chunk text."""

    if not text:
        return text

    t = normalize_spaces(text)
    t = re.sub(r"\b(\w+)(?:\s+\1\b)+", r"\1", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    t = re.sub(r"([,.;:!?])(?=[A-Za-z])", r"\1 ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def detect_repeated_headers(page_texts: list[PageText]) -> set[str]:
    """Find lines that repeat across pages and are likely headers or footers."""

    candidate_counts: Counter[str] = Counter()
    for page in page_texts:
        lines = [normalize_spaces(line) for line in page.text.splitlines() if normalize_spaces(line)]
        candidates = lines[:3] + lines[-3:]
        for candidate in candidates:
            key = normalize_noise_key(candidate)
            if key and len(key) <= 80:
                candidate_counts[key] += 1

    threshold = max(3, len(page_texts) // 5 + 1)
    repeated = {
        line
        for line, count in candidate_counts.items()
        if count >= threshold and looks_like_header_line(line)
    }
    repeated.update(NORMALIZED_NOISE_PHRASES)
    return repeated


def find_primary_chapter_index(blocks: list[ContentBlock]) -> int | None:
    """Prefer the first explicit chapter-style heading as the document anchor."""

    for index, block in enumerate(blocks):
        if block.kind == "chapter":
            return index
    for index, block in enumerate(blocks):
        if block.kind in {"section", "subtopic", "title"} and block.title != "Front Matter":
            return index
    return None


def infer_chapter_title(blocks: list[ContentBlock], metadata: BookMetadata) -> str:
    """Choose the chapter title from the strongest detected heading."""

    def is_meaningful_all_caps(title: str) -> bool:
        if not title:
            return False
        t = normalize_heading(title)
        if re.search(r"(?i)^chapter\s+\w+", t):
            return False
        words = re.findall(r"[A-Za-z]+", t)
        if len(words) < 2:
            return False
        return t.isupper() or all(w[0].isupper() for w in t.split() if w)

    # Prefer first meaningful title-style heading and ignore "Chapter One".
    for block in blocks:
        title = fix_heading(block.title)
        if re.search(r"(?i)^chapter\s+\w+$", title):
            continue
        if is_meaningful_all_caps(title) and is_valid_topic(title):
            return title

    primary_index = find_primary_chapter_index(blocks)
    if primary_index is not None:
        return fix_heading(normalize_heading(blocks[primary_index].title))

    return fix_heading(normalize_heading(metadata.book_title or metadata.subject))


def classify_heading(line: str) -> str:
    """Return a heading category when a line looks like a structural title."""

    normalized = normalize_heading(line)
    if not normalized:
        return ""

    if is_chapter_heading(normalized):
        return "chapter"
    if is_section_heading(normalized):
        return "section"
    if is_subtopic_heading(normalized):
        return "subtopic"
    if looks_like_title_heading(normalized):
        return "title"
    return ""


def is_chapter_heading(line: str) -> bool:
    match = CHAPTER_PATTERN.match(line)
    if match:
        return True
    return bool(re.fullmatch(r"[A-Z][A-Z0-9 ,:'()/-]{6,80}", line)) and len(line.split()) <= 8


def is_section_heading(line: str) -> bool:
    return bool(NUMBERED_HEADING_PATTERN.match(line)) and len(line) <= 100


def is_subtopic_heading(line: str) -> bool:
    if is_chapter_heading(line) or is_section_heading(line):
        return False
    return bool(re.fullmatch(r"\d+(?:\.\d+){2,}\s+[A-Z].{2,}", line)) or (
        looks_like_title_heading(line) and len(line.split()) <= 9 and len(line) <= 90
    )


def looks_like_title_heading(line: str) -> bool:
    if len(line) > 90:
        return False
    if line.endswith((".", ",", ";", "?", "!")):
        return False
    if any(char.isdigit() for char in line) and not re.search(r"\d+(?:\.\d+)+", line):
        return False
    words = line.split()
    if not words or len(words) > 12:
        return False
    if sum(1 for word in words if word and word[0].isupper()) >= max(2, len(words) // 2):
        return True
    return line.isupper() and len(words) <= 10


def looks_like_formula(text: str) -> bool:
    # Avoid false positives from long narrative/biography paragraphs.
    if word_count(text) > 120 and not re.search(r"=|\^|×|÷|∑|∫|√|≈|≤|≥", text):
        return False

    sci_number = re.search(r"\b\d+(?:\.\d+)?\s*[x×]\s*10\s*\^?\s*-?\d+\b", text, re.IGNORECASE)
    equation_like = re.search(r"\b[A-Za-z][A-Za-z0-9_]*\s*=\s*[^=]", text)
    symbolic = re.search(r"[=^×÷∑∫√≈≤≥]", text)

    if equation_like:
        return True
    if sci_number and symbolic:
        return True
    if symbolic and re.search(r"\b\d+(?:\.\d+)?\b", text) and word_count(text) <= 80:
        return True
    return False


def split_sentences(text: str) -> list[str]:
    """Split text without cutting through sentence boundaries."""

    normalized = normalize_spaces(text)
    if not normalized:
        return []
    sentences = [sentence.strip() for sentence in SENTENCE_SPLIT_PATTERN.split(normalized) if sentence.strip()]
    return sentences or [normalized]


def rebalance_chunks(chunks: list[str], min_words: int, max_words: int) -> list[str]:
    """Merge tiny tail chunks when that does not break the upper bound badly."""

    if len(chunks) < 2:
        return chunks

    rebalance = list(chunks)
    if word_count(rebalance[-1]) < min_words:
        merged = f"{rebalance[-2]} {rebalance[-1]}".strip()
        if word_count(merged) <= max_words + 40:
            rebalance[-2] = merged
            rebalance.pop()
    return rebalance


def merge_broken_lines(lines: list[str]) -> list[str]:
    """Repair hyphenated words and heading fragments split across lines."""

    merged: list[str] = []
    for line in lines:
        if not merged:
            merged.append(line)
            continue

        previous = merged[-1]
        if previous.endswith("-") and line and line[0].islower():
            merged[-1] = previous[:-1] + line
            continue

        if is_heading_fragment(previous) and is_heading_fragment(line) and len(previous) + len(line) + 1 <= 120:
            merged[-1] = f"{previous} {line}".strip()
            continue

        merged.append(line)

    return merged


def merge_paragraphs(lines: list[str]) -> list[str]:
    """Merge wrapped lines into paragraphs while preserving heading lines."""

    paragraphs: list[str] = []
    current: list[str] = []

    for line in lines:
        if classify_heading(line):
            if current:
                paragraphs.append(" ".join(current).strip())
                current = []
            paragraphs.append(line)
            continue

        if not current:
            current.append(line)
            continue

        if should_join_line(current[-1], line):
            current[-1] = f"{current[-1].rstrip('- ')} {line}".strip()
        else:
            current.append(line)

        if ends_paragraph(current[-1]):
            paragraphs.append(" ".join(current).strip())
            current = []

    if current:
        paragraphs.append(" ".join(current).strip())

    return [paragraph for paragraph in paragraphs if paragraph]


def should_join_line(previous: str, line: str) -> bool:
    if previous.endswith(("-", ":", ";", "(", "/")):
        return True
    if line and line[0].islower():
        return True
    if len(previous) < 35 and not previous.endswith((".", "?", "!")):
        return True
    return False


def ends_paragraph(line: str) -> bool:
    return line.endswith((".", "?", "!")) and len(line.split()) >= 10


def is_heading_fragment(line: str) -> bool:
    if len(line) > 80:
        return False
    if line.endswith((".", ",", ";", "?", "!", ":")):
        return False
    if re.fullmatch(r"[A-Z0-9 ,:'()/-]+", line):
        return True
    return line.istitle() and len(line.split()) <= 8


def looks_like_header_line(line: str) -> bool:
    return normalize_noise_key(line) in NORMALIZED_NOISE_PHRASES or (
        len(line) <= 80 and not any(char.isdigit() for char in line) and line.split()
    )


def normalize_noise_key(value: str) -> str:
    return normalize_spaces(value).lower().strip(" .:-")


def normalize_heading(line: str) -> str:
    return normalize_spaces(line).strip(" .:-")


def normalize_spaces(value: str) -> str:
    return MULTISPACE_PATTERN.sub(" ", value).strip()


def word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


NORMALIZED_NOISE_PHRASES = {normalize_noise_key(value) for value in NOISE_PHRASES}


def serialize_structured_json(payload: dict) -> str:
    """Serialize the final JSON using the standard library json module."""

    return json.dumps(payload, indent=2, ensure_ascii=False)
