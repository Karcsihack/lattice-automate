# Lattice-Automate

> **AI Agent Framework with Business Guardrails**  
> Deterministic AI responses for regulated industries — insurance, banking, legal.

[![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python)](https://python.org)
[![Pydantic](https://img.shields.io/badge/Pydantic-v2-red?logo=pydantic)](https://docs.pydantic.dev)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

---

## ⚠️ Requisito Obligatorio

> **This project requires [Lattice Privacy Proxy](https://github.com/Karcsihack/lattice-proxy) running on port 8080.**  
> Lattice anonymizes all personal data (names, IDs, medical info) **before** it reaches the LLM.  
> Without it, your users' data is exposed to third-party AI providers.

```bash
# Inicia Lattice primero (desde el repo lattice-proxy):
.\lattice.exe
```

---

## ¿Qué es Lattice-Automate?

**Lattice-Automate** convierte la IA de un experimento en una herramienta que una aseguradora o un banco pueden comprar. Lo hace resolviendo los dos problemas que impiden que las empresas adopten IA:

| Problema                                           | Solución                                                     |
| -------------------------------------------------- | ------------------------------------------------------------ |
| "La IA puede filtrar datos personales de clientes" | **Lattice Proxy** — todo pasa por un filtro de anonimización |
| "La IA puede inventarse precios o condiciones"     | **PolicyEngine** — valida cada respuesta antes de mostrarla  |

### La arquitectura completa (los dos repositorios):

```
[Usuario]
    │
    ▼
[LatticeAgent]          ← Este repo: lógica del agente
    │
    ├─► [Lattice Proxy :8080]  ← Repo lattice-proxy: anonimización
    │         │
    │         ▼
    │       [LLM]  (Mistral, GPT-4, etc.)
    │         │
    │    Respuesta con datos anonimizados
    │
    ├─► [Pydantic Schema]      ← Valida estructura JSON (sin alucinaciones)
    │
    └─► [PolicyEngine]         ← Valida reglas de negocio
            │
            ▼
    [Respuesta segura al usuario]
```

---

## Instalación

```bash
# 1. Clona este repositorio
git clone https://github.com/Karcsihack/lattice-automate.git
cd lattice-automate

# 2. Crea entorno virtual
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/Mac

# 3. Instala dependencias
pip install -r requirements.txt

# 4. Configura variables de entorno
copy .env.example .env
# Edita .env si tu Lattice corre en un puerto distinto
```

---

## Uso

### Demo automática (5 casos de prueba)

```bash
python main.py
```

La demo ejecuta 5 escenarios reales:

| Caso | Descripción                               | Resultado esperado                 |
| ---- | ----------------------------------------- | ---------------------------------- |
| 1    | Cotización normal, adulto, zona válida    | ✅ Aprobado                        |
| 2    | Solicitante menor de edad (16 años)       | 🛡️ Bloqueado PRE-LLM (POLICY_101)  |
| 3    | Zona restringida (ZONA_CONFLICTO_1)       | 🛡️ Bloqueado PRE-LLM (POLICY_102)  |
| 4    | Solicitud de descuento del 25%            | 🛡️ Bloqueado POST-LLM (POLICY_001) |
| 5    | Seguimiento con historial de conversación | ✅ Procesado con contexto          |

### Modo interactivo

```bash
python main.py interactive
```

### Importar el agente en tu código

```python
from main import LatticeAgent, PolicyViolationError, LatticeConnectionError

agent = LatticeAgent()

try:
    # Pasa edad y región para validación pre-LLM (sin gastar tokens)
    result = agent.process(
        "Quiero asegurar mi piso de 90m² en Barcelona, tengo 38 años.",
        edad=38,
        region="BARCELONA",
    )
    print(f"Prima: {result.valor_final:.2f} EUR")
    print(f"Riesgo: {result.nivel_riesgo}")
    print(f"Aprobado: {result.aprobado}")

except PolicyViolationError as e:
    # Captura tanto violaciones pre-LLM (edad/región) como post-LLM (descuento/prima)
    print(f"Bloqueado: {e}")

except LatticeConnectionError as e:
    print(f"Lattice Proxy no disponible: {e}")
```

---

## Componentes principales

### `InsuranceQuoteResponse` (Pydantic Schema)

Define la única respuesta válida que puede dar la IA. Si la IA devuelve cualquier otra cosa, se rechaza en validación.

```python
class InsuranceQuoteResponse(BaseModel):
    explicacion: str           # Obligatorio, mínimo 10 caracteres
    aprobado: bool             # True/False — sin ambigüedad
    valor_final: float         # EUR, >= 0
    descuento_aplicado: float  # 0.0 a 100.0
    nivel_riesgo: str          # Exactamente: "BAJO" | "MEDIO" | "ALTO"
```

### `PolicyEngine` (Guardrails de Negocio)

Capa determinista de reglas que **no puede ser evadida por el LLM**.
Las reglas se cargan desde [`business_rules.yaml`](business_rules.yaml) en tiempo de ejecución — sin tocar código.

**Validación PRE-LLM** (antes de gastar tokens):

| Regla      | Descripción                  | YAML key             |
| ---------- | ---------------------------- | -------------------- |
| POLICY_101 | Edad mínima del asegurado    | `min_age_insured`    |
| POLICY_102 | Zonas geográficas bloqueadas | `restricted_regions` |

**Validación POST-LLM** (sobre la respuesta de la IA):

| Regla      | Descripción                        | YAML key                 |
| ---------- | ---------------------------------- | ------------------------ |
| POLICY_001 | Descuento máximo: 15%              | `max_discount`           |
| POLICY_002 | Prima máxima: 50.000 EUR           | `max_premium_eur`        |
| POLICY_003 | Prima mínima (si aprobado): 50 EUR | `min_premium_eur`        |
| POLICY_004 | Riesgo ALTO → descuento máximo 5%  | `high_risk_max_discount` |

### `LatticeAgent` (Orquestador)

Gestiona el pipeline completo incluyendo **memoria de conversación** con límite de tokens que respeta la configuración del Vault de Lattice.

---

## `business_rules.yaml` — Tu Arma Secreta

Este archivo es el **"cerebro" del PolicyEngine**. El equipo de Compliance lo edita sin tocar código Python.
Cada vez que el sistema arranca, carga las reglas en caliente.

```yaml
policies:
  max_premium_eur: 50000.0 # Prima máxima cotizable
  min_premium_eur: 50.0 # Prima mínima aceptable
  max_discount: 0.15 # Descuento máximo (15%)
  high_risk_max_discount: 0.05 # Descuento máx. en riesgo ALTO (5%)
  min_age_insured: 18 # Edad mínima del asegurado

  restricted_regions: # Zonas no asegurables
    - "ZONA_CONFLICTO_1"
    - "ZONA_CATASTROFE_A"

  required_fields: # Campos que Lattice debe haber validado
    - "DNI"
    - "EMAIL"
    - "PHONE"

guardrails:
  block_hallucinated_discounts: true
  log_all_violations: true
  max_llm_retries: 0 # 0 = fail-fast (recomendado para producción)
```

> **Caso de venta:** El Director de Compliance de una aseguradora puede cambiar `max_discount: 0.15`
> a `max_discount: 0.10` y el sistema completo lo respeta en el siguiente arranque.
> Sin PR. Sin reunión con el equipo de ingeniería.

---

## Configuración

Crea un archivo `.env` basado en `.env.example`:

```env
# URL del Lattice Privacy Proxy
LATTICE_URL=http://localhost:8080/v1/chat/completions

# Modelo LLM a usar (el proxy decide el destino real)
LLM_MODEL=mistral

# Temperatura baja = más determinismo (0.0 = completamente determinista)
LLM_TEMPERATURE=0.1

# Límite de caracteres de historial (respeta el vault de tokens de Lattice)
MAX_HISTORY_CHARS=16000
```

> Los límites del PolicyEngine ya **no** van en `.env` — ahora viven en [`business_rules.yaml`](business_rules.yaml).

---

## Por qué esto importa para tu empresa

Esta arquitectura implementa lo que en la industria se llama **"Constitutional AI"** — IA con una constitución de negocio:

- **Google/OpenAI llaman así** a la IA que no puede mentir ni saltarse reglas
- **Las aseguradoras y bancos** lo necesitan para pasar auditorías regulatorias (GDPR, Solvencia II, DORA)
- **Tú vendes** el ecosistema completo: **Privacidad (Lattice Proxy) + Control (Lattice Automate)**

### El flujo de venta:

```
Cliente: "¿Y si la IA da un precio equivocado?"
Tú:      "Imposible. El PolicyEngine lo bloquea antes de que salga del sistema."

Cliente: "¿Y los datos de mis clientes?"
Tú:      "Nunca llegan al LLM. Lattice Proxy los anonimiza primero."
```

---

## Ecosistema Lattice

| Repositorio                                                  | Función                             | Estado        |
| ------------------------------------------------------------ | ----------------------------------- | ------------- |
| [lattice-proxy](https://github.com/Karcsihack/lattice-proxy) | Proxy de privacidad y anonimización | ✅ Disponible |
| **lattice-automate**                                         | Framework de agentes con guardrails | 🚀 Este repo  |

---

## Licencia

MIT — Libre para uso comercial con atribución.

---

_Construido con Python, Pydantic y la convicción de que la IA empresarial tiene que ser predecible._
