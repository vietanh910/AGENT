"""Kiểm tra và xác thực chữ ký Webhook từ GitHub."""

from __future__ import annotations

import hashlib
import hmac


def verify_github_webhook_signature(
    payload_bytes: bytes,
    signature_header: str | None,
    secret: str,
) -> bool:
    """Xác thực chữ ký HMAC SHA-256 của Webhook gửi từ GitHub (X-Hub-Signature-256).

    Tham số:
    - payload_bytes: Toàn bộ body nguyên bản của HTTP request dạng bytes.
    - signature_header: Giá trị header X-Hub-Signature-256 (có tiền tố sha256=...).
    - secret: Webhook secret cấu hình trong GitHub repository settings.
    """
    if not signature_header or not secret:
        return False

    if not signature_header.startswith("sha256="):
        return False

    expected_sig = signature_header[7:]  # Cắt bỏ 'sha256='
    mac = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256)
    computed_sig = mac.hexdigest()

    return hmac.compare_digest(computed_sig, expected_sig)
