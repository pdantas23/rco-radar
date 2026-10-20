# Robôs do RCO Radar (consomem radar.jobs). Um ou mais containers podem rodar ao mesmo tempo.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY radar ./radar
RUN pip install --no-cache-dir . && useradd -u 1000 radar
USER radar
# RADAR_DATABASE_URL (obrigatório), GOSOM_URL, GOSOM_DEPTH, APIFY_TOKEN, CASADOSDADOS_API_KEY, RADAR_DOLAR_BRL
CMD ["python", "-m", "radar.worker"]
