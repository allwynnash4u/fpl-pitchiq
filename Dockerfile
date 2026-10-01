FROM python:3.12-slim
WORKDIR /app
COPY pitchiq_server.py /app/pitchiq_server.py
ENV PYTHONUNBUFFERED=1
CMD ["python","/app/pitchiq_server.py"]