"""Topic auto-reply UI logic (Connections -> Google), run in Node.

The Flight Deck frontend has no JS test runner, so — like test_gmail_send_ui.py
— these tests lift the named top-level declarations out of the TypeScript
source with the TypeScript compiler from flight-deck/node_modules, transpile
them and run them in a Node vm. The store's actions run against a stub zustand
``create`` and a stub ``fetchJson`` (no network). The topic checks are also
held against the server's own (gmail_autoreply_config), so the inline errors
and Save never disagree with what the PUT takes. They skip when Node or the
frontend dependencies aren't installed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from captain_claw.flight_deck import gmail_autoreply_config as arcfg

_FD = Path(__file__).resolve().parents[2] / "flight-deck"
_STORE = _FD / "src" / "stores" / "gmailAutoReplyStore.ts"
_GOOGLE_STORE = _FD / "src" / "stores" / "googleAuthStore.ts"
_NOTIFY = _FD / "src" / "stores" / "notificationStore.ts"
_SECTION = _FD / "src" / "components" / "connections" / "GmailAutoReplySection.tsx"
_CARD = _FD / "src" / "components" / "connections" / "GoogleConnection.tsx"
_TS = _FD / "node_modules" / "typescript"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not _TS.is_dir(),
    reason="needs node and flight-deck/node_modules (npm install)",
)

# argv: typescript dir. stdin: {"parts": [{"src", "names"}], "prelude": js,
# "body": js}. Runs prelude + the named declarations (functions, classes and
# const/let statements, in source order per file; "f()" names a top-level call
# of f) + body in a vm; body sets globalThis.out, and may set globalThis.done
# to a promise to wait for.
_RUNNER = r"""
const ts = require(process.argv[1]);
const fs = require('fs');
const vm = require('vm');
const req = JSON.parse(fs.readFileSync(0, 'utf8'));
const parts = [];
for (const part of req.parts) {
  const kind = part.src.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const sf = ts.createSourceFile(part.src, fs.readFileSync(part.src, 'utf8'),
    ts.ScriptTarget.Latest, true, kind);
  const found = new Set();
  for (const st of sf.statements) {
    let declared = [];
    if (st.name) declared = [st.name.text];
    else if (ts.isVariableStatement(st)) {
      declared = st.declarationList.declarations.map((d) => d.name.getText(sf));
    } else if (ts.isExpressionStatement(st) && ts.isCallExpression(st.expression)) {
      declared = [st.expression.expression.getText(sf) + '()'];
    }
    if (declared.some((n) => part.names.includes(n))) {
      parts.push(st.getText(sf));
      declared.forEach((n) => found.add(n));
    }
  }
  const missing = part.names.filter((n) => !found.has(n));
  if (missing.length) throw new Error('declarations not found: ' + missing.join(', '));
}
const js = ts.transpileModule(
  (req.prelude || '') + '\n' + parts.join('\n') + '\n' + req.body, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const ctx = vm.createContext({ exports: {}, out: null, done: null });
vm.runInContext(js, ctx);
Promise.resolve(ctx.done).then(
  () => process.stdout.write(JSON.stringify(ctx.out)),
  (err) => { process.stderr.write(String(err && err.stack || err)); process.exit(1); },
);
"""


def _run(parts: list[tuple[Path, list[str]]], body: str, prelude: str = ""):
    proc = subprocess.run(
        ["node", "-e", _RUNNER, str(_TS)],
        input=json.dumps({
            "parts": [{"src": str(src), "names": names} for src, names in parts],
            "prelude": prelude, "body": body,
        }),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# What each helper needs lifted with it.
_REQUIREMENTS = ["AUTO_REPLY_REQUIREMENTS", "requirementText", "autoReplyOptInLocked"]
_LINT = [
    "AUTO_REPLY_LIMITS", "oneLine", "multiLine", "charCount", "FOLD_EXTRA", "foldText",
    "BROAD_FIRST_WORDS", "BROAD_PHRASE_RE", "EMAIL_WORD_RE", "FIRST_WORD_RE", "WORD_RE",
    "lintText", "lintWords", "NO_EXAMPLE", "lintTopic",
]
_VALIDATE = _LINT + [
    "lengthError", "SENDER_ADDRESS_RE", "SENDER_DOMAIN_RE", "normalizeSenders",
    "normalizeFolder", "exampleErrors", "validateTopic",
]
_ERRORS = [(_GOOGLE_STORE, ["HttpError", "_gmailSendError"]),
           (_STORE, ["AUTO_REPLY_STALE", "autoReplyError"])]


# ── autoReplyOptInLocked: only the turn-on direction, saying what's missing ──


def _view(enabled: bool, missing: list[str], missing_scopes: list[str] | None = None) -> dict:
    return {"enabled": enabled, "status": {"missing": missing,
                                           "missing_scopes": missing_scopes or []}}


def _locked(view) -> list[str]:
    return _run([(_STORE, _REQUIREMENTS)], f"out = autoReplyOptInLocked({json.dumps(view)});")


def test_opt_in_lock_lists_every_unmet_requirement_in_the_servers_order():
    out = _locked(_view(False, ["google_not_connected", "no_signature", "no_agent", "no_topics"]))
    assert out == [
        "Connect Google.",
        "Set your agentic signature under Email sending — every auto-reply ends with it.",
        "Choose the agent that writes the replies.",
        "Turn on at least one topic.",
    ]


def test_opt_in_lock_never_stops_turning_off():
    # On, with requirements missing (the deck switch went off since): the
    # owner must still be able to turn it off.
    assert _locked(_view(True, ["deck_disabled", "no_agent"])) == []
    assert _locked(None) == []


def test_opt_in_lock_is_open_when_nothing_is_missing():
    assert _locked(_view(False, [])) == []
    # A view without the list (it can't say) locks nothing — the server
    # still refuses with a 409 that says what's missing.
    assert _locked({"enabled": False, "status": {}}) == []


def test_opt_in_lock_names_the_missing_scopes_and_the_deck_switches():
    out = _locked(_view(False, ["deck_disabled", "send_deck_disabled", "missing_scopes"],
                        ["gmail.readonly", "gmail.send"]))
    assert "FD_GMAIL_AUTOREPLY=off" in out[0]
    assert "FD_GMAIL_SEND=off" in out[1]
    assert out[2].startswith("Google must allow gmail.readonly and gmail.send")
    # Without the scope list, the generic words.
    assert _locked(_view(False, ["missing_scopes"]))[0].startswith(
        "Google must allow reading and sending mail")


def test_opt_in_lock_knows_every_server_requirement_and_passes_unknown_codes():
    from captain_claw.flight_deck import gmail_autoreply_routes as routes

    codes = list(routes.REQUIREMENTS)
    out = _locked(_view(False, codes + ["brand_new_code", "no_agent"]))
    # One readable sentence per server code (none falls back to the code),
    # repeats dropped, an unknown code shown as itself.
    assert len(out) == len(codes) + 1
    assert not set(codes) & set(out)
    assert out[-1] == "brand_new_code"


_CHECKBOX_DISABLED = r"""
const ts = require(process.argv[1]);
const fs = require('fs');
const src = process.argv[2];
const sf = ts.createSourceFile(src, fs.readFileSync(src, 'utf8'),
  ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const found = [];
(function walk(n) {
  if (ts.isJsxSelfClosingElement(n) && n.tagName.getText(sf) === 'input') {
    const attr = (name) => n.attributes.properties.find(
      (p) => ts.isJsxAttribute(p) && p.name.getText(sf) === name);
    const change = attr('onChange');
    if (change && change.getText(sf).includes('setEnabled(')) {
      found.push(attr('disabled').initializer.expression.getText(sf));
    }
  }
  ts.forEachChild(n, walk);
})(sf);
process.stdout.write(JSON.stringify(found));
"""


def test_the_switch_lets_an_opted_in_user_turn_off_whatever_is_missing():
    proc = subprocess.run(
        ["node", "-e", _CHECKBOX_DISABLED, str(_TS), str(_SECTION)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    exprs = json.loads(proc.stdout)
    assert len(exprs) == 1, exprs
    expr = exprs[0]

    def disabled(saved: bool, missing: list[str], saving: bool, ticked: bool | None = None) -> bool:
        # ticked: the box as the user left it (unsaved); the saved value by default.
        return _run([(_STORE, _REQUIREMENTS)], (
            f"const view = {json.dumps(_view(saved, missing))};"
            f" const saving = {json.dumps(saving)};"
            f" const enabled = {json.dumps(saved if ticked is None else ticked)};"
            " const optInLocked = autoReplyOptInLocked(view);"
            f" out = !!({expr});"
        ))

    assert disabled(saved=True, missing=["deck_disabled"], saving=False) is False
    assert disabled(saved=False, missing=["no_agent"], saving=False) is True
    assert disabled(saved=False, missing=[], saving=False) is False
    assert disabled(saved=True, missing=[], saving=True) is True
    # Ticked before a refused turn-on showed what's missing: it can be unticked.
    assert disabled(saved=False, missing=["no_agent"], saving=False, ticked=True) is False


# ── validateTopic / lintTopic: the server's lint, word for word ─────────────


def _topic(**over) -> dict:
    t = {
        "name": "Meetup logistics",
        "enabled": False,
        "about": "Emails asking when or where the Zagreb AI Meetup on 14 Nov is, or how to register.",
        "not_this": "Sponsorship, speaking slots, invoices.",
        "examples": ["Where is the meetup on the 14th?"],
        "counter_examples": ["Can I speak at the meetup?"],
        "senders": [],
        "brief": "The meetup is on 14 Nov at 18:00 at the Hub, Ilica 1, Zagreb. Registration is free.",
        "sources": {"folder": "", "insights": False, "playbooks": True, "web_search": False},
    }
    t.update(over)
    return t


def _validate(topics: list[dict]) -> list[dict]:
    return _run([(_STORE, _VALIDATE)], f"out = {json.dumps(topics)}.map(validateTopic);")


def test_a_narrow_topic_has_no_errors():
    assert _validate([_topic()]) == [{}]


def test_each_field_reports_its_own_error():
    (errors,) = _validate([_topic(
        name="ab",
        about="Meetup questions",
        not_this="x" * 401,
        examples=["short"],
        counter_examples=["y" * 301],
        senders=["alice@example.com", "not an address", "@nope"],
        brief="too short",
        sources={"folder": "vfs:meetup/../../etc", "insights": False, "playbooks": True,
                 "web_search": False},
    )])
    assert errors == {
        "name": "The name must be 3 to 60 characters (got 2).",
        "about": "The description must be 20 to 400 characters (got 16).",
        "not_this": '"Not this" can be at most 400 characters (got 401).',
        "examples": "Each example must be 10 to 300 characters (got 5).",
        "counter_examples": "Each near-miss example must be 10 to 300 characters (got 301).",
        "senders": ("Not an address or a domain: not an address, @nope. "
                    "Use alice@example.com or @example.com."),
        "brief": "What the agent may say must be 50 to 4000 characters (got 9).",
        "folder": "Choose a folder in your files (vfs:<project>/<folder>).",
    }


def test_counts_and_required_examples():
    (none, too_many, counters) = _validate([
        _topic(examples=["", "   "]),
        _topic(examples=[f"Where is meetup number {i}?" for i in range(6)]),
        _topic(counter_examples=[f"Can I sponsor meetup {i}?" for i in range(6)]),
    ])
    assert none == {"examples": "Too broad: add at least one example of an email that should match."}
    assert too_many == {"examples": "At most 5 examples (got 6)."}
    assert counters == {"counter_examples": "At most 5 near-miss examples (got 6)."}


@pytest.mark.parametrize(("over", "field", "text"), [
    ({"name": "Anything else"}, "name", 'starts with "anything"'),
    ({"name": "General inquiries"}, "name", 'starts with "general"'),
    ({"name": "Općenito pitanja"}, "name", 'starts with "opcenito"'),
    ({"name": "SVE o meetupu"}, "name", 'starts with "sve"'),
    ({"about": "Reply to all e-mails about the meetup on 14 Nov please"}, "about",
     'says "all emails"'),
    ({"about": "Odgovori na sve mailove o meetupu u Zagrebu molim"}, "about",
     'says "sve mailove"'),
    ({"about": "Pitanja o meetupu, bilo što vezano uz registraciju"}, "about",
     'says "bilo sto"'),
    ({"about": "Questions about the meetup and anything else people ask"}, "about",
     'says "anything else"'),
    ({"about": "Where is the meetup held"}, "about", "at least 6 words"),
])
def test_broad_topics_are_flagged_on_the_right_field(over, field, text):
    (errors,) = _validate([_topic(**over)])
    assert list(errors) == [field]
    assert errors[field].startswith("Too broad:")
    assert text in errors[field]


# Names, descriptions and examples the lint must judge exactly as the server.
_LINT_CASES = [
    ("Meetup logistics", "Emails asking when or where the Zagreb AI Meetup on 14 Nov is.", ["Where?"]),
    ("Anything else", "Emails asking when or where the Zagreb AI Meetup on 14 Nov is.", ["x"]),
    ("all-hands logistics", "Emails asking when the all-hands meeting on Friday starts", ["x"]),
    ("Everything", "Emails asking when or where the meetup is held this year", ["x"]),
    ("Misc.", "Emails asking when or where the meetup is held this year", ["x"]),
    ("Other stuff", "Emails asking when or where the meetup is held this year", ["x"]),
    ("whatever", "Emails asking when or where the meetup is held this year", ["x"]),
    ("Opcenito", "Emails asking when or where the meetup is held this year", ["x"]),
    ("Općenito", "Emails asking when or where the meetup is held this year", ["x"]),
    ("Bilo što", "Emails asking when or where the meetup is held this year", ["x"]),
    ("Svi upiti", "Emails asking when or where the meetup is held this year", ["x"]),
    ("Meetup", "Any email about the meetup or the venue or the date", ["x"]),
    ("Meetup", "Questions: any question about the meetup venue or date", ["x"]),
    ("Meetup", "All emails about the meetup venue, the date and the time", ["x"]),
    ("Meetup", "Meetup venue and date; also general inquiries from guests", ["x"]),
    ("Meetup", "Meetup venue and date; also general inquiry from guests", ["x"]),
    ("Meetup", "Meetup venue and date, plus everything else guests ask", ["x"]),
    ("Meetup", "Odgovori na svim mailovima o meetupu i registraciji", ["x"]),
    ("Meetup", "Sva pitanja o meetupu, mjestu i datumu održavanja", ["x"]),
    ("Meetup", "Pitanja o meetupu: bilo sto vezano uz registraciju", ["x"]),
    ("Meetup", "Email-ovi o meetupu, all e mails about registration", ["x"]),
    ("Meetup", "Emails about the meetup's venue", ["x"]),
    ("Meetup", "Emails about the meetup venue and the date", []),
    ("Meetup", "Emails about the meetup venue and the date", ["  "]),
    ("Meetup", "Emails about everythingness of the meetup venue and date", ["x"]),
    ("Meetup", "Emails about the meetup venue, and every guest's date question", ["x"]),
    ("Meetup", "E-MAILS about the meetup venue: ANY E-MAIL from a registered guest", ["x"]),
    ("Đak upisi", "Emails asking when the Đak enrolment for Zagreb opens", ["x"]),
]


def test_lint_matches_the_server_for_every_case():
    cases = [[arcfg._one_line(n), arcfg._one_line(a), ex] for n, a, ex in _LINT_CASES]
    js = _run([(_STORE, _LINT)], (
        f"out = {json.dumps(cases)}.map(([n, a, ex]) => lintTopic(n, a, ex));"
    ))
    py = [arcfg.lint_topic(n, a, ex) for n, a, ex in cases]
    assert js == py
    # Both kinds of answer are in the corpus.
    assert any(py) and not all(py)


# Whole topics: validateTopic finds nothing exactly when the server takes the
# topic (a fresh topic, off — the gate and the route's own checks aside).
_PARITY_TOPICS = [
    _topic(),
    _topic(name="  Meetup   logistics  "),
    _topic(name="Me"),
    _topic(name="M" * 60),
    _topic(name="M" * 61),
    _topic(about="x" * 19),
    _topic(about=("word " * 80).strip()),
    _topic(about=("word " * 81).strip()),
    _topic(about="Emails\tasking when or\nwhere the meetup is held"),
    _topic(about="Emails about the meetup​ venue and the date please"),
    _topic(not_this="n" * 400),
    _topic(not_this="n" * 401),
    _topic(examples=["Where is it?"]),
    _topic(examples=["Where?"]),
    _topic(examples=["e" * 300]),
    _topic(examples=["e" * 301]),
    _topic(examples=["Where is the meetup?", "", "When does the meetup start?"]),
    _topic(examples=[]),
    _topic(counter_examples=["Can I sponsor?"] * 5),
    _topic(counter_examples=["Can I sponsor?"] * 6),
    _topic(senders=["Alice@Example.com", "partner.com", "@hr.example.org"]),
    _topic(senders=["alice@", "@-bad.com"]),
    _topic(senders=[f"u{i}@example.com" for i in range(50)]),
    _topic(senders=[f"u{i}@example.com" for i in range(51)]),
    _topic(brief="b" * 49),
    _topic(brief="b" * 4000),
    _topic(brief="b" * 4001),
    _topic(brief="Line one of the brief for guests.\n\n\n\n   \nLine two, after blank lines."),
    _topic(sources={"folder": "vfs:meetup/faq", "insights": True, "playbooks": False,
                    "web_search": True}),
    _topic(sources={"folder": "meetup", "insights": False, "playbooks": True,
                    "web_search": False}),
    _topic(sources={"folder": "vfs:/", "insights": False, "playbooks": True,
                    "web_search": False}),
    _topic(sources={"folder": "vfs:a/../b", "insights": False, "playbooks": True,
                    "web_search": False}),
    _topic(sources={"folder": "vfs:" + "p" * 600, "insights": False, "playbooks": True,
                    "web_search": False}),
    _topic(name="Anything else"),
    _topic(about="Svi mailovi o meetupu, datumu i mjestu održavanja"),
    _topic(about="Where is the meetup held"),
]


def test_validate_topic_agrees_with_the_server_on_whole_topics():
    js = _validate(_PARITY_TOPICS)
    for topic, errors in zip(_PARITY_TOPICS, js, strict=True):
        try:
            arcfg.apply_patch(arcfg.default_config(), {"topics": [topic]})
            server_ok = True
        except arcfg.ConfigError:
            server_ok = False
        assert (errors == {}) is server_ok, (topic, errors)


def test_folder_and_sender_normalization_matches_the_server():
    folders = ["", "  ", "vfs:meetup/faq", "VFS:meetup//faq/./docs/", "meetup",
               "meetup\\faq", "vfs:/", "vfs:a/../b", "/meetup/faq/"]
    senders = [["Alice@Example.com", "partner.com", "@HR.example.org", "alice@example.com"]]
    out = _run([(_STORE, ["SENDER_ADDRESS_RE", "SENDER_DOMAIN_RE", "normalizeSenders",
                          "normalizeFolder"])], (
        f"out = {{ folders: {json.dumps(folders)}.map(normalizeFolder),"
        f" senders: {json.dumps(senders)}.map((s) => normalizeSenders(s).senders) }};"
    ))
    for raw, js in zip(folders, out["folders"], strict=True):
        try:
            py = arcfg._folder(raw, "Topic")
        except arcfg.ConfigError:
            py = None
        assert js == py, raw
    assert out["senders"] == [list(arcfg._senders(s, "Topic")) for s in senders]


def test_fold_matches_the_server():
    texts = ["Općenito Đak", "ŽELJA čćšđž", "Straße", "Ærø œuvre", "İstanbul ı", "Café Noël"]
    js = _run([(_STORE, ["FOLD_EXTRA", "foldText"])], f"out = {json.dumps(texts)}.map(foldText);")
    assert js == [arcfg.fold(t) for t in texts]


def test_one_line_and_multi_line_match_the_server():
    texts = ["  a \t b\n c  ", "x​y\u0007z", "keep nbsp", "",
             "One.\r\n\r\n\r\n\r\nTwo.   \n  Three.  ", "\n\n  lead and trail  \n\n"]
    out = _run([(_STORE, ["oneLine", "multiLine"])], (
        f"out = {{ one: {json.dumps(texts)}.map(oneLine), multi: {json.dumps(texts)}.map(multiLine) }};"
    ))
    assert out["one"] == [arcfg._one_line(t) for t in texts]
    assert out["multi"] == [arcfg._multi_line(t) for t in texts]


# ── error mapping ───────────────────────────────────────────────────────────

_STALE = "This Flight Deck doesn't offer topic auto-replies yet — it needs an update and a restart."


def _error_text(status: int, status_text: str, body: str) -> str:
    return _run(_ERRORS, (
        f"out = autoReplyError(new HttpError({status}, {json.dumps(status_text)}, "
        f"{json.dumps(body)}));"
    ))


def test_a_deck_without_the_routes_reads_as_needs_update():
    assert _error_text(404, "Not Found", json.dumps(
        {"detail": "No route for /fd/google/gmail-autoreply"})) == _STALE
    assert _error_text(404, "Not Found", json.dumps({"detail": "Not Found"})) == _STALE
    assert _error_text(404, "Not Found", "") == _STALE
    assert _error_text(405, "Method Not Allowed", json.dumps(
        {"detail": "Method Not Allowed"})) == _STALE


def test_the_routes_own_404s_keep_their_words():
    # An unknown topic or another user's held card: the route says so.
    assert _error_text(404, "Not Found", json.dumps({"detail": "No such topic"})) == "No such topic"
    assert _error_text(404, "Not Found", json.dumps(
        {"detail": "No such auto-reply."})) == "No such auto-reply."


def test_other_refusals_keep_the_backends_words():
    enable = "Topic auto-replies can't be turned on yet: connect Google in Connections → Google."
    assert _error_text(409, "Conflict", json.dumps(
        {"detail": enable, "missing": ["google_not_connected"]})) == enable
    assert _error_text(422, "Unprocessable Entity", json.dumps({
        "detail": "Not sent: the reply contains a card number.",
        "findings": [{"kind": "card_number", "level": "block", "hint": "41…11", "echo": False}],
    })) == "Not sent: the reply contains a card number."
    assert _error_text(429, "Too Many Requests", json.dumps(
        {"detail": "The breadth test runs at most once a minute — try again shortly."})
    ).startswith("The breadth test runs at most once a minute")
    assert _error_text(401, "Unauthorized", "{}").startswith("Your Flight Deck session has expired")
    # Not an HTTP answer at all (the network): the error's own message.
    assert _run(_ERRORS, "out = autoReplyError(new Error('Failed to fetch'));") == "Failed to fetch"


# ── the bell ────────────────────────────────────────────────────────────────


def test_bell_types_for_auto_replies():
    out = _run([(_NOTIFY, ["mapServerType"])], (
        "out = ['email_autoreply', 'email_autoreply_held', 'email_autoreply_issue',"
        " 'email_autoreply_blocked', 'email_sent', 'run_error', 'bat_ask'].map(mapServerType);"
    ))
    assert out == ["success", "warning", "warning", "error", "info", "error", "warning"]


# ── Open in Gmail ───────────────────────────────────────────────────────────


def test_gmail_thread_link():
    out = _run([(_STORE, ["gmailThreadUrl"])], (
        "out = [gmailThreadUrl('me+x@example.com', '18c2f0a9b1d2e3f4'),"
        " gmailThreadUrl('', '18c2f0a9b1d2e3f4'),"
        " gmailThreadUrl('me@example.com', ''),"
        " gmailThreadUrl('me@example.com', 'abc/../x')];"
    ))
    assert out == [
        "https://mail.google.com/mail/?authuser=me%2Bx%40example.com#all/18c2f0a9b1d2e3f4",
        "https://mail.google.com/mail/#all/18c2f0a9b1d2e3f4",
        "",
        "",
    ]


# ── the store's actions (stub create + fetchJson: no network) ───────────────

# A minimal zustand `create`, and a fetchJson that records each call and
# answers from a queue — an answer with status >= 400 throws HttpError, as the
# real fetchJson does.
_STORE_PRELUDE = r"""
function create(init) {
  let state;
  const set = (p) => { state = { ...state, ...(typeof p === 'function' ? p(state) : p) }; };
  const get = () => state;
  state = init(set, get);
  return { getState: get, setState: set };
}
const calls = [];
const answers = [];
const teardowns = [];
function fetchJson(url, init) {
  calls.push({ url, method: (init && init.method) || 'GET',
               body: init && init.body !== undefined ? JSON.parse(init.body) : null });
  const a = answers.shift();
  if (!a) return Promise.reject(new Error('no answer queued for ' + url));
  if (a.status >= 400) {
    return Promise.reject(new HttpError(a.status, 'Error', JSON.stringify(a.body)));
  }
  return Promise.resolve(a.body);
}
function registerSignOutTeardown(fn) { teardowns.push(fn); }
"""
_STORE_PARTS = [
    (_GOOGLE_STORE, ["HttpError", "_gmailSendError"]),
    (_STORE, ["TRY_IT_SAMPLE_FROM", "tryItSender", "AUTO_REPLY_STALE", "autoReplyError",
              "BASE", "_attempt", "useGmailAutoReplyStore", "registerSignOutTeardown()"]),
]


def _store(answers: list[dict], script: str):
    """Run *script* (async; ``S`` is the store's state getter) with the
    answers queued; returns ``{result, state, calls}``."""
    body = (
        f"answers.push(...{json.dumps(answers)});\n"
        "const S = () => useGmailAutoReplyStore.getState();\n"
        "done = (async () => {\n"
        f"  const result = await (async () => {{ {script} }})();\n"
        "  const st = S();\n"
        "  out = { result, calls, state: { config: st.config, activity: st.activity,"
        " agents: st.agents, error: st.error } };\n"
        "})();\n"
    )
    return _run(_STORE_PARTS, body, prelude=_STORE_PRELUDE)


_V = {"enabled": False, "agent_ref": "", "daily_limit": 10, "never_disclose": [],
      "topics": [], "status": {"missing": ["no_agent"]}}


def test_save_puts_only_the_patch_and_keeps_the_view_and_notice():
    patch = {"daily_limit": 12, "never_disclose": ["Project Falcon"]}
    out = _store([{"status": 200, "body": {**_V, "daily_limit": 12,
                                           "notice": "re-run the breadth test"}}],
                 f"return S().saveConfig({json.dumps(patch)});")
    assert out["result"] == {"ok": True, "notice": "re-run the breadth test"}
    assert out["calls"] == [{"url": "/fd/google/gmail-autoreply", "method": "PUT", "body": patch}]
    assert out["state"]["config"]["daily_limit"] == 12
    assert out["state"]["error"] is None


def test_a_refused_enable_rereads_the_view_and_keeps_the_reason():
    detail = "Topic auto-replies can't be turned on yet: choose the agent that writes the replies."
    fresh = {**_V, "status": {"missing": ["no_agent", "no_topics"]}}
    out = _store([{"status": 409, "body": {"detail": detail, "missing": ["no_agent"]}},
                  {"status": 200, "body": fresh}],
                 "return S().saveConfig({ enabled: true });")
    assert out["result"] == {"ok": False, "notice": ""}
    assert [(c["method"], c["url"]) for c in out["calls"]] == [
        ("PUT", "/fd/google/gmail-autoreply"), ("GET", "/fd/google/gmail-autoreply")]
    assert out["state"]["error"] == detail
    assert out["state"]["config"] == fresh


_REFUSED_TOPIC = ("Topic 1: the what the agent may say contains one of your never-disclose "
                  "terms, so every reply quoting it would be blocked. Remove the term from one of them.")


def test_a_refused_save_that_turns_off_still_turns_off():
    # On, and the owner unticks the switch while also editing a brief to say a
    # never-disclose term. The whole save goes as one PUT (stale-checked); the
    # server refuses it — so the turn-off is sent again on its own, and only
    # it: auto-replies are off, the refused edit isn't saved.
    off_view = {**_V, "enabled": False}
    topics = [_topic(brief="Project Falcon demo at the meetup on 14 Nov at 18:00 at the Hub, Ilica 1.")]
    out = _store([{"status": 400, "body": {"detail": _REFUSED_TOPIC}},
                  {"status": 200, "body": off_view}],
                 f"return S().saveConfig({{ topics: {json.dumps(topics)}, enabled: false }});")
    assert [(c["method"], c["url"], c["body"]) for c in out["calls"]] == [
        ("PUT", "/fd/google/gmail-autoreply", {"topics": topics, "enabled": False}),
        ("PUT", "/fd/google/gmail-autoreply", {"enabled": False}),
    ]
    assert out["result"] == {"ok": False, "notice": ""}
    # It IS off, and the error says so as well as why the rest wasn't saved.
    assert out["state"]["config"]["enabled"] is False
    assert out["state"]["error"] == (
        "Topic auto-replies are off now, but your other changes weren't saved: " + _REFUSED_TOPIC)


def test_a_turn_off_that_fails_twice_sends_nothing_else():
    detail = "Flight Deck can't read its agent records right now — try again."
    out = _store([{"status": 503, "body": {"detail": detail}},
                  {"status": 503, "body": {"detail": detail}}],
                 "return S().saveConfig({ daily_limit: 12, never_disclose: ['Falcon'], enabled: false });")
    assert [c["body"] for c in out["calls"]] == [
        {"daily_limit": 12, "never_disclose": ["Falcon"], "enabled": False}, {"enabled": False}]
    assert out["result"] == {"ok": False, "notice": ""}
    # Nothing was written: the view (here: none read yet) is left as it was.
    assert out["state"]["config"] is None
    assert out["state"]["error"] == detail


def test_turning_off_with_edits_the_server_takes_is_one_put():
    out = _store([{"status": 200, "body": {**_V, "enabled": False, "daily_limit": 12,
                                           "notice": "re-run the breadth test"}}],
                 "return S().saveConfig({ enabled: false, daily_limit: 12 });")
    assert [c["body"] for c in out["calls"]] == [{"enabled": False, "daily_limit": 12}]
    assert out["result"] == {"ok": True, "notice": "re-run the breadth test"}
    assert out["state"]["config"]["daily_limit"] == 12
    assert out["state"]["error"] is None


def test_a_stale_tab_turning_off_with_edits_never_saves_the_edits():
    # The page was read at T1; a newer change made T2. The save carries T1, is
    # refused as stale (409), and only the turn-off follows — never the stale
    # edits with the off answer's fresh updated_at.
    stale = "Your topic auto-reply settings changed since this page loaded — reload."
    out = _store([{"status": 200, "body": {**_V, "enabled": True, "updated_at": "T1"}},
                  {"status": 409, "body": {"detail": stale}},
                  {"status": 200, "body": {**_V, "enabled": False, "updated_at": "T3"}}],
                 "await S().fetchConfig(); return S().saveConfig({ daily_limit: 12, enabled: false });")
    puts = [c["body"] for c in out["calls"] if c["method"] == "PUT"]
    assert puts == [{"daily_limit": 12, "enabled": False, "updated_at": "T1"}, {"enabled": False}]
    assert out["state"]["config"]["enabled"] is False


def test_turning_on_and_turning_off_alone_stay_one_put():
    # Turning ON is checked against the edits that come with it (a topic
    # switched on in the same Save): never split. A bare turn-off (the kiosk
    # line's) is one PUT too.
    topics = [_topic()]
    out = _store([{"status": 200, "body": {**_V, "enabled": True}},
                  {"status": 200, "body": _V}], (
        f"await S().saveConfig({{ topics: {json.dumps(topics)}, enabled: true }});"
        " return S().saveConfig({ enabled: false });"
    ))
    assert [c["body"] for c in out["calls"]] == [{"topics": topics, "enabled": True},
                                                {"enabled": False}]


def test_the_server_never_refuses_a_turn_off_on_its_own():
    # What the split relies on: the route's own checks look only at what a
    # PUT changes, so {enabled: false} alone passes where the same turn-off
    # sent with the edit is refused.
    import asyncio

    from fastapi import HTTPException

    from captain_claw.flight_deck import gmail_autoreply_routes as routes

    topic = _topic(brief="Project Falcon demo at the meetup on 14 Nov at 18:00 at the Hub, Ilica 1.")
    current = arcfg.apply_patch(arcfg.default_config(),
                                {"topics": [topic], "never_disclose": ["Falcon"]}).config

    async def check(patch):
        await routes._check_update(None, "u1", current,
                                   arcfg.apply_patch(current, patch).config, patch)

    asyncio.run(check({"enabled": False}))
    with pytest.raises(HTTPException) as refused:
        asyncio.run(check({"enabled": False, "topics": [topic]}))
    assert refused.value.status_code == 400


def _handle_save() -> str:
    src = _SECTION.read_text(encoding="utf-8")
    body = src[src.index("const handleSave = async () => {"):]
    return body[:body.index("\n  }\n")]


def test_a_failed_save_shows_the_switch_as_saved():
    # After a refused save the box reads what the server holds — ON again when
    # even the turn-off failed, OFF once it went through — never the unticked
    # box of a turn-off that didn't happen.
    save = _handle_save()
    assert "if (!ok) {" in save
    failed = save[save.index("if (!ok) {"):]
    failed = failed[:failed.index("return")]
    assert ("if (patch.enabled !== undefined) setEnabled("
            "!!useGmailAutoReplyStore.getState().config?.enabled)") in failed


def test_a_bad_value_is_not_reread():
    out = _store([{"status": 400, "body": {"detail": "daily_limit must be a whole number from 1 to 50"}}],
                 "return S().saveConfig({ daily_limit: 99 });")
    assert out["result"]["ok"] is False
    assert len(out["calls"]) == 1
    assert out["state"]["error"] == "daily_limit must be a whole number from 1 to 50"
    assert out["state"]["config"] is None


def test_a_deck_without_the_routes_says_so_on_load():
    out = _store([{"status": 404, "body": {"detail": "No route for /fd/google/gmail-autoreply"}}],
                 "return S().fetchConfig();")
    assert out["state"]["error"] == _STALE
    assert out["state"]["config"] is None


def test_activity_agents_and_pause_hit_their_routes():
    row = {"id": "ar_1", "state": "held"}
    out = _store([
        {"status": 200, "body": [row]},
        {"status": 200, "body": {"not": "a list"}},
        {"status": 200, "body": [{"ref": "process:bot:1", "name": "Bot"}]},
        {"status": 200, "body": {**_V, "disabled_reason": "paused_by_user"}},
    ], (
        "await S().fetchActivity(true); const all = S().activity;"
        " await S().fetchActivity(); const shown = S().activity;"
        " await S().fetchAgents();"
        " const paused = await S().pause();"
        " return { all, shown, paused };"
    ))
    assert out["result"] == {"all": [row], "shown": [], "paused": True}
    assert [(c["method"], c["url"]) for c in out["calls"]] == [
        ("GET", "/fd/google/gmail-autoreply/activity?limit=50&all=true"),
        ("GET", "/fd/google/gmail-autoreply/activity?limit=50&all=false"),
        ("GET", "/fd/google/gmail-autoreply/agents"),
        ("POST", "/fd/google/gmail-autoreply/pause"),
    ]
    assert out["state"]["agents"] == [{"ref": "process:bot:1", "name": "Bot"}]
    assert out["state"]["config"]["disabled_reason"] == "paused_by_user"


def test_held_send_as_is_or_edited():
    sent = {"ok": True, "message_id": "m1", "thread_id": "t1", "to": "guest@example.com",
            "subject": "Re: Meetup", "row": None}
    out = _store([
        {"status": 200, "body": sent},
        {"status": 200, "body": sent},
        {"status": 422, "body": {"detail": "Not sent: the reply contains a card number.",
                                 "findings": [{"kind": "card_number"}]}},
        {"status": 409, "body": {"detail": "This auto-reply isn't waiting for you any more."}},
    ], (
        "const asIs = await S().sendHeld('ar_1/x');"
        " const edited = await S().sendHeld('ar_1', 'Hello — see you there.');"
        " const blocked = await S().sendHeld('ar_1', 'Pay with 4111 1111 1111 1111');"
        " const again = await S().sendHeld('ar_1');"
        " return { asIs, edited, blocked, again };"
    ))
    r = out["result"]
    assert r["asIs"] == {"ok": True, "data": sent} and r["edited"]["ok"] is True
    assert r["blocked"] == {"ok": False, "error": "Not sent: the reply contains a card number."}
    assert r["again"] == {"ok": False, "error": "This auto-reply isn't waiting for you any more."}
    assert [(c["url"], c["body"]) for c in out["calls"]] == [
        ("/fd/google/gmail-autoreply/activity/ar_1%2Fx/send", {}),
        ("/fd/google/gmail-autoreply/activity/ar_1/send", {"text": "Hello — see you there."}),
        ("/fd/google/gmail-autoreply/activity/ar_1/send", {"text": "Pay with 4111 1111 1111 1111"}),
        ("/fd/google/gmail-autoreply/activity/ar_1/send", {}),
    ]
    assert all(c["method"] == "POST" for c in out["calls"])
    # A held card's refusal stays on the card, not the section.
    assert out["state"]["error"] is None


def test_dismiss_returns_the_row():
    row = {"id": "ar_1", "state": "dismissed"}
    out = _store([{"status": 200, "body": {"ok": True, "row": row}},
                  {"status": 404, "body": {"detail": "No such auto-reply."}}],
                 "return [await S().dismissHeld('ar_1'), await S().dismissHeld('ar_2')];")
    assert out["result"] == [{"ok": True, "data": row},
                             {"ok": False, "error": "No such auto-reply."}]
    assert out["calls"][0] == {"url": "/fd/google/gmail-autoreply/activity/ar_1/dismiss",
                               "method": "POST", "body": {}}


def test_a_breadth_test_rereads_the_view_only_when_it_ran():
    result = {"topic_id": "t_0123abcd", "eligible": 20, "matched": 2, "rate": 0.1, "rows": [],
              "gate": {"ok": True, "reason": "", "note": "", "warning": ""}}
    out = _store([
        {"status": 200, "body": result},
        {"status": 200, "body": _V},
        {"status": 429, "body": {"detail": "The breadth test runs at most once a minute — try again shortly."}},
    ], (
        "const ran = await S().dryRun('t_0123abcd');"
        " const limited = await S().dryRun('t_0123abcd');"
        " return { ran, limited };"
    ))
    assert out["result"]["ran"] == {"ok": True, "data": result}
    assert out["result"]["limited"]["ok"] is False
    assert out["result"]["limited"]["error"].startswith("The breadth test runs at most once")
    assert [(c["method"], c["url"]) for c in out["calls"]] == [
        ("POST", "/fd/google/gmail-autoreply/topics/t_0123abcd/dry-run"),
        ("GET", "/fd/google/gmail-autoreply"),
        ("POST", "/fd/google/gmail-autoreply/topics/t_0123abcd/dry-run"),
    ]


def test_try_it_posts_the_made_up_email():
    payload = {"subject": "Meetup", "body": "Where is it?", "from": "guest@example.com",
               "topic_id": "t_0123abcd"}
    answer = {"match": None, "topic": None, "forced": False, "writer": None, "guard": None,
              "judge": None, "outcome": "skipped"}
    out = _store([{"status": 200, "body": answer}],
                 f"return S().tryIt({json.dumps(payload)});")
    assert out["result"] == {"ok": True, "data": answer}
    assert out["calls"] == [{"url": "/fd/google/gmail-autoreply/test", "method": "POST",
                             "body": payload}]


def test_try_it_with_from_left_blank_is_an_email_from_the_made_up_guest():
    # The writer answers a sender: a sample without one always ended "Failed —
    # no sender to answer". A blank From is the made-up guest instead.
    answer = {"match": None, "topic": None, "forced": False, "writer": None, "guard": None,
              "judge": None, "outcome": "skipped"}
    out = _store([{"status": 200, "body": answer}] * 3, (
        "await S().tryIt({ subject: 'Meetup', body: 'Where is it?' });"
        " await S().tryIt({ subject: 'Meetup', body: 'Where is it?', from: '   ' });"
        " await S().tryIt({ subject: 'Meetup', body: 'Where is it?', from: ' alice@partner.com ' });"
        " return TRY_IT_SAMPLE_FROM;"
    ))
    sample = out["result"]
    assert sample == "guest@example.com"
    assert [c["body"]["from"] for c in out["calls"]] == [sample, sample, "alice@partner.com"]


def test_the_made_up_guest_gets_through_to_the_writer():
    # The route takes it as one plain address, and the writer has someone to
    # answer (no "no_sender").
    from captain_claw import gmail_compose, gmail_inbound
    from captain_claw.flight_deck import gmail_autoreply_writer as writer

    sample = _run([(_STORE, ["TRY_IT_SAMPLE_FROM", "tryItSender"])],
                  "out = [tryItSender(''), tryItSender(undefined), tryItSender(null)];")
    assert len(set(sample)) == 1
    addr = sample[0]
    assert gmail_compose.parse_addresses(addr) == [addr]
    inb = writer.sample_inbound(addr, "Meetup", "Where is it?")
    assert gmail_inbound.sender_address(inb) == addr


_FROM_PLACEHOLDER = r"""
const ts = require(process.argv[1]);
const fs = require('fs');
const src = process.argv[2];
const sf = ts.createSourceFile(src, fs.readFileSync(src, 'utf8'),
  ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const found = [];
(function walk(n) {
  if (ts.isJsxSelfClosingElement(n) && n.tagName.getText(sf) === 'input') {
    const attr = (name) => n.attributes.properties.find(
      (p) => ts.isJsxAttribute(p) && p.name.getText(sf) === name);
    const change = attr('onChange');
    if (change && change.getText(sf).includes('setFrom(')) {
      const init = attr('placeholder').initializer;
      found.push(ts.isJsxExpression(init) ? init.expression.getText(sf) : init.getText(sf));
    }
  }
  ts.forEachChild(n, walk);
})(sf);
process.stdout.write(JSON.stringify(found));
"""


def test_the_try_it_from_field_says_what_a_blank_means():
    proc = subprocess.run(
        ["node", "-e", _FROM_PLACEHOLDER, str(_TS), str(_SECTION)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    exprs = json.loads(proc.stdout)
    assert len(exprs) == 1, exprs
    shown = _run([(_STORE, ["TRY_IT_SAMPLE_FROM"])], f"out = {exprs[0]};")
    # Not "optional" (a blank From used to fail every time): it names the
    # address a blank one stands for.
    assert "optional" not in shown.lower()
    assert "guest@example.com" in shown and "blank" in shown
    # And the box sends that same sender, never nothing.
    src = _SECTION.read_text(encoding="utf-8")
    box = src[src.index("function TryItBox"):]
    box = box[:box.index("\n}\n")]
    assert "const sender = tryItSender(from)" in box
    assert "from: sender," in box


def test_sender_allowed_matches_the_server():
    from captain_claw import gmail_compose

    cases = [
        ([], "guest@example.com"),
        (["alice@partner.com"], "alice@partner.com"),
        (["alice@partner.com"], "ALICE@partner.com "),
        (["alice@partner.com"], "bob@partner.com"),
        (["@partner.com"], "bob@partner.com"),
        (["@partner.com"], "bob@sub.partner.com"),
        (["@partner.com"], "guest@example.com"),
        (["@example.com", "x@y.org"], "guest@example.com"),
        (["guest@example.com"], "guest@example.com"),
        (["@partner.com"], ""),
        (["@partner.com"], "partner.com"),
    ]
    js = _run([(_STORE, ["senderAllowed"])],
              f"out = {json.dumps(cases)}.map(([s, a]) => senderAllowed(s, a));")
    py = [not gmail_compose.recipients_allowed([a], s) if s else True for s, a in cases]
    assert js == py
    assert True in py and False in py


def test_sign_out_clears_everything_the_user_saw():
    out = _store([{"status": 200, "body": {**_V, "never_disclose": ["Project Falcon"]}},
                  {"status": 200, "body": [{"id": "ar_1", "reply_text": "secret-ish"}]},
                  {"status": 200, "body": [{"ref": "process:bot:1"}]}], (
        "await S().fetchConfig(); await S().fetchActivity(); await S().fetchAgents();"
        " useGmailAutoReplyStore.setState({ error: 'x' });"
        " teardowns.forEach((fn) => fn());"
        " return teardowns.length;"
    ))
    assert out["result"] == 1
    assert out["state"] == {"config": None, "activity": [], "agents": [], "error": None}


# ── where the section sits, and the kiosk ───────────────────────────────────


def test_the_section_follows_email_sending_for_a_connected_account():
    src = _CARD.read_text(encoding="utf-8")
    send = src.index("{connected && <GmailSendSection />}")
    auto = src.index("{connected && <GmailAutoReplySection />}")
    assert send < auto
    # Nothing but a comment between them.
    between = src[send + len("{connected && <GmailSendSection />}"):auto]
    assert re.fullmatch(r"\s*\{/\*.*?\*/\}\s*", between, re.DOTALL), between


def test_the_kiosk_shows_only_an_off_switch_once_on():
    src = _SECTION.read_text(encoding="utf-8")
    main = src[src.index("export default function GmailAutoReplySection"):]
    main = main[:main.index("\n}\n")]
    assert "useAuthStore(selectKioskLocked)" in main
    assert "kioskLocked ? <KioskAutoReplyLine /> : <AutoReplyEditor />" in main
    kiosk = src[src.index("function KioskAutoReplyLine"):]
    kiosk = kiosk[:kiosk.index("\n}\n")]
    assert "if (!view?.enabled) return null" in kiosk
    # Its one control turns them off — never on.
    assert "saveConfig({ enabled: false })" in kiosk
    assert "enabled: true" not in kiosk
