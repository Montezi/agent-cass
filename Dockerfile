# Imagem do agent-cass para o Cloud Run.
# Sem o Strands Decider (passo 11): nada de PyTorch nem modelo local na imagem.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependências primeiro: a camada fica em cache enquanto o requirements.txt não muda.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Só o que o serviço precisa. Nada de .env: os segredos vêm do ambiente do Cloud Run.
COPY cass/ cass/
COPY skills/ skills/
COPY start.sh .

# Roda sem root; os arquivos da imagem ficam só para leitura desse usuário.
RUN chmod 755 start.sh && useradd --create-home --uid 10001 cass
USER cass

ENV PORT=8080
EXPOSE 8080
CMD ["./start.sh"]
