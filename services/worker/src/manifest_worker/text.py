"""Splitting a PDF into pages, and each page's text: the native layer, else OCR."""

import io
import shutil
import subprocess
import threading

import pypdfium2 as pdfium

from manifest_domain.lifecycle import DocumentKind
from manifest_worker.errors import ContentError, NotConfiguredError, TransientError

OCR_DPI = 300
OCR_TIMEOUT_S = 120

# PDFium is not thread-safe, not even across separate documents: every call goes through this
# lock, and pages are closed explicitly so no finalizer touches PDFium from another thread.
_PDFIUM = threading.Lock()


def split_pages(pdf: bytes) -> list[bytes]:
    """One single-page PDF per page, in order."""
    pages = []
    with _PDFIUM:
        try:
            source = pdfium.PdfDocument(pdf)
        except pdfium.PdfiumError as exc:
            raise ContentError(f"not a readable PDF: {exc}") from exc
        try:
            for index in range(len(source)):
                single = pdfium.PdfDocument.new()
                single.import_pages(source, [index])
                out = io.BytesIO()
                single.save(out)
                single.close()
                pages.append(out.getvalue())
        finally:
            source.close()
    if not pages:
        raise ContentError("PDF has no pages")
    return pages


def native_text(page_pdf: bytes) -> str:
    with _PDFIUM:
        document = pdfium.PdfDocument(page_pdf)
        try:
            page = document[0]
            textpage = page.get_textpage()
            text = str(textpage.get_text_range())
            textpage.close()
            page.close()
            return text
        finally:
            document.close()


def tesseract_ocr(page_pdf: bytes) -> str:
    """OCR one page with the ``tesseract`` CLI (a system package, like Pango for WeasyPrint)."""
    if shutil.which("tesseract") is None:
        raise NotConfiguredError("tesseract is not installed; scanned pages can't be read")
    with _PDFIUM:
        document = pdfium.PdfDocument(page_pdf)
        try:
            page = document[0]
            image = page.render(scale=OCR_DPI / 72).to_pil().convert("L")
            page.close()
        finally:
            document.close()
    png = io.BytesIO()
    image.save(png, "PNG")
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["tesseract", "stdin", "stdout", "--dpi", str(OCR_DPI)],  # noqa: S607 - from PATH
            input=png.getvalue(),
            capture_output=True,
            timeout=OCR_TIMEOUT_S,
            check=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise TransientError(f"OCR timed out after {OCR_TIMEOUT_S}s") from exc
    except subprocess.CalledProcessError as exc:
        raise ContentError(f"OCR failed: {exc.stderr.decode(errors='replace')[:200]}") from exc
    return result.stdout.decode("utf-8", errors="replace")


def classify_kind(text: str) -> DocumentKind:
    """Baseline keyword classifier; the order matters (a packing list cites its invoice)."""
    upper = text.upper()
    if "BILL OF LADING" in upper:
        return DocumentKind.BILL_OF_LADING
    if "PACKING LIST" in upper:
        return DocumentKind.PACKING_LIST
    if any(word in upper for word in ("INVOICE", "RECHNUNG", "FACTURE")):
        return DocumentKind.COMMERCIAL_INVOICE
    return DocumentKind.OTHER
