import os
import json
import base64
import time
import urllib.request
import urllib.error
from PIL import Image
from io import BytesIO


class EngineeringExtractor:
    """
    Calls OpenRouter (OpenAI-compatible) API for:
      1. Per-page engineering detail extraction (image + MD → structured JSON)
      2. Document-level metadata extraction (combined MD → structured JSON)
      3. Vision transcription of sparse/drawing pages (image → Markdown)
    """

    OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self):
        pass

    def _robust_json_loads(self, text: str) -> dict:
        import re
        text_strip = text.strip()
        
        # Try direct parse first
        try:
            return json.loads(text_strip)
        except Exception:
            pass
            
        # Strip markdown fences if present
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

        # Try locating matching braces
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
        
        # Fallback to standard loads which will throw the original error
        return json.loads(text_strip)

    # -----------------------------------------------------------------------
    # Shared HTTP helper
    # -----------------------------------------------------------------------

    def _call_gemini_direct(self, prompt: str, require_json: bool = True) -> str:
        gemini_key = os.getenv("GEMINI_API_KEY")
        if not gemini_key:
            raise ValueError("GEMINI_API_KEY is not configured in environment variables.")
        model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}]
        }
        if require_json:
            payload["generationConfig"] = {"responseMimeType": "application/json"}

        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=180) as response:
            res_json = json.loads(response.read().decode("utf-8"))
            return res_json["candidates"][0]["content"]["parts"][0]["text"].strip()

    def _call_openrouter(
        self,
        api_key: str,
        model: str,
        messages: list,
        max_tokens: int = 2000,
        require_json: bool = False,
        context: str = "OpenRouter",
    ) -> str:
        """
        Makes an OpenRouter chat-completions call and returns the raw text response.
        Falls back automatically to direct Gemini API if OpenRouter encounters credit/quota errors (402).
        """
        if not api_key:
            # Fallback to direct Gemini API if no OpenRouter key
            prompt_text = "\n\n".join(m.get("content", "") for m in messages if m.get("content"))
            return self._call_gemini_direct(prompt_text, require_json=require_json)

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if require_json:
            payload["response_format"] = {"type": "json_object"}

        req = urllib.request.Request(
            self.OPENROUTER_BASE_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": os.getenv("APP_BASE_URL", "http://localhost:8000"),
                "X-Title": os.getenv("APP_NAME", "AI Takeoff Pro"),
            },
            method="POST",
        )

        max_retries = 3
        last_error = None
        for attempt in range(max_retries):
            try:
                with urllib.request.urlopen(req, timeout=180) as response:
                    res_body = response.read().decode("utf-8")
                    res_json = json.loads(res_body)
                    text = res_json["choices"][0]["message"]["content"]
                    return text.strip()

            except urllib.error.HTTPError as e:
                error_body = ""
                try:
                    error_body = e.read().decode("utf-8")
                except Exception:
                    pass
                print(
                    f"[{context}] HTTPError (Attempt {attempt+1}/{max_retries}): "
                    f"{e.code} - {e.reason}\nBody: {error_body}"
                )
                last_error = e
                if e.code in [402, 401, 403]:
                    print(f"[{context}] OpenRouter payment/auth error ({e.code}) — falling back to direct Gemini API...")
                    prompt_text = "\n\n".join(m.get("content", "") for m in messages if m.get("content"))
                    return self._call_gemini_direct(prompt_text, require_json=require_json)
                elif e.code in [429, 503]:
                    sleep_time = 30 if e.code == 429 else 10
                    print(f"[{context}] Rate limit/server error — sleeping {sleep_time}s...")
                    time.sleep(sleep_time)
                    continue
                break

            except Exception as e:
                print(f"[{context}] GeneralError (Attempt {attempt+1}/{max_retries}): {e}")
                last_error = e
                time.sleep(3)
                continue

        # Universal fallback to direct Gemini API if all retries fail
        try:
            print(f"[{context}] All OpenRouter retries failed. Attempting direct Gemini API fallback...")
            prompt_text = "\n\n".join(m.get("content", "") for m in messages if m.get("content"))
            return self._call_gemini_direct(prompt_text, require_json=require_json)
        except Exception as fallback_err:
            raise RuntimeError(f"[{context}] OpenRouter and direct Gemini fallback both failed. Last error: {fallback_err}")

    # -----------------------------------------------------------------------
    # 2. Document-level metadata extraction from combined Markdown
    # -----------------------------------------------------------------------

    def extract_metadata_from_markdown(
        self, api_key: str, model: str, doc_name: str, markdown_content: str
    ) -> dict:
        """
        Calls OpenRouter with the combined Markdown content of the full PDF
        to extract page-wise metadata, measurements, and element labels.
        """

        system_prompt = f"""\
You are an expert AEC (Architecture, Engineering and Construction) document parser.
Analyze the following Markdown content extracted from an architectural/construction PDF drawing set.
The source document is: {doc_name}
The model used is: {model}

For EACH page found in the Markdown (separated by "# PAGE N" headings), extract:
1. page_number (integer), sheetNumber (e.g. A-101), title (e.g. FLOOR PLAN)
2. discipline: one of Architectural, Structural, Mechanical, Electrical, Plumbing, Civil, Landscape, Mixed, Other, Unknown
3. drawingType: one of Floor Plan, Roof Plan, Elevation, Section, Detail, Schedule, Legend, Site Plan, Foundation Plan, Framing Plan, Ceiling Plan, Notes Sheet, Mixed, Other, Unknown
4. phase: one of Existing, Demolition, Renovation, Proposed, Construction, As Built, Unknown
5. scale (e.g. "1:75", "1:125", "N/A")
6. metadata: projectName, location, architect, projectID (from title block text)
7. revisions: list of drawing issues/revisions with revisionNumber, description, date (from emissions table)
8. compliance: regulatory details from Part 10 table (originalConstructionYear, interventionType, isRegulated, codeArticles)
9. generalNotes: list of general notes/descriptions
10. measurements: EXHAUSTIVE extraction of all dimension values printed on the plan. Scan for all numeric values (starting with ±, e.g. ±13463, ±1822, ±915, or units like 150mm) in the text. Treat every occurrence of a dimension independently. If multiple identical values appear in different locations, extract each one separately.
    CRITICAL: Associate dimensions with their respective elements using architectural and geometric logic:
    - Prioritize geometric connections: Match extension lines, dimension chains, and offsets to their physical elements using drafting priority logic.
    - Match individual segments in a dimension chain to their component elements, and overall chain totals to the full boundary limits.
    - Match spot elevations and thickness values to their correct vertical reference or material descriptors.
    - If the association cannot be verified with high confidence, set to "Unknown" rather than guessing.
11. elements: list of {{name, type (Wall/Roof/Window/Door/Column/Beam/Chimney/Vent/Pipe/Drain/Skylight/Slab/Gutter), status (Demolish/New/Preserve), description}}
12. instructions: list of {{action (Remove/Replace/Demolish/Preserve/Protect/Repair/Rebuild/Install/Inspect/Modify/Relocate/Unknown), element, originalWording}}

CRITICAL: Be exhaustive with dimensions. Scan the markdown carefully and do not omit any printed dimension numbers. Keep descriptions concise.

RESPOND WITH VALID JSON ONLY matching this structure:
{{
  "document_name": str,
  "model_used": str,
  "pages": [
    {{
      "page_number": int,
      "sheetNumber": str,
      "title": str,
      "discipline": str,
      "drawingType": str,
      "phase": str,
      "scale": str,
      "metadata": {{"projectName": str, "location": str, "architect": str, "projectID": str}},
      "revisions": [{{"revisionNumber": str, "description": str, "date": str}}],
      "compliance": {{"originalConstructionYear": str, "interventionType": str, "isRegulated": bool, "codeArticles": [str]}},
      "generalNotes": [str],
      "measurements": [{{"value": str, "unit": str, "measurementType": str, "associatedElement": str}}],
      "elements": [{{"name": str, "type": str, "status": str, "description": str}}],
      "instructions": [{{"action": str, "element": str, "originalWording": str}}]
    }}
  ]
}}"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Here is the complete extracted Markdown document:\n\n{markdown_content}"},
        ]

        try:
            raw = self._call_openrouter(
                api_key=api_key,
                model=model,
                messages=messages,
                max_tokens=4000,
                require_json=True,
                context="MetadataExtractor",
            )
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("```", 2)[-1] if text.startswith("```json") else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            return self._robust_json_loads(text)

        except Exception as e:
            print(f"[MetadataExtractor] Extraction failed: {e}")
            return {
                "document_name": doc_name,
                "model_used": model,
                "pages": [],
            }
