# Lattice-Automate

> **AI Agent Framework with Business Guardrails**  
> Deterministic AI responses for regulated industries — insurance, banking, legal.

[![Security & Quality](https://github.com/Karcsihack/lattice-automate/actions/workflows/security.yml/badge.svg)](https://github.com/Karcsihack/lattice-automate/actions/workflows/security.yml)
[![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python)](https://python.org)
[![Pydantic](https://img.shields.io/badge/Pydantic-v2-red?logo=pydantic)](https://docs.pydantic.dev)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

---

## ⚠️ Required Dependency

> **This project requires [Lattice Privacy Proxy](https://github.com/Karcsihack/lattice-proxy) running on port 8080.**  
> Lattice anonymizes all personal data (names, IDs, medical info) **before** it reaches the LLM.  
> Without it, your users' data is exposed to third-party AI providers.

```bash
# Start Lattice first (from the lattice-proxy repo):
.\lattice.exe
```

---

## What is Lattice-Automate?

**Lattice-Automate** turns AI from an experiment into a tool that an insurer or a bank would actually buy. It does so by solving the two problems that stop companies from adopting AI:

| Problem                                         | Solution                                                                 |
| ----------------------------------------------- | ------------------------------------------------------------------------ |
| "The AI might leak personal client data"        | **Lattice Proxy** — every request passes through an anonymization filter |
| "The AI might hallucinate prices or conditions" | **PolicyEngine** — validates every response before it reaches the user   |

### Full architecture (both repositories):

```
[User]
    │
    ▼
[LatticeAgent]          ← This repo: agent logic
    │
    ├─► [Lattice Proxy :8080]  ← lattice-proxy repo: anonymization
    │         │
    │         ▼
    │       [LLM]  (Mistral, GPT-4, etc.)
    │         │
    │    Response with anonymized data
    │
    ├─► [Pydantic Schema]      ← Validates JSON structure (no hallucinations)
    │
    └─► [PolicyEngine]         ← Validates business rules
            │
            ▼
    [Safe response to user]
```

---

## Installation

```bash
# 1. Clone this repository
git clone https://github.com/Karcsihack/lattice-automate.git
cd lattice-automate

# 2. Create a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/Mac

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up environment variables
copy .env.example .env
# Edit .env if your Lattice proxy runs on a different port
```

---

## Usage

### Automated demo (5 test cases)

```bash
python main.py
```

The demo runs 5 real-world scenarios:

| Case | Description                          | Expected result                  |
| ---- | ------------------------------------ | -------------------------------- |
| 1    | Normal quote, adult, valid region    | ✅ Approved                      |
| 2    | Underage applicant (16 years old)    | 🛡️ Blocked PRE-LLM (POLICY_101)  |
| 3    | Restricted region (ZONA_CONFLICTO_1) | 🛡️ Blocked PRE-LLM (POLICY_102)  |
| 4    | Request for 25% discount             | 🛡️ Blocked POST-LLM (POLICY_001) |
| 5    | Follow-up with conversation history  | ✅ Processed with context        |

### Interactive mode

```bash
python main.py interactive
```

### Import the agent in your code

```python
from main import LatticeAgent, PolicyViolationError, LatticeConnectionError

agent = LatticeAgent()

try:
    # Pass age and region for pre-LLM validation (no tokens spent)
    result = agent.process(
        "I want to insure my 90m² flat in London. I am 38 years old.",
        age=38,
        region="LONDON",
    )
    print(f"Premium: {result.final_value:.2f} EUR")
    print(f"Risk:    {result.risk_level}")
    print(f"Approved: {result.approved}")

except PolicyViolationError as e:
    # Catches both pre-LLM violations (age/region) and post-LLM (discount/premium)
    print(f"Blocked: {e}")

except LatticeConnectionError as e:
    print(f"Lattice Proxy unavailable: {e}")
```

---

## Core Components

### `InsuranceQuoteResponse` (Pydantic Schema)

Defines the only valid response the AI can produce. If the AI returns anything else, it is rejected at validation.

```python
class InsuranceQuoteResponse(BaseModel):
    explanation: str       # Required, minimum 10 characters
    approved: bool         # True/False — no ambiguity
    final_value: float     # EUR, >= 0
    discount_applied: float  # 0.0 to 100.0
    risk_level: str        # Exactly: "LOW" | "MEDIUM" | "HIGH"
```

### `PolicyEngine` (Business Guardrails)

Deterministic rule layer that **cannot be bypassed by the LLM**.
Rules are loaded from [`business_rules.yaml`](business_rules.yaml) at startup — no code changes required.

**PRE-LLM validation** (before spending any tokens):

| Rule       | Description                | YAML key             |
| ---------- | -------------------------- | -------------------- |
| POLICY_101 | Minimum insurable age      | `min_age_insured`    |
| POLICY_102 | Blocked geographic regions | `restricted_regions` |

**POST-LLM validation** (against the AI response):

| Rule       | Description                           | YAML key                 |
| ---------- | ------------------------------------- | ------------------------ |
| POLICY_001 | Maximum discount: 15%                 | `max_discount`           |
| POLICY_002 | Maximum premium: 50,000 EUR           | `max_premium_eur`        |
| POLICY_003 | Minimum premium (if approved): 50 EUR | `min_premium_eur`        |
| POLICY_004 | HIGH risk → max discount 5%           | `high_risk_max_discount` |

### `LatticeAgent` (Orchestrator)

Manages the full pipeline including **conversation memory** with a token limit that respects the Lattice Vault configuration.

---

## `business_rules.yaml` — Your Secret Weapon

This file is the **"brain" of the PolicyEngine**. The Compliance team edits it without touching Python code.
Every time the system starts, rules are reloaded from disk.

```yaml
policies:
  max_premium_eur: 50000.0 # Maximum quotable premium
  min_premium_eur: 50.0 # Minimum acceptable premium
  max_discount: 0.15 # Maximum discount (15%)
  high_risk_max_discount: 0.05 # Max discount on HIGH risk (5%)
  min_age_insured: 18 # Minimum insurable age

  restricted_regions: # Non-insurable zones
    - "ZONA_CONFLICTO_1"
    - "ZONA_CATASTROFE_A"

  required_fields: # Fields Lattice must have validated
    - "SSN"
    - "EMAIL"
    - "PHONE"

guardrails:
  block_hallucinated_discounts: true
  log_all_violations: true
  max_llm_retries: 0 # 0 = fail-fast (recommended for production)
```

> **Sales pitch:** The Head of Compliance at an insurer can change `max_discount: 0.15`
> to `max_discount: 0.10` and the entire system respects it on the next restart.
> No PR. No engineering meeting required.

---

## Configuration

Create a `.env` file from `.env.example`:

```env
# Lattice Privacy Proxy URL
LATTICE_URL=http://localhost:8080/v1/chat/completions

# LLM model to use (the proxy decides the actual backend)
LLM_MODEL=mistral

# Low temperature = more determinism (0.0 = fully deterministic)
LLM_TEMPERATURE=0.1

# Conversation history character limit (respects the Lattice token vault)
MAX_HISTORY_CHARS=16000
```

> Business rule limits are no longer in `.env` — they live in [`business_rules.yaml`](business_rules.yaml).

---

## Why this matters for your business

This architecture implements what the industry calls **"Constitutional AI"** — AI with a business constitution:

- **Google/OpenAI** use this term for AI that cannot lie or bypass rules
- **Insurers and banks** need it to pass regulatory audits (GDPR, Solvency II, DORA)
- **You sell** the complete ecosystem: **Privacy (Lattice Proxy) + Control (Lattice Automate)**

### The sales conversation:

```
Client: "What if the AI gives a wrong price?"
You:    "Impossible. The PolicyEngine blocks it before it leaves the system."

Client: "What about our clients' personal data?"
You:    "It never reaches the LLM. Lattice Proxy anonymizes it first."
```

---

## Lattice Ecosystem

| Repository                                                           | Language         | Function                                  | Status       |
| -------------------------------------------------------------------- | ---------------- | ----------------------------------------- | ------------ |
| [lattice-proxy](https://github.com/Karcsihack/lattice-proxy)         | Go               | Privacy proxy and anonymization           | ✅ Available |
| **lattice-automate**                                                 | Python           | AI agent framework with guardrails        | 🚀 This repo |
| [lattice-dashboard](https://github.com/Karcsihack/lattice-dashboard) | Python/Streamlit | Governance console & real-time monitoring | ✅ Available |

---

## License

MIT — Free for commercial use with attribution.

---

_Built with Python, Pydantic, and the conviction that enterprise AI must be predictable._
