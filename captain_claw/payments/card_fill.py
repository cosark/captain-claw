"""Fill a checkout's card form and pay, without the model ever seeing the card.

Payments Phase 3 (docs/payments-phase3-plan.md). The `payments` tool gets a
one-time card (a Link Agent Wallet virtual card, or a Revolut card made for this
purchase) from Flight Deck and hands it straight to :func:`fill_and_pay`, which
works on the agent's own Playwright page:

1. find the card fields in the page and every frame — hosted fields (Stripe,
   Adyen, Braintree, Checkout.com …) live in iframes;
2. type the card in (keystrokes, so the fields' own formatting runs);
3. click the pay button the agent named;
4. wait, then report only the outcome — which fields were filled (names, never
   values), and the resulting page's title, address and a scrubbed excerpt.

The model never gets to look at the filled form: filling and paying are one
step. If the form is still there afterwards (declined, error), the card fields
are cleared so a later screenshot or page read can't show them.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from captain_claw import card_scrub

SELECTORS: dict[str, tuple[str, ...]] = {
    "number": (
        'input[autocomplete="cc-number"]', 'input[name="cardnumber"]', 'input[name="cardNumber"]',
        'input[name="card_number"]', 'input[name="number"]', 'input[data-elements-stable-field-name="cardNumber"]',
        'input[data-fieldtype="encryptedCardNumber"]', '#credit-card-number', 'input[id*="card-number" i]',
        'input[id*="cardnumber" i]', 'input[name*="cardnumber" i]', 'input[placeholder*="card number" i]',
        'input[aria-label*="card number" i]', 'input[placeholder*="broj kartice" i]',
    ),
    "exp": (
        'input[autocomplete="cc-exp"]', 'input[name="exp-date"]', 'input[name="expiry"]', 'input[name="cardExpiry"]',
        'input[data-elements-stable-field-name="cardExpiry"]', 'input[data-fieldtype="encryptedExpiryDate"]',
        '#expiration', 'input[name*="expir" i]', 'input[placeholder*="MM / YY" i]', 'input[placeholder*="MM/YY" i]',
    ),
    "exp_month": (
        'select[autocomplete="cc-exp-month"]', 'input[autocomplete="cc-exp-month"]',
        'select[name*="exp_month" i]', 'select[name*="expmonth" i]', 'input[name*="exp_month" i]',
    ),
    "exp_year": (
        'select[autocomplete="cc-exp-year"]', 'input[autocomplete="cc-exp-year"]',
        'select[name*="exp_year" i]', 'select[name*="expyear" i]', 'input[name*="exp_year" i]',
    ),
    "cvc": (
        'input[autocomplete="cc-csc"]', 'input[name="cvc"]', 'input[name="cvv"]', 'input[name="cardCvc"]',
        'input[data-elements-stable-field-name="cardCvc"]', 'input[data-fieldtype="encryptedSecurityCode"]',
        '#cvv', 'input[name*="cvc" i]', 'input[name*="cvv" i]', 'input[name*="securitycode" i]',
    ),
    "name": (
        'input[autocomplete="cc-name"]', 'input[name*="cardholder" i]', 'input[name="ccname"]',
        'input[placeholder*="name on card" i]',
    ),
}
LINK_PAY_TOKEN = 'input[name="link_pay_token"]'

_CONFIRMED_RE = re.compile(
    r"(?i)\b(?:thank\s*you|order\s*(?:number|no\.?|#|confirmed|received|placed)|confirmation\s*(?:number|#)"
    r"|payment\s*(?:successful|complete[d]?|received)|hvala|narud[žz]b\w*\s+(?:je\s+)?(?:zaprimljen|potvr[đd]en|uspje[šs]n)"
    r"|pla[ćc]anje\s+(?:je\s+)?uspje[šs]n\w*|bestellung\s+(?:best[äa]tigt|eingegangen)|vielen\s+dank)")
_DECLINED_RE = re.compile(
    r"(?i)\b(?:declined|was\s+not\s+(?:accepted|authori[sz]ed)|payment\s+failed|card\s+(?:was\s+)?refused"
    r"|insufficient\s+funds|odbijen\w*|neuspje[šs]n\w*|abgelehnt)")
_ACTION_RE = re.compile(
    r"(?i)\b(?:3-?d\s*secure|verify\s+(?:this|your)\s+(?:payment|purchase)|authenticat\w+\s+(?:this|your)\s+payment"
    r"|approve\s+(?:it\s+)?in\s+your\s+(?:bank|banking)?\s*app|potvrdite?\s+(?:u|plaćanje))")


@dataclass
class Card:
    number: str
    exp_month: int
    exp_year: int
    cvc: str
    name: str = ""

    def __repr__(self) -> str:          # never print the card
        return f"Card(…{self.number[-4:]})"


@dataclass
class FillResult:
    filled: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    outcome: str = "unknown"            # confirmed | declined | needs_action | not_filled | no_button | unknown
    url: str = ""
    title: str = ""
    excerpt: str = ""
    cleared: bool = False

    def as_dict(self) -> dict:
        return {"filled": self.filled, "missing": self.missing, "outcome": self.outcome, "url": self.url,
                "title": self.title, "excerpt": self.excerpt, "cleared": self.cleared}


async def _first_visible(page: Any, selectors: tuple[str, ...], override: str = "") -> Any | None:
    """The first visible element matching *override* or *selectors* in the page
    or any of its frames."""
    for frame in page.frames:
        for selector in ((override,) if override else selectors):
            try:
                loc = frame.locator(selector)
                count = await loc.count()
            except Exception:  # noqa: BLE001 — a detached frame, a bad selector
                continue
            for i in range(count):
                item = loc.nth(i)
                try:
                    if await item.is_visible():
                        return item
                except Exception:  # noqa: BLE001
                    continue
    return None


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


async def _type(loc: Any, value: str) -> None:
    await loc.click()
    await loc.fill("")
    await loc.press_sequentially(value, delay=15)


async def _select(loc: Any, wanted: tuple[str, ...]) -> bool:
    for value in wanted:
        for how in ("value", "label"):
            try:
                await loc.select_option(**{how: value})
                return True
            except Exception:  # noqa: BLE001
                continue
    return False


async def _set(loc: Any, select_values: tuple[str, ...], text: str) -> bool:
    """A <select> picks one of *select_values*; an input gets *text* typed."""
    if await loc.evaluate("e => e.tagName") == "SELECT":
        return await _select(loc, select_values)
    await _type(loc, text)
    return True


async def fill_card(page: Any, card: Card, overrides: dict[str, str] | None = None) -> tuple[list[str], list[str]]:
    """Fill the card fields; returns (filled field names, missing field names)."""
    overrides = overrides or {}
    filled: list[str] = []
    missing: list[str] = []

    number = await _first_visible(page, SELECTORS["number"], overrides.get("number", ""))
    if number is None:
        return [], ["number"]
    await _type(number, card.number)
    if _digits(await number.input_value()).endswith(card.number[-4:]):
        filled.append("number")
    else:
        missing.append("number")

    mm, yy = f"{card.exp_month:02d}", f"{card.exp_year % 100:02d}"
    exp = await _first_visible(page, SELECTORS["exp"], overrides.get("exp", ""))
    if exp is not None:
        await _type(exp, mm + yy)
        filled.append("expiry")
    else:
        month = await _first_visible(page, SELECTORS["exp_month"], overrides.get("exp_month", ""))
        year = await _first_visible(page, SELECTORS["exp_year"], overrides.get("exp_year", ""))
        ok_month = month is not None and await _set(month, (mm, str(card.exp_month)), mm)
        ok_year = year is not None and await _set(year, (str(card.exp_year), yy), str(card.exp_year))
        (filled if ok_month and ok_year else missing).append("expiry")

    cvc = await _first_visible(page, SELECTORS["cvc"], overrides.get("cvc", ""))
    if cvc is not None:
        await _type(cvc, card.cvc)
        filled.append("cvc")
    else:
        missing.append("cvc")

    if card.name:
        holder = await _first_visible(page, SELECTORS["name"], overrides.get("name", ""))
        if holder is not None and not (await holder.input_value()).strip():
            await _type(holder, card.name)
            filled.append("name")
    return filled, missing


async def clear_card(page: Any) -> bool:
    """Empty every card field still on the page (after a decline or an error)."""
    cleared = False
    for name in ("number", "exp", "exp_month", "exp_year", "cvc"):
        for frame in page.frames:
            for selector in SELECTORS[name]:
                try:
                    loc = frame.locator(selector)
                    for i in range(await loc.count()):
                        item = loc.nth(i)
                        if await item.evaluate("e => e.tagName") == "INPUT" and await item.input_value():
                            await item.fill("")
                            cleared = True
                except Exception:  # noqa: BLE001
                    continue
    return cleared


async def fill_link_pay_token(page: Any, token: str) -> bool:
    """Stripe-enabled checkouts: set the hidden Link Pay Token field the way
    Stripe documents it (native value setter + input event)."""
    for frame in page.frames:
        try:
            loc = frame.locator(f".AiAgentPaymentSteering {LINK_PAY_TOKEN}, {LINK_PAY_TOKEN}")
            if await loc.count():
                await loc.first.evaluate(
                    "(el, t) => { Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')"
                    ".set.call(el, t); el.dispatchEvent(new Event('input', {bubbles: true})); }", token)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def _pay_button(page: Any, pay_button: str) -> Any | None:
    text = (pay_button or "").strip()
    if not text:
        return None
    looks_css = bool(re.match(r"^[#.\[]|^\w+[#.\[]", text))
    for frame in page.frames:
        candidates = ([frame.locator(text)] if looks_css else [
            frame.get_by_role("button", name=text, exact=True), frame.get_by_role("button", name=text),
            frame.locator(f'input[type="submit"][value="{text}"]'),
        ])
        for loc in candidates:
            try:
                if await loc.count() and await loc.first.is_visible():
                    return loc.first
            except Exception:  # noqa: BLE001
                continue
    return None


async def read_outcome(page: Any) -> tuple[str, str, str, str]:
    """(outcome, url, title, scrubbed excerpt) of the page after paying."""
    try:
        text = await page.locator("body").inner_text(timeout=5000)
    except Exception:  # noqa: BLE001
        text = ""
    frames_text = " ".join(f.url for f in page.frames)
    excerpt = card_scrub.scrub(" ".join(text.split())[:1500])[0]
    if _DECLINED_RE.search(text):
        outcome = "declined"
    elif _CONFIRMED_RE.search(text):
        outcome = "confirmed"
    elif _ACTION_RE.search(text) or re.search(r"(?i)3ds|acs|challenge", frames_text):
        outcome = "needs_action"
    else:
        outcome = "unknown"
    try:
        title = await page.title()
    except Exception:  # noqa: BLE001
        title = ""
    return outcome, page.url, card_scrub.scrub(title)[0], excerpt


def redact_known(text: str, card: Card | None) -> str:
    """Remove the card we typed from *text*, however the page wrote it (spaced,
    dashed) — whether or not it looks like a card to the general scrubber."""
    if not text or card is None:
        return text
    digits = _digits(card.number)
    if len(digits) >= 8:
        pattern = r"[ \-]?".join(re.escape(d) for d in digits)
        text = re.sub(pattern, f"[card number removed · ends {digits[-4:]}]", text)
    if card.cvc and len(card.cvc) >= 3:
        text = re.sub(rf"(?i)(cvc|cvv|csc|security code)\W{{0,3}}{re.escape(card.cvc)}\b", r"\1 [CVV removed]", text)
    return text


async def fill_and_pay(page: Any, *, card: Card | None = None, link_pay_token: str = "",
                       pay_button: str, overrides: dict[str, str] | None = None,
                       wait_seconds: float = 20.0) -> FillResult:
    """Fill the card (or the Link Pay Token) and click *pay_button*; see module doc."""
    result = FillResult()
    if link_pay_token:
        if not await fill_link_pay_token(page, link_pay_token):
            result.outcome, result.missing = "not_filled", ["link_pay_token"]
            return result
        result.filled = ["link_pay_token"]
        await asyncio.sleep(2.0)            # Stripe swaps the card form for the saved card
    elif card is not None:
        result.filled, result.missing = await fill_card(page, card, overrides)
        if "number" in result.missing or "cvc" in result.missing:
            result.cleared = await clear_card(page)
            result.outcome = "not_filled"
            return result
    button = await _pay_button(page, pay_button)
    if button is None:
        result.cleared = await clear_card(page)
        result.outcome = "no_button"
        return result
    await button.click()
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while True:
        try:
            await page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:  # noqa: BLE001
            pass
        result.outcome, result.url, result.title, result.excerpt = await read_outcome(page)
        if result.outcome in ("confirmed", "declined") or asyncio.get_running_loop().time() > deadline:
            break
        await asyncio.sleep(1.0)
    if result.outcome != "confirmed":
        result.cleared = await clear_card(page)
    result.excerpt = redact_known(result.excerpt, card)
    result.title = redact_known(result.title, card)
    result.url = redact_known(result.url, card)
    return result
