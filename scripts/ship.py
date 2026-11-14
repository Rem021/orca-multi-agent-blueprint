#!/usr/bin/env python3
"""agent-ship: take a finished task branch all the way to main without asking the user.

Run inside the worktree of a committed task branch:

    agent-ship [--title T] [--body B] [--check CMD] [--method squash|merge|rebase]
               [--no-merge] [--force-merge] [--dry-run]

What it does (serialized per repository via flock so parallel worktrees queue cleanly):
  1. Refuses default/detached branches, restricted branches (develop, feat/C-*), dirty
     tracked files, and SHIP_DENY repositories (default: perp-pilot).
  2. Scans added diff lines for secrets; any match aborts before pushing.
  3. Rebases onto latest origin/<default> (or local <default> for local-only Git repos);
     conflicts abort cleanly with exit code 3.
  4. Runs the configured local check (--check or ~/.orca/ship.json).
  5. For GitHub repos: pushes branch (--force-with-lease), opens/reuses PR, pauses in
     Orca 'in-review' (--unread) if --no-merge or high-risk product/signing/migration
     files were modified (unless --force-merge), waits for GitHub CI if workflows exist,
     merges pinned to --match-head-commit <SHA>, deletes remote & local task branch,
     fast-forwards clean main clone, detaches worktree at origin/<default>, and marks
     the Orca workspace completed.
  6. For local-only Git repos (no 'origin' remote, e.g. Bridge / 家庭系统): rebases onto
     local main, runs local check ("local/<repo>" in ~/.orca/ship.json), fast-forwards
     clean main, detaches worktree at main, deletes the merged task branch, and marks
     the Orca workspace completed.

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

SECRET_PATTERNS = [
    ("private key", r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP |ENCRYPTED )?PRIVATE KEY-----"),
    ("AWS access key", r"\bAKIA[0-9A-Z]{16}\b"),
    ("GitHub token", r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    ("GitHub token", r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"),
    ("OpenAI/Anthropic key", r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{24,}\b"),
    ("Slack token", r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ("Google API key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("assigned secret", r"(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d)\b\s*[:=]\s*['\"][^'\"\s]{12,}['\"]"),
]

HIGH_RISK_PATTERNS = [
    ("entitlements/signing", r"(?:^|/)(?:[^/]+\.entitlements|ExportOptions[^/]*\.plist|AppStoreConnect[^/]*|Fastfile|Deliverfile)$"),
    ("billing/StoreKit", r"(?:^|/)(?:[^/]+\.storekit|[^/]*StoreKit[^/]*\.swift)$"),
    ("schema migration", r"(?:^|/)(?:migrations|drizzle)/[^/]+\.(?:sql|py|ts)$"),
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


def scan_secrets(diff_text):
    """Findings in the added lines of a unified diff: [(kind, line excerpt)]."""
    hits = []
    for line in diff_text.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for kind, pat in SECRET_PATTERNS:
            if re.search(pat, line):
                hits.append((kind, line[1:].strip()[:24] + "..."))
                break
    return hits


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


def github_slug(url):
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url.strip())
    return m.group(1) if m else None


def gh_json(args, cwd=None):
    p = run(["gh"] + args, cwd=cwd)
    return json.loads(p.stdout) if p.stdout.strip() else None


def has_ci(top):
    d = os.path.join(top, ".github/workflows")
    return os.path.isdir(d) and any(f.endswith((".yml", ".yaml")) for f in os.listdir(d))


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
    if git(top, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise Stop(2, "uncommitted changes to tracked files; commit them (or stash) first")

    os.makedirs(LOCK_DIR, exist_ok=True)
    lock_path = os.path.join(LOCK_DIR, "local__" + re.sub(r"[^A-Za-z0-9_.-]+", "_", repo_name) + ".lock")
    with open(lock_path, "w", encoding="utf-8") as lock:
        return _ship_local_only_locked(args, top, branch, main_clone, repo_name, default, lock)


def _ship_local_only_locked(args, top, branch, main_clone, repo_name, default, lock):
    say("waiting for the local/%s queue" % repo_name)
    fcntl.flock(lock, fcntl.LOCK_EX)
    say("local/%s: %s -> %s" % (repo_name, branch, default))

    if git(top, "rev-list", "--count", default + "..HEAD").stdout.strip() in ("", "0"):
        raise Stop(2, "nothing to ship: %s has no commits that %s lacks" % (branch, default))
    diff = git(top, "diff", default + "...HEAD").stdout
    hits = scan_secrets(diff)
    if hits:
        raise Stop(5, "possible secrets in the branch: " + "; ".join("%s (%s)" % h for h in hits[:5]))
    say("secret scan: clean")

    changed = git(top, "diff", "--name-only", default + "...HEAD").stdout.splitlines()
    risk_hits = scan_high_risk_paths(changed)
    hold = args.no_merge or (bool(risk_hits) and not args.force_merge)

    if git(top, "rebase", default, check=False).returncode != 0:
        git(top, "rebase", "--abort", check=False)
        raise Stop(3, "rebase onto %s hit a conflict; it was aborted. Resolve and run again" % default)

    check = local_check(args, "local/" + repo_name)
    if check:
        say("local check: " + check)
        p = subprocess.run(check, shell=True, cwd=top)
        if p.returncode != 0:
            raise Stop(4, "local check failed (exit %d); main was not updated" % p.returncode)

    if args.dry_run:
        say("dry run: would fast-forward local %s from %s" % (default, branch))
        return 0
    if hold:
        reason = "--no-merge" if args.no_merge else ("high-risk files: " + ", ".join("%s (%s)" % h for h in risk_hits[:4]))
        orca_status(top, status="in-review", unread=True)
        say("paused before merging into local %s (%s); waiting for user decision" % (default, reason))
        return 10

    head_sha = git(top, "rev-parse", "HEAD").stdout.strip()
    if git(main_clone, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip() == default:
        if git(main_clone, "status", "--porcelain", "--untracked-files=no").stdout.strip():
            raise Stop(7, "main clone (%s) has uncommitted tracked changes; cannot fast-forward %s safely" % (main_clone, default))
        git(main_clone, "merge", "-q", "--ff-only", head_sha)
    else:
        git(top, "fetch", "-q", ".", "%s:refs/heads/%s" % (head_sha, default))

    git(top, "switch", "-q", "--detach", default, check=False)
    if top != main_clone:
        git(top, "branch", "-D", branch, check=False)
    orca_status(top, status="completed")
    say("fast-forwarded local %s (%s). Next task here: git switch -c codex/<task> %s" % (default, head_sha[:8], default))
    return 0


def ship(args):
    top = git(os.getcwd(), "rev-parse", "--show-toplevel").stdout.strip()
    branch = git(top, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    url = git(top, "remote", "get-url", "origin", check=False).stdout.strip()
    if not url:
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
    if git(top, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise Stop(2, "uncommitted changes to tracked files; commit them (or stash) first")

    os.makedirs(LOCK_DIR, exist_ok=True)
    lock_path = os.path.join(LOCK_DIR, slug.replace("/", "__") + ".lock")
    with open(lock_path, "w", encoding="utf-8") as lock:
        return _ship_github_locked(args, top, branch, slug, repo, default, lock)


def _ship_github_locked(args, top, branch, slug, repo, default, lock):
    say("waiting for the %s queue" % slug)
    fcntl.flock(lock, fcntl.LOCK_EX)          # held until exit: one ship per repository at a time
    say("%s: %s -> %s" % (slug, branch, default))

    git(top, "fetch", "-q", "origin", default)
    base = "origin/" + default
    if git(top, "rev-list", "--count", base + "..HEAD").stdout.strip() in ("", "0"):
        raise Stop(2, "nothing to ship: %s has no commits that %s lacks" % (branch, base))
    diff = git(top, "diff", base + "...HEAD").stdout
    hits = scan_secrets(diff)
    if hits:
        raise Stop(5, "possible secrets in the branch, nothing was pushed: " + "; ".join("%s (%s)" % h for h in hits[:5]))
    say("secret scan: clean")

    changed = git(top, "diff", "--name-only", base + "...HEAD").stdout.splitlines()
    risk_hits = scan_high_risk_paths(changed)
    hold = args.no_merge or (bool(risk_hits) and not args.force_merge)

    if git(top, "rebase", base, check=False).returncode != 0:
        git(top, "rebase", "--abort", check=False)
        raise Stop(3, "rebase onto %s hit a conflict; it was aborted. Merge %s into the branch, resolve, commit, run again" % (base, base))
    check = local_check(args, slug)
    if check:
        say("local check: " + check)
        p = subprocess.run(check, shell=True, cwd=top)
        if p.returncode != 0:
            raise Stop(4, "local check failed (exit %d); nothing was pushed" % p.returncode)
    ci = has_ci(top)
    subjects = git(top, "log", "--format=%s", base + "..HEAD").stdout.strip().splitlines()
    if args.dry_run:
        say("dry run: would push %d commit(s), open a PR (%s), %s, then %s" % (
            len(subjects), subjects[0][:60] if subjects else branch,
            "wait for CI" if ci else "no CI",
            "pause for user review" if hold else "merge"))
        return 0

    git(top, "push", "-q", "--force-with-lease", "-u", "origin", "HEAD:refs/heads/" + branch)
    head_sha = git(top, "rev-parse", "HEAD").stdout.strip()
    prs = gh_json(["pr", "list", "-R", slug, "--head", branch, "--state", "open", "--json", "number,url"]) or []
    if prs:
        pr, pr_url = prs[0]["number"], prs[0]["url"]
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
        time.sleep(10)                         # let the checks register
        try:
            p = run(["gh", "pr", "checks", str(pr), "-R", slug, "--watch", "--fail-fast", "--interval", "20"],
                    check=False, timeout=CI_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise Stop(6, "CI did not finish in time; PR %s stays open" % pr_url)
        text = (p.stdout + p.stderr).lower()
        if p.returncode != 0 and "no checks reported" not in text:
            raise Stop(6, "CI failed on %s; fix on the branch and run again" % pr_url)
    allowed = {"squash": repo.get("allow_squash_merge"), "merge": repo.get("allow_merge_commit"),
               "rebase": repo.get("allow_rebase_merge")}
    method = choose_method(allowed, args.method)
    run(["gh", "pr", "merge", str(pr), "-R", slug, "--" + method, "--match-head-commit", head_sha])
    say("merged %s (%s @ %s)" % (pr_url, method, head_sha[:8]))
    run(["gh", "api", "-X", "DELETE", "repos/%s/git/refs/heads/%s" % (slug, branch)], check=False)

    git(top, "fetch", "-q", "origin", default)
    main_clone = resolve_main_clone(top)
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
