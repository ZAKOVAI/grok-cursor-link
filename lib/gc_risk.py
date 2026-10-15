"""gc_risk: deterministic risk tier for a shell command. No model, no I/O.

  tag(command, cwd) -> (risk, reason)    risk in "low" | "medium" | "high"

high   = anything that pushes, deploys, deletes outside /tmp, escalates, pipes the
         network into a shell, rewrites history, merges, migrates, or touches
         env/key/secret files. Matched anywhere in the text (also inside quotes,
         `sh -c "..."`), so false positives land on the safe side.
low    = every segment is on an explicit read-only / build / test allowlist, with no
         command substitution and no write redirect.
medium = everything else.
Shared by the hook gate, the CLI (`always add`) and the watcher. Python 3 stdlib.
"""
import os
import re
import shlex

_SEG = r"[^;&|\n]*"
HIGH = [
    (re.compile(r"\bgit\b" + _SEG + r"\bpush\b"), "git push"),
    (re.compile(r"--force\b|--force-with-lease"), "--force"),
    (re.compile(r"(?i)\bdeploy"), "deploy"),
    (re.compile(r"\b(?:sudo|doas)\b"), "sudo"),
    (re.compile(r"\b(?:curl|wget)\b[^;&\n]*\|\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b"), "curl | sh"),
    (re.compile(r"\bgit\b" + _SEG + r"\breset\b" + _SEG + r"--hard"), "git reset --hard"),
    (re.compile(r"\bgit\b" + _SEG + r"\bclean\b"), "git clean"),
    (re.compile(r"\bgit\b" + _SEG + r"\bmerge\b"), "git merge"),
    (re.compile(r"(?i)migrat"), "migration"),
    (re.compile(r"(?:^|[\s/'\"=<>])\.env\b"), ".env file"),
    (re.compile(r"(?i)key|secret|\.pem\b|id_rsa|id_ed25519"), "key/secret path"),
    (re.compile(r"\blaunchctl\b"), "launchctl"),
    (re.compile(r"\bosascript\b"), "osascript"),
]
LOW = [
    ("ls",), ("cat",), ("head",), ("tail",), ("wc",), ("pwd",), ("cd",), ("rg",), ("grep",),
    ("git", "status"), ("git", "diff"), ("git", "log"), ("git", "show"),
    ("npm", "test"), ("npm", "run", "test"), ("npm", "run", "lint"), ("npm", "run", "build"),
    ("python3", "-m", "unittest"), ("python3", "-m", "pytest"), ("pytest",),
    ("tsc", "--noEmit"), ("npx", "tsc", "--noEmit"), ("go", "test"),
]
# options that make an otherwise read-only tool run code or write files
LOW_BREAKERS = re.compile(r"^--?(?:pre|output|exec|ext-diff)\b")
SEPARATORS = {";", ";;", "&", "&&", "|", "||", "|&", "(", ")"}
SAFE_REDIRECT_TARGETS = {"/dev/null", "1", "2"}
TMP_DIRS = ("/tmp/", "/private/tmp/")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

def tag(command, cwd=""):
    cmd = (command or "").strip()
    if not cmd:
        return "medium", "empty command"
    for rx, why in HIGH:
        if rx.search(cmd):
            return "high", why
    segments = _segments(cmd)
    for seg in segments:
        why = _rm_risk(seg, cwd)
        if why:
            return "high", why
    if "$(" in cmd or "`" in cmd:
        return "medium", "command substitution"
    for seg in segments:
        why = _not_low(seg)
        if why:
            return "medium", why
    return "low", "allowlisted"

def _tokens(cmd):
    try:
        lx = shlex.shlex(cmd.replace("\n", " ; "), posix=True, punctuation_chars=True)
        lx.whitespace_split = True
        return list(lx)
    except ValueError:                  # unbalanced quotes: rough split, never crash
        return cmd.replace("\n", " ; ").split()

def _segments(cmd):
    segs, cur = [], []
    for tok in _tokens(cmd):
        if tok in SEPARATORS:
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        segs.append(cur)
    return segs

def _rm_risk(seg, cwd):
    """'rm -r' / 'rm outside /tmp' if this segment runs rm anywhere (also `xargs rm`, `git rm`)."""
    idx = [i for i, t in enumerate(seg) if t == "rm" or t.endswith("/rm")]
    if not idx:
        return ""
    args = seg[idx[0] + 1:]
    flags = [a for a in args if a.startswith("-")]
    if "--recursive" in flags or any(not f.startswith("--") and re.search("[rR]", f) for f in flags):
        return "rm -r"
    targets = [a for a in args if not a.startswith("-") and not re.match(r"^[<>]", a)]
    if not targets:
        return "rm outside /tmp"
    for t in targets:
        if t.startswith(("~", "$")) or (not t.startswith("/") and not cwd):
            return "rm outside /tmp"
        path = os.path.normpath(os.path.join(cwd or "/", t))
        if not path.startswith(TMP_DIRS):
            return "rm outside /tmp"
    return ""

def _not_low(seg):
    words = list(seg)
    while words and _ASSIGN.match(words[0]):
        words.pop(0)
    i = 0
    while i < len(words):                      # redirects: only to /dev/null or another fd
        t = words[i]
        if t and set(t) <= set("<>&|") and ("<" in t or ">" in t):     # <, >, >>, &>, >&, <<
            if ">" in t and (i + 1 >= len(words) or words[i + 1] not in SAFE_REDIRECT_TARGETS):
                return "write redirect"
            del words[i:i + 2]
            continue
        i += 1
    if not words:
        return ""
    for prefix in LOW:
        if tuple(words[:len(prefix)]) == prefix:
            if any(LOW_BREAKERS.match(w) for w in words[len(prefix):]):
                return "option runs code or writes files"
            return ""
    return "not on the low allowlist"
