# PDF Text Structurer

A small web app that uploads a textbook PDF, accepts manual metadata such as board, class, and subject, extracts text, and stores the result as structured JSON:

```text
board -> class -> subject -> chapters -> subtopics -> contents
```

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app/main.py
```

Or, from the project root:

```powershell
uvicorn app.main:app --reload
```

Open:

```text
http://127.0.0.1:8000
```

Uploaded PDFs are stored in `storage/uploads/`.
Generated JSON files are stored in `storage/outputs/`.

## Notes

This version extracts selectable PDF text with pdfplumber. Scanned books still need OCR support later, for example with Tesseract or a vision/OCR API.

The parser uses textbook-style heading heuristics:

- chapter headings like `Chapter 1`, `Unit 2`, `1. Introduction`
- subtopics like `1.1`, `1.2.3`, or short title-like heading lines

For production quality, keep this pipeline:

1. Extract raw pages.
2. Clean headers, footers, page numbers, and repeated noise.
3. Detect table of contents when available.
4. Split chapters.
5. Split subtopics.
6. Store JSON with page references.
7. Review/edit JSON before final save.
