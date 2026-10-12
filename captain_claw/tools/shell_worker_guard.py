"""Shell rules for Vatra workers (``CLAW_VATRA_WORKER`` set at spawn).

Two refusals, both read from the command text before it runs:

1. **A web page printed into the context.** ``curl``/``wget`` of an http(s) URL
   whose body comes straight back as output — no file to save it to, no
   redirect, no pipe filtering it — is refused and pointed at ``web_fetch``,
   which returns clean text (and offloads to the run's source corpus when that's
   on). Raw HTML in the context is what ballooned a worker past its window.
   Downloading a file (``curl -L -o report.pdf URL``) and filtering a response
   (``curl -s URL | grep -o 'href=…'``) are fine.

2. **A write into VFS storage outside the run's folder.** Workers share
   ``vfs:<project>/`` (the run's folder); a shell command that writes into any
   other part of the VFS storage tree (another project, another user's tree) or
   into another linked folder bypasses every check the file tools make, so it's
   refused. Writes into the run's own folder stay allowed — that is how one
   worker hands a downloaded report to the next — unless the folder is a
   read-only link.

Heuristic by nature: it reads redirects, ``-o``/``-O``/``-P`` style outputs,
``tee``/``cp``/``mv``/``mkdir``/``rm``/… operands and ``cd`` chains, with simple
``NAME=value`` variables expanded. A script that opens a file for writing from
inside python is not seen. Commands the parser can't follow are let through.
"""

from __future__ import annotations

import os
import re
import shlex

_FETCHERS = frozenset({"curl", "wget"})
# curl/wget options that take a value (never the URL fetched).
_CURL_VALUE_OPTS = frozenset({
    "-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--data-urlencode",
    "--json", "-F", "--form", "--form-string", "-H", "--header", "-e", "--referer",
    "-x", "--proxy", "-o", "--output", "--output-dir", "-A", "--user-agent", "-u",
    "--user", "-w", "--write-out", "-m", "--max-time", "-X", "--request", "-b",
    "--cookie", "-c", "--cookie-jar", "--connect-timeout", "--retry", "-r", "--range",
    "-T", "--upload-file", "-K", "--config", "-E", "--cert", "-D", "--dump-header",
    "--limit-rate", "-z", "--time-cond", "--resolve", "--max-filesize",
})
_WGET_VALUE_OPTS = frozenset({
    "-O", "--output-document", "-P", "--directory-prefix", "-U", "--user-agent",
    "--header", "--referer", "--post-data", "--body-data", "-e", "--execute",
    "-o", "--output-file", "-a", "--append-output", "-T", "--timeout", "-t", "--tries",
    "--user", "--password", "-w", "--wait", "-Q", "--quota",
})
_SEPARATORS = frozenset({";", ";;", "&", "&&", "|", "||", "|&", "(", ")", "{", "}"})
_PIPES = frozenset({"|", "|&"})
_REDIRECTS = frozenset({">", ">>", "&>", "&>>", ">|"})
# Operators whose operand is read (or an fd), never written.
_INPUT_OPS = frozenset({"<", "<<", "<<<", "<<-", ">&", "<&", "<>"})
_WRAPPERS = frozenset({"sudo", "doas", "env", "time", "nohup", "command", "builtin",
                       "exec", "nice", "stdbuf"})
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_NOT_A_FILE = frozenset({"-", "/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})

PAGE_DUMP_MESSAGE = (
    "curl/wget would print a web page straight into your context. Read web pages "
    "with web_fetch (clean text, lean context). Use curl/wget in shell only to "
    "download a file (curl -L -o <file> <url>) or to filter a response through a "
    "pipe (curl -s <url> | grep …)."
)


def _vfs_write_message(target: str, project: str) -> str:
    return (
        f"this writes into shared VFS storage outside this run's folder ({target}). "
        f"Write into the run's folder — vfs:{project}/ (the write tool), or its "
        "folder path in shell — or keep the file in your workspace "
        "(saved/downloads/…)."
    )


def worker_shell_refusal(command: str, cwd: str | os.PathLike | None) -> str | None:
    """Why a Vatra worker may not run *command*, or None when it may.
    A no-op outside a Vatra worker process."""
    if not os.environ.get("CLAW_VATRA_WORKER"):
        return None
    try:
        simple = list(_simple_commands(command, str(cwd) if cwd else None))
    except Exception:
        return None  # never let the parser itself block a command
    for cmd in simple:
        if _dumps_page(cmd):
            return PAGE_DUMP_MESSAGE
    return _vfs_write_refusal(simple)


# ── parsing ───────────────────────────────────────────────────────────

class _Cmd:
    __slots__ = ("program", "args", "redirects", "piped", "cwd", "variables")

    def __init__(self, program: str, args: list[str], redirects: list[str],
                 piped: bool, cwd: str | None, variables: dict[str, str]):
        self.program = program
        self.args = args
        self.redirects = redirects
        self.piped = piped
        self.cwd = cwd
        self.variables = dict(variables)  # the NAME=value set at this point


def _tokens(line: str) -> list[str] | None:
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.wordchars += ":@%+,$={}~"
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return None


def _expand(token: str, variables: dict[str, str]) -> str | None:
    """*token* with $NAME / ${NAME} replaced from *variables*; None when a
    variable is unknown (or the token still holds a substitution)."""
    missing = False

    def _sub(m: re.Match) -> str:
        nonlocal missing
        name = m.group(1) or m.group(2)
        if name in variables:
            return variables[name]
        missing = True
        return ""

    out = _VAR_RE.sub(_sub, token)
    if missing or "$" in out or "`" in out:
        return None
    return out


def _resolve(token: str, cwd: str | None, variables: dict[str, str]) -> str | None:
    t = _expand(token, variables)
    if not t or t in _NOT_A_FILE or t.startswith("&"):
        return None
    t = os.path.expanduser(t)
    if not os.path.isabs(t):
        if not cwd:
            return None
        t = os.path.join(cwd, t)
    return os.path.realpath(t)


def _simple_commands(command: str, cwd: str | None):
    """Every simple command *command* runs, with its args, redirect targets,
    whether its output is piped, and the directory it runs in (`cd` chains and
    NAME=value assignments are followed in order)."""
    from captain_claw.tools.shell import _command_lines

    variables: dict[str, str] = {}
    for line in _command_lines(command):
        toks = _tokens(line)
        if toks is None:
            continue
        segment: list[str] = []
        for i in range(len(toks) + 1):
            tok = toks[i] if i < len(toks) else ";"
            if tok not in _SEPARATORS:
                segment.append(tok)
                continue
            cmd = _make_cmd(segment, tok in _PIPES, cwd, variables)
            segment = []
            if cmd is None:
                continue
            if cmd.program == "cd":
                target = cmd.args[0] if cmd.args else "~"
                new = _resolve(target, cwd, variables)
                if new is not None:
                    cwd = new
                continue
            yield cmd


def _make_cmd(seg: list[str], piped: bool, cwd: str | None,
              variables: dict[str, str]) -> _Cmd | None:
    words: list[str] = []
    redirects: list[str] = []
    i = 0
    while i < len(seg):
        tok = seg[i]
        nxt = seg[i + 1] if i + 1 < len(seg) else ""
        if tok.isdigit() and (nxt in _REDIRECTS or nxt in _INPUT_OPS):
            i += 1  # the fd number of `2>` / `2>&1`
            continue
        if tok in _REDIRECTS:
            if nxt:
                redirects.append(nxt)
            i += 2
            continue
        if tok in _INPUT_OPS:
            i += 2
            continue
        words.append(tok)
        i += 1
    # Leading NAME=value assignments (a bare one sets a variable for later).
    while words and _ASSIGN_RE.match(words[0]):
        name, value = _ASSIGN_RE.match(words.pop(0)).groups()
        expanded = _expand(value, variables)
        if expanded is not None:
            variables[name] = expanded
    # Wrappers that run their argument: sudo, env, nohup, timeout <n> …
    while words:
        head = words[0].rsplit("/", 1)[-1]
        if head in _WRAPPERS:
            words.pop(0)
            while words and (words[0].startswith("-") or _ASSIGN_RE.match(words[0])):
                words.pop(0)
            continue
        if head == "timeout":
            words.pop(0)
            while words and words[0].startswith("-"):
                words.pop(0)
            if words:
                words.pop(0)  # the duration
            continue
        break
    if not words:
        return None
    return _Cmd(words[0].rsplit("/", 1)[-1], words[1:], redirects, piped, cwd, variables)


# ── rule 1: web page printed into the context ────────────────────────

def _fetch_urls(cmd: _Cmd) -> list[str]:
    value_opts = _CURL_VALUE_OPTS if cmd.program == "curl" else _WGET_VALUE_OPTS
    urls: list[str] = []
    skip = False
    for arg in cmd.args:
        if skip:
            skip = False
            continue
        if arg.startswith("-"):
            if "=" in arg:
                continue
            skip = arg in value_opts or (
                not arg.startswith("--") and len(arg) > 2 and f"-{arg[-1]}" in value_opts)
            continue
        if arg.lower().startswith(("http://", "https://")):
            urls.append(arg)
    return urls


def _short_flags(args: list[str]) -> str:
    """The letters of every single-dash option bundle (``-sLo`` → ``sLo``)."""
    return "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))


def _dumps_page(cmd: _Cmd) -> bool:
    if cmd.program not in _FETCHERS or cmd.piped or cmd.redirects:
        return False
    if not _fetch_urls(cmd):
        return False
    args = cmd.args
    longs = {a.split("=", 1)[0] for a in args if a.startswith("--")}
    flags = _short_flags(args)
    if cmd.program == "curl":
        if "o" in flags or "O" in flags or "I" in flags:
            return False  # saved to a file, or headers only
        return not (longs & {"--output", "--remote-name", "--remote-name-all", "--head",
                             "--output-dir"})
    # wget saves to a file unless told to print (-O - / -qO- / --output-document=-).
    for i, arg in enumerate(args):
        if arg in ("-O", "--output-document") and i + 1 < len(args):
            return args[i + 1] == "-"
        if arg == "--output-document=-" or (arg.startswith("-") and not arg.startswith("--")
                                            and arg.endswith("O-")):
            return True
        if arg.startswith("-") and not arg.startswith("--") and arg.endswith("O") \
                and i + 1 < len(args):
            return args[i + 1] == "-"
    return False


# ── rule 2: writes into VFS storage outside the run's folder ─────────

def _write_targets(cmd: _Cmd) -> list[str]:
    """Raw (unresolved) paths *cmd* writes to; ``.`` stands for its cwd."""
    p, args = cmd.program, cmd.args
    out = list(cmd.redirects)
    operands = [a for a in args if not a.startswith("-")]
    if p == "curl":
        flags = _short_flags(args)
        out_dir = "."
        for i, arg in enumerate(args):
            nxt = args[i + 1] if i + 1 < len(args) else None
            if arg in ("--output",) or (arg.startswith("-") and not arg.startswith("--")
                                        and arg.endswith("o")):
                if nxt:
                    out.append(nxt)
            elif arg.startswith("--output="):
                out.append(arg.split("=", 1)[1])
            elif arg == "--output-dir" and nxt:
                out_dir = nxt
        if "O" in flags or "--remote-name" in args or "--remote-name-all" in args:
            out.append(out_dir)
    elif p == "wget":
        prints, prefix = False, "."
        for i, arg in enumerate(args):
            nxt = args[i + 1] if i + 1 < len(args) else None
            if arg in ("-O", "--output-document") or (
                    arg.startswith("-") and not arg.startswith("--") and arg.endswith("O")):
                if nxt == "-":
                    prints = True
                elif nxt:
                    out.append(nxt)
            elif arg.endswith("O-") or arg == "--output-document=-":
                prints = True
            elif arg.startswith("--output-document="):
                out.append(arg.split("=", 1)[1])
            elif arg in ("-P", "--directory-prefix") and nxt:
                prefix = nxt
            elif arg.startswith("--directory-prefix="):
                prefix = arg.split("=", 1)[1]
        if not prints and _fetch_urls(cmd):
            out.append(prefix)
    elif p == "tee":
        out += operands
    elif p in ("cp", "mv", "install", "ln", "rsync", "scp"):
        if "-t" in args:
            i = args.index("-t")
            if i + 1 < len(args):
                out.append(args[i + 1])
        elif len(operands) >= 2:
            out.append(operands[-1])
        if p == "mv":
            out += operands[:-1]  # the source is removed from where it was
    elif p in ("mkdir", "touch", "rm", "rmdir", "truncate", "unlink", "shred"):
        out += operands
    elif p == "unzip":
        if not set(_short_flags(args)) & set("lvtZp"):  # list / test / pipe write nothing
            out.append(args[args.index("-d") + 1] if "-d" in args and args.index("-d") + 1 < len(args)
                       else ".")
    elif p in ("tar", "bsdtar", "gtar"):
        # The mode letters: option bundles, plus an old-style first word (`tar xzf a.tgz`).
        modes = _short_flags(args) + (args[0] if args and not args[0].startswith("-") else "")
        if "x" in modes or "--extract" in args or "--get" in args:
            dest = "."
            for i, arg in enumerate(args):
                if arg in ("-C", "--directory") and i + 1 < len(args):
                    dest = args[i + 1]
            out.append(dest)
    elif p == "dd":
        out += [a[3:] for a in args if a.startswith("of=")]
    return out


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _vfs_write_refusal(cmds: list[_Cmd]) -> str | None:
    from captain_claw import vfs

    try:
        base = os.path.realpath(str(vfs.vfs_base()))
        project = vfs.default_project()
        user_root = vfs.user_root()
        try:
            run_root = os.path.realpath(str(vfs.project_root(project)))
        except Exception:
            run_root = os.path.realpath(str(user_root / project))
        run_ro = vfs.project_is_readonly(project)
        links = []
        for name in (vfs.read_links_at(user_root) or {}):
            target = vfs.link_target_at(user_root, name)
            if target is not None:
                links.append(os.path.realpath(str(target)))
    except Exception:
        return None
    for cmd in cmds:
        for raw in _write_targets(cmd):
            target = _resolve(raw, cmd.cwd, cmd.variables)
            if target is None:
                continue
            if _within(target, run_root):
                if run_ro:
                    return _vfs_write_message(target, project).replace(
                        "outside this run's folder", "in a read-only folder")
                continue
            if _within(target, base) or any(_within(target, link) for link in links):
                return _vfs_write_message(target, project)
    return None
