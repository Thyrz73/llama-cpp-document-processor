"""End-to-end extraction and selective OCR tests (run in the Docker image)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pymupdf
from app import extract_pdf


class TestExtraction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "example.pdf"

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def write_text_page(pdf, txt):
        page = pdf.new_page()
        page.insert_text((72, 80), txt, fontsize=16)
        return page

    @staticmethod
    def insert_scan_page(pdf, text):
        # Source with selectable text, rasterized into a flat image-only PDF page.
        source = pymupdf.open()
        scan_source = source.new_page()
        scan_source.insert_text((72, 80), text, fontsize=18)
        png = scan_source.get_pixmap(dpi=150, alpha=False).tobytes("png")
        page = pdf.new_page()
        page.insert_image(page.rect, stream=png)
        source.close()

    def test_searchable_pdf_skips_ocr(self):
        pdf = pymupdf.open()
        self.write_text_page(pdf, "This is a searchable document with real letters and words about geophysics.")
        pdf.save(self.file)
        pdf.close()
        with patch("app.ocr_page", side_effect=AssertionError("OCR should not run")):
            pages, count, missing, ocr = extract_pdf(self.file)
        self.assertEqual((count, ocr, missing), (1, [], []))
        self.assertIn("searchable document", pages[0]["text"])

    def test_image_pdf_uses_ocr(self):
        pdf = pymupdf.open()
        self.insert_scan_page(pdf, "Analyse du document scientifique. Donnees de terrain et resultats importants.")
        pdf.save(self.file)
        pdf.close()
        pages, count, missing, ocr = extract_pdf(self.file)
        self.assertEqual(count, 1)
        self.assertEqual(ocr, [1])
        self.assertEqual(missing, [])
        self.assertIn("document scientifique", pages[0]["text"].lower())

    def test_mixed_pdf_ocr_only_scan_page(self):
        pdf = pymupdf.open()
        self.write_text_page(pdf, "Measurements and measurements and observations are recorded here in text.")
        self.insert_scan_page(pdf, "Numerical model results are recorded here with a clear conclusion.")
        pdf.save(self.file)
        pdf.close()
        pages, count, missing, ocr = extract_pdf(self.file)
        self.assertEqual((count, missing, ocr), (2, [], [2]))
        self.assertEqual([p["page"] for p in pages], [1, 2])


if __name__ == "__main__":
    unittest.main()
