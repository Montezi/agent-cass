"""Regressões AppSec sem provedores: recursos, Unicode, logs e identidade."""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cass import main
from cass.limites import LimiteDeCorpo
from cass.modelos import PedidoAgente, DiaDePrevisao
from cass.seguranca_http import Admissao, SegurancaHTTP


@pytest.mark.parametrize('texto', ['   ', '\ud800', '\udfff', '😀' * 251])
def test_pergunta_invalida_nao_chega_ao_modelo(texto):
    with pytest.raises(ValidationError):
        PedidoAgente(question=texto, language='pt')


def test_limite_utf16_aceita_250_emojis():
    assert PedidoAgente(question='😀' * 250, language='en').question == '😀' * 250


@pytest.mark.parametrize('valor', [float('inf'), float('-inf'), float('nan')])
def test_medicao_nao_aceita_nao_finitos(valor):
    with pytest.raises(ValidationError):
        DiaDePrevisao(date='2026-11-15', precipitation_mm=valor)


@pytest.mark.parametrize('rota', ['/api/agente', '/api/agente/stream'])
def test_erro_de_validacao_nao_reflete_unicode_invalido(monkeypatch, rota):
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'teste')
    monkeypatch.setattr(main, 'responder', lambda *args, **kwargs: pytest.fail('Não deve chamar modelo'))
    with TestClient(main.app) as cliente:
        r = cliente.post(rota, content=b'{"question":"\\ud800","language":"pt"}',
                         headers={'Content-Type': 'application/json'})
    assert r.status_code == 422
    assert 'input' not in r.json()['detail'][0]
    assert r.headers['x-content-type-options'] == 'nosniff'
    assert r.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('rota', ['/api/agente', '/api/agente/stream'])
def test_erro_do_provedor_nao_vaza_conteudo_em_logs(monkeypatch, caplog, rota):
    async def falhar(*args, **kwargs):
        raise RuntimeError('MARCADOR_PRIVADO: pergunta e resposta do provedor')
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'teste')
    monkeypatch.setattr(main, '_responder_protegido', falhar)
    with caplog.at_level(logging.ERROR), TestClient(main.app) as cliente:
        r = cliente.post(rota, json={'question': 'Radar?', 'language': 'pt'})
    assert 'MARCADOR_PRIVADO' not in r.text
    assert 'MARCADOR_PRIVADO' not in caplog.text
    assert 'RuntimeError' in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_corpo_lento_expira_antes_do_json():
    async def testar():
        enviadas = []
        async def receber():
            await asyncio.sleep(1)
            return {'type': 'http.request', 'body': b' ', 'more_body': True}
        async def enviar(mensagem):
            enviadas.append(mensagem)
        async def app(*args):
            pytest.fail('O parser não deve receber um corpo incompleto')
        await LimiteDeCorpo(app, timeout_s=0.01)(
            {'type': 'http', 'path': '/api/agente', 'method': 'POST', 'headers': []}, receber, enviar)
        assert enviadas[0]['status'] == 408
    asyncio.run(testar())


def test_limite_agregado_persiste_mesmo_com_ips_diferentes():
    async def testar():
        estado = SimpleNamespace(config=object())
        async def responder(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})
        middleware = SegurancaHTTP(responder)
        async def receber():
            return {'type': 'http.request', 'body': b''}
        for i in range(31):
            enviadas = []
            async def enviar(m):
                enviadas.append(m)
            await middleware({'type': 'http', 'method': 'POST',
                'path': '/api/agente/stream' if i % 2 else '/api/agente',
                'app': SimpleNamespace(state=estado), 'client': (f'198.51.100.{i}', 1234)}, receber, enviar)
            assert enviadas[0]['status'] == (200 if i < 30 else 429)
        assert estado.admissao.ativos == 0
    asyncio.run(testar())


def test_vaga_conta_sse_aberto_e_e_liberada_no_cancelamento():
    async def testar():
        config = object()
        admissao = Admissao(simultaneos=1)
        estado = SimpleNamespace(config=config, admissao=admissao, admissao_config=config)
        iniciou = asyncio.Event()
        async def responder(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            iniciou.set()
            await asyncio.Event().wait()
        middleware = SegurancaHTTP(responder)
        async def receber():
            return {'type': 'http.request', 'body': b''}
        mensagens = []
        async def enviar(m):
            mensagens.append(m)
        scope = {'type': 'http', 'method': 'POST', 'path': '/api/agente/stream',
                 'app': SimpleNamespace(state=estado)}
        tarefa = asyncio.create_task(middleware(scope, receber, enviar))
        await iniciou.wait()
        await middleware(scope, receber, enviar)
        assert mensagens[-2]['status'] == 429
        tarefa.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarefa
        assert admissao.ativos == 0
        assert admissao.entrar() is None
        admissao.sair()
    asyncio.run(testar())


@pytest.mark.parametrize('rota', ['/api/agente', '/api/agente/stream'])
def test_conferencia_de_hotspot_participa_do_prazo_total(monkeypatch, rota):
    import time
    def ids_lentos():
        time.sleep(0.08)
        return ['guaiba-rs']
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'teste')
    monkeypatch.setattr(main, 'TEMPO_LIMITE_S', 0.01)
    monkeypatch.setattr(main, 'ids_dos_hotspots', ids_lentos)
    monkeypatch.setattr(main, 'responder', lambda *args, **kwargs: pytest.fail('Prazo já expirou'))
    with TestClient(main.app) as cliente:
        r = cliente.post(rota, json={'question': 'Radar?', 'language': 'pt', 'hotspot_id': 'guaiba-rs'})
    assert r.status_code == 504
