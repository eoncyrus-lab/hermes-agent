FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml ./
COPY sonder ./sonder
RUN pip install --no-cache-dir -e .

COPY skills ./skills
COPY migrations ./migrations
COPY evals ./evals

# Non-root: this container handles other people's private conversations.
RUN useradd --create-home --uid 10001 sonder && chown -R sonder:sonder /app
USER sonder

EXPOSE 8000
CMD ["uvicorn", "sonder.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
