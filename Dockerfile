FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
# Playwright + Chromium already bundled in the base image

COPY . .
CMD ["python", "monitor.py"]
