"""Read an invoice: the payment barcodes in it (HUB3 PDF417, EPC QR) and its text.

A barcode the payee printed is the most reliable source there is, so prepare
looks for one first; the text is what the fields a model read are checked
against."""

from __future__ import annotations

import io
from dataclasses import dataclass

from captain_claw.payments import epc, hub3

_PDF_DPI = 200
_MAX_PDF_PAGES = 4
_TEXT_SUFFIXES = (".txt", ".md", ".csv", ".html", ".htm", ".eml", ".xml", ".json")


@dataclass(frozen=True)
class Found:
    kind: str          # "hub3" | "epc"
    text: str
    fields: dict


def _classify(text: str) -> Found | None:
    fields = hub3.parse(text)
    if fields is not None:
        return Found("hub3", text, fields)
    fields = epc.parse(text)
    if fields is not None:
        return Found("epc", text, fields)
    return None


def _scan_image(image) -> list[Found]:
    import zxingcpp

    formats = (zxingcpp.BarcodeFormat.PDF417, zxingcpp.BarcodeFormat.QRCode)
    found: list[Found] = []
    for result in zxingcpp.read_barcodes(image, formats=formats):
        item = _classify(result.text)
        if item is not None and all(f.text != item.text for f in found):
            found.append(item)
    return found


def is_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


def read(data: bytes, name: str = "") -> tuple[list[Found], str]:
    """(payment barcodes found, the document's text) for a PDF, an image or a
    text file. Text is '' for a photo or a scanned PDF."""
    if not data:
        return [], ""
    if is_pdf(data):
        import pypdfium2 as pdfium

        from captain_claw.pdfium_lock import PDFIUM_LOCK

        # pdfium isn't thread-safe: render the pages under the process-wide
        # lock, then scan the pictures for barcodes outside it.
        with PDFIUM_LOCK:
            pdf = pdfium.PdfDocument(data)
            try:
                texts: list[str] = []
                images = []
                for index in range(min(len(pdf), _MAX_PDF_PAGES)):
                    page = pdf[index]
                    texts.append(page.get_textpage().get_text_range())
                    images.append(page.render(scale=_PDF_DPI / 72).to_pil())
            finally:
                pdf.close()
        found: list[Found] = []
        for image in images:
            for item in _scan_image(image):
                if all(f.text != item.text for f in found):
                    found.append(item)
        return found, "\n".join(texts)
    if name.lower().endswith(_TEXT_SUFFIXES):
        return [], data.decode("utf-8", errors="replace")
    from PIL import Image, UnidentifiedImageError

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError):
        return [], ""
    return _scan_image(image.convert("RGB")), ""
