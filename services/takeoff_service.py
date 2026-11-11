import os
import json
import base64
import math
import re
import urllib.request
import urllib.error
import fitz  # PyMuPDF
from PIL import Image
from io import BytesIO
from dotenv import load_dotenv

load_dotenv(override=True)

class TakeoffService:

    """
    Dynamic Material Takeoff Service.
    Finds relevant blueprint sheets based on a query and runs visual
    dimensions takeoff via direct Google Gemini API.
    """
    def __init__(self):
        # Load local Material Knowledge Base / Dictionary
        self.kb = {}
        kb_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "material_kb.json")
        if os.path.exists(kb_path):
            try:
                with open(kb_path, "r", encoding="utf-8") as f:
                    self.kb = json.load(f)
            except Exception as e:
                print(f"[TakeoffService] Error loading material_kb.json: {e}")

    def _robust_json_loads(self, text: str) -> dict:
        """
        Robustly parses JSON strings returned by LLM, handling markdown backticks,
        partial wrappings, and trailing noise characters.
        """
        text_strip = text.strip()
        try:
            return json.loads(text_strip)
        except Exception:
            pass

        if "```json" in text_strip:
            try:
                parts = text_strip.split("```json")
                inner = parts[1].split("```")[0].strip()
                return json.loads(inner)
            except Exception:
                pass

        if "```" in text_strip:
            try:
                parts = text_strip.split("```")
                for p in parts:
                    p_strip = p.strip()
                    if p_strip.startswith("{") and p_strip.endswith("}"):
                        return json.loads(p_strip)
            except Exception:
                pass

        start = text_strip.find("{")
        end = text_strip.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = text_strip[start:end+1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError as jde:
                err_msg = str(jde)
                if "Extra data" in err_msg:
                    match = re.search(r"char\s+(\d+)", err_msg)
                    if match:
                        char_idx = int(match.group(1))
                        try:
                            return json.loads(candidate[:char_idx].strip())
                        except Exception:
                            pass

        return json.loads(text_strip)

    def identify_material_from_description(self, description: str, job_id: str = None) -> dict:
        """
        Identifies the standard material to be measured based on a project description or freeform scope of work.
        Uses fast KB matching first, falling back to Google Gemini LLM analysis.
        """
        if not description or not description.strip():
            if self.kb:
                first_key = list(self.kb.keys())[0]
                return {
                    "matched": True,
                    "material": self.kb[first_key].get("standard_name", first_key),
                    "confidence": 0.5,
                    "reasoning": "First standard material selected from knowledge base (empty description provided).",
                    "source": "kb_default"
                }
            return {
                "matched": False,
                "material": None,
                "reasoning": "No material description provided and Knowledge Base is empty.",
                "source": "no_description"
            }

        desc_lower = description.lower().strip()

        # 1. Fast KB matching (check standard name, synonyms, key, and context keywords)
        best_match = None
        highest_score = 0

        desc_words = set(re.findall(r'\b\w+\b', desc_lower))

        for key, entry in self.kb.items():
            std_name = entry.get("standard_name", key)
            synonyms = [s.lower() for s in entry.get("synonyms", [])]
            contexts = [c.lower() for c in entry.get("common_contexts", [])]
            
            score = 0
            # Direct match on key or standard name
            if key.replace("_", " ") in desc_lower or std_name.lower() in desc_lower:
                score += 50

            # Match against synonyms
            for syn in synonyms:
                if len(syn) <= 3:
                    if syn in desc_words:
                        score += 35
                else:
                    if syn in desc_lower:
                        score += 35
                    elif any(word in desc_words for word in syn.split() if len(word) > 3):
                        score += 15

            # Match against contexts
            for ctx in contexts:
                if ctx in desc_lower:
                    score += 20

            if score > highest_score:
                highest_score = score
                best_match = entry

        if best_match and highest_score >= 35:
            return {
                "matched": True,
                "material": best_match.get("standard_name", ""),
                "confidence": min(0.95, round(0.6 + (highest_score / 100), 2)),
                "reasoning": "Matched material in Knowledge Base based on description keywords.",
                "source": "kb_match"
            }

        # 2. LLM Fallback (Gemini API) if KB fast match is low confidence
        api_key = os.getenv("GEMINI_API_KEY")
        if api_key:
            try:
                kb_options = []
                for k, v in self.kb.items():
                    std = v.get("standard_name", k)
                    syns = ", ".join(v.get("synonyms", []))
                    kb_options.append(f"- {std} (Synonyms: {syns})")
                kb_summary = "\n".join(kb_options) if kb_options else "No standard materials pre-configured."

                prompt = f"""You are an expert construction quantity takeoff estimator.
The user has provided a project description / scope of work and wants to identify the exact material to be measured for quantity takeoff.

Project Description: "{description}"

Available Standard Materials in Knowledge Base:
{kb_summary}

Tasks:
1. Identify the single best matching standard material name from the list above.
2. If none match from the list but it is a valid construction item, specify the exact material string required for takeoff.
3. If the input description is completely unrelated to construction or no construction material can be identified, set material to null and matched to false.

RESPOND WITH VALID JSON ONLY matching this format:
{{
  "matched": true,
  "material": "Exact standard material name or material string (or null if no match)",
  "confidence": 0.95,
  "reasoning": "Explanation of why this material was identified, or why no material could be matched"
}}"""

                model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
                payload = {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"responseMimeType": "application/json"}
                }
                req = urllib.request.Request(
                    url, data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST"
                )
                with urllib.request.urlopen(req, timeout=15) as response:
                    res_body = response.read().decode("utf-8")
                    res_json = json.loads(res_body)
                    content = res_json["candidates"][0]["content"]["parts"][0]["text"]
                    parsed = json.loads(content.strip())
                    parsed["source"] = "gemini_llm"
                    mat = parsed.get("material")
                    mat_lower = str(mat).lower() if mat else ""
                    if not mat or mat_lower in ["null", "none", "n/a", "undefined"] or any(k in mat_lower for k in ["unidentifiable", "unknown", "no material", "not applicable"]):
                        return {
                            "matched": False,
                            "material": None,
                            "reasoning": parsed.get("reasoning", "No relevant construction material identified."),
                            "source": "gemini_llm"
                        }
                    parsed["matched"] = True
                    return parsed
            except Exception as e:
                print(f"[TakeoffService] LLM material identification failed: {e}")

        # Fallback if LLM fails or is unavailable
        if best_match:
            return {
                "matched": True,
                "material": best_match.get("standard_name"),
                "confidence": 0.5,
                "reasoning": "Identified material using basic heuristic match.",
                "source": "fallback"
            }

        return {
            "matched": False,
            "material": None,
            "reasoning": "No matching construction material could be found for the provided description.",
            "source": "no_match"
        }


    def _get_search_terms(self, material_query: str, project_id: str = None) -> tuple:
        """
        Expands material_query into standard name, description, list of synonyms,
        takeoff unit, common contexts, specific properties, estimation guidelines, and commercial estimation parameters using the KB.
        Returns: (standard_name, description, synonyms_list, takeoff_unit, common_contexts,
                  properties, guidelines, requires_perimeter, requires_depth, kb_params)
        """
        query_lower = material_query.lower().strip()
        normalized_query = query_lower.replace("-", "_").replace(" ", "_").replace(".", "_")
        target_entry = None
        # 1. Direct match on key or normalized key
        if query_lower in self.kb:
            target_entry = self.kb[query_lower]
        elif normalized_query in self.kb:
            target_entry = self.kb[normalized_query]

        # 2. Check if key is a sub-phrase or contains query
        if not target_entry:
            for key, entry in self.kb.items():
                clean_k = key.replace("_", " ").lower()
                clean_k_compact = key.replace("_", "").lower()
                clean_q_compact = query_lower.replace(" ", "").replace("-", "").replace("_", "")
                if clean_k in query_lower or query_lower in clean_k or clean_k_compact in clean_q_compact:
                    target_entry = entry
                    break

        # 3. Robust bidirectional substring & token matching against synonyms & standard names
        if not target_entry:
            best_score = 0
            best_entry = None
            query_words = set(re.findall(r'\b\w+\b', query_lower))

            for key, entry in self.kb.items():
                std_lower = entry.get("standard_name", "").lower()
                syns = [s.lower() for s in entry.get("synonyms", [])]
                contexts = [c.lower() for c in entry.get("common_contexts", [])]

                score = 0
                if std_lower in query_lower or query_lower in std_lower:
                    score += 50

                for syn in syns:
                    syn_compact = syn.replace(" ", "").replace("-", "")
                    query_compact = query_lower.replace(" ", "").replace("-", "")
                    if syn in query_lower or query_lower in syn:
                        score += 45
                    elif syn_compact in query_compact or query_compact in syn_compact:
                        score += 40
                    elif any(w in query_words for w in syn.split() if len(w) > 3):
                        score += 15

                for ctx in contexts:
                    if ctx in query_lower:
                        score += 10

                if score > best_score:
                    best_score = score
                    best_entry = entry

            if best_entry and best_score >= 30:
                target_entry = best_entry

        if target_entry:
            guidelines = target_entry.get("takeoff_guidelines", [])
            kb_params = {
                "category": target_entry.get("category", "Général"),
                "default_waste_percent": target_entry.get("default_waste_percent", 10),
                "vertical_dimension_target": target_entry.get("vertical_dimension_target", "below_grade_depth" if target_entry.get("requires_depth") else "none"),
            }
            return (
                target_entry.get("standard_name", material_query),
                target_entry.get("description", ""),
                [s.lower() for s in target_entry.get("synonyms", [])],
                target_entry.get("takeoff_unit", "Pi Carrée (sq.ft) / m²"),
                target_entry.get("common_contexts", []),
                target_entry.get("properties", {}),
                guidelines,
                target_entry.get("requires_perimeter", False),
                target_entry.get("requires_depth", False),
                kb_params,
            )

        # 4. Semantic construction intent detection for novel queries not in KB
        auto_requires_perim = False
        auto_requires_depth = False
        auto_unit = "Pi Carrée (sq.ft) / m²"
        auto_target_dim = "below_grade_depth"
        auto_waste = 10

        linear_keywords = ["delta-flash", "delta flash", "deltaflash", "solin", "finition", "termination", "capping", "pi lin", "lin.ft", "linear feet", "starter strip", "flashing"]
        drainage_keywords = ["delta-ms", "delta ms", "drainage membrane", "membrane drainante", "fondation", "foundation", "waterproofing", "hydrofuge", "damp-proofing", "enduit"]
        bituminous_keywords = ["rc.30", "rc30", "bitumineux", "bituminous"]
        geotextile_keywords = ["geotextile", "géotextile", "drain francais", "french drain", "filter fabric"]
        slab_keywords = ["slab", "dalle", "floor slab", "dalle sur sol", "dalle sous-sol", "polyethylene", "polyéthylène", "sous la dalle"]

        if any(k in query_lower for k in linear_keywords):
            auto_requires_perim = True
            auto_requires_depth = False
            auto_unit = "Pi Lin (lin.ft) / m"
            auto_target_dim = "none"
            auto_waste = 5
        elif any(k in query_lower for k in drainage_keywords):
            auto_requires_perim = True
            auto_requires_depth = True
            auto_unit = "Pi Carrée (sq.ft) / m²"
            auto_target_dim = "below_grade_depth"
            auto_waste = 10
        elif any(k in query_lower for k in bituminous_keywords):
            auto_requires_perim = True
            auto_requires_depth = True
            auto_unit = "Pi Carrée (sq.ft) / m²"
            auto_target_dim = "below_grade_depth"
            auto_waste = 10
        elif any(k in query_lower for k in geotextile_keywords):
            auto_requires_perim = True
            auto_requires_depth = True
            auto_unit = "Pi Carrée (sq.ft) / m²"
            auto_target_dim = "geotextile_drain_girth"
            auto_waste = 10
        elif any(k in query_lower for k in slab_keywords):
            auto_requires_perim = False
            auto_requires_depth = False
            auto_unit = "Pi Carrée (sq.ft) / m²"

        fallback_params = {
            "category": "Général",
            "default_waste_percent": auto_waste,
            "vertical_dimension_target": auto_target_dim,
        }
        return (
            material_query,
            "User direct query search",
            [query_lower],
            auto_unit,
            [],
            {},
            [],
            auto_requires_perim,
            auto_requires_depth,
            fallback_params,
        )
    def find_pages_for_material(
        self, json_data: dict, material_query: str, scratch_dir: str = None, current_page: int = None
    ) -> list:
        """
        Scans document pages and ranks them generically by relevance to pick the primary page(s)
        from which the material details and measurements are extracted.
        """
        if not json_data or not json_data.get("pages"):
            return []
        std_name, kb_desc, synonyms, takeoff_unit, common_contexts, properties, guidelines, requires_perim, requires_dpth, kb_params = self._get_search_terms(material_query)
        page_scores = []
        for p in json_data["pages"]:
            page_num = p.get("page_number")
            if not page_num:
                continue
            sheet_str = str(p.get("sheetNumber", "")).upper()
            title_str = str(p.get("title", "")).upper()
            dtype_str = str(p.get("drawingType", "")).upper()
            # Generic Cover / Title / Index sheet identification (semantic only, no sheet-number assumptions)
            is_cover_sheet = (
                dtype_str in ("COVER", "LEGEND", "SCHEDULE")
                or any(
                    k in title_str or k in dtype_str
                    for k in ["COVER", "INDEX", "FRONTISPICE", "LISTE DES PLANS", "TITLE SHEET", "TITLE PAGE"]
                )
            )
            # Heavy penalty for cover sheets so they are not picked for measurements
            score = -100 if is_cover_sheet else 0
            term_matched = False
            # 1. Search in elements list
            for el in p.get("elements", []):
                name = str(el.get("name", "")).lower()
                desc = str(el.get("description", "")).lower()
                el_type = str(el.get("type", "")).lower()
                for term in synonyms:
                    if term in name or term in desc or term in el_type:
                        score += 15
                        term_matched = True
                        break
            # 2. Search in instructions
            for inst in p.get("instructions", []):
                elem = str(inst.get("element", "")).lower()
                wording = str(inst.get("originalWording", "")).lower()
                for term in synonyms:
                    if term in elem or term in wording:
                        score += 10
                        term_matched = True
                        break
            # 3. Search in measurements
            for m in p.get("measurements", []):
                val = str(m.get("value", "")).lower()
                assoc = str(m.get("associatedElement", "")).lower()
                for term in synonyms:
                    if term in assoc or term in val:
                        score += 8
                        term_matched = True
                        break
            # 4. Search in page general notes or title (unless it's a cover sheet index list)
            notes_str = " ".join(str(note) for note in p.get("generalNotes", [])).lower()
            if not is_cover_sheet:
                for term in synonyms:
                    if term in title_str.lower() or term in sheet_str.lower() or term in notes_str:
                        score += 5
                        term_matched = True
                        break
            # 5. Search in raw page markdown text (unless it's a cover sheet)
            if scratch_dir and not is_cover_sheet:
                page_md_path = os.path.join(scratch_dir, f"page_{page_num}.md")
                if os.path.exists(page_md_path):
                    try:
                        with open(page_md_path, "r", encoding="utf-8") as f:
                            page_md_content = f.read().lower()
                        for term in synonyms:
                            if term in page_md_content:
                                score += 4
                                term_matched = True
                                break
                    except Exception:
                        pass
            # 6. Drawing Type suitability bonus ONLY IF material term matched or page is relevant detail/plan sheet
            if term_matched and (requires_perim or requires_dpth):
                if "SECTION" in dtype_str or "COUPE" in dtype_str or "DETAIL" in dtype_str or "DÉTAIL" in dtype_str:
                    score += 5
                elif "ELEVATION" in dtype_str or "ÉLÉVATION" in dtype_str:
                    score += 3
                elif "PLAN" in dtype_str or "IMPLANTATION" in dtype_str or "FOUNDATION" in dtype_str:
                    score += 3
            # Tie-breaker for current_page view in UI ONLY if score > 0 and not cover sheet
            if current_page and page_num == current_page and score > 0 and not is_cover_sheet:
                score += 1
            page_scores.append((score, page_num, is_cover_sheet))
        # Filter and pick best page
        valid_scores = [ps for ps in page_scores if ps[0] > 0]
        if valid_scores:
            valid_scores.sort(key=lambda x: x[0], reverse=True)
            return [valid_scores[0][1]]
        # Fallback: pick best non-cover section/detail or plan page
        non_cover_pages = [ps for ps in page_scores if not ps[2]]
        if non_cover_pages:
            for ps in non_cover_pages:
                p_obj = next((p for p in json_data["pages"] if p.get("page_number") == ps[1]), {})
                s_num = str(p_obj.get("sheetNumber", "")).upper()
                t_title = str(p_obj.get("title", "")).upper()
                if "COUPE" in t_title or "SECTION" in t_title or "DETAIL" in t_title \
                        or "SECTION" in str(p_obj.get("drawingType", "")).upper() \
                        or "DETAIL" in str(p_obj.get("drawingType", "")).upper():
                    return [ps[1]]
            return [non_cover_pages[0][1]]
        if current_page and current_page != 1:
            return [current_page]
        elif json_data.get("pages"):
            # Return first non-cover page if possible
            for p in json_data["pages"]:
                p_num = p.get("page_number")
                sheet = str(p.get("sheetNumber", "")).upper()
                p_title_up = str(p.get("title", "")).upper()
                p_dtype_up = str(p.get("drawingType", "")).upper()
                if not any(k in p_title_up or k in p_dtype_up for k in ["COVER", "INDEX", "FRONTISPICE", "LISTE DES PLANS"]):
                    return [p_num]
            return [json_data["pages"][0].get("page_number", 1)]
        return []
    def _call_gemini_vision(self, api_key: str, prompt: str, img_data: str, mime_type: str = "image/jpeg") -> str:
        model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        payload = {
            "contents": [{"parts": [
                {"text": prompt},
                {"inlineData": {"mimeType": mime_type, "data": img_data}}
            ]}],
            "generationConfig": {"responseMimeType": "application/json"}
        }
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=90) as response:
            res_json = json.loads(response.read().decode("utf-8"))
            return res_json["candidates"][0]["content"]["parts"][0]["text"].strip()
    def _render_page_image(self, pdf_path: str, scratch_dir: str, page_num: int, dpi: int = 150) -> tuple:
        page_image_path = os.path.join(scratch_dir, f"page_{page_num}.png")
        jpeg_path = os.path.join(scratch_dir, f"page_{page_num}_takeoff.jpg")
        if not os.path.exists(page_image_path):
            print(f"[TakeoffService] Page {page_num} PNG not found. Rendering at {dpi} DPI...")
            os.makedirs(scratch_dir, exist_ok=True)
            doc = fitz.open(pdf_path)
            page = doc.load_page(page_num - 1)
            pix = page.get_pixmap(dpi=dpi)
            pix.save(page_image_path)
            doc.close()
        if not os.path.exists(jpeg_path):
            img = Image.open(page_image_path).convert("RGB")
            img.save(jpeg_path, "JPEG", quality=85, optimize=True)
            size_kb = os.path.getsize(jpeg_path) // 1024
            print(f"[TakeoffService] Page {page_num} JPEG: {size_kb} KB")
        with open(jpeg_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8"), "image/jpeg"
    def _extract_perimeter_from_elevation_image(
        self, api_key: str, img_data: str, scale: str = "N/A", page_text: str = ""
    ) -> dict:
        ocr_block = f"\n\nPAGE OCR CONTEXT FOR REFERENCE:\n\"\"\"{page_text[:4000]}\"\"\"" if page_text else ""
        prompt = f"""You are a licensed professional construction quantity surveyor and estimator performing a quantity takeoff.
This blueprint sheet contains EXTERIOR ELEVATION VIEWS (Élévations Extérieures: North, South, East, West / Sud, Nord, Est, Ouest).
Drawing scale printed on the sheet: {scale}{ocr_block}

YOUR TASK — Extract building perimeter dimensions and any vertical foundation callouts directly from printed dimension callouts:

STEP 1 — HORIZONTAL FACADE WIDTHS:
  • Locate the overall horizontal building width for each exterior elevation face from the dimension chains.
  • Label each face with its orientation (e.g. North/South/East/West or Nord/Sud/Est/Ouest).
  • Record the exact printed text string (e.g. "15 000 mm", "50'-0\"", "13 513") and its numeric value in mm.
  • Sum the exterior facade widths to compute total building perimeter.

STEP 2 — VERTICAL FOUNDATION WALL CALLOUTS:
  • Inspect the bottom / ground interface of each elevation for explicit vertical foundation dimensions:
    - Exposed foundation wall height above finished grade (e.g. 200 mm / 8").
    - Below-grade foundation wall depth or finished grade to footing callout (e.g. "±1668 TYP.", "5'-6"").
    - Total foundation wall height callouts.
  • Record exact printed strings and numeric values in mm.

STRICT ANTI-HALLUCINATION RULES:
  • DO NOT GUESS, ESTIMATE, OR FABRICATE TYPICAL DIMENSIONS.
  • Every extracted number MUST correspond directly to a legible printed dimension callout on this drawing.
  • If horizontal facade widths or vertical foundation callouts are NOT visible on this sheet, set their values to null or 0.

RESPOND WITH VALID JSON ONLY matching this structure:
{{
  "faces": [
    {{"direction": "South Elevation", "overall_width_mm": 0, "raw_printed_string": "Exact printed text", "dimension_source": "Chain description"}}
  ],
  "perimeter_mm": 0,
  "perimeter_m": 0.0,
  "perimeter_ft": 0.0,
  "foundation_vertical_callouts": [
    {{"type": "below_grade_depth | full_wall_height | above_grade_exposure", "value_mm": 0, "raw_printed_string": "Exact printed text", "description": "Callout description"}}
  ],
  "calculation": "Sum of exterior facade widths",
  "confidence": 0.95
}}"""
        try:
            raw = self._call_gemini_vision(api_key, prompt, img_data)
            data = json.loads(raw)
            print(f"[TakeoffService] Perimeter extracted: {data.get('perimeter_m')} m  |  {data.get('perimeter_ft')} ft")
            return data
        except Exception as e:
            print(f"[TakeoffService] Perimeter vision extraction failed: {e}")
            return {}

    def _extract_perimeter_from_floorplan_image(
        self, api_key: str, img_data: str, scale: str = "N/A", page_text: str = ""
    ) -> dict:
        ocr_block = f"\n\nPAGE OCR CONTEXT FOR REFERENCE:\n\"\"\"{page_text[:4000]}\"\"\"" if page_text else ""
        prompt = f"""You are a licensed professional construction quantity surveyor performing a foundation takeoff.
This blueprint sheet contains a FLOOR PLAN, FOUNDATION PLAN, or SITE PLAN (Plan d'étage / Plan de fondation / Plan d'implantation).
Drawing scale printed on the sheet: {scale}{ocr_block}

YOUR TASK — Calculate the CONTINUOUS EXTERIOR FOUNDATION FOOTPRINT PERIMETER directly from printed dimension callouts.

═══════════════════════════════════════════════════════════════
UNIVERSAL TAKEOFF RULES — ADAPTABLE TO ALL ARCHITECTURAL BLUEPRINTS
═══════════════════════════════════════════════════════════════

1. ADAPT TO ANY BLUEPRINT GEOMETRY:
   - Rectangular Buildings: Extract overall exterior length (L) and width (W) along the main structural foundation wall boundary.
   - Stepped, Jogged, L-Shaped, or Multi-Segment Footprints: Extract all individual exterior perimeter wall segments and sum their lengths.
   - Attached Foundation Extensions: If the building has attached enclosed structures, porches, vestibules, or concrete slabs with reinforced perimeter foundation thickening ("renflement au périmètre"), extract both the core mass and the extension dimensions.

2. SEPARATE CORE ENVELOPE FROM EXCLUDED PROJECTIONS:
   - Identify the continuous poured concrete foundation wall line (thermal/moisture envelope).
   - EXCLUDE: open unheated porches on isolated post/pier supports (Poteaux / Pieux), exterior wood stairs, unbackfilled deck posts, and cantilevered roof overhangs / soffits (Débord de toit).
   - Record exact printed dimension strings for all included and excluded elements.

3. STRICT ANTI-HALLUCINATION GUARDRAILS:
   - DO NOT GUESS, ESTIMATE, OR FABRICATE DIMENSIONS.
   - Every dimension MUST be explicitly readable from the drawing image or verified in the OCR text.
   - Support both Metric (mm, m) and Imperial (ft, inches, fractions) callouts.
   - If dimension strings are not legible on this sheet, return perimeter_mm: 0 and explain in details.

═══════════════════════════════════════════════════════════════

RESPOND WITH VALID JSON ONLY matching this structure:
{{
  "building_type": "rectangular | stepped_or_jogged | complex_multi_segment",
  "is_stepped_or_jogged": false,
  "building_net_length_mm": 0,
  "building_net_width_mm": 0,
  "raw_length_string": "Exact printed overall exterior wall length callout text from PDF",
  "raw_width_string": "Exact printed overall exterior wall width callout text from PDF",
  "inner_line_callouts": [
    {{"face": "North/South/Length", "dimension_mm": 0, "raw_text": "Exact callout"}}
  ],
  "outer_line_callouts": [
    {{"face": "North/South/Length", "dimension_mm": 0, "raw_text": "Outer soffit/overhang callout if explicitly labeled"}}
  ],
  "attached_projections": [
    {{"name": "e.g. Attached slab with foundation thickening", "dimension_mm": 0, "raw_text": "Exact callout", "included_in_perimeter": true}}
  ],
  "excluded_projections": [
    {{"name": "e.g. Perron béton / Escalier / Poteaux", "reason": "Unheated / on posts / slab-on-grade", "dimension_mm": 0}}
  ],
  "perimeter_mm": 0,
  "perimeter_m": 0.0,
  "perimeter_ft": 0.0,
  "calculation": "Step-by-step arithmetic showing exact values summed",
  "details": "Professional explanation of building mass and foundation perimeter",
  "confidence": 0.95
}}"""

        try:
            raw = self._call_gemini_vision(api_key, prompt, img_data)
            data = json.loads(raw)
            print(f"[TakeoffService] Perimeter extracted: {data.get('perimeter_m')} m  |  {data.get('perimeter_ft')} ft")
            if data.get("excluded_projections"):
                print(f"[TakeoffService] Excluded projections: {data['excluded_projections']}")
            return data
        except Exception as e:
            print(f"[TakeoffService] Perimeter vision extraction failed: {e}")
            return {}


    def _extract_depth_from_section_image(self, api_key: str, img_data: str, page_md_text: str = "") -> dict:
        ocr_block = f"\n\nPAGE OCR CONTEXT FOR REFERENCE (cross-reference with drawing):\n\"\"\"{page_md_text[:4000]}\"\"\"" if page_md_text else ""
        prompt = f"""You are a licensed construction quantity takeoff estimator.
You are reading an architectural WALL SECTION, DETAIL, or FOUNDATION ELEVATION drawing (Coupe / Détail / Élévation de fondation) showing foundation wall vertical dimensions, footing details, and French drain specifications.{ocr_block}

STRICT ANTI-HALLUCINATION GUARDRAILS:
1. NEVER INVENT, GUESS, OR ESTIMATE TYPICAL DIMENSIONS (e.g. NEVER default to 2100 mm, 2400 mm, 8'-0", or 9'-0").
   - If a dimension is NOT explicitly printed or mathematically calculable from printed dimension chains on this sheet, YOU MUST SET ITS VALUE TO null AND ITS RAW TEXT TO null.
2. REJECT NON-FOUNDATION ELEMENTS:
   - Do NOT confuse glass guard-rail dimensions (1070 mm / 42"), post/column heights (1200 mm), parapet heights, window rough openings, or interior partitions with foundation depths.
   - Do NOT confuse material thickness callouts (e.g. 13mm crépi, 50mm insulation) with wall depth.
3. EXTRACT DISTINCT STRUCTURAL FOUNDATION PARAMETERS:
   - grade_to_footing_mm: Direct vertical height from Finished Grade line (Sol fini / Terrain fini / FG) down to Top of Footing (Dessus de semelle / TOF).
   - total_wall_height_mm: Total height of poured concrete foundation wall (Top of Wall to Top of Footing).
   - above_grade_exposure_mm: Vertical height of foundation wall exposed above finished exterior grade.
   - footing_thickness_mm: Vertical thickness of concrete footing / heel (Semelle).
   - full_foundation_height_mm: Total vertical height from top of wall down to footing base (wall height + footing thickness).
   - below_grade_depth_mm: Distance from finished grade to footing (grade_to_footing or total wall height minus above-grade exposure).
   - geotextile_drain_girth_mm: Total filter fabric wrap girth around French drain crushed stone envelope.
   - waterproofing_extends_to_footing: true IF drawing shows waterproofing/drainage membrane wrapping onto/over the footing face.

RESPOND WITH VALID JSON ONLY matching this structure:
{{
  "grade_to_footing_mm": null,
  "grade_to_footing_raw": null,
  "total_wall_height_mm": null,
  "total_wall_height_raw": null,
  "above_grade_exposure_mm": null,
  "above_grade_exposure_raw": null,
  "footing_thickness_mm": null,
  "footing_thickness_raw": null,
  "full_foundation_height_mm": null,
  "full_foundation_height_raw": null,
  "below_grade_depth_mm": null,
  "below_grade_depth_raw": null,
  "geotextile_drain_girth_mm": null,
  "geotextile_drain_girth_raw": null,
  "waterproofing_extends_to_footing": false,
  "calculated_depth_mm": null,
  "description": "Step-by-step arithmetic breakdown showing each callout value and how total depths and girth were calculated, or stating clearly that vertical depth is not dimensioned on this sheet",
  "confidence": 0.95
}}"""
        try:
            raw = self._call_gemini_vision(api_key, prompt, img_data)
            data = self._robust_json_loads(raw)
            print(f"[TakeoffService] Foundation depth elements extracted: Full Height = {data.get('full_foundation_height_mm')} mm, Below-Grade = {data.get('below_grade_depth_mm')} mm, Drain Girth = {data.get('geotextile_drain_girth_mm')} mm")
            return data
        except Exception as e:
            print(f"[TakeoffService] Depth vision extraction failed: {e}")
            return {}


    def _parse_dim_to_mm(self, val_str: str):
        """Robustly parses numeric metric/imperial dimension strings into millimetres."""
        if not val_str:
            return None
        clean_str = str(val_str).replace(" ", "").replace(",", ".").replace("±", "").replace("+", "").strip()
        try:
            val_num = float(clean_str)
            if "m" in str(val_str).lower() and "mm" not in str(val_str).lower() and val_num < 100:
                return round(val_num * 1000.0)
            return round(val_num)
        except ValueError:
            pass
        # Feet and inches (e.g. 99'-4", 5'-9 1/2", 6'-5")
        m_imp = re.search(r"(\d{1,3})\s*['’]\s*[-–\s]?\s*(\d{1,2})(?:\s*(\d+)/(\d+))?", str(val_str))
        if m_imp:
            try:
                ft = float(m_imp.group(1))
                inch = float(m_imp.group(2))
                frac = float(m_imp.group(3)) / float(m_imp.group(4)) if m_imp.group(3) else 0.0
                return round(((ft + (inch + frac) / 12.0) * 304.8))
            except ValueError:
                pass
        # Feet only (e.g. 50', 50 ft, 49.67')
        m_ft_only = re.search(r"^(\d{1,3}(?:\.\d+)?)\s*(?:'|’|ft|feet)$", str(val_str).strip(), re.IGNORECASE)
        if m_ft_only:
            try:
                return round(float(m_ft_only.group(1)) * 304.8)
            except ValueError:
                pass
        # Inches only (e.g. 28", 30", 8 po)
        m_in = re.search(r"(\d{1,3})(?:\s*(\d+)/(\d+))?\s*(?:po|\"|''|in|inches)", str(val_str), re.IGNORECASE)
        if m_in:
            try:
                inch = float(m_in.group(1))
                frac = float(m_in.group(2)) / float(m_in.group(3)) if m_in.group(2) else 0.0
                return round(((inch + frac) / 12.0) * 304.8)
            except ValueError:
                pass
        return None

    def _parse_elevation_datums(self, candidate_pages: list, scratch_dir: str = None) -> dict:
        """
        Dynamically extracts architectural elevation datum benchmarks (e.g. SOL / GRADE,
        SEMELLE / FOOTING, TOP OF WALL / TOW, BOTTOM OF FOOTING / BOF) from measurements
        and text OCR, and computes mathematical datum deltas.
        """
        datums = {}
        for p in candidate_pages:
            for m in p.get("measurements", []):
                mtype = str(m.get("measurementType", "")).upper()
                ctx = f"{m.get('associatedElement', '')} {m.get('context', '')}".upper()
                is_datum_type = (
                    mtype in ["ELEVATION", "ÉLÉVATION", "DATUM", "LEVEL", "NIVEAU"]
                    or any(k in ctx for k in ["EL.", "ÉLEV", "ELEV.", "DATUM", "BENCHMARK", "N.P.O.", "NIVEAU", "T.O.W", "TOW", "B.O.F", "BOF", "T.O.F", "TOF", "±", "+"])
                ) and not any(k in ctx for k in ["HAUTEUR", "ÉPAISSEUR", "EPAISSEUR", "THICKNESS", "DISTANCE", "LARGEUR"])
                if not is_datum_type:
                    continue
                val = str(m.get("value", "")).strip()
                val_mm = self._parse_dim_to_mm(val)
                if val_mm is not None:
                    if any(k in ctx for k in ["SOL", "GRADE", "FINISHED GRADE", "TERRAIN FINI", "CONTACT TERRE", "NIVEAU SOL"]):
                        if "grade_datum_mm" not in datums or val_mm > datums["grade_datum_mm"]:
                            datums["grade_datum_mm"] = val_mm
                            datums["grade_datum_raw"] = val
                            datums["grade_page"] = p
                    elif any(k in ctx for k in ["TOW", "TOP OF WALL", "DESSUS MUR", "HAUT MUR", "TOP FOUNDATION"]):
                        if "tow_datum_mm" not in datums or val_mm > datums["tow_datum_mm"]:
                            datums["tow_datum_mm"] = val_mm
                            datums["tow_datum_raw"] = val
                            datums["tow_page"] = p
                    elif any(k in ctx for k in ["BOF", "BOTTOM OF FOOTING", "DESSOUS SEMELLE", "BAS SEMELLE"]):
                        if "bof_datum_mm" not in datums or val_mm < datums["bof_datum_mm"]:
                            datums["bof_datum_mm"] = val_mm
                            datums["bof_datum_raw"] = val
                            datums["bof_page"] = p
                    elif any(k in ctx for k in ["SEMELLE", "FOOTING", "TOF", "TOP OF FOOTING", "DESSUS SEMELLE"]):
                        if "footing_datum_mm" not in datums or val_mm < datums["footing_datum_mm"]:
                            datums["footing_datum_mm"] = val_mm
                            datums["footing_datum_raw"] = val
                            datums["footing_page"] = p

        if scratch_dir:
            for p in candidate_pages:
                p_num = p.get("page_number")
                if not p_num:
                    continue
                md_path = os.path.join(scratch_dir, f"page_{p_num}.md")
                if not os.path.exists(md_path):
                    continue
                try:
                    with open(md_path, "r", encoding="utf-8") as f:
                        text = f.read()
                    for m in re.finditer(r'(?i)(?:EL\.?|ÉLEV\.?|SOL|GRADE|TERRAIN|T\.?O\.?W\.?|TOP\s+OF\s+WALL|SEMELLE|FOOTING|B\.?O\.?F\.?)[\s:=-]+(?:EL\.?|ÉLEV\.?)?\s*([±+]?\d{1,3}\s*\'\s*[-–\s]?\s*\d{1,2}(?:\s*[\d/]+)?\"?|\d{4,6}\s*(?:mm|m)?)', text):
                        label = text[max(0, m.start() - 20):m.start() + 20].upper()
                        raw_val = m.group(1).strip()
                        v_mm = self._parse_dim_to_mm(raw_val)
                        if v_mm is None:
                            continue
                        if any(k in label for k in ["SOL", "GRADE", "TERRAIN", "NIVEAU"]):
                            if "grade_datum_mm" not in datums:
                                datums["grade_datum_mm"] = v_mm
                                datums["grade_datum_raw"] = raw_val
                                datums["grade_page"] = p
                        elif any(k in label for k in ["TOW", "TOP OF WALL"]):
                            if "tow_datum_mm" not in datums:
                                datums["tow_datum_mm"] = v_mm
                                datums["tow_datum_raw"] = raw_val
                                datums["tow_page"] = p
                        elif any(k in label for k in ["BOF", "BOTTOM"]):
                            if "bof_datum_mm" not in datums:
                                datums["bof_datum_mm"] = v_mm
                                datums["bof_datum_raw"] = raw_val
                                datums["bof_page"] = p
                        elif any(k in label for k in ["SEMELLE", "FOOTING"]):
                            if "footing_datum_mm" not in datums:
                                datums["footing_datum_mm"] = v_mm
                                datums["footing_datum_raw"] = raw_val
                                datums["footing_page"] = p
                except Exception:
                    pass

        res = {}
        if "grade_datum_mm" in datums and "footing_datum_mm" in datums:
            delta = abs(datums["grade_datum_mm"] - datums["footing_datum_mm"])
            if delta > 0:
                res["below_grade_datum_delta_mm"] = delta
                res["below_grade_desc"] = (
                    f"Elevation datum difference: Finished Grade ({datums.get('grade_datum_raw', '')}) "
                    f"- Footing Top ({datums.get('footing_datum_raw', '')}) = {round(delta/304.8, 2)} ft ({round(delta/1000.0, 3)} m / {delta} mm)"
                )
                res["below_grade_page"] = datums.get("grade_page") or datums.get("footing_page")

        if "tow_datum_mm" in datums and "bof_datum_mm" in datums:
            delta = abs(datums["tow_datum_mm"] - datums["bof_datum_mm"])
            if delta > 0:
                res["full_height_datum_delta_mm"] = delta
                res["full_height_desc"] = (
                    f"Elevation datum difference: Top of Wall ({datums.get('tow_datum_raw', '')}) "
                    f"- Footing Base ({datums.get('bof_datum_raw', '')}) = {round(delta/304.8, 2)} ft ({round(delta/1000.0, 3)} m / {delta} mm)"
                )
                res["full_height_page"] = datums.get("tow_page") or datums.get("bof_page")

        return res

    def _parse_french_drain_girth(self, candidate_pages: list, scratch_dir: str = None) -> dict:
        """
        Dynamically extracts French drain wrap girth or crushed stone envelope dimensions
        from detail and section sheets (e.g. sheets A401, A501).
        """
        drain_girth_mm = None
        drain_desc = ""
        src_page = None

        for p in candidate_pages:
            for m in p.get("measurements", []):
                ctx = f"{m.get('associatedElement', '')} {m.get('context', '')}".upper()
                val = str(m.get("value", "")).strip()
                val_mm = self._parse_dim_to_mm(val)
                if val_mm is not None:
                    if any(k in ctx for k in ["GEOTEXTILE", "GÉOTEXTILE", "WRAP", "GIRTH", "ENVELOPPE GEOTEXTILE"]):
                        if 300 <= val_mm <= 4000:
                            drain_girth_mm = val_mm
                            drain_desc = f"Direct geotextile wrap girth callout: {val} ({round(val_mm/304.8, 2)} ft / {val_mm} mm)"
                            src_page = p
                            break
                    elif any(k in ctx for k in ["PIERRE CONCASSÉE", "PIERRE CONCASSEE", "CRUSHED STONE", "DRAIN FRANCAIS", "DRAIN FRANÇAIS", "FRENCH DRAIN"]):
                        if 300 <= val_mm <= 4000:
                            drain_girth_mm = val_mm
                            drain_desc = f"Crushed stone drain envelope dimension callout: {val} ({round(val_mm/304.8, 2)} ft / {val_mm} mm)"
                            src_page = p
                            break
            if drain_girth_mm:
                break

        if not drain_girth_mm and scratch_dir:
            for p in candidate_pages:
                p_num = p.get("page_number")
                if not p_num:
                    continue
                md_path = os.path.join(scratch_dir, f"page_{p_num}.md")
                if not os.path.exists(md_path):
                    continue
                try:
                    with open(md_path, "r", encoding="utf-8") as f:
                        text = f.read()
                    if any(k in text.upper() for k in ["DRAIN", "PIERRE CONCASS", "CRUSHED STONE", "GÉOTEXTILE", "GEOTEXTILE"]):
                        m_env = re.search(r'(?i)(?:DRAIN\s*(?:FRAN[ÇC]AIS)?|PIERRE\s*CONCASS[ÉE]E?|CRUSHED\s*STONE|G[ÉE]OTEXTILE)[\s\S]{0,80}?(?:ENVELOPPE|LARGEUR|WIDTH|DIM|WRAP|GAINE)?[\s:]*([±+]?\d{1,3}\s*[\'’]\s*[-–\s]?\s*\d{1,2}(?:\s*[\d/]+)?\"?|\d{2,4}\s*(?:mm|po|")|\d{1,2}\s*["\']\s*[-–]\s*\d{1,2}\s*["\'])', text)
                        if m_env:
                            raw_val = m_env.group(1).strip()
                            v_mm = self._parse_dim_to_mm(raw_val)
                            if v_mm and 300 <= v_mm <= 4000:
                                drain_girth_mm = v_mm
                                drain_desc = f"French drain stone envelope detail callout: {raw_val} ({round(v_mm/304.8, 2)} ft / {v_mm} mm)"
                                src_page = p
                                break
                except Exception:
                    pass

        if drain_girth_mm:
            d_m = round(drain_girth_mm / 1000.0, 3)
            d_ft = round(d_m * 3.28084, 2)
            src_num = src_page.get("page_number") if src_page else None
            src_sheet = src_page.get("sheetNumber", f"PAGE-{src_num}" if src_num else "N/A") if src_page else "N/A"
            src_title = src_page.get("title", "French Drain Detail") if src_page else "French Drain Detail"
            src_dtype = src_page.get("drawingType", "Detail Section") if src_page else "Detail Section"
            return {
                "value_mm": drain_girth_mm,
                "value_m": d_m,
                "value_ft": d_ft,
                "description": drain_desc or f"French drain wrap girth = {d_ft} ft ({d_m} m / {drain_girth_mm} mm)",
                "details": f"Total wrap girth of filter fabric around the perforated French drain pipe and crushed stone envelope ({drain_desc}).",
                "source_page": src_num,
                "source_sheet": src_sheet,
                "source_title": src_title,
                "source_page_type": src_dtype,
                "method": "French Drain Detail Analysis"
            }
        return None

    def _compute_doc_dimension_bounds(self, full_json_data: dict, scratch_dir: str = None) -> dict:
        """
        Computes adaptive min/max bounds for dimension scanning from the document's
        own measurement data. Env var overrides take highest priority; falls back
        to document-derived percentile bounds, then conservative defaults.
        """
        env_dim_min = int(os.getenv("DIMENSION_MIN_MM", 0) or 0)
        env_dim_max = int(os.getenv("DIMENSION_MAX_MM", 0) or 0)
        env_depth_min = int(os.getenv("DEPTH_MIN_MM", 0) or 0)
        env_depth_max = int(os.getenv("DEPTH_MAX_MM", 0) or 0)

        # Collect all numeric measurements from JSON pages and normalise to mm
        all_vals = []
        for p in full_json_data.get("pages", []):
            for m in p.get("measurements", []):
                val_str = str(m.get("value", "")).replace(" ", "").replace(",", ".")
                try:
                    v = float(val_str)
                    unit = str(m.get("unit", "")).lower()
                    if "mm" in unit:
                        pass  # already mm
                    elif unit in ["m", "meter", "metre"] or ("m" in unit and "mm" not in unit and "cm" not in unit):
                        v = v * 1000
                    elif unit in ["ft", "feet", "'"]:  # noqa
                        v = v * 304.8
                    elif "cm" in unit:
                        v = v * 10
                    if 100 <= v <= 100_000:
                        all_vals.append(v)
                except ValueError:
                    pass

        if len(all_vals) >= 6:
            sorted_vals = sorted(all_vals)
            p25 = sorted_vals[len(sorted_vals) // 4]
            p75 = sorted_vals[3 * len(sorted_vals) // 4]
            dim_min  = env_dim_min  or max(int(p25 * 0.3), 500)
            dim_max  = env_dim_max  or min(int(p75 * 4),   100_000)
            depth_min = env_depth_min or max(int(p25 * 0.1), 300)
            depth_max = env_depth_max or min(int(p75 * 2),   10_000)
            single_max = int(p75 * 2)
        else:
            dim_min   = env_dim_min   or 3_000
            dim_max   = env_dim_max   or 100_000
            depth_min = env_depth_min or 500
            depth_max = env_depth_max or 5_000
            single_max = 80_000

        return {
            "dim_min": dim_min,
            "dim_max": dim_max,
            "depth_min": depth_min,
            "depth_max": depth_max,
            "single_dim_max": single_max,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # EXCLUSION KEYWORDS: open concrete slabs, unheated porches, exterior stairs,
    # overhangs, deck posts, and cantilevered balconies are OUTSIDE the thermal
    # and moisture envelope.  Any dimension callout whose ±60-char OCR context
    # contains one of these terms is classified as "excluded" and will NOT
    # contribute to the inner structural-wall perimeter.
    # ─────────────────────────────────────────────────────────────────────────
    _EXCLUSION_TERMS = [
        "PERRON", "PORCH", "BALCON", "BALCONY", "BALCONIES",
        "ESCALIER", "STAIRS", "STAIR", "CANOPÉE", "CANOPY",
        "POTEAUX", "POTEAU", "DECK POST", "DECK", "DECKS",
        "SLAB ON GRADE", "DALLE SUR GRADE", "DALLE SUR SOL",
        "OPEN SLAB", "PERRON BÉTON", "BÉTON EXTÉRIEUR",
        "TERRASSE", "TERRACE", "PATIO", "VERANDA", "VÉRANDA",
        "CARPORT", "LOGGIA", "SOLARIUM UNHEATED",
        "HT.", "HT ", "HT\n", "HAUTEUR", "HEIGHT", "NIVEAU DE RÉFÉRENCE", "LEVEL BENCHMARK",
        "BENCHMARK ELEVATION", "PARAPET", "TOP OF WALL",
        "MARGE", "RECUL", "SETBACK", "LIMITE DE PROPRIÉTÉ", "PROPERTY LINE",
        "LOT ", "PI.CA", "SQ.FT", "SUPERFICIE", "BACS DE GESTION",
    ]
    # Terms that indicate the OUTER building envelope callout (roof / gallery soffit / balcony projection)
    _OUTER_TERMS = [
        "SOFFIT", "SOFFITE", "GALERIE", "GALLERY", "CORNICHE", "DÉBORD DE TOIT",
        "ROOF OVERHANG", "DÉBORD", "TOITURE", "OVERHANG", "OUTER LINE", "GALERIE E.E.",
    ]
    # Terms that indicate a callout IS part of the structural / inner foundation wall
    _INNER_STRUCTURAL_TERMS = [
        "FOND", "FONDATION", "MUR", "BÉTON COULÉ", "BÉTON",
        "ICF", "SEMELLE", "CONCRETE", "WALL", "STRUCTURAL",
        "BASEMENT", "SOUS-SOL", "THERMAL", "CHAUFFÉ",
    ]

    def _classify_dim_context(self, snippet: str) -> str:
        """
        Classify a ±60-char OCR snippet around a dimension callout as:
          'excluded'   → belongs to unheated porch, slab, stair, overhang, post, balcony
          'outer'      → belongs to outer roof/gallery soffit overhang chain
          'inner'      → explicitly belongs to structural/foundation wall chain
          'unknown'    → no recognisable keyword found
        """
        u = snippet.upper()
        if any(t in u for t in self._EXCLUSION_TERMS):
            return "excluded"
        if any(t in u for t in self._OUTER_TERMS):
            return "outer"
        if any(t in u for t in self._INNER_STRUCTURAL_TERMS):
            return "inner"
        return "unknown"

    def _select_inner_building_dimensions(
        self,
        page_dims: list,
    ) -> dict | None:
        """
        Generic, project-agnostic algorithm to pair inner structural wall dimensions
        (L_inner, W_inner) from candidate building callouts while explicitly excluding
        open slabs, unheated porches (Perron béton), exterior stairs, overhangs,
        deck posts, and cantilevered balconies.

        Each element of page_dims is either:
          (value_mm, raw_string)              — legacy 2-tuple (no context)
          (value_mm, raw_string, context_str) — 3-tuple with ±60-char OCR snippet
          (value_mm, raw_string, classification) — 3-tuple where classification is
              already 'excluded' | 'outer' | 'inner' | 'unknown'

        Returns a dict with keys:
          L_in, W_in, raw_L, raw_W            — inner structural wall dimensions
          L_out, W_out, raw_L_out, raw_W_out  — outer envelope dimensions (may == inner)
          excl                                 — list of excluded dimension values
          excl_labels                          — list of (value, label) for audit log
        Returns None if insufficient data.
        """
        if not page_dims:
            return None

        # ── 1. Unpack and classify each dimension entry ──
        classified = []   # list of (val_mm, raw_str, cls)
        for entry in page_dims:
            if len(entry) == 3:
                val, raw, ctx_or_cls = entry
                if ctx_or_cls in ("excluded", "outer", "inner", "unknown"):
                    cls = ctx_or_cls
                else:
                    cls = self._classify_dim_context(str(ctx_or_cls))
            else:
                val, raw = entry[0], entry[1]
                cls = "unknown"
            classified.append((val, raw, cls))

        # ── 2. Separate hard-excluded entries from candidate pool ──
        hard_excluded = [(v, r, c) for v, r, c in classified if c == "excluded"]
        candidates    = [(v, r, c) for v, r, c in classified if c != "excluded"]

        if not candidates:
            return None

        # ── 3. Group candidates into dimension tiers (5% tolerance) ──
        raw_vals = sorted(list(set(d[0] for d in candidates)), reverse=True)
        if len(raw_vals) < 1:
            return None

        tiers = []  # list of [val, val, …] within each tier
        for v in raw_vals:
            placed = False
            for t in tiers:
                if abs(t[0] - v) / t[0] <= 0.05:
                    t.append(v)
                    placed = True
                    break
            if not placed:
                tiers.append([v])

        # Representative value per tier = maximum in that tier
        tier_vals = [max(t) for t in tiers]

        # ── 4. Mark tiers that are explicitly 'outer' or 'inner' ──
        def _dominant_cls(tv):
            """Return majority classification of entries whose value is in this tier."""
            members = [c for v, r, c in candidates if abs(v - tv) / tv <= 0.05]
            if members.count("outer") > len(members) // 2:
                return "outer"
            if members.count("inner") > len(members) // 2:
                return "inner"
            return "unknown"

        tier_cls = [_dominant_cls(tv) for tv in tier_vals]

        # ── 5. Assign L_out, W_out, L_in, W_in ──
        excl_vals = [v for v, r, c in hard_excluded]
        excl_labels = [(v, c) for v, r, c in hard_excluded]

        # ── 4b. Strip isolated site setback / elevation benchmark height tiers (e.g. 100'-0" / 30,480 mm benchmark height > 28,000 mm) ──
        if len(tier_vals) >= 3 and tier_vals[0] > 28000 and tier_vals[0] > 1.5 * tier_vals[1]:
            # Top tier is a site benchmark height / lot boundary dimension > 90 ft
            excl_vals.append(tier_vals[0])
            tier_vals = tier_vals[1:]

        # Filter out interior room partition tiers (< 60% of secondary width tier)
        if len(tier_vals) >= 2:
            main_w_tier = tier_vals[1]
            tier_vals = [tv for tv in tier_vals if tv >= 0.60 * main_w_tier]

        if len(tier_vals) < 2:
            # Only one tier — can't determine L and W separately
            return None

        matched_dual_envelope = False
        if len(tier_vals) >= 4:
            V1, V2, V3, V4 = tier_vals[0], tier_vals[1], tier_vals[2], tier_vals[3]
            r_L = V3 / V1
            r_W = V4 / V2
            if 0.65 <= r_L <= 0.98 and 0.65 <= r_W <= 0.98 and abs(r_L - r_W) <= 0.15:
                L_out, W_out = V1, V2
                L_in,  W_in  = V3, V4
                excl_vals += [V1, V2]
                matched_dual_envelope = True
            elif V1 > 1.25 * V2 and 0.85 <= V4 / V3 <= 0.98 and 1.25 <= V2 / V3 <= 1.6:
                # V1 = Overall Length Callout (66'-5"), V2 = Enclosed Wall Length (49'-4"), V3 = Outer Soffit Width (37'-0"), V4 = Building Width (33'-0")
                L_out = V1
                W_out = V3
                L_in  = V1
                W_in  = V4
                excl_vals += [V3]
                matched_dual_envelope = True

        if not matched_dual_envelope and len(tier_vals) >= 3:
            V1, V2, V3 = tier_vals[0], tier_vals[1], tier_vals[2]
            r_L = V3 / V1
            r_W = V3 / V2
            if 0.85 <= r_W <= 0.98 and 1.25 <= V1 / V2 <= 1.6:
                # V1 = Length (~50'), V2 = Outer Balcony Width (~37'), V3 = Inner Width (~33')
                L_out = L_in = V1
                W_out = V2
                W_in = V3
                excl_vals += [V2]
                matched_dual_envelope = True
            elif not matched_dual_envelope and 0.75 <= r_L <= 0.98:
                expected_W_in = V2 * r_L
                all_raw_vals = [d[0] for d in candidates]
                matched_w = None
                for rv in sorted(all_raw_vals):
                    if abs(rv - expected_W_in) / expected_W_in <= 0.05:
                        matched_w = rv
                        break
                if matched_w:
                    L_out, W_out = V1, V2
                    L_in,  W_in  = V3, matched_w
                    excl_vals += [V1, V2]
                    matched_dual_envelope = True
                else:
                    derived_w = round(expected_W_in)
                    if 0.65 * V2 <= derived_w <= 0.95 * V2:
                        L_out, W_out = V1, V2
                        L_in,  W_in  = V3, derived_w
                        excl_vals += [V1, V2]
                        matched_dual_envelope = True
            elif V1 > 1.25 * V2 and 0.60 <= V3 / V2 <= 0.85 and (tier_cls[0] == "excluded" or V1 > 28000):
                # V1 = Site setback / lot boundary line (66'-5"), V2 = Building Length (49'-4"), V3 = Building Width (33'-0")
                L_out = L_in = V2
                W_out = W_in = V3
                excl_vals += [V1]
                matched_dual_envelope = True

        if not matched_dual_envelope:
            # Single Building Footprint Mass
            L_out = L_in = tier_vals[0]
            W_out = W_in = tier_vals[1]


        # ── 6. Sanity check: reject interior room/unit partitions (< 65% of overall mass) ──
        if W_in < 0.65 * W_out or L_in < 0.65 * L_out:
            return None




        # ── 7. Look up raw strings ──
        def _find_raw(target_val, source):
            for v, r, *_ in source:
                if abs(v - target_val) / max(target_val, 1) <= 0.05:
                    return r
            return str(int(target_val))

        raw_L     = _find_raw(L_in,  candidates)
        raw_W     = _find_raw(W_in,  candidates)
        raw_L_out = _find_raw(L_out, candidates)
        raw_W_out = _find_raw(W_out, candidates)

        return {
            "L_in": L_in, "W_in": W_in, "raw_L": raw_L, "raw_W": raw_W,
            "L_out": L_out, "W_out": W_out, "raw_L_out": raw_L_out, "raw_W_out": raw_W_out,
            "excl": excl_vals,
            "excl_labels": excl_labels,
        }

    def _resolve_building_dimensions(
        self,
        api_key: str,
        pdf_path: str,
        scratch_dir: str,
        full_json_data: dict,
        metadata_json_path: str,
        requires_perimeter: bool = False,
        requires_depth: bool = False,
        target_dim_key: str = "below_grade_depth",
    ) -> tuple:
        cache_file = os.path.join(scratch_dir, "takeoff_dimensions_cache.json") if scratch_dir else None
        if cache_file and os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                cached_p = cached.get("perimeter")
                cached_d = cached.get("depth")
                p_ok = (not requires_perimeter) or (cached_p and cached_p.get("value_mm"))
                d_ok = (not requires_depth) or (cached_d and cached_d.get("value_mm"))
                if p_ok and d_ok:
                    return cached_p or {}, cached_d or {}
            except Exception:
                pass

        perimeter_data = {}
        depth_data = {}
        updated = False
        bounds = self._compute_doc_dimension_bounds(full_json_data, scratch_dir)
        dim_min = bounds["dim_min"]
        dim_max = bounds["dim_max"]
        depth_min = bounds["depth_min"]
        depth_max = bounds["depth_max"]
        max_single_dim_mm = bounds["single_dim_max"]
        project_kws = ["STREET", "ROAD", "DRIVE", "AVENUE", "BOULEVARD", "ROUTE", "TEL", "POSTAL", "FAX", "PHONE", "MOBILE", "PROJECT", "CLIENT", "DOSSIER", "FILE", "CADASTRE", "LOT", "MATRICULE", "PROJET", "TITLE", "CARTOUCHE", "REVISION", "REV", "DATE", "SCALE", "ÉCHELLE", "SHEET", "PAGE", "DRAWING", "DESSIN", "ARCHITECTE", "INGÉNIEUR", "APPROVED", "APPROVAL", "SEAL", "SCEAU", "N°", "NO."]
        for p in full_json_data.get("pages", []):
            meta = p.get("metadata") or {}
            for k in ["projectName", "location", "architect", "projectID"]:
                val = str(meta.get(k, "")).upper()
                if val and val != "UNKNOWN":
                    words = [w.strip() for w in re.split(r'[^A-Z0-9À-ÖØ-ßā-ſ]', val) if len(w.strip()) >= 3]
                    project_kws.extend(words)
        project_kws = list(set(project_kws))
        def is_cover_page(p_dict: dict) -> bool:
            title = str(p_dict.get("title", "")).upper()
            dtype = str(p_dict.get("drawingType", "")).upper()
            return dtype == "COVER" or any(
                k in title or k in dtype
                for k in ["COVER", "INDEX", "FRONTISPICE", "LISTE DES PLANS", "TITLE SHEET", "TITLE PAGE"]
            )
        if scratch_dir:
            for p in full_json_data.get("pages", []):
                p_num = p.get("page_number")
                if not p_num:
                    continue
                page_md_path = os.path.join(scratch_dir, f"page_{p_num}.md")
                if os.path.exists(page_md_path):
                    try:
                        with open(page_md_path, "r", encoding="utf-8") as f:
                            text = f.read().upper()
                        old_type = p.get("drawingType")
                        old_title = p.get("title")
                        header_matches = re.findall(r'###\s*(.*)', text)
                        matched = False
                        def _set_title(p_obj, header_text):
                            cur = str(p_obj.get("title", "")).strip()
                            if not cur or cur.startswith("Page ") or cur in ("Unknown", "Untitled", "Mixed"):
                                p_obj["title"] = header_text.strip().title()
                        if header_matches:
                            for h in reversed(header_matches):
                                h_clean = h.strip().upper()
                                if "PLAN D'IMPLANTATION" in h_clean or "IMPLANTATION" in h_clean:
                                    p["drawingType"] = "Site Plan"
                                    _set_title(p, h)
                                    matched = True
                                    break
                                elif "PLAN DE FONDATION" in h_clean or "PLAN DE FONDATIONS" in h_clean or "PLAN FONDATION" in h_clean or "PLAN DES FONDATIONS" in h_clean:
                                    p["drawingType"] = "Floor Plan"
                                    _set_title(p, h)
                                    matched = True
                                    break
                                elif "PLAN SOUS-SOL" in h_clean or "PLAN DU SOUS-SOL" in h_clean or "PLAN SOUS SOL" in h_clean or "PLAN DU S-SOL" in h_clean:
                                    p["drawingType"] = "Floor Plan"
                                    _set_title(p, h)
                                    matched = True
                                    break
                                elif "REZ-DE-CHAUSSÉE" in h_clean or "REZ DE CHAUSSÉE" in h_clean or "RDC" in h_clean:
                                    p["drawingType"] = "Floor Plan"
                                    _set_title(p, h)
                                    matched = True
                                    break
                                elif "PLAN D'ÉTAGE" in h_clean or "PLAN DE L'ÉTAGE" in h_clean or "PLAN ÉTAGE" in h_clean or "FLOOR PLAN" in h_clean or "PLAN 2E ÉTAGE" in h_clean or "PLAN 3E ÉTAGE" in h_clean:
                                    p["drawingType"] = "Floor Plan"
                                    _set_title(p, h)
                                    matched = True
                                    break
                                elif "PLAN DE TOITURE" in h_clean or "PLAN DE TOIT" in h_clean or "ROOF PLAN" in h_clean:
                                    p["drawingType"] = "Roof Plan"
                                    _set_title(p, h)
                                    matched = True
                                    break
                                elif "COUPE DE MUR" in h_clean or "COUPE ET DÉTAILS" in h_clean or "WALL SECTION" in h_clean or "COUPES DE MUR" in h_clean:
                                    p["drawingType"] = "Section"
                                    _set_title(p, h)
                                    matched = True
                                    break
                                elif "COUPE" in h_clean or "SECTION" in h_clean:
                                    p["drawingType"] = "Section"
                                    _set_title(p, h)
                                    matched = True
                                    break
                                elif "ÉLÉVATION" in h_clean or "ELEVATION" in h_clean:
                                    p["drawingType"] = "Elevation"
                                    _set_title(p, h)
                                    matched = True
                                    break
                        if not matched:
                            if "IMPLANTATION" in text or "PLAN D'IMPLANTATION" in text:
                                p["drawingType"] = "Site Plan"
                                _set_title(p, "Site Plan")
                            elif "SITE PLAN" in text:
                                p["drawingType"] = "Site Plan"
                                _set_title(p, "Site Plan")
                            elif "COUPE DE MUR" in text or "COUPE ET DÉTAILS" in text or "WALL SECTION" in text:
                                p["drawingType"] = "Section"
                                _set_title(p, "Wall Section & Details")
                            elif "ÉLÉVATION" in text or "ELEVATION" in text:
                                p["drawingType"] = "Elevation"
                                _set_title(p, "Elevations")
                            elif "PLAN DE TOITURE" in text or "PLAN DE TOIT" in text or "ROOF PLAN" in text:
                                p["drawingType"] = "Roof Plan"
                                _set_title(p, "Roof Plan")
                            elif any(k in text for k in ["PLAN DE FONDATION", "PLAN DE FONDATIONS", "PLAN FONDATION", "PLAN DES FONDATIONS"]):
                                p["drawingType"] = "Floor Plan"
                                _set_title(p, "Foundation Plan")
                            elif any(k in text for k in ["PLAN SOUS-SOL", "PLAN DU SOUS-SOL", "PLAN SOUS SOL"]):
                                p["drawingType"] = "Floor Plan"
                                _set_title(p, "Basement Plan")
                            elif any(k in text for k in ["PLAN DU REZ-DE-CHAUSSÉE", "PLAN REZ-DE-CHAUSSÉE", "REZ-DE-CHAUSSÉE", "PLAN DU RDC", "PLAN RDC"]):
                                p["drawingType"] = "Floor Plan"
                                _set_title(p, "Ground Floor Plan")
                            elif "PLAN DE L'ÉTAGE" in text or "PLAN DU PREMIER ÉTAGE" in text or "PLAN D'ÉTAGE" in text:
                                p["drawingType"] = "Floor Plan"
                                _set_title(p, "Floor Plan")
                            elif "FLOOR PLAN" in text:
                                p["drawingType"] = "Floor Plan"
                                _set_title(p, "Floor Plan")
                        if p.get("drawingType") != old_type or p.get("title") != old_title:
                            updated = True
                            print(f"[TakeoffService] Corrected Page {p_num} classification: {old_type} -> {p['drawingType']} | Title: {old_title} -> {p['title']}")
                    except Exception as e:
                        print(f"[TakeoffService] Error correcting metadata for page {p_num}: {e}")
        max_single_dim_mm = 0
        if scratch_dir:
            for p in full_json_data.get("pages", []):
                p_num = p.get("page_number")
                dtype = str(p.get("drawingType", "")).upper()
                if p_num and ("FLOOR PLAN" in dtype or "PLAN" in dtype or "SECTION" in dtype):
                    page_md_path = os.path.join(scratch_dir, f"page_{p_num}.md")
                    if os.path.exists(page_md_path):
                        try:
                            with open(page_md_path, "r", encoding="utf-8") as f:
                                page_text = f.read()
                            for m in re.finditer(r'(?<![A-Za-z0-9])(?:[±+]?\s*)(\d{4,6})(?:\s*mm)?(?![A-Za-z0-9])', page_text):
                                try:
                                    v = float(m.group(1))
                                    start_idx = max(0, m.start() - 30)
                                    end_idx = min(len(page_text), m.end() + 30)
                                    snippet = page_text[start_idx:end_idx].upper()
                                    if any(kw in snippet for kw in project_kws):
                                        continue
                                    if 3000 <= v <= 100000:
                                        max_single_dim_mm = max(max_single_dim_mm, v)
                                except ValueError:
                                    pass
                        except Exception:
                            pass
        max_plausible_perimeter_mm = max(200_000, 4 * max_single_dim_mm) if max_single_dim_mm else 300_000
        # Always force dynamic resolution of buildingPerimeter and foundationWallDepth from blueprint
        for p in full_json_data.get("pages", []):
            p.pop("buildingPerimeter", None)
            p.pop("foundationWallDepth", None)
        depth_data = {}

        perimeter_data = {}

        if requires_perimeter and not perimeter_data:
            candidate_pages = []
            PLAN_KEYWORDS = {"FLOOR PLAN", "ELEVATION", "ÉLÉVATION", "SITE PLAN",
                             "FOUNDATION", "FONDATION", "IMPLANTATION", "PLAN"}
            for p in full_json_data.get("pages", []):
                if is_cover_page(p):
                    continue
                title = str(p.get("title", "")).upper()
                dtype = str(p.get("drawingType", "")).upper()
                sheet = str(p.get("sheetNumber", "")).upper()
                # Skip upper floor level plans when ground/foundation plans are present in drawing set
                if any(k in title or k in sheet for k in ["PREMIER ÉTAGE", "1ER ÉTAGE", "2E ÉTAGE", "3E ÉTAGE", "UPPER FLOOR", "ROOF"]) and any(any(g in str(pg.get("title", "")).upper() or g in str(pg.get("drawingType", "")).upper() for g in ["GROUND", "REZ-DE-CHAUSSÉE", "RDC", "FOUNDATION", "FONDATION", "BASEMENT", "SOUS-SOL"]) for pg in full_json_data.get("pages", [])):
                    continue
                if any(k in title or k in dtype for k in PLAN_KEYWORDS):
                    candidate_pages.append(p)
            def perimeter_page_priority(page):
                dtype = str(page.get("drawingType", "")).upper()
                title = str(page.get("title", "")).upper()
                # 0: Foundation / Ground / Basement Plans
                if any(k in title or k in dtype for k in ["FOUNDATION", "FONDATION", "SOUS-SOL", "BASEMENT", "GROUND FLOOR", "REZ-DE-CHAUSSÉE", "RDC", "MAIN FLOOR"]):
                    return 0
                # 1: General Floor Plans (excluding upper levels)
                if "FLOOR PLAN" in dtype or any(k in title for k in ["FLOOR PLAN", "ÉTAGE", "ETAGE", "LEVEL"]):
                    if any(k in title for k in ["PREMIER ÉTAGE", "1ER ÉTAGE", "2E ÉTAGE", "3E ÉTAGE", "UPPER FLOOR", "ROOF"]):
                        return 4
                    return 1
                # 2: Site / Implantation Plans
                if "SITE PLAN" in dtype or "IMPLANTATION" in title or "SITE" in title:
                    return 2
                # 3: General Plans
                if "PLAN" in dtype:
                    return 3
                return 5
            candidate_pages.sort(key=perimeter_page_priority)

            # A. GENERIC DOCUMENT-DRIVEN RESOLUTION (Full OCR Text Context Scan):
            dim_L, dim_W, proj_val = None, None, 0
            dim_L_raw, dim_W_raw = "", ""
            excluded_overhang_vals = []

            if scratch_dir or pdf_path:
                all_doc_dims = []
                for p in candidate_pages:
                    p_num = p.get("page_number")
                    if not p_num:
                        continue
                    page_text = ""
                    if scratch_dir:
                        page_md_path = os.path.join(scratch_dir, f"page_{p_num}.md")
                        if os.path.exists(page_md_path):
                            try:
                                with open(page_md_path, "r", encoding="utf-8") as f:
                                    page_text = f.read()
                            except Exception:
                                pass
                    if pdf_path and os.path.exists(pdf_path):
                        try:
                            import fitz
                            doc = fitz.open(pdf_path)
                            if 1 <= p_num <= len(doc):
                                ftxt = doc[p_num - 1].get_text()
                                if ftxt:
                                    page_text = f"{page_text}\n{ftxt}"
                            doc.close()
                        except Exception:
                            pass

                    if page_text:
                        try:
                            page_dims = []
                            # Metric match e.g. 22758 mm or 22 758 (excludes T24353 / dossier numbers / project codes 60029-12)
                            for m in re.finditer(r'(?<![A-Za-z0-9])(?:[±+]?\s*)(\d{1,2}[\s_.]?\d{3})(?:\s*mm)?(?![A-Za-z0-9])', page_text):
                                raw_str = m.group(1).replace(" ", "").replace("_", "").replace(".", "")
                                try:
                                    start_look = max(0, m.start() - 5)
                                    end_look = min(len(page_text), m.end() + 10)
                                    window = page_text[start_look:end_look]
                                    if re.search(rf'{raw_str}\s*-\s*\d+', window) or re.search(rf'\d+\s*-\s*{raw_str}', window):
                                        continue
                                    v = float(raw_str)
                                    start_idx = max(0, m.start() - 120)
                                    end_idx = min(len(page_text), m.end() + 120)
                                    snippet = page_text[start_idx:end_idx].upper()
                                    if dim_min <= v <= dim_max:
                                        ctx_cls = self._classify_dim_context(snippet)
                                        page_dims.append((v, f"{raw_str} mm", snippet))
                                except ValueError:
                                    pass

                            # Imperial match e.g. 74'-8" or 74' - 8"
                            for m in re.finditer(r"(?:[±+]?\s*)(\d{1,3})\s*'\s*[-–\s]?\s*(\d{1,2})(?:\s+[\d/]+)?\s*(?:""|''|\")?", page_text):
                                try:
                                    ft = float(m.group(1))
                                    inch = float(m.group(2))
                                    v = round((ft + inch / 12.0) * 304.8)
                                    start_idx = max(0, m.start() - 60)
                                    end_idx = min(len(page_text), m.end() + 60)
                                    snippet = page_text[start_idx:end_idx].upper()
                                    if dim_min <= v <= dim_max:
                                        ctx_cls = self._classify_dim_context(snippet)
                                        page_dims.append((v, f"{m.group(1)}'-{m.group(2)}\"", ctx_cls))
                                except ValueError:
                                    pass

                            # Reversed OCR match e.g. 8" - 74'
                            for m in re.finditer(r"(?:[±+]?\s*)(\d{1,2})\s*\"\s*[-–\s]?\s*(\d{1,3})\s*'", page_text):
                                try:
                                    inch = float(m.group(1))
                                    ft = float(m.group(2))
                                    v = round((ft + inch / 12.0) * 304.8)
                                    start_idx = max(0, m.start() - 60)
                                    end_idx = min(len(page_text), m.end() + 60)
                                    snippet = page_text[start_idx:end_idx].upper()
                                    if dim_min <= v <= dim_max:
                                        ctx_cls = self._classify_dim_context(snippet)
                                        page_dims.append((v, f"{m.group(2)}'-{m.group(1)}\"", ctx_cls))
                                except ValueError:
                                    pass

                            # Generic: synthesise a candidate width dimension from sub-segment pairs ONLY when
                            # the current page_dims do not yet contain two distinct major tiers (i.e. both a
                            # length and a width are NOT already present).  Guard prevents spurious combinations
                            # from polluting pages where both dimensions were already OCR'd correctly.
                            if len(page_dims) >= 2:
                                existing_vals_raw = sorted(set(d[0] for d in page_dims), reverse=True)
                                # Build 5%-tolerance tiers from already-detected values
                                _tiers: list = []
                                for _v in existing_vals_raw:
                                    _placed = False
                                    for _t in _tiers:
                                        if abs(_t[0] - _v) / _t[0] <= 0.05:
                                            _t.append(_v)
                                            _placed = True
                                            break
                                    if not _placed:
                                        _tiers.append([_v])
                                _tier_reps = [max(_t) for _t in _tiers]
                                _major_tiers = [tv for tv in _tier_reps if dim_min <= tv <= dim_max]
                                # Only synthesise when a second major-dim tier is absent
                                if len(_major_tiers) < 2:
                                    existing_vals = [d[0] for d in page_dims]
                                    top_val = max(existing_vals)
                                    sub_segs = sorted(
                                        [d for d in page_dims if d[0] < top_val * 0.75],
                                        key=lambda d: d[0], reverse=True
                                    )
                                    seen_sums: set = set()
                                    for _i in range(len(sub_segs)):
                                        for _j in range(_i + 1, len(sub_segs)):
                                            v_sum = round(sub_segs[_i][0] + sub_segs[_j][0])
                                            if v_sum in seen_sums:
                                                continue
                                            seen_sums.add(v_sum)
                                            already_present = any(
                                                abs(e - v_sum) / max(v_sum, 1) <= 0.01
                                                for e in existing_vals
                                            )
                                            if not already_present and dim_min <= v_sum <= dim_max:
                                                raw_sum_str = f"{sub_segs[_i][1]} + {sub_segs[_j][1]}"
                                                page_dims.append((v_sum, raw_sum_str, "inner"))

                            if page_dims:
                                all_doc_dims.extend(page_dims)
                                selection = self._select_inner_building_dimensions(page_dims)
                                if selection:
                                    sel_dict = selection
                                    dim_L, dim_W = sel_dict["L_in"], sel_dict["W_in"]
                                    dim_L_raw, dim_W_raw = sel_dict["raw_L"], sel_dict["raw_W"]
                                    excluded_overhang_vals = sel_dict["excl"]
                                    proj_val = 0
                                    candidate_pages = [p] + [cp for cp in candidate_pages if cp != p]
                                    print(f"[TakeoffService] Perimeter dims from page {p_num} markdown scan: Inner L={dim_L} mm, Inner W={dim_W} mm | Outer L={sel_dict['L_out']} mm, Outer W={sel_dict['W_out']} mm")
                                    break
                        except Exception as e:
                            print(f"[TakeoffService] Error scanning page {p_num} markdown: {e}")

            if not (dim_L and dim_W):
                for p in candidate_pages:
                    page_dims = []
                    for m in p.get("measurements", []):
                        val = m.get("value")
                        ctx_str = f"{m.get('associatedElement', '')} {m.get('context', '')}"
                        try:
                            val_num = float(str(val).replace(" ", "").replace(",", "."))
                            if dim_min <= val_num <= dim_max:
                                page_dims.append((val_num, str(val), self._classify_dim_context(ctx_str)))
                        except ValueError:
                            m_imp = re.search(r"(\d{1,3})\s*'\s*[-–\s]?\s*(\d{1,2})", str(val))
                            if m_imp:
                                try:
                                    ft = float(m_imp.group(1))
                                    inch = float(m_imp.group(2))
                                    val_num = round((ft + inch / 12.0) * 304.8)
                                    if dim_min <= val_num <= dim_max:
                                        page_dims.append((val_num, str(val), self._classify_dim_context(ctx_str)))
                                except ValueError: pass

                    if page_dims:
                        selection = self._select_inner_building_dimensions(page_dims)
                        if selection:
                            sel_dict = selection
                            dim_L, dim_W = sel_dict["L_in"], sel_dict["W_in"]
                            dim_L_raw, dim_W_raw = sel_dict["raw_L"], sel_dict["raw_W"]
                            excluded_overhang_vals = sel_dict.get("excl", [])
                            print(f"[TakeoffService] Perimeter dims from page {p.get('page_number')} JSON measurements: Inner L={dim_L} mm, Inner W={dim_W} mm | Outer L={sel_dict['L_out']} mm, Outer W={sel_dict['W_out']} mm")
                            break
                    selection = self._select_inner_building_dimensions(all_doc_dims)
                    if selection:
                        sel_dict = selection
                        dim_L, dim_W = sel_dict["L_in"], sel_dict["W_in"]
                        dim_L_raw, dim_W_raw = sel_dict["raw_L"], sel_dict["raw_W"]
                        excluded_overhang_vals = sel_dict.get("excl", [])
                        proj_val = 0
                        print(f"[TakeoffService] Perimeter dims from document markdown aggregation: Inner L={dim_L} mm, Inner W={dim_W} mm | Outer L={sel_dict['L_out']} mm, Outer W={sel_dict['W_out']} mm")

            if dim_L and dim_W:
                # Check for attached foundation concrete structures (e.g. SOCLE DE BÉTON, RAMPE/ESCALIER BÉTON, FONDATION EXTENSION)
                ext_found_val = 0
                ext_found_desc = ""
                ext_kws = ["SOCLE DE BÉTON", "SOCLE BÉTON", "SOCLE", "RAMPE BÉTON", "ESCALIER BÉTON", "PERRON BÉTON", "CHEEK WALL", "MURET BÉTON", "DALLE SOUTERRAINE"]
                candidate_dim_sources = (page_dims if 'page_dims' in locals() and page_dims else []) + (all_doc_dims if 'all_doc_dims' in locals() and all_doc_dims else [])
                for d_entry in candidate_dim_sources:
                    val = d_entry[0]
                    raw = d_entry[1]
                    ctx_str = str(d_entry[2]).upper() if len(d_entry) > 2 and d_entry[2] else ""
                    if 2000 <= val <= 0.85 * min(dim_L, dim_W):
                        if any(kw in ctx_str for kw in ext_kws) and val > ext_found_val:
                            ext_found_val = val
                            ext_found_desc = f"Attached Concrete Foundation Base (Callout {raw}): {val/1000:.3f} m ({round(val/304.8, 2)} ft)"

                p_core_mm = round(2 * (dim_L + dim_W) + 2 * proj_val)
                p_mm = p_core_mm + round(ext_found_val)
                p_m = round(p_mm / 1000.0, 3)
                p_ft = float(round(p_mm / 304.8)) if ext_found_val else round(p_m * 3.28084, 2)
                
                L_out = sel_dict["L_out"] if 'sel_dict' in locals() else dim_L
                W_out = sel_dict["W_out"] if 'sel_dict' in locals() else dim_W
                p_out_mm = round(2 * (L_out + W_out))
                p_out_m = round(p_out_mm / 1000.0, 3)
                p_out_ft = round(p_out_m * 3.28084, 2)

                max_plausible_perimeter_mm = max(max_single_dim_mm * 4, dim_max * 2) if max_single_dim_mm else dim_max * 2
                if p_mm <= max_plausible_perimeter_mm and p_mm >= dim_min:
                    src_page_obj = candidate_pages[0] if candidate_pages else {}
                    src_page_num = src_page_obj.get("page_number")
                    src_sheet_no = src_page_obj.get("sheetNumber", f"PAGE-{src_page_num}" if src_page_num else "N/A")
                    src_title = src_page_obj.get("title", "Floor Plan")
                    src_dtype = src_page_obj.get("drawingType", "Floor Plan")

                    len_str = f"{dim_L_raw} ({dim_L/1000:.3f} m / {dim_L} mm)" if dim_L_raw else f"{dim_L/1000:.3f} m ({dim_L} mm)"
                    wid_str = f"{dim_W_raw} ({dim_W/1000:.3f} m / {dim_W} mm)" if dim_W_raw else f"{dim_W/1000:.3f} m ({dim_W} mm)"
                    
                    is_single_footprint = (p_out_mm == p_mm or (L_out == dim_L and W_out == dim_W))
                    if ext_found_val:
                        calc_str = f"Main Building Core Footprint: 2 × ({dim_L_raw or f'{dim_L/1000:.3f}m'} + {dim_W_raw or f'{dim_W/1000:.3f}m'}) = {round(p_core_mm/1000.0, 3)} m ({round(p_core_mm/304.8, 2)} ft) + {ext_found_desc} → Total Continuous Foundation Perimeter = {p_ft:.0f} ft ({p_m} m / {p_mm} mm)"
                        details_str = (
                            f"Extracted from structural dimension callouts: "
                            f"Main Building Core: Length = {len_str}, Width = {wid_str} → Core Footprint Perimeter = {round(p_core_mm/304.8, 2)} ft ({round(p_core_mm/1000.0, 3)} m). "
                            f"Attached Foundation Extension ({ext_found_desc}) → Total Continuous Foundation Perimeter = {p_ft:.0f} ft ({p_m} m / {p_mm} mm). "
                            f"Outer Building Envelope: Length = {L_out} mm ({L_out/304.8:.2f} ft), Width = {W_out} mm ({W_out/304.8:.2f} ft) → Outer Perimeter = {p_out_ft} ft ({p_out_m} m / {p_out_mm} mm)."
                        )
                    elif is_single_footprint:
                        calc_str = f"Building Footprint Mass: 2 × ({dim_L_raw or f'{dim_L/1000:.3f}m'} + {dim_W_raw or f'{dim_W/1000:.3f}m'}) = {p_m} m ({p_ft} ft)"
                        details_str = (
                            f"Extracted from structural dimension callouts: "
                            f"Building Footprint Mass: Length = {len_str}, Width = {wid_str} → "
                            f"Main Building Foundation Perimeter = {p_ft} ft ({p_m} m / {p_mm} mm)."
                        )
                    else:
                        calc_str = f"Inner Structural Wall: 2 × ({dim_L_raw or f'{dim_L/1000:.3f}m'} + {dim_W_raw or f'{dim_W/1000:.3f}m'}) = {p_m} m ({p_ft} ft) | Outer Building Envelope: 2 × ({L_out} mm + {W_out} mm) = {p_out_m} m ({p_out_ft} ft)"
                        details_str = (
                            f"Extracted from structural dimension callouts: "
                            f"Inner Wall Line: Length = {len_str}, Width = {wid_str} → Inner Perimeter = {p_ft} ft ({p_m} m / {p_mm} mm). "
                            f"Outer Building Envelope: Length = {L_out} mm ({L_out/304.8:.2f} ft), Width = {W_out} mm ({W_out/304.8:.2f} ft) → Outer Perimeter = {p_out_ft} ft ({p_out_m} m / {p_out_mm} mm)."
                        )

                    perimeter_data = {
                        "value_mm": p_mm, "value_m": p_m, "value_ft": p_ft,
                        "calculation": calc_str,
                        "details": details_str,
                        "inner_perimeter": {
                            "value_mm": p_mm, "value_m": p_m, "value_ft": p_ft,
                            "length_mm": dim_L, "width_mm": dim_W,
                            "core_perimeter_ft": round(p_core_mm / 304.8, 2),
                            "extension_perimeter_ft": round(ext_found_val / 304.8, 2) if ext_found_val else 0.0
                        },
                        "core_perimeter": {
                            "value_mm": p_core_mm, "value_m": round(p_core_mm / 1000.0, 3), "value_ft": round(p_core_mm / 304.8, 2),
                            "length_mm": dim_L, "width_mm": dim_W
                        },
                        "outer_perimeter": {
                            "value_mm": p_out_mm, "value_m": p_out_m, "value_ft": p_out_ft,
                            "length_mm": L_out, "width_mm": W_out
                        },
                        "source_page": src_page_num,
                        "source_sheet": src_sheet_no,
                        "source_title": src_title,
                        "source_page_type": src_dtype,
                        "method": "Continuous Foundation Perimeter Callout Analysis" if ext_found_val else "Dual Building Envelope Callout Analysis"
                    }
                    for pd in full_json_data.get("pages", []):
                        if pd.get("drawingType", "").upper() in ("FLOOR PLAN", "PLAN"):
                            pd["buildingPerimeter"] = perimeter_data
                            break
                    updated = True
                    print(f"[TakeoffService] Perimeter resolved dynamically from blueprint text scan: {p_ft} ft ({p_m} m)")

            # B. VISION FLOORPLAN FALLBACK (Only executed if text scanning does not yield both dimensions):
            if not perimeter_data and candidate_pages and api_key and pdf_path:
                for p in candidate_pages:
                    p_num = p.get("page_number")
                    dtype = str(p.get("drawingType", "")).upper()
                    if not p_num or "ELEVATION" in dtype or "ROOF" in dtype:
                        continue
                    try:
                        scale = p.get("scale", "N/A")
                        page_txt = ""
                        if scratch_dir and p_num:
                            page_md_path = os.path.join(scratch_dir, f"page_{p_num}.md")
                            if os.path.exists(page_md_path):
                                try:
                                    with open(page_md_path, "r", encoding="utf-8") as f:
                                        page_txt = f.read()
                                except Exception:
                                    pass
                        if not page_txt and pdf_path and os.path.exists(pdf_path):
                            try:
                                import fitz
                                doc = fitz.open(pdf_path)
                                if 1 <= p_num <= len(doc):
                                    page_txt = doc[p_num - 1].get_text()
                                doc.close()
                            except Exception:
                                pass

                        img_data, mime_type = self._render_page_image(pdf_path, scratch_dir, p_num)
                        result = self._extract_perimeter_from_floorplan_image(api_key, img_data, scale, page_text=page_txt)
                        if result.get("perimeter_mm") and result["perimeter_mm"] >= dim_min:
                            p_mm = round(result["perimeter_mm"])
                            p_m = round(p_mm / 1000.0, 3)
                            p_ft = round(p_m * 3.28084, 2)
                            perimeter_data = {
                                "value_mm": p_mm,
                                "value_m": p_m,
                                "value_ft": p_ft,
                                "calculation": result.get("calculation", f"Net building footprint perimeter = {p_m} m ({p_ft} ft)"),
                                "details": result.get("details", result.get("reasoning", "Extracted core foundation wall dimensions excluding gallery overhangs.")),
                                "source_page": p_num,
                                "source_sheet": p.get("sheetNumber", f"PAGE-{p_num}"),
                                "source_title": p.get("title", "Floor Plan"),
                                "source_page_type": dtype or "Floor Plan",
                                "method": "Vision Floorplan Footprint Analysis"
                            }
                            p["buildingPerimeter"] = perimeter_data
                            updated = True
                            print(f"[TakeoffService] Net building perimeter resolved via Vision: {p_ft} ft ({p_m} m)")
                            break
                    except Exception as e:
                        print(f"[TakeoffService] Vision floorplan pass failed on page {p_num}: {e}")

            # C. VISION ELEVATION FALLBACK:
            if not perimeter_data and candidate_pages:
                for p in candidate_pages:
                    p_num = p.get("page_number")
                    try:
                        scale = p.get("scale", "N/A")
                        page_txt = ""
                        if scratch_dir and p_num:
                            page_md_path = os.path.join(scratch_dir, f"page_{p_num}.md")
                            if os.path.exists(page_md_path):
                                try:
                                    with open(page_md_path, "r", encoding="utf-8") as f:
                                        page_txt = f.read()
                                except Exception:
                                    pass
                        if not page_txt and pdf_path and os.path.exists(pdf_path):
                            try:
                                import fitz
                                doc = fitz.open(pdf_path)
                                if 1 <= p_num <= len(doc):
                                    page_txt = doc[p_num - 1].get_text()
                                doc.close()
                            except Exception:
                                pass

                        img_data, mime_type = self._render_page_image(pdf_path, scratch_dir, p_num)
                        result = self._extract_perimeter_from_elevation_image(api_key, img_data, scale, page_text=page_txt)
                        if result.get("perimeter_mm") and dim_min <= result["perimeter_mm"] <= max_plausible_perimeter_mm:
                            perimeter_data = {
                                "value_mm": result["perimeter_mm"], "value_m": result.get("perimeter_m"),
                                "value_ft": result.get("perimeter_ft"),
                                "calculation": result.get("calculation", "Sum of exterior facade widths"),
                                "details": "Extracted via vision analysis of horizontal facade elevation dimension chains.",
                                "source_page": p_num,
                                "source_sheet": p.get("sheetNumber", f"PAGE-{p_num}"),
                                "source_title": p.get("title", "Elevation Drawing"),
                                "source_page_type": p.get("drawingType", "Elevation"),
                                "method": "Vision Elevation Model Extraction"
                            }
                            p["buildingPerimeter"] = perimeter_data
                            updated = True
                            break
                    except Exception as e:
                        print(f"[TakeoffService] Perimeter vision pass failed on page {p_num}: {e}")

        # 3. Resolve foundation vertical dimensions & drain girth (from Section / Detail / Elevation sheets)
        if requires_depth and not depth_data:
            candidate_section_pages = []
            SECTION_TYPES = {"SECTION", "DETAIL", "WALL SECTION", "COUPE", "DÉTAIL", "ELEVATION", "ÉLÉVATION"}
            SECTION_KEYWORDS = ["SECTION", "DETAIL", "COUPE", "DÉTAIL", "WALL SECTION", "ELEVATION", "ÉLÉVATION", "FONDATION"]
            SKIP_TYPES = {"FLOOR PLAN", "SITE PLAN", "ROOF PLAN", "COVER", "INDEX", "LEGEND", "SCHEDULE", "FRAMING PLAN", "CEILING PLAN"}
            for p in full_json_data.get("pages", []):
                if is_cover_page(p): continue
                title = str(p.get("title", "")).upper()
                dtype = str(p.get("drawingType", "")).upper()
                sheet = str(p.get("sheetNumber", "")).upper()
                if dtype in SKIP_TYPES:
                    continue
                if dtype in SECTION_TYPES or any(k in dtype or k in title or k in sheet for k in SECTION_KEYWORDS):
                    candidate_section_pages.append(p)
            
            def section_priority(p):
                dtype = str(p.get("drawingType", "")).upper()
                title = str(p.get("title", "")).upper()
                if "WALL SECTION" in dtype or "COUPE DE MUR" in title:
                    return 0
                if "SECTION" in dtype or "COUPE" in title:
                    return 1
                if "DETAIL" in dtype or "DÉTAIL" in title:
                    return 2
                if "ELEVATION" in dtype or "ÉLÉVATION" in title:
                    return 3
                return 4
            candidate_section_pages.sort(key=section_priority)

            # A. DYNAMIC DATUM AND FRENCH DRAIN RESOLUTION:
            datum_results = self._parse_elevation_datums(candidate_section_pages, scratch_dir)
            drain_girth_data = self._parse_french_drain_girth(candidate_section_pages, scratch_dir)

            # B. GENERIC SECTION MEASUREMENTS PARSING:
            grade_to_footing_h, total_wall_h, exposure_h, footing_h = None, None, None, None
            grade_raw, wall_raw, exposure_raw, footing_raw = "", "", "", ""
            wall_page, grade_page, footing_page = None, None, None
            
            below_grade_kws = ["grade", "sol fini", "sol", "remblai", "terrain fini", "below grade", "contact terre", "sous terre", "ragréer", "ragreer", "apparent", "fondation apparente"]
            exposure_kws = ["exposure", "dégagement", "hors sol", "above grade", "saillie"]
            wall_kws = ["wall", "mur", "hauteur", "height", "fondation", "foundation", "vertical", "détail", "detail", "coupe", "section", "béton", "concrete", "élévation", "elevation"]
            footing_kws = ["footing", "semelle", "heel", "embase"]

            for p in candidate_section_pages:
                for m in p.get("measurements", []):
                    context = str(m.get("associatedElement", "")).lower()
                    val = m.get("value")
                    val_num = self._parse_dim_to_mm(val)

                    if val_num is not None:
                        if any(k in context for k in footing_kws) and (100 <= val_num <= 1000 or int(depth_min * 0.15) <= val_num <= int(depth_max * 0.4)):
                            footing_h = val_num
                            footing_raw = str(val)
                            footing_page = p
                        elif any(k in context for k in exposure_kws) and (50 <= val_num <= 600):
                            exposure_h = val_num
                            exposure_raw = str(val)
                        elif any(k in context for k in below_grade_kws) and depth_min <= val_num <= depth_max:
                            if grade_to_footing_h is None or val_num > grade_to_footing_h:
                                grade_to_footing_h = val_num
                                grade_raw = str(val)
                                grade_page = p
                        elif (any(k in context for k in wall_kws) or not context or context in ["dimension", "height", "hauteur"]) and depth_min <= val_num <= depth_max:
                            if total_wall_h is None or val_num > total_wall_h:
                                total_wall_h = val_num
                                wall_raw = str(val)
                                wall_page = p

            primary_sec_p = candidate_section_pages[0] if candidate_section_pages else {}

            # Construct below_grade_depth:
            below_grade_mm = None
            below_grade_desc = ""
            below_grade_src_page = grade_page or wall_page or primary_sec_p

            if datum_results.get("below_grade_datum_delta_mm"):
                below_grade_mm = datum_results["below_grade_datum_delta_mm"]
                below_grade_desc = datum_results["below_grade_desc"]
                below_grade_src_page = datum_results.get("below_grade_page") or below_grade_src_page
            elif grade_to_footing_h:
                below_grade_mm = round(grade_to_footing_h)
                g_ft = round(below_grade_mm / 304.8, 2)
                below_grade_desc = f"Direct below-grade section callout (Finished Grade to Top of Footing): {grade_raw or below_grade_mm} ({g_ft} ft / {below_grade_mm} mm)"
            elif total_wall_h and exposure_h:
                below_grade_mm = round(total_wall_h - exposure_h)
                w_ft = round(total_wall_h / 304.8, 2)
                exp_ft = round(exposure_h / 304.8, 2)
                d_ft_tmp = round(below_grade_mm / 304.8, 2)
                below_grade_desc = f"Calculated: Total Wall Height ({wall_raw or total_wall_h} / {w_ft} ft) - Above-Grade Exposure ({exposure_raw or exposure_h} / {exp_ft} ft) → Below-Grade Depth = {d_ft_tmp} ft ({below_grade_mm} mm)"
            elif total_wall_h:
                below_grade_mm = round(total_wall_h)
                w_ft = round(below_grade_mm / 304.8, 2)
                below_grade_desc = f"Foundation Wall Height callout: {wall_raw or below_grade_mm} ({w_ft} ft / {below_grade_mm} mm)"

            below_grade_data = None
            if below_grade_mm:
                bg_m = round(below_grade_mm / 1000.0, 3)
                bg_ft = round(bg_m * 3.28084, 2)
                bg_num = below_grade_src_page.get("page_number") if isinstance(below_grade_src_page, dict) else None
                bg_sheet = below_grade_src_page.get("sheetNumber", f"PAGE-{bg_num}" if bg_num else "N/A") if isinstance(below_grade_src_page, dict) else "N/A"
                bg_title = below_grade_src_page.get("title", "Wall Section") if isinstance(below_grade_src_page, dict) else "Wall Section"
                bg_dtype = below_grade_src_page.get("drawingType", "Wall Section") if isinstance(below_grade_src_page, dict) else "Wall Section"
                below_grade_data = {
                    "value_mm": below_grade_mm,
                    "value_m": bg_m,
                    "value_ft": bg_ft,
                    "description": f"{below_grade_desc} → Below-Grade Depth = {bg_ft} ft ({bg_m} m / {below_grade_mm} mm)",
                    "details": "Vertical distance of foundation wall in contact with backfill from finished grade to top/base of footing.",
                    "source_page": bg_num,
                    "source_sheet": bg_sheet,
                    "source_title": bg_title,
                    "source_page_type": bg_dtype,
                    "method": "Below-Grade Section Analysis"
                }

            # Construct full_foundation_height:
            full_height_mm = None
            full_height_desc = ""
            full_height_src_page = wall_page or footing_page or primary_sec_p

            if datum_results.get("full_height_datum_delta_mm"):
                full_height_mm = datum_results["full_height_datum_delta_mm"]
                full_height_desc = datum_results["full_height_desc"]
                full_height_src_page = datum_results.get("full_height_page") or full_height_src_page
            elif total_wall_h and footing_h:
                full_height_mm = round(total_wall_h + footing_h)
                w_ft = round(total_wall_h / 304.8, 2)
                f_ft = round(footing_h / 304.8, 2)
                tot_ft = round(full_height_mm / 304.8, 2)
                full_height_desc = f"Calculated: Total Wall Height ({wall_raw or total_wall_h} / {w_ft} ft) + Footing Thickness ({footing_raw or footing_h} / {f_ft} ft) → Full Foundation Height = {tot_ft} ft ({full_height_mm} mm)"
            elif total_wall_h:
                full_height_mm = round(total_wall_h)
                w_ft = round(full_height_mm / 304.8, 2)
                full_height_desc = f"Full Foundation Wall Height callout: {wall_raw or full_height_mm} ({w_ft} ft / {full_height_mm} mm)"

            full_height_data = None
            if full_height_mm:
                fh_m = round(full_height_mm / 1000.0, 3)
                fh_ft = round(fh_m * 3.28084, 2)
                fh_num = full_height_src_page.get("page_number") if isinstance(full_height_src_page, dict) else None
                fh_sheet = full_height_src_page.get("sheetNumber", f"PAGE-{fh_num}" if fh_num else "N/A") if isinstance(full_height_src_page, dict) else "N/A"
                fh_title = full_height_src_page.get("title", "Wall Section") if isinstance(full_height_src_page, dict) else "Wall Section"
                fh_dtype = full_height_src_page.get("drawingType", "Wall Section") if isinstance(full_height_src_page, dict) else "Wall Section"
                full_height_data = {
                    "value_mm": full_height_mm,
                    "value_m": fh_m,
                    "value_ft": fh_ft,
                    "description": f"{full_height_desc} → Full Foundation Height = {fh_ft} ft ({fh_m} m / {full_height_mm} mm)",
                    "details": "Total vertical height of poured concrete foundation wall from subfloor ledge down to bottom of footing.",
                    "source_page": fh_num,
                    "source_sheet": fh_sheet,
                    "source_title": fh_title,
                    "source_page_type": fh_dtype,
                    "method": "Full Foundation Height Analysis"
                }

            # C. AUTHORITATIVE DRAWING CALLOUT SCAN (Elevation & Section Sheets):
            if not below_grade_data and candidate_section_pages:
                depth_candidates = []
                effective_depth_min = max(1000, depth_min)
                sheet_codes = {"001", "101", "201", "202", "301", "302", "303", "401", "501", "601", "701", "801", "901"}
                ignore_element_kws = [
                    "POTEAU", "FENÊTRE", "FENETRE", "VENTILATEUR", "LUCARNE", "PORTE",
                    "CONDUIT", "SOFFITE", "FASCIA", "SPIGOT", "LIMON", "ESCALIER",
                    "VERRE", "FIXATION", "PINCE", "GARDE-CORPS", "GARDE CORPS",
                    "MAIN-COURANTE", "MAIN COURANTE", "ÉVENT", "EVENT", "RAILING"
                ]

                for p in candidate_section_pages:
                    p_num = p.get("page_number")
                    if not p_num:
                        continue
                    text = ""
                    if scratch_dir:
                        page_md_path = os.path.join(scratch_dir, f"page_{p_num}.md")
                        if os.path.exists(page_md_path):
                            try:
                                with open(page_md_path, "r", encoding="utf-8") as f:
                                    text = f.read()
                            except Exception:
                                pass
                    if pdf_path and os.path.exists(pdf_path):
                        try:
                            import fitz
                            doc = fitz.open(pdf_path)
                            if 1 <= p_num <= len(doc):
                                ftxt = doc[p_num - 1].get_text()
                                if ftxt:
                                    text = f"{text}\n{ftxt}"
                            doc.close()
                        except Exception:
                            pass

                    if not text:
                        continue

                    for m in re.finditer(r'(?:[±+]?\s*)(\d{3,5})(?:\s*mm)?', text, re.IGNORECASE):
                        raw_str = m.group(1).replace(" ", "").replace("_", "").replace(".", "")
                        if raw_str in sheet_codes:
                            continue
                        try:
                            v = float(raw_str)
                            start_idx = max(0, m.start() - 100)
                            end_idx = min(len(text), m.end() + 100)
                            snippet = text[start_idx:end_idx].upper()
                            # Reject cartouche, address, title block, or calendar year/date text dynamically
                            is_title_or_address = (
                                any(kw in snippet for kw in project_kws)
                                or bool(re.search(r'\b(19\d{2}|20\d{2})\b', raw_str))
                                or bool(re.search(r'\b\d{4}[-/]\d{2}(?:[-/]\d{2})?\b', snippet))
                                or any(kw in snippet for kw in ["RUE", "STREET", "AVENUE", "BOULEVARD", "CHEMIN", "ROUTE", "HIGHWAY", "CADASTRE", "DOSSIER", "MATRICULE", "CARTOUCHE"])
                            )
                            if is_title_or_address:
                                continue
                            if any(kw in snippet for kw in ignore_element_kws) and not any(k in snippet for k in ["FONDATION", "MUR", "SOL", "SEMELLE", "ÉLÉVATION", "ELEVATION", "RAGRÉER", "RAGREER"]):
                                continue
                            if effective_depth_min <= v <= depth_max:
                                is_elev = any(k in snippet for k in ["ÉLÉVATION", "ELEVATION", "FONDATION", "SOL", "MUR", "GRADE", "RAGRÉER", "RAGREER"])
                                is_typ = "TYP" in snippet
                                has_plusminus = "±" in text[max(0, m.start()-2):m.start()]
                                # Score priority: 0 for elevation/foundation callout with ±/TYP, 1 for elevation/foundation, 2 for general
                                score = 0 if (is_elev and (is_typ or has_plusminus)) else (1 if is_elev else 2)
                                raw_val = f"±{raw_str} mm" if has_plusminus else f"{raw_str} mm"
                                depth_candidates.append((score, v, raw_val, is_elev, p))
                        except ValueError:
                            pass

                    for m in re.finditer(r"(?:[±+]?\s*)(\d{1,2})\s*'\s*-\s*(\d{1,2})(?:\s+[\d/]+)?\s*(?:""|''|\")?", text):
                        try:
                            ft = float(m.group(1))
                            inch = float(m.group(2))
                            v = round((ft + inch / 12.0) * 304.8)
                            start_idx = max(0, m.start() - 80)
                            end_idx = min(len(text), m.end() + 80)
                            snippet = text[start_idx:end_idx].upper()
                            if effective_depth_min <= v <= depth_max:
                                has_ctx = any(k in snippet for k in ["FONDATION", "WALL", "GRADE", "HEIGHT", "SECTION", "ELEVATION", "ÉLÉVATION"])
                                depth_candidates.append((0 if has_ctx else 1, v, f"{m.group(1)}'-{m.group(2)}\"", has_ctx, p))
                        except ValueError:
                            pass

                if depth_candidates:
                    # Sort prioritizing authoritative foundation elevation callouts in the 1400-2600mm typical residential depth band
                    depth_candidates.sort(key=lambda x: (x[0], 0 if (1400 <= x[1] <= 2600) else 1, section_priority(x[4])))
                    score, val_mm, val_raw, has_ctx, src_p = depth_candidates[0]
                    d_m = round(val_mm / 1000.0, 3)
                    d_ft = round(val_mm / 304.8, 1)
                    src_p_num = src_p.get("page_number")
                    below_grade_data = {
                        "value_mm": round(val_mm),
                        "value_m": d_m,
                        "value_ft": d_ft,
                        "description": f"Extracted from drawing foundation elevation callout: {val_raw} ({d_ft} ft / {d_m} m / {round(val_mm)} mm)",
                        "details": f"Vertical foundation wall depth ({val_raw}) extracted from architectural section/elevation callout.",
                        "source_page": src_p_num,
                        "source_sheet": src_p.get("sheetNumber", f"PAGE-{src_p_num}"),
                        "source_title": src_p.get("title", "Foundation Section / Elevation"),
                        "source_page_type": src_p.get("drawingType", "Section / Elevation"),
                        "method": "Structural Drawing Callout Analysis"
                    }
                    if not full_height_data:
                        fh_mm = round(val_mm + 300)  # Add standard footing thickness
                        fh_m = round(fh_mm / 1000.0, 3)
                        fh_ft = round(fh_mm / 304.8, 1)
                        full_height_data = {
                            "value_mm": fh_mm,
                            "value_m": fh_m,
                            "value_ft": fh_ft,
                            "description": f"Full Foundation Height (Wall {val_raw} + Footing ~300mm) = {fh_ft} ft ({fh_m} m / {fh_mm} mm)",
                            "details": "Total vertical height of foundation wall from top of wall down to footing base.",
                            "source_page": src_p_num,
                            "source_sheet": src_p.get("sheetNumber", f"PAGE-{src_p_num}"),
                            "source_title": src_p.get("title", "Foundation Section / Elevation"),
                            "source_page_type": src_p.get("drawingType", "Section / Elevation"),
                            "method": "Structural Drawing Callout Analysis"
                        }
                    print(f"[TakeoffService] Foundation depth resolved from drawing callout: {val_raw} -> {d_ft} ft ({d_m} m)")

            # D. VISION FALLBACK (Only executed if text scanning does not yield parameters):
            needs_vision_depth = False
            if requires_depth:
                if target_dim_key == "geotextile_drain_girth":
                    needs_vision_depth = not drain_girth_data
                elif target_dim_key == "full_foundation_height":
                    needs_vision_depth = not full_height_data
                else:
                    needs_vision_depth = not below_grade_data and not full_height_data

            if needs_vision_depth and candidate_section_pages and api_key and pdf_path:
                for p in candidate_section_pages:
                    p_num = p.get("page_number")
                    try:
                        page_txt = ""
                        if scratch_dir and p_num:
                            md_file = os.path.join(scratch_dir, f"page_{p_num}.md")
                            if os.path.exists(md_file):
                                with open(md_file, "r", encoding="utf-8") as f:
                                    page_txt = f.read()

                        img_data, mime_type = self._render_page_image(pdf_path, scratch_dir, p_num)
                        result = self._extract_depth_from_section_image(api_key, img_data, page_md_text=page_txt)
                        
                        v_full = result.get("full_foundation_height_mm")
                        v_bg = result.get("below_grade_depth_mm") or result.get("calculated_depth_mm") or result.get("grade_to_footing_mm")
                        v_drain = result.get("geotextile_drain_girth_mm")
                        tot_w = result.get("total_wall_height_mm")
                        foot_h = result.get("footing_thickness_mm")
                        exp_h = result.get("above_grade_exposure_mm")

                        if not below_grade_data and (v_bg or tot_w):
                            eff_bg = float(v_bg or (tot_w - (exp_h or 0)))
                            if depth_min <= eff_bg <= depth_max:
                                bg_d_mm = round(eff_bg)
                                bg_d_m = round(bg_d_mm / 1000.0, 3)
                                bg_d_ft = round(bg_d_m * 3.28084, 2)
                                below_grade_data = {
                                    "value_mm": bg_d_mm, "value_m": bg_d_m, "value_ft": bg_d_ft,
                                    "description": result.get("description") or f"Below-Grade Wall Depth = {bg_d_ft} ft ({bg_d_m} m / {bg_d_mm} mm)",
                                    "details": "Vertical distance in contact with earth from finished grade down to footing.",
                                    "source_page": p_num,
                                    "source_sheet": p.get("sheetNumber", f"PAGE-{p_num}"),
                                    "source_title": p.get("title", "Wall Section"),
                                    "source_page_type": p.get("drawingType", "Wall Section"),
                                    "method": "Vision Section Extraction"
                                }

                        if not full_height_data and (v_full or tot_w):
                            eff_fh = float(v_full or (tot_w + (foot_h or 0)))
                            if depth_min <= eff_fh <= depth_max:
                                fh_d_mm = round(eff_fh)
                                fh_d_m = round(fh_d_mm / 1000.0, 3)
                                fh_d_ft = round(fh_d_m * 3.28084, 2)
                                full_height_data = {
                                    "value_mm": fh_d_mm, "value_m": fh_d_m, "value_ft": fh_d_ft,
                                    "description": f"Full Foundation Height = {fh_d_ft} ft ({fh_d_m} m / {fh_d_mm} mm)",
                                    "details": "Total vertical height from top of foundation wall down to footing base.",
                                    "source_page": p_num,
                                    "source_sheet": p.get("sheetNumber", f"PAGE-{p_num}"),
                                    "source_title": p.get("title", "Wall Section"),
                                    "source_page_type": p.get("drawingType", "Wall Section"),
                                    "method": "Vision Section Extraction"
                                }

                        if not drain_girth_data and v_drain and (300 <= float(v_drain) <= 4000):
                            dg_mm = round(float(v_drain))
                            dg_m = round(dg_mm / 1000.0, 3)
                            dg_ft = round(dg_m * 3.28084, 2)
                            drain_girth_data = {
                                "value_mm": dg_mm, "value_m": dg_m, "value_ft": dg_ft,
                                "description": f"French Drain Wrap Girth = {dg_ft} ft ({dg_m} m / {dg_mm} mm)",
                                "details": "Filter fabric wrap girth around French drain crushed stone envelope.",
                                "source_page": p_num,
                                "source_sheet": p.get("sheetNumber", f"PAGE-{p_num}"),
                                "source_title": p.get("title", "French Drain Detail"),
                                "source_page_type": p.get("drawingType", "Detail Section"),
                                "method": "Vision Drain Detail Extraction"
                            }

                        if below_grade_data and full_height_data:
                            break
                    except Exception as e:
                        print(f"[TakeoffService] Section vision pass failed on page {p_num}: {e}")

            # E. STRUCTURE COMBINED DEPTH DATA:
            primary_d_obj = below_grade_data or full_height_data or drain_girth_data or {}
            if primary_d_obj:
                depth_data = {
                    "value_mm": primary_d_obj.get("value_mm"),
                    "value_m": primary_d_obj.get("value_m"),
                    "value_ft": primary_d_obj.get("value_ft"),
                    "description": primary_d_obj.get("description", ""),
                    "details": primary_d_obj.get("details", ""),
                    "source_page": primary_d_obj.get("source_page"),
                    "source_sheet": primary_d_obj.get("source_sheet"),
                    "source_title": primary_d_obj.get("source_title"),
                    "source_page_type": primary_d_obj.get("source_page_type"),
                    "below_grade_depth": below_grade_data,
                    "full_foundation_height": full_height_data,
                    "geotextile_drain_girth": drain_girth_data,
                    "perimeter": perimeter_data,
                }
                full_json_data["pages"][0]["foundationWallDepth"] = depth_data
                full_json_data["pages"][0]["foundationDimensions"] = {
                    "perimeter_linear_feet": perimeter_data,
                    "below_grade_depth": below_grade_data,
                    "full_foundation_height": full_height_data,
                    "geotextile_drain_girth": drain_girth_data,
                }
                updated = True
                print(f"[TakeoffService] Foundation dimensions resolved: Full Height={full_height_data.get('value_ft') if full_height_data else 'N/A'} ft, Below-Grade={below_grade_data.get('value_ft') if below_grade_data else 'N/A'} ft, Drain Girth={drain_girth_data.get('value_ft') if drain_girth_data else 'N/A'} ft")

        # Save updated cached metadata to JSON file
        if updated and metadata_json_path and os.path.exists(metadata_json_path):
            try:
                with open(metadata_json_path, "w", encoding="utf-8") as f:
                    json.dump(full_json_data, f, indent=2, ensure_ascii=False)
            except Exception as e:
                print(f"[TakeoffService] Failed to save resolved dimensions: {e}")

        if cache_file and (perimeter_data or depth_data):
            try:
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump({"perimeter": perimeter_data, "depth": depth_data}, f, indent=2, ensure_ascii=False)
            except Exception as e:
                print(f"[TakeoffService] Failed to save takeoff dimensions cache: {e}")

        return perimeter_data, depth_data
    def resolve_document_dimensions(self, json_data: dict) -> tuple:
        # Wrapper around _resolve_building_dimensions for unit compatibility
        return self._resolve_building_dimensions(
            api_key="",
            pdf_path="",
            scratch_dir="",
            full_json_data=json_data,
            metadata_json_path="",
            requires_perimeter=True,
            requires_depth=True
        )
    def _resolve_takeoff_unit(self, takeoff_unit: str, doc_measurements: list) -> str:
        if not takeoff_unit or "/" not in takeoff_unit:
            return takeoff_unit
        uses_metric = True
        for m in doc_measurements:
            unit = str(m.get("unit", "")).lower()
            if any(u in unit for u in ["ft", "in", "sq.ft", "linear feet", "lin.ft"]):
                uses_metric = False
                break
        parts = [p.strip() for p in takeoff_unit.split("/")]
        def is_metric(u: str) -> bool:
            u_lower = u.lower()
            return any(x in u_lower for x in ["m²", "meter", "metre", "mm", "cm", "sq.m"])
        metric_parts = [p for p in parts if is_metric(p)]
        imperial_parts = [p for p in parts if not is_metric(p)]
        if uses_metric and metric_parts:
            return metric_parts[0]
        elif not uses_metric and imperial_parts:
            return imperial_parts[0]
        return parts[0]
    def _validate_and_correct_takeoff(
        self,
        takeoff_res: dict,
        perimeter_data: dict,
        depth_data: dict,
        takeoff_unit: str,
        requires_perimeter: bool,
        requires_depth: bool,
        std_name: str = "",
        kb_params: dict = None,
        full_json_data: dict = None,
        page_num: int = None,
        sheet_number: str = "",
        sheet_title: str = "",
        drawing_type: str = "",
    ) -> dict:
        """
        Validates the takeoff calculation returned by the LLM and formats a clean,
        generic measurement estimation summary using Pi Carrée (sq.ft) and m² units.
        Calculates values 100% dynamically without hardcoded constants.
        """
        if not takeoff_res:
            return takeoff_res
        if not kb_params:
            kb_params = {}
        if not std_name:
            std_name = takeoff_res.get("material", "Material")
        category = kb_params.get("category", "Général")
        waste_pct = kb_params.get("default_waste_percent", 10)
        # ── Extract perimeter values — always prefer the INNER structural wall line ──
        # If the perimeter_data contains an 'inner_perimeter' sub-key (populated by the
        # new text-scan or vision extraction passes), use THOSE values.  This ensures
        # that even if the top-level value_mm was accidentally set to the outer envelope,
        # we always calculate using the thermal-envelope (inner) line.
        inner_p = (perimeter_data or {}).get("inner_perimeter", {})
        if inner_p and inner_p.get("value_mm"):
            p_mm = float(inner_p["value_mm"])
            p_m  = inner_p.get("value_m",  round(p_mm / 1000.0, 3))
            p_ft = inner_p.get("value_ft", round(p_mm / 304.8, 2))
            # Warn if the top-level value diverged from the inner line
            top_mm = float((perimeter_data or {}).get("value_mm", p_mm))
            if abs(top_mm - p_mm) > 1:
                print(
                    f"[TakeoffService] NOTICE: perimeter_data.value_mm ({top_mm/1000:.2f} m) "
                    f"differs from inner_perimeter ({p_mm/1000:.2f} m) — using inner line for takeoff."
                )
        else:
            # Fallback: use top-level value (assume it is already the inner line)
            p_ft = perimeter_data.get("value_ft") if perimeter_data else None
            p_m  = perimeter_data.get("value_m")  if perimeter_data else None
            p_mm = perimeter_data.get("value_mm") if perimeter_data else None
            if p_ft is None and p_mm:
                p_ft = round(p_mm / 304.8, 2)
            if p_m is None and p_mm:
                p_m = round(p_mm / 1000, 3)

        # Determine the target dimension for this specific material
        target_dim_key = (kb_params or {}).get("vertical_dimension_target", "below_grade_depth" if requires_depth else "none")
        target_dim_data = None
        dim_label = "Foundation Wall Depth"

        if depth_data:
            if target_dim_key == "full_foundation_height":
                target_dim_data = depth_data.get("full_foundation_height") or depth_data
                dim_label = "Full Foundation Wall Height (Top of Wall to Footing Base)"
            elif target_dim_key == "below_grade_depth":
                target_dim_data = depth_data.get("below_grade_depth") or depth_data
                dim_label = "Below-Grade Foundation Wall Depth (Finished Grade to Footing)"
            elif target_dim_key == "geotextile_drain_girth":
                target_dim_data = depth_data.get("geotextile_drain_girth") or depth_data
                dim_label = "French Drain Geotextile Wrap Girth (Stone Envelope Wrap)"
            else:
                target_dim_data = depth_data
                dim_label = "Foundation Wall Depth"

        # Resolve target dimension numeric values
        d_ft = target_dim_data.get("value_ft") if target_dim_data else (depth_data.get("value_ft") if depth_data else None)
        d_m = target_dim_data.get("value_m") if target_dim_data else (depth_data.get("value_m") if depth_data else None)
        d_mm = target_dim_data.get("value_mm") if target_dim_data else (depth_data.get("value_mm") if depth_data else None)
        if d_ft is None and d_mm:
            d_ft = round(d_mm / 304.8, 2)
        if d_m is None and d_mm:
            d_m = round(d_mm / 1000, 3)

        # Check raw page measurements extracted in takeoff_res if perimeter/depth is missing
        if (p_ft is None or (requires_depth and d_ft is None)) and takeoff_res.get("measurements"):
            extracted_lengths = []
            extracted_height = None
            for m in takeoff_res["measurements"]:
                m_type = str(m.get("type", "")).lower()
                val_str = str(m.get("value", "")).replace(" ", "").replace(",", ".")
                try:
                    val_num = float(val_str)
                    unit_str = str(m.get("unit", "")).lower()
                    val_m = val_num
                    if "mm" in unit_str:
                        val_m = val_num / 1000.0
                    elif "ft" in unit_str or "'" in unit_str:
                        val_m = val_num / 3.28084
                    elif "cm" in unit_str:
                        val_m = val_num / 100.0
                    if "height" in m_type or "depth" in m_type or "hauteur" in m_type or "girth" in m_type:
                        extracted_height = val_m
                    elif "length" in m_type or "width" in m_type or "longueur" in m_type or "largeur" in m_type:
                        extracted_lengths.append(val_m)
                except ValueError:
                    pass
            if extracted_lengths and p_m is None:
                if len(extracted_lengths) >= 2:
                    p_m = round(2 * (extracted_lengths[0] + extracted_lengths[1]), 3)
                else:
                    p_m = round(extracted_lengths[0], 3)
                p_ft = round(p_m * 3.28084, 2)
            if extracted_height and d_m is None:
                d_m = round(extracted_height, 3)
                d_ft = round(d_m * 3.28084, 2)

        # Resolve default fallback page numbers dynamically from JSON pages structure
        default_p_page = None
        default_d_page = None
        if full_json_data:
            for pd in full_json_data.get("pages", []):
                dtype = str(pd.get("drawingType", "")).upper()
                if "FLOOR PLAN" in dtype or "SITE PLAN" in dtype or "PLAN" in dtype:
                    if default_p_page is None:
                        default_p_page = pd.get("page_number")
                if "SECTION" in dtype or "DETAIL" in dtype:
                    if default_d_page is None:
                        default_d_page = pd.get("page_number")

        p_page = (perimeter_data.get("source_page") if perimeter_data else None) or default_p_page
        p_sheet = (perimeter_data.get("source_sheet") if perimeter_data else None) or (f"PAGE-{p_page}" if p_page else "N/A")
        p_title = (perimeter_data.get("source_title") if perimeter_data else None) or "Plan Drawing"
        p_dtype = (perimeter_data.get("source_page_type") if perimeter_data else None) or "Floor Plan"

        d_src_obj = target_dim_data or depth_data or {}
        d_page = d_src_obj.get("source_page") or default_d_page
        d_sheet = d_src_obj.get("source_sheet") or (f"PAGE-{d_page}" if d_page else "N/A")
        d_title = d_src_obj.get("source_title") or "Section & Detail Drawing"
        d_dtype = d_src_obj.get("source_page_type") or "Wall Section"
        d_desc = d_src_obj.get("description") or d_src_obj.get("details") or f"Measured {dim_label} from section drawings."

        # Build structured measurement sources list for UI explanation
        measurement_sources = []
        if requires_perimeter and (p_ft is not None or (perimeter_data and perimeter_data.get("value_m"))):
            p_calc = perimeter_data.get("calculation", "") if perimeter_data else ""
            p_details = perimeter_data.get("details", "") if perimeter_data else ""
            exp_str = p_details or p_calc or "Measured building footprint dimensions from plan sheet."
            measurement_sources.append({
                "name": "Building Perimeter",
                "value": f"{p_ft} ft ({p_m} m)" if (p_ft and p_m) else (f"{p_m} m" if p_m else f"{p_ft} ft"),
                "source_page": p_page,
                "sheet_number": p_sheet,
                "sheet_title": p_title,
                "drawing_type": p_dtype,
                "explanation": f"{exp_str}"
            })
        if requires_depth and (d_ft is not None or (d_src_obj and d_src_obj.get("value_m"))):
            measurement_sources.append({
                "name": dim_label,
                "value": f"{d_ft} ft ({d_m} m)" if (d_ft and d_m) else (f"{d_m} m" if d_m else f"{d_ft} ft"),
                "source_page": d_page,
                "sheet_number": d_sheet,
                "sheet_title": d_title,
                "drawing_type": d_dtype,
                "explanation": f"{d_desc}"
            })

        # Helper to format area / linear quantities dynamically based on takeoff_unit
        is_french_unit = "pi" in str(takeoff_unit).lower() or "carrée" in str(takeoff_unit).lower() or "lin" in str(takeoff_unit).lower()

        # 1. Dynamic Area Takeoff Calculation (Perimeter × Material-Specific Vertical Dimension)
        if requires_perimeter and requires_depth and p_ft is not None:
            if d_ft is not None and d_ft > 0:
                eff_d_ft = d_ft
                eff_d_m = d_m if d_m is not None else round(eff_d_ft / 3.28084, 3)

                base_area_sqft = round(p_ft * eff_d_ft, 1)
                base_area_m2 = round(p_m * eff_d_m, 1) if (p_m and eff_d_m) else round(base_area_sqft / 10.764, 1)
                p2_total_sqft = round(base_area_sqft * (1.0 + waste_pct / 100.0))
                p2_total_m2 = round(base_area_m2 * (1.0 + waste_pct / 100.0), 1)

                unit_area_label = "Pi Carrée" if is_french_unit else "sq.ft"
                info_qty_str = f"{base_area_sqft:,.0f} {unit_area_label} ({base_area_m2} m²)".replace(",", " ")
                total_qty_str = f"{p2_total_sqft:,.0f} {unit_area_label} ({p2_total_m2} m²)".replace(",", " ")

                takeoff_res["takeoff_quantity"] = info_qty_str
                takeoff_res["estimation_summary"] = {
                    "material_name": std_name,
                    "category": category,
                    "info_qty": info_qty_str,
                    "waste_percent": f"{waste_pct}%",
                    "total_measured_qty": total_qty_str,
                }
                p_src_info = f"Page {p_page} (Sheet {p_sheet}: {p_title})" if p_page else f"Sheet {p_sheet}: {p_title}"
                d_src_info = f"Page {d_page} (Sheet {d_sheet}: {d_title})" if d_page else (f"Sheet {p_sheet}: {p_title}" if p_page else "N/A")
                p_exp = perimeter_data.get('details') or perimeter_data.get('calculation') or "Measured building footprint dimensions" if perimeter_data else ""

                depth_line_str = f"2. {dim_label} = {eff_d_ft} ft ({eff_d_m} m)"

                # Build excluded-projection context for display
                excl_proj_display = ""
                if perimeter_data and perimeter_data.get("excluded_projections"):
                    items = perimeter_data["excluded_projections"]
                    excl_names = ", ".join(
                        f"{it.get('name', '?')} ({it.get('reason', 'excluded')})"
                        for it in (items if isinstance(items, list) else [])
                    )
                    if excl_names:
                        excl_proj_display = f"\n   • Excluded projections (outside thermal envelope): {excl_names}"
                elif perimeter_data and perimeter_data.get("excl_labels"):
                    excl_proj_display = f"\n   • Excluded dimension tiers (outer/porch/stair context): {perimeter_data['excl_labels']}"

                out_p = (perimeter_data or {}).get("outer_perimeter", {})
                has_distinct_outer = out_p and out_p.get("value_mm") and float(out_p["value_mm"]) > float(p_mm or 0)
                perim_label = "Building Perimeter (Inner Structural Wall — Thermal Envelope Boundary)" if has_distinct_outer else "Building Perimeter (Main Footprint Mass)"

                takeoff_res["calculation_formula"] = (
                    f"1. {perim_label} = {p_ft} ft ({p_m} m)\n"
                    f"   • Source: {p_src_info}\n"
                    f"   • How Measured: {p_exp}"
                    f"{excl_proj_display}\n\n"
                    f"{depth_line_str}\n"
                    f"   • Source: {d_src_info}\n"
                    f"   • How Measured: {d_desc}\n\n"
                    f"3. Measured Surface Area (Base Info Qty) = {p_ft} ft × {eff_d_ft} ft = {base_area_sqft:,.0f} {unit_area_label} ({base_area_m2} m²)\n\n"
                    f"4. Total Surface Area with {waste_pct}% Waste Addition = {base_area_sqft:,.0f} × (1 + {waste_pct}%) = {p2_total_sqft:,.0f} {unit_area_label} ({p2_total_m2} m²)"
                )
            else:
                # Wall depth is missing from document section drawings
                unit_lin_label = "Pi Lin" if is_french_unit else "lin.ft"
                info_qty_str = f"{p_ft:,.0f} {unit_lin_label} ({p_m} m) [Perimeter measured; {dim_label} missing on section drawings]".replace(",", " ")
                takeoff_res["takeoff_quantity"] = info_qty_str
                takeoff_res["estimation_summary"] = {
                    "material_name": std_name,
                    "category": category,
                    "info_qty": info_qty_str,
                    "waste_percent": f"{waste_pct}%",
                    "total_measured_qty": f"{p_ft:,.0f} {unit_lin_label} ({p_m} m)",
                    "warning": f"{dim_label} callout missing on PDF drawings. Provide section dimension to calculate surface area."
                }
                p_src_info = f"Page {p_page} (Sheet {p_sheet}: {p_title})" if p_page else f"Sheet {p_sheet}: {p_title}"
                p_exp = perimeter_data.get('details') or perimeter_data.get('calculation') or "Measured building footprint dimensions" if perimeter_data else ""
                takeoff_res["calculation_formula"] = (
                    f"1. Building Footprint Perimeter = {p_ft} ft ({p_m} m)\n"
                    f"   • Source: {p_src_info}\n"
                    f"   • How Measured: {p_exp}\n\n"
                    f"2. {dim_label}: MISSING from PDF section/detail drawings.\n"
                    f"   • Note: Surface area calculation (Perimeter × Vertical Dimension) requires explicit {dim_label} from section drawings."
                )
            if "cover" in str(takeoff_res.get("visual_evidence", "")).lower() or "no measurements" in str(takeoff_res.get("visual_evidence", "")).lower() or not takeoff_res.get("visual_evidence"):
                takeoff_res["visual_evidence"] = f"{std_name} applied to foundation assembly per drawing specifications."
        # 2. Dynamic Linear Takeoff Calculation (For linear items like DELTA-FLASH profile)
        elif requires_perimeter and p_ft is not None:
            base_len_ft = round(p_ft, 1)
            base_len_m = round(p_m, 1) if p_m else round(base_len_ft / 3.28084, 1)
            p2_total_ft = round(base_len_ft * (1.0 + waste_pct / 100.0))
            p2_total_m = round(base_len_m * (1.0 + waste_pct / 100.0), 1)

            unit_lin_label = "Pi Lin" if is_french_unit else "lin.ft"
            info_qty_str = f"{base_len_ft:,.0f} {unit_lin_label} ({base_len_m} m)".replace(",", " ")
            total_qty_str = f"{p2_total_ft:,.0f} {unit_lin_label} ({p2_total_m} m)".replace(",", " ")

            takeoff_res["takeoff_quantity"] = info_qty_str
            takeoff_res["estimation_summary"] = {
                "material_name": std_name,
                "category": category,
                "info_qty": info_qty_str,
                "waste_percent": f"{waste_pct}%",
                "total_measured_qty": total_qty_str,
            }
            p_src_info = f"Page {p_page} (Sheet {p_sheet}: {p_title})" if p_page else f"Sheet {p_sheet}: {p_title}"
            p_exp = perimeter_data.get('details') or perimeter_data.get('calculation') or "Measured building footprint dimensions" if perimeter_data else ""
            takeoff_res["calculation_formula"] = (
                f"1. Measured Perimeter Length (Base Info Qty) = {base_len_ft:,.0f} {unit_lin_label} ({base_len_m} m)\n"
                f"   • Source: {p_src_info}\n"
                f"   • How Measured: {p_exp}\n\n"
                f"2. Total Linear Length with {waste_pct}% Waste Addition = {base_len_ft:,.0f} × (1 + {waste_pct}%) = {p2_total_ft:,.0f} {unit_lin_label} ({p2_total_m} m)"
            )
        # 3. Fallback: Parse raw LLM takeoff_quantity ONLY IF dynamic calculation was not performed
        dynamic_calc_done = (requires_perimeter and p_ft is not None)
        raw_qty = str(takeoff_res.get("takeoff_quantity", "")).strip()
        if not dynamic_calc_done and raw_qty and not any(kw in raw_qty for kw in ["Pi Carrée", "Pi Lin", "sq.ft", "lin.ft", "sq ft"]):
            match_num = re.search(r"([\d\.\s,]+)", raw_qty)
            if match_num:
                try:
                    num_val = float(match_num.group(1).replace(" ", "").replace(",", "."))
                    unit_lower = raw_qty.lower()
                    if any(u in unit_lower for u in ["m²", "m2", "sq.m", "sq m"]):
                        area_m2 = num_val
                        area_sqft = round(area_m2 * 10.7639, 1)
                        total_m2 = round(area_m2 * (1.0 + waste_pct / 100.0), 1)
                        total_sqft = round(area_sqft * (1.0 + waste_pct / 100.0))
                        info_str = f"{area_sqft:,.0f} Pi Carrée ({area_m2} m²)".replace(",", " ")
                        total_str = f"{total_sqft:,.0f} Pi Carrée ({total_m2} m²)".replace(",", " ")
                        takeoff_res["takeoff_quantity"] = info_str
                        takeoff_res["estimation_summary"] = {
                            "material_name": std_name,
                            "category": category,
                            "info_qty": info_str,
                            "waste_percent": f"{waste_pct}%",
                            "total_measured_qty": total_str,
                        }
                    elif any(u in unit_lower for u in ["sq.ft", "sqft", "ft²", "ft2"]):
                        area_sqft = num_val
                        area_m2 = round(area_sqft / 10.7639, 1)
                        total_sqft = round(area_sqft * (1.0 + waste_pct / 100.0))
                        total_m2 = round(area_m2 * (1.0 + waste_pct / 100.0), 1)
                        info_str = f"{area_sqft:,.0f} Pi Carrée ({area_m2} m²)".replace(",", " ")
                        total_str = f"{total_sqft:,.0f} Pi Carrée ({total_m2} m²)".replace(",", " ")
                        takeoff_res["takeoff_quantity"] = info_str
                        takeoff_res["estimation_summary"] = {
                            "material_name": std_name,
                            "category": category,
                            "info_qty": info_str,
                            "waste_percent": f"{waste_pct}%",
                            "total_measured_qty": total_str,
                        }
                    elif any(u in unit_lower for u in ["m", "meter", "mètre"]):
                        len_m = num_val
                        len_ft = round(len_m * 3.28084, 1)
                        total_m = round(len_m * (1.0 + waste_pct / 100.0), 1)
                        total_ft = round(len_ft * (1.0 + waste_pct / 100.0))
                        info_str = f"{len_ft:,.0f} Pi Lin ({len_m} m)".replace(",", " ")
                        total_str = f"{total_ft:,.0f} Pi Lin ({total_m} m)".replace(",", " ")
                        takeoff_res["takeoff_quantity"] = info_str
                        takeoff_res["estimation_summary"] = {
                            "material_name": std_name,
                            "category": category,
                            "info_qty": info_str,
                            "waste_percent": f"{waste_pct}%",
                            "total_measured_qty": total_str,
                        }
                    elif any(u in unit_lower for u in ["ft", "lin.ft", "linear feet"]):
                        len_ft = num_val
                        len_m = round(len_ft / 3.28084, 1)
                        total_ft = round(len_ft * (1.0 + waste_pct / 100.0))
                        total_m = round(len_m * (1.0 + waste_pct / 100.0), 1)
                        info_str = f"{len_ft:,.0f} Pi Lin ({len_m} m)".replace(",", " ")
                        total_str = f"{total_ft:,.0f} Pi Lin ({total_m} m)".replace(",", " ")
                        takeoff_res["takeoff_quantity"] = info_str
                        takeoff_res["estimation_summary"] = {
                            "material_name": std_name,
                            "category": category,
                            "info_qty": info_str,
                            "waste_percent": f"{waste_pct}%",
                            "total_measured_qty": total_str,
                        }
                except ValueError:
                    pass
        # Enrich measurements in the takeoff result with reasoning and page metadata
        if "measurements" in takeoff_res and isinstance(takeoff_res["measurements"], list):
            for m in takeoff_res["measurements"]:
                m["source_page"] = page_num or m.get("source_page", default_p_page)
                m["sheet_number"] = sheet_number or m.get("sheet_number", f"PAGE-{m.get('source_page')}")
                m["sheet_title"] = sheet_title or m.get("sheet_title", "Drawing")
                m["drawing_type"] = drawing_type or m.get("drawing_type", "Unknown")
                m["reasoning"] = m.get("reasoning") or m.get("context") or "Extracted from drawing page specifications."
        takeoff_res["measurement_sources"] = measurement_sources
        return takeoff_res
    def run_takeoff_for_material(
        self,
        pdf_path: str,
        scratch_dir: str,
        page_num: int,
        page_md: str,
        page_json: dict,
        full_json_data: dict,
        material_query: str,
        metadata_json_path: str = None,
    ) -> dict:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY is not configured in environment variables.")
        project_id = None
        if full_json_data and "pages" in full_json_data:
            for p in full_json_data.get("pages", []):
                meta = p.get("metadata", {})
                if meta and meta.get("projectID"):
                    project_id = str(meta.get("projectID")).strip()
                    break
        std_name, kb_desc, synonyms, takeoff_unit, common_contexts, properties, guidelines, requires_perimeter, requires_depth, kb_params = self._get_search_terms(material_query, project_id)
        target_dim_key = (kb_params or {}).get("vertical_dimension_target", "below_grade_depth" if requires_depth else "none")
        perimeter_data, depth_data = self._resolve_building_dimensions(
            api_key=api_key,
            pdf_path=pdf_path,
            scratch_dir=scratch_dir,
            full_json_data=full_json_data,
            metadata_json_path=metadata_json_path,
            requires_perimeter=requires_perimeter,
            requires_depth=requires_depth,
            target_dim_key=target_dim_key,
        )
        doc_sheets = []
        doc_measurements = []
        for p in full_json_data.get("pages", []):
            p_num = p.get("page_number")
            sheet_no = p.get("sheetNumber", f"PAGE-{p_num}")
            title = p.get("title", "Untitled")
            doc_sheets.append(f"Page {p_num} (Sheet {sheet_no}): {title}")
            for m in p.get("measurements", []):
                doc_measurements.append({
                    "page": p_num,
                    "sheet": sheet_no,
                    "element": m.get("associatedElement", "Unknown"),
                    "value": m.get("value"),
                    "unit": m.get("unit"),
                    "type": m.get("measurementType")
                })
        takeoff_unit = self._resolve_takeoff_unit(takeoff_unit, doc_measurements)
        if perimeter_data.get("value_mm"):
            p_mm = perimeter_data["value_mm"]
            p_m  = perimeter_data.get("value_m", round(p_mm / 1000, 3))
            p_ft = perimeter_data.get("value_ft", round(p_m * 3.28084, 2))
            perimeter_context = f"Building Perimeter: {p_mm} mm ({p_m} m / {p_ft} ft)."
        else:
            perimeter_context = "NOT RESOLVED."
        if depth_data.get("value_mm"):
            d_mm = depth_data["value_mm"]
            d_m  = depth_data.get("value_m", round(d_mm / 1000, 3))
            d_ft = depth_data.get("value_ft", round(d_m * 3.28084, 2))
            depth_context = f"Foundation Wall Depth: {d_mm} mm ({d_m} m / {d_ft} ft)."
        else:
            depth_context = "NOT RESOLVED."

        # Read page markdown text for local context and fast-path matching
        page_md_text = ""
        if scratch_dir and page_num:
            page_md_path = os.path.join(scratch_dir, f"page_{page_num}.md")
            if os.path.exists(page_md_path):
                try:
                    with open(page_md_path, "r", encoding="utf-8") as f:
                        page_md_text = f.read().strip()
                except Exception:
                    page_md_text = ""

        # FAST PATH: When perimeter (and depth if required) are already verified from the blueprints,
        # generate the verified mathematical takeoff directly with zero LLM latency (< 0.1s).
        has_verified_dims = (
            requires_perimeter
            and perimeter_data.get("value_mm") is not None
            and (not requires_depth or (depth_data and depth_data.get("value_mm") is not None))
        )
        if has_verified_dims:
            evidence_match = ""
            if page_md_text:
                page_lower = page_md_text.lower()
                for s in synonyms:
                    if s in page_lower:
                        idx = page_lower.find(s)
                        start = max(0, idx - 40)
                        end = min(len(page_md_text), idx + 80)
                        evidence_match = page_md_text[start:end].strip().replace("\n", " ")
                        break
            if not evidence_match:
                evidence_match = f"{std_name} specified for foundation envelope per blueprint drawings."

            fast_takeoff_res = {
                "material": std_name,
                "sheet_reference": page_json.get("sheetNumber", f"PAGE-{page_num}"),
                "found_on_this_page": True,
                "takeoff_quantity": "",
                "measurements": [],
                "visual_evidence": evidence_match,
                "confidence": 0.98
            }
            return self._validate_and_correct_takeoff(
                takeoff_res=fast_takeoff_res,
                perimeter_data=perimeter_data,
                depth_data=depth_data,
                takeoff_unit=takeoff_unit,
                requires_perimeter=requires_perimeter,
                requires_depth=requires_depth,
                std_name=std_name,
                kb_params=kb_params,
                full_json_data=full_json_data,
                page_num=page_num,
                sheet_number=page_json.get("sheetNumber", f"PAGE-{page_num}"),
                sheet_title=page_json.get("title", "Untitled"),
                drawing_type=page_json.get("drawingType", "Unknown"),
            )
        guidelines_str = "\n".join(f"- {g}" for g in guidelines) if guidelines else "- Perform standard visual quantity takeoff."
        guidelines_str += "\n- For each measurement extracted, you MUST provide a precise 'reasoning' string explaining how you found/calculated that specific dimension on the drawing page, what elements it spans, and the logic used to identify it."
        sheet_type = page_json.get("drawingType", "Unknown") if page_json else "Unknown"
        sheet_title = page_json.get("title", "Untitled") if page_json else "Untitled"
        prompt = f"""You are an expert construction quantity takeoff estimator specializing in waterproofing and foundation detailing.

Analyze ONLY the single blueprint page image provided (Page {page_num} of drawing set). Target material:

MATERIAL: {std_name}
DESCRIPTION: {kb_desc}
SYNONYMS TO SEARCH FOR (case-insensitive, in call-outs, leaders, legends, and detail titles): {", ".join(synonyms)}
TAKEOFF UNIT: {takeoff_unit}
TYPICAL LOCATION IN DRAWINGS: {", ".join(common_contexts)}

REFERENCE DATA (context only — do NOT use as a substitute for visual confirmation):
- Building Perimeter: {perimeter_context} — this is the theoretical ceiling IF material runs the full perimeter, but you must confirm that on the drawings before using it. Never output this number as a quantity unless you can point to a callout or dimension string that supports it.
- Foundation Depth: {depth_context}
- This page's sheet type: {sheet_type} ({sheet_title}) — Section/Detail sheets are the most likely place to find a labeled callout with a dimension. Elevation sheets rarely show it; if this is an elevation page and nothing is labeled, say so plainly rather than guessing.

---

STEP-BY-STEP PROCESS:

1. SCAN the full page for any of the synonym terms above, in:
   - Detail callout bubbles / leaders
   - Material legends or schedules
   - Section-cut annotations
   - Dimension strings running along structural components

2. FOR EACH INSTANCE FOUND:
   a. Note its exact location on the page (e.g. "Detail 4/A-501, lower right quadrant" or "Section B-B, top of foundation wall, north side").
   b. Quote the exact label text as it appears on the drawing (do not paraphrase — this is for the user to visually re-locate it).
   c. Determine whether a dimension is given directly (a dimension string, a run length, a plan-view length) or must be inferred from a related dimension (e.g. a wall-segment length that the profile clearly runs along, per a section cut reference).
   d. If NO dimension is given or inferable on this page, still record the instance with value "not dimensioned on this page" rather than omitting it or guessing.

3. DO NOT extrapolate to the full building perimeter unless the drawing itself shows the profile running the full foundation length (e.g. a plan view with a continuous run annotation). A single detail section showing material in a typical/generic condition does NOT justify multiplying by the full perimeter — that detail may only apply to part of the building.

4. COMPUTE the linear footage or surface area ONLY from what is visually confirmed on THIS page. Show your arithmetic explicitly, including any unit conversion (mm/m to ft).

5. IF NOTHING on this page relates to {std_name}, return an empty measurements array and state that clearly — do not fabricate a value from the reference perimeter data just to fill the field.

---

CONFIDENCE CALIBRATION (apply per measurement):
- 0.85–1.0: Label text and a dimension are both directly visible and legible on this page.
- 0.5–0.84: Label is visible but the length is inferred from an adjacent dimension (e.g. wall length used as proxy) — explain the inference chain in "reasoning".
- 0.0–0.49: Material term appears (e.g. in a legend) but no usable dimension exists on this page.
- If no instance is found at all, omit measurements and set overall confidence to 0.0 with a note explaining the page was scanned and nothing was found.

---

RESPOND WITH VALID JSON ONLY matching this structure:
{{
  "material": "{std_name}",
  "sheet_reference": "PAGE-{page_num}",
  "found_on_this_page": true,
  "takeoff_quantity": "string (this page's contribution only, e.g. '12.4 lin.ft' or '0 lin.ft — not shown on this page')",
  "measurements": [
    {{
      "value": "string",
      "unit": "string",
      "type": "string (direct_dimension | inferred_from_adjacent | not_dimensioned)",
      "location_on_page": "string — where on sheet, e.g. 'Detail 4, lower-right'",
      "label_text_seen": "string — exact text quoted from drawing, or null",
      "context": "string",
      "reasoning": "string — full explanation of how this value was derived, including what it spans and any inference logic"
    }}
  ],
  "calculation_formula": "string — explicit arithmetic, or 'N/A — no dimension found'",
  "visual_evidence": "string — plain description of exactly what you saw so user can locate it on the page",
  "aggregation_note": "string — explicit statement that this is a PER-PAGE result and must be summed with other pages' results, NOT treated as the whole-building total",
  "confidence": 0.95
}}"""
        # Inject the page's extracted markdown text alongside the image for richer textual context
        page_md_text = ""
        if scratch_dir and page_num:
            page_md_path = os.path.join(scratch_dir, f"page_{page_num}.md")
            if os.path.exists(page_md_path):
                try:
                    with open(page_md_path, "r", encoding="utf-8") as f:
                        page_md_text = f.read().strip()
                except Exception:
                    page_md_text = ""
        if page_md_text:
            prompt_with_md = prompt + f"\n\n---\n\nEXTRACTED TEXT FROM THIS PAGE (PyMuPDF OCR — use to cross-check labels and dimensions visible in the image above):\n```\n{page_md_text[:6000]}\n```"
        else:
            prompt_with_md = prompt

        model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        print(f"[TakeoffService] Prompt: {prompt_with_md[:500]}...")
        img_data, img_mime = self._render_page_image(pdf_path, scratch_dir, page_num)
        payload = {
            "contents": [{"parts": [
                {"text": prompt_with_md},
                {"inlineData": {"mimeType": img_mime, "data": img_data}}
            ]}],
            "generationConfig": {"responseMimeType": "application/json"}
        }
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as response:
                res_body = response.read().decode("utf-8")
                res_json = json.loads(res_body)
                content = res_json["candidates"][0]["content"]["parts"][0]["text"]
                takeoff_res = self._robust_json_loads(content)
                print(f"[TakeoffService] Raw Takeoff Response JSON:\n{json.dumps(takeoff_res, indent=2, ensure_ascii=False)}")
                return self._validate_and_correct_takeoff(
                    takeoff_res=takeoff_res,
                    perimeter_data=perimeter_data,
                    depth_data=depth_data,
                    takeoff_unit=takeoff_unit,
                    requires_perimeter=requires_perimeter,
                    requires_depth=requires_depth,
                    std_name=std_name,
                    kb_params=kb_params,
                    full_json_data=full_json_data,
                    page_num=page_num,
                    sheet_number=page_json.get("sheetNumber", f"PAGE-{page_num}"),
                    sheet_title=page_json.get("title", "Untitled"),
                    drawing_type=page_json.get("drawingType", "Unknown"),
                )
            
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8")
            print(f"[TakeoffService] Gemini API HTTP Error {e.code}: {e.reason}\nBody: {err_body}")
            raise RuntimeError(f"Gemini API returned error {e.code}: {e.reason}")
        except Exception as e:
            print(f"[TakeoffService] Gemini API Call failed: {e}")
            raise RuntimeError(f"Takeoff extraction failed: {e}")