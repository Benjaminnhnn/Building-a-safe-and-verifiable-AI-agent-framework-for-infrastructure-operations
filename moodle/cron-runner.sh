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
    started_at="$(date +%s)"
    php /var/www/html/admin/cli/cron.php --keep-alive=0
    finished_at="$(date +%s)"
    elapsed="$((finished_at - started_at))"

    if [ "$elapsed" -lt "$interval" ]; then
        sleep "$((interval - elapsed))"
    else
        echo "Cron runtime ${elapsed}s met or exceeded the ${interval}s interval; starting the next run immediately." >&2
    fi
done
