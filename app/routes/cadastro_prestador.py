"""
Endpoint de auto-cadastro de prestador via CNPJ + BrasilAPI.
Fluxo: CNPJ -> BrasilAPI -> valida CNAE -> cria prestador com plano GRATUITO.
"""
import re
import logging
import bcrypt
import jwt
import os
from datetime import datetime, timedelta
from psycopg2.extras import RealDictCursor
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr

log = logging.getLogger(__name__)
JWT_SECRET = os.getenv("JWT_SECRET", "secret")


class CadastroReq(BaseModel):
    cnpj: str
    email: EmailStr
    senha: str


def build_router(get_conn, consultar_brasilapi):
    router = APIRouter(prefix="/api/prestadores", tags=["cadastro"])

    @router.post("/cadastrar")
    async def cadastrar_prestador(req: CadastroReq):
        # 1. Limpa CNPJ
        cnpj_limpo = re.sub(r"\D", "", req.cnpj)
        if len(cnpj_limpo) != 14:
            raise HTTPException(400, "CNPJ deve ter 14 digitos.")

        if len(req.senha) < 6:
            raise HTTPException(400, "Senha deve ter pelo menos 6 caracteres.")

        # 2. Confere se email ja existe
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM prestadores WHERE email=%s", (req.email,))
                if cur.fetchone():
                    raise HTTPException(400, "Email ja cadastrado.")
                cur.execute("SELECT 1 FROM prestadores WHERE cnpj=%s", (cnpj_limpo,))
                if cur.fetchone():
                    raise HTTPException(400, "CNPJ ja cadastrado.")

            # 3. Carrega CNAEs de interesse
            with conn.cursor() as cur:
                cur.execute("SELECT cnae FROM cnaes_interesse")
                cnaes_validos = {row[0] for row in cur.fetchall()}

            # 4. Consulta BrasilAPI (com cache)
            try:
                dados = consultar_brasilapi(cnpj_limpo)
            except Exception as e:
                log.exception(f"Erro BrasilAPI para {cnpj_limpo}")
                raise HTTPException(503, "Erro consultando dados do CNPJ. Tente novamente.")

            if not dados:
                raise HTTPException(404, "CNPJ nao encontrado na Receita Federal.")

            # 5. Valida CNAE
            cnae_principal = str(dados.get("cnae_fiscal", "")).strip()
            cnae_secundarios = [
                str(c.get("codigo", "")).strip()
                for c in dados.get("cnaes_secundarios", [])
            ]

            cnae_match = (
                cnae_principal in cnaes_validos
                or any(c in cnaes_validos for c in cnae_secundarios)
            )

            if not cnae_match:
                raise HTTPException(
                    400,
                    f"CNAE {cnae_principal} nao e elegivel para WiNS Hub. "
                    "Plataforma e dedicada a construcao, engenharia e infraestrutura."
                )

            # 6. Cria/atualiza empresa em fornecedores (consultar_brasilapi ja faz isso)
            # Confirma que tem
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT cnpj FROM fornecedores WHERE cnpj=%s", (cnpj_limpo,))
                if not cur.fetchone():
                    log.warning(f"Empresa {cnpj_limpo} nao foi salva pelo consultar_brasilapi")

            # 7. Cria prestador
            senha_hash = bcrypt.hashpw(req.senha.encode(), bcrypt.gensalt()).decode()
            nome_empresa = (
                dados.get("razao_social")
                or dados.get("nome_fantasia")
                or f"CNPJ {cnpj_limpo}"
            )

            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO prestadores
                        (nome_empresa, cnpj, email, senha_hash, plano, optin_em, optin_origem)
                    VALUES (%s, %s, %s, %s, 'GRATUITO', now(), 'auto-cadastro')
                    RETURNING id
                """, (nome_empresa, cnpj_limpo, req.email, senha_hash))
                prestador_id = cur.fetchone()[0]

                # 7b. Marca empresa em fornecedores como cadastrada e amarra ao
                # prestador recém-criado. consultar_brasilapi normalmente já criou
                # a linha; o INSERT...ON CONFLICT abaixo é fallback defensivo.
                cur.execute("""
                    UPDATE fornecedores
                       SET cadastrado = TRUE,
                           cadastrado_em = NOW(),
                           usuario_id = %s,
                           plano = 'GRATUITO'
                     WHERE cnpj = %s
                """, (prestador_id, cnpj_limpo))
                if cur.rowcount == 0:
                    cur.execute("""
                        INSERT INTO fornecedores
                            (cnpj, razao_social, cadastrado, cadastrado_em,
                             usuario_id, plano)
                        VALUES (%s, %s, TRUE, NOW(), %s, 'GRATUITO')
                        ON CONFLICT (cnpj) DO UPDATE SET
                            cadastrado = TRUE,
                            cadastrado_em = NOW(),
                            usuario_id = EXCLUDED.usuario_id,
                            plano = 'GRATUITO'
                    """, (cnpj_limpo, nome_empresa, prestador_id))
            conn.commit()

            # 8. Token JWT pra login automatico
            payload = {
                "sub": str(prestador_id),
                "plano": "GRATUITO",
                "exp": datetime.utcnow() + timedelta(days=7),
            }
            token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")

            return {
                "token": token,
                "prestador": {
                    "id": str(prestador_id),
                    "nome_empresa": nome_empresa,
                    "cnpj": cnpj_limpo,
                    "email": req.email,
                    "plano": "GRATUITO",
                },
                "empresa_dados": {
                    "razao_social": dados.get("razao_social"),
                    "nome_fantasia": dados.get("nome_fantasia"),
                    "uf": dados.get("uf"),
                    "municipio": dados.get("municipio"),
                    "cnae_principal": cnae_principal,
                },
            }

        except HTTPException:
            raise
        except Exception as e:
            conn.rollback()
            log.exception(f"Erro cadastrando prestador")
            raise HTTPException(500, f"Erro interno: {str(e)}")
        finally:
            conn.close()

    return router
