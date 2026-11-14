#!/usr/bin/env python3
"""agent-ship: take a finished task branch all the way to main without asking the user.

Run inside the worktree of a committed task branch:

    agent-ship [--title T] [--body B] [--check CMD] [--method squash|merge|rebase]
               [--no-merge] [--force-merge] [--dry-run]

What it does (serialized per repository via flock so parallel worktrees queue cleanly):
  1. Refuses default/detached branches, restricted branches (develop, feat/C-*), dirty or
     untracked non-ignored files, ambiguous non-origin remotes, and SHIP_DENY repositories.
  2. Scans every commit in `<base>..HEAD` (`git log -p`) for secrets with redacted output;
     any match aborts before pushing or merging (catching secrets added then scrubbed).
  3. Inspects `git diff --name-status -M` (both old and new paths on renames) for high-risk
     signing, billing, schema migration, and CI workflow files.
  4. In `--dry-run`, previews commit count, checks, and merge/hold plan without rebasing or
     mutating `HEAD`.
  5. Rebases onto latest `origin/<default>` (or local `<default>` for local-only Git repos),
     runs local check (`--check` or `~/.orca/ship.json`), and merges/fast-forwards cleanly.

Exit codes: 0 merged, 2 refused, 3 rebase conflict, 4 local check failed, 5 secret found,
6 CI failed or timed out, 7 merge failed, 10 paused for user decision (PR/branch ready).
"""
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
LOCK_DIR = os.path.join(HOME, "AI-Workspace/AI/Data/ship-locks")
CONFIG = os.path.join(HOME, ".orca/ship.json")
ORCA = "/Applications/Orca.app/Contents/Resources/bin/orca"
RESTRICTED = ("develop", "feat/C-")
DENY = tuple(x for x in os.environ.get("SHIP_DENY", "perp-pilot").split(",") if x)
CI_TIMEOUT = int(os.environ.get("SHIP_CI_TIMEOUT", "2400"))
CI_RETRY_DELAY = float(os.environ.get("SHIP_CI_RETRY_DELAY", "10"))
LOCK_TIMEOUT = int(os.environ.get("SHIP_LOCK_TIMEOUT", "900"))

SECRET_PATTERNS = [
    ("private key", r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP |ENCRYPTED )?PRIVATE KEY-----"),
    ("AWS access key", r"\bAKIA[0-9A-Z]{16}\b"),
    ("AWS secret key", r"(?i)\baws_secret_access_key\b\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{30,}['\"]?"),
    ("GitHub token", r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    ("GitHub token", r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"),
    ("OpenAI/Anthropic key", r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b"),
    ("Stripe live key", r"\b[rs]k_live_[A-Za-z0-9]{16,}\b"),
    ("Slack token", r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ("Google API key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("Bearer JWT/token", r"(?i)\bAuthorization\s*:\s*Bearer\s+[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}"),
    (
        "assigned secret",
        r"(?i)(?:['\"]?(?:[A-Za-z0-9_]*?(?:(?:api|openai|anthropic|claude|gemini|google|stripe|github|slack|aws|private|signing|master|encryption)[_-]?key|key[_-]?(?:backup|secret)|secret(?:[_-]?key)?|client[_-]?secret|access[_-]?token|auth[_-]?token|\btoken\b|passw(?:or)?d)[A-Za-z0-9_]*?)['\"]?)"
        r"\s*[:=]\s*"
        r"(?:['\"](?!<|\$\{|\{\{|None\b|null\b|true\b|false\b|changeme|example|your[_-])[^'\"\s]{12,}['\"]"
        r"|(?![\s'\"<${\[{#]|None\b|null\b|true\b|false\b|os\.|env\.|process\.env|sys\.|self\.|cls\.)[A-Za-z0-9/+._~=-]{14,})",
    ),
]

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


class Stop(Exception):
    def __init__(self, code, msg):
        Exception.__init__(self, msg)
        self.code = code


def say(msg):
    print("agent-ship: " + msg, flush=True)


def run(cmd, cwd=None, check=True, timeout=None, env=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    if check and p.returncode != 0:
        raise Stop(7, "%s failed: %s" % (" ".join(cmd[:4]), (p.stderr or p.stdout).strip()[:400]))
    return p


def git(cwd, *args, **kw):
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    kw.setdefault("env", env)
    return run(["git", "-c", "core.fsmonitor=false"] + list(args), cwd=cwd, **kw)


def _redact_secret_line(raw_line):
    """Return a non-sensitive identifier preview without echoing raw secret bytes."""
    s = raw_line.strip()
    m = re.match(r'''^([A-Za-z0-9_."'\s-]{1,40}?[:=])''', s)
    if m:
        return m.group(1).strip() + " <REDACTED>"
    return "<REDACTED>"


def scan_secrets(diff_text):
    """Findings in the added lines of a unified diff / git log -p: [(kind, redacted_context)]."""
    hits = []
    cur_file = ""
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            cur_file = line[6:].strip()
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        body = line[1:]
        for kind, pat in SECRET_PATTERNS:
            if re.search(pat, body):
                preview = _redact_secret_line(body)
                loc = ("%s: %s" % (cur_file, preview)) if cur_file else preview
                hits.append((kind, loc))
                break
    return hits


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
        return "on %s: commit the task on its own branch first (git switch -c codex/<task>)" % branch
    if branch == RESTRICTED[0] or branch.startswith(RESTRICTED[1]):
        return "%s is a restricted branch (plaintext keys in its history); it is never pushed" % branch
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
    return json.loads(p.stdout) if p.stdout.strip() else None


_EXPLICIT_PR_OR_PUSH_RE = re.compile(r"\b(?:push|pull_request|pull_request_target)\b")


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
        if not clean or _EXPLICIT_PR_OR_PUSH_RE.search(clean):
            return True
    return False


def local_check(args, key):
    if args.check:
        return args.check
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            return json.load(fh).get(key)
    except (OSError, ValueError):
        return None


def orca_status(top, pr=None, status="completed", unread=False):
    if not os.path.exists(ORCA):
        return
    cmd = [ORCA, "worktree", "set", "--worktree", "path:" + top, "--workspace-status", status]
    if pr is not None:
        cmd += ["--pr", str(pr)]
    if unread:
        cmd.append("--unread")
    cmd.append("--json")
    subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def resolve_main_clone(top):
    common = git(top, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    return os.path.dirname(common) if os.path.basename(common) == ".git" else None


def ship_local_only(args, top, branch):
    main_clone = resolve_main_clone(top) or top
    repo_name = os.path.basename(main_clone.rstrip(os.sep))
    if any(d and d in (repo_name,) + tuple(top.split(os.sep)) for d in DENY):
        raise Stop(2, "local/%s is excluded from automatic shipping (SHIP_DENY)" % repo_name)

    default = "main" if git(top, "show-ref", "--verify", "--quiet", "refs/heads/main", check=False).returncode == 0 else "master"
    why = refuse_branch(branch, default)
    if why:
        raise Stop(2, why)
    ensure_clean_worktree(top)

    os.makedirs(LOCK_DIR, exist_ok=True)
    lock_path = os.path.join(LOCK_DIR, "local__" + re.sub(r"[^A-Za-z0-9_.-]+", "_", repo_name) + ".lock")
    with open(lock_path, "w", encoding="utf-8") as lock:
        return _ship_local_only_locked(args, top, branch, main_clone, repo_name, default, lock)


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


def _ship_local_only_locked(args, top, branch, main_clone, repo_name, default, lock):
    say("waiting for the local/%s queue" % repo_name)
    acquire_lock(lock, "local/" + repo_name)
    say("local/%s: %s -> %s" % (repo_name, branch, default))

    if git(top, "rev-list", "--count", default + "..HEAD").stdout.strip() in ("", "0"):
        raise Stop(2, "nothing to ship: %s has no commits that %s lacks" % (branch, default))

    # Scan every commit patch in range so secrets added and scrubbed in a later commit are still caught
    history_patches = git(top, "log", "-p", "--no-color", default + "..HEAD").stdout
    hits = scan_secrets(history_patches)
    if hits:
        raise Stop(5, "possible secrets in commit history: " + "; ".join("%s (%s)" % h for h in hits[:5]))
    say("secret scan: clean")

    changed = collect_changed_paths(top, default + "...HEAD")
    risk_hits = scan_high_risk_paths(changed)
    hold = args.no_merge or (bool(risk_hits) and not args.force_merge)

    if args.dry_run:
        say("dry run: would rebase onto %s and %s local %s from %s" % (
            default, "pause review for" if hold else "fast-forward", default, branch))
        return 0

    if git(top, "rebase", default, check=False).returncode != 0:
        git(top, "rebase", "--abort", check=False)
        raise Stop(3, "rebase onto %s hit a conflict; it was aborted. Resolve and run again" % default)

    check = local_check(args, "local/" + repo_name)
    if check:
        say("local check: " + check)
        p = subprocess.run(check, shell=True, cwd=top)
        if p.returncode != 0:
            raise Stop(4, "local check failed (exit %d); main was not updated" % p.returncode)
    ensure_clean_worktree(top)

    if hold:
        reason = "--no-merge" if args.no_merge else ("high-risk files: " + ", ".join("%s (%s)" % h for h in risk_hits[:4]))
        orca_status(top, status="in-review", unread=True)
        say("paused before merging into local %s (%s); waiting for user decision" % (default, reason))
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
    say("fast-forwarded local %s (%s). Next task here: git switch -c codex/<task> %s" % (default, head_sha[:8], default))
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
    if any(d and (d in top.split(os.sep) or slug.endswith("/" + d)) for d in DENY):
        raise Stop(2, "%s is excluded from automatic shipping (SHIP_DENY)" % slug)
    repo = gh_json(["api", "repos/" + slug])
    default = repo.get("default_branch") or "main"
    why = refuse_branch(branch, default)
    if why:
        raise Stop(2, why)
    ensure_clean_worktree(top)

    os.makedirs(LOCK_DIR, exist_ok=True)
    lock_path = os.path.join(LOCK_DIR, slug.replace("/", "__") + ".lock")
    with open(lock_path, "w", encoding="utf-8") as lock:
        return _ship_github_locked(args, top, branch, slug, repo, default, lock)


def _ship_github_locked(args, top, branch, slug, repo, default, lock):
    say("waiting for the %s queue" % slug)
    acquire_lock(lock, slug)
    say("%s: %s -> %s" % (slug, branch, default))

    git(top, "fetch", "-q", "origin", default)
    base = "origin/" + default
    if git(top, "rev-list", "--count", base + "..HEAD").stdout.strip() in ("", "0"):
        raise Stop(2, "nothing to ship: %s has no commits that %s lacks" % (branch, base))

    history_patches = git(top, "log", "-p", "--no-color", base + "..HEAD").stdout
    hits = scan_secrets(history_patches)
    if hits:
        raise Stop(5, "possible secrets in commit history, nothing was pushed: " + "; ".join("%s (%s)" % h for h in hits[:5]))
    say("secret scan: clean")

    changed = collect_changed_paths(top, base + "...HEAD")
    risk_hits = scan_high_risk_paths(changed)
    hold = args.no_merge or (bool(risk_hits) and not args.force_merge)
    ci = has_ci(top)
    subjects = git(top, "log", "--format=%s", base + "..HEAD").stdout.strip().splitlines()

    if args.dry_run:
        say("dry run: would push %d commit(s), open a PR (%s), %s, then %s" % (
            len(subjects), subjects[0][:60] if subjects else branch,
            "wait for CI" if ci else "no CI",
            "pause for user review" if hold else "merge"))
        return 0

    if git(top, "rebase", base, check=False).returncode != 0:
        git(top, "rebase", "--abort", check=False)
        raise Stop(3, "rebase onto %s hit a conflict; it was aborted. Merge %s into the branch, resolve, commit, run again" % (base, base))
    check = local_check(args, slug)
    if check:
        say("local check: " + check)
        p = subprocess.run(check, shell=True, cwd=top)
        if p.returncode != 0:
            raise Stop(4, "local check failed (exit %d); nothing was pushed" % p.returncode)
    ensure_clean_worktree(top)
    subjects = git(top, "log", "--format=%s", base + "..HEAD").stdout.strip().splitlines()

    git(top, "push", "-q", "--force-with-lease", "-u", "origin", "HEAD:refs/heads/" + branch)
    head_sha = git(top, "rev-parse", "HEAD").stdout.strip()
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
        footer = "Paused for user decision (`%s`)." % (
            "--no-merge" if args.no_merge else "high-risk paths: " + ", ".join(p for _, p in risk_hits[:4])
        ) if hold else "Opened and merged by agent-ship (no human review requested)."
        body += "\n\nChecks: %s%s\n\n%s" % (
            ("local `%s`" % check) if check else "none configured locally", "; GitHub CI" if ci else "", footer)
        out = run(["gh", "pr", "create", "-R", slug, "--base", default, "--head", branch, "--title", title, "--body", body]).stdout
        pr_url = out.strip().splitlines()[-1]
        pr = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
        say("opened %s" % pr_url)
    if hold:
        orca_status(top, pr=pr, status="in-review", unread=True)
        reason = "--no-merge" if args.no_merge else ("high-risk files: " + ", ".join("%s (%s)" % h for h in risk_hits[:4]))
        say("paused (%s): PR %s awaits user decision" % (reason, pr_url))
        return 10

    if ci:
        say("waiting for GitHub checks (up to %d min)" % (CI_TIMEOUT // 60))
        p = None
        for attempt in range(3):
            time.sleep(CI_RETRY_DELAY)
            try:
                p = run(["gh", "pr", "checks", str(pr), "-R", slug, "--watch", "--fail-fast", "--interval", "20"],
                        check=False, timeout=CI_TIMEOUT)
            except subprocess.TimeoutExpired:
                orca_status(top, pr=pr, status="in-review", unread=True)
                raise Stop(6, "CI did not finish in time; PR %s stays open" % pr_url)
            text = ((p.stdout or "") + (p.stderr or "")).lower()
            if "no checks reported" not in text:
                break
        if p is None:
            orca_status(top, pr=pr, status="in-review", unread=True)
            raise Stop(6, "CI checks could not be queried on %s; PR stays open for review" % pr_url)
        text = ((p.stdout or "") + (p.stderr or "")).lower()
        if "no checks reported" in text:
            orca_status(top, pr=pr, status="in-review", unread=True)
            raise Stop(
                6,
                "expected GitHub CI checks on %s (head %s), but no checks were reported after 3 attempts; PR stays open"
                % (pr_url, head_sha[:8]),
            )
        if p.returncode != 0:
            orca_status(top, pr=pr, status="in-review", unread=True)
            raise Stop(6, "CI failed on %s; fix on the branch and run again" % pr_url)
    allowed = {"squash": repo.get("allow_squash_merge"), "merge": repo.get("allow_merge_commit"),
               "rebase": repo.get("allow_rebase_merge")}
    method = choose_method(allowed, args.method)
    run(["gh", "pr", "merge", str(pr), "-R", slug, "--" + method, "--match-head-commit", head_sha])
    say("merged %s (%s @ %s)" % (pr_url, method, head_sha[:8]))
    run(["gh", "api", "-X", "DELETE", "repos/%s/git/refs/heads/%s" % (slug, branch)], check=False)

    git(top, "fetch", "-q", "origin", default)
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
        if main_clone and top != main_clone:
            git(top, "branch", "-D", branch, check=False)
    orca_status(top, pr=pr, status="completed")
    say("done. Next task here: git switch -c codex/<task> %s" % base)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--title")
    ap.add_argument("--body")
    ap.add_argument("--check", help="local check command, run in the worktree before pushing")
    ap.add_argument("--method", choices=("squash", "merge", "rebase"))
    ap.add_argument("--no-merge", action="store_true", help="open the PR and stop (a decision for the user)")
    ap.add_argument("--force-merge", action="store_true", help="merge even when high-risk files were modified (after explicit user approval)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        return ship(args)
    except Stop as e:
        say("STOP: " + str(e))
        return e.code


if __name__ == "__main__":
    sys.exit(main())
