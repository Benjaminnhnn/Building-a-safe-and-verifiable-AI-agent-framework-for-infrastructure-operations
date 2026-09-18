#!/usr/bin/env sh
set -eu

interval="${MOODLE_CRON_INTERVAL_SECONDS:-60}"
case "$interval" in
    *[!0-9]*|'')
        echo 'MOODLE_CRON_INTERVAL_SECONDS must be a positive integer' >&2
        exit 64
        ;;
esac

while true; do
    php /var/www/html/admin/cli/cron.php --non-interactive
    sleep "$interval"
done
