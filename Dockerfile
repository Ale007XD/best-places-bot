# Используем официальный образ Python
FROM python:3.11-slim

# Устанавливаем рабочую директорию в контейнере
WORKDIR /app

# Копируем файл с зависимостями
COPY requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код нашего бота в контейнер
COPY ./bot ./bot

# Указываем команду для запуска бота при старте контейнера
CMD ["python", "bot/main.py"]
