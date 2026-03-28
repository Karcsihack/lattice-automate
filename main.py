"""
╔══════════════════════════════════════════════════════════════╗
║          LATTICE-AUTOMATE  |  AI Agent Framework             ║
║          Business Guardrails + Privacy Proxy Integration     ║
╚══════════════════════════════════════════════════════════════╝

Architecture:
  [User] → [LatticeAgent] → [Lattice Proxy :8080] → [LLM]
                 ↓
          [Pydantic Schema]  ← Validates JSON structure
                 ↓
          [PolicyEngine]     ← Validates business rules
                 ↓
          [Safe response to user]
"""

import json
import os
import sys
import logging
from pathlib import Path
from typing import Optional

import httpx
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, ValidationError

# ─────────────────────────────────────────────────────────────
# Business rules file path
# ─────────────────────────────────────────────────────────────
_RULES_FILE = Path(__file__).parent / "business_rules.yaml"

# ─────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────
load_dotenv()

LATTICE_URL: str = os.getenv("LATTICE_URL", "http://localhost:8080/v1/chat/completions")
LLM_MODEL: str = os.getenv("LLM_MODEL", "mistral")
LLM_TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", "0.1"))
MAX_HISTORY_CHARS: int = int(os.getenv("MAX_HISTORY_CHARS", "16000"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("lattice-automate")


# ─────────────────────────────────────────────────────────────
# Pydantic Schemas (100% Structured Output)
# ─────────────────────────────────────────────────────────────

class InsuranceQuoteResponse(BaseModel):
    """
    Response schema for insurance quotes.
    The LLM can ONLY return this exact format — no exceptions.
    """

    explanation: str = Field(
        ...,
        min_length=10,
        description="Detailed explanation of the premium calculation",
    )
    approved: bool = Field(
        ...,
        description="Whether the quote has been approved by the system",
    )
    final_value: float = Field(
        ...,
        ge=0.0,
        description="Final premium in EUR",
    )
    discount_applied: float = Field(
        default=0.0,
        ge=0.0,
        le=100.0,
        description="Discount percentage applied (0-100)",
    )
    risk_level: str = Field(
        ...,
        description="Calculated risk level: LOW | MEDIUM | HIGH",
    )

    @field_validator("risk_level")
    @classmethod
    def validate_risk_level(cls, value: str) -> str:
        allowed = {"LOW", "MEDIUM", "HIGH"}
        normalized = value.strip().upper()
        if normalized not in allowed:
            raise ValueError(
                f"Invalid risk_level: '{value}'. Must be one of {allowed}"
            )
        return normalized

    def to_display(self) -> str:
        """Formats the response for display to the user."""
        status = "✅ APPROVED" if self.approved else "❌ NOT APPROVED"
        return (
            f"\n  Status:          {status}\n"
            f"  Risk Level:      {self.risk_level}\n"
            f"  Final Premium:   {self.final_value:,.2f} EUR\n"
            f"  Discount:        {self.discount_applied:.1f}%\n"
            f"  Explanation:     {self.explanation}\n"
        )


# ─────────────────────────────────────────────────────────────
# Security Exceptions
# ─────────────────────────────────────────────────────────────

class PolicyViolationError(Exception):
    """
    Raised when the AI response violates a business rule.
    The message is logged but NEVER shown directly to the user.
    """


class LatticeConnectionError(Exception):
    """Raised when the Lattice Proxy is unreachable."""


# ─────────────────────────────────────────────────────────────
# Business Rules Engine — PolicyEngine
# ─────────────────────────────────────────────────────────────

class PolicyEngine:
    """
    Deterministic AI response validator.

    Loads rules from 'business_rules.yaml' at startup.
    Acts as a firewall between LLM output and the user:
      · validate_request() — blocks BEFORE calling the LLM (age, region)
      · validate()         — blocks AFTER (discount, premium, risk)

    This is the component that makes Lattice-Automate "Constitutional AI"
    — AI with a business constitution it cannot override.
    """

    # Fallbacks when business_rules.yaml is not found
    _DEFAULTS: dict = {
        "policies": {
            "max_discount": 0.15,
            "high_risk_max_discount": 0.05,
            "max_premium_eur": 50000.0,
            "min_premium_eur": 50.0,
            "min_age_insured": 18,
            "restricted_regions": [],
            "required_fields": ["SSN", "EMAIL", "PHONE"],
        }
    }

    def __init__(self) -> None:
        self._rules = self._load_rules()
        p = self._rules["policies"]

        self.MAX_DISCOUNT_PCT: float = p["max_discount"] * 100          # 0.15 → 15.0
        self.HIGH_RISK_MAX_DISCOUNT_PCT: float = p["high_risk_max_discount"] * 100
        self.MAX_PREMIUM_EUR: float = p["max_premium_eur"]
        self.MIN_PREMIUM_EUR: float = p["min_premium_eur"]
        self.MIN_AGE: int = p["min_age_insured"]
        self.RESTRICTED_REGIONS: set[str] = {r.upper() for r in p["restricted_regions"]}

        logger.info(
            "[PolicyEngine] Rules loaded — max_discount=%.0f%% | premium=[%.0f–%.0f EUR] | "
            "min_age=%d | blocked_regions=%d",
            self.MAX_DISCOUNT_PCT, self.MIN_PREMIUM_EUR, self.MAX_PREMIUM_EUR,
            self.MIN_AGE, len(self.RESTRICTED_REGIONS),
        )

    def _load_rules(self) -> dict:
        """Loads business_rules.yaml. Falls back to defaults if not found."""
        if _RULES_FILE.exists():
            with open(_RULES_FILE, encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh)
            logger.info("[PolicyEngine] Rules loaded from %s", _RULES_FILE.name)
            return loaded
        logger.warning(
            "[PolicyEngine] %s not found. Using built-in defaults.", _RULES_FILE.name
        )
        return self._DEFAULTS

    # ── PRE-LLM validation (user request) ────────────────────

    def validate_request(self, age: int | None = None, region: str | None = None) -> None:
        """
        Validates user data BEFORE calling the LLM.
        If there is a problem (underage, blocked region), the request is
        rejected without spending a single token.
        """
        violations: list[str] = []

        # RULE 101 — Minimum insurable age
        if age is not None and age < self.MIN_AGE:
            violations.append(
                f"POLICY_101: Age {age} is below the minimum insurable age ({self.MIN_AGE})"
            )

        # RULE 102 — Non-insurable regions
        if region is not None and region.upper() in self.RESTRICTED_REGIONS:
            violations.append(
                f"POLICY_102: Region '{region}' is in the blocked zone list: "
                f"{sorted(self.RESTRICTED_REGIONS)}"
            )

        if violations:
            detail = "\n".join(f"  • {v}" for v in violations)
            raise PolicyViolationError(
                f"[PolicyEngine] Request blocked (pre-LLM):\n{detail}"
            )

    # ── POST-LLM validation (AI response) ────────────────────

    def validate(self, response: InsuranceQuoteResponse) -> None:
        """
        Runs all business rules against the LLM response.
        Raises PolicyViolationError if any rule fails.
        """
        violations: list[str] = []

        # RULE 001 — Maximum discount allowed
        if response.discount_applied > self.MAX_DISCOUNT_PCT:
            violations.append(
                f"POLICY_001: Discount {response.discount_applied:.1f}% "
                f"exceeds the authorized limit of {self.MAX_DISCOUNT_PCT:.1f}%"
            )

        # RULE 002 — Maximum quotable premium
        if response.final_value > self.MAX_PREMIUM_EUR:
            violations.append(
                f"POLICY_002: Premium {response.final_value:,.2f} EUR "
                f"exceeds the maximum quotable value of {self.MAX_PREMIUM_EUR:,.2f} EUR"
            )

        # RULE 003 — Minimum premium (only if approved)
        if response.approved and response.final_value < self.MIN_PREMIUM_EUR:
            violations.append(
                f"POLICY_003: Premium {response.final_value:.2f} EUR "
                f"is below the minimum allowed of {self.MIN_PREMIUM_EUR:.2f} EUR"
            )

        # RULE 004 — HIGH risk with excessive discount
        if (
            response.approved
            and response.risk_level == "HIGH"
            and response.discount_applied > self.HIGH_RISK_MAX_DISCOUNT_PCT
        ):
            violations.append(
                f"POLICY_004: HIGH risk policy — maximum discount allowed is "
                f"{self.HIGH_RISK_MAX_DISCOUNT_PCT:.0f}%, "
                f"requested {response.discount_applied:.1f}%"
            )

        if violations:
            detail = "\n".join(f"  • {v}" for v in violations)
            raise PolicyViolationError(
                f"[PolicyEngine] {len(violations)} policy violation(s) detected:\n{detail}"
            )

        logger.info("[PolicyEngine] ✓ Response validated. No violations.")


# ─────────────────────────────────────────────────────────────
# Main Agent — LatticeAgent
# ─────────────────────────────────────────────────────────────

def _build_system_prompt(engine: "PolicyEngine") -> str:
    """Generates the system prompt dynamically from the rules loaded in PolicyEngine."""
    return (
        "You are a specialized insurance agent for a regulated insurance company.\n\n"
        "Your ONLY function is to calculate insurance premiums following the business rules below.\n"
        "You are subject to a strict audit system. Any response outside the required format\n"
        "or that violates the rules will be automatically rejected.\n\n"
        "═══ BUSINESS RULES (loaded from business_rules.yaml) ════════\n"
        f"  • Minimum premium:              {engine.MIN_PREMIUM_EUR:,.0f} EUR\n"
        f"  • Maximum premium:              {engine.MAX_PREMIUM_EUR:,.0f} EUR\n"
        f"  • Maximum discount:             {engine.MAX_DISCOUNT_PCT:.0f}%\n"
        f"  • Discount on HIGH risk:        maximum {engine.HIGH_RISK_MAX_DISCOUNT_PCT:.0f}%\n"
        "  • Valid risk levels:            LOW | MEDIUM | HIGH\n"
        "══════════════════════════════════════════════════════════════\n\n"
        "MANDATORY RESPONSE FORMAT (JSON only, no additional text):\n"
        "{\n"
        '    "explanation": "<clear explanation of the calculation, minimum 10 characters>",\n'
        '    "approved": <true or false>,\n'
        '    "final_value": <premium in EUR as a decimal number>,\n'
        '    "discount_applied": <percentage between 0.0 and 100.0>,\n'
        '    "risk_level": "<LOW | MEDIUM | HIGH>"\n'
        "}\n\n"
        "CRITICAL: Do not include any text before or after the JSON. Output the JSON object only."
    )


class LatticeAgent:
    """
    AI agent with business guardrails.

    Full pipeline:
      1. Builds messages with conversation history
      2. Routes the LLM call through the Lattice Proxy (privacy)
      3. Parses the response with Pydantic (guaranteed structure)
      4. Validates with PolicyEngine (business rules)
      5. Persists history only if all validations pass
    """

    def __init__(self) -> None:
        self.policy_engine = PolicyEngine()
        self._system_prompt = _build_system_prompt(self.policy_engine)
        self.conversation_history: list[dict[str, str]] = []

    # ── Context construction ──────────────────────────────────

    def _build_messages(self, user_message: str) -> list[dict[str, str]]:
        """
        Builds the message list respecting the Lattice token vault.
        Includes recent history up to MAX_HISTORY_CHARS characters.
        """
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._system_prompt}
        ]

        # Include history back-to-front respecting the token limit
        accumulated_chars = len(self._system_prompt)
        history_slice: list[dict[str, str]] = []

        for msg in reversed(self.conversation_history):
            msg_len = len(msg["content"])
            if accumulated_chars + msg_len > MAX_HISTORY_CHARS:
                logger.debug("[LatticeAgent] History truncated at token limit.")
                break
            history_slice.insert(0, msg)
            accumulated_chars += msg_len

        messages.extend(history_slice)
        messages.append({"role": "user", "content": user_message})
        return messages

    # ── Lattice proxy call ────────────────────────────────────

    def _call_lattice(self, messages: list[dict[str, str]]) -> str:
        """
        Sends the request through the Lattice Privacy Proxy.
        Lattice anonymizes personal data BEFORE it reaches the LLM.
        """
        payload = {
            "model": LLM_MODEL,
            "messages": messages,
            "temperature": LLM_TEMPERATURE,
            "stream": False,
        }

        logger.info("[LatticeAgent] Sending request to %s (model: %s)", LATTICE_URL, LLM_MODEL)

        try:
            with httpx.Client(timeout=60.0) as client:
                response = client.post(LATTICE_URL, json=payload)
                response.raise_for_status()
        except httpx.ConnectError:
            raise LatticeConnectionError(
                f"Cannot connect to Lattice Proxy at {LATTICE_URL}. "
                "Make sure lattice-proxy is running (port 8080)."
            )
        except httpx.HTTPStatusError as exc:
            raise LatticeConnectionError(
                f"Lattice Proxy returned HTTP error {exc.response.status_code}: {exc.response.text}"
            )

        data = response.json()
        raw_content: str = data["choices"][0]["message"]["content"]
        logger.debug("[LatticeAgent] Raw response received (%d chars)", len(raw_content))
        return raw_content

    # ── Structural parsing and validation ────────────────────

    def _parse_response(self, raw: str) -> InsuranceQuoteResponse:
        """
        Extracts and validates the JSON from the LLM response using Pydantic.
        If the AI included extra text, it is stripped. If the JSON is invalid,
        an exception is raised before reaching the PolicyEngine.
        """
        raw = raw.strip()

        # Extract the JSON block even if there is surrounding text
        start = raw.find("{")
        end = raw.rfind("}") + 1

        if start == -1 or end == 0:
            raise ValueError(
                f"The AI did not return valid JSON. Response received:\n{raw[:300]}"
            )

        json_str = raw[start:end]

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed JSON in AI response: {exc}") from exc

        try:
            return InsuranceQuoteResponse(**data)
        except ValidationError as exc:
            raise ValueError(
                f"AI response does not match the Pydantic schema:\n{exc}"
            ) from exc

    # ── Main pipeline ─────────────────────────────────────────

    def process(
        self,
        user_message: str,
        age: int | None = None,
        region: str | None = None,
    ) -> InsuranceQuoteResponse:
        """
        Executes the full pipeline in a safe and deterministic way.

        Args:
            user_message: Natural language query from the user.
            age:    Applicant's age (validated pre-LLM against business_rules.yaml).
            region: Geographic area (validated pre-LLM against restricted_regions).

        Returns:
            InsuranceQuoteResponse validated and approved by PolicyEngine.

        Raises:
            PolicyViolationError: if the AI violates business rules OR if the
                                  client does not meet pre-conditions (age, region).
            LatticeConnectionError: if the Lattice Proxy is not available.
            ValueError: if the AI response has an invalid format.
        """
        # 0. PRE-LLM validation — no tokens spent
        self.policy_engine.validate_request(age=age, region=region)

        messages = self._build_messages(user_message)

        # 1. LLM call through Lattice (privacy guaranteed)
        raw_response = self._call_lattice(messages)

        # 2. Structured parsing with Pydantic
        quote = self._parse_response(raw_response)

        # 3. Business rule validation — may raise PolicyViolationError
        self.policy_engine.validate(quote)

        # 4. Persist history ONLY if everything is valid
        self.conversation_history.append({"role": "user", "content": user_message})
        self.conversation_history.append({"role": "assistant", "content": raw_response})

        return quote

    def reset_history(self) -> None:
        """Clears the conversation history."""
        self.conversation_history.clear()
        logger.info("[LatticeAgent] Conversation history reset.")


# ─────────────────────────────────────────────────────────────
# Demo — Insurance Use Case
# ─────────────────────────────────────────────────────────────

def run_demo() -> None:
    """
    Full system demonstration.

    Test cases:
      ✓ Normal quote → approved
      ✗ Underage applicant → blocked PRE-LLM (no tokens spent)
      ✗ Restricted region → blocked PRE-LLM (no tokens spent)
      ✗ Excessive discount → blocked POST-LLM by PolicyEngine
      ✓ Follow-up query → uses conversation history
    """
    separator = "═" * 65

    print(f"\n{separator}")
    print("  LATTICE-AUTOMATE  |  AI Agent with Business Guardrails")
    print(f"  Privacy proxy: {LATTICE_URL}")
    print(f"  LLM model:     {LLM_MODEL}")
    print(separator)

    agent = LatticeAgent()

    # (message, age, region) — last two are optional
    test_cases: list[tuple[str, int | None, str | None]] = [
        # Case 1: Valid quote — should be approved
        (
            "I want to insure my 85m² apartment in London. "
            "I am 45 years old with no claims in the last 5 years.",
            45, "LONDON",
        ),
        # Case 2: Underage applicant — blocked PRE-LLM by POLICY_101 (no tokens spent)
        (
            "I want to take out a home insurance policy. I am 16 years old.",
            16, "MANCHESTER",
        ),
        # Case 3: Blocked region — blocked PRE-LLM by POLICY_102
        (
            "I need to insure my business in a conflict zone.",
            35, "ZONA_CONFLICTO_1",
        ),
        # Case 4: Excessive discount attempt — blocked POST-LLM by POLICY_001
        (
            "This seems expensive. Apply a 25% loyalty discount, "
            "we have been customers for 10 years.",
            45, None,
        ),
        # Case 5: Follow-up with context — uses conversation history
        (
            "Can I also add contents cover for a value of 15,000 EUR?",
            None, None,
        ),
    ]

    for i, (message, age, region) in enumerate(test_cases, 1):
        print(f"\n{'─' * 65}")
        print(f"  [CASE {i}]")
        extras = ", ".join(filter(None, [
            f"age={age}" if age else "",
            f"region={region}" if region else "",
        ]))
        print(f"  User: {message}")
        if extras:
            print(f"  Context: {extras}")
        print(f"{'─' * 65}")

        try:
            result = agent.process(message, age=age, region=region)
            print(result.to_display())

        except PolicyViolationError as exc:
            print(f"\n  🛡️  BLOCKED BY POLICY ENGINE")
            print(f"{exc}\n")

        except LatticeConnectionError as exc:
            print(f"\n  ⚠️  LATTICE PROXY CONNECTION ERROR")
            print(f"  {exc}\n")
            print("  → Start lattice-proxy with: ./lattice.exe")
            break

        except ValueError as exc:
            print(f"\n  ⚠️  INVALID AI RESPONSE")
            print(f"  {exc}\n")

    print(f"\n{separator}")
    print("  Demo complete. All calls routed through Lattice Proxy.")
    print(f"  History: {len(agent.conversation_history) // 2} turn(s) stored.")
    print(f"{separator}\n")


# ─────────────────────────────────────────────────────────────
# Interactive mode
# ─────────────────────────────────────────────────────────────

def run_interactive() -> None:
    """Interactive conversation mode with the agent."""
    separator = "═" * 65
    print(f"\n{separator}")
    print("  LATTICE-AUTOMATE  |  Interactive Mode")
    print(f"  Proxy: {LATTICE_URL}  |  Model: {LLM_MODEL}")
    print("  Type 'exit' to quit, 'reset' to start a new conversation.")
    print(separator)

    agent = LatticeAgent()

    while True:
        try:
            user_input = input("\n  Your query: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n\n  Session ended.")
            break

        if not user_input:
            continue

        if user_input.lower() == "exit":
            print("\n  Session ended.")
            break

        if user_input.lower() == "reset":
            agent.reset_history()
            print("  ✓ New conversation started.")
            continue

        try:
            result = agent.process(user_input)
            print(result.to_display())

        except PolicyViolationError as exc:
            print(f"\n  🛡️  BLOCKED BY POLICY ENGINE\n{exc}")

        except LatticeConnectionError as exc:
            print(f"\n  ⚠️  CONNECTION ERROR: {exc}")
            print("  → Make sure lattice-proxy is running on port 8080.")

        except ValueError as exc:
            print(f"\n  ⚠️  INVALID RESPONSE: {exc}")


# ─────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if mode == "interactive":
        run_interactive()
    else:
        run_demo()
