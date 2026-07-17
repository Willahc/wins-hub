#!/usr/bin/env python3
"""Adaptador minimo pos-gravacao V1 -> pipeline mestre V2.

Uso nos captadores (uma linha apos commit bem-sucedido):

    from _master_hook import notificar_master_v2
    notificar_master_v2(fonte=..., captador=..., id_externo=..., payload=..., captura_id=...)

Nunca propaga excecao. Nunca publica obra V2.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

logger = logging.getLogger("wins_v2.master_hook")


def _load_pipeline():
    try:
        from services.master_pipeline_v2 import apos_gravacao_v1, is_master_pipeline_enabled
        return apos_gravacao_v1, is_master_pipeline_enabled
    except Exception:
        pass
    # caminhos comuns: /app (container) e pacote local
    roots = [
        Path("/app"),
        Path(__file__).resolve().parent.parent,
        Path("/home/william/winshub_v2_pipeline_master"),
    ]
    for root in roots:
        services = root / "services"
        if services.is_dir():
            s = str(root)
            if s not in sys.path:
                sys.path.insert(0, s)
            try:
                from services.master_pipeline_v2 import (  # type: ignore
                    apos_gravacao_v1,
                    is_master_pipeline_enabled,
                )
                return apos_gravacao_v1, is_master_pipeline_enabled
            except Exception:
                continue
    return None, None


def notificar_master_v2(
    *,
    fonte: str,
    id_externo: str,
    payload: Any,
    captador: Optional[str] = None,
    captura_id: Any = None,
    contexto: Optional[Mapping[str, Any]] = None,
) -> dict:
    """Ponto unico chamado pelos captadores apos gravação V1 bem-sucedida."""
    try:
        apos, is_enabled = _load_pipeline()
        if apos is None:
            return {"status": "HOOK_UNAVAILABLE"}
        if is_enabled is not None and not is_enabled():
            return {"status": "DISABLED"}
        return apos(
            fonte=fonte,
            captador=captador or f"captar_{fonte}",
            id_externo=id_externo,
            payload=payload,
            captura_id=captura_id,
            contexto=contexto,
        )
    except Exception as exc:  # defesa em profundidade
        logger.exception("hook master V2 engoliu erro: %s", exc)
        return {"status": "HOOK_ERROR", "erro": str(exc)[:300]}
