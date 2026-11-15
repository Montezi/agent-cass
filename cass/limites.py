"""Limite local por IP e CORS a partir da configuração carregada na subida."""

import ipaddress
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from math import ceil
from threading import Lock
from time import monotonic

from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

LIMITE_POR_MINUTO = 10
JANELA_S = 60.0
MAX_IPS = 10_000


@dataclass
class _Janela:
    validade: float
    usadas: int = 0


class LimitadorPorIP:
    """Janelas fixas por processo, com memória limitada e expiração monotônica.

    Quando o mapa enche, novos IPs aguardam uma expiração. Remover um IP ativo
    para admitir outro permitiria zerar a cota alternando endereços.
    """

    def __init__(
        self,
        limite: int = LIMITE_POR_MINUTO,
        janela_s: float = JANELA_S,
        max_ips: int = MAX_IPS,
        relogio: Callable[[], float] = monotonic,
    ) -> None:
        if limite < 1 or janela_s <= 0 or max_ips < 1:
            raise ValueError("limite, janela_s e max_ips devem ser positivos")
        self._limite = limite
        self._janela_s = janela_s
        self._max_ips = max_ips
        self._relogio = relogio
        self._janelas: OrderedDict[str, _Janela] = OrderedDict()
        self._trava = Lock()

    def admitir(self, ip: str) -> int | None:
        """None autoriza; um inteiro informa quantos segundos aguardar (Retry-After)."""
        with self._trava:
            agora = self._relogio()
            # Janelas entram em ordem de validade e não se renovam a cada acesso.
            while self._janelas and next(iter(self._janelas.values())).validade <= agora:
                self._janelas.popitem(last=False)
            janela = self._janelas.get(ip)
            if janela is None:
                if len(self._janelas) >= self._max_ips:
                    primeira = next(iter(self._janelas.values()))
                    return max(1, ceil(primeira.validade - agora))
                janela = _Janela(validade=agora + self._janela_s)
                self._janelas[ip] = janela
            if janela.usadas >= self._limite:
                return max(1, ceil(janela.validade - agora))
            janela.usadas += 1
            return None


def ip_do_cliente(request: Request) -> str:
    """Primeiro endereço do X-Forwarded-For; sem ele, o cliente da conexão.

    No Cloud Run, request.client.host é o proxy do Google, e quem chamou vem no
    X-Forwarded-For ("cliente, proxy, ..."). Esse cabeçalho pode ser falsificado:
    quem chama pode mandar o próprio, e o primeiro endereço vira o que ele quiser.
    O limite por IP só freia o uso casual; o teto de custo real é o --max-instances
    do Cloud Run. Um primeiro item que não seja IP cai no client.host, para lixo
    forjado não virar chave do limitador.
    """
    primeiro = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if primeiro:
        try:
            return str(ipaddress.ip_address(primeiro))
        except ValueError:
            pass
    return request.client.host if request.client is not None else "desconhecido"


def verificar_limite_ip(request: Request) -> int | None:
    """Compartilha a cota JSON/SSE por ip_do_cliente (ver o aviso sobre falsificação)."""
    estado = request.app.state
    config = estado.config
    if getattr(estado, "limite_ip_config", None) is not config:
        # Também permite testes ASGI sem lifespan que instalam a própria config.
        estado.limite_ip = LimitadorPorIP()
        estado.limite_ip_config = config
    return estado.limite_ip.admitir(ip_do_cliente(request))


class CORSConfigurado:
    """Lê APP_ORIGIN depois do lifespan, sem exigir configuração no import.

    A instância de CORSMiddleware é local a cada requisição; mudar a configuração
    entre ciclos de vida não compartilha origens nem estado entre requisições.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        config = getattr(scope["app"].state, "config", None)
        origem = config.app_origin if config is not None else None
        cors = CORSMiddleware(
            self.app,
            allow_origins=[origem] if origem and origem != "*" else [],
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "Accept"],
            expose_headers=["Retry-After"],
            allow_credentials=False,
        )
        await cors(scope, receive, send)
