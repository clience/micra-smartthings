FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY micra_bridge ./micra_bridge
RUN pip install --no-cache-dir .
USER 1000:1000
ENTRYPOINT ["micra-bridge", "--config", "/data/config.json"]
CMD ["serve", "--host", "0.0.0.0"]
