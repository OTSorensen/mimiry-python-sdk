"""Main client class for the Mimiry API."""

import time
from typing import Any, Dict, List, Optional

import httpx

from .exceptions import MimiryError, raise_for_status

DEFAULT_BASE_URL = "https://ypoycmbljujlkmjuhfif.supabase.co/functions/v1/public-api"
DEFAULT_TIMEOUT = 30.0


class MimiryClient:
    """Client for the Mimiry GPU Cloud API.

    Args:
        api_key: Your Mimiry API key (starts with ``mky_``).
        base_url: Override the default API endpoint.
        timeout: Request timeout in seconds (default 30).
        max_retries: Number of retries for transient failures (default 3).

    Example::

        from mimiry import MimiryClient

        client = MimiryClient(api_key="mky_your_key_here")
        gpus = client.list_instance_types()
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 3,
    ):
        if not api_key:
            raise MimiryError("api_key is required")

        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = httpx.HTTPTransport(retries=max_retries)
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=self._timeout,
            transport=self._transport,
        )

    # ── internal helpers ──────────────────────────────────────────

    def _request(self, method: str, path: str, **kwargs) -> Any:
        """Make an HTTP request and return parsed JSON."""
        response = self._client.request(method, path, **kwargs)
        body = response.json() if response.content else {}
        raise_for_status(response.status_code, body)
        return body

    def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        return self._request("GET", path, params=params)

    def _post(self, path: str, json: Optional[Dict] = None) -> Any:
        return self._request("POST", path, json=json)

    def _delete(self, path: str) -> Any:
        return self._request("DELETE", path)

    # ── Jobs ──────────────────────────────────────────────────────

    def list_jobs(self) -> List[Dict]:
        """List all batch jobs for the authenticated user.

        Returns:
            List of job dictionaries.

        Required scope: ``jobs:read``
        """
        return self._get("/jobs")

    def get_job(self, job_id: str) -> Dict:
        """Get details of a specific job.

        Args:
            job_id: The UUID of the job.

        Returns:
            Job dictionary with full details.

        Required scope: ``jobs:read``
        """
        return self._get(f"/jobs/{job_id}")

    def submit_job(
        self,
        name: str,
        instance_type: str,
        image: str,
        location: str,
        ssh_key_ids: List[str],
        startup_script: str = "",
        provider: str = "datacrunch",
        auto_shutdown: bool = True,
        heartbeat_timeout_seconds: int = 600,
        max_runtime_seconds: Optional[int] = None,
    ) -> Dict:
        """Submit a new batch job.

        Args:
            name: Human-readable job name.
            instance_type: GPU instance type (e.g. ``"1V100.6V"``).
            image: OS image code (e.g. ``"ubuntu-22.04-cuda-12.0"``).
            location: Datacenter code (e.g. ``"FIN-01"``).
            ssh_key_ids: List of SSH key UUIDs to inject.
            startup_script: Bash script to run on boot.
            provider: Cloud provider slug (default ``"datacrunch"``).
            auto_shutdown: Terminate instance when script finishes.
            heartbeat_timeout_seconds: Seconds without heartbeat before
                marking the job stale (default 600).
            max_runtime_seconds: Hard time limit for the job. ``None``
                means no limit.

        Returns:
            Created job dictionary including ``id`` and ``status``.

        Required scopes: ``jobs:write``, ``instances:read``
        """
        payload = {
            "name": name,
            "instance_type": instance_type,
            "image": image,
            "location": location,
            "ssh_key_ids": ssh_key_ids,
            "startup_script": startup_script,
            "provider": provider,
            "auto_shutdown": auto_shutdown,
            "heartbeat_timeout_seconds": heartbeat_timeout_seconds,
        }
        if max_runtime_seconds is not None:
            payload["max_runtime_seconds"] = max_runtime_seconds
        return self._post("/jobs", json=payload)

    def cancel_job(self, job_id: str) -> Dict:
        """Cancel a running or queued job.

        This terminates the underlying cloud instance if one is running.

        Args:
            job_id: The UUID of the job to cancel.

        Returns:
            Updated job dictionary.

        Required scope: ``jobs:write``
        """
        return self._delete(f"/jobs/{job_id}")

    def wait_for_job(
        self,
        job_id: str,
        poll_interval: float = 10,
        timeout: float = 3600,
        terminal_statuses: Optional[List[str]] = None,
    ) -> Dict:
        """Poll a job until it reaches a terminal status.

        Args:
            job_id: The UUID of the job.
            poll_interval: Seconds between status checks (default 10).
            timeout: Maximum seconds to wait (default 3600).
            terminal_statuses: Statuses considered final. Defaults to
                ``["completed", "failed", "cancelled"]``.

        Returns:
            Final job dictionary.

        Raises:
            MimiryError: If the timeout is exceeded.
        """
        if terminal_statuses is None:
            terminal_statuses = ["completed", "failed", "cancelled"]

        start = time.monotonic()
        while True:
            job = self.get_job(job_id)
            if job.get("status") in terminal_statuses:
                return job
            elapsed = time.monotonic() - start
            if elapsed + poll_interval > timeout:
                raise MimiryError(
                    f"Timed out waiting for job {job_id} after {timeout}s "
                    f"(last status: {job.get('status')})"
                )
            time.sleep(poll_interval)

    def submit_job_and_wait(
        self,
        poll_interval: float = 10,
        timeout: float = 3600,
        **job_kwargs,
    ) -> Dict:
        """Submit a job and block until it finishes.

        Accepts the same keyword arguments as :meth:`submit_job`, plus
        ``poll_interval`` and ``timeout`` for the wait phase.

        Returns:
            Final job dictionary.
        """
        job = self.submit_job(**job_kwargs)
        return self.wait_for_job(
            job["id"],
            poll_interval=poll_interval,
            timeout=timeout,
        )

    # ── Instances / GPU Types ─────────────────────────────────────

    def list_instance_types(
        self,
        currency: str = "usd",
        provider: Optional[str] = None,
    ) -> List[Dict]:
        """List available GPU instance types with pricing.

        Args:
            currency: Price currency (default ``"usd"``).
            provider: Filter by provider slug (optional).

        Returns:
            List of instance type dictionaries.

        Required scope: ``instances:read``
        """
        params = {"currency": currency}
        if provider:
            params["provider"] = provider
        return self._get("/instances", params=params)

    # ── Availability ──────────────────────────────────────────────

    def check_availability(
        self,
        instance_type: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> Any:
        """Check real-time GPU availability.

        Args:
            instance_type: Specific instance type to check. If ``None``,
                returns availability for all types.
            provider: Filter by provider slug (optional).

        Returns:
            Availability data (list or dict depending on query).

        Required scope: ``instances:read``
        """
        params = {}
        if provider:
            params["provider"] = provider
        if instance_type:
            return self._get(f"/availability/{instance_type}", params=params)
        return self._get("/availability", params=params)

    # ── Locations ─────────────────────────────────────────────────

    def list_locations(self, provider: Optional[str] = None) -> List[Dict]:
        """List available datacenter locations.

        Args:
            provider: Filter by provider slug (optional).

        Returns:
            List of location dictionaries.

        Required scope: ``instances:read``
        """
        params = {}
        if provider:
            params["provider"] = provider
        return self._get("/locations", params=params)

    # ── OS Images ─────────────────────────────────────────────────

    def list_images(
        self,
        instance_type: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> List[Dict]:
        """List available OS images.

        Args:
            instance_type: Filter images compatible with this type.
            provider: Filter by provider slug (optional).

        Returns:
            List of image dictionaries.

        Required scope: ``instances:read``
        """
        params = {}
        if instance_type:
            params["instance_type"] = instance_type
        if provider:
            params["provider"] = provider
        return self._get("/images", params=params)

    # ── Providers ─────────────────────────────────────────────────

    def list_providers(self) -> List[Dict]:
        """List all supported cloud providers.

        Returns:
            List of provider dictionaries.

        Required scope: ``instances:read``
        """
        return self._get("/providers")

    # ── SSH Keys ──────────────────────────────────────────────────

    def list_ssh_keys(self) -> List[Dict]:
        """List all SSH keys for the authenticated user.

        Returns:
            List of SSH key dictionaries.

        Required scope: ``ssh_keys:read``
        """
        return self._get("/ssh-keys")

    def add_ssh_key(self, name: str, public_key: str) -> Dict:
        """Register a new SSH public key.

        Args:
            name: Human-readable key name.
            public_key: The full SSH public key string
                (e.g. ``"ssh-ed25519 AAAA... user@host"``).

        Returns:
            Created SSH key dictionary including ``id``.

        Required scope: ``ssh_keys:write``
        """
        return self._post("/ssh-keys", json={"name": name, "public_key": public_key})

    def delete_ssh_key(self, key_id: str) -> Dict:
        """Delete an SSH key.

        Args:
            key_id: The UUID of the key to delete.

        Returns:
            Confirmation dictionary.

        Required scope: ``ssh_keys:write``
        """
        return self._delete(f"/ssh-keys/{key_id}")

    # ── Lifecycle ─────────────────────────────────────────────────

    def close(self):
        """Close the underlying HTTP client.

        Call this when you're done using the client, or use it as a
        context manager::

            with MimiryClient(api_key="...") as client:
                client.list_jobs()
        """
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __repr__(self):
        return f"MimiryClient(base_url={self._base_url!r})"
