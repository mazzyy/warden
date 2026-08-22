"""The only write path in the system.

Two modes. With a token it opens real pull requests against estate-gitops. In
dry-run — no token configured — it records what it *would* have done and returns
a plausible result, so the whole incident loop can be exercised offline.

Dry-run is a development affordance and must never appear in the submitted demo:
the hackathon requires unedited live execution. `dry_run` is reported in every
result precisely so it is impossible to show a fake PR on camera by accident.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

log = logging.getLogger("warden.github")


@dataclass
class DryRunPR:
    title: str
    body: str
    changes: dict[str, str]
    branch: str


class GitHubClient:
    # What an installation token is minted with. Narrower than the App's own
    # grant on purpose: the App may hold more (a future agent might need
    # issues, say), but *this* client only ever needs to read files and open
    # pull requests, so that is all its token carries. Least privilege at the
    # moment of use, not merely at the moment of installation.
    #
    # Note what is absent: `administration`. Without it the token cannot read
    # or edit branch protection — it cannot even see the rule that governs it,
    # let alone relax it.
    TOKEN_PERMISSIONS: ClassVar[dict[str, str]] = {
        "contents": "write",
        "pull_requests": "write",
        "metadata": "read",
    }

    def __init__(
        self,
        *,
        repo_full_name: str,
        token: str | None = None,
        base_branch: str = "main",
        credential: Any | None = None,
    ) -> None:
        self._repo_name = repo_full_name
        self._credential = credential
        self._token = token if credential is None else getattr(credential, "token", None)
        self._base = base_branch
        self._repo = None
        kind = getattr(credential, "kind", None)
        self.dry_run = (kind == "none") if credential is not None else token is None
        self.dry_run_prs: list[DryRunPR] = []

    @property
    def identity(self) -> str:
        """How the pull request will be signed. Shown in the demo header."""
        if self.dry_run:
            return "dry run — no GitHub credential"
        return getattr(self._credential, "label", None) or "personal access token"

    @property
    def review_boundary_enforced(self) -> bool:
        """True only when the platform, not our code, prevents a self-merge."""
        return bool(getattr(self._credential, "enforced", False))

    # -- lazy real client --------------------------------------------------

    def _repo_handle(self):
        if self._repo is None:
            from github import Auth, Github

            cred = self._credential
            if getattr(cred, "kind", None) == "app":
                auth = Auth.AppAuth(cred.app_id, cred.private_key).get_installation_auth(
                    int(cred.installation_id), token_permissions=self.TOKEN_PERMISSIONS
                )
            else:
                auth = Auth.Token(self._token)
            gh = Github(auth=auth)
            self._repo = gh.get_repo(self._repo_name)
        return self._repo

    @staticmethod
    def _branch_name(title: str) -> str:
        slug = "".join(c if c.isalnum() else "-" for c in title.lower())[:40].strip("-")
        stamp = datetime.now().strftime("%m%d-%H%M%S")
        return f"warden/{slug}-{stamp}"

    # -- operations --------------------------------------------------------

    @staticmethod
    def _failure(action: str, exc: Exception) -> dict[str, Any]:
        """Turn a GitHub failure into a result the agent can reason about.

        Raising here would abort the whole incident: an expired token, a
        revoked grant or a rate limit would take down a live demo mid-run with
        a stack trace. Returning an error dict lets the agent say "I could not
        read that file" and carry on, which is both more honest and survivable
        on camera.
        """
        log.warning("github %s failed: %s", action, exc)
        return {
            "error": f"github_{action}_failed",
            "detail": str(exc)[:300],
            "hint": "check the PAT scope on estate-gitops and that it has not expired",
        }

    async def list_files(self, prefix: str = "") -> dict[str, Any]:
        if self.dry_run:
            return {
                "paths": ["apps/checkout-svc/deployment.yaml", "apps/checkout-svc/service.yaml"],
                "dry_run": True,
            }

        def _list():
            repo = self._repo_handle()
            tree = repo.get_git_tree(self._base, recursive=True)
            paths = [e.path for e in tree.tree if e.type == "blob"]
            if prefix:
                paths = [p for p in paths if p.startswith(prefix)]
            return {"paths": sorted(paths)[:200], "count": len(paths)}

        try:
            return await asyncio.to_thread(_list)
        except Exception as exc:
            return self._failure("list", exc)

    async def read_file(self, path: str) -> dict[str, Any]:
        if self.dry_run:
            return {"path": path, "content": "", "dry_run": True, "note": "no token configured"}

        def _read():
            f = self._repo_handle().get_contents(path, ref=self._base)
            return {"path": path, "content": f.decoded_content.decode(), "sha": f.sha}

        try:
            return await asyncio.to_thread(_read)
        except Exception as exc:
            return self._failure("read", exc)

    def _signature(self) -> str:
        """The footer on every Warden pull request.

        It states what a reviewer can rely on, and it must not overstate it.
        The old text said flatly "cannot merge this pull request" on every
        path. On the PAT path that was untrue — a PAT owned by a repository
        admin bypasses branch protection — and a governance claim printed on
        the artefact itself is exactly the wrong place to be optimistic.
        """
        common = "Opened by **Warden** — an autonomous remediation agent.\n\nIt holds no cluster credentials; its only write primitive is this pull request.\n\n"
        if self.review_boundary_enforced:
            return common + (
                "It is authenticated as a GitHub App with no repository role, so branch "
                "protection applies to it with no admin bypass, and an App cannot approve "
                "a pull request. **It cannot merge this — the platform will refuse.**"
            )
        return common + (
            "⚠️ It is authenticated with a personal access token, which inherits the "
            "repository role of the human who minted it. If that human is an admin, "
            "nothing but Warden's own source code prevents this agent from merging its "
            "own work. Treat that as a promise, not a control."
        )

    async def open_pull_request(
        self, *, title: str, body: str, changes: dict[str, str], max_changed_lines: int = 0
    ) -> dict[str, Any]:
        branch = self._branch_name(title)
        signed_body = f"{body}\n\n---\n{self._signature()}"

        if self.dry_run:
            self.dry_run_prs.append(
                DryRunPR(title=title, body=signed_body, changes=changes, branch=branch)
            )
            log.info("DRY RUN: would open PR %r on %s touching %s", title, branch, list(changes))
            return {
                "dry_run": True,
                "pr_url": f"https://github.com/{self._repo_name}/pull/DRY-RUN",
                "branch": branch,
                "files_changed": list(changes),
                "title": title,
            }

        def _open():
            repo = self._repo_handle()
            base_sha = repo.get_branch(self._base).commit.sha
            repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base_sha)

            written: list[str] = []
            unchanged: list[str] = []
            for path, content in changes.items():
                existing = repo.get_contents(path, ref=branch)
                # Compare before writing. Passing identical content to
                # update_file produces a commit with an EMPTY diff, and then a
                # pull request that claims to fix something and changes
                # nothing. That is worse than failing: it looks like success.
                current = existing.decoded_content.decode()
                if current == content:
                    unchanged.append(path)
                    continue

                if max_changed_lines:
                    import difflib

                    delta = sum(
                        1
                        for line in difflib.unified_diff(
                            current.splitlines(), content.splitlines(), n=0
                        )
                        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
                    )
                    if delta > max_changed_lines:
                        with contextlib.suppress(Exception):
                            repo.get_git_ref(f"heads/{branch}").delete()
                        log.warning(
                            "patch to %s changes %d lines, over the %d-line blast radius",
                            path,
                            delta,
                            max_changed_lines,
                        )
                        return {
                            "error": "patch_exceeds_blast_radius",
                            "detail": (
                                f"The proposed change to {path} touches {delta} lines, but this "
                                f"agent's blastRadius allows {max_changed_lines}. Remediate the "
                                "specific failure described in the incident and change nothing "
                                "else. If the file genuinely needs a larger rewrite, that is a "
                                "human's call, not yours."
                            ),
                            "changed_lines": delta,
                            "limit": max_changed_lines,
                        }
                repo.update_file(
                    path=path,
                    message=f"fix: {title}",
                    content=content,
                    sha=existing.sha,
                    branch=branch,
                )
                written.append(path)

            if not written:
                # Nothing to propose. Clean up the branch rather than leaving
                # an orphan, and tell the agent plainly.
                with contextlib.suppress(Exception):
                    repo.get_git_ref(f"heads/{branch}").delete()
                log.warning("no-op patch: %s already match the proposed content", unchanged)
                return {
                    "error": "no_changes_needed",
                    "detail": (
                        f"{', '.join(unchanged)} already contains exactly the proposed "
                        "content, so there is nothing to change. The file in the "
                        "repository may already be correct, or the estate you diagnosed "
                        "may be out of sync with the repository."
                    ),
                    "files_unchanged": unchanged,
                }

            pr = repo.create_pull(title=title, body=signed_body, head=branch, base=self._base)
            return {
                "dry_run": False,
                "pr_url": pr.html_url,
                "pr_number": pr.number,
                "branch": branch,
                "files_changed": written,
                "files_unchanged": unchanged,
            }

        try:
            return await asyncio.to_thread(_open)
        except Exception as exc:
            return self._failure("open_pull_request", exc)

    async def open_revert(self, *, pr_number: int, reason: str) -> dict[str, Any]:
        if self.dry_run:
            log.info("DRY RUN: would revert PR #%s — %s", pr_number, reason)
            return {"dry_run": True, "reverts": pr_number, "reason": reason}

        def _revert():
            repo = self._repo_handle()
            original = repo.get_pull(pr_number)
            branch = f"warden/revert-{pr_number}"
            base_sha = repo.get_branch(self._base).commit.sha
            repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base_sha)
            for f in original.get_files():
                if f.previous_filename or f.status == "modified":
                    before = repo.get_contents(f.filename, ref=f"{original.base.sha}")
                    current = repo.get_contents(f.filename, ref=branch)
                    repo.update_file(
                        path=f.filename,
                        message=f"revert: PR #{pr_number}",
                        content=before.decoded_content,
                        sha=current.sha,
                        branch=branch,
                    )
            pr = repo.create_pull(
                title=f"Revert #{pr_number}",
                body=f"{reason}\n\n---\nOpened by **Warden** after failed verification.",
                head=branch,
                base=self._base,
            )
            return {"dry_run": False, "pr_url": pr.html_url, "pr_number": pr.number}

        try:
            return await asyncio.to_thread(_revert)
        except Exception as exc:
            return self._failure("open_revert", exc)
