# Monitoring

Logs: every service writes one JSON object per line to stdout, with secrets redacted. On ECS, the awslogs driver sends this to `/aorts/<service>`, which keeps 1 week of logs.

Useful CloudWatch Logs Insights queries:
```
fields ts, service, action, decision, reason, transaction_id | filter ispresent(action) | sort ts desc | limit 100
fields ts, msg, code, reason | filter msg = "operation rejected" | stats count() by code
```

Metrics: with `AORTS_EMF_ENABLED=true`, every service prints a CloudWatch Embedded Metric Format document once per second (namespace `AORTS`, dimension `Service`). The alarms use stable, unlabelled counters such as `check_rejected_total` and `auth_failures_total`.

Alarms (defined in `cloud/cdk/aorts_stack.py`, all notifying the `Alarms` SNS topic):

| Alarm | Signal |
|---|---|
| DlqNotEmpty | the dispatch DLQ has visible messages |
| DispatchBacklogOld | the oldest SQS message is more than 120 s old, for 3 minutes |
| Alb5xx / AlbLatency | target 5xx count > 10 in 5 min; p95 response time > 2 s |
| RemitterUnhealthy | ALB unhealthy host count > 0, for 3 minutes |
| RdsCpu, Cpu-<service> | CPU > 80/85 % |
| StatusCheckFlood | switch `check_rejected_total` > 300/min, for 2 minutes |
| AuthFailures | remitter `auth_failures_total` > 20 in 5 min |

Locally, the Streamlit dashboard (http://127.0.0.1:8501) and `GET /api/v1/metrics/summary` on the control service provide the same information.
