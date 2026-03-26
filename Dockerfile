FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    lighttpd \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip install --no-cache-dir meshtastic websockets

# Create web directory
RUN mkdir -p /var/www/html/meshtastic /var/log/lighttpd /var/cache/lighttpd/uploads

# Copy backend
COPY backend/ /app/backend/

# Copy frontend to web root
COPY frontend/ /var/www/html/meshtastic/

# Copy docker support files
COPY docker/lighttpd.conf /etc/lighttpd/lighttpd.conf
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

WORKDIR /app

EXPOSE 80 8765

ENTRYPOINT ["/app/entrypoint.sh"]
