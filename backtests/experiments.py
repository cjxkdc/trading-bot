"""Archive research evidence without importing a platform or ranking a winner."""
from datetime import datetime, timezone
import ast
from hashlib import sha256
import json
from pathlib import Path
from framework.scheduling import schedule_config


def read_json(path):
    def reject_constant(value):
        raise ValueError("Non-finite JSON value: " + value)

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key: " + key)
            result[key] = value
        return result
    return json.loads(Path(path).read_text(encoding="utf-8-sig"),
                      parse_constant=reject_constant, object_pairs_hook=unique_keys)


def source_hash(path):
    # Existing Windows exports used CRLF; newline style is not an algorithm edit.
    return sha256(Path(path).read_text(encoding="utf-8-sig").encode("utf-8")).hexdigest()


def file_hash(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def validate_export(folder, manifest_name="manifest.json", code_name="main.py"):
    folder = Path(folder)
    manifest = read_json(folder / manifest_name)
    if manifest.get("schema_version") != 1 or manifest.get("status") != "exported_not_run":
        raise ValueError("Unsupported experiment manifest")
    if source_hash(folder / code_name) != manifest["main_sha256"]:
        raise ValueError("Exported source changed after the experiment was frozen")
    tree = ast.parse((folder / code_name).read_text(encoding="utf-8-sig"))
    configs = [ast.literal_eval(node.value) for node in tree.body
               if isinstance(node, ast.Assign) and any(
                   isinstance(target, ast.Name) and target.id == "_RUN_CONFIG" for target in node.targets)]
    if configs != [manifest["configuration"]]:
        raise ValueError("Manifest configuration differs from the frozen source")
    return manifest


def _write(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False, allow_nan=False)


def import_result(experiment, result_path, parser, *, run_code=None):
    experiment, result_path = Path(experiment), Path(result_path)
    manifest = validate_export(experiment)
    normalized = parser(read_json(result_path))
    cfg = manifest["configuration"]
    warnings = []
    observed = normalized["period"]
    for key in ("start", "end"):
        if observed[key] is not None and observed[key] != cfg["backtest"][key]:
            raise ValueError("QC result period differs from the exported configuration")
    if None in observed.values():
        warnings.append("Historical dates missing in result; shown dates are export assumptions")
    if run_code is not None and source_hash(run_code) != manifest["main_sha256"]:
        raise ValueError("Code downloaded from the run differs from this experiment")
    evidence = "supplied_code_matches_export" if run_code else "source_unverified"
    if not run_code:
        warnings.append("No run code supplied; association with this export is user-declared")
    else:
        warnings.append("Code copy matches; no online verification of its association with this result")
    if normalized["completion"] != "reported_complete":
        warnings.append("Backtest completion is " + normalized["completion"])
    checksum = file_hash(result_path)
    # Include evidence inputs so adding a run-code copy creates a new import.
    import_id = sha256((checksum + evidence).encode()).hexdigest()[:16]
    out = experiment / ("result-" + import_id)
    out.mkdir(exist_ok=False)
    copies = {"raw-result.json": result_path, "export-main.py": experiment / "main.py",
              "export-manifest.json": experiment / "manifest.json"}
    if run_code:
        copies["run-main.py"] = Path(run_code)
    for name, source in copies.items():
        (out / name).write_bytes(source.read_bytes())
    record = {
        "schema_version": 1, "imported_at": datetime.now(timezone.utc).isoformat(),
        "experiment": experiment.name, "configuration": cfg,
        "code_evidence": evidence, "warnings": warnings, "result": normalized,
        "files": {name: file_hash(out / name) for name in copies},
    }
    _write(out / "summary.json", record)
    # The immutable export manifest still describes an export, not a successful run.
    return out


def verified_record(folder):
    folder = Path(folder)
    record = read_json(folder / "summary.json")
    if record.get("schema_version") != 1:
        raise ValueError("Unsupported experiment result version")
    files = record.get("files", {})
    required = {"raw-result.json", "export-main.py", "export-manifest.json"}
    if not required <= set(files) or set(files) - required - {"run-main.py"}:
        raise ValueError("Invalid experiment evidence files")
    for name, checksum in files.items():
        if file_hash(folder / name) != checksum:
            raise ValueError("Experiment evidence changed: " + name)
    manifest = validate_export(folder, "export-manifest.json", "export-main.py")
    if manifest["configuration"] != record["configuration"]:
        raise ValueError("Experiment manifest or configuration changed")
    if "run-main.py" in files and source_hash(folder / "run-main.py") != manifest["main_sha256"]:
        raise ValueError("Run source differs from the frozen export")
    expected = "supplied_code_matches_export" if "run-main.py" in files else "source_unverified"
    if record["code_evidence"] != expected:
        raise ValueError("Code evidence label differs from the archived files")
    return record


def compare_results(folders, parser, out):
    rows = []
    folders = list(folders)
    if len(folders) < 2:
        raise ValueError("Choose at least two result directories to compare")
    if len({Path(p).resolve() for p in folders}) != len(folders):
        raise ValueError("Duplicate comparison inputs")
    for folder in folders:
        record = verified_record(folder)
        # Reparse raw evidence instead of trusting an editable summary metric.
        if parser(read_json(Path(folder) / "raw-result.json")) != record["result"]:
            raise ValueError("Result summary no longer matches raw evidence")
        cfg, result = record["configuration"], record["result"]
        rows.append({"experiment": record["experiment"], "result_dir": str(Path(folder).resolve()),
                     "strategy": cfg["strategy"], "start": cfg["backtest"]["start"],
                     "end": cfg["backtest"]["end"], "initial_cash": cfg["initial_cash"],
                     "slippage_bps": cfg["backtest"]["slippage_bps"], "fees": cfg["fees"],
                     "schedule": schedule_config(cfg.get('schedule', {'initial_entry': 'scheduled'})),
                     "benchmark": cfg.get('benchmark', 'QQQ'),
                     "completion": result["completion"], "code_evidence": record["code_evidence"],
                     "warnings": record["warnings"], **result["metrics"]})
    differences = [key for key in ("start", "end", "initial_cash", "slippage_bps", "fees", "schedule", "benchmark")
                   if any(row[key] != rows[0][key] for row in rows[1:])]
    report = {"schema_version": 1, "comparison_differences": differences, "rows": rows,
              "conclusion": "Descriptive comparison only; no strategy is automatically approved for trading"}
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    _write(out / "comparison.json", report)

    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")

    def percent(value):
        return "unknown" if value is None else f"{value:.2%}"

    lines = ["# 回测比较", "", "日期、成本和初始资金取自导出配置；核验状态见表格及 JSON。",
             "不同区间不能直接按收益高低选优；成本差异可用于压力测试。", "",
             "条件差异：" + (", ".join(differences) or "日期、资金、成本相同"), "",
             "| 实验 | 区间 | 滑点 bps | 每单费用 | 总收益 | 年化收益 | 最大回撤 | Sharpe | 订单数 | 完成状态 | 代码证据 |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|"]
    for row in rows:
        values = [row["experiment"], row["start"] + " → " + row["end"], row["slippage_bps"],
                  row["fees"]["per_order"], percent(row["total_return"]),
                  percent(row["annual_return"]), percent(row["max_drawdown"]), row["sharpe"],
                  row["total_orders"], row["completion"], row["code_evidence"]]
        lines.append("| " + " | ".join(cell(v) for v in values) + " |")
    lines += ["", "代码副本匹配不是在线来源认证。缺失值不按零处理；这里不会认定任何策略可以实盘。", ""]
    for row in rows:
        lines.append("- " + cell(row["experiment"]) + ": " + "; ".join(row["warnings"]))
    (out / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return report
