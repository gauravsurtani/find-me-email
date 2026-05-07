"""GitHub commit-author email harvester.

Strategy:
1. Find a candidate GitHub username for the person:
   a. From `person.extra` if a `github` URL was supplied in the input CSV
   b. From the GitHub user-search API (`q="<name>"`)
2. For each plausible username (max 3, ranked by name match):
   a. Fetch `/users/<username>` to grab public profile email if exposed
   b. Fetch `/users/<username>/events/public` for recent push events; pull
      author + committer emails out of each commit object
   c. Fetch top N public repos and scan recent commits for emails too
3. Filter:
   - GitHub's `<id>+<user>@users.noreply.github.com` mask → drop
   - Generic addresses (webmaster@, info@, etc.) → drop
   - Anything not containing a name token → mark SPECULATIVE only
4. Score:
   - Profile-page email + name match: HIGH (verified by user)
   - Commit author email + name match + matches school domain: MEDIUM
   - Commit author email + name match: LOW
   - Commit author email + no name match: SPECULATIVE

Why this works: most GitHub users (especially pre-2020 accounts) push from a
shell where `git config user.email` was set during initial install — exposing
their real mail forever in commit history. GitHub introduced the `noreply`
mask for the web UI default in 2017, but CLI-pushed commits still leak unless
the user explicitly switched. Industry research suggests ~40-70% leak rate.

Free, no auth required for low volume (60 req/hr); set GITHUB_TOKEN env var
for 5000 req/hr.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from find_me_email.providers.base import EnrichmentProvider
from find_me_email.schemas import Confidence, EmailCandidate, Person
from find_me_email.settings import settings

GITHUB_API = "https://api.github.com"
NOREPLY_RE = re.compile(r"^\d*\+?[A-Za-z0-9_-]+@users\.noreply\.github\.com$", re.I)
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
ROLE_LOCAL_PARTS = {
    "noreply",
    "no-reply",
    "webmaster",
    "info",
    "contact",
    "support",
    "admin",
    "help",
    "hello",
    "ci",
    "build",
    "actions",
    "github-actions[bot]",
}
GITHUB_URL_RE = re.compile(r"github\.com/([A-Za-z0-9-]{1,39})", re.I)


class GithubEmailLeakProvider(EnrichmentProvider):
    name = "github_email_leak"
    cost_per_call_usd = 0.0  # Pure GitHub API; no money

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.token: str = self.config.get("token") or settings.github_token
        self.timeout_s: float = float(self.config.get("timeout_s", 20.0))
        self.max_username_candidates: int = int(self.config.get("max_username_candidates", 3))
        self.max_repos_to_scan: int = int(self.config.get("max_repos_to_scan", 5))
        self.max_commits_per_repo: int = int(self.config.get("max_commits_per_repo", 30))
        # GitHub user-search API needs at least a name to be useful.

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
            # Quick rate-limit pre-check so we don't burn time on a known-empty quota.
            try:
                rl = await client.get(f"{GITHUB_API}/rate_limit")
                if rl.status_code == 200:
                    core = rl.json().get("resources", {}).get("core", {})
                    if core.get("remaining", 0) < len(targets) * 3:
                        from rich.console import Console
                        Console().print(
                            f"[yellow]github_email_leak: only {core.get('remaining', 0)} "
                            f"API calls left in the hour for ~{len(targets) * 3} needed. "
                            f"{'Set GITHUB_TOKEN in .env for 5000/hr.' if not self.token else 'Wait for reset at ' + str(core.get('reset', '?'))}[/yellow]"
                        )
            except Exception:
                pass

            async def _one(person: Person) -> None:
                async with sem:
                    try:
                        out[person.row_id] = await self._enrich_one(client, person)
                    except Exception as e:
                        out[person.row_id].append(self._error_candidate(str(e)))

            await asyncio.gather(*(_one(p) for p in targets), return_exceptions=True)
        return out

    # ---------------------------------------------------------------- pipeline

    async def _enrich_one(
        self, client: httpx.AsyncClient, person: Person
    ) -> list[EmailCandidate]:
        usernames = await self._discover_usernames(client, person)
        if not usernames:
            return []

        results: dict[str, EmailCandidate] = {}
        person_tokens = self._person_tokens(person)
        target_domain = (person.school_domain or "").lower()

        for username in usernames[: self.max_username_candidates]:
            # 1. Profile page — has public email if the user opted to expose it.
            try:
                r = await client.get(f"{GITHUB_API}/users/{username}")
                if r.status_code == 200:
                    profile = r.json()
                    public_email = (profile.get("email") or "").strip().lower()
                    if public_email and self._is_usable_email(public_email):
                        self._merge(
                            results,
                            public_email,
                            person_tokens,
                            target_domain,
                            source=f"https://github.com/{username}",
                            origin="profile",
                        )
            except httpx.HTTPError:
                continue

            # 2. Recent public events — push events have full commit objects with email.
            try:
                r = await client.get(
                    f"{GITHUB_API}/users/{username}/events/public",
                    params={"per_page": 100},
                )
                if r.status_code == 200:
                    for event in r.json():
                        if event.get("type") != "PushEvent":
                            continue
                        for commit in (event.get("payload") or {}).get("commits") or []:
                            author = commit.get("author") or {}
                            email = (author.get("email") or "").strip().lower()
                            if email and self._is_usable_email(email):
                                self._merge(
                                    results,
                                    email,
                                    person_tokens,
                                    target_domain,
                                    source=f"https://github.com/{username}",
                                    origin="events",
                                )
            except httpx.HTTPError:
                pass

            # 3. Repo-level commit history — useful for older accounts whose
            #    activity is no longer in the events feed (events are 90-day TTL).
            try:
                r = await client.get(
                    f"{GITHUB_API}/users/{username}/repos",
                    params={"per_page": self.max_repos_to_scan, "sort": "pushed"},
                )
                if r.status_code != 200:
                    continue
                for repo in r.json():
                    full = repo.get("full_name")
                    if not full:
                        continue
                    cr = await client.get(
                        f"{GITHUB_API}/repos/{full}/commits",
                        params={"per_page": self.max_commits_per_repo},
                    )
                    if cr.status_code != 200:
                        continue
                    for commit in cr.json():
                        for who in ("author", "committer"):
                            cobj = (commit.get("commit") or {}).get(who) or {}
                            email = (cobj.get("email") or "").strip().lower()
                            if email and self._is_usable_email(email):
                                self._merge(
                                    results,
                                    email,
                                    person_tokens,
                                    target_domain,
                                    source=f"https://github.com/{full}",
                                    origin="repo_commits",
                                )
            except httpx.HTTPError:
                pass

        return list(results.values())

    # ------------------------------------------------------ username discovery

    async def _discover_usernames(
        self, client: httpx.AsyncClient, person: Person
    ) -> list[str]:
        # Direct hint in the source CSV (e.g., a `github` column).
        explicit = self._extract_github_url(person)
        if explicit:
            return [explicit]

        if not person.name:
            return []

        # GitHub user search. Bare-name-only first — bonus filters (school,
        # location) get AND-combined and zero out results too aggressively.
        # If we get many candidates, our username-overlap score handles
        # disambiguation downstream.
        q = f'"{person.name}" in:fullname'
        try:
            r = await client.get(
                f"{GITHUB_API}/search/users",
                params={"q": q, "per_page": 10},
            )
            if r.status_code != 200:
                return []
            items = r.json().get("items") or []
        except httpx.HTTPError:
            return []

        # Return logins ranked by name-similarity; GitHub already orders by
        # relevance but verify name overlap to avoid completely-wrong matches.
        ranked: list[tuple[int, str]] = []
        person_tokens = self._person_tokens(person)
        for item in items:
            login = (item.get("login") or "").strip()
            if not login:
                continue
            score = self._username_score(login, person_tokens)
            ranked.append((score, login))
        ranked.sort(key=lambda x: -x[0])
        return [login for score, login in ranked if score > 0]

    @staticmethod
    def _extract_github_url(person: Person) -> str | None:
        # Look in person.extra for any field that mentions github.com
        for v in (person.extra or {}).values():
            if isinstance(v, str):
                m = GITHUB_URL_RE.search(v)
                if m:
                    return m.group(1)
        return None

    @staticmethod
    def _person_tokens(person: Person) -> set[str]:
        tokens: set[str] = set()
        for s in (person.name, person.first_name, person.last_name):
            if not s:
                continue
            for t in re.split(r"[^a-z]+", s.lower()):
                if len(t) >= 3:
                    tokens.add(t)
        return tokens

    @staticmethod
    def _username_score(login: str, person_tokens: set[str]) -> int:
        if not person_tokens:
            return 0
        login_lower = login.lower()
        return sum(1 for tok in person_tokens if tok in login_lower)

    # ---------------------------------------------------------- email handling

    @staticmethod
    def _is_usable_email(email: str) -> bool:
        if not email or "@" not in email:
            return False
        if not EMAIL_RE.match(email):
            return False
        if NOREPLY_RE.match(email):
            return False
        local = email.split("@", 1)[0].lower()
        if local in ROLE_LOCAL_PARTS:
            return False
        return True

    def _merge(
        self,
        bag: dict[str, EmailCandidate],
        email: str,
        person_tokens: set[str],
        target_domain: str,
        source: str,
        origin: str,
    ) -> None:
        confidence, note = self._score(email, person_tokens, target_domain, origin)
        existing = bag.get(email)
        if existing is None:
            bag[email] = EmailCandidate(
                email=email,
                confidence=confidence,
                source_provider=self.name,
                verified=False,
                notes=note,
                cost_usd=0.0,
                raw={"source": source, "origin": origin},
            )
            return
        order = {
            Confidence.HIGH: 0,
            Confidence.MEDIUM: 1,
            Confidence.LOW: 2,
            Confidence.SPECULATIVE: 3,
        }
        if order[confidence] < order[existing.confidence]:
            existing.confidence = confidence
            existing.notes = note

    @staticmethod
    def _score(
        email: str, person_tokens: set[str], target_domain: str, origin: str
    ) -> tuple[Confidence, str]:
        local, _, dom = email.partition("@")
        local_lower = local.lower()
        token_match = (
            any(tok in local_lower for tok in person_tokens) if person_tokens else False
        )
        domain_match = bool(target_domain) and dom.endswith(target_domain.lstrip("."))

        if origin == "profile":
            # User explicitly set this as their public profile email.
            if token_match or domain_match:
                return (
                    Confidence.MEDIUM,
                    "github: public profile email (user-set, name/domain match)",
                )
            return Confidence.LOW, "github: public profile email (user-set)"
        # Commit-history sources (events / repo_commits)
        if token_match and domain_match:
            return (
                Confidence.MEDIUM,
                f"github: commit author email matches name+school ({origin})",
            )
        if token_match:
            return Confidence.LOW, f"github: commit author email matches name ({origin})"
        if domain_match:
            return Confidence.LOW, f"github: commit author email matches school domain ({origin})"
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
