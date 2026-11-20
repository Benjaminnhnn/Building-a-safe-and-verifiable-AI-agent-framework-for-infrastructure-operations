# prometheus_check.py
# Utilities để kiểm lại Prometheus metrics và xác nhận issue đã resolve

import os
import logging
import requests
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# Prometheus configuration
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
PROMETHEUS_TIMEOUT = 10


class PrometheusChecker:
    """
    Kiểm lại Prometheus metrics để xác nhận issue đã resolve.
    
    Usage:
        checker = PrometheusChecker()
        is_resolved = checker.is_alert_resolved("node_cpu_high", "10.10.1.68")
    """
    
    def __init__(self, prometheus_url: str = PROMETHEUS_URL):
        self.base_url = prometheus_url.rstrip("/")
        self.query_url = f"{self.base_url}/api/v1/query"

    def query(self, query: str) -> list[dict]:
        try:
            response = requests.get(
                self.query_url,
                params={"query": query},
                timeout=PROMETHEUS_TIMEOUT,
            )
            response.raise_for_status()

            data = response.json()
            if data.get("status") != "success":
                logger.error("Prometheus query failed: %s", data)
                return []
            return data.get("data", {}).get("result", [])
        except Exception as e:
            logger.error("Error querying Prometheus: query=%s error=%s", query, e)
            return []

    @staticmethod
    def _label_value(labels: dict, key: str, default: str = "") -> str:
        value = labels.get(key, default)
        return str(value).replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _first_value(results: list[dict]) -> Optional[float]:
        if not results:
            return None
        value = results[0].get("value", [None, None])[1]
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    
    def get_metric(self, instance: str, metric_name: str) -> Optional[float]:
        """
        Query Prometheus để lấy current metric value.
        
        Example:
            - metric_name: "node_cpu_percent{instance='10.10.1.68'}"
            - Return: 45.5 (CPU percent)
        """
        try:
            query = f"{metric_name}{{instance='{instance}'}}"
            params = {"query": query}
            
            response = requests.get(
                self.query_url,
                params=params,
                timeout=PROMETHEUS_TIMEOUT
            )
            response.raise_for_status()
            
            data = response.json()
            if data.get("status") == "success":
                results = data.get("data", {}).get("result", [])
                if results:
                    value = results[0].get("value", [None, None])[1]
                    return float(value) if value else None
            
            return None
        except Exception as e:
            logger.error(f"Error querying Prometheus for {metric_name}: {e}")
            return None
    
    def is_alert_resolved(self, alert_name: str, instance: str, labels: Optional[dict] = None) -> bool:
        """
        Kiểm lại nếu alert đã resolve dựa trên alert name.
        
        Mapping:
        - HTTP endpoint alerts: matching blackbox probe_success == 1
        - DockerContainerDown: container_last_seen exists and is fresh
        - PostgreSQLDown / RedisDown: exporter target and service metric are up
        - Host resource alerts: current value is below its alert threshold
        """
        
        try:
            # Extract instance IP/hostname
            # instance format: "10.10.1.68:9100" or "10.10.1.68"
            host = instance.split(":")[0]
            
            labels = labels or {}
            component = self._label_value(labels, "component")
            runbook = self._label_value(labels, "runbook")

            if alert_name == "FrontendAPIProxyDown":
                instance_label = self._label_value(labels, "instance", instance)
                dependency = self._label_value(labels, "dependency", "payment-api-staging")
                dependency_instance = self._label_value(labels, "dependency_instance", "bank-core-01")
                condition_query = (
                    f'probe_success{{runbook="api-proxy",component="{component}",'
                    f'instance="{instance_label}"}} == 0 '
                    'and on() '
                    f'probe_success{{runbook="payment-api",component="{dependency}",'
                    f'instance="{dependency_instance}"}} == 1'
                )
                return not bool(self.query(condition_query))

            if alert_name in {"WebEndpointDown", "PaymentAPIEndpointDown"}:
                expected_runbook = {
                    "WebEndpointDown": "nginx",
                    "PaymentAPIEndpointDown": "payment-api",
                }[alert_name]
                query = (
                    f'probe_success{{runbook="{runbook or expected_runbook}",component="{component}",'
                    f'instance="{self._label_value(labels, "instance", instance)}"}}'
                )
                value = self._first_value(self.query(query))
                return value == 1

            elif alert_name == "DockerContainerDown":
                if not component:
                    logger.warning("DockerContainerDown missing component label; cannot verify resolved")
                    return False
                age_query = (
                    'time() - max by (instance) '
                    f'(container_last_seen{{name=~".*{component}",'
                    f'instance="{self._label_value(labels, "instance", instance)}"}})'
                )
                age_seconds = self._first_value(self.query(age_query))
                return age_seconds is not None and age_seconds <= 30

            elif alert_name == "PostgreSQLDown":
                instance_label = self._label_value(labels, "instance", instance)
                component_matcher = f',component="{component}"' if component else ""
                runbook_matcher = f',runbook="{runbook or "postgresql"}"'
                up = self._first_value(self.query(f'up{{instance="{instance_label}"{component_matcher}{runbook_matcher}}}'))
                pg_up = self._first_value(self.query(f'pg_up{{instance="{instance_label}"{component_matcher}{runbook_matcher}}}'))
                return up == 1 and pg_up == 1

            elif alert_name == "RedisDown":
                instance_label = self._label_value(labels, "instance", instance)
                component_matcher = f',component="{component}"' if component else ""
                runbook_matcher = f',runbook="{runbook or "redis"}"'
                up = self._first_value(self.query(f'up{{instance="{instance_label}"{component_matcher}{runbook_matcher}}}'))
                redis_up = self._first_value(self.query(f'redis_up{{instance="{instance_label}"{component_matcher}{runbook_matcher}}}'))
                return up == 1 and redis_up == 1

            elif alert_name in {"HighCPUUsage", "CriticalCPUUsage"}:
                instance_label = self._label_value(labels, "instance", instance)
                query = (
                    '100 - (avg by (instance) '
                    f'(irate(node_cpu_seconds_total{{mode="idle",instance="{instance_label}"}}[1m])) * 100)'
                )
                cpu = self._first_value(self.query(query))
                threshold = 95 if alert_name == "CriticalCPUUsage" else 80
                return cpu is not None and cpu < threshold

            elif alert_name in {"HighMemoryUsage", "CriticalMemoryUsage"}:
                instance_label = self._label_value(labels, "instance", instance)
                query = (
                    '100 - (node_memory_MemAvailable_bytes'
                    f'{{instance="{instance_label}"}} / node_memory_MemTotal_bytes'
                    f'{{instance="{instance_label}"}} * 100)'
                )
                memory = self._first_value(self.query(query))
                threshold = 95 if alert_name == "CriticalMemoryUsage" else 85
                return memory is not None and memory < threshold

            elif alert_name in {"HighDiskUsage", "CriticalDiskUsage"}:
                instance_label = self._label_value(labels, "instance", instance)
                query = (
                    '100 - (node_filesystem_avail_bytes'
                    f'{{mountpoint="/",instance="{instance_label}"}} / node_filesystem_size_bytes'
                    f'{{mountpoint="/",instance="{instance_label}"}} * 100)'
                )
                disk = self._first_value(self.query(query))
                threshold = 90 if alert_name == "CriticalDiskUsage" else 80
                return disk is not None and disk < threshold
            
            elif alert_name == "network_packet_loss":
                # Check if packet loss is below 1%
                loss = self.get_metric(host, "node_network_packet_loss_percent")
                threshold = 1
                return loss is not None and loss < threshold
            
            elif alert_name == "service_down":
                # Check if service is up (via port health check)
                # This would require specific port configuration
                logger.warning(f"Service check for {host} is not implemented; cannot verify resolved")
                return False

            else:
                logger.warning(f"Unknown alert type: {alert_name}")
                return False
        
        except Exception as e:
            logger.error(f"Error in is_alert_resolved: {e}")
            return False
    
    def get_alert_metrics(self, instance: str) -> Dict[str, Any]:
        """
        Lấy tất cả metrics liên quan để report.
        
        Return:
        {
            "cpu_percent": 45.5,
            "memory_percent": 62.3,
            "disk_percent": 70.1,
            "timestamp": "2026-04-28T14:53:00"
        }
        """
        from datetime import datetime
        
        host = instance.split(":")[0]
        metrics = {
            "timestamp": datetime.now().isoformat(),
            "cpu_percent": self.get_metric(host, "node_cpu_usage_percent"),
            "memory_percent": self.get_metric(host, "node_memory_usage_percent"),
            "disk_percent": self.get_metric(host, "node_disk_usage_percent"),
        }
        return metrics


# Singleton instance
_checker: Optional[PrometheusChecker] = None


def get_prometheus_checker() -> PrometheusChecker:
    """Get singleton instance of PrometheusChecker"""
    global _checker
    if _checker is None:
        _checker = PrometheusChecker()
    return _checker
