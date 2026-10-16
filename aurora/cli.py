"""Comandos de linha: subir a API e restaurar os dados iniciais."""

import argparse
import logging

import uvicorn


def servir() -> None:
  logging.basicConfig(level=logging.INFO)
  uvicorn.run("aurora.api:api", host="0.0.0.0", port=8000)


def restaurar() -> None:
  from . import db

  parser = argparse.ArgumentParser(
      description="Volta reservas e visitantes ao estado dos arquivos de dados/."
  )
  parser.add_argument(
      "--sessoes",
      action="store_true",
      help="também apaga todas as sessões e conversas",
  )
  args = parser.parse_args()
  db.restaurar()
  print("Reservas e visitantes restaurados a partir de dados/.")
  if args.sessoes:
    from . import config

    db.apagar_sessoes()
    for sufixo in ("", "-wal", "-shm"):
      config.SESSOES_DB.with_name(config.SESSOES_DB.name + sufixo).unlink(missing_ok=True)
    print("Sessões apagadas.")
