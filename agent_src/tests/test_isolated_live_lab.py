from __future__ import annotations

import importlib.util
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('isolated_lab', ROOT / 'automation/moodle-isolated-live-lab.py')
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


def test_lab_detects_a_real_dependency_shutdown_and_restoration():
    dependency = lab.start_dependency()
    app = ThreadingHTTPServer(('127.0.0.1', 0), lab.LabHandler)
    app.dependencies = {'database': f'http://127.0.0.1:{dependency[0].server_port}/', 'related': 'http://127.0.0.1:1/'}
    app_thread = threading.Thread(target=app.serve_forever, daemon=True)
    app_thread.start()
    base = f'http://127.0.0.1:{app.server_port}'
    try:
        assert lab.read_http(base, '/health') == 200
        assert lab.read_http(base, '/transaction') == 200
        lab.stop_dependency(dependency)
        assert lab.read_http(base, '/health') == 200
        assert lab.read_http(base, '/transaction') == 503
        replacement = lab.start_dependency()
        try:
            app.dependencies['database'] = f'http://127.0.0.1:{replacement[0].server_port}/'
            assert lab.read_http(base, '/transaction') == 200
        finally:
            lab.stop_dependency(replacement)
    finally:
        app.shutdown()
        app.server_close()
        app_thread.join(timeout=5)
