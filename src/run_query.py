#!/usr/bin/env python3
"""
run_query.py — Multitasking Text Utility

Script ejecutable que recibe una pregunta del usuario, la envía a la API de
OpenAI usando few-shot prompting, y devuelve una respuesta en JSON estructurado
con campos: answer, confidence, actions.

Registra métricas por ejecución: tokens, latencia y costo estimado.
Incluye paso de moderación de seguridad antes de llamar a la API.

Uso:
    python src/run_query.py "¿Cómo reseteo mi contraseña?"
    python src/run_query.py   # modo interactivo
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Agregar el directorio raíz del proyecto al path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402  # isort:skip
from openai import OpenAI  # noqa: E402  # isort:skip
from src.safety import moderate  # noqa: E402  # isort:skip


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
load_dotenv(PROJECT_ROOT / ".env")

PROMPT_FILE = PROJECT_ROOT / "prompts" / "main_prompt.md"
METRICS_FILE = PROJECT_ROOT / "metrics" / "metrics.json"
LAST_RESPONSE_FILE = PROJECT_ROOT / "metrics" / "last_response.json"

# Costos por 1M tokens (USD) — gpt-4o-mini (actualizar según modelo)
MODEL_PRICING = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-3.5-turbo": {"input": 0.50, "output": 1.50},
}


def load_system_prompt() -> str:
    """Carga la plantilla de prompt desde el archivo."""
    if not PROMPT_FILE.exists():
        print(f"Error: No se encontró {PROMPT_FILE}", file=sys.stderr)
        sys.exit(1)
    return PROMPT_FILE.read_text(encoding="utf-8")


def estimate_cost(model: str, prompt_tokens: int,
                  completion_tokens: int) -> float:
    """Calcula el costo estimado en USD basado en el modelo y tokens usados."""
    pricing = MODEL_PRICING.get(model, MODEL_PRICING["gpt-4o-mini"])
    cost = (prompt_tokens * pricing["input"] / 1_000_000 +
            completion_tokens * pricing["output"] / 1_000_000)
    return round(cost, 8)


def save_metrics(metrics: dict) -> None:
    """Agrega las métricas a metrics/metrics.json."""
    METRICS_FILE.parent.mkdir(exist_ok=True)

    existing: list[dict] = []
    if METRICS_FILE.exists():
        try:
            existing = json.loads(METRICS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            existing = []

    existing.append(metrics)
    METRICS_FILE.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def save_last_response(response_data: dict) -> None:
    """Guarda la última respuesta en metrics/last_response.json."""
    LAST_RESPONSE_FILE.parent.mkdir(exist_ok=True)
    LAST_RESPONSE_FILE.write_text(
        json.dumps(response_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def query_openai(question: str, system_prompt: str,
                 model: str) -> dict:
    """
    Envía la pregunta a OpenAI y retorna el resultado parseado con métricas.

    Returns:
        dict con claves: response (dict JSON), metrics (dict)
    """
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    start_time = time.perf_counter()

    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ],
        temperature=0.3,
        response_format={"type": "json_object"},
    )

    latency_ms = round((time.perf_counter() - start_time) * 1000, 2)

    # Extraer uso de tokens
    usage = completion.usage
    prompt_tokens = usage.prompt_tokens
    completion_tokens = usage.completion_tokens
    total_tokens = usage.total_tokens

    # Parsear respuesta JSON
    raw_content = completion.choices[0].message.content
    try:
        response_json = json.loads(raw_content)
    except json.JSONDecodeError:
        response_json = {
            "answer": raw_content,
            "confidence": 0.0,
            "actions": ["Error: respuesta no fue JSON válido"],
        }

    # Construir métricas
    metrics = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "question": question,
        "tokens_prompt": prompt_tokens,
        "tokens_completion": completion_tokens,
        "total_tokens": total_tokens,
        "latency_ms": latency_ms,
        "estimated_cost_usd": estimate_cost(model, prompt_tokens,
                                            completion_tokens),
    }

    return {"response": response_json, "metrics": metrics}


async def run(question: str) -> None:
    """Flujo principal: moderación → consulta → métricas → output."""
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    # --- Paso 1: Moderación de seguridad ---
    moderation_result = await moderate(
        user_id="cli-user",
        input_text=question,
        context={},
    )

    if moderation_result["action"] == "block":
        blocked_response = {
            "answer": "Lo siento, no puedo procesar esta solicitud porque fue "
                      "marcada por el sistema de seguridad.",
            "confidence": 1.0,
            "actions": ["Solicitud bloqueada por moderación"],
            "moderation": moderation_result,
        }
        print(json.dumps(blocked_response, indent=2, ensure_ascii=False))
        save_last_response(blocked_response)

        # Registrar métricas (sin llamada a API)
        metrics = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "question": question,
            "tokens_prompt": 0,
            "tokens_completion": 0,
            "total_tokens": 0,
            "latency_ms": 0,
            "estimated_cost_usd": 0,
            "blocked_by_moderation": True,
        }
        save_metrics(metrics)
        return

    # --- Paso 2: Consultar OpenAI ---
    system_prompt = load_system_prompt()
    result = query_openai(question, system_prompt, model)

    # --- Paso 3: Output ---
    print(json.dumps(result["response"], indent=2, ensure_ascii=False))

    # --- Paso 4: Guardar métricas y respuesta ---
    save_metrics(result["metrics"])
    save_last_response(result["response"])

    # Imprimir resumen de métricas a stderr
    m = result["metrics"]
    print(
        f"\n--- Métricas ---\n"
        f"Modelo:      {m['model']}\n"
        f"Tokens:      {m['tokens_prompt']} prompt + "
        f"{m['tokens_completion']} completion = {m['total_tokens']} total\n"
        f"Latencia:    {m['latency_ms']} ms\n"
        f"Costo est.:  ${m['estimated_cost_usd']} USD",
        file=sys.stderr,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Multitasking Text Utility — Asistente de soporte al cliente",
    )
    parser.add_argument(
        "question",
        nargs="?",
        help="Pregunta del usuario (si se omite, se pide interactivamente)",
    )
    args = parser.parse_args()

    question = args.question
    if not question:
        question = input("Ingresa tu pregunta: ").strip()
        if not question:
            print("Error: No se proporcionó ninguna pregunta.", file=sys.stderr)
            sys.exit(1)

    asyncio.run(run(question))


if __name__ == "__main__":
    main()
