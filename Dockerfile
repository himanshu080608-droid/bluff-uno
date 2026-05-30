# Use Python 3.11 slim image (smaller and faster)
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# 1. Copy only requirements first (for better caching)
COPY requirements.txt .

# 2. Install dependencies
# [standard] is crucial for WebSocket support
RUN pip install --no-cache-dir -r requirements.txt

# 3. Copy the rest of your application code
COPY . .

# 4. Run the server
# We explicitly tell it to run server:app on port 8000
# This overrides any internal uvicorn.run() logic
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]