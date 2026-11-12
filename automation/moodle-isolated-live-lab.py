#!/usr/bin/env python3
"""Local controlled-fault lab. Never calls the staging executor or AWS.

Uses actual Gate/Verifier classes and loopback HTTP probes. The lab executor and
alert detector are separate from the deployed Moodle services. Never treat its
results as live Moodle or independent-verifier attestations.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'agent_src'), str(ROOT / 'automation/lib')]
from cryptography.hazmat.primitives import serialization
from core.evidence.adapters import SafeLiveActionRequest
from core.safety.action_catalog import ActionCatalog
from core.safety.safe_execution_gate import ApprovalRecord, SafeExecutionGate
from core.schema.incident import Incident
from core.schema.verification import ProbeResult, StabilityObservation
from core.verification.verification_agent import VerificationAgent
from telegram_client import TelegramClient, TelegramError

spec = importlib.util.spec_from_file_location('fixture_requests', ROOT / 'automation/moodle-high-risk-safety-demo.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)

class LabHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        dependencies = {'/transaction': 'database', '/related': 'related'}
        if self.path in ('/health', '/boundary'):
            status = 200
        elif self.path in dependencies:
            target = self.server.dependencies[dependencies[self.path]]
            try:
                with urllib.request.urlopen(target, timeout=2) as response:
                    status = 200 if response.status == 200 else 503
            except (urllib.error.URLError, TimeoutError):
                status = 503
        else:
            status = 404
        self.send_response(status)
        self.end_headers()
        self.wfile.write(b'lab probe\n')
    def log_message(self, *_args):
        pass

class DependencyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
    def log_message(self, *_args):
        pass

def start_dependency():
    server = ThreadingHTTPServer(('127.0.0.1', 0), DependencyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread

def stop_dependency(item):
    server, thread = item
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)

class LabProbe:
    def __init__(self, base, name, group, path):
        self.base, self.name, self.probe_type, self.path = base, name, group, path
    def check(self):
        try:
            with urllib.request.urlopen(self.base + self.path, timeout=3) as response:
                status = response.status
        except (urllib.error.URLError, TimeoutError):
            status = 0
        ok = status == 200
        return ProbeResult(name=self.name, passed=ok, simulated=False, details=f'loopback HTTP {status}')

def read_http(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=3) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code

def live_request(case, evidence_refs):
    data = fixture._request('NET-01', case).model_dump(mode='python', exclude={'dry_run'})
    data['action']['evidence_refs'] = evidence_refs
    nonce = uuid.uuid4().hex
    data.update(request_id='lab-' + nonce, idempotency_key='lab-' + nonce)
    return SafeLiveActionRequest(**data)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--telegram', action='store_true', help='send messages and wait for operator approval')
    parser.add_argument('--output', type=Path, default=ROOT / 'terraform/.artifacts/isolated-live-lab/report.json')
    args = parser.parse_args()
    if not args.telegram:
        parser.error('this interactive lab requires --telegram and operator approval')
    client = TelegramClient()
    if not client.configured or not client.admin_ids:
        raise RuntimeError('Telegram token, chat ID and operator user ID must be configured')
    private_path = Path(os.environ.get('MOODLE_OPERATOR_SIGNING_KEY_FILE', '~/.config/moodle-aiops/keys/operator.pem')).expanduser()
    private_pem = private_path.read_bytes()
    private_key = serialization.load_pem_private_key(private_pem, password=None)
    public_pem = private_key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    gate = SafeExecutionGate(approval_verification_key=public_pem, approved_actors={'operator'})
    server = ThreadingHTTPServer(('127.0.0.1', 0), LabHandler)
    dependencies = {'database': start_dependency(), 'related': start_dependency()}
    server.dependencies = {name: f'http://127.0.0.1:{item[0].server_port}/' for name, item in dependencies.items()}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    run_id = 'lab-' + uuid.uuid4().hex[:12]
    events = []
    def emit(text):
        print(text, flush=True)
        client.send('🧪 LAB CÔ LẬP · ' + run_id + '\n' + text)
    def event(kind, **fields):
        events.append({'at': datetime.now(timezone.utc).isoformat(), 'kind': kind, **fields})
    try:
        if any(read_http(base, p) != 200 for p in ('/health', '/transaction', '/related', '/boundary')):
            raise RuntimeError('lab baseline failed')
        stop_dependency(dependencies.pop('database'))
        observed = {path: read_http(base, '/' + path) for path in ('health', 'transaction', 'related')}
        if observed != {'health': 200, 'transaction': 503, 'related': 200}:
            raise RuntimeError('controlled lab fault was not observed as expected')
        event('real_controlled_fault', **observed)
        evidence_refs = [f'{run_id}-{key}-{value}' for key, value in observed.items()]
        emit('ALERT THẬT TRONG LAB: tiến trình phụ thuộc đã dừng, giao dịch HTTP 503. Đây là dịch vụ lab trên máy này, không phải Moodle/AWS.')
        dangerous = live_request('dangerous', evidence_refs).model_copy(update={'catalog_action_id': 'modify_route53_records'})
        rejected = gate.evaluate(dangerous, actor_role='executor', confidence=.72)
        assert rejected.decision == 'DENY'
        event('gate_dangerous', decision=rejected.decision, reason=rejected.reason)
        emit('SAFETY GATE: DENY thay đổi Route 53 ngoài catalog. Không dispatch. Đề xuất cố định do lab tạo, không phải AI sinh. Admin kiểm tra bằng chứng, phạm vi và rollback.')
        safe = live_request('safe', evidence_refs)
        empty_gate = SafeExecutionGate(approval_verification_key=public_pem, approved_actors={'operator'}, catalog=ActionCatalog([]))
        bad_policy = empty_gate.evaluate(safe, actor_role='executor', confidence=.72)
        normal_preview = gate.explain(safe, actor_role='executor', confidence=.72)
        assert bad_policy.decision == 'DENY' and normal_preview['decision'] == 'REQUIRE_APPROVAL'
        event('isolated_policy_fault', empty_catalog=bad_policy.decision, reviewed_catalog=normal_preview['decision'])
        emit('GATE CHẶN SAI TRONG LAB: catalog rỗng cố ý chặn yêu cầu có trong catalog chuẩn. Đối chiếu catalog chuẩn: yêu cầu chuyển sang chờ duyệt. Policy staging không bị sửa.')
        prepared = {'status': 'approval_required', 'execution_permitted': False, 'isolated_lab': True, 'gate_preview': normal_preview,
                    'request': safe.model_dump(mode='json'), 'executor_action_sha256': gate.action_hash(safe)}
        with tempfile.TemporaryDirectory(prefix='moodle-isolated-lab-') as tmp:
            prepared_path = Path(tmp) / 'prepared.json'
            prepared_path.write_text(json.dumps(prepared), encoding='utf-8')
            os.chmod(prepared_path, 0o600)
            # The production bridge owns Telegram polling and validates chat/operator.
            proc = subprocess.run([sys.executable, str(ROOT / 'automation/moodle-telegram-approval.py'),
                                   'approve', '--prepared', str(prepared_path)], capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError('operator did not approve lab request')
            approval = json.loads(proc.stdout)
            if approval.get('decision') != 'approved' or approval.get('request_id') != safe.request_id:
                raise RuntimeError('operator approval does not match lab request')
        signature = ApprovalRecord.create_ed25519(action_sha256=gate.action_hash(safe), actor_id='operator', private_key_pem=private_pem)
        verdict = gate.evaluate(safe, actor_role='executor', confidence=.72, approval=signature)
        if verdict.decision != 'ALLOW':
            raise RuntimeError('gate did not allow signed lab request')
        event('operator_approval', decision=verdict.decision)
        # Deliberately defective lab-only executor: repairs one dependency and breaks another.
        # It has no access to the staging executor and performs no AWS or Moodle operation.
        dependencies['database'] = start_dependency()
        server.dependencies['database'] = f'http://127.0.0.1:{dependencies["database"][0].server_port}/'
        stop_dependency(dependencies.pop('related'))
        event('lab_executor', transaction_http=read_http(base, '/transaction'), related_http=read_http(base, '/related'))
        emit('LAB EXECUTOR: giao dịch chính đã phục hồi; bài thử cố ý làm hỏng một phụ thuộc liên quan. Chưa RESOLVED.')
        contract = {'allowed': ['transaction'], 'forbidden': ['boundary'], 'related': ['related']}
        probes = [LabProbe(base, 'transaction', 'allowed', '/transaction'), LabProbe(base, 'boundary', 'forbidden', '/boundary'), LabProbe(base, 'related', 'related', '/related')]
        incident = Incident(incident_id=run_id, fingerprint=run_id, title='controlled lab dependency failure', severity='warning', affected_resources=['loopback-lab'])
        result = VerificationAgent().verify(incident, contract, dry_run=False, probes=probes, stability_observations=[])
        if read_http(base, '/health') != 200 or result.verdict != 'collateral_damage' or result.simulated:
            raise RuntimeError('verifier did not detect actual lab collateral failure')
        event('verifier_bad', verdict=result.verdict, health_http=200, related_http=503, simulated=result.simulated)
        emit('VERIFIER LAB: health HTTP 200, phụ thuộc HTTP 503 → collateral_damage; không RESOLVED. Đây là probe HTTP thật trong lab, không phải attestation Verifier Moodle.')
        dependencies['related'] = start_dependency()
        server.dependencies['related'] = f'http://127.0.0.1:{dependencies["related"][0].server_port}/'
        emit('LAB: đã reset phụ thuộc có chủ đích; bắt đầu đo ba mẫu thật trong 120 giây.')
        observations = []
        for index in range(3):
            if index: time.sleep(60)
            healthy = all(probe.check().passed for probe in probes)
            observations.append(StabilityObservation(observed_at=datetime.now(timezone.utc), healthy=healthy, simulated=False))
            event('sample', number=index + 1, healthy=healthy)
        result = VerificationAgent().verify(incident, contract, dry_run=False, probes=probes, stability_observations=observations, max_stability_gap_seconds=65)
        if result.verdict != 'resolved' or result.simulated:
            raise RuntimeError(f'lab verification failed: {result.verdict}')
        event('verifier_final', verdict=result.verdict, stability_seconds=result.stability_seconds, samples=3)
        emit(f'VERIFIER LAB: 3/3 mẫu đạt trong {result.stability_seconds} giây; verdict resolved của lab. Không cập nhật incident Moodle staging.')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump({'mode': 'isolated_loopback_lab', 'run_id': run_id, 'moodle_or_aws_changed': False,
                       'proposal_source': 'fixed_lab_proposal_not_ai', 'production_executor_used': False,
                       'production_verifier_attestation': False, 'events': events}, out, ensure_ascii=False, indent=2)
        print(f'Lab evidence: {args.output}', flush=True)
        return 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        for item in dependencies.values():
            stop_dependency(item)

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, TelegramError) as exc:
        print(f'Lab failed: {type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(1)
