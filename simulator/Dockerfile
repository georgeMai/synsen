FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv/simulator
RUN pip install "paho-mqtt>=2.1,<3"
COPY simulator.py .
CMD ["python", "simulator.py"]
