"""GitHub commit-author email harvester.

Most CLI-pushed Git commits embed `git config user.email` in the commit
object forever. This provider:
  1. Searches GitHub for usernames matching the person's name
  2. Pulls public profile email + recent commit-author emails (events feed
     + recent repos)
  3. Filters GitHub's `<id>+<user>@users.noreply.github.com` mask + role
     accounts
  4. Scores by name-token + school-domain match

Free + no auth required for low volume (60 req/hr); set GITHUB_TOKEN for
5000 req/hr.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx
from rich.console import Console

from find_me_email.providers._emailmatch import (
    EMAIL_FULLMATCH_RE,
    ROLE_LOCAL_PARTS,
    match_flags,
    merge_candidate,
    person_tokens,
)
from find_me_email.providers.base import EnrichmentProvider
from find_me_email.schemas import Confidence, EmailCandidate, Person

GITHUB_API = "https://api.github.com"
NOREPLY_RE = re.compile(r"^\d*\+?[A-Za-z0-9_-]+@users\.noreply\.github\.com$", re.I)
GITHUB_URL_RE = re.compile(r"github\.com/([A-Za-z0-9-]{1,39})", re.I)

# CI/bot accounts in addition to the standard role-accounts.
GITHUB_ROLE_LOCAL_PARTS = ROLE_LOCAL_PARTS | {
    "ci",
    "build",
    "actions",
    "github-actions[bot]",
    "dependabot[bot]",
}

console = Console()


class GithubEmailLeakProvider(EnrichmentProvider):
    name = "github_email_leak"
    cost_per_call_usd = 0.0  # Pure GitHub API; no money

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        from find_me_email.settings import settings as _settings

        self.token: str = self.config.get("token") or _settings.github_token
        self.timeout_s: float = float(self.config.get("timeout_s", 20.0))
        self.max_username_candidates: int = int(self.config.get("max_username_candidates", 3))
        self.max_repos_to_scan: int = int(self.config.get("max_repos_to_scan", 5))
        self.max_commits_per_repo: int = int(self.config.get("max_commits_per_repo", 30))

    def can_handle(self, person: Person) -> bool:
        return bool(person.name) or bool(self._extract_github_url(person))

    async def enrich(self, person: Person) -> list[EmailCandidate]:
        return (await self.enrich_batch([person])).get(person.row_id, [])

    async def enrich_batch(self, people: list[Person]) -> dict[str, list[EmailCandidate]]:
        out: dict[str, list[EmailCandidate]] = {p.row_id: [] for p in people}
        targets = [p for p in people if self.can_handle(p)]
        if not targets:
            return out

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "find-me-email/1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        sem = asyncio.Semaphore(4)
        async with httpx.AsyncClient(timeout=self.timeout_s, headers=headers) as client:
            await self._warn_if_quota_low(client, len(targets))

            async def _one(person: Person) -> None:
                async with sem:
                    try:
                        out[person.row_id] = await self._enrich_one(client, person)
                    except Exception as e:
                        out[person.row_id].append(self._error_candidate(str(e)))

            await asyncio.gather(*(_one(p) for p in targets), return_exceptions=True)
        return out

    async def _warn_if_quota_low(self, client: httpx.AsyncClient, n_targets: int) -> None:
        # ~3 API calls per target (search + profile + events); rough overestimate.
        try:
            rl = await client.get(f"{GITHUB_API}/rate_limit")
        except Exception:
            return
        if rl.status_code != 200:
            return
        core = rl.json().get("resources", {}).get("core", {})
        remaining = core.get("remaining", 0)
        if remaining >= n_targets * 3:
            return
        suffix = (
            "Set GITHUB_TOKEN in .env for 5000/hr."
            if not self.token
            else f"Wait for reset at {core.get('reset', '?')}"
        )
        console.print(
            f"[yellow]github_email_leak: only {remaining} API calls left "
            f"in the hour for ~{n_targets * 3} needed. {suffix}[/yellow]"
        )

    # ---------------------------------------------------------------- pipeline

    async def _enrich_one(
        self, client: httpx.AsyncClient, person: Person
    ) -> list[EmailCandidate]:
        usernames = await self._discover_usernames(client, person)
        if not usernames:
            return []

        results: dict[str, EmailCandidate] = {}
        tokens = person_tokens(person)
        target_domain = (person.school_domain or "").lower()

        for username in usernames[: self.max_username_candidates]:
            await self._harvest_username(
                client, username, results, tokens, target_domain
            )
        return list(results.values())

    async def _harvest_username(
        self,
        client: httpx.AsyncClient,
        username: str,
        results: dict[str, EmailCandidate],
        tokens: set[str],
        target_domain: str,
    ) -> None:
        # Three independent endpoints; fan out concurrently.
        profile_t = asyncio.create_task(
            client.get(f"{GITHUB_API}/users/{username}")
        )
        events_t = asyncio.create_task(
            client.get(
                f"{GITHUB_API}/users/{username}/events/public",
                params={"per_page": 100},
            )
        )
        repos_t = asyncio.create_task(
            client.get(
                f"{GITHUB_API}/users/{username}/repos",
                params={"per_page": self.max_repos_to_scan, "sort": "pushed"},
            )
        )
        profile_r, events_r, repos_r = await asyncio.gather(
            profile_t, events_t, repos_t, return_exceptions=True
        )

        # Profile: public email if user opted to expose it.
        if isinstance(profile_r, httpx.Response) and profile_r.status_code == 200:
            email = (profile_r.json().get("email") or "").strip().lower()
            if email and self._is_usable(email):
                self._record(
                    results,
                    email,
                    tokens,
                    target_domain,
                    source=f"https://github.com/{username}",
                    origin="profile",
                )

        # Events feed: PushEvents have full commit objects with author email.
        if isinstance(events_r, httpx.Response) and events_r.status_code == 200:
            for event in events_r.json():
                if event.get("type") != "PushEvent":
                    continue
                for commit in (event.get("payload") or {}).get("commits") or []:
                    email = ((commit.get("author") or {}).get("email") or "").strip().lower()
                    if email and self._is_usable(email):
                        self._record(
                            results,
                            email,
                            tokens,
                            target_domain,
                            source=f"https://github.com/{username}",
                            origin="events",
                        )

        # Repo commit history: covers older accounts whose events have rolled off
        # (events feed has a 90-day TTL).
        if isinstance(repos_r, httpx.Response) and repos_r.status_code == 200:
            repo_full_names = [
                r.get("full_name") for r in repos_r.json() if r.get("full_name")
            ]
            commit_responses = await asyncio.gather(
                *(
                    client.get(
                        f"{GITHUB_API}/repos/{full}/commits",
                        params={"per_page": self.max_commits_per_repo},
                    )
                    for full in repo_full_names
                ),
                return_exceptions=True,
            )
            for full, cr in zip(repo_full_names, commit_responses):
                if not isinstance(cr, httpx.Response) or cr.status_code != 200:
                    continue
                for commit in cr.json():
                    for who in ("author", "committer"):
                        email = (
                            (commit.get("commit") or {}).get(who) or {}
                        ).get("email", "").strip().lower()
                        if email and self._is_usable(email):
                            self._record(
                                results,
                                email,
                                tokens,
                                target_domain,
                                source=f"https://github.com/{full}",
                                origin="repo_commits",
                            )

    # ------------------------------------------------------ username discovery

    async def _discover_usernames(
        self, client: httpx.AsyncClient, person: Person
    ) -> list[str]:
        explicit = self._extract_github_url(person)
        if explicit:
            return [explicit]

        if not person.name:
            return []

        # Bare-name-only search; bonus filters AND-combine and zero out results.
        try:
            r = await client.get(
                f"{GITHUB_API}/search/users",
                params={"q": f'"{person.name}" in:fullname', "per_page": 10},
            )
        except httpx.HTTPError:
            return []
        if r.status_code != 200:
            return []

        tokens = person_tokens(person)
        ranked: list[tuple[int, str]] = []
        for item in r.json().get("items") or []:
            login = (item.get("login") or "").strip()
            if not login:
                continue
            score = sum(1 for tok in tokens if tok in login.lower())
            if score > 0:
                ranked.append((score, login))
        ranked.sort(key=lambda x: -x[0])
        return [login for _, login in ranked]

    @staticmethod
    def _extract_github_url(person: Person) -> str | None:
        for v in (person.extra or {}).values():
            if isinstance(v, str):
                m = GITHUB_URL_RE.search(v)
                if m:
                    return m.group(1)
        return None

    # ---------------------------------------------------------- email handling

    @staticmethod
    def _is_usable(email: str) -> bool:
        if not EMAIL_FULLMATCH_RE.match(email):
            return False
        if NOREPLY_RE.match(email):
            return False
        local = email.split("@", 1)[0].lower()
        return local not in GITHUB_ROLE_LOCAL_PARTS

    def _record(
        self,
        results: dict[str, EmailCandidate],
        email: str,
        tokens: set[str],
        target_domain: str,
        source: str,
        origin: str,
    ) -> None:
        confidence, note = self._score(email, tokens, target_domain, origin)
        merge_candidate(
            results,
            email=email,
            confidence=confidence,
            notes=note,
            source_provider=self.name,
            raw={"source": source, "origin": origin},
        )

    @staticmethod
    def _score(
        email: str, tokens: set[str], target_domain: str, origin: str
    ) -> tuple[Confidence, str]:
        token_match, domain_match = match_flags(email, tokens, target_domain)

        if origin == "profile":
            # User explicitly set this as their public profile email.
            if token_match or domain_match:
                return (
                    Confidence.MEDIUM,
                    "github: public profile email (user-set, name/domain match)",
                )
            return Confidence.LOW, "github: public profile email (user-set)"
        if token_match and domain_match:
            return (
                Confidence.MEDIUM,
                f"github: commit author email matches name+school ({origin})",
            )
        if token_match:
            return Confidence.LOW, f"github: commit author email matches name ({origin})"
        if domain_match:
            return (
                Confidence.LOW,
                f"github: commit author email matches school domain ({origin})",
            )
        return (
            Confidence.SPECULATIVE,
            f"github: commit author email; no name/domain match ({origin})",
        )

    @staticmethod
    def _error_candidate(msg: str) -> EmailCandidate:
        return EmailCandidate(
            email="",
            confidence=Confidence.SPECULATIVE,
            source_provider="github_email_leak",
            verified=False,
            notes=f"github error: {msg}",
        )
