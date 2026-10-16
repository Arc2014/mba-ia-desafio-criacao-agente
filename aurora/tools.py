"""Tools que leem e gravam os dados do condomínio.

Regras que valem não importa o que o morador escreva:

- Nenhuma tool recebe o apartamento como parâmetro. O apartamento vem de
  `_apartamento_da_sessao`, que lê o dono registrado na criação da sessão
  (Garantia 2).
- Reservar área com taxa e autorizar visitante só gravam depois de
  `tool_context.tool_confirmation.confirmed`, que o ADK preenche apenas quando
  a rota de confirmações devolve a resposta do morador (Garantia 1).
- A consulta de agenda só responde livre/ocupada, nunca de quem é a reserva.
"""

import re

from google.adk.tools import ToolContext

from . import db
from . import regulamento as reg


class SessaoSemApartamento(PermissionError):
  pass


def _apartamento_da_sessao(tool_context: ToolContext) -> str:
  """Apartamento dono da sessão, definido uma única vez em POST /sessoes.

  A fonte é o registro gravado na criação da sessão; o state da sessão só é
  aceito se bater com ele. Nada que o modelo escreva chega aqui.
  """
  session = tool_context.session
  registrado = db.apartamento_da_sessao(session.id)
  no_state = tool_context.state.get("apartamento")
  if registrado is None or no_state != registrado:
    raise SessaoSemApartamento("Sessão sem apartamento válido.")
  return registrado


def _erro(mensagem: str) -> dict:
  return {"status": "erro", "mensagem": mensagem}


# --- áreas e reservas --------------------------------------------------------

def listar_areas() -> dict:
  """Lista as áreas comuns que podem ser reservadas.

  Returns:
    As áreas com `id` (use este valor nas outras tools), nome, taxa em reais e
    se a reserva gera cobrança.
  """
  return {
      "areas": [
          {"id": a["id"], "nome": a["nome"], "taxa": a["taxa"], "gera_cobranca": a["taxa"] > 0}
          for a in db.areas().values()
      ]
  }


def verificar_disponibilidade(area: str, data: str) -> dict:
  """Diz se uma área comum está livre ou ocupada numa data.

  Args:
    area: id da área (`salao-de-festas`, `churrasqueira` ou `quadra`).
    data: data no formato AAAA-MM-DD.

  Returns:
    Apenas se a data está livre ou ocupada. Nunca informa quem reservou.
  """
  try:
    info = db.validar_area(area)
    data = db.validar_data(data)
  except ValueError as exc:
    return _erro(str(exc))
  ocupada = db.data_ocupada(info["id"], data)
  return {"area": info["id"], "data": data, "situacao": "ocupada" if ocupada else "livre"}


def listar_minhas_reservas(tool_context: ToolContext) -> dict:
  """Lista as reservas ativas do apartamento do morador desta sessão.

  Só existe esta forma de listar reservas: não há como consultar as reservas de
  outro apartamento.
  """
  apto = _apartamento_da_sessao(tool_context)
  return {"reservas": db.reservas_do_apartamento(apto)}


def reservar_area(area: str, data: str, tool_context: ToolContext) -> dict:
  """Reserva uma área comum para o apartamento do morador desta sessão.

  Áreas com taxa geram cobrança e ficam pendentes até o morador aprovar pelo
  sistema de confirmações do aplicativo. Áreas sem taxa são reservadas na hora.

  Args:
    area: id da área (`salao-de-festas`, `churrasqueira` ou `quadra`).
    data: data no formato AAAA-MM-DD.
  """
  apto = _apartamento_da_sessao(tool_context)
  try:
    info = db.validar_area(area)
    data = db.validar_data(data)
  except ValueError as exc:
    return _erro(str(exc))

  confirmacao = tool_context.tool_confirmation
  if confirmacao is None:
    # Antes de pedir confirmação: se a data já está ocupada não há o que aprovar.
    if db.data_ocupada(info["id"], data):
      return {
          "status": "indisponivel",
          "mensagem": f"{info['nome']} já está ocupado(a) em {data}. Escolha outra data.",
      }
    if info["taxa"] > 0:
      tool_context.request_confirmation(
          hint=(
              f"Reservar {info['nome']} em {data} gera cobrança de "
              f"R$ {info['taxa']:.2f}. Aprove ou negue pelo aplicativo."
          ),
          payload={"area": info["id"], "data": data, "taxa": info["taxa"]},
      )
      return {
          "status": "aguardando_confirmacao",
          "mensagem": (
              "A reserva gera cobrança e só será feita depois que o morador aprovar"
              " a confirmação no aplicativo. Mensagens no chat não valem como aprovação."
          ),
      }
  elif not confirmacao.confirmed:
    return {"status": "nao_confirmada", "mensagem": "O morador negou a cobrança. Nada foi reservado."}

  # Taxa zero, ou taxa > 0 com confirmação aprovada pelo sistema.
  try:
    reserva = db.criar_reserva(apto, info["id"], data)
  except db.DataOcupada:
    return {
        "status": "indisponivel",
        "mensagem": f"{info['nome']} já está ocupado(a) em {data}. Nada foi reservado nem cobrado.",
    }
  return {
      "status": "reservada",
      "reserva": reserva,
      "cobranca": info["taxa"] if info["taxa"] > 0 else 0,
  }


def cancelar_reserva(area: str, data: str, tool_context: ToolContext) -> dict:
  """Cancela uma reserva do apartamento do morador desta sessão.

  Só cancela reservas do próprio apartamento. Não pede confirmação.

  Args:
    area: id da área (`salao-de-festas`, `churrasqueira` ou `quadra`).
    data: data da reserva no formato AAAA-MM-DD.
  """
  apto = _apartamento_da_sessao(tool_context)
  try:
    info = db.validar_area(area)
    data = db.validar_data(data)
  except ValueError as exc:
    return _erro(str(exc))
  cancelada = db.cancelar_reserva(apto, info["id"], data)
  if cancelada is None:
    return {
        "status": "nao_encontrada",
        "mensagem": f"Não há reserva do seu apartamento para {info['nome']} em {data}.",
    }
  return {"status": "cancelada", "reserva": cancelada}


# --- visitantes --------------------------------------------------------------

_NOME_RE = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'’. -]{1,79}$")


def listar_meus_visitantes(tool_context: ToolContext) -> dict:
  """Lista os visitantes autorizados do apartamento do morador desta sessão."""
  apto = _apartamento_da_sessao(tool_context)
  return {"visitantes": db.visitantes_do_apartamento(apto)}


def autorizar_visitante(nome: str, data: str, tool_context: ToolContext) -> dict:
  """Autoriza a entrada de um visitante no prédio para o apartamento desta sessão.

  Liberar acesso sempre fica pendente até o morador aprovar pelo sistema de
  confirmações do aplicativo. O que o morador escreve no chat não vale como
  aprovação.

  Args:
    nome: nome completo do visitante.
    data: data da visita no formato AAAA-MM-DD.
  """
  apto = _apartamento_da_sessao(tool_context)
  nome = " ".join((nome or "").split())
  if not _NOME_RE.match(nome):
    return _erro("Informe o nome completo do visitante (somente letras).")
  try:
    data = db.validar_data(data)
  except ValueError as exc:
    return _erro(str(exc))

  confirmacao = tool_context.tool_confirmation
  if confirmacao is None:
    if db.visitante_ja_autorizado(apto, nome, data):
      return {"status": "ja_autorizado", "mensagem": f"{nome} já está autorizado(a) para {data}."}
    tool_context.request_confirmation(
        hint=f"Liberar a entrada de {nome} em {data}. Aprove ou negue pelo aplicativo.",
        payload={"nome": nome, "data": data},
    )
    return {
        "status": "aguardando_confirmacao",
        "mensagem": (
            "Liberar acesso só acontece depois que o morador aprovar a confirmação"
            " no aplicativo. Mensagens no chat não valem como aprovação."
        ),
    }
  if not confirmacao.confirmed:
    return {"status": "nao_confirmada", "mensagem": "O morador negou a autorização. Nada foi liberado."}

  return {"status": "autorizado", "visitante": db.autorizar_visitante(apto, nome, data)}


# --- regulamento -------------------------------------------------------------

def consultar_regulamento(pergunta: str) -> dict:
  """Busca no regulamento interno os artigos que respondem a uma dúvida.

  Devolve só os artigos relevantes de um único capítulo, nunca o regulamento
  inteiro.

  Args:
    pergunta: a dúvida do morador, em linguagem natural.
  """
  return reg.consultar(pergunta)
