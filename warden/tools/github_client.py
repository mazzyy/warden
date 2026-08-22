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
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

log = logging.getLogger("warden.github")


@dataclass
class DryRunPR:
    title: str
    body: str
    changes: dict[str, str]
    branch: str


class GitHubClient:
    def __init__(
        self,
        *,
        repo_full_name: str,
        token: str | None = None,
        base_branch: str = "main",
    ) -> None:
        self._repo_name = repo_full_name
        self._token = token
        self._base = base_branch
        self._repo = None
        self.dry_run = token is None
        self.dry_run_prs: list[DryRunPR] = []

    # -- lazy real client --------------------------------------------------

    def _repo_handle(self):
        if self._repo is None:
            from github import Auth, Github

            gh = Github(auth=Auth.Token(self._token))
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

    async def open_pull_request(
        self, *, title: str, body: str, changes: dict[str, str]
    ) -> dict[str, Any]:
        branch = self._branch_name(title)
        signed_body = (
            f"{body}\n\n---\n"
            "Opened by **Warden** — an autonomous remediation agent.\n\n"
            "This agent holds no cluster credentials and cannot merge this pull "
            "request. A human review is required before anything reaches the estate."
        )

        if self.dry_run:
            self.dry_run_prs.append(DryRunPR(title=title, body=signed_body, changes=changes, branch=branch))
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
            for path, content in changes.items():
                existing = repo.get_contents(path, ref=branch)
                repo.update_file(
                    path=path,
                    message=f"fix: {title}",
                    content=content,
                    sha=existing.sha,
                    branch=branch,
                )
            pr = repo.create_pull(title=title, body=signed_body, head=branch, base=self._base)
            return {
                "dry_run": False,
                "pr_url": pr.html_url,
                "pr_number": pr.number,
                "branch": branch,
                "files_changed": list(changes),
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
