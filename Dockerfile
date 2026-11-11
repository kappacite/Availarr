FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY availarr ./availarr
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 1000 availarr \
    && mkdir -p /data /config && chown availarr /data
USER availarr
EXPOSE 8787 8788 8789
CMD ["python", "-m", "availarr.server"]
