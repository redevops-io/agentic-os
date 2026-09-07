-- Car-diagnosis storage/analytics schema for Apache Doris 4.x (proxmox k3s, MySQL proto :9030 / NodePort 30930).
-- Append-only history tables are monthly-partitioned so cold months can tier to S3 (see 002_s3_cold_tier.sql).
-- Fractional fields are DECIMAL for analytics; text/JSON as STRING. `procedure` is reserved → repair_procedure.

CREATE DATABASE IF NOT EXISTS car_diagnosis;

-- one row per driver+vehicle case (upsert)
CREATE TABLE IF NOT EXISTS car_diagnosis.cases (
  case_id VARCHAR(64) NOT NULL, participant_ref VARCHAR(128), vin VARCHAR(17),
  make VARCHAR(64), model VARCHAR(128), year VARCHAR(8), powertrain VARCHAR(16),
  status VARCHAR(32), created_at DATETIME, updated_at DATETIME
) ENGINE=OLAP UNIQUE KEY(case_id) DISTRIBUTED BY HASH(case_id) BUCKETS 4
PROPERTIES("replication_num"="1");

-- append-only multimodal conversation log (monthly partitions, tierable to S3)
CREATE TABLE IF NOT EXISTS car_diagnosis.interaction_events (
  dt DATE NOT NULL, case_id VARCHAR(64) NOT NULL, interaction_id VARCHAR(96) NOT NULL,
  ts DATETIME, channel VARCHAR(24), modality VARCHAR(16), text STRING,
  artifact_ref VARCHAR(96), transcript STRING, provenance VARCHAR(64)
) ENGINE=OLAP DUPLICATE KEY(dt, case_id, interaction_id) PARTITION BY RANGE(dt)()
DISTRIBUTED BY HASH(case_id) BUCKETS 4
PROPERTIES("replication_num"="1","dynamic_partition.enable"="true","dynamic_partition.time_unit"="MONTH",
  "dynamic_partition.start"="-60","dynamic_partition.end"="1","dynamic_partition.prefix"="p","dynamic_partition.buckets"="4");

-- machine-read observations (DTCs / PIDs / freeze frames)
CREATE TABLE IF NOT EXISTS car_diagnosis.observations (
  case_id VARCHAR(64) NOT NULL, evidence_id VARCHAR(96) NOT NULL, kind VARCHAR(24),
  code VARCHAR(16), value STRING, unit VARCHAR(24), source VARCHAR(32), observed_at DATETIME
) ENGINE=OLAP DUPLICATE KEY(case_id, evidence_id) DISTRIBUTED BY HASH(case_id) BUCKETS 4
PROPERTIES("replication_num"="1");

-- ranked, replayable diagnosis trail (monthly partitions, tierable)
CREATE TABLE IF NOT EXISTS car_diagnosis.diagnoses (
  dt DATE NOT NULL, case_id VARCHAR(64) NOT NULL, diagnosis_id VARCHAR(96) NOT NULL,
  top_cause VARCHAR(256), top_confidence DECIMAL(5,4), safety_gate_passed BOOLEAN,
  needs_more_evidence BOOLEAN, hypotheses_json STRING, created_at DATETIME
) ENGINE=OLAP DUPLICATE KEY(dt, case_id, diagnosis_id) PARTITION BY RANGE(dt)()
DISTRIBUTED BY HASH(case_id) BUCKETS 4
PROPERTIES("replication_num"="1","dynamic_partition.enable"="true","dynamic_partition.time_unit"="MONTH",
  "dynamic_partition.start"="-60","dynamic_partition.end"="1","dynamic_partition.prefix"="p","dynamic_partition.buckets"="4");

-- the flywheel: confirmed repair outcomes (upsert)
CREATE TABLE IF NOT EXISTS car_diagnosis.outcomes (
  outcome_id VARCHAR(96) NOT NULL, case_id VARCHAR(64), diagnosis_id VARCHAR(96),
  diagnosis VARCHAR(256), repair_procedure VARCHAR(256), part VARCHAR(256),
  labor_hours DECIMAL(6,2), cost DECIMAL(10,2), fixed BOOLEAN, provenance VARCHAR(32), verified_at DATETIME
) ENGINE=OLAP UNIQUE KEY(outcome_id) DISTRIBUTED BY HASH(outcome_id) BUCKETS 4
PROPERTIES("replication_num"="1");

-- evidence lake (NHTSA TSBs/complaints, MechanicDB) with a vector ANN index for RAG (dim=1024, bge-m3)
CREATE TABLE IF NOT EXISTS car_diagnosis.knowledge (
  doc_id VARCHAR(96) NOT NULL, source VARCHAR(32), dtc VARCHAR(16),
  make VARCHAR(64), model VARCHAR(128), year VARCHAR(8), title STRING, body STRING,
  embedding ARRAY<FLOAT> NOT NULL,
  INDEX idx_emb (embedding) USING ANN PROPERTIES("index_type"="hnsw","metric_type"="l2_distance","dim"="1024")
) ENGINE=OLAP DUPLICATE KEY(doc_id) DISTRIBUTED BY HASH(doc_id) BUCKETS 4
PROPERTIES("replication_num"="1");
