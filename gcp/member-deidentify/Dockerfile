FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY gcp/member-deidentify/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY tools/deidentify-member-csv/deidentify.py ./deidentify.py
COPY gcp/member-deidentify/main.py ./main.py

RUN useradd --create-home --uid 10001 appuser
USER appuser

CMD ["python", "main.py"]
