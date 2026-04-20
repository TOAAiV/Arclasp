"""
aag.exceptions — SDK exception hierarchy.
"""

from __future__ import annotations


class ActionDeniedError(Exception):
    """
    Raised when the backend policy engine returns a 'deny' decision for an
    agent action.  Carries structured context so callers can surface a clear
    error message to operators.
    """

    def __init__(
        self,
        message: str,
        policy_name: str | None = None,
        condition: str | None = None,
        chain_context: str | None = None,
        remediation: str | None = None,
        docs_url: str | None = None,
    ) -> None:
        self.message = message
        self.policy_name = policy_name
        self.condition = condition
        self.chain_context = chain_context
        self.remediation = remediation
        self.docs_url = docs_url
        super().__init__(str(self))

    def __str__(self) -> str:
        lines = [f"ActionDeniedError: {self.message}"]
        if self.policy_name:
            lines.append(f"  Policy      : {self.policy_name}")
        if self.condition:
            lines.append(f"  Condition   : {self.condition}")
        if self.chain_context:
            lines.append(f"  Chain       : {self.chain_context}")
        if self.remediation:
            lines.append(f"  Remediation : {self.remediation}")
        if self.docs_url:
            lines.append(f"  Docs        : {self.docs_url}")
        return "\n".join(lines)


class PolicyViolationError(Exception):
    """
    Raised when an agent action violates a configured policy rule before or
    during backend evaluation (e.g. a local fast-path check).
    """

    def __init__(
        self,
        message: str,
        policy_name: str | None = None,
        condition: str | None = None,
        chain_context: str | None = None,
        remediation: str | None = None,
        docs_url: str | None = None,
    ) -> None:
        self.message = message
        self.policy_name = policy_name
        self.condition = condition
        self.chain_context = chain_context
        self.remediation = remediation
        self.docs_url = docs_url
        super().__init__(str(self))

    def __str__(self) -> str:
        lines = [f"PolicyViolationError: {self.message}"]
        if self.policy_name:
            lines.append(f"  Policy      : {self.policy_name}")
        if self.condition:
            lines.append(f"  Condition   : {self.condition}")
        if self.chain_context:
            lines.append(f"  Chain       : {self.chain_context}")
        if self.remediation:
            lines.append(f"  Remediation : {self.remediation}")
        if self.docs_url:
            lines.append(f"  Docs        : {self.docs_url}")
        return "\n".join(lines)


class BackendUnavailableError(Exception):
    """
    Raised when the aag backend cannot be reached and fail_mode is 'deny'.
    Carries the original failure message and the configured fail_mode for
    context.
    """

    def __init__(self, message: str, fail_mode: str = "deny") -> None:
        self.message = message
        self.fail_mode = fail_mode
        super().__init__(f"BackendUnavailableError: {message} (fail_mode={fail_mode})")


class ChainTimeoutError(Exception):
    """
    Raised when a chain exceeds the configured approval timeout without a
    resolution, or when the approval polling loop times out.
    """

    def __init__(self, chain_id: str, timeout_seconds: int) -> None:
        self.chain_id = chain_id
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"ChainTimeoutError: chain '{chain_id}' timed out after {timeout_seconds}s"
        )
