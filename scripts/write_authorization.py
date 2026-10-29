from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class WriteAuthorization:
    allowed: bool
    environment: str
    source: str
    reason: str


def resolve_write_authorization(
    context: dict,
    env: Mapping[str, str] | None = None,
) -> WriteAuthorization:
    values = env or {}
    environment = str(context.get("environment") or "").strip().lower()
    raw_policy = context.get("write_operations")
    policy = raw_policy if isinstance(raw_policy, Mapping) else {}
    explicit = policy.get("authorized") is True
    scope = str(policy.get("scope") or "").strip().lower()
    if environment != "test":
        if explicit and scope == "test_environment":
            return WriteAuthorization(
                False,
                environment,
                "none",
                "test_environment_authorization_cannot_be_inherited",
            )
        valid_scopes = {environment, f"{environment}_environment"} if environment else set()
        if explicit and scope in valid_scopes:
            return WriteAuthorization(
                True, environment, "run_context", "explicit_context_authorization"
            )
        if explicit and scope:
            return WriteAuthorization(
                False,
                environment,
                "none",
                "write_authorization_scope_mismatch",
            )
        return WriteAuthorization(
            False,
            environment,
            "none",
            "non_test_environment_requires_explicit_context_authorization",
        )
    if explicit:
        return WriteAuthorization(True, environment, "run_context", "explicit_context_authorization")
    if str(values.get("ALLOW_WRITE_TESTS") or "").lower() == "true":
        return WriteAuthorization(True, environment, "environment", "legacy_environment_authorization")
    return WriteAuthorization(False, environment, "none", "write_operations_not_authorized")
