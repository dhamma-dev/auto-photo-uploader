"""Minimal Immich API client: just what the importer needs."""
import httpx

CHUNK = 500


class ImmichError(Exception):
    pass


class Immich:
    def __init__(self, base_url: str, api_key: str):
        # Base URL without /api (the same form immich-go takes).
        self.base = base_url.rstrip("/")
        self.headers = {"x-api-key": api_key, "Accept": "application/json"}

    def _request(self, method: str, path: str, **kw):
        try:
            r = httpx.request(method, f"{self.base}/api{path}", headers=self.headers, timeout=30, **kw)
        except httpx.HTTPError as e:
            raise ImmichError(f"Can't reach Immich at {self.base}: {e}") from e
        if r.status_code in (401, 403):
            raise ImmichError(f"Immich refused {method} {path} (check IMMICH_API_KEY and its permissions)")
        if r.status_code >= 400:
            raise ImmichError(f"Immich {method} {path} failed: HTTP {r.status_code} {r.text[:200]}")
        return r.json() if r.content else None

    def whoami(self) -> str:
        me = self._request("GET", "/users/me")
        return me.get("email") or me.get("name") or "?"

    def present(self, checksums: dict[str, str]) -> dict[str, str]:
        """{id: assetId} for the ids (keys of `checksums`, values are sha1 hex) Immich already has.

        Immich answers bulk-upload-check with action=reject + reason=duplicate exactly when
        an asset with that checksum exists for this user — the independent proof we rely on.
        """
        found: dict[str, str] = {}
        items = list(checksums.items())
        for i in range(0, len(items), CHUNK):
            resp = self._request(
                "POST",
                "/assets/bulk-upload-check",
                json={"assets": [{"id": k, "checksum": v} for k, v in items[i : i + CHUNK]]},
            )
            for res in resp.get("results", []):
                if res.get("action") == "reject" and res.get("reason") == "duplicate":
                    found[res["id"]] = res.get("assetId")
        return found

    def album_names(self) -> list[str]:
        return sorted({a["albumName"] for a in self._request("GET", "/albums")}, key=str.lower)

    def add_to_album(self, name: str, asset_ids: list[str]) -> str:
        """Add assets to the album called `name` (created if missing). Returns the album id."""
        album = next((a for a in self._request("GET", "/albums") if a["albumName"] == name), None)
        if album is None:
            album = self._request("POST", "/albums", json={"albumName": name})
        for i in range(0, len(asset_ids), CHUNK):
            self._request("PUT", f"/albums/{album['id']}/assets", json={"ids": asset_ids[i : i + CHUNK]})
        return album["id"]

    def tag(self, names: list[str], asset_ids: list[str]) -> None:
        tags = self._request("PUT", "/tags", json={"tags": names})
        tag_ids = [t["id"] for t in tags]
        for i in range(0, len(asset_ids), CHUNK):
            self._request("PUT", "/tags/assets", json={"tagIds": tag_ids, "assetIds": asset_ids[i : i + CHUNK]})
