FROM python:3.10-slim

USER root

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY exporter.py /app/exporter.py
COPY dns.py /app/dns.py
COPY nodes.py /app/nodes.py
COPY merger.py /app/merger.py


# no cmd
CMD ["exit", "0"]