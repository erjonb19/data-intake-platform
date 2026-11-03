# Stage 11 proof: the platform drills with the incident agent live

Run cap $0.15 (each drill's chain $0.0375); every input synthetic.

| Drill | Incident | Alert seen | Root cause (expected) | Model calls | Cost | Trace spans | Closed after undo | Result |
|---|---|---|---|---|---|---|---|---|
| scheduler_down | 1 PLATFORM.SCHEDULER_DOWN `scheduler` | AirflowSchedulerDown: yes | platform_scheduler (platform_scheduler) | 5 | $0.0050 | 11 | yes | ok |
| oom_task | 2 TASK.OOMKILLED `drill_oom.eat_memory` | TaskPodOOMKilled: yes | platform_resources (platform_resources) | 6 | $0.0052 | 14 | yes | ok |
| bad_secret | 3 DAG.FAILED `intake__healthcare__eligibility` | DagRunFailing: yes | platform_credentials (platform_credentials) | 7 | $0.0056 | 13 | yes | ok |
| stuck_queued | 4 TASK.STUCK_QUEUED `drill_queue.wait_for_room` | TaskQueuedTooLong: yes | platform_capacity (platform_capacity) | 5 | $0.0044 | 11 | yes | ok |

Model spend, all drills: $0.0201 (metered, ops.model_call).
