FROM python:3.12-slim-bookworm

WORKDIR /app

# HEIC support for pillow-heif
RUN apt-get update \
    && apt-get install -y --no-install-recommends libheif1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY logging_config.py process_bulletin_board.py select_photo.py telegram_bot.py ./

RUN mkdir -p /app/input

ENV PYTHONUNBUFFERED=1 \
    MONGO_URI=mongodb://mongo:27017 \
    MONGO_DB=uniroom-data \
    MONGO_COLLECTION=housing_listings \
    MONGO_FILES_COLLECTION=reviewed_files

CMD ["python", "select_photo.py"]
