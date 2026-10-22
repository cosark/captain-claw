"""One lock for every pdfium call in the process.

PDFium is not thread-safe, not even across separate documents: two threads
rendering at once crash the whole process (SIGSEGV) or fail to load valid
files. Flight Deck renders PDF thumbnails in worker threads and reads invoices
for payments, so every use of pypdfium2 holds this lock from opening the
document to closing it.
"""

from __future__ import annotations

import threading

PDFIUM_LOCK = threading.Lock()
