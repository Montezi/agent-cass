# Imagem do agent-cass para o Cloud Run.
# A variante padrão continua leve; INCLUIR_DECIDER=true acrescenta o servidor CPU.
FROM python:3.12-slim

ARG INCLUIR_DECIDER=false
RUN case "$INCLUIR_DECIDER" in true|false) ;; \
    *) echo "INCLUIR_DECIDER deve ser true ou false." >&2; exit 1 ;; esac

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DECIDER_ATIVO=${INCLUIR_DECIDER} \
    DECIDER_INCLUIDO=${INCLUIR_DECIDER} \
    DECIDER_MODELO=/opt/decider-model \
    HF_HOME=/opt/decider-cache \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HUB_DISABLE_IMPLICIT_TOKEN=1 \
    OMP_NUM_THREADS=4 \
    MKL_NUM_THREADS=4 \
    OPENBLAS_NUM_THREADS=1

WORKDIR /app

# Dependências primeiro: a camada fica em cache enquanto o requirements.txt não muda.
COPY requirements.txt requirements-decider.txt ./
RUN pip install -r requirements.txt \
    && if [ "$INCLUIR_DECIDER" = "true" ]; then \
         pip install --index-url https://download.pytorch.org/whl/cpu "torch==2.7.1+cpu" \
         && SETUPTOOLS_SCM_PRETEND_VERSION=0.3.0+git.79bf6e7 pip install -r requirements-decider.txt \
         && python -c "import torch; assert torch.version.cuda is None, 'PyTorch precisa ser somente CPU'"; \
       else \
         python -c "from importlib.util import find_spec; assert find_spec('torch') is None, 'A variante leve não deve instalar PyTorch'"; \
       fi

# Cacheia tanto LoRA/head quanto a base fixa; o runtime usa o snapshot local offline.
COPY scripts/baixar_modelo_decider.py ./baixar_modelo_decider.py
RUN if [ "$INCLUIR_DECIDER" = "true" ]; then \
      python baixar_modelo_decider.py \
      && chmod -R a+rX /opt/decider-cache; \
    fi

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
