"""Safe card fill (Payments Phase 3): the card goes into the checkout's fields —
plain, hosted in an iframe, or split month/year — the pay button is clicked in
the same step, and nothing the model can see afterwards carries the card."""

from __future__ import annotations

import asyncio
import html

import pytest

from captain_claw.payments import card_fill

playwright = pytest.importorskip("playwright.async_api")

CARD = card_fill.Card(number="4000009990001984", exp_month=6, exp_year=2029, cvc="123", name="Ana Horvat")

_PAY_JS = """
<script>
function pay(get) {
  const ok = get('num').replace(/\\D/g,'') === '4000009990001984' && get('cvc') === '123';
  document.getElementById('out').textContent = ok ? 'Thank you! Order number A-1001.' : 'Payment declined.';
  if (ok) { document.getElementById('form').remove(); }
}
</script>"""

PLAIN = f"""<html><body>{_PAY_JS}
<div id="form">
 <input autocomplete="cc-number" id="num"> <input autocomplete="cc-exp" id="exp">
 <input autocomplete="cc-csc" id="cvc"> <input autocomplete="cc-name" id="name">
</div>
<button onclick="pay(id => document.getElementById(id).value)">Pay now</button><p id="out"></p></body></html>"""

FRAME_FIELDS = ('<input name="cardnumber" id="num" placeholder="Card number">'
                '<input name="exp-date" id="exp" placeholder="MM / YY"><input name="cvc" id="cvc">')
HOSTED = f"""<html><body>{_PAY_JS}
<div id="form"><iframe id="f" name="__privateStripeFrame1" srcdoc="{html.escape(FRAME_FIELDS)}"></iframe></div>
<button onclick="pay(id => document.getElementById('f').contentDocument.getElementById(id).value)">Place order</button>
<p id="out"></p></body></html>"""

SPLIT = f"""<html><body>{_PAY_JS}
<div id="form"><input name="cardNumber" id="num">
 <select autocomplete="cc-exp-month" id="m"><option value="">MM</option><option value="06">06</option></select>
 <select autocomplete="cc-exp-year" id="y"><option value="">YYYY</option><option value="2029">2029</option></select>
 <input name="cvv" id="cvc"></div>
<button onclick="pay(id => document.getElementById(id).value)">Plati</button><p id="out"></p></body></html>"""

TOKEN = """<html><body><div class="AiAgentPaymentSteering"><input name="link_pay_token" type="hidden" id="t"></div>
<script>document.getElementById('t').addEventListener('input', e => window.tok = e.target.value)</script>
<button onclick="document.getElementById('out').textContent = window.tok === 'lpt_test_1' ? 'Payment successful' : 'Payment failed'">Pay</button>
<p id="out"></p></body></html>"""


def _run(page_html: str, **kwargs):
    async def go():
        async with playwright.async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content(page_html)
                result = await card_fill.fill_and_pay(page, wait_seconds=3, **kwargs)
                body = await page.content()
                frames = [await f.content() for f in page.frames]
                return result, body, frames
            finally:
                await browser.close()
    return asyncio.run(go())


def _no_card_anywhere(result, body: str, frames: list[str]) -> None:
    text = repr(result.as_dict()) + repr(result)
    assert "4000009990001984" not in text and "123" not in result.excerpt


def test_plain_form_is_filled_and_paid():
    result, body, frames = _run(PLAIN, card=CARD, pay_button="Pay now")
    assert result.filled == ["number", "expiry", "cvc", "name"] and result.outcome == "confirmed"
    assert "Order number A-1001" in result.excerpt
    _no_card_anywhere(result, body, frames)


def test_hosted_fields_in_an_iframe():
    result, *_ = _run(HOSTED, card=CARD, pay_button="Place order")
    assert result.filled[:3] == ["number", "expiry", "cvc"] and result.outcome == "confirmed"


def test_split_month_and_year_selects():
    result, *_ = _run(SPLIT, card=CARD, pay_button="Plati")
    assert "expiry" in result.filled and result.outcome == "confirmed"


def test_a_decline_clears_the_card_from_the_page():
    wrong = card_fill.Card(number="4242424242424242", exp_month=6, exp_year=2029, cvc="999")
    result, body, frames = _run(PLAIN, card=wrong, pay_button="Pay now")
    assert result.outcome == "declined" and result.cleared
    assert "4242" not in "".join(frames)


def test_no_pay_button_means_nothing_is_paid_and_the_form_is_cleared():
    result, body, frames = _run(PLAIN, card=CARD, pay_button="Buy with crypto")
    assert result.outcome == "no_button" and result.cleared


def test_link_pay_token():
    result, *_ = _run(TOKEN, link_pay_token="lpt_test_1", pay_button="Pay")
    assert result.filled == ["link_pay_token"] and result.outcome == "confirmed"


def test_card_repr_never_shows_the_number():
    assert "4000009990001984" not in repr(CARD) and repr(CARD).endswith("1984)")


def test_the_card_we_typed_is_removed_from_whatever_the_page_says():
    page_text = "Receipt: paid with 4000 0099 9000 1984, CVC 123. Order A-7."
    out = card_fill.redact_known(page_text, CARD)
    assert "4000" not in out and "1984]" in out and "123" not in out.replace("A-7", "")
