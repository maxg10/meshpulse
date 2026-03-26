FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    lighttpd \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip install --no-cache-dir meshtastic websockets

# Create directories
RUN mkdir -p /var/www/html/meshtastic /app/data

# Copy backend
COPY backend/ /app/backend/

# Copy frontend to web root
COPY frontend/ /var/www/html/meshtastic/

# Copy lighttpd config
COPY docker/lighttpd.conf /etc/lighttpd/lighttpd.conf

# Copy entrypoint
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# nodes.json lives in /app/data (persistent volume)
# symlink so backend and frontend both find it
RUN ln -sf /app/data/nodes.json /var/www/html/meshtastic/nodes.json

WORKDIR /app

EXPOSE 80 8765

ENTRYPOINT ["/app/entrypoint.sh"]
