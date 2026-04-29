"""dataverse_client.py — MSAL auth + Dataverse Web API wrapper with retry/throttle."""

from __future__ import annotations

import logging
import time
from typing import Any, Iterator, Optional
from urllib.parse import urljoin

import msal
import requests

logger = logging.getLogger(__name__)

_RESOURCE = "https://{}/"   # filled with org host


class DataverseError(Exception):
    def __init__(self, message: str, status_code: int = 0, response_body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class DataverseClient:
    def __init__(self, cfg: dict):
        dv = cfg["dataverse"]
        self.org_url = dv["org_url"].rstrip("/")
        self.api_version = dv.get("api_version", "9.2")
        self.base_url = f"{self.org_url}/api/data/v{self.api_version}/"
        self.max_retries = int(dv.get("max_retries", 5))
        self.page_size = int(dv.get("page_size", 100))
        self.dry_run = cfg.get("run", {}).get("dry_run", False)

        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._msal_app = self._build_msal_app(dv)
        self._scopes = [f"{self.org_url}/.default"]

    # ------------------------------------------------------------------ #
    # Authentication                                                       #
    # ------------------------------------------------------------------ #

    def _build_msal_app(self, dv: dict):
        tenant_id = dv["tenant_id"]
        client_id = dv["client_id"]
        authority = f"https://login.microsoftonline.com/{tenant_id}"
        if dv.get("client_certificate_path"):
            with open(dv["client_certificate_path"], "rb") as f:
                cert_data = f.read()
            return msal.ConfidentialClientApplication(
                client_id, authority=authority,
                client_credential={"private_key": cert_data, "thumbprint": dv.get("thumbprint", "")},
            )
        return msal.ConfidentialClientApplication(
            client_id, authority=authority, client_credential=dv["client_secret"]
        )

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        result = self._msal_app.acquire_token_for_client(scopes=self._scopes)
        if "access_token" not in result:
            raise DataverseError(
                f"MSAL token acquisition failed: {result.get('error_description', result)}"
            )
        self._token = result["access_token"]
        self._token_expiry = time.time() + result.get("expires_in", 3600)
        return self._token

    def _headers(self, extra: Optional[dict] = None) -> dict:
        h = {
            "Authorization": f"Bearer {self._get_token()}",
            "Accept": "application/json",
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
            "Content-Type": "application/json",
        }
        if extra:
            h.update(extra)
        return h

    # ------------------------------------------------------------------ #
    # Core HTTP methods with retry                                         #
    # ------------------------------------------------------------------ #

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict] = None,
        json: Optional[Any] = None,
        stream: bool = False,
        extra_headers: Optional[dict] = None,
    ) -> requests.Response:
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.request(
                    method,
                    url,
                    headers=self._headers(extra_headers),
                    params=params,
                    json=json,
                    stream=stream,
                    timeout=120,
                )
                if resp.status_code == 429 or resp.status_code == 503:
                    wait = int(resp.headers.get("Retry-After", 2 ** attempt))
                    logger.warning("Throttled (%s). Waiting %ds (attempt %d/%d).",
                                   resp.status_code, wait, attempt, self.max_retries)
                    time.sleep(wait)
                    continue
                if resp.status_code >= 500 and attempt < self.max_retries:
                    wait = 2 ** attempt
                    logger.warning("Server error %s. Retrying in %ds.", resp.status_code, wait)
                    time.sleep(wait)
                    continue
                return resp
            except requests.RequestException as exc:
                if attempt == self.max_retries:
                    raise DataverseError(f"Request failed after {self.max_retries} attempts: {exc}") from exc
                time.sleep(2 ** attempt)
        raise DataverseError(f"All {self.max_retries} retries exhausted for {url}")

    # ------------------------------------------------------------------ #
    # Dataverse API helpers                                                #
    # ------------------------------------------------------------------ #

    def get(self, entity_set: str, params: Optional[dict] = None) -> dict:
        url = urljoin(self.base_url, entity_set)
        resp = self._request("GET", url, params=params)
        if not resp.ok:
            raise DataverseError(f"GET {entity_set} failed: {resp.status_code}", resp.status_code, resp.text)
        return resp.json()

    def get_all_pages(self, entity_set: str, params: Optional[dict] = None) -> Iterator[dict]:
        """Yield every record across all @odata.nextLink pages."""
        url = urljoin(self.base_url, entity_set)
        while url:
            resp = self._request("GET", url, params=params)
            if not resp.ok:
                raise DataverseError(f"GET {entity_set} failed: {resp.status_code}", resp.status_code, resp.text)
            data = resp.json()
            for record in data.get("value", []):
                yield record
            url = data.get("@odata.nextLink")
            params = None  # nextLink already contains all params

    def post(self, entity_set: str, body: dict) -> dict:
        url = urljoin(self.base_url, entity_set)
        resp = self._request("POST", url, json=body)
        if not resp.ok:
            raise DataverseError(f"POST {entity_set} failed: {resp.status_code}", resp.status_code, resp.text)
        # 204 No Content → return empty dict; 200/201 → return JSON
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    def delete(self, entity_set: str, record_id: str) -> None:
        if self.dry_run:
            logger.info("[DRY-RUN] DELETE %s(%s)", entity_set, record_id)
            return
        url = urljoin(self.base_url, f"{entity_set}({record_id})")
        resp = self._request("DELETE", url)
        if resp.status_code not in (204, 200):
            raise DataverseError(
                f"DELETE {entity_set}({record_id}) failed: {resp.status_code}",
                resp.status_code, resp.text,
            )

    def retain(self, entity_set: str, record_id: str) -> None:
        """Apply Dataverse LTDR Retain action to keep metadata read-only."""
        if self.dry_run:
            logger.info("[DRY-RUN] RETAIN %s(%s)", entity_set, record_id)
            return
        url = urljoin(self.base_url, f"{entity_set}({record_id})/Microsoft.Dynamics.CRM.Retain")
        resp = self._request("POST", url, json={})
        if resp.status_code not in (200, 204):
            raise DataverseError(
                f"Retain {entity_set}({record_id}) failed: {resp.status_code}",
                resp.status_code, resp.text,
            )

    def get_file_sas_url(self, entity_set: str, record_id: str, file_attribute: str) -> tuple[str, str]:
        """
        Call GetFileSasUrl to get a time-limited SAS URL for a file attribute.
        Returns (sas_url, file_name).
        """
        path = (
            f"GetFileSasUrl(Target=@p1,FileAttributeName='{file_attribute}')"
            f"?@p1={{\"@odata.id\":\"{entity_set}({record_id})\"}}"
        )
        url = urljoin(self.base_url, path)
        resp = self._request("GET", url)
        if not resp.ok:
            raise DataverseError(
                f"GetFileSasUrl for {entity_set}({record_id}) failed: {resp.status_code}",
                resp.status_code, resp.text,
            )
        result = resp.json().get("Result", {})
        return result.get("SasUrl", ""), result.get("FileName", "recording.mp4")

    def download_from_sas(self, sas_url: str) -> bytes:
        """Download a file from a SAS URL. Returns raw bytes."""
        resp = requests.get(sas_url, stream=True, timeout=300)
        if not resp.ok:
            raise DataverseError(f"SAS download failed: {resp.status_code}", resp.status_code)
        chunks = []
        for chunk in resp.iter_content(chunk_size=8 * 1024 * 1024):
            if chunk:
                chunks.append(chunk)
        return b"".join(chunks)

    def discover_file_attributes(self, entity_logical_name: str) -> list[dict]:
        """Return list of File-type attributes on an entity."""
        path = (
            f"EntityDefinitions(LogicalName='{entity_logical_name}')/Attributes"
            f"?$filter=AttributeType eq Microsoft.Dynamics.CRM.AttributeTypeCode'File'"
            f"&$select=LogicalName,DisplayName"
        )
        data = self.get(path)
        return data.get("value", [])
