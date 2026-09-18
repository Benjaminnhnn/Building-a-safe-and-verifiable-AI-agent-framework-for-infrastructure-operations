#!/usr/bin/env sh
set -eu

required_var() {
    var_name="$1"
    eval "var_value=\${$var_name:-}"
    if [ -z "$var_value" ]; then
        echo "Required environment variable is empty: $var_name" >&2
        exit 64
    fi
}

required_file() {
    var_name="$1"
    eval "file_path=\${$var_name:-}"
    if [ -z "$file_path" ] || [ ! -r "$file_path" ]; then
        echo "Required readable file is missing: $var_name" >&2
        exit 64
    fi
}

php_env_literal() {
    php -r 'echo var_export(getenv($argv[1]), true);' "$1"
}

required_var MOODLE_WWWROOT
required_var MOODLE_DB_HOST
required_var MOODLE_DB_NAME
required_var MOODLE_DB_USER
required_var MOODLE_DATA_ROOT
required_file MOODLE_DB_PASSWORD_FILE

case "$MOODLE_WWWROOT" in
    http://*|https://*) ;;
    *)
        echo 'MOODLE_WWWROOT must start with http:// or https://' >&2
        exit 64
        ;;
esac

case "${MOODLE_SSL_PROXY:-false}" in
    true|false) ;;
    *)
        echo 'MOODLE_SSL_PROXY must be true or false' >&2
        exit 64
        ;;
esac

mkdir -p "$MOODLE_DATA_ROOT" "$MOODLE_DATA_ROOT/cache" "$MOODLE_DATA_ROOT/localcache" \
    "$MOODLE_DATA_ROOT/sessions" "$MOODLE_DATA_ROOT/temp" "$MOODLE_DATA_ROOT/trashdir"
chmod 0770 "$MOODLE_DATA_ROOT" "$MOODLE_DATA_ROOT/cache" "$MOODLE_DATA_ROOT/localcache" \
    "$MOODLE_DATA_ROOT/sessions" "$MOODLE_DATA_ROOT/temp" "$MOODLE_DATA_ROOT/trashdir"

db_password_literal="$(php -r '$value = rtrim(stream_get_contents(STDIN), "\\r\\n"); if ($value === "") { exit(1); } echo var_export($value, true);' < "$MOODLE_DB_PASSWORD_FILE")"
if [ -z "$db_password_literal" ]; then
    echo 'Moodle database password must not be empty' >&2
    exit 64
fi

umask 027
cat > /var/www/html/config.php <<EOF
<?php
unset(\$CFG);
global \$CFG;
\$CFG = new stdClass();
\$CFG->dbtype = 'pgsql';
\$CFG->dblibrary = 'native';
\$CFG->dbhost = $(php_env_literal MOODLE_DB_HOST);
\$CFG->dbname = $(php_env_literal MOODLE_DB_NAME);
\$CFG->dbuser = $(php_env_literal MOODLE_DB_USER);
\$CFG->dbpass = ${db_password_literal};
\$CFG->prefix = 'mdl_';
\$CFG->dboptions = [
    'dbpersist' => false,
    'dbsocket' => false,
    'dbport' => $(php_env_literal MOODLE_DB_PORT),
    'ssl' => 'verify-full',
];
\$CFG->wwwroot = $(php_env_literal MOODLE_WWWROOT);
\$CFG->dataroot = $(php_env_literal MOODLE_DATA_ROOT);
\$CFG->directorypermissions = 02770;
\$CFG->reverseproxy = true;
\$CFG->sslproxy = ${MOODLE_SSL_PROXY};
require_once(__DIR__ . '/lib/setup.php');
EOF

exec docker-php-entrypoint "$@"
