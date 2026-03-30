FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUNBUFFERED=1 \
	PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
	&& apt-get install -y --no-install-recommends poppler-utils \
	&& rm -rf /var/lib/apt/lists/*

COPY webapp/requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip \
	&& pip install -r /tmp/requirements.txt

COPY . .

CMD ["sh", "-c", "gunicorn webapp.app:app --bind 0.0.0.0:${PORT:-10000} --timeout 300 --graceful-timeout 30 --workers 2"]
