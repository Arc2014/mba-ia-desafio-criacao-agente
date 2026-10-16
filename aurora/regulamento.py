"""Consulta ao regulamento interno sem carregar o texto inteiro.

O arquivo é dividido em capítulos (`## Capítulo ...`) e cada capítulo em
artigos (`**Art. N**`). Uma consulta pontua os capítulos pelas palavras da
pergunta, escolhe UM único capítulo e devolve apenas os artigos dele que
tratam da pergunta. Trechos de outros capítulos nunca saem desta função.
"""

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from . import config

_STOPWORDS = {
    "a", "o", "as", "os", "um", "uma", "uns", "umas", "de", "do", "da", "dos",
    "das", "em", "no", "na", "nos", "nas", "por", "pelo", "pela", "para", "pra",
    "com", "sem", "e", "ou", "que", "qual", "quais", "quando", "como", "onde",
    "ate", "se", "ao", "aos", "eu", "meu", "minha", "posso", "pode", "podem",
    "e", "sao", "ser", "esta", "isso", "isto", "sobre", "regra", "regras",
    "regulamento", "condominio", "horas", "hora", "horario", "horarios", "funciona",
    "funcionamento", "permitido", "proibido", "vedado", "dia", "dias", "mais",
    "menos", "tem", "ter", "ha", "existe", "devo", "preciso", "fazer", "uso",
    "usar", "aberto", "aberta", "fecha", "abre", "morador", "moradores",
}


def _normalizar(texto: str) -> str:
  texto = unicodedata.normalize("NFKD", texto.lower())
  return "".join(c for c in texto if not unicodedata.combining(c))


# Palavras do morador que o regulamento escreve de outro jeito.
_SINONIMOS = {
    "cachorro": ("caes", "animais"), "cachorra": ("caes", "animais"),
    "cao": ("caes", "animais"), "gato": ("animais",), "gata": ("animais",),
    "pet": ("animais",), "bicho": ("animais",), "barulho": ("ruido", "silencio"),
    "festa": ("salao",), "churrasco": ("churrasqueira",), "lixo": ("residuo",),
    "reforma": ("obra",), "carro": ("veiculo", "garagem"), "visita": ("visitante",),
}

_PERGUNTA_HORARIO = {"hora", "horas", "horario", "horarios", "funciona", "abre", "fecha", "aberto", "aberta"}
_TEM_HORARIO = re.compile(r"\d{1,2}h")


def _termos(texto: str) -> list[str]:
  palavras = re.findall(r"[a-z0-9]+", _normalizar(texto))
  termos = []
  for p in palavras:
    if p in _STOPWORDS or len(p) < 3:
      continue
    termos.append(_radical(p))
    termos.extend(_radical(s) for s in _SINONIMOS.get(p, ()))
  return termos


def _radical(palavra: str) -> str:
  """Radical simples para casar singular/plural (domingos ~ domingo)."""
  for sufixo in ("oes", "aes", "ais", "eis", "res", "es", "s"):
    if palavra.endswith(sufixo) and len(palavra) - len(sufixo) >= 4:
      return palavra[: -len(sufixo)]
  return palavra


@dataclass(frozen=True)
class Capitulo:
  titulo: str
  artigos: tuple[str, ...]

  @property
  def termos_titulo(self) -> set[str]:
    return set(_termos(self.titulo))


@lru_cache(maxsize=1)
def capitulos() -> tuple[Capitulo, ...]:
  texto = (config.DADOS_DIR / "regulamento.md").read_text(encoding="utf-8")
  blocos = re.split(r"^## ", texto, flags=re.MULTILINE)[1:]
  resultado = []
  for bloco in blocos:
    titulo, _, corpo = bloco.partition("\n")
    artigos = re.split(r"\n(?=\*\*Art\. )", corpo.strip())
    resultado.append(
        Capitulo(titulo=titulo.strip(), artigos=tuple(a.strip() for a in artigos if a.strip()))
    )
  return tuple(resultado)


def _pontuar(termos: list[str], texto: str) -> int:
  alvo = set(_termos(texto))
  return sum(1 for t in termos if t in alvo)


def consultar(pergunta: str, max_artigos: int = 3) -> dict:
  termos = _termos(pergunta)
  if not termos:
    return {"encontrado": False}

  melhor, melhor_nota = None, 0
  for cap in capitulos():
    # O título pesa mais: diz do que o capítulo trata.
    nota = 5 * sum(1 for t in termos if t in cap.termos_titulo)
    nota += sum(_pontuar(termos, art) for art in cap.artigos)
    if nota > melhor_nota:
      melhor, melhor_nota = cap, nota
  if melhor is None:
    return {"encontrado": False}

  pede_horario = bool(_PERGUNTA_HORARIO & set(re.findall(r"[a-z]+", _normalizar(pergunta))))
  notas = [
      (_pontuar(termos, art) + (2 if pede_horario and _TEM_HORARIO.search(art) else 0), i)
      for i, art in enumerate(melhor.artigos)
  ]
  escolhidos = sorted(i for nota, i in sorted(notas, reverse=True)[:max_artigos] if nota > 0)
  if not escolhidos:
    escolhidos = list(range(min(max_artigos, len(melhor.artigos))))
  return {
      "encontrado": True,
      "capitulo": melhor.titulo,
      "trechos": [melhor.artigos[i] for i in escolhidos],
  }
