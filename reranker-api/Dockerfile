FROM python:3.11-slim

WORKDIR /app

# Установить Poetry
RUN pip install --no-cache-dir poetry

# Копировать poetry файлы
COPY pyproject.toml poetry.lock* ./

# Установить зависимости через Poetry
RUN poetry config virtualenvs.create false && \
    poetry install --no-interaction --no-ansi

# Копировать приложение
COPY app.py .

# Запустить API
EXPOSE 8081

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8081"]
