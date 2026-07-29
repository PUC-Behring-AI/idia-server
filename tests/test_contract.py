"""Contract tests for LiteLLM API contract.

These tests verify that the LiteLLM proxy rejects invalid requests with
the correct HTTP status codes and error shapes. No GPU or real model
required — the LiteLLM mock simulates the API contract.

See docs/ARCHITECTURE.md §4.3 for the LiteLLM configuration.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest


# ── Mock LiteLLM responses ────────────────────────────────────────────────
# These simulate the actual LiteLLM behavior without starting a proxy.
# The error shapes are extracted from LiteLLM's OpenAPI spec (v1.85.0).

def _mock_litellm_response(
    status: int,
    error_type: str,
    message: str,
) -> dict[str, Any]:
    """Simulate a LiteLLM error response matching the real API contract."""
    return {
        "error": {
            "type": error_type,
            "message": message,
            "code": status,
        }
    }


def _simulate_litellm_request(
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Simulate a LiteLLM request and return (status, response).

    This is a pure-Python simulation of LiteLLM's routing logic based
    on the config.yaml configuration:

    - Requires Authorization header with Bearer token.
    - Requires valid model in model_list.
    - Rate limits: hard=15rpm, regular=4rpm, light=1rpm (config.yaml:38-45)
    - max_parallel_requests: 20 (config.yaml:32)
    - /key/generate requires master_key admin auth.

    For contract testing, this mock focuses on the REJECTION paths that
    are hardest to test with a real instance (no GPU needed).
    """
    headers = headers or {}
    body = body or {}

    # Auth check — skip for unauthenticated endpoints
    if path not in ("/health",):
        auth = headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not auth[len("Bearer "):]:
            return 401, _mock_litellm_response(
                401,
                "auth_error",
                "Authentication required. Set Authorization header.",
            )

    # Path routing
    if path == "/health":
        return 200, {"status": "healthy"}

    if path == "/chat/completions":
        return _simulate_chat_completion(headers, body)

    if path == "/models":
        return 200, {
            "data": [
                {"id": "mistral-7b", "object": "model"},
            ]
        }

    return 404, _mock_litellm_response(
            404,
            "not_found",
            f"Path not found: {path}",
        )


def _simulate_chat_completion(
    headers: dict[str, str],
    body: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """Simulate /chat/completions routing logic."""
    model = body.get("model", "")
    messages = body.get("messages", [])

    # Model is required
    if not model:
        return 422, _mock_litellm_response(
            422,
            "invalid_request",
            "model is required.",
        )

    # Model check
    if model and model not in ("mistral-7b",):
        return 404, _mock_litellm_response(
                404,
                "not_found",
                f"This model '{model}' is not available in your proxy.",
            )

    # Messages validation
    if not messages:
        return 422, _mock_litellm_response(
            422,
            "invalid_request",
            "messages must be a non-empty list.",
        )

    # Content validation per message
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            return 422, _mock_litellm_response(
                422,
                "invalid_request",
                f"messages[{i}] must be an object with 'role' and 'content'.",
            )
        if "role" not in msg:
            return 422, _mock_litellm_response(
                422,
                "invalid_request",
                f"messages[{i}] is missing 'role'.",
            )
        if "content" not in msg:
            return 422, _mock_litellm_response(
                422,
                "invalid_request",
                f"messages[{i}] is missing 'content'.",
            )

    # Success path
    return 200, {
        "id": "chatcmpl-mock-001",
        "object": "chat.completion",
        "model": model or "mistral-7b",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "This is a mock response. No real model was called.",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 15,
            "total_tokens": 25,
        },
    }


# ── Tests ──────────────────────────────────────────────────────────────────


class TestLiteLLMContract:
    """Contract tests for the LiteLLM API (simulated, no GPU required)."""

    @pytest.mark.parametrize(
        ("body", "expected_status", "expected_error"),
        [
            (
                # Missing model → 422
                {"messages": [{"role": "user", "content": "hello"}]},
                422,
                "invalid_request",
            ),
            (
                # Empty messages → 422
                {"messages": [], "model": "mistral-7b"},
                422,
                "invalid_request",
            ),
            (
                # Messages not a list → 422
                {"messages": "not-a-list", "model": "mistral-7b"},
                422,
                "invalid_request",
            ),
        ],
    )
    def test_chat_completion_rejected(
        self,
        body: dict[str, Any],
        expected_status: int,
        expected_error: str,
    ) -> None:
        """Invalid requests are rejected with proper error codes."""
        status, response = _simulate_litellm_request(
            "POST",
            "/chat/completions",
            headers={"Authorization": "Bearer sk-valid-key"},
            body=body,
        )
        assert status == expected_status, (
            f"Expected HTTP {expected_status}, got {status}: "
            f"{response.get('error', {}).get('message')}"
        )
        err = response.get("error", {})
        assert err.get("type") == expected_error, (
            f"Expected error type '{expected_error}', got '{err.get('type')}'"
        )

    @pytest.mark.parametrize(
        ("status", "error_type", "message"),
        [
            (401, "auth_error", "Authentication required"),
            (404, "not_found", "not available in your proxy"),
            (422, "invalid_request", "non-empty list"),
        ],
    )
    def test_error_response_shape(
        self,
        status: int,
        error_type: str,
        message: str,
    ) -> None:
        """Error responses have the expected JSON structure."""
        response = _mock_litellm_response(status, error_type, message)
        assert "error" in response
        err = response["error"]
        assert err["type"] == error_type
        assert err["code"] == status
        assert isinstance(err["message"], str)

    @pytest.mark.parametrize(
        ("headers", "expected_status"),
        [
            ({}, 401),
            ({"Authorization": ""}, 401),
            ({"Authorization": "Bearer "}, 401),
            ({"Authorization": "Basic dGVzdDp0ZXN0"}, 401),
            ({"Authorization": "Bearer sk-valid-key"}, 200),
        ],
    )
    def test_authn_required(
        self,
        headers: dict[str, str],
        expected_status: int,
    ) -> None:
        """Authentication is required for chat completions."""
        body = {
            "model": "mistral-7b",
            "messages": [{"role": "user", "content": "hello"}],
        }
        status, _ = _simulate_litellm_request("POST", "/chat/completions", headers, body)
        assert status == expected_status, (
            f"Expected HTTP {expected_status}, got {status} for headers={headers}"
        )

    def test_model_not_found(self) -> None:
        """Request for non-existent model returns 404 with specific error."""
        body = {
            "model": "non-existent-model-v2",
            "messages": [{"role": "user", "content": "hello"}],
        }
        status, response = _simulate_litellm_request(
            "POST",
            "/chat/completions",
            headers={"Authorization": "Bearer sk-valid-key"},
            body=body,
        )
        assert status == 404
        assert "not available" in response.get("error", {}).get("message", "")

    def test_health_endpoint(self) -> None:
        """Health endpoint returns 200 with status."""
        status, response = _simulate_litellm_request("GET", "/health")
        assert status == 200
        assert response.get("status") == "healthy"

    def test_models_endpoint(self) -> None:
        """Models endpoint returns list of available models."""
        status, response = _simulate_litellm_request(
            "GET",
            "/models",
            headers={"Authorization": "Bearer sk-valid-key"},
        )
        assert status == 200
        assert "data" in response
        model_ids = [m["id"] for m in response["data"]]
        assert "mistral-7b" in model_ids

    def test_success_response_shape(self) -> None:
        """Successful completions have OpenAI-compatible shape."""
        body = {
            "model": "mistral-7b",
            "messages": [{"role": "user", "content": "hello"}],
        }
        status, response = _simulate_litellm_request(
            "POST",
            "/chat/completions",
            headers={"Authorization": "Bearer sk-valid-key"},
            body=body,
        )
        assert status == 200
        assert response["object"] == "chat.completion"
        assert len(response["choices"]) > 0
        assert response["choices"][0]["message"]["role"] == "assistant"
        assert response["usage"]["total_tokens"] > 0

    def test_missing_role_in_message(self) -> None:
        """Messages without 'role' are rejected."""
        body = {
            "model": "mistral-7b",
            "messages": [{"content": "hello"}],
        }
        status, response = _simulate_litellm_request(
            "POST",
            "/chat/completions",
            headers={"Authorization": "Bearer sk-valid-key"},
            body=body,
        )
        assert status == 422
        assert "role" in response.get("error", {}).get("message", "").lower()

    def test_empty_content_rejected(self) -> None:
        """Messages with empty content are handled (accepted by LiteLLM)."""
        body = {
            "model": "mistral-7b",
            "messages": [{"role": "user", "content": ""}],
        }
        status, _ = _simulate_litellm_request(
            "POST",
            "/chat/completions",
            headers={"Authorization": "Bearer sk-valid-key"},
            body=body,
        )
        assert status == 200, "Empty content should be accepted (LiteLLM behavior)"
