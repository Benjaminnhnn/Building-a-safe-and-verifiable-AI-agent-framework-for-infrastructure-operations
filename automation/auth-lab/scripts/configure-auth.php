<?php
// Run only against the disposable local auth-lab Moodle database.
define('CLI_SCRIPT', true);
require('/var/www/html/config.php');

$readerpassword = trim(file_get_contents('/run/secrets/ldap_reader_password'));
if ($readerpassword === '') {
    fwrite(STDERR, "LDAP reader secret is empty\n");
    exit(64);
}

set_config('auth', 'manual,ldap');
$settings = [
    'host_url' => 'ldaps://openldap:636',
    'ldap_version' => '3',
    'start_tls' => '0',
    'ldapencoding' => 'utf-8',
    'preventpassindb' => '1',
    'bind_dn' => 'cn=moodle-reader,dc=example,dc=org',
    'bind_pw' => $readerpassword,
    'user_type' => 'posix',
    'contexts' => 'ou=moodleusers,dc=example,dc=org',
    'search_sub' => '0',
    'user_attribute' => 'uid',
    'objectclass' => 'posixAccount',
    'auth_user_create' => '1',
];
foreach ($settings as $name => $value) {
    set_config($name, $value, 'auth_ldap');
}
purge_all_caches();
fwrite(STDOUT, "Moodle LDAP auth configured for local LDAPS; no password printed.\n");
