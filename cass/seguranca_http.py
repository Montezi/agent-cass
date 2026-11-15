"""Admissão local e cabeçalhos da API pública, sem confiar em identidade externa."""

from threading import Lock

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cass.limites import LimitadorPorIP, ROTAS_COM_LIMITE_DE_CORPO

MAX_PEDIDOS_POR_MINUTO = 30
MAX_PEDIDOS_SIMULTANEOS = 4


class Admissao:
    """Freio por processo: persiste entre IPs, inclusive quando são falsificados."""

    def __init__(self, por_minuto: int = MAX_PEDIDOS_POR_MINUTO,
                 simultaneos: int = MAX_PEDIDOS_SIMULTANEOS) -> None:
        self.cota = LimitadorPorIP(limite=por_minuto, max_ips=1)
        self.simultaneos = simultaneos
        self.ativos = 0
        self.trava = Lock()

    def entrar(self) -> int | None:
        with self.trava:
            if self.ativos >= self.simultaneos:
                return 1
            espera = self.cota.admitir("processo")
            if espera is not None:
                return espera
            self.ativos += 1
            return None

    def sair(self) -> None:
        with self.trava:
            self.ativos -= 1


class SegurancaHTTP:
    """Mantém a vaga até encerrar JSON/SSE, também em erro ou cancelamento."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def enviar(mensagem: Message) -> None:
            if mensagem["type"] == "http.response.start":
                headers = [(k, v) for k, v in mensagem.get("headers", [])
                           if k.lower() not in {b"x-content-type-options", b"cache-control"}]
                mensagem = {**mensagem, "headers": [*headers,
                    (b"x-content-type-options", b"nosniff"),
                    (b"cache-control", b"no-store"),
                ]}
            await send(mensagem)

        admissao = None
        if scope["method"] == "POST" and scope["path"] in ROTAS_COM_LIMITE_DE_CORPO:
            estado = scope["app"].state
            config = getattr(estado, "config", None)
            if not hasattr(estado, "admissao") or getattr(estado, "admissao_config", None) is not config:
                estado.admissao = Admissao()
                estado.admissao_config = config
            admissao = estado.admissao
            espera = admissao.entrar()
            if espera is not None:
                await JSONResponse(status_code=429, headers={"Retry-After": str(espera)}, content={
                    "paragraphs": ["Cass está ocupado. Tente novamente em instantes. / Cass is busy. Please try again shortly."],
                    "tools": [], "sources": [], "note": None,
                })(scope, receive, enviar)
                return
        try:
            await self.app(scope, receive, enviar)
        finally:
            if admissao is not None:
                admissao.sair()
