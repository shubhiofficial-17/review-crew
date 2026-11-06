"""Minimal GitHub REST client: fetch a PR's diff and post one review with inline comments."""

from __future__ import annotations

from reviewcrew.report import MARKER


class GitHub:
    def __init__(self, token: str, api: str = "https://api.github.com"):
        import httpx

        self._http = httpx.Client(
            base_url=api,
            timeout=60,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def pull(self, repo: str, number: int) -> dict:
        r = self._http.get(f"/repos/{repo}/pulls/{number}")
        r.raise_for_status()
        return r.json()

    def diff(self, repo: str, number: int) -> str:
        r = self._http.get(f"/repos/{repo}/pulls/{number}", headers={"Accept": "application/vnd.github.diff"})
        r.raise_for_status()
        return r.text

    def previous_reviews(self, repo: str, number: int) -> list[dict]:
        r = self._http.get(f"/repos/{repo}/pulls/{number}/reviews", params={"per_page": 100})
        r.raise_for_status()
        return [rv for rv in r.json() if MARKER in (rv.get("body") or "")]

    def post_review(self, repo: str, number: int, payload: dict) -> dict:
        r = self._http.post(f"/repos/{repo}/pulls/{number}/reviews", json=payload)
        if r.status_code == 422 and payload.get("comments"):
            # A comment GitHub can't anchor (e.g. the diff moved under us): fall back to summary only.
            fallback = {**payload, "comments": []}
            fallback["body"] += "\n\n<sub>Inline comments could not be placed on the current diff.</sub>"
            r = self._http.post(f"/repos/{repo}/pulls/{number}/reviews", json=fallback)
        r.raise_for_status()
        return r.json()
