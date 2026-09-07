-- S3 cold-tiering for the append-only history tables: hot data stays on Doris BE local disk; monthly
-- partitions older than the cooldown TTL migrate to the S3 datalake automatically, keeping the cluster
-- small while retaining the full evidence/outcome graph queryably in S3.
--
-- APPLIED on the proxmox cluster against the in-cluster MinIO (bucket redevops-datalake). Credentials
-- come from the k8s secret `minio-credentials` (keys AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY) —
-- resolve them at apply time, DO NOT commit secrets:
--   AK=$(kubectl -n minio get secret minio-credentials -o jsonpath='{.data.AWS_ACCESS_KEY_ID}'     | base64 -d)
--   SK=$(kubectl -n minio get secret minio-credentials -o jsonpath='{.data.AWS_SECRET_ACCESS_KEY}' | base64 -d)
-- then substitute $AK/$SK below and pipe to the Doris FE:
--   kubectl -n doris exec -i doris-fe-0 -- mysql -h127.0.0.1 -P9030 -uroot < 002_s3_cold_tier.sql
--
-- MinIO requires PATH-STYLE addressing: the flag is `use_path_style` (NOT `s3.use_path_style`, which
-- Doris silently ignores → it tries virtual-host `bucket.endpoint` and fails DNS).

CREATE RESOURCE IF NOT EXISTS car_s3_cold PROPERTIES (
  "type" = "s3",
  "s3.endpoint" = "http://minio.minio.svc.cluster.local:9000",   -- in-cluster MinIO S3 API
  "s3.region" = "us-east-1",                                      -- MinIO ignores it; Doris requires a value
  "s3.access_key" = "$AK",
  "s3.secret_key" = "$SK",
  "s3.bucket" = "redevops-datalake",
  "s3.root.path" = "doris/car_diagnosis",
  "use_path_style" = "true"                                       -- REQUIRED for MinIO
);

-- cool partitions to S3 after 90 days (7776000s). Tune per retention policy.
CREATE STORAGE POLICY IF NOT EXISTS car_cold_90d
PROPERTIES ("storage_resource" = "car_s3_cold", "cooldown_ttl" = "7776000");

-- attach to the append-heavy, time-partitioned tables (existing + future dynamic partitions inherit it)
ALTER TABLE car_diagnosis.interaction_events SET ("storage_policy" = "car_cold_90d");
ALTER TABLE car_diagnosis.diagnoses          SET ("storage_policy" = "car_cold_90d");

-- verify (safe — no creds): SHOW STORAGE POLICY;  SHOW PARTITIONS FROM car_diagnosis.interaction_events\G
-- (cases/outcomes are small and hot; knowledge is reference data — left on local storage.)
