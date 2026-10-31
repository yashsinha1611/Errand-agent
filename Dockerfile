# For container platforms (e.g. Amazon ECS Express Mode). The app needs no packages.
# Note: SQLite lives in /app/var; mount persistent storage there, or data resets on every restart.
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN useradd --system app && mkdir -p var && chown -R app:app var
USER app
ENV ERRAND_DATA_DIR=/app/var ERRAND_DEMO=0
EXPOSE 8765
CMD ["python", "-m", "errand", "serve", "--no-supervise", "--host", "0.0.0.0", "--port", "8765"]
