FROM debian:12-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    postfix \
    dovecot-core \
    python3 \
    python3-pip \
    openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# Static Postfix and Dovecot configs
COPY postfix/master.cf /etc/postfix/master.cf
COPY dovecot/dovecot.conf /etc/dovecot/dovecot.conf

# Application scripts
COPY app/start.sh app/configure.py app/oauth_deliver.py ./
RUN chmod +x /app/start.sh /app/oauth_deliver.py && \
    chmod 755 /app/configure.py && \
    mkdir -p /data/certs /data/logs /config && \
    chmod 755 /data /data/logs && \
    touch /etc/dovecot/users

EXPOSE 587

CMD ["/app/start.sh"]
