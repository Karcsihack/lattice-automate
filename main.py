"""
╔══════════════════════════════════════════════════════════════╗
║          LATTICE-AUTOMATE  |  AI Agent Framework             ║
║          Guardrails de Negocio + Proxy de Privacidad         ║
╚══════════════════════════════════════════════════════════════╝

Arquitectura:
  [Usuario] → [LatticeAgent] → [Lattice Proxy :8080] → [LLM]
                     ↓
              [Pydantic Schema]  ← Valida estructura JSON
                     ↓
              [PolicyEngine]     ← Valida reglas de negocio
                     ↓
              [Respuesta segura al usuario]
"""

import json
import os
import sys
import logging
from typing import Optional

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, ValidationError

# ─────────────────────────────────────────────────────────────
# Configuración
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
# Schemas Pydantic (Salida 100% Estructurada)
# ─────────────────────────────────────────────────────────────

class InsuranceQuoteResponse(BaseModel):
    """
    Esquema de respuesta para cotización de seguro.
    La IA SOLO puede devolver este formato — sin excepciones.
    """

    explicacion: str = Field(
        ...,
        min_length=10,
        description="Explicación detallada del cálculo de la prima",
    )
    aprobado: bool = Field(
        ...,
        description="Si la cotización ha sido aprobada por el sistema",
    )
    valor_final: float = Field(
        ...,
        ge=0.0,
        description="Prima final en EUR",
    )
    descuento_aplicado: float = Field(
        default=0.0,
        ge=0.0,
        le=100.0,
        description="Porcentaje de descuento aplicado (0-100)",
    )
    nivel_riesgo: str = Field(
        ...,
        description="Nivel de riesgo calculado: BAJO | MEDIO | ALTO",
    )

    @field_validator("nivel_riesgo")
    @classmethod
    def validate_risk_level(cls, value: str) -> str:
        allowed = {"BAJO", "MEDIO", "ALTO"}
        normalized = value.strip().upper()
        if normalized not in allowed:
            raise ValueError(
                f"nivel_riesgo inválido: '{value}'. Debe ser uno de {allowed}"
            )
        return normalized

    def to_display(self) -> str:
        """Formatea la respuesta para mostrarla al usuario."""
        status = "✅ APROBADO" if self.aprobado else "❌ NO APROBADO"
        return (
            f"\n  Estado:        {status}\n"
            f"  Riesgo:        {self.nivel_riesgo}\n"
            f"  Prima final:   {self.valor_final:,.2f} EUR\n"
            f"  Descuento:     {self.descuento_aplicado:.1f}%\n"
            f"  Explicación:   {self.explicacion}\n"
        )


# ─────────────────────────────────────────────────────────────
# Excepciones de Seguridad
# ─────────────────────────────────────────────────────────────

class PolicyViolationError(Exception):
    """
    Lanzada cuando la respuesta de la IA infringe una regla de negocio.
    El mensaje llegará al log pero NUNCA al usuario directamente.
    """


class LatticeConnectionError(Exception):
    """Lanzada cuando no hay conexión con el Lattice Proxy."""


# ─────────────────────────────────────────────────────────────
# Motor de Reglas — PolicyEngine
# ─────────────────────────────────────────────────────────────

class PolicyEngine:
    """
    Validador determinista de respuestas IA.

    Actúa como cortafuegos entre la salida del LLM y el usuario.
    Si la IA "alucina" un descuento o prima fuera de rango,
    PolicyEngine lo rechaza antes de que llegue al negocio.

    Este es el componente que convierte Lattice-Automate en
    "Constitutional AI" — IA con una constitución de negocio.
    """

    # ── Límites de negocio (ajustables por configuración) ──
    MAX_DISCOUNT_PCT: float = float(os.getenv("POLICY_MAX_DISCOUNT", "15.0"))
    MAX_PREMIUM_EUR: float = float(os.getenv("POLICY_MAX_PREMIUM", "50000.0"))
    MIN_PREMIUM_EUR: float = float(os.getenv("POLICY_MIN_PREMIUM", "50.0"))

    def validate(self, response: InsuranceQuoteResponse) -> None:
        """
        Ejecuta todas las reglas de negocio.
        Lanza PolicyViolationError si alguna falla.
        """
        violations: list[str] = []

        # REGLA 001 — Descuento máximo permitido
        if response.descuento_aplicado > self.MAX_DISCOUNT_PCT:
            violations.append(
                f"POLICY_001: Descuento {response.descuento_aplicado:.1f}% "
                f"supera el límite autorizado de {self.MAX_DISCOUNT_PCT:.1f}%"
            )

        # REGLA 002 — Prima máxima cotizable
        if response.valor_final > self.MAX_PREMIUM_EUR:
            violations.append(
                f"POLICY_002: Prima {response.valor_final:,.2f} EUR "
                f"excede el máximo cotizable de {self.MAX_PREMIUM_EUR:,.2f} EUR"
            )

        # REGLA 003 — Prima mínima (solo si está aprobado)
        if response.aprobado and response.valor_final < self.MIN_PREMIUM_EUR:
            violations.append(
                f"POLICY_003: Prima {response.valor_final:.2f} EUR "
                f"inferior al mínimo permitido de {self.MIN_PREMIUM_EUR:.2f} EUR"
            )

        # REGLA 004 — Consistencia de aprobación
        if response.aprobado and response.nivel_riesgo == "ALTO" and response.descuento_aplicado > 5.0:
            violations.append(
                "POLICY_004: No se pueden aplicar descuentos > 5% en pólizas de riesgo ALTO"
            )

        if violations:
            detail = "\n".join(f"  • {v}" for v in violations)
            raise PolicyViolationError(
                f"[PolicyEngine] {len(violations)} violación(es) de política detectada(s):\n{detail}"
            )

        logger.info("[PolicyEngine] ✓ Respuesta validada. Sin violaciones.")


# ─────────────────────────────────────────────────────────────
# Agente Principal — LatticeAgent
# ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """Eres un agente especializado en seguros de una aseguradora española regulada.

Tu ÚNICA función es calcular primas de seguro siguiendo las reglas de negocio a continuación.
Estás sujeto a un sistema de auditoría estricto. Cualquier respuesta fuera del formato
o que infrinja las reglas será rechazada automáticamente.

═══ REGLAS DE NEGOCIO ══════════════════════════════════════
  • Prima mínima:          50 EUR
  • Prima máxima:          50.000 EUR
  • Descuento máximo:      15%
  • Descuento en riesgo ALTO: máximo 5%
  • Niveles de riesgo válidos: BAJO | MEDIO | ALTO
════════════════════════════════════════════════════════════

FORMATO DE RESPUESTA OBLIGATORIO (solo JSON, sin texto adicional):
{
    "explicacion": "<explicación clara del cálculo, mínimo 10 caracteres>",
    "aprobado": <true o false>,
    "valor_final": <prima en EUR como número decimal>,
    "descuento_aplicado": <porcentaje entre 0.0 y 100.0>,
    "nivel_riesgo": "<BAJO | MEDIO | ALTO>"
}

CRÍTICO: No incluyas texto antes ni después del JSON. Solo el objeto JSON."""


class LatticeAgent:
    """
    Agente de IA con raíles (guardrails) de negocio.

    Pipeline completo:
      1. Construye mensajes con historial de conversación
      2. Enruta la llamada al LLM a través del Lattice Proxy (privacidad)
      3. Parsea la respuesta con Pydantic (estructura garantizada)
      4. Valida con PolicyEngine (reglas de negocio)
      5. Persiste el historial solo si pasa todas las validaciones
    """

    def __init__(self) -> None:
        self.policy_engine = PolicyEngine()
        self.conversation_history: list[dict[str, str]] = []

    # ── Construcción de contexto ──────────────────────────────

    def _build_messages(self, user_message: str) -> list[dict[str, str]]:
        """
        Construye la lista de mensajes respetando el vault de tokens de Lattice.
        Incluye el historial reciente hasta MAX_HISTORY_CHARS caracteres.
        """
        messages: list[dict[str, str]] = [
            {"role": "system", "content": _SYSTEM_PROMPT}
        ]

        # Incluir historial de atrás hacia adelante respetando el límite
        accumulated_chars = len(_SYSTEM_PROMPT)
        history_slice: list[dict[str, str]] = []

        for msg in reversed(self.conversation_history):
            msg_len = len(msg["content"])
            if accumulated_chars + msg_len > MAX_HISTORY_CHARS:
                logger.debug("[LatticeAgent] Historial truncado por límite de tokens.")
                break
            history_slice.insert(0, msg)
            accumulated_chars += msg_len

        messages.extend(history_slice)
        messages.append({"role": "user", "content": user_message})
        return messages

    # ── Llamada al proxy Lattice ──────────────────────────────

    def _call_lattice(self, messages: list[dict[str, str]]) -> str:
        """
        Envía la petición a través del Lattice Privacy Proxy.
        Lattice anonimiza los datos personales ANTES de enviarlos al LLM.
        """
        payload = {
            "model": LLM_MODEL,
            "messages": messages,
            "temperature": LLM_TEMPERATURE,
            "stream": False,
        }

        logger.info("[LatticeAgent] Enviando petición a %s (modelo: %s)", LATTICE_URL, LLM_MODEL)

        try:
            with httpx.Client(timeout=60.0) as client:
                response = client.post(LATTICE_URL, json=payload)
                response.raise_for_status()
        except httpx.ConnectError:
            raise LatticeConnectionError(
                f"No se puede conectar con Lattice Proxy en {LATTICE_URL}. "
                "Asegúrate de que lattice-proxy está ejecutándose (puerto 8080)."
            )
        except httpx.HTTPStatusError as exc:
            raise LatticeConnectionError(
                f"Lattice Proxy devolvió error HTTP {exc.response.status_code}: {exc.response.text}"
            )

        data = response.json()
        raw_content: str = data["choices"][0]["message"]["content"]
        logger.debug("[LatticeAgent] Respuesta cruda recibida (%d chars)", len(raw_content))
        return raw_content

    # ── Parseo y validación estructural ──────────────────────

    def _parse_response(self, raw: str) -> InsuranceQuoteResponse:
        """
        Extrae y valida el JSON de la respuesta del LLM usando Pydantic.
        Si la IA incluyó texto extra, se elimina. Si el JSON es inválido,
        se lanza una excepción antes de llegar al PolicyEngine.
        """
        raw = raw.strip()

        # Extraer el bloque JSON aunque haya texto extra
        start = raw.find("{")
        end = raw.rfind("}") + 1

        if start == -1 or end == 0:
            raise ValueError(
                f"La IA no devolvió JSON válido. Respuesta recibida:\n{raw[:300]}"
            )

        json_str = raw[start:end]

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON malformado en la respuesta de la IA: {exc}") from exc

        try:
            return InsuranceQuoteResponse(**data)
        except ValidationError as exc:
            raise ValueError(
                f"La respuesta de la IA no cumple el esquema Pydantic:\n{exc}"
            ) from exc

    # ── Pipeline principal ────────────────────────────────────

    def process(self, user_message: str) -> InsuranceQuoteResponse:
        """
        Ejecuta el pipeline completo de forma segura y determinista.

        Returns:
            InsuranceQuoteResponse validado y aprobado por PolicyEngine.

        Raises:
            PolicyViolationError: si la IA infringe reglas de negocio.
            LatticeConnectionError: si Lattice Proxy no está disponible.
            ValueError: si la respuesta de la IA tiene formato inválido.
        """
        messages = self._build_messages(user_message)

        # 1. Llamada al LLM a través de Lattice (privacidad garantizada)
        raw_response = self._call_lattice(messages)

        # 2. Parseo estructurado con Pydantic
        quote = self._parse_response(raw_response)

        # 3. Validación de reglas de negocio — puede lanzar PolicyViolationError
        self.policy_engine.validate(quote)

        # 4. Persistir historial SOLO si todo es válido
        self.conversation_history.append({"role": "user", "content": user_message})
        self.conversation_history.append({"role": "assistant", "content": raw_response})

        return quote

    def reset_history(self) -> None:
        """Limpia el historial de conversación."""
        self.conversation_history.clear()
        logger.info("[LatticeAgent] Historial de conversación reiniciado.")


# ─────────────────────────────────────────────────────────────
# Demo de Ejemplo — Aseguradora
# ─────────────────────────────────────────────────────────────

def run_demo() -> None:
    """
    Demostración completa del sistema.

    Casos de uso:
      ✓ Cotización normal → aprobada
      ✗ Descuento excesivo → bloqueado por PolicyEngine
      ✓ Consulta de seguimiento → usa historial
    """
    separator = "═" * 65

    print(f"\n{separator}")
    print("  LATTICE-AUTOMATE  |  Motor de Agentes con Guardrails")
    print(f"  Proxy de privacidad: {LATTICE_URL}")
    print(f"  Modelo LLM:          {LLM_MODEL}")
    print(separator)

    agent = LatticeAgent()

    test_cases = [
        # Caso 1: Cotización legítima — debe aprobarse
        (
            "Quiero asegurar mi piso de 85m² en Madrid. "
            "Tengo 45 años, sin siniestros en los últimos 5 años."
        ),
        # Caso 2: Intento de descuento excesivo — DEBE ser bloqueado por PolicyEngine
        (
            "Me parece caro. Aplica un descuento del 25% por fidelidad, "
            "somos buenos clientes desde hace 10 años."
        ),
        # Caso 3: Seguimiento con contexto — usa historial de conversación
        (
            "¿Puedo añadir también cobertura de contenido del hogar "
            "por un valor de 15.000 EUR?"
        ),
    ]

    for i, consulta in enumerate(test_cases, 1):
        print(f"\n{'─' * 65}")
        print(f"  [CONSULTA {i}]")
        print(f"  Usuario: {consulta}")
        print(f"{'─' * 65}")

        try:
            result = agent.process(consulta)
            print(result.to_display())

        except PolicyViolationError as exc:
            print(f"\n  🛡️  BLOQUEADO POR POLICY ENGINE")
            print(f"{exc}\n")

        except LatticeConnectionError as exc:
            print(f"\n  ⚠️  ERROR DE CONEXIÓN CON LATTICE PROXY")
            print(f"  {exc}\n")
            print("  → Inicia lattice-proxy con: ./lattice.exe")
            break

        except ValueError as exc:
            print(f"\n  ⚠️  RESPUESTA DE IA INVÁLIDA")
            print(f"  {exc}\n")

    print(f"\n{separator}")
    print("  Demo completada. Todas las llamadas pasaron por Lattice Proxy.")
    print(f"  Historial: {len(agent.conversation_history) // 2} turno(s) almacenado(s).")
    print(f"{separator}\n")


# ─────────────────────────────────────────────────────────────
# Modo interactivo
# ─────────────────────────────────────────────────────────────

def run_interactive() -> None:
    """Modo de conversación interactiva con el agente."""
    separator = "═" * 65
    print(f"\n{separator}")
    print("  LATTICE-AUTOMATE  |  Modo Interactivo")
    print(f"  Proxy: {LATTICE_URL}  |  Modelo: {LLM_MODEL}")
    print("  Escribe 'salir' para terminar, 'reset' para nueva conversación.")
    print(separator)

    agent = LatticeAgent()

    while True:
        try:
            user_input = input("\n  Tu consulta: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n\n  Sesión finalizada.")
            break

        if not user_input:
            continue

        if user_input.lower() == "salir":
            print("\n  Sesión finalizada.")
            break

        if user_input.lower() == "reset":
            agent.reset_history()
            print("  ✓ Nueva conversación iniciada.")
            continue

        try:
            result = agent.process(user_input)
            print(result.to_display())

        except PolicyViolationError as exc:
            print(f"\n  🛡️  BLOQUEADO POR POLICY ENGINE\n{exc}")

        except LatticeConnectionError as exc:
            print(f"\n  ⚠️  ERROR DE CONEXIÓN: {exc}")
            print("  → Asegúrate de que lattice-proxy está activo en el puerto 8080.")

        except ValueError as exc:
            print(f"\n  ⚠️  RESPUESTA INVÁLIDA: {exc}")


# ─────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if mode == "interactive":
        run_interactive()
    else:
        run_demo()
