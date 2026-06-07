"""
SentinelSwarm Hook — Pre-call security interceptor for any AI agent.

Drop this in front of ANY agent call to automatically screen inputs through
the SentinelSwarm security swarm before the agent ever sees them.

THREE ways to use it:

─────────────────────────────────────────────────────────────────────────────
Usage 1 — Decorator (wrap an existing agent function):

    from agents.hook import sentinel_guard

    @sentinel_guard
    def my_agent(prompt: str) -> str:
        return call_gpt4o(prompt)   # only runs if SentinelSwarm allows it

─────────────────────────────────────────────────────────────────────────────
Usage 2 — Inline hook (one call at any boundary):

    from agents.hook import SentinelHook

    hook = SentinelHook()
    result = hook.check("User input here")
    if not result.allowed:
        return f"Blocked. Incident: {result.incident_id}"
    # safe to proceed

─────────────────────────────────────────────────────────────────────────────
Usage 3 — HTTP hook (for calling a deployed SentinelSwarm instance):

    from agents.hook import RemoteSentinelHook

    hook = RemoteSentinelHook(sentinel_url="https://your-sentinelswarm.azurecontainerapps.io")
    result = hook.check("User input here")
    if not result.allowed:
        return f"Blocked. Incident: {result.incident_id}"

─────────────────────────────────────────────────────────────────────────────

Why this matters (the pitch):
    Any multi-agent system has boundary points where content crosses from
    one agent to another. SentinelSwarm sits at those boundaries and screens
    every crossing — without the receiving agent needing any security logic.

    Hook Point 1 → User input before Agent A
    Hook Point 2 → Agent A output before Agent B  ← catches indirect injection
    Hook Point 3 → Tool output before being processed
    Hook Point 4 → Final output before reaching user
"""

import functools
from dataclasses import dataclass
from typing import Callable, Any

from agents.watcher import run_watcher
from agents.classifier import run_classifier
from agents.analyst import run_analyst
from agents.responder import run_responder


# ─────────────────────────────────────────────────────────────────────────────
# Result object returned by every hook check
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class HookResult:
    """
    The result of a SentinelSwarm security check.

    Fields:
        allowed     — True if the input is safe to forward, False if blocked
        severity    — LOW / MEDIUM / HIGH / CRITICAL
        status      — MONITORING / BLOCKED / QUARANTINED / PATCHED
        incident_id — Unique incident identifier (always present)
        reason      — Human-readable explanation of the decision
        watcher     — Full Watcher agent output dict
        classifier  — Full Classifier agent output dict
        analyst     — Full Analyst agent output dict (may have skipped=True)
        responder   — Full Responder agent output dict
    """
    allowed: bool
    severity: str
    status: str
    incident_id: str
    reason: str
    watcher: dict
    classifier: dict
    analyst: dict
    responder: dict

    def __str__(self):
        icon = "✅" if self.allowed else "🚫"
        return (
            f"{icon} SentinelSwarm | severity={self.severity} | "
            f"status={self.status} | incident={self.incident_id}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Local hook — runs the full swarm pipeline in-process (no HTTP)
# Use this when SentinelSwarm is deployed alongside your agent code
# ─────────────────────────────────────────────────────────────────────────────

class SentinelHook:
    """
    Synchronous in-process hook — runs all 4 SentinelSwarm agents directly.

    Best for:
    - Applications where SentinelSwarm is installed as a library
    - Local development and testing
    - When you want zero network latency on the security check

    Example:
        hook = SentinelHook()

        # At Hook Point 2 — screen Agent A's output before passing to Agent B
        tool_output = agent_a.run(user_query)
        result = hook.check(tool_output)
        if not result.allowed:
            raise SecurityError(f"Indirect injection detected: {result.incident_id}")
        agent_b.run(tool_output)
    """

    def check(self, content: str) -> HookResult:
        """
        Run the full SentinelSwarm security pipeline on the given content.

        Args:
            content: The text to screen — user input, tool output, or inter-agent message

        Returns:
            HookResult with allowed=True if safe, allowed=False if threat detected
        """
        watcher_out = run_watcher(content)
        classifier_out = run_classifier(watcher_out)
        analyst_out = run_analyst(classifier_out)
        responder_out = run_responder(analyst_out, classifier_out)

        status = responder_out.get("status", "BLOCKED")
        allowed = status == "MONITORING"

        return HookResult(
            allowed=allowed,
            severity=classifier_out.get("severity", "UNKNOWN"),
            status=status,
            incident_id=responder_out.get("incident_id", "UNKNOWN"),
            reason=responder_out.get("threat_brief", "No threat." if allowed else "Threat detected."),
            watcher=watcher_out,
            classifier=classifier_out,
            analyst=analyst_out,
            responder=responder_out,
        )

    def guard(self, content: str, raise_on_block: bool = False) -> HookResult:
        """
        Alias for check() with optional exception raising.

        Args:
            content:        The text to screen
            raise_on_block: If True, raises SecurityBlockedError instead of returning result

        Returns:
            HookResult (only if allowed=True when raise_on_block=True)

        Raises:
            SecurityBlockedError: If the content is blocked and raise_on_block=True
        """
        result = self.check(content)
        if not result.allowed and raise_on_block:
            raise SecurityBlockedError(result)
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Remote hook — calls a deployed SentinelSwarm instance via HTTP
# Use this when SentinelSwarm is deployed as a separate microservice
# ─────────────────────────────────────────────────────────────────────────────

class RemoteSentinelHook:
    """
    HTTP hook — calls a deployed SentinelSwarm /guard endpoint.

    Best for:
    - Microservice architectures where SentinelSwarm is a separate service
    - Polyglot systems (Node.js, Java, etc. calling SentinelSwarm)
    - Production deployments on Azure Container Apps

    Example:
        hook = RemoteSentinelHook("https://sentinelswarm.azurecontainerapps.io")

        # Any agent in any language can call this
        result = hook.check(user_input)
        if not result.allowed:
            return {"error": "blocked", "incident_id": result.incident_id}
    """

    def __init__(self, sentinel_url: str, timeout: int = 30):
        """
        Args:
            sentinel_url: Base URL of the deployed SentinelSwarm instance
                          e.g. "https://sentinelswarm.azurecontainerapps.io"
            timeout:      Request timeout in seconds (default 30)
        """
        self.sentinel_url = sentinel_url.rstrip("/")
        self.timeout = timeout

    def check(self, content: str) -> HookResult:
        """
        Call the remote /guard endpoint to screen the content.

        Args:
            content: The text to screen

        Returns:
            HookResult with the screening decision

        Raises:
            ConnectionError: If the SentinelSwarm service is unreachable
        """
        try:
            import requests
        except ImportError:
            raise ImportError(
                "RemoteSentinelHook requires the 'requests' package. "
                "Install it with: pip install requests"
            )

        try:
            response = requests.post(
                f"{self.sentinel_url}/guard",
                json={"prompt": content},
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()

            return HookResult(
                allowed=data.get("allowed", False),
                severity=data.get("severity", "UNKNOWN"),
                status="MONITORING" if data.get("allowed") else "BLOCKED",
                incident_id=data.get("incident_id", "UNKNOWN"),
                reason=data.get("reason", ""),
                watcher={},
                classifier={},
                analyst={},
                responder={},
            )
        except Exception as exc:
            # Fail-closed: if SentinelSwarm is unreachable, block by default
            # This is the secure default — never allow traffic through on error
            return HookResult(
                allowed=False,
                severity="UNKNOWN",
                status="BLOCKED",
                incident_id="INC-HOOK-ERROR",
                reason=f"SentinelSwarm unreachable — blocked by default. Error: {str(exc)}",
                watcher={},
                classifier={},
                analyst={},
                responder={},
            )

    def guard(self, content: str, raise_on_block: bool = False) -> HookResult:
        result = self.check(content)
        if not result.allowed and raise_on_block:
            raise SecurityBlockedError(result)
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Exception raised when raise_on_block=True and content is blocked
# ─────────────────────────────────────────────────────────────────────────────

class SecurityBlockedError(Exception):
    """
    Raised when SentinelSwarm blocks a request and raise_on_block=True.

    The full HookResult is available on the exception:
        try:
            hook.guard(content, raise_on_block=True)
        except SecurityBlockedError as e:
            log_incident(e.result.incident_id)
            return "Request blocked by security policy."
    """

    def __init__(self, result: HookResult):
        self.result = result
        super().__init__(
            f"SentinelSwarm blocked the request. "
            f"Severity={result.severity} | Incident={result.incident_id} | "
            f"Reason={result.reason}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Decorator — wrap any agent function with automatic security screening
# ─────────────────────────────────────────────────────────────────────────────

_default_hook = SentinelHook()


def sentinel_guard(func: Callable = None, *, hook: SentinelHook = None,
                   prompt_arg: str = "prompt", block_message: str = None):
    """
    Decorator that automatically screens the first argument (or named arg)
    of any agent function through SentinelSwarm before allowing execution.

    Usage:
        @sentinel_guard
        def customer_service_agent(prompt: str) -> str:
            return call_gpt4o(prompt)

        # With custom hook (e.g. remote):
        remote = RemoteSentinelHook("https://mysentinel.azurecontainerapps.io")
        @sentinel_guard(hook=remote)
        def my_agent(prompt: str) -> str:
            ...

    Args:
        func:          The function to wrap (when used as @sentinel_guard)
        hook:          Custom SentinelHook instance (defaults to local in-process hook)
        prompt_arg:    Name of the argument containing the prompt (default "prompt")
        block_message: Custom message returned when blocked (default: incident ID message)
    """
    active_hook = hook or _default_hook

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> Any:
            # Extract the content to screen
            # Try named kwarg first, then first positional arg
            content = kwargs.get(prompt_arg) or (args[0] if args else "")

            result = active_hook.check(str(content))
            print(result)  # audit log visible in Container Apps log stream

            if not result.allowed:
                msg = block_message or (
                    f"⛔ Request blocked by SentinelSwarm. "
                    f"Severity: {result.severity} | Incident: {result.incident_id}"
                )
                return msg

            # Content cleared — execute the original agent function
            return fn(*args, **kwargs)

        return wrapper

    # Support both @sentinel_guard and @sentinel_guard(...)
    if func is not None:
        return decorator(func)
    return decorator
