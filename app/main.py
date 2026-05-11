import json
from pathlib import Path
from uuid import uuid4

if __package__ in {None, ""}:
    import sys

    sys.path.append(str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.parser import BookMetadata, parse_pdf_to_book


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
STORAGE_DIR = BASE_DIR / "storage"
UPLOAD_DIR = STORAGE_DIR / "uploads"
OUTPUT_DIR = STORAGE_DIR / "outputs"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="PDF Text Structurer")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/extract")
async def extract_book(
    board: str = Form(...),
    class_name: str = Form(...),
    subject: str = Form(...),
    book_title: str = Form(""),
    file: UploadFile = File(...),
) -> JSONResponse:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a PDF file.")

    upload_id = uuid4().hex
    safe_name = Path(file.filename).name
    pdf_path = UPLOAD_DIR / f"{upload_id}-{safe_name}"
    output_path = OUTPUT_DIR / f"{upload_id}.json"

    contents = await file.read()
    pdf_path.write_bytes(contents)

    metadata = BookMetadata(
        board=board.strip(),
        class_name=class_name.strip(),
        subject=subject.strip(),
        book_title=book_title.strip() or Path(file.filename).stem,
        source_file=safe_name,
    )

    try:
        book = parse_pdf_to_book(pdf_path, metadata)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - defensive API guard
        raise HTTPException(status_code=500, detail="Failed to process the PDF.") from exc

    output_path.write_text(json.dumps(book, indent=2, ensure_ascii=False), encoding="utf-8")

    return JSONResponse(
        {
            "id": upload_id,
            "jsonUrl": f"/api/outputs/{upload_id}",
            "book": book,
        }
    )


@app.get("/api/outputs/{output_id}")
def get_output(output_id: str) -> FileResponse:
    output_path = OUTPUT_DIR / f"{output_id}.json"
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="Output not found.")
    return FileResponse(output_path, media_type="application/json", filename=f"{output_id}.json")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
