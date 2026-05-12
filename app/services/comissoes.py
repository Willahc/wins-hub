"""Cálculo e registro de comissões de representante.

Tipos de comissão:
- INICIAL:    1ª assinatura paga do prestador → pct_inicial (default 50%)
- RECORRENTE: renovações mensais por comissao_meses_recorrencia (default 12) → pct_recorrente (default 25%)
- AVULSO:     compras pontuais (créditos, desbloqueios) → pct_inicial (default 50%, sempre)

Janela de chargeback: status='pendente' por 30 dias após criação, depois 'disponivel'.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional
from psycopg2.extras import RealDictCursor

log = logging.getLogger(__name__)


def eh_primeira_assinatura(conn, prestador_id) -> bool:
    """Decide se este é o primeiro pagamento aprovado de assinatura desse prestador.

    True → rep ganha INICIAL (apenas uma vez na vida do prestador).
    False → já houve assinatura paga antes, mesmo que tenha cancelado e voltado.

    Importante: deve ser chamada APÓS o UPDATE pagamentos.mp_status='approved' do
    pagamento corrente. Assim, count==1 significa que essa é a primeira aprovada.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM pagamentos
            WHERE prestador_id=%s
              AND tipo='plano'
              AND mp_status='approved'
        """, (prestador_id,))
        n = cur.fetchone()[0]
    return n <= 1


def calcular_comissao_lead(conn, prestador_id, valor_centavos: int,
                           tipo: str, recorrencia_mes: Optional[int] = None,
                           lead_outbound_id_explicit=None):
    """Cria registro em comissoes se prestador foi indicado por algum rep.

    Args:
        conn: conexão psycopg2 (mesma da transação chamadora — não commita aqui)
        prestador_id: UUID do prestador que pagou
        valor_centavos: valor base do pagamento em centavos
        tipo: 'INICIAL' | 'RECORRENTE' | 'AVULSO'
        recorrencia_mes: 1..12 quando tipo='RECORRENTE'; None para outros
        lead_outbound_id_explicit: se passado, usa direto sem buscar (pra retrocompat)

    Returns:
        UUID da comissão criada, ou None se não houver lead/rep vinculado (silencioso).
    """
    if tipo not in ('INICIAL', 'RECORRENTE', 'AVULSO'):
        raise ValueError(f"tipo inválido: {tipo}")
    if not valor_centavos or valor_centavos <= 0:
        log.warning("valor_centavos não-positivo (%s) — comissão não criada", valor_centavos)
        return None

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # 1) Resolver lead vinculado
        if lead_outbound_id_explicit:
            cur.execute("""
                SELECT id, representante_id FROM leads_outbound
                WHERE id=%s AND representante_id IS NOT NULL
            """, (lead_outbound_id_explicit,))
        else:
            cur.execute("""
                SELECT id, representante_id FROM leads_outbound
                WHERE prestador_id=%s AND representante_id IS NOT NULL
                ORDER BY cadastrou_em DESC NULLS LAST, criado_em DESC
                LIMIT 1
            """, (prestador_id,))
        lead = cur.fetchone()
        if not lead:
            return None  # sem rep, sem comissão (silencioso)

        # 2) Buscar % do rep
        cur.execute("""
            SELECT COALESCE(comissao_pct_inicial, 50) AS pct_inicial,
                   COALESCE(comissao_pct_recorrente, 25) AS pct_recorrente
            FROM prestadores WHERE id=%s
        """, (lead['representante_id'],))
        rep = cur.fetchone()
        if not rep:
            log.warning("rep %s não encontrado", lead['representante_id'])
            return None

        # 3) % aplicável
        if tipo == 'RECORRENTE':
            pct = rep['pct_recorrente']
        else:
            pct = rep['pct_inicial']  # INICIAL e AVULSO usam o mesmo %

        valor_comissao = (valor_centavos * pct) // 100
        disponivel_em = datetime.utcnow() + timedelta(days=30)

        # 4) INSERT comissão
        cur.execute("""
            INSERT INTO comissoes (
                representante_id, lead_outbound_id, prestador_id,
                tipo, valor_base_centavos, pct_aplicado, valor_comissao_centavos,
                status, disponivel_em, recorrencia_mes, referente_a, criado_em
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'pendente', %s, %s, CURRENT_DATE, NOW())
            RETURNING id
        """, (lead['representante_id'], lead['id'], prestador_id,
              tipo, valor_centavos, pct, valor_comissao,
              disponivel_em, recorrencia_mes))
        cid = cur.fetchone()['id']
        log.info("Comissão %s criada: tipo=%s prestador=%s rep=%s valor=%d cents",
                 cid, tipo, prestador_id, lead['representante_id'], valor_comissao)
        return cid
