"""Armazenamento do condomínio em SQLite.

Os arquivos de `dados/` são o estado inicial e nunca são alterados: eles só
são lidos para semear o banco (`restaurar`) e para consultar apartamentos e
áreas. Reservas e visitantes vivem em `.storage/condominio.db`.

A exclusividade "uma reserva ativa por área e data" é garantida pelo índice
único parcial `ux_reserva_ativa_area_data`: o próprio SQLite recusa a segunda
gravação no instante do INSERT, mesmo que duas aprovações cheguem juntas.
"""

import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Iterator

from . import config

ATIVA = "ativa"
CANCELADA = "cancelada"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reservas (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    codigo        TEXT    NOT NULL UNIQUE,
    apartamento   TEXT    NOT NULL,
    area          TEXT    NOT NULL,
    data          TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'ativa'
                          CHECK (status IN ('ativa', 'cancelada')),
    criada_em     TEXT    NOT NULL,
    cancelada_em  TEXT
);

-- Garantia 5: no máximo uma reserva ATIVA por área e data, imposto pelo banco.
CREATE UNIQUE INDEX IF NOT EXISTS ux_reserva_ativa_area_data
    ON reservas (area, data) WHERE status = 'ativa';

CREATE INDEX IF NOT EXISTS ix_reservas_apartamento ON reservas (apartamento);

-- Todo código já emitido, inclusive de reservas canceladas ou apagadas por uma
-- restauração. A restauração nunca limpa esta tabela, então um código novo
-- jamais repete um antigo.
CREATE TABLE IF NOT EXISTS codigos_emitidos (
    codigo TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS visitantes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    apartamento  TEXT NOT NULL,
    nome         TEXT NOT NULL,
    data         TEXT NOT NULL,
    criado_em    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_visitantes_apartamento ON visitantes (apartamento);

-- Dono de cada sessão, definido uma única vez na criação.
CREATE TABLE IF NOT EXISTS sessoes (
    session_id   TEXT PRIMARY KEY,
    apartamento  TEXT NOT NULL,
    criada_em    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    chave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);
"""

_DATA_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DataInvalida(ValueError):
  pass


class AreaInvalida(ValueError):
  pass


class DataOcupada(Exception):
  """A área já tem reserva ativa na data (detectado na gravação)."""


def _agora() -> str:
  return datetime.now(timezone.utc).isoformat()


def _ler_json(nome: str):
  with open(config.DADOS_DIR / nome, encoding="utf-8") as f:
    return json.load(f)


# --- dados fixos (somente leitura) -------------------------------------------

def apartamentos() -> dict[str, str]:
  return {a["numero"]: a["morador"] for a in _ler_json("apartamentos.json")}


def areas() -> dict[str, dict]:
  return {a["id"]: a for a in _ler_json("areas.json")}


def validar_data(valor: str) -> str:
  valor = (valor or "").strip()
  if not _DATA_RE.match(valor):
    raise DataInvalida("A data deve estar no formato AAAA-MM-DD.")
  try:
    date.fromisoformat(valor)
  except ValueError as exc:
    raise DataInvalida(f"A data {valor} não existe no calendário.") from exc
  return valor


def validar_area(area_id: str) -> dict:
  todas = areas()
  chave = (area_id or "").strip().lower()
  if chave not in todas:
    raise AreaInvalida(
        "Área desconhecida. Áreas válidas: " + ", ".join(sorted(todas)) + "."
    )
  return todas[chave]


# --- conexão -----------------------------------------------------------------

@contextmanager
def conectar() -> Iterator[sqlite3.Connection]:
  config.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
  conn = sqlite3.connect(config.CONDOMINIO_DB, timeout=30, isolation_level=None)
  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA journal_mode=WAL")
  conn.execute("PRAGMA busy_timeout=30000")
  try:
    yield conn
  finally:
    conn.close()


def inicializar() -> None:
  """Cria o schema. Semeia apenas se o banco nunca foi semeado."""
  with conectar() as conn:
    conn.executescript(_SCHEMA)
    semeado = conn.execute(
        "SELECT 1 FROM meta WHERE chave = 'semeado'"
    ).fetchone()
  if not semeado:
    restaurar()


def restaurar() -> None:
  """Volta reservas e visitantes ao estado dos arquivos de `dados/`."""
  reservas = _ler_json("reservas.json")
  visitantes = _ler_json("visitantes.json")
  agora = _agora()
  with conectar() as conn:
    conn.executescript(_SCHEMA)
    conn.execute("BEGIN IMMEDIATE")
    try:
      conn.execute("DELETE FROM reservas")
      conn.execute("DELETE FROM visitantes")
      for r in reservas:
        conn.execute(
            "INSERT INTO reservas (codigo, apartamento, area, data, status, criada_em)"
            " VALUES (?, ?, ?, ?, 'ativa', ?)",
            (r["codigo"], r["apartamento"], r["area"], r["data"], agora),
        )
        conn.execute(
            "INSERT OR IGNORE INTO codigos_emitidos (codigo) VALUES (?)",
            (r["codigo"],),
        )
      for v in visitantes:
        conn.execute(
            "INSERT INTO visitantes (apartamento, nome, data, criado_em)"
            " VALUES (?, ?, ?, ?)",
            (v["apartamento"], v["nome"], v["data"], agora),
        )
      conn.execute(
          "INSERT OR REPLACE INTO meta (chave, valor) VALUES ('semeado', ?)",
          (agora,),
      )
      conn.execute("COMMIT")
    except Exception:
      conn.execute("ROLLBACK")
      raise


# --- sessões -----------------------------------------------------------------

def registrar_sessao(session_id: str, apartamento: str) -> None:
  with conectar() as conn:
    conn.execute(
        "INSERT INTO sessoes (session_id, apartamento, criada_em) VALUES (?, ?, ?)",
        (session_id, apartamento, _agora()),
    )


def apartamento_da_sessao(session_id: str) -> str | None:
  with conectar() as conn:
    row = conn.execute(
        "SELECT apartamento FROM sessoes WHERE session_id = ?", (session_id,)
    ).fetchone()
  return row["apartamento"] if row else None


def apagar_sessoes() -> None:
  with conectar() as conn:
    conn.execute("DELETE FROM sessoes")


# --- reservas ----------------------------------------------------------------

def reservas_do_apartamento(apartamento: str) -> list[dict]:
  with conectar() as conn:
    rows = conn.execute(
        "SELECT codigo, area, data FROM reservas"
        " WHERE apartamento = ? AND status = 'ativa' ORDER BY data, area",
        (apartamento,),
    ).fetchall()
  return [dict(r) for r in rows]


def data_ocupada(area: str, data: str) -> bool:
  """Só diz se há reserva ativa; nunca de quem ela é."""
  with conectar() as conn:
    row = conn.execute(
        "SELECT 1 FROM reservas WHERE area = ? AND data = ? AND status = 'ativa'",
        (area, data),
    ).fetchone()
  return row is not None


def _novo_codigo(conn: sqlite3.Connection) -> str:
  while True:
    codigo = "RSV-" + secrets.token_hex(3).upper()
    try:
      conn.execute("INSERT INTO codigos_emitidos (codigo) VALUES (?)", (codigo,))
      return codigo
    except sqlite3.IntegrityError:
      continue  # código já emitido algum dia: sorteia outro


def criar_reserva(apartamento: str, area: str, data: str) -> dict:
  """Grava a reserva. Levanta DataOcupada se o índice único recusar."""
  with conectar() as conn:
    conn.execute("BEGIN IMMEDIATE")
    try:
      codigo = _novo_codigo(conn)
      conn.execute(
          "INSERT INTO reservas (codigo, apartamento, area, data, status, criada_em)"
          " VALUES (?, ?, ?, ?, 'ativa', ?)",
          (codigo, apartamento, area, data, _agora()),
      )
      conn.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
      conn.execute("ROLLBACK")
      if "ux_reserva_ativa_area_data" in str(exc) or "reservas.area" in str(exc):
        raise DataOcupada() from exc
      raise
    except Exception:
      conn.execute("ROLLBACK")
      raise
  return {"codigo": codigo, "area": area, "data": data}


def cancelar_reserva(apartamento: str, area: str, data: str) -> dict | None:
  """Cancela a reserva ativa do PRÓPRIO apartamento naquela área e data."""
  with conectar() as conn:
    conn.execute("BEGIN IMMEDIATE")
    try:
      row = conn.execute(
          "SELECT id, codigo FROM reservas WHERE apartamento = ? AND area = ?"
          " AND data = ? AND status = 'ativa'",
          (apartamento, area, data),
      ).fetchone()
      if row is None:
        conn.execute("ROLLBACK")
        return None
      conn.execute(
          "UPDATE reservas SET status = 'cancelada', cancelada_em = ? WHERE id = ?",
          (_agora(), row["id"]),
      )
      conn.execute("COMMIT")
    except Exception:
      conn.execute("ROLLBACK")
      raise
  return {"codigo": row["codigo"], "area": area, "data": data}


# --- visitantes --------------------------------------------------------------

def visitantes_do_apartamento(apartamento: str) -> list[dict]:
  with conectar() as conn:
    rows = conn.execute(
        "SELECT nome, data FROM visitantes WHERE apartamento = ? ORDER BY data, nome",
        (apartamento,),
    ).fetchall()
  return [dict(r) for r in rows]


def visitante_ja_autorizado(apartamento: str, nome: str, data: str) -> bool:
  with conectar() as conn:
    row = conn.execute(
        "SELECT 1 FROM visitantes WHERE apartamento = ? AND lower(nome) = lower(?)"
        " AND data = ?",
        (apartamento, nome, data),
    ).fetchone()
  return row is not None


def autorizar_visitante(apartamento: str, nome: str, data: str) -> dict:
  with conectar() as conn:
    conn.execute(
        "INSERT INTO visitantes (apartamento, nome, data, criado_em) VALUES (?, ?, ?, ?)",
        (apartamento, nome, data, _agora()),
    )
  return {"nome": nome, "data": data}
