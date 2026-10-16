"""API HTTP do assistente do Residencial Aurora."""

import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService, Session
from google.genai import types
from pydantic import BaseModel
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import create_async_engine

from . import config, db
from .agents import app as adk_app

logger = logging.getLogger("aurora.api")

CONFIRMACAO = "adk_request_confirmation"


class NovaSessao(BaseModel):
  apartamento: str


class NovaMensagem(BaseModel):
  texto: str


class RespostaConfirmacao(BaseModel):
  id: str
  confirmado: bool


def _criar_session_service() -> DatabaseSessionService:
  config.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
  engine = create_async_engine(
      f"sqlite+aiosqlite:///{config.SESSOES_DB}", connect_args={"timeout": 30}
  )

  @sa_event.listens_for(engine.sync_engine, "connect")
  def _pragmas(dbapi_conn, _):  # noqa: ANN001
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()

  return DatabaseSessionService(db_engine=engine)


_estado: dict[str, Any] = {}
# Uma execução por vez em cada sessão (sessões diferentes rodam em paralelo).
_travas: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


@asynccontextmanager
async def lifespan(_: FastAPI):
  db.inicializar()
  session_service = _criar_session_service()
  _estado["sessions"] = session_service
  _estado["runner"] = Runner(app=adk_app, session_service=session_service)
  yield
  await _estado["runner"].close()


api = FastAPI(title="Residencial Aurora", lifespan=lifespan)


def _user_id(apartamento: str) -> str:
  return f"apto-{apartamento}"


async def _carregar_sessao(session_id: str) -> Session:
  apartamento = db.apartamento_da_sessao(session_id)
  if apartamento is None:
    raise HTTPException(status_code=404, detail="Sessão não encontrada.")
  session = await _estado["sessions"].get_session(
      app_name=config.APP_NAME, user_id=_user_id(apartamento), session_id=session_id
  )
  if session is None:
    raise HTTPException(status_code=404, detail="Sessão não encontrada.")
  return session


def _confirmacoes_pendentes(session: Session) -> list[dict]:
  """Pedidos de confirmação gravados na sessão que ainda não têm resposta."""
  pedidos: dict[str, dict] = {}
  respondidas: set[str] = set()
  for ev in session.events:
    for fc in ev.get_function_calls():
      if fc.name == CONFIRMACAO and fc.id:
        args = fc.args or {}
        original = args.get("originalFunctionCall") or {}
        confirmacao = args.get("toolConfirmation") or {}
        pedidos[fc.id] = {
            "id": fc.id,
            "acao": original.get("name", ""),
            "detalhes": confirmacao.get("payload") or original.get("args") or {},
            "_invocation_id": ev.invocation_id,
        }
    for fr in ev.get_function_responses():
      if fr.name == CONFIRMACAO and fr.id:
        respondidas.add(fr.id)
  return [p for i, p in pedidos.items() if i not in respondidas]


def _publico(pendentes: list[dict]) -> list[dict]:
  return [{k: v for k, v in p.items() if not k.startswith("_")} for p in pendentes]


async def _executar(session: Session, mensagem: types.Content, invocation_id: str | None = None) -> dict:
  runner: Runner = _estado["runner"]
  textos: list[str] = []
  try:
    async for ev in runner.run_async(
        user_id=session.user_id,
        session_id=session.id,
        new_message=mensagem,
        invocation_id=invocation_id,
    ):
      if ev.author == "user" or ev.partial or not ev.content or not ev.content.parts:
        continue
      if ev.is_final_response():
        for part in ev.content.parts:
          if part.text and not part.thought:
            textos.append(part.text.strip())
  except Exception:  # erro do modelo/rede: a conversa segue, sem erro de servidor
    logger.exception("Falha ao executar o agente na sessão %s", session.id)
    textos.append("Desculpe, não consegui concluir agora. Pode tentar de novo em instantes?")

  atualizada = await _estado["sessions"].get_session(
      app_name=config.APP_NAME, user_id=session.user_id, session_id=session.id
  )
  return {
      "resposta": "\n\n".join(t for t in textos if t),
      "confirmacoes_pendentes": _publico(_confirmacoes_pendentes(atualizada)),
  }


@api.post("/sessoes", status_code=201)
async def criar_sessao(body: NovaSessao):
  apartamento = body.apartamento.strip()
  if apartamento not in db.apartamentos():
    raise HTTPException(status_code=422, detail="Apartamento inexistente.")
  session = await _estado["sessions"].create_session(
      app_name=config.APP_NAME,
      user_id=_user_id(apartamento),
      state={"apartamento": apartamento},
  )
  db.registrar_sessao(session.id, apartamento)
  return {"session_id": session.id}


@api.post("/sessoes/{session_id}/mensagens")
async def enviar_mensagem(session_id: str, body: NovaMensagem):
  async with _travas[session_id]:
    session = await _carregar_sessao(session_id)
    mensagem = types.Content(role="user", parts=[types.Part(text=body.texto)])
    return await _executar(session, mensagem)


@api.post("/sessoes/{session_id}/confirmacoes")
async def responder_confirmacao(session_id: str, body: RespostaConfirmacao):
  async with _travas[session_id]:
    session = await _carregar_sessao(session_id)
    pendente = next((p for p in _confirmacoes_pendentes(session) if p["id"] == body.id), None)
    if pendente is None:
      return JSONResponse(
          status_code=409,
          content={"detail": "Não existe confirmação pendente com esse id nesta sessão."},
      )
    resposta = types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=body.id, name=CONFIRMACAO, response={"confirmed": body.confirmado}
                )
            )
        ],
    )
    return await _executar(session, resposta, invocation_id=pendente["_invocation_id"])


@api.get("/sessoes/{session_id}/eventos")
async def listar_eventos(session_id: str):
  session = await _carregar_sessao(session_id)
  return [ev.model_dump(mode="json", by_alias=True, exclude_none=True) for ev in session.events]


@api.get("/apartamentos/{numero}/reservas")
async def reservas_do_apartamento(numero: str):
  return db.reservas_do_apartamento(numero)


@api.get("/apartamentos/{numero}/visitantes")
async def visitantes_do_apartamento(numero: str):
  return db.visitantes_do_apartamento(numero)
