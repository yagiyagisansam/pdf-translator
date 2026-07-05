# Container image for the PDF EN->JA translator web app.
# Works on any container host (Hugging Face Spaces, Render, Cloud Run, Railway).
FROM python:3.11-slim

# Noto CJK fonts are required to embed Japanese in the output PDF.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-noto-cjk \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for better layer caching.
COPY pdf-translator/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# App code (src/, data/, samples/).
COPY pdf-translator/ ./

# Hosts inject $PORT; default 7860 (Hugging Face Spaces). Bind all interfaces.
ENV HOST=0.0.0.0 \
    PORT=7860 \
    PYTHONUNBUFFERED=1 \
    PDF_TRANSLATOR_JOBS=/tmp/webjobs

EXPOSE 7860

CMD ["python", "src/webapp.py"]
