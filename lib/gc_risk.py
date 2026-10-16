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
    why = catastrophic(cmd, cwd)
    if why:
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


# ---------------------------------------------------------------- catastrophic
# Held even in a turn the human typed. Short on purpose: everything else in a
# human-typed turn runs without asking.
SAFE_INSTALL_HOSTS = ("brew.sh", "raw.githubusercontent.com/Homebrew/", "sh.rustup.rs", "astral.sh/",
                      "bun.sh/", "deno.land/", "get.pnpm.io/", "install.python-poetry.org")
SECRET_PATHS = re.compile(
    r"\.ssh/(?!known_hosts)|id_rsa\b|id_ed25519\b|id_ecdsa\b|id_dsa\b|\.pem\b|\.p12\b|\.pfx\b|\.jks\b|\.keystore\b|\.gpg\b|\.asc\b"
    r"|\.netrc\b|\.npmrc\b|\.pypirc\b|\.aws/|\.gnupg/|\.kube/|\.docker/config|\.config/gh/hosts|\.config/gcloud/"
    r"|\.config/(?:op|hub|openai|anthropic|github-copilot|configstore)/?|\.1password/|\.terraform\.d/|\.grok-cursor/watch\.env"
    r"|\.(?:claude|codex|vercel|railway|supabase|openai|anthropic)(?:[/.]|\b)|com\.vercel\.cli|\.cursor/mcp\.json"
    r"|\w+_history\b|state\.vscdb|Cookies\.binarycookies|Library/Keychains|/etc/(?:shadow|master\.passwd)|/proc/self/environ"
    r"|credentials?\.(?:json|db|tfrc\.json)|service-account|adminsdk|token\.json|access-token|auth\.json|hosts\.yml")
CATASTROPHIC = [
    (re.compile(r"\bgit\b" + _SEG + r"\bpush\b" + _SEG
                + r"(?:--force\b|--force-with-lease\b|--force-if-includes\b|--mirror\b|--prune\b|--delete\b"
                + r"|\s-[a-zA-Z]*[fd][a-zA-Z]*\b|\s\+\S|\s:\S)"),
     "force-push"),
    (re.compile(r"\bgit\b" + _SEG + r"\breset\b" + _SEG + r"--(?:hard|keep|merge)\b"), "git reset --hard"),
    (re.compile(r"\bgit\b" + _SEG + r"\b(?:clean\s+-[a-zA-Z]*[fdx]|stash\s+(?:drop|clear)\b|branch\s+-[a-zA-Z]*D|update-ref\s+-d\b"
                + r"|reflog\s+expire\b|checkout\s+-[a-zA-Z]*B\b|restore\s+\.|checkout\s+--\s+\.)"), "git history/worktree wipe"),
    (re.compile(r"\b(?:find\s+(?:~|\$\{?HOME\}?|/)\S*" + _SEG + r"\s-delete\b|rsync\b" + _SEG + r"--delete\b" + _SEG
                + r"\s(?:~|\$\{?HOME\}?|/Users/)|shred\b)"), "bulk delete"),
    (re.compile(r"(?i)\.cursor/(?:permissions|mcp|cli-config|settings)\.json|\.cursor/\*"), "cursor config"),
    (re.compile(r"\b(?:mv|rm|find|chmod|chown)\b" + _SEG + r"\s(?:~|\$\{?HOME\}?|/Users/[^/\s]+)/\.cursor/?(?:\s|$|\*)"), "cursor config"),
    (re.compile(r"\b(?:csrutil|spctl)\b|\btmutil\s+(?:delete|disable)\b|\bcrontab\s+-r\b|\bkill\s+-9\s+-1\b"
                + r"|(?:^|[;&|(]\s*)(?:sudo\s+)?(?:shutdown|reboot|halt)\b|\bsecureErase\b|\bunmountDisk\s+force\b"
                + r"|\b(?:mke2fs|asr\s+restore|hdiutil\s+erase)\b|>\s*/dev/(?:r?disk|sd)"), "system wipe"),
    (re.compile(r"\b(?:mkfs(?:\.\w+)?|newfs\w*|fdisk|gpt)\b"), "disk format"),
    (re.compile(r"\bdiskutil\b" + _SEG + r"\b(?:erase\w*|partition\w*|zero\w*|reformat|apfs\s+delete\w*)\b", re.I),
     "disk erase"),
    (re.compile(r"\bdd\b" + _SEG + r"\bof=/dev/"), "dd to device"),
    (re.compile(r"\bsecurity\b" + _SEG + r"\b(?:find-(?:generic|internet)-password|dump-keychain|export)\b"),
     "keychain read"),
    (re.compile(r"(?:^|[\s/'\"=<>])\.env(?:\.(?!example\b|sample\b|template\b)[\w.-]+)?(?=$|[\s'\";|&)])"),
     "secret file (.env)"),
    (SECRET_PATHS, "secret file"),
    (re.compile(r"\.envrc\b(?!\.(?:example|sample|template)\b)|\.env_\w+|(?:^|\s)\.e(?:n)?[*?]|\*\.env\b|(?i:\.cursor/perm\S*)"), "secret file (.env)"),
    (re.compile(r"(?i)permissions\.json"), "permissions.json"),
    (re.compile(r"\bgh\s+(?:repo|release|secret|variable|gist|codespace)\s+delete\b|\bgh\s+api\b[^;&|\n]*-X\s*DELETE\b"
                r"|\bterraform\s+(?:destroy\b|apply\b[^;&|\n]*-destroy\b)|\bpulumi\s+(?:destroy|stack\s+rm)\b"
                r"|\bsupabase\s+(?:db\s+reset|projects\s+delete|branches\s+delete|storage\s+rm)\b"
                r"|\brailway\s+(?:down|delete|environment\s+delete|volume\s+delete)\b"
                r"|\bvercel\s+(?:remove|rm|project\s+(?:rm|remove)|domains?\s+(?:rm|remove))\b"
                r"|\b(?:fly|flyctl)\s+(?:apps\s+destroy|destroy)\b|\bheroku\s+apps:destroy\b"
                r"|\baws\s+(?:s3\s+rb\b|s3\s+rm\b[^;&|\n]*--recursive|rds\s+delete-db|dynamodb\s+delete-table|cloudformation\s+delete-stack)"
                r"|\bgcloud\b[^;&|\n]*\s(?:delete|destroy)\b|\bwrangler\b[^;&|\n]*\s(?:delete|rm)\b"
                r"|\bkubectl\s+delete\s+(?:ns|namespace|all|--all)\b|\bdropdb\b"
                r"|(?i:\bdrop\s+(?:database|schema|table)\b|\btruncate\s+table\b)"), "cloud/data destroy"),
    (re.compile(r"(?:^|[;&|(]\s*)(?:env|printenv|export|set)\s*(?:$|[;&|)])|\bgh\s+auth\s+(?:token|status\s+--show-token)\b"
                r"|\bop\s+read\b|\bprint-access-token\b|\bgit\s+credential\s+fill\b|\bvercel\s+env\s+pull\b"
                r"|\b(?:supabase|doppler)\s+secrets\b|aws_secret_access_key"), "secret dump"),
]
_SHELL = (r"(?:sudo\s+(?:-\S+\s+)*|doas\s+|env\s+|exec\s+|command\s+|nohup\s+|busybox\s+|/\S*/)*"
          r"(?:(?:ba|z|da|k|c|tc|fi)?sh\b|(?:python\d?|perl|ruby|node|php|osascript)(?=\s*(?:-\s|-$|$|[;&|)])))")
_PIPE_SH = re.compile(r"\b(?:curl|wget|fetch)\b([^;&\n|]*)(?:\|[^;&\n|]*)*?\|&?\s*" + _SHELL)
_SUBST_SH = re.compile(r"(?:<\(|\$\(|`)\s*(?:curl|wget)\b([^)`]*)")
_URL = re.compile(r"""https?://([^/\s'"]+)(/[^\s'"]*)?""")

def _safe_host(text):
    """True only if every URL in `text` is on a known installer host (host match, not substring)."""
    urls = _URL.findall(text)
    if not urls:
        return False
    for host, path in urls:
        ok = False
        for h in SAFE_INSTALL_HOSTS:
            hh, _, hp = h.partition("/")
            if (host == hh or host.endswith("." + hh)) and (path or "/").startswith("/" + hp if hp else "/"):
                ok = True
        if not ok:
            return False
    return True

def _strip_quotes(cmd):
    """Drop quote characters and backslash escapes so `cat .e''nv` matches like `cat .env`."""
    return re.sub(r"\\(.)", r"\1", re.sub(r"""["'`]""", "", cmd))

_DOC_COPY = re.compile(r"(?i)\S+(?:\.(?:example|sample|template|md|txt)|readme)(?=\s|$)")

def catastrophic(command, cwd=""):
    """Reason string if `command` stays held even in a human-typed turn, else ''."""
    raw = str(command or "")
    for cmd in (raw, _strip_quotes(raw)):
        cmd = _DOC_COPY.sub("", cmd)
        for rx, why in CATASTROPHIC:
            if rx.search(cmd):
                return why
        for m in _PIPE_SH.finditer(cmd):
            if not _safe_host(m.group(1)):
                return "curl | sh from unknown host"
        for m in _SUBST_SH.finditer(cmd):
            if not _safe_host(m.group(1)):
                return "curl | sh from unknown host"
    here = cwd
    for seg in _segments(raw):
        if seg and seg[0] in ("cd", "pushd"):
            here = _after_cd(seg, here)       # follow the cd; unknown target = unknown cwd
            continue
        why = _rm_catastrophic(seg, here)
        if why:
            return why
        for i, t in enumerate(seg):          # `sh -c '...'`: look inside the quoted body
            if t == "-c" and 1 <= i < len(seg) - 1 and re.search(r"(?:^|/)(?:ba|z|da|k)?sh$", seg[i - 1]):
                why = catastrophic(seg[i + 1], cwd)
                if why:
                    return why
    return ""

def _after_cd(seg, cwd):
    """The directory after `cd X`: absolute or ~-relative targets are known, anything else only if cwd is."""
    target = next((a for a in seg[1:] if not a.startswith("-")), "")
    if not target or target.startswith("$"):
        return ""
    target = re.sub(r"^\$\{?HOME\}?", os.path.expanduser("~"), target)
    target = os.path.expanduser(target)
    if os.path.isabs(target):
        out = os.path.normpath(target)
    else:
        out = os.path.normpath(os.path.join(cwd, target)) if cwd else ""
    home = os.path.expanduser("~")
    return "" if out in ("/", home) or home.startswith(out + "/") else out   # ~ or above is no workspace

def _rm_catastrophic(seg, cwd):
    idx = [i for i, t in enumerate(seg) if t == "rm" or t.endswith("/rm")]
    if not idx:
        return ""
    args = seg[idx[0] + 1:]
    flags = [a for a in args if a.startswith("-")]
    recursive = "--recursive" in flags or any(not f.startswith("--") and re.search("[rR]", f) for f in flags)
    home = os.path.expanduser("~")
    root = os.path.realpath(cwd) if cwd else ""
    seen_dashdash = False
    for a in args:
        if a == "--":
            seen_dashdash = True
            continue
        if (a.startswith("-") and not seen_dashdash) or re.match(r"^[<>]", a):
            continue
        raw = t = a
        t = re.sub(r"^\$\{?HOME\}?", home, t)
        t = os.path.expanduser(t)
        if t.startswith("$") or "*" in raw and raw.strip("/*") in ("", "~", "$HOME"):
            return "rm of / or ~"
        path = os.path.normpath(os.path.join(cwd or "/", t))
        if path in ("/", home) or (recursive and home.startswith(path + "/")):
            return "rm of / or ~"
        path = os.path.realpath(path)          # macOS: /tmp -> /private/tmp, like `root`
        if recursive and raw.rstrip("/") in (".", "./*", "*"):
            return "rm -r of the workspace"
        if not recursive and not os.path.isabs(t) and ".." not in raw.split("/") and "*" not in raw \
                and root != home and (not root or not home.startswith(root + "/")):
            continue                           # one named file under cwd (not ~ or /): never catastrophic
        if root and (path == root or ".." in raw.split("/")):
            return "rm -r outside workspace" if recursive else "rm outside workspace"
        if path.startswith(TMP_DIRS) or path.startswith("/var/folders/"):
            continue
        if not root or not path.startswith(root + "/"):
            return "rm -r outside workspace" if recursive else "rm outside workspace"
    return ""
