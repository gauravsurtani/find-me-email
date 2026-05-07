from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from rich.console import Console

from find_me_email.settings import settings

console = Console()

APIFY_BASE = "https://api.apify.com/v2"


class ApifyError(RuntimeError):
    pass


class ApifyClient:
    """Minimal async Apify client. Run actor with input, poll until done, fetch dataset."""

    def __init__(self, token: str | None = None, timeout: float = 60.0):
        self.token = token or settings.apify_token
        if not self.token:
            raise ApifyError("APIFY_TOKEN is not set")
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()

    async def run_actor_sync(
        self,
        actor_id: str,
        input_payload: dict[str, Any],
        wait_secs: int = 1800,
        progress_every_s: int = 30,
    ) -> list[dict[str, Any]]:
        """Run an actor and return its dataset items.

        Intentionally NOT wrapped in retry — retrying the whole flow on a poll
        timeout would spawn a duplicate actor run (= duplicate billing). Caller
        should retry only on transient errors during start.
        """
        actor_path = actor_id.replace("/", "~")
        start_url = f"{APIFY_BASE}/acts/{actor_path}/runs"
        params = {"token": self.token}

        r = await self._client.post(start_url, params=params, json=input_payload)
        r.raise_for_status()
        run = r.json()["data"]
        run_id = run["id"]
        dataset_id = run["defaultDatasetId"]
        console.print(f"  [dim]apify run started: {run_id} (waiting up to {wait_secs}s)[/dim]")

        deadline = time.time() + wait_secs
        status_url = f"{APIFY_BASE}/actor-runs/{run_id}"
        last_progress = time.time()
        last_status = ""
        while time.time() < deadline:
            await asyncio.sleep(5)
            sr = await self._client.get(status_url, params=params)
            sr.raise_for_status()
            data = sr.json()["data"]
            status = data["status"]
            if status != last_status or (time.time() - last_progress) >= progress_every_s:
                elapsed = int(time.time() - (deadline - wait_secs))
                stats = data.get("stats", {})
                console.print(
                    f"  [dim]…{run_id[:8]}: {status} "
                    f"(elapsed {elapsed}s, runtime {stats.get('runTimeSecs', 0)}s)[/dim]"
                )
                last_status = status
                last_progress = time.time()
            if status == "SUCCEEDED":
                break
            if status in ("FAILED", "ABORTED", "TIMED-OUT"):
                raise ApifyError(f"Actor {actor_id} {status}")
        else:
            # Don't leave the run orphaned — abort it so we don't keep getting billed.
            try:
                await self._client.post(f"{APIFY_BASE}/actor-runs/{run_id}/abort", params=params)
            except Exception:
                pass
            raise ApifyError(f"Actor {actor_id} did not finish in {wait_secs}s (run {run_id} aborted)")

        items_url = f"{APIFY_BASE}/datasets/{dataset_id}/items"
        ir = await self._client.get(items_url, params={**params, "clean": "true"})
        ir.raise_for_status()
        return ir.json()

    async def get_user_info(self) -> dict[str, Any]:
        r = await self._client.get(f"{APIFY_BASE}/users/me", params={"token": self.token})
        r.raise_for_status()
        return r.json()["data"]
