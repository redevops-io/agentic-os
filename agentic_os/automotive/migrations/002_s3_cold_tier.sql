-- S3 cold-tiering for the append-only history tables: hot data stays on Doris BE local disk; partitions
-- older than the cooldown TTL migrate to an S3 datalake automatically, keeping the cluster small while
-- retaining the full evidence/outcome graph queryably in S3.
--
-- Apply AFTER 001, and ONLY with real S3 credentials — DO NOT commit secrets. Fill the placeholders from
-- Vault/env (e.g. an S3/MinIO/Backblaze-B2 S3-compatible bucket). Placeholders: <S3_ENDPOINT> <S3_REGION>
-- <S3_ACCESS_KEY> <S3_SECRET_KEY> <S3_BUCKET> <S3_PREFIX>.

CREATE RESOURCE IF NOT EXISTS "car_s3_cold"
PROPERTIES (
  "type" = "s3",
  "s3.endpoint" = "<S3_ENDPOINT>",
  "s3.region"   = "<S3_REGION>",
  "s3.access_key" = "<S3_ACCESS_KEY>",
  "s3.secret_key" = "<S3_SECRET_KEY>",
  "s3.bucket"   = "<S3_BUCKET>",
  "s3.root.path" = "<S3_PREFIX>/car_diagnosis"
);

-- cool partitions to S3 after 90 days (7776000s). Tune per retention policy.
CREATE STORAGE POLICY IF NOT EXISTS car_cold_90d
PROPERTIES ("storage_resource" = "car_s3_cold", "cooldown_ttl" = "7776000");

-- attach the policy to the append-heavy, time-partitioned tables
ALTER TABLE car_diagnosis.interaction_events SET ("storage_policy" = "car_cold_90d");
ALTER TABLE car_diagnosis.diagnoses          SET ("storage_policy" = "car_cold_90d");

-- (cases/outcomes are small and hot; knowledge is reference data — left on local storage.)
