<?php
// Authenticated, staging-only fixture endpoint for the Moodle synthetic probe.
// It exercises both PostgreSQL (config_plugins) and the shared moodledata EFS.

define('AJAX_SCRIPT', true);
require dirname(__DIR__) . '/config.php';

require_login();

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

function synthetic_response(int $status, array $payload): never {
    http_response_code($status);
    echo json_encode($payload, JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR);
    exit;
}

$action = optional_param('action', 'read', PARAM_ALPHA);
$fixtureid = required_param('fixture_id', PARAM_ALPHANUMEXT);
if (!preg_match('/^[a-zA-Z0-9_-]{8,64}$/', $fixtureid)) {
    synthetic_response(400, ['status' => 'error', 'reason' => 'invalid_fixture_id']);
}

$configkey = 'fixture_' . substr(hash('sha256', $fixtureid), 0, 32);
$fixturedir = $CFG->dataroot . '/synthetic-fixtures';
$fixturefile = $fixturedir . '/' . $configkey . '.txt';

if ($action === 'read') {
    $dbvalue = get_config('local_synthetic', $configkey);
    $filevalue = is_readable($fixturefile) ? file_get_contents($fixturefile) : false;
    $exists = $dbvalue !== false || $filevalue !== false;
    synthetic_response(200, [
        'status' => 'ok',
        'action' => 'read',
        'exists' => $exists,
        'consistent' => $dbvalue !== false && $filevalue !== false && hash_equals((string)$dbvalue, (string)$filevalue),
        'value' => $dbvalue === false ? null : (string)$dbvalue,
        'sesskey' => sesskey(),
    ]);
}

require_sesskey();

if ($action === 'delete') {
    unset_config($configkey, 'local_synthetic');
    if (is_file($fixturefile) && !unlink($fixturefile)) {
        synthetic_response(500, ['status' => 'error', 'reason' => 'efs_delete_failed']);
    }
    synthetic_response(200, ['status' => 'ok', 'action' => 'delete']);
}

if (!in_array($action, ['create', 'update'], true)) {
    synthetic_response(400, ['status' => 'error', 'reason' => 'unsupported_action']);
}

$value = required_param('value', PARAM_RAW_TRIMMED);
if ($value === '' || strlen($value) > 256) {
    synthetic_response(400, ['status' => 'error', 'reason' => 'invalid_value']);
}

$existing = get_config('local_synthetic', $configkey);
if ($action === 'create' && ($existing !== false || is_file($fixturefile))) {
    synthetic_response(409, ['status' => 'error', 'reason' => 'fixture_exists']);
}
if ($action === 'update' && ($existing === false || !is_file($fixturefile))) {
    synthetic_response(404, ['status' => 'error', 'reason' => 'fixture_missing']);
}

if (!is_dir($fixturedir) && !make_writable_directory($fixturedir)) {
    synthetic_response(500, ['status' => 'error', 'reason' => 'efs_directory_unavailable']);
}

if (!set_config($configkey, $value, 'local_synthetic')) {
    synthetic_response(500, ['status' => 'error', 'reason' => 'database_write_failed']);
}
if (file_put_contents($fixturefile, $value, LOCK_EX) === false) {
    unset_config($configkey, 'local_synthetic');
    synthetic_response(500, ['status' => 'error', 'reason' => 'efs_write_failed']);
}
chmod($fixturefile, 0660);

synthetic_response(200, ['status' => 'ok', 'action' => $action]);
