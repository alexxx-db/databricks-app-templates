"""Load MIMIC-IV or MIMIC-III into a common schema in Unity Catalog and grant the app access.

Datasets (--dataset):
  mimic-iv-demo   Open (ODbL), 100 patients, downloaded from PhysioNet. Default.
  mimic-iii-demo  Open (ODbL), 100 patients, downloaded from PhysioNet.
  mimic-iv        Credentialed. --source-path = your copy (folder containing hosp/ and icu/).
  mimic-iii       Credentialed. --source-path = your copy (folder containing PATIENTS.csv.gz, ...).

Clinical notes are credentialed-only and must come from the same family as the
structured data (MIMIC-III and MIMIC-IV use different patient IDs):
  mimic-iv  + --notes-path = MIMIC-IV-Note folder containing discharge.csv.gz (and optionally radiology.csv.gz)
  mimic-iii + notes are read from NOTEEVENTS in --source-path

No MIMIC data is stored in this repository.
"""

import argparse
import os
import urllib.request
from pathlib import Path

DEMO_URLS = {
    "mimic-iv-demo": "https://physionet.org/files/mimic-iv-demo/2.2/",
    "mimic-iii-demo": "https://physionet.org/files/mimiciii-demo/1.4/",
}
# Raw files each family needs, relative to the dataset root (extension resolved at read time).
FILES = {
    "iv": ["hosp/patients", "hosp/admissions", "hosp/diagnoses_icd", "hosp/d_icd_diagnoses", "hosp/labevents",
           "hosp/d_labitems", "hosp/prescriptions", "icu/icustays"],
    "iii": ["PATIENTS", "ADMISSIONS", "DIAGNOSES_ICD", "D_ICD_DIAGNOSES", "LABEVENTS", "D_LABITEMS",
            "PRESCRIPTIONS", "ICUSTAYS"],
}
LICENSES = {
    "mimic-iv-demo": "MIMIC-IV Clinical Database Demo v2.2, ODbL v1.0. https://doi.org/10.13026/dp1f-ex47",
    "mimic-iii-demo": "MIMIC-III Clinical Database Demo v1.4, ODbL v1.0. https://doi.org/10.13026/C2HM2Q",
    "mimic-iv": "MIMIC-IV, PhysioNet Credentialed Health Data License 1.5.0",
    "mimic-iii": "MIMIC-III, PhysioNet Credentialed Health Data License 1.5.0",
}


def family(dataset: str) -> str:
    return "iv" if dataset.startswith("mimic-iv") else "iii"


def download_demo(dataset: str, dest: str) -> str:
    """Fetch the open demo files into dest (e.g. a UC volume path). Skips files already present."""
    base = DEMO_URLS[dataset]
    ext = ".csv.gz" if family(dataset) == "iv" else ".csv"
    for rel in FILES[family(dataset)]:
        target = Path(dest) / f"{rel}{ext}"
        if target.exists() and target.stat().st_size > 0:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        with urllib.request.urlopen(base + f"{rel}{ext}", timeout=120) as resp, open(tmp, "wb") as out:
            while chunk := resp.read(1 << 20):
                out.write(chunk)
        os.replace(tmp, target)
    return dest


def read_raw(spark, root: str, rel: str, multiline: bool = False):
    """Read a MIMIC CSV whatever its compression; column names lowercased (MIMIC-III full data is uppercase).

    root is a local or /Volumes/... path.
    """
    for ext in (".csv.gz", ".csv"):
        path = f"{root.rstrip('/')}/{rel}{ext}"
        if os.path.exists(path):
            df = (spark.read.option("header", True).option("escape", '"').option("multiLine", multiline)
                  .csv(path))
            return df.toDF(*[c.lower() for c in df.columns])
    raise FileNotFoundError(f"{rel}.csv(.gz) not found under {root}")


# Common schema, built with Spark SQL over temp views named after the raw tables.
COMMON_SQL = {
    "iv": {
        "patients": """
            SELECT CAST(subject_id AS BIGINT) subject_id, gender, CAST(try_to_timestamp(dod) AS DATE) dod FROM patients""",
        "admissions": """
            SELECT CAST(a.subject_id AS BIGINT) subject_id, CAST(a.hadm_id AS BIGINT) hadm_id,
                   try_to_timestamp(a.admittime) admittime, try_to_timestamp(a.dischtime) dischtime,
                   a.admission_type, a.admission_location, a.discharge_location, a.insurance, a.race,
                   CAST(a.hospital_expire_flag AS INT) hospital_expire_flag,
                   least(CAST(p.anchor_age AS INT) + year(try_to_timestamp(a.admittime)) - CAST(p.anchor_year AS INT), 90) age,
                   round((unix_timestamp(try_to_timestamp(a.dischtime)) - unix_timestamp(try_to_timestamp(a.admittime))) / 86400, 2) los_days
            FROM admissions a JOIN patients p ON a.subject_id = p.subject_id""",
        "diagnoses": """
            SELECT CAST(d.subject_id AS BIGINT) subject_id, CAST(d.hadm_id AS BIGINT) hadm_id,
                   CAST(d.seq_num AS INT) seq_num, d.icd_code, CAST(d.icd_version AS INT) icd_version, dd.long_title
            FROM diagnoses_icd d LEFT JOIN d_icd_diagnoses dd
              ON d.icd_code = dd.icd_code AND d.icd_version = dd.icd_version""",
        "labs": """
            SELECT CAST(l.subject_id AS BIGINT) subject_id, CAST(l.hadm_id AS BIGINT) hadm_id,
                   try_to_timestamp(l.charttime) charttime, CAST(l.itemid AS INT) itemid, i.label, i.fluid, i.category,
                   try_cast(l.valuenum AS DOUBLE) valuenum, l.valueuom, l.flag
            FROM labevents l LEFT JOIN d_labitems i ON l.itemid = i.itemid""",
        "prescriptions": """
            SELECT CAST(subject_id AS BIGINT) subject_id, CAST(hadm_id AS BIGINT) hadm_id,
                   try_to_timestamp(starttime) starttime, drug, dose_val_rx, dose_unit_rx, route FROM prescriptions""",
        "icustays": """
            SELECT CAST(subject_id AS BIGINT) subject_id, CAST(hadm_id AS BIGINT) hadm_id,
                   CAST(stay_id AS BIGINT) stay_id, first_careunit, try_to_timestamp(intime) intime,
                   try_to_timestamp(outtime) outtime, try_cast(los AS DOUBLE) los FROM icustays""",
    },
    "iii": {
        "patients": """
            SELECT CAST(subject_id AS BIGINT) subject_id, gender, CAST(try_to_timestamp(dod) AS DATE) dod FROM patients""",
        "admissions": """
            SELECT CAST(a.subject_id AS BIGINT) subject_id, CAST(a.hadm_id AS BIGINT) hadm_id,
                   try_to_timestamp(a.admittime) admittime, try_to_timestamp(a.dischtime) dischtime,
                   a.admission_type, a.admission_location, a.discharge_location, a.insurance, a.ethnicity race,
                   CAST(a.hospital_expire_flag AS INT) hospital_expire_flag,
                   -- MIMIC-III shifts ages over 89 to ~300 years; cap at 90 (meaning 90+)
                   least(CAST(floor(months_between(try_to_timestamp(a.admittime), try_to_timestamp(p.dob)) / 12) AS INT), 90) age,
                   round((unix_timestamp(try_to_timestamp(a.dischtime)) - unix_timestamp(try_to_timestamp(a.admittime))) / 86400, 2) los_days
            FROM admissions a JOIN patients p ON a.subject_id = p.subject_id""",
        "diagnoses": """
            SELECT CAST(d.subject_id AS BIGINT) subject_id, CAST(d.hadm_id AS BIGINT) hadm_id,
                   CAST(d.seq_num AS INT) seq_num, d.icd9_code icd_code, 9 icd_version, dd.long_title
            FROM diagnoses_icd d LEFT JOIN d_icd_diagnoses dd ON d.icd9_code = dd.icd9_code""",
        "labs": """
            SELECT CAST(l.subject_id AS BIGINT) subject_id, CAST(l.hadm_id AS BIGINT) hadm_id,
                   try_to_timestamp(l.charttime) charttime, CAST(l.itemid AS INT) itemid, i.label, i.fluid, i.category,
                   try_cast(l.valuenum AS DOUBLE) valuenum, l.valueuom, l.flag
            FROM labevents l LEFT JOIN d_labitems i ON l.itemid = i.itemid""",
        "prescriptions": """
            SELECT CAST(subject_id AS BIGINT) subject_id, CAST(hadm_id AS BIGINT) hadm_id,
                   try_to_timestamp(startdate) starttime, drug, dose_val_rx, dose_unit_rx, route FROM prescriptions""",
        "icustays": """
            SELECT CAST(subject_id AS BIGINT) subject_id, CAST(hadm_id AS BIGINT) hadm_id,
                   CAST(icustay_id AS BIGINT) stay_id, first_careunit, try_to_timestamp(intime) intime,
                   try_to_timestamp(outtime) outtime, try_cast(los AS DOUBLE) los FROM icustays""",
    },
}

NOTES_SQL = {
    "iv": """
        SELECT note_id, CAST(subject_id AS BIGINT) subject_id, CAST(hadm_id AS BIGINT) hadm_id,
               try_to_timestamp(charttime) charttime, note_type, text FROM raw_notes""",
    "iii": """
        SELECT CAST(row_id AS STRING) note_id, CAST(subject_id AS BIGINT) subject_id, CAST(hadm_id AS BIGINT) hadm_id,
               coalesce(try_to_timestamp(charttime), try_to_timestamp(chartdate)) charttime, category note_type, text
        FROM raw_notes WHERE iserror IS NULL OR iserror != '1'""",
}


def build_tables(spark, dataset: str, root: str, notes_path: str | None = None) -> dict:
    """Return {table: DataFrame} in the common schema."""
    fam = family(dataset)
    for rel in FILES[fam]:
        read_raw(spark, root, rel).createOrReplaceTempView(rel.split("/")[-1].lower())
    tables = {name: spark.sql(q) for name, q in COMMON_SQL[fam].items()}

    notes = None
    if fam == "iv" and notes_path:
        parts = [read_raw(spark, notes_path, n, multiline=True) for n in ("discharge", "radiology")
                 if any(os.path.exists(f"{notes_path.rstrip('/')}/{n}{e}") for e in (".csv.gz", ".csv"))]
        if not parts:
            raise FileNotFoundError(f"No discharge/radiology notes under {notes_path}")
        notes = parts[0].unionByName(parts[1], allowMissingColumns=True) if len(parts) > 1 else parts[0]
    elif fam == "iii" and not dataset.endswith("-demo"):
        notes = read_raw(spark, root, "NOTEEVENTS", multiline=True)
    if notes is not None:
        notes.createOrReplaceTempView("raw_notes")
        tables["notes"] = spark.sql(NOTES_SQL[fam])
    return tables


def main() -> None:
    from pyspark.sql import SparkSession
    from pyspark.sql import functions as F

    p = argparse.ArgumentParser()
    p.add_argument("--catalog", required=True)
    p.add_argument("--schema", required=True)
    p.add_argument("--dataset", default="mimic-iv-demo", choices=list(LICENSES))
    p.add_argument("--source-path", default="", help="credentialed datasets: /Volumes/... folder with your MIMIC files")
    p.add_argument("--notes-path", default="", help="mimic-iv only: MIMIC-IV-Note folder")
    p.add_argument("--app-name", default="")
    args = p.parse_args()

    spark = SparkSession.builder.getOrCreate()
    fqn = f"`{args.catalog}`.`{args.schema}`"
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {fqn}")

    if args.dataset.endswith("-demo"):
        spark.sql(f"CREATE VOLUME IF NOT EXISTS {fqn}.raw")
        root = download_demo(args.dataset, f"/Volumes/{args.catalog}/{args.schema}/raw/{args.dataset}")
    elif not args.source_path:
        raise SystemExit(f"--source-path is required for {args.dataset} (credentialed data you have access to)")
    else:
        root = args.source_path
    if args.notes_path and family(args.dataset) != "iv":
        raise SystemExit("--notes-path is for MIMIC-IV-Note; MIMIC-III notes come from NOTEEVENTS in --source-path")

    tables = build_tables(spark, args.dataset, root, args.notes_path or None)
    spark.sql(f"DROP TABLE IF EXISTS {fqn}.notes")  # don't leave notes from a previous credentialed load
    for name, df in tables.items():
        df.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{fqn}.{name}")
        print(f"{name}: {spark.table(f'{fqn}.{name}').count()} rows")

    spark.createDataFrame([{
        "dataset": args.dataset,
        "credentialed": not args.dataset.endswith("-demo"),
        "has_notes": "notes" in tables,
        "license": LICENSES[args.dataset],
    }]).withColumn("loaded_at", F.current_timestamp()) \
        .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{fqn}.dataset_info")

    if args.app_name:
        from databricks.sdk import WorkspaceClient

        sp = WorkspaceClient().apps.get(args.app_name).service_principal_client_id
        spark.sql(f"GRANT USE CATALOG ON CATALOG `{args.catalog}` TO `{sp}`")
        spark.sql(f"GRANT USE SCHEMA, SELECT ON SCHEMA {fqn} TO `{sp}`")
        print(f"Granted read access to {sp}")


if __name__ == "__main__":
    main()
