FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*
COPY . /w
WORKDIR /w
RUN pip install --no-cache-dir -r requirements.txt
CMD ["python3", "src/reproduce.py"]
