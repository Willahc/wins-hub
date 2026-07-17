#!/usr/bin/env python3
"""Testes de integracao do resolvedor; nenhuma credencial e embutida."""

from __future__ import annotations

import os
import re
import sys
import threading
import unittest
from types import SimpleNamespace
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from unittest.mock import patch

import psycopg2


ARTIFACT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ARTIFACT_DIR))


def first_env(*names: str) -> Optional[str]:
    return next((os.environ[name] for name in names if os.environ.get(name)), None)


def db_configured() -> bool:
    return bool(
        first_env("DATABASE_URL", "DB_DSN")
        or (
            first_env("DB_HOST", "PGHOST")
            and first_env("DB_NAME", "PGDATABASE")
            and first_env("DB_USER", "PGUSER")
            and first_env("DB_PASSWORD", "PGPASSWORD")
        )
    )


def connect() -> Any:
    dsn = first_env("DATABASE_URL", "DB_DSN")
    if dsn:
        return psycopg2.connect(
            dsn, connect_timeout=10, application_name="wins_cnpj_resolver_tests"
        )
    return psycopg2.connect(
        host=first_env("DB_HOST", "PGHOST"),
        port=first_env("DB_PORT", "PGPORT") or "5432",
        dbname=first_env("DB_NAME", "PGDATABASE"),
        user=first_env("DB_USER", "PGUSER"),
        password=first_env("DB_PASSWORD", "PGPASSWORD"),
        connect_timeout=10,
        application_name="wins_cnpj_resolver_tests",
    )


def mask_cnpj(cnpj: str) -> str:
    return f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"


def make_valid_cnpj(base: int) -> str:
    first_twelve = f"{base:012d}"

    def digit(value: str, weights: Tuple[int, ...]) -> int:
        remainder = sum(int(char) * weight for char, weight in zip(value, weights)) % 11
        return 0 if remainder < 2 else 11 - remainder

    first = digit(first_twelve, (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
    second = digit(first_twelve + str(first), (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
    return first_twelve + str(first) + str(second)


class ReplayTerminalClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import cnpj_master_resolver
        import run_replay

        cls.resolver = cnpj_master_resolver
        cls.replay = run_replay

    def invoke_without_db_or_http(self, value: Any) -> Dict[str, Any]:
        with patch.object(
            self.resolver.psycopg2,
            "connect",
            side_effect=AssertionError("DB proibido para estado terminal"),
        ), patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("HTTP proibido"),
        ), patch("urllib.request.urlopen", side_effect=AssertionError("HTTP proibido")):
            return self.replay.invoke_resolver(self.resolver, value, 1)

    def test_none_vazio_e_sentinelas_sem_cnpj(self) -> None:
        for value in (None, "", "  ", "-", "N/A", "NULL", "sem cnpj", "nao informado"):
            with self.subTest(value=value):
                result = self.invoke_without_db_or_http(value)
                self.assertEqual(result["status"], "SEM_CNPJ")
                self.assertFalse(result["consulta_externa_necessaria"])

    def test_cpf_mascarado_e_sem_mascara_nao_aplicavel(self) -> None:
        cpf_digits = "".join(("123", "456", "789", "09"))
        cpf_masked = (
            f"{cpf_digits[:3]}.{cpf_digits[3:6]}."
            f"{cpf_digits[6:9]}-{cpf_digits[9:]}"
        )
        for value in (cpf_digits, cpf_masked):
            with self.subTest(value=value):
                result = self.invoke_without_db_or_http(value)
                self.assertEqual(result["status"], "CPF_NAO_APLICAVEL")
                safe = self.replay.safe_identifier(value, result)
                self.assertTrue(safe.startswith("CPF_SHA256:"))
                self.assertNotIn(cpf_digits, safe)

    def test_cientifico_e_zero_invalid_sem_db_http(self) -> None:
        for value in (0, "0", "0E+00", "3.574370000012E+12"):
            with self.subTest(value=value):
                result = self.invoke_without_db_or_http(value)
                self.assertEqual(result["status"], "INVALID")
                self.assertFalse(result["consulta_externa_necessaria"])

    def test_cnpj_valido_e_passado_bruto_ao_resolvedor(self) -> None:
        raw = "03.574.370/0001-20"
        seen = []

        def resolve(value: Any, **kwargs: Any) -> Dict[str, Any]:
            seen.append(value)
            return {"status": "FULL_HIT", "dados_encontrados": {}}

        result = self.replay.invoke_resolver(SimpleNamespace(resolve_cnpj=resolve), raw, 1)
        self.assertEqual(result["status"], "FULL_HIT")
        self.assertEqual(seen, [raw])

    def test_cientifico_integral_valido_nao_perde_expoente(self) -> None:
        original_flag = self.resolver.MASTER_CNPJ_LOOKUP_ENABLED
        self.resolver.MASTER_CNPJ_LOOKUP_ENABLED = False
        try:
            result = self.replay.invoke_resolver(
                self.resolver, "8.3169623000110E+13", 1
            )
        finally:
            self.resolver.MASTER_CNPJ_LOOKUP_ENABLED = original_flag
        self.assertEqual(result["status"], "MISS")
        self.assertEqual(result["cnpj_normalizado"], "83169623000110")


@unittest.skipUnless(db_configured(), "configure banco somente por DATABASE_URL/DB_*")
class ResolverIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import cnpj_master_resolver

        cls.resolver = cnpj_master_resolver
        cls.original_flag = cnpj_master_resolver.MASTER_CNPJ_LOOKUP_ENABLED
        cnpj_master_resolver.MASTER_CNPJ_LOOKUP_ENABLED = True
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT cnpj_normalizado
                FROM wins_v2.entidades_lookup
                WHERE razao_social IS NOT NULL
                  AND btrim(razao_social) <> ''
                  AND situacao IS NOT NULL
                  AND btrim(situacao) <> ''
                ORDER BY cnpj_normalizado
                LIMIT 1
                """
            )
            row = cursor.fetchone()
            if row is None:
                raise unittest.SkipTest("cadastro nao possui caso FULL_HIT")
            cls.full_cnpj = row[0]

            cursor.execute(
                """
                SELECT cnpj_normalizado,
                       CASE
                         WHEN endereco IS NULL OR btrim(endereco) = '' THEN 'endereco'
                         WHEN municipio IS NULL OR btrim(municipio) = '' THEN 'municipio'
                         WHEN uf IS NULL OR btrim(uf) = '' THEN 'uf'
                       END
                FROM wins_v2.entidades_lookup
                WHERE razao_social IS NOT NULL
                  AND (
                    endereco IS NULL OR btrim(endereco) = ''
                    OR municipio IS NULL OR btrim(municipio) = ''
                    OR uf IS NULL OR btrim(uf) = ''
                  )
                ORDER BY cnpj_normalizado
                LIMIT 1
                """
            )
            cls.partial_case = cursor.fetchone()

            cursor.execute(
                """
                SELECT cnpj_normalizado
                FROM wins_v2.entidades_lookup
                WHERE cnpj_normalizado LIKE '0%'
                ORDER BY cnpj_normalizado
                LIMIT 1
                """
            )
            leading = cursor.fetchone()
            cls.leading_zero_cnpj = leading[0] if leading else cls.full_cnpj

            cls.miss_cnpj = None
            for base in range(999999000001, 999999001001):
                candidate = make_valid_cnpj(base)
                cursor.execute(
                    "SELECT 1 FROM wins_v2.entidades_lookup WHERE cnpj_normalizado = %s",
                    (candidate,),
                )
                if cursor.fetchone() is None:
                    cls.miss_cnpj = candidate
                    break
            if cls.miss_cnpj is None:
                raise unittest.SkipTest("nao foi possivel gerar CNPJ MISS")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.resolver.MASTER_CNPJ_LOOKUP_ENABLED = cls.original_flag

    def resolve(self, cnpj: str, fields: Any) -> Dict[str, Any]:
        return self.resolver.resolve_cnpj(
            cnpj,
            required_fields=fields,
            context={"contexto": "test_resolver", "write_log": False},
        )

    def test_cnpj_com_e_sem_mascara(self) -> None:
        plain = self.resolve(self.full_cnpj, ["razao_social", "situacao"])
        masked = self.resolve(mask_cnpj(self.full_cnpj), ["razao_social", "situacao"])
        self.assertEqual(plain["status"], "FULL_HIT")
        self.assertEqual(masked["status"], "FULL_HIT")
        self.assertEqual(plain["cnpj_normalizado"], masked["cnpj_normalizado"])

    def test_zeros_a_esquerda(self) -> None:
        result = self.resolve(self.leading_zero_cnpj, ["razao_social"])
        self.assertEqual(result["cnpj_normalizado"], self.leading_zero_cnpj)
        self.assertEqual(len(result["cnpj_normalizado"]), 14)

    def test_invalid_nao_requer_externo(self) -> None:
        result = self.resolve("11.111.111/1111-11", ["razao_social"])
        self.assertEqual(result["status"], "INVALID")
        self.assertFalse(result["consulta_externa_necessaria"])

    def test_partial_hit(self) -> None:
        if self.partial_case is None:
            self.skipTest("cadastro nao possui campo obrigatorio ausente")
        cnpj, missing_field = self.partial_case
        result = self.resolve(cnpj, ["razao_social", missing_field])
        self.assertEqual(result["status"], "PARTIAL_HIT")
        self.assertIn(missing_field, result["campos_ausentes"])

    def test_miss(self) -> None:
        result = self.resolve(self.miss_cnpj, ["razao_social"])
        self.assertEqual(result["status"], "MISS")
        self.assertTrue(result["consulta_externa_necessaria"])

    def test_full_hit_proibe_http_objetivamente(self) -> None:
        def fail_external(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("provedor externo foi acionado em FULL_HIT")

        with patch("requests.sessions.Session.request", side_effect=fail_external), patch(
            "requests.get", side_effect=fail_external
        ):
            result = self.resolve(self.full_cnpj, ["razao_social", "situacao"])
        self.assertEqual(result["status"], "FULL_HIT")
        self.assertFalse(result["consulta_externa_necessaria"])

    def test_falha_do_resolvedor_mantem_fallback(self) -> None:
        with patch.object(
            self.resolver.psycopg2, "connect", side_effect=RuntimeError("db indisponivel")
        ):
            result = self.resolve(self.full_cnpj, ["razao_social"])
        self.assertEqual(result["status"], "MISS")
        self.assertTrue(result["consulta_externa_necessaria"])

    def test_concorrencia_de_lookups(self) -> None:
        errors = []

        def worker() -> None:
            try:
                for _ in range(5):
                    self.assertEqual(
                        self.resolve(self.full_cnpj, ["razao_social"])["status"],
                        "FULL_HIT",
                    )
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
