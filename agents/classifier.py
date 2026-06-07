"""
Classifier Agent — Severity Scorer in the SentinelSwarm.

Takes the Watcher's output dict and calls Azure AI Content Safety to get
an objective, API-backed severity score.

TWO-API STRATEGY:
  1. shield_prompt()  — Azure AI Content Safety's dedicated prompt injection and
                        jailbreak detection endpoint. Returns True/False for:
                          - user_prompt_attack_detected  (direct injection / jailbreak)
                          - documents_attack_detected    (indirect injection in documents)
                        This is the CORRECT API for detecting AI-specific attacks.
                        Reference: https://learn.microsoft.com/azure/ai-services/content-safety/concepts/jailbreak-detection

  2. analyze_text()   — General content moderation (hate/violence/sexual/self-harm).
                        Used as a secondary signal for social engineering or
                        threatening content that accompanies an attack.

Combines both signals with the Watcher's preliminary risk to produce:
    LOW | MEDIUM | HIGH | CRITICAL

Also sets `escalate_to_analyst` = True for HIGH and CRITICAL threats.
"""

from azure.ai.contentsafety import ContentSafetyClient
from azure.ai.contentsafety.models import AnalyzeTextOptions
from azure.core.credentials import AzureKeyCredential

# ShieldPromptOptions was added in azure-ai-contentsafety 1.0.0
# Import conditionally so older SDK versions fall back gracefully
try:
    from azure.ai.contentsafety.models import ShieldPromptOptions
    _SHIELD_AVAILABLE = True
except ImportError:
    ShieldPromptOptions = None
    _SHIELD_AVAILABLE = False
from config.settings import CONTENT_SAFETY_KEY, CONTENT_SAFETY_ENDPOINT


# ---------------------------------------------------------------------------
# Agent function
# ---------------------------------------------------------------------------
def run_classifier(watcher_output: dict) -> dict:
    """
    Score the threat identified by the Watcher using Azure AI Content Safety.

    Uses shield_prompt() for injection/jailbreak detection and analyze_text()
    for general harm scoring. Combines both with Watcher's preliminary risk
    to produce a final severity level.

    Args:
        watcher_output: The dict returned by run_watcher().

    Returns:
        A dict containing severity, scores, escalation flag, and detection detail.
    """
    input_text = watcher_output.get("input_received", "")

    # ------------------------------------------------------------------
    # Initialise Content Safety client
    # ------------------------------------------------------------------
    cs_client = ContentSafetyClient(
        endpoint=CONTENT_SAFETY_ENDPOINT,
        credential=AzureKeyCredential(CONTENT_SAFETY_KEY),
    )

    # ------------------------------------------------------------------
    # API Call 1: shield_prompt() — injection & jailbreak detection
    # This is the primary signal for AI-specific attacks.
    # Returns boolean flags, not a numeric score.
    # ------------------------------------------------------------------
    prompt_attack_detected = False
    documents_attack_detected = False
    shield_available = False

    if _SHIELD_AVAILABLE:
        try:
            shield_response = cs_client.shield_prompt(
                ShieldPromptOptions(
                    user_prompt=input_text[:10000],  # API limit
                    documents=[],
                )
            )
            prompt_attack_detected = (
                shield_response.user_prompt_attack_detected or False
            )
            documents_attack_detected = (
                shield_response.documents_attack_detected or False
            )
            shield_available = True
        except Exception:
            shield_available = False

    # ------------------------------------------------------------------
    # API Call 2: analyze_text() — general harm scoring (secondary signal)
    # Scores Hate / Violence / SelfHarm / Sexual on 0–7 scale.
    # Prompt injection attacks typically score 0 here — that is expected.
    # This catches social engineering with threatening/coercive language.
    # ------------------------------------------------------------------
    harm_max_score = 0
    harm_scores: dict = {}

    try:
        harm_response = cs_client.analyze_text(
            AnalyzeTextOptions(text=input_text[:1000])  # API char limit
        )
        harm_scores = {
            item.category: item.severity
            for item in (harm_response.categories_analysis or [])
        }
        harm_max_score = max(harm_scores.values(), default=0)
    except Exception:
        # Fallback: derive numeric proxy from Watcher's preliminary risk
        harm_max_score = {"low": 0, "medium": 2, "high": 4}.get(
            watcher_output.get("preliminary_risk", "low"), 0
        )

    # ------------------------------------------------------------------
    # Combine all signals → final severity
    #
    # Priority order:
    #   1. shield_prompt() attack detected           → minimum HIGH
    #   2. Watcher rule-based high + suspicious      → minimum HIGH
    #   3. harm_max_score >= 4                       → CRITICAL
    #   4. harm_max_score >= 2 or Watcher medium     → HIGH
    #   5. Watcher suspicious or harm >= 1           → MEDIUM
    #   6. No signals                                → LOW
    # ------------------------------------------------------------------
    watcher_risk = watcher_output.get("preliminary_risk", "low")
    is_suspicious = watcher_output.get("suspicious", False)
    detection_layer = watcher_output.get("detection_layer", "llm")

    # Shield detected an attack → guaranteed HIGH at minimum
    shield_hit = prompt_attack_detected or documents_attack_detected

    if shield_hit and harm_max_score >= 4:
        severity = "CRITICAL"
    elif shield_hit or (detection_layer == "rule_based" and is_suspicious):
        # Shield fired OR rule-based watcher hit → HIGH
        severity = "HIGH"
        # Upgrade to CRITICAL if Watcher also said high risk
        if watcher_risk == "high":
            severity = "CRITICAL"
    elif harm_max_score >= 4 or (is_suspicious and watcher_risk == "high"):
        severity = "CRITICAL"
    elif harm_max_score >= 2 or (is_suspicious and watcher_risk == "medium"):
        severity = "HIGH"
    elif is_suspicious or harm_max_score >= 1:
        severity = "MEDIUM"
    else:
        severity = "LOW"

    return {
        "agent": "Classifier",
        "severity": severity,
        # Shield prompt results (primary signal for AI attacks)
        "shield_prompt_attack_detected": prompt_attack_detected,
        "shield_documents_attack_detected": documents_attack_detected,
        "shield_available": shield_available,
        # General harm results (secondary signal)
        "content_safety_max_score": harm_max_score,
        "content_safety_scores": harm_scores,
        # Watcher passthrough
        "watcher_preliminary": watcher_risk,
        "watcher_detection_layer": detection_layer,
        "patterns_from_watcher": watcher_output.get("patterns_detected", []),
        # Escalation gate
        "escalate_to_analyst": severity in ("HIGH", "CRITICAL"),
        "input": input_text,
    }
