"""Mide el acierto del estado sugerido por el LLM sobre samples/eval_chats.json.

Uso:  python -m app.eval                  (todos los casos)
      python -m app.eval t1 t2 --pause 2  (solo esos casos, con pausa entre llamadas)
Usa GEMINI_API_KEY y GEMINI_MODEL del entorno.
Sale con código 1 si el acierto es menor al 85 %.
"""
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from app import config, llm

TARGET = 0.85
SAMPLES = Path(__file__).resolve().parent.parent / "samples" / "eval_chats.json"


def transcript(chat: list[list[str]]) -> str:
    return "\n".join(f"{'Negocio' if who == 'negocio' else 'Cliente'}: {text}" for who, text in chat)


async def main() -> int:
    if not config.GEMINI_API_KEY:
        print("Falta GEMINI_API_KEY", file=sys.stderr)
        return 2
    cases = json.loads(SAMPLES.read_text(encoding="utf-8"))
    args = sys.argv[1:]
    pause = float(args[args.index("--pause") + 1]) if "--pause" in args else 0.0
    only = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or args[i - 1] != "--pause")]
    if only:
        cases = [c for c in cases if c["id"] in only]
    now = datetime(2026, 11, 22, 10, 0, tzinfo=config.LOCAL_TZ)
    ok = 0
    errors = 0
    confusion: Counter = Counter()
    for case in cases:
        if pause:
            await asyncio.sleep(pause)
        try:
            res = await llm.analyze(transcript(case["chat"]), now)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            print(f"{case['id']:>3}  ERROR  {exc}")
            continue
        hit = res.estado == case["expected"]
        ok += hit
        if not hit:
            confusion[(case["expected"], res.estado)] += 1
        print(f"{case['id']:>3}  {'OK ' if hit else 'MAL'}  esperado={case['expected']:<22} obtenido={res.estado}")
    total = len(cases)
    rate = ok / total
    print(f"\nAcierto: {ok}/{total} = {rate:.0%} (objetivo {TARGET:.0%}) · errores de la API: {errors}")
    for (exp, got), n in confusion.most_common():
        print(f"  {exp} -> {got}: {n}")
    return 0 if rate >= TARGET and errors == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
