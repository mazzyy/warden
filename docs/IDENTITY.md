# Who the agent writes as

Warden's central claim is that an agent's only write primitive is opening a
pull request. Everything else in the system — the policy plugin, the manifests,
the read-only ServiceAccount — exists to keep that true.

This document is about the one place where we got it wrong, how we found out,
and what actually fixes it.

## The claim we could not back

Every Warden pull request carried this footer:

> This agent holds no cluster credentials and cannot merge this pull request.
> A human review is required before anything reaches the estate.

The first sentence was true and provable: `estate-gitops/scripts/verify-rbac.sh`
attempts a delete with the agent's ServiceAccount token and gets

```
Error from server (Forbidden): deployments.apps "checkout-svc" is forbidden:
User "system:serviceaccount:demo:warden-reader" cannot delete resource ...
```

The second sentence had no such proof behind it. It rested on the credential
being a fine-grained personal access token scoped to one repository, with only
`contents: write` and `pull requests: write`.

## What we measured

`estate-gitops/scripts/verify-github-token.sh` attempts a direct commit to
`main` using the agent's own credential. With branch protection enabled and
one approving review required, it returned:

```
!! ALLOWED direct commit to main (HTTP 201)
```

The commit landed on `main`. No pull request, no review.

## Why

A personal access token inherits the repository role of the human who minted
it. The token's owner is a repository admin, and by default
(`enforce_admins: false`) branch protection does not apply to admins.

Scopes and repository pinning decide **which repositories** a token may reach
and **which APIs** it may call. They do not decide **who the token is**. A
narrow PAT owned by an admin is still an admin.

This is the same shape as the earlier RBAC mistake, where a `kubectl --token=`
check appeared to prove the agent could not delete a Deployment, while the
admin kubeconfig's client certificate was quietly doing the authenticating —
and the delete succeeded. In both cases a real-looking check passed while the
thing it claimed to test was never exercised.

The lesson is the same both times: **a control you have not seen refuse
something is not a control.**

## The fix: give the agent its own identity

A GitHub App is not a member of the repository and holds no role. Branch
protection applies to it with no bypass to inherit. An App also cannot approve
a pull request, so it cannot manufacture the approval that would let its own
work merge.

That turns the footer from a promise into a property.

### 1. Create the App

<https://github.com/settings/apps/new>

| Field | Value |
| --- | --- |
| Name | `warden-remediator` |
| Homepage URL | your repository URL |
| Webhook | **uncheck Active** |

Repository permissions — exactly these three:

| Permission | Access |
| --- | --- |
| Contents | Read and write |
| Pull requests | Read and write |
| Metadata | Read only (mandatory) |

Leave **Administration** at *No access*. With it, the agent could read and
relax the branch protection rule that governs it.

Under "Where can this GitHub App be installed?", choose **Only on this
account**.

### 2. Install it on `estate-gitops` only

From the App's page: *Install App* → your account → **Only select
repositories** → `estate-gitops`.

The installation ID is the last path segment of the URL you land on:
`https://github.com/settings/installations/<INSTALLATION_ID>`.

### 3. Generate a private key

On the App's page: *Private keys* → *Generate a private key*. A `.pem`
downloads. Move it somewhere outside both repositories:

```bash
mkdir -p ~/.warden
mv ~/Downloads/warden-remediator.*.private-key.pem ~/.warden/github-app.pem
chmod 600 ~/.warden/github-app.pem
```

### 4. Point Warden at it

In `warden/.env`:

```
GITHUB_APP_ID=<app id from the App's General page>
GITHUB_APP_INSTALLATION_ID=<from the URL in step 2>
GITHUB_APP_PRIVATE_KEY_PATH=~/.warden/github-app.pem
```

The key is referenced by **path**, never pasted into `.env`. A PEM in a dotenv
file gets shoulder-surfed on a screen share, pasted into a chat window, and
committed by accident.

Leave `GITHUB_TOKEN` in place. Warden prefers the App and falls back to the PAT
only if the App is misconfigured — and says so loudly when it does, because
silently running the bypassable path while believing you are on the enforced
one is the worst available outcome.

### 5. Leave `enforce_admins` false — on purpose

An earlier draft of this document told you to set `enforce_admins: true`. That
was wrong, and the reason is worth understanding rather than just correcting.

`enforce_admins` decides whether **repository admins** are exempt from branch
protection. Once the agent authenticates as an App, it holds no repository role
at all — so it was never going to be exempt, whatever this flag says. Turning
the flag on does not constrain the agent. It constrains *you*, and it breaks
two things:

- You could no longer merge the App's pull requests, because one approval is
  required and GitHub does not let you approve a pull request you can then
  merge unilaterally in a single-maintainer repository.
- `estate-gitops/scripts/inject.sh` pushes a bad commit straight to `main` —
  that is the whole point of it. Humans break things directly; the agent fixes
  them through review. That asymmetry is the demo. A protected `main` that
  admins cannot push to removes the ability to stage a failure at all.

So the correct setting is the one you already have:

```bash
gh api -X PUT repos/mazzyy/estate-gitops/branches/main/protection --input - <<'JSON'
{
  "required_status_checks": null,
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "dismiss_stale_reviews": true
  },
  "restrictions": null
}
JSON
```

The bypass belongs to the human reviewer, who is the control — not to the thing
being controlled. That distinction is the whole point, and it only becomes true
once the agent stops borrowing the reviewer's identity.

Which is exactly why this was a hole before: with a PAT, the agent *was* the
admin, so "the bypass belongs to the human" and "the bypass belongs to the
agent" were the same sentence.

You can approve the App's pull requests because the App is a different
principal from you. It cannot approve its own.

### 6. Prove it

```bash
cd estate-gitops && ./scripts/verify-github-token.sh
```

Expected:

```
identity:   GitHub App <id> · installation <id>

Must be refused
  ✗ denied   direct commit to main (HTTP 409)
      no bypass inherited — the same refusal covers merging unapproved work
  ✗ denied   read branch protection (HTTP 403 — no administration scope)

  The agent can propose changes and cannot land them.
```

## Least privilege at the moment of use

The App is installed with `contents: write` and `pull requests: write`. Each
installation token Warden mints is narrowed further, to exactly what the
Remediator needs for that run:

```python
TOKEN_PERMISSIONS = {"contents": "write", "pull_requests": "write", "metadata": "read"}
```

Installation tokens also expire after an hour, so a leaked one has a short
life — unlike a PAT, which lives until someone remembers to revoke it.

## What Warden says when the boundary is not enforced

Nothing here is hidden at runtime. On the PAT path:

- the demo header prints `boundary   NOT enforced` with the reason and the
  three environment variables that fix it;
- every pull request footer says the agent is authenticated with a PAT, that
  the PAT inherits the operator's repository role, and that the boundary is
  "a promise, not a control";
- `verify-github-token.sh` exits non-zero.

`tests/test_github_identity.py` asserts that the phrase "cannot merge this"
can only appear on the App path.
