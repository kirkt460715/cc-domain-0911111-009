FROM python:3.11-slim-bookworm
WORKDIR /opt/product
COPY . .
CMD ["sh", "-c", "mkdir -p /tmp/product-run && cp -R /opt/product/. /tmp/product-run/ && cd /tmp/product-run && python3 -m unittest discover -s tests -v && python3 -m compileall -q meterbook && python3 -m meterbook replay fixtures/settlement.json"]
