# Use a minimal Alpine Linux base image.
FROM alpine:latest

# --- Install Dependencies ---
RUN apk update && apk add \
    dnsmasq \
    lighttpd \
    iptables \
    jq \
    openssl

# Clean up the apk cache in a separate step after installation is complete.
RUN rm -rf /var/cache/apk/*

# --- Web Server Setup ---
# Create the web root and cgi-bin directory.
RUN mkdir -p /var/www/localhost/htdocs/cgi-bin

# Copy the configuration files for our services into the image.
COPY lighttpd.conf /etc/lighttpd/lighttpd.conf
COPY dnsmasq.conf /etc/dnsmasq.conf

# Copy the web interface and CGI scripts.
COPY index.html /var/www/localhost/htdocs/index.html
COPY save_config.cgi /var/www/localhost/htdocs/cgi-bin/save_config.cgi
COPY list_configs.cgi /var/www/localhost/htdocs/cgi-bin/list_configs.cgi
COPY publish_config.cgi /var/www/localhost/htdocs/cgi-bin/publish_config.cgi
COPY delete_config.cgi /var/www/localhost/htdocs/cgi-bin/delete_config.cgi
COPY clear_stats.cgi /var/www/localhost/htdocs/cgi-bin/clear_stats.cgi
COPY log_processor.sh /log_processor.sh
COPY entrypoint.sh /entrypoint.sh

# --- Data Persistence Setup ---
# Create a symlink so the persistent /data volume is accessible from the web root.
# This allows clients to download the active.conf file.
RUN ln -s /data /var/www/localhost/htdocs/data

# --- Permissions and Execution ---
# Make all our custom scripts executable.
RUN chmod +x /entrypoint.sh \
             /log_processor.sh \
             /var/www/localhost/htdocs/cgi-bin/*.cgi

# Create the log directory and give ownership to the lighttpd user.
RUN mkdir -p /var/log/lighttpd && chown -R lighttpd:lighttpd /var/log/lighttpd

# --- Container Execution ---
# Expose the web server port
EXPOSE 8081

# Define the command to run when the container starts.
CMD ["/entrypoint.sh"]

