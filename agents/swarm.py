"""
SentinelSwarm — AutoGen 0.4 Orchestrator

Implements genuine Microsoft AutoGen multi-agent communication.
Each of the 4 specialist agents is an AutoGen AssistantAgent with its
existing function registered as a tool. They run via RoundRobinGroupChat
so each agent reads the previous agent's JSON output from the shared
AutoGen message thread and calls its own tool in turn.

Communication flow (via AutoGen message bus):
    Watcher  ──[JSON message]──▶  Classifier
    Classifier ──[JSON message]──▶  Analyst
    Analyst  ──[JSON message]──▶  Responder
    Responder ──[incident report]──▶  SWARM_COMPLETE

Replacing: direct Python dict passing in api/main.py
With: AutoGen RoundRobinGroupChat message-passing orchestration
"""

import json
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_agentchat.conditions import MaxMessageTermination, TextMentionTermination
from autogen_ext.models.openai import AzureOpenAIChatCompletionClient

from agents.watcher import run_watcher
from agents.classifier import run_classifier
from agents.analyst import run_analyst
from agents.responder import run_responder
from config.settings import (
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_ENDPOINT,
    AZURE_OPENAI_DEPLOYMENT,
    AZURE_OPENAI_API_VERSION,
)

# ---------------------------------------------------------------------------
# Module-level model client singleton
# Created once at import time — not per request. Avoids repeated TLS handshakes
# and connection setup overhead on every /analyze call.
# ---------------------------------------------------------------------------
_MODEL_CLIENT = AzureOpenAIChatCompletionClient(
    azure_deployment=AZURE_OPENAI_DEPLOYMENT,
    model="gpt-4o",
    api_key=AZURE_OPENAI_API_KEY,
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
    api_version=AZURE_OPENAI_API_VERSION,
)

# Thread pool for running synchronous agent functions from async context
_EXECUTOR = ThreadPoolExecutor(max_workers=4)

# Per-agent timeout in seconds — prevents the swarm from hanging on a slow API call
AGENT_TIMEOUT_SECONDS = 15.0


async def run_autogen_swarm(user_input: str) -> dict[str, Any]:
    """
    Run the full 4-agent swarm via AutoGen RoundRobinGroupChat.

    Each agent is an AssistantAgent with one registered tool (its specialist
    function). The AutoGen message bus passes each agent's JSON output to the
    next agent in the chain. Side-effect capture collects structured results.

    Args:
        user_input: Raw text submitted by the end user.

    Returns:
        dict with keys: watcher, classifier, analyst, responder —
        each containing the structured output from that agent.
    """

    # Shared results dict — populated as side effects when each tool runs.
    # This is how we extract structured outputs from the AutoGen message stream.
    pipeline_results: dict[str, Any] = {
        "watcher": {},
        "classifier": {},
        "analyst": {},
        "responder": {},
    }

    # ------------------------------------------------------------------
    # Tool wrappers
    # Each wraps an existing agent function, captures its result, and
    # returns a JSON string for the AutoGen message thread.
    # Defined here (not at module level) so they close over pipeline_results.
    # ------------------------------------------------------------------

    loop = asyncio.get_event_loop()

    async def _run_with_timeout(fn, *args, label: str) -> Any:
        """Run a synchronous agent function in a thread with a timeout."""
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(_EXECUTOR, fn, *args),
                timeout=AGENT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            print(f"[SWARM] ⚠️  {label} timed out after {AGENT_TIMEOUT_SECONDS}s — using fallback")
            return None

    def watcher_scan(user_input: str) -> str:
        """
        Scan input for prompt injection, jailbreak, data extraction, and encoding attacks.
        Layer 1: rule-based pre-check (instant, zero API cost).
        Layer 2: GPT-4o semantic analysis (catches subtle attacks).
        Returns a JSON string with: agent, suspicious, preliminary_risk,
        patterns_detected, reason, detection_layer.
        """
        result = run_watcher(user_input)
        pipeline_results["watcher"] = result
        return json.dumps(result)

    def classify_threat(watcher_json: str) -> str:
        """
        Score threat severity using Azure AI Content Safety.
        Primary: shield_prompt() — dedicated injection/jailbreak detection.
        Secondary: analyze_text() — general harm scoring.
        Input: the complete JSON string returned by watcher_scan.
        Returns a JSON string with: agent, severity (LOW/MEDIUM/HIGH/CRITICAL),
        shield_prompt_attack_detected, escalate_to_analyst.
        """
        result = run_classifier(json.loads(watcher_json))
        pipeline_results["classifier"] = result
        return json.dumps(result)

    def analyze_threat(classifier_json: str) -> str:
        """
        Deep GPT-4o threat analysis — attack type, OWASP category, intent,
        blast radius, recommended action, and concrete system prompt patch.
        Input: the complete JSON string returned by classify_threat.
        Returns a JSON string with: agent, attack_type, owasp_code, owasp_name,
        blast_radius, recommended_action, system_prompt_patch, brief.
        """
        result = run_analyst(json.loads(classifier_json))
        pipeline_results["analyst"] = result
        return json.dumps(result)

    def respond_to_threat(analyst_json: str, classifier_json: str) -> str:
        """
        Execute block/quarantine/patch/monitor action and generate incident report.
        Incident ID is collision-resistant (timestamp + UUID hex suffix).
        Includes OWASP category, concrete patch text, and shield detection signal.
        Input: JSON strings from analyze_threat AND classify_threat.
        Returns a JSON string with the full timestamped incident report.
        """
        result = run_responder(
            json.loads(analyst_json),
            json.loads(classifier_json),
        )
        pipeline_results["responder"] = result
        return json.dumps(result)

    # ------------------------------------------------------------------
    # Build the 4-agent AutoGen team (uses module-level singleton client)
    # ------------------------------------------------------------------
    watcher_agent = AssistantAgent(
        name="Watcher",
        model_client=_MODEL_CLIENT,
        tools=[watcher_scan],
        system_message=(
            "You are the Watcher security agent — the first line of defence. "
            "When you receive a message to analyse, call watcher_scan with the exact "
            "raw user input text as the argument. "
            "After the tool executes, output ONLY the JSON string it returned — no extra text."
        ),
    )

    classifier_agent = AssistantAgent(
        name="Classifier",
        model_client=_MODEL_CLIENT,
        tools=[classify_threat],
        system_message=(
            "You are the Classifier security agent. "
            "In the conversation history, find the JSON output from the Watcher agent. "
            "Call classify_threat passing that complete Watcher JSON string as the argument. "
            "After the tool executes, output ONLY the JSON string it returned — no extra text."
        ),
    )

    analyst_agent = AssistantAgent(
        name="Analyst",
        model_client=_MODEL_CLIENT,
        tools=[analyze_threat],
        system_message=(
            "You are the Analyst security agent. "
            "In the conversation history, find the JSON output from the Classifier agent. "
            "Call analyze_threat passing that complete Classifier JSON string as the argument. "
            "After the tool executes, output ONLY the JSON string it returned — no extra text."
        ),
    )

    responder_agent = AssistantAgent(
        name="Responder",
        model_client=_MODEL_CLIENT,
        tools=[respond_to_threat],
        system_message=(
            "You are the Responder security agent — the action taker and incident closer. "
            "In the conversation history, find the Analyst JSON output and the Classifier JSON output. "
            "Call respond_to_threat passing BOTH: the Analyst JSON as analyst_json, "
            "and the Classifier JSON as classifier_json. "
            "After the tool executes, output ONLY the JSON string it returned, "
            "then on a new line say exactly: SWARM_COMPLETE"
        ),
    )

    # Stop when Responder says SWARM_COMPLETE, or after 16 messages as a safety ceiling
    termination = (
        TextMentionTermination("SWARM_COMPLETE")
        | MaxMessageTermination(max_messages=16)
    )

    team = RoundRobinGroupChat(
        participants=[watcher_agent, classifier_agent, analyst_agent, responder_agent],
        termination_condition=termination,
    )

    # ------------------------------------------------------------------
    # Run the swarm — agents communicate via AutoGen's message bus
    # Results are captured in pipeline_results as each tool executes
    # ------------------------------------------------------------------
    print(f"\n[AUTOGEN] ── Swarm started (RoundRobinGroupChat) ──────────────")
    await team.run(task=f"Analyse this input for security threats: {user_input}")
    print(f"[AUTOGEN] ── Swarm complete ──────────────────────────────────\n")

    return pipeline_results
