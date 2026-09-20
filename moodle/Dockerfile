# syntax=docker/dockerfile:1.7
# The digest pins the PHP 8.4 Apache base selected for Moodle 5.2.3.
FROM php:8.4-apache-bookworm@sha256:209a8584f5297c7d9d81ec72e44c59e85a2f5f488a01113482687fe1655671e8

ARG MOODLE_VERSION=5.2.3
ARG MOODLE_GIT_SHA=344232c15336c71b80f9aca8359ce0e0a9f3d116

LABEL org.opencontainers.image.title="Moodle LMS" \
      org.opencontainers.image.version="${MOODLE_VERSION}" \
      org.opencontainers.image.source="https://github.com/moodle/moodle" \
      org.opencontainers.image.revision="${MOODLE_GIT_SHA}"

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ca-certificates \
        curl \
        git \
        libcurl4-openssl-dev \
        libfreetype6-dev \
        libicu-dev \
        libjpeg62-turbo-dev \
        libonig-dev \
        libpng-dev \
        libpq-dev \
        libxml2-dev \
        libzip-dev \
        unzip \
    && docker-php-ext-configure gd --with-freetype --with-jpeg \
    && docker-php-ext-install -j"$(nproc)" curl gd intl mbstring opcache pgsql soap zip \
    && a2enmod expires headers remoteip rewrite \
    && sed -ri 's/^Listen 80$/Listen 8080/' /etc/apache2/ports.conf \
    && sed -ri 's/<VirtualHost \*:80>/<VirtualHost *:8080>/' /etc/apache2/sites-available/000-default.conf \
    && sed -ri 's#DocumentRoot /var/www/html#DocumentRoot /var/www/html/public#' /etc/apache2/sites-available/000-default.conf \
    && rm -rf /var/lib/apt/lists/*

RUN chown root:root /var/www/html \
    && git init /var/www/html \
    && git -C /var/www/html remote add origin https://github.com/moodle/moodle.git \
    && git -C /var/www/html fetch --depth=1 origin "${MOODLE_GIT_SHA}" \
    && git -C /var/www/html checkout --detach FETCH_HEAD \
    && test "$(git -C /var/www/html rev-parse HEAD)" = "${MOODLE_GIT_SHA}" \
    && rm -rf /var/www/html/.git \
    && groupadd --gid 1000 moodle \
    && useradd --uid 1000 --gid moodle --home-dir /var/moodledata --shell /usr/sbin/nologin moodle \
    && mkdir -p /var/moodledata /var/run/apache2 /var/lock/apache2 /var/log/apache2 \
    && chown -R moodle:moodle /var/www/html /var/moodledata /var/run/apache2 /var/lock/apache2 /var/log/apache2 \
    && sed -ri 's/^export APACHE_RUN_USER=.*/export APACHE_RUN_USER=moodle/' /etc/apache2/envvars \
    && sed -ri 's/^export APACHE_RUN_GROUP=.*/export APACHE_RUN_GROUP=moodle/' /etc/apache2/envvars

COPY apache-moodle-router.conf /etc/apache2/conf-available/moodle-router.conf
RUN a2enconf moodle-router \
    && apache2ctl -t

COPY --chown=moodle:moodle docker-entrypoint.sh cron-runner.sh moodle-install.sh healthz.php synthetic-transaction.php /usr/local/bin/
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh /usr/local/bin/cron-runner.sh /usr/local/bin/moodle-install.sh \
    && mv /usr/local/bin/healthz.php /var/www/html/public/healthz.php \
    && mv /usr/local/bin/synthetic-transaction.php /var/www/html/public/synthetic-transaction.php \
    && php -l /var/www/html/public/synthetic-transaction.php \
    && printf '%s\n' \
        'memory_limit = 256M' \
        'max_execution_time = 300' \
        'max_input_vars = 5000' \
        'post_max_size = 64M' \
        'upload_max_filesize = 64M' \
        'opcache.enable = 1' \
        'opcache.validate_timestamps = 0' \
        > /usr/local/etc/php/conf.d/moodle.ini \
    && apache2ctl -t

USER 1000:1000
EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["apache2-foreground"]
