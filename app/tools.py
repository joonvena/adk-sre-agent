"""Custom tools for the SRE diagnosis agent."""

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from google.cloud import logging_v2, monitoring_v3, pubsub_v1
from google.protobuf.timestamp_pb2 import Timestamp

from .config import config

logger = logging.getLogger(__name__)

_log_client: logging_v2.Client | None = None
_monitoring_client: monitoring_v3.MetricServiceClient | None = None
_publisher_client: pubsub_v1.PublisherClient | None = None


def _get_log_client() -> logging_v2.Client:
    global _log_client
    if _log_client is None:
        _log_client = logging_v2.Client(project=config.default_project)
    return _log_client


def _get_monitoring_client() -> monitoring_v3.MetricServiceClient:
    global _monitoring_client
    if _monitoring_client is None:
        _monitoring_client = monitoring_v3.MetricServiceClient()
    return _monitoring_client


def _get_publisher() -> pubsub_v1.PublisherClient:
    global _publisher_client
    if _publisher_client is None:
        _publisher_client = pubsub_v1.PublisherClient()
    return _publisher_client


def _timestamp_to_proto(iso_string: str) -> Timestamp:
    """Convert ISO string to protobuf Timestamp."""
    dt = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
    ts = Timestamp()
    ts.FromDatetime(dt)
    return ts


def query_cloud_logging(
    project: str,
    filter: str,
    start_time: str,
    end_time: str,
    size: int = 20,
) -> dict[str, Any]:
    """Query Cloud Logging for log entries matching the given filter.

    Args:
        project: The GCP project ID to query logs from.
        filter: Cloud Logging filter expression (e.g. 'severity>=ERROR AND resource.type="cloud_run_revision"').
        start_time: Start of the time window in ISO 8601 format (e.g. '2024-01-15T10:00:00Z').
        end_time: End of the time window in ISO 8601 format.
        size: Maximum number of log entries to return (default 20, max 100).

    Returns:
        A dict with 'status' and 'entries' keys. Each entry contains timestamp, severity,
        resource info, and log body.
    """
    size = min(size, 100)
    max_window = timedelta(minutes=config.max_time_window_minutes)

    try:
        start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
        if (end_dt - start_dt) > max_window:
            start_dt = end_dt - max_window
    except (ValueError, AttributeError):
        return {
            "status": "error",
            "error": f"Invalid time format. Use ISO 8601 (e.g. '2024-01-15T10:00:00Z'). Got start_time={start_time}, end_time={end_time}",
        }

    full_filter = f'({filter}) AND timestamp>="{start_dt.isoformat()}" AND timestamp<="{end_dt.isoformat()}"'

    try:
        client = _get_log_client()
        entries = []
        for entry in client.list_entries(
            resource_names=[f"projects/{project}"],
            filter_=full_filter,
            order_by="timestamp desc",
            max_results=size,
        ):
            body = {}
            if entry.json_payload is not None:
                body = entry.json_payload
            elif entry.text_payload:
                body = {"message": entry.text_payload[:2000]}
            elif entry.proto_payload:
                body = {"proto_payload": str(entry.proto_payload)[:2000]}

            entries.append(
                {
                    "timestamp": entry.timestamp.isoformat()
                    if entry.timestamp
                    else None,
                    "severity": entry.severity.name if entry.severity else None,
                    "resource_type": entry.resource.type if entry.resource else None,
                    "resource_labels": dict(entry.resource.labels)
                    if entry.resource
                    else {},
                    "log_name": entry.log_name,
                    "body": body,
                }
            )

        return {"status": "success", "count": len(entries), "entries": entries}

    except Exception as e:
        logger.exception("Failed to query Cloud Logging")
        return {"status": "error", "error": str(e)}


def query_cloud_monitoring(
    project: str,
    metric: str,
    start_time: str,
    end_time: str,
    filter: str = "",
) -> dict[str, Any]:
    """Query Cloud Monitoring for time-series metric data.

    Args:
        project: The GCP project ID to query metrics from.
        metric: The metric type (e.g. 'run.googleapis.com/http5xx_ratio', 'kubernetes.io/pods/container/cpu/usage_per_second').
        start_time: Start of the time window in ISO 8601 format.
        end_time: End of the time window in ISO 8601 format.
        filter: Optional additional Monitoring filter expression (e.g. 'resource.labels.service_name="my-service"').

    Returns:
        A dict with 'status' and 'series' keys. Each series contains metric name,
        resource labels, and data points with timestamps and values.
    """
    max_window = timedelta(minutes=config.max_time_window_minutes)

    try:
        start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
        if (end_dt - start_dt) > max_window:
            start_dt = end_dt - max_window
    except (ValueError, AttributeError):
        return {
            "status": "error",
            "error": f"Invalid time format. Use ISO 8601. Got start_time={start_time}, end_time={end_time}",
        }

    metric_filter = f'metric.type = "{metric}"'
    if filter:
        metric_filter = f"({metric_filter} AND {filter})"

    try:
        client = _get_monitoring_client()
        name = f"projects/{project}"
        start_ts = _timestamp_to_proto(start_dt.isoformat())
        end_ts = _timestamp_to_proto(end_dt.isoformat())

        series_data = []
        response = client.list_time_series(
            name=name,
            filter=metric_filter,
            interval={"start_time": start_ts, "end_time": end_ts},
            view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
        )

        for series in response:
            data_points = []
            for point in series.points:
                value = {
                    "int64_value": None,
                    "double_value": None,
                    "bool_value": None,
                    "distribution_value": None,
                }
                if point.HasField("int64_value"):
                    value["int64_value"] = point.int64_value
                elif point.HasField("double_value"):
                    value["double_value"] = point.double_value
                elif point.HasField("bool_value"):
                    value["bool_value"] = point.bool_value
                elif point.HasField("distribution_value"):
                    d = point.distribution_value
                    value["distribution_value"] = {
                        "count": d.count,
                        "mean": d.mean,
                        "standard_deviation": d.standard_deviation,
                    }

                data_points.append(
                    {
                        "timestamp": point.start_time.isoformat()
                        if point.start_time
                        else None,
                        "value": value,
                    }
                )

            series_data.append(
                {
                    "metric": series.metric.type,
                    "labels": dict(series.metric.labels),
                    "resource": dict(series.resource.labels) if series.resource else {},
                    "points": data_points[:100],
                }
            )

        return {"status": "success", "count": len(series_data), "series": series_data}

    except Exception as e:
        logger.exception("Failed to query Cloud Monitoring")
        return {"status": "error", "error": str(e)}


def list_available_metrics(
    project: str,
    filter_prefix: str = "",
) -> dict[str, Any]:
    """List available metric descriptors for a project, optionally filtered by name prefix.

    Use this to discover what metrics are available for a service before querying.
    For example, filter_prefix='run.googleapis.com' lists all Cloud Run metrics.

    Args:
        project: The GCP project ID.
        filter_prefix: Optional prefix to filter metric names (e.g. 'run.googleapis.com', 'kubernetes.io', 'cloudfunctions').

    Returns:
        A dict with 'status' and 'metrics' keys. Each metric contains name, description,
        and value type.
    """
    try:
        from google.api import metric_pb2
        from google.cloud.monitoring_v3.types import metric_service

        client = _get_monitoring_client()
        name = f"projects/{project}"

        filter_expr = ""
        if filter_prefix:
            filter_expr = f'metric.type = "{filter_prefix}"*'

        metrics = []
        request = metric_service.ListMetricDescriptorsRequest(name=name)
        if filter_expr:
            request.filter = filter_expr
        response = client.list_metric_descriptors(request=request)
        for md in response:
            metrics.append(
                {
                    "name": md.name,
                    "display_name": md.display_name,
                    "description": (md.description or "")[:200],
                    "metric_type": md.type,
                    "value_type": metric_pb2.MetricDescriptor.ValueType.Name(md.value_type),
                    "unit": md.unit,
                }
            )

        return {"status": "success", "count": len(metrics), "metrics": metrics[:50]}

    except Exception as e:
        logger.exception("Failed to list metric descriptors")
        return {"status": "error", "error": str(e)}


def publish_diagnosis(
    topic: str,
    alert_id: str,
    project: str,
    service: str,
    severity: str,
    root_cause: str,
    evidence: list[str],
    suggested_actions: list[str],
    confidence: str,
) -> dict[str, Any]:
    """Publish a structured diagnosis report to a Pub/Sub topic.

    Args:
        topic: The Pub/Sub topic name to publish to (e.g. 'sre-diagnoses').
        alert_id: The ID of the alert being diagnosed.
        project: The GCP project the alert is about.
        service: The service that triggered the alert.
        severity: The alert severity (critical, error, warning, info).
        root_cause: The identified root cause of the issue.
        evidence: List of evidence strings (log snippets, metric observations) supporting the diagnosis.
        suggested_actions: List of recommended remediation actions.
        confidence: Confidence level in the diagnosis (high, medium, low).

    Returns:
        A dict with 'status' and 'message_id' of the published message.
    """
    if not topic:
        topic = config.diagnosis_output_topic
    if not topic:
        return {
            "status": "error",
            "error": "No output topic configured. Set DIAGNOSIS_OUTPUT_TOPIC env var or pass topic explicitly.",
        }

    diagnosis = {
        "alert_id": alert_id,
        "project": project,
        "service": service,
        "severity": severity,
        "root_cause": root_cause,
        "evidence": evidence,
        "suggested_actions": suggested_actions,
        "confidence": confidence,
        "diagnosed_at": datetime.now(UTC).isoformat(),
    }

    try:
        publisher = _get_publisher()
        project_id = project or config.default_project
        topic_path = f"projects/{project_id}/topics/{topic}"

        data = json.dumps(diagnosis).encode("utf-8")
        future = publisher.publish(topic_path, data)

        def _on_error(error: Exception) -> None:
            logger.error("Failed to publish diagnosis: %s", error)

        future.add_callback(_on_success)
        future.add_done_callback(
            lambda f: _on_error(f.exception()) if f.exception() else None
        )

        message_id = future.result(timeout=30)
        return {
            "status": "success",
            "message_id": message_id,
            "topic": topic_path,
            "diagnosis": diagnosis,
        }

    except Exception as e:
        logger.exception("Failed to publish diagnosis to Pub/Sub")
        return {"status": "error", "error": str(e), "diagnosis": diagnosis}


def _on_success(message_id: str) -> None:
    logger.info("Diagnosis published to Pub/Sub, message_id=%s", message_id)
