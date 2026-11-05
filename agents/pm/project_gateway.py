"""PM's project entry point uses the durable Control Node project lifecycle."""

from urllib.parse import quote

import httpx


class ControlGatewayError(RuntimeError):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail
        super().__init__(str(detail))


def control_request(config, method, path="", payload=None, resource="runs"):
    if not config.control_admin_token:
        raise ControlGatewayError(503, "Set CONTROL_ADMIN_TOKEN in PM .env to the Control Node ADMIN_TOKEN")
    with httpx.Client(timeout=120.0) as client:
        try:
            response = client.request(method, f"{config.control_url.rstrip('/')}/api/v1/{resource}{path}",
                headers={"Authorization": f"Bearer {config.control_admin_token}"}, json=payload)
        except httpx.RequestError as exc:
            raise ControlGatewayError(503, "Control Node is unavailable") from exc
        if response.is_error:
            try:
                detail = response.json().get("detail") or response.json().get("error") or "Control Node rejected the request"
            except ValueError:
                detail = "Control Node rejected the request"
            raise ControlGatewayError(response.status_code, detail)
        return response.json()


def project_path(run_id, suffix=""):
    return f"/{quote(run_id, safe='')}{suffix}"
