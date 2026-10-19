"""HUB3 PDF417 and EPC QR: the payloads follow the specs and decode back."""

import io

import pytest

from captain_claw.payments import decode, epc, hub3

zxingcpp = pytest.importorskip("zxingcpp")
pytest.importorskip("pdf417gen")
pytest.importorskip("segno")

# HUB's own example (HUB3A barcode instruction v6, Sept 2022, "Primjer zapisa").
SPEC_EXAMPLE = (
    "HRVHUB30\nEUR\n000000000012355\nŽELJKO SENEKOVIĆ\nIVANEČKA ULICA 125\n42000 VARAŽDIN\n"
    "2DBK d.d.\nALKARSKI PROLAZ 13B\n21230 SINJ\nHR1210010051863000160\nHR01\n"
    "7269-68499637766-00019\nCOST\nTroškovi za 1. mjesec\n"
)


def _spec_slip(**over):
    args = dict(
        amount_cents=12355, iban="HR1210010051863000160", payee_name="2DBK d.d.",
        payee_street="ALKARSKI PROLAZ 13B", payee_city="21230 SINJ", model="HR01",
        reference="7269-68499637766-00019", purpose_code="COST",
        description="Troškovi za 1. mjesec", payer_name="ŽELJKO SENEKOVIĆ",
        payer_street="IVANEČKA ULICA 125", payer_city="42000 VARAŽDIN",
    )
    args.update(over)
    return hub3.build_payload(**args)


def _decode(png: bytes) -> str:
    from PIL import Image

    results = zxingcpp.read_barcodes(Image.open(io.BytesIO(png)))
    assert results, "nothing decoded"
    return results[0].text


def test_hub3_matches_the_spec_example():
    slip = _spec_slip()
    assert slip.payload == SPEC_EXAMPLE
    assert slip.notes == ()


def test_hub3_payer_left_empty():
    slip = _spec_slip(payer_name="", payer_street="", payer_city="")
    assert slip.payload.split("\n")[3:6] == ["", "", ""]


def test_hub3_is_binary_compacted():
    assert hub3.data_codewords(SPEC_EXAMPLE)[0] in (901, 924)


def test_hub3_round_trip():
    assert _decode(hub3.render_png(SPEC_EXAMPLE)) == SPEC_EXAMPLE


def test_hub3_fits_text_and_says_so():
    slip = _spec_slip(payee_name="Müller & Söhne Elektro-Installationen GmbH", description="Račun #42; hvala!")
    lines = slip.payload.split("\n")
    assert lines[6] == "Muller + Sohne Elektro-In"        # transliterated, & → +, cut to 25
    assert lines[13] == "Račun 42, hvala."
    assert any(n.startswith("payee:") for n in slip.notes)
    assert any(n.startswith("description:") for n in slip.notes)


def test_hub3_refuses_foreign_iban():
    with pytest.raises(ValueError):
        _spec_slip(iban="DE89370400440532013000")


def test_hub3_parse():
    fields = hub3.parse(SPEC_EXAMPLE)
    assert fields["amount_cents"] == 12355
    assert fields["iban"] == "HR1210010051863000160"
    assert fields["reference"] == "7269-68499637766-00019"
    assert fields["payee_name"] == "2DBK d.d."
    assert hub3.parse("BCD\n002") is None


def test_epc_payload_with_text():
    payload = epc.build_payload(name="Wikimedia Foerdergesellschaft", iban="DE33 1002 0500 0001 1947 00",
                                amount_cents=2000, text="Spende fuer Wikipedia").payload
    assert payload == ("BCD\n002\n1\nSCT\n\nWikimedia Foerdergesellschaft\nDE33100205000001194700\n"
                       "EUR20.00\n\n\nSpende fuer Wikipedia")


def test_epc_rf_reference_is_structured():
    payload = epc.build_payload(name="Franz Mustermänn", iban="DE71110220330123456789",
                                amount_cents=1230, reference="RF18539007547034", text="ignored").payload
    lines = payload.split("\n")
    assert lines[9] == "RF18539007547034"
    assert len(lines) == 10                        # nothing after the last element


def test_epc_croatian_reference_travels_as_text():
    payload = epc.build_payload(name="X", iban="HR1210010051863000160", amount_cents=100,
                                reference="7269-1", text="Invoice 7").payload
    assert payload.split("\n")[10] == "7269-1 Invoice 7"


def test_epc_limits():
    with pytest.raises(ValueError):
        epc.build_payload(name="X", iban="DE89370400440532013000", amount_cents=0)
    long = epc.build_payload(name="X" * 90, iban="DE89370400440532013000", amount_cents=1, text="y" * 300)
    assert len(long.payload.encode()) <= epc.MAX_BYTES
    assert long.notes


def test_epc_round_trip_and_parse():
    payload = epc.build_payload(name="2DBK d.d.", iban="HR1210010051863000160", amount_cents=12355,
                                text="Troškovi za 1. mjesec").payload
    text = _decode(epc.render_png(payload))
    assert text == payload
    fields = epc.parse(text)
    assert fields["amount_cents"] == 12355 and fields["iban"] == "HR1210010051863000160"


def test_decode_finds_barcodes_in_image_and_pdf():
    from PIL import Image

    png = hub3.render_png(SPEC_EXAMPLE)
    found, text = decode.read(png, "slip.png")
    assert [f.kind for f in found] == ["hub3"] and text == ""
    assert found[0].fields["amount_cents"] == 12355

    pdf = io.BytesIO()
    page = Image.new("RGB", (1240, 1754), "white")
    page.paste(Image.open(io.BytesIO(png)).convert("RGB"), (100, 1200))
    page.save(pdf, format="PDF", resolution=150)
    found, _ = decode.read(pdf.getvalue(), "invoice.pdf")
    assert [f.kind for f in found] == ["hub3"]


def test_decode_text_file_and_junk():
    found, text = decode.read("IBAN HR1210010051863000160".encode(), "mail.txt")
    assert found == [] and "HR12" in text
    assert decode.read(b"not an image", "x.bin") == ([], "")
