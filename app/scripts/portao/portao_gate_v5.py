#!/usr/bin/env python3
"""Portão de Obras v5 — decisão APROVADA | REJEITADA | EM_ANALISE.

Não inventa dados. Não publica. Não altera captadores.
Reutiliza sinais de obra_classificacao.yaml e estrutura existente.

API:
  decidir_portao(obra: dict, *, conn=None, permitir_externo=False) -> Decisao
  processar_fila(limit=50) -> stats
  aplicar_decisao(obra_id, decisao, *, dry_run=False) -> None
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Paths
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_APP_PORTAO = Path("/app/scripts/portao")
if _APP_PORTAO.is_dir() and str(_APP_PORTAO) not in sys.path:
    sys.path.insert(0, str(_APP_PORTAO))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

PORTAO_VERSAO = "portao-v5.0.0"
VALOR_MINIMO = 100_000.0

# ---------------------------------------------------------------------------
# Lexicons
# ---------------------------------------------------------------------------

INTERVENCAO_FISICA = [
    "construcao", "construção", "implantacao", "implantação", "ampliacao", "ampliação",
    "reforma estrutural", "retrofit", "montagem", "instalacao de", "instalação de",
    "pavimentacao", "pavimentação", "terraplenagem", "duplicacao", "duplicação",
    "recuperacao estrutural", "recuperação estrutural", "execucao de obra",
    "execução de obra", "execucao de infraestrutura", "execução de infraestrutura",
    "construcao de unidade", "construção de unidade", "modernizacao fisica",
    "modernização física", "obras de", "obra de", "edificacao", "edificação",
    "infraestrutura", "greenfield", "brownfield", "nova planta", "nova usina",
    "nova fabrica", "nova fábrica", "nova unidade", "expansao", "expansão",
]

ATIVOS_FISICOS = [
    "edificio", "edifício", "hospital", "escola", "unidade industrial", "fabrica",
    "fábrica", "galpao", "galpão", "armazem", "armazém", "rodovia", "ponte",
    "tunel", "túnel", "ferrovia", "porto", "terminal", "aeroporto", "subestacao",
    "subestação", "linha de transmissao", "linha de transmissão", "usina",
    "estacao", "estação", "saneamento", "tratamento de esgoto", "tratamento de agua",
    "tratamento de água", "barragem", "loteamento", "data center", "planta",
    "instalacao produtiva", "instalação produtiva", "complexo", "empreendimento",
    "park", "parque solar", "parque eolico", "parque eólico", "ufv", "pch", "cgh",
    "linhao", "linhao", "ducto", "gasoduto", "oleoduto", "mineroduto",
]

REJEICAO_CLARA = [
    "software", "licenca de software", "licença de software", "saas",
    "consultoria", "auditoria", "treinamento", "capacitacao", "capacitação",
    "publicidade", "propaganda", "alimentacao", "alimentação", "limpeza predial",
    "servico de limpeza", "serviço de limpeza", "vigilancia", "vigilância",
    "seguranca patrimonial", "segurança patrimonial", "transporte de passageiros",
    "locacao de veiculos", "locação de veículos", "terceirizacao de mao de obra",
    "terceirização de mão de obra", "servico juridico", "serviço jurídico",
    "servico contabil", "serviço contábil", "credenciamento",
    "contratacao de pessoal", "contratação de pessoal", "folha de pagamento",
    "capital de giro", "aquisicao de participacao", "aquisição de participação",
    "royalty", "royalties", "cfem", "renovabio", "apoio a pesquisa",
    "apoio à pesquisa", "p&d", "pesquisa e desenvolvimento",
    "compra de material", "aquisicao de material", "aquisição de material",
    "fornecimento de materiais", "aquisicao de equipamentos",
    "aquisição de equipamentos", "sem instalacao", "sem instalação",
    "licenca operacional", "licença operacional", "outorga de uso",
]

MANUTENCAO_ROTINA = [
    "manutencao preventiva", "manutenção preventiva", "manutencao corretiva",
    "manutenção corretiva", "manutencao predial", "manutenção predial",
    "manutencao de rotina", "manutenção de rotina", "servicos de manutencao",
    "serviços de manutenção",
]

MANUTENCAO_ESTRUTURAL = [
    "manutencao estrutural", "manutenção estrutural", "recuperacao estrutural",
    "recuperação estrutural", "reforco estrutural", "reforço estrutural",
    "reabilitacao", "reabilitação", "reforma estrutural",
]

SETOR_ELEGIVEL = {
    "ENERGIA", "INFRAESTRUTURA", "INDUSTRIAL", "MINERACAO", "LOGISTICO",
    "PORTUARIO", "SANEAMENTO", "PETROLEO_GAS", "CONSTRUCAO", "CONSTRUCAO_CIVIL",
    "TRANSPORTE", "IMOBILIARIO", "AGROINDUSTRIAL", "SUCROENERGETICO",
    "LATICINIOS", "PAPEL_CELULOSE", "AUTOMOTIVO", "OUTRO",  # OUTRO -> EM_ANALISE
}

VALOR_AGREGADO_SINAIS = [
    "programa de", "pacote de", "credenciamento", "teto de", "emissao de debenture",
    "emissão de debênture", "faturamento", "valor global do programa",
    "linha de credito", "linha de crédito",
]


def _norm(s: Any) -> str:
    s = unicodedata.normalize("NFKD", str(s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _has_any(texto: str, termos: Sequence[str]) -> Optional[str]:
    for t in termos:
        if _norm(t) in texto:
            return t
    return None


def _to_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float, Decimal)):
        return float(v)
    try:
        return float(str(v).replace(".", "").replace(",", ".")) if "," in str(v) and "." in str(v) else float(str(v).replace(",", "."))
    except Exception:
        return None


@dataclass
class Decisao:
    status_portao: str
    confianca: float
    motivo: str
    regra_aplicada: str
    criterios_atendidos: List[str] = field(default_factory=list)
    criterios_ausentes: List[str] = field(default_factory=list)
    evidencias: List[Dict[str, Any]] = field(default_factory=list)
    campos_analisados: Dict[str, Any] = field(default_factory=dict)
    versao: str = PORTAO_VERSAO
    dup_id: Optional[str] = None
    fase_real_obra: str = "DESCONHECIDA"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def mapear_fase_real(fase: Optional[str], status_licenca: Optional[str] = None) -> str:
    f = _norm(fase or "")
    sl = _norm(status_licenca or "")
    blob = f"{f} {sl}"
    if any(x in blob for x in ("planej", "projeto basico", "projeto executivo")):
        return "PLANEJAMENTO"
    if any(x in blob for x in ("licenc", "lp", "li", "lo")):
        return "LICENCIAMENTO"
    if "licit" in blob or "concorrencia" in blob or "pregao" in blob:
        return "LICITACAO"
    if "contrat" in blob:
        return "CONTRATACAO"
    if any(x in blob for x in ("execuc", "em_execucao", "construcao", "obras")):
        return "EM_EXECUCAO"
    if "paralis" in blob:
        return "PARALISADA"
    if any(x in blob for x in ("conclu", "operac", "entregue")):
        return "CONCLUIDA"
    return "DESCONHECIDA"


def decidir_portao(obra: Dict[str, Any], *, conn: Any = None, permitir_externo: bool = False) -> Decisao:
    """Aplica a matriz do Portão v5 sobre um dict de obra (sem side-effects)."""
    nome = obra.get("nome") or obra.get("titulo") or ""
    desc = obra.get("descricao") or obra.get("descricao_publica") or ""
    setor = (obra.get("setor") or "").upper() or None
    fonte = obra.get("fonte") or ""
    fonte_tipo = (obra.get("fonte_tipo") or "OFICIAL").upper()
    valor = _to_float(obra.get("valor_estimado"))
    capex_fonte = obra.get("capex_fonte")
    url = obra.get("url_fonte") or ""
    cnpj = re.sub(r"\D", "", str(obra.get("cnpj") or "")) or None
    empresa = obra.get("empresa")
    id_externo = obra.get("id_externo")
    fase = obra.get("fase")
    texto = _norm(f"{nome} {desc}")
    campos = {
        "nome": nome[:200],
        "setor": setor,
        "fonte": fonte,
        "fonte_tipo": fonte_tipo,
        "valor_estimado": valor,
        "capex_fonte": capex_fonte,
        "cnpj": cnpj,
        "empresa": (str(empresa)[:120] if empresa else None),
        "url_fonte": (url[:200] if url else None),
        "fase": fase,
    }
    fase_real = mapear_fase_real(fase, obra.get("status_licenca"))
    atendidos: List[str] = []
    ausentes: List[str] = []
    evidencias: List[Dict[str, Any]] = []

    # --- Duplicidade (quando conn disponível) ---
    dup_id = None
    if conn is not None and cnpj and nome:
        try:
            from portao import acha_duplicata  # type: ignore

            dup_id = acha_duplicata(
                conn,
                nome,
                cnpj,
                exclude_id=obra.get("id") or obra.get("_self_id"),
                exclude_id_externo=id_externo,
            )
        except Exception:
            dup_id = None
    if dup_id:
        return Decisao(
            status_portao="REJEITADA",
            confianca=0.95,
            motivo=f"duplicata_empreendimento:{dup_id}",
            regra_aplicada="DEDUP_EMPREENDIMENTO",
            criterios_atendidos=["duplicata_detectada"],
            criterios_ausentes=[],
            evidencias=[{"tipo": "duplicata", "obra_id": dup_id}],
            campos_analisados=campos,
            dup_id=dup_id,
            fase_real_obra=fase_real,
        )

    # --- Rejeições automáticas claras ---
    hit_rej = _has_any(texto, REJEICAO_CLARA)
    if hit_rej:
        return Decisao(
            status_portao="REJEITADA",
            confianca=0.92,
            motivo=f"nao_obra_clara:{hit_rej}",
            regra_aplicada="REJEICAO_AUTOMATICA_NAO_OBRA",
            criterios_atendidos=[],
            criterios_ausentes=["intervencao_fisica", "ativo_fisico"],
            evidencias=[{"tipo": "lexicon_rejeicao", "termo": hit_rej}],
            campos_analisados=campos,
            fase_real_obra=fase_real,
        )

    # Manutenção rotineira sem sinal estrutural
    hit_man_rot = _has_any(texto, MANUTENCAO_ROTINA)
    hit_man_est = _has_any(texto, MANUTENCAO_ESTRUTURAL)
    if hit_man_rot and not hit_man_est and not _has_any(texto, INTERVENCAO_FISICA):
        return Decisao(
            status_portao="REJEITADA",
            confianca=0.88,
            motivo=f"manutencao_rotineira:{hit_man_rot}",
            regra_aplicada="REJEICAO_MANUTENCAO_ROTINA",
            criterios_atendidos=[],
            criterios_ausentes=["intervencao_fisica_relevante"],
            evidencias=[{"tipo": "lexicon_manutencao", "termo": hit_man_rot}],
            campos_analisados=campos,
            fase_real_obra=fase_real,
        )

    # Valor baixo confiável
    if valor is not None and valor < VALOR_MINIMO and not (capex_fonte and str(capex_fonte).startswith("ESTIMATIVA")):
        # estimativas fracas + valor baixo: rejeita se valor explícito < 100k
        if valor < VALOR_MINIMO:
            return Decisao(
                status_portao="REJEITADA",
                confianca=0.97,
                motivo=f"valor_abaixo_minimo:{valor:.0f}",
                regra_aplicada="REJEICAO_VALOR_MINIMO",
                criterios_atendidos=[],
                criterios_ausentes=["valor_elegivel_100k"],
                evidencias=[{"tipo": "valor", "valor_estimado": valor, "minimo": VALOR_MINIMO}],
                campos_analisados=campos,
                fase_real_obra=fase_real,
            )

    # --- Blocos de aprovação ---
    # A. Intervenção física
    hit_int = _has_any(texto, INTERVENCAO_FISICA) or hit_man_est
    if hit_int:
        atendidos.append("intervencao_fisica")
        evidencias.append({"tipo": "intervencao", "termo": hit_int})
    else:
        ausentes.append("intervencao_fisica")

    # B. Ativo físico
    hit_ativo = _has_any(texto, ATIVOS_FISICOS)
    if hit_ativo:
        atendidos.append("ativo_fisico")
        evidencias.append({"tipo": "ativo", "termo": hit_ativo})
    else:
        ausentes.append("ativo_fisico")

    # C. Setor elegível
    if setor and setor in SETOR_ELEGIVEL and setor != "OUTRO":
        atendidos.append("setor_elegivel")
        evidencias.append({"tipo": "setor", "setor": setor})
    elif setor == "OUTRO" or not setor:
        ausentes.append("setor_elegivel")
    else:
        # setor desconhecido mas presente -> análise
        ausentes.append("setor_elegivel_confirmado")

    # D. Valor elegível
    valor_agregado = _has_any(texto, VALOR_AGREGADO_SINAIS)
    if valor is not None and valor >= VALOR_MINIMO and not valor_agregado:
        # capex artificial / estimativa sem obra: se só estimativa e sem intervenção -> não conta pleno
        if capex_fonte and str(capex_fonte).startswith("ESTIMATIVA") and not hit_int:
            ausentes.append("valor_elegivel_confiavel")
            evidencias.append({"tipo": "valor_estimativa_fraca", "capex_fonte": capex_fonte, "valor": valor})
        else:
            atendidos.append("valor_elegivel_100k")
            evidencias.append({"tipo": "valor", "valor_estimado": valor, "capex_fonte": capex_fonte})
    elif valor is None:
        ausentes.append("valor_elegivel_100k")
    elif valor_agregado:
        ausentes.append("valor_obra_individual")
        evidencias.append({"tipo": "valor_agregado_suspeito", "termo": valor_agregado, "valor": valor})
    else:
        ausentes.append("valor_elegivel_100k")

    # Fonte rastreável
    if url or fonte:
        atendidos.append("fonte_rastreavel")
        evidencias.append({"tipo": "fonte", "fonte": fonte, "url": bool(url)})
    else:
        ausentes.append("fonte_rastreavel")

    # --- Decisão ---
    blocos_ok = {
        "intervencao_fisica": "intervencao_fisica" in atendidos,
        "ativo_fisico": "ativo_fisico" in atendidos,
        "setor_elegivel": "setor_elegivel" in atendidos,
        "valor_elegivel_100k": "valor_elegivel_100k" in atendidos,
    }
    n_ok = sum(1 for v in blocos_ok.values() if v)

    # APROVADA: 4 blocos OU (intervenção+ativo+valor e setor ok/conhecido)
    if n_ok >= 4 or (
        blocos_ok["intervencao_fisica"]
        and blocos_ok["ativo_fisico"]
        and blocos_ok["valor_elegivel_100k"]
        and (blocos_ok["setor_elegivel"] or (setor and setor != "OUTRO"))
    ):
        conf = 0.70 + 0.07 * n_ok
        if "fonte_rastreavel" in atendidos:
            conf = min(0.98, conf + 0.05)
        return Decisao(
            status_portao="APROVADA",
            confianca=round(conf, 4),
            motivo="criterios_portao_atendidos",
            regra_aplicada="APROVACAO_AUTOMATICA_4_BLOCOS",
            criterios_atendidos=atendidos,
            criterios_ausentes=ausentes,
            evidencias=evidencias,
            campos_analisados=campos,
            fase_real_obra=fase_real,
        )

    # Notícia vaga / serviço genérico de engenharia
    if fonte_tipo == "NOTICIA" and n_ok < 3:
        return Decisao(
            status_portao="EM_ANALISE",
            confianca=0.45,
            motivo="noticia_sem_evidencia_suficiente",
            regra_aplicada="ANALISE_NOTICIA",
            criterios_atendidos=atendidos,
            criterios_ausentes=ausentes,
            evidencias=evidencias,
            campos_analisados=campos,
            fase_real_obra=fase_real,
        )

    if "servicos de engenharia" in texto or "serviços de engenharia" in texto or "servico de engenharia" in texto:
        if not hit_int:
            return Decisao(
                status_portao="EM_ANALISE",
                confianca=0.50,
                motivo="servico_engenharia_sem_escopo_fisico",
                regra_aplicada="ANALISE_SERVICO_ENGENHARIA",
                criterios_atendidos=atendidos,
                criterios_ausentes=ausentes,
                evidencias=evidencias,
                campos_analisados=campos,
                fase_real_obra=fase_real,
            )

    # Default: possibilidade sem evidência suficiente
    return Decisao(
        status_portao="EM_ANALISE",
        confianca=round(0.35 + 0.1 * n_ok, 4),
        motivo="evidencia_insuficiente_para_aprovacao",
        regra_aplicada="ANALISE_PADRAO",
        criterios_atendidos=atendidos,
        criterios_ausentes=ausentes,
        evidencias=evidencias,
        campos_analisados=campos,
        fase_real_obra=fase_real,
    )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _connect():
    import psycopg2

    return psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "wins_hub"),
        user=os.getenv("DB_USER", "wins_app"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def flag_on(conn, chave: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT wins_v2.portao_flag_on(%s)", (chave,))
        return bool(cur.fetchone()[0])


def registrar_decisao(
    conn,
    *,
    obra_id: str,
    status_anterior: Optional[str],
    decisao: Decisao,
    origem: str = "regra",
    usuario: str = "portao_automatico",
    captura_id: Optional[str] = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO wins_v2.portao_decisoes (
                obra_id, captura_id, status_anterior, status_novo, regra_aplicada,
                versao_portao, confianca, motivo, criterios_atendidos, criterios_ausentes,
                evidencias, campos_analisados, origem_decisao, usuario_ou_agente
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s::jsonb, %s::jsonb,
                %s::jsonb, %s::jsonb, %s, %s
            ) RETURNING id
            """,
            (
                obra_id,
                captura_id,
                status_anterior,
                decisao.status_portao,
                decisao.regra_aplicada,
                decisao.versao,
                decisao.confianca,
                decisao.motivo,
                json.dumps(decisao.criterios_atendidos, ensure_ascii=False),
                json.dumps(decisao.criterios_ausentes, ensure_ascii=False),
                json.dumps(decisao.evidencias, ensure_ascii=False, default=str),
                json.dumps(decisao.campos_analisados, ensure_ascii=False, default=str),
                origem,
                usuario,
            ),
        )
        return int(cur.fetchone()[0])


def aplicar_decisao(
    conn,
    obra_id: str,
    decisao: Decisao,
    *,
    dry_run: bool = False,
    origem: str = "regra",
) -> Dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status_portao, visivel, motivo_invisivel, classificacao_computed
              FROM public.obras WHERE id=%s
            """,
            (obra_id,),
        )
        row = cur.fetchone()
        if not row:
            return {"status": "OBRA_AUSENTE", "obra_id": obra_id}
        status_ant, visivel_ant, motivo_ant, tier = row

    if dry_run:
        return {
            "status": "DRY_RUN",
            "obra_id": obra_id,
            "status_anterior": status_ant,
            "decisao": decisao.to_dict(),
            "tier_atual": tier,
        }

    visivel_novo = decisao.status_portao == "APROVADA"
    motivo_novo = None if visivel_novo else (
        f"portao:{decisao.motivo}"[:200]
    )
    status_enr = "NAO_INICIADO"
    if decisao.status_portao == "APROVADA":
        status_enr = "EM_PROCESSAMENTO" if flag_on(conn, "AUTO_ENRICH_AFTER_GATE_ENABLED") else "NAO_INICIADO"
    elif decisao.status_portao == "REJEITADA":
        status_enr = "NAO_INICIADO"

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.obras SET
                status_portao = %s,
                status_enriquecimento = COALESCE(%s, status_enriquecimento),
                fase_real_obra = COALESCE(%s, fase_real_obra),
                portao_confianca = %s,
                portao_motivo = %s,
                portao_versao = %s,
                portao_decidido_em = now(),
                portao_criterios = %s::jsonb,
                portao_evidencias = %s::jsonb,
                visivel = %s,
                motivo_invisivel = %s
            WHERE id = %s
            """,
            (
                decisao.status_portao,
                status_enr,
                decisao.fase_real_obra,
                decisao.confianca,
                decisao.motivo[:500],
                decisao.versao,
                json.dumps(
                    {
                        "atendidos": decisao.criterios_atendidos,
                        "ausentes": decisao.criterios_ausentes,
                        "regra": decisao.regra_aplicada,
                    },
                    ensure_ascii=False,
                ),
                json.dumps(decisao.evidencias, ensure_ascii=False, default=str),
                visivel_novo,
                motivo_novo,
                obra_id,
            ),
        )
        did = registrar_decisao(
            conn,
            obra_id=obra_id,
            status_anterior=status_ant,
            decisao=decisao,
            origem=origem,
        )

        # Auto enrich após aprovação
        if decisao.status_portao == "APROVADA" and flag_on(conn, "AUTO_ENRICH_AFTER_GATE_ENABLED"):
            try:
                cur.execute(
                    """
                    INSERT INTO enrichment_queue (obra_id, capex)
                    SELECT id, COALESCE(valor_estimado, 0)
                      FROM public.obras WHERE id=%s
                    ON CONFLICT (obra_id) DO NOTHING
                    """,
                    (obra_id,),
                )
            except Exception:
                pass

        # Duplicata: manter evidência mas rejeitada
        if decisao.dup_id:
            cur.execute(
                """
                UPDATE public.obras
                   SET portao_motivo = portao_motivo || ' | vinculo_dup=' || %s
                 WHERE id=%s
                """,
                (decisao.dup_id, obra_id),
            )

    conn.commit()
    return {
        "status": "APLICADO",
        "obra_id": obra_id,
        "decisao_id": did,
        "status_portao": decisao.status_portao,
        "visivel": visivel_novo,
        "visivel_anterior": visivel_ant,
    }


def processar_fila(limit: int = 50) -> Dict[str, Any]:
    conn = _connect()
    stats = {"lidas": 0, "aprovada": 0, "rejeitada": 0, "em_analise": 0, "erro": 0}
    try:
        if not flag_on(conn, "PORTAO_OBRAS_ENABLED"):
            return {"status": "DISABLED", **stats}
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.id, f.obra_id, f.tentativas
                  FROM wins_v2.portao_fila f
                 WHERE f.status = 'pendente'
                   AND f.proxima_tentativa <= now()
                 ORDER BY f.criado_em ASC
                 LIMIT %s
                 FOR UPDATE SKIP LOCKED
                """,
                (limit,),
            )
            rows = cur.fetchall()
        for fid, obra_id, tent in rows:
            stats["lidas"] += 1
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE wins_v2.portao_fila SET status='processando', tentativas=tentativas+1, atualizado_em=now() WHERE id=%s",
                        (fid,),
                    )
                conn.commit()
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id::text, nome, descricao, descricao_publica, setor, fonte, fonte_tipo,
                               valor_estimado, capex_fonte, url_fonte, cnpj, empresa, id_externo,
                               fase, status_licenca, municipio, uf
                          FROM public.obras WHERE id=%s
                        """,
                        (obra_id,),
                    )
                    cols = [d[0] for d in cur.description]
                    row = cur.fetchone()
                if not row:
                    raise RuntimeError("obra ausente")
                obra = dict(zip(cols, row))
                dec = decidir_portao(obra, conn=conn)
                aplicar_decisao(conn, str(obra_id), dec, dry_run=False)
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE wins_v2.portao_fila SET status='concluido', processado_em=now(), atualizado_em=now() WHERE id=%s",
                        (fid,),
                    )
                conn.commit()
                key = dec.status_portao.lower()
                if key in stats:
                    stats[key] += 1
                elif dec.status_portao == "APROVADA":
                    stats["aprovada"] += 1
                elif dec.status_portao == "REJEITADA":
                    stats["rejeitada"] += 1
                elif dec.status_portao == "EM_ANALISE":
                    stats["em_analise"] += 1
            except Exception as exc:
                stats["erro"] += 1
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE wins_v2.portao_fila
                           SET status = CASE WHEN tentativas >= max_tentativas THEN 'desistido' ELSE 'pendente' END,
                               ultimo_erro = %s,
                               proxima_tentativa = now() + (interval '5 minutes' * tentativas),
                               atualizado_em = now()
                         WHERE id=%s
                        """,
                        (str(exc)[:2000], fid),
                    )
                    # marca ERRO_PORTAO se desistiu
                    cur.execute("SELECT status FROM wins_v2.portao_fila WHERE id=%s", (fid,))
                    st = cur.fetchone()[0]
                    if st == "desistido":
                        cur.execute(
                            """
                            UPDATE public.obras
                               SET status_portao='ERRO_PORTAO', visivel=false,
                                   motivo_invisivel='erro_portao',
                                   portao_motivo=%s, portao_decidido_em=now()
                             WHERE id=%s
                            """,
                            (str(exc)[:500], obra_id),
                        )
                conn.commit()
    finally:
        conn.close()
    return stats


def classificar_comercial_pos_enriquecimento(obra: Dict[str, Any], enrich: Dict[str, Any]) -> str:
    """Define OURO/PRATA/BRONZE/PIPELINE somente após tentativa de enriquecimento.

    Não é aplicada no Portão; chamada pelo fluxo pós-aprovação.
    """
    if obra.get("status_portao") != "APROVADA":
        return "PIPELINE"
    decisor = enrich.get("decisor") or {}
    tem_decisor = bool(decisor.get("nome") or obra.get("nivel1_nome"))
    email = decisor.get("email") or obra.get("nivel1_email")
    telefone = decisor.get("telefone") or obra.get("nivel1_telefone")
    empresa_ok = bool(obra.get("empresa") or enrich.get("razao"))
    contato_ok = bool(email or telefone)
    contato_conf = bool(email and (enrich.get("email_validado") or obra.get("nivel1_email_status") == "valido"))

    if tem_decisor and contato_ok and contato_conf and empresa_ok:
        return "OURO"
    if tem_decisor and empresa_ok:
        return "PRATA"
    if empresa_ok or tem_decisor:
        return "BRONZE"
    return "PIPELINE"  # obra confirmada, enriquecimento insuficiente


__all__ = [
    "decidir_portao",
    "aplicar_decisao",
    "processar_fila",
    "Decisao",
    "PORTAO_VERSAO",
    "VALOR_MINIMO",
    "classificar_comercial_pos_enriquecimento",
]
