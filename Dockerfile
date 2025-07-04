FROM python:3.10-slim

# Установка системных пакетов
RUN apt-get update && apt-get install -y \
    gcc build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Установка рабочей директории
WORKDIR /app

# Копируем все файлы проекта
COPY . .

# Установка Python зависимостей
RUN pip install --no-cache-dir -r requirements.txt

# Запуск бота
CMD ["python", "-m", "bot"]

