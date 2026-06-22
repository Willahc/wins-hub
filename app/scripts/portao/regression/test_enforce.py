#!/usr/bin/env python3
"""test_enforce.py — cobre o caminho de PRODUÇÃO (filtrar_e_enriquecer + guardrail),
que o test_casos não exercita. Sai !=0 em falha. Sem chamadas externas."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import portao

CNPJ_OK = "45990181000189"  # válido (Bosch matriz)


def tup(nome, setor, valor, cnpj=CNPJ_OK):
    # tupla no formato dos captadores oficiais (idx: nome=1..valor=7); pad até 8
    return ("idext", nome, "Empresa X", cnpj, setor, "Mun", "SP", valor)


def main():
    conn = portao.get_conn()
    falhas = []

    # 1) filtra não-obra (CFEM) e mantém obra real
    lote = [tup("Construção de nova usina greenfield", "ENERGIA", 2e8) for _ in range(8)] \
         + [tup("Compensação financeira CFEM mineração", "MINERACAO", 2e8) for _ in range(2)]
    out = portao.filtrar_e_enriquecer(lote, "teste", conn, web_search_fn=None)
    if len(out) != 8:
        falhas.append(f"filtro não-obra: manteve {len(out)} (esperado 8)")

    # 2) guardrail: lote todo inválido (setor OUTRO) -> fail-open devolve original
    lote_ruim = [tup("Obra", "OUTRO", 2e8) for _ in range(12)]
    out2 = portao.filtrar_e_enriquecer(lote_ruim, "teste", conn, web_search_fn=None)
    if len(out2) != 12:
        falhas.append(f"guardrail fail-open: devolveu {len(out2)} (esperado 12 = passthrough)")

    # 3) kill-switch por arquivo
    flag = portao._DISABLE_FLAG
    try:
        open(flag, "w").close()
        out3 = portao.filtrar_e_enriquecer(lote, "teste", conn, web_search_fn=None)
        if len(out3) != len(lote):
            falhas.append(f"kill-switch arquivo: filtrou {len(out3)} (esperado {len(lote)} passthrough)")
    finally:
        if os.path.exists(flag):
            os.remove(flag)

    # 4) enrich externo OFF por padrão (sem PORTAO_ENRICH_SERPER) -> nunca chama web_search_fn
    chamou = {"n": 0}
    def ws(_obra):
        chamou["n"] += 1
        return {"dominio": "x.com"}
    os.environ.pop("PORTAO_ENRICH_SERPER", None)
    portao.filtrar_e_enriquecer([tup("Construção nova fábrica", "INDUSTRIAL", 3e8, cnpj="12345678000195")],
                                "teste", conn, web_search_fn=ws)
    if chamou["n"] != 0:
        falhas.append(f"enrich deveria estar OFF por padrão, mas web_search_fn foi chamado {chamou['n']}x")

    for f in falhas:
        print(f"  [FALHA] {f}")
    print(f"\n{4-len(falhas)}/4 testes de enforce OK")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
