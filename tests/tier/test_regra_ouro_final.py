"""Testes da regra definitiva OURO (8 critérios; telefone não classifica)."""
import importlib.util
import sys
from pathlib import Path

# load script module
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "regra", ROOT / "scripts" / "auditoria" / "aplicar_regra_ouro_final.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def obra(**kw):
    base = {
        "id": "x",
        "cnpj": "37353051000107",
        "valor_estimado": 1_000_000,
        "empresa": "ACME SA",
        "classificacao_computed": "PRATA",
        "status_portao": "APROVADA",
    }
    base.update(kw)
    return base


def dec(**kw):
    base = {
        "nome": "Maria Silva",
        "cargo": "Gerente de Suprimentos",
        "email": "maria.silva@acme.com.br",
        "email_status": "valid",
        "email_smtp_status": None,
        "linkedin_url": "https://www.linkedin.com/in/maria-silva",
        "telefone": "11999999999",
        "hipotese_replicacao": None,
    }
    base.update(kw)
    return base


def test_oito_criterios_ouro():
    r = mod.calc_tier(obra(), [dec()])
    assert r["tier"] == "OURO"


def test_sem_email_validado_prata():
    r = mod.calc_tier(obra(), [dec(email=None, email_status=None)])
    assert r["tier"] == "PRATA"


def test_email_inferido_prata():
    r = mod.calc_tier(obra(), [dec(email_status="inferido")])
    assert r["tier"] == "PRATA"


def test_email_generico_prata():
    r = mod.calc_tier(obra(), [dec(email="contato@acme.com.br", email_status="valid")])
    assert r["tier"] == "PRATA"


def test_telefone_direto_sem_email_prata():
    r = mod.calc_tier(obra(), [dec(email=None, telefone="11988887777")])
    assert r["tier"] == "PRATA"
    assert r["best"]["telefone"]


def test_email_validado_sem_telefone_ouro():
    r = mod.calc_tier(obra(), [dec(telefone=None)])
    assert r["tier"] == "OURO"
    assert not r["best"]["telefone"]


def test_sem_linkedin_nao_ouro():
    r = mod.calc_tier(obra(), [dec(linkedin_url=None)])
    assert r["tier"] != "OURO"


def test_sem_capex_nao_ouro():
    r = mod.calc_tier(obra(valor_estimado=0), [dec()])
    assert r["tier"] != "OURO"


def test_sem_dominio_pessoal_nao_ouro():
    r = mod.calc_tier(obra(), [dec(email="maria.silva@gmail.com", email_status="valid")])
    assert r["tier"] != "OURO"


def test_sem_cnpj_nao_ouro():
    r = mod.calc_tier(obra(cnpj=None, empresa="ACME"), [dec()])
    # may be PRATA if empresa+decisor complete without cnpj - rule says CNPJ required for OURO
    assert r["tier"] != "OURO"


def test_decisor_sem_cargo_nao_ouro():
    r = mod.calc_tier(obra(), [dec(cargo="")])
    assert r["tier"] != "OURO"


def test_email_outra_pessoa_nao_ouro():
    r = mod.calc_tier(obra(), [dec(nome="Flavio Barcellos", email="filipe.fernandes@empresa.com.br", email_status="valid")])
    assert r["tier"] != "OURO"


def test_nominal_match_helpers():
    assert mod.email_nominal_match("Maria Silva", "maria.silva@x.com")
    assert not mod.email_nominal_match("Flavio Barcellos", "filipe.fernandes@x.com")


if __name__ == "__main__":
    import traceback
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("OK", name)
            except Exception as e:
                failed += 1
                print("FAIL", name, e)
                traceback.print_exc()
    sys.exit(1 if failed else 0)
