<?php
declare(strict_types=1);

http_response_code(200);
header('Cache-Control: no-store');
header('Content-Type: text/plain; charset=utf-8');
echo "ok\n";
