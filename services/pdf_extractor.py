import os
import fitz  # PyMuPDF
from PIL import Image
from io import BytesIO

# Attempt to import pymupdf4llm for layout-aware Markdown extraction
# Forced to False due to hangs on complex scanned blueprint PDFs
PYMUPDF4LLM_AVAILABLE = False

class PDFExtractor:
    def __init__(self, dpi: int = 200):
        self.dpi = dpi

    def extract_pdf_data(self, pdf_path: str, output_dir: str, render_images: bool = False) -> list:
        """
        Processes a PDF file, rendering pages to high-res PNG (optional) and extracting
        all layout metadata (dimensions, text blocks with coordinates, tables, embedded images).

        Uses pymupdf4llm.to_markdown() when available for superior layout-aware Markdown.
        Falls back to basic get_text("blocks") extraction otherwise.

        Returns a list of dictionaries, one per page.
        """
        os.makedirs(output_dir, exist_ok=True)
        doc = fitz.open(pdf_path)
        pages_data = []

        # 1. First, extract all embedded images to map them page by page
        embedded_images = self.extract_embedded_images(doc, output_dir)

        # 2. If pymupdf4llm is available, extract full-document layout-aware MD once
        #    (it's more efficient to run on the full doc than per-page)
        llm_md_per_page = {}
        if PYMUPDF4LLM_AVAILABLE:
            try:
                print("[PDFExtractor] Running pymupdf4llm.to_markdown() on full document...")
                llm_md_pages = pymupdf4llm.to_markdown(
                    doc,
                    page_chunks=True,         # Returns list of dicts, one per page
                    write_images=False,        # Don't embed images in MD
                    show_progress=False,
                )
                for chunk in llm_md_pages:
                    # chunk is a dict with keys: 'metadata', 'text', 'images', etc.
                    metadata = chunk.get("metadata", {})
                    page_idx = metadata.get("page_number")
                    if page_idx is None:
                        page_idx = metadata.get("page", 0) + 1
                    llm_md_per_page[page_idx] = chunk.get("text", "")
                print(f"[PDFExtractor] pymupdf4llm extracted MD for {len(llm_md_per_page)} pages.")
            except Exception as e:
                print(f"[PDFExtractor] pymupdf4llm extraction failed: {e}. Will fall back to basic extraction.")
                llm_md_per_page = {}

        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            page_idx = page_num + 1

            # --- Render page at high-res PNG (200-300 DPI) if requested ---
            img_path = ""
            if render_images:
                img_filename = f"page_{page_idx}.png"
                img_path = os.path.join(output_dir, img_filename)
                if not os.path.exists(img_path):
                    pix = page.get_pixmap(dpi=self.dpi)
                    pix.save(img_path)

            # Page dimensions in points
            rect = page.rect
            width = rect.width
            height = rect.height

            # Filter embedded images for this page
            page_embedded_images = [img for img in embedded_images if img["page_number"] == page_idx]

            # --- Extract tables (always, needed for cleaner + fallback) ---
            tables = page.find_tables()
            table_list = tables.tables
            table_bboxes = []
            table_markdowns = []

            for tbl in table_list:
                table_bboxes.append(tbl.bbox)
                table_data = tbl.extract()
                if not table_data:
                    continue

                headers = table_data[0]
                rows = table_data[1:]

                # Clean headers and cells
                headers = [str(h or "").strip().replace("\n", " ") for h in headers]
                # Skip tables that are entirely empty (all headers and rows are empty)
                has_content = any(h for h in headers)
                if not has_content:
                    for row in rows:
                        if any(str(c or "").strip() for c in row):
                            has_content = True
                            break
                if not has_content:
                    continue

                md_table = []
                md_table.append("| " + " | ".join(headers) + " |")
                md_table.append("| " + " | ".join(["---"] * len(headers)) + " |")

                for row in rows:
                    row_cells = [str(c or "").strip().replace("\n", " ") for c in row]
                    if len(row_cells) < len(headers):
                        row_cells += [""] * (len(headers) - len(row_cells))
                    elif len(row_cells) > len(headers):
                        row_cells = row_cells[:len(headers)]
                    md_table.append("| " + " | ".join(row_cells) + " |")

                table_markdowns.append({
                    "bbox": tbl.bbox,
                    "markdown": "\n" + "\n".join(md_table) + "\n"
                })

            # --- Extract text blocks with coordinates (needed by MarkdownCleaner) ---
            blocks = page.get_text("blocks")
            text_blocks_data = []
            raw_elements = []

            for b in blocks:
                xmin, ymin, xmax, ymax, text, block_no, block_type = b
                if block_type != 0:  # Skip non-text blocks
                    continue

                text = text.strip()
                if not text:
                    continue

                # Coordinate details (points) — [ymin, xmin, ymax, xmax]
                bbox = [ymin, xmin, ymax, xmax]

                # Check if block center is inside any table bounding box
                block_center_x = (xmin + xmax) / 2
                block_center_y = (ymin + ymax) / 2
                is_inside_table = False
                for t_bbox in table_bboxes:
                    if (t_bbox[0] <= block_center_x <= t_bbox[2] and
                            t_bbox[1] <= block_center_y <= t_bbox[3]):
                        is_inside_table = True
                        break

                text_blocks_data.append({
                    "bbox": bbox,
                    "text": text,
                    "is_inside_table": is_inside_table
                })

                if not is_inside_table:
                    raw_elements.append((ymin, "text", text))

            # Add tables to elements
            for i, tbl in enumerate(table_markdowns):
                raw_elements.append((tbl["bbox"][1], "table", tbl["markdown"]))

            # Sort elements by y-coordinate (ymin) for correct reading order
            raw_elements = sorted(raw_elements, key=lambda e: e[0])

            # --- Choose raw_markdown source ---
            if page_idx in llm_md_per_page and llm_md_per_page[page_idx].strip():
                # Use pymupdf4llm layout-aware MD — superior quality
                raw_markdown = llm_md_per_page[page_idx]
                print(f"[PDFExtractor] Page {page_idx}: Using pymupdf4llm MD ({len(raw_markdown)} chars)")
            else:
                # Fallback: build basic MD from sorted text + table elements
                page_md = []
                for ymin, el_type, content in raw_elements:
                    if el_type == "text":
                        lines = content.split("\n")
                        # Preserve original line structure — don't collapse into one paragraph
                        cleaned_lines = [line.strip() for line in lines if line.strip()]
                        if cleaned_lines:
                            if len(cleaned_lines) == 1 and cleaned_lines[0].isupper() and len(cleaned_lines[0]) < 60:
                                page_md.append(f"\n### {cleaned_lines[0]}\n")
                            else:
                                page_md.append("\n" + "\n".join(cleaned_lines) + "\n")
                    elif el_type == "table":
                        page_md.append(content)
                raw_markdown = "".join(page_md)
                print(f"[PDFExtractor] Page {page_idx}: Using fallback basic MD ({len(raw_markdown)} chars)")

            pages_data.append({
                "page_number": page_idx,
                "image_path": img_path,
                "width": width,
                "height": height,
                "raw_markdown": raw_markdown,
                "text_blocks": text_blocks_data,
                "tables": [{"bbox": t.bbox, "cols": t.col_count, "rows": t.row_count} for t in table_list],
                "embedded_images": page_embedded_images
            })

        doc.close()
        return pages_data

    def extract_embedded_images(self, doc: fitz.Document, output_dir: str) -> list:
        """
        Extracts embedded raster images from all pages in the PDF.
        """
        extracted = []
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            image_list = page.get_images(full=True)

            for img_idx, img_info in enumerate(image_list):
                xref = img_info[0]
                try:
                    base_image = doc.extract_image(xref)
                    image_bytes = base_image["image"]
                    image_ext = base_image["ext"]

                    try:
                        img = Image.open(BytesIO(image_bytes))
                        width, height = img.size
                    except Exception:
                        width, height = 0, 0

                    filename = f"page_{page_num + 1}_embedded_{img_idx + 1}.{image_ext}"
                    filepath = os.path.join(output_dir, filename)

                    with open(filepath, "wb") as f:
                        f.write(image_bytes)

                    extracted.append({
                        "page_number": page_num + 1,
                        "image_index": img_idx + 1,
                        "filename": filename,
                        "width": width,
                        "height": height,
                        "format": image_ext,
                        "path": filepath
                    })
                except Exception as e:
                    print(f"Error extracting image index {img_idx} on page {page_num + 1}: {e}")

        return extracted
