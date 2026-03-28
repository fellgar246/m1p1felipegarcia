"""
safety.py — Módulo de moderación y seguridad para prompts adversariales.

Implementa detección de contenido inseguro mediante:
1. Patrones adversariales locales (regex)
2. API de moderación de OpenAI (fallback si la API falla: solo reglas locales)

Logging estructurado de todas las decisiones a logs/moderation_logs.json.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from openai import OpenAI

# ---------------------------------------------------------------------------
# Logging estructurado
# ---------------------------------------------------------------------------
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("moderation")
logger.setLevel(logging.INFO)

_log_file = LOG_DIR / "moderation_logs.json"
_handler = logging.FileHandler(_log_file, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_handler)

# ---------------------------------------------------------------------------
# Patrones adversariales locales
# ---------------------------------------------------------------------------
ADVERSARIAL_PATTERNS: list[dict] = [
    {
        "pattern": re.compile(
            r"ignora\s+(todas\s+)?(las\s+)?instrucciones|ignore\s+(all\s+)?instructions",
            re.IGNORECASE,
        ),
        "code": "inyeccion_de_prompt",
        "description": "Intento de inyección de prompt detectado",
    },
    {
        "pattern": re.compile(
            r"olvi(da|date)\s+.*instrucciones|forget\s+.*instructions",
            re.IGNORECASE,
        ),
        "code": "inyeccion_de_prompt",
        "description": "Intento de sobreescribir instrucciones del sistema",
    },
    {
        "pattern": re.compile(
            r"(comparte|dame|dime|revela|muestra)\s+.*(contraseña|password|clave|secreto|api.?key|token)",
            re.IGNORECASE,
        ),
        "code": "filtracion_de_privacidad",
        "description": "Solicitud de datos privados o credenciales",
    },
    {
        "pattern": re.compile(
            r"(actúa|actua|act)\s+(como|as)\s+(un\s+)?(hacker|atacante|attacker)",
            re.IGNORECASE,
        ),
        "code": "jailbreak",
        "description": "Intento de jailbreak mediante cambio de rol",
    },
    {
        "pattern": re.compile(
            r"(modo|mode)\s+(desarrollador|developer|DAN|admin)",
            re.IGNORECASE,
        ),
        "code": "jailbreak",
        "description": "Intento de activar modo no autorizado",
    },
]


def _check_local_patterns(text: str) -> list[dict]:
    """Evalúa el texto contra patrones adversariales locales."""
    reasons = []
    for entry in ADVERSARIAL_PATTERNS:
        if entry["pattern"].search(text):
            reasons.append({
                "code": entry["code"],
                "description": entry["description"],
            })
    return reasons


def _check_openai_moderation(text: str) -> list[dict]:
    """Llama a la API de moderación de OpenAI. Retorna razones si se flaggea."""
    try:
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = client.moderations.create(input=text)
        result = response.results[0]

        if result.flagged:
            flagged_categories = [
                cat for cat, flagged in result.categories.model_dump().items()
                if flagged
            ]
            return [{
                "code": "openai_moderation",
                "description": f"Categorías flaggeadas: {', '.join(flagged_categories)}",
            }]
    except Exception:
        # Si la API falla, no bloqueamos — las reglas locales ya cubrieron
        pass

    return []


def _log_decision(user_id: str, input_text: str, action: str,
                  reasons: list[dict]) -> None:
    """Registra la decisión de moderación en el log."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "input_text": input_text[:200],  # Truncar para el log
        "action": action,
        "reasons": reasons,
    }
    logger.info(json.dumps(entry, ensure_ascii=False))


async def moderate(user_id: str, input_text: str,
                   context: dict | None = None) -> dict:
    """
    Evalúa un texto de entrada para detectar contenido adversarial o inseguro.

    Args:
        user_id: Identificador del usuario.
        input_text: Texto a evaluar.
        context: Contexto adicional (reservado para uso futuro).

    Returns:
        dict con:
            - action: "allow" o "block"
            - reasons: lista de razones si se bloquea
    """
    reasons: list[dict] = []

    # 1. Verificar patrones locales
    reasons.extend(_check_local_patterns(input_text))

    # 2. Verificar con API de moderación de OpenAI
    reasons.extend(_check_openai_moderation(input_text))

    action = "block" if reasons else "allow"

    # Registrar decisión
    _log_decision(user_id, input_text, action, reasons)

    return {"action": action, "reasons": reasons}
