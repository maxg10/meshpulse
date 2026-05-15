FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    lighttpd \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip install --no-cache-dir meshtastic websockets

# Create directories
RUN mkdir -p /var/www/html/meshpulse /var/log/lighttpd /var/cache/lighttpd/uploads

# Copy backend
COPY backend/ /app/backend/

# Copy mapper module (plugin system)
COPY mapper/ /app/mapper/

# Copy frontend to web root
COPY frontend/ /var/www/html/meshpulse/

# Keep a copy of frontend files in image for volume-safe updates
COPY frontend/ /app/frontend_dist/

# Create plugins directory (persistent via volume)
RUN mkdir -p /var/www/html/meshpulse/plugins

# Copy docker support files
COPY docker/lighttpd.conf /etc/lighttpd/lighttpd.conf
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

WORKDIR /app

EXPOSE 80 8765

ENTRYPOINT ["/app/entrypoint.sh"]
