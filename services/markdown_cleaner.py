import re
from collections import Counter

class MarkdownCleaner:
    def __init__(self, boundary_ratio: float = 0.08, min_page_occurrences: float = 0.5):
        """
        boundary_ratio: The fraction of page height (from top and bottom) considered header/footer area.
        min_page_occurrences: The minimum fraction of pages a header/footer must appear in to be removed.
                              If pages count <= 2, it must appear in at least 2 pages.
        """
        self.boundary_ratio = boundary_ratio
        self.min_page_occurrences = min_page_occurrences

    def clean_pages(self, pages_data: list) -> list:
        """
        Cleans the markdown and removes repeated headers/footers from the page data.
        Modifies pages_data in-place and adds/updates "cleaned_markdown".
        """
        if not pages_data:
            return pages_data

        # 1. Identify repeated headers/footers
        headers_footers_to_remove = self._detect_headers_footers(pages_data)

        # 2. Process each page
        for page in pages_data:
            raw_md = page.get("raw_markdown", "")
            text_blocks = page.get("text_blocks", [])
            height = page.get("height", 842.0) # default A4 height in pt

            # Filter text blocks and remove those identified as headers/footers
            cleaned_blocks = []
            for block in text_blocks:
                bbox = block["bbox"] # [ymin, xmin, ymax, xmax]
                block_text = block["text"]
                
                # Check if block falls in header/footer area
                is_boundary = (bbox[0] < self.boundary_ratio * height) or (bbox[2] > (1 - self.boundary_ratio) * height)
                
                if is_boundary:
                    # Normalize text to check against detected headers/footers
                    normalized_text = self._normalize_for_matching(block_text)
                    if normalized_text in headers_footers_to_remove:
                        # Skip this block (removing from coordinate list)
                        continue
                
                cleaned_blocks.append(block)
            
            page["text_blocks"] = cleaned_blocks

            # Clean raw markdown by removing occurrences of the header/footer strings
            cleaned_md = raw_md
            for norm_hf in headers_footers_to_remove:
                # Find matching lines in markdown and remove them
                lines = cleaned_md.split("\n")
                new_lines = []
                for line in lines:
                    normalized_line = self._normalize_for_matching(line)
                    # If line matches the normalized header/footer, filter it out
                    if normalized_line == norm_hf or (norm_hf and norm_hf in normalized_line and len(line) < 150):
                        continue
                    new_lines.append(line)
                cleaned_md = "\n".join(new_lines)

            # Perform HTML tag cleaning
            cleaned_md = self.clean_html_tags(cleaned_md)

            # Perform whitespace cleaning
            cleaned_md = self.clean_whitespace(cleaned_md)
            page["cleaned_markdown"] = cleaned_md

        return pages_data

    def clean_html_tags(self, text: str) -> str:
        """
        Removes HTML tags from markdown text.
        Replaces break tags with newlines and strips comments and other tags.
        """
        if not text:
            return ""
        # Replace <br> and <br/> with newlines
        text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
        # Remove HTML comments
        text = re.sub(r'<!--[\s\S]*?-->', '', text)
        # Strip all other HTML tags (e.g. <div>, <span>, etc.)
        text = re.sub(r'<[a-zA-Z/][^>]*>', '', text)
        return text

    def clean_whitespace(self, text: str) -> str:
        """
        Cleans markdown text by removing duplicate spaces and excessive newlines.
        """
        if not text:
            return ""

        # Normalize line endings
        text = text.replace("\r\n", "\n")

        # Replace multiple spaces in a line with a single space
        text = re.sub(r'[ \t]+', ' ', text)

        # Remove trailing whitespace from each line
        text = "\n".join(line.rstrip() for line in text.split("\n"))

        # Replace three or more consecutive newlines with exactly two newlines (one blank line)
        text = re.sub(r'\n{3,}', '\n\n', text)

        # Clean leading/trailing whitespaces of the whole document
        return text.strip()

    def _normalize_for_matching(self, text: str) -> str:
        """
        Normalizes a string for header/footer comparison by stripping whitespace,
        converting to lowercase, and replacing numbers/page counters with '#' to match
        dynamic footers like 'Page 1 of 10' and 'Page 2 of 10'.

        To prevent unique sheet numbers (like A-401, A-501) from colliding and being
        falsely stripped, we do NOT replace digits with '#' for short, non-page strings.
        """
        if not text:
            return ""
        # Lowercase
        text = text.lower().strip()

        # Replace digits with '#' only if length is >= 10 OR it contains a page indicator keyword
        has_page_keyword = any(k in text for k in ["page", "pg", "sheet", "plan", "pl.", "of", "sur", "de", "/"])
        if len(text) >= 10 or has_page_keyword:
            text = re.sub(r'\d+', '#', text)

        # Collapse multiple whitespaces/punctuation
        text = re.sub(r'[\s\-_.,/|]+', ' ', text)
        return text.strip()


    def _detect_headers_footers(self, pages_data: list) -> set:
        """
        Scans boundary regions of all pages and detects text blocks that repeat
        across pages, returning a set of normalized strings to remove.
        """
        boundary_candidates = []
        total_pages = len(pages_data)
        
        for page in pages_data:
            height = page.get("height", 842.0)
            text_blocks = page.get("text_blocks", [])

            for block in text_blocks:
                bbox = block["bbox"] # [ymin, xmin, ymax, xmax]
                block_text = block["text"]

                # Check if block falls in header (top) or footer (bottom) area
                is_header = bbox[0] < self.boundary_ratio * height
                is_footer = bbox[2] > (1 - self.boundary_ratio) * height

                if is_header or is_footer:
                    norm = self._normalize_for_matching(block_text)
                    if norm: # ignore empty/whitespace blocks
                        boundary_candidates.append(norm)

        if not boundary_candidates:
            return set()

        # Count frequencies
        counts = Counter(boundary_candidates)
        
        # Decide threshold based on page count
        if total_pages <= 2:
            threshold = 2
        else:
            threshold = max(2, int(total_pages * self.min_page_occurrences))

        # Repeated elements are those that appear >= threshold times
        repeated = {text for text, count in counts.items() if count >= threshold}
        return repeated
