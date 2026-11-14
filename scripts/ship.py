#!/usr/bin/env python3
"""agent-ship: take a finished task branch all the way to main without asking the user.

Run it inside the worktree of a task branch, after committing:

    agent-ship [--title T] [--body B] [--check CMD] [--method squash|merge|rebase]
               [--no-merge] [--dry-run]

What it does, one repository at a time (a lock per repository makes parallel agents
line up the way a merge queue would, so every merge is checked against the newest main):

  1. refuses: the default branch and the restricted branches (develop, feat/C-*),
     uncommitted changes, repositories without a GitHub remote, and repositories named
     in SHIP_DENY (default: perp-pilot, whose history holds plaintext keys)
  2. scans the added lines of the branch for secrets; a hit stops everything
  3. fetches and rebases onto origin/<default>; a conflict aborts the rebase and stops
  4. runs the local check: --check, else the entry for this repository in
     ~/.orca/ship.json ({"owner/repo": "command"}), else none
  5. pushes the branch (with --force-with-lease, it may have been rebased) and opens a
     pull request, or reuses the open one
  6. waits for the GitHub checks when the repository has CI workflows
  7. merges with a method the repository allows (squash first), deletes the remote
     branch, fast-forwards the local main clone when it is clean, detaches the worktree
     at the new main so the next task starts fresh, and marks the Orca workspace done

GitHub's own auto-merge and merge queue need branch protection, which private repositories
on a free plan do not get; this script does the same job locally.

--no-merge stops after step 5: use it for a change that needs the user's decision.
Exit codes: 0 merged, 2 refused, 3 rebase conflict, 4 local check failed, 5 secret found,
6 CI failed or timed out, 7 merge failed, 10 pull request opened but not merged.
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


class Stop(Exception):
    def __init__(self, code, msg):
        Exception.__init__(self, msg)
        self.code = code


def say(msg):
    print("agent-ship: " + msg, flush=True)


def run(cmd, cwd=None, check=True, timeout=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise Stop(7, "%s failed: %s" % (" ".join(cmd[:4]), (p.stderr or p.stdout).strip()[:400]))
    return p


def git(cwd, *args, **kw):
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


def local_check(args, slug):
    if args.check:
        return args.check
    try:
        return json.load(open(CONFIG, encoding="utf-8")).get(slug)
    except (OSError, ValueError):
        return None


def orca_done(top, pr):
    if os.path.exists(ORCA):
        subprocess.run([ORCA, "worktree", "set", "--worktree", "path:" + top, "--pr", str(pr),
                        "--workspace-status", "completed", "--json"], capture_output=True, text=True, timeout=60)


def ship(args):
    top = git(os.getcwd(), "rev-parse", "--show-toplevel").stdout.strip()
    branch = git(top, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    url = git(top, "remote", "get-url", "origin", check=False).stdout
    slug = github_slug(url) if url else None
    if not slug:
        raise Stop(2, "no GitHub remote 'origin' in %s" % top)
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
    lock = open(os.path.join(LOCK_DIR, slug.replace("/", "__") + ".lock"), "w")
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
        say("dry run: would push %d commit(s), open a PR (%s), %s, then merge" % (
            len(subjects), subjects[0][:60] if subjects else branch, "wait for CI" if ci else "no CI"))
        return 0

    git(top, "push", "-q", "--force-with-lease", "-u", "origin", "HEAD:refs/heads/" + branch)
    prs = gh_json(["pr", "list", "-R", slug, "--head", branch, "--state", "open", "--json", "number,url"]) or []
    if prs:
        pr, pr_url = prs[0]["number"], prs[0]["url"]
        say("reusing PR #%d" % pr)
    else:
        title = args.title or (subjects[-1] if len(subjects) == 1 else branch.split("/", 1)[-1].replace("-", " "))
        body = args.body or "\n".join("- " + s for s in reversed(subjects))
        body += "\n\nChecks: %s%s\n\nOpened and merged by agent-ship (no human review requested)." % (
            ("local `%s`" % check) if check else "none configured locally", "; GitHub CI" if ci else "")
        out = run(["gh", "pr", "create", "-R", slug, "--base", default, "--head", branch, "--title", title, "--body", body]).stdout
        pr_url = out.strip().splitlines()[-1]
        pr = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
        say("opened %s" % pr_url)
    if args.no_merge:
        say("--no-merge: the PR waits for the user")
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
    run(["gh", "pr", "merge", str(pr), "-R", slug, "--" + method])
    say("merged %s (%s)" % (pr_url, method))
    run(["gh", "api", "-X", "DELETE", "repos/%s/git/refs/heads/%s" % (slug, branch)], check=False)

    git(top, "fetch", "-q", "origin", default)
    common = git(top, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    main_clone = os.path.dirname(common) if os.path.basename(common) == ".git" else None
    if main_clone and git(main_clone, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip() == default \
            and not git(main_clone, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        if git(main_clone, "merge", "-q", "--ff-only", base, check=False).returncode == 0:
            say("main clone fast-forwarded")
    git(top, "switch", "-q", "--detach", base, check=False)
    orca_done(top, pr)
    say("done. Next task here: git switch -c codex/<task> %s" % base)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--title")
    ap.add_argument("--body")
    ap.add_argument("--check", help="local check command, run in the worktree before pushing")
    ap.add_argument("--method", choices=("squash", "merge", "rebase"))
    ap.add_argument("--no-merge", action="store_true", help="open the PR and stop (a decision for the user)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        return ship(args)
    except Stop as e:
        say("STOP: " + str(e))
        return e.code


if __name__ == "__main__":
    sys.exit(main())
