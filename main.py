import os
import uuid
import threading
import shutil
import re
import json
from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# Import converter and takeoff services
from services.converter_service import ConverterService
from services.takeoff_service import TakeoffService

# Load environment variables
load_dotenv(override=True)

app = FastAPI(title="AI Takeoff Pro - Architectural Blueprint & Estimation Engine")

# Enable CORS for local testing
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)

# Mount static files
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/outputs", StaticFiles(directory=OUTPUT_DIR), name="outputs")

# In-memory job state
JOBS = {}


def run_conversion_task(job_id: str, pdf_path: str, filename: str, pdf_hash: str):
    job = JOBS[job_id]
    job["status"] = "processing"

    scratch_dir = os.path.join(UPLOAD_DIR, f"cache_{pdf_hash}")
    output_md_filename = f"{os.path.splitext(filename)[0]}.md"
    output_md_path = os.path.join(OUTPUT_DIR, output_md_filename)

    converter = ConverterService()

    def update_status(message: str):
        print(f"[Job {job_id}] {message}")
        job["logs"].append(message)
        if "Splitting PDF" in message:
            job["progress"] = 15
        elif "Transcribing all" in message:
            job["progress"] = 45
        elif "Processing Page" in message or "Page " in message:
            try:
                parts = message.split("Page ")[1].split(" of ")
                curr = int(parts[0])
                total = int(parts[1].split("...")[0].split(":")[0])
                job["progress"] = int(15 + (curr / total) * 80)
            except Exception:
                pass

    try:
        # Perform actual generic multi-page conversion
        update_status("Splitting PDF pages to PNG images...")
        markdown_text = converter.convert_pdf_to_markdown(
            pdf_path=pdf_path,
            output_md_path=output_md_path,
            scratch_dir=scratch_dir,
            status_callback=update_status,
        )

        job["progress"] = 100
        job["status"] = "completed"
        job["markdown"] = markdown_text
        job["output_filename"] = output_md_filename
        job["pages_count"] = len([f for f in os.listdir(scratch_dir) if re.match(r"^page_\d+\.png$", f)])
        update_status("Conversion complete!")

    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)
        update_status(f"Error during conversion: {e}")


@app.get("/", response_class=HTMLResponse)
async def get_index():
    index_path = os.path.join(TEMPLATES_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return f.read()
    return """
    <html>
        <body>
            <h2>AI Takeoff Pro</h2>
            <p>Templates folder not initialized or index.html missing.</p>
        </body>
    </html>
    """


@app.post("/convert-pdf")
async def convert_pdf(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    job_id = str(uuid.uuid4())
    pdf_filename = f"{job_id}_{file.filename}"
    pdf_path = os.path.join(UPLOAD_DIR, pdf_filename)

    with open(pdf_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    import hashlib
    hasher = hashlib.sha256()
    with open(pdf_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    pdf_hash = hasher.hexdigest()

    JOBS[job_id] = {
        "status": "starting",
        "progress": 0,
        "logs": ["Initialised conversion task..."],
        "filename": file.filename,
        "pdf_url": f"/uploads/{pdf_filename}",
        "scratch_url": f"/uploads/cache_{pdf_hash}",
    }

    background_tasks.add_task(run_conversion_task, job_id, pdf_path, file.filename, pdf_hash)
    return {"job_id": job_id}


@app.get("/job-status/{job_id}")
async def get_job_status(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")
    return JOBS[job_id]


@app.get("/download-md/{job_id}")
async def download_md(job_id: str):
    if job_id not in JOBS or JOBS[job_id]["status"] != "completed":
        raise HTTPException(status_code=400, detail="Markdown file is not ready or job failed")

    filename = JOBS[job_id]["output_filename"]
    filepath = os.path.join(OUTPUT_DIR, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Markdown file not found on disk")

    return FileResponse(filepath, media_type="text/markdown", filename=filename)


@app.get("/download-json/{job_id}")
async def download_json(job_id: str):
    if job_id not in JOBS or JOBS[job_id]["status"] != "completed":
        raise HTTPException(status_code=400, detail="JSON file is not ready or job failed")

    md_filename = JOBS[job_id]["output_filename"]
    filename = os.path.splitext(md_filename)[0] + ".json"
    filepath = os.path.join(OUTPUT_DIR, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="JSON file not found on disk")

    return FileResponse(filepath, media_type="application/json", filename=filename)


@app.get("/download-html/{job_id}")
async def download_html(job_id: str):
    if job_id not in JOBS or JOBS[job_id]["status"] != "completed":
        raise HTTPException(status_code=400, detail="HTML file is not ready or job failed")

    md_filename = JOBS[job_id]["output_filename"]
    filename = os.path.splitext(md_filename)[0] + ".html"
    filepath = os.path.join(OUTPUT_DIR, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="HTML file not found on disk")

    return FileResponse(filepath, media_type="text/html", filename=filename)


@app.get("/kb-materials")
async def get_kb_materials():
    """Returns the list of materials from the knowledge base for frontend use."""
    kb_path = os.path.join(BASE_DIR, "services", "material_kb.json")
    if not os.path.exists(kb_path):
        return {"materials": []}
    with open(kb_path, "r", encoding="utf-8") as f:
        kb = json.load(f)
    materials = [
        {"key": key, "standard_name": entry.get("standard_name", key)}
        for key, entry in kb.items()
    ]
    return {"materials": materials}


from pydantic import BaseModel

class IdentifyMaterialRequest(BaseModel):
    description: str
    job_id: str = None


class TakeoffRequest(BaseModel):
    job_id: str
    material: str
    current_page: int = None


@app.post("/identify-material")
async def identify_material(req: IdentifyMaterialRequest):
    try:
        takeoff_service = TakeoffService()
        result = takeoff_service.identify_material_from_description(
            description=req.description, job_id=req.job_id
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to identify material: {str(e)}")



@app.get("/active-job")
async def get_active_job():
    for jid, job in JOBS.items():
        if job.get("status") == "completed" and job.get("scratch_url"):
            return {
                "job_id": jid,
                "scratch_url": job["scratch_url"],
                "pages_count": job.get("pages_count", 0),
                "filename": job.get("filename", ""),
            }
    # Fallback to existing cache directories in uploads/
    cache_dirs = [d for d in os.listdir(UPLOAD_DIR) if d.startswith("cache_") and os.path.isdir(os.path.join(UPLOAD_DIR, d))]
    if cache_dirs:
        latest_cache = cache_dirs[-1]
        scratch_dir = os.path.join(UPLOAD_DIR, latest_cache)
        pages = len([f for f in os.listdir(scratch_dir) if re.match(r"^page_\d+\.png$", f)])
        pdf_files = [f for f in os.listdir(UPLOAD_DIR) if f.endswith(".pdf")]
        filename = pdf_files[0] if pdf_files else "blueprint.pdf"
        clean_name = re.sub(r"^[0-9a-fA-F-]{36}_", "", filename)
        return {
            "job_id": latest_cache.replace("cache_", ""),
            "scratch_url": f"/uploads/{latest_cache}",
            "pages_count": pages,
            "filename": clean_name,
        }
    return {}


@app.post("/takeoff")
async def run_takeoff(req: TakeoffRequest):
    if req.job_id not in JOBS:
        cache_dirs = [d for d in os.listdir(UPLOAD_DIR) if d.startswith("cache_") and os.path.isdir(os.path.join(UPLOAD_DIR, d))]
        if cache_dirs:
            latest_cache = cache_dirs[-1]
            scratch_dir = os.path.join(UPLOAD_DIR, latest_cache)
            pages_count = len([f for f in os.listdir(scratch_dir) if re.match(r"^page_\d+\.png$", f)])
            pdf_files = [f for f in os.listdir(UPLOAD_DIR) if f.endswith(".pdf")]
            pdf_name = pdf_files[0] if pdf_files else "blueprint.pdf"
            clean_name = re.sub(r"^[0-9a-fA-F-]{36}_", "", pdf_name)
            JOBS[req.job_id] = {
                "status": "completed",
                "filename": clean_name,
                "scratch_url": f"/uploads/{latest_cache}",
                "pages_count": pages_count,
            }
        else:
            raise HTTPException(status_code=404, detail="Job not found")

    job = JOBS[req.job_id]

    filename = job["filename"]
    pdf_filename = f"{req.job_id}_{filename}"
    pdf_path = os.path.join(UPLOAD_DIR, pdf_filename)
    if not os.path.exists(pdf_path):
        # Fallback to any matching pdf in uploads
        pdf_files = [f for f in os.listdir(UPLOAD_DIR) if f.endswith(".pdf")]
        if pdf_files:
            pdf_path = os.path.join(UPLOAD_DIR, pdf_files[0])

    scratch_hash = job["scratch_url"].split("cache_")[-1]
    scratch_dir = os.path.join(UPLOAD_DIR, f"cache_{scratch_hash}")

    md_filename = f"{os.path.splitext(filename)[0]}.md"
    json_filename = os.path.splitext(md_filename)[0] + ".json"
    json_path = os.path.join(OUTPUT_DIR, json_filename)

    if not os.path.exists(json_path):
        raise HTTPException(status_code=400, detail="Processed document JSON is not ready yet.")

    with open(json_path, "r", encoding="utf-8") as f:
        json_data = json.load(f)

    takeoff_service = TakeoffService()
    matched_pages = takeoff_service.find_pages_for_material(
        json_data, req.material, scratch_dir, current_page=req.current_page
    )

    # Fallback to current page if no matching page is found in pre-processed text
    if not matched_pages and req.current_page:
        matched_pages = [req.current_page]

    if not matched_pages:
        return {"material": req.material, "matched_pages": [], "results": []}

    results = []
    for page_num in matched_pages:
        page_md = ""
        page_md_path = os.path.join(scratch_dir, f"page_{page_num}.md")
        if os.path.exists(page_md_path):
            with open(page_md_path, "r", encoding="utf-8") as f:
                page_md = f.read()

        page_json = {}
        for p in json_data.get("pages", []):
            if p.get("page_number") == page_num:
                page_json = p
                break

        try:
            metadata_json_path = os.path.join(scratch_dir, "metadata_extraction.json")
            takeoff_data = takeoff_service.run_takeoff_for_material(
                pdf_path=pdf_path,
                scratch_dir=scratch_dir,
                page_num=page_num,
                page_md=page_md,
                page_json=page_json,
                full_json_data=json_data,
                material_query=req.material,
                metadata_json_path=metadata_json_path,
            )
            results.append({
                "page_number": page_num,
                "sheet_number": page_json.get("sheetNumber", f"PAGE-{page_num}"),
                "sheet_title": page_json.get("title", "Untitled"),
                "takeoff": takeoff_data,
            })
        except Exception as e:
            print(f"Takeoff failed for page {page_num}: {e}")
            results.append({
                "page_number": page_num,
                "sheet_number": page_json.get("sheetNumber", f"PAGE-{page_num}"),
                "sheet_title": page_json.get("title", "Untitled"),
                "error": str(e),
            })

    # Save the updated JSON data containing resolved buildingPerimeter and foundationWallDepth back to outputs/
    try:
        import copy
        cleaned_json = copy.deepcopy(json_data)
        for p in cleaned_json.get("pages", []):
            p.pop("buildingPerimeter", None)
            p.pop("foundationWallDepth", None)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(cleaned_json, f, indent=2, ensure_ascii=False)
        print(f"[Main] Saved updated takeoff dimensions to output: {json_path}")
    except Exception as e:
        print(f"[Main] Error saving updated JSON output: {e}")

    total_pages_found = 0
    try:
        total_pages_found = len([f for f in os.listdir(scratch_dir) if re.match(r"^page_\d+\.png$", f)])
    except Exception:
        pass

    return {
        "material": req.material,
        "matched_pages": matched_pages,
        "results": results,
        "scratch_url": f"/uploads/cache_{scratch_hash}",
        "pages_count": total_pages_found or job.get("pages_count", 0),
        "job_id": req.job_id,
    }
