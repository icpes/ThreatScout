"""
Analyst Agent — Deep Thinker in the SentinelSwarm.

Only activated for HIGH or CRITICAL threats (escalate_to_analyst = True).
Uses Azure OpenAI GPT-4o to reason about:
  - What type of attack this is (expanded taxonomy from NVIDIA Garak + OWASP LLM Top 10)
  - What the attacker was trying to achieve (intent)
  - How bad it could have been (blast radius)
  - OWASP LLM Top 10 2025 category mapping
  - What concrete system prompt patch would close this vector
  - What the Responder should do next (recommended_action)

Produces a structured threat brief consumable by both the Responder agent
and a human security operator.

References:
  - OWASP LLM Top 10 2025: https://genai.owasp.org/llm-top-10/
  - NVIDIA Garak attack taxonomy: https://github.com/NVIDIA/garak
"""

from openai import AzureOpenAI
from config.settings import (
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_ENDPOINT,
    AZURE_OPENAI_DEPLOYMENT,
    AZURE_OPENAI_API_VERSION,
)
import json

# ---------------------------------------------------------------------------
# Azure OpenAI client — module-level singleton
# ---------------------------------------------------------------------------
client = AzureOpenAI(
    api_key=AZURE_OPENAI_API_KEY,
    api_version=AZURE_OPENAI_API_VERSION,
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
)

# ---------------------------------------------------------------------------
# OWASP LLM Top 10 2025 reference map
# Used to add compliance-ready category labels to incident reports
# ---------------------------------------------------------------------------
OWASP_LLM_TOP10 = {
    "prompt_injection":      ("LLM01", "Prompt Injection"),
    "jailbreak":             ("LLM01", "Prompt Injection"),
    "indirect_injection":    ("LLM01", "Prompt Injection"),
    "data_extraction":       ("LLM02", "Sensitive Information Disclosure"),
    "system_prompt_exfil":   ("LLM02", "Sensitive Information Disclosure"),
    "encoding_attack":       ("LLM01", "Prompt Injection"),
    "homoglyph_encoding":    ("LLM01", "Prompt Injection"),
    "social_engineering":    ("LLM02", "Sensitive Information Disclosure"),
    "credential_fishing":    ("LLM02", "Sensitive Information Disclosure"),
    "excessive_agency":      ("LLM06", "Excessive Agency"),
    "adversarial_suffix":    ("LLM01", "Prompt Injection"),
    "package_hallucination": ("LLM09", "Misinformation"),
    "unknown":               ("LLM01", "Prompt Injection"),  # safe default
}

# ---------------------------------------------------------------------------
# System prompt — expanded taxonomy, OWASP mapping, and patch generation
# ---------------------------------------------------------------------------
ANALYST_SYSTEM_PROMPT = """
You are a senior security analyst agent called Analyst.
You receive a classified threat and must reason about it deeply.

Use the expanded attack taxonomy (informed by NVIDIA Garak and OWASP LLM Top 10 2025):
  attack_type options: prompt_injection | jailbreak | indirect_injection | data_extraction |
                       system_prompt_exfil | encoding_attack | social_engineering |
                       credential_fishing | adversarial_suffix | multi_turn_escalation | unknown

OWASP LLM Top 10 2025 categories (map to the most relevant):
  LLM01 — Prompt Injection
  LLM02 — Sensitive Information Disclosure
  LLM06 — Excessive Agency
  LLM07 — System Prompt Leakage
  LLM09 — Misinformation

Respond ONLY with a valid JSON object — no markdown, no explanation, no extra text.
Use this exact schema:
{
  "agent": "Analyst",
  "attack_type": "<one of the attack_type options above>",
  "owasp_code": "<e.g. LLM01>",
  "owasp_name": "<e.g. Prompt Injection>",
  "intent": "<one sentence: what the attacker was specifically trying to achieve>",
  "blast_radius": "contained" | "moderate" | "severe",
  "blast_radius_explanation": "<one sentence: what could have been exposed or compromised>",
  "confidence": "low" | "medium" | "high",
  "recommended_action": "block" | "quarantine" | "patch_system_prompt" | "monitor",
  "system_prompt_patch": "<concrete text to ADD to the target agent's system prompt to close this vector, or null if not applicable>",
  "brief": "<2-3 sentence plain English summary for a security operator>"
}
"""


# ---------------------------------------------------------------------------
# Agent function
# ---------------------------------------------------------------------------
def run_analyst(classifier_output: dict) -> dict:
    """
    Perform deep threat analysis on a HIGH or CRITICAL classified threat.

    Args:
        classifier_output: The dict returned by run_classifier().

    Returns:
        A dict containing the threat brief, OWASP category, recommended action,
        and a concrete system prompt patch suggestion.
        Returns a lightweight skip dict for LOW/MEDIUM threats.
    """
    # Only run deep analysis when the Classifier escalates
    if not classifier_output.get("escalate_to_analyst", False):
        return {
            "agent": "Analyst",
            "skipped": True,
            "reason": "Threat severity too low for deep analysis.",
            "attack_type": "unknown",
            "owasp_code": None,
            "owasp_name": None,
            "recommended_action": "monitor",
            "system_prompt_patch": None,
            "brief": (
                "Threat assessed as low risk by the Classifier. "
                "No deep analysis required. Enhanced monitoring activated."
            ),
        }

    # Build threat context for the LLM — include all available signals
    threat_context = (
        f"Threat Input: {classifier_output.get('input', '')}\n"
        f"Severity: {classifier_output.get('severity', 'UNKNOWN')}\n"
        f"Detected Patterns: {', '.join(classifier_output.get('patterns_from_watcher', []))}\n"
        f"Content Safety Score: {classifier_output.get('content_safety_max_score', 0)}\n"
        f"Shield Prompt Attack Detected: {classifier_output.get('shield_prompt_attack_detected', False)}\n"
        f"Watcher Detection Layer: {classifier_output.get('watcher_detection_layer', 'llm')}"
    )

    response = client.chat.completions.create(
        model=AZURE_OPENAI_DEPLOYMENT,
        messages=[
            {"role": "system", "content": ANALYST_SYSTEM_PROMPT},
            {"role": "user", "content": f"Analyse this threat:\n{threat_context}"},
        ],
        temperature=0.2,
        max_tokens=700,
    )

    raw = response.choices[0].message.content.strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        result = json.loads(raw)

        # If model didn't provide OWASP fields, derive them from attack_type
        if not result.get("owasp_code"):
            attack_type = result.get("attack_type", "unknown")
            owasp_code, owasp_name = OWASP_LLM_TOP10.get(
                attack_type, OWASP_LLM_TOP10["unknown"]
            )
            result["owasp_code"] = owasp_code
            result["owasp_name"] = owasp_name

        # Ensure system_prompt_patch key always exists
        result.setdefault("system_prompt_patch", None)
        return result

    except json.JSONDecodeError:
        return {
            "agent": "Analyst",
            "attack_type": "unknown",
            "owasp_code": "LLM01",
            "owasp_name": "Prompt Injection",
            "intent": "Could not determine intent — model response was unparseable.",
            "blast_radius": "moderate",
            "blast_radius_explanation": "Unable to assess blast radius due to parse error.",
            "confidence": "low",
            "recommended_action": "block",
            "system_prompt_patch": None,
            "brief": raw[:300] if raw else "Analyst produced no output.",
        }
