# Use Python 3.11 slim image (smaller and faster)
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app
ENV PYTHONUNBUFFERED=1

# 1. Copy only requirements first (for better caching)
COPY requirements.txt .

# 2. Install dependencies
# [standard] is crucial for WebSocket support
RUN pip install --no-cache-dir -r requirements.txt

# 3. Copy the rest of your application code
COPY . .

# 4. Run the server
CMD ["sh", "-c", "exec uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000} --ws-ping-interval 30 --ws-ping-timeout 30"]
