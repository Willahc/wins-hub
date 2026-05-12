"""Prompts versionados (suffix _V1, _V2 para A/B)."""

FILTRO_EX_FUNCIONARIO_V1 = """Voce e analista verificando se profissional ainda trabalha em empresa-alvo.

INPUT:
Nome: {nome_pessoa}
Cargo declarado: {cargo_raw}
Snippet (do LinkedIn/Google): {snippet_origem}
Empresa-alvo: {empresa_alvo}

TAREFA: Determinar se TRABALHA ATUALMENTE na empresa-alvo.

INDICADORES de EX-funcionario:
- "ex-", "former", "anteriormente"
- Datas no passado (ex: "2016-2017", "8 meses ate 2024", "saiu em")
- "atualmente em [outra empresa]"
- Tempo verbal passado proximo a empresa-alvo

INDICADORES de ATUAL:
- "atualmente", "currently"
- Verbo presente sem qualificador temporal passado
- Cargo + empresa-alvo sem datas historicas

Em caso de AMBIGUIDADE -> confianca='baixa', trabalha_atualmente=true (conservador).

Responda APENAS JSON valido (sem markdown, sem ```):
{{
  "trabalha_atualmente": true/false,
  "confianca": "alta"|"media"|"baixa",
  "empresa_atual_inferida": "string ou null",
  "empresa_anterior_inferida": "string ou null",
  "razao": "explicacao 1 frase"
}}"""
