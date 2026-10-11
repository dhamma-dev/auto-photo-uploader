"""Minimal Immich API client: just what the importer needs."""
import httpx

CHECK_CHUNK = 500


class ImmichError(Exception):
    pass


class Immich:
    def __init__(self, base_url: str, api_key: str):
        # Base URL without /api (the same form immich-go takes).
        self.base = base_url.rstrip("/")
        self.headers = {"x-api-key": api_key, "Accept": "application/json"}

    def _request(self, method: str, path: str, **kw) -> dict:
        try:
            r = httpx.request(method, f"{self.base}/api{path}", headers=self.headers, timeout=30, **kw)
        except httpx.HTTPError as e:
            raise ImmichError(f"Can't reach Immich at {self.base}: {e}") from e
        if r.status_code in (401, 403):
            raise ImmichError("Immich rejected the API key (check IMMICH_API_KEY and its permissions)")
        if r.status_code >= 400:
            raise ImmichError(f"Immich {method} {path} failed: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    def whoami(self) -> str:
        me = self._request("GET", "/users/me")
        return me.get("email") or me.get("name") or "?"

    def present(self, checksums: dict[str, str]) -> set[str]:
        """Return the ids (keys of `checksums`, values are sha1 hex) whose content Immich already has.

        Immich answers bulk-upload-check with action=reject + reason=duplicate exactly when
        an asset with that checksum exists for this user — the independent proof we rely on.
        """
        found: set[str] = set()
        items = list(checksums.items())
        for i in range(0, len(items), CHECK_CHUNK):
            chunk = items[i : i + CHECK_CHUNK]
            resp = self._request(
                "POST",
                "/assets/bulk-upload-check",
                json={"assets": [{"id": k, "checksum": v} for k, v in chunk]},
            )
            for res in resp.get("results", []):
                if res.get("action") == "reject" and res.get("reason") == "duplicate":
                    found.add(res["id"])
        return found
