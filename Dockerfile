FROM python:3.12-alpine
RUN pip install --no-cache-dir "paho-mqtt>=2.0"
COPY gerber_mqtt_bridge.py /app/gerber_mqtt_bridge.py
CMD ["python", "-u", "/app/gerber_mqtt_bridge.py"]
