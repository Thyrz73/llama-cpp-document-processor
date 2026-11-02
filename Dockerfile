FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt ./
# Tesseract is invoked only for pages whose text layer cannot be extracted.
# Language packs are local and work offline after the image is built.
RUN apt-get update && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-fra tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./
COPY document_processor ./document_processor
COPY templates ./templates
COPY static ./static
COPY scripts ./scripts
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin appuser
USER 1000:1000
EXPOSE 9932
CMD ["python", "-u", "app.py"]
