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
from pathlib import Path
from typing import Optional

import httpx
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, ValidationError

# ─────────────────────────────────────────────────────────────
# Ruta del archivo de reglas de negocio
# ─────────────────────────────────────────────────────────────
_RULES_FILE = Path(__file__).parent / "business_rules.yaml"

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

    Carga las reglas desde 'business_rules.yaml' en tiempo de ejecución.
    Actúa como cortafuegos entre la salida del LLM y el usuario:
      · validate_request() — bloquea ANTES de llamar al LLM (edad, región)
      · validate()         — bloquea DESPUÉS (descuento, prima, riesgo)

    Este es el componente que convierte Lattice-Automate en
    "Constitutional AI" — IA con una constitución de negocio.
    """

    # Fallbacks si no existe business_rules.yaml
    _DEFAULTS: dict = {
        "policies": {
            "max_discount": 0.15,
            "high_risk_max_discount": 0.05,
            "max_premium_eur": 50000.0,
            "min_premium_eur": 50.0,
            "min_age_insured": 18,
            "restricted_regions": [],
            "required_fields": ["DNI", "EMAIL", "PHONE"],
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
            "[PolicyEngine] Reglas cargadas — max_descuento=%.0f%% | prima=[%.0f–%.0f EUR] | "
            "edad_min=%d | regiones_bloqueadas=%d",
            self.MAX_DISCOUNT_PCT, self.MIN_PREMIUM_EUR, self.MAX_PREMIUM_EUR,
            self.MIN_AGE, len(self.RESTRICTED_REGIONS),
        )

    def _load_rules(self) -> dict:
        """Carga business_rules.yaml. Si no existe, usa valores por defecto."""
        if _RULES_FILE.exists():
            with open(_RULES_FILE, encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh)
            logger.info("[PolicyEngine] Reglas cargadas desde %s", _RULES_FILE.name)
            return loaded
        logger.warning(
            "[PolicyEngine] %s no encontrado. Usando valores por defecto.", _RULES_FILE.name
        )
        return self._DEFAULTS

    # ── Validación PRE-LLM (petición del usuario) ─────────────

    def validate_request(self, edad: int | None = None, region: str | None = None) -> None:
        """
        Valida los datos del usuario ANTES de llamar al LLM.
        Si hay un problema (menor de edad, zona bloqueada), se rechaza
        sin gastar ni un token.
        """
        violations: list[str] = []

        # REGLA 101 — Edad mínima del asegurado
        if edad is not None and edad < self.MIN_AGE:
            violations.append(
                f"POLICY_101: Edad {edad} inferior al mínimo asegurable ({self.MIN_AGE} años)"
            )

        # REGLA 102 — Zonas no asegurables
        if region is not None and region.upper() in self.RESTRICTED_REGIONS:
            violations.append(
                f"POLICY_102: Región '{region}' está en la lista de zonas no asegurables: "
                f"{sorted(self.RESTRICTED_REGIONS)}"
            )

        if violations:
            detail = "\n".join(f"  • {v}" for v in violations)
            raise PolicyViolationError(
                f"[PolicyEngine] Solicitud bloqueada pre-LLM:\n{detail}"
            )

    # ── Validación POST-LLM (respuesta de la IA) ─────────────

    def validate(self, response: InsuranceQuoteResponse) -> None:
        """
        Ejecuta todas las reglas de negocio sobre la respuesta del LLM.
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

        # REGLA 004 — Riesgo ALTO con descuento excesivo
        if (
            response.aprobado
            and response.nivel_riesgo == "ALTO"
            and response.descuento_aplicado > self.HIGH_RISK_MAX_DISCOUNT_PCT
        ):
            violations.append(
                f"POLICY_004: Riesgo ALTO — descuento máximo permitido "
                f"{self.HIGH_RISK_MAX_DISCOUNT_PCT:.0f}%, "
                f"solicitado {response.descuento_aplicado:.1f}%"
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

def _build_system_prompt(engine: "PolicyEngine") -> str:
    """Genera el prompt de sistema dinámicamente desde las reglas cargadas en PolicyEngine."""
    return (
        "Eres un agente especializado en seguros de una aseguradora española regulada.\n\n"
        "Tu ÚNICA función es calcular primas de seguro siguiendo las reglas de negocio a continuación.\n"
        "Estás sujeto a un sistema de auditoría estricto. Cualquier respuesta fuera del formato\n"
        "o que infrinja las reglas será rechazada automáticamente.\n\n"
        "═══ REGLAS DE NEGOCIO (cargadas desde business_rules.yaml) ════\n"
        f"  • Prima mínima:               {engine.MIN_PREMIUM_EUR:,.0f} EUR\n"
        f"  • Prima máxima:               {engine.MAX_PREMIUM_EUR:,.0f} EUR\n"
        f"  • Descuento máximo:           {engine.MAX_DISCOUNT_PCT:.0f}%\n"
        f"  • Descuento en riesgo ALTO:   máximo {engine.HIGH_RISK_MAX_DISCOUNT_PCT:.0f}%\n"
        "  • Niveles de riesgo válidos:  BAJO | MEDIO | ALTO\n"
        "══════════════════════════════════════════════════════════════\n\n"
        "FORMATO DE RESPUESTA OBLIGATORIO (solo JSON, sin texto adicional):\n"
        "{\n"
        '    "explicacion": "<explicación clara del cálculo, mínimo 10 caracteres>",\n'
        '    "aprobado": <true o false>,\n'
        '    "valor_final": <prima en EUR como número decimal>,\n'
        '    "descuento_aplicado": <porcentaje entre 0.0 y 100.0>,\n'
        '    "nivel_riesgo": "<BAJO | MEDIO | ALTO>"\n'
        "}\n\n"
        "CRÍTICO: No incluyas texto antes ni después del JSON. Solo el objeto JSON."
    )


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
        self._system_prompt = _build_system_prompt(self.policy_engine)
        self.conversation_history: list[dict[str, str]] = []

    # ── Construcción de contexto ──────────────────────────────

    def _build_messages(self, user_message: str) -> list[dict[str, str]]:
        """
        Construye la lista de mensajes respetando el vault de tokens de Lattice.
        Incluye el historial reciente hasta MAX_HISTORY_CHARS caracteres.
        """
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._system_prompt}
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

    def process(
        self,
        user_message: str,
        edad: int | None = None,
        region: str | None = None,
    ) -> InsuranceQuoteResponse:
        """
        Ejecuta el pipeline completo de forma segura y determinista.

        Args:
            user_message: Consulta en lenguaje natural del usuario.
            edad:  Edad del solicitante (validada pre-LLM contra business_rules.yaml).
            region: Zona geográfica (validada pre-LLM contra restricted_regions).

        Returns:
            InsuranceQuoteResponse validado y aprobado por PolicyEngine.

        Raises:
            PolicyViolationError: si la IA infringe reglas de negocio O si el
                                  cliente no cumple requisitos previos (edad, región).
            LatticeConnectionError: si Lattice Proxy no está disponible.
            ValueError: si la respuesta de la IA tiene formato inválido.
        """
        # 0. Validación PRE-LLM — sin gastar tokens
        self.policy_engine.validate_request(edad=edad, region=region)

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

    # (mensaje, edad, region) — los dos últimos son opcionales
    test_cases: list[tuple[str, int | None, str | None]] = [
        # Caso 1: Cotización legítima — debe aprobarse
        (
            "Quiero asegurar mi piso de 85m² en Madrid. "
            "Tengo 45 años, sin siniestros en los últimos 5 años.",
            45, "MADRID",
        ),
        # Caso 2: Menor de edad — bloqueado PRE-LLM por POLICY_101 (sin gastar tokens)
        (
            "Quiero contratar un seguro de hogar. Tengo 16 años.",
            16, "BARCELONA",
        ),
        # Caso 3: Zona bloqueada — bloqueado PRE-LLM por POLICY_102
        (
            "Necesito asegurar mi empresa en una zona de conflicto.",
            35, "ZONA_CONFLICTO_1",
        ),
        # Caso 4: Intento de descuento excesivo — bloqueado POST-LLM por POLICY_001
        (
            "Me parece caro. Aplica un descuento del 25% por fidelidad, "
            "somos buenos clientes desde hace 10 años.",
            45, None,
        ),
        # Caso 5: Seguimiento con contexto — usa historial de conversación
        (
            "¿Puedo añadir también cobertura de contenido del hogar "
            "por un valor de 15.000 EUR?",
            None, None,
        ),
    ]

    for i, (consulta, edad, region) in enumerate(test_cases, 1):
        print(f"\n{'─' * 65}")
        print(f"  [CONSULTA {i}]")
        extras = ", ".join(filter(None, [
            f"edad={edad}" if edad else "",
            f"región={region}" if region else "",
        ]))
        print(f"  Usuario: {consulta}")
        if extras:
            print(f"  Contexto: {extras}")
        print(f"{'─' * 65}")

        try:
            result = agent.process(consulta, edad=edad, region=region)
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
