import unittest
import os
import json
from services.takeoff_service import TakeoffService

class TestGenericTakeoff(unittest.TestCase):
    def test_identify_material_empty_description(self):
        service = TakeoffService()
        res = service.identify_material_from_description("")
        self.assertIn("material", res)
        self.assertIn(res.get("source"), ["kb_default", "no_description"])
        if service.kb:
            first_key = list(service.kb.keys())[0]
            self.assertEqual(res.get("material"), service.kb[first_key].get("standard_name", first_key))

    def test_identify_material_arbitrary_query(self):
        service = TakeoffService()
        res = service.identify_material_from_description("Hydrofuge foundation damp-proofing membrane")
        self.assertTrue(res.get("matched"))
        self.assertIn("material", res)

    def test_resolve_building_dimensions_generic(self):
        service = TakeoffService()
        sample_json = {
            "pages": [
                {
                    "page_number": 1,
                    "sheetNumber": "A-101",
                    "title": "Ground Floor Plan",
                    "drawingType": "Floor Plan",
                    "measurements": [
                        {"value": "15000", "unit": "mm", "associatedElement": "building length"},
                        {"value": "10000", "unit": "mm", "associatedElement": "building width"}
                    ]
                },
                {
                    "page_number": 2,
                    "sheetNumber": "A-501",
                    "title": "Wall Section",
                    "drawingType": "Wall Section",
                    "measurements": [
                        {"value": "2000", "unit": "mm", "associatedElement": "foundation wall height"},
                        {"value": "300", "unit": "mm", "associatedElement": "concrete footing heel"}
                    ]
                }
            ]
        }

        perim, depth = service.resolve_document_dimensions(sample_json)
        self.assertEqual(perim.get("value_mm"), 50000)
        self.assertEqual(perim.get("value_m"), 50.0)
        self.assertEqual(perim.get("source_page"), 1)
        self.assertIn("A-101", perim.get("source_sheet"))

        self.assertEqual(depth.get("value_mm"), 2000)
        self.assertEqual(depth.get("value_m"), 2.0)
        self.assertEqual(depth.get("source_page"), 2)
        self.assertIn("A-501", depth.get("source_sheet"))

    def test_validate_and_correct_takeoff_formatting(self):
        service = TakeoffService()
        perim = {
            "value_mm": 50000, "value_m": 50.0, "value_ft": 164.04,
            "details": "Length = 15000 mm, Width = 10000 mm",
            "source_page": 1, "source_sheet": "A-101", "source_title": "Ground Floor Plan", "source_page_type": "Floor Plan"
        }
        depth = {
            "value_mm": 2000, "value_m": 2.0, "value_ft": 6.56,
            "description": "Foundation Wall Height = 2000 mm (6.56 ft)",
            "source_page": 2, "source_sheet": "A-501", "source_title": "Wall Section", "source_page_type": "Wall Section"
        }

        raw_res = {"material": "Drainage Membrane", "measurements": []}
        corrected = service._validate_and_correct_takeoff(
            takeoff_res=raw_res,
            perimeter_data=perim,
            depth_data=depth,
            takeoff_unit="Pi Carrée (sq.ft) / m²",
            requires_perimeter=True,
            requires_depth=True,
            std_name="Drainage Membrane",
            kb_params={"category": "Foundation Waterproofing", "default_waste_percent": 10}
        )

        self.assertIn("estimation_summary", corrected)
        self.assertIn("calculation_formula", corrected)
        self.assertIn("measurement_sources", corrected)
        self.assertEqual(len(corrected["measurement_sources"]), 2)
        self.assertEqual(corrected["measurement_sources"][0]["sheet_number"], "A-101")
        self.assertEqual(corrected["measurement_sources"][1]["sheet_number"], "A-501")

    def test_inner_wall_perimeter_and_depth_callout_breakdown(self):
        service = TakeoffService()
        sample_json = {
            "pages": [
                {
                    "page_number": 1,
                    "sheetNumber": "A-101",
                    "title": "Ground Floor Plan",
                    "drawingType": "Floor Plan",
                    "measurements": [
                        {"value": "17151", "unit": "mm", "associatedElement": "roof overhang length"},
                        {"value": "15939", "unit": "mm", "associatedElement": "roof overhang width"},
                        {"value": "13513", "unit": "mm", "associatedElement": "inner foundation length"},
                        {"value": "12294", "unit": "mm", "associatedElement": "inner foundation width"}
                    ]
                },
                {
                    "page_number": 2,
                    "sheetNumber": "A-501",
                    "title": "Wall Section",
                    "drawingType": "Wall Section",
                    "measurements": [
                        {"value": "1737", "unit": "mm", "associatedElement": "poured concrete wall height"},
                        {"value": "254", "unit": "mm", "associatedElement": "concrete footing thickness"}
                    ]
                }
            ]
        }

        perim, depth = service.resolve_document_dimensions(sample_json)

        # Primary perimeter is the Inner Foundation Line: 2 * (13513 + 12294) = 51614 mm = 51.614 m
        self.assertEqual(perim.get("value_mm"), 51614)
        self.assertEqual(perim.get("value_m"), 51.614)
        self.assertEqual(perim.get("outer_perimeter").get("value_mm"), 66180)
        self.assertIn("13513", perim.get("details"))
        self.assertIn("12294", perim.get("details"))

        # Depth uses the structural foundation wall depth callout: 1737 mm = 1.737 m = 5.7 ft
        self.assertEqual(depth.get("value_mm"), 1737)
        self.assertEqual(depth.get("value_m"), 1.737)
        self.assertIn("1737", depth.get("description"))

    def test_foundation_wall_depth_modes(self):
        service = TakeoffService()
        # Mode 1: Total Wall Height minus Above-Grade Exposure
        sample_exposed = {
            "pages": [
                {
                    "page_number": 1,
                    "drawingType": "Section",
                    "measurements": [
                        {"value": "2400", "unit": "mm", "associatedElement": "hauteur du mur de fondation"},
                        {"value": "200", "unit": "mm", "associatedElement": "dégagement hors sol / exposure"}
                    ]
                }
            ]
        }
        _, depth = service.resolve_document_dimensions(sample_exposed)
        self.assertEqual(depth.get("value_mm"), 2200)

        # Mode 2: Direct Below-Grade callout
        sample_direct = {
            "pages": [
                {
                    "page_number": 1,
                    "drawingType": "Section",
                    "measurements": [
                        {"value": "1800", "unit": "mm", "associatedElement": "contact terre sous niveau du sol"}
                    ]
                }
            ]
        }
        _, depth2 = service.resolve_document_dimensions(sample_direct)
        self.assertEqual(depth2.get("value_mm"), 1800)

    def test_robust_json_parsing(self):
        service = TakeoffService()
        raw_json_str = '```json\n{"material": "Delta-MS", "takeoff_quantity": "1200 sq.ft"}\n```'
        parsed = service._robust_json_loads(raw_json_str)
        self.assertEqual(parsed.get("material"), "Delta-MS")
        self.assertEqual(parsed.get("takeoff_quantity"), "1200 sq.ft")

    def test_exclusion_classification_porch_slab(self):
        """Dimension callouts whose OCR context contains porch/slab/stair/post keywords
        must be classified as 'excluded' and NOT contribute to the inner perimeter."""
        service = TakeoffService()

        # Simulate context snippets for various excluded element types
        cases = [
            ("PERRON BÉTON 3658 mm CÔTÉ NORD", "excluded"),
            ("POTEAUX DECK 2438 mm", "excluded"),
            ("ESCALIER EXTÉRIEUR 1200 mm", "excluded"),
            ("DALLE SUR GRADE 3000 mm", "excluded"),
            ("DÉBORD DE TOIT 1524 mm", "outer"),    # Not excluded, but 'outer'
            ("MUR FONDATION BÉTON COULÉ 13513 mm", "inner"),  # Structural wall
            ("SOME UNKNOWN CONTEXT 9000 mm", "unknown"),
        ]
        for snippet, expected_cls in cases:
            cls = service._classify_dim_context(snippet)
            self.assertEqual(
                cls, expected_cls,
                f"Expected '{expected_cls}' for snippet: '{snippet}' but got '{cls}'"
            )

    def test_inner_outer_two_tier(self):
        """When only 2 distinct dimension tiers exist (no separate overhang chain),
        _select_inner_building_dimensions should return L_in==L_out and W_in==W_out."""
        service = TakeoffService()

        # Two tiers only — inner == outer, no exclusion
        page_dims = [
            (13513, "13513 mm", "unknown"),
            (13500, "13500 mm", "unknown"),  # near-duplicate of 13513, same tier
            (12294, "12294 mm", "unknown"),
            (12280, "12280 mm", "unknown"),  # near-duplicate of 12294, same tier
        ]
        result = service._select_inner_building_dimensions(page_dims)
        self.assertIsNotNone(result)
        # Only 2 distinct tiers → inner == outer
        self.assertEqual(result["L_in"], result["L_out"])
        self.assertEqual(result["W_in"], result["W_out"])
        self.assertEqual(result["excl"], [])  # Nothing excluded

    def test_exclusion_removes_porch_from_inner_perimeter(self):
        """When a dimension callout is labeled with PERRON context, it must be
        placed in the excluded tier so it does NOT affect the inner perimeter."""
        service = TakeoffService()

        # 4-tuple scenario: first 2 entries are porch (excluded), next 2 are structural
        page_dims = [
            (17151, "17151 mm", "excluded"),  # Perron outer extent — excluded
            (15939, "15939 mm", "excluded"),  # Perron width — excluded
            (13513, "13513 mm", "inner"),     # Structural wall length
            (12294, "12294 mm", "inner"),     # Structural wall width
        ]
        result = service._select_inner_building_dimensions(page_dims)
        self.assertIsNotNone(result)
        # Inner perimeter must be from the structural walls only
        self.assertAlmostEqual(result["L_in"], 13513, delta=500)
        self.assertAlmostEqual(result["W_in"], 12294, delta=500)
        # Excluded must contain the porch dimensions
        self.assertIn(17151, result["excl"])
        self.assertIn(15939, result["excl"])

    def test_validate_uses_inner_perimeter_when_present(self):
        """_validate_and_correct_takeoff must use inner_perimeter.value_mm for
        calculation, not the top-level value_mm (which could be the outer line)."""
        service = TakeoffService()

        # Simulate perimeter_data where top-level is outer (66180) but inner is 51614
        perim = {
            "value_mm": 66180,   # outer — should NOT be used
            "value_m": 66.180,
            "value_ft": 217.12,
            "inner_perimeter": {
                "value_mm": 51614,
                "value_m": 51.614,
                "value_ft": 169.34
            },
            "outer_perimeter": {"value_mm": 66180},
            "details": "Inner Wall Line: Length = 13513 mm, Width = 12294 mm",
            "source_page": 1, "source_sheet": "A-101",
            "source_title": "Ground Floor Plan", "source_page_type": "Floor Plan"
        }
        depth = {
            "value_mm": 1991, "value_m": 1.991, "value_ft": 6.53,
            "description": "Foundation Wall = 1737 mm + Footing = 254 mm",
            "source_page": 2, "source_sheet": "A-501",
            "source_title": "Wall Section", "source_page_type": "Wall Section"
        }

        raw_res = {"material": "Drainage Membrane", "measurements": []}
        corrected = service._validate_and_correct_takeoff(
            takeoff_res=raw_res,
            perimeter_data=perim,
            depth_data=depth,
            takeoff_unit="Pi Carrée (sq.ft) / m²",
            requires_perimeter=True,
            requires_depth=True,
            std_name="DELTA-MS",
            kb_params={"category": "Foundation Waterproofing", "default_waste_percent": 10}
        )

        formula = corrected.get("calculation_formula", "")
        # Must reference the INNER perimeter (169.34 ft from 51.614 m), NOT 217.12 ft
        self.assertIn("169", formula, "calculation_formula should use the inner perimeter value, not the outer")
        self.assertIn("Thermal Envelope", formula, "calculation_formula should label the inner line explicitly")

    def test_multi_unit_overall_footprint_selection(self):
        """For a multi-unit building with overall dimensions 74'-8" (22758 mm) x 26'-10" (8179 mm)
        and interior room partitions (21'-10" / 6655 mm and 20'-1" / 6121 mm),
        _select_inner_building_dimensions MUST select the overall building footprint (22758 x 8179)
        and NOT drop to interior room dimensions."""
        service = TakeoffService()

        page_dims = [
            (22758, "74'-8\"", "unknown"),    # Overall length (74'-8")
            (8179, "26'-10\"", "unknown"),    # Overall width (26'-10")
            (6655, "21'-10\"", "unknown"),    # Room/unit partition 1
            (6121, "20'-1\"", "unknown"),     # Room/unit partition 2
        ]
        result = service._select_inner_building_dimensions(page_dims)
        self.assertIsNotNone(result)
        # MUST select 22,758 mm x 8,179 mm (~61.8 m / ~203 ft perimeter)
        self.assertEqual(result["L_in"], 22758)
        self.assertEqual(result["W_in"], 8179)
        # Total perimeter = 2 * (22.758 + 8.179) = 61.874 m = ~203 ft (NOT 2 * (6.655 + 6.121) = 25.55 m = ~83.8 ft)
        perim_m = 2 * (result["L_in"] + result["W_in"]) / 1000.0
        self.assertAlmostEqual(perim_m, 61.874, delta=0.5)

    def test_reject_interior_partition_dimensions(self):
        """When candidate dimensions include interior room partitions (6655 x 6121),
        _select_inner_building_dimensions MUST reject the room partitions and fall back
        to the overall building footprint mass (22758 x 8179)."""
        service = TakeoffService()

        page_dims = [
            (22758, "74'-8\"", "outer"),     # Overall length
            (8179, "26'-10\"", "outer"),     # Overall width
            (6655, "21'-10\"", "inner"),     # Interior room partition (< 65% of L_out)
            (6121, "20'-1\"", "inner"),      # Interior room partition (< 65% of W_out)
        ]
        result = service._select_inner_building_dimensions(page_dims)
        self.assertIsNotNone(result)
        # Must select overall building mass (22758 x 8179), NOT interior room partition (6655 x 6121)
        self.assertEqual(result["L_in"], 22758)
        self.assertEqual(result["W_in"], 8179)


    def test_single_footprint_perimeter_formatting(self):
        """When inner and outer perimeters are equal (no roof overhang chain),
        _validate_and_correct_takeoff MUST format calculation_formula as 'Building Perimeter (Main Footprint Mass)'
        without printing redundant duplicate outer envelope lines."""
        service = TakeoffService()

        perim = {
            "value_mm": 61874, "value_m": 61.874, "value_ft": 203.0,
            "inner_perimeter": {"value_mm": 61874, "value_m": 61.874, "value_ft": 203.0, "length_mm": 22758, "width_mm": 8179},
            "outer_perimeter": {"value_mm": 61874, "value_m": 61.874, "value_ft": 203.0, "length_mm": 22758, "width_mm": 8179},
            "details": "Building Footprint Mass: Length = 74'-8\" (22.758 m), Width = 26'-10\" (8.179 m) → Main Building Foundation Perimeter = 203.0 ft (61.874 m).",
            "source_page": 1, "source_sheet": "A-101", "source_title": "Ground Floor Plan", "source_page_type": "Floor Plan"
        }
        depth = {
            "value_mm": 1991, "value_m": 1.991, "value_ft": 6.53,
            "description": "Foundation Wall = 1737 mm + Footing = 254 mm",
            "source_page": 2, "source_sheet": "A-501", "source_title": "Wall Section", "source_page_type": "Wall Section"
        }

        raw_res = {"material": "Drainage Membrane", "measurements": []}
        corrected = service._validate_and_correct_takeoff(
            takeoff_res=raw_res,
            perimeter_data=perim,
            depth_data=depth,
            takeoff_unit="Pi Carrée (sq.ft) / m²",
            requires_perimeter=True,
            requires_depth=True,
            std_name="DELTA-MS",
            kb_params={"category": "Foundation Waterproofing", "default_waste_percent": 10}
        )

        formula = corrected.get("calculation_formula", "")
        self.assertIn("Main Footprint Mass", formula)
        self.assertNotIn("Thermal Envelope Boundary", formula)

    def test_reject_drawing_code_numbers(self):
        """Verifies that project/drawing codes like '60029-12' or '24353-01' are ignored
        and do not corrupt building footprint perimeter calculation."""
        service = TakeoffService()

        # Simulate text scan containing project code 60029-12 and actual callouts 17151, 15939, 13513, 12294
        sample_json = {
            "pages": [{
                "page_number": 8,
                "drawingType": "Floor Plan",
                "title": "Ground Floor Plan - Demolition",
                "sheetNumber": "A-601"
            }]
        }
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            md_path = os.path.join(tmp_dir, "page_8.md")
            with open(md_path, "w", encoding="utf-8") as f:
                f.write("60029-12 ET NOUVEAU\n±17151 mm\n±15939 mm\n±13513 mm\n±12294 mm\n")
            
            perim, depth = service._resolve_building_dimensions(
                api_key="", pdf_path="", scratch_dir=tmp_dir, full_json_data=sample_json, metadata_json_path="", requires_perimeter=True
            )
            self.assertIsNotNone(perim)
            # Inner Perimeter MUST be 13513 x 12294 (169.34 ft / 51.614 m), NOT 60029 x 17151 (506.43 ft / 154.36 m)
            self.assertAlmostEqual(perim["inner_perimeter"]["value_m"], 51.614, delta=0.1)
            self.assertAlmostEqual(perim["outer_perimeter"]["value_m"], 66.180, delta=0.1)

    def test_three_tier_proportional_dual_envelope(self):
        """When candidate tiers contain 17151, 15939, 13513 (where 12294 was in a non-top tier),
        _select_inner_building_dimensions MUST proportionally resolve Inner W = 12294 mm."""
        service = TakeoffService()

        page_dims = [
            (17151, "17151 mm", "unknown"),
            (15939, "15939 mm", "unknown"),
            (13513, "13513 mm", "unknown"),
            (7010, "7010 mm", "unknown"),
            (12294, "12294 mm", "unknown"),
        ]
        result = service._select_inner_building_dimensions(page_dims)
        self.assertIsNotNone(result)
        self.assertEqual(result["L_out"], 17151)
        self.assertEqual(result["W_out"], 15939)
        self.assertEqual(result["L_in"], 13513)
        self.assertEqual(result["W_in"], 12294)

    def test_separate_vertical_dimensions_extraction(self):
        """Verifies that resolve_document_dimensions returns distinct full_foundation_height,
        below_grade_depth, and geotextile_drain_girth dimensions."""
        service = TakeoffService()
        sample_json = {
            "pages": [
                {
                    "page_number": 1,
                    "sheetNumber": "A100",
                    "title": "Ground Floor Plan",
                    "drawingType": "Floor Plan",
                    "measurements": [
                        {"value": "15000", "unit": "mm", "associatedElement": "Inner building length"},
                        {"value": "10000", "unit": "mm", "associatedElement": "Inner building width"}
                    ]
                },
                {
                    "page_number": 2,
                    "sheetNumber": "A301",
                    "title": "Wall Section & Foundation Detail",
                    "drawingType": "Wall Section",
                    "measurements": [
                        {"value": "1737", "unit": "mm", "associatedElement": "Hauteur mur fondation béton"},
                        {"value": "254", "unit": "mm", "associatedElement": "Épaisseur semelle footing"},
                        {"value": "200", "unit": "mm", "associatedElement": "Dégagement hors sol / exposure"}
                    ]
                },
                {
                    "page_number": 3,
                    "sheetNumber": "A401",
                    "title": "French Drain Detail",
                    "drawingType": "Detail",
                    "measurements": [
                        {"value": "720", "unit": "mm", "associatedElement": "Enveloppe pierre concassée géotextile wrap"}
                    ]
                }
            ]
        }

        perim, depth = service.resolve_document_dimensions(sample_json)

        # 1. Perimeter: 2 * (15000 + 10000) = 50,000 mm = 50.0 m = 164.04 ft
        self.assertEqual(perim.get("value_mm"), 50000)
        self.assertEqual(perim.get("value_m"), 50.0)

        # 2. Below-Grade Depth: 1737 mm - 200 mm = 1537 mm = 1.537 m = 5.04 ft
        self.assertIsNotNone(depth.get("below_grade_depth"))
        self.assertEqual(depth["below_grade_depth"]["value_mm"], 1537)
        self.assertEqual(depth["below_grade_depth"]["value_m"], 1.537)

        # 3. Full Foundation Height: 1737 mm + 254 mm = 1991 mm = 1.991 m = 6.53 ft
        self.assertIsNotNone(depth.get("full_foundation_height"))
        self.assertEqual(depth["full_foundation_height"]["value_mm"], 1991)
        self.assertEqual(depth["full_foundation_height"]["value_m"], 1.991)

        # 4. French Drain Wrap Girth: 720 mm = 0.72 m = 2.36 ft
        self.assertIsNotNone(depth.get("geotextile_drain_girth"))
        self.assertEqual(depth["geotextile_drain_girth"]["value_mm"], 720)
        self.assertEqual(depth["geotextile_drain_girth"]["value_m"], 0.72)

    def test_elevation_datum_delta_subtraction(self):
        """Verifies dynamic calculation of below-grade depth and full wall height
        directly from architectural elevation datum benchmarks."""
        service = TakeoffService()
        sample_json = {
            "pages": [
                {
                    "page_number": 1,
                    "sheetNumber": "A100",
                    "drawingType": "Floor Plan",
                    "measurements": [
                        {"value": "50'-0\"", "unit": "ft", "associatedElement": "Building length"},
                        {"value": "30'-0\"", "unit": "ft", "associatedElement": "Building width"}
                    ]
                },
                {
                    "page_number": 2,
                    "sheetNumber": "A301",
                    "drawingType": "Elevation",
                    "measurements": [
                        {"value": "100'-0\"", "measurementType": "Elevation", "associatedElement": "EL. TOP OF WALL T.O.W."},
                        {"value": "99'-4\"", "measurementType": "Elevation", "associatedElement": "EL. SOL FINI GRADE NIVEAU"},
                        {"value": "94'-3\"", "measurementType": "Elevation", "associatedElement": "EL. TOP OF FOOTING SEMELLE"},
                        {"value": "93'-4\"", "measurementType": "Elevation", "associatedElement": "EL. BOTTOM OF FOOTING B.O.F."}
                    ]
                }
            ]
        }

        perim, depth = service.resolve_document_dimensions(sample_json)

        # Below-grade depth = 99'-4" - 94'-3" = 5'-1" = 5.08 ft (1549 mm)
        bg = depth.get("below_grade_depth")
        self.assertIsNotNone(bg)
        self.assertAlmostEqual(bg["value_ft"], 5.08, delta=0.05)

        # Full height = 100'-0" - 93'-4" = 6'-8" = 6.67 ft (2032 mm)
        fh = depth.get("full_foundation_height")
        self.assertIsNotNone(fh)
        self.assertAlmostEqual(fh["value_ft"], 6.67, delta=0.05)

    def test_material_specific_dimension_routing_and_calculations(self):
        """Verifies that each material calculates area/length using strictly its designated
        vertical dimension parameter without crossover."""
        service = TakeoffService()
        perim = {
            "value_mm": 50000, "value_m": 50.0, "value_ft": 164.04,
            "details": "Measured building footprint",
            "source_page": 1, "source_sheet": "A100", "source_title": "Floor Plan", "source_page_type": "Floor Plan"
        }
        depth_data = {
            "value_mm": 1537, "value_m": 1.537, "value_ft": 5.04,
            "full_foundation_height": {
                "value_mm": 1991, "value_m": 1.991, "value_ft": 6.53,
                "description": "Full Foundation Wall Height = 6.53 ft (1.991 m / 1991 mm)",
                "source_page": 2, "source_sheet": "A301", "source_title": "Wall Section", "source_page_type": "Wall Section"
            },
            "below_grade_depth": {
                "value_mm": 1537, "value_m": 1.537, "value_ft": 5.04,
                "description": "Below-Grade Wall Depth = 5.04 ft (1.537 m / 1537 mm)",
                "source_page": 2, "source_sheet": "A301", "source_title": "Wall Section", "source_page_type": "Wall Section"
            },
            "geotextile_drain_girth": {
                "value_mm": 720, "value_m": 0.72, "value_ft": 2.36,
                "description": "French Drain Wrap Girth = 2.36 ft (0.72 m / 720 mm)",
                "source_page": 3, "source_sheet": "A401", "source_title": "French Drain Detail", "source_page_type": "Detail"
            }
        }

        # 1. Bituminous Dampproofing / Hydrofuge (RC.30) -> uses full_foundation_height (6.53 ft)
        hydrofuge_res = service._validate_and_correct_takeoff(
            takeoff_res={"material": "Bituminous Damp-Proofing (RC.30)"},
            perimeter_data=perim,
            depth_data=depth_data,
            takeoff_unit="Pi Carrée (sq.ft) / m²",
            requires_perimeter=True,
            requires_depth=True,
            std_name="Bituminous Damp-Proofing (RC.30)",
            kb_params={"category": "Damp-Proofing", "vertical_dimension_target": "full_foundation_height", "default_waste_percent": 10}
        )
        # Base Area = 164.04 * 6.53 = ~1,071 sq.ft (99.6 m²), with 10% waste = ~1,178 sq.ft
        self.assertIn("Full Foundation Wall Height", hydrofuge_res["calculation_formula"])
        self.assertIn("6.53 ft", hydrofuge_res["calculation_formula"])
        self.assertIn("1 071", hydrofuge_res["takeoff_quantity"])

        # 2. Delta-MS Drainage Board -> uses below_grade_depth (5.04 ft)
        delta_ms_res = service._validate_and_correct_takeoff(
            takeoff_res={"material": "DELTA-MS Drainage Board"},
            perimeter_data=perim,
            depth_data=depth_data,
            takeoff_unit="Pi Carrée (sq.ft) / m²",
            requires_perimeter=True,
            requires_depth=True,
            std_name="DELTA-MS Drainage Board",
            kb_params={"category": "Drainage Board", "vertical_dimension_target": "below_grade_depth", "default_waste_percent": 10}
        )
        # Base Area = 164.04 * 5.04 = ~827 sq.ft (76.9 m²), with 10% waste = ~909 sq.ft
        self.assertIn("Below-Grade Foundation Wall Depth", delta_ms_res["calculation_formula"])
        self.assertIn("5.04 ft", delta_ms_res["calculation_formula"])
        self.assertIn("827", delta_ms_res["takeoff_quantity"])

        # 3. Delta-Flash -> uses perimeter linear length ONLY (164.04 ft + 5% waste)
        delta_flash_res = service._validate_and_correct_takeoff(
            takeoff_res={"material": "DELTA-FLASH Profile"},
            perimeter_data=perim,
            depth_data=depth_data,
            takeoff_unit="Pi Lin (lin.ft) / m",
            requires_perimeter=True,
            requires_depth=False,
            std_name="DELTA-FLASH Profile",
            kb_params={"category": "Flashing", "vertical_dimension_target": "none", "default_waste_percent": 5}
        )
        # Base Length = 164 lin.ft (50 m), with 5% waste = 172 lin.ft (52.5 m)
        self.assertIn("164 Pi Lin", delta_flash_res["takeoff_quantity"])
        self.assertIn("172 Pi Lin", delta_flash_res["estimation_summary"]["total_measured_qty"])
        self.assertNotIn("Height", delta_flash_res["calculation_formula"])
        self.assertNotIn("Depth", delta_flash_res["calculation_formula"])

        # 4. Geotextile Membrane (French Drain) -> uses geotextile_drain_girth (2.36 ft)
        geotextile_res = service._validate_and_correct_takeoff(
            takeoff_res={"material": "French Drain Geotextile Membrane"},
            perimeter_data=perim,
            depth_data=depth_data,
            takeoff_unit="Pi Carrée (sq.ft) / m²",
            requires_perimeter=True,
            requires_depth=True,
            std_name="French Drain Geotextile Membrane",
            kb_params={"category": "Drainage", "vertical_dimension_target": "geotextile_drain_girth", "default_waste_percent": 10}
        )
        # Base Area = 164.04 * 2.36 = ~387 sq.ft (36.0 m²), with 10% waste = ~426 sq.ft
        self.assertIn("French Drain Geotextile Wrap Girth", geotextile_res["calculation_formula"])
        self.assertIn("2.36 ft", geotextile_res["calculation_formula"])
        self.assertIn("387", geotextile_res["takeoff_quantity"])

    def test_zero_hardcoding_dynamic_calculations(self):
        """Verifies that arbitrary variable inputs produce exact dynamic results
        without any hardcoded defaults or static magic numbers."""
        service = TakeoffService()
        custom_p_ft = 225.5
        custom_p_m = round(custom_p_ft / 3.28084, 3)
        custom_depth_ft = 8.75
        custom_depth_m = round(custom_depth_ft / 3.28084, 3)
        waste = 12

        perim = {"value_ft": custom_p_ft, "value_m": custom_p_m, "value_mm": int(custom_p_m * 1000)}
        depth = {
            "value_ft": custom_depth_ft, "value_m": custom_depth_m, "value_mm": int(custom_depth_m * 1000),
            "below_grade_depth": {"value_ft": custom_depth_ft, "value_m": custom_depth_m, "value_mm": int(custom_depth_m * 1000)}
        }

        res = service._validate_and_correct_takeoff(
            takeoff_res={"material": "Custom Material"},
            perimeter_data=perim,
            depth_data=depth,
            takeoff_unit="sq.ft / m²",
            requires_perimeter=True,
            requires_depth=True,
            std_name="Custom Material",
            kb_params={"category": "Custom", "vertical_dimension_target": "below_grade_depth", "default_waste_percent": waste}
        )

        expected_base_sqft = round(custom_p_ft * custom_depth_ft, 1)
        expected_total_sqft = round(expected_base_sqft * (1.0 + waste / 100.0))

        base_str = f"{expected_base_sqft:,.0f}".replace(",", " ")
        total_str = f"{expected_total_sqft:,.0f}".replace(",", " ")

        self.assertIn(f"{base_str} sq.ft", res["takeoff_quantity"])
        self.assertIn(f"{total_str} sq.ft", res["estimation_summary"]["total_measured_qty"])

if __name__ == "__main__":
    unittest.main()





