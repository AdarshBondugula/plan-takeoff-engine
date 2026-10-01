import os
import json
import time
import re
import urllib.request
import urllib.error
import fitz  # PyMuPDF
from PIL import Image
from io import BytesIO

# Import our new pipeline modules
from services.pdf_extractor import PDFExtractor
from services.markdown_cleaner import MarkdownCleaner
from services.engineering_extractor import EngineeringExtractor

class ConverterService:
    def __init__(self):
        self.api_key = os.getenv("OPENROUTER_API_KEY")
        model = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")
        self.models = [model]

    def convert_pdf_to_images(self, pdf_path: str, output_dir: str) -> list:
        """
        Converts each page of a PDF into high-res PNG images. (Backward compatibility wrapper)
        Returns a list of tuples: (page_number, image_path)
        """
        extractor = PDFExtractor(dpi=150)  # Use 150 DPI for direct compatibility rendering
        pages_data = extractor.extract_pdf_data(pdf_path, output_dir, render_images=True)
        return [(page["page_number"], page["image_path"]) for page in pages_data]

    def extract_embedded_images(self, pdf_path: str, output_dir: str) -> list:
        """
        Extracts embedded raster images from each page. (Backward compatibility wrapper)
        """
        doc = fitz.open(pdf_path)
        extractor = PDFExtractor()
        extracted = extractor.extract_embedded_images(doc, output_dir)
        doc.close()
        return extracted

    def _needs_vision_transcription(self, cleaned_md: str, page_data: dict) -> bool:
        """
        Determines whether a page should be sent to the vision LLM for transcription.

        Triggers vision if:
        - The extracted MD is short (< 300 chars)
        - OR the MD is sparse in AEC content indicators (measurements, scales, callouts)
          while the page image is non-trivial (i.e., has tables or embedded images that suggest drawings)
        """
        text = cleaned_md.strip() if cleaned_md else ""
        char_count = len(text)

        # Short page — always transcribe
        if char_count < 300:
            return True

        # Medium page: check if it lacks measurement/scale content but the page has drawing tables/images
        if char_count < 2000:
            import re
            # Count AEC-specific indicators: ±, mm, m², scales like 1:75, dimension-style numbers
            aec_pattern = re.compile(
                r'(±|\bm²\b|\bmm\b|\bcm\b|\bft\b|\bin\b|1:\d+|\d+\'|\d+"'  # measurements
                r'|ÉCHELLE|SCALE|PHOTO\s*\d+|[A-Z]-\d{3})',                   # AEC labels
                re.IGNORECASE
            )
            aec_hits = len(aec_pattern.findall(text))
            has_tables = len(page_data.get("tables", [])) > 0
            has_embedded = len(page_data.get("embedded_images", [])) > 0

            # If fewer than 3 AEC indicators but page has tables/images, vision will give better results
            if aec_hits < 3 and (has_tables or has_embedded):
                return True

        return False

    def _reconstruct_metadata_offline(self, scratch_dir: str, doc_name: str, total_pages: int, cache_json_path: str) -> dict:
        """
        Reconstructs the document-level metadata JSON completely offline
        by aggregating individual per-page engineering extraction caches.
        """
        print(f"[MetadataExtractor] Reconstructing document metadata offline from per-page cached engineering JSONs...")
        json_data = {
            "document_name": doc_name,
            "model_used": "offline-reconstruction",
            "pages": []
        }
        
        for page_num in range(1, total_pages + 1):
            page_eng_json_path = os.path.join(scratch_dir, f"page_{page_num}_engineering.json")
            if os.path.exists(page_eng_json_path):
                try:
                    with open(page_eng_json_path, "r", encoding="utf-8") as f:
                        eng = json.load(f)
                    
                    # Map per-page engineering objects to elements format
                    elements = []
                    for obj in eng.get("objects", []):
                        elements.append({
                            "name": obj.get("type", ""),
                            "type": obj.get("type", ""),
                            "status": obj.get("status", "Preserve"),
                            "description": obj.get("description", "")
                        })

                    # Map measurements list format
                    measurements = []
                    for m in eng.get("measurements", []):
                        measurements.append({
                            "value": m.get("value", ""),
                            "unit": m.get("unit", ""),
                            "measurementType": m.get("measurementType", "linear"),
                            "associatedElement": m.get("nearestObject", "")
                        })

                    # Map instructions list format
                    instructions = []
                    for ins in eng.get("instructions", []):
                        instructions.append({
                            "action": ins.get("action", "Unknown"),
                            "element": ins.get("element", ""),
                            "originalWording": ins.get("originalWording", "")
                        })

                    page_entry = {
                        "page_number": page_num,
                        "sheetNumber": eng.get("page", {}).get("sheetNumber", f"PAGE-{page_num}"),
                        "title": eng.get("page", {}).get("title", f"Page {page_num}"),
                        "discipline": eng.get("page", {}).get("discipline", "Unknown"),
                        "drawingType": eng.get("page", {}).get("drawingType", "Mixed"),
                        "phase": eng.get("page", {}).get("phase", "Unknown"),
                        "scale": eng.get("page", {}).get("scale", "N/A"),
                        "metadata": eng.get("metadata", {}),
                        "revisions": eng.get("revisions", []),
                        "compliance": eng.get("compliance", {}),
                        "generalNotes": eng.get("generalNotes", []),
                        "measurements": measurements,
                        "elements": elements,
                        "instructions": instructions,
                        "engineering_objects": eng.get("objects", []),
                        "engineering_views": eng.get("views", []),
                        "engineering_references": eng.get("references", []),
                        "engineering_photos": eng.get("photos", [])
                    }
                    json_data["pages"].append(page_entry)
                except Exception as parse_err:
                    print(f"Error parsing cached engineering JSON for page {page_num}: {parse_err}")
            else:
                # Placeholder if no per-page extraction was generated for this page
                json_data["pages"].append({
                    "page_number": page_num,
                    "sheetNumber": f"PAGE-{page_num}",
                    "title": f"Page {page_num}",
                    "discipline": "Unknown",
                    "drawingType": "Mixed",
                    "phase": "Unknown",
                    "scale": "N/A",
                    "metadata": {},
                    "revisions": [],
                    "compliance": {},
                    "generalNotes": [],
                    "measurements": [],
                    "elements": [],
                    "instructions": [],
                    "engineering_objects": [],
                    "engineering_views": [],
                    "engineering_references": [],
                    "engineering_photos": []
                })
        
        # Save reconstructed metadata to cache
        if json_data.get("pages"):
            try:
                with open(cache_json_path, "w", encoding="utf-8") as f:
                    json.dump(json_data, f, indent=2, ensure_ascii=False)
            except Exception as cache_err:
                print(f"Failed to cache reconstructed metadata: {cache_err}")
                
        return json_data

    def _parse_sheet_metadata_from_page_text(self, page_num: int, text: str) -> dict:
        sheet_number = f"PAGE-{page_num}"
        title = f"Page {page_num}"
        drawing_type = "Mixed"

        # Match sheet number pattern like A-001, A-101, A-201, A-401, A-501, S-101, etc.
        match_sheet = re.search(r'\b([A-Z]-\d{3,4})\b', text)
        if match_sheet:
            sheet_number = match_sheet.group(1).upper()

        text_upper = text.upper()
        if any(k in text_upper for k in ["FRONTISPICE", "LISTE DES PLANS", "COVER", "INDEX", "TITLE SHEET", "TITLE PAGE", "DRAWING LIST"]):
            drawing_type = "Cover"
            title = "Cover Sheet"
        elif any(k in text_upper for k in ["COUPE DE MUR", "COUPE ET DÉTAILS", "COUPE", "SECTION", "DÉTAIL", "DETAIL", "WALL SECTION"]):
            drawing_type = "Section"
            title = "Section & Details"
        elif any(k in text_upper for k in ["ÉLÉVATIONS", "ELEVATION", "ÉLÉVATION", "FACADE", "FAÇADE"]):
            drawing_type = "Elevation"
            title = "Elevations"
        elif any(k in text_upper for k in ["IMPLANTATION", "SITE PLAN", "CADASTRAL PLAN", "ARPENTAGE"]):
            drawing_type = "Site Plan"
            title = "Site Plan"
        elif any(k in text_upper for k in ["PLAN DE TOITURE", "PLAN DE TOIT", "ROOF PLAN"]):
            drawing_type = "Roof Plan"
            title = "Roof Plan"
        elif any(k in text_upper for k in ["PLAN DE FONDATION", "PLAN DES FONDATIONS", "PLAN FONDATION", "FOUNDATION PLAN"]):
            drawing_type = "Floor Plan"
            title = "Foundation Plan"
        elif any(k in text_upper for k in ["PLAN DU REZ", "PLAN REZ-DE-CHAUSSÉE", "REZ-DE-CHAUSSÉE", "PLAN DU RDC", "GROUND FLOOR PLAN"]):
            drawing_type = "Floor Plan"
            title = "Ground Floor Plan"
        elif any(k in text_upper for k in ["PLAN DU PREMIER", "PLAN D'ÉTAGE", "PLAN SOUS-SOL", "BASEMENT PLAN", "FLOOR PLAN"]):
            drawing_type = "Floor Plan"
            title = "Floor Plan"

        return {
            "sheetNumber": sheet_number,
            "title": title,
            "drawingType": drawing_type,
        }

    def _enrich_json_metadata_from_page_texts(self, json_data: dict, extracted_mds: dict) -> dict:
        if not json_data:
            json_data = {"pages": []}
        existing_nums = {p.get("page_number") for p in json_data.get("pages", [])}

        # Add missing page objects if json_data was empty or missing pages
        for p_num in sorted(extracted_mds.keys()):
            if p_num not in existing_nums:
                json_data["pages"].append({
                    "page_number": p_num,
                    "sheetNumber": f"PAGE-{p_num}",
                    "title": f"Page {p_num}",
                    "drawingType": "Mixed"
                })

        # Post-process every page to ensure accurate sheetNumber, title, drawingType, perimeter and depth
        for p in json_data.get("pages", []):
            p_num = p.get("page_number")
            text = extracted_mds.get(p_num, "")
            parsed = self._parse_sheet_metadata_from_page_text(p_num, text)

            if not p.get("sheetNumber") or str(p.get("sheetNumber")).startswith("PAGE-") or p.get("sheetNumber") == "PAGE-0":
                p["sheetNumber"] = parsed["sheetNumber"]
            if not p.get("title") or str(p.get("title")).startswith("Page "):
                p["title"] = parsed["title"]
            if not p.get("drawingType") or p.get("drawingType") in ["Mixed", "Unknown"]:
                p["drawingType"] = parsed["drawingType"]

            sheet = str(p.get("sheetNumber", "")).upper()
            title = str(p.get("title", "")).upper()
            dtype = str(p.get("drawingType", "")).upper()

        return json_data

    def _generate_enriched_markdown(self, page_entry: dict, original_md: str, perimeter_data: dict = None, depth_data: dict = None) -> str:
        """
        Appends structured engineering metadata, measurements, elements, and
        instructions to the end of the page's markdown.
        """
        def clean_val(v, default="N/A") -> str:
            if v is None:
                return default
            s = str(v).strip()
            if not s:
                return default
            return s.replace("\n", " ").replace("|", "\\|")

        lines = [original_md.strip()]
        lines.append("")
        lines.append("---")
        
        page_num = page_entry.get("page_number", 0)
        lines.append(f"### Page {page_num} Structured Engineering Details")
        lines.append("")
        
        # 1. Sheet Metadata
        lines.append("#### Sheet Metadata")
        lines.append(f"- **Sheet Number:** {clean_val(page_entry.get('sheetNumber'))}")
        lines.append(f"- **Title:** {clean_val(page_entry.get('title'))}")
        lines.append(f"- **Discipline:** {clean_val(page_entry.get('discipline'))}")
        lines.append(f"- **Drawing Type:** {clean_val(page_entry.get('drawingType'))}")
        lines.append(f"- **Phase:** {clean_val(page_entry.get('phase'))}")
        lines.append(f"- **Scale:** {clean_val(page_entry.get('scale'))}")
        
        # Project metadata
        meta = page_entry.get("metadata", {})
        if meta:
            lines.append(f"- **Project Name:** {clean_val(meta.get('projectName'))}")
            lines.append(f"- **Location:** {clean_val(meta.get('location'))}")
            lines.append(f"- **Architect:** {clean_val(meta.get('architect'))}")
            lines.append(f"- **Project ID:** {clean_val(meta.get('projectID'))}")

        bp = perimeter_data if (perimeter_data and perimeter_data.get("source_page") == page_num) else None
        if bp and bp.get("value_ft"):
            lines.append(f"- **Building Perimeter:** {bp.get('value_ft')} ft ({bp.get('value_m')} m) — {clean_val(bp.get('calculation'))}")

        fd = depth_data if (depth_data and depth_data.get("source_page") == page_num) else None
        if fd and fd.get("value_ft"):
            lines.append(f"- **Foundation Wall Depth:** {fd.get('value_ft')} ft ({fd.get('value_m')} m) — {clean_val(fd.get('description'))}")

        lines.append("")

        # 1b. Code Compliance (Part 10 table)
        comp = page_entry.get("compliance", {})
        if comp and any(comp.values()):
            lines.append("#### Code Compliance (Part 10)")
            lines.append(f"- **Original Construction Year:** {clean_val(comp.get('originalConstructionYear'))}")
            lines.append(f"- **Intervention Type:** {clean_val(comp.get('interventionType'))}")
            lines.append(f"- **Is Regulated:** {clean_val(comp.get('isRegulated'))}")
            articles = comp.get('codeArticles', [])
            if articles:
                lines.append(f"- **Code Articles:** {', '.join(clean_val(art) for art in articles)}")
            lines.append("")

        # 1c. Sheet Revisions
        revs = page_entry.get("revisions", [])
        if revs:
            lines.append("#### Sheet Revisions")
            lines.append("| No. | Description | Date |")
            lines.append("| :--- | :--- | :--- |")
            for r in revs:
                no = clean_val(r.get("revisionNumber"))
                desc = clean_val(r.get("description"))
                date_val = clean_val(r.get("date"))
                lines.append(f"| {no} | {desc} | {date_val} |")
            lines.append("")

        # 2. General Notes
        notes = page_entry.get("generalNotes", [])
        if notes:
            lines.append("#### General Notes")
            for note in notes:
                if note:
                    cleaned_note = str(note).strip().replace("\n", " ")
                    if cleaned_note:
                        lines.append(f"- {cleaned_note}")
            lines.append("")

        # 3. Measurements
        measurements = page_entry.get("measurements", [])
        if measurements:
            lines.append("#### Measurements & Dimensions")
            lines.append("| Value | Unit | Type | Associated Element / Context |")
            lines.append("| :--- | :--- | :--- | :--- |")
            for m in measurements:
                val = clean_val(m.get("value"))
                unit = clean_val(m.get("unit"), default="dim")
                m_type = clean_val(m.get("measurementType"), default="linear")
                assoc = m.get("associatedElement") or m.get("nearestObject")
                assoc_cleaned = clean_val(assoc, default="Unknown Context")
                lines.append(f"| {val} | {unit} | {m_type} | {assoc_cleaned} |")
            lines.append("")

        # 4. Elements & Objects
        elements = page_entry.get("elements", [])
        if elements:
            lines.append("#### Elements & Objects")
            lines.append("| Element | Type | Status | Description |")
            lines.append("| :--- | :--- | :--- | :--- |")
            for e in elements:
                name = clean_val(e.get("name"))
                e_type = clean_val(e.get("type"))
                status = clean_val(e.get("status"))
                desc = clean_val(e.get("description"), default="")
                lines.append(f"| {name} | {e_type} | {status} | {desc} |")
            lines.append("")

        # 5. Key Instructions
        instructions = page_entry.get("instructions", [])
        if instructions:
            lines.append("#### Key Instructions")
            for inst in instructions:
                action = clean_val(inst.get("action"), default="Unknown")
                elem = clean_val(inst.get("element"))
                orig = clean_val(inst.get("originalWording"), default="")
                if orig:
                    lines.append(f"- **{action}** {elem} (Original: *{orig}*)")
                else:
                    lines.append(f"- **{action}** {elem}")
            lines.append("")

        return "\n".join(lines)

    def convert_pdf_to_markdown(self, pdf_path: str, output_md_path: str, scratch_dir: str, status_callback=None) -> str:
        """
        Modular Multi-Stage Pipeline orchestrator.

        Stage 1: PyMuPDF + pymupdf4llm text extraction
        Stage 2: Markdown cleaning (remove repeated headers/footers)
        Stage 3: Combined Markdown -> document-level metadata JSON (via OpenRouter)
        """
        total_start_time = time.time()

        # Initialize the pipeline modules
        pdf_extractor = PDFExtractor()
        markdown_cleaner = MarkdownCleaner()
        engineering_extractor = EngineeringExtractor()

        # Step 1: PyMuPDF + pymupdf4llm Extraction (No image rendering)
        if status_callback:
            status_callback("Stage 1: Running PyMuPDF + pymupdf4llm extraction...")
        pages_extractor_data = pdf_extractor.extract_pdf_data(pdf_path, scratch_dir, render_images=True)
        total_pages = len(pages_extractor_data)


        # Step 2: Markdown Cleaning & Pre-processing
        if status_callback:
            status_callback("Stage 2: Cleaning extracted Markdown and removing headers/footers...")
        pages_extractor_data = markdown_cleaner.clean_pages(pages_extractor_data)

        # Build raw/cleaned markdown map for HTML preview and file outputs
        extracted_mds = {}
        markdown_parts = []

        for p in pages_extractor_data:
            page_num = p["page_number"]
            cleaned_md = p["cleaned_markdown"]

            # Caching check for page markdown
            page_md_path = os.path.join(scratch_dir, f"page_{page_num}.md")
            page_md_cached = False
            if os.path.exists(page_md_path):
                try:
                    with open(page_md_path, "r", encoding="utf-8") as f:
                        cached = f.read()
                    cached_stripped = cached.strip()
                    if cached_stripped and not cached_stripped.startswith(("Warning:", "Error:")):
                        cleaned_md = cached
                        page_md_cached = True
                        if status_callback:
                            status_callback(
                                f"Page {page_num}/{total_pages}: Loaded cached page MD "
                                f"({len(cleaned_md.strip())} chars)."
                            )
                except Exception as e:
                    print(f"Error loading cached page {page_num} markdown: {e}")

            if not page_md_cached:
                if status_callback:
                    status_callback(
                        f"Page {page_num}/{total_pages}: Using pymupdf4llm MD ({len(cleaned_md.strip())} chars)."
                    )
                # Only save to cache if valid
                if not cleaned_md.strip().startswith(("Warning:", "Error:")):
                    os.makedirs(scratch_dir, exist_ok=True)
                    with open(page_md_path, "w", encoding="utf-8") as f:
                        f.write(cleaned_md)

            extracted_mds[page_num] = cleaned_md
            markdown_parts.append(f"# PAGE {page_num}\n---\n{cleaned_md}")

        # Combine markdown from all pages
        final_content = "\n\n".join(markdown_parts)

        # Save Raw Markdown output (temporarily, will be overwritten with enriched)
        os.makedirs(os.path.dirname(output_md_path), exist_ok=True)
        with open(output_md_path, "w", encoding="utf-8") as f:
            f.write(final_content)

        # Stage 3: Pass combined Markdown to OpenRouter for document-level metadata & measurements extraction
        cache_json_path = os.path.join(scratch_dir, "metadata_extraction.json")
        json_data = None
        if os.path.exists(cache_json_path):
            if status_callback:
                status_callback("Loading cached document-level metadata extraction...")
            try:
                with open(cache_json_path, "r", encoding="utf-8") as f:
                    temp_data = json.load(f)
                if temp_data and temp_data.get("pages"):
                    json_data = temp_data
            except Exception as e:
                print(f"Error loading cached metadata: {e}. Will reprocess.")

        if not json_data:
            if status_callback:
                status_callback("Stage 3: Passing combined Markdown to OpenRouter to extract JSON...")
            doc_name = os.path.basename(pdf_path)
            try:
                json_data = engineering_extractor.extract_metadata_from_markdown(
                    api_key=self.api_key,
                    model=self.models[0],
                    doc_name=doc_name,
                    markdown_content=final_content
                )
                # Save to cache
                with open(cache_json_path, "w", encoding="utf-8") as f:
                    json.dump(json_data, f, indent=2, ensure_ascii=False)
            except Exception as e:
                print(f"[MetadataExtractor] LLM extraction failed: {e}")
                json_data = {"document_name": doc_name, "model_used": self.models[0], "pages": []}

        # Enrich JSON metadata with deterministic text parsing
        json_data = self._enrich_json_metadata_from_page_texts(json_data, extracted_mds)

        # Save JSON output
        output_json_path = os.path.splitext(output_md_path)[0] + ".json"
        os.makedirs(os.path.dirname(output_json_path), exist_ok=True)
        with open(output_json_path, "w", encoding="utf-8") as f:
            json.dump(json_data, f, indent=2, ensure_ascii=False)

        # Stage 4: Enrich markdown with structured engineering details
        if status_callback:
            status_callback("Stage 4: Enriching markdown with structured engineering details...")

        # Calculate buildingPerimeter and foundationWallDepth dynamically without saving to JSON
        from services.takeoff_service import TakeoffService
        takeoff_service = TakeoffService()
        cache_json_path = os.path.join(scratch_dir, "metadata_extraction.json")
        perimeter_data, depth_data = takeoff_service._resolve_building_dimensions(
            api_key=self.api_key,
            pdf_path=pdf_path,
            scratch_dir=scratch_dir,
            full_json_data=json_data,
            metadata_json_path=cache_json_path,
            requires_perimeter=True,
            requires_depth=True
        )

        enriched_markdown_parts = []
        pages_by_number = {p["page_number"]: p for p in json_data.get("pages", [])}

        for p_num in sorted(extracted_mds.keys()):
            original_page_md = extracted_mds[p_num]
            page_entry = pages_by_number.get(p_num)
            if page_entry:
                enriched_page_md = self._generate_enriched_markdown(
                    page_entry, original_page_md,
                    perimeter_data=perimeter_data,
                    depth_data=depth_data
                )
                extracted_mds[p_num] = enriched_page_md
                enriched_markdown_parts.append(f"# PAGE {p_num}\n---\n{enriched_page_md}")
            else:
                enriched_markdown_parts.append(f"# PAGE {p_num}\n---\n{original_page_md}")

        final_content = "\n\n".join(enriched_markdown_parts)

        # Overwrite output Markdown with enriched markdown
        with open(output_md_path, "w", encoding="utf-8") as f:
            f.write(final_content)

        # Generate and save combined HTML output
        combined_html = []
        combined_html.append("""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Converted PDF Document - Markdown Text Layout</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body {
            font-family: 'Inter', sans-serif;
            background-color: #0f172a;
            color: #e2e8f0;
            margin: 0;
            padding: 40px 20px;
            display: flex;
            flex-direction: column;
            align-items: center;
        }
        .container {
            max-width: 900px;
            width: 100%;
        }
        h1 {
            color: #38bdf8;
            border-bottom: 1px solid #334155;
            padding-bottom: 10px;
            font-size: 28px;
            margin-bottom: 30px;
            text-align: center;
        }
        .page-card {
            background-color: #1e293b;
            border: 1px solid #334155;
            border-radius: 12px;
            padding: 30px;
            margin-bottom: 40px;
            box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.3);
        }
        .page-header {
            font-size: 20px;
            color: #38bdf8;
            margin-top: 0;
            margin-bottom: 20px;
            border-bottom: 1px solid #475569;
            padding-bottom: 8px;
        }
        .page-content {
            background-color: #0f172a;
            border-radius: 8px;
            padding: 20px;
            overflow-x: auto;
            color: #cbd5e1;
            font-size: 14px;
            line-height: 1.6;
            border: 1px solid #1e293b;
            white-space: pre-wrap;
            font-family: monospace;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Converted Document Markdown Layout Structure</h1>
""")

        for p_num in sorted(extracted_mds.keys()):
            combined_html.append(f"""
        <div class="page-card" id="page-{p_num}">
            <div class="page-header">Page {p_num} Layout Structure</div>
            <div class="page-content">
{extracted_mds[p_num]}
            </div>
        </div>
""")

        combined_html.append("""
    </div>
</body>
</html>
""")

        output_html_path = os.path.splitext(output_md_path)[0] + ".html"
        with open(output_html_path, "w", encoding="utf-8") as f:
            f.write("".join(combined_html))

        print(f"Multi-stage pipeline completed in {time.time() - total_start_time:.2f} seconds")
        return final_content

        return final_content

