#!/usr/bin/env sh
set -eu

if [ -z "${MOODLE_ADMIN_PASSWORD_FILE:-}" ] || [ ! -r "$MOODLE_ADMIN_PASSWORD_FILE" ]; then
    echo 'Required readable file is missing: MOODLE_ADMIN_PASSWORD_FILE' >&2
    exit 64
fi

for required_name in MOODLE_SITE_FULLNAME MOODLE_SITE_SHORTNAME MOODLE_SITE_SUMMARY MOODLE_ADMIN_USER MOODLE_ADMIN_EMAIL; do
    eval "required_value=\${$required_name:-}"
    if [ -z "$required_value" ]; then
        echo "Required environment variable is empty: $required_name" >&2
        exit 64
    fi
done

admin_password="$(tr -d '\r\n' < "$MOODLE_ADMIN_PASSWORD_FILE")"
if [ -z "$admin_password" ]; then
    echo 'Moodle admin password must not be empty' >&2
    exit 64
fi

exec php /var/www/html/admin/cli/install_database.php \
    --non-interactive \
    --agree-license \
    --fullname="$MOODLE_SITE_FULLNAME" \
    --shortname="$MOODLE_SITE_SHORTNAME" \
    --summary="$MOODLE_SITE_SUMMARY" \
    --adminuser="$MOODLE_ADMIN_USER" \
    --adminpass="$admin_password" \
    --adminemail="$MOODLE_ADMIN_EMAIL"
