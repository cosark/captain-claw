"""Payments: get a bill ready for the user to pay in their own bank app.

Pure helpers, no I/O beyond rendering PNGs: IBAN and payment-reference checks,
the HUB3 PDF417 and EPC QR barcodes, finding those barcodes in an invoice,
grounding the fields a model read in the document's text, and the warnings a
prepared payment carries. Flight Deck's ledger and routes use them.

Captain never moves money: it prepares a payment and hands over a barcode; the
user approves it in their bank app (docs/payments-phase0-plan.md).
"""
