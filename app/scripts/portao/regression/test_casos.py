#!/usr/bin/env python3
"""test_casos.py — roda portao.avaliar() contra casos_decisao.yaml e compara vereditos.
Usado pelo run_harness.sh (Fase 1+). Sai !=0 se algum caso falhar."""
import os, sys, yaml
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # acha portao.py
import portao

CASOS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "casos_decisao.yaml")
ASSERT_MAP = {
    "hunter_chamado_inline":   lambda v: v["hunter"]["inline"],
    "hunter_enfileirado_batch": lambda v: v["hunter"]["enfileirado"],
    "dominio_origem":          lambda v: v.get("origem_resolucao", {}).get("dominio"),
    "web_search_chamado":      lambda v: False,  # shadow nunca chama externo
}


def roda():
    conn = portao.get_conn()
    casos = yaml.safe_load(open(CASOS, encoding="utf-8"))["casos"]
    falhas = []
    for caso in casos:
        obra = dict(caso.get("obra") or {})
        if "haiku_extraiu" in caso:
            obra["haiku_extraiu"] = caso["haiku_extraiu"]
        fonte_meta = {"fonte": caso.get("fonte", ""), "fonte_tipo": caso.get("fonte_tipo", "OFICIAL")}
        esp = caso["espera"]
        try:
            v = portao.avaliar(obra, fonte_meta, conn)
        except Exception as e:
            falhas.append((caso["id"], f"exceção: {e!r}")); continue
        erro = None
        want_pass = esp["veredito"] == "PASSA"
        if v["passou"] != want_pass:
            erro = f"passou={v['passou']} esperado={want_pass} (motivo={v.get('motivo')})"
        elif not want_pass and "motivo" in esp and v.get("motivo") != esp["motivo"]:
            erro = f"motivo={v.get('motivo')} esperado={esp['motivo']}"
        elif "tier_min" in esp and portao.TIER_RANK.get(v["tier"], -1) < portao.TIER_RANK[esp["tier_min"]]:
            erro = f"tier={v['tier']} < min {esp['tier_min']}"
        elif "tier_esperado" in esp and v["tier"] != esp["tier_esperado"]:
            erro = f"tier={v['tier']} esperado={esp['tier_esperado']}"
        if not erro:
            for k, val in (esp.get("assert") or {}).items():
                got = ASSERT_MAP[k](v)
                if got != val:
                    erro = f"assert {k}={got} esperado={val}"; break
        print(f"  {'[OK]  ' if not erro else '[FALHA]'} {caso['id']}" + (f"  -> {erro}" if erro else ""))
        if erro:
            falhas.append((caso["id"], erro))
    print(f"\n{len(casos)-len(falhas)}/{len(casos)} casos OK")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(roda())
