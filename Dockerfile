FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy dependencies first (layer cache optimisation)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Port the API listens on
EXPOSE 8000

# Run the FastAPI app via uvicorn
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
