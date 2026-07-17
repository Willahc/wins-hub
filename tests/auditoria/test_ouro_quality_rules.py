"""Testes de governança de qualidade OURO."""
import importlib.util
from pathlib import Path

def load():
    for p in [
        Path('/tmp/auditar_qualidade_ouro.py'),
        Path('/app/scripts/auditoria/auditar_qualidade_ouro.py'),
        Path('/root/wins_hub/scripts/auditoria/auditar_qualidade_ouro.py'),
    ]:
        if p.exists():
            spec = importlib.util.spec_from_file_location('aq', str(p))
            m = importlib.util.module_from_spec(spec)
            # avoid loading ouro dependency path issues - only need classify if available
            try:
                spec.loader.exec_module(m)
                return m
            except Exception as e:
                print('load fail', p, e)
                continue
    raise RuntimeError('module not found')

def test_email_generico_nao_forte():
    m = load()
    cls = m.classify_email_source(
        email='contato@empresa.com.br', decisor_nome='Maria Silva', obra_cnpj='123',
        email_status='valid', email_smtp='', fonte_decisor='', email_audit_fonte=None,
        sibling_same_name=True, sibling_same_cnpj=True, domain_matches_empresa_dominios=True,
    )
    assert cls in ('INFERIDO', 'REUSO_INTERNO_INSUFICIENTE'), cls

def test_email_outra_pessoa_conflito():
    m = load()
    cls = m.classify_email_source(
        email='filipe.fernandes@empresa.com.br', decisor_nome='Flavio Barcellos', obra_cnpj='1',
        email_status='valid', email_smtp='', fonte_decisor='', email_audit_fonte='x',
        sibling_same_name=True, sibling_same_cnpj=True, domain_matches_empresa_dominios=True,
    )
    assert cls == 'CONFLITANTE', cls

def test_reuso_forte():
    m = load()
    cls = m.classify_email_source(
        email='maria.silva@empresa.com.br', decisor_nome='Maria Silva', obra_cnpj='1',
        email_status='valid', email_smtp='', fonte_decisor='reuso_cnpj', email_audit_fonte='irmao',
        sibling_same_name=True, sibling_same_cnpj=True, domain_matches_empresa_dominios=True,
    )
    assert cls == 'REUSO_INTERNO_COM_EVIDENCIA_FORTE', cls

if __name__ == '__main__':
    test_email_generico_nao_forte()
    test_email_outra_pessoa_conflito()
    test_reuso_forte()
    print('OK 3 tests')
