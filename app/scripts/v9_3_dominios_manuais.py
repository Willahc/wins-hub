"""V9.3 - Domínios validados manualmente no chat (sessão 2026-05-14).

Registra a inserção de 17 domínios em empresa_dominios via INSERT ... ON CONFLICT
DO UPDATE (preserva manual_chat_v6/v7 anteriores). Executado no chat; este script
serve como rastro auditável da batch.

Resumo:
  - 10 rows com CNPJ explícito (Camil, Clariant, Superfrio, Supremo Carnes,
    Adeste, Bello, Boa Vista, Vitão, Rivelli, Zanchetta).
  - 8 rows tentados via fornecedores ILIKE (Cocal, Lanxess, Air Products,
    Unipar, White Martins/Linde, Heineken, Gelnex) — 0 hits (não estavam em
    fornecedores; matches eram ruído tipo LINDEMBERG).
  - 7 rows residuais resolvidos via BrasilAPI + Serper (NS Alimentos,
    Laticínios Porto Alegre, Linave, Maringá Energia, Arroba, Gelico, Viva).
  - 2 pendentes: Nati Alimentos (grafia divergente "Natto"), Santa Rosa
    Transportes AM (sem site oficial claro).

Cobertura OURO/PRATA com domínio após batch:
  - OURO: 124/168 (73.8%)
  - PRATA: 39/46 (84.8%)

confianca=4: Boa Vista Alimentos (recuperação judicial), Adeste.
confianca=3: Viva Alimentos PE (vivavidaalimentos.com.br, nome diverge).
"""

ROWS_COM_CNPJ = [
    ('64904295000103', 'Camil Alimentos', 'camilalimentos.com.br', 5),
    ('31452113000151', 'Clariant Brasil', 'clariant.com.br', 5),
    ('02666906000175', 'Superfrio', 'superfrio.com.br', 5),
    ('07540084000103', 'Supremo Carnes', 'supremocarnes.com.br', 5),
    ('44885291002838', 'Adeste Industria', 'adeste.com.br', 4),
    ('08201770000538', 'Bello Alimentos Frango Bello', 'belloalimentos.com.br', 5),
    ('37356854000115', 'Boa Vista Alimentos', 'boavistaalimentos.com.br', 4),
    ('04869719000548', 'Vitão Alimentos', 'vitao.com.br', 5),
    ('21005582000179', 'Rivelli Alimentos', 'rivelli.ind.br', 5),
    ('33920401000119', 'Zanchetta Industria de Alimentos', 'zanchetta.com.br', 5),
]

ROWS_RESIDUAIS_BRASILAPI_SERPER = [
    ('08070362000160', 'NS Alimentos', 'nsalimentos.com.br', 5),
    ('66301334000707', 'Laticínios Porto Alegre', 'laticiniosportoalegre.com.br', 5),
    ('84156249000180', 'Linave Navegação', 'linave.com.br', 5),
    ('31858600000119', 'Maringá Energia (Grupo Maringá)', 'grupomaringa.com.br', 5),
    ('26657341000128', 'Arroba Alimentos', 'arrobaalimentos.com.br', 5),
    ('65133423000125', 'Gelico Gelatinas', 'gelico.com.br', 5),
    ('16776645000150', 'Viva Alimentos PE', 'vivavidaalimentos.com.br', 3),
]

PENDENTES = [
    ('10407857000184', 'Nati Alimentos', 'grafia divergente; Serper sugere nattoalimentos.com.br'),
    ('12347939000106', 'Santa Rosa Transportes AM', 'sem site oficial localizável'),
]
