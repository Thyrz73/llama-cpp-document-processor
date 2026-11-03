"""Test PDF extraction with local Tesseract."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pymupdf
from app import extract_pdf

# CI installs Tesseract, so a missing binary there must fail instead of skipping.
requires_tesseract = unittest.skipUnless(
    shutil.which("tesseract") or os.getenv("CI"), "Tesseract is not installed locally")


class TestExtraction(unittest.TestCase):
    """Cover text PDFs, scans, and mixed documents."""

    def setUp(self):
        """Prepare temporary test resources."""
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "example.pdf"

    def tearDown(self):
        """Clean up temporary test files."""
        self.tmp.cleanup()

    @staticmethod
    def write_text_page(pdf, txt):
        """Add a text page to pdf and return it."""
        page = pdf.new_page()
        page.insert_text((72, 80), txt, fontsize=16)
        return page

    @staticmethod
    def insert_scan_page(pdf, text):
        """Add an image page containing text to the PDF.

        Create an intermediate document with selectable text, convert it to
        an image, then insert that image into the target document. Close the
        intermediate document afterward.
        """
        source = pymupdf.open()
        scan_source = source.new_page()
        scan_source.insert_text((72, 80), text, fontsize=18)
        png = scan_source.get_pixmap(dpi=150, alpha=False).tobytes("png")
        page = pdf.new_page()
        page.insert_image(page.rect, stream=png)
        source.close()

    def test_searchable_pdf_skips_ocr(self):
        """Skip OCR when the text layer is usable."""
        pdf = pymupdf.open()
        self.write_text_page(pdf, "This is a searchable document with real letters and words about geophysics.")
        pdf.save(self.file)
        pdf.close()
        with patch("app.ocr_page", side_effect=AssertionError("OCR should not run")):
            pages, count, missing, ocr = extract_pdf(self.file)
        self.assertEqual((count, ocr, missing), (1, [], []))
        self.assertIn("searchable document", pages[0]["text"])

    @requires_tesseract
    def test_image_pdf_uses_ocr(self):
        """Recognize an image page and preserve its page number."""
        pdf = pymupdf.open()
        self.insert_scan_page(pdf, "Scientific document analysis. Field data and important results.")
        pdf.save(self.file)
        pdf.close()
        pages, count, missing, ocr = extract_pdf(self.file)
        self.assertEqual(count, 1)
        self.assertEqual(ocr, [1])
        self.assertEqual(missing, [])
        self.assertIn("scientific document", pages[0]["text"].lower())

    @requires_tesseract
    def test_mixed_pdf_ocr_only_scan_page(self):
        """Restrict OCR to the scanned page in a mixed document."""
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
