FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    MOR_DATA_DIR=/data

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY MORSELF.py manager_bot.py ./

RUN mkdir -p /data && chmod 700 /data

CMD ["python", "manager_bot.py"]
