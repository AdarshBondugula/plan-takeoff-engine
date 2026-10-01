import unittest
from unittest.mock import MagicMock, patch
import os
import json


from PIL import Image

from services.markdown_cleaner import MarkdownCleaner
from services.pdf_extractor import PDFExtractor
from services.engineering_extractor import EngineeringExtractor
from services.converter_service import ConverterService

class TestPipelineStages(unittest.TestCase):
    def setUp(self):
        self.cleaner = MarkdownCleaner(boundary_ratio=0.1, min_page_occurrences=0.5)

    def test_markdown_cleaner_whitespace(self):
        raw_text = "Hello    World!   \n\n\n\nNew    Paragraph.  \n"
        expected = "Hello World!\n\nNew Paragraph."
        cleaned = self.cleaner.clean_whitespace(raw_text)
        self.assertEqual(cleaned, expected)

    def test_markdown_cleaner_headers_footers(self):
        # Setup page data mimicking repeated header and page numbers
        pages = [
            {
                "page_number": 1,
                "height": 100.0,
                "raw_markdown": "Header Title\nContent for page 1\nPage 1 of 2",
                "text_blocks": [
                    {"bbox": [5, 10, 15, 90], "text": "Header Title"}, # header
                    {"bbox": [40, 10, 50, 90], "text": "Content for page 1"},
                    {"bbox": [92, 10, 98, 90], "text": "Page 1 of 2"}  # footer
                ]
            },
            {
                "page_number": 2,
                "height": 100.0,
                "raw_markdown": "Header Title\nContent for page 2\nPage 2 of 2",
                "text_blocks": [
                    {"bbox": [5, 10, 15, 90], "text": "Header Title"}, # header
                    {"bbox": [40, 10, 50, 90], "text": "Content for page 2"},
                    {"bbox": [92, 10, 98, 90], "text": "Page 2 of 2"}  # footer
                ]
            }
        ]

        cleaned_pages = self.cleaner.clean_pages(pages)
        
        # Header "Header Title" and footer "Page X of 2" should be removed
        for page in cleaned_pages:
            cleaned_md = page["cleaned_markdown"]
            self.assertNotIn("Header Title", cleaned_md)
            self.assertNotIn("Page 1 of 2", cleaned_md)
            self.assertNotIn("Page 2 of 2", cleaned_md)
            
            # The coordinate blocks should also be cleaned
            texts = [b["text"] for b in page["text_blocks"]]
            self.assertNotIn("Header Title", texts)
            self.assertNotIn("Page 1 of 2", texts)
            self.assertNotIn("Page 2 of 2", texts)



    @patch('urllib.request.urlopen')
    def test_extract_metadata_from_markdown_mock(self, mock_urlopen):
        mock_response = MagicMock()
        mock_json_content = json.dumps({
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "document_name": "test.pdf",
                        "model_used": "gemini-3.5-flash",
                        "pages": [
                            {
                                "page_number": 1,
                                "sheetNumber": "A-001",
                                "title": "Front Page",
                                "discipline": "Architectural",
                                "drawingType": "Index",
                                "phase": "Existing",
                                "scale": "N/A",
                                "metadata": {
                                    "projectName": "Test Project"
                                },
                                "revisions": [
                                    {
                                        "revisionNumber": "1",
                                        "description": "EMIS POUR SOUMISSION",
                                        "date": "2025-05-16"
                                    }
                                ],
                                "compliance": {
                                    "originalConstructionYear": "1876",
                                    "interventionType": "Transformation/rénovation",
                                    "isRegulated": True,
                                    "codeArticles": ["10.1.1.2"]
                                },
                                "generalNotes": ["Note 1"],
                                "measurements": [
                                    {
                                        "value": "100",
                                        "unit": "m",
                                        "measurementType": "linear",
                                        "associatedElement": "wall"
                                    }
                                ],
                                "elements": [
                                    {
                                        "name": "column",
                                        "type": "Column",
                                        "status": "Preserve",
                                        "description": "existing column"
                                    }
                                ],
                                "instructions": [
                                    {
                                        "action": "Preserve",
                                        "element": "column"
                                    }
                                ]
                            }
                        ]
                    })
                }
            }]
        })
        mock_response.read.return_value = mock_json_content.encode('utf-8')
        mock_urlopen.return_value.__enter__.return_value = mock_response

        extractor = EngineeringExtractor()
        result = extractor.extract_metadata_from_markdown(
            api_key="mock_key",
            model="gemini-3.5-flash",
            doc_name="test.pdf",
            markdown_content="# PAGE 1\n---\nSome text"
        )
        
        self.assertEqual(result["document_name"], "test.pdf")
        self.assertEqual(result["model_used"], "gemini-3.5-flash")
        self.assertEqual(len(result["pages"]), 1)
        self.assertEqual(result["pages"][0]["sheetNumber"], "A-001")
        self.assertEqual(result["pages"][0]["measurements"][0]["value"], "100")
        self.assertEqual(result["pages"][0]["elements"][0]["name"], "column")
        self.assertEqual(result["pages"][0]["revisions"][0]["revisionNumber"], "1")
        self.assertEqual(result["pages"][0]["compliance"]["originalConstructionYear"], "1876")

    def test_generate_enriched_markdown(self):
        service = ConverterService()
        page_entry = {
            "page_number": 1,
            "sheetNumber": "A-101",
            "title": "SITE PLAN",
            "discipline": "Architectural",
            "drawingType": "Site Plan",
            "phase": "Renovation",
            "scale": "1:125",
            "metadata": {
                "projectName": "Test Project",
                "location": "123 Main St",
                "architect": "Test Architect",
                "projectID": "9999"
            },
            "revisions": [
                {
                    "revisionNumber": "1",
                    "description": "EMIS POUR SOUMISSION",
                    "date": "2025-05-16"
                }
            ],
            "compliance": {
                "originalConstructionYear": "1876",
                "interventionType": "Transformation/rénovation",
                "isRegulated": True,
                "codeArticles": ["10.1.1.2"]
            },
            "generalNotes": ["Note A", "Note B"],
            "measurements": [
                {
                    "value": "1816",
                    "unit": "mm",
                    "measurementType": "Distance",
                    "associatedElement": "Front slab width"
                }
            ],
            "elements": [
                {
                    "name": "Dalle",
                    "type": "Slab",
                    "status": "Demolish",
                    "description": "Existante"
                }
            ],
            "instructions": [
                {
                    "action": "Remove",
                    "element": "Socle de béton",
                    "originalWording": "Remove existing concrete slab"
                }
            ]
        }
        original_md = "Raw layout text content"
        enriched = service._generate_enriched_markdown(page_entry, original_md)
        
        self.assertIn("Raw layout text content", enriched)
        self.assertIn("### Page 1 Structured Engineering Details", enriched)
        self.assertIn("- **Sheet Number:** A-101", enriched)
        self.assertIn("- **Title:** SITE PLAN", enriched)
        self.assertIn("- **Project Name:** Test Project", enriched)
        self.assertIn("#### Code Compliance (Part 10)", enriched)
        self.assertIn("- **Original Construction Year:** 1876", enriched)
        self.assertIn("- **Intervention Type:** Transformation/rénovation", enriched)
        self.assertIn("- **Is Regulated:** True", enriched)
        self.assertIn("- **Code Articles:** 10.1.1.2", enriched)
        self.assertIn("#### Sheet Revisions", enriched)
        self.assertIn("| 1 | EMIS POUR SOUMISSION | 2025-05-16 |", enriched)
        self.assertIn("- Note A", enriched)
        self.assertIn("| 1816 | mm | Distance | Front slab width |", enriched)
        self.assertIn("| Dalle | Slab | Demolish | Existante |", enriched)
        self.assertIn("- **Remove** Socle de béton (Original: *Remove existing concrete slab*)", enriched)

    def test_validate_and_correct_takeoff(self):
        from services.takeoff_service import TakeoffService
        service = TakeoffService()

        # Case 1: Metric area calculation with incorrect LLM outputs
        raw_takeoff = {
            "material": "Membrane de drainage DELTA-MS",
            "takeoff_quantity": "49.77 m²",
            "measurements": [
                {
                    "value": "48.524",
                    "unit": "m",
                    "type": "length",
                    "context": "Building Perimeter"
                },
                {
                    "value": "1.07",
                    "unit": "m",
                    "type": "height",
                    "context": "Foundation Wall Depth"
                }
            ],
            "calculation_formula": "46.524 * 1.07 = 49.77068 m²",
            "visual_evidence": "Detail 1 on A-501",
            "confidence": 0.9
        }

        perimeter_data = {
            "value_mm": 46524,
            "value_m": 46.524,
            "value_ft": 152.64
        }
        depth_data = {
            "value_mm": 1070,
            "value_m": 1.07,
            "value_ft": 3.51
        }

        corrected = service._validate_and_correct_takeoff(
            takeoff_res=raw_takeoff,
            perimeter_data=perimeter_data,
            depth_data=depth_data,
            takeoff_unit="m²",
            requires_perimeter=True,
            requires_depth=True
        )

        self.assertIn("49.8 m²", corrected["takeoff_quantity"])
        self.assertIn("Building Perimeter", corrected["calculation_formula"])
        self.assertIn("Foundation Wall Depth", corrected["calculation_formula"])
        self.assertTrue(len(corrected["measurement_sources"]) >= 2)

    def test_resolve_document_dimensions(self):
        from services.takeoff_service import TakeoffService
        service = TakeoffService()
        
        mock_json_data = {
            "pages": [
                {
                    "page_number": 2,
                    "sheetNumber": "A-101",
                    "title": "Floor Plan",
                    "drawingType": "Floor Plan",
                    "measurements": [
                        {
                            "value": "15000",
                            "unit": "mm",
                            "measurementType": "Distance",
                            "associatedElement": "Largeur hors-tout du bâtiment"
                        },
                        {
                            "value": "10000",
                            "unit": "mm",
                            "measurementType": "Distance",
                            "associatedElement": "Longueur hors-tout du bâtiment"
                        }
                    ]
                },
                {
                    "page_number": 12,
                    "sheetNumber": "A-501",
                    "title": "Wall Section Detail",
                    "drawingType": "Section",
                    "measurements": [
                        {
                            "value": "1500",
                            "unit": "mm",
                            "measurementType": "Height",
                            "associatedElement": "Hauteur fondation sous le niveau du sol"
                        },
                        {
                            "value": "300",
                            "unit": "mm",
                            "measurementType": "Height",
                            "associatedElement": "Semelle footing de fondation"
                        }
                    ]
                }
            ]
        }
        
        perimeter_data, depth_data = service.resolve_document_dimensions(mock_json_data)
        
        self.assertEqual(perimeter_data["value_mm"], 50000)
        self.assertEqual(perimeter_data["value_m"], 50.0)
        self.assertEqual(perimeter_data["source_page"], 2)
        
        self.assertEqual(depth_data["value_mm"], 1500)
        self.assertEqual(depth_data["value_m"], 1.5)
        self.assertEqual(depth_data["source_page"], 12)

if __name__ == '__main__':
    unittest.main()

