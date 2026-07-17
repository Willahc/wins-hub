#!/usr/bin/env python3
"""Reprocessa wins_v2.pipeline_falhas e pipeline_inbox (sem timer).

Uso:
  MASTER_PIPELINE_V2_ENABLED=true python reprocessar_falhas_pipeline_v2.py --limit 50
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, "/app")

from services.master_pipeline_v2 import (  # noqa: E402
    ORIGEM_REPROC,
    _connect,
    is_master_pipeline_enabled,
    processar_captura_para_master_safe,
)


def reprocess_falhas(limit: int) -> Dict[str, Any]:
    if not is_master_pipeline_enabled():
        return {"status": "DISABLED"}
    conn = _connect()
    stats = {"lidas": 0, "ok": 0, "erro": 0, "duplicado": 0}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, fonte, captador, id_externo, namespace, payload, contexto
                  FROM wins_v2.pipeline_falhas
                 WHERE status = 'pendente'
                 ORDER BY criado_em ASC
                 LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        for row in rows:
            fid, fonte, captador, id_externo, namespace, payload, contexto = row
            stats["lidas"] += 1
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE wins_v2.pipeline_falhas SET status='reprocessando', tentativas=tentativas+1, atualizado_em=now() WHERE id=%s",
                    (fid,),
                )
            conn.commit()
            ctx = dict(contexto or {})
            ctx["namespace"] = namespace or "default"
            ctx["origem_marcador"] = ORIGEM_REPROC
            result = processar_captura_para_master_safe(
                fonte=fonte or "desconhecida",
                captador=captador or "desconhecido",
                id_externo=id_externo or f"falha:{fid}",
                payload=payload or {},
                contexto=ctx,
            )
            with conn.cursor() as cur:
                if result.get("status") in {"OK", "DUPLICADO", "DISABLED"}:
                    cur.execute(
                        "UPDATE wins_v2.pipeline_falhas SET status='resolvido', resolvido_em=now(), atualizado_em=now() WHERE id=%s",
                        (fid,),
                    )
                    if result.get("status") == "DUPLICADO":
                        stats["duplicado"] += 1
                    else:
                        stats["ok"] += 1
                else:
                    cur.execute(
                        "UPDATE wins_v2.pipeline_falhas SET status='pendente', erro=%s, atualizado_em=now() WHERE id=%s",
                        (str(result)[:2000], fid),
                    )
                    stats["erro"] += 1
            conn.commit()
    finally:
        conn.close()
    return stats


def reprocess_inbox(limit: int) -> Dict[str, Any]:
    if not is_master_pipeline_enabled():
        return {"status": "DISABLED"}
    conn = _connect()
    stats = {"lidas": 0, "ok": 0, "erro": 0, "duplicado": 0}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, v1_obra_id, fonte, id_externo, payload_minimo
                  FROM wins_v2.pipeline_inbox
                 WHERE status = 'pendente'
                 ORDER BY criado_em ASC
                 LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        for row in rows:
            iid, v1_id, fonte, id_externo, payload = row
            stats["lidas"] += 1
            result = processar_captura_para_master_safe(
                fonte=fonte or "desconhecida",
                captador=f"captar_{fonte or 'inbox'}",
                id_externo=id_externo or f"inbox:{iid}",
                payload=payload or {},
                contexto={
                    "captura_id": str(v1_id) if v1_id else None,
                    "v1_obra_id": str(v1_id) if v1_id else None,
                    "origem_marcador": ORIGEM_REPROC,
                },
            )
            with conn.cursor() as cur:
                if result.get("status") in {"OK", "DUPLICADO"}:
                    cur.execute(
                        "UPDATE wins_v2.pipeline_inbox SET status='processado', processado_em=now() WHERE id=%s",
                        (iid,),
                    )
                    stats["ok" if result.get("status") == "OK" else "duplicado"] += 1
                elif result.get("status") == "DISABLED":
                    cur.execute(
                        "UPDATE wins_v2.pipeline_inbox SET status='ignorado', processado_em=now() WHERE id=%s",
                        (iid,),
                    )
                else:
                    cur.execute(
                        "UPDATE wins_v2.pipeline_inbox SET status='erro', erro=%s WHERE id=%s",
                        (str(result)[:2000], iid),
                    )
                    stats["erro"] += 1
            conn.commit()
    finally:
        conn.close()
    return stats


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--inbox", action="store_true")
    p.add_argument("--falhas", action="store_true", default=True)
    args = p.parse_args()
    out: Dict[str, Any] = {}
    if args.falhas:
        out["falhas"] = reprocess_falhas(args.limit)
    if args.inbox:
        out["inbox"] = reprocess_inbox(args.limit)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
