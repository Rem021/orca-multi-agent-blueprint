#!/usr/bin/env python3
"""agent-ship: take a finished task branch all the way to main without asking the user.

Run inside the worktree of a committed task branch:

    agent-ship [--title T] [--body B] [--check CMD ...] [--method squash|merge|rebase]
               [--no-merge] [--force-merge] [--allow-test-skip] [--dry-run]

What it does (serialized per repository via flock so parallel worktrees queue cleanly):
  1. Refuses default/detached branches, restricted branches (`GUARD_RESTRICTED_BRANCHES`, none by
     default), dirty or untracked non-ignored files, ambiguous non-origin remotes, and SHIP_DENY
     repositories.
  2. Scans every commit in `<base>..HEAD` for secrets: patches as text (binary files and
     `-diff`/textconv attributes cannot hide content, merge commits are shown as combined diffs),
     commit messages, and files that usually hold credentials (`.env`, `.netrc`, ...). Output is
     redacted; any match aborts before pushing or merging (secrets added then scrubbed included).
  3. Refuses newly added test skip/disable/focus markers unless `--allow-test-skip`.
  4. Inspects `git diff --name-status -M` (both old and new paths on renames) for high-risk
     signing, billing, schema migration, and CI workflow files and pauses for a decision.
  5. In `--dry-run`, previews commit count, checks, and merge/hold plan without rebasing or
     mutating `HEAD`.
  6. Rebases onto latest `origin/<default>` (or local `<default>` for local-only Git repos) unless
     the branch already contains it, runs the configured check (`~/.orca/ship.json`) plus every
     `--check`, scans the final HEAD again (so commits made by a check are covered), pushes, waits
     for every reported check, and merges exactly the scanned SHA.

Exit codes: 0 merged, 2 refused, 3 rebase conflict, 4 local check failed or test skip added,
5 secret found, 6 CI failed or timed out, 7 merge failed or unexpected error,
10 paused for user decision (PR/branch ready).
"""
import argparse
import fcntl
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

HOME = os.path.expanduser("~")


def _load_guard_env():
    vals = {}
    env_file = os.path.join(HOME, ".orca/guard.env")
    if os.path.isfile(env_file):
        try:
            with open(env_file, encoding="utf-8", errors="ignore") as fh:
                for raw in fh:
                    ln = raw.strip()
                    if not ln or ln.startswith("#") or "=" not in ln:
                        continue
                    if ln.startswith("export "):
                        ln = ln[7:].strip()
                    k, v = ln.split("=", 1)
                    vals[k.strip()] = v.strip().strip('"').strip("'").replace("$HOME", HOME).replace("${HOME}", HOME)
        except OSError:
            pass
    return vals


_GUARD_ENV = _load_guard_env()


def _setting(key, default=""):
    return (os.environ.get(key) or _GUARD_ENV.get(key) or default).strip()


_VOL_BASE = _setting("AI_VOLUME_PATH") or os.path.join(HOME, "AI-Workspace")
LOCK_DIR = os.path.join(_VOL_BASE, "AI/Data/ship-locks")
CONFIG = os.path.join(HOME, ".orca/ship.json")
ORCA = "/Applications/Orca.app/Contents/Resources/bin/orca"
_raw_restricted = _setting("GUARD_RESTRICTED_BRANCHES", "none")
RESTRICTED = () if _raw_restricted.lower() in ("", "none", "off", "false") else tuple(p for p in _raw_restricted.split() if p)
BRANCH_PREFIX = re.sub(r"[^A-Za-z0-9._-]+", "", _setting("DEFAULT_BRANCH_PREFIX"))
DENY = tuple(x.strip() for x in os.environ.get("SHIP_DENY", "").split(",") if x.strip())
CI_TIMEOUT = int(os.environ.get("SHIP_CI_TIMEOUT", "2400"))
CI_RETRY_DELAY = float(os.environ.get("SHIP_CI_RETRY_DELAY", "10"))
LOCK_TIMEOUT = int(os.environ.get("SHIP_LOCK_TIMEOUT", "900"))


def _read_config():
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _effective_deny():
    items = list(DENY)
    extra = _read_config().get("_deny_repos") or []
    if isinstance(extra, list):
        items.extend(str(x).strip() for x in extra if str(x).strip())
    return tuple(items)


def next_task_hint(branch, base):
    """`git switch -c <prefix>/<task> <base>`: configured prefix, else the shipped branch's own prefix."""
    prefix = BRANCH_PREFIX or (branch.split("/", 1)[0] if "/" in branch and branch != "HEAD" else "agent")
    return "git switch -c %s/<task> %s" % (prefix, base)


SECRET_PATTERNS = [
    ("private key", r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----|-----BEGIN PGP PRIVATE KEY BLOCK-{5}"),
    ("AWS access key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("AWS secret key", r"(?i)\baws_secret_access_key\b\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{30,}['\"]?"),
    ("GitHub token", r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    ("GitHub token", r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"),
    ("GitLab token", r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    ("OpenAI/Anthropic key", r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b"),
    ("Stripe live key", r"\b[rs]k_live_[A-Za-z0-9]{16,}\b"),
    ("Slack token", r"\bxox[abeoprs]-[A-Za-z0-9-]{10,}\b|\bxapp-\d-[A-Za-z0-9-]{10,}\b"),
    ("Slack webhook", r"\bhooks\.slack\.com/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/[A-Za-z0-9]{16,}"),
    ("npm token", r"\bnpm_[A-Za-z0-9]{36}\b"),
    ("Hugging Face token", r"\bhf_[A-Za-z0-9]{30,}\b"),
    ("SendGrid key", r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"),
    ("Google API key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("Bearer JWT/token", r"(?i)\bAuthorization\s*:\s*Bearer\s+[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}"),
    (
        "password in URL",
        r"\b[A-Za-z][A-Za-z0-9+.-]{1,20}://[^\s:/@'\"]{1,64}:"
        r"(?![$<{%*]|(?i:password|passwd|pass|secret|changeme|example|xxx+)@)[^\s:/@'\"]{6,}"
        r"@(?!localhost\b|127\.|0\.0\.0\.0\b|host\.docker\.internal\b|example\.(?:com|org|net)\b)[A-Za-z0-9.-]+",
    ),
    (
        "assigned secret",
        r"(?i)(?:['\"]?(?:[A-Za-z0-9_]*?(?:(?:api|openai|anthropic|claude|gemini|google|stripe|github|slack|aws|private|signing|master|encryption)[_-]?key|key[_-]?(?:backup|secret)|secret(?:[_-]?key)?|client[_-]?secret|access[_-]?token|auth[_-]?token|\btoken\b|passw(?:or)?d)[A-Za-z0-9_]*?)['\"]?)"
        r"\s*[:=]\s*"
        r"(?:['\"](?!<|\$\{|\{\{|None\b|null\b|true\b|false\b|changeme|example|your[_-])[^'\"\s]{12,}['\"]"
        r"|(?![\s'\"<${\[{#]|None\b|null\b|true\b|false\b|os\.|env\.|process\.env|sys\.|self\.|cls\.)[A-Za-z0-9/+._~=-]{14,})",
    ),
]
_SECRET_RES = [(kind, re.compile(pat)) for kind, pat in SECRET_PATTERNS]

# Files that hold credentials by convention; committing them is refused whatever their content.
SECRET_FILE_RE = re.compile(
    r"(?i)(?:^|/)(?:"
    r"\.env(?:\.(?!(?:example|sample|template|dist|defaults?|schema)$)[^/]+)?"
    r"|\.netrc|\.pgpass|\.git-credentials"
    r"|[^/]+\.(?:ppk|pfx|tfstate|tfstate\.backup)"
    r")$"
)

HIGH_RISK_PATTERNS = [
    (
        "entitlements/signing",
        r"(?i)(?:^|/)(?:[^/]+\.entitlements|[^/]+\.mobileprovision|[^/]+\.(?:p12|cer|keystore)|Signing[^/]*\.xcconfig|ExportOptions[^/]*\.plist|AppStoreConnect[^/]*|Fastfile|Deliverfile|[^/]+\.xcodeproj/project\.pbxproj|Info\.plist)(?:$|/)",
    ),
    (
        "billing/StoreKit",
        r"(?i)(?:^|/)(?:Billing|IAP|StoreKit)(?:/|$)|(?:^|/)(?:[^/]+\.storekit|[^/]*(?:StoreKit|PurchaseManager|SubscriptionStore)[^/]*\.swift)$",
    ),
    (
        "schema migration",
        r"(?i)(?:^|/)(?:[^/]+\.xcdatamodeld|[^/]+\.xcdatamodel|migrations|drizzle|alembic/versions|db/migrate)(?:/|$)|(?:^|/)schema\.prisma$",
    ),
    (
        "CI workflow",
        r"^\.github/workflows/[^/]+\.(?:yml|yaml)$",
    ),
]

# Patches are read as text with fixed a/ b/ prefixes so no repository attribute or user diff setting
# (`-diff`, `binary`, textconv, external diff, noprefix, mnemonicPrefix, color) can hide or reshape them.
PATCH_FLAGS = ("--text", "--no-textconv", "--no-ext-diff", "--no-color", "--src-prefix=a/", "--dst-prefix=b/")


class Stop(Exception):
    def __init__(self, code, msg):
        Exception.__init__(self, msg)
        self.code = code


def say(msg):
    print("agent-ship: " + msg, flush=True)


def run(cmd, cwd=None, check=True, timeout=None, env=None):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        raise Stop(2, "%s is not installed or not on PATH" % cmd[0])
    if check and p.returncode != 0:
        raise Stop(7, "%s failed: %s" % (" ".join(cmd[:4]), (p.stderr or p.stdout).strip()[:400]))
    return p


def git(cwd, *args, **kw):
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    kw.setdefault("env", env)
    return run(["git", "-c", "core.fsmonitor=false"] + list(args), cwd=cwd, **kw)


def _matches_secret(text):
    for kind, rx in _SECRET_RES:
        if rx.search(text):
            return kind
    return None


def _redact_secret_line(raw_line):
    """Return a non-sensitive identifier preview without echoing raw secret bytes."""
    s = raw_line.strip()
    m = re.match(r'''^([A-Za-z0-9_."'\s-]{1,40}?[:=])''', s)
    if m:
        prefix = m.group(1).strip()
        # The "key" itself can be the secret (`ghp_...: x`) or look like one: never echo it then.
        if not _matches_secret(prefix) and not re.search(r"[A-Za-z0-9]{24,}", prefix):
            return prefix + " <REDACTED>"
    return "<REDACTED>"


def _patch_path(line):
    """Destination path of a `+++` header line, or None when the line is not a header."""
    rest = line[4:].strip()
    if rest == "/dev/null":
        return ""
    if rest.startswith('"') and rest.endswith('"'):
        try:
            rest = rest[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")
        except (UnicodeError, ValueError):
            rest = rest[1:-1]
    return rest[2:] if rest.startswith("b/") else None


def _added_lines(diff_text):
    """Yield (path, body) for every added line of a unified or combined (`--cc`) diff.

    In a combined diff of a merge commit only lines new relative to *every* parent are yielded: those
    are what the merge itself introduced (conflict resolution, evil merge); lines taken from one
    parent were already scanned in that parent's commits or are on the base branch."""
    cur_file = ""
    parents = 1
    for line in diff_text.splitlines():
        if line.startswith("diff "):
            parents = 2 if line.startswith(("diff --cc ", "diff --combined ")) else 1
            continue
        if line.startswith("+++ "):
            path = _patch_path(line)
            if path is not None:
                cur_file = path
                continue
        if line.startswith("@@"):
            parents = max(1, len(line) - len(line.lstrip("@")) - 1)
            continue
        if line.startswith("+" * parents):
            yield cur_file, line[parents:]


def scan_secrets(diff_text):
    """Findings in the added lines of a unified diff / git log -p: [(kind, redacted_context)]."""
    hits = []
    for cur_file, body in _added_lines(diff_text):
        kind = _matches_secret(body)
        if kind:
            preview = _redact_secret_line(body)
            hits.append((kind, ("%s: %s" % (cur_file, preview)) if cur_file else preview))
    return hits


def scan_commit_messages(messages):
    """Commit messages are pushed too: scan every line of `git log --format=%B`."""
    hits = []
    for body in messages.splitlines():
        kind = _matches_secret(body)
        if kind:
            hits.append((kind, "commit message: " + _redact_secret_line(body)))
    return hits


def scan_secret_paths(paths):
    return [("credential file", p) for p in paths if SECRET_FILE_RE.search(p.strip())]


def collect_changed_paths(top, rev_range):
    """Return all touched paths across `rev_range`, including both old and new paths on renames."""
    out = git(top, "diff", "--name-status", "-M", rev_range).stdout
    paths = []
    seen = set()
    for raw in out.splitlines():
        parts = raw.strip().split("\t")
        for p in parts[1:]:
            p = p.strip()
            if p and p not in seen:
                seen.add(p)
                paths.append(p)
    return paths


def collect_added_paths(top, rev_range):
    """Paths any commit in `rev_range` added or modified (also ones a later commit deleted again)."""
    out = git(top, "log", "--format=", "--name-only", "--no-renames", "--diff-filter=ACMRT", "-z", rev_range).stdout
    return sorted({p.strip("\n") for p in out.split("\0") if p.strip("\n")})


def scan_high_risk_paths(paths):
    """Return [(category, path)] for files that require explicit human sign-off."""
    hits = []
    for p in paths:
        p_norm = p.strip()
        if not p_norm:
            continue
        for kind, pat in HIGH_RISK_PATTERNS:
            if re.search(pat, p_norm):
                hits.append((kind, p_norm))
                break
    return hits


_TEST_DIR_NAMES = frozenset({"test", "tests", "__tests__", "spec", "specs", "uitests"})
_TEST_FILE_SUFFIXES = (
    "tests.swift", "test.swift", "_test.py", "_tests.py", "_test.go", "_test.rs",
    ".test.ts", ".test.tsx", ".test.js", ".test.jsx", ".test.mjs", ".test.cjs",
    ".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx", ".spec.mjs", ".spec.cjs",
)
# Files that can switch tests off without touching a test file.
_TEST_CONFIG_FILES = frozenset({"conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml"})
TEST_SKIP_PATTERNS = [
    ("unittest skip/xfail",
     r"\bunittest\.(?:skip(?:If|Unless)?|expectedFailure)\b|@(?:skip(?:If|Unless)?|expectedFailure)\b"
     r"|\bself\.skipTest\s*\(|\braise\s+(?:unittest\.)?SkipTest\b"),
    ("pytest skip/xfail", r"\bpytest\.mark\.(?:skip(?:if)?|xfail)\b|\bpytest\.(?:skip|xfail)\s*\("),
    ("pytest collection", r"^\s*collect_ignore(?:_glob)?\s*(?:\+?=|\.(?:append|extend)\s*\()|--deselect\b"),
    ("XCTSkip / disabled",
     r"\bXCTSkip(?:If|Unless)?\s*\(|\bXCTExpectFailure\s*\(|@(?:Test|Suite)\s*\([^)]*\.disabled\b"),
    ("JS/TS skip/only",
     r"\b(?:it|test|describe|context|suite)\.(?:skip|only|todo)\s*\("
     r"|(?<![\w.$])(?:xit|xtest|xdescribe|xcontext|fit|fdescribe|ftest)\s*\("),
    ("Rust #[ignore]", r"#\s*\[\s*ignore\b"),
    ("Go t.Skip", r"\b[tb]\.Skip(?:f|Now)?\s*\("),
]


def _is_test_file(path):
    p = path.strip().replace("\\", "/")
    if not p:
        return False
    parts = [seg.lower() for seg in p.split("/") if seg]
    if any(seg in _TEST_DIR_NAMES for seg in parts[:-1]):
        return True
    base = parts[-1]
    return (base.startswith(("test_", "spec_")) or base.endswith(_TEST_FILE_SUFFIXES)
            or base in _TEST_CONFIG_FILES)


def scan_test_skip_additions(diff_text):
    """Detect newly added test-skip/disable markers in added lines of test files: [(kind, file:snippet)]."""
    hits = []
    for cur_file, raw in _added_lines(diff_text):
        if not _is_test_file(cur_file):
            continue
        body = raw.strip()
        if not body:
            continue
        if body.startswith(("//", "/*", "*")):
            continue
        if body.startswith("#") and not re.match(r"^#\s*\[\s*ignore\b", body):
            continue
        for kind, pat in TEST_SKIP_PATTERNS:
            if re.search(pat, body):
                hits.append((kind, "%s: %s" % (cur_file, body[:80])))
                break
    return hits


def inspect_range(top, base, args, label):
    """Run every content gate over `<base>..HEAD`; raise Stop on secrets or test skips, return risk hits."""
    rng = base + "..HEAD"
    patches = git(top, "log", "-p", "--cc", *PATCH_FLAGS, rng).stdout
    messages = git(top, "log", "--format=%B", rng).stdout
    hits = scan_secret_paths(collect_added_paths(top, rng)) + scan_secrets(patches) + scan_commit_messages(messages)
    if hits:
        raise Stop(5, "possible secrets in %s, nothing was pushed or merged: %s"
                   % (label, "; ".join("%s (%s)" % h for h in hits[:5])))
    if not getattr(args, "allow_test_skip", False):
        net_diff = git(top, "diff", *PATCH_FLAGS, base + "...HEAD").stdout
        skip_hits = scan_test_skip_additions(net_diff)
        if skip_hits:
            raise Stop(
                4,
                "test skip/disable marker(s) added in branch diff (pass --allow-test-skip if intentional): "
                + "; ".join("%s (%s)" % h for h in skip_hits[:5]),
            )
    return scan_high_risk_paths(collect_changed_paths(top, base + "...HEAD"))


def rebase_onto(top, base):
    """Rebase on `base` unless HEAD already contains it (a branch that merged `base` in stays as is)."""
    if git(top, "merge-base", "--is-ancestor", base, "HEAD", check=False).returncode == 0:
        return False
    if git(top, "rebase", base, check=False).returncode != 0:
        git(top, "rebase", "--abort", check=False)
        raise Stop(3, "rebase onto %s hit a conflict and was aborted. Run `git rebase %s` (or `git merge %s`) "
                      "in this worktree, resolve, commit, and run agent-ship again" % (base, base, base))
    return True


def choose_method(allowed, wanted=None):
    """allowed: {"squash": bool, "merge": bool, "rebase": bool}."""
    if wanted:
        if not allowed.get(wanted):
            raise Stop(7, "the repository does not allow %s merges" % wanted)
        return wanted
    for m in ("squash", "merge", "rebase"):
        if allowed.get(m):
            return m
    raise Stop(7, "the repository allows no merge method")


def refuse_branch(branch, default):
    if branch in ("HEAD", default, "main", "master"):
        return "on %s: commit the task on its own branch first (%s)" % (branch, next_task_hint(branch, default))
    for pat in RESTRICTED:
        if branch == pat or fnmatch.fnmatchcase(branch, pat):
            return "%s matches GUARD_RESTRICTED_BRANCHES (%s); it is never pushed" % (branch, pat)
    return None


_BENIGN_UNTRACKED_PREFIXES = ("__pycache__/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/")


def ensure_clean_worktree(top):
    status = git(top, "status", "--porcelain").stdout.strip()
    if not status:
        return
    lines = []
    for ln in status.splitlines():
        if not ln.strip():
            continue
        if ln.startswith("?? "):
            rel = ln[3:].strip().strip('"')
            base = os.path.basename(rel.rstrip("/"))
            if base == ".DS_Store" or base.endswith(".pyc") or any(
                rel == p.rstrip("/") or rel.startswith(p) or ("/" + p) in rel for p in _BENIGN_UNTRACKED_PREFIXES
            ):
                continue
        lines.append(ln)
    if not lines:
        return
    if all(ln.startswith("?? ") for ln in lines):
        sample = lines[0][3:].strip()
        raise Stop(
            2,
            "untracked file(s) present (e.g. %s); commit, add to .git/info/exclude, or move scratch files to work/_scratch/ first"
            % sample,
        )
    raise Stop(2, "uncommitted or untracked changes in worktree; commit them (or stash) first")


def github_slug(url):
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url.strip())
    return m.group(1) if m else None


def gh_json(args, cwd=None):
    p = run(["gh"] + args, cwd=cwd)
    try:
        return json.loads(p.stdout) if p.stdout.strip() else None
    except ValueError:
        raise Stop(7, "gh %s returned invalid JSON" % " ".join(args[:2]))


_EXPLICIT_PR_OR_PUSH_RE = re.compile(r"\b(?:push|pull_request|pull_request_target)\b")


def _extract_workflow_on_block(txt):
    """Extract only the top-level `on:` trigger block from a GitHub Actions workflow YAML."""
    lines = txt.splitlines()
    on_lines = []
    in_on = False
    for raw in lines:
        stripped = raw.split("#", 1)[0].rstrip()
        if not stripped.strip():
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if not in_on:
            m = re.match(r"^(?:on|['\"]on['\"]|true)\s*:(.*)$", stripped.strip())
            if indent == 0 and m:
                in_on = True
                if m.group(1).strip():
                    on_lines.append(m.group(1).strip())
        else:
            if indent == 0:
                break
            on_lines.append(stripped.strip())
    return "\n".join(on_lines)


def has_ci(top):
    """True when `.github/workflows` has at least one workflow that triggers on push/PR (or stub/unreadable fail-closed)."""
    d = os.path.join(top, ".github/workflows")
    if not os.path.isdir(d):
        return False
    wf_files = [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith((".yml", ".yaml"))]
    if not wf_files:
        return False
    for wf in wf_files:
        try:
            with open(wf, encoding="utf-8", errors="ignore") as fh:
                txt = fh.read()
        except OSError:
            return True
        clean = "\n".join(ln.split("#", 1)[0] for ln in txt.splitlines()).strip()
        if not clean:
            return True
        on_block = _extract_workflow_on_block(txt)
        if not on_block:
            if _EXPLICIT_PR_OR_PUSH_RE.search(clean):
                return True
        elif _EXPLICIT_PR_OR_PUSH_RE.search(on_block):
            return True
    return False


def configured_checks(args, key):
    """The check configured for this repository in ~/.orca/ship.json, followed by every --check."""
    checks = []
    configured = _read_config().get(key)
    if isinstance(configured, str) and configured.strip():
        checks.append(configured.strip())
    for extra in getattr(args, "check", None) or []:
        if extra and extra.strip() and extra.strip() not in checks:
            checks.append(extra.strip())
    return checks


def run_checks(top, checks, what):
    for check in checks:
        say("local check: " + check)
        p = subprocess.run(check, shell=True, cwd=top)
        if p.returncode != 0:
            raise Stop(4, "local check failed (exit %d); %s" % (p.returncode, what))


def orca_status(top, pr=None, status="completed", unread=False):
    """Best effort: the Orca workspace badge must never turn a finished ship into a failure."""
    if not os.path.exists(ORCA):
        return
    cmd = [ORCA, "worktree", "set", "--worktree", "path:" + top, "--workspace-status", status]
    if pr is not None:
        cmd += ["--pr", str(pr)]
    if unread:
        cmd.append("--unread")
    cmd.append("--json")
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass


def resolve_main_clone(top):
    common = git(top, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    return os.path.dirname(common) if os.path.basename(common) == ".git" else None


def open_lock(name):
    try:
        os.makedirs(LOCK_DIR, exist_ok=True)
        return open(os.path.join(LOCK_DIR, name), "w", encoding="utf-8")
    except OSError as exc:
        raise Stop(2, "cannot create the ship lock in %s (%s); is the workspace volume mounted?" % (LOCK_DIR, exc.strerror or exc))


def acquire_lock(lock, label):
    deadline = time.monotonic() + max(1, LOCK_TIMEOUT)
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise Stop(7, "timed out after %ds waiting for %s ship lock" % (LOCK_TIMEOUT, label))
            time.sleep(0.5)


def _hold_reason(args, risk_hits):
    return "--no-merge" if args.no_merge else ("high-risk files: " + ", ".join("%s (%s)" % h for h in risk_hits[:4]))


def ship_local_only(args, top, branch):
    main_clone = resolve_main_clone(top) or top
    repo_name = os.path.basename(main_clone.rstrip(os.sep))
    if any(d and d in (repo_name,) + tuple(top.split(os.sep)) for d in _effective_deny()):
        raise Stop(2, "local/%s is excluded from automatic shipping (SHIP_DENY)" % repo_name)

    default = None
    for candidate in ("main", "master"):
        if git(top, "show-ref", "--verify", "--quiet", "refs/heads/" + candidate, check=False).returncode == 0:
            default = candidate
            break
    if default is None:
        raise Stop(2, "local-only repository has neither a main nor a master branch")
    why = refuse_branch(branch, default)
    if why:
        raise Stop(2, why)
    ensure_clean_worktree(top)

    with open_lock("local__" + re.sub(r"[^A-Za-z0-9_.-]+", "_", repo_name) + ".lock") as lock:
        return _ship_local_only_locked(args, top, branch, main_clone, repo_name, default, lock)


def _ship_local_only_locked(args, top, branch, main_clone, repo_name, default, lock):
    say("waiting for the local/%s queue" % repo_name)
    acquire_lock(lock, "local/" + repo_name)
    say("local/%s: %s -> %s" % (repo_name, branch, default))

    if git(top, "rev-list", "--count", default + "..HEAD").stdout.strip() in ("", "0"):
        raise Stop(2, "nothing to ship: %s has no commits that %s lacks" % (branch, default))

    risk_hits = inspect_range(top, default, args, "commit history")
    say("secret scan: clean")

    if args.dry_run:
        hold = args.no_merge or (bool(risk_hits) and not args.force_merge)
        say("dry run: would rebase onto %s and %s local %s from %s" % (
            default, "pause review for" if hold else "fast-forward", default, branch))
        return 0

    rebase_onto(top, default)
    run_checks(top, configured_checks(args, "local/" + repo_name), "main was not updated")
    ensure_clean_worktree(top)
    # Checks may commit (formatters, generators): the final HEAD is what gets merged, so scan it again.
    risk_hits = inspect_range(top, default, args, "the final branch")
    hold = args.no_merge or (bool(risk_hits) and not args.force_merge)

    if hold:
        orca_status(top, status="in-review", unread=True)
        say("paused before merging into local %s (%s); waiting for user decision" % (default, _hold_reason(args, risk_hits)))
        return 10

    head_sha = git(top, "rev-parse", "HEAD").stdout.strip()
    if top == main_clone:
        # Running directly inside the main clone on a task branch: switch back to `default`, fast-forward, and delete task branch
        git(main_clone, "switch", "-q", default)
        git(main_clone, "merge", "-q", "--ff-only", head_sha)
        git(main_clone, "branch", "-D", branch, check=False)
    else:
        if git(main_clone, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip() == default:
            if git(main_clone, "status", "--porcelain", "--untracked-files=no").stdout.strip():
                raise Stop(7, "main clone (%s) has uncommitted tracked changes; cannot fast-forward %s safely" % (main_clone, default))
            git(main_clone, "merge", "-q", "--ff-only", head_sha)
        else:
            git(top, "fetch", "-q", ".", "%s:refs/heads/%s" % (head_sha, default))
        git(top, "switch", "-q", "--detach", default, check=False)
        git(top, "branch", "-D", branch, check=False)

    orca_status(top, status="completed")
    say("fast-forwarded local %s (%s). Next task here: %s" % (default, head_sha[:8], next_task_hint(branch, default)))
    return 0


def ship(args):
    top = git(os.getcwd(), "rev-parse", "--show-toplevel").stdout.strip()
    branch = git(top, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    url = git(top, "remote", "get-url", "origin", check=False).stdout.strip()
    if not url:
        other_remotes = [r.strip() for r in git(top, "remote", check=False).stdout.splitlines() if r.strip()]
        if other_remotes:
            raise Stop(
                2,
                "no 'origin' remote configured (found: %s); refusing to treat remote-backed repo as local-only"
                % ", ".join(other_remotes),
            )
        return ship_local_only(args, top, branch)

    slug = github_slug(url)
    if not slug:
        raise Stop(2, "origin remote (%s) is not a GitHub URL in %s" % (url, top))
    if any(d and (d in top.split(os.sep) or slug.endswith("/" + d)) for d in _effective_deny()):
        raise Stop(2, "%s is excluded from automatic shipping (SHIP_DENY)" % slug)
    repo = gh_json(["api", "repos/" + slug]) or {}
    default = repo.get("default_branch") or "main"
    why = refuse_branch(branch, default)
    if why:
        raise Stop(2, why)
    ensure_clean_worktree(top)

    with open_lock(slug.replace("/", "__") + ".lock") as lock:
        return _ship_github_locked(args, top, branch, slug, repo, default, lock)


def wait_for_ci(top, pr, pr_url, slug, head_sha, expect_ci):
    """Wait for every check GitHub reports on the PR. Without workflows in the repository, external CI
    or required status checks can still report: only "no checks reported" lets the merge go ahead."""
    attempts = 3 if expect_ci else 1
    say("waiting for GitHub checks (up to %d min)" % (CI_TIMEOUT // 60))
    p = None
    for _ in range(attempts):
        time.sleep(CI_RETRY_DELAY)
        try:
            p = run(["gh", "pr", "checks", str(pr), "-R", slug, "--watch", "--fail-fast", "--interval", "20"],
                    check=False, timeout=CI_TIMEOUT)
        except subprocess.TimeoutExpired:
            orca_status(top, pr=pr, status="in-review", unread=True)
            raise Stop(6, "CI did not finish in time; PR %s stays open" % pr_url)
        if "no checks reported" not in ((p.stdout or "") + (p.stderr or "")).lower():
            break
    if "no checks reported" in ((p.stdout or "") + (p.stderr or "")).lower():
        if not expect_ci:
            say("no checks reported on %s; merging without CI" % pr_url)
            return
        orca_status(top, pr=pr, status="in-review", unread=True)
        raise Stop(
            6,
            "expected GitHub CI checks on %s (head %s), but no checks were reported after %d attempts; PR stays open"
            % (pr_url, head_sha[:8], attempts),
        )
    if p.returncode != 0:
        orca_status(top, pr=pr, status="in-review", unread=True)
        raise Stop(6, "CI failed on %s; fix on the branch and run again" % pr_url)


def _ship_github_locked(args, top, branch, slug, repo, default, lock):
    say("waiting for the %s queue" % slug)
    acquire_lock(lock, slug)
    say("%s: %s -> %s" % (slug, branch, default))

    git(top, "fetch", "-q", "origin", default)
    base = "origin/" + default
    if git(top, "rev-list", "--count", base + "..HEAD").stdout.strip() in ("", "0"):
        raise Stop(2, "nothing to ship: %s has no commits that %s lacks" % (branch, base))

    risk_hits = inspect_range(top, base, args, "commit history")
    say("secret scan: clean")
    checks = configured_checks(args, slug)

    if args.dry_run:
        hold = args.no_merge or (bool(risk_hits) and not args.force_merge)
        subjects = git(top, "log", "--format=%s", base + "..HEAD").stdout.strip().splitlines()
        say("dry run: would push %d commit(s), open a PR (%s), run %d local check(s), %s, then %s" % (
            len(subjects), subjects[0][:60] if subjects else branch, len(checks),
            "wait for CI" if has_ci(top) else "wait for any reported checks",
            "pause for user review" if hold else "merge"))
        return 0

    rebase_onto(top, base)
    run_checks(top, checks, "nothing was pushed")
    ensure_clean_worktree(top)
    # Checks may commit (formatters, generators): scan the HEAD that is about to be pushed.
    risk_hits = inspect_range(top, base, args, "the final branch")
    hold = args.no_merge or (bool(risk_hits) and not args.force_merge)
    ci = has_ci(top)
    subjects = git(top, "log", "--format=%s", base + "..HEAD").stdout.strip().splitlines()

    head_sha = git(top, "rev-parse", "HEAD").stdout.strip()
    git(top, "push", "-q", "--force-with-lease", "-u", "origin", head_sha + ":refs/heads/" + branch)
    prs = gh_json(["pr", "list", "-R", slug, "--head", branch, "--state", "open", "--json", "number,url,baseRefName"]) or []
    if prs:
        pr, pr_url = prs[0]["number"], prs[0]["url"]
        pr_base = prs[0].get("baseRefName") or default
        if pr_base != default:
            raise Stop(2, "existing open PR #%d targets %r instead of default branch %r; retarget or close it first" % (pr, pr_base, default))
        say("reusing PR #%d" % pr)
    else:
        title = args.title or (subjects[-1] if len(subjects) == 1 else branch.split("/", 1)[-1].replace("-", " "))
        body = args.body or "\n".join("- " + s for s in reversed(subjects))
        footer = "Paused for user decision (`%s`)." % _hold_reason(args, risk_hits) if hold \
            else "Opened and merged by agent-ship (no human review requested)."
        body += "\n\nChecks: %s%s\n\n%s" % (
            ("local " + ", ".join("`%s`" % c for c in checks)) if checks else "none configured locally",
            "; GitHub CI" if ci else "", footer)
        out = run(["gh", "pr", "create", "-R", slug, "--base", default, "--head", branch, "--title", title, "--body", body]).stdout
        pr_url = out.strip().splitlines()[-1]
        pr = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
        say("opened %s" % pr_url)
    if hold:
        orca_status(top, pr=pr, status="in-review", unread=True)
        say("paused (%s): PR %s awaits user decision" % (_hold_reason(args, risk_hits), pr_url))
        return 10

    wait_for_ci(top, pr, pr_url, slug, head_sha, ci)
    allowed = {"squash": repo.get("allow_squash_merge"), "merge": repo.get("allow_merge_commit"),
               "rebase": repo.get("allow_rebase_merge")}
    method = choose_method(allowed, args.method)
    run(["gh", "pr", "merge", str(pr), "-R", slug, "--" + method, "--match-head-commit", head_sha])
    say("merged %s (%s @ %s)" % (pr_url, method, head_sha[:8]))
    run(["gh", "api", "-X", "DELETE", "repos/%s/git/refs/heads/%s" % (slug, urllib.parse.quote(branch, safe="/"))], check=False)

    git(top, "fetch", "-q", "origin", default, check=False)
    main_clone = resolve_main_clone(top)
    if top == main_clone:
        git(main_clone, "switch", "-q", default, check=False)
        git(main_clone, "merge", "-q", "--ff-only", base, check=False)
        git(main_clone, "branch", "-D", branch, check=False)
    else:
        if main_clone and git(main_clone, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip() == default \
                and not git(main_clone, "status", "--porcelain", "--untracked-files=no").stdout.strip():
            if git(main_clone, "merge", "-q", "--ff-only", base, check=False).returncode == 0:
                say("main clone fast-forwarded")
        git(top, "switch", "-q", "--detach", base, check=False)
        if main_clone:
            git(top, "branch", "-D", branch, check=False)
    orca_status(top, pr=pr, status="completed")
    say("done. Next task here: %s" % next_task_hint(branch, base))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--title")
    ap.add_argument("--body")
    ap.add_argument("--check", action="append",
                    help="extra local check run in the worktree before pushing, after the one configured in "
                         "~/.orca/ship.json (repeatable)")
    ap.add_argument("--method", choices=("squash", "merge", "rebase"))
    ap.add_argument("--no-merge", action="store_true", help="open the PR and stop (a decision for the user)")
    ap.add_argument("--force-merge", action="store_true", help="merge even when high-risk files were modified (after explicit user approval)")
    ap.add_argument("--allow-test-skip", action="store_true", help="allow newly added test skip/disable markers in the branch diff")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        return ship(args)
    except Stop as e:
        say("STOP: " + str(e))
        return e.code
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as e:
        say("STOP: unexpected error: %s: %s" % (type(e).__name__, e))
        return 7


if __name__ == "__main__":
    sys.exit(main())
