# Use Python 3.14 slim image (supports both ARM64 and AMD64)
FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy all agent source files
COPY *.py .

# Expose the port that BedrockAgentCoreApp runs on
EXPOSE 8080

ENV PYTHONUNBUFFERED=1

# Run the application
CMD ["opentelemetry-instrument", "python", "app.py"]