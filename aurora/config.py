"""Configuração central: caminhos e variáveis de ambiente."""

import os
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
load_dotenv(RAIZ / ".env")

# Estado inicial do condomínio (somente leitura, nunca alterado).
DADOS_DIR = RAIZ / "dados"

# Armazenamento local: banco do condomínio e banco de sessões do ADK.
STORAGE_DIR = Path(os.getenv("AURORA_STORAGE_DIR") or RAIZ / ".storage")
CONDOMINIO_DB = STORAGE_DIR / "condominio.db"
SESSOES_DB = STORAGE_DIR / "sessoes.db"

APP_NAME = "residencial_aurora"

# No free tier do Google AI Studio os modelos "flash" têm cota de ~20
# requisições/dia; a linha "flash-lite" aguenta o fluxo inteiro do avaliador.
MODELO_PRINCIPAL = os.getenv("AURORA_MODELO_PRINCIPAL") or "gemini-3.5-flash-lite"
MODELO_ESPECIALISTAS = os.getenv("AURORA_MODELO_ESPECIALISTAS") or MODELO_PRINCIPAL
