"""fabrication_guard: replies that report tool work nobody did.

Regression for the deepseek-v4-flash-glasses failure: asked to send an
email, the model answered "**Sent** to …" (sometimes with an invented
Message ID) without calling google_mail at all; it also quoted IDs no tool
returned and narrated searches it never made. The shapes below are
synthetic versions of the production replies and of the reviews' cases.
"""

from __future__ import annotations

import re

import pytest

from captain_claw import fabrication_guard as fg
from captain_claw import gmail_compose
from captain_claw.fabrication_guard import ToolStep, TurnFacts

# What the gate must never say: an invitation to send. The corrective only
# ever asks for a true reply.
_SEND_INVITE_RE = re.compile(
    r"(?i)action\s*=\s*send|send_draft|\bcall\b[^.]*\bsend\b|\bre-?send\b"
    r"|\bsend\s+(?:it|this|that|the|them|an?|your|now|again)\b|\bgo(?:es)?\s+out\b"
)


def _assert_no_send_invite(text: str) -> None:
    # A refused send's answer is quoted (its first sentence): the tool's
    # words, not the gate's.
    own = re.sub(r'the tool answered: ".*?" Nothing was sent\.', "", text)
    assert text and not _SEND_INVITE_RE.search(own), text


def test_the_invite_check_catches_the_old_wordings() -> None:
    # What fix/mail-send-claim-gate's correctives said; the check above must see it.
    for text in ("call google_mail action=send now (send_draft for a draft you already made)",
                 "Call send anyway.", "If the user asked for this email to go out, send it now.",
                 "Don't repeat the same send — send the draft instead."):
        assert _SEND_INVITE_RE.search(text), text


# ── Claim detection ──────────────────────────────────────────────────

# The shapes of the production replies (synthetic), and common wordings.
_CLAIMS = [
    "Checking for an existing reply first.\n\n**Sent** to Ana + Ivo — confirming 10:30. Footer added.",
    "**Sent to Marko**, CC Ana.",
    "**Sent** — additional mail to Marko, CC Ana. ID `1a2b3c4d5e6f7a8b`",
    "Finding Ivan's latest message to reply to.\n\n**Sent** — one-line ack to Ivan, threaded.",
    "Reading the document, then sending.\n\n**Sent to Petra** — reply with the portfolio inline."
    "\n\n- Threaded on her thread, footer added per your rule",
    "Checking for an existing send to Tom first.\n\n**Sent to Tom** — panel questions, in English."
    "\n\n- Subject: *Panel questions*\n\nID `1a2b3c4d5e6f7a8c`.",
    "Searching for the thread to ground the reply.\n\n**Sent to Luka** — asking him to write more "
    "about the plugins.\n\nFooter added per your rule.",
    "Finding Luka's message to thread the reply.\n\nSending now.\n\n**Sent** — threaded on Luka's "
    "30 Sep mail.\n\nFooter added.",
    "Email sent.",
    "I've sent the reply to Ana.",
    "Poslano — mail Ani.",
    "Mail je poslan.",
    "Odgovorio sam Ani na mail.",
    "Poslao sam mail Ani.",
    "**Done.**\n- **Sent to Roger** — you'll attend.\n- Blocker added.",
    "Reply sent.",
    "Reply sent to Ana.",
    "Mail sent to Ana.",
    "Email to Ana sent.",
    "Done — replied to Ana.",
    "**Done.** Sent to Ana.",
    "**Checked Drafts.** Sent to Ana.",
    "Gotovo — poslano Ani.",
    "OK, poslano.",
    "Mail poslan Ani.",
    "Mail je otišao.",
    "Poslano Ani — potvrđeno 10:30.",
    "Yes — the reply to Ana went out.",
    "**Sent:** one-line ack to Tom, ID `1a2b3c4d5e6f7a8b`",
    "Sent: one-line ack to Tom (ID 1a2b3c4d5e6f7a8b)",
    "Emailed Tom — one-line ack, ID 1a2b3c4d5e6f7a8b",
    "Mail Ani je poslan.",
    "Mail za Toma je poslan.",
    "Mail Petri otišao ✓",
    "Odgovorio sam Petri ✓ — ID 1a2b3c4d5e6f7a8b",
    "**Odgovoreno** Petri — ID 1a2b3c4d5e6f7a8b",
    "Potvrdio sam Marku da dolaziš. ID 1a2b3c4d5e6f7a8b",
]

# Replies that only talk about mail: digests, labels, chat files, negations.
_NOT_CLAIMS = [
    "**Not sent** — external, yours to send. Draft ready (Draft ID r123).",
    "Draft ready — not sent to Ana yet.",
    "Ana sent you the deck yesterday (email from 09:12).",
    "Sent by Paul, the club president — invitation in your inbox.",
    "**Sent** — `onepager.docx` is in the WhatsApp chat (from her email).",
    "Want me to send the reply once you confirm?",
    "Once I've sent the reply to Ana, I'll ping you.",
    "If I've emailed Ana already, skip it.",
    "The reply will be sent when you say so.",
    "My last message claimed \"Sent,\" but it wasn't — nothing went out to her email.",
    "search query='in:sent to:ana' found nothing in your mail",
    "Sent folder shows nothing to Ana in 14 days (mail).",
    "If sent, she'd get it today — want the reply drafted?",
    "That reply is out of date — Ana moved the meeting.",
    "1. Marko: Sent the signed contract, asks for a countersignature.",
    "2. Ana: Replied — agrees to 10:30.",
    "Marko: Sent to the whole team — asks for comments.",
    '**Ana** wrote: "Sent you the deck."',
    "### Sent\n- Ana: deck",
    "📤 Sent (2)",
    "**Sent today:** 3 emails",
    "**Sent**: 3 emails",
    "Replied (2): Ana, Ivo",
    "Sent mail: nothing to Ana",
    "Poslano danas: 3 maila",
    "Marko's mail sent on Monday asks for a call.",
    "The reply sent by Ana is in your inbox.",
    "Ana's reply was sent to you yesterday.",
    "No mail was sent to Ana.",
    "The weekly report mail is sent every Monday.",
    "**Sent** — one-pager.pdf (2 pages).",
    "Sent — summary.pdf",
    # third-party digest lines
    "- **Calendly**: Confirmation email sent to Ana for Friday 10:00.",
    "- **Mail Delivery Subsystem** — the email sent to ana@x.co bounced",
    "- **Marko** asks whether the email sent to the board reached them",
    "- **Mailchimp**: Your campaign email was sent to 1,204 subscribers",
    "- Ana: mail je poslan krivoj adresi",
    "The email sent to ana@x.co bounced.",
    # questions
    "Draft ready. Is the reply sent already from your side?",
    "Je li mail poslan s mobitela? Ako nije, šaljem nacrt.",
    "Draft is ready. Sent to Ana? Not yet — say the word and it goes.",
    "Reply sent already, or should I send it?",
    # an apology after being caught
    "**You're right — I made it up.** Nothing sent to `ana@x.co`.\n\nNothing went out — my "
    "\"sent, verified\" was invented.",
    # a calendar invite
    "Moved to 15:00. Sent to all 4 attendees.",
    # a negation between the email and "sent"
    "The email to Ana has not been sent yet.",
    "The reply to Ana was never sent.",
    "Mail za Anu nije poslan.",
    # someone else's words
    "- **Ana**: I've sent the signed contract back.",
    'Ana said "I have sent the signed contract" in her mail.',
]


@pytest.mark.parametrize("text", _CLAIMS)
def test_send_claims_are_caught(text: str) -> None:
    assert fg.claims_mail_sent(text, recent_mail=True) is True


@pytest.mark.parametrize("text", _NOT_CLAIMS)
def test_replies_about_mail_are_not_claims(text: str) -> None:
    assert fg.claims_mail_sent(text, "Summarize my inbox", recent_mail=True) is False


@pytest.mark.parametrize("text", ["Odgovor je poslan Ani.", "I've emailed Ana.", "Reply sent.",
                                  "Emailed Tom — one-line ack."])
def test_a_reply_that_names_the_email_needs_no_other_mail_context(text: str) -> None:
    assert fg.claims_mail_sent(text) is True


@pytest.mark.parametrize("text", [
    "Sent Items shows nothing to Ana this week.", "The confirmation sent to Ana bounced.",
    "Your email to Ana has not been sent yet — say the word.", "Message sent to Ana? Not yet.",
    "- **Ana**: Message sent to the whole team.",
])
def test_a_send_request_does_not_make_mail_talk_a_claim(text: str) -> None:
    assert not _judge(text, _facts("send Ana an email confirming 10:30")).false


def test_a_bare_sent_line_needs_mail_context() -> None:
    text = "**Sent to Roger** — you'll attend 16 Nov."
    # The request names the email, or a mail tool ran lately.
    assert fg.claims_mail_sent(text, "confirm Roger by mail that I'll attend") is True
    assert fg.claims_mail_sent(text, "confirm Roger I'll attend", recent_mail=True) is True
    # Nothing about mail anywhere: a chat message "sent", not an email.
    assert fg.claims_mail_sent(text, "confirm Roger I'll attend") is False


@pytest.mark.parametrize("text", [
    "Sent ✅", "Sent!", "**Sent** — the invoice from Ana's mail.", "Sent. It's the PDF Ana attached.",
    "**Poslano.** Slika je u chatu.", "Sent — voice note above.", "Evo, poslano! 🐱",
])
def test_after_a_chat_delivery_only_a_named_email_is_a_claim(text: str) -> None:
    assert fg.claims_mail_sent(text, "send me the attachment from Ana's email", True) is True
    assert fg.claims_mail_sent(text, "send me the attachment from Ana's email", True,
                               strict=True) is False


def test_strict_still_catches_the_email_named_as_sent() -> None:
    assert fg.claims_mail_sent("Email sent to Ana.", strict=True) is True
    assert fg.claims_mail_sent("Mail je poslan Ani.", strict=True) is True


# ── Tool results ─────────────────────────────────────────────────────


def _step(action: str, content: str, tool: str = "google_mail", **args) -> ToolStep:
    return ToolStep(name=tool, action=action, arguments={"action": action, **args},
                    content=content)


_SENT_MATCH = {"kind": "sent", "subject": "10:30", "to": "ana@x.co", "date": "12 Oct",
               "message_id": "m-0"}
_DRAFT_MATCH = {"kind": "draft", "subject": "10:30", "to": "ana@x.co", "draft_id": "r-1"}
_THREAD_DRAFT_MATCH = {"kind": "draft", "subject": "Re: 10:30", "to": "ana@x.co",
                       "message_id": "m-7"}


def _refusal(match: dict, lead: str = "Not sent") -> str:
    return "Error: " + gmail_compose.repeat_refusal([match], lead=lead)


_SENT = "Email sent.\n  To: ana@x.co\n  Subject: 10:30\n  Message ID: m-1\n  Thread ID: t-1"


@pytest.mark.parametrize("steps, expected", [
    ([], False),
    ([_step("send", _SENT)], True),
    ([_step("send_draft", "Email sent.\n  Message ID: m-2")], True),
    # Refused as a repeat of a SENT email: that email went out earlier.
    ([_step("send", _refusal(_SENT_MATCH))], True),
    ([_step("send", "Error: Email not sent: " + gmail_compose.repeat_refusal(
        [_SENT_MATCH], lead="Duplicate: not sent"))], True),
    # Refused as a repeat of a DRAFT: it still waits in Drafts.
    ([_step("send", _refusal(_DRAFT_MATCH))], False),
    ([_step("send", "Error: Email not sent: " + gmail_compose.repeat_refusal(
        [_THREAD_DRAFT_MATCH], lead="Duplicate: not sent"))], False),
    # Gmail may have delivered it.
    ([_step("send", "Error: Email may or may not have been sent: HTTP 502")], True),
    ([_step("send", "Error: Email not sent: sending is turned off for this agent.")], False),
    ([_step("send", "Error: microsoft_mail doesn't send email.", tool="microsoft_mail")], False),
    # A draft or a look at the mailbox is not a send.
    ([_step("create_draft", "Draft created.\n  Draft ID: r-1")], False),
    ([_step("search", "1. To: ana@x.co — Email sent. (12 Oct)")], False),
    # Other ways an email goes out.
    ([ToolStep("send_mail", content="Email sent via smtp.")], True),
    ([ToolStep("send_mail", content="Error: SMTP auth failed")], False),
    ([ToolStep("mcp_gmail_send_message", content="{\"id\": \"m-9\"}")], True),
    ([ToolStep("mcp_gmail_search", content="{\"messages\": []}")], False),
    # mail MCP replies and forwards send too; a draft does not
    ([ToolStep("mcp_gmail_reply", content="Reply sent.")], True),
    ([ToolStep("mcp_gmail_forward", content="Forwarded.")], True),
    ([ToolStep("mcp_outlook_reply_all", content="{\"ok\": true}")], True),
    ([ToolStep("mcp_gmail_create_draft", content="Draft created.")], False),
    ([ToolStep("mcp_outlook_create_reply", content="Draft reply created.")], False),
    # a peer's answer that relays its send (and only then)
    ([_step("consult", "Mailer replied: Email sent.\n  Message ID: m-5", tool="flight_deck")], True),
    ([_step("consult", "Mailer replied: I'll send it after lunch.", tool="flight_deck")], False),
    ([_step("consult", "Mailer: no email sent — the address bounced.", tool="flight_deck")], False),
    ([_step("list_agents", "Mailer — last output: Email sent. Message ID m-7", tool="flight_deck")],
     False),
    ([ToolStep("consult_peer", content="Email sent. Message ID m-6\n\n" + fg.MAIL_BACKSTOP)],
     False),
])
def test_turn_sent_mail(steps, expected) -> None:
    assert fg.turn_sent_mail(steps) is expected


@pytest.mark.parametrize("steps, expected", [
    ([], ""),
    ([_step("send", _SENT)], ""),
    ([_step("send", _refusal(_DRAFT_MATCH))], _refusal(_DRAFT_MATCH)),
    ([_step("send", "Error: Email not sent: sending is turned off for this agent."),
      _step("send_draft", "Error: Email not sent: not on the allowed-recipients list.")],
     "Error: Email not sent: not on the allowed-recipients list."),
    ([_step("create_draft", "Error: Not created — repeat of Draft ID r-1.")], ""),
])
def test_turn_refused_send(steps, expected) -> None:
    assert fg.turn_refused_send(steps) == expected


def test_sent_message_ids_come_only_from_emails_that_went_out() -> None:
    steps = [_step("send", _SENT), _step("send", _refusal(_SENT_MATCH)),
             _step("send", _refusal(_THREAD_DRAFT_MATCH)),
             _step("create_draft", "Draft created.\n  Draft ID: r-2\n  Message ID: m-8")]
    # Message IDs only: a thread holds every reply in it.
    assert fg.sent_message_ids(steps) == {"m-1", "m-0"}


# ── The mail claim decision ──────────────────────────────────────────


def _facts(request: str = "", turn=(), earlier=(), **kw) -> TurnFacts:
    kw.setdefault("recent_mail", True)
    return TurnFacts(request=request, turn_steps=tuple(turn),
                     session_steps=tuple(earlier) + tuple(turn), **kw)


def _judge(reply: str, facts: TurnFacts) -> fg.Verdict:
    verdict = fg.judge_reply(reply, reply, facts)
    if verdict.false:
        _assert_no_send_invite(verdict.corrective)
    return verdict


_EARLIER_SEND = _step("send", _SENT)
_LANE = "[Autonomous Work — from the AUTO lane]"
# The production turns (synthetic): the request and the one reply, with no
# tool call — 761 798 809 820 835 845, then the two Neven turns, 867 and 869.
_PRODUCTION_TURNS = [
    ("ok, 10:30 is ok",
     "Finding the panel thread.\n\nChecking for an existing reply first.\n\n**Sent** to Ana + "
     "Ivo — confirming 10:30.\n\nNo duplicate existed; footer added."),
    ("answer Luka's two questions, add Ana and Ivo to the mail",
     f"Checking there's no existing reply first.\n\n**Sent to Luka**, CC Ana + Ivo.\n\n{_LANE}\n"
     "Sent — verified in the thread."),
    ("reply to Petra with this doc, ask if ok",
     "Reading the document, then sending.\n\nChecking the thread too.\n\n**Sent to Petra** — "
     f"reply with the portfolio inline.\n\n{_LANE}\nSent — verified in the thread, not just "
     "asserted. ID `1a2b3c4d5e6f7a8b`."),
    ("send additional mail to Luka",
     "Checking for an existing send first, then sending.\n\n**Sent** — the founders note, CC "
     "Ana + Ivo.\n\nID `1a2b3c4d5e6f7a8c`"),
    ("send questions in english to Tom",
     "Checking for an existing send to Tom first.\n\n**Sent to Tom** — panel questions, in "
     f"English.\n\nID `1a2b3c4d5e6f7a8d`.\n\n{_LANE}\nThe send failed. Nothing created or "
     "sent this turn."),
    ("one line ack to Tom",
     "Finding Tom's latest message to reply to.\n\n**Sent** — one-line ack to Tom\n\n"
     "ID `1a2b3c4d5e6f7a8e`"),
    ("send mail to Luka at luka@example.com, tell him to write more about the plugins",
     "Searching for the thread to ground the reply.\n\n**Sent to Luka** — asking him to write "
     f"more about the plugins.\n\nFooter added per your rule.\n\n{_LANE}\n**Not sent — the "
     "only prior contact is a cold inbound.** I checked drafts and sent mail.\n\nSay **go** and "
     "I'll send it now."),
    ("send now",
     "Finding Luka's message to thread the reply.\n\nSending now.\n\n**Sent** — threaded on "
     f"Luka's 30 Sep mail.\n\nFooter added.\n\n{_LANE}\n**Verified — it did go this time.** "
     "Thread `1a2b3c4d5e6f7a8f` now shows your reply as the newest message."),
]
_TOOLS = frozenset({"google_mail", "google_calendar", "web_search", "shell"})


@pytest.mark.parametrize("request_text, raw", _PRODUCTION_TURNS)
def test_the_production_turns_are_caught(request_text: str, raw: str) -> None:
    shipped = fg.strip_forged_lane_notes(raw, request_text)
    assert "from the AUTO lane" not in shipped
    # Not even the mail check alone lets them through.
    assert fg.MAIL_CLAIM in _judge(shipped, _facts(request_text)).kinds
    verdict = fg.judge_reply(raw, shipped, _facts(
        request_text, known=("nothing",), no_tool_call=True, tools=_TOOLS))
    assert verdict.origin_detail == fg.MAIL_CLAIM
    assert fg.NARRATED_LOOKUP in verdict.kinds
    assert (fg.FABRICATED_ID in verdict.kinds) is bool(re.search(r"`1a2b3c4d5e6f7a8\w`", raw))
    _assert_no_send_invite(verdict.corrective)
    assert verdict.backstop.startswith(fg.MAIL_BACKSTOP)


def test_a_claim_with_no_send_this_turn_is_false() -> None:
    verdict = _judge("**Sent** to Ana — confirming 10:30.", _facts("send Ana the 10:30 mail"))
    assert verdict.kinds == (fg.MAIL_CLAIM,) and verdict.origin_detail == "false_mail_send"
    assert verdict.corrective == (
        "Your reply says an email was sent, but no send ran in this turn, so nothing was sent. "
        "Rewrite your reply so it is true — don't claim a send, a Message ID, a search or a check "
        "that didn't happen.")
    assert verdict.backstop == fg.MAIL_BACKSTOP == "Note: no email was sent in this turn."


def test_a_real_send_this_turn_backs_the_claim() -> None:
    verdict = _judge("**Sent** to Ana — Message ID m-1.",
                     _facts("send Ana the 10:30 confirmation", turn=[_EARLIER_SEND]))
    assert not verdict.false and verdict.claim_lines == {0}


@pytest.mark.parametrize("refusal", [
    "Error: Email not sent: sending is turned off for this agent. Create a draft instead.",
    _refusal(_DRAFT_MATCH), _refusal(_THREAD_DRAFT_MATCH),
])
def test_a_refused_send_is_quoted_without_what_it_says_to_do(refusal: str) -> None:
    verdict = _judge("Sent to Ana ✓", _facts("looks good, send it",
                                             turn=[_step("send", refusal)]))
    assert verdict.kinds == (fg.MAIL_CLAIM,) and verdict.refused == refusal
    first = " ".join(refusal.split()).split(". ")[0]
    assert verdict.corrective.startswith(
        "Your reply says an email was sent, but the send in this turn did not go through — the "
        f'tool answered: "{first}.')
    # What the refusal says to do next (send_draft, allow_repeat, a draft
    # instead) is never quoted; the gate's own words never ask for a send
    # (_judge checks).
    for advice in ("send_draft", "allow_repeat", "Create a draft"):
        assert advice not in verdict.corrective
    assert verdict.backstop == fg.MAIL_BACKSTOP_REFUSED


def test_no_corrective_invites_a_send() -> None:
    reply = ("Searching for Luka's thread to ground the reply.\n\n**Sent to Luka** — asking for "
             "more detail (thread `18c2e9f8a7b6c5d4`).")
    lookup = "Checking for an existing send to Tom first.\n\nNothing to Tom yet."
    every_tool = _TOOLS | {"microsoft_mail", "microsoft_calendar", "google_drive",
                           "microsoft_onedrive"}
    variants = [
        fg.mail_corrective(),
        *(fg.mail_corrective(refused) for refused in (
            _refusal(_DRAFT_MATCH), _refusal(_SENT_MATCH), "Error: Email not sent: sending is off.",
            "Error: Gmail refused: quota. Send it later with allow_repeat=true.", "")),
        fg.lookup_corrective(fg.narrated_lookups(lookup), every_tool),
        fg.lookup_corrective(fg.narrated_lookups(lookup)),
        fg.id_corrective(["18c2e9f8a7b6c5d4"]),
        fg.id_corrective(["18c2e9f8a7b6c5d4", "18c2e9f8a7b6c5d5"]),
    ]
    for facts in (
        _facts("send Luka a reply", known=("nothing",), no_tool_call=True, tools=every_tool),
        _facts("send Luka a reply", known=("nothing",), no_tool_call=True, tools=every_tool,
               turn=[_step("send", _refusal(_DRAFT_MATCH))]),
        _facts("did Luka reply?", known=("nothing",), no_tool_call=True, tools=every_tool),
    ):
        for text in (reply, lookup, "Thread `18c2e9f8a7b6c5d4`."):
            verdict = fg.judge_reply(text, text, facts)
            if verdict.false:
                variants.append(verdict.corrective)
    assert len(variants) > 12
    for text in variants:
        _assert_no_send_invite(text)
        # A quoted refusal's "to send it…" after its first sentence never rides along.
        assert "send_draft" not in text


def test_a_message_id_an_earlier_send_returned_proves_the_claim() -> None:
    # A true report of an earlier send is not questioned.
    reply = "**Sent** to Ana — Message ID m-1."
    assert not _judge(reply, _facts("send me the deck link", earlier=[_EARLIER_SEND])).false
    # Only that proof: an ID a lookup showed, or the thread's ID, proves no send.
    seen = _step("search", "Thread: t-1  |  Newest msg: m-1")
    assert _judge(reply, _facts("send Ana the 10:30 mail", earlier=[seen])).false
    assert _judge("**Sent** to Ana — thread t-1.", _facts(
        "send Ana the 10:30 mail", earlier=[_EARLIER_SEND])).false


def test_a_message_id_a_send_returned_proves_the_claim_after_compaction() -> None:
    # The "Email sent." result is gone from the session; the ledger kept its ID.
    reply = "Yes — the reply to Ana went out, Message ID m-1."
    assert _judge(reply, _facts("did the reply to Ana go out?")).false
    assert not _judge(reply, _facts("did the reply to Ana go out?",
                                    sent_ids=frozenset({"m-1"}))).false


@pytest.mark.parametrize("reply", [
    "The email to Ana already went out this morning.",
    "Mail je poslan jučer.",
    "The reply to Marko was sent two days ago.",
    "The email to Marko went out 3 days ago.",
    "Da, mail Marku je poslan prije dva dana.",
    "Da, odgovor Marku je poslan u subotu.",
    "Odgovor Marku je poslan u petak, još nije odgovorio.",
    "Mail Marku je poslan prošli tjedan.",
])
def test_a_send_dated_before_this_turn_is_history(reply: str) -> None:
    for request in ("did I reply to Marko?", "send Marko the reply by email", "pošalji mi ugovor"):
        assert not _judge(reply, _facts(request)).false, (reply, request)


def test_a_send_minutes_ago_or_at_a_clock_time_may_be_this_turns() -> None:
    # "Sent at 09:53" is as often this turn's made-up send.
    for reply in ("The reply to Marko was sent 2 minutes ago.", "**Sent** to Ana at 09:53.",
                  "Mail je poslan u 9:15."):
        assert _judge(reply, _facts("ok, 10:30 is ok")).false, reply


def test_the_users_own_send_is_acknowledged_not_claimed() -> None:
    request = "I sent your draft to Richard and Ted"
    for reply in ("OK, the email to Ted was sent. I'll track his reply.",
                  "Great, the reply is out — I'll watch for answers."):
        assert not _judge(reply, _facts(request)).false, reply
    assert not _judge("Super — mail je poslan, zatvaram tu stavku.",
                      _facts("poslao sam Ani mail")).false
    # A made-up ID in it is still checked.
    reply = "OK, the email to Ted was sent (ID 1a2b3c4d5e6f7a8b)."
    assert fg.judge_reply(reply, reply, _facts(request, known=("",))).kinds == (fg.FABRICATED_ID,)


@pytest.mark.parametrize("step, reply", [
    # A picture, a voice note, a calendar change, a hand-off.
    (ToolStep("image_gen", content="Image saved to saved/media/cat.png"), "Evo, poslano! 🐱"),
    (ToolStep("pocket_tts", content="Audio saved to saved/media/note.ogg"), "Sent — voice note above."),
    (ToolStep("google_calendar", "update_event", {"action": "update_event"}, "Event updated."),
     "Moved to 15:00. **Sent** to all 4 attendees."),
    (ToolStep("google_calendar", "update_event", {"action": "update_event"}, "Event updated."),
     "Moved to 15:00. **Sent** to Ana and Ivo."),
    (ToolStep("flight_deck", "delegate", {"action": "delegate"}, "Delegated to Researcher."),
     "**Sent** to the Researcher — I'll post the findings when they're back."),
    # A file sent to the chat, after mail was read.
    (ToolStep("whatsapp_send_file", content="File sent to the WhatsApp chat."), "Sent ✅"),
    (ToolStep("whatsapp_send_file", content="File sent to the WhatsApp chat."),
     "**Sent** — the invoice from Ana's mail."),
])
def test_another_delivery_this_turn_owns_a_bare_sent(step: ToolStep, reply: str) -> None:
    request = ("send me the attachment from Ana's email" if step.name == "whatsapp_send_file"
               else "pošalji mi sliku mačke" if step.name in ("image_gen", "pocket_tts")
               else "move the sync with Ana to 3pm and let everyone know"
               if step.name == "google_calendar" else "have the Researcher look into Q3 pricing")
    assert not _judge(reply, _facts(request, turn=[step])).false


def test_after_nothing_but_mail_a_bare_sent_is_a_claim() -> None:
    # What the exemptions above turn off: recent mail makes a bare "Sent" an email.
    for reply in ("Moved to 15:00. **Sent** to Ana and Ivo.", "Evo, poslano! 🐱"):
        assert _judge(reply, _facts("ok, 10:30 is ok")).false, reply


def test_an_email_named_as_sent_after_a_chat_delivery_is_still_a_claim() -> None:
    step = ToolStep("whatsapp_send_file", content="File sent to the WhatsApp chat.")
    assert _judge("Sent ✅\n\nThe email to Ana was sent too.",
                  _facts("send me the deck", turn=[step])).false


_RELAYED = _step("consult", "Mailer replied: Email sent.\n  To: ana@x.co\n  Message ID: "
                            "18c2f3a4b5d6e7f1", tool="flight_deck")


def test_a_send_a_peer_relays_backs_the_claim() -> None:
    request = "Ask the Mailer agent to send the email to Ana confirming 10:30."
    for reply in ("Email sent to Ana via the Mailer agent — Message ID 18c2f3a4b5d6e7f1.",
                  "Email sent to Ana via the Mailer agent."):
        assert not _judge(reply, _facts(request, turn=[_RELAYED])).false, reply
    # An ID the relay did not name is still made up.
    reply = "Email sent — Message ID 18c2f3a4b5d6e7f9."
    verdict = fg.judge_reply(reply, reply, _facts(request, turn=[_RELAYED],
                                                  known=(_RELAYED.content,)))
    assert verdict.kinds == (fg.FABRICATED_ID,) and verdict.fabricated == ("18c2f3a4b5d6e7f9",)
    # A mail MCP reply that went out backs the claim.
    assert not _judge("Replied to Ana ✓", _facts(
        "Reply to Ana's email that 10:30 works and send it",
        turn=[ToolStep("mcp_gmail_reply", content="Reply sent.")])).false


_LANE_SENT = ("[Autonomous Work — from the AUTO lane]\nEmail sent. To: ana@x.co — Message ID "
              "18c2bb00cc11dd22")


def test_a_send_a_lane_note_relays_backs_the_claim() -> None:
    reply = "Yes — the reply to Ana went out, Message ID 18c2bb00cc11dd22."
    request = "did the reply to Ana go out?"
    assert not _judge(reply, _facts(request, relayed=(_LANE_SENT,))).false
    # Known, but not relayed as a send: no proof.
    assert _judge(reply, _facts(request, known=(_LANE_SENT,))).kinds == (fg.MAIL_CLAIM,)
    # A note that carries a backstop relays nothing.
    noted = _LANE_SENT + "\n\n" + fg.MAIL_BACKSTOP
    assert _judge(reply, _facts(request, relayed=(noted,))).false


def test_no_mail_tool_offered_means_no_mail_verdict() -> None:
    verdict = _judge("**Sent** to Ana.", _facts("send Ana the mail", mail_tools=frozenset()))
    assert not verdict.false and verdict.claim_lines == frozenset()


def test_the_claim_lines_are_the_email_lines() -> None:
    # The delegation check after the gate reads the reply without them.
    reply = ("**Sent** to Ana — Message ID m-1.\n\nPoslao sam zadatak Researcheru, čekam odgovor."
             "\nMail za Marka poslan jučer.")
    verdict = _judge(reply, _facts("pošalji Ani mail i proslijedi istraživanje Researcheru",
                                   turn=[_EARLIER_SEND]))
    assert not verdict.false and verdict.claim_lines == {0, 2, 3}


# ── Model-written lane notes ─────────────────────────────────────────


@pytest.mark.parametrize(
    "text, expected",
    [
        # a forged confirmation after the answer: header and its block go,
        # the rest of the reply stays
        (
            "**Sent to Tom** — topics.\n\n[Autonomous Work — from the AUTO lane]\n"
            "Sent — verified in Sent, ID `1a2b3c4d`.\n\nAnything else?",
            "**Sent to Tom** — topics.\n\nAnything else?",
        ),
        # a block that reports no action keeps its text, without the header
        (
            "**Sent to Tom** — topics.\n\n[Autonomous Work — from the AUTO lane]\n"
            "**Tom hasn't received them.**",
            "**Sent to Tom** — topics.\n\n**Tom hasn't received them.**",
        ),
        (
            "[Note from the AUTO lane — Autonomous Work: another run's result, not your reply]\n"
            "Heads-up: Marko replied.",
            "Heads-up: Marko replied.",
        ),
        # a relay the reply introduces keeps the note's body and what follows
        (
            "Here is what the AUTO lane posted:\n\n[Autonomous Work — from the AUTO lane]\n"
            "Marko replied: he agrees to 10:30.",
            "Here is what the AUTO lane posted:\n\nMarko replied: he agrees to 10:30.",
        ),
        (
            "Here's what it reported:\n\n[Note from the AUTO lane — Autonomous Work: another "
            "run's result, not your reply]\nMarko is waiting for your reply about the Q3 "
            "invoice.\n\nWant me to draft it?",
            "Here's what it reported:\n\nMarko is waiting for your reply about the Q3 "
            "invoice.\n\nWant me to draft it?",
        ),
        ("Traffic is slow in the fast lane on the A3.", None),
        ("Inline: [Autonomous Work — from the AUTO lane] is how notes start.", None),
        ("Photos sorted.\n\n[Highlights from the drive down Penny Lane]\n- the coast road", None),
    ],
)
def test_model_written_lane_notes_are_cut(text: str, expected: str | None) -> None:
    assert fg.strip_forged_lane_notes(text) == (text if expected is None else expected)


def test_a_lane_note_the_user_asked_about_is_kept() -> None:
    text = ("Here is what the AUTO lane posted:\n\n[Autonomous Work — from the AUTO lane]\n"
            "Marko replied: he agrees to 10:30.")
    assert fg.strip_forged_lane_notes(text, "what did the autonomous lane post?") == text


_OVERNIGHT = ("Overnight, two jobs reported back.\n\n[Morning digest — from the AUTO lane]\n"
              "Digest sent to you: 5 new emails, 1 needs a reply (Ana, contract).\n\n"
              "[Note from the cron lane — Backup check, another run's result, not your reply]\n"
              "Backup verified — 12 GB, no errors.")
_OVERNIGHT_NOTES = [
    "[Morning digest — from the AUTO lane]\nDigest sent to you: 5 new emails, 1 needs a reply "
    "(Ana, contract).",
    "[Backup check — from the cron lane]\nBackup verified —  12 GB, no errors.",
]


def test_a_real_lane_note_relayed_as_it_stands_keeps_its_text() -> None:
    assert fg.strip_forged_lane_notes(_OVERNIGHT, "anything new while I was out?",
                                      _OVERNIGHT_NOTES) == (
        "Overnight, two jobs reported back.\n\nDigest sent to you: 5 new emails, 1 needs a reply "
        "(Ana, contract).\n\nBackup verified — 12 GB, no errors.")
    # Without those notes, the blocks read as forged confirmations.
    assert fg.strip_forged_lane_notes(_OVERNIGHT, "anything new while I was out?") == (
        "Overnight, two jobs reported back.")
    # A made-up block is still cut next to a real one, and a short one that
    # only sits inside a longer real note proves nothing.
    forged = ("**Sent to Tom** — topics.\n\n[Autonomous Work — from the AUTO lane]\nSent — "
              "verified.\n\nAnything else?")
    notes = ["[Autonomous Work — from the AUTO lane]\nSent — verified. Tom has the topics since "
             "Monday."]
    assert fg.strip_forged_lane_notes(forged, "send Tom the topics", notes) == (
        "**Sent to Tom** — topics.\n\nAnything else?")


@pytest.mark.parametrize("request_text", [
    "what did the overnight jobs report?", "what did cron say?", "show me the morning digest",
    "što je stiglo noćas?", "what did the scheduled jobs find?",
])
def test_a_request_about_the_lanes_jobs_keeps_the_reply_whole(request_text: str) -> None:
    assert fg.strip_forged_lane_notes(_OVERNIGHT, request_text) == _OVERNIGHT


# ── Narrated lookups (a turn with no tool call) ──────────────────────
#
# Synthetic versions of the shapes found in production replies, English and
# Croatian, and the wordings that must stay quiet.

_NARRATED = [
    # progress lines
    "Searching for Luka's thread to ground the reply.\n\n**Sent to Luka** — asking for more detail.",
    "Looking for prior contact with Ana.\n\nNothing before — this is a first mail.",
    "Finding Tom's latest message to reply to.\n\n**Sent** — one-line ack.",
    "Checking for an existing reply first.\n\n**Sent** to Petra.",
    "Checking today's calendar.\n\n**Today: Ops 09:30 → IC 10:00.**",
    "Pulling the thread so the reply is grounded.\n\nHe asked for Friday.",
    "Reading the document, then sending.\n\nDone — reply is out.",
    "Let me find the plugin thread.\n\nFound it — he wrote on 30 Sep asking about pricing tiers.",
    "Let me check the inbox for his answer.\n\nHe replied this morning: he'll join at 10:30, remote.",
    "Let me open that event to see exactly what's booked.\n\nIt's tomorrow 19:50, Google Meet, "
    "with Nordwind.",
    "Verifying the invite went out.\n\nIt did — Ivo is on the guest list.",
    # past-tense claims
    "I checked drafts and sent mail — nothing to Luka yet.",
    "I've searched the inbox: no reply from Ana since Monday.",
    "Opened the thread (4 messages; his is the latest, Mon 12 Oct):\n- he asks for 30 min before "
    "the panel",
    "Re-confirmed from the calendar (nothing's changed since my last check): Fri 13:30 Acme.",
    "Verified — it did go this time. Thread 18c2e9f8a7b6c5d4.",
    "The sent-mail check returns it: your reply went out at 11:16.",
    "Double-checked the drafts: only three exist, none to Example Labs.",
    "I looked through the calendar for Friday — three meetings, back to back.",
    "Pulling up the thread now.\n\nHe replied Monday asking for Friday.",
    "Looking up his LinkedIn.\n\nHe's the CTO at Acme.",
    "Scanning your inbox for anything urgent.\n\nTwo items need you today.",
    "Fetching the latest from Gmail.\n\nNothing new since 09:00.",
    "Checked the calendar — Friday is open after 13:00.",
    "I double-checked the calendar: you're free at 15:00.",
    "I had a look at the inbox — nothing from Ivo yet.",
    "Ran a quick search of your mail: no thread with Luka.",
    # Croatian
    "Provjeravam inbox.\n\nNema novog odgovora od Ane.",
    "Pretražujem mailove od Luke.\n\nZadnji je od 30. rujna.",
    "Tražim nit s Markom.\n\nNašao sam je — 4 poruke.",
    "Provjerio sam kalendar — petak je slobodan od 13h.",
    "Pogledao sam inbox: nema ništa novo od Ive.",
    "Pretražio sam sve draftove — nema poruke za Orbit.",
    "Daj da provjerim kalendar za petak.\n\nPetak: 09:00 Ops, 10:00 banka, 13:30 Acme.",
    "Otvorio sam mail od Petre: potvrđuje 10:30.",
    "Gledam kalendar za petak.\n\nPetak je slobodan nakon 13h.",
    "Čitam njegov mail.\n\nTraži termin u petak.",
    "Otvaram nit s Petrom.\n\nPotvrdila je 10:30.",
    "Provjereno u inboxu — nema odgovora od Ive.",
    "Pronašao sam njegovu poruku u inboxu — traži termin u petak.",
    # a greeting without a sign-off is no drafted letter
    "Hej Ivane,\n\nProvjerio sam kalendar — petak je slobodan od 13h.",
    # a result verb after the gerund is a narration, not a remark
    "Checking the calendar shows you're free at 15:00.",
    "- Checking the calendar: Friday is open after 13:00.",
    # mail by its inflected forms and its mailbox's name
    "Provjeravam mailove.\n\nNema ništa novo od Ive.",
    "Pogledao sam mailove, ništa novo.",
    "Pretražio sam mailove od Luke: zadnji je od 30. rujna.",
    "Provjerio sam u mailu — Petra potvrđuje 10:30.",
    "Checked Gmail — nothing from Ivo.",
    "I checked your Outlook: nothing from Ivo.",
    "I checked your mailbox: nothing new.",
    "Provjerio sam sandučić — ništa novo.",
]
_NOT_NARRATED = [
    # offers, questions, plans
    "I can check the calendar for Friday if you want.",
    "Want me to look for his last email?",
    "Should I search the inbox for Luka's thread?",
    "I'll check the inbox and get back to you.",
    "Let me know if you want me to check the calendar.",
    "Let me check.",
    # a bare "let me" with nothing after it is a stall (the stall gate's)
    "Let me check the calendar for Friday.",
    "If you'd like, I can pull up the thread later.",
    # negations, retractions
    "I haven't checked the inbox yet — say the word and I'll look.",
    "I didn't actually check the calendar — that was a guess. Sorry, my mistake.",
    "My mistake — I never opened the thread. Opened the thread (4 messages) was made up; I don't "
    "know what he asked.",
    "Nisam provjerio inbox — mogu sad ako želiš.",
    # an earlier turn's check
    "I checked the thread earlier today; he hadn't replied then.",
    "Jutros sam provjerio kalendar — tada je petak bio slobodan.",
    # thinking, not looking
    "Checking your math: 3 nights × €120 = €360, so you're right.",
    "Double-checking the arithmetic — 15% of 2,000 is 300.",
    "Finding the right meeting slot is tricky this week; Tuesday is cleanest.",
    "Looking at your calendar, you have three meetings tomorrow.",
    "Looking forward to the panel — it should be a good one.",
    "Checking in: how did the bank meeting go?",
    "Pulling it all together: the deal is a pass.",
    "Reading between the lines, he wants a discount.",
    "Searching for the right word here — 'pivot' fits best.",
    # what the user put in the chat
    "Reading the notes you pasted, the key issue is the cap table.",
    "Checking the list you sent me: two names are duplicates.",
    "Pregledao sam tekst koji si mi poslao — dobar je, samo skrati uvod.",
    "I looked at the thread you forwarded — he wants Friday.",
    # someone else's words: a quote, a letter drafted for the user
    'He wrote: "I checked the calendar and Friday works."',
    "Draft for Ana:\n\nHi Ana,\n\nI checked my calendar and Friday 10:00 works for me. I've "
    "reviewed the deck too.\n\nBest,\nK",
    "Prijedlog:\n\nBok Ivo,\n\npogledao sam dokument i sve je u redu. Provjerio sam kalendar — "
    "petak može.\n\nLijep pozdrav,\nK",
    "> Searching for the thread now, will reply soon\n\nThat's Ana's last message — she hasn't "
    "replied since.",
    # routines, labels, headings, tables
    "- Checking the inbox daily at 09:00 for Ivo's reply.",
    "Watch is set: checking his thread every morning.",
    "**Finding:** the deck overstates TAM by 3x.",
    "| Finding | Detail |\n|---|---|\n| TAM | overstated |",
    "### Searching the web\nUse web_search for fresh data.",
    # plain answers
    "**White — malvazija, no debate.** Grilled fish wants a crisp white.",
    "Hej Ivane, hvala za deck. Kad uhvatiš vremena, spoji me s njima.",
    "Good — that clears the whole Friday worry.",
    "Opened in 2019, the restaurant is known for grilled fish.",
    "Verified facts only: the fund closed in 2021.",
    "That email went out on 7 Oct (I checked it then).",
    "Opening the doc requires access — share it with me first.",
    "Searching the web won't find this — it's internal.",
    "Checking the inbox is something I can do every morning if you want.",
    "Provjeravam još jednom: 3 × 4 = 12.",
    "Gledam tvoj kalendar, sutra imaš tri sastanka.",
    "Looking for steak, the coast isn't the place — go for fish.",
    "Pulling the trigger on this deal is your call.",
    "I read your message — I'll keep it shorter.",
    "I looked at the numbers you gave me: margin is 18%.",
    "I've reviewed the plan — go ahead.",
    "Confirmed — Friday 13:30 stays.",
    "Verified: you're right, the deadline is 15 Oct.",
    "Ako želiš, mogu provjeriti kalendar.",
    "Pogledat ću inbox kasnije.",
    "Nisam pogledao mail, ali mogu odmah.",
    "Pogledao sam tvoj prijedlog — super je.",
    "Reading your draft, it's solid — maybe shorten the intro.",
    "Got it — I'll reply to Ana once you confirm the time.",
    "You asked me to look for his email — I can't, Gmail isn't connected here.",
    "I had a look at your plan — it's solid.",
    "If you want, I can run a quick search of the inbox.",
    # how-to answers: step labels, a gerund as the subject
    "A few ways:\n- **Searching by sender**: type `from:ana@x.co` in the search box.\n- **Looking "
    "up by date**: add `before:2025/01/01`.\n- **Finding attachments**: add `has:attachment`.",
    "Searching Gmail works best with operators like `from:` and `has:attachment`.",
    "- Checking availability: click 'Find a time'.",
    "Searching by sender: type from:ana@x.co in the search box.",
    "Finding old threads lets you skip the scrolling — use `older_than:1y`.",
    "Searching by sender saves a lot of scrolling in a busy inbox.",
    "- **Searching by date**: Gmail understands `before:` and `after:`.",
]


@pytest.mark.parametrize("reply", _NARRATED)
def test_narrated_lookups_are_found(reply: str) -> None:
    assert fg.narrated_lookups(reply)


@pytest.mark.parametrize("reply", _NOT_NARRATED)
def test_offers_plans_and_thinking_are_not_narrated_lookups(reply: str) -> None:
    assert fg.narrated_lookups(reply) == ()


def test_a_narrated_lookup_names_its_sentence() -> None:
    reply = "Checking for an existing reply first.\n\n**Sent** to Petra."
    assert fg.narrated_lookups(reply) == ("Checking for an existing reply first.",)


def test_a_how_to_question_is_not_judged_for_narrated_lookups() -> None:
    reply = "Checking the inbox: open Gmail.\n\nSearching for a sender: from:ana@x.co."
    assert fg.narrated_lookups(reply)
    for request in ("how do I find old emails from Ana in Gmail?", "How to check my inbox?",
                    "kako da pretražim mailove?"):
        assert not fg.judge_reply(reply, reply, _facts(
            request, no_tool_call=True, tools=_TOOLS)).false, request
    assert fg.judge_reply(reply, reply, _facts(
        "any mail from Ana?", no_tool_call=True, tools=_TOOLS)).narrated


# ── Made-up IDs ──────────────────────────────────────────────────────

# What the tools showed (synthetic IDs of the real shapes).
_KNOWN = (
    "Search results (2 threads):\n  ID: 18c2f4a6b8d0e1f3  |  Thread: 18c2a0b1c2d3e4f5\n"
    "Draft created.\n  Draft ID: r7031164207550018842\n  Message ID: 18c2bb00cc11dd22\n"
    "Event: Acme sync\n  ID: q3vb81kd4m5n6p7r8s9t0u1v2a\n"
    "[Google Doc: IC notes — file id 1Qw3Er5Ty7Ui9Op1As3Df5Gh7Jk9Lz2Xc4Vb6Nm8Pq]\n"
)
_MADE_UP = [
    ("**Sent** — ID `18c2ff00ee11dd22`.", ("18c2ff00ee11dd22",)),
    ("Threaded on his mail (thread 18c2e9f8a7b6c5d4).", ("18c2e9f8a7b6c5d4",)),
    ("Draft ready: r1234567890123456789.", ("r1234567890123456789",)),
    ("Opened the event (`_c4n6p8r0s2t4v6a8c0e2g4i6k8m0o2q4`).",
     ("_c4n6p8r0s2t4v6a8c0e2g4i6k8m0o2q4",)),
    ("Folder: https://drive.google.com/drive/folders/1Zx8Cv7-Bn6Mm5Ll4Kk3Jj2-Hh1Gg0Ff9",
     ("1Zx8Cv7-Bn6Mm5Ll4Kk3Jj2-Hh1Gg0Ff9",)),
    ("Event k2m4o6q8s0u2a4c6e8g0i2_20261014T080000Z moved.",
     ("k2m4o6q8s0u2a4c6e8g0i2_20261014T080000Z",)),
    ("Outlook id AAMkAGI2THVSAAA1234567890abcdefGHIJKLmnopQRSTuv=",
     ("AAMkAGI2THVSAAA1234567890abcdefGHIJKLmnopQRSTuv=",)),
]
_NOT_MADE_UP = [
    "**Sent** — ID `18c2bb00cc11dd22`, to Tom.",                     # a tool returned it
    "Reply in thread 18C2A0B1C2D3E4F5.",                              # case differs only
    "Draft r7031164207550018842 is still blank.",
    "Doc: https://docs.google.com/document/d/1Qw3Er5Ty7Ui9Op1As3Df5Gh7Jk9Lz2Xc4Vb6Nm8Pq/edit",
    "My earlier \"ID `18c2ab12cd34ef56`\" was fabricated — nothing was sent.",  # a retraction
    "No `18c2e9f8a7b6c5d4`. Nothing sent.",
    "Meeting at 2026-10-16 13:30, room 4B, budget €12,500.",          # no ID shape
    "File: IC-Agenda-2026-10-13-final-version.docx",                  # a file name
    "Commit deadbeef in branch fix/mail.",                            # too short
    "UUID 3fd06085-1234-4abc-9def-0123456789ab",
]


@pytest.mark.parametrize("reply, expected", _MADE_UP)
def test_an_id_no_tool_returned_is_made_up(reply: str, expected: tuple[str, ...]) -> None:
    assert fg.fabricated_ids(reply, [_KNOWN]) == expected


@pytest.mark.parametrize("reply", _NOT_MADE_UP)
def test_known_ids_retractions_and_non_ids_are_not_made_up(reply: str) -> None:
    assert fg.fabricated_ids(reply, [_KNOWN]) == ()


def test_an_id_counts_as_known_in_any_given_text() -> None:
    reply = "The sync (event q3vb81kd4m5n6p7r8s9t0u1v2a) is at 15:00."
    assert fg.fabricated_ids(reply, ["nothing here"]) == ("q3vb81kd4m5n6p7r8s9t0u1v2a",)
    assert fg.fabricated_ids(reply, ["nothing here", "ID: q3vb81kd4m5n6p7r8s9t0u1v2a"]) == ()


def test_ids_in_example_code_or_made_up_on_request_are_not_judged() -> None:
    sample = "```python\nsheet_id = '1Zx8Cv7-Bn6Mm5Ll4Kk3Jj2-Hh1Gg0Ff9'\n```\nRun it with your own ID."
    assert fg.fabricated_ids(sample, [_KNOWN]) == ()
    token = "Here you go: `q8Zr2LmN0vX7pT4kW9bY3cH6jF1sD5gA`"
    assert fg.fabricated_ids(token, [_KNOWN]) == ("q8Zr2LmN0vX7pT4kW9bY3cH6jF1sD5gA",)
    assert fg.fabricated_ids(token, [_KNOWN], "generate a random 32-char token") == ()
    assert fg.fabricated_ids(token, [_KNOWN], "napravi nasumičnu lozinku") == ()


# ── The ID ledger ────────────────────────────────────────────────────


def _tool_msg(content: str, tool: str = "google_calendar", **args) -> dict:
    return {"role": "tool", "content": content, "tool_name": tool, "tool_call_id": "c1",
            "tool_arguments": args, "origin": "tool"}


def test_the_ledger_keeps_the_ids_a_tool_result_showed() -> None:
    ledger = fg.ledger_with(None, _tool_msg("Event: Acme sync\n  ID: q3vb81kd4m5n6p7r8s9t0u1v2a"))
    assert ledger == {"ids": ["q3vb81kd4m5n6p7r8s9t0u1v2a"], "sent": []}
    assert "q3vb81kd4m5n6p7r8s9t0u1v2a" in fg.ledger_text(ledger)
    # Nothing new: no new ledger.
    assert fg.ledger_with(ledger, _tool_msg("ID: q3vb81kd4m5n6p7r8s9t0u1v2a")) is None
    ledger = fg.ledger_with(ledger, _tool_msg("Draft created.\n  Message ID: m-8", "google_mail",
                                              action="create_draft"))
    assert ledger["ids"] == ["q3vb81kd4m5n6p7r8s9t0u1v2a", "m-8"]


def test_the_ledger_keeps_what_sends_returned_apart() -> None:
    ledger = fg.ledger_with(None, _tool_msg(_SENT, "google_mail", action="send"))
    assert fg.ledger_sent_ids(ledger) == {"m-1"}
    assert "t-1" in fg.ledger_text(ledger)
    drafted = fg.ledger_with(None, _tool_msg("Draft created.\n  Message ID: m-8", "google_mail",
                                             action="create_draft"))
    assert fg.ledger_sent_ids(drafted) == frozenset()


@pytest.mark.parametrize("msg, kept", [
    ({"role": "user", "content": "the doc is 1Qw3Er5Ty7Ui9Op1As3Df5Gh7Jk9Lz2Xc4Vb6Nm8Pq",
      "origin": "human"}, True),
    ({"role": "assistant", "content": "[lane] event q3vb81kd4m5n6p7r8s9t0u1v2a moved",
      "origin": "system_note"}, True),
    # the model's own words, a corrective quoting a made-up ID back, a monitor row
    ({"role": "assistant", "content": "Opened event q3vb81kd4m5n6p7r8s9t0u1v2a.",
      "origin": "model"}, False),
    ({"role": "user", "content": "Your reply quotes `18c2ff00ee11dd22` — no tool returned it.",
      "origin": "corrective"}, False),
    ({"role": "tool", "content": "trace 18c2ff00ee11dd22", "tool_name": "llm_trace"}, False),
])
def test_the_ledger_takes_only_what_the_model_was_given(msg: dict, kept: bool) -> None:
    assert (fg.ledger_with(None, msg) is not None) is kept


def test_the_ledger_is_bounded() -> None:
    page = "\n".join(f"ID: {n:016x}a" for n in range(150))
    ledger = fg.ledger_with(None, _tool_msg(page, "web_fetch"))
    assert len(ledger["ids"]) == fg._LEDGER_PER_MESSAGE
    for start in range(0, 2000, 100):
        more = "\n".join(f"ID: {n:015x}b" for n in range(start, start + 100))
        ledger = fg.ledger_with(ledger, _tool_msg(more, "web_fetch"))
    assert len(ledger["ids"]) == fg._LEDGER_IDS
    assert ledger["ids"][-1] == f"{1999:015x}b"


def test_the_ledger_keeps_what_a_relayed_send_returned() -> None:
    lane = {"role": "assistant", "content": _LANE_SENT, "origin": "system_note"}
    assert fg.ledger_sent_ids(fg.ledger_with(None, lane)) == {"18c2bb00cc11dd22"}
    delegated = {"role": "user", "origin": "delegated_result",
                 "content": "[Delegated result from Mailer]\nEmail sent. Message ID: m-31"}
    assert fg.ledger_sent_ids(fg.ledger_with(None, delegated)) == {"m-31"}
    peer = _tool_msg("Mailer replied: Email sent.\n  Message ID: m-32", "flight_deck",
                     action="consult")
    assert fg.ledger_sent_ids(fg.ledger_with(None, peer)) == {"m-32"}
    # A person's message saying so is not another run's result.
    said = {"role": "user", "origin": "human", "content": "Email sent. Message ID: m-33"}
    assert fg.ledger_sent_ids(fg.ledger_with(None, said)) == frozenset()


_MADE = "18c2e9f8a7b6c5d4"


def _echo(query: str) -> dict:
    return _tool_msg(f"No threads found for: {query}", "google_mail", action="search", query=query)


def test_a_tool_naming_the_calls_own_id_back_proves_nothing() -> None:
    # The model searched for the ID it made up; the tool's "No threads found
    # for: <query>" holds it, as the model's word.
    rows = [_echo(f"in:sent {_MADE}")]
    steps = fg.tool_steps(rows)
    assert _MADE not in steps[0].content and steps[0].content.startswith("No threads found for:")
    assert fg.ledger_with(None, rows[0]) is None
    assert _MADE not in fg.outside_text(rows[0])
    for reply in (f"It already went out — thread `{_MADE}`.",
                  f"Yes, I replied to Luka — thread `{_MADE}`."):
        verdict = fg.judge_reply(reply, reply, _facts(
            "did you reply to Luka?", turn=steps, known=(fg.outside_text(rows[0]),)))
        assert verdict.fabricated == (_MADE,), reply
    # Errors name the arguments back too.
    error = _tool_msg(f"Error: Failed to load reply_to_message_id={_MADE}: 404", "google_mail",
                      action="create_draft", reply_to_message_id=_MADE)
    assert _MADE not in fg.outside_text(error) and fg.ledger_with(None, error) is None
    error = _tool_msg(f"Error: Gmail API returned 400 for message_id={_MADE}.", "google_mail",
                      action="read_message", message_id=_MADE)
    assert fg.ledger_with(None, error) is None
    # An ID of any shape the call passed as an "…_id" argument.
    missing = _tool_msg("Error: Message ID m-made-9 not found.", "google_mail",
                        action="read_message", message_id="m-made-9")
    assert fg.ledger_with(None, missing) is None
    assert fg.tool_steps([missing])[0].content == "Error: Message ID … not found."
    # A result that answers with what was asked for does show it.
    thread = _tool_msg(f"Thread {_MADE} (2 messages)\n  ID: 18c2e9f8a7b6c5d5  |  From: luka@x.co",
                       "google_mail", action="get_thread", thread_id=_MADE)
    assert fg.ledger_with(None, thread)["ids"] == [_MADE, "18c2e9f8a7b6c5d5"]
    results = _tool_msg(f"Search results for 'from:luka' (1):\n  ID: {_MADE}  |  Thread: t-9",
                        "google_mail", action="search", query=f"from:luka {_MADE}")
    assert _MADE in fg.outside_text(results).split("\n", 1)[1]


_DIGEST = (
    "Conversation summary of earlier messages (compacted memory):\n"
    "Digest of the earlier part of this conversation (folded by compaction; …):\n"
    "Before that: By topic: - Mail [mail] — 1 exchange(s), 2026-10-01 last asked: \"which thread?\" "
    "last reply: \"Ana's thread is 18c2e9f8a7b6c5d1 — she agreed\" Other exchanges: - 2026-10-02 "
    "\"and Marko?\" → \"His is 18c2e9f8a7b6c5d2.\" - 2026-10-03 \"ok\" Also folded: 4 tool result(s).\n"
    "By topic:\n- Mail [mail] — 2 exchange(s), 2026-10-12\n  last asked: \"did Ana reply?\"\n"
    "  last reply: \"Ana replied on Monday — thread `18c2e9f8a7b6c5d4` (she said \"ok\")\"\n"
    "Other exchanges:\n- 2026-10-12 \"when?\" → \"Thread 18c2e9f8a7b6c5d3, Friday.\"\n"
    "Also folded: 3 tool result(s); event q3vb81kd4m5n6p7r8s9t0u1v2a kept."
)


def test_a_compaction_summary_never_vouches_for_the_models_own_replies() -> None:
    # The digest quotes the model's replies; their IDs are its words.
    summary = {"role": "assistant", "content": _DIGEST, "tool_name": "compaction_summary",
               "origin": "system_note", "origin_detail": "compaction"}
    text = fg.outside_text(summary)
    for made in ("18c2e9f8a7b6c5d1", "18c2e9f8a7b6c5d2", "18c2e9f8a7b6c5d3", "18c2e9f8a7b6c5d4"):
        assert made not in text
        assert fg.fabricated_ids(f"The thread is `{made}`.", [text]) == (made,)
    # The rest stays: what was asked, what else the summary says.
    assert 'last asked: "did Ana reply?"' in text and "q3vb81kd4m5n6p7r8s9t0u1v2a" in text
    assert "Also folded: 4 tool result(s)." in text
    assert fg.ledger_with(None, summary)["ids"] == ["q3vb81kd4m5n6p7r8s9t0u1v2a"]
    # A fallback summary lists the model's replies as "- assistant: …".
    fallback = ("Conversation summary of earlier messages (compacted memory):\nKey points from earlier "
                f"conversation:\n- user: which thread?\n- assistant: It is {_MADE}.\n- tool: ID: "
                "18c2e9f8a7b6c5d5")
    text = fg.without_digest_replies(fallback)
    assert _MADE not in text and "18c2e9f8a7b6c5d5" in text
    flat = ("Before that: Key points from earlier conversation: - user: which? - assistant: It is "
            f"{_MADE}. - tool: ID: 18c2e9f8a7b6c5d5")
    text = fg.without_digest_replies(flat)
    assert _MADE not in text and "- user: which?" in text and "- tool: ID: 18c2e9f8a7b6c5d5" in text


def test_the_ledger_is_kept_for_every_writer() -> None:
    # A lane note, a notification or a delegated result added straight to the
    # session is taken in too; a corrective is not.
    from captain_claw.session import Session

    session = Session(id="s1", name="t")
    session.add_message("assistant", "[Autonomous Work — from the AUTO lane]\nSent to Ana — Message "
                        "ID 18c2bb00cc11dd22", origin="system_note", origin_detail="automation_result")
    session.add_message("user", "Your reply quotes `18c2ff00ee11dd22` — invented.", origin="corrective")
    assert session.metadata[fg.ID_LEDGER_KEY]["ids"] == ["18c2bb00cc11dd22"]


# ── One decision for a reply ─────────────────────────────────────────


def test_a_narrated_lookup_with_no_tool_call_is_false() -> None:
    reply = "Checking today's calendar.\n\n**Today: Ops 09:30 → IC 10:00.**"
    verdict = fg.judge_reply(reply, reply, _facts(known=(), no_tool_call=True, tools=_TOOLS))
    assert verdict.kinds == (fg.NARRATED_LOOKUP,) and verdict.origin_detail == "narrated_lookup"
    assert verdict.corrective == (
        "Your reply describes lookups (\"Checking today's calendar.\") but you made no tool call in "
        "this turn, so none of them happened. If you need that information, look it up now "
        "(google_mail search / read_message, google_calendar list_events, web_search) and report "
        "only what it returns; otherwise answer without describing any search, check or lookup, "
        "and say plainly what you don't know.")
    assert verdict.backstop == fg.LOOKUP_BACKSTOP
    # A turn that made a tool call is not judged for narration.
    assert not fg.judge_reply(reply, reply, _facts(known=(), tools=_TOOLS)).false


def test_a_made_up_id_alone_gets_the_id_corrective_and_backstop() -> None:
    reply = "The sync (event q3vb81kd4m5n6p7r8s9t0u1v2a) is at 15:00."
    verdict = fg.judge_reply(reply, reply, _facts(known=("nothing",), tools=_TOOLS))
    assert verdict.kinds == (fg.FABRICATED_ID,) and verdict.origin_detail == "fabricated_id"
    assert verdict.corrective == (
        "Your reply quotes `q3vb81kd4m5n6p7r8s9t0u1v2a` — no tool in this conversation returned "
        "that ID, so it is invented. Leave it out, or look it up now and quote only what the tool "
        "returns.")
    assert verdict.backstop == "Note: `q3vb81kd4m5n6p7r8s9t0u1v2a` was not returned by any tool."
    assert fg.has_backstop(verdict.backstop)
    assert not fg.judge_reply(reply, reply, _facts(known=(_KNOWN,), tools=_TOOLS)).false
    # known=None: no ID check.
    assert not fg.judge_reply(reply, reply, _facts(tools=_TOOLS)).false


def test_a_made_up_id_only_in_the_cut_lane_note_is_named_but_not_backstopped() -> None:
    raw = ("Asked Luka for more detail on the plugins.\n\n[Autonomous Work — from the AUTO lane]\n"
           "Sent — verified, thread 18c2e9f8a7b6c5d4.")
    shipped = fg.strip_forged_lane_notes(raw)
    assert "18c2e9f8a7b6c5d4" not in shipped
    verdict = fg.judge_reply(raw, shipped, _facts(known=(_KNOWN,), tools=_TOOLS))
    assert verdict.fabricated == ("18c2e9f8a7b6c5d4",)
    assert "`18c2e9f8a7b6c5d4`" in verdict.corrective
    # Nothing ships that it would point at.
    assert verdict.backstop == ""


def test_every_kind_found_goes_into_one_corrective() -> None:
    # The production shape: no tool call, narrated searches, a made-up thread
    # ID, "Sent", plus a made-up event ID.
    request = "send Luka a reply asking him to write more about the plugins"
    reply = ("Searching for Luka's thread to ground the reply.\n\n**Sent to Luka** — asking for "
             "more detail on the plugins (thread `18c2e9f8a7b6c5d4`).\n\nBlocked "
             "k2m4o6q8s0u2a4c6e8g0i2 for the follow-up.")
    verdict = fg.judge_reply(reply, reply, _facts(request, known=(_KNOWN,), no_tool_call=True,
                                                  tools=_TOOLS))
    assert verdict.kinds == (fg.MAIL_CLAIM, fg.NARRATED_LOOKUP, fg.FABRICATED_ID)
    assert verdict.origin_detail == "false_mail_send"
    assert verdict.corrective == fg.mail_corrective() + (
        " Also: your reply describes lookups (\"Searching for Luka's thread to ground the "
        "reply.\") that never ran — no tool was called in this turn."
        " Also: no tool returned `18c2e9f8a7b6c5d4` and `k2m4o6q8s0u2a4c6e8g0i2` — leave them out.")
    assert verdict.backstop.split("\n") == [
        fg.MAIL_BACKSTOP, fg.LOOKUP_BACKSTOP,
        "Note: `18c2e9f8a7b6c5d4` and `k2m4o6q8s0u2a4c6e8g0i2` were not returned by any tool."]
    assert all(fg.has_backstop(line) for line in verdict.backstop.split("\n"))
    _assert_no_send_invite(verdict.corrective)


def test_a_correct_reply_has_no_verdict() -> None:
    reply = "The structure works — lead with the problem, then traction."
    verdict = fg.judge_reply(reply, reply, _facts(known=(_KNOWN,), no_tool_call=True, tools=_TOOLS))
    assert not verdict.false and verdict.corrective == "" and verdict.backstop == ""


def test_monitor_rows_are_no_tool_steps() -> None:
    # An llm_trace row repeats the model's reply: never a tool's answer.
    trace = {"role": "tool", "tool_name": "llm_trace",
             "content": "[assistant_response]\n**Sent** — ID `18c2ff00ee11dd22`."}
    result = {"role": "tool", "tool_name": "google_mail", "content": "ok",
              "tool_arguments": {"action": "search"}}
    assert fg.tool_steps([trace, result]) == (ToolStep("google_mail", "search",
                                                       {"action": "search"}, "ok"),)
